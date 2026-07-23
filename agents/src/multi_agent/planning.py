"""Phase 3 planning contracts.

All contracts inherit :class:`StrictContract` from Phase 2 so that
``extra="forbid"`` and ``validate_assignment=True`` apply uniformly.

Hash design (R1, post-review):

* ``request_hash`` — SHA-256 over the canonical :class:`PlanningRequest`
  content.  Stored on :class:`PlanDraft` AND the full request snapshot
  is stored too, so the draft can be verified *without* holding the
  original request.
* ``plan_hash`` — SHA-256 over ``request_hash`` + ``complexity`` +
  canonical tasks + ``planner_version``.  Excludes ``summary``,
  ``warnings``, wall-clock times, and ``plan_hash`` itself.

Both hashes use the shared :func:`multi_agent.serialization.stable_hash`
pipeline so they are stable across processes and platforms.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hmac import compare_digest
from types import MappingProxyType
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from multi_agent.contracts import (
    AgentAuthority,
    AgentTask,
    ComplexityDecision,
    ExecutionBudget,
    JsonValue,
    StrictContract,
    ToolAuthority,
    _non_blank,
    _reject_sensitive_keys,
    _validate_resource_id,
)
from multi_agent.serialization import canonicalize, stable_hash

# ---------------------------------------------------------------------------
# Planner version — bumped whenever the planner algorithm changes.
# ---------------------------------------------------------------------------

PLANNER_VERSION = "ma-03.7.0"

#: R3 P1 — upper bound on the number of cartesian-product combinations
#: the global multi-agent assignment search will evaluate before
#: failing closed.  Phase 3 task/candidate counts are bounded by
#: ``max_tasks`` (default 16), but this guard prevents pathological
#: registries from exhausting CPU during planning.
MAX_ASSIGNMENT_COMBINATIONS = 1_000_000

# ---------------------------------------------------------------------------
# R4 P0-1 — stable Intent-graph validation issue codes (shared by Planner
# and Validator so both sides agree on what makes an intent graph invalid).
# ---------------------------------------------------------------------------

CODE_INTENT_DUPLICATE_ID = "duplicate_intent_id"
CODE_INTENT_MISSING_DEPENDENCY = "missing_intent_dependency"
CODE_INTENT_CYCLE = "intent_cycle"

# ---------------------------------------------------------------------------
# R5 P0-1 — stable Write/Approval requirement issue codes (shared by
# Planner and Validator so both sides agree on when a request that
# declares requires_write / requires_approval is missing a PROPOSE
# intent).
# ---------------------------------------------------------------------------

CODE_WRITE_REQUEST_MISSING_PROPOSE = "write_request_missing_propose_intent"
CODE_APPROVAL_REQUEST_MISSING_PROPOSE = "approval_request_missing_propose_intent"

# ---------------------------------------------------------------------------
# R4 P0-3 — Tool Authority → Agent Authority mapping.  An Intent's
# preferred_authority must cover the highest authority required by any
# of its required_tools.  Silent auto-elevation is forbidden.
#
# R5 P1-1 — static definition.  ``ToolAuthority`` is imported at module
# load (no circular dependency), so the mapping is populated eagerly and
# never mutated.  The previous lazy ``_init_tool_authority_mapping()``
# left the public mapping empty until first use, which made it unsafe
# to read from external code.
#
# R6 P0-4 — wrapped in ``MappingProxyType`` so the public mapping is
# **immutable**.  Previously it was a plain ``dict`` exported via
# ``multi_agent.__init__``, so any caller (e.g. a misbehaving plugin or
# a tampered test) could do
# ``TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ``
# and silently downgrade the authority boundary for the entire process.
# ``MappingProxyType`` raises ``TypeError`` on mutation attempts, and
# the type annotation ``Mapping[ToolAuthority, AgentAuthority]`` makes
# the read-only contract explicit to type checkers.
# ---------------------------------------------------------------------------

TOOL_TO_AGENT_AUTHORITY: Mapping[ToolAuthority, AgentAuthority] = MappingProxyType(
    {
        ToolAuthority.READ: AgentAuthority.READ,
        ToolAuthority.PROPOSE: AgentAuthority.PROPOSE,
        ToolAuthority.EXECUTE: AgentAuthority.EXECUTE,
    }
)

_AUTHORITY_RANK: dict[Any, int] = {
    AgentAuthority.READ: 0,
    AgentAuthority.PROPOSE: 1,
    AgentAuthority.EXECUTE: 2,
}

_COST_CLASS_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------------------
# R5 P0-1 — RetryPolicy: Canonical retry contract shared by RequestedTask,
# TaskIntent, PlannedTask, Canonical Plan Reconstruction, Plan Hash, and
# PlanValidator.  Previously ``max_retries=0`` was hardcoded in
# ``build_expected_planned_tasks``, making retry untestable through the
# real Phase 3 → Phase 4 boundary.
# ---------------------------------------------------------------------------


# R6 P1: Error codes that must NEVER be retried, regardless of the
# RetryPolicy.  This is the canonical source — ``supervisor.py`` imports
# this frozenset so the planning-layer validator and the runtime
# ``should_retry()`` function share the same definition.  These represent
# definite business-domain failures, identity mismatches, or explicit
# cancellation — retrying them would be semantically incorrect even if
# the policy's ``retryable_error_codes`` explicitly lists them.
NEVER_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_receipt",
        "invalid_result",
        "usage_unavailable",
        "non_retryable_error",
        "run_deadline_exceeded",
        "tenant_mismatch",
        "agent_identity_mismatch",
        "cancelled",
        "kill_switch",
    }
)


class RetryPolicy(StrictContract):
    """Canonical retry policy for a planned task.

    R5 P0-1 — replaces the hardcoded ``max_retries=0`` in
    :func:`build_expected_planned_tasks` with a first-class planning
    contract.  The policy travels through:

    ``RequestedTask`` → ``TaskIntent`` → ``PlannedTask`` →
    Canonical Plan Reconstruction → Plan Hash → PlanValidator.

    Both Planner and Validator call the same
    :func:`build_expected_planned_tasks`, so any tampered ``max_retries``
    value is detected by the Canonical Plan comparison.

    ``retryable_error_codes`` is a frozenset of error-code strings that
    are eligible for retry.  When empty (default), only
    :class:`RetryableAgentError` and timeout are retried.  When
    non-empty, only error codes in the set are retried.

    R6 P1 — ``retryable_error_codes`` is validated at construction:

    * Blank / whitespace-only strings are rejected.
    * Codes in :data:`NEVER_RETRYABLE_ERROR_CODES` are rejected —
      listing ``invalid_receipt`` or ``non_retryable_error`` in the
      allowlist would be a no-op at runtime (``should_retry()`` always
      refuses them), so rejecting them at planning time surfaces the
      misconfiguration immediately instead of letting a bad policy
      silently enter the Plan Hash.
    """

    max_retries: int = Field(default=0, ge=0, le=3)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("retryable_error_codes")
    @classmethod
    def _validate_retryable_error_codes(cls, v: frozenset[str]) -> frozenset[str]:
        """R6 P1: reject blank strings and never-retryable codes.

        A frozenset could otherwise contain ``""``, ``" "``, typos, or
        codes that :func:`should_retry` would refuse anyway.  Catching
        these at construction prevents a bad policy from entering the
        Plan Hash and only being discovered at runtime.
        """
        cleaned: set[str] = set()
        for code in v:
            stripped = code.strip()
            if not stripped:
                raise ValueError("retryable_error_codes must not contain blank strings")
            if stripped in NEVER_RETRYABLE_ERROR_CODES:
                raise ValueError(
                    f"retryable_error_codes must not contain "
                    f"never-retryable code {stripped!r} — codes in "
                    f"NEVER_RETRYABLE_ERROR_CODES are always refused by "
                    f"should_retry(); listing them is a no-op that "
                    f"indicates a misconfiguration"
                )
            cleaned.add(stripped)
        return frozenset(cleaned)


# ---------------------------------------------------------------------------
# PlanningSignals
# ---------------------------------------------------------------------------


class RequestedTask(StrictContract):
    """Explicit Domain→Task mapping for non-template multi-agent plans.

    When a request enters the ``multi_agent`` route without a template,
    the planner requires an explicit :class:`RequestedTask` per task.
    This prevents the planner from arbitrarily binding every task to
    ``sorted(domains)[0]`` or guessing the domain from the task type.

    The ``preferred_authority`` field carries the *minimum* authority
    the eventual agent must have.  Phase 3 bounds it to READ or PROPOSE.
    """

    intent_id: str
    domain: str
    task_type: str
    objective: str

    dependencies: list[str] = Field(default_factory=list)
    required: bool = True

    preferred_authority: AgentAuthority = AgentAuthority.READ
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    estimated_tool_calls: int = Field(default=0, ge=0)

    # R5 P0-1 — retry policy is now a first-class planning contract.
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("intent_id")
    @classmethod
    def _intent_id_required(cls, v: str) -> str:
        return _validate_resource_id(v, "intent_id")

    @field_validator("domain")
    @classmethod
    def _domain_non_blank(cls, v: str) -> str:
        return _non_blank(v, "domain")

    @field_validator("task_type")
    @classmethod
    def _task_type_non_blank(cls, v: str) -> str:
        return _non_blank(v, "task_type")

    @field_validator("objective")
    @classmethod
    def _objective_non_blank(cls, v: str) -> str:
        return _non_blank(v, "objective")

    @field_validator("preferred_authority")
    @classmethod
    def _authority_bounded(cls, v: AgentAuthority) -> AgentAuthority:
        if v is AgentAuthority.EXECUTE:
            raise ValueError(
                "RequestedTask.preferred_authority must not be EXECUTE in Phase 3"
            )
        return v

    @field_validator("dependencies")
    @classmethod
    def _dependencies_dedup(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for dep in v:
            stripped = dep.strip()
            if not stripped:
                raise ValueError("dependency must not be blank")
            if stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
        return out

    @field_validator("required_tools")
    @classmethod
    def _required_tools_non_blank(cls, v: frozenset[str]) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                raise ValueError("required_tools members must be strings")
            stripped = item.strip()
            if not stripped:
                raise ValueError("required_tools members must not be blank")
            cleaned.add(stripped)
        return frozenset(cleaned)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "RequestedTask.metadata")
        from multi_agent.serialization import validate_strict_json

        return validate_strict_json(v)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _no_self_dependency(self) -> RequestedTask:
        if self.intent_id and self.intent_id in self.dependencies:
            raise ValueError("RequestedTask cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _tool_calls_cover_required_tools(self) -> RequestedTask:
        """R2 P0-5: a task that requires tools must not declare zero
        tool calls.  The minimum estimate is one call per required tool.
        """
        if self.required_tools and self.estimated_tool_calls < len(self.required_tools):
            raise ValueError(
                f"RequestedTask {self.intent_id!r} requires "
                f"{len(self.required_tools)} tool(s) but estimated_tool_calls="
                f"{self.estimated_tool_calls}; must be >= "
                f"{len(self.required_tools)}"
            )
        return self


class PlanningSignals(StrictContract):
    """Trusted system-side signals about a request.

    Signals are *system input* — they describe what the runtime knows
    about the request, not what an LLM inferred.  They MUST NOT carry
    tenant overrides, actor identity, budget, prompts, or internal
    reasoning.
    """

    event_type: str | None = None
    domains: frozenset[str] = Field(default_factory=frozenset)
    requested_task_types: frozenset[str] = Field(default_factory=frozenset)
    requested_tasks: list[RequestedTask] = Field(default_factory=list)

    requires_cross_domain: bool = False
    requires_write: bool = False
    requires_approval: bool = False
    has_conflicting_signals: bool = False
    missing_required_context: bool = False

    objective_kind: str | None = None

    @field_validator("event_type")
    @classmethod
    def _event_type_non_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("event_type must not be blank when present")
        return v

    @field_validator("objective_kind")
    @classmethod
    def _objective_kind_non_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("objective_kind must not be blank when present")
        return v

    @field_validator("domains", "requested_task_types")
    @classmethod
    def _string_set_non_blank(cls, v: frozenset[str]) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                raise ValueError("signal set members must be strings")
            stripped = item.strip()
            if not stripped:
                raise ValueError("signal set members must not be blank")
            cleaned.add(stripped)
        return frozenset(cleaned)

    @model_validator(mode="after")
    def _requested_tasks_consistency(self) -> PlanningSignals:
        """If requested_tasks is non-empty, ``domains`` and
        ``requested_task_types`` (when explicitly provided) must equal
        the sets derived from the tasks.

        R2 P0-2: ``requested_tasks`` is the primary source of truth.
        When only ``requested_tasks`` is provided (``domains`` and
        ``requested_task_types`` empty), no consistency check is
        performed — the derived sets are the effective sets.
        """
        if not self.requested_tasks:
            return self
        task_domains = {t.domain for t in self.requested_tasks}
        task_types = {t.task_type for t in self.requested_tasks}
        if self.domains and self.domains != frozenset(task_domains):
            raise ValueError(
                "signals.domains must equal the set of RequestedTask.domain "
                f"values when both are present: {self.domains} vs {task_domains}"
            )
        if self.requested_task_types and self.requested_task_types != frozenset(
            task_types
        ):
            raise ValueError(
                "signals.requested_task_types must equal the set of "
                "RequestedTask.task_type values when both are present: "
                f"{self.requested_task_types} vs {task_types}"
            )
        return self


# ---------------------------------------------------------------------------
# PlanningRequest
# ---------------------------------------------------------------------------


class PlanningRequest(StrictContract):
    """The single input to :class:`ComplexityGate` and :class:`Planner`.

    Authorization, API keys, or full customer records MUST NOT be placed
    in ``context_summary`` — it is an opaque minimum-necessary summary
    string.
    """

    run_id: str
    tenant_id: str

    actor_type: Literal["user", "service"]
    actor_id: str

    objective: str
    signals: PlanningSignals
    budget: ExecutionBudget

    context_summary: str | None = None
    registry_version: str

    @field_validator("run_id")
    @classmethod
    def _run_id_required(cls, v: str) -> str:
        return _validate_resource_id(v, "run_id")

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        return _non_blank(v, "tenant_id")

    @field_validator("actor_id")
    @classmethod
    def _actor_id_required(cls, v: str) -> str:
        return _non_blank(v, "actor_id")

    @field_validator("objective")
    @classmethod
    def _objective_required(cls, v: str) -> str:
        return _non_blank(v, "objective")

    @field_validator("context_summary")
    @classmethod
    def _context_summary_safe(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        from multi_agent.contracts import (
            _NORMALIZED_SECRET_PATTERNS,
            _normalize_sensitive_key,
        )

        normalized = _normalize_sensitive_key(v)
        if any(pattern in normalized for pattern in _NORMALIZED_SECRET_PATTERNS):
            raise ValueError("context_summary appears to contain a secret")
        return v

    @field_validator("registry_version")
    @classmethod
    def _registry_version_required(cls, v: str) -> str:
        return _non_blank(v, "registry_version")


# ---------------------------------------------------------------------------
# TaskIntent (template-only — RequestedTask is the user-facing form)
# ---------------------------------------------------------------------------


class TaskIntent(StrictContract):
    """Planner's intent for a single task — *before* agent selection.

    Emitted by templates (e.g. CustomerRecoveryTemplate) and by the
    generic multi-agent planner when converting
    :class:`RequestedTask` instances into intents.

    A :class:`TaskIntent` does not carry a concrete handler or Python
    object.  ``preferred_authority`` is bounded to ``READ`` or
    ``PROPOSE`` in Phase 3; ``EXECUTE`` is rejected at construction.
    """

    intent_id: str
    task_type: str
    domain: str
    objective: str

    dependencies: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    required: bool = True

    preferred_authority: AgentAuthority = AgentAuthority.READ
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    estimated_tool_calls: int = Field(default=0, ge=0)

    # R5 P0-1 — retry policy propagated from RequestedTask.
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("intent_id")
    @classmethod
    def _intent_id_required(cls, v: str) -> str:
        return _validate_resource_id(v, "intent_id")

    @field_validator("task_type")
    @classmethod
    def _task_type_non_blank(cls, v: str) -> str:
        return _non_blank(v, "task_type")

    @field_validator("domain")
    @classmethod
    def _domain_non_blank(cls, v: str) -> str:
        return _non_blank(v, "domain")

    @field_validator("objective")
    @classmethod
    def _objective_non_blank(cls, v: str) -> str:
        return _non_blank(v, "objective")

    @field_validator("preferred_authority")
    @classmethod
    def _authority_bounded(cls, v: AgentAuthority) -> AgentAuthority:
        if v is AgentAuthority.EXECUTE:
            raise ValueError(
                "TaskIntent.preferred_authority must not be EXECUTE in Phase 3"
            )
        return v

    @field_validator("dependencies")
    @classmethod
    def _dependencies_dedup(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for dep in v:
            stripped = dep.strip()
            if not stripped:
                raise ValueError("dependency must not be blank")
            if stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
        return out

    @field_validator("required_tools")
    @classmethod
    def _required_tools_non_blank(cls, v: frozenset[str]) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                raise ValueError("required_tools members must be strings")
            stripped = item.strip()
            if not stripped:
                raise ValueError("required_tools members must not be blank")
            cleaned.add(stripped)
        return frozenset(cleaned)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "TaskIntent.metadata")
        from multi_agent.serialization import validate_strict_json

        return validate_strict_json(v)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _no_self_dependency(self) -> TaskIntent:
        if self.intent_id and self.intent_id in self.dependencies:
            raise ValueError("TaskIntent cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _tool_calls_cover_required_tools(self) -> TaskIntent:
        """R2 P0-5: a task that requires tools must not declare zero
        tool calls.  The minimum estimate is one call per required tool.
        """
        if self.required_tools and self.estimated_tool_calls < len(self.required_tools):
            raise ValueError(
                f"TaskIntent {self.intent_id!r} requires "
                f"{len(self.required_tools)} tool(s) but estimated_tool_calls="
                f"{self.estimated_tool_calls}; must be >= "
                f"{len(self.required_tools)}"
            )
        return self


def task_intent_from_requested_task(rt: RequestedTask) -> TaskIntent:
    """Convert a :class:`RequestedTask` into a :class:`TaskIntent`."""
    return TaskIntent(
        intent_id=rt.intent_id,
        task_type=rt.task_type,
        domain=rt.domain,
        objective=rt.objective,
        dependencies=list(rt.dependencies),
        required=rt.required,
        preferred_authority=rt.preferred_authority,
        required_tools=rt.required_tools,
        estimated_tool_calls=rt.estimated_tool_calls,
        retry_policy=rt.retry_policy,
        metadata=dict(rt.metadata),
    )


# ---------------------------------------------------------------------------
# Effective domains / task types (R2 P0-2)
# ---------------------------------------------------------------------------


def effective_domains(signals: PlanningSignals) -> frozenset[str]:
    """Return the effective domain set for *signals*.

    If ``signals.requested_tasks`` is non-empty, the effective domain set
    is *derived* from the tasks (the union of every ``RequestedTask.domain``).
    Otherwise, the explicit ``signals.domains`` set is returned.

    This makes :attr:`PlanningSignals.requested_tasks` the primary source
    of truth for routing decisions, while keeping ``domains`` as a
    compatibility field that must agree when both are present.
    """
    if signals.requested_tasks:
        return frozenset({t.domain for t in signals.requested_tasks})
    return signals.domains


def effective_task_types(signals: PlanningSignals) -> frozenset[str]:
    """Return the effective task-type set for *signals*.

    Derived from ``requested_tasks`` when present; otherwise the explicit
    ``requested_task_types`` set is returned.
    """
    if signals.requested_tasks:
        return frozenset({t.task_type for t in signals.requested_tasks})
    return signals.requested_task_types


# ---------------------------------------------------------------------------
# Expected intents — shared pure function (R2 P0-1)
# ---------------------------------------------------------------------------


def resolve_expected_intents(
    request: PlanningRequest,
    decision: ComplexityDecision,
) -> list[TaskIntent]:
    """Return the canonical list of :class:`TaskIntent` expected for *request*.

    This is the **single source of truth** for what intents a plan should
    contain.  Both :class:`DeterministicPlanner` and :class:`PlanValidator`
    must call this function so that a tampered plan cannot pass validation
    even if every individual task is registry-supported.

    Routing rules:

    * ``deterministic_workflow`` → empty list (no tasks).
    * ``single_agent`` → exactly one intent.  Derived from
      ``requested_tasks[0]`` when present, otherwise synthesised from
      ``signals.domains`` / ``signals.requested_task_types`` /
      ``request.objective``.  Multiple ``requested_tasks`` are rejected
      (single-agent cannot carry multiple intents).
    * ``multi_agent`` with ``objective_kind == customer_recovery`` →
      Customer Recovery template intents (5 tasks).
    * ``multi_agent`` otherwise → one intent per ``requested_tasks``.
      Missing ``requested_tasks`` is a :class:`PlanningInputError`.
    """
    from multi_agent.planning_errors import PlanningInputError

    route = decision.route

    if route == "deterministic_workflow":
        return []

    if route == "single_agent":
        rts = request.signals.requested_tasks
        if len(rts) > 1:
            raise PlanningInputError(
                "single_agent route cannot carry more than one RequestedTask; "
                f"got {len(rts)}"
            )
        if rts:
            return [task_intent_from_requested_task(rts[0])]
        # Synthesise a single primary intent from signals.
        domains = effective_domains(request.signals)
        task_types = effective_task_types(request.signals)
        if not domains:
            raise PlanningInputError(
                "single_agent route requires at least one domain in signals"
            )
        domain = sorted(domains)[0]
        task_type = sorted(task_types)[0] if task_types else "default"
        authority = (
            AgentAuthority.PROPOSE
            if request.signals.requires_approval or request.signals.requires_write
            else AgentAuthority.READ
        )
        return [
            TaskIntent(
                intent_id="primary",
                task_type=task_type,
                domain=domain,
                objective=request.objective,
                dependencies=[],
                required=True,
                preferred_authority=authority,
                required_tools=frozenset(),
                estimated_tool_calls=0,
            )
        ]

    if route == "multi_agent":
        # Customer Recovery template.
        if request.signals.objective_kind == "customer_recovery":
            from multi_agent.planning_templates import (
                DEFAULT_CUSTOMER_RECOVERY_TEMPLATE,
            )

            return DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.build_intents()

        rts = request.signals.requested_tasks
        if not rts:
            raise PlanningInputError(
                "multi_agent route without a template requires explicit "
                "signals.requested_tasks; cannot infer domain→task mapping"
            )
        return [task_intent_from_requested_task(rt) for rt in rts]

    raise PlanningInputError(f"unknown route {route!r}")


# ---------------------------------------------------------------------------
# Shared pure functions for Planner + Validator (R3 + R4)
# ---------------------------------------------------------------------------


def validate_intent_graph(intents: Sequence[TaskIntent]) -> list[str]:
    """R4 P0-1 — validate the Intent dependency graph.

    Shared between Planner and Validator so both sides agree on what
    makes an intent graph valid *before* Agent Assignment or Canonical
    Task construction.  This closes the ``KeyError`` hole: previously
    only the Planner validated the graph, so a tampered request with
    missing dependencies would crash the Validator's
    :func:`build_expected_planned_tasks` via ``intent_to_task_id[dep]``.

    Returns a list of stable issue code strings (empty == valid):

    * :data:`CODE_INTENT_DUPLICATE_ID` — duplicate ``intent_id``.
    * :data:`CODE_INTENT_MISSING_DEPENDENCY` — dependency references a
      non-existent ``intent_id``.
    * :data:`CODE_INTENT_CYCLE` — dependency graph contains a cycle.

    Cycle detection is skipped when missing dependencies exist — they
    would masquerade as roots and produce misleading cycle reports.
    """
    issues: list[str] = []
    if not intents:
        return issues

    # Duplicate intent_id.
    seen: set[str] = set()
    for intent in intents:
        if intent.intent_id in seen:
            issues.append(CODE_INTENT_DUPLICATE_ID)
        seen.add(intent.intent_id)

    # Missing dependencies.
    intent_ids = {i.intent_id for i in intents}
    has_missing = False
    for intent in intents:
        missing = set(intent.dependencies) - intent_ids
        if missing:
            issues.append(CODE_INTENT_MISSING_DEPENDENCY)
            has_missing = True

    # Cycle detection — only when no missing deps (they'd mask as roots).
    if not has_missing:
        graph: dict[str, set[str]] = {i.intent_id: set() for i in intents}
        in_degree: dict[str, int] = {i.intent_id: 0 for i in intents}
        for intent in intents:
            for dep in intent.dependencies:
                graph[dep].add(intent.intent_id)
                in_degree[intent.intent_id] += 1
        queue: list[str] = sorted(iid for iid, deg in in_degree.items() if deg == 0)
        visited: set[str] = set()
        while queue:
            node = queue.pop(0)
            visited.add(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        cycle_nodes = sorted(set(in_degree.keys()) - visited)
        if cycle_nodes:
            issues.append(CODE_INTENT_CYCLE)

    return issues


def validate_intent_tool_authority(intent: TaskIntent, registry: Any) -> None:
    """R4 P0-3 — Intent ``preferred_authority`` must cover the highest
    authority required by any of its ``required_tools``.

    Mapping (:data:`TOOL_TO_AGENT_AUTHORITY`):

    * READ Tool     → Intent Authority >= READ
    * PROPOSE Tool  → Intent Authority >= PROPOSE
    * EXECUTE Tool  → Phase 3 ceiling; rejected earlier by candidate filter

    Does NOT auto-elevate — fails closed with :class:`PlanningInputError`
    so the caller's explicitly declared authority boundary is preserved.

    Shared between Planner and Validator.  The Planner calls it before
    Agent Assignment so an invalid request fails fast.  The Validator
    calls it before Canonical Plan reconstruction so an invalid request
    produces a stable Issue instead of propagating an exception.
    """
    from multi_agent.planning_errors import PlanningInputError

    if not intent.required_tools:
        return

    for tool_name in intent.required_tools:
        if not registry.tool_catalog.is_registered(tool_name):
            # Unknown tool — handled by candidate filtering (returns []).
            continue
        tool = registry.tool_catalog.resolve(tool_name)
        if tool.authority is ToolAuthority.EXECUTE:
            # Phase 3 ceiling — handled by candidate filtering.
            continue
        required_authority = TOOL_TO_AGENT_AUTHORITY[tool.authority]
        if (
            _AUTHORITY_RANK[intent.preferred_authority]
            < _AUTHORITY_RANK[required_authority]
        ):
            raise PlanningInputError(
                f"Intent {intent.intent_id!r} preferred_authority="
                f"{intent.preferred_authority.value} is lower than required "
                f"tool {tool_name!r} authority={tool.authority.value}; "
                f"intent authority must be >= {required_authority.value}"
            )


def _longest_path_node_count(intents: Sequence[TaskIntent]) -> int:
    """R4 P0-2 — return the longest-path node count of the Intent DAG.

    Used by :func:`resolve_agent_assignment` to pre-check the iteration
    budget (``max_iterations``) before searching candidate combinations.
    The iteration budget bounds the Supervisor graph depth, which equals
    the longest path in the DAG (each node = one iteration).
    """
    if not intents:
        return 0

    graph: dict[str, set[str]] = {i.intent_id: set() for i in intents}
    in_degree: dict[str, int] = {i.intent_id: 0 for i in intents}
    for intent in intents:
        for dep in intent.dependencies:
            if dep in graph:
                graph[dep].add(intent.intent_id)
                in_degree[intent.intent_id] += 1

    longest: dict[str, int] = {iid: 1 for iid in graph}
    queue: list[str] = sorted(iid for iid, deg in in_degree.items() if deg == 0)
    while queue:
        node = queue.pop(0)
        for neighbor in sorted(graph[node]):
            if longest[neighbor] < longest[node] + 1:
                longest[neighbor] = longest[node] + 1
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    return max(longest.values()) if longest else 0


def _estimate_assignment_deadline_ms(
    intents: Sequence[TaskIntent],
    assignment: Mapping[str, Any],
) -> int:
    """R4 P0-2 — compute the DAG critical-path deadline for *assignment*.

    The critical path is the longest path through the Intent DAG,
    summing the ``timeout_ms`` of each assigned capability.  This is the
    same metric the Validator computes post-hoc on the PlanDraft; moving
    it into the assignment search ensures the Planner only picks
    deadline-feasible combinations instead of picking the cheapest
    combination and letting the Validator reject it.
    """
    if not intents:
        return 0

    timeout_by_intent = {
        i.intent_id: assignment[i.intent_id].timeout_ms for i in intents
    }
    graph: dict[str, set[str]] = {i.intent_id: set() for i in intents}
    in_degree: dict[str, int] = {i.intent_id: 0 for i in intents}
    for intent in intents:
        for dep in intent.dependencies:
            if dep in graph:
                graph[dep].add(intent.intent_id)
                in_degree[intent.intent_id] += 1

    deadline: dict[str, int] = {iid: timeout_by_intent[iid] for iid in graph}
    queue: list[str] = sorted(iid for iid, deg in in_degree.items() if deg == 0)
    while queue:
        node = queue.pop(0)
        for neighbor in sorted(graph[node]):
            if deadline[neighbor] < deadline[node] + timeout_by_intent[neighbor]:
                deadline[neighbor] = deadline[node] + timeout_by_intent[neighbor]
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    return max(deadline.values()) if deadline else 0


def _stable_task_id(
    *,
    run_id: str,
    intent_id: str,
    task_type: str,
    agent_id: str,
) -> str:
    """Deterministic 24-char task ID — no random UUIDs.

    Shared between Planner and Validator so both sides agree on what
    the canonical task_id should be for a given (run_id, intent_id,
    task_type, agent_id) tuple.
    """
    return stable_hash(
        {
            "run_id": run_id,
            "intent_id": intent_id,
            "task_type": task_type,
            "agent_id": agent_id,
        }
    )[:24]


def resolve_candidate_agents(
    intent: TaskIntent,
    registry: Any,
) -> list[Any]:
    """Return the stable, **tool-aware** candidate list for *intent*.

    Shared between Planner and Validator (R3 P0-2).  Both sides must
    agree on which agents are eligible — otherwise a tampered plan
    could substitute a more privileged or more expensive agent that
    the Validator would accept as "registry-supported".

    Filters (all AND, R2 P0-3):

    1. ``enabled=True``
    2. ``supported_tasks`` contains ``intent.task_type``
    3. ``domains`` contains ``intent.domain``
    4. ``authority`` is READ or PROPOSE (EXECUTE filtered out)
    5. ``authority >= intent.preferred_authority``
    6. ``required_tools ⊆ cap.allowed_tools`` AND every required tool
       exists in the catalog with authority <= cap.authority and
       <= PROPOSE (Phase 3 ceiling).

    Sort key (ascending, deterministic):

    1. ``_AUTHORITY_RANK[authority]`` — READ before PROPOSE
    2. ``_COST_CLASS_RANK[estimated_cost_class]``
    3. ``timeout_ms`` — smaller first
    4. ``agent_id`` — lexicographic
    5. ``version`` — lexicographic
    """
    candidates: list[Any] = []
    # Pre-validate required tools against the catalog once per intent.
    for tool_name in intent.required_tools:
        if not registry.tool_catalog.is_registered(tool_name):
            # Unknown tool → no candidate can satisfy this intent.
            return []
        tool = registry.tool_catalog.resolve(tool_name)
        # Phase 3 ceiling: required tools must be READ or PROPOSE.
        if tool.authority is ToolAuthority.EXECUTE:
            return []

    for cap in registry.list_all():
        if not cap.enabled:
            continue
        if intent.task_type not in cap.supported_tasks:
            continue
        if intent.domain not in cap.domains:
            continue
        if cap.authority is AgentAuthority.EXECUTE:
            # EXECUTE agents are filtered out, not failed on sight.
            continue
        if _AUTHORITY_RANK[cap.authority] < _AUTHORITY_RANK[intent.preferred_authority]:
            continue
        # R2 P0-3: tool-aware filtering.
        if not intent.required_tools.issubset(cap.allowed_tools):
            continue
        # Per-tool authority hierarchy check.
        tool_ok = True
        for tool_name in intent.required_tools:
            tool = registry.tool_catalog.resolve(tool_name)
            if (
                cap.authority is AgentAuthority.READ
                and tool.authority is not ToolAuthority.READ
            ):
                tool_ok = False
                break
            if (
                cap.authority is AgentAuthority.PROPOSE
                and tool.authority is ToolAuthority.EXECUTE
            ):
                tool_ok = False
                break
        if not tool_ok:
            continue
        candidates.append(cap)

    candidates.sort(
        key=lambda c: (
            _AUTHORITY_RANK[c.authority],
            _COST_CLASS_RANK[c.estimated_cost_class],
            c.timeout_ms,
            c.agent_id,
            c.version,
        )
    )
    return candidates


def resolve_agent_assignment(
    request: PlanningRequest,
    decision: ComplexityDecision,
    intents: list[TaskIntent],
    registry: Any,
) -> dict[str, Any]:
    """Return the canonical agent assignment for *intents*.

    Shared between Planner and Validator (R3 P0-2).  Both sides must
    agree on which agent is selected for each intent — otherwise a
    tampered plan could substitute a more privileged or more expensive
    agent that the Validator would accept as "registry-supported".

    Algorithm:

    * ``single_agent`` or ``len(intents) < 2``: greedy per-intent
      selection (first deadline-feasible candidate per intent).
    * ``multi_agent`` with ``len(intents) >= 2``: deterministic
      cartesian-product search for a diverse, **deadline-feasible**
      assignment.  Composite sort key: total authority rank →
      total cost class rank → total timeout → (intent_id, agent_id,
      version) tuple preserving the intent→agent mapping.

    R3 P1: the search is bounded by :data:`MAX_ASSIGNMENT_COMBINATIONS`.
    If the cartesian product exceeds this limit, the function fails
    closed with :class:`UnsupportedCapabilityError`.

    R4 P0-2: the search is **budget-aware**.  Pre-checks reject
    infeasible requests before searching:

    * ``len(intents) <= budget.max_tasks``
    * ``len(intents) <= budget.max_agent_calls``
    * ``sum(estimated_tool_calls) <= budget.max_tool_calls``
    * ``longest_path_node_count(intents) <= budget.max_iterations``

    Each candidate combination is filtered by
    ``_estimate_assignment_deadline_ms(combo) <= budget.deadline_ms``.
    Only deadline-feasible combinations enter the sort.  This ensures
    the Planner picks the cheapest *feasible* combination, not the
    cheapest combination overall (which might violate the deadline and
    be rejected by the Validator).

    R3 P1: if no feasible diverse assignment exists for
    ``multi_agent``, the function fails closed with
    :class:`UnsupportedCapabilityError`.  R4 P0-2: if diverse
    assignments exist but none are deadline-feasible, the function
    fails closed with :class:`BudgetExceededPlanningError`.

    R6 P0-2 — uses a **canonical intent order** (sorted by
    ``intent_id``) for every step: candidate-list construction,
    cartesian-product iteration, deadline estimation, and the final
    tie-breaker.  The tie-breaker is now an ``intent_id → agent_id →
    version`` tuple that preserves the intent→agent *mapping*;
    previously two semantically-identical requests with permuted
    ``requested_tasks`` could pick swapped agents because
    ``sorted(agent_ids)`` and ``sorted(versions)`` lost the mapping
    and produced the same key for different assignments.
    """
    from itertools import product

    from multi_agent.planning_errors import (
        BudgetExceededPlanningError,
        UnsupportedCapabilityError,
    )

    # R6 P0-2 — establish the canonical intent order once and use it
    # everywhere below.  This makes the assignment invariant under
    # list-order permutations of ``requested_tasks`` (the Request Hash
    # is already order-invariant via canonical_request_payload; the
    # assignment and Plan Hash must follow).
    canonical_intents = sorted(intents, key=lambda i: i.intent_id)

    # R4 P0-2 — pre-check all structural budgets before searching.
    budget = request.budget
    if len(canonical_intents) > budget.max_tasks:
        raise BudgetExceededPlanningError(
            f"intent count {len(canonical_intents)} > max_tasks {budget.max_tasks}"
        )
    if len(canonical_intents) > budget.max_agent_calls:
        raise BudgetExceededPlanningError(
            f"intent count {len(canonical_intents)} > "
            f"max_agent_calls {budget.max_agent_calls}"
        )
    total_tool_calls = sum(i.estimated_tool_calls for i in canonical_intents)
    if total_tool_calls > budget.max_tool_calls:
        raise BudgetExceededPlanningError(
            f"estimated_tool_calls {total_tool_calls} > "
            f"max_tool_calls {budget.max_tool_calls}"
        )
    longest_path_nodes = _longest_path_node_count(canonical_intents)
    if longest_path_nodes > budget.max_iterations:
        raise BudgetExceededPlanningError(
            f"intent DAG longest path {longest_path_nodes} nodes > "
            f"max_iterations {budget.max_iterations}"
        )

    # Build per-intent candidate lists (in canonical order).
    intent_candidates: dict[str, list[Any]] = {}
    for intent in canonical_intents:
        candidates = resolve_candidate_agents(intent, registry)
        if not candidates:
            raise UnsupportedCapabilityError(
                f"No READ/PROPOSE agent with required tools supports "
                f"task_type={intent.task_type!r} domain={intent.domain!r} "
                f"authority>={intent.preferred_authority.value} "
                f"required_tools={sorted(intent.required_tools)!r}"
            )
        intent_candidates[intent.intent_id] = candidates

    # single_agent or fewer than 2 intents → greedy selection.
    # R4 P0-2: filter by per-intent deadline (timeout_ms <= deadline_ms).
    if decision.route != "multi_agent" or len(canonical_intents) < 2:
        assignment: dict[str, Any] = {}
        for intent in canonical_intents:
            candidates = intent_candidates[intent.intent_id]
            feasible = [c for c in candidates if c.timeout_ms <= budget.deadline_ms]
            if not feasible:
                raise BudgetExceededPlanningError(
                    f"no deadline-feasible agent for intent "
                    f"{intent.intent_id!r}; all {len(candidates)} candidate(s) "
                    f"exceed deadline_ms={budget.deadline_ms}"
                )
            assignment[intent.intent_id] = feasible[0]
        return assignment

    # multi_agent → search for a diverse, budget-feasible assignment.
    lists = [intent_candidates[i.intent_id] for i in canonical_intents]
    intent_ids = [i.intent_id for i in canonical_intents]

    # R3 P1 — bound the search space.
    total_combinations = 1
    for lst in lists:
        total_combinations *= max(len(lst), 1)
        if total_combinations > MAX_ASSIGNMENT_COMBINATIONS:
            raise UnsupportedCapabilityError(
                f"agent assignment search space exceeds "
                f"MAX_ASSIGNMENT_COMBINATIONS={MAX_ASSIGNMENT_COMBINATIONS}; "
                f"cannot find a deterministic diverse assignment"
            )

    best_assignment: dict[str, Any] | None = None
    best_key: tuple[Any, ...] | None = None
    any_diverse_found = False

    for combo in product(*lists):
        distinct_agents = {c.agent_id for c in combo}
        if len(distinct_agents) < 2:
            continue
        any_diverse_found = True
        # R4 P0-2 — filter by DAG critical-path deadline.
        combo_assignment = dict(zip(intent_ids, combo))
        combo_deadline = _estimate_assignment_deadline_ms(
            canonical_intents, combo_assignment
        )
        if combo_deadline > budget.deadline_ms:
            continue
        total_auth = sum(_AUTHORITY_RANK[c.authority] for c in combo)
        total_cost = sum(_COST_CLASS_RANK[c.estimated_cost_class] for c in combo)
        total_timeout = sum(c.timeout_ms for c in combo)
        # R6 P0-2 — preserve intent→agent→version mapping in the
        # tie-breaker.  Previously ``sorted(agent_ids)`` and
        # ``sorted(versions)`` were independent lists that lost the
        # mapping, so two semantically-identical requests with
        # permuted requested_tasks could pick swapped agents (same
        # total auth/cost/timeout, same sorted agent_ids, same sorted
        # versions, but different intent→agent assignment) and produce
        # different plan_hash despite identical request_hash.
        assignment_key = tuple(
            (intent.intent_id, cap.agent_id, cap.version)
            for intent, cap in zip(canonical_intents, combo)
        )
        key = (
            total_auth,
            total_cost,
            total_timeout,
            assignment_key,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_assignment = combo_assignment

    if best_assignment is None:
        if not any_diverse_found:
            # R3 P1 — fail closed instead of returning a greedy assignment
            # that the Validator would reject with multi_agent_too_few_agents.
            raise UnsupportedCapabilityError(
                "no feasible multi-agent assignment with >=2 distinct agents; "
                "cannot satisfy multi_agent route diversity requirement"
            )
        # R4 P0-2 — diverse assignments exist but none are deadline-feasible.
        raise BudgetExceededPlanningError(
            f"no budget-feasible diverse assignment found; "
            f"all {total_combinations} combination(s) exceed "
            f"deadline_ms={budget.deadline_ms}"
        )

    return best_assignment


def build_expected_planned_tasks(
    request: PlanningRequest,
    intents: list[TaskIntent],
    assignment: dict[str, Any],
) -> list[PlannedTask]:
    """Build the canonical :class:`PlannedTask` list for *intents*.

    Shared between Planner and Validator (R3 P0-3).  Both sides must
    agree on every field of the canonical task — otherwise a tampered
    plan could substitute different timeout, max_retries, status,
    input_data, etc. values that the Validator would accept.

    The canonical task is **fully determined** by
    (request, intent, capability).  No field is left to the Planner's
    discretion.

    Canonical field values:

    * ``task_id`` = ``stable_hash({run_id, intent_id, task_type, agent_id})[:24]``
    * ``agent_id`` = ``assignment[intent_id].agent_id``
    * ``task_type`` = ``intent.task_type``
    * ``objective`` = ``intent.objective``
    * ``tenant_id`` = ``request.tenant_id``
    * ``dependencies`` = resolved intent dependencies (intent_id → task_id)
    * ``required`` = ``intent.required``
    * ``required_evidence`` = ``list(intent.required_evidence)``
    * ``timeout_ms`` = ``cap.timeout_ms``  (R3 P0-3 — not lowerable)
    * ``max_retries`` = ``intent.retry_policy.max_retries``  (R5 P0-1 —
      configurable via :class:`RetryPolicy`, default 0)
    * ``idempotency_key`` = ``f"{run_id}:{task_id}"``
    * ``priority`` = ``"medium"``  (Phase 3 default)
    * ``status`` = ``"pending"``  (R3 P0-3 — Plan-time invariant)
    * ``input_data`` = ``{}``  (R3 P0-3 — Plan-time invariant)
    * ``user_id`` = ``None``  (R3 P0-3 — Plan-time invariant)
    * ``correlation_id`` = ``None``  (R3 P0-3 — Plan-time invariant)
    * ``started_at`` = ``None``  (R3 P0-3 — Plan-time invariant)
    * ``completed_at`` = ``None``  (R3 P0-3 — Plan-time invariant)
    * ``planning_metadata`` = ``dict(intent.metadata)``  (R4 P1-1 — enters
      Plan Hash and Canonical comparison so Phase 4 can recover template
      phase / context information)

    R6 P0-2 — iterates over ``intents`` in **canonical intent order**
    (sorted by ``intent_id``) so that two semantically-identical
    requests with permuted ``requested_tasks`` produce identical
    PlannedTask lists (same order, same content) and therefore the
    same Plan Hash.
    """
    from multi_agent.contracts import AgentTask

    # R6 P0-2 — canonical intent order, shared with resolve_agent_assignment.
    canonical_intents = sorted(intents, key=lambda i: i.intent_id)

    # Build intent_id → task_id mapping first (for dependency resolution).
    intent_to_task_id: dict[str, str] = {}
    for intent in canonical_intents:
        cap = assignment[intent.intent_id]
        task_id = _stable_task_id(
            run_id=request.run_id,
            intent_id=intent.intent_id,
            task_type=intent.task_type,
            agent_id=cap.agent_id,
        )
        intent_to_task_id[intent.intent_id] = task_id

    planned_tasks: list[PlannedTask] = []
    for intent in canonical_intents:
        cap = assignment[intent.intent_id]
        task_id = intent_to_task_id[intent.intent_id]
        resolved_deps: frozenset[str] = frozenset(
            intent_to_task_id[dep] for dep in intent.dependencies
        )
        task = AgentTask(
            task_id=task_id,
            agent_id=cap.agent_id,
            task_type=intent.task_type,
            objective=intent.objective,
            tenant_id=request.tenant_id,
            dependencies=resolved_deps,
            required=intent.required,
            required_evidence=list(intent.required_evidence),
            timeout_ms=cap.timeout_ms,
            max_retries=intent.retry_policy.max_retries,
            idempotency_key=f"{request.run_id}:{task_id}",
            priority="medium",
            status="pending",
            input_data={},
            user_id=None,
            correlation_id=None,
            started_at=None,
            completed_at=None,
        )
        planned_tasks.append(
            PlannedTask(
                intent_id=intent.intent_id,
                domain=intent.domain,
                preferred_authority=intent.preferred_authority,
                required_tools=intent.required_tools,
                estimated_tool_calls=intent.estimated_tool_calls,
                required=intent.required,
                planning_metadata=dict(intent.metadata),
                retry_policy=intent.retry_policy,
                task=task,
            )
        )
    return planned_tasks


# ---------------------------------------------------------------------------
# PlannedTask
# ---------------------------------------------------------------------------


class PlannedTask(StrictContract):
    """A :class:`TaskIntent` bound to a concrete :class:`AgentTask`.

    Carries the planning-side metadata (intent id, domain, preferred
    authority, required tools, estimated tool-call count, required flag,
    and planning metadata) that the Validator needs *without* polluting
    :class:`AgentTask` itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    domain: str
    preferred_authority: AgentAuthority
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    estimated_tool_calls: int = Field(default=0, ge=0)
    required: bool = True

    # R4 P1-1 — planning_metadata is copied verbatim from
    # TaskIntent.metadata by build_expected_planned_tasks.  It enters
    # the Plan Hash and the Canonical Plan comparison so Phase 4 can
    # recover template phase / context information, and so a tampered
    # plan cannot silently drop or alter metadata.
    planning_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    # R5 P0-1 — retry_policy is copied verbatim from
    # TaskIntent.retry_policy by build_expected_planned_tasks.  It
    # enters the Plan Hash and the Canonical Plan comparison so a
    # tampered plan cannot silently change the retry configuration.
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    task: AgentTask

    @field_validator("intent_id")
    @classmethod
    def _intent_id_required(cls, v: str) -> str:
        return _validate_resource_id(v, "intent_id")

    @field_validator("domain")
    @classmethod
    def _domain_non_blank(cls, v: str) -> str:
        return _non_blank(v, "domain")

    @field_validator("preferred_authority")
    @classmethod
    def _authority_bounded(cls, v: AgentAuthority) -> AgentAuthority:
        if v is AgentAuthority.EXECUTE:
            raise ValueError(
                "PlannedTask.preferred_authority must not be EXECUTE in Phase 3"
            )
        return v

    @field_validator("required_tools")
    @classmethod
    def _required_tools_non_blank(cls, v: frozenset[str]) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                raise ValueError("required_tools members must be strings")
            stripped = item.strip()
            if not stripped:
                raise ValueError("required_tools members must not be blank")
            cleaned.add(stripped)
        return frozenset(cleaned)

    @field_validator("planning_metadata")
    @classmethod
    def _validate_planning_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "PlannedTask.planning_metadata")
        from multi_agent.serialization import validate_strict_json

        return validate_strict_json(v)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _task_no_self_dependency(self) -> PlannedTask:
        if self.task.task_id and self.task.task_id in self.task.dependencies:
            raise ValueError("PlannedTask.task cannot depend on itself")
        return self


