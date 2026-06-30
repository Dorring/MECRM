from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from .db import tenant_transaction


@dataclass(frozen=True)
class SnapshotRecord:
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    version: int
    ts: datetime
    state: dict[str, Any]
    kafka_topic: str | None
    kafka_partition: int | None
    kafka_offset: int | None
    created_at: datetime


async def save_snapshot(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    version: int,
    ts: datetime,
    state: dict[str, Any],
    kafka_topic: str | None = None,
    kafka_partition: int | None = None,
    kafka_offset: int | None = None,
) -> None:
    async with tenant_transaction(pool, tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO aggregate_snapshots (
              tenant_id, aggregate_type, aggregate_id, version, ts, state,
              kafka_topic, kafka_partition, kafka_offset
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (tenant_id, aggregate_type, aggregate_id, version)
            DO UPDATE SET
              ts = EXCLUDED.ts,
              state = EXCLUDED.state,
              kafka_topic = EXCLUDED.kafka_topic,
              kafka_partition = EXCLUDED.kafka_partition,
              kafka_offset = EXCLUDED.kafka_offset
            """,
            tenant_id,
            aggregate_type,
            aggregate_id,
            version,
            ts,
            json.dumps(state),
            kafka_topic,
            kafka_partition,
            kafka_offset,
        )


async def get_latest_snapshot(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
) -> SnapshotRecord | None:
    async with tenant_transaction(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, aggregate_type, aggregate_id, version, ts, state,
                   kafka_topic, kafka_partition, kafka_offset, created_at
            FROM aggregate_snapshots
            WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3
            ORDER BY version DESC
            LIMIT 1
            """,
            tenant_id,
            aggregate_type,
            aggregate_id,
        )
        if not row:
            return None
        state_val = row["state"]
        state = json.loads(state_val) if isinstance(state_val, str) else dict(state_val)
        return SnapshotRecord(
            tenant_id=row["tenant_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            version=row["version"],
            ts=row["ts"],
            state=state,
            kafka_topic=row["kafka_topic"],
            kafka_partition=row["kafka_partition"],
            kafka_offset=row["kafka_offset"],
            created_at=row["created_at"],
        )


async def get_snapshot_at_version(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    version: int,
) -> SnapshotRecord | None:
    async with tenant_transaction(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, aggregate_type, aggregate_id, version, ts, state,
                   kafka_topic, kafka_partition, kafka_offset, created_at
            FROM aggregate_snapshots
            WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3 AND version <= $4
            ORDER BY version DESC
            LIMIT 1
            """,
            tenant_id,
            aggregate_type,
            aggregate_id,
            version,
        )
        if not row:
            return None
        state_val = row["state"]
        state = json.loads(state_val) if isinstance(state_val, str) else dict(state_val)
        return SnapshotRecord(
            tenant_id=row["tenant_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            version=row["version"],
            ts=row["ts"],
            state=state,
            kafka_topic=row["kafka_topic"],
            kafka_partition=row["kafka_partition"],
            kafka_offset=row["kafka_offset"],
            created_at=row["created_at"],
        )


async def purge_old_snapshots(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    keep_last_n: int,
) -> int:
    if keep_last_n <= 0:
        return 0
    async with tenant_transaction(pool, tenant_id) as conn:
        result = await conn.execute(
            """
            WITH keep AS (
              SELECT version
              FROM aggregate_snapshots
              WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3
              ORDER BY version DESC
              LIMIT $4
            )
            DELETE FROM aggregate_snapshots s
            WHERE s.tenant_id = $1 AND s.aggregate_type = $2 AND s.aggregate_id = $3
              AND s.version NOT IN (SELECT version FROM keep)
            """,
            tenant_id,
            aggregate_type,
            aggregate_id,
            keep_last_n,
        )
        return int(result.split(" ")[-1])

