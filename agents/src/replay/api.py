from __future__ import annotations

import asyncio
import contextlib
import os
from uuid import UUID

import structlog
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

from .db import create_db_pool
from .event_ingestor import EventIngestor
from .models import ReplayStartRequest
from .replay_service import EventReplayService
from .metrics import metrics_payload


logger = structlog.get_logger()

app = FastAPI(title="Replay Service", version="2.0")

_pool = None
_service: EventReplayService | None = None
_ingestor: EventIngestor | None = None
_ingestor_task: asyncio.Task | None = None
_replay_tasks: dict[UUID, asyncio.Task] = {}


def _require_tenant(x_tenant_id: str | None) -> UUID:
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="Missing X-Tenant-Id header")
    try:
        return UUID(x_tenant_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id header")


@app.on_event("startup")
async def _startup() -> None:
    global _pool, _service, _ingestor, _ingestor_task
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    kafka_brokers = os.getenv("KAFKA_BROKERS", "localhost:9094")

    _pool = await create_db_pool(database_url)
    _service = EventReplayService(pool=_pool, kafka_brokers=kafka_brokers)

    if os.getenv("ENABLE_REPLAY_INGESTOR", "true").lower() in ("1", "true", "yes"):
        _ingestor = EventIngestor(kafka_brokers=kafka_brokers, database_url=database_url)
        await _ingestor.start()
        _ingestor_task = asyncio.create_task(_ingestor.run_forever())
        logger.info("Replay event ingestor running")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _pool, _ingestor_task, _ingestor, _replay_tasks
    for task in list(_replay_tasks.values()):
        task.cancel()
    _replay_tasks.clear()
    if _ingestor_task:
        _ingestor_task.cancel()
        with contextlib.suppress(Exception):
            await _ingestor_task
    if _ingestor:
        await _ingestor.stop()
    if _pool:
        await _pool.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)


@app.post("/api/v1/replay/start")
async def start_replay(req: ReplayStartRequest, x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")) -> dict[str, str]:
    tenant_id = _require_tenant(x_tenant_id)
    if not _service:
        raise HTTPException(status_code=503, detail="Replay service not ready")

    topic = req.topic or (f"crm.{req.aggregate_type.lower()}s.events" if req.aggregate_type.lower() in ("lead", "ticket") else "crm.events")
    partition = int(req.partition or 0)

    if req.mode == "offset":
        if req.offset is None:
            raise HTTPException(status_code=400, detail="offset is required for mode=offset")
        job_id = await _service.create_job(
            tenant_id=tenant_id,
            aggregate_type=req.aggregate_type,
            aggregate_id=req.aggregate_id,
            mode="offset",
            topic=topic,
            partition=partition,
            start_offset=int(req.offset),
            target_time=None,
        )
        task = asyncio.create_task(
            _service.run_offset_job(
                job_id=job_id,
                topic=topic,
                offset=int(req.offset),
                tenant_id=tenant_id,
                aggregate_type=req.aggregate_type,
                aggregate_id=req.aggregate_id,
                partition=partition,
            )
        )
        _replay_tasks[job_id] = task
        task.add_done_callback(lambda _: _replay_tasks.pop(job_id, None))
        return {"job_id": str(job_id), "status": "running"}

    if req.mode == "time":
        if req.target_time is None:
            raise HTTPException(status_code=400, detail="target_time is required for mode=time")
        start_offset = int(req.offset or 0)
        job_id = await _service.create_job(
            tenant_id=tenant_id,
            aggregate_type=req.aggregate_type,
            aggregate_id=req.aggregate_id,
            mode="time",
            topic=topic,
            partition=partition,
            start_offset=start_offset,
            target_time=req.target_time,
        )
        task = asyncio.create_task(
            _service.run_time_job(
                job_id=job_id,
                topic=topic,
                start_offset=start_offset,
                target_time=req.target_time,
                tenant_id=tenant_id,
                aggregate_type=req.aggregate_type,
                aggregate_id=req.aggregate_id,
                partition=partition,
            )
        )
        _replay_tasks[job_id] = task
        task.add_done_callback(lambda _: _replay_tasks.pop(job_id, None))
        return {"job_id": str(job_id), "status": "running"}

    raise HTTPException(status_code=400, detail="Invalid mode")


@app.get("/api/v1/replay/{job_id}/status")
async def replay_status(job_id: UUID, x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")) -> dict:
    tenant_id = _require_tenant(x_tenant_id)
    if not _service:
        raise HTTPException(status_code=503, detail="Replay service not ready")
    job = await _service.get_job_status(job_id, tenant_id=tenant_id)
    return job.model_dump()


@app.get("/api/v1/replay/{job_id}/diff")
async def replay_diff(job_id: UUID, from_version: int, to_version: int, x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")) -> dict:
    tenant_id = _require_tenant(x_tenant_id)
    if not _service:
        raise HTTPException(status_code=503, detail="Replay service not ready")
    job = await _service.get_job_status(job_id, tenant_id=tenant_id)
    return await _service.diff(
        tenant_id=tenant_id,
        aggregate_type=job.aggregate_type,
        aggregate_id=job.aggregate_id,
        from_version=from_version,
        to_version=to_version,
    )


@app.get("/api/v1/aggregates/{aggregate_type}/{aggregate_id}/timeline")
async def aggregate_timeline(
    aggregate_type: str,
    aggregate_id: UUID,
    tenant_id: str | None = None,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> list[dict]:
    effective_tenant = _require_tenant(x_tenant_id or tenant_id)
    if not _service:
        raise HTTPException(status_code=503, detail="Replay service not ready")
    return await _service.timeline(tenant_id=effective_tenant, aggregate_type=aggregate_type, aggregate_id=aggregate_id)


@app.post("/api/v1/aggregates/{aggregate_type}/{aggregate_id}/snapshot")
async def create_aggregate_snapshot(
    aggregate_type: str,
    aggregate_id: UUID,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, str]:
    tenant_id = _require_tenant(x_tenant_id)
    if not _service:
        raise HTTPException(status_code=503, detail="Replay service not ready")
    await _service.create_snapshot(str(aggregate_id), tenant_id, aggregate_type=aggregate_type)
    return {"status": "ok"}

