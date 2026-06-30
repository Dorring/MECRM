from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from langgraph.graph import END, StateGraph
from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError

from .draft_generator import DraftOutput, parse_drafts
from .proposals import ActionProposal, compute_dedupe_key, new_proposal_id


Priority = Literal["low", "medium", "high"]
ActionType = Literal["reminder", "followup", "task"]

tracer = trace.get_tracer(__name__)


class ActionReasoning(BaseModel):
    action_type: ActionType = "reminder"
    target_entity: str = "unknown"
    target_entity_id: str = Field(default="")
    priority: Priority = "medium"
    justification: str = Field(default="")


class LlmCaller(Protocol):
    async def call_llm(self, prompt: str, *, tenant_id: str) -> str: ...


class ContextStore(Protocol):
    async def build_context(self, *, tenant_id: str, signal: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ProductivityDeps:
    llm: LlmCaller
    context: ContextStore


@dataclass
class ProductivityState:
    tenant_id: str
    signal: dict[str, Any]
    context: dict[str, Any] | None = None
    reasoning: ActionReasoning | None = None
    drafts: DraftOutput | None = None
    proposal: ActionProposal | None = None


def build_productivity_graph(*, deps: ProductivityDeps):
    g: StateGraph = StateGraph(ProductivityState)

    async def intake(state: ProductivityState) -> ProductivityState:
        return state

    async def build_context(state: ProductivityState) -> ProductivityState:
        state.context = await deps.context.build_context(tenant_id=state.tenant_id, signal=state.signal)
        return state

    async def reason(state: ProductivityState) -> ProductivityState:
        with tracer.start_as_current_span("reasoning"):
            prompt = (
                "You are a personal productivity agent for an Enterprise CRM.\n"
                "Given a signal and CRM context, propose a helpful action that requires human approval.\n"
                "Return ONLY valid JSON in this schema:\n"
                "{\n"
                "  \"action_type\": \"reminder|followup|task\",\n"
                "  \"target_entity\": \"lead|ticket|customer|task\",\n"
                "  \"target_entity_id\": \"...\",\n"
                "  \"priority\": \"low|medium|high\",\n"
                "  \"justification\": \"...\"\n"
                "}\n"
                "Constraints:\n"
                "- Never execute; only propose.\n"
                "- Justification must be specific and non-empty.\n"
                f"Signal: {json.dumps(state.signal, separators=(',', ':'), ensure_ascii=False)}\n"
                f"Context: {json.dumps(state.context or {}, separators=(',', ':'), ensure_ascii=False)}\n"
            )
            raw = await deps.llm.call_llm(prompt, tenant_id=state.tenant_id)
            parsed = _parse_json_obj(raw)
            try:
                state.reasoning = ActionReasoning.model_validate(parsed)
            except ValidationError:
                state.reasoning = ActionReasoning(
                    action_type="reminder",
                    target_entity=str((state.context or {}).get("entity_type") or "unknown"),
                    target_entity_id=str((state.context or {}).get("entity_id") or ""),
                    priority="medium",
                    justification="Follow up based on detected inactivity.",
                )
            if not (state.reasoning.justification or "").strip():
                state.reasoning = state.reasoning.model_copy(update={"justification": "Follow up based on detected inactivity."})
            return state

    async def draft(state: ProductivityState) -> ProductivityState:
        with tracer.start_as_current_span("draft_generation"):
            r = state.reasoning or ActionReasoning()
            prompt = (
                "Draft professional, concise messages for the proposed action.\n"
                "Return ONLY valid JSON:\n"
                "{\n"
                "  \"email\": {\"subject\": \"...\", \"body\": \"...\"},\n"
                "  \"whatsapp\": {\"message\": \"...\"},\n"
                "  \"task\": {\"description\": \"...\"}\n"
                "}\n"
                "You may omit channels by returning null values.\n"
                f"Action: {r.model_dump()}\n"
                f"Context: {json.dumps(state.context or {}, separators=(',', ':'), ensure_ascii=False)}\n"
            )
            raw = await deps.llm.call_llm(prompt, tenant_id=state.tenant_id)
            state.drafts = parse_drafts(raw)
            return state

    async def propose(state: ProductivityState) -> ProductivityState:
        r = state.reasoning or ActionReasoning()
        ctx = state.context or {}
        user_id = str(ctx.get("owner_user_id") or ctx.get("assigned_to") or ctx.get("fallback_user_id") or "")
        target_entity = str(r.target_entity or ctx.get("entity_type") or "unknown")
        target_id = str(r.target_entity_id or ctx.get("entity_id") or "")
        if not user_id:
            user_id = str(ctx.get("fallback_user_id") or "")
        dedupe_key = compute_dedupe_key(
            tenant_id=state.tenant_id,
            user_id=user_id,
            action_type=r.action_type,
            target_entity=target_entity,
            target_id=target_id,
            signal_type=str(state.signal.get("type") or "unknown"),
        )
        state.proposal = ActionProposal(
            proposal_id=new_proposal_id(),
            tenant_id=state.tenant_id,
            user_id=user_id,
            action_type=r.action_type,
            target_entity=target_entity,
            target_id=target_id,
            priority=r.priority,
            justification=r.justification.strip(),
            drafts=(state.drafts.to_json() if state.drafts else {}),
            created_at=_utc_now(),
            dedupe_key=dedupe_key,
            signal_type=str(state.signal.get("type") or "unknown"),
            signal=state.signal,
        )
        return state

    g.add_node("intake", intake)
    g.add_node("context", build_context)
    g.add_node("reason", reason)
    g.add_node("draft", draft)
    g.add_node("propose", propose)

    g.set_entry_point("intake")
    g.add_edge("intake", "context")
    g.add_edge("context", "reason")
    g.add_edge("reason", "draft")
    g.add_edge("draft", "propose")
    g.add_edge("propose", END)
    return g.compile()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

