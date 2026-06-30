from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import StateGraph

from .compliance_agent import AuditSearchFilters, ComplianceIntelligenceAgent


@dataclass
class AuditSearchState:
    tenant_id: str
    query: str
    filters: AuditSearchFilters | None = None
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditSearchDeps:
    agent: ComplianceIntelligenceAgent


def build_audit_search_graph(*, deps: AuditSearchDeps):
    g: StateGraph = StateGraph(AuditSearchState)

    async def _search(state: AuditSearchState) -> AuditSearchState:
        state.result = await deps.agent.semantic_audit_search(tenant_id=state.tenant_id, query=state.query, filters=state.filters, top_k=20)
        return state

    g.add_node("search", _search)
    g.set_entry_point("search")
    g.set_finish_point("search")
    return g.compile()

