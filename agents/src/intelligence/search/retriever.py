from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import asyncpg
import httpx
from intelligence.providers import create_embeddings, vector_collection_name


EntityType = Literal["lead", "deal", "ticket", "customer", "knowledge"]


@dataclass(frozen=True)
class RetrievedResult:
    entity_type: EntityType
    entity_id: str
    tenant_id: str
    title: str
    description: str | None
    created_at: datetime | None
    updated_at: datetime | None
    source: Literal["structured", "semantic"]
    structured_score: float = 0.0
    semantic_score: float = 0.0
    metadata: dict[str, Any] | None = None


class HybridRetriever:
    def __init__(
        self,
        *,
        database_url: str,
        weaviate_url: str,
        ollama_url: str,
        embedding_model: str,
        pool_min: int = 1,
        pool_max: int = 5,
        timeout_seconds: float = 1.5,
    ):
        self._database_url = database_url
        self._weaviate_url = weaviate_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._embeddings = create_embeddings(ollama_url=ollama_url, embedding_model=embedding_model)
        self._crm_collection = vector_collection_name("CrmEntity")
        self._kb_collection = vector_collection_name("KnowledgeBase")

    async def start(self) -> None:
        async with self._pool_lock:
            if self._pool:
                return
            self._pool = await asyncpg.create_pool(dsn=self._database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        async with self._pool_lock:
            if not self._pool:
                return
            await self._pool.close()
            self._pool = None

    async def structured_search(
        self,
        *,
        tenant_id: str,
        query: str,
        entity: EntityType | None,
        filters: dict[str, Any] | None,
        limit: int,
    ) -> list[RetrievedResult]:
        if not self._pool:
            await self.start()
        assert self._pool

        q = query.strip()
        if not q:
            return []

        like = f"%{q}%"
        filters = filters or {}
        status = filters.get("status")
        per_entity = max(3, int(limit / 2))

        out: list[RetrievedResult] = []
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id)
            if entity in (None, "lead"):
                rows = await conn.fetch(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, name, email, company, status, created_at, updated_at
                    FROM leads
                    WHERE tenant_id=$1::uuid
                      AND (name ILIKE $2 OR email ILIKE $2 OR company ILIKE $2)
                      AND ($3::text IS NULL OR status=$3)
                    ORDER BY updated_at DESC
                    LIMIT $4
                    """,
                    tenant_id,
                    like,
                    status,
                    per_entity,
                )
                for r in rows:
                    desc = " ".join([p for p in [r.get("email"), r.get("company"), r.get("status")] if p])
                    out.append(
                        RetrievedResult(
                            entity_type="lead",
                            entity_id=r["id"],
                            tenant_id=r["tenant_id"],
                            title=r["name"],
                            description=desc or None,
                            created_at=r.get("created_at"),
                            updated_at=r.get("updated_at"),
                            source="structured",
                            structured_score=1.0,
                            semantic_score=0.0,
                            metadata={"status": r.get("status")},
                        )
                    )

            if entity in (None, "deal"):
                rows = await conn.fetch(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, name, stage, created_at, updated_at, amount, currency
                    FROM deals
                    WHERE tenant_id=$1::uuid
                      AND (name ILIKE $2 OR stage ILIKE $2)
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    tenant_id,
                    like,
                    per_entity,
                )
                for r in rows:
                    desc = " ".join([p for p in [r.get("stage"), _money(r.get("amount"), r.get("currency"))] if p])
                    out.append(
                        RetrievedResult(
                            entity_type="deal",
                            entity_id=r["id"],
                            tenant_id=r["tenant_id"],
                            title=r["name"],
                            description=desc or None,
                            created_at=r.get("created_at"),
                            updated_at=r.get("updated_at"),
                            source="structured",
                            structured_score=1.0,
                            semantic_score=0.0,
                            metadata={"stage": r.get("stage")},
                        )
                    )

            if entity in (None, "ticket"):
                rows = await conn.fetch(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, subject, description, status, priority, created_at, updated_at
                    FROM tickets
                    WHERE tenant_id=$1::uuid
                      AND (subject ILIKE $2 OR description ILIKE $2)
                      AND ($3::text IS NULL OR status=$3)
                    ORDER BY updated_at DESC
                    LIMIT $4
                    """,
                    tenant_id,
                    like,
                    status,
                    per_entity,
                )
                for r in rows:
                    desc = " ".join([p for p in [r.get("status"), r.get("priority")] if p])
                    out.append(
                        RetrievedResult(
                            entity_type="ticket",
                            entity_id=r["id"],
                            tenant_id=r["tenant_id"],
                            title=r["subject"],
                            description=(r.get("description") or "")[:180] or (desc or None),
                            created_at=r.get("created_at"),
                            updated_at=r.get("updated_at"),
                            source="structured",
                            structured_score=1.0,
                            semantic_score=0.0,
                            metadata={"status": r.get("status"), "priority": r.get("priority")},
                        )
                    )

            if entity in (None, "customer"):
                rows = await conn.fetch(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, name, email, phone, company, status, created_at, updated_at
                    FROM customers
                    WHERE tenant_id=$1::uuid
                      AND deleted_at IS NULL
                      AND (name ILIKE $2 OR email ILIKE $2 OR company ILIKE $2)
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    tenant_id,
                    like,
                    per_entity,
                )
                for r in rows:
                    desc = " ".join([p for p in [r.get("email"), r.get("company"), r.get("status")] if p])
                    out.append(
                        RetrievedResult(
                            entity_type="customer",
                            entity_id=r["id"],
                            tenant_id=r["tenant_id"],
                            title=r["name"],
                            description=desc or None,
                            created_at=r.get("created_at"),
                            updated_at=r.get("updated_at"),
                            source="structured",
                            structured_score=1.0,
                            semantic_score=0.0,
                            metadata={"status": r.get("status")},
                        )
                    )

        return out[:limit]

    async def semantic_search(
        self,
        *,
        tenant_id: str,
        query: str,
        entity: EntityType | None,
        limit: int,
    ) -> list[RetrievedResult]:
        q = query.strip()
        if not q:
            return []

        try:
            vector = await self._embeddings.aembed_query(q)
        except Exception:
            return []

        class_name = self._crm_collection
        where: dict[str, Any] = {
            "operator": "And",
            "operands": [
                {"path": ["tenant_id"], "operator": "Equal", "valueString": tenant_id},
            ],
        }
        if entity:
            where["operands"].append({"path": ["entity_type"], "operator": "Equal", "valueString": entity})

        gql = {
            "query": """
            query Hybrid($limit: Int!, $where: WhereFilter!, $vector: [Float!]!) {
              Get {
                CrmEntity(
                  where: $where,
                  nearVector: { vector: $vector },
                  limit: $limit
                ) {
                  entity_id
                  tenant_id
                  entity_type
                  title
                  description
                  created_at
                  updated_at
                  metadata
                  _additional { distance }
                }
              }
            }
            """,
            "variables": {"limit": limit, "where": where, "vector": vector},
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(f"{self._weaviate_url}/v1/graphql", json=gql)
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            return []

        items = (((body.get("data") or {}).get("Get") or {}).get(class_name)) or []
        out: list[RetrievedResult] = []
        for it in items:
            et = it.get("entity_type")
            if et not in ("lead", "deal", "ticket", "customer"):
                continue
            dist = (((it.get("_additional") or {}).get("distance")) or 1.0)
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            out.append(
                RetrievedResult(
                    entity_type=et,
                    entity_id=str(it.get("entity_id") or it.get("id")),
                    tenant_id=str(it.get("tenant_id")),
                    title=str(it.get("title") or ""),
                    description=(it.get("description") or None),
                    created_at=_parse_dt(it.get("created_at")),
                    updated_at=_parse_dt(it.get("updated_at")),
                    source="semantic",
                    structured_score=0.0,
                    semantic_score=score,
                    metadata=_parse_json(it.get("metadata")),
                )
            )
        return out

    async def semantic_search_knowledge(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int,
    ) -> list[RetrievedResult]:
        q = query.strip()
        if not q:
            return []

        try:
            vector = await self._embeddings.aembed_query(q)
        except Exception:
            return []

        class_name = self._kb_collection
        where: dict[str, Any] = {
            "operator": "And",
            "operands": [
                {"path": ["tenant_id"], "operator": "Equal", "valueString": tenant_id},
            ],
        }

        gql = {
            "query": """
            query Knowledge($limit: Int!, $where: WhereFilter!, $vector: [Float!]!) {
              Get {
                KnowledgeBase(
                  where: $where,
                  nearVector: { vector: $vector },
                  limit: $limit
                ) {
                  article_id
                  tenant_id
                  title
                  content
                  tags
                  created_at
                  _additional { distance }
                }
              }
            }
            """,
            "variables": {"limit": limit, "where": where, "vector": vector},
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(f"{self._weaviate_url}/v1/graphql", json=gql)
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            return []

        items = (((body.get("data") or {}).get("Get") or {}).get(class_name)) or []
        out: list[RetrievedResult] = []
        for it in items:
            dist = (((it.get("_additional") or {}).get("distance")) or 1.0)
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            content = str(it.get("content") or "")
            snippet = content[:240] + ("…" if len(content) > 240 else "")
            out.append(
                RetrievedResult(
                    entity_type="knowledge",
                    entity_id=str(it.get("article_id") or it.get("id")),
                    tenant_id=str(it.get("tenant_id")),
                    title=str(it.get("title") or ""),
                    description=snippet or None,
                    created_at=_parse_dt(it.get("created_at")),
                    updated_at=None,
                    source="semantic",
                    structured_score=0.0,
                    semantic_score=score,
                    metadata={"tags": _parse_any_json(it.get("tags"))},
                )
            )
        return out

    async def ensure_weaviate_schema(self) -> None:
        crm_entity = {
            "class": self._crm_collection,
            "description": "Tenant-isolated CRM entities for hybrid search",
            "vectorizer": "none",
            "properties": [
                {"name": "entity_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "tenant_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "entity_type", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "title", "dataType": ["text"], "indexSearchable": True},
                {"name": "description", "dataType": ["text"], "indexSearchable": True},
                {"name": "created_at", "dataType": ["date"], "indexFilterable": True, "indexSearchable": False},
                {"name": "updated_at", "dataType": ["date"], "indexFilterable": True, "indexSearchable": False},
                {"name": "metadata", "dataType": ["text"], "indexSearchable": False},
            ],
        }
        knowledge_base = {
            "class": self._kb_collection,
            "description": "Tenant-isolated knowledge base articles",
            "vectorizer": "none",
            "properties": [
                {"name": "article_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "tenant_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "title", "dataType": ["text"], "indexSearchable": True},
                {"name": "content", "dataType": ["text"], "indexSearchable": True},
                {"name": "tags", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                {"name": "created_at", "dataType": ["date"], "indexFilterable": True, "indexSearchable": False},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                existing = await client.get(f"{self._weaviate_url}/v1/schema/{self._crm_collection}")
                if existing.status_code in (404, 422):
                    await client.post(f"{self._weaviate_url}/v1/schema", json=crm_entity)
                kb_existing = await client.get(f"{self._weaviate_url}/v1/schema/{self._kb_collection}")
                if kb_existing.status_code in (404, 422):
                    await client.post(f"{self._weaviate_url}/v1/schema", json=knowledge_base)
        except Exception:
            return


def _parse_any_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def _parse_json(val: Any) -> dict[str, Any] | None:
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            obj = json.loads(val)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _parse_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str) and val:
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _money(amount: Any, currency: Any) -> str | None:
    if amount is None:
        return None
    try:
        cur = str(currency or "").upper() or "USD"
        return f"{cur} {amount}"
    except Exception:
        return None