# ---------------------------------------------------------------------------
# PlanDraft
# ---------------------------------------------------------------------------


def _canonical_tasks_payload(tasks: list[PlannedTask]) -> list[dict[str, Any]]:
    """Return a stable, hash-friendly list of task dicts.

    Order-independent: tasks are sorted by ``task_id`` before hashing
    so that list reordering does not change the plan hash (the DAG
    structure is preserved in each task's ``dependencies`` field).

    Volatile fields excluded from each task:
      - ``task.created_at`` (wall-clock)
      - ``task.started_at`` (wall-clock, always None at plan time)
      - ``task.completed_at`` (wall-clock, always None at plan time)

    R6 P0-1 — uses ``mode="python"`` so that ``frozenset`` fields
    (``dependencies`` / ``required_tools`` / ``allowed_tools``) reach
    the Canonicalizer's set/frozenset branch (which sorts) instead of
    being converted to plain lists with process-random iteration order
    that produced different ``plan_hash`` across ``PYTHONHASHSEED``
    values.
    """
    sorted_tasks = sorted(tasks, key=lambda pt: pt.task.task_id)
    _exclude = {"created_at", "started_at", "completed_at"}
    out: list[dict[str, Any]] = []
    for pt in sorted_tasks:
        data = pt.model_dump(mode="python")
        task_data = data.get("task", {})
        for k in _exclude:
            task_data.pop(k, None)
        data["task"] = task_data
        out.append(canonicalize(data))
    return out


