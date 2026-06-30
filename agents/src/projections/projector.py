import asyncio
import json
import os
from typing import Any
from uuid import UUID

import asyncpg
from aiokafka import AIOKafkaConsumer
from aiokafka.structs import OffsetAndMetadata

from replay.db import create_db_pool, tenant_transaction
from schema.schema_registry import SchemaRegistry


class SkipEvent(Exception):
    pass


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _parse_event(value: str) -> dict[str, Any]:
    ev = json.loads(value)
    if not isinstance(ev, dict):
        raise ValueError("event is not an object")
    return ev


def _event_fields(ev: dict[str, Any]) -> tuple[UUID, UUID, UUID, str, int, int, dict[str, Any]]:
    tenant_id = UUID(ev["tenantid"])
    event_id = UUID(ev["id"])
    data = ev.get("data") or {}
    aggregate_id = UUID(data.get("aggregate_id") or data.get("aggregateId"))
    event_type = data.get("event_type")
    version_raw = data.get("version")
    if version_raw is None:
        raise SkipEvent("missing data.version")
    version = int(version_raw)
    schema_version = int(data.get("schema_version") or 1)
    payload = data.get("payload") or {}
    if not event_type:
        raise SkipEvent("missing data.event_type")
    return tenant_id, event_id, aggregate_id, str(event_type), schema_version, version, payload


async def _dedupe(conn: asyncpg.Connection, *, tenant_id: UUID, event_id: UUID) -> bool:
    res = await conn.execute(
        """
        INSERT INTO processed_events (tenant_id, event_id)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        tenant_id,
        event_id,
    )
    return res.endswith("1")


async def _lead_current_version(conn: asyncpg.Connection, *, tenant_id: UUID, lead_id: UUID) -> int:
    row = await conn.fetchrow(
        """
        SELECT version
        FROM lead_read_model
        WHERE tenant_id = $1 AND lead_id = $2
        """,
        tenant_id,
        lead_id,
    )
    return int(row["version"]) if row else 0


async def _apply_lead_created(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    lead_id: UUID,
    version: int,
    payload: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO lead_read_model (tenant_id, lead_id, name, email, phone, company, status, score, assigned_to, metadata, version, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,now())
        ON CONFLICT (tenant_id, lead_id)
        DO UPDATE SET
          name = EXCLUDED.name,
          email = EXCLUDED.email,
          phone = EXCLUDED.phone,
          company = EXCLUDED.company,
          status = EXCLUDED.status,
          score = EXCLUDED.score,
          assigned_to = EXCLUDED.assigned_to,
          metadata = EXCLUDED.metadata,
          version = EXCLUDED.version,
          updated_at = now()
        """,
        tenant_id,
        lead_id,
        payload.get("name") or "",
        payload.get("email"),
        payload.get("phone"),
        payload.get("company"),
        payload.get("status") or "new",
        payload.get("score"),
        payload.get("assignedTo") or payload.get("assigned_to"),
        json.dumps(payload.get("metadata") or {}),
        version,
    )


async def _apply_lead_updated(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    lead_id: UUID,
    version: int,
    payload: dict[str, Any],
) -> None:
    changes = payload.get("changes") or payload
    fields = {
        "name": changes.get("name"),
        "email": changes.get("email"),
        "phone": changes.get("phone"),
        "company": changes.get("company"),
        "status": changes.get("status"),
        "score": changes.get("score"),
        "assigned_to": changes.get("assignedTo") or changes.get("assigned_to"),
        "metadata": changes.get("metadata"),
    }

    await conn.execute(
        """
        UPDATE lead_read_model
        SET
          name = COALESCE($3, name),
          email = COALESCE($4, email),
          phone = COALESCE($5, phone),
          company = COALESCE($6, company),
          status = COALESCE($7, status),
          score = COALESCE($8, score),
          assigned_to = COALESCE($9, assigned_to),
          metadata = COALESCE($10::jsonb, metadata),
          version = $11,
          updated_at = now()
        WHERE tenant_id = $1 AND lead_id = $2
        """,
        tenant_id,
        lead_id,
        fields["name"],
        fields["email"],
        fields["phone"],
        fields["company"],
        fields["status"],
        fields["score"],
        fields["assigned_to"],
        json.dumps(fields["metadata"]) if fields["metadata"] is not None else None,
        version,
    )


