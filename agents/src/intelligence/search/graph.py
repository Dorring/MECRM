from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, StateGraph

from .intent_parser import IntentParseResult, SearchIntent, parse_intent
from .ranker import RankedResult, rank_results
from .retriever import RetrievedResult
from .suggestions import Suggestion, generate_suggestions


@dataclass
class SearchState:
    query: str
    normalized_query: str | None = None
    module: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    roles: list[str] | None = None
    intent: SearchIntent | None = None
    intent_raw: str | None = None
    intent_error: str | None = None
    retrieved: list[RetrievedResult] | None = None
    ranked: list[RankedResult] | None = None
    suggestions: list[Suggestion] | None = None
    timings_ms: dict[str, float] | None = None


def build_search_graph(*, deps: Any):
    g: StateGraph = StateGraph(SearchState)

    async def normalize(state: SearchState) -> SearchState:
        q = (state.query or "").strip()
        state.normalized_query = " ".join(q.split())
        return state

    async def intent(state: SearchState) -> SearchState:
        res: IntentParseResult = await parse_intent(llm=deps.llm, query=state.normalized_query or state.query)
        state.intent = res.intent
        state.intent_raw = res.raw
        state.intent_error = res.error
        return state

    async def retrieve(state: SearchState) -> SearchState:
        assert state.tenant_id
        ent = None
        if state.intent and state.intent.entity != "unknown":
            ent = state.intent.entity

        structured, semantic, kb = await asyncio.gather(
            deps.retriever.structured_search(
                tenant_id=state.tenant_id,
                query=state.normalized_query or state.query,
                entity=ent if ent in ("lead", "deal", "ticket", "customer") else None,
                filters=(state.intent.filters if state.intent else None),
                limit=deps.limit,
            ),
            deps.retriever.semantic_search(
                tenant_id=state.tenant_id,
                query=state.normalized_query or state.query,
                entity=ent if ent in ("lead", "deal", "ticket", "customer") else None,
                limit=deps.limit,
            ),
            deps.retriever.semantic_search_knowledge(
                tenant_id=state.tenant_id,
                query=state.normalized_query or state.query,
                limit=max(3, int(deps.limit / 2)),
            ),
        )
        state.retrieved = (structured or []) + (semantic or []) + (kb or [])
        return state

    async def rank(state: SearchState) -> SearchState:
        roles = state.roles or []
        state.ranked = rank_results(
            query=state.normalized_query or state.query,
            roles=roles,
            module=state.module,
            results=state.retrieved or [],
            limit=deps.limit,
        )
        return state

    async def suggest(state: SearchState) -> SearchState:
        top = state.ranked[0].entity_type if state.ranked else None
        state.suggestions = generate_suggestions(intent=state.intent or SearchIntent(), roles=state.roles or [], top_entity_type=top)
        return state

    async def done(state: SearchState) -> SearchState:
        return state

    g.add_node("normalize", normalize)
    g.add_node("intent", intent)
    g.add_node("retrieve", retrieve)
    g.add_node("rank", rank)
    g.add_node("suggest", suggest)
    g.add_node("done", done)

    g.set_entry_point("normalize")
    g.add_edge("normalize", "intent")
    g.add_edge("intent", "retrieve")
    g.add_edge("retrieve", "rank")
    g.add_edge("rank", "suggest")
    g.add_edge("suggest", "done")
    g.add_edge("done", END)
    return g.compile()

