from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg


@dataclass(frozen=True)
class OutboxEvent:
    tenant_id: UUID
    event_id: UUID
    event_type: str
    topic: str
    payload: dict[str, Any]
    schema_version: int = 1
    idempotency_key: str | None = None


class TransactionalOutbox:
    async def enqueue_in_transaction(self, conn: asyncpg.Connection, *, items: list[OutboxEvent]) -> None:
        if not items:
            return

        values = []
        for it in items:
            values.append(
                (
                    uuid4(),
                    it.tenant_id,
                    it.event_id,
                    it.event_type,
                    it.topic,
                    json.dumps(it.payload),
                    int(it.schema_version),
                    it.idempotency_key,
                )
            )

        await conn.executemany(
            """
            INSERT INTO outbox_events (id, tenant_id, event_id, event_type, topic, payload, schema_version, idempotency_key)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
            """,
            values,
        )

