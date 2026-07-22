"""Phase 5A Review Contracts.

Strict, deterministically-hashable contracts for the Reviewer &
Governance Decision Layer.

Design rules (Phase 5A Section 5):

* Every public contract inherits :class:`StrictContract`
  (``extra="forbid"``, ``validate_assignment=True``).
* Frozen contracts (``ReviewFinding``, ``ProposalReview``,
  ``ReviewBatchResult``, ``ReviewRequest``) cannot be mutated after
  construction — audit records must be immutable.
* Stable serialization via :func:`stable_hash` (SHA-256 over the
  canonicalized form).  The same input MUST produce the same hash
  across processes, ``PYTHONHASHSEED`` values, and call order.
* No ``Any`` type in field annotations; ``details`` uses
  ``dict[str, JsonValue]`` which is the existing project pattern
  (see :class:`AgentError.details`, :class:`AgentCapability.metadata`).
* No Handler / Callable / non-serialisable object is stored.

Phase 5A Section 3 reminder: ``approved`` means "Proposal has passed
review".  It NEVER means "Proposal has been executed".
"""

from __future__ import annotations

from enum import StrEnum
from hmac import compare_digest
from typing import Any

from pydantic import ConfigDict, Field, field_validator, model_validator

from multi_agent.contracts import (
    ActionProposal,
    AgentCapability,
    Evidence,
    JsonValue,
    StrictContract,
)
from multi_agent.review_errors import ReviewIntegrityError
from multi_agent.serialization import canonicalize, stable_hash


# ---------------------------------------------------------------------------
# Version — bumped whenever the Reviewer algorithm changes.
# ---------------------------------------------------------------------------

REVIEWER_VERSION = "ma-05a.1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReviewDecisionStatus(StrEnum):
    """Final decision for a single Proposal.

    Ordering matters for :func:`batch_status_priority` — see
    :class:`ReviewBatchResult.batch_status`.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    CONFLICT = "conflict"


class ReviewFindingSeverity(StrEnum):
    """Severity for :class:`ReviewFinding`.

    ``ERROR`` and ``CRITICAL`` findings always force a non-approved
    decision; ``WARNING`` is informational and may still yield
    ``approved``; ``INFO`` is purely descriptive.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ReviewBatchStatus(StrEnum):
    """Highest-priority decision across all Proposals in a batch.

    Priority (highest first): ``conflict`` > ``rejected`` >
    ``needs_input`` > ``needs_approval`` > ``approved``.
    """

    APPROVED = "approved"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    REJECTED = "rejected"
    CONFLICT = "conflict"


# ---------------------------------------------------------------------------
# Risk & Approval classification (Phase 5A Section 9)
# ---------------------------------------------------------------------------


