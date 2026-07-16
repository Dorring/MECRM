from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import httpx
import structlog
from intelligence.providers import create_embeddings

from orchestrator.config import settings


logger = structlog.get_logger()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _stringify_json(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(v)


def _decision_text_blob(d: dict[str, Any]) -> str:
    parts = [
        f"agent={d.get('agent_id')}",
        f"action={d.get('action_type')}",
        f"risk={d.get('risk_level')}",
        f"status={d.get('status')}",
    ]
    reasoning = d.get("reasoning") or {}
    factors = reasoning.get("factors")
    if factors:
        parts.append("factors=" + _stringify_json(factors)[:2000])
    if reasoning:
        parts.append("reasoning=" + _stringify_json(reasoning)[:2000])
    evidence = d.get("evidence")
    if evidence:
        parts.append("evidence=" + _stringify_json(evidence)[:1500])
    tool_calls = d.get("tool_calls")
    if tool_calls:
        parts.append("tool_calls=" + _stringify_json(tool_calls)[:1500])
    return "\n".join(parts).strip()


@dataclass(frozen=True)
class AuditHit:
    decision_id: str
    tenant_id: str
    agent_name: str
    text_blob: str
    created_at: str | None


class AuditIndexer:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._embeddings = create_embeddings(ollama_url=settings.OLLAMA_URL, embedding_model=_env("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
        self._weaviate_url = settings.WEAVIATE_URL.rstrip("/")
        self._running = False
        self._task: asyncio.Task | None = None
        self._cursor_by_tenant: dict[str, datetime] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
        await self._ensure_schema()
        self._task = asyncio.create_task(self._loop())
        logger.info("Audit indexer started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._pool:
            await self._pool.close()
        self._pool = None
        logger.info("Audit indexer stopped")

    async def _ensure_schema(self) -> None:
        schema = {
            "class": "AuditEmbedding",
            "description": "Explainability/audit decision embeddings",
            "properties": [
                {"name": "decision_id", "dataType": ["text"]},
                {"name": "tenant_id", "dataType": ["text"]},
                {"name": "agent_name", "dataType": ["text"]},
                {"name": "action_type", "dataType": ["text"]},
                {"name": "risk_level", "dataType": ["text"]},
                {"name": "status", "dataType": ["text"]},
                {"name": "created_at", "dataType": ["date"]},
                {"name": "text_blob", "dataType": ["text"]},
            ],
            "vectorizer": "none",
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                existing = await client.get(f"{self._weaviate_url}/v1/schema")
                if existing.status_code == 200:
                    classes = ((existing.json() or {}).get("classes")) or []
                    if any(isinstance(c, dict) and c.get("class") == "AuditEmbedding" for c in classes):
                        return
                await client.post(f"{self._weaviate_url}/v1/schema", json=schema)
        except Exception:
            return

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Audit indexer tick failed", error=str(e))
            await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            tenants = await conn.fetch("SELECT id::text as id FROM tenants")
        for t in tenants:
            tenant_id = str(t.get("id"))
            await self._index_tenant(tenant_id)

    async def _index_tenant(self, tenant_id: str) -> None:
        if not self._pool:
            return
        since = self._cursor_by_tenant.get(tenant_id)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """
                    SELECT
                      id::text as id,
                      tenant_id::text as tenant_id,
                      agent_id,
                      action_type,
                      risk_level,
                      status,
                      reasoning,
                      evidence,
                      tool_calls,
                      created_at
                    FROM agent_decisions
                    WHERE tenant_id = $1::uuid
                      AND ($2::timestamptz IS NULL OR created_at > $2::timestamptz)
                    ORDER BY created_at ASC
                    LIMIT 200
                    """,
                    tenant_id,
                    since,
                )
        if not rows:
            return

        max_ts: datetime | None = None
        for r in rows:
            created_at = r.get("created_at")
            if isinstance(created_at, datetime):
                if max_ts is None or created_at > max_ts:
                    max_ts = created_at
            await self._upsert(
                decision_id=str(r.get("id")),
                tenant_id=str(r.get("tenant_id")),
                agent_name=str(r.get("agent_id")),
                action_type=str(r.get("action_type")),
                risk_level=str(r.get("risk_level")),
                status=str(r.get("status")),
                created_at=created_at if isinstance(created_at, datetime) else None,
                text_blob=_decision_text_blob(dict(r)),
            )
        if max_ts:
            self._cursor_by_tenant[tenant_id] = max_ts

    async def _upsert(
        self,
        *,
        decision_id: str,
        tenant_id: str,
        agent_name: str,
        action_type: str,
        risk_level: str,
        status: str,
        created_at: datetime | None,
        text_blob: str,
    ) -> None:
        try:
            vector = await self._embeddings.aembed_query(text_blob[:6000])
        except Exception:
            return

        obj = {
            "class": "AuditEmbedding",
            "id": decision_id,
            "properties": {
                "decision_id": decision_id,
                "tenant_id": tenant_id,
                "agent_name": agent_name,
                "action_type": action_type,
                "risk_level": risk_level,
                "status": status,
                "created_at": _as_iso(created_at) or _as_iso(_utc_now()),
                "text_blob": text_blob[:9000],
            },
            "vector": vector,
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.put(f"{self._weaviate_url}/v1/objects/{decision_id}", json=obj)
        except Exception:
            return


def _env(key: str, default: str) -> str:
    import os

    val = os.getenv(key)
    return val.strip() if val and val.strip() else default

