from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg
from langgraph.graph import END, StateGraph

from .feature_extractor import extract_customer_features, extract_lead_features, extract_ticket_features
from .predictors import Prediction, predict_churn, predict_conversion, predict_escalation, predict_sla_breach


@dataclass
class AnalyticsDeps:
    conn: asyncpg.Connection


@dataclass
class AnalyticsState:
    tenant_id: str
    journey_event: dict[str, Any]
    predictions: list[Prediction] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_analytics_graph(*, deps: AnalyticsDeps):
    g: StateGraph = StateGraph(AnalyticsState)

    async def predict(state: AnalyticsState) -> AnalyticsState:
        data = state.journey_event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        tenant_id = state.tenant_id
        customer_id = str(data.get("customer_id") or "")
        stage = str(data.get("stage") or "awareness")
        timeline = data.get("timeline_entry") or {}
        if not isinstance(timeline, dict):
            timeline = {}

        out: list[Prediction] = []

        if customer_id:
            cust_features = (await extract_customer_features(tenant_id=tenant_id, customer_id=customer_id, conn=deps.conn)).values
            out.append(predict_churn(customer_id=customer_id, stage=stage, features=cust_features))
            out.append(predict_conversion(entity_type="customer", entity_id=customer_id, stage=stage, features=cust_features))

        if str(timeline.get("event_type") or "").startswith("ticket."):
            payload = timeline.get("event_payload") or {}
            if isinstance(payload, dict):
                ticket_id = str(payload.get("ticket_id") or "")
                if ticket_id:
                    tf = (await extract_ticket_features(tenant_id=tenant_id, ticket_id=ticket_id, conn=deps.conn)).values
                    out.append(predict_sla_breach(ticket_id=ticket_id, features=tf))
                    out.append(predict_escalation(ticket_id=ticket_id, features=tf))

        if str(timeline.get("event_type") or "").startswith("deal."):
            payload = timeline.get("event_payload") or {}
            if isinstance(payload, dict):
                deal_id = str(payload.get("deal_id") or "")
                if deal_id:
                    row = await deps.conn.fetchrow(
                        "SELECT lead_id::text as lead_id FROM deals WHERE tenant_id = $1::uuid AND id = $2::uuid",
                        tenant_id,
                        deal_id,
                    )
                    lead_id = str(row["lead_id"]) if row and row.get("lead_id") else ""
                    if lead_id:
                        lf = (await extract_lead_features(tenant_id=tenant_id, lead_id=lead_id, conn=deps.conn)).values
                        out.append(predict_conversion(entity_type="lead", entity_id=lead_id, stage=stage, features=lf))

        state.predictions = out
        return state

    g.add_node("predict", predict)
    g.set_entry_point("predict")
    g.add_edge("predict", END)

    return g.compile()

