from __future__ import annotations

import json
from typing import Any

import httpx
from intelligence.providers import create_embeddings


class VectorSearch:
    def __init__(
        self,
        *,
        weaviate_url: str,
        ollama_url: str,
        embedding_model: str,
        timeout_seconds: float = 2.0,
    ):
        self._weaviate_url = weaviate_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._embeddings = create_embeddings(ollama_url=ollama_url, embedding_model=embedding_model)

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int = 8,
        entity: str | None = None,
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []

        vector = await self._embeddings.aembed_query(q)
        limit = max(1, min(int(top_k or 8), 20))

        if entity == "knowledge":
            return await self._search_knowledge(tenant_id=tenant_id, vector=vector, limit=limit)

        if entity and entity in ("lead", "deal", "ticket", "customer"):
            return await self._search_crm(tenant_id=tenant_id, vector=vector, limit=limit, entity=entity)

        crm_limit = max(1, int(limit * 0.6))
        kb_limit = max(1, limit - crm_limit)
        crm, kb = await _gather2(
            self._search_crm(tenant_id=tenant_id, vector=vector, limit=crm_limit, entity=None),
            self._search_knowledge(tenant_id=tenant_id, vector=vector, limit=kb_limit),
        )
        merged = (crm or []) + (kb or [])
        merged.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return merged[:limit]

    async def _search_crm(self, *, tenant_id: str, vector: list[float], limit: int, entity: str | None) -> list[dict[str, Any]]:
        where: dict[str, Any] = {
            "operator": "And",
            "operands": [{"path": ["tenant_id"], "operator": "Equal", "valueText": tenant_id}],
        }
        if entity and entity in ("lead", "deal", "ticket", "customer"):
            where["operands"].append({"path": ["entity_type"], "operator": "Equal", "valueText": entity})

        gql = {
            "query": """
            query CrmEntitySearch($where:WhereFilter!, $limit:Int!, $vector:[Float!]!) {
              Get {
                CrmEntity(
                  where: $where,
                  limit: $limit,
                  nearVector: { vector: $vector }
                ) {
                  entity_id
                  tenant_id
                  entity_type
                  title
                  description
                  updated_at
                  metadata
                  _additional { distance }
                }
              }
            }
            """,
            "variables": {"where": where, "limit": limit, "vector": vector},
        }
        body = await _post_gql(url=self._weaviate_url, timeout=self._timeout_seconds, gql=gql)
        items = (((body.get("data") or {}).get("Get") or {}).get("CrmEntity")) or []
        if not isinstance(items, list):
            return []

        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            dist = (((it.get("_additional") or {}).get("distance")) or 1.0)
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            out.append(
                {
                    "entity_type": it.get("entity_type"),
                    "id": it.get("entity_id"),
                    "title": it.get("title") or "",
                    "description": it.get("description"),
                    "score": score,
                    "metadata": _parse_json(it.get("metadata")),
                }
            )
        return out

    async def _search_knowledge(self, *, tenant_id: str, vector: list[float], limit: int) -> list[dict[str, Any]]:
        where: dict[str, Any] = {
            "operator": "And",
            "operands": [{"path": ["tenant_id"], "operator": "Equal", "valueText": tenant_id}],
        }
        gql = {
            "query": """
            query KnowledgeSearch($where:WhereFilter!, $limit:Int!, $vector:[Float!]!) {
              Get {
                KnowledgeBase(
                  where: $where,
                  limit: $limit,
                  nearVector: { vector: $vector }
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
            "variables": {"where": where, "limit": limit, "vector": vector},
        }
        body = await _post_gql(url=self._weaviate_url, timeout=self._timeout_seconds, gql=gql)
        items = (((body.get("data") or {}).get("Get") or {}).get("KnowledgeBase")) or []
        if not isinstance(items, list):
            return []

        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            dist = (((it.get("_additional") or {}).get("distance")) or 1.0)
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            content = str(it.get("content") or "")
            snippet = content[:360] + ("…" if len(content) > 360 else "")
            out.append(
                {
                    "entity_type": "knowledge",
                    "id": it.get("article_id"),
                    "title": it.get("title") or "",
                    "description": snippet or None,
                    "score": score,
                    "metadata": {"tags": _parse_any_json(it.get("tags"))},
                }
            )
        return out


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


async def _post_gql(*, url: str, timeout: float, gql: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url}/v1/graphql", json=gql)
            if resp.status_code != 200:
                return {}
            body = resp.json()
            return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def _gather2(a, b):
    import asyncio

    return await asyncio.gather(a, b, return_exceptions=False)

