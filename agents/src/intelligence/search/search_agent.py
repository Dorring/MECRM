from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from aiokafka import AIOKafkaProducer
from langchain_ollama import ChatOllama
from opentelemetry import trace

from policy.opa_binding import OpaClient

from orchestrator.config import settings
from governance.agent_telemetry import error_rate

from .graph import SearchState, build_search_graph
from .intent_parser import SearchIntent
from .retriever import HybridRetriever


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass
class SearchDeps:
    llm: ChatOllama
    retriever: HybridRetriever
    opa: OpaClient
    limit: int


class SearchAgent:
    agent_id = "search-agent"

    def __init__(self):
        self._producer: AIOKafkaProducer | None = None
        self._producer_lock = asyncio.Lock()
        self._retriever = HybridRetriever(
            database_url=settings.DATABASE_URL,
            weaviate_url=settings.WEAVIATE_URL,
            ollama_url=settings.OLLAMA_URL,
            embedding_model=_env("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )
        self._llm = ChatOllama(base_url=settings.OLLAMA_URL, model=settings.OLLAMA_MODEL, temperature=0)
        self._opa = OpaClient(settings.OPA_URL, timeout_seconds=1.0)
        self._deps = SearchDeps(llm=self._llm, retriever=self._retriever, opa=self._opa, limit=12)
        self._graph = build_search_graph(deps=self._deps)
        self._schema_checked = False
        self._schema_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._retriever.start()
        await self._ensure_producer()

    async def close(self) -> None:
        await self._retriever.close()
        async with self._producer_lock:
            if self._producer:
                await self._producer.stop()
                self._producer = None

    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        query: str,
        module: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        search_id = str(uuid4())
        t0 = time.perf_counter()
        await self._ensure_weaviate_schema()

        with tracer.start_as_current_span("intelligence.search") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("user_id", user_id)
            span.set_attribute("search_id", search_id)
            span.set_attribute("query_len", len(query or ""))

            state = SearchState(
                query=query,
                module=module,
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
            )

            try:
                out: SearchState = await self._graph.ainvoke(state)
            except Exception as e:
                error_rate.labels(agent_id=self.agent_id, error_type="search_graph_failed").inc()
                await self._emit_event(
                    topic=_topic("SearchPerformed"),
                    event_type="search.performed",
                    tenant_id=tenant_id,
                    correlation_id=correlation_id,
                    data={"searchId": search_id, "query": query, "status": "error", "error": str(e)},
                )
                return {
                    "search_id": search_id,
                    "intent": SearchIntent().model_dump(),
                    "results": [],
                    "suggestions": [],
                    "explainability": {
                        "error": "search_failed",
                        "correlation_id": correlation_id,
                    },
                }

            ranked = out.ranked or []
            allowed = await self._filter_with_opa(tenant_id=tenant_id, user_id=user_id, roles=roles, results=ranked)

            duration_ms = (time.perf_counter() - t0) * 1000.0
            await self._emit_event(
                topic=_topic("SearchPerformed"),
                event_type="search.performed",
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                data={
                    "searchId": search_id,
                    "query": query,
                    "intent": (out.intent.model_dump() if out.intent else SearchIntent().model_dump()),
                    "resultCount": len(allowed),
                    "durationMs": round(duration_ms, 2),
                    "module": module,
                    "userId": user_id,
                },
            )

            if not allowed:
                error_rate.labels(agent_id=self.agent_id, error_type="zero_results").inc()

            return {
                "search_id": search_id,
                "intent": (out.intent.model_dump() if out.intent else SearchIntent().model_dump()),
                "results": [
                    {
                        "entity_type": r.entity_type,
                        "id": r.entity_id,
                        "title": r.title,
                        "description": r.description,
                        "url": r.url,
                        "score": r.final_score,
                        "sources": r.sources,
                        "score_components": r.score_components,
                        "reasoning": r.reasoning,
                        "metadata": r.metadata,
                    }
                    for r in allowed
                ],
                "suggestions": [s.__dict__ for s in (out.suggestions or [])],
                "explainability": {
                    "ranking_formula": "semantic*0.5 + role_weight*0.2 + recency*0.2 + module_affinity*0.1",
                    "correlation_id": correlation_id,
                    "timing_ms": {"total": round(duration_ms, 2)},
                    "intent_raw": out.intent_raw,
                    "intent_error": out.intent_error,
                },
            }

    async def _ensure_weaviate_schema(self) -> None:
        if self._schema_checked:
            return
        async with self._schema_lock:
            if self._schema_checked:
                return
            await self._retriever.ensure_weaviate_schema()
            self._schema_checked = True

    async def _ensure_producer(self) -> None:
        async with self._producer_lock:
            if self._producer:
                return
            producer = AIOKafkaProducer(
                bootstrap_servers=settings.KAFKA_BROKERS,
                value_serializer=lambda v: v.encode("utf-8"),
            )
            await producer.start()
            self._producer = producer

    async def _emit_event(self, *, topic: str, event_type: str, tenant_id: str, correlation_id: str | None, data: dict[str, Any]) -> None:
        await self._ensure_producer()
        assert self._producer

        payload = {
            "specversion": "1.0",
            "type": f"crm.intelligence.{event_type}",
            "source": "/services/agents",
            "id": str(uuid4()),
            "time": None,
            "datacontenttype": "application/json",
            "tenantid": tenant_id,
            "correlationid": correlation_id,
            "data": data,
        }
        payload["time"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        try:
            await self._producer.send_and_wait(topic, json.dumps(payload, separators=(",", ":")))
        except Exception:
            return

    async def _filter_with_opa(self, *, tenant_id: str, user_id: str, roles: list[str], results: list[Any]) -> list[Any]:
        if not results:
            return []

        async def check(r: Any) -> tuple[Any, bool]:
            input_obj = {
                "tenant_id": tenant_id,
                "user": {"id": user_id, "roles": roles},
                "action": f"{_plural(r.entity_type)}:read",
                "resource": {"type": _plural(r.entity_type), "id": r.entity_id, "tenant_id": tenant_id},
                "actor_type": "user",
            }
            paths = ["enterprise_crm/tenant_isolation", "enterprise_crm/rbac"]
            decisions = await asyncio.gather(
                *[self._opa.evaluate(policy_path=p, input_obj=input_obj) for p in paths],
                return_exceptions=True,
            )
            allow = True
            for d in decisions:
                if isinstance(d, Exception):
                    allow = False
                    break
                if not getattr(d, "allow", False):
                    allow = False
                    break
            return r, allow

        checked = await asyncio.gather(*[check(r) for r in results[:20]])
        allowed_set = {r.entity_id for (r, ok) in checked if ok}
        return [r for r in results if r.entity_id in allowed_set]


def _plural(entity_type: str) -> str:
    if entity_type == "knowledge":
        return "knowledge"
    return entity_type if entity_type.endswith("s") else f"{entity_type}s"


def _env(key: str, default: str) -> str:
    import os

    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _topic(name: str) -> str:
    mapping = {
        "UserQuery": "crm.intelligence.user-query",
        "SearchPerformed": "crm.intelligence.search-performed",
        "SearchClicked": "crm.intelligence.search-clicked",
        "SearchAbandoned": "crm.intelligence.search-abandoned",
    }
    return mapping.get(name, f"crm.intelligence.{name}")

