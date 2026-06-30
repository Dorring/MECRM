from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


RiskLevel = Literal["green", "yellow", "red"]


@dataclass(frozen=True)
class Prediction:
    entity_type: str
    entity_id: str
    prediction_type: str
    probability: float
    risk_level: RiskLevel
    explanation: str
    features: dict[str, Any]
    model_version: str = "heuristic_v1"


def risk_level_from_probability(p: float) -> RiskLevel:
    if p < 0.3:
        return "green"
    if p < 0.6:
        return "yellow"
    return "red"


def _clamp01(p: float) -> float:
    if p < 0:
        return 0.0
    if p > 1:
        return 1.0
    return float(p)


def predict_churn(*, customer_id: str, stage: str, features: dict[str, Any]) -> Prediction:
    base = 0.15
    if stage == "churn_risk":
        base = 0.7
    elif stage == "hesitation":
        base = 0.45
    elif stage == "converted":
        base = 0.2

    overdue = int(features.get("overdue_tickets") or 0)
    open_high = int(features.get("open_high_tickets") or 0)
    tickets_30d = int(features.get("tickets_30d") or 0)

    p = base + min(0.25, overdue * 0.15) + min(0.2, open_high * 0.07) + min(0.2, tickets_30d * 0.02)
    p = _clamp01(p)
    level = risk_level_from_probability(p)

    explanation = "Low churn risk based on recent activity."
    if level == "yellow":
        explanation = "Moderate churn risk: elevated support activity or slower progress."
    if level == "red":
        explanation = "High churn risk: overdue or high-priority unresolved tickets indicate customer distress."

    return Prediction(
        entity_type="customer",
        entity_id=customer_id,
        prediction_type="churn",
        probability=p,
        risk_level=level,
        explanation=explanation,
        features=features,
    )


def predict_conversion(*, entity_type: str, entity_id: str, stage: str, features: dict[str, Any]) -> Prediction:
    base = 0.25
    latest_stage = str(features.get("latest_deal_stage") or "")
    deal_age_days = features.get("deal_age_days")

    if latest_stage in {"proposal"}:
        base = 0.55
    elif latest_stage in {"negotiation"}:
        base = 0.68
    elif latest_stage in {"qualification"}:
        base = 0.45
    elif latest_stage in {"prospecting"}:
        base = 0.35

    if stage == "converted":
        base = 0.9

    if deal_age_days is not None:
        if deal_age_days > 45:
            base -= 0.2
        elif deal_age_days > 20:
            base -= 0.1

    p = _clamp01(base)
    level = risk_level_from_probability(p)
    explanation = "Conversion likelihood inferred from deal stage and deal age."
    if stage == "converted":
        explanation = "Already converted: closed-won deal indicates high conversion probability."

    return Prediction(
        entity_type=entity_type,
        entity_id=entity_id,
        prediction_type="conversion",
        probability=p,
        risk_level=level,
        explanation=explanation,
        features=features,
    )


def predict_sla_breach(*, ticket_id: str, features: dict[str, Any]) -> Prediction:
    if features.get("missing"):
        return Prediction(
            entity_type="ticket",
            entity_id=ticket_id,
            prediction_type="sla",
            probability=0.0,
            risk_level="green",
            explanation="Insufficient ticket data for SLA prediction.",
            features=features,
        )

    priority = str(features.get("priority") or "medium")
    time_to_sla = features.get("time_to_sla_hours")
    overdue = bool(features.get("overdue"))

    base = 0.2
    if priority == "urgent":
        base = 0.45
    elif priority == "high":
        base = 0.35

    if overdue:
        p = 0.9
    elif time_to_sla is None:
        p = base
    elif time_to_sla <= 1:
        p = base + 0.35
    elif time_to_sla <= 4:
        p = base + 0.2
    else:
        p = base

    p = _clamp01(p)
    level = risk_level_from_probability(p)
    explanation = "SLA breach likelihood inferred from priority and time-to-SLA."
    if overdue:
        explanation = "SLA breach is highly likely: the ticket is already past its SLA due time."

    return Prediction(
        entity_type="ticket",
        entity_id=ticket_id,
        prediction_type="sla",
        probability=p,
        risk_level=level,
        explanation=explanation,
        features=features,
    )


def predict_escalation(*, ticket_id: str, features: dict[str, Any]) -> Prediction:
    if features.get("missing"):
        return Prediction(
            entity_type="ticket",
            entity_id=ticket_id,
            prediction_type="escalation",
            probability=0.0,
            risk_level="green",
            explanation="Insufficient ticket data for escalation prediction.",
            features=features,
        )

    priority = str(features.get("priority") or "medium")
    overdue = bool(features.get("overdue"))
    age_hours = features.get("age_hours") or 0

    p = 0.15
    if priority in {"high", "urgent"}:
        p += 0.2
    if overdue:
        p += 0.4
    if age_hours and age_hours > 48:
        p += 0.15

    p = _clamp01(p)
    level = risk_level_from_probability(p)
    explanation = "Escalation risk inferred from priority, age, and SLA status."
    if level == "red":
        explanation = "High escalation risk: overdue or long-running high-priority ticket."

    return Prediction(
        entity_type="ticket",
        entity_id=ticket_id,
        prediction_type="escalation",
        probability=p,
        risk_level=level,
        explanation=explanation,
        features=features,
    )

