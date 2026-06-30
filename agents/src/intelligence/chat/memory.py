from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from langchain_ollama import OllamaEmbeddings


Role = Literal["user", "assistant", "system"]


@dataclass
class ChatMemoryItem:
    conversation_id: str
    tenant_id: str
    user_id: str
    role: Role
    message: str
    timestamp: str


class WeaviateChatMemory:
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
        self._embeddings = OllamaEmbeddings(base_url=ollama_url, model=embedding_model)

    async def ensure_schema(self) -> None:
        body = {
            "class": "ChatMemory",
            "description": "Tenant-isolated conversational memory for CRM copilot",
            "vectorizer": "none",
            "properties": [
                {"name": "conversation_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "tenant_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "user_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "role", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                {"name": "message", "dataType": ["text"], "indexFilterable": False, "indexSearchable": True},
                {"name": "timestamp", "dataType": ["date"], "indexFilterable": True, "indexSearchable": False},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                existing = await client.get(f"{self._weaviate_url}/v1/schema/ChatMemory")
                if existing.status_code == 200:
                    return
                if existing.status_code not in (404, 422):
                    return
                await client.post(f"{self._weaviate_url}/v1/schema", json=body)
        except Exception:
            return

    async def append(self, *, item: ChatMemoryItem) -> None:
        try:
            await self.ensure_schema()
            vector = await self._embeddings.aembed_query(item.message or "")
            obj = {
                "class": "ChatMemory",
                "properties": {
                    "conversation_id": item.conversation_id,
                    "tenant_id": item.tenant_id,
                    "user_id": item.user_id,
                    "role": item.role,
                    "message": item.message,
                    "timestamp": item.timestamp,
                },
                "vector": vector,
            }
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                await client.post(f"{self._weaviate_url}/v1/objects", json=obj)
        except Exception:
            return

    async def load_window(self, *, tenant_id: str, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 50))
        query = {
            "query": """
            query ChatWindow($tenant:String!, $conv:String!, $limit:Int!) {
              Get {
                ChatMemory(
                  where: { operator: And, operands: [
                    { path: [\"tenant_id\"], operator: Equal, valueText: $tenant },
                    { path: [\"conversation_id\"], operator: Equal, valueText: $conv }
                  ]},
                  limit: $limit,
                  sort: [{ path: [\"timestamp\"], order: desc }]
                ) {
                  conversation_id
                  tenant_id
                  user_id
                  role
                  message
                  timestamp
                }
              }
            }
            """,
            "variables": {"tenant": tenant_id, "conv": conversation_id, "limit": limit},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(f"{self._weaviate_url}/v1/graphql", json=query)
                if resp.status_code != 200:
                    return []
                body = resp.json()
        except Exception:
            return []

        items = (((body.get("data") or {}).get("Get") or {}).get("ChatMemory")) or []
        if not isinstance(items, list):
            return []
        items = [x for x in items if isinstance(x, dict)]
        items.reverse()
        return items


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

