from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .db import tenant_transaction


class ConcurrencyError(Exception):
    def __init__(self, *, stream_id: str, expected_version: int, actual_version: int):
        super().__init__(f"Concurrency conflict for stream {stream_id}: expected={expected_version} actual={actual_version}")
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version


@dataclass(frozen=True)
class NewEvent:
    event_type: str
    payload: dict[str, Any]
    schema_version: int = 1
    event_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class StoredEvent:
    id: UUID
    tenant_id: UUID
    stream_id: str
    version: int
    event_id: UUID
    event_type: str
    schema_version: int
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: datetime


class EventStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def append(
        self,
        *,
        tenant_id: UUID,
        stream_id: str,
        events: list[NewEvent],
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> int:
        if not events:
            return expected_version

        async with tenant_transaction(self._pool, tenant_id) as conn:
            return await self.append_in_transaction(
                conn,
                tenant_id=tenant_id,
                stream_id=stream_id,
                events=events,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    async def append_in_transaction(
        self,
        conn: asyncpg.Connection,
        *,
        tenant_id: UUID,
        stream_id: str,
        events: list[NewEvent],
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> int:
        if not events:
            return expected_version

        await conn.execute(
            """
            INSERT INTO event_streams (tenant_id, stream_id, current_version)
            VALUES ($1, $2, 0)
            ON CONFLICT (tenant_id, stream_id) DO NOTHING
            """,
            tenant_id,
            stream_id,
        )

        row = await conn.fetchrow(
            """
            SELECT current_version
            FROM event_streams
            WHERE tenant_id = $1 AND stream_id = $2
            FOR UPDATE
            """,
            tenant_id,
            stream_id,
        )
        if row is None:
            raise RuntimeError("event_streams row not found after insert")

        current_version = int(row["current_version"])

        if idempotency_key:
            existing = await conn.fetchrow(
                """
                SELECT max(version) AS max_version
                FROM events
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                tenant_id,
                idempotency_key,
            )
            if existing and existing["max_version"] is not None:
                return int(existing["max_version"])

        if expected_version != current_version:
            raise ConcurrencyError(stream_id=stream_id, expected_version=expected_version, actual_version=current_version)

        start_version = current_version
        values = []
        for i, ev in enumerate(events, start=1):
            values.append(
                (
                    uuid4(),
                    tenant_id,
                    stream_id,
                    start_version + i,
                    ev.event_id,
                    ev.event_type,
                    int(ev.schema_version),
                    json.dumps(ev.payload),
                    idempotency_key,
                )
            )

        try:
            await conn.executemany(
                """
                INSERT INTO events (id, tenant_id, stream_id, version, event_id, event_type, schema_version, payload, idempotency_key)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
                """,
                values,
            )
        except asyncpg.UniqueViolationError:
            if not idempotency_key:
                raise
            existing = await conn.fetchrow(
                """
                SELECT max(version) AS max_version
                FROM events
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                tenant_id,
                idempotency_key,
            )
            if existing and existing["max_version"] is not None:
                return int(existing["max_version"])
            raise

        new_version = start_version + len(events)
        await conn.execute(
            """
            UPDATE event_streams
            SET current_version = $3, updated_at = now()
            WHERE tenant_id = $1 AND stream_id = $2
            """,
            tenant_id,
            stream_id,
            new_version,
        )

        return new_version

    async def read_stream(self, *, tenant_id: UUID, stream_id: str, from_version: int = 0) -> list[StoredEvent]:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, tenant_id, stream_id, version, event_id, event_type, schema_version, payload, idempotency_key, created_at
                FROM events
                WHERE tenant_id = $1 AND stream_id = $2 AND version > $3
                ORDER BY version ASC
                """,
                tenant_id,
                stream_id,
                from_version,
            )
            out: list[StoredEvent] = []
            for r in rows:
                payload_val = r["payload"]
                if isinstance(payload_val, str):
                    payload = json.loads(payload_val)
                elif isinstance(payload_val, dict):
                    payload = payload_val
                else:
                    payload = dict(payload_val)
                out.append(
                    StoredEvent(
                        id=r["id"],
                        tenant_id=r["tenant_id"],
                        stream_id=r["stream_id"],
                        version=int(r["version"]),
                        event_id=r["event_id"],
                        event_type=r["event_type"],
                        schema_version=int(r["schema_version"]),
                        payload=payload,
                        idempotency_key=r["idempotency_key"],
                        created_at=r["created_at"],
                    )
                )
            return out

    async def current_version(self, *, tenant_id: UUID, stream_id: str) -> int:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT current_version
                FROM event_streams
                WHERE tenant_id = $1 AND stream_id = $2
                """,
                tenant_id,
                stream_id,
            )
            return int(row["current_version"]) if row else 0

