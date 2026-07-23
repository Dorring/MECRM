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
from multi_agent.execution import ExecutionCapabilitySnapshot
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewIntegrityError,
)
from multi_agent.serialization import canonicalize, content_hash, stable_hash


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
    DEDUPLICATED = "deduplicated"


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
    ``needs_input`` > ``needs_approval`` > ``deduplicated`` >
    ``approved``.
    """

    APPROVED = "approved"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    DEDUPLICATED = "deduplicated"


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


class ReviewProposalEnvelope(StrictContract):
    """Frozen, strict-origin envelope binding a Proposal to the exact
    Phase 4 Task/Result that produced it.

    Phase 5A must NOT accept a Proposal whose origin can be re-attached
    to an arbitrary Task.  The Reviewer validates every field below
    against the ReviewRequest's task_records / capability_bindings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: ActionProposal
    run_id: str
    result_id: str
    task_id: str
    agent_id: str
    agent_version: str
    origin_hash: str

    @field_validator(
        "run_id", "result_id", "task_id", "agent_id", "agent_version", "origin_hash"
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewProposalEnvelope identity fields must not be blank")
        return v

    @model_validator(mode="after")
    def _verify_origin_hash(self) -> "ReviewProposalEnvelope":
        from multi_agent.serialization import stable_hash

        expected = stable_hash(
            {
                "proposal": self.proposal.model_dump(mode="python"),
                "run_id": self.run_id,
                "result_id": self.result_id,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "agent_version": self.agent_version,
            }
        )
        if self.origin_hash != expected:
            raise ValueError("ReviewProposalEnvelope origin_hash mismatch")
        return self


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


def canonical_review_request_payload(request: ReviewRequest) -> dict[str, Any]:
    """Return a canonical, ORDER-INVARIANT dict for hashing.

    Each list field is sorted by a stable key so that reordering the
    input lists does not change the request_hash (R1 P0-4).
    ``request_hash`` is excluded (self-referential).
    """
    proposals = sorted(request.proposals, key=lambda p: p.proposal_id)
    proposal_envelopes = sorted(
        request.proposal_envelopes, key=lambda e: e.proposal.proposal_id
    )
    evidence = sorted(request.evidence, key=lambda e: e.evidence_id)
    task_records = sorted(request.task_records, key=lambda t: t.task_id)
    capability_bindings = sorted(
        request.capability_bindings, key=lambda c: (c.task_id, c.agent_id)
    )
    trace = sorted(request.trace, key=lambda t: t.sequence)
    rules = sorted(
        request.policy_context.rules,
        key=lambda r: (int(r.get("priority", 0)), str(r.get("rule_id", ""))),
    )

    payload: dict[str, Any] = {
        "review_id": request.review_id,
        "run_id": request.run_id,
        "tenant_id": request.tenant_id,
        "plan_hash": request.plan_hash,
        "registry_version": request.registry_version,
        "proposals": [p.model_dump(mode="python") for p in proposals],
        "evidence": [e.model_dump(mode="python") for e in evidence],
        "task_records": [t.model_dump(mode="python") for t in task_records],
        "trace": [t.model_dump(mode="python") for t in trace],
        "proposal_envelopes": [e.model_dump(mode="python") for e in proposal_envelopes],
        "capability_bindings": [
            c.model_dump(mode="python") for c in capability_bindings
        ],
        "policy_context": {
            **request.policy_context.model_dump(mode="python"),
            "rules": rules,
        },
        "reviewer_version": request.reviewer_version,
    }
    return canonicalize(payload)


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
    proposal_envelopes: list[ReviewProposalEnvelope] = Field(default_factory=list)
    capability_bindings: list[ExecutionCapabilitySnapshot] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def _validate_identity_uniqueness(self) -> "ReviewRequest":
        """Fail-closed identity uniqueness checks.

        Any duplicate-with-different-content, duplicate task_id,
        duplicate agent_id in capability_bindings, duplicate trace
        sequence, duplicate origin_hash, or envelope/proposal mismatch
        raises :class:`InvalidReviewRequestError`.
        """
        from multi_agent.evidence_review import compute_review_evidence_hash

        # proposal_id uniqueness — same id (whether same or different
        # content) is an error: Phase 4 merge should have deduped.
        proposals_by_id: dict[str, list[ActionProposal]] = {}
        for p in self.proposals:
            proposals_by_id.setdefault(p.proposal_id, []).append(p)
        for pid, group in proposals_by_id.items():
            if len(group) > 1:
                hashes = {content_hash(p) for p in group}
                if len(hashes) > 1:
                    raise InvalidReviewRequestError(
                        f"Duplicate proposal_id {pid!r} with different content "
                        f"in ReviewRequest {self.review_id!r}"
                    )
                raise InvalidReviewRequestError(
                    f"Duplicate proposal_id {pid!r} (same content) in "
                    f"ReviewRequest {self.review_id!r} — Phase 4 merge should "
                    f"have deduped"
                )

        # evidence_id uniqueness — same id with different review-hash
        # → fail-closed raise.  Same id + same hash → benign, allow.
        evidence_by_id: dict[str, list[Evidence]] = {}
        for ev in self.evidence:
            evidence_by_id.setdefault(ev.evidence_id, []).append(ev)
        for eid, evidence_group in evidence_by_id.items():
            if len(evidence_group) > 1:
                hashes = {
                    compute_review_evidence_hash(ev_item) for ev_item in evidence_group
                }
                if len(hashes) > 1:
                    raise InvalidReviewRequestError(
                        f"Duplicate evidence_id {eid!r} with different content "
                        f"in ReviewRequest {self.review_id!r}"
                    )

        # task_id uniqueness across task_records
        task_ids: dict[str, int] = {}
        for tr in self.task_records:
            task_ids[tr.task_id] = task_ids.get(tr.task_id, 0) + 1
        for tid, count in task_ids.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate task_id {tid!r} in ReviewRequest {self.review_id!r}"
                )

        # agent_id uniqueness across capability_bindings
        agent_ids: dict[str, int] = {}
        for cb in self.capability_bindings:
            agent_ids[cb.agent_id] = agent_ids.get(cb.agent_id, 0) + 1
        for aid, count in agent_ids.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate agent_id {aid!r} in capability_bindings of "
                    f"ReviewRequest {self.review_id!r}"
                )

        # sequence uniqueness across trace
        sequences: dict[int, int] = {}
        for trace_ev in self.trace:
            sequences[trace_ev.sequence] = sequences.get(trace_ev.sequence, 0) + 1
        for seq, count in sequences.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate trace sequence {seq} in ReviewRequest "
                    f"{self.review_id!r}"
                )

        # origin_hash uniqueness across proposal_envelopes
        origin_hashes: dict[str, int] = {}
        for env in self.proposal_envelopes:
            origin_hashes[env.origin_hash] = origin_hashes.get(env.origin_hash, 0) + 1
        for oh, count in origin_hashes.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate origin_hash {oh[:12]!r} in proposal_envelopes "
                    f"of ReviewRequest {self.review_id!r}"
                )

        # Envelope ↔ Proposal bijection: every envelope's proposal_id
        # must match exactly one proposal, and every proposal must have
        # a matching envelope.
        proposal_ids = {p.proposal_id for p in self.proposals}
        envelope_proposal_ids = {
            env.proposal.proposal_id for env in self.proposal_envelopes
        }
        missing_envelopes = proposal_ids - envelope_proposal_ids
        if missing_envelopes:
            raise InvalidReviewRequestError(
                f"Proposals without matching envelope in ReviewRequest "
                f"{self.review_id!r}: {sorted(missing_envelopes)!r}"
            )
        orphan_envelopes = envelope_proposal_ids - proposal_ids
        if orphan_envelopes:
            raise InvalidReviewRequestError(
                f"Envelopes without matching proposal in ReviewRequest "
                f"{self.review_id!r}: {sorted(orphan_envelopes)!r}"
            )

        return self

    # -- public API ---------------------------------------------------------

    def compute_hash(self) -> str:
        """Return a stable SHA-256 over the canonical request content.

        Excludes ``request_hash`` (self-referential) and any
        wall-clock field.  List fields are sorted by stable key so
        the hash is ORDER-INVARIANT (R1 P0-4).
        """
        return stable_hash(canonical_review_request_payload(self))

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
        return canonical_review_request_payload(self)


