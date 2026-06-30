from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import structlog
from aiokafka import AIOKafkaConsumer, TopicPartition
from pydantic import ValidationError

from .db import tenant_transaction
from .models import CanonicalEvent, CloudEvent, ReplayJobRecord, ReplayJobStatus
from .read_model_projector import project_events
from .snapshot_store import get_snapshot_at_version, get_latest_snapshot, save_snapshot


logger = structlog.get_logger()


@dataclass(frozen=True)
class ReplayResult:
    job: ReplayJobRecord
    state: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _parse_canonical(value: str, *, topic: str, partition: int, offset: int) -> CanonicalEvent:
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


class EventReplayService:
    def __init__(self, *, pool: asyncpg.Pool, kafka_brokers: str):
        self._pool = pool
        self._kafka_brokers = kafka_brokers.split(",")

    async def create_job(
        self,
        *,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        mode: str,
        topic: str,
        partition: int,
        start_offset: int,
        target_time: datetime | None,
    ) -> UUID:
        job_id = uuid4()
        await self._create_job(
            job_id=job_id,
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            mode=mode,
            topic=topic,
            partition=partition,
            start_offset=start_offset,
            target_time=target_time,
        )
        return job_id

    async def run_offset_job(
        self,
        *,
        job_id: UUID,
        topic: str,
        offset: int,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        partition: int = 0,
    ) -> None:
        try:
            _, end_offset, processed, snapshot_used = await self._consume_and_project(
                topic=topic,
                partition=partition,
                start_offset=offset,
                tenant_id=tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                target_time=None,
            )
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.DONE, end_offset=end_offset, events_processed=processed, snapshot_used=snapshot_used, error=None)
        except Exception as e:
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.FAILED, end_offset=None, events_processed=0, snapshot_used=False, error=str(e))
            raise

    async def run_time_job(
        self,
        *,
        job_id: UUID,
        topic: str,
        start_offset: int,
        target_time: datetime,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        partition: int = 0,
    ) -> None:
        try:
            _, end_offset, processed, snapshot_used = await self._consume_and_project(
                topic=topic,
                partition=partition,
                start_offset=start_offset,
                tenant_id=tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                target_time=target_time,
            )
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.DONE, end_offset=end_offset, events_processed=processed, snapshot_used=snapshot_used, error=None)
        except Exception as e:
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.FAILED, end_offset=None, events_processed=0, snapshot_used=False, error=str(e))
            raise

    async def replay_from_offset(self, topic: str, offset: int, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID, partition: int = 0) -> ReplayResult:
        job_id = uuid4()
        await self._create_job(
            job_id=job_id,
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            mode="offset",
            topic=topic,
            partition=partition,
            start_offset=offset,
            target_time=None,
        )

        try:
            state, end_offset, processed, snapshot_used = await self._consume_and_project(
                topic=topic,
                partition=partition,
                start_offset=offset,
                tenant_id=tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                target_time=None,
            )
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.DONE, end_offset=end_offset, events_processed=processed, snapshot_used=snapshot_used, error=None)
        except Exception as e:
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.FAILED, end_offset=None, events_processed=0, snapshot_used=False, error=str(e))
            raise

        job = await self.get_job_status(job_id, tenant_id=tenant_id)
        return ReplayResult(job=job, state=state)

    async def replay_to_time(self, topic: str, start_offset: int, target_time: datetime, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID, partition: int = 0) -> ReplayResult:
        job_id = uuid4()
        await self._create_job(
            job_id=job_id,
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            mode="time",
            topic=topic,
            partition=partition,
            start_offset=start_offset,
            target_time=target_time,
        )

        try:
            state, end_offset, processed, snapshot_used = await self._consume_and_project(
                topic=topic,
                partition=partition,
                start_offset=start_offset,
                tenant_id=tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                target_time=target_time,
            )
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.DONE, end_offset=end_offset, events_processed=processed, snapshot_used=snapshot_used, error=None)
        except Exception as e:
            await self._finish_job(job_id, tenant_id, status=ReplayJobStatus.FAILED, end_offset=None, events_processed=0, snapshot_used=False, error=str(e))
            raise

        job = await self.get_job_status(job_id, tenant_id=tenant_id)
        return ReplayResult(job=job, state=state)

    async def rebuild_aggregate(self, aggregate_type: str, aggregate_id: str, tenant_id: UUID) -> dict[str, Any]:
        events = await self._get_events_for_aggregate(tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=UUID(aggregate_id))
        snapshot = await get_latest_snapshot(self._pool, tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=UUID(aggregate_id))
        if snapshot:
            remaining = [e for e in events if (e.version or 0) > snapshot.version]
            result = project_events(remaining, initial_state=snapshot.state)
            return result.state
        return project_events(events).state

    async def create_snapshot(self, aggregate_id: str, tenant_id: UUID, aggregate_type: str) -> None:
        aggregate_uuid = UUID(aggregate_id)
        events = await self._get_events_for_aggregate(tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=aggregate_uuid)
        if not events:
            return
        last = max([e for e in events if e.version is not None], key=lambda e: int(e.version or 0), default=None)
        state = project_events(events).state
        await save_snapshot(
            self._pool,
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_uuid,
            version=int(last.version) if last and last.version is not None else len(events),
            ts=last.ts if last else _now(),
            state=state,
            kafka_topic=last.kafka_topic if last else None,
            kafka_partition=last.kafka_partition if last else None,
            kafka_offset=last.kafka_offset if last else None,
        )

    async def get_job_status(self, job_id: UUID, *, tenant_id: UUID) -> ReplayJobRecord:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT job_id, tenant_id, aggregate_type, aggregate_id, mode, topic, partition,
                       start_offset, end_offset, target_time, status, started_at, finished_at,
                       events_processed, snapshot_used, error
                FROM replay_jobs
                WHERE job_id = $1 AND tenant_id = $2
                """,
                job_id,
                tenant_id,
            )
            if not row:
                raise ValueError("job not found")
            return ReplayJobRecord.model_validate(dict(row))

    async def timeline(self, *, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID) -> list[dict[str, Any]]:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, ts, event_type, version, payload
                FROM event_log
                WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3
                ORDER BY version ASC
                """,
                tenant_id,
                aggregate_type.lower(),
                aggregate_id,
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                payload_val = r["payload"]
                if payload_val is None:
                    payload = {}
                elif isinstance(payload_val, str):
                    payload = json.loads(payload_val)
                else:
                    payload = dict(payload_val)
                summary = {k: payload.get(k) for k in ("name", "status", "stage", "priority", "subject") if k in payload}
                out.append(
                    {
                        "event_id": str(r["event_id"]),
                        "ts": r["ts"].isoformat(),
                        "event_type": r["event_type"],
                        "version": int(r["version"]),
                        "payload_summary": summary,
                    }
                )
            return out

    async def diff(self, *, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID, from_version: int, to_version: int) -> dict[str, Any]:
        if to_version < from_version:
            raise ValueError("to_version must be >= from_version")
        state_from = await self._state_at_version(tenant_id, aggregate_type, aggregate_id, from_version)
        state_to = await self._state_at_version(tenant_id, aggregate_type, aggregate_id, to_version)
        changes = _json_key_diff(state_from, state_to)
        return {"changed_keys": sorted(changes.keys()), "before": {k: changes[k]["before"] for k in changes}, "after": {k: changes[k]["after"] for k in changes}}

    async def _create_job(
        self,
        *,
        job_id: UUID,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        mode: str,
        topic: str,
        partition: int,
        start_offset: int,
        target_time: datetime | None,
    ) -> None:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO replay_jobs (
                  job_id, tenant_id, aggregate_type, aggregate_id, mode, topic, partition,
                  start_offset, target_time, status, started_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                job_id,
                tenant_id,
                aggregate_type.lower(),
                aggregate_id,
                mode,
                topic,
                partition,
                start_offset,
                target_time,
                ReplayJobStatus.RUNNING,
                _now(),
            )

    async def _finish_job(
        self,
        job_id: UUID,
        tenant_id: UUID,
        *,
        status: str,
        end_offset: int | None,
        events_processed: int,
        snapshot_used: bool,
        error: str | None,
    ) -> None:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            await conn.execute(
                """
                UPDATE replay_jobs
                SET status = $3,
                    end_offset = $4,
                    finished_at = $5,
                    events_processed = $6,
                    snapshot_used = $7,
                    error = $8
                WHERE job_id = $1 AND tenant_id = $2
                """,
                job_id,
                tenant_id,
                status,
                end_offset,
                _now(),
                events_processed,
                snapshot_used,
                error,
            )

    async def _consume_and_project(
        self,
        *,
        topic: str,
        partition: int,
        start_offset: int,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        target_time: datetime | None,
    ) -> tuple[dict[str, Any], int | None, int, bool]:
        snapshot_used = False
        initial_state: dict[str, Any] | None = None
        snapshot = await get_snapshot_at_version(self._pool, tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id, version=10**9)
        if snapshot and snapshot.kafka_topic == topic and snapshot.kafka_partition == partition and snapshot.kafka_offset is not None and snapshot.kafka_offset <= start_offset:
            initial_state = snapshot.state
            snapshot_used = True

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._kafka_brokers,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda m: m.decode("utf-8"),
        )
        await consumer.start()
        try:
            tp = TopicPartition(topic, partition)
            consumer.assign([tp])
            consumer.seek(tp, start_offset)

            filtered: list[CanonicalEvent] = []
            processed = 0
            last_offset: int | None = None
            end_offset = int((await consumer.end_offsets([tp]))[tp])
            if start_offset >= end_offset:
                return project_events(filtered, initial_state=initial_state).state, None, 0, snapshot_used

            while True:
                batch = await consumer.getmany(timeout_ms=200, max_records=500)
                if not batch:
                    end_offset = int((await consumer.end_offsets([tp]))[tp])
                    if last_offset is None:
                        if start_offset >= end_offset:
                            break
                    elif last_offset >= end_offset - 1:
                        break
                    continue

                for records in batch.values():
                    for msg in records:
                        last_offset = int(msg.offset)
                        ev = _parse_canonical(msg.value, topic=msg.topic, partition=msg.partition, offset=int(msg.offset))
                        if ev.tenant_id != tenant_id:
                            continue
                        if ev.aggregate_type.lower() != aggregate_type.lower():
                            continue
                        if ev.aggregate_id != aggregate_id:
                            continue
                        if target_time is not None and ev.ts > target_time:
                            return project_events(filtered, initial_state=initial_state).state, last_offset, processed, snapshot_used
                        processed += 1
                        filtered.append(ev)
                if last_offset is not None and last_offset >= end_offset - 1:
                    break

            return project_events(filtered, initial_state=initial_state).state, last_offset, processed, snapshot_used
        finally:
            await consumer.stop()

    async def _get_events_for_aggregate(self, *, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID) -> list[CanonicalEvent]:
        async with tenant_transaction(self._pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload,
                       kafka_topic, kafka_partition, kafka_offset
                FROM event_log
                WHERE tenant_id = $1 AND aggregate_type = $2 AND aggregate_id = $3
                ORDER BY version ASC
                """,
                tenant_id,
                aggregate_type.lower(),
                aggregate_id,
            )
            out: list[CanonicalEvent] = []
            for r in rows:
                payload_val = r["payload"]
                if payload_val is None:
                    payload = {}
                elif isinstance(payload_val, str):
                    payload = json.loads(payload_val)
                else:
                    payload = dict(payload_val)
                out.append(
                    CanonicalEvent(
                        event_id=r["event_id"],
                        tenant_id=r["tenant_id"],
                        aggregate_type=r["aggregate_type"],
                        aggregate_id=r["aggregate_id"],
                        event_type=r["event_type"],
                        payload=payload,
                        version=int(r["version"]),
                        ts=r["ts"],
                        kafka_topic=r["kafka_topic"],
                        kafka_partition=r["kafka_partition"],
                        kafka_offset=r["kafka_offset"],
                    )
                )
            return out

    async def _state_at_version(self, tenant_id: UUID, aggregate_type: str, aggregate_id: UUID, version: int) -> dict[str, Any]:
        snap = await get_snapshot_at_version(self._pool, tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id, version=version)
        events = await self._get_events_for_aggregate(tenant_id=tenant_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id)
        if snap:
            remaining = [e for e in events if (e.version or 0) > snap.version and (e.version or 0) <= version]
            return project_events(remaining, initial_state=snap.state).state
        filtered = [e for e in events if (e.version or 0) <= version]
        return project_events(filtered).state


def _json_key_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = set(before.keys()) | set(after.keys())
    out: dict[str, dict[str, Any]] = {}
    for k in keys:
        if before.get(k) != after.get(k):
            out[k] = {"before": before.get(k), "after": after.get(k)}
    return out


async def create_replay_service_from_env(pool: asyncpg.Pool) -> EventReplayService:
    kafka_brokers = os.getenv("KAFKA_BROKERS", "localhost:9094")
    return EventReplayService(pool=pool, kafka_brokers=kafka_brokers)

