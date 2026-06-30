from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID, uuid4

import structlog
from aiokafka import AIOKafkaConsumer
from aiokafka.structs import OffsetAndMetadata, TopicPartition
from pydantic import ValidationError

from .db import create_db_pool, tenant_transaction
from .models import CanonicalEvent, CloudEvent
from .metrics import consumer_lag, replay_failures
from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
from resilience.retry_policy import RetryPolicy, RetryExhaustedError, retry_async


logger = structlog.get_logger()


DEFAULT_TOPICS = ["crm.leads.events", "crm.tickets.events"]


def _infer_aggregate_type(topic: str, event_type: str) -> str:
    lowered = f"{topic} {event_type}".lower()
    if "leads" in lowered or "lead" in lowered:
        return "lead"
    if "tickets" in lowered or "ticket" in lowered:
        return "ticket"
    return "unknown"


def _infer_aggregate_id(aggregate_type: str, data: dict[str, Any]) -> UUID:
    if "aggregate_id" in data:
        return UUID(str(data["aggregate_id"]))
    if aggregate_type == "lead" and "leadId" in data:
        return UUID(str(data["leadId"]))
    if aggregate_type == "ticket" and "ticketId" in data:
        return UUID(str(data["ticketId"]))
    if "id" in data:
        return UUID(str(data["id"]))
    raise ValueError("aggregate_id not found in event payload")


def _parse_event(value: str, topic: str, partition: int, offset: int) -> CanonicalEvent:
    raw = json.loads(value)
    try:
        ce = CloudEvent.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"invalid CloudEvent payload: {e}") from e

    aggregate_type = str(ce.data.get("aggregate_type") or _infer_aggregate_type(topic, ce.type))
    aggregate_id = _infer_aggregate_id(aggregate_type, ce.data)
    event_type = str(ce.data.get("event_type") or ce.type)
    payload = dict(ce.data)

    return CanonicalEvent(
        event_id=ce.id,
        tenant_id=ce.tenantid,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        ts=ce.time,
        kafka_topic=topic,
        kafka_partition=partition,
        kafka_offset=offset,
    )


class EventIngestor:
    def __init__(self, *, kafka_brokers: str, database_url: str, topics: list[str] | None = None):
        self._kafka_brokers = kafka_brokers
        self._database_url = database_url
        self._topics = topics or DEFAULT_TOPICS
        self._consumer: AIOKafkaConsumer | None = None
        self._pool = None
        self._running = False
        self._db_breaker = CircuitBreaker(
            name="replay-ingestor",
            dependency="postgres",
            config=CircuitBreakerConfig(failure_threshold=3, recovery_timeout_seconds=5.0, half_open_max_calls=1, success_threshold=1),
        )
        self._db_retry = RetryPolicy(max_retries=5, base_delay_seconds=0.1, max_delay_seconds=2.0, max_elapsed_seconds=10.0, jitter_ratio=0.2)

    async def start(self) -> None:
        self._pool = await create_db_pool(self._database_url)
        self._consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._kafka_brokers.split(","),
            group_id=os.getenv("REPLAY_INGESTOR_GROUP", "replay-event-ingestor"),
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=lambda m: m.decode("utf-8"),
        )
        await self._consumer.start()
        self._running = True
        logger.info("Event ingestor started", topics=self._topics)

    async def stop(self) -> None:
        self._running = False
        if self._consumer:
            await self._consumer.stop()
        if self._pool:
            await self._pool.close()
        logger.info("Event ingestor stopped")

    async def run_forever(self) -> None:
        if not self._consumer or not self._pool:
            raise RuntimeError("EventIngestor not started")

        try:
            async for msg in self._consumer:
                if not self._running:
                    break
                processed = await self._handle_message(msg.topic, msg.partition, int(msg.offset), msg.value)
                if processed:
                    tp = TopicPartition(msg.topic, msg.partition)
                    await self._consumer.commit({tp: OffsetAndMetadata(msg.offset + 1, "")})
        except asyncio.CancelledError:
            return

    async def _handle_message(self, topic: str, partition: int, offset: int, value: str) -> bool:
        try:
            event = _parse_event(value, topic, partition, offset)
        except Exception as e:
            logger.error("Failed to parse event", topic=topic, partition=partition, offset=offset, error=str(e))
            replay_failures.labels(component="ingestor", error_type="parse").inc()
            return True

        if self._consumer:
            tp = TopicPartition(topic, partition)
            try:
                end = await self._consumer.end_offsets([tp])
                end_offset = int(end.get(tp, 0))
                consumer_lag.labels(group_id=str(self._consumer._group_id), topic=topic, partition=str(partition)).set(max(0, end_offset - offset))
            except Exception:
                pass

        try:
            await self._db_breaker.call(
                lambda: retry_async(
                    lambda: self._write_event(event),
                    policy=self._db_retry,
                    operation="event_ingest",
                    dependency="postgres",
                    tenant_id=str(event.tenant_id),
                )
            )
            return True
        except CircuitOpenError:
            replay_failures.labels(component="ingestor", error_type="db_circuit_open").inc()
            return False
        except RetryExhaustedError:
            replay_failures.labels(component="ingestor", error_type="db_retry_exhausted").inc()
            return False
        except Exception:
            replay_failures.labels(component="ingestor", error_type="db_error").inc()
            return False

    async def _write_event(self, event: CanonicalEvent) -> None:
        async with tenant_transaction(self._pool, event.tenant_id) as conn:
            lock_key = f"{event.aggregate_type}:{event.aggregate_id}"
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", lock_key)

            row = await conn.fetchrow(
                """
                SELECT COALESCE(MAX(version), 0) AS v
                FROM event_log
                WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3
                """,
                event.tenant_id,
                event.aggregate_type,
                event.aggregate_id,
            )
            next_version = int(row["v"]) + 1

            await conn.execute(
                """
                INSERT INTO event_log (
                  id, event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload,
                  kafka_topic, kafka_partition, kafka_offset
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (event_id) DO NOTHING
                """,
                uuid4(),
                event.event_id,
                event.tenant_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                next_version,
                event.ts,
                json.dumps(event.payload),
                event.kafka_topic,
                event.kafka_partition,
                event.kafka_offset,
            )


async def main() -> None:
    kafka_brokers = os.getenv("KAFKA_BROKERS", "localhost:9094")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    topics = os.getenv("REPLAY_INGEST_TOPICS")
    ingestor = EventIngestor(
        kafka_brokers=kafka_brokers,
        database_url=database_url,
        topics=topics.split(",") if topics else None,
    )
    await ingestor.start()
    try:
        await ingestor.run_forever()
    finally:
        await ingestor.stop()


if __name__ == "__main__":
    asyncio.run(main())

