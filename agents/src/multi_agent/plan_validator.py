"""Phase 3 Plan DAG Validator.

Verifies a :class:`PlanDraft` against:

* Identity & tenant homogeneity
* Registry capability and tool access
* DAG structure (dependencies, cycles, topology)
* Budget limits (hard fail-closed for structural budgets)
* Authority bounds (no EXECUTE in Phase 3)
* Plan hash integrity

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
    PlannedTask,
)

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
CODE_DETERMINISTIC_HAS_TASKS = "deterministic_route_has_tasks"
CODE_SINGLE_AGENT_NOT_ONE = "single_agent_not_one_task"
CODE_MULTI_AGENT_TOO_FEW_TASKS = "multi_agent_too_few_tasks"
CODE_MULTI_AGENT_TOO_FEW_AGENTS = "multi_agent_too_few_agents"
CODE_UNSUPPORTED_TASK = "unsupported_task"
CODE_DISABLED_AGENT = "disabled_agent"
CODE_EXECUTE_AGENT = "execute_agent_rejected"
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


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class PlanValidator:
    """Read-only plan validator.

    The validator collects *all* issues (errors + warnings) before
    deciding ``valid``.  This gives reviewers a complete picture of
    what's wrong rather than a single failure point.
    """

    def validate(
        self,
        request: Any,  # PlanningRequest — typed as Any to avoid import cycle
        plan: PlanDraft,
        registry: AgentRegistry,
    ) -> PlanValidationReport:
        # Late import to avoid circular dependency at module load time.
        from multi_agent.planning import PlanningRequest

        assert isinstance(request, PlanningRequest)

        issues: list[PlanValidationIssue] = []

        # -- Identity & tenant ------------------------------------------------
        issues.extend(self._check_identity(request, plan))

        # -- Plan hash --------------------------------------------------------
        issues.extend(self._check_plan_hash(plan))

        # -- Registry version -------------------------------------------------
        issues.extend(self._check_registry_version(request, plan, registry))

        # -- Per-task registry + authority checks -----------------------------
        issues.extend(self._check_tasks_against_registry(plan, registry))

        # -- DAG structure ----------------------------------------------------
        issues.extend(self._check_dag(plan, issues))

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

        # Topological order — always computed (even on failure) so reviewers
        # can inspect the partial order.  Returns [] if cyclic.
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

            if pt.preferred_authority is AgentAuthority.EXECUTE:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_AUTHORITY_EXCEEDS_PROPOSE,
                        severity="error",
                        message=f"PlannedTask {pt.intent_id!r} preferred_authority is EXECUTE",
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
    def _check_dag(
        cls, plan: PlanDraft, existing_issues: list[PlanValidationIssue]
    ) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []

        task_ids: set[str] = {pt.task.task_id for pt in plan.tasks}

        for pt in plan.tasks:
            deps = pt.task.dependencies
            # Self-dependency (defensive — AgentTask already enforces).
            if pt.task.task_id in deps:
                issues.append(
                    PlanValidationIssue(
                        code=CODE_SELF_DEPENDENCY,
                        severity="error",
                        message=f"task {pt.task.task_id!r} depends on itself",
                        task_id=pt.task.task_id,
                    )
                )
            # Duplicate dependencies (defensive — PlannedTask dedups at
            # construction, but AgentTask.dependencies is a frozenset so
            # duplicates are already impossible — still check for clarity).
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

        # Cycle detection (Kahn's algorithm).  Only run if no missing
        # dependencies — otherwise missing deps would masquerade as roots.
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
        # Build adjacency list: task_id -> set of dependents.
        graph: dict[str, set[str]] = {pt.task.task_id: set() for pt in plan.tasks}
        in_degree: dict[str, int] = {pt.task.task_id: 0 for pt in plan.tasks}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] = in_degree.get(pt.task.task_id, 0) + 1

        # Kahn's algorithm — but we need to detect cycle nodes, not just
        # existence.  Nodes that never reach in_degree 0 are in a cycle
        # (or depend on a cycle).
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
        # Build in_degree from scratch (Kahn's, deterministic ordering by
        # sorting the ready queue at every step).
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
                    # Insert in sorted position to keep order stable.
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
    def _check_required_vs_optional(
        plan: PlanDraft,
    ) -> list[PlanValidationIssue]:
        """A required task must not depend on an optional task."""
        issues: list[PlanValidationIssue] = []
        optional_ids: set[str] = set()
        id_to_pt: dict[str, PlannedTask] = {}
        for pt in plan.tasks:
            id_to_pt[pt.intent_id] = pt
            if not pt.required:
                optional_ids.add(pt.intent_id)

        # Also consider task_id → intent_id mapping, since dependencies
        # are expressed in task_id space (AgentTask.dependencies).
        intent_id_by_task_id: dict[str, str] = {
            pt.task.task_id: pt.intent_id for pt in plan.tasks
        }
        task_id_by_intent_id: dict[str, str] = {
            pt.intent_id: pt.task.task_id for pt in plan.tasks
        }

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
        # Silence unused-variable warnings from the lookup maps.
        _ = (id_to_pt, intent_id_by_task_id)
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

        Hard fail-closed limits: max_tasks, max_agent_calls,
        max_tool_calls, max_iterations, deadline_ms.

        Soft (warning) limits: token_budget, cost_budget_usd — Phase 3
        has no reliable estimate, so a warning is emitted when they are
        set.  No dollar amounts are fabricated.
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

        # Structural budgets — hard fail-closed.
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

        # Token / cost budgets — warning only, no fabricated estimates.
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
        """Return the number of nodes on the longest path (0 if empty)."""
        if not plan.tasks:
            return 0
        if cls._detect_cycle(plan) is not None:
            # Cyclic — return node count as an upper bound; the cycle
            # issue will already be reported by _check_dag.
            return len(plan.tasks)

        # Topological order via Kahn's algorithm.
        task_ids = {pt.task.task_id for pt in plan.tasks}
        graph: dict[str, set[str]] = {tid: set() for tid in task_ids}
        in_degree: dict[str, int] = {tid: 0 for tid in task_ids}
        for pt in plan.tasks:
            for dep in pt.task.dependencies:
                if dep in graph:
                    graph[dep].add(pt.task.task_id)
                    in_degree[pt.task.task_id] += 1

        # Longest path DP over topological order.
        longest: dict[str, int] = {tid: 1 for tid in task_ids}
        queue: deque[str] = deque(
            sorted(tid for tid, deg in in_degree.items() if deg == 0)
        )
        visited: set[str] = set()
        while queue:
            node = queue.popleft()
            visited.add(node)
            for neighbor in sorted(graph[node]):
                if longest[neighbor] < longest[node] + 1:
                    longest[neighbor] = longest[node] + 1
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return max(longest.values()) if longest else 0

    @classmethod
    def _longest_path_deadline_ms(cls, plan: PlanDraft) -> int:
        """Sum of timeout_ms along the longest path."""
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


__all__ = [
    "PlanValidator",
]
