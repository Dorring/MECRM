from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .rule_parser import WorkflowSpec


_TRIGGER_TOPICS: dict[str, list[str]] = {
    "invoice_overdue": ["crm.invoices.updated"],
    "deal_updated": ["crm.deals.updated", "crm.deals.stage-changed", "crm.deals.closed"],
    "ticket_updated": ["crm.tickets.updated", "crm.tickets.sla-breached", "crm.tickets.created"],
    "customer_updated": ["crm.customers.updated", "crm.customers.created"],
    "prediction_generated": ["crm.analytics.prediction-generated"],
}

_ALLOWED_ACTION_TYPES = {"notify", "create_task", "propose_followup"}


@dataclass(frozen=True)
class CompiledWorkflow:
    trigger_type: str
    trigger_topics: list[str]
    conditions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_type": self.trigger_type,
            "trigger_topics": self.trigger_topics,
            "conditions": self.conditions,
            "actions": self.actions,
            "warnings": self.warnings,
        }


def compile_workflow(workflow: WorkflowSpec) -> CompiledWorkflow:
    warnings: list[str] = []
    trigger_type = workflow.trigger.strip()
    if not trigger_type:
        trigger_type = "customer_updated"
        warnings.append("missing_trigger_defaulted")

    topics = _TRIGGER_TOPICS.get(trigger_type)
    if not topics:
        warnings.append("unknown_trigger")
        topics = _TRIGGER_TOPICS["customer_updated"]
        trigger_type = "customer_updated"

    conditions = [c.model_dump() for c in workflow.conditions]

    actions: list[dict[str, Any]] = []
    for a in workflow.actions:
        d = a.model_dump()
        typ = str(d.get("type") or "")
        if typ not in _ALLOWED_ACTION_TYPES:
            warnings.append("disallowed_action")
            continue
        actions.append(d)

    if not actions:
        warnings.append("no_actions_after_validation")

    return CompiledWorkflow(
        trigger_type=trigger_type,
        trigger_topics=list(topics),
        conditions=conditions,
        actions=actions,
        warnings=warnings,
    )

