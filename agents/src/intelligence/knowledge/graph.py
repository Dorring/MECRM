from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import StateGraph

from .classifier import TopicClassificationResult, classify_topic
from .summarizer import DraftResult, generate_draft_from_conversation, generate_draft_from_ticket


SourceType = Literal["ticket_resolved", "conversation_closed"]


@dataclass
class KnowledgeDraftState:
    source_type: SourceType
    tenant_id: str
    source_id: str
    subject: str | None = None
    description: str | None = None
    resolution: str | None = None
    transcript: list[dict[str, Any]] | None = None
    draft: DraftResult | None = None
    classification: TopicClassificationResult | None = None


@dataclass(frozen=True)
class KnowledgeDraftDeps:
    llm: Any


def build_knowledge_draft_graph(*, deps: KnowledgeDraftDeps):
    g: StateGraph = StateGraph(KnowledgeDraftState)

    async def _summarize(state: KnowledgeDraftState) -> KnowledgeDraftState:
        if state.source_type == "ticket_resolved":
            state.draft = await generate_draft_from_ticket(
                llm=deps.llm,
                subject=state.subject or "",
                description=state.description,
                resolution=state.resolution,
            )
        else:
            state.draft = await generate_draft_from_conversation(
                llm=deps.llm,
                conversation_id=state.source_id,
                transcript=state.transcript or [],
            )
        return state

    async def _classify(state: KnowledgeDraftState) -> KnowledgeDraftState:
        if not state.draft:
            return state
        d = state.draft.draft
        state.classification = await classify_topic(llm=deps.llm, title=d.title, problem=d.problem_summary, resolution="; ".join(d.solution_steps))
        return state

    g.add_node("summarize", _summarize)
    g.add_node("classify", _classify)
    g.set_entry_point("summarize")
    g.add_edge("summarize", "classify")
    g.set_finish_point("classify")
    return g.compile()