# ---------------------------------------------------------------------------
# ProposalReview
# ---------------------------------------------------------------------------


# Finding codes whose presence justifies a REJECTED decision.  Used by
# :meth:`ProposalReview.verify_semantics` to detect a status/finding
# contradiction (e.g. status==REJECTED but no rejection-class finding).
REJECTION_FINDING_CODES: frozenset[str] = frozenset(
    {
        CODE_TENANT_CROSS_REFERENCE,
        CODE_TENANT_SECRET_FIELD,
        CODE_TENANT_PII_EGRESS,
        CODE_AUTHORITY_INSUFFICIENT,
        CODE_AUTHORITY_READ_ONLY_WRITE,
        CODE_AUTHORITY_PROPOSE_EXECUTE,
        CODE_AUTHORITY_EXCEEDS_SNAPSHOT,
        CODE_POLICY_DENIED,
        CODE_EVIDENCE_HASH_MISMATCH,
        CODE_EVIDENCE_DUPLICATE,
        CODE_EVIDENCE_FOREIGN_TENANT,
        CODE_EVIDENCE_TYPE_MISMATCH,
        CODE_IDENTITY_MISMATCH,
        CODE_IDENTITY_DUPLICATE_PROPOSAL_ID,
        CODE_ACTION_UNKNOWN_TYPE,
        CODE_ACTION_TOOL_FORBIDDEN,
        CODE_ACTION_PARAMETER_INVALID,
        CODE_ACTION_CATEGORY_NOT_REVIEWABLE,
        CODE_IDEMPOTENCY_MISSING,
        CODE_IDEMPOTENCY_BLANK,
        CODE_IDEMPOTENCY_INCONSISTENT,
    }
)


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

    def verify_semantics(self) -> None:
        """Validate that status and flags are consistent with findings.

        Raises :class:`InvalidReviewResultError` on any inconsistency.
        """
        has_error = any(
            f.severity in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )
        finding_codes = {f.finding_code for f in self.findings}

        if self.status == ReviewDecisionStatus.APPROVED:
            if has_error:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"has ERROR/CRITICAL findings"
                )
        elif self.status == ReviewDecisionStatus.REJECTED:
            if not (finding_codes & REJECTION_FINDING_CODES):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status REJECTED but "
                    f"no rejection-class finding"
                )
        elif self.status == ReviewDecisionStatus.NEEDS_APPROVAL:
            if not self.required_approval:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_APPROVAL "
                    f"but required_approval is False"
                )
        elif self.status == ReviewDecisionStatus.NEEDS_INPUT:
            if not (finding_codes & {CODE_EVIDENCE_MISSING, CODE_POLICY_NEEDS_INPUT}):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_INPUT "
                    f"but no {CODE_EVIDENCE_MISSING!r}/{CODE_POLICY_NEEDS_INPUT!r} "
                    f"finding"
                )
        elif self.status == ReviewDecisionStatus.CONFLICT:
            if not any(c.startswith("review.conflict.") for c in finding_codes):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status CONFLICT but "
                    f"no review.conflict.* finding"
                )
        elif self.status == ReviewDecisionStatus.DEDUPLICATED:
            if CODE_DUPLICATE_DEDUPED not in finding_codes:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status DEDUPLICATED "
                    f"but no {CODE_DUPLICATE_DEDUPED!r} finding"
                )

        # Flag ↔ finding consistency
        has_authority_error = any(
            f.finding_code.startswith("review.authority.")
            and f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )
        if has_authority_error and self.authority_valid:
            raise InvalidReviewResultError(
                f"ProposalReview {self.proposal_id!r}: authority_valid=True but "
                f"an authority ERROR finding exists"
            )
        if CODE_POLICY_DENIED in finding_codes and self.policy_valid:
            raise InvalidReviewResultError(
                f"ProposalReview {self.proposal_id!r}: policy_valid=True but "
                f"a {CODE_POLICY_DENIED!r} finding exists"
            )
        has_idempotency_error = any(
            f.finding_code.startswith("review.idempotency.")
            and f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )
        if has_idempotency_error and self.idempotency_valid:
            raise InvalidReviewResultError(
                f"ProposalReview {self.proposal_id!r}: idempotency_valid=True "
                f"but an idempotency ERROR finding exists"
            )