class ReviewRiskLevel(StrEnum):
    """Reviewer-side risk classification.

    Distinct from :class:`ActionRiskLevel` (which is the Agent's
    self-declared risk on the Proposal).  The Reviewer recomputes a
    canonical risk level from ``action_type`` / ``target_entity`` /
    ``payload`` so a misbehaving Agent cannot lower the approval bar
    by declaring ``risk_level=low``.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Stable finding-code prefixes — audit consumers key off these strings.
# ---------------------------------------------------------------------------

CODE_IDENTITY_MISMATCH = "review.identity.mismatch"
CODE_IDENTITY_DUPLICATE_PROPOSAL_ID = "review.identity.duplicate_proposal_id"
CODE_EVIDENCE_MISSING = "review.evidence.missing"
CODE_EVIDENCE_DANGLING = "review.evidence.dangling"
CODE_EVIDENCE_FOREIGN_TENANT = "review.evidence.foreign_tenant"
CODE_EVIDENCE_HASH_MISMATCH = "review.evidence.hash_mismatch"
CODE_EVIDENCE_DUPLICATE = "review.evidence.duplicate"
CODE_EVIDENCE_TYPE_MISMATCH = "review.evidence.type_mismatch"
CODE_AUTHORITY_INSUFFICIENT = "review.authority.insufficient"
CODE_AUTHORITY_READ_ONLY_WRITE = "review.authority.read_only_write"
CODE_AUTHORITY_PROPOSE_EXECUTE = "review.authority.propose_execute"
CODE_AUTHORITY_EXCEEDS_SNAPSHOT = "review.authority.exceeds_snapshot"
CODE_ACTION_UNKNOWN_TYPE = "review.action.unknown_type"
CODE_ACTION_UNKNOWN_TOOL = "review.action.unknown_tool"
CODE_ACTION_TOOL_FORBIDDEN = "review.action.tool_forbidden"
CODE_ACTION_PARAMETER_INVALID = "review.action.parameter_invalid"
CODE_ACTION_CATEGORY_NOT_REVIEWABLE = "review.action.category_not_reviewable"
CODE_TENANT_CROSS_REFERENCE = "review.tenant.cross_reference"
CODE_TENANT_SECRET_FIELD = "review.tenant.secret_field"
CODE_TENANT_PII_EGRESS = "review.tenant.pii_egress"
CODE_IDEMPOTENCY_MISSING = "review.idempotency.missing"
CODE_IDEMPOTENCY_BLANK = "review.idempotency.blank"
CODE_IDEMPOTENCY_INCONSISTENT = "review.idempotency.inconsistent"
CODE_RISK_HIGH_NEEDS_APPROVAL = "review.risk.high_needs_approval"
CODE_RISK_CRITICAL_NEEDS_APPROVAL = "review.risk.critical_needs_approval"
CODE_RISK_REQUIRES_EVIDENCE = "review.risk.requires_evidence"
CODE_POLICY_DENIED = "review.policy.denied"
CODE_POLICY_NEEDS_INPUT = "review.policy.needs_input"
CODE_POLICY_NEEDS_APPROVAL = "review.policy.needs_approval"
CODE_DUPLICATE_DETECTED = "review.duplicate.detected"
CODE_DUPLICATE_DEDUPED = "review.duplicate.deduped"
CODE_CONFLICT_FIELD_VALUE = "review.conflict.field_value"
CODE_CONFLICT_ACTIVATE_DEACTIVATE = "review.conflict.activate_deactivate"
CODE_CONFLICT_CREATE_DELETE = "review.conflict.create_delete"
CODE_CONFLICT_IDEMPOTENCY_MISMATCH = "review.conflict.idempotency_mismatch"
CODE_CONFLICT_MUTEX_NOTIFICATION = "review.conflict.mutex_notification"
CODE_CONFLICT_OWNER_REASSIGN = "review.conflict.owner_reassign"


# ---------------------------------------------------------------------------
# Task / Trace summaries — frozen, minimal snapshots carried by ReviewRequest.
# ---------------------------------------------------------------------------


class TaskRecordSummary(StrictContract):
    """Frozen summary of a :class:`TaskExecutionRecord`.

    The Reviewer only needs identity + agent binding for authority
    validation.  Full attempt history is NOT carried — it would
    bloat the request hash and is already audited via
    :class:`ExecutionTraceEvent`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    agent_id: str
    status: str
    skip_reason: str | None = None


class TraceSummary(StrictContract):
    """Frozen summary of a single :class:`ExecutionTraceEvent`.

    Only the identity + event_type are kept — ``data`` is excluded
    because it carries event-specific payload that does not affect
    the Reviewer's decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    event_type: str
    task_id: str | None = None
    agent_id: str | None = None


class CapabilitySnapshot(StrictContract):
    """Frozen Capability snapshot used for Authority validation.

    Phase 5A Section 7.3: the Reviewer MUST validate against the
    snapshot taken at Phase 4 pre-flight time.  It MUST NOT read a
    live registry to decide historical Proposal authority.

    ``AgentCapability`` is already frozen, but wrapping it in a
    dedicated snapshot type makes the boundary explicit and lets
    the request hash include the agent_id → capability mapping as
    a stable, sorted structure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    capability: AgentCapability


# ---------------------------------------------------------------------------
# ReviewFinding
# ---------------------------------------------------------------------------


