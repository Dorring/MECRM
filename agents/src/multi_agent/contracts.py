"""Unified multi-agent data contracts.

All models are strict Pydantic BaseModels suitable for JSON serialisation,
with validators that enforce the Phase 2 rules defined in
docs/multi-agent/phase-2-contracts-registry.md.

Contracts are independent of LangGraph, Provider, and Kafka; they describe
*what* agents exchange, not *how* the exchange is transported.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# JSON value helper — matches the Phase 2 spec "JsonValue" type.
# ---------------------------------------------------------------------------

JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]

# ---------------------------------------------------------------------------
# Authority enums
# ---------------------------------------------------------------------------


class AgentAuthority(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    EXECUTE = "execute"


class ToolAuthority(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    EXECUTE = "execute"


# ---------------------------------------------------------------------------
# Stable agent-id format
# ---------------------------------------------------------------------------

_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")


def _is_stable_agent_id(value: str) -> bool:
    """Return True when *value* looks like a stable agent_id token."""
    return bool(_AGENT_ID_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# AgentCapability
# ---------------------------------------------------------------------------


class AgentCapability(BaseModel):
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
            raise ValueError(
                f"agent_id must match {_AGENT_ID_RE.pattern!r}; got {v!r}"
            )
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

    @model_validator(mode="after")
    def _validate_authority_tools(self) -> "AgentCapability":
        """Ensure authority level is consistent with allowed_tools."""
        tool_authorities = self._tool_authorities()
        if self.authority == AgentAuthority.READ:
            for tool in self.allowed_tools:
                if tool_authorities.get(tool) in (
                    ToolAuthority.PROPOSE,
                    ToolAuthority.EXECUTE,
                ):
                    raise ValueError(
                        f"READ agent {self.agent_id} cannot use "
                        f"{tool_authorities[tool].value}-level tool {tool!r}"
                    )
        elif self.authority == AgentAuthority.PROPOSE:
            for tool in self.allowed_tools:
                if tool_authorities.get(tool) == ToolAuthority.EXECUTE:
                    raise ValueError(
                        f"PROPOSE agent {self.agent_id} cannot use "
                        f"execute-level tool {tool!r}"
                    )
        return self

    @staticmethod
    def _tool_authorities() -> dict[str, ToolAuthority]:
        """Return the built-in tool→authority mapping.

        Extended in Phase 3+ when real tools are classified.  For now the
        registry uses this static map for validation.
        """
        return {
            # Chat read tools
            "crm_reader.get_leads": ToolAuthority.READ,
            "crm_reader.get_deals": ToolAuthority.READ,
            "crm_reader.get_tickets": ToolAuthority.READ,
            "crm_reader.get_customers": ToolAuthority.READ,
            "crm_reader.get_tasks": ToolAuthority.READ,
            "crm_reader.get_invoices": ToolAuthority.READ,
            "vector_search.search": ToolAuthority.READ,
            "search_adapter.search": ToolAuthority.READ,
            # Chat propose tools
            "crm_writer.propose": ToolAuthority.PROPOSE,
            # Executor tools (Phase 5+)
            "automation_executor.execute": ToolAuthority.EXECUTE,
            "kafka.emit_event": ToolAuthority.EXECUTE,
            "governance.approve": ToolAuthority.EXECUTE,
        }


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Provider-reported token consumption for a single agent invocation."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _total_sane(self) -> "TokenUsage":
        if self.total_tokens < 0:
            raise ValueError("total_tokens must be >= 0")
        return self


# ---------------------------------------------------------------------------
# ProviderMetadata
# ---------------------------------------------------------------------------


class ProviderMetadata(BaseModel):
    """Safe-to-log metadata about the AI provider used for a run."""

    provider: str
    chat_model: str
    embedding_model: str
    ai_mode: str
    remote: bool = False

    @field_validator("provider", "chat_model", "embedding_model", "ai_mode")
    @classmethod
    def _no_secrets(cls, v: str) -> str:
        low = v.lower()
        for secret in ("api_key", "apikey", "token", "password", "secret", "authorization"):
            if secret in low:
                raise ValueError(f"ProviderMetadata must not contain {secret!r}")
        return v


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

_EVIDENCE_TYPE_ALLOWLIST = frozenset(
    {
        "opa_policy",
        "llm_reasoning",
        "tool_output",
        "chain_of_thought",
        "human_approval",
        "audit_log",
        "kafka_topic",
        "event_id",
        "governance_decision",
        "data_guard_check",
    }
)


class Evidence(BaseModel):
    """A single piece of explainability / audit evidence."""

    evidence_id: str
    evidence_type: str
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

    @field_validator("evidence_type")
    @classmethod
    def _type_allowed(cls, v: str) -> str:
        if v not in _EVIDENCE_TYPE_ALLOWLIST:
            raise ValueError(
                f"evidence_type {v!r} not in allowlist"
            )
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v


# ---------------------------------------------------------------------------
# ToolCallRecord
# ---------------------------------------------------------------------------


class ToolCallRecord(BaseModel):
    """A bounded record of a tool invocation (no raw arguments or payloads)."""

    tool_name: str
    authority: ToolAuthority
    ok: bool
    duration_ms: float = Field(default=0.0, ge=0.0)
    error_code: str | None = None


# ---------------------------------------------------------------------------
# AgentError
# ---------------------------------------------------------------------------


class AgentError(BaseModel):
    """Structured error from an agent run."""

    error_code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] | None = None

    @field_validator("error_code")
    @classmethod
    def _error_code_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("error_code must not be empty")
        return v.strip()


# ---------------------------------------------------------------------------
# ActionProposal (unified)
# ---------------------------------------------------------------------------


def _compute_proposal_hash(
    *,
    tenant_id: str,
    created_by_agent: str,
    action_type: str,
    target_entity: str,
    target_id: str | None,
    payload: dict[str, Any],
    priority: str,
    justification: str | None,
    evidence_ids: list[str],
    requires_approval: bool,
    idempotency_key: str,
) -> str:
    """Compute a stable SHA-256 hash over proposal content.

    The hash deliberately EXCLUDES:
      - proposal_id (assigned at creation)
      - created_at (non-deterministic timestamp)
      - proposal_hash (the hash itself)

    The goal: two proposals with identical *semantic* content produce the
    same hash, enabling deduplication in state merge.
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
            "justification": justification,
            "evidence_ids": sorted(evidence_ids),
            "requires_approval": requires_approval,
            "idempotency_key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionProposal(BaseModel):
    """A proposed action from an agent; carries no side-effects.

    An ActionProposal is an *intent to act*, not the act itself.  Only a
    GovernedExecutor (Phase 5+) may convert a proposal into a real write.
    """

    proposal_id: str
    proposal_hash: str
    tenant_id: str
    created_by_agent: str
    action_type: str
    target_entity: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Literal["low", "medium", "high"] = "medium"
    justification: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = True
    idempotency_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ActionProposal must have a tenant_id")
        return v.strip()

    @field_validator("payload")
    @classmethod
    def _no_tenant_override(cls, v: dict[str, Any]) -> dict[str, Any]:
        low = {k.lower() for k in v}
        if "tenant_id" in low or "tenantid" in low:
            raise ValueError(
                "ActionProposal payload must not contain tenant_id override"
            )
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _high_risk_needs_evidence(self) -> "ActionProposal":
        if self.priority == "high" and not self.evidence_ids:
            raise ValueError(
                "high-priority ActionProposal must provide at least one evidence_id"
            )
        return self


