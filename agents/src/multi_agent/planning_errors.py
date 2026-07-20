"""Phase 3 planning errors.

All planning errors inherit from :class:`MultiAgentError` so callers can
catch the entire family with a single ``except``.  Each error carries a
stable ``code`` attribute that may be surfaced over HTTP / logs without
revealing internal exception details, API keys, endpoints, or
chain-of-thought.
"""

from __future__ import annotations

from multi_agent.errors import MultiAgentError


class PlanningError(MultiAgentError):
    """Base for every Phase 3 planning failure."""

    code: str = "planning_error"


class PlanningInputError(PlanningError):
    """Structural input contradiction — request cannot be planned at all.

    Raised for *structural* contradictions such as
    ``requires_cross_domain=True`` with ``domains={"support"}`` or
    ``requires_approval=True`` without any PROPOSE-capable intent.

    This is distinct from :class:`InsufficientContextError` (missing
    context) and from business-level conflicting signals (which route to
    ``multi_agent`` instead of failing).
    """

    code = "planning_input_error"


class InsufficientContextError(PlanningError):
    """Required context is missing for the requested objective."""

    code = "insufficient_context_error"


class UnsupportedCapabilityError(PlanningError):
    """No registered agent can satisfy the requested capability."""

    code = "unsupported_capability_error"


class AmbiguousAgentSelectionError(PlanningError):
    """Multiple equivalent candidates exist after stable tiebreakers.

    Phase 3 deterministic planner must never raise this — the final
    tiebreaker is ``agent_id`` lexicographic order.  It exists for
    future LLM planners that may need to surface the ambiguity.
    """

    code = "ambiguous_agent_selection_error"


class PlanValidationError(PlanningError):
    """Plan DAG failed structural / Registry / Budget validation."""

    code = "plan_validation_error"


class PlanCycleError(PlanValidationError):
    """The plan DAG contains a cycle."""

    code = "plan_cycle_error"


class PlanIntegrityError(PlanningError):
    """Plan hash does not match recomputed content."""

    code = "plan_integrity_error"


class RegistryVersionMismatchError(PlanningError):
    """Planner's Registry Snapshot version != PlanningRequest.registry_version."""

    code = "registry_version_mismatch_error"


class BudgetExceededPlanningError(PlanningError):
    """Planned usage exceeds a hard structural budget limit."""

    code = "budget_exceeded_planning_error"


__all__ = [
    "AmbiguousAgentSelectionError",
    "BudgetExceededPlanningError",
    "InsufficientContextError",
    "PlanCycleError",
    "PlanIntegrityError",
    "PlanValidationError",
    "PlanningError",
    "PlanningInputError",
    "RegistryVersionMismatchError",
    "UnsupportedCapabilityError",
]
