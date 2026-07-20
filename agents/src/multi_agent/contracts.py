"""Unified multi-agent data contracts — Phase 2 R5.

Every contract inherits :class:`StrictContract`.  JSON fields are validated
through :func:`validate_strict_json` at the Pydantic boundary — bytes, sets,
tuples, Decimal, NaN, and non-string dict keys are rejected at construction.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hmac import compare_digest
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from multi_agent.errors import ProposalHashMismatchError
from multi_agent.integrity import compute_proposal_hash
from multi_agent.serialization import validate_strict_json

JsonValue = Any

# Sensitive keys rejected from all metadata fields.
# Patterns are pre-normalized at module load so that the comparison is
# normalization-consistent: both the key under test and the pattern go
# through the same ``_normalize_sensitive_key`` pipeline.
_SENSITIVE_KEY_PATTERNS = frozenset(
    {
        "authorization",
        "api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
        "cookie",
    }
)


def _normalize_sensitive_key(value: str) -> str:
    """Normalize a key for sensitive-pattern comparison.

    Strips every non-alphanumeric character (underscores, hyphens, spaces,
    dots, …) and lowercases.  This makes ``access_token``, ``access-token``,
    ``ACCESS_TOKEN`` and ``access token`` all collapse to ``accesstoken``.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


_NORMALIZED_SECRET_PATTERNS = frozenset(
    _normalize_sensitive_key(pattern) for pattern in _SENSITIVE_KEY_PATTERNS
)


