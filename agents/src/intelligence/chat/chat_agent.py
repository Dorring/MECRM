from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from aiokafka import AIOKafkaProducer
from intelligence.providers import create_chat_model
from opentelemetry import trace

from orchestrator.config import settings
from governance.agent_telemetry import inc_error, observe_chat_latency

from .graph import ChatDeps, ChatIntent, ChatState, ToolCall, ToolResult, build_chat_graph
from .memory import WeaviateChatMemory


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass
class ChatResponse:
    conversation_id: str
    intent: dict[str, Any]
    message: str
    suggested_replies: list[str]
    action_proposals: list[dict[str, Any]]
    debug: dict[str, Any]


class ChatAgent:
    agent_id = "chat-agent"

    def __init__(self, *, tool_executor: Any, memory: Any | None = None):
        self._producer: AIOKafkaProducer | None = None
        self._producer_lock = asyncio.Lock()
        self._llm = create_chat_model(temperature=0)
        if memory is None:
            memory = WeaviateChatMemory(
                weaviate_url=settings.WEAVIATE_URL,
                ollama_url=settings.OLLAMA_URL,
                embedding_model=settings.OLLAMA_EMBED_MODEL,
            )
        self._deps = ChatDeps(llm=self._llm, tool_executor=tool_executor, memory=memory, memory_window=12)
        self._graph = build_chat_graph(deps=self._deps)

    async def start(self) -> None:
        await self._ensure_producer()

    async def close(self) -> None:
        async with self._producer_lock:
            if self._producer:
                await self._producer.stop()
                self._producer = None

    async def chat(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        authorization: str | None,
        correlation_id: str | None,
        conversation_id: str | None,
        query: str,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        await self._emit_event(
            topic=_topic("UserQuery"),
            event_type="user-query",
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            data={"conversationId": conversation_id, "query": query, "userId": user_id},
        )
        with tracer.start_as_current_span("intelligence.chat") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("user_id", user_id)
            if conversation_id:
                span.set_attribute("conversation_id", conversation_id)
            span.set_attribute("query_len", len(query or ""))

            state = ChatState(
                query=query,
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
                authorization=authorization,
                correlation_id=correlation_id,
            )
            try:
                raw_out: Any = await self._graph.ainvoke(state)
            except Exception as e:
                duration_ms = (time.perf_counter() - t0) * 1000.0
                observe_chat_latency(agent_id=self.agent_id, status="error", duration_ms=duration_ms)
                inc_error(agent_id=self.agent_id, error_type="chat_graph_failed")
                await self._emit_event(
                    topic=_topic("AgentDecision"),
                    event_type="agent-decision",
                    tenant_id=tenant_id,
                    correlation_id=correlation_id,
                    data={"conversationId": conversation_id, "error": str(e)},
                )
                return ChatResponse(
                    conversation_id=conversation_id or str(uuid4()),
                    intent={"intent": "question", "entity": "unknown", "confidence": 0.0},
                    message="I couldn’t complete that request safely. Please try again.",
                    suggested_replies=["Try searching instead", "Ask for recent leads", "Ask for open tickets"],
                    action_proposals=[],
                    debug={"error": str(e)},
                ).__dict__

        out = _coerce_state(raw_out)
        intent = out.intent.model_dump() if out.intent else {"intent": "question", "entity": "unknown", "confidence": 0.0}
        duration_ms = (time.perf_counter() - t0) * 1000.0
        observe_chat_latency(agent_id=self.agent_id, status="ok", duration_ms=duration_ms)

        await self._emit_event(
            topic=_topic("AgentDecision"),
            event_type="agent-decision",
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            data={"conversationId": out.conversation_id, "intent": intent, "tool": (out.tool_call.tool if out.tool_call else None)},
        )
        if out.tool_call:
            await self._emit_event(
                topic=_topic("ToolCalled"),
                event_type="tool-called",
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                data={
                    "conversationId": out.conversation_id,
                    "tool": out.tool_call.tool,
                    "ok": bool(out.tool_result.ok) if out.tool_result else False,
                    "error": (out.tool_result.error if out.tool_result else None),
                },
            )
        if out.action_proposals:
            for p in out.action_proposals[:3]:
                await self._emit_event(
                    topic=_topic("ActionSuggested"),
                    event_type="action-suggested",
                    tenant_id=tenant_id,
                    correlation_id=correlation_id,
                    data={"conversationId": out.conversation_id, "proposal": p, "userId": user_id},
                )

        return ChatResponse(
            conversation_id=str(out.conversation_id),
            intent=intent,
            message=str(out.response_text or ""),
            suggested_replies=list(out.suggested_replies or []),
            action_proposals=list(out.action_proposals or []),
            debug={"intent_raw": out.intent_raw, "intent_error": out.intent_error, "tool": (out.tool_call.tool if out.tool_call else None)},
        ).__dict__

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
        except Exception as e:
            inc_error(agent_id=self.agent_id, error_type="kafka_emit_failed")
            logger.warning("chat.emit_failed", topic=topic, error=str(e))
            return


def _topic(name: str) -> str:
    mapping = {
        "UserQuery": "crm.intelligence.user-query",
        "AgentDecision": "crm.intelligence.agent-decision",
        "ToolCalled": "crm.intelligence.tool-called",
        "ActionSuggested": "crm.intelligence.action-suggested",
    }
    return mapping.get(name, f"crm.intelligence.{name}")


def _coerce_state(raw_out: Any) -> ChatState:
    if isinstance(raw_out, ChatState):
        return raw_out
    if not isinstance(raw_out, dict):
        return ChatState(query="", conversation_id=str(uuid4()))

    out = ChatState(**{k: v for k, v in raw_out.items() if k in ChatState.__dataclass_fields__})
    if isinstance(out.intent, dict):
        try:
            out.intent = ChatIntent.model_validate(out.intent)
        except Exception:
            out.intent = ChatIntent()
    if isinstance(out.tool_call, dict):
        try:
            out.tool_call = ToolCall(**out.tool_call)
        except Exception:
            out.tool_call = None
    if isinstance(out.tool_result, dict):
        try:
            out.tool_result = ToolResult(**out.tool_result)
        except Exception:
            out.tool_result = None
    return out

