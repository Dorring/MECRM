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

from hmac import compare_digest
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from multi_agent.contracts import (
    AgentAuthority,
    AgentTask,
    ComplexityDecision,
    ExecutionBudget,
    JsonValue,
    StrictContract,
    _non_blank,
    _reject_sensitive_keys,
    _validate_resource_id,
)
from multi_agent.serialization import canonicalize, stable_hash

# ---------------------------------------------------------------------------
# Planner version — bumped whenever the planner algorithm changes.
# ---------------------------------------------------------------------------

PLANNER_VERSION = "ma-03.2.0"


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
    def _no_self_dependency(self) -> "RequestedTask":
        if self.intent_id and self.intent_id in self.dependencies:
            raise ValueError("RequestedTask cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _tool_calls_cover_required_tools(self) -> "RequestedTask":
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
    def _requested_tasks_consistency(self) -> "PlanningSignals":
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
    def _no_self_dependency(self) -> "TaskIntent":
        if self.intent_id and self.intent_id in self.dependencies:
            raise ValueError("TaskIntent cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _tool_calls_cover_required_tools(self) -> "TaskIntent":
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
# PlannedTask
# ---------------------------------------------------------------------------


class PlannedTask(StrictContract):
    """A :class:`TaskIntent` bound to a concrete :class:`AgentTask`.

    Carries the planning-side metadata (intent id, domain, preferred
    authority, required tools, estimated tool-call count, required flag)
    that the Validator needs *without* polluting :class:`AgentTask`
    itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    domain: str
    preferred_authority: AgentAuthority
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    estimated_tool_calls: int = Field(default=0, ge=0)
    required: bool = True

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

    @model_validator(mode="after")
    def _task_no_self_dependency(self) -> "PlannedTask":
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
    """
    sorted_tasks = sorted(tasks, key=lambda pt: pt.task.task_id)
    _exclude = {"created_at", "started_at", "completed_at"}
    out: list[dict[str, Any]] = []
    for pt in sorted_tasks:
        data = pt.model_dump(mode="json")
        task_data = data.get("task", {})
        for k in _exclude:
            task_data.pop(k, None)
        data["task"] = task_data
        out.append(canonicalize(data))
    return out


def compute_request_hash(request: PlanningRequest) -> str:
    """Stable SHA-256 over the canonical PlanningRequest content."""
    payload = {
        "run_id": request.run_id,
        "tenant_id": request.tenant_id,
        "actor_type": request.actor_type,
        "actor_id": request.actor_id,
        "objective": request.objective,
        "signals": canonicalize(request.signals.model_dump(mode="json")),
        "budget": canonicalize(request.budget.model_dump(mode="json")),
        "context_summary": request.context_summary,
        "registry_version": request.registry_version,
    }
    return stable_hash(payload)


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
    """
    payload = {
        "request_hash": request_hash,
        "complexity": canonicalize(complexity.model_dump(mode="json")),
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
    def _verify_request_hash(self) -> "PlanDraft":
        expected = compute_request_hash(self.request)
        if not compare_digest(self.request_hash, expected):
            raise ValueError(
                f"request_hash mismatch: provided "
                f"{self.request_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    @model_validator(mode="after")
    def _auto_compute_and_verify_hash(self) -> "PlanDraft":
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
    def _tenant_homogeneity(self) -> "PlanDraft":
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

    def agent_tasks(self) -> list[AgentTask]:
        """Return the wrapped :class:`AgentTask` list for Phase 4+."""
        return [pt.task for pt in self.tasks]


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
    def _valid_implies_no_errors(self) -> "PlanValidationReport":
        if self.valid:
            for issue in self.issues:
                if issue.severity == "error":
                    raise ValueError(
                        f"valid=True but error-severity issue present: {issue.code!r}"
                    )
        return self


__all__ = [
    "PLANNER_VERSION",
    "PlanDraft",
    "PlanValidationIssue",
    "PlanValidationReport",
    "PlannedTask",
    "PlanningRequest",
    "PlanningSignals",
    "RequestedTask",
    "TaskIntent",
    "compute_plan_hash",
    "compute_request_hash",
    "effective_domains",
    "effective_task_types",
    "resolve_expected_intents",
    "task_intent_from_requested_task",
]
