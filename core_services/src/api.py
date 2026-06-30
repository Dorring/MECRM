from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from write.db import create_db_pool, tenant_transaction
from write.event_store import EventStore
from write.outbox import TransactionalOutbox
from write.commands.lead_commands import CreateLeadCommand, create_lead
from cache.metrics import render_metrics


class CreateLeadRequest(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    idempotency_key: str | None = None


class CreateLeadResponse(BaseModel):
    aggregate_id: str
    version: int


app = FastAPI(title="Core Services (CQRS Write API)", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}

@app.get("/metrics")
async def metrics() -> Response:
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)


@app.on_event("startup")
async def _startup() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    app.state.pool = await create_db_pool(database_url)
    app.state.store = EventStore(app.state.pool)
    app.state.outbox = TransactionalOutbox()


@app.on_event("shutdown")
async def _shutdown() -> None:
    pool = getattr(app.state, "pool", None)
    if pool:
        await pool.close()


@app.post("/commands/leads", response_model=CreateLeadResponse)
async def command_create_lead(
    body: CreateLeadRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
) -> Any:
    try:
        tenant_id = UUID(x_tenant_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id")

    cmd = CreateLeadCommand(
        tenant_id=tenant_id,
        name=body.name,
        email=body.email,
        phone=body.phone,
        company=body.company,
        idempotency_key=body.idempotency_key,
    )

    async with tenant_transaction(app.state.pool, tenant_id) as conn:
        try:
            result = await create_lead(conn, store=app.state.store, outbox=app.state.outbox, cmd=cmd)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {"aggregate_id": str(result.aggregate_id), "version": result.version}

