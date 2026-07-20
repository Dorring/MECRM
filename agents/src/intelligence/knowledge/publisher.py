from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import httpx
import structlog
from intelligence.providers import create_embeddings, vector_collection_name

from orchestrator.config import settings


logger = structlog.get_logger()


class KnowledgePublisher:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._weaviate_url = settings.WEAVIATE_URL.rstrip("/")
        self._embeddings = create_embeddings(ollama_url=settings.OLLAMA_URL, embedding_model=settings.OLLAMA_EMBED_MODEL)
        self._collection = vector_collection_name("KnowledgeBase")

    async def start(self) -> None:
        if self._pool:
            return
        self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
        await self._ensure_schema()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
        self._pool = None

    async def embed_article(self, *, tenant_id: str, article_id: str) -> None:
        if not self._pool:
            await self.start()
        assert self._pool

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, title, content, tags, created_at
                    FROM knowledge_articles
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    article_id,
                )
        if not row:
            return

        doc = dict(row)
        content = str(doc.get("content") or "")
        title = str(doc.get("title") or "")
        tags = doc.get("tags")
        tags_list = tags if isinstance(tags, list) else []
        vector = await self._embeddings.aembed_query((title + "\n\n" + content)[:8000])

        obj = {
            "class": self._collection,
            "id": article_id,
            "properties": {
                "article_id": article_id,
                "tenant_id": tenant_id,
                "title": title,
                "content": content[:20000],
                "tags": json.dumps(tags_list, ensure_ascii=False),
                "created_at": _as_iso(doc.get("created_at")),
            },
            "vector": vector,
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.put(f"{self._weaviate_url}/v1/objects/{article_id}", json=obj)

    async def _ensure_schema(self) -> None:
        schema = {
            "class": self._collection,
            "description": "Tenant-isolated knowledge base articles",
            "vectorizer": "none",
            "properties": [
                {"name": "article_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "tenant_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "title", "dataType": ["text"], "indexFilterable": False, "indexSearchable": True},
                {"name": "content", "dataType": ["text"], "indexFilterable": False, "indexSearchable": True},
                {"name": "tags", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                {"name": "created_at", "dataType": ["date"], "indexFilterable": True, "indexSearchable": False},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                existing = await client.get(f"{self._weaviate_url}/v1/schema/{self._collection}")
                if existing.status_code == 200:
                    return
                if existing.status_code not in (404, 422):
                    return
                await client.post(f"{self._weaviate_url}/v1/schema", json=schema)
        except Exception as e:
            logger.warn("Knowledge base schema ensure failed", error=str(e))


def _as_iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