def canonical_request_payload(request: PlanningRequest) -> dict[str, Any]:
    """Build an order-invariant canonical payload for Request Hash.

    R5 P0-3 — ``compute_request_hash`` previously hashed
    ``request.signals.model_dump(mode="json")`` directly, which preserved
    the insertion order of ``requested_tasks`` and ``dependencies``.
    Two semantically identical requests differing only in list order
    produced different hashes, violating the Phase 3 invariant that
    *task order does not encode dependency — dependencies are expressed
    by the DAG*.

    This function normalizes the signals payload:

    * ``requested_tasks`` → sorted by ``intent_id``;
    * each task's ``dependencies`` → sorted lexicographically;
    * ``domains``, ``requested_task_types``, ``required_tools`` → sorted
      (these are already ``frozenset`` on the model, but ``model_dump``
      emits them as lists with arbitrary order — we make the order
      deterministic here so the hash is stable).

    Field values themselves (e.g. ``objective``, ``intent_id``) are
    NOT modified — only list ordering is normalized.

    R6 P0-1 — uses ``mode="python"`` so that ``frozenset`` fields
    (``domains`` / ``requested_task_types`` / ``required_tools``)
    reach :func:`canonicalize`'s set/frozenset branch (which sorts)
    instead of being converted to plain lists first.  Combined with
    the explicit sort of ``requested_tasks`` by ``intent_id`` and
    each task's ``dependencies``, this makes the Request Hash fully
    invariant under list-order permutations *and* stable across
    ``PYTHONHASHSEED`` values.
    """
    signals_data = request.signals.model_dump(mode="python")

    # Sort requested_tasks by intent_id; sort each task's dependencies.
    requested_tasks = signals_data.get("requested_tasks") or []
    for task in requested_tasks:
        if isinstance(task, dict) and "dependencies" in task:
            deps = task["dependencies"]
            if isinstance(deps, list):
                task["dependencies"] = sorted(deps)
    requested_tasks_sorted = sorted(
        requested_tasks, key=lambda t: t.get("intent_id", "")
    )
    signals_data["requested_tasks"] = requested_tasks_sorted

    # frozenset fields (domains / requested_task_types / required_tools)
    # are preserved by mode="python" and will be sorted by canonicalize().
    # We still sort set-like values defensively in case any slipped
    # through as plain lists (e.g. via Pydantic v2 edge cases).
    for set_field in ("domains", "requested_task_types"):
        v = signals_data.get(set_field)
        if isinstance(v, list):
            signals_data[set_field] = sorted(v)
    for task in signals_data["requested_tasks"]:
        if isinstance(task, dict):
            rt = task.get("required_tools")
            if isinstance(rt, list):
                task["required_tools"] = sorted(rt)

    return {
        "run_id": request.run_id,
        "tenant_id": request.tenant_id,
        "actor_type": request.actor_type,
        "actor_id": request.actor_id,
        "objective": request.objective,
        "signals": canonicalize(signals_data),
        "budget": canonicalize(request.budget.model_dump(mode="python")),
        "context_summary": request.context_summary,
        "registry_version": request.registry_version,
    }


