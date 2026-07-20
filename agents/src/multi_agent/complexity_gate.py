"""Phase 3 Complexity Gate.

Decides whether a request should:

* ``deterministic_workflow`` — handled by the existing AgentRouter
  / Kafka handlers, no Supervisor graph;
* ``single_agent`` — one Specialist suffices;
* ``multi_agent`` — multiple Specialists must collaborate.

This module is **deterministic** — no LLM, no network.  Every decision
is reproducible from ``(request, registry)`` alone.
"""

from __future__ import annotations

from typing import Protocol

from multi_agent.contracts import AgentAuthority, ComplexityDecision
from multi_agent.registry import AgentRegistry
from multi_agent.planning import PlanningRequest, PlanningSignals
from multi_agent.planning_errors import (
    InsufficientContextError,
    PlanningInputError,
    RegistryVersionMismatchError,
    UnsupportedCapabilityError,
)

# ---------------------------------------------------------------------------
# Deterministic event allowlist
# ---------------------------------------------------------------------------

#: Canonical event-type names that ALWAYS route to deterministic_workflow.
#: These are NOT Kafka topic names; they are upper-layer semantic event
#: categories produced by the existing router / handlers.
DETERMINISTIC_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ticket.sla_breached",
        "approval.resolved",
        "audit.event_recorded",
        "lifecycle.stage_changed",
        "automation.triggered",
    }
)

#: Mapping from existing Kafka topics to canonical event_type names.
#: Phase 3 does NOT subscribe to Kafka — this map exists only so that
#: upstream callers (and the docs) can translate router topics into the
#: PlanningSignals.event_type namespace unambiguously.
KAFKA_TOPIC_TO_EVENT_TYPE: dict[str, str] = {
    "crm.tickets.sla-breached": "ticket.sla_breached",
    "crm.approvals.decision": "approval.resolved",
    "crm.knowledge.published": "audit.event_recorded",
    "crm.conversations.closed": "lifecycle.stage_changed",
    "crm.automation.simulation.requested": "automation.triggered",
}

#: Stable reason codes used in :class:`ComplexityDecision.reasons`.
REASON_FIXED_EVENT_ALLOWLIST = "fixed_event_allowlist"
REASON_SINGLE_DOMAIN_SINGLE_TASK = "single_domain_single_task"
REASON_CROSS_DOMAIN_OBJECTIVE = "cross_domain_objective"
REASON_MULTIPLE_TASK_TYPES = "multiple_task_types"
REASON_CONFLICTING_SIGNALS = "conflicting_signals"
REASON_CUSTOMER_RECOVERY_TEMPLATE = "customer_recovery_template"
REASON_REGISTRY_VERSION_MISMATCH = "registry_version_mismatch"
REASON_MISSING_CONTEXT = "missing_context"
REASON_NO_CAPABLE_AGENT = "no_capable_agent"
REASON_INSUFFICIENT_BUDGET = "insufficient_budget"

#: The objective_kind value that triggers the Customer Recovery template.
CUSTOMER_RECOVERY_OBJECTIVE_KIND = "customer_recovery"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ComplexityGate(Protocol):
    """Pluggable complexity gate."""

    def decide(
        self,
        request: PlanningRequest,
        registry: AgentRegistry,
    ) -> ComplexityDecision: ...


# ---------------------------------------------------------------------------
# Rule-based implementation
# ---------------------------------------------------------------------------