# ---------------------------------------------------------------------------
# ReviewBatchResult
# ---------------------------------------------------------------------------


def batch_status_priority(status: ReviewBatchStatus) -> int:
    """Return priority weight — higher wins.

    Priority order (Phase 5A Section 11):
        conflict > rejected > needs_input > needs_approval > deduplicated > approved
    """
    order = {
        ReviewBatchStatus.APPROVED: 0,
        ReviewBatchStatus.DEDUPLICATED: 1,
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
        ReviewDecisionStatus.DEDUPLICATED: ReviewBatchStatus.DEDUPLICATED,
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
    deduplicated_proposal_ids: list[str] = Field(default_factory=list)
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

    def verify_semantics(self) -> None:
        """Validate that batch-level summaries are consistent with
        per-proposal reviews.

        Raises :class:`InvalidReviewResultError` on any inconsistency.
        """
        if not self.reviewer_version.strip():
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: reviewer_version blank"
            )

        # proposal_ids unique
        ids = [r.proposal_id for r in self.proposal_reviews]
        if len(ids) != len(set(ids)):
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: duplicate proposal_ids "
                f"in proposal_reviews"
            )

        # batch_status recompute
        if self.proposal_reviews:
            recomputed = max(
                (proposal_status_to_batch(r.status) for r in self.proposal_reviews),
                key=batch_status_priority,
            )
        else:
            recomputed = ReviewBatchStatus.APPROVED
        if self.batch_status != recomputed:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: batch_status "
                f"{self.batch_status.value!r} != recomputed "
                f"{recomputed.value!r}"
            )

        # Summary id lists
        expected_approved = sorted(
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.APPROVED
        )
        if self.approved_proposal_ids != expected_approved:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: approved_proposal_ids mismatch"
            )
        expected_rejected = sorted(
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.REJECTED
        )
        if self.rejected_proposal_ids != expected_rejected:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: rejected_proposal_ids mismatch"
            )
        expected_approval = sorted(
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.NEEDS_APPROVAL
        )
        if self.approval_required_proposal_ids != expected_approval:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"approval_required_proposal_ids mismatch"
            )
        expected_conflicted = sorted(
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.CONFLICT
        )
        if self.conflicted_proposal_ids != expected_conflicted:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"conflicted_proposal_ids mismatch"
            )
        expected_deduped = sorted(
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.DEDUPLICATED
        )
        if self.deduplicated_proposal_ids != expected_deduped:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"deduplicated_proposal_ids mismatch"
            )

        # Per-proposal semantic validation
        for r in self.proposal_reviews:
            r.verify_semantics()


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
    "REJECTION_FINDING_CODES",
    "REVIEWER_VERSION",
    "ReviewBatchResult",
    "ReviewBatchStatus",
    "ReviewDecisionStatus",
    "ReviewFinding",
    "ReviewFindingSeverity",
    "ReviewProposalEnvelope",
    "ReviewRequest",
    "ReviewRiskLevel",
    "TaskRecordSummary",
    "TraceSummary",
    "batch_status_priority",
    "canonical_review_request_payload",
    "proposal_status_to_batch",
]