def compute_request_hash(request: PlanningRequest) -> str:
    """Stable SHA-256 over the canonical PlanningRequest content.

    R5 P0-3 — uses :func:`canonical_request_payload` so the hash is
    invariant under list-order permutations of ``requested_tasks``,
    ``dependencies``, ``domains``, ``requested_task_types``, and
    ``required_tools``.  The hash only changes when the request's
    *semantic* content changes.
    """
    return stable_hash(canonical_request_payload(request))


def validate_write_approval_requirements(
    request: PlanningRequest,
    intents: Sequence[TaskIntent],
) -> list[str]:
    """Validate that a write/approval request carries at least one
    PROPOSE intent.

    R5 P0-1 — shared between Planner and Validator.  Previously the
    rule lived only in :class:`DeterministicPlanner` as the private
    method ``_validate_write_approval_requirements``, so a tampered
    request bypassing ``create_plan`` (e.g. a hand-built
    :class:`PlanDraft`) could pass Validator checks with
    ``requires_write=True`` and only READ tasks.

    Returns a list of stable Issue Codes (empty = valid):

    * :data:`CODE_WRITE_REQUEST_MISSING_PROPOSE` — ``requires_write``
      is set but no intent has ``preferred_authority == PROPOSE``.
    * :data:`CODE_APPROVAL_REQUEST_MISSING_PROPOSE` —
      ``requires_approval`` is set but no intent has
      ``preferred_authority == PROPOSE``.

    The Planner raises :class:`PlanningInputError` on the first issue;
    the Validator returns the codes as :class:`PlanValidationIssue`.
    """
    issues: list[str] = []
    if not intents:
        return issues
    signals = request.signals
    if not (signals.requires_write or signals.requires_approval):
        return issues
    has_propose = any(i.preferred_authority is AgentAuthority.PROPOSE for i in intents)
    if has_propose:
        return issues
    if signals.requires_write:
        issues.append(CODE_WRITE_REQUEST_MISSING_PROPOSE)
    if signals.requires_approval:
        issues.append(CODE_APPROVAL_REQUEST_MISSING_PROPOSE)
    return issues