class ReviewFinding(StrictContract):
    """A single observation produced during Proposal review.

    ``details`` is a restricted ``dict[str, JsonValue]`` — never
    ``Any``.  It must pass :func:`validate_strict_json` so that
    bytes / sets / Decimals / datetimes are rejected at construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_code: str
    severity: ReviewFindingSeverity
    message: str
    proposal_id: str
    task_id: str | None = None
    agent_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    policy_source: str = ""
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("finding_code")
    @classmethod
    def _finding_code_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("finding_code must not be blank")
        return v

    @field_validator("message")
    @classmethod
    def _message_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be blank")
        return v

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("proposal_id must not be blank")
        return v

    @field_validator("details")
    @classmethod
    def _validate_details(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Reject sensitive keys (delegates to the shared scanner used
        # by ActionProposal.payload) and reject non-JSON types.
        from multi_agent.contracts import _reject_sensitive_keys
        from multi_agent.serialization import validate_strict_json

        _reject_sensitive_keys(v, "ReviewFinding.details")
        return validate_strict_json(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ReviewRequest
# ---------------------------------------------------------------------------


class PolicyContext(StrictContract):
    """Frozen, minimal policy context carried by :class:`ReviewRequest`.

    The Reviewer does NOT receive the live policy engine — only a
    snapshot of the rules + version that the
    :class:`DeterministicPolicyEvaluator` should apply.

    ``rules`` is a list of plain-dict rule descriptors so the request
    hash is stable.  Each rule dict must be JSON-strict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str
    rules: list[dict[str, JsonValue]] = Field(default_factory=list)
    tenant_overrides: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("policy_version")
    @classmethod
    def _version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("policy_version must not be blank")
        return v

    @field_validator("rules", "tenant_overrides")
    @classmethod
    def _validate_json_strict(cls, v: Any) -> Any:
        from multi_agent.contracts import _reject_sensitive_keys
        from multi_agent.serialization import validate_strict_json

        _reject_sensitive_keys(v, "PolicyContext")
        return validate_strict_json(v)


class ReviewRequest(StrictContract):
    """Frozen input to :meth:`ProposalReviewer.review`.

    Carries everything the Reviewer needs to make a deterministic
    decision: Proposals, Evidence, Task Records, Trace, Policy
    Context, Capability Snapshots, and the Phase 4 identity
    (run_id / tenant_id / plan_hash / registry_version).

    The ``request_hash`` is computed over the canonical form and
    verifies integrity on construction.  Mutating any field after
    construction is impossible (``frozen=True``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    tenant_id: str
    plan_hash: str
    registry_version: str

    proposals: list[ActionProposal] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    task_records: list[TaskRecordSummary] = Field(default_factory=list)
    trace: list[TraceSummary] = Field(default_factory=list)
    capability_snapshots: list[CapabilitySnapshot] = Field(default_factory=list)
    policy_context: PolicyContext
    reviewer_version: str = REVIEWER_VERSION

    request_hash: str = ""

    @field_validator(
        "review_id", "run_id", "tenant_id", "plan_hash", "registry_version"
    )
    @classmethod
    def _identity_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewRequest identity fields must not be blank")
        return v

    @field_validator("reviewer_version")
    @classmethod
    def _reviewer_version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reviewer_version must not be blank")
        return v

    @model_validator(mode="after")
    def _populate_and_verify_request_hash(self) -> "ReviewRequest":
        # Single hash computation path: always call compute_hash() so
        # the value populated on first construction is identical to
        # the value verified on round-trip.  Using object.__setattr__
        # bypasses the frozen-model guard — this is the documented
        # escape hatch for validators that need to seed a field.
        expected = self.compute_hash()
        if not self.request_hash:
            object.__setattr__(self, "request_hash", expected)
        elif not compare_digest(self.request_hash, expected):
            raise ReviewIntegrityError(
                f"ReviewRequest {self.review_id!r}: stored request_hash "
                f"{self.request_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    # -- public API ---------------------------------------------------------

    def compute_hash(self) -> str:
        """Return a stable SHA-256 over the canonical request content.

        Excludes ``request_hash`` (self-referential) and any
        wall-clock field.  List fields are sorted by stable key inside
        :func:`canonicalize` (via frozenset conversion for sets, and
        explicit sort for lists of BaseModel via the ``mode="python"``
        round-trip).
        """
        return stable_hash(self, exclude={"request_hash"})

    def verify_integrity(self) -> None:
        """Recompute and compare ``request_hash``.  Raise on mismatch."""
        if not compare_digest(self.request_hash, self.compute_hash()):
            raise ReviewIntegrityError(
                f"ReviewRequest {self.review_id!r}: request_hash does not "
                f"match recomputed content"
            )

    def to_canonical_payload(self) -> dict[str, Any]:
        """Return the canonical dict used for hashing — exposed for
        audit / debugging."""
        data = self.model_dump(mode="python")
        data.pop("request_hash", None)
        return canonicalize(data)


# ---------------------------------------------------------------------------
# ProposalReview
# ---------------------------------------------------------------------------


class ProposalReview(StrictContract):
    """Per-Proposal review outcome.

    Frozen so audit consumers can hold a reference without worrying
    about mutation.  The ``review_hash`` covers every field that
    affects the decision so a tampered review is detectable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    status: ReviewDecisionStatus
    findings: list[ReviewFinding] = Field(default_factory=list)
    matched_evidence_ids: list[str] = Field(default_factory=list)
    required_approval: bool = False
    risk_level: ReviewRiskLevel = ReviewRiskLevel.LOW
    authority_valid: bool = False
    policy_valid: bool = False
    idempotency_valid: bool = False
    review_hash: str = ""

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("proposal_id must not be blank")
        return v

    @model_validator(mode="after")
    def _populate_and_verify_review_hash(self) -> "ProposalReview":
        expected = self.compute_hash()
        if not self.review_hash:
            object.__setattr__(self, "review_hash", expected)
        elif not compare_digest(self.review_hash, expected):
            raise ReviewIntegrityError(
                f"ProposalReview {self.proposal_id!r}: stored review_hash "
                f"{self.review_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"review_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.review_hash, self.compute_hash()):
            raise ReviewIntegrityError(
                f"ProposalReview {self.proposal_id!r}: review_hash does not "
                f"match recomputed content"
            )


