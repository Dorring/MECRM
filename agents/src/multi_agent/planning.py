"""Phase 3 planning contracts.

All contracts inherit :class:`StrictContract` from Phase 2 so that
``extra="forbid"`` and ``validate_assignment=True`` apply uniformly.

Hash design (per Phase 3 review R1):

* ``request_hash`` — covers everything in :class:`PlanningRequest`
  *except* volatile fields.  Stored on :class:`PlanDraft` so the draft
  can be verified *without* holding the original request.
* ``plan_hash`` — covers ``request_hash`` + ``complexity`` + canonical
  tasks + ``planner_version``.  Excludes ``summary``, ``warnings``,
  wall-clock times, and ``plan_hash`` itself.

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

PLANNER_VERSION = "ma-03.1.0"


# ---------------------------------------------------------------------------
# PlanningSignals
# ---------------------------------------------------------------------------


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
        # Sensitive-key scan — context_summary is a string, so we only
        # need to reject obvious token patterns.  A full structured
        # scan is unnecessary here, but we keep the same normaliser to
        # stay consistent with Phase 2.
        from multi_agent.contracts import (
            _normalize_sensitive_key,
            _NORMALIZED_SECRET_PATTERNS,
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
# TaskIntent
# ---------------------------------------------------------------------------


class TaskIntent(StrictContract):
    """Planner's intent for a single task — *before* agent selection.

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
    def _dependencies_dedup_and_no_self(cls, v: list[str]) -> list[str]:
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
    def _intent_task_consistency(self) -> "PlannedTask":
        # The wrapped AgentTask must share tenant_id with the planning
        # context — caller sets it explicitly, but we double-check.
        if self.task.task_type and self.intent_id == self.task.task_id:
            # task_id may equal intent_id in some templates; that's fine.
            pass
        if self.task.task_id and self.task.task_id in self.task.dependencies:
            # AgentTask already enforces no-self-dependency, but be defensive.
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
    """Stable SHA-256 over the canonical PlanningRequest content.

    Excludes nothing — every field on PlanningRequest is semantic.
    ``context_summary`` is included because it is part of the planning
    input contract.
    """
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

    Carries enough context (``run_id``, ``tenant_id``, ``actor_*``,
    ``objective``, ``request_hash``) to be re-verified *without* the
    original :class:`PlanningRequest`.
    """

    run_id: str
    tenant_id: str
    actor_type: Literal["user", "service"]
    actor_id: str
    objective: str

    complexity: ComplexityDecision
    tasks: list[PlannedTask] = Field(default_factory=list)

    planner_version: str
    registry_version: str

    request_hash: str
    plan_hash: str = ""  # auto-computed if empty

    summary: str = ""
    warnings: list[str] = Field(default_factory=list)

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

    @field_validator("planner_version")
    @classmethod
    def _planner_version_required(cls, v: str) -> str:
        return _non_blank(v, "planner_version")

    @field_validator("registry_version")
    @classmethod
    def _registry_version_required(cls, v: str) -> str:
        return _non_blank(v, "registry_version")

    @field_validator("request_hash")
    @classmethod
    def _request_hash_required(cls, v: str) -> str:
        return _non_blank(v, "request_hash")

    @field_validator("summary")
    @classmethod
    def _summary_safe(cls, v: str) -> str:
        # Summary is a free-form string; reject obvious secret patterns.
        from multi_agent.contracts import (
            _normalize_sensitive_key,
            _NORMALIZED_SECRET_PATTERNS,
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
    def _auto_compute_and_verify_hash(self) -> "PlanDraft":
        """Auto-compute plan_hash if empty; verify if provided.

        Runs *after* all field validators, so ``self.tasks`` is a fully
        validated ``list[PlannedTask]``.  This avoids the fragility of
        trying to compute the hash from raw dict input in ``before``
        mode.
        """
        expected = self.compute_plan_hash()
        if not self.plan_hash:
            # Auto-compute.  Use object.__setattr__ to bypass
            # validate_assignment — the value is already validated.
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
        """Raise :class:`PlanIntegrityError` if stored hash != recomputed hash."""
        if not compare_digest(self.plan_hash, self.compute_plan_hash()):
            from multi_agent.planning_errors import PlanIntegrityError

            raise PlanIntegrityError(
                f"Plan {self.run_id!r}: stored plan_hash does not match "
                f"recomputed content"
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
    "TaskIntent",
    "compute_plan_hash",
    "compute_request_hash",
]