def canonical_complexity_payload(
    decision: ComplexityDecision,
) -> dict[str, Any]:
    """Build a canonical, order-invariant payload for a ComplexityDecision.

    R7 P0-1 — shared between ``compute_plan_hash`` and
    ``PlanValidator._check_complexity_decision`` so both sides use the
    *same* definition of Complexity equality.

    Canonical rules:

    * ``domains`` — sorted; duplicates and blank elements are rejected;
    * ``reasons`` — sorted; duplicates and blank elements are rejected;
    * ``route`` — as-is;
    * ``confidence`` — as-is (R7: now enters comparison, was ignored
      before);
    * ``requires_human_review`` — as-is.

    Raises :class:`ValueError` on duplicate or blank elements so that
    a tampered ``ComplexityDecision`` (e.g. ``domains=["support",
    "support"]``) cannot produce a valid plan hash or pass Validator
    comparison.  The Planner never produces such values — the Gate
    emits clean, deduplicated domains/reasons — so this only triggers
    on post-construction tampering.
    """
    domains = decision.domains
    reasons = decision.reasons

    # Reject blank elements.
    for d in domains:
        if not isinstance(d, str) or not d.strip():
            raise ValueError(
                f"ComplexityDecision.domains contains a blank element: {domains!r}"
            )
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            raise ValueError(
                f"ComplexityDecision.reasons contains a blank element: {reasons!r}"
            )

    # Reject duplicates.
    if len(set(domains)) != len(domains):
        raise ValueError(f"ComplexityDecision.domains contains duplicates: {domains!r}")
    if len(set(reasons)) != len(reasons):
        raise ValueError(f"ComplexityDecision.reasons contains duplicates: {reasons!r}")

    return {
        "route": decision.route,
        "domains": sorted(domains),
        "reasons": sorted(reasons),
        "confidence": decision.confidence,
        "requires_human_review": decision.requires_human_review,
    }