class RuleBasedComplexityGate:
    """Pure-rule complexity gate — no LLM, no network.

    Decision order:

    1. Verify registry version.
    2. Verify minimum context.
    3. Verify structural input consistency.
    4. Fixed-event allowlist → ``deterministic_workflow``.
    5. Customer Recovery objective kind → ``multi_agent``.
    6. Multi-domain / multi-task / cross-domain / conflicting signals
       → ``multi_agent``.
    7. Otherwise → ``single_agent`` (with capability existence check).
    """

    def decide(
        self,
        request: PlanningRequest,
        registry: AgentRegistry,
    ) -> ComplexityDecision:
        # Step 1 — Registry version match.
        snapshot = registry.snapshot()
        if snapshot.version != request.registry_version:
            raise RegistryVersionMismatchError(
                f"Registry version mismatch: request="
                f"{request.registry_version!r} snapshot={snapshot.version!r}"
            )

        signals = request.signals

        # Step 2 — Minimum context.
        if signals.missing_required_context:
            raise InsufficientContextError(
                "signals.missing_required_context is True; cannot plan"
            )

        # Step 3 — Structural input consistency.
        self._validate_structural_consistency(signals)

        # Step 4 — Fixed-event allowlist.
        if signals.event_type and signals.event_type in DETERMINISTIC_EVENT_TYPES:
            return ComplexityDecision(
                route="deterministic_workflow",
                domains=[],
                reasons=[REASON_FIXED_EVENT_ALLOWLIST],
                confidence=1.0,
                requires_human_review=False,
            )

        domains_sorted = sorted(signals.domains)
        task_types_sorted = sorted(signals.requested_task_types)

        # Step 5 — Customer Recovery template.
        if signals.objective_kind == CUSTOMER_RECOVERY_OBJECTIVE_KIND:
            return ComplexityDecision(
                route="multi_agent",
                domains=domains_sorted,
                reasons=[REASON_CUSTOMER_RECOVERY_TEMPLATE],
                confidence=1.0,
                requires_human_review=False,
            )

        # Step 6 — Multi-agent triggers.
        multi_reasons: list[str] = []
        if len(domains_sorted) >= 2:
            multi_reasons.append(REASON_CROSS_DOMAIN_OBJECTIVE)
        if len(task_types_sorted) >= 2:
            multi_reasons.append(REASON_MULTIPLE_TASK_TYPES)
        if signals.requires_cross_domain:
            multi_reasons.append(REASON_CROSS_DOMAIN_OBJECTIVE)
        if signals.has_conflicting_signals:
            multi_reasons.append(REASON_CONFLICTING_SIGNALS)

        if multi_reasons:
            # Dedup while preserving order.
            seen: set[str] = set()
            deduped: list[str] = []
            for r in multi_reasons:
                if r not in seen:
                    seen.add(r)
                    deduped.append(r)
            return ComplexityDecision(
                route="multi_agent",
                domains=domains_sorted,
                reasons=deduped,
                confidence=1.0,
                requires_human_review=False,
            )

        # Step 7 — Single-agent path.
        # Require exactly one domain and at most one task type.
        if not domains_sorted:
            raise PlanningInputError(
                "single_agent route requires at least one domain in signals"
            )

        # Verify at least one enabled agent covers the requested domain.
        if not self._has_capable_agent(registry, domains_sorted, task_types_sorted):
            raise UnsupportedCapabilityError(
                f"No enabled agent covers domains={domains_sorted!r} "
                f"task_types={task_types_sorted!r}"
            )

        return ComplexityDecision(
            route="single_agent",
            domains=domains_sorted,
            reasons=[REASON_SINGLE_DOMAIN_SINGLE_TASK],
            confidence=1.0,
            requires_human_review=False,
        )

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _validate_structural_consistency(signals: PlanningSignals) -> None:
        """Reject *structural* input contradictions.

        Business-level conflicting signals (e.g. low support satisfaction
        vs. high sales renewal probability) are NOT rejected here — they
        route to ``multi_agent`` with reason ``conflicting_signals``.

        Only contradictions that make the request unplanneable are
        rejected:

        * ``requires_cross_domain=True`` but ``domains`` has < 2 entries.
        * ``requires_approval=True`` and ``requires_write=False`` and
          no PROPOSE-level task type is requested.
        * ``requested_task_types`` references a task type that doesn't
          belong to any domain in ``domains`` (when both sets are
          non-empty AND domains has exactly one member — a heuristic
          for "the caller mislabelled the task type").
        """
        if signals.requires_cross_domain and len(signals.domains) < 2:
            raise PlanningInputError(
                "requires_cross_domain=True but domains has fewer than 2 entries"
            )

        if (
            signals.requires_approval
            and not signals.requires_write
            and not signals.requested_task_types
        ):
            raise PlanningInputError(
                "requires_approval=True with no requested_task_types; "
                "cannot identify a PROPOSE-capable intent"
            )

    @staticmethod
    def _has_capable_agent(
        registry: AgentRegistry,
        domains: list[str],
        task_types: list[str],
    ) -> bool:
        """Return True iff at least one enabled agent covers any requested domain.

        Authority filter: EXECUTE-only agents are NOT considered
        "capable" for planning purposes — they cannot participate in a
        Phase 3 plan.  This mirrors the planner's candidate filter.
        """
        if not domains:
            return False
        for agent in registry.list_all():
            if not agent.enabled:
                continue
            if agent.authority is AgentAuthority.EXECUTE:
                continue
            if any(d in agent.domains for d in domains):
                if not task_types:
                    return True
                if any(tt in agent.supported_tasks for tt in task_types):
                    return True
        return False


__all__ = [
    "CUSTOMER_RECOVERY_OBJECTIVE_KIND",
    "ComplexityGate",
    "DETERMINISTIC_EVENT_TYPES",
    "KAFKA_TOPIC_TO_EVENT_TYPE",
    "REASON_CONFLICTING_SIGNALS",
    "REASON_CROSS_DOMAIN_OBJECTIVE",
    "REASON_CUSTOMER_RECOVERY_TEMPLATE",
    "REASON_FIXED_EVENT_ALLOWLIST",
    "REASON_INSUFFICIENT_BUDGET",
    "REASON_MISSING_CONTEXT",
    "REASON_MULTIPLE_TASK_TYPES",
    "REASON_NO_CAPABLE_AGENT",
    "REASON_REGISTRY_VERSION_MISMATCH",
    "REASON_SINGLE_DOMAIN_SINGLE_TASK",
    "RuleBasedComplexityGate",
]