# ---------------------------------------------------------------------------
# AgentTask
# ---------------------------------------------------------------------------


class AgentTask(BaseModel):
    """A unit of work dispatched to an agent.

    AgentTask is the *request* side of the contract; :class:`AgentResult` is
    the *response* side.
    """

    task_id: str
    agent_id: str
    task_type: str
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = (
        "pending"
    )
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

    @model_validator(mode="after")
    def _deps_deduped(self) -> "AgentTask":
        # Pydantic set handles dedup, but validate round-trip consistency
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must be deduplicated")
        return self


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------


class AgentResult(BaseModel):
    """The output of a single agent run — success or failure.

    Every AgentResult references the originating :class:`AgentTask` via
    ``task_id`` and carries the evidence / proposals / token usage produced
    during the run.
    """

    result_id: str
    task_id: str
    agent_id: str
    tenant_id: str
    status: Literal["completed", "failed", "degraded", "cancelled"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    output: dict[str, Any] | None = None
    error: AgentError | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    action_proposals: list[ActionProposal] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    provider_metadata: ProviderMetadata | None = None
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

    @field_validator("completed_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _failed_requires_error(self) -> "AgentResult":
        if self.status == "failed" and self.error is None:
            raise ValueError("status 'failed' requires an AgentError")
        return self

    @model_validator(mode="after")
    def _completed_no_fatal_error(self) -> "AgentResult":
        if self.status == "completed" and self.error is not None:
            raise ValueError("status 'completed' must not have a fatal AgentError")
        return self


# ---------------------------------------------------------------------------
# AgentExecutionContext
# ---------------------------------------------------------------------------


class AgentExecutionContext(BaseModel):
    """Context passed to every agent invocation."""

    tenant_id: str
    user_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    authorization: str | None = None
    correlation_id: str | None = None
    parent_task_id: str | None = None
    run_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("tenant_id")
    @classmethod
    def _tenant_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("AgentExecutionContext must have a tenant_id")
        return v.strip()


# ---------------------------------------------------------------------------
# Adapters — map existing ActionProposal variants into the unified contract.
# These are READ-ONLY; they do NOT modify the source dataclasses.
# ---------------------------------------------------------------------------


def from_crm_writer_proposal(
    proposal: Any,
    *,
    tenant_id: str,
    agent_id: str,
) -> ActionProposal:
    """Adapt the simple dataclass from ``chat/tools/crm_writer.py``.

    The original has: proposal_id, entity, operation, payload,
    requires_approval, created_at.
    """
    evidence_ids: list[str] = []
    justification: str | None = None
    priority: Literal["low", "medium", "high"] = "medium"

    proposal_hash = _compute_proposal_hash(
        tenant_id=tenant_id,
        created_by_agent=agent_id,
        action_type=proposal.operation,
        target_entity=proposal.entity,
        target_id=None,
        payload=proposal.payload,
        priority=priority,
        justification=justification,
        evidence_ids=evidence_ids,
        requires_approval=proposal.requires_approval,
        idempotency_key=proposal.proposal_id,
    )

    return ActionProposal(
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal_hash,
        tenant_id=tenant_id,
        created_by_agent=agent_id,
        action_type=proposal.operation,
        target_entity=proposal.entity,
        payload=proposal.payload,
        priority=priority,
        justification=justification,
        evidence_ids=evidence_ids,
        requires_approval=proposal.requires_approval,
        idempotency_key=proposal.proposal_id,
        created_at=_parse_iso_utc(proposal.created_at),
    )


def from_productivity_proposal(
    proposal: Any,
) -> ActionProposal:
    """Adapt the frozen dataclass from ``productivity/proposals.py``.

    The original has: proposal_id, tenant_id, user_id, action_type,
    target_entity, target_id, priority, justification, drafts, created_at,
    dedupe_key, signal_type, signal.
    """
    payload: dict[str, Any] = {
        "user_id": proposal.user_id,
        "drafts": proposal.drafts,
        "signal_type": proposal.signal_type,
        "signal": proposal.signal,
    }

    evidence_ids: list[str] = []
    if proposal.priority == "high":
        evidence_ids = [f"productivity:signal:{proposal.signal_type}"]

    proposal_hash = _compute_proposal_hash(
        tenant_id=proposal.tenant_id,
        created_by_agent="productivity_agent",
        action_type=proposal.action_type,
        target_entity=proposal.target_entity,
        target_id=proposal.target_id,
        payload=payload,
        priority=proposal.priority,
        justification=proposal.justification,
        evidence_ids=evidence_ids,
        requires_approval=True,
        idempotency_key=proposal.dedupe_key,
    )

    return ActionProposal(
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal_hash,
        tenant_id=proposal.tenant_id,
        created_by_agent="productivity_agent",
        action_type=proposal.action_type,
        target_entity=proposal.target_entity,
        target_id=proposal.target_id,
        payload=payload,
        priority=proposal.priority,
        justification=proposal.justification,
        evidence_ids=evidence_ids,
        requires_approval=True,
        idempotency_key=proposal.dedupe_key,
        created_at=_parse_iso_utc(proposal.created_at),
    )


def _parse_iso_utc(raw: str) -> datetime:
    """Best-effort parse an ISO-8601 string into a UTC datetime."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