async def _rebuild_lead_from_event_store(conn: asyncpg.Connection, *, tenant_id: UUID, lead_id: UUID) -> int:
    stream_id = f"lead:{lead_id}"
    rows = await conn.fetch(
        """
        SELECT version, event_type, payload
        FROM events
        WHERE tenant_id = $1 AND stream_id = $2
        ORDER BY version ASC
        """,
        tenant_id,
        stream_id,
    )

    state: dict[str, Any] = {}
    last_version = 0
    for r in rows:
        v = int(r["version"])
        et = r["event_type"]
        payload_val = r["payload"]
        if isinstance(payload_val, str):
            pl = json.loads(payload_val)
        elif isinstance(payload_val, dict):
            pl = payload_val
        else:
            pl = dict(payload_val)

        if et == "lead.created":
            state.update(pl)
        elif et == "lead.updated":
            changes = pl.get("changes") or {}
            state.update(changes)
        last_version = v

    if last_version == 0:
        return 0

    await _apply_lead_created(conn, tenant_id=tenant_id, lead_id=lead_id, version=last_version, payload=state)
    return last_version

async def run_projector() -> None:
    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    kafka_brokers = _env("KAFKA_BROKERS", "localhost:9094")
    topics = [t.strip() for t in _env("PROJECTION_TOPICS", "crm.leads.events,crm.tickets.events").split(",") if t.strip()]
    group_id = _env("PROJECTION_GROUP_ID", "cqrs-projector")
    repo_root = os.environ.get("REPO_ROOT", os.getcwd())
    max_messages = int(os.environ.get("PROJECTOR_MAX_MESSAGES", "0"))
    tenant_filter_raw = os.environ.get("PROJECTOR_TENANT_ID")
    tenant_filter = UUID(tenant_filter_raw) if tenant_filter_raw else None
    registry = SchemaRegistry()
    registry.load_from_repo(root=repo_root)

    pool = await create_db_pool(database_url)
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=kafka_brokers.split(","),
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset=os.environ.get("PROJECTION_OFFSET_RESET", "earliest"),
        value_deserializer=lambda m: m.decode("utf-8"),
    )
    await consumer.start()
    try:
        processed_messages = 0
        while True:
            batch = await consumer.getmany(timeout_ms=500, max_records=200)
            if not batch:
                await asyncio.sleep(0.05)
                continue

            for tp, records in batch.items():
                for msg in records:
                    try:
                        ev = _parse_event(msg.value)
                        tenant_id, event_id, aggregate_id, event_type, schema_version, version, payload = _event_fields(ev)
                        if tenant_filter and tenant_id != tenant_filter:
                            continue
                        registry.validate_payload(event_type=event_type, schema_version=schema_version, payload=payload)
                        async with tenant_transaction(pool, tenant_id) as conn:
                            inserted = await _dedupe(conn, tenant_id=tenant_id, event_id=event_id)
                            if not inserted:
                                continue
                            if event_type.startswith("lead."):
                                current = await _lead_current_version(conn, tenant_id=tenant_id, lead_id=aggregate_id)
                                if version > current + 1:
                                    await _rebuild_lead_from_event_store(conn, tenant_id=tenant_id, lead_id=aggregate_id)
                                elif version == current + 1:
                                    if event_type == "lead.created":
                                        await _apply_lead_created(conn, tenant_id=tenant_id, lead_id=aggregate_id, version=version, payload=payload)
                                    elif event_type == "lead.updated":
                                        await _apply_lead_updated(conn, tenant_id=tenant_id, lead_id=aggregate_id, version=version, payload=payload)
                        processed_messages += 1
                    except SkipEvent:
                        continue
                    except Exception as e:
                        print(f"projector_error: {e}", flush=True)
                        raise

                last_offset = records[-1].offset
                await consumer.commit({tp: OffsetAndMetadata(last_offset + 1, "")})

            if max_messages and processed_messages >= max_messages:
                return
    finally:
        await consumer.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_projector())