def compute_plan_hash(
    *,
    request_hash: str,
    complexity: ComplexityDecision,
    tasks: list[PlannedTask],
    planner_version: str,
) -> str:
    """Stable SHA-256 over canonical plan content.

    Excludes ``summary``, ``warnings``, wall-clock times, and
    ``plan_hash`` itself.  Tasks are sorted by ``task_id`` so list
    reordering does not change the hash.

    R6 P0-1 — uses ``mode="python"`` for ``complexity`` so that any
    ``frozenset`` fields reach the Canonicalizer's set/frozenset branch
    (which sorts) instead of being converted to plain lists with
    process-random iteration order.

    R7 P0-1 — delegates complexity canonicalisation to
    :func:`canonical_complexity_payload`, which sorts ``domains`` /
    ``reasons``, rejects duplicates and blanks, and includes
    ``confidence``.  Both ``compute_plan_hash`` and
    ``PlanValidator._check_complexity_decision`` use the same function
    so there is a single definition of Complexity equality.
    """
    complexity_payload = canonical_complexity_payload(complexity)

    payload = {
        "request_hash": request_hash,
        "complexity": canonicalize(complexity_payload),
        "tasks": _canonical_tasks_payload(tasks),
        "planner_version": planner_version,
    }
    return stable_hash(payload)


