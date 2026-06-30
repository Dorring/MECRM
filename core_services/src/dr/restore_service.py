from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from .metrics import rebuild_duration_seconds, restore_duration_seconds
from .object_store import LocalObjectStore, ObjectStore


@dataclass(frozen=True)
class RestoreReport:
    backup_id: str
    restore_database_seconds: float
    restore_snapshots_seconds: float
    rebuild_read_models_seconds: float
    integrity: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RestoreService:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        object_store: ObjectStore | None = None,
        docker_compose_project_dir: str | None = None,
    ):
        self._pool = pool
        self._object_store = object_store or LocalObjectStore(base_dir=__import__("pathlib").Path("backups"))
        self._docker_compose_project_dir = docker_compose_project_dir

    async def restore_database(self, *, backup_id: str, database_url: str) -> float:
        key = f"dr/{backup_id}/db.sql"
        raw = await self._object_store.get_bytes(key=key)
        t0 = time.perf_counter()
        self._psql_restore(database_url, raw)
        dur = time.perf_counter() - t0
        restore_duration_seconds.labels(type="db").observe(dur)
        return dur

    async def restore_snapshots(self, *, backup_id: str) -> float:
        t0 = time.perf_counter()
        await self._import_jsonl(
            key=f"dr/{backup_id}/aggregate_snapshots.jsonl",
            insert_sql="""
            INSERT INTO aggregate_snapshots (tenant_id, aggregate_type, aggregate_id, version, ts, state, kafka_topic, kafka_partition, kafka_offset)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)
            ON CONFLICT (tenant_id, aggregate_type, aggregate_id, version) DO NOTHING
            """,
            map_row=lambda r: (
                UUID(r["tenant_id"]),
                r["aggregate_type"],
                UUID(r["aggregate_id"]),
                int(r["version"]),
                r["ts"],
                json.dumps(r["state"], separators=(",", ":"), ensure_ascii=False) if isinstance(r["state"], (dict, list)) else r["state"],
                r.get("kafka_topic"),
                int(r.get("kafka_partition") or 0),
                int(r.get("kafka_offset") or 0),
            ),
        )
        await self._import_jsonl(
            key=f"dr/{backup_id}/event_log.jsonl",
            insert_sql="""
            INSERT INTO event_log (tenant_id, aggregate_type, aggregate_id, event_id, event_type, version, ts, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
            ON CONFLICT (tenant_id, aggregate_type, aggregate_id, version) DO NOTHING
            """,
            map_row=lambda r: (
                UUID(r["tenant_id"]),
                r["aggregate_type"],
                UUID(r["aggregate_id"]),
                UUID(r["event_id"]),
                r["event_type"],
                int(r["version"]),
                r["ts"],
                json.dumps(r["payload"], separators=(",", ":"), ensure_ascii=False) if isinstance(r["payload"], (dict, list)) else r["payload"],
            ),
        )
        dur = time.perf_counter() - t0
        restore_duration_seconds.labels(type="snapshots").observe(dur)
        return dur

    async def rebuild_read_models(self) -> float:
        t0 = time.perf_counter()
        await self._rebuild_lead_read_model()
        dur = time.perf_counter() - t0
        rebuild_duration_seconds.labels(model="lead_read_model").observe(dur)
        return dur

    async def validate_integrity(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        async with self._pool.acquire() as conn:
            tenants = await conn.fetch("SELECT id FROM tenants ORDER BY id")

        results: dict[str, Any] = {"validated_at": _now_iso(), "tenants": {}}
        for t in tenants:
            tenant_id = UUID(str(t["id"]))
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                events_count = await conn.fetchval("SELECT count(*) FROM events")
                streams_count = await conn.fetchval("SELECT count(*) FROM event_streams")
                leads_count = await conn.fetchval("SELECT count(*) FROM lead_read_model")
                max_event_ts = await conn.fetchval("SELECT max(created_at) FROM events")
                lead_checksum = await conn.fetchval(
                    """
                    SELECT md5(coalesce(string_agg(lead_id::text || ':' || version::text, ',' ORDER BY lead_id::text), ''))
                    FROM lead_read_model
                    """
                )
            results["tenants"][str(tenant_id)] = {
                "event_streams": int(streams_count or 0),
                "events": int(events_count or 0),
                "lead_read_model": int(leads_count or 0),
                "max_event_ts": str(max_event_ts) if max_event_ts else None,
                "lead_read_model_checksum": str(lead_checksum) if lead_checksum else None,
            }

        results["validation_seconds"] = round(time.perf_counter() - t0, 6)
        return results

    async def full_restore(
        self,
        *,
        backup_id: str,
        database_url: str,
        restore_snapshots: bool = True,
        rebuild_read_models: bool = True,
    ) -> RestoreReport:
        db_s = await self.restore_database(backup_id=backup_id, database_url=database_url)
        snap_s = await self.restore_snapshots(backup_id=backup_id) if restore_snapshots else 0.0
        rebuild_s = await self.rebuild_read_models() if rebuild_read_models else 0.0
        integrity = await self.validate_integrity()
        return RestoreReport(
            backup_id=backup_id,
            restore_database_seconds=db_s,
            restore_snapshots_seconds=snap_s,
            rebuild_read_models_seconds=rebuild_s,
            integrity=integrity,
        )

    async def _rebuild_lead_read_model(self) -> None:
        async with self._pool.acquire() as conn:
            tenants = await conn.fetch("SELECT id FROM tenants ORDER BY id")

        for t in tenants:
            tenant_id = UUID(str(t["id"]))
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                streams = await conn.fetch(
                    "SELECT stream_id FROM event_streams WHERE stream_id LIKE 'lead:%' ORDER BY updated_at DESC"
                )
                for s in streams:
                    stream_id = str(s["stream_id"])
                    lead_id = stream_id.split(":", 1)[1]
                    rows = await conn.fetch(
                        "SELECT version, event_type, payload FROM events WHERE stream_id=$1 ORDER BY version ASC",
                        stream_id,
                    )
                    state: dict[str, Any] = {}
                    last_version = 0
                    for r in rows:
                        payload_val = r["payload"]
                        if isinstance(payload_val, str):
                            payload = json.loads(payload_val)
                        elif isinstance(payload_val, dict):
                            payload = payload_val
                        else:
                            payload = dict(payload_val)
                        event_type = str(r["event_type"])
                        if event_type == "lead.created":
                            state.update(payload)
                        elif event_type == "lead.updated":
                            state.update(payload.get("changes") or {})
                        last_version = int(r["version"])
                    if last_version == 0:
                        continue
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
                        UUID(lead_id),
                        state.get("name") or "",
                        state.get("email"),
                        state.get("phone"),
                        state.get("company"),
                        state.get("status") or "new",
                        state.get("score"),
                        state.get("assignedTo") or state.get("assigned_to"),
                        json.dumps(state.get("metadata") or {}),
                        last_version,
                    )

    async def _import_jsonl(self, *, key: str, insert_sql: str, map_row) -> None:
        try:
            raw = await self._object_store.get_bytes(key=key)
        except FileNotFoundError:
            return
        if not raw:
            return
        lines = raw.decode("utf-8").splitlines()
        async with self._pool.acquire() as conn:
            for line in lines:
                if not line.strip():
                    continue
                r = json.loads(line)
                await conn.execute(insert_sql, *map_row(r))

    def _psql_restore(self, database_url: str, sql_dump: bytes) -> None:
        cmd = ["psql", database_url, "-v", "ON_ERROR_STOP=1"]
        try:
            subprocess.run(cmd, input=sql_dump, check=True)
        except Exception:
            if self._docker_compose_project_dir:
                subprocess.run(
                    ["docker", "compose", "exec", "-T", "postgres", "psql", database_url, "-v", "ON_ERROR_STOP=1"],
                    cwd=self._docker_compose_project_dir,
                    input=sql_dump,
                    check=True,
                )
            else:
                raise