# ---------------------------------------------------------------------------
# ReviewBatchResult
# ---------------------------------------------------------------------------


def batch_status_priority(status: ReviewBatchStatus) -> int:
    """Return priority weight — higher wins.

    Priority order (Phase 5A Section 11):
        conflict > rejected > needs_input > needs_approval > approved
    """
    order = {
        ReviewBatchStatus.APPROVED: 0,
        ReviewBatchStatus.NEEDS_APPROVAL: 1,
        ReviewBatchStatus.NEEDS_INPUT: 2,
        ReviewBatchStatus.REJECTED: 3,
        ReviewBatchStatus.CONFLICT: 4,
    }
    return order[status]


def proposal_status_to_batch(status: ReviewDecisionStatus) -> ReviewBatchStatus:
    """Map a per-proposal decision to its batch-level equivalent."""
    mapping = {
        ReviewDecisionStatus.APPROVED: ReviewBatchStatus.APPROVED,
        ReviewDecisionStatus.NEEDS_APPROVAL: ReviewBatchStatus.NEEDS_APPROVAL,
        ReviewDecisionStatus.NEEDS_INPUT: ReviewBatchStatus.NEEDS_INPUT,
        ReviewDecisionStatus.REJECTED: ReviewBatchStatus.REJECTED,
        ReviewDecisionStatus.CONFLICT: ReviewBatchStatus.CONFLICT,
    }
    return mapping[status]