class PlanDraft(StrictContract):
    """A complete, hash-verified plan ready for Phase 4+ execution.

    Stores the **full** :class:`PlanningRequest` snapshot so the draft
    can be re-verified *without* the caller holding the original
    request.  ``request_hash`` binds the snapshot; ``plan_hash`` binds
    the plan content.
    """

    request: PlanningRequest
    request_hash: str

    complexity: ComplexityDecision
    tasks: list[PlannedTask] = Field(default_factory=list)

    planner_version: str
    plan_hash: str = ""

    summary: str = ""
    warnings: list[str] = Field(default_factory=list)

    # Convenience accessors that delegate to the request snapshot.
    @property
    def run_id(self) -> str:
        return self.request.run_id

    @property
    def tenant_id(self) -> str:
        return self.request.tenant_id

    @property
    def actor_type(self) -> Literal["user", "service"]:
        return self.request.actor_type

    @property
    def actor_id(self) -> str:
        return self.request.actor_id

    @property
    def objective(self) -> str:
        return self.request.objective

    @property
    def registry_version(self) -> str:
        return self.request.registry_version

    @field_validator("planner_version")
    @classmethod
    def _planner_version_required(cls, v: str) -> str:
        return _non_blank(v, "planner_version")

    @field_validator("request")
    @classmethod
    def _request_deep_snapshot(cls, v: PlanningRequest) -> PlanningRequest:
        """R5 P0-2 — force a deep copy of the caller's PlanningRequest.

        Pydantic v2 reuses the same nested model instance when the
        caller passes a pre-built model, so without this guard
        ``plan.request is original_request`` holds and mutating the
        caller's request (or its nested ``PlanningSignals`` /
        ``RequestedTask``) corrupts the PlanDraft.  The snapshot must
        be independent at construction time so the PlanDraft's
        ``request_hash`` and ``plan_hash`` cannot be invalidated by
        external mutation.
        """
        return PlanningRequest.model_validate(v.model_dump(mode="python"))

    @field_validator("complexity")
    @classmethod
    def _complexity_deep_snapshot(cls, v: ComplexityDecision) -> ComplexityDecision:
        """R6 P0-3 — force a deep copy of the caller's ComplexityDecision.

        Without this guard, ``plan.complexity is original_complexity``
        holds and mutating the caller's ``ComplexityDecision`` (e.g.
        appending to ``domains`` or ``reasons``) corrupts the PlanDraft
        and invalidates ``plan_hash``.  The snapshot must be independent
        at construction time so the PlanDraft is a true deep snapshot
        of (request, complexity, tasks), not just ``request``.
        """
        return ComplexityDecision.model_validate(v.model_dump(mode="python"))

    @field_validator("tasks")
    @classmethod
    def _tasks_deep_snapshot(cls, v: list[PlannedTask]) -> list[PlannedTask]:
        """R6 P0-3 — force a deep copy of each caller-provided PlannedTask.

        ``PlannedTask`` itself is ``frozen=True``, but its nested
        ``AgentTask`` and ``planning_metadata`` dict are still mutable.
        Without this guard, ``plan.tasks[0] is original_planned_task``
        and ``plan.tasks[0].task is original_task`` hold, so mutating
        ``original_task.status`` or ``original_planning_metadata``
        corrupts the PlanDraft and invalidates ``plan_hash``.

        The snapshot must be independent at construction time so the
        PlanDraft is a true deep snapshot.  ``build_execution_tasks()``
        separately protects the Plan→Execution boundary; this validator
        protects the Caller→Plan boundary.
        """
        return [PlannedTask.model_validate(pt.model_dump(mode="python")) for pt in v]

    @field_validator("request_hash")
    @classmethod
    def _request_hash_required(cls, v: str) -> str:
        return _non_blank(v, "request_hash")

    @field_validator("summary")
    @classmethod
    def _summary_safe(cls, v: str) -> str:
        from multi_agent.contracts import (
            _NORMALIZED_SECRET_PATTERNS,
            _normalize_sensitive_key,
        )

        normalized = _normalize_sensitive_key(v)
        if any(pattern in normalized for pattern in _NORMALIZED_SECRET_PATTERNS):
            raise ValueError("summary appears to contain a secret")
        return v

    @field_validator("warnings")
    @classmethod
    def _warnings_dedup(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for w in v:
            if not isinstance(w, str):
                raise ValueError("warnings must be strings")
            stripped = w.strip()
            if not stripped:
                raise ValueError("warning must not be blank")
            if stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
        return out

    @model_validator(mode="after")
    def _verify_request_hash(self) -> PlanDraft:
        expected = compute_request_hash(self.request)
        if not compare_digest(self.request_hash, expected):
            raise ValueError(
                f"request_hash mismatch: provided "
                f"{self.request_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    @model_validator(mode="after")
    def _auto_compute_and_verify_hash(self) -> PlanDraft:
        """Auto-compute plan_hash if empty; verify if provided."""
        expected = self.compute_plan_hash()
        if not self.plan_hash:
            object.__setattr__(self, "plan_hash", expected)
        elif not compare_digest(self.plan_hash, expected):
            raise ValueError(
                f"plan_hash mismatch: provided "
                f"{self.plan_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    @model_validator(mode="after")
    def _tenant_homogeneity(self) -> PlanDraft:
        for pt in self.tasks:
            if pt.task.tenant_id != self.tenant_id:
                raise ValueError(
                    f"PlannedTask {pt.intent_id!r} tenant "
                    f"{pt.task.tenant_id!r} != plan tenant {self.tenant_id!r}"
                )
        return self

    # -- public API ---------------------------------------------------------

    def compute_plan_hash(self) -> str:
        """Recompute the plan hash from current content."""
        return compute_plan_hash(
            request_hash=self.request_hash,
            complexity=self.complexity,
            tasks=self.tasks,
            planner_version=self.planner_version,
        )

    def verify_integrity(self) -> None:
        """Raise :class:`PlanIntegrityError` if any hash is invalid."""
        from multi_agent.planning_errors import PlanIntegrityError

        expected_request_hash = compute_request_hash(self.request)
        if not compare_digest(self.request_hash, expected_request_hash):
            raise PlanIntegrityError(
                f"Plan {self.run_id!r}: stored request_hash does not match "
                f"recomputed request content"
            )
        if not compare_digest(self.plan_hash, self.compute_plan_hash()):
            raise PlanIntegrityError(
                f"Plan {self.run_id!r}: stored plan_hash does not match "
                f"recomputed plan content"
            )

    def build_execution_tasks(self) -> list[AgentTask]:
        """Return fresh :class:`AgentTask` copies for Phase 4+ dispatch.

        R5 P0-2 — previously this method returned the *internal*
        ``AgentTask`` references (``plan.tasks[i].task``), so mutating
        any returned task (e.g. setting ``status`` / ``started_at``)
        would corrupt the PlanDraft and invalidate ``plan_hash``.  The
        method now builds new ``AgentTask`` instances via
        ``model_validate(model_dump(mode="python"))`` so the PlanDraft
        stays immutable.

        Renamed from ``agent_tasks()`` to make the cost explicit —
        callers should treat the result as *execution* tasks, not as
        a view into the plan.
        """
        return [
            AgentTask.model_validate(pt.task.model_dump(mode="python"))
            for pt in self.tasks
        ]


# ---------------------------------------------------------------------------
# Plan Validation Report
# ---------------------------------------------------------------------------


class PlanValidationIssue(StrictContract):
    code: str
    severity: Literal["warning", "error"]
    message: str
    task_id: str | None = None

    @field_validator("code")
    @classmethod
    def _code_non_blank(cls, v: str) -> str:
        return _non_blank(v, "code")

    @field_validator("message")
    @classmethod
    def _message_non_blank(cls, v: str) -> str:
        return _non_blank(v, "message")


class PlanValidationReport(StrictContract):
    valid: bool
    issues: list[PlanValidationIssue] = Field(default_factory=list)
    topological_order: list[str] = Field(default_factory=list)
    estimated_agent_calls: int = Field(default=0, ge=0)
    estimated_tool_calls: int = Field(default=0, ge=0)
    estimated_iterations: int = Field(default=0, ge=0)
    estimated_deadline_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _valid_implies_no_errors(self) -> PlanValidationReport:
        if self.valid:
            for issue in self.issues:
                if issue.severity == "error":
                    raise ValueError(
                        f"valid=True but error-severity issue present: {issue.code!r}"
                    )
        return self


__all__ = [
    "CODE_APPROVAL_REQUEST_MISSING_PROPOSE",
    "CODE_INTENT_CYCLE",
    "CODE_INTENT_DUPLICATE_ID",
    "CODE_INTENT_MISSING_DEPENDENCY",
    "CODE_WRITE_REQUEST_MISSING_PROPOSE",
    "MAX_ASSIGNMENT_COMBINATIONS",
    "PLANNER_VERSION",
    "TOOL_TO_AGENT_AUTHORITY",
    "PlanDraft",
    "PlanValidationIssue",
    "PlanValidationReport",
    "PlannedTask",
    "PlanningRequest",
    "PlanningSignals",
    "RequestedTask",
    "TaskIntent",
    "build_expected_planned_tasks",
    "canonical_complexity_payload",
    "canonical_request_payload",
    "compute_plan_hash",
    "compute_request_hash",
    "effective_domains",
    "effective_task_types",
    "resolve_agent_assignment",
    "resolve_candidate_agents",
    "resolve_expected_intents",
    "task_intent_from_requested_task",
    "validate_intent_graph",
    "validate_intent_tool_authority",
    "validate_write_approval_requirements",
]
