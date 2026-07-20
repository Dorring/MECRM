"""Phase 3 plan templates.

Templates are pure data — they produce :class:`TaskIntent` lists
without consulting the registry.  The Planner is responsible for
binding each intent to a concrete agent.

Currently the only template is Customer Recovery, which generates a
5-intent DAG:

::

    customer_context          (required)
        ├── support_analysis          (required)
        ├── sales_risk_analysis       (required)
        ├── knowledge_recommendation  (optional)
        └── recovery_metrics          (optional)

All five intents are always emitted — ``required=False`` only signals
that a downstream executor may skip the task if no capable agent is
available; the Planner still attempts to bind every intent.
"""

from __future__ import annotations

from pydantic import field_validator

from multi_agent.contracts import AgentAuthority, StrictContract, _non_blank
from multi_agent.planning import TaskIntent

# ---------------------------------------------------------------------------
# Reason codes shared with the gate / planner
# ---------------------------------------------------------------------------

CUSTOMER_RECOVERY_DOMAIN = "customer_recovery"

# Stable intent IDs — these are NOT task_ids.  Task IDs are derived
# from (run_id, intent_id, task_type, agent_id) by the Planner.
INTENT_CUSTOMER_CONTEXT = "customer_context"
INTENT_SUPPORT_ANALYSIS = "support_analysis"
INTENT_SALES_RISK_ANALYSIS = "sales_risk_analysis"
INTENT_KNOWLEDGE_RECOMMENDATION = "knowledge_recommendation"
INTENT_RECOVERY_METRICS = "recovery_metrics"

_TASK_TYPE_CUSTOMER_CONTEXT = "customer_context_summary"
_TASK_TYPE_SUPPORT_ANALYSIS = "support_analysis"
_TASK_TYPE_SALES_RISK_ANALYSIS = "sales_risk_analysis"
_TASK_TYPE_KNOWLEDGE_RECOMMENDATION = "knowledge_recommendation"
_TASK_TYPE_RECOVERY_METRICS = "recovery_metrics"


# ---------------------------------------------------------------------------
# Template descriptor
# ---------------------------------------------------------------------------


