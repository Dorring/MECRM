from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg
from langgraph.graph import END, StateGraph

from .stage_classifier import StageResult, classify_stage
from .timeline_builder import TimelineEntry, build_timeline_entries


@dataclass
class JourneyDeps:
    conn: asyncpg.Connection


@dataclass
class JourneyState:
    tenant_id: str
    event: dict[str, Any]
    timeline_entries: list[TimelineEntry] | None = None
    stage_by_customer: dict[str, StageResult] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_journey_graph(*, deps: JourneyDeps):
    g: StateGraph = StateGraph(JourneyState)

    async def build_entries(state: JourneyState) -> JourneyState:
        state.timeline_entries = await build_timeline_entries(tenant_id=state.tenant_id, event=state.event, conn=deps.conn)
        return state

    async def classify(state: JourneyState) -> JourneyState:
        out: dict[str, StageResult] = {}
        for e in state.timeline_entries or []:
            if e.customer_id and e.customer_id not in out:
                out[e.customer_id] = await classify_stage(tenant_id=state.tenant_id, customer_id=e.customer_id, conn=deps.conn)
        state.stage_by_customer = out
        return state

    g.add_node("build_entries", build_entries)
    g.add_node("classify", classify)
    g.set_entry_point("build_entries")
    g.add_edge("build_entries", "classify")
    g.add_edge("classify", END)

    return g.compile()

