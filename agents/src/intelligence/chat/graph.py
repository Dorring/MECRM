from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

import structlog
from langgraph.graph import END, StateGraph
from langchain_ollama import ChatOllama
from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError

from .memory import ChatMemoryItem, utc_now_iso


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


IntentType = Literal["read", "write", "question"]
EntityType = Literal["lead", "ticket", "customer", "invoice", "task", "unknown"]


class ChatIntent(BaseModel):
    intent: IntentType = "question"
    entity: EntityType = "unknown"
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any | None = None
    error: str | None = None


class ToolExecutor(Protocol):
    async def execute(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        authorization: str | None,
        correlation_id: str | None,
        call: ToolCall,
    ) -> ToolResult: ...


class MemoryStore(Protocol):
    async def load_window(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def append(self, *, item: ChatMemoryItem) -> None: ...


@dataclass
class ChatDeps:
    llm: ChatOllama
    tool_executor: ToolExecutor
    memory: MemoryStore | None
    memory_window: int


@dataclass
class ChatState:
    query: str
    conversation_id: str | None = None
    normalized_query: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    roles: list[str] | None = None
    authorization: str | None = None
    correlation_id: str | None = None
    history: list[dict[str, Any]] | None = None
    intent: ChatIntent | None = None
    intent_raw: str | None = None
    intent_error: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    response_text: str | None = None
    suggested_replies: list[str] | None = None
    action_proposals: list[dict[str, Any]] | None = None


def build_chat_graph(*, deps: ChatDeps):
    g: StateGraph = StateGraph(ChatState)

    async def normalize(state: ChatState) -> ChatState:
        with tracer.start_as_current_span("input_normalization"):
            q = (state.query or "").strip()
            state.normalized_query = " ".join(q.split())
            if not state.conversation_id:
                state.conversation_id = str(uuid4())
            if deps.memory and state.tenant_id and state.conversation_id:
                try:
                    state.history = await deps.memory.load_window(
                        tenant_id=state.tenant_id,
                        conversation_id=state.conversation_id,
                        limit=deps.memory_window,
                    )
                except Exception:
                    state.history = []
            else:
                state.history = state.history or []
            return state

    async def intent(state: ChatState) -> ChatState:
        with tracer.start_as_current_span("intent_classification"):
            prompt = (
                "Classify the user message for a CRM copilot.\n"
                "Return ONLY valid JSON matching this schema:\n"
                '{\"intent\":\"read|write|question\",\"entity\":\"lead|ticket|invoice|customer|task|unknown\",\"confidence\":0.0}\n'
                "Rules:\n"
                "- read: user wants to retrieve CRM entities now\n"
                "- write: user wants to create/update/delete CRM entities (never execute)\n"
                "- question: user is asking for info/explanations; use vector search\n"
                "User message:\n"
                f"{state.normalized_query or state.query}\n"
            )
            raw = None
            try:
                msg = await deps.llm.ainvoke(prompt)
                raw = getattr(msg, "content", None) or str(msg)
                state.intent_raw = raw
                parsed = _parse_json_obj(raw)
                state.intent = ChatIntent.model_validate(parsed)
                state.intent_error = None
            except ValidationError as ve:
                state.intent = ChatIntent()
                state.intent_error = str(ve)
            except Exception as e:
                state.intent = ChatIntent()
                state.intent_error = str(e)
            return state

    async def select_tool(state: ChatState) -> ChatState:
        with tracer.start_as_current_span("tool_selection"):
            intent_obj = state.intent or ChatIntent()
            ent = intent_obj.entity
            q = (state.normalized_query or state.query or "").lower()
            wants_risk = any(k in q for k in ("risk", "churn", "stage", "timeline", "prediction", "sla", "escalation"))
            if intent_obj.intent == "read":
                if ent in ("lead", "ticket", "customer", "invoice", "task"):
                    state.tool_call = ToolCall(tool=f"crm_reader.get_{_plural(ent)}", args={"limit": 10})
                else:
                    state.tool_call = ToolCall(tool="vector_search.search", args={"query": state.normalized_query or state.query, "top_k": 8})
                state.action_proposals = []
            elif intent_obj.intent == "write":
                state.tool_call = ToolCall(tool="crm_writer.propose", args={"raw": state.normalized_query or state.query})
                state.action_proposals = []
            else:
                if ent == "customer" and wants_risk:
                    state.tool_call = ToolCall(tool="crm_reader.get_customer_risks", args={"limit": 10})
                else:
                    state.tool_call = ToolCall(tool="vector_search.search", args={"query": state.normalized_query or state.query, "top_k": 8})
                state.action_proposals = []
            return state

    async def execute_tool(state: ChatState) -> ChatState:
        with tracer.start_as_current_span("tool_execution"):
            if not state.tool_call or not state.tenant_id or not state.user_id:
                state.tool_result = ToolResult(tool="none", ok=False, error="missing_context")
                return state
            try:
                res = await deps.tool_executor.execute(
                    tenant_id=state.tenant_id,
                    user_id=state.user_id,
                    roles=state.roles or [],
                    authorization=state.authorization,
                    correlation_id=state.correlation_id,
                    call=state.tool_call,
                )
                state.tool_result = res
            except Exception as e:
                state.tool_result = ToolResult(tool=state.tool_call.tool, ok=False, error=str(e))
            return state

    async def respond(state: ChatState) -> ChatState:
        with tracer.start_as_current_span("response_generation"):
            intent_obj = state.intent or ChatIntent()
            tr = state.tool_result
            if tr and tr.ok:
                if tr.tool == "crm_writer.propose" and isinstance(tr.data, dict) and isinstance(tr.data.get("proposal"), dict):
                    state.action_proposals = [tr.data["proposal"]]
                state.response_text = _render_response(intent=intent_obj.intent, entity=intent_obj.entity, data=tr.data)
            else:
                state.response_text = "I couldn’t complete that request safely. Try rephrasing, or ask to search."
            state.suggested_replies = _suggest_replies(intent_obj)
            return state

    async def persist(state: ChatState) -> ChatState:
        if deps.memory and state.tenant_id and state.user_id and state.conversation_id:
            try:
                await deps.memory.append(
                    item=ChatMemoryItem(
                        conversation_id=state.conversation_id,
                        tenant_id=state.tenant_id,
                        user_id=state.user_id,
                        role="user",
                        message=state.normalized_query or state.query,
                        timestamp=utc_now_iso(),
                    )
                )
                await deps.memory.append(
                    item=ChatMemoryItem(
                        conversation_id=state.conversation_id,
                        tenant_id=state.tenant_id,
                        user_id=state.user_id,
                        role="assistant",
                        message=state.response_text or "",
                        timestamp=utc_now_iso(),
                    )
                )
            except Exception:
                return state
        return state

    g.add_node("normalize", normalize)
    g.add_node("intent", intent)
    g.add_node("tool", select_tool)
    g.add_node("execute", execute_tool)
    g.add_node("respond", respond)
    g.add_node("persist", persist)

    g.set_entry_point("normalize")
    g.add_edge("normalize", "intent")
    g.add_edge("intent", "tool")
    g.add_edge("tool", "execute")
    g.add_edge("execute", "respond")
    g.add_edge("respond", "persist")
    g.add_edge("persist", END)
    return g.compile()


def _parse_json_obj(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return {}
    return {}


def _plural(entity: str) -> str:
    if entity in ("lead", "ticket", "customer", "invoice", "task"):
        return f"{entity}s"
    return "entities"


def _render_response(*, intent: str, entity: str, data: Any) -> str:
    if intent == "read":
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            items = data["data"]
            if not items:
                return f"I didn’t find any {entity}s."
            top = items[:5]
            lines = []
            for it in top:
                title = (it.get("name") or it.get("subject") or it.get("id") or "").strip()
                lines.append(f"- {title} ({it.get('id')})")
            suffix = "" if len(items) <= 5 else f"\nShowing 5 of {len(items)}."
            return f"Here are the latest {entity}s:\n" + "\n".join(lines) + suffix
        return f"Here’s what I found for {entity}."
    if intent == "write":
        return "I can propose that change for approval, but I won’t execute it."
    return "Here’s what I found."


def _suggest_replies(intent: ChatIntent) -> list[str]:
    if intent.intent == "read":
        return ["Filter by status", "Show the most recent ones", "Open one by ID"]
    if intent.intent == "write":
        return ["Show the proposed action", "Modify the proposal", "Cancel"]
    return ["Search related records", "Summarize the key points", "Show supporting items"]

