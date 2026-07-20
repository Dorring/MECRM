"""Unified multi-agent data contracts — Phase 2 R2.

Every contract inherits :class:`StrictContract` which forbids unknown fields
and enables ``validate_assignment``.  Contracts describe *what* agents
exchange; they are independent of LangGraph, Kafka, and provider internals.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# JSON value helper
# ---------------------------------------------------------------------------

JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]

# ---------------------------------------------------------------------------
# Strict base contract — all Phase 2 models inherit this
# ---------------------------------------------------------------------------


class StrictContract(BaseModel):
    """Base for all multi-agent contracts.

    - ``extra="forbid"`` → unknown fields cause a ValidationError, not silent ignore.
    - ``validate_assignment=True`` → setting an invalid value after construction also raises.
    """

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
    """Business-domain evidence types — explicitly excludes chain-of-thought / raw LLM internals."""

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


# -- AgentCapability ---------------------------------------------------------


class AgentCapability(StrictContract):
    """Declares what an agent can do, its authority level, and cost profile."""

    agent_id: str
    version: str
    description: str
    domains: set[str]
    supported_tasks: set[str]
    allowed_tools: set[str]
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

    # NOTE: Tool-authority validation is no longer on AgentCapability itself;
    # the AgentRegistry validates against the injected ToolCatalog at
    # registration time.  This avoids a hidden static map in the model.


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
    source_id: str | None = None
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, JsonValue] | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Evidence must have a tenant_id")
        return v.strip()

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v


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


# ============================================================================
# ACTION PROPOSAL
# ============================================================================


def _compute_proposal_hash(
    *,
    tenant_id: str,
    created_by_agent: str,
    action_type: str,
    target_entity: str,
    target_id: str | None,
    payload: dict[str, Any],
    priority: str,
    risk_level: str,
    justification: str | None,
    evidence_ids: list[str],
    requires_approval: bool,
) -> str:
    """Compute a stable SHA-256 digest over canonical proposal content.

    Excluded:
      - proposal_id / proposal_hash (self-referential / assigned at creation)
      - created_at (wall-clock, non-deterministic)
      - idempotency_key (identity key, not content)
    """
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "created_by_agent": created_by_agent,
            "action_type": action_type,
            "target_entity": target_entity,
            "target_id": target_id,
            "payload": payload,
            "priority": priority,
            "risk_level": risk_level,
            "justification": justification,
            "evidence_ids": sorted(evidence_ids),
            "requires_approval": requires_approval,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scan_payload_for_tenant_override(
    payload: dict[str, Any], path: str = "payload"
) -> None:
    """Recursively scan *payload* for ``tenant_id`` / ``tenantId`` keys."""
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
    proposal_hash: str = ""
    tenant_id: str
    created_by_agent: str
    action_type: str
    target_entity: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
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
    def _no_tenant_override(cls, v: dict[str, Any]) -> dict[str, Any]:
        _scan_payload_for_tenant_override(v)
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v

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

    @model_validator(mode="after")
    def _verify_hash(self) -> "ActionProposal":
        """If proposal_hash is non-empty, it MUST match the computed hash."""
        if self.proposal_hash:
            expected = self.compute_hash()
            if self.proposal_hash != expected:
                raise ValueError(
                    f"proposal_hash mismatch: provided {self.proposal_hash[:12]!r} "
                    f"!= computed {expected[:12]!r}"
                )
        return self

    def compute_hash(self) -> str:
        return _compute_proposal_hash(
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

    @classmethod
    def create(cls, **data: Any) -> "ActionProposal":
        """Factory: create an ActionProposal and auto-compute its hash.

        Use this instead of the raw constructor whenever you are *creating* a
        new proposal (as opposed to deserializing one from a trusted source
        that already carries a verified hash).
        """
        data.pop("proposal_hash", None)
        instance = cls.model_validate({**data, "proposal_hash": ""})
        h = instance.compute_hash()
        return cls.model_validate({**data, "proposal_hash": h})


# ============================================================================
# AGENT TASK
# ============================================================================


class AgentTask(StrictContract):
    task_id: str
    agent_id: str
    task_type: str
    objective: str = ""
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
    input_data: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str
    user_id: str | None = None
    dependencies: set[str] = Field(default_factory=set)
    timeout_ms: int = 300_000
    max_retries: int = 0
    idempotency_key: str = ""
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentTask must have a tenant_id")
        return v.strip()

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
    status: Literal["completed", "failed", "degraded", "cancelled"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    summary: str = ""
    output: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
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
    def _utc_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
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
        """Every embedded Evidence and ActionProposal must share the result's tenant_id."""
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
                    f"ActionProposal {p.proposal_id!r} created_by_agent {p.created_by_agent!r} "
                    f"!= result agent_id {self.agent_id!r}"
                )
        return self