def _reject_sensitive_keys(value: JsonValue, path: str) -> None:
    """Recursively reject sensitive keys in dicts and lists.

    Scans both nested dictionaries and lists so that
    ``{"providers": [{"access_token": "..."}]}`` is caught.
    """
    if isinstance(value, dict):
        for k, child in value.items():
            normalized = _normalize_sensitive_key(str(k))
            if any(pattern in normalized for pattern in _NORMALIZED_SECRET_PATTERNS):
                raise ValueError(f"{path} contains sensitive key {k!r}")
            _reject_sensitive_keys(child, f"{path}.{k}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# Strict base contract
# ---------------------------------------------------------------------------


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentAuthority(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    EXECUTE = "execute"


class ToolAuthority(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    EXECUTE = "execute"


class ActionRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceType(str, Enum):
    CUSTOMER = "customer"
    CONTACT = "contact"
    TICKET = "ticket"
    DEAL = "deal"
    KNOWLEDGE_ARTICLE = "knowledge_article"
    METRIC = "metric"
    TOOL_RESULT = "tool_result"
    AUDIT_EVENT = "audit_event"
    POLICY_DECISION = "policy_decision"
    HUMAN_APPROVAL = "human_approval"
    OPA_POLICY = "opa_policy"
    KAFKA_TOPIC = "kafka_topic"
    EVENT_ID = "event_id"
    GOVERNANCE_DECISION = "governance_decision"
    DATA_GUARD_CHECK = "data_guard_check"


class AgentErrorCategory(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    TENANT = "tenant"
    TOOL = "tool"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Stable agent-id format
# ---------------------------------------------------------------------------

_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _is_stable_agent_id(value: str) -> bool:
    return bool(_AGENT_ID_RE.fullmatch(value))


# ============================================================================
# CONTRACTS
# ============================================================================


# -- AgentCapability (frozen) ------------------------------------------------


class AgentCapability(StrictContract):
    """Immutable declaration of what an agent can do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    version: str
    description: str
    domains: frozenset[str]
    supported_tasks: frozenset[str]
    allowed_tools: frozenset[str]
    authority: AgentAuthority
    input_contract: str
    output_contract: str
    timeout_ms: int
    max_retries: int
    estimated_cost_class: Literal["low", "medium", "high"]
    enabled: bool = True
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def _agent_id_stable(cls, v: str) -> str:
        if not _is_stable_agent_id(v):
            raise ValueError(f"agent_id must match {_AGENT_ID_RE.pattern!r}; got {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _version_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("version must not be empty")
        return v.strip()

    @field_validator("metadata")
    @classmethod
    def _validate_cap_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "AgentCapability.metadata")
        return validate_strict_json(v)  # type: ignore[return-value]

    @field_validator("timeout_ms")
    @classmethod
    def _timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_ms must be > 0")
        return v

    @field_validator("max_retries")
    @classmethod
    def _retries_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries must be >= 0")
        return v


# -- TokenUsage --------------------------------------------------------------


class TokenUsage(StrictContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _total_sane(self) -> "TokenUsage":
        if self.total_tokens < 0:
            raise ValueError("total_tokens must be >= 0")
        return self


# -- ProviderMetadata --------------------------------------------------------


class ProviderMetadata(StrictContract):
    provider: str
    chat_model: str
    embedding_model: str
    ai_mode: str
    remote: bool = False


# -- Evidence ----------------------------------------------------------------


class Evidence(StrictContract):
    evidence_id: str
    evidence_type: EvidenceType
    tenant_id: str
    source_agent: str
    summary: str = ""
    source_id: str | None = None
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retrieved_at: datetime | None = None
    metadata: dict[str, JsonValue] | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Evidence must have a tenant_id")
        return v.strip()

    @field_validator("created_at", "retrieved_at")
    @classmethod
    def _utc_aware_evidence(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return v

    @field_validator("metadata")
    @classmethod
    def _validate_evidence_metadata(
        cls, v: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if v is not None:
            _reject_sensitive_keys(v, "Evidence.metadata")
            validate_strict_json(v)
        return v


# -- ToolDescriptor (frozen) -------------------------------------------------


class ToolDescriptor(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    authority: ToolAuthority
    description: str = ""
    input_contract: str = ""
    output_contract: str = ""


# -- ToolCallRecord ----------------------------------------------------------


class ToolCallRecord(StrictContract):
    tool_name: str
    authority: ToolAuthority
    ok: bool = True
    duration_ms: float = Field(default=0.0, ge=0.0)
    error_code: str | None = None


# -- AgentError --------------------------------------------------------------


class AgentError(StrictContract):
    error_code: str
    message: str
    category: AgentErrorCategory = AgentErrorCategory.UNKNOWN
    retryable: bool = False
    details: dict[str, JsonValue] | None = None

    @field_validator("error_code")
    @classmethod
    def _error_code_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("error_code must not be empty")
        return v.strip()

    @field_validator("details")
    @classmethod
    def _validate_agent_error_details(
        cls, v: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if v is not None:
            _reject_sensitive_keys(v, "AgentError.details")
            validate_strict_json(v)
        return v


# ============================================================================
# ACTION PROPOSAL
# ============================================================================


def _scan_payload_for_tenant_override(
    payload: dict[str, Any], path: str = "payload"
) -> None:
    for k, v in payload.items():
        if k.lower() in ("tenant_id", "tenantid"):
            raise ValueError(
                f"ActionProposal.{path} must not contain tenant_id override; found key {k!r}"
            )
        if isinstance(v, dict):
            _scan_payload_for_tenant_override(v, f"{path}.{k}")
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    _scan_payload_for_tenant_override(item, f"{path}.{k}[{i}]")


class ActionProposal(StrictContract):
    proposal_id: str
    proposal_hash: str = ""  # auto-computed if empty
    tenant_id: str
    created_by_agent: str
    action_type: str
    target_entity: str
    target_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    risk_level: ActionRiskLevel = ActionRiskLevel.MEDIUM
    justification: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = True
    idempotency_key: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- validators ----------------------------------------------------------

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ActionProposal must have a tenant_id")
        return v.strip()

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, v: dict[str, Any]) -> dict[str, Any]:
        _scan_payload_for_tenant_override(v)
        validate_strict_json(v)  # reject non-JSON types
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="before")
    @classmethod
    def _populate_hash(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("proposal_hash"):
                data = dict(data)
                data["proposal_hash"] = cls._compute_hash_from_data(data)
        return data

    @model_validator(mode="after")
    def _verify_hash(self) -> "ActionProposal":
        expected = self.compute_hash()
        if self.proposal_hash and not compare_digest(self.proposal_hash, expected):
            raise ValueError(
                f"proposal_hash mismatch: provided "
                f"{self.proposal_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    @model_validator(mode="after")
    def _high_risk_requires_evidence(self) -> "ActionProposal":
        if self.risk_level == ActionRiskLevel.HIGH:
            if not self.evidence_ids:
                raise ValueError(
                    "high-risk ActionProposal must provide at least one evidence_id"
                )
            if not self.requires_approval:
                raise ValueError("high-risk ActionProposal must require approval")
        return self

    # -- public methods ------------------------------------------------------

    def compute_hash(self) -> str:
        return compute_proposal_hash(
            tenant_id=self.tenant_id,
            created_by_agent=self.created_by_agent,
            action_type=self.action_type,
            target_entity=self.target_entity,
            target_id=self.target_id,
            payload=self.payload,
            priority=self.priority,
            risk_level=self.risk_level.value,
            justification=self.justification,
            evidence_ids=self.evidence_ids,
            requires_approval=self.requires_approval,
        )

    def verify_integrity(self) -> None:
        if not compare_digest(self.proposal_hash, self.compute_hash()):
            raise ProposalHashMismatchError(
                f"Proposal {self.proposal_id!r}: stored hash does not match recomputed content"
            )
        _scan_payload_for_tenant_override(self.payload)
        if self.risk_level == ActionRiskLevel.HIGH:
            if not self.evidence_ids:
                raise ValueError("high-risk proposal missing evidence_ids")
            if not self.requires_approval:
                raise ValueError("high-risk proposal must require approval")

    @classmethod
    def create(cls, **data: Any) -> "ActionProposal":
        data.pop("proposal_hash", None)
        return cls.model_validate(data)

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _compute_hash_from_data(data: dict[str, Any]) -> str:
        try:
            return compute_proposal_hash(
                tenant_id=data.get("tenant_id", ""),
                created_by_agent=data.get("created_by_agent", ""),
                action_type=data.get("action_type", ""),
                target_entity=data.get("target_entity", ""),
                target_id=data.get("target_id"),
                payload=data.get("payload", {}),
                priority=data.get("priority", "medium"),
                risk_level=data.get("risk_level", "medium")
                if isinstance(data.get("risk_level"), str)
                else data.get("risk_level", "medium"),
                justification=data.get("justification"),
                evidence_ids=data.get("evidence_ids", []),
                requires_approval=data.get("requires_approval", True),
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"Cannot compute proposal hash: {e}") from e


# ============================================================================
# AGENT TASK
# ============================================================================


class AgentTask(StrictContract):
    task_id: str
    agent_id: str
    task_type: str
    objective: str = Field(min_length=1)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    status: Literal[
        "pending",
        "running",
        "ready",
        "degraded",
        "skipped",
        "needs_input",
        "completed",
        "failed",
        "cancelled",
    ] = "pending"
    required_evidence: list[str] = Field(default_factory=list)
    required: bool = True
    input_data: dict[str, JsonValue] = Field(default_factory=dict)
    tenant_id: str
    user_id: str | None = None
    dependencies: frozenset[str] = frozenset()
    timeout_ms: int = 300_000
    max_retries: int = 0
    idempotency_key: str = ""
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required_task(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentTask must have a tenant_id")
        return v.strip()

    @field_validator("input_data")
    @classmethod
    def _validate_input_data(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_strict_json(v)  # type: ignore[return-value]

    @field_validator("objective")
    @classmethod
    def _objective_non_blank_task(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("objective must not be blank")
        return v

    @field_validator("timeout_ms")
    @classmethod
    def _timeout_positive_task(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_ms must be > 0")
        return v

    @field_validator("max_retries")
    @classmethod
    def _retries_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries must be >= 0")
        return v

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def _utc_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "AgentTask":
        if self.task_id and self.task_id in self.dependencies:
            raise ValueError("AgentTask cannot depend on itself")
        return self


# ============================================================================
# AGENT RESULT
# ============================================================================


class AgentResult(StrictContract):
    result_id: str
    task_id: str
    agent_id: str
    agent_version: str = ""
    tenant_id: str
    status: Literal[
        "completed", "failed", "degraded", "cancelled", "needs_input", "skipped"
    ]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    summary: str = ""
    output: dict[str, JsonValue] | None = None
    findings: list[dict[str, JsonValue]] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    action_proposals: list[ActionProposal] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    provider_metadata: ProviderMetadata | None = None
    started_at: datetime | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentResult must have a tenant_id")
        return v.strip()

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_aware_result(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v

    @field_validator("output")
    @classmethod
    def _validate_output(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            validate_strict_json(v)
        return v

    @field_validator("findings")
    @classmethod
    def _validate_findings(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        validate_strict_json(v)
        return v

    @model_validator(mode="after")
    def _failed_requires_errors(self) -> "AgentResult":
        if self.status == "failed" and not self.errors:
            raise ValueError(
                "status 'failed' requires at least one AgentError in errors list"
            )
        return self

    @model_validator(mode="after")
    def _completed_no_errors(self) -> "AgentResult":
        if self.status == "completed" and self.errors:
            raise ValueError("status 'completed' must not have errors")
        return self

    @model_validator(mode="after")
    def _tenant_homogeneity(self) -> "AgentResult":
        for ev in self.evidence:
            if ev.tenant_id != self.tenant_id:
                raise ValueError(
                    f"Evidence {ev.evidence_id!r} tenant_id {ev.tenant_id!r} "
                    f"!= result tenant_id {self.tenant_id!r}"
                )
        for p in self.action_proposals:
            if p.tenant_id != self.tenant_id:
                raise ValueError(
                    f"ActionProposal {p.proposal_id!r} tenant_id {p.tenant_id!r} "
                    f"!= result tenant_id {self.tenant_id!r}"
                )
            if p.created_by_agent != self.agent_id:
                raise ValueError(
                    f"ActionProposal {p.proposal_id!r} created_by_agent "
                    f"{p.created_by_agent!r} != result agent_id {self.agent_id!r}"
                )
        # Verify proposal evidence_ids reference actual evidence
        known_evidence_ids = {ev.evidence_id for ev in self.evidence}
        for p in self.action_proposals:
            missing = [eid for eid in p.evidence_ids if eid not in known_evidence_ids]
            if missing:
                raise ValueError(
                    f"ActionProposal {p.proposal_id!r} references unknown evidence: {missing}"
                )
        return self


# ============================================================================
# AGENT EXECUTION CONTEXT
# ============================================================================


class AgentExecutionContext(StrictContract):
    tenant_id: str
    user_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    policy_context: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_id: str | None = None
    parent_task_id: str | None = None
    run_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required_ctx(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentExecutionContext must have a tenant_id")
        return v.strip()

    @field_validator("policy_context")
    @classmethod
    def _validate_policy_context(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "AgentExecutionContext.policy_context")
        return validate_strict_json(v)  # type: ignore[return-value]

    @field_validator("run_metadata")
    @classmethod
    def _validate_run_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "AgentExecutionContext.run_metadata")
        return validate_strict_json(v)  # type: ignore[return-value]


# ============================================================================
# PHASE 2 STATE MODELS
# ============================================================================


class ComplexityDecision(StrictContract):
    route: Literal["deterministic_workflow", "single_agent", "multi_agent"]
    domains: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    requires_human_review: bool = False


class ExecutionBudget(StrictContract):
    max_tasks: int = Field(default=16, ge=1)
    max_agent_calls: int = Field(default=128, ge=1)
    max_tool_calls: int = Field(default=512, ge=1)
    max_iterations: int = Field(default=10, ge=1)
    token_budget: int | None = Field(default=None, gt=0)
    cost_budget_usd: Decimal | None = Field(default=None, ge=0)
    deadline_ms: int = Field(default=300_000, ge=1)

    @model_validator(mode="after")
    def _positive_deadline(self) -> "ExecutionBudget":
        if self.deadline_ms <= 0:
            raise ValueError("deadline_ms must be > 0")
        return self


class ExecutionUsage(StrictContract):
    tasks_dispatched: int = Field(default=0, ge=0)
    agent_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    iterations: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0.00"), ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


RunStatus = Literal[
    "idle",
    "planning",
    "dispatching",
    "executing",
    "reviewing",
    "merging",
    "awaiting_approval",
    "degraded",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
]


class MultiAgentState(StrictContract):
    run_id: str
    tenant_id: str
    actor_type: Literal["user", "service"] = "user"
    actor_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    status: RunStatus = "idle"
    plan: list[AgentTask] = Field(default_factory=list)
    agent_results: list[AgentResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    proposed_actions: list[ActionProposal] = Field(default_factory=list)
    complexity: ComplexityDecision | None = None
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)
    current_iteration: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required_state(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("MultiAgentState must have a tenant_id")
        return v.strip()

    @field_validator("actor_id", "objective")
    @classmethod
    def _non_blank_state(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("actor_id and objective must not be blank")
        return v

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _tenant_homogeneity_and_integrity(self) -> "MultiAgentState":
        for task in self.plan:
            if task.tenant_id != self.tenant_id:
                raise ValueError(
                    f"Task {task.task_id!r} tenant {task.tenant_id!r} "
                    f"!= state tenant {self.tenant_id!r}"
                )
        for r in self.agent_results:
            if r.tenant_id != self.tenant_id:
                raise ValueError(
                    f"Result {r.result_id!r} tenant {r.tenant_id!r} "
                    f"!= state tenant {self.tenant_id!r}"
                )
        for ev in self.evidence:
            if ev.tenant_id != self.tenant_id:
                raise ValueError(
                    f"Evidence {ev.evidence_id!r} tenant {ev.tenant_id!r} "
                    f"!= state tenant {self.tenant_id!r}"
                )
        # Build the set of evidence IDs available to state-level proposals.
        # Sources: state.evidence + every result.evidence.
        available_evidence_ids = {ev.evidence_id for ev in self.evidence}
        for r in self.agent_results:
            available_evidence_ids.update(
                evidence.evidence_id for evidence in r.evidence
            )
        for p in self.proposed_actions:
            if p.tenant_id != self.tenant_id:
                raise ValueError(
                    f"Proposal {p.proposal_id!r} tenant {p.tenant_id!r} "
                    f"!= state tenant {self.tenant_id!r}"
                )
            missing = sorted(set(p.evidence_ids) - available_evidence_ids)
            if missing:
                raise ValueError(
                    f"ActionProposal {p.proposal_id!r} references missing "
                    f"evidence: {missing}"
                )
            p.verify_integrity()
        return self


# ============================================================================
# ADAPTERS
# ============================================================================


def from_crm_writer_proposal(
    proposal: Any,
    *,
    tenant_id: str,
    agent_id: str,
) -> ActionProposal:
    return ActionProposal.create(
        proposal_id=proposal.proposal_id,
        tenant_id=tenant_id,
        created_by_agent=agent_id,
        action_type=proposal.operation,
        target_entity=proposal.entity,
        payload=proposal.payload,
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        evidence_ids=[],
        requires_approval=proposal.requires_approval,
        idempotency_key=proposal.proposal_id,
        created_at=_parse_iso_utc(proposal.created_at),
    )


def from_productivity_proposal(
    proposal: Any,
    *,
    evidence_ids: list[str] | None = None,
) -> ActionProposal:
    payload: dict[str, JsonValue] = {
        "user_id": proposal.user_id,
        "drafts": proposal.drafts,
        "signal_type": proposal.signal_type,
        "signal": proposal.signal,
    }

    if evidence_ids is None:
        if proposal.priority == "high":
            raise ValueError(
                "high-priority productivity proposal requires evidence_ids; pass them explicitly"
            )
        evidence_ids = []

    return ActionProposal.create(
        proposal_id=proposal.proposal_id,
        tenant_id=proposal.tenant_id,
        created_by_agent="productivity_agent",
        action_type=proposal.action_type,
        target_entity=proposal.target_entity,
        target_id=proposal.target_id,
        payload=payload,
        priority=proposal.priority,
        risk_level=(
            ActionRiskLevel.HIGH
            if proposal.priority == "high"
            else ActionRiskLevel.MEDIUM
        ),
        justification=proposal.justification,
        evidence_ids=evidence_ids,
        requires_approval=True,
        idempotency_key=proposal.dedupe_key,
        created_at=_parse_iso_utc(proposal.created_at),
    )


def _parse_iso_utc(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