class ReviewBatchResult(StrictContract):
    """Final output of :meth:`ProposalReviewer.review`.

    Frozen, deterministically hashable, and sorted: every list field
    is ordered by a stable key so the same input always produces the
    same ``result_hash`` regardless of insertion order or
    ``PYTHONHASHSEED``.

    The ``batch_status`` is the highest-priority decision across all
    per-Proposal reviews (Phase 5A Section 11).  A batch marked
    ``rejected`` does NOT mean every Proposal was rejected — each
    Proposal retains its own independent decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    tenant_id: str
    request_hash: str

    proposal_reviews: list[ProposalReview] = Field(default_factory=list)
    batch_status: ReviewBatchStatus = ReviewBatchStatus.APPROVED
    approved_proposal_ids: list[str] = Field(default_factory=list)
    rejected_proposal_ids: list[str] = Field(default_factory=list)
    approval_required_proposal_ids: list[str] = Field(default_factory=list)
    conflicted_proposal_ids: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)

    result_hash: str = ""
    reviewer_version: str = REVIEWER_VERSION

    @field_validator("review_id", "run_id", "tenant_id", "request_hash")
    @classmethod
    def _identity_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewBatchResult identity fields must not be blank")
        return v

    @field_validator("reviewer_version")
    @classmethod
    def _reviewer_version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reviewer_version must not be blank")
        return v

    @model_validator(mode="after")
    def _populate_and_verify_result_hash(self) -> "ReviewBatchResult":
        expected = self.compute_hash()
        if not self.result_hash:
            object.__setattr__(self, "result_hash", expected)
        elif not compare_digest(self.result_hash, expected):
            raise ReviewIntegrityError(
                f"ReviewBatchResult {self.review_id!r}: stored result_hash "
                f"{self.result_hash[:12]!r} != computed {expected[:12]!r}"
            )
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"result_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.result_hash, self.compute_hash()):
            raise ReviewIntegrityError(
                f"ReviewBatchResult {self.review_id!r}: result_hash does not "
                f"match recomputed content"
            )


__all__ = [
    "CODE_ACTION_CATEGORY_NOT_REVIEWABLE",
    "CODE_ACTION_PARAMETER_INVALID",
    "CODE_ACTION_TOOL_FORBIDDEN",
    "CODE_ACTION_UNKNOWN_TOOL",
    "CODE_ACTION_UNKNOWN_TYPE",
    "CODE_AUTHORITY_EXCEEDS_SNAPSHOT",
    "CODE_AUTHORITY_INSUFFICIENT",
    "CODE_AUTHORITY_PROPOSE_EXECUTE",
    "CODE_AUTHORITY_READ_ONLY_WRITE",
    "CODE_CONFLICT_ACTIVATE_DEACTIVATE",
    "CODE_CONFLICT_CREATE_DELETE",
    "CODE_CONFLICT_FIELD_VALUE",
    "CODE_CONFLICT_IDEMPOTENCY_MISMATCH",
    "CODE_CONFLICT_MUTEX_NOTIFICATION",
    "CODE_CONFLICT_OWNER_REASSIGN",
    "CODE_DUPLICATE_DEDUPED",
    "CODE_DUPLICATE_DETECTED",
    "CODE_EVIDENCE_DANGLING",
    "CODE_EVIDENCE_DUPLICATE",
    "CODE_EVIDENCE_FOREIGN_TENANT",
    "CODE_EVIDENCE_HASH_MISMATCH",
    "CODE_EVIDENCE_MISSING",
    "CODE_EVIDENCE_TYPE_MISMATCH",
    "CODE_IDEMPOTENCY_BLANK",
    "CODE_IDEMPOTENCY_INCONSISTENT",
    "CODE_IDEMPOTENCY_MISSING",
    "CODE_IDENTITY_DUPLICATE_PROPOSAL_ID",
    "CODE_IDENTITY_MISMATCH",
    "CODE_POLICY_DENIED",
    "CODE_POLICY_NEEDS_APPROVAL",
    "CODE_POLICY_NEEDS_INPUT",
    "CODE_RISK_CRITICAL_NEEDS_APPROVAL",
    "CODE_RISK_HIGH_NEEDS_APPROVAL",
    "CODE_RISK_REQUIRES_EVIDENCE",
    "CODE_TENANT_CROSS_REFERENCE",
    "CODE_TENANT_PII_EGRESS",
    "CODE_TENANT_SECRET_FIELD",
    "CapabilitySnapshot",
    "PolicyContext",
    "ProposalReview",
    "REVIEWER_VERSION",
    "ReviewBatchResult",
    "ReviewBatchStatus",
    "ReviewDecisionStatus",
    "ReviewFinding",
    "ReviewFindingSeverity",
    "ReviewRequest",
    "ReviewRiskLevel",
    "TaskRecordSummary",
    "TraceSummary",
    "batch_status_priority",
    "proposal_status_to_batch",
]