class CustomerRecoveryTemplate(StrictContract):
    """Customer Recovery plan template.

    A frozen descriptor that emits 5 :class:`TaskIntent` objects.  The
    template never carries customer IDs, tenant IDs, or secrets — those
    are bound by the Planner from :class:`PlanningRequest`.
    """

    name: str = "customer_recovery"
    version: str = "ma-03.1.0"

    # Per-intent flags.  Defaults follow the Phase 3 review R1 spec:
    # context/support/sales are required; knowledge/metrics are optional.
    customer_context_required: bool = True
    support_analysis_required: bool = True
    sales_risk_analysis_required: bool = True
    knowledge_recommendation_required: bool = False
    recovery_metrics_required: bool = False

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        return _non_blank(v, "name")

    @field_validator("version")
    @classmethod
    def _version_required(cls, v: str) -> str:
        return _non_blank(v, "version")

    # -- API ---------------------------------------------------------------

    def build_intents(
        self, *, domain: str = CUSTOMER_RECOVERY_DOMAIN
    ) -> list[TaskIntent]:
        """Return the 5 TaskIntents for Customer Recovery.

        Order is deterministic: root first, then children in stable
        intent-id order.  Callers should not rely on list position
        for dependency resolution — ``dependencies`` is the source of
        truth.
        """
        root = INTENT_CUSTOMER_CONTEXT
        return [
            TaskIntent(
                intent_id=INTENT_CUSTOMER_CONTEXT,
                task_type=_TASK_TYPE_CUSTOMER_CONTEXT,
                domain=domain,
                objective="Summarise customer context for recovery planning",
                dependencies=[],
                required_evidence=[],
                required=self.customer_context_required,
                preferred_authority=AgentAuthority.READ,
                required_tools=frozenset({"crm_reader.get_customers"}),
                estimated_tool_calls=1,
                metadata={"template": self.name, "phase": "context"},
            ),
            TaskIntent(
                intent_id=INTENT_SUPPORT_ANALYSIS,
                task_type=_TASK_TYPE_SUPPORT_ANALYSIS,
                domain=domain,
                objective="Analyse recent support tickets and SLA breaches",
                dependencies=[root],
                required_evidence=[],
                required=self.support_analysis_required,
                preferred_authority=AgentAuthority.READ,
                required_tools=frozenset({"crm_reader.get_tickets"}),
                estimated_tool_calls=2,
                metadata={"template": self.name, "phase": "support"},
            ),
            TaskIntent(
                intent_id=INTENT_SALES_RISK_ANALYSIS,
                task_type=_TASK_TYPE_SALES_RISK_ANALYSIS,
                domain=domain,
                objective="Assess sales pipeline risk and renewal probability",
                dependencies=[root],
                required_evidence=[],
                required=self.sales_risk_analysis_required,
                preferred_authority=AgentAuthority.READ,
                required_tools=frozenset({"crm_reader.get_deals"}),
                estimated_tool_calls=2,
                metadata={"template": self.name, "phase": "sales"},
            ),
            TaskIntent(
                intent_id=INTENT_KNOWLEDGE_RECOMMENDATION,
                task_type=_TASK_TYPE_KNOWLEDGE_RECOMMENDATION,
                domain=domain,
                objective="Recommend knowledge articles for recovery",
                dependencies=[root],
                required_evidence=[],
                required=self.knowledge_recommendation_required,
                preferred_authority=AgentAuthority.READ,
                required_tools=frozenset({"vector_search.search"}),
                estimated_tool_calls=1,
                metadata={"template": self.name, "phase": "knowledge"},
            ),
            TaskIntent(
                intent_id=INTENT_RECOVERY_METRICS,
                task_type=_TASK_TYPE_RECOVERY_METRICS,
                domain=domain,
                objective="Compute recovery health metrics",
                dependencies=[root],
                required_evidence=[],
                required=self.recovery_metrics_required,
                preferred_authority=AgentAuthority.READ,
                required_tools=frozenset({"crm_reader.get_customers"}),
                estimated_tool_calls=1,
                metadata={"template": self.name, "phase": "metrics"},
            ),
        ]

    def expected_intent_ids(self) -> list[str]:
        """Return the stable list of intent IDs emitted by this template."""
        return [
            INTENT_CUSTOMER_CONTEXT,
            INTENT_SUPPORT_ANALYSIS,
            INTENT_SALES_RISK_ANALYSIS,
            INTENT_KNOWLEDGE_RECOMMENDATION,
            INTENT_RECOVERY_METRICS,
        ]

    def expected_dependencies(self) -> dict[str, list[str]]:
        """Return the canonical dependency map for tests / docs."""
        return {
            INTENT_CUSTOMER_CONTEXT: [],
            INTENT_SUPPORT_ANALYSIS: [INTENT_CUSTOMER_CONTEXT],
            INTENT_SALES_RISK_ANALYSIS: [INTENT_CUSTOMER_CONTEXT],
            INTENT_KNOWLEDGE_RECOMMENDATION: [INTENT_CUSTOMER_CONTEXT],
            INTENT_RECOVERY_METRICS: [INTENT_CUSTOMER_CONTEXT],
        }


# Singleton instance for default usage.
DEFAULT_CUSTOMER_RECOVERY_TEMPLATE = CustomerRecoveryTemplate()


__all__ = [
    "CUSTOMER_RECOVERY_DOMAIN",
    "DEFAULT_CUSTOMER_RECOVERY_TEMPLATE",
    "INTENT_CUSTOMER_CONTEXT",
    "INTENT_KNOWLEDGE_RECOMMENDATION",
    "INTENT_RECOVERY_METRICS",
    "INTENT_SALES_RISK_ANALYSIS",
    "INTENT_SUPPORT_ANALYSIS",
    "CustomerRecoveryTemplate",
]
