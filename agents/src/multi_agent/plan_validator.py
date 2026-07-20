"""Phase 3 Plan DAG Validator.

Verifies a :class:`PlanDraft` against:

* Identity & tenant homogeneity
* Request snapshot integrity (request_hash + plan.request == request)
* Plan hash integrity
* Registry capability, authority hierarchy, and tool access
* Complexity decision consistency (re-runs the gate)
* **Plan-vs-expected-intent semantic binding (R2 P0-1)** — every
  PlannedTask must match the intent produced by
  :func:`resolve_expected_intents` for the same request + decision.
  Stable task IDs and idempotency keys are recomputed and compared.
* DAG structure (dependencies, cycles, topology)
* Budget limits (hard fail-closed for structural budgets)
* Authority bounds (no EXECUTE in Phase 3; agent authority >= task preferred)
* Route-specific constraints

The validator is **read-only** — it never mutates the plan, request,
or registry.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from multi_agent.contracts import AgentAuthority, ToolAuthority
from multi_agent.registry import AgentRegistry
from multi_agent.planning import (
    PlanDraft,
    PlanValidationIssue,
    PlanValidationReport,
    compute_request_hash,
    resolve_expected_intents,
)
from multi_agent.complexity_gate import (
    ComplexityGate,
    RuleBasedComplexityGate,
)
from multi_agent.planning_errors import PlanningError
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Issue codes (stable strings surfaced over HTTP / logs)
# ---------------------------------------------------------------------------

CODE_DUPLICATE_TASK_ID = "duplicate_task_id"
CODE_MISSING_DEPENDENCY = "missing_dependency"
CODE_SELF_DEPENDENCY = "self_dependency"
CODE_DUPLICATE_DEPENDENCY = "duplicate_dependency"
CODE_CYCLE = "cycle"
CODE_TENANT_MISMATCH = "tenant_mismatch"
CODE_RUN_ID_MISMATCH = "run_id_mismatch"
CODE_REGISTRY_VERSION_MISMATCH = "registry_version_mismatch"
CODE_PLAN_HASH_MISMATCH = "plan_hash_mismatch"
CODE_REQUEST_HASH_MISMATCH = "request_hash_mismatch"
CODE_REQUEST_SNAPSHOT_MISMATCH = "request_snapshot_mismatch"
CODE_COMPLEXITY_DECISION_MISMATCH = "complexity_decision_mismatch"
CODE_DETERMINISTIC_HAS_TASKS = "deterministic_route_has_tasks"
CODE_SINGLE_AGENT_NOT_ONE = "single_agent_not_one_task"
CODE_MULTI_AGENT_TOO_FEW_TASKS = "multi_agent_too_few_tasks"
CODE_MULTI_AGENT_TOO_FEW_AGENTS = "multi_agent_too_few_agents"
CODE_UNSUPPORTED_TASK = "unsupported_task"
CODE_DISABLED_AGENT = "disabled_agent"
CODE_EXECUTE_AGENT = "execute_agent_rejected"
CODE_INSUFFICIENT_AGENT_AUTHORITY = "insufficient_agent_authority"
CODE_UNKNOWN_TOOL = "unknown_tool"
CODE_UNAUTHORIZED_TOOL = "unauthorized_tool"
CODE_TASK_BUDGET_EXCEEDED = "task_budget_exceeded"
CODE_AGENT_CALL_BUDGET_EXCEEDED = "agent_call_budget_exceeded"
CODE_TOOL_CALL_BUDGET_EXCEEDED = "tool_call_budget_exceeded"
CODE_ITERATION_BUDGET_EXCEEDED = "iteration_budget_exceeded"
CODE_DEADLINE_EXCEEDED = "deadline_exceeded"
CODE_REQUIRED_DEPENDS_ON_OPTIONAL = "required_depends_on_optional"
CODE_AUTHORITY_EXCEEDS_PROPOSE = "authority_exceeds_propose"
CODE_TOKEN_BUDGET_ESTIMATE_UNAVAILABLE = "token_budget_estimate_unavailable"
CODE_COST_BUDGET_ESTIMATE_UNAVAILABLE = "cost_budget_estimate_unavailable"

# R2 P0-1 — intent-binding issue codes.
CODE_PLAN_INTENT_MISMATCH = "plan_intent_mismatch"
CODE_UNSTABLE_TASK_ID = "unstable_task_id"
CODE_IDEMPOTENCY_KEY_MISMATCH = "idempotency_key_mismatch"
CODE_PLANNED_TASK_REQUIRED_MISMATCH = "planned_task_required_mismatch"
CODE_DUPLICATE_INTENT_ID = "duplicate_intent_id"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class PlanValidator:
    """Read-only plan validator.

    The validator collects *all* issues (errors + warnings) before
    deciding ``valid``.  This gives reviewers a complete picture of
    what's wrong rather than a single failure point.

    The validator injects a :class:`ComplexityGate` so it can re-run
    the gate decision and verify the plan's ``complexity`` matches
    what the gate would produce for the same request + registry.
    """

    def __init__(self, gate: ComplexityGate | None = None) -> None:
        self._gate = gate or RuleBasedComplexityGate()

    def validate(
        self,
        request: Any,
        plan: PlanDraft,
        registry: AgentRegistry,
    ) -> PlanValidationReport:
        from multi_agent.planning import PlanningRequest

        assert isinstance(request, PlanningRequest)

        issues: list[PlanValidationIssue] = []

        # -- Request snapshot integrity --------------------------------------
        issues.extend(self._check_request_snapshot(request, plan))

        # -- Identity & tenant ------------------------------------------------
        issues.extend(self._check_identity(request, plan))

        # -- Plan hash --------------------------------------------------------
        issues.extend(self._check_plan_hash(plan))

        # -- Registry version -------------------------------------------------
        issues.extend(self._check_registry_version(request, plan, registry))

        # -- Complexity decision consistency ---------------------------------
        issues.extend(self._check_complexity_decision(request, plan, registry))

        # -- Plan-vs-expected-intent binding (R2 P0-1) -----------------------
        issues.extend(self._check_intent_binding(request, plan, registry))

        # -- Per-task registry + authority checks -----------------------------
        issues.extend(self._check_tasks_against_registry(plan, registry))

        # -- DAG structure ----------------------------------------------------
        issues.extend(self._check_dag(plan))

        # -- Route-specific constraints --------------------------------------
        issues.extend(self._check_route_constraints(plan))

        # -- Required vs optional dependencies -------------------------------
        issues.extend(self._check_required_vs_optional(plan))

        # -- Budget -----------------------------------------------------------
        estimates, budget_issues = self._check_budget(request, plan)
        issues.extend(budget_issues)

        # -- Aggregate --------------------------------------------------------
        errors = [i for i in issues if i.severity == "error"]
        valid = len(errors) == 0

        topo = self._topological_order(plan)

        return PlanValidationReport(
            valid=valid,
            issues=issues,
            topological_order=topo,
            estimated_agent_calls=estimates["agent_calls"],
            estimated_tool_calls=estimates["tool_calls"],
            estimated_iterations=estimates["iterations"],
            estimated_deadline_ms=estimates["deadline_ms"],
        )

    # ------------------------------------------------------------------
    # Request snapshot integrity
    # ------------------------------------------------------------------

    @staticmethod
    def _check_request_snapshot(
        request: Any, plan: PlanDraft
    ) -> list[PlanValidationIssue]:
        """Verify plan.request matches the caller's request, and that
        plan.request_hash is consistent with both.
        """
        issues: list[PlanValidationIssue] = []

        # request_hash on plan must equal compute_request_hash(plan.request).
        expected_from_snapshot = compute_request_hash(plan.request)
        if plan.request_hash != expected_from_snapshot:
            issues.append(
                PlanValidationIssue(
                    code=CODE_REQUEST_HASH_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.request_hash={plan.request_hash[:12]!r} != "
                        f"compute_request_hash(plan.request)="
                        f"{expected_from_snapshot[:12]!r}"
                    ),
                )
            )

        # plan.request must equal the caller's request.
        if plan.request != request:
            issues.append(
                PlanValidationIssue(
                    code=CODE_REQUEST_SNAPSHOT_MISMATCH,
                    severity="error",
                    message="plan.request snapshot does not match caller request",
                )
            )

        # request_hash must also equal compute_request_hash(request).
        expected_from_request = compute_request_hash(request)
        if plan.request_hash != expected_from_request:
            issues.append(
                PlanValidationIssue(
                    code=CODE_REQUEST_HASH_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.request_hash={plan.request_hash[:12]!r} != "
                        f"compute_request_hash(request)="
                        f"{expected_from_request[:12]!r}"
                    ),
                )
            )

        return issues

    # ------------------------------------------------------------------
    # Identity & tenant
    # ------------------------------------------------------------------

    @staticmethod
    def _check_identity(request: Any, plan: PlanDraft) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        if plan.run_id != request.run_id:
            issues.append(
                PlanValidationIssue(
                    code=CODE_RUN_ID_MISMATCH,
                    severity="error",
                    message=f"plan.run_id={plan.run_id!r} != request.run_id={request.run_id!r}",
                )
            )
        if plan.tenant_id != request.tenant_id:
            issues.append(
                PlanValidationIssue(
                    code=CODE_TENANT_MISMATCH,
                    severity="error",
                    message=f"plan.tenant_id={plan.tenant_id!r} != request.tenant_id={request.tenant_id!r}",
                )
            )
        for pt in plan.tasks:
            if pt.task.tenant_id != plan.tenant_id:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_TENANT_MISMATCH,
                        severity="error",
                        message=f"task {pt.task.task_id!r} tenant {pt.task.tenant_id!r} != plan tenant {plan.tenant_id!r}",
                        task_id=pt.task.task_id,
                    )
                )
        return issues

    # ------------------------------------------------------------------
    # Plan hash
    # ------------------------------------------------------------------

    @staticmethod
    def _check_plan_hash(plan: PlanDraft) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        if not plan.plan_hash:
            issues.append(
                PlanValidationIssue(
                    code=CODE_PLAN_HASH_MISMATCH,
                    severity="error",
                    message="plan_hash is empty",
                )
            )
            return issues
        expected = plan.compute_plan_hash()
        if plan.plan_hash != expected:
            issues.append(
                PlanValidationIssue(
                    code=CODE_PLAN_HASH_MISMATCH,
                    severity="error",
                    message=f"plan_hash {plan.plan_hash[:12]!r} != computed {expected[:12]!r}",
                )
            )
        return issues

    # ------------------------------------------------------------------
    # Registry version
    # ------------------------------------------------------------------

    @staticmethod
    def _check_registry_version(
        request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        snapshot = registry.snapshot()
        if plan.registry_version != snapshot.version:
            issues.append(
                PlanValidationIssue(
                    code=CODE_REGISTRY_VERSION_MISMATCH,
                    severity="error",
                    message=f"plan.registry_version={plan.registry_version!r} != registry={snapshot.version!r}",
                )
            )
        if plan.registry_version != request.registry_version:
            issues.append(
                PlanValidationIssue(
                    code=CODE_REGISTRY_VERSION_MISMATCH,
                    severity="error",
                    message=f"plan.registry_version={plan.registry_version!r} != request.registry_version={request.registry_version!r}",
                )
            )
        return issues

    # ------------------------------------------------------------------
    # Complexity decision consistency
    # ------------------------------------------------------------------

    def _check_complexity_decision(
        self, request: Any, plan: PlanDraft, registry: AgentRegistry
    ) -> list[PlanValidationIssue]:
        """Re-run the gate and verify plan.complexity matches.

        Compares route, domains (as sets), reasons (as sets), and
        requires_human_review.  ``confidence`` is not compared because
        it's a numeric hint, not a structural invariant.

        R2 P1: only :class:`PlanningError` is caught.  Unknown
        exceptions (programming bugs) are allowed to propagate so they
        surface in tests, logs, and error monitoring rather than being
        silently downgraded to a validation issue.
        """
        issues: list[PlanValidationIssue] = []
        try:
            expected = self._gate.decide(request, registry)
        except PlanningError as exc:
            issues.append(
                PlanValidationIssue(
                    code=CODE_COMPLEXITY_DECISION_MISMATCH,
                    severity="error",
                    message=f"gate.decide() raised {type(exc).__name__}: {exc}",
                )
            )
            return issues

        if plan.complexity.route != expected.route:
            issues.append(
                PlanValidationIssue(
                    code=CODE_COMPLEXITY_DECISION_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.complexity.route={plan.complexity.route!r} != "
                        f"gate route={expected.route!r}"
                    ),
                )
            )
        if set(plan.complexity.domains) != set(expected.domains):
            issues.append(
                PlanValidationIssue(
                    code=CODE_COMPLEXITY_DECISION_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.complexity.domains={sorted(plan.complexity.domains)!r} != "
                        f"gate domains={sorted(expected.domains)!r}"
                    ),
                )
            )
        if set(plan.complexity.reasons) != set(expected.reasons):
            issues.append(
                PlanValidationIssue(
                    code=CODE_COMPLEXITY_DECISION_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.complexity.reasons={sorted(plan.complexity.reasons)!r} != "
                        f"gate reasons={sorted(expected.reasons)!r}"
                    ),
                )
            )
        if plan.complexity.requires_human_review != expected.requires_human_review:
            issues.append(
                PlanValidationIssue(
                    code=CODE_COMPLEXITY_DECISION_MISMATCH,
                    severity="error",
                    message=(
                        f"plan.complexity.requires_human_review="
                        f"{plan.complexity.requires_human_review} != "
                        f"gate={expected.requires_human_review}"
                    ),
                )
            )
        return issues

    # ------------------------------------------------------------------
    # Plan-vs-expected-intent binding (R2 P0-1)
    # ------------------------------------------------------------------

    def _check_intent_binding(
        self,
        request: Any,
        plan: PlanDraft,
        registry: AgentRegistry,  # noqa: ARG002 - reserved for future use
    ) -> list[PlanValidationIssue]:
        """Verify every PlannedTask matches the expected
        :class:`TaskIntent` produced by :func:`resolve_expected_intents`
        for the same request + decision.

        This closes the "Plan task substitution" hole: even if a
        tampered task is registry-supported and the plan hash is
        recomputed, the Validator will reject the plan because the
        tampered task no longer matches the expected intent.

        Fields compared per task:

        * ``intent_id`` (must exist in expected intents)
        * ``domain`` / ``task_type`` / ``objective``
        * ``preferred_authority``
        * ``required_tools`` (as sets)
        * ``estimated_tool_calls``
        * ``required``

        Recomputed and compared:

        * ``task_id`` = ``stable_hash({run_id, intent_id, task_type, agent_id})[:24]``
        * ``idempotency_key`` = ``f"{run_id}:{task_id}"``
        * ``planned_task.required`` == ``agent_task.required``

        Also verifies:

        * No duplicate ``intent_id`` among PlannedTasks.
        * The set of plan intent_ids equals the set of expected
          intent_ids (no extra / missing intents).
        """
        issues: list[PlanValidationIssue] = []

        # Resolve expected intents via the shared pure function.
        try:
            expected = resolve_expected_intents(request, plan.complexity)
        except PlanningError as exc:
            issues.append(
                PlanValidationIssue(
                    code=CODE_PLAN_INTENT_MISMATCH,
                    severity="error",
                    message=(
                        f"resolve_expected_intents() raised {type(exc).__name__}: {exc}"
                    ),
                )
            )
            return issues

        expected_by_id = {i.intent_id: i for i in expected}

        # Duplicate intent_id check.
        seen_intent_ids: set[str] = set()
        for pt in plan.tasks:
            if pt.intent_id in seen_intent_ids:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_DUPLICATE_INTENT_ID,
                        severity="error",
                        message=f"duplicate intent_id {pt.intent_id!r}",
                        task_id=pt.task.task_id,
                    )
                )
            seen_intent_ids.add(pt.intent_id)

        # Missing / extra intents.
        expected_ids = set(expected_by_id.keys())
        plan_ids = seen_intent_ids
        missing = expected_ids - plan_ids
        extra = plan_ids - expected_ids
        if missing:
            issues.append(
                PlanValidationIssue(
                    code=CODE_PLAN_INTENT_MISMATCH,
                    severity="error",
                    message=(
                        f"plan is missing tasks for intent_ids: {sorted(missing)!r}"
                    ),
                )
            )
        if extra:
            issues.append(
                PlanValidationIssue(
                    code=CODE_PLAN_INTENT_MISMATCH,
                    severity="error",
                    message=(f"plan has extra tasks for intent_ids: {sorted(extra)!r}"),
                )
            )

        # Per-task field comparison.
        for pt in plan.tasks:
            intent = expected_by_id.get(pt.intent_id)
            if intent is None:
                # Already reported above; skip field-level checks.
                continue

            if pt.domain != intent.domain:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} domain "
                            f"{pt.domain!r} != expected {intent.domain!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.task.task_type != intent.task_type:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} task_type "
                            f"{pt.task.task_type!r} != expected "
                            f"{intent.task_type!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.task.objective != intent.objective:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} objective "
                            f"{pt.task.objective!r} != expected "
                            f"{intent.objective!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.preferred_authority != intent.preferred_authority:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} preferred_authority "
                            f"{pt.preferred_authority.value!r} != expected "
                            f"{intent.preferred_authority.value!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if set(pt.required_tools) != set(intent.required_tools):
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} required_tools "
                            f"{sorted(pt.required_tools)!r} != expected "
                            f"{sorted(intent.required_tools)!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.estimated_tool_calls != intent.estimated_tool_calls:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} estimated_tool_calls "
                            f"{pt.estimated_tool_calls} != expected "
                            f"{intent.estimated_tool_calls}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.required != intent.required:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLAN_INTENT_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} required "
                            f"{pt.required} != expected {intent.required}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            # PlannedTask.required must equal AgentTask.required.
            if pt.required != pt.task.required:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_PLANNED_TASK_REQUIRED_MISMATCH,
                        severity="error",
                        message=(
                            f"PlannedTask {pt.intent_id!r} required="
                            f"{pt.required} but AgentTask.required="
                            f"{pt.task.required}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )

            # Stable task ID recomputation.
            expected_task_id = stable_hash(
                {
                    "run_id": plan.run_id,
                    "intent_id": intent.intent_id,
                    "task_type": intent.task_type,
                    "agent_id": pt.task.agent_id,
                }
            )[:24]
            if pt.task.task_id != expected_task_id:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_UNSTABLE_TASK_ID,
                        severity="error",
                        message=(
                            f"task_id {pt.task.task_id!r} != expected "
                            f"{expected_task_id!r} for intent "
                            f"{intent.intent_id!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )

            # Idempotency key recomputation.
            expected_idem = f"{plan.run_id}:{expected_task_id}"
            if pt.task.idempotency_key != expected_idem:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_IDEMPOTENCY_KEY_MISMATCH,
                        severity="error",
                        message=(
                            f"idempotency_key {pt.task.idempotency_key!r} != "
                            f"expected {expected_idem!r} for task "
                            f"{pt.task.task_id!r}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )

        return issues

    # ------------------------------------------------------------------
    # Per-task registry + authority checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_tasks_against_registry(
        plan: PlanDraft, registry: AgentRegistry
    ) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []

        # First pass: unique task IDs.
        seen_ids: set[str] = set()
        for pt in plan.tasks:
            if pt.task.task_id in seen_ids:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_DUPLICATE_TASK_ID,
                        severity="error",
                        message=f"duplicate task_id {pt.task.task_id!r}",
                        task_id=pt.task.task_id,
                    )
                )
            seen_ids.add(pt.task.task_id)

        # Second pass: per-task capability + tool access.
        from multi_agent.errors import DisabledAgentError

        _authority_rank: dict[AgentAuthority, int] = {
            AgentAuthority.READ: 0,
            AgentAuthority.PROPOSE: 1,
            AgentAuthority.EXECUTE: 2,
        }

        for pt in plan.tasks:
            cap_id = pt.task.agent_id
            if not registry.is_registered(cap_id):
                issues.append(
                    PlanValidationIssue(
                        code=CODE_UNSUPPORTED_TASK,
                        severity="error",
                        message=f"agent {cap_id!r} is not registered",
                        task_id=pt.task.task_id,
                    )
                )
                continue

            try:
                cap = registry.resolve_capability(cap_id)
            except DisabledAgentError:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_DISABLED_AGENT,
                        severity="error",
                        message=f"agent {cap_id!r} is disabled",
                        task_id=pt.task.task_id,
                    )
                )
                continue

            # Authority bound: no EXECUTE in Phase 3.
            if cap.authority is AgentAuthority.EXECUTE:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_EXECUTE_AGENT,
                        severity="error",
                        message=f"agent {cap_id!r} has EXECUTE authority; Phase 3 plans must not include EXECUTE agents",
                        task_id=pt.task.task_id,
                    )
                )
                continue

            # PlannedTask preferred_authority must not be EXECUTE.
            if pt.preferred_authority is AgentAuthority.EXECUTE:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_AUTHORITY_EXCEEDS_PROPOSE,
                        severity="error",
                        message=f"PlannedTask {pt.intent_id!r} preferred_authority is EXECUTE",
                        task_id=pt.task.task_id,
                    )
                )
            # Agent authority must be >= task preferred_authority.
            if _authority_rank[cap.authority] < _authority_rank[pt.preferred_authority]:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_INSUFFICIENT_AGENT_AUTHORITY,
                        severity="error",
                        message=(
                            f"agent {cap_id!r} authority={cap.authority.value} "
                            f"< task {pt.task.task_id!r} preferred_authority="
                            f"{pt.preferred_authority.value}"
                        ),
                        task_id=pt.task.task_id,
                    )
                )
            if pt.task.task_type not in cap.supported_tasks:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_UNSUPPORTED_TASK,
                        severity="error",
                        message=f"agent {cap_id!r} does not support task_type {pt.task.task_type!r}",
                        task_id=pt.task.task_id,
                    )
                )
            if pt.domain not in cap.domains:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_UNSUPPORTED_TASK,
                        severity="error",
                        message=f"agent {cap_id!r} does not cover domain {pt.domain!r}",
                        task_id=pt.task.task_id,
                    )
                )
            if pt.task.timeout_ms > cap.timeout_ms:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_UNSUPPORTED_TASK,
                        severity="error",
                        message=f"task timeout {pt.task.timeout_ms}ms exceeds agent capability {cap.timeout_ms}ms",
                        task_id=pt.task.task_id,
                    )
                )

            # Required tools.
            for tool_name in sorted(pt.required_tools):
                if not registry.tool_catalog.is_registered(tool_name):
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_UNKNOWN_TOOL,
                            severity="error",
                            message=f"required tool {tool_name!r} is not in the catalog",
                            task_id=pt.task.task_id,
                        )
                    )
                    continue
                tool = registry.tool_catalog.resolve(tool_name)
                if tool_name not in cap.allowed_tools:
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_UNAUTHORIZED_TOOL,
                            severity="error",
                            message=f"agent {cap_id!r} is not allowed to use tool {tool_name!r}",
                            task_id=pt.task.task_id,
                        )
                    )
                # Authority hierarchy check.
                if (
                    cap.authority is AgentAuthority.READ
                    and tool.authority is not ToolAuthority.READ
                ):
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_UNAUTHORIZED_TOOL,
                            severity="error",
                            message=f"READ agent {cap_id!r} cannot use {tool.authority.value}-level tool {tool_name!r}",
                            task_id=pt.task.task_id,
                        )
                    )
                if (
                    cap.authority is AgentAuthority.PROPOSE
                    and tool.authority is ToolAuthority.EXECUTE
                ):
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_UNAUTHORIZED_TOOL,
                            severity="error",
                            message=f"PROPOSE agent {cap_id!r} cannot use EXECUTE-level tool {tool_name!r}",
                            task_id=pt.task.task_id,
                        )
                    )

        return issues

    # ------------------------------------------------------------------
    # DAG
    # ------------------------------------------------------------------

    @classmethod
    def _check_dag(cls, plan: PlanDraft) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []

        task_ids: set[str] = {pt.task.task_id for pt in plan.tasks}

        for pt in plan.tasks:
            deps = pt.task.dependencies
            # Self-dependency.
            if pt.task.task_id in deps:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_SELF_DEPENDENCY,
                        severity="error",
                        message=f"task {pt.task.task_id!r} depends on itself",
                        task_id=pt.task.task_id,
                    )
                )
            # Duplicate dependencies.
            dep_list = list(deps)
            if len(dep_list) != len(set(dep_list)):
                issues.append(
                    PlanValidationIssue(
                        code=CODE_DUPLICATE_DEPENDENCY,
                        severity="error",
                        message=f"task {pt.task.task_id!r} has duplicate dependencies",
                        task_id=pt.task.task_id,
                    )
                )
            # Missing dependencies.
            for dep in deps:
                if dep not in task_ids:
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_MISSING_DEPENDENCY,
                            severity="error",
                            message=f"task {pt.task.task_id!r} depends on missing task {dep!r}",
                            task_id=pt.task.task_id,
                        )
                    )

        # Cycle detection — skip if missing deps exist (they'd masquerade as roots).
        missing_dep_issues = [i for i in issues if i.code == CODE_MISSING_DEPENDENCY]
        if not missing_dep_issues:
            cycle = cls._detect_cycle(plan)
            if cycle:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_CYCLE,
                        severity="error",
                        message=f"plan DAG contains a cycle involving {cycle!r}",
                    )
                )

        return issues

    @staticmethod
    def _detect_cycle(plan: PlanDraft) -> list[str] | None:
        """Return a list of task_ids involved in a cycle, or None."""
        graph: dict[str, set[str]] = {pt.task.task_id: set() for pt in plan.tasks}
        in_degree: dict[str, int] = {pt.task.task_id: 0 for pt in plan.tasks}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] = in_degree.get(pt.task.task_id, 0) + 1

        queue: deque[str] = deque(
            sorted(tid for tid, deg in in_degree.items() if deg == 0)
        )
        visited: set[str] = set()
        while queue:
            node = queue.popleft()
            visited.add(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        cycle_nodes = sorted(set(in_degree.keys()) - visited)
        return cycle_nodes if cycle_nodes else None

    @classmethod
    def _topological_order(cls, plan: PlanDraft) -> list[str]:
        """Return stable topological order, or [] if cyclic."""
        if cls._detect_cycle(plan) is not None:
            return []
        task_ids = {pt.task.task_id for pt in plan.tasks}
        graph: dict[str, set[str]] = {tid: set() for tid in task_ids}
        in_degree: dict[str, int] = {tid: 0 for tid in task_ids}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] += 1

        order: list[str] = []
        ready = sorted(tid for tid, deg in in_degree.items() if deg == 0)
        while ready:
            node = ready.pop(0)
            order.append(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    import bisect

                    bisect.insort(ready, neighbor)
        return order

    # ------------------------------------------------------------------
    # Route constraints
    # ------------------------------------------------------------------

    @staticmethod
    def _check_route_constraints(plan: PlanDraft) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        route = plan.complexity.route
        n_tasks = len(plan.tasks)
        n_agents = len({pt.task.agent_id for pt in plan.tasks})

        if route == "deterministic_workflow":
            if n_tasks != 0:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_DETERMINISTIC_HAS_TASKS,
                        severity="error",
                        message=f"deterministic_workflow route must have 0 tasks, got {n_tasks}",
                    )
                )
        elif route == "single_agent":
            if n_tasks != 1:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_SINGLE_AGENT_NOT_ONE,
                        severity="error",
                        message=f"single_agent route must have exactly 1 task, got {n_tasks}",
                    )
                )
        elif route == "multi_agent":
            if n_tasks < 2:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_MULTI_AGENT_TOO_FEW_TASKS,
                        severity="error",
                        message=f"multi_agent route must have at least 2 tasks, got {n_tasks}",
                    )
                )
            if n_agents < 2:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_MULTI_AGENT_TOO_FEW_AGENTS,
                        severity="error",
                        message=f"multi_agent route must involve at least 2 distinct agents, got {n_agents}",
                        task_id=None,
                    )
                )
        return issues

    # ------------------------------------------------------------------
    # Required vs optional dependencies
    # ------------------------------------------------------------------

    @staticmethod
    def _check_required_vs_optional(plan: PlanDraft) -> list[PlanValidationIssue]:
        """A required task must not depend on an optional task."""
        issues: list[PlanValidationIssue] = []
        optional_ids: set[str] = set()
        task_id_by_intent_id: dict[str, str] = {}
        for pt in plan.tasks:
            task_id_by_intent_id[pt.intent_id] = pt.task.task_id
            if not pt.required:
                optional_ids.add(pt.intent_id)

        # Build optional set in task_id space.
        optional_task_ids: set[str] = {
            task_id_by_intent_id[iid]
            for iid in optional_ids
            if iid in task_id_by_intent_id
        }

        for pt in plan.tasks:
            if not pt.required:
                continue
            for dep_task_id in pt.task.dependencies:
                if dep_task_id in optional_task_ids:
                    issues.append(
                        PlanValidationIssue(
                            code=CODE_REQUIRED_DEPENDS_ON_OPTIONAL,
                            severity="error",
                            message=(
                                f"required task {pt.task.task_id!r} depends on "
                                f"optional task {dep_task_id!r}"
                            ),
                            task_id=pt.task.task_id,
                        )
                    )
        return issues

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    @classmethod
    def _check_budget(
        cls, request: Any, plan: PlanDraft
    ) -> tuple[dict[str, int], list[PlanValidationIssue]]:
        """Return (estimates, issues).

        Estimates follow the Phase 3 review R1 rules:

        * ``agent_calls`` = number of PlannedTasks
        * ``tool_calls``  = sum of PlannedTask.estimated_tool_calls
        * ``iterations``  = longest path node count in the DAG
        * ``deadline_ms`` = sum of task.timeout_ms along the longest path
        """
        from multi_agent.planning import PlanningRequest

        assert isinstance(request, PlanningRequest)
        budget = request.budget

        n_tasks = len(plan.tasks)
        agent_calls = n_tasks
        tool_calls = sum(pt.estimated_tool_calls for pt in plan.tasks)
        iterations = cls._longest_path_length(plan)
        deadline_ms = cls._longest_path_deadline_ms(plan)

        issues: list[PlanValidationIssue] = []

        if n_tasks > budget.max_tasks:
            issues.append(
                PlanValidationIssue(
                    code=CODE_TASK_BUDGET_EXCEEDED,
                    severity="error",
                    message=f"tasks {n_tasks} > max_tasks {budget.max_tasks}",
                )
            )
        if agent_calls > budget.max_agent_calls:
            issues.append(
                PlanValidationIssue(
                    code=CODE_AGENT_CALL_BUDGET_EXCEEDED,
                    severity="error",
                    message=f"agent_calls {agent_calls} > max_agent_calls {budget.max_agent_calls}",
                )
            )
        if tool_calls > budget.max_tool_calls:
            issues.append(
                PlanValidationIssue(
                    code=CODE_TOOL_CALL_BUDGET_EXCEEDED,
                    severity="error",
                    message=f"tool_calls {tool_calls} > max_tool_calls {budget.max_tool_calls}",
                )
            )
        if iterations > budget.max_iterations:
            issues.append(
                PlanValidationIssue(
                    code=CODE_ITERATION_BUDGET_EXCEEDED,
                    severity="error",
                    message=f"iterations {iterations} > max_iterations {budget.max_iterations}",
                )
            )
        if deadline_ms > budget.deadline_ms:
            issues.append(
                PlanValidationIssue(
                    code=CODE_DEADLINE_EXCEEDED,
                    severity="error",
                    message=f"estimated deadline {deadline_ms}ms > budget.deadline_ms {budget.deadline_ms}ms",
                )
            )

        if budget.token_budget is not None:
            issues.append(
                PlanValidationIssue(
                    code=CODE_TOKEN_BUDGET_ESTIMATE_UNAVAILABLE,
                    severity="warning",
                    message=(
                        "token_budget is set but Phase 3 has no token estimate; "
                        "treat as estimate_unavailable"
                    ),
                )
            )
        if budget.cost_budget_usd is not None:
            issues.append(
                PlanValidationIssue(
                    code=CODE_COST_BUDGET_ESTIMATE_UNAVAILABLE,
                    severity="warning",
                    message=(
                        "cost_budget_usd is set but Phase 3 has no cost estimate; "
                        "treat as estimate_unavailable"
                    ),
                )
            )

        estimates = {
            "agent_calls": agent_calls,
            "tool_calls": tool_calls,
            "iterations": iterations,
            "deadline_ms": deadline_ms,
        }
        return estimates, issues

    # ------------------------------------------------------------------
    # Longest-path helpers (DAG)
    # ------------------------------------------------------------------

    @classmethod
    def _longest_path_length(cls, plan: PlanDraft) -> int:
        if not plan.tasks:
            return 0
        if cls._detect_cycle(plan) is not None:
            return len(plan.tasks)

        task_ids = {pt.task.task_id for pt in plan.tasks}
        graph: dict[str, set[str]] = {tid: set() for tid in task_ids}
        in_degree: dict[str, int] = {tid: 0 for tid in task_ids}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] += 1

        longest: dict[str, int] = {tid: 1 for tid in task_ids}
        queue: deque[str] = deque(
            sorted(tid for tid, deg in in_degree.items() if deg == 0)
        )
        while queue:
            node = queue.popleft()
            for neighbor in sorted(graph[node]):
                if longest[neighbor] < longest[node] + 1:
                    longest[neighbor] = longest[node] + 1
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return max(longest.values()) if longest else 0

    @classmethod
    def _longest_path_deadline_ms(cls, plan: PlanDraft) -> int:
        if not plan.tasks:
            return 0
        if cls._detect_cycle(plan) is not None:
            return sum(pt.task.timeout_ms for pt in plan.tasks)

        task_ids = {pt.task.task_id for pt in plan.tasks}
        timeout_by_id = {pt.task.task_id: pt.task.timeout_ms for pt in plan.tasks}
        graph: dict[str, set[str]] = {tid: set() for tid in task_ids}
        in_degree: dict[str, int] = {tid: 0 for tid in task_ids}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] += 1

        deadline: dict[str, int] = {tid: timeout_by_id[tid] for tid in task_ids}
        queue: deque[str] = deque(
            sorted(tid for tid, deg in in_degree.items() if deg == 0)
        )
        while queue:
            node = queue.popleft()
            for neighbor in sorted(graph[node]):
                if deadline[neighbor] < deadline[node] + timeout_by_id[neighbor]:
                    deadline[neighbor] = deadline[node] + timeout_by_id[neighbor]
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return max(deadline.values()) if deadline else 0


__all__ = ["PlanValidator"]
