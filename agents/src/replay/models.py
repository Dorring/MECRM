from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CloudEvent(BaseModel):
    specversion: str
    type: str
    source: str
    id: UUID
    time: datetime
    datacontenttype: str | None = None
    tenantid: UUID
    correlationid: str | None = None
    data: dict[str, Any]


class CanonicalEvent(BaseModel):
    event_id: UUID
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    version: int | None = None
    ts: datetime
    kafka_topic: str | None = None
    kafka_partition: int | None = None
    kafka_offset: int | None = None

    @field_validator("aggregate_type")
    @classmethod
    def _normalize_aggregate_type(cls, v: str) -> str:
        return v.strip().lower()


class ReplayMode(str):
    OFFSET = "offset"
    TIME = "time"


class ReplayStartRequest(BaseModel):
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    mode: Literal["offset", "time"]
    offset: int | None = None
    target_time: datetime | None = None
    topic: str | None = None
    partition: int | None = None


class ReplayJobStatus(str):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ReplayJobRecord(BaseModel):
    job_id: UUID
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    mode: str
    topic: str
    partition: int
    start_offset: int
    end_offset: int | None = None
    target_time: datetime | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    events_processed: int = 0
    snapshot_used: bool = False
    error: str | None = None


class TimelineEvent(BaseModel):
    event_id: UUID
    ts: datetime
    event_type: str
    version: int
    payload_summary: dict[str, Any] = Field(default_factory=dict)