# ============================================================================
# AGENT EXECUTION CONTEXT  (safe — no raw authorization)
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
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentExecutionContext must have a tenant_id")
        return v.strip()


# ============================================================================
# PHASE 2 STATE MODELS
# ============================================================================


class ComplexityDecision(StrictContract):
    """Decision from the Complexity Gate (Phase 3).  Defined now so contracts
    are stable."""

    complexity: Literal["simple", "single_agent", "multi_agent_supervised"]
    reason: str
    recommended_agents: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


class ExecutionBudget(StrictContract):
    """Budget constraints for a multi-agent run."""

    max_agents: int = Field(default=8, ge=1)
    max_iterations: int = Field(default=10, ge=1)
    max_cost: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_timeout_ms: int = Field(default=300_000, ge=1)
    total_cost: Decimal = Field(default=Decimal("0.00"), ge=0)
    agent_calls: int = Field(default=0, ge=0)
    iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _cost_not_exceeded(self) -> "ExecutionBudget":
        if self.total_cost > self.max_cost > 0:
            raise ValueError("total_cost exceeds max_cost")
        return self


RunStatus = Literal[
    "idle",
    "planning",
    "dispatching",
    "executing",
    "merging",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
]


class MultiAgentState(StrictContract):
    """Canonical multi-agent run state for Phase 2+."""

    run_id: str
    tenant_id: str
    status: RunStatus = "idle"
    tasks: list[AgentTask] = Field(default_factory=list)
    results: list[AgentResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    proposals: list[ActionProposal] = Field(default_factory=list)
    complexity: ComplexityDecision | None = None
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    current_iteration: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("MultiAgentState must have a tenant_id")
        return v.strip()

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC)")
        return v


# ============================================================================
# ADAPTERS
# ============================================================================


def from_crm_writer_proposal(
    proposal: Any,
    *,
    tenant_id: str,
    agent_id: str,
) -> ActionProposal:
    evidence_ids: list[str] = []
    justification: str | None = None

    return ActionProposal.create(
        proposal_id=proposal.proposal_id,
        tenant_id=tenant_id,
        created_by_agent=agent_id,
        action_type=proposal.operation,
        target_entity=proposal.entity,
        payload=proposal.payload,
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        justification=justification,
        evidence_ids=evidence_ids,
        requires_approval=proposal.requires_approval,
        idempotency_key=proposal.proposal_id,
        created_at=_parse_iso_utc(proposal.created_at),
    )


def from_productivity_proposal(
    proposal: Any,
    *,
    evidence_ids: list[str] | None = None,
) -> ActionProposal:
    payload: dict[str, Any] = {
        "user_id": proposal.user_id,
        "drafts": proposal.drafts,
        "signal_type": proposal.signal_type,
        "signal": proposal.signal,
    }

    if evidence_ids is None:
        # Productivity proposals must have evidence provided by the caller;
        # we no longer fabricate them.
        if proposal.priority == "high":
            raise ValueError(
                "high-priority productivity proposal requires evidence_ids; "
                "pass them explicitly"
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
        risk_level=ActionRiskLevel.HIGH
        if proposal.priority == "high"
        else ActionRiskLevel.MEDIUM,
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
