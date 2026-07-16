from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from intelligence.providers import create_embeddings

from orchestrator.config import settings


logger = structlog.get_logger()


@dataclass(frozen=True)
class AuditSearchFilters:
    from_ts: str | None = None
    to_ts: str | None = None
    agent_name: str | None = None
    action_type: str | None = None
    status: str | None = None
    risk_level: str | None = None


class ComplianceIntelligenceAgent:
    def __init__(self):
        self._weaviate_url = settings.WEAVIATE_URL.rstrip("/")
        self._embeddings = create_embeddings(ollama_url=settings.OLLAMA_URL, embedding_model=_env("OLLAMA_EMBED_MODEL", "nomic-embed-text"))

    async def semantic_audit_search(
        self,
        *,
        tenant_id: str,
        query: str,
        filters: AuditSearchFilters | None = None,
        top_k: int = 20,
    ) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"hits": []}
        vector = await self._embeddings.aembed_query(q)

        where_operands: list[dict[str, Any]] = [{"path": ["tenant_id"], "operator": "Equal", "valueText": tenant_id}]
        f = filters or AuditSearchFilters()
        if f.agent_name:
            where_operands.append({"path": ["agent_name"], "operator": "Equal", "valueText": f.agent_name})
        if f.action_type:
            where_operands.append({"path": ["action_type"], "operator": "Equal", "valueText": f.action_type})
        if f.status:
            where_operands.append({"path": ["status"], "operator": "Equal", "valueText": f.status})
        if f.risk_level:
            where_operands.append({"path": ["risk_level"], "operator": "Equal", "valueText": f.risk_level})
        if f.from_ts:
            where_operands.append({"path": ["created_at"], "operator": "GreaterThanEqual", "valueDate": f.from_ts})
        if f.to_ts:
            where_operands.append({"path": ["created_at"], "operator": "LessThanEqual", "valueDate": f.to_ts})

        where = {"operator": "And", "operands": where_operands}

        gql = {
            "query": """
            query AuditSearch($where:WhereFilter!, $limit:Int!, $vector:[Float!]!) {
              Get {
                AuditEmbedding(
                  where: $where,
                  limit: $limit,
                  nearVector: { vector: $vector }
                ) {
                  decision_id
                  tenant_id
                  agent_name
                  action_type
                  risk_level
                  status
                  created_at
                  text_blob
                  _additional { distance }
                }
              }
            }
            """,
            "variables": {"where": where, "limit": max(1, min(int(top_k or 20), 50)), "vector": vector},
        }

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(f"{self._weaviate_url}/v1/graphql", json=gql)
            if resp.status_code != 200:
                return {"hits": []}
            body = resp.json()

        items = (((body.get("data") or {}).get("Get") or {}).get("AuditEmbedding")) or []
        if not isinstance(items, list):
            return {"hits": []}
        hits: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            dist = (((it.get("_additional") or {}).get("distance")) or 1.0)
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            hits.append(
                {
                    "decision_id": it.get("decision_id"),
                    "tenant_id": it.get("tenant_id"),
                    "agent_name": it.get("agent_name"),
                    "action_type": it.get("action_type"),
                    "risk_level": it.get("risk_level"),
                    "status": it.get("status"),
                    "created_at": it.get("created_at"),
                    "score": score,
                    "snippet": (it.get("text_blob") or "")[:800],
                }
            )
        return {"hits": hits}


def _env(key: str, default: str) -> str:
    import os

    val = os.getenv(key)
    return val.strip() if val and val.strip() else default

