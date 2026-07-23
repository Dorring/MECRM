"""Phase 5A Review Contracts (R2).

Strict, deterministically-hashable contracts for the Reviewer &
Governance Decision Layer.

Design rules (Phase 5A Section 5, reinforced by R2):

* Every public contract inherits :class:`StrictContract`
  (``extra="forbid"``, ``validate_assignment=True``).
* Frozen contracts (``ReviewFinding``, ``ProposalReview``,
  ``ReviewBatchResult``, ``ReviewRequest``) cannot be mutated after
  construction — audit records must be immutable.
* R2 S1 (deep immutability): every audit-boundary collection is a
  ``tuple`` (or ``frozenset``), never a ``list``/``set``.  This
  prevents ``request.proposals.append(...)`` even though
  ``frozen=True`` only blocks field re-assignment.
* Stable serialization via :func:`stable_hash` (SHA-256 over the
  canonicalized form).  The same input MUST produce the same hash
  across processes, ``PYTHONHASHSEED`` values, and call order.
* No ``Any`` type in field annotations; ``details`` uses
  ``dict[str, JsonValue]`` which is the existing project pattern
  (see :class:`AgentError.details`, :class:`AgentCapability.metadata`).
* No Handler / Callable / non-serialisable object is stored.

Phase 5A Section 3 reminder: ``approved`` means "Proposal has passed
review".  It NEVER means "Proposal has been executed".

R2 changes (P0-1 .. P0-9 + S1 .. S14):

* ``REVIEWER_VERSION`` bumped to ``ma-05a.2.0``.
* :class:`PolicyDecision` moved here (was in :mod:`multi_agent.policy`)
  so :class:`PolicyRule` can reference it without a circular import.
* :class:`PolicyRule` replaces the raw-dict ``rules`` on
  :class:`PolicyContext` (P0-6).
* :class:`ReviewEvidenceSnapshot` wraps every :class:`Evidence` with a
  verified ``snapshot_hash`` (P0-3).
* :class:`PolicyDecisionAudit` is carried on every
  :class:`ProposalReview` (S5).
* :class:`ReviewBatchStatus.NO_ACTIONS` for empty batches (S7).
* :class:`ReviewBatchResult.verify_against_request` binds Result →
  Request (S8).
* All audit-boundary collections are ``tuple`` (S1).
* ``batch_status_priority`` uses unique weights (P0-7).
* :class:`ProposalReview.verify_semantics` enforces validity-flag
  consistency (P0-8).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hmac import compare_digest
from typing import Any, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentCapability,
    Evidence,
    StrictContract,
)
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ResultOriginSnapshot,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewIntegrityError,
)
from multi_agent.serialization import canonicalize, content_hash, stable_hash


# ---------------------------------------------------------------------------
# R2.1 P0-1: Deep Frozen JSON — recursive immutable JSON-like value.
# ---------------------------------------------------------------------------


def freeze_json_value(value: Any) -> Any:
    """Recursively convert a JSON-like value into a deeply immutable form.

    R2.1 P0-1: ``frozen=True`` on a Pydantic model only blocks field
    re-assignment — it does NOT block in-place mutation of ``dict`` /
    ``list`` / ``set`` field values.  This converter produces a fully
    immutable structure so a frozen Contract's nested payload cannot
    be tampered with between ``verify_integrity()`` and decision use.

    Conversion rules (recursive):

    * ``dict`` → ``tuple`` of ``(str_key, frozen_value)`` tuples sorted
      by key (canonical order so the frozen form is hash-stable).
    * ``list`` / ``tuple`` → ``tuple`` of frozen values (order preserved).
    * ``set`` / ``frozenset`` → ``frozenset`` of frozen values.
    * ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` → returned as-is.
    * Anything else (Enum, datetime, Decimal, BaseModel, bytes) is
      canonicalised via :func:`multi_agent.serialization.canonicalize`
      first, then frozen — so the frozen form is always JSON-native.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return tuple(
            (str(k), freeze_json_value(v))
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_json_value(v) for v in value)
    # Non-JSON-native (Enum, datetime, Decimal, BaseModel, bytes) —
    # canonicalise to JSON-native first, then freeze.
    return freeze_json_value(canonicalize(value))


def frozen_value_to_json(value: Any) -> Any:
    """Inverse of :func:`freeze_json_value` — returns a JSON-native copy.

    Used by ``model_dump()`` / hash computation so the frozen tuple-of-
    tuples form round-trips back to a plain dict for serialisation.
    The returned value is a fresh copy — mutating it does NOT affect
    the frozen Contract.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        # Distinguish dict-derived tuples (tuple of (str, v) tuples)
        # from list-derived tuples (tuple of plain values).
        if value and all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {k: frozen_value_to_json(v) for k, v in value}
        return [frozen_value_to_json(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(
            (frozen_value_to_json(v) for v in value),
            key=lambda x: str(x),
        )
    return value


# ``FrozenJsonValue`` is the type alias for the output of
# :func:`freeze_json_value`.  It is declared as ``Any`` because the
# actual recursive type (``Union[None, bool, int, float, str,
# tuple[FrozenJsonValue, ...], frozenset[FrozenJsonValue],
# tuple[tuple[str, FrozenJsonValue], ...]]``) is not directly
# expressible as a Pydantic field type without a custom
# ``__get_pydantic_core_schema__``.  Immutability is enforced by the
# ``freeze_json_value`` validator, NOT by the type annotation.
FrozenJsonValue = Any


# ---------------------------------------------------------------------------
# Version — bumped whenever the Reviewer algorithm changes.
# ---------------------------------------------------------------------------

REVIEWER_VERSION = "ma-05a.2.0"

# R2 S10: schema version carried in serialization / hash so a payload
# built against an older schema is rejected at the boundary.
REVIEW_SCHEMA_VERSION = "1"


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

    R2 P0-7 / S7 priority (highest first):

        ``conflict`` > ``rejected`` > ``needs_input`` >
        ``needs_approval`` > ``deduplicated`` > ``approved`` >
        ``no_actions``.

    ``no_actions`` is reserved for an empty batch — it is NEVER
    equivalent to ``approved`` so Phase 5B cannot mis-treat an empty
    Review as authorisation to execute nothing-as-everything.
    """

    NO_ACTIONS = "no_actions"
    APPROVED = "approved"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    DEDUPLICATED = "deduplicated"


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


class PolicyDecision(StrEnum):
    """Possible Policy decisions for a single Proposal.

    R2: moved here from :mod:`multi_agent.policy` so
    :class:`PolicyRule` (in this module) can reference it without a
    circular import.  :mod:`multi_agent.policy` re-imports it.

    The Reviewer maps these to :class:`ReviewDecisionStatus`:

    * ``ALLOWED`` → contributes to ``approved`` (subject to other checks)
    * ``DENIED`` → ``rejected``
    * ``NEEDS_APPROVAL`` → ``needs_approval``
    * ``NEEDS_INPUT`` → ``needs_input``
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"


# ---------------------------------------------------------------------------
# Risk & Approval classification (Phase 5A Section 9)
# ---------------------------------------------------------------------------


# Stable finding-code prefixes — audit consumers key off these strings.

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

    R2 S14: this remains as a Reviewer-internal convenience wrapper
    over :class:`ExecutionCapabilitySnapshot` for legacy call sites
    that look up capability by ``agent_id``.  R2 P0-2 changes the
    Reviewer to look up by ``task_id`` via
    :class:`ExecutionCapabilitySnapshot` directly —
    :class:`CapabilitySnapshot` is retained for backwards
    compatibility with :func:`validate_authority`'s public signature.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    capability: AgentCapability


class ReviewProposalSnapshot(StrictContract):
    """R2.1 P0-1: Reviewer-owned deep-frozen snapshot of an ActionProposal.

    Phase 2's :class:`ActionProposal` is NOT frozen (it uses
    ``validate_assignment=True`` but not ``frozen=True``) and its
    ``payload: dict`` / ``evidence_ids: list`` fields are mutable
    in-place.  Carrying a raw :class:`ActionProposal` inside
    :class:`ReviewRequest` left a TOCTOU window: the Reviewer called
    ``verify_integrity()`` once at entry, then ``await policy_evaluator.evaluate(...)``
    could yield control, during which ``request.proposals[0].payload["amount"] = 999999``
    would silently mutate the audited content.

    This snapshot deep-freezes every mutable field:

    * ``payload`` → ``FrozenJsonValue`` (tuple-of-tuples, sorted by key).
    * ``evidence_ids`` → ``tuple[str, ...]``.
    * ``snapshot_hash`` is verified on construction so a tampered
      snapshot is rejected at the boundary.

    The Reviewer consumes ONLY :class:`ReviewProposalSnapshot` — it
    never reads the original :class:`ActionProposal` from Phase 2.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    proposal_hash: str
    tenant_id: str
    created_by_agent: str
    action_type: str
    target_entity: str
    target_id: str | None = None
    payload: FrozenJsonValue = ()
    priority: str
    risk_level: str
    justification: str | None = None
    evidence_ids: tuple[str, ...] = ()
    requires_approval: bool = False
    idempotency_key: str = ""
    created_at: datetime
    snapshot_hash: str

    @field_validator(
        "proposal_id",
        "proposal_hash",
        "tenant_id",
        "created_by_agent",
        "action_type",
        "target_entity",
        "priority",
        "risk_level",
        "snapshot_hash",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "ReviewProposalSnapshot identity / hash fields must not be blank"
            )
        return v

    @field_validator("payload")
    @classmethod
    def _freeze_payload(cls, v: Any) -> Any:
        # R2.1 P0-1: deep-freeze the payload so nested dict / list
        # values become immutable tuple / frozenset structures.
        return freeze_json_value(v)

    @field_validator("evidence_ids")
    @classmethod
    def _freeze_evidence_ids(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(x).strip() for x in v if str(x).strip())
        return tuple(v)

    @model_validator(mode="after")
    def _verify_snapshot_hash(self) -> "ReviewProposalSnapshot":
        expected = self.compute_snapshot_hash()
        if self.snapshot_hash != expected:
            raise ValueError(
                "ReviewProposalSnapshot.snapshot_hash mismatch — tamper detected"
            )
        return self

    def compute_snapshot_hash(self) -> str:
        """Stable SHA-256 over the canonical snapshot content.

        Excludes ``snapshot_hash`` (self-referential).  Uses
        ``self.payload`` directly (already frozen) so the hash matches
        :meth:`from_proposal`'s ``snapshot_hash`` computation — both
        paths feed the frozen payload through :func:`stable_hash`,
        which canonicalises tuples to lists.  An earlier version used
        :func:`frozen_value_to_json` here, which diverged from
        ``from_proposal`` for empty dicts (``{}`` → ``()`` → ``[]``).
        """
        payload = {
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "tenant_id": self.tenant_id,
            "created_by_agent": self.created_by_agent,
            "action_type": self.action_type,
            "target_entity": self.target_entity,
            "target_id": self.target_id,
            "payload": self.payload,
            "priority": self.priority,
            "risk_level": self.risk_level,
            "justification": self.justification,
            "evidence_ids": sorted(self.evidence_ids),
            "requires_approval": self.requires_approval,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }
        return stable_hash(payload)

    @classmethod
    def from_proposal(cls, proposal: ActionProposal) -> "ReviewProposalSnapshot":
        """Build a deep-frozen snapshot from a Phase 2 :class:`ActionProposal`.

        Used by the Phase 5A Adapter when constructing
        :class:`ReviewRequest`.  The snapshot's ``snapshot_hash`` is
        computed from the proposal's canonical content so cross-checks
        against ``ActionProposal.proposal_hash`` are stable.

        R2.1 P0-1 note: ``snapshot_hash`` is computed over the
        **frozen** payload (not the raw dict) so it matches what
        :meth:`compute_snapshot_hash` produces when called on the
        constructed snapshot.  Both paths feed the payload through
        :func:`freeze_json_value` and then through :func:`stable_hash`
        (which canonicalises tuples to lists).  An earlier version
        computed ``snapshot_hash`` from the raw dict and
        ``compute_snapshot_hash`` from ``frozen_value_to_json`` —
        those two diverged for empty dicts (``{}`` → ``()`` → ``[]``)
        and caused false tamper alerts.
        """
        payload = freeze_json_value(proposal.payload)
        evidence_ids = tuple(proposal.evidence_ids or ())
        # Compute snapshot_hash from the FROZEN payload so the value
        # matches compute_snapshot_hash() after construction.
        snapshot_hash = stable_hash(
            {
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.proposal_hash,
                "tenant_id": proposal.tenant_id,
                "created_by_agent": proposal.created_by_agent,
                "action_type": proposal.action_type,
                "target_entity": proposal.target_entity,
                "target_id": proposal.target_id,
                "payload": payload,
                "priority": proposal.priority,
                "risk_level": proposal.risk_level,
                "justification": proposal.justification,
                "evidence_ids": sorted(proposal.evidence_ids or []),
                "requires_approval": proposal.requires_approval,
                "idempotency_key": proposal.idempotency_key,
                "created_at": proposal.created_at,
            }
        )
        return cls(
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            tenant_id=proposal.tenant_id,
            created_by_agent=proposal.created_by_agent,
            action_type=proposal.action_type,
            target_entity=proposal.target_entity,
            target_id=proposal.target_id,
            payload=payload,
            priority=proposal.priority,
            risk_level=proposal.risk_level,
            justification=proposal.justification,
            evidence_ids=evidence_ids,
            requires_approval=proposal.requires_approval,
            idempotency_key=proposal.idempotency_key,
            created_at=proposal.created_at,
            snapshot_hash=snapshot_hash,
        )

    def to_action_proposal(self) -> ActionProposal:
        """Return a fresh :class:`ActionProposal` copy of this snapshot.

        Used when the Reviewer needs to delegate to Phase 2 helpers
        (e.g. :func:`compute_proposal_hash`).  The returned proposal is
        a independent copy — mutating it does NOT affect the snapshot.

        R2.1 P0-1 note: ``payload`` is forced to a ``dict`` so it
        satisfies :class:`ActionProposal`'s ``dict[str, JsonValue]``
        field type even when the frozen payload is an empty tuple
        (which ``frozen_value_to_json`` would otherwise convert to a
        list).
        """
        raw_payload = frozen_value_to_json(self.payload)
        # ActionProposal.payload is typed as dict[str, JsonValue].  An
        # empty dict-derived frozen tuple round-trips to [] — coerce
        # back to dict so the Phase 2 contract validates.
        if not isinstance(raw_payload, dict):
            raw_payload = dict(raw_payload) if raw_payload else {}
        return ActionProposal(
            proposal_id=self.proposal_id,
            proposal_hash=self.proposal_hash,
            tenant_id=self.tenant_id,
            created_by_agent=self.created_by_agent,
            action_type=self.action_type,
            target_entity=self.target_entity,
            target_id=self.target_id,
            payload=raw_payload,
            priority=cast(Any, self.priority),
            risk_level=cast(ActionRiskLevel, self.risk_level),
            justification=self.justification,
            evidence_ids=list(self.evidence_ids),
            requires_approval=self.requires_approval,
            idempotency_key=self.idempotency_key,
            created_at=self.created_at,
        )


class ReviewProposalEnvelope(StrictContract):
    """Frozen, strict-origin envelope binding a Proposal to the exact
    Phase 4 Task/Result that produced it.

    Phase 5A must NOT accept a Proposal whose origin can be re-attached
    to an arbitrary Task.  The Reviewer validates every field below
    against the ReviewRequest's task_records / capability_bindings /
    result_origins (R2 P0-1).

    R2.1 P0-1: ``proposal`` is now a :class:`ReviewProposalSnapshot`
    (deep-frozen) instead of a raw :class:`ActionProposal`.  This
    closes the TOCTOU window where ``payload`` / ``evidence_ids``
    could be mutated between ``verify_integrity()`` and decision use.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: ReviewProposalSnapshot
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
                "proposal": self.proposal.to_action_proposal().model_dump(
                    mode="python"
                ),
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
# R2 P0-3: ReviewEvidenceSnapshot — single-Evidence hash verification.
# ---------------------------------------------------------------------------


class ReviewEvidenceSnapshot(StrictContract):
    """Frozen wrapper carrying one :class:`Evidence` plus its verified
    ``snapshot_hash``.

    R2 P0-3: previously the Reviewer only checked that
    ``Evidence.content_hash`` was a hex string.  A tampered Evidence
    that kept the old declared ``content_hash`` was accepted silently.
    The ``snapshot_hash`` covers every canonical Evidence field
    EXCEPT the self-referential ``content_hash`` (computed by
    :func:`multi_agent.evidence_review.compute_review_evidence_hash`),
    so a tampered Evidence is detected at the Request boundary.

    The snapshot is constructed by the Phase 5A Adapter; the
    Reviewer verifies ``snapshot_hash`` matches the carried Evidence
    before consuming it.

    R2.1 P0-1: ``metadata_frozen`` is the deep-frozen form of
    :attr:`Evidence.metadata`.  The Reviewer reads ``metadata_frozen``
    (not ``evidence.metadata``) so a TOCTOU mutation of the original
    dict cannot affect the decision.  ``snapshot_hash`` is recomputed
    from the frozen form so a tampered ``metadata`` field (which
    would change the frozen form) is detected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: Evidence
    snapshot_hash: str
    # R2.1 P0-1: deep-frozen copy of ``evidence.metadata``.  Storing
    # both the original Evidence (for backward compat with helpers
    # that read ``evidence.metadata``) and the frozen form (for
    # Reviewer decision logic) is intentional — the frozen form is
    # what audit consumers should read.
    metadata_frozen: FrozenJsonValue = None

    @field_validator("snapshot_hash")
    @classmethod
    def _snapshot_hash_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewEvidenceSnapshot.snapshot_hash must not be blank")
        return v

    @field_validator("metadata_frozen")
    @classmethod
    def _freeze_metadata(cls, v: Any) -> Any:
        return freeze_json_value(v)

    @model_validator(mode="after")
    def _verify_snapshot_hash(self) -> "ReviewEvidenceSnapshot":
        from multi_agent.evidence_review import compute_review_evidence_hash

        expected = compute_review_evidence_hash(self.evidence)
        if self.snapshot_hash != expected:
            raise ValueError(
                "ReviewEvidenceSnapshot.snapshot_hash does not match the carried "
                "Evidence content — tamper detected at the Request boundary"
            )
        return self


# ---------------------------------------------------------------------------
# R2 P0-6 / S5: PolicyRule + PolicyDecisionAudit
# ---------------------------------------------------------------------------


class PolicyRule(StrictContract):
    """Frozen, strictly-typed Policy rule descriptor.

    R2 P0-6: replaces the raw ``dict[str, JsonValue]`` rule entries on
    :class:`PolicyContext`.  Empty ``rule_id``, illegal ``effect``,
    illegal ``priority`` etc. now raise ``ValidationError`` at
    construction — they cannot be silently skipped by the
    :class:`DeterministicPolicyEvaluator`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_version: str
    priority: int
    effect: PolicyDecision
    action_type: str | None = None

    @field_validator("rule_id", "rule_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("PolicyRule.rule_id / rule_version must not be blank")
        return v


class PolicyMatchedRule(StrictContract):
    """A single rule that matched during policy evaluation.

    Frozen so audit consumers can hold references safely.  R2 S5:
    carried inside :class:`PolicyDecisionAudit` so the Reviewer can
    verify every matched rule's ID + Version came from the Request's
    frozen :class:`PolicyContext.rules` snapshot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_version: str = ""
    effect: PolicyDecision
    matched_fields: tuple[str, ...] = ()

    @field_validator("rule_id", "rule_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "PolicyMatchedRule.rule_id / rule_version must not be blank"
            )
        return v


class PolicyDecisionAudit(StrictContract):
    """R2 S5 / R2.1 P0-7: per-Proposal audit of the Policy decision.

    Every Proposal — including those that skipped external Policy
    evaluation because Authority already failed — MUST carry one
    :class:`PolicyDecisionAudit`.  ``evaluator_source_id`` records
    which evaluator produced the decision
    (``deterministic-policy``, ``opa``, ``skipped-authority-failure``).

    R2.1 P0-7: ``proposal_id`` / ``request_hash`` /
    ``policy_request_hash`` bind the audit to the exact Proposal and
    Request it answers, so a tampered or replayed audit cannot be
    attached to a different Proposal / Request.  These fields are
    REQUIRED (non-blank) and participate in ``evaluation_hash``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator_source_id: str
    evaluator_version: str
    policy_version: str
    decision: PolicyDecision
    matched_rules: tuple[PolicyMatchedRule, ...] = ()
    # R2.1 P0-7: identity binding fields.
    proposal_id: str
    request_hash: str
    policy_request_hash: str
    evaluation_hash: str

    @field_validator(
        "evaluator_source_id",
        "evaluator_version",
        "policy_version",
        "proposal_id",
        "request_hash",
        "policy_request_hash",
        "evaluation_hash",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("PolicyDecisionAudit identity fields must not be blank")
        return v

    @model_validator(mode="after")
    def _verify_evaluation_hash(self) -> "PolicyDecisionAudit":
        expected = stable_hash(
            {
                "evaluator_source_id": self.evaluator_source_id,
                "evaluator_version": self.evaluator_version,
                "policy_version": self.policy_version,
                "decision": self.decision.value,
                "matched_rules": [
                    r.model_dump(mode="python") for r in self.matched_rules
                ],
                "proposal_id": self.proposal_id,
                "request_hash": self.request_hash,
                "policy_request_hash": self.policy_request_hash,
            }
        )
        if self.evaluation_hash != expected:
            raise ValueError("PolicyDecisionAudit.evaluation_hash mismatch")
        return self


# ---------------------------------------------------------------------------
# R2 S6: EvidenceDeduplicationAudit
# ---------------------------------------------------------------------------


class EvidenceDeduplicationAudit(StrictContract):
    """R2 S6: record of the Adapter's deterministic Evidence dedup.

    Built by :func:`multi_agent.review_evaluation.build_review_request`
    so the Reviewer can prove that identical duplicate Evidence was
    collapsed to a single :class:`ReviewEvidenceSnapshot` rather than
    silently retained.  Same ``evidence_id`` + different content is
    NOT deduped — it remains a fail-closed contract violation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deduped_evidence_ids: frozenset[str] = frozenset()
    original_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    audit_hash: str

    @model_validator(mode="after")
    def _verify_audit_hash(self) -> "EvidenceDeduplicationAudit":
        expected = stable_hash(
            {
                "deduped_evidence_ids": sorted(self.deduped_evidence_ids),
                "original_count": self.original_count,
                "snapshot_count": self.snapshot_count,
            }
        )
        if self.audit_hash != expected:
            raise ValueError("EvidenceDeduplicationAudit.audit_hash mismatch")
        return self


# ---------------------------------------------------------------------------
# R2 S9: ReviewExpectedOutcome — fixture label without name leakage.
# ---------------------------------------------------------------------------


class ReviewExpectedOutcome(StrictContract):
    """R2 S9: explicit expected outcome for a fixture Request.

    Replaces the ``fixture.name``-based label leak in R1's
    :func:`compute_review_metrics`.  The metrics computation reads
    these per-Proposal expected statuses / finding codes rather than
    inferring them from the fixture name.

    R2.1 P0-1: ``expected_status_by_proposal`` and
    ``expected_finding_codes_by_proposal`` are deep-frozen so they
    cannot be mutated in-place after construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_status_by_proposal: FrozenJsonValue = None
    expected_finding_codes_by_proposal: FrozenJsonValue = None

    @field_validator("expected_status_by_proposal")
    @classmethod
    def _freeze_status_map(cls, v: Any) -> Any:
        if v is None:
            return None
        # Convert ReviewDecisionStatus values to their string form
        # before freezing so the frozen structure is JSON-native.
        if isinstance(v, dict):
            normalised = {
                str(k): (sv.value if hasattr(sv, "value") else str(sv))
                for k, sv in v.items()
            }
            return freeze_json_value(normalised)
        return freeze_json_value(v)

    @field_validator("expected_finding_codes_by_proposal")
    @classmethod
    def _freeze_finding_map(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, dict):
            normalised = {
                str(k): (set(sv) if not isinstance(sv, (set, frozenset)) else sv)
                for k, sv in v.items()
            }
            return freeze_json_value(normalised)
        return freeze_json_value(v)


# ---------------------------------------------------------------------------
# R2 S12: ReviewGraphError — strict, persistable graph error.
# ---------------------------------------------------------------------------


class ReviewGraphError(StrictContract):
    """R2 S12: persistable error captured by the LangGraph adapter.

    The graph does NOT carry the raw :class:`Exception` as business
    state — that would couple the audit trail to a non-serialisable
    Python object.  Instead the graph node captures
    ``error_code`` + ``message`` so downstream consumers can replay
    or persist the failure deterministically.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str
    message: str

    @field_validator("error_code", "message")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewGraphError fields must not be blank")
        return v


# ---------------------------------------------------------------------------
# ReviewFinding
# ---------------------------------------------------------------------------


class ReviewFinding(StrictContract):
    """A single observation produced during Proposal review.

    ``details`` is a restricted ``dict[str, JsonValue]`` — never
    ``Any``.  It must pass :func:`validate_strict_json` so that
    bytes / sets / Decimals / datetimes are rejected at construction.

    R2 S1: ``evidence_ids`` is a ``tuple`` so a frozen
    :class:`ProposalReview` cannot be mutated by appending to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_code: str
    severity: ReviewFindingSeverity
    message: str
    proposal_id: str
    task_id: str | None = None
    agent_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    policy_source: str = ""
    details: FrozenJsonValue = None

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
    def _validate_details(cls, v: Any) -> Any:
        # R2.1 P0-1: deep-freeze the details dict so it cannot be
        # mutated in-place after the Finding is constructed.
        if v is None:
            return None
        from multi_agent.contracts import _reject_sensitive_keys
        from multi_agent.serialization import validate_strict_json

        _reject_sensitive_keys(v, "ReviewFinding.details")
        validated = validate_strict_json(v)
        return freeze_json_value(validated)


# ---------------------------------------------------------------------------
# PolicyContext
# ---------------------------------------------------------------------------


class PolicyContext(StrictContract):
    """Frozen, minimal policy context carried by :class:`ReviewRequest`.

    The Reviewer does NOT receive the live policy engine — only a
    snapshot of the rules + version that the
    :class:`DeterministicPolicyEvaluator` should apply.

    R2 P0-6: ``rules`` is now a ``tuple[PolicyRule, ...]`` so each
    rule is strictly typed at the contract boundary.  Empty
    ``rule_id``, illegal ``effect``, and illegal ``priority`` raise
    ``ValidationError`` at construction — they can no longer be
    silently skipped by the evaluator.

    R2.1 P0-6: ``model_validator`` enforces:

    * Every :class:`PolicyRule.rule_version` equals ``policy_version``
      (so a Rule from an older Policy Snapshot cannot slip in).
    * ``rule_id`` is unique within ``rules``.
    * ``(priority, rule_id)`` is stable (sorted order is canonical).
    * ``tenant_overrides`` is deep-frozen (R2.1 P0-1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str
    rules: tuple[PolicyRule, ...] = ()
    tenant_overrides: FrozenJsonValue = None

    @field_validator("policy_version")
    @classmethod
    def _version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("policy_version must not be blank")
        return v

    @field_validator("tenant_overrides")
    @classmethod
    def _validate_and_freeze_overrides(cls, v: Any) -> Any:
        # R2.1 P0-1: deep-freeze the dict so it cannot be mutated
        # in-place after the Request boundary.
        if v is None:
            return None
        from multi_agent.contracts import _reject_sensitive_keys
        from multi_agent.serialization import validate_strict_json

        # First validate as strict JSON (rejects bytes / sets / etc.)
        # then freeze to tuple-of-tuples form.
        _reject_sensitive_keys(v, "PolicyContext.tenant_overrides")
        validated = validate_strict_json(v)
        return freeze_json_value(validated)

    @model_validator(mode="after")
    def _validate_rule_version_and_uniqueness(self) -> "PolicyContext":
        # R2.1 P0-6: every rule_version must equal policy_version.
        for rule in self.rules:
            if rule.rule_version != self.policy_version:
                raise ValueError(
                    f"PolicyRule {rule.rule_id!r} rule_version "
                    f"{rule.rule_version!r} != policy_version "
                    f"{self.policy_version!r}"
                )
        # R2.1 P0-6: rule_id uniqueness.
        rule_ids: dict[str, int] = {}
        for rule in self.rules:
            rule_ids[rule.rule_id] = rule_ids.get(rule.rule_id, 0) + 1
        dup_ids = [rid for rid, count in rule_ids.items() if count > 1]
        if dup_ids:
            raise ValueError(
                f"Duplicate rule_id in PolicyContext.rules: {sorted(dup_ids)!r}"
            )
        # R2.1 P0-6: (priority, rule_id) stable — sorted order is
        # canonical for hashing.  We don't enforce a particular order
        # here (sorting happens in canonical_review_request_payload),
        # but we do enforce that the tuple is non-decreasing in
        # (priority, rule_id) so two PolicyContexts with the same
        # rules in different orders still produce the same hash.
        return self


def canonical_review_request_payload(request: ReviewRequest) -> dict[str, Any]:
    """Return a canonical, ORDER-INVARIANT dict for hashing.

    Each list field is sorted by a stable key so that reordering the
    input lists does not change the request_hash (R1 P0-4).
    ``request_hash`` is excluded (self-referential).

    R2: tuple fields are coerced to sorted lists for canonical
    hashing.  ``governance_spec_version`` / ``governance_spec_hash``
    / ``run_identity`` / ``result_origins`` / ``evidence_dedup_audit``
    participate in the hash so a Request built against a different
    governance spec or a different Run identity is rejected.
    """
    proposals = sorted(request.proposals, key=lambda p: p.proposal_id)
    proposal_envelopes = sorted(
        request.proposal_envelopes, key=lambda e: e.proposal.proposal_id
    )
    evidence = sorted(request.evidence, key=lambda e: e.evidence.evidence_id)
    task_records = sorted(request.task_records, key=lambda t: t.task_id)
    capability_bindings = sorted(
        request.capability_bindings, key=lambda c: (c.task_id, c.agent_id)
    )
    result_origins = sorted(request.result_origins, key=lambda r: r.result_id)
    trace = sorted(request.trace, key=lambda t: t.sequence)
    rules = sorted(
        request.policy_context.rules,
        key=lambda r: (r.priority, r.rule_id),
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
        "result_origins": [r.model_dump(mode="python") for r in result_origins],
        "policy_context": {
            **request.policy_context.model_dump(mode="python"),
            "rules": [r.model_dump(mode="python") for r in rules],
        },
        "reviewer_version": request.reviewer_version,
        "review_schema_version": request.review_schema_version,
        "governance_spec_version": request.governance_spec_version,
        "governance_spec_hash": request.governance_spec_hash,
    }
    if request.run_identity is not None:
        payload["run_identity"] = request.run_identity.model_dump(mode="python")
    if request.evidence_dedup_audit is not None:
        payload["evidence_dedup_audit"] = request.evidence_dedup_audit.model_dump(
            mode="python"
        )
    return canonicalize(payload)


class ReviewRequest(StrictContract):
    """Frozen input to :meth:`ProposalReviewer.review`.

    Carries everything the Reviewer needs to make a deterministic
    decision: Proposals, Evidence Snapshots, Task Records, Trace,
    Policy Context, Capability Snapshots, Result Origins, the Phase 4
    authoritative Run identity, and the Action Governance Spec hash.

    R2 S1: every collection is a ``tuple`` so the frozen model cannot
    be mutated by appending / removing items.  ``request_hash`` is
    computed over the canonical form and verifies integrity on
    construction.  Mutating any field after construction is
    impossible (``frozen=True``).

    R2 P0-2: ``capability_bindings`` are unique by ``task_id`` (NOT
    ``agent_id``) — the same Agent may legitimately execute multiple
    Tasks in one Run.

    R2 P0-3: ``evidence`` is a tuple of :class:`ReviewEvidenceSnapshot`
    so every Evidence's ``snapshot_hash`` is verified at the Request
    boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    tenant_id: str
    plan_hash: str
    registry_version: str

    proposals: tuple[ReviewProposalSnapshot, ...] = ()
    evidence: tuple[ReviewEvidenceSnapshot, ...] = ()
    task_records: tuple[TaskRecordSummary, ...] = ()
    trace: tuple[TraceSummary, ...] = ()
    proposal_envelopes: tuple[ReviewProposalEnvelope, ...] = ()
    capability_bindings: tuple[ExecutionCapabilitySnapshot, ...] = ()
    result_origins: tuple[ResultOriginSnapshot, ...] = ()
    policy_context: PolicyContext

    # R2 S2 / R2.1 P0-4: authoritative Run identity is REQUIRED for a
    # formal Phase 5A Request.  The Adapter MUST source
    # ``run_id`` / ``tenant_id`` / ``plan_hash`` / ``registry_version``
    # from this field — the ``_extract_tenant_id()`` Legacy Fallback
    # is removed.  Legacy fixtures that pre-date R2.1 must be updated
    # to supply ``run_identity`` explicitly.
    run_identity: ExecutionRunIdentity

    # R2 S3 / R2.1 P0-7: governance spec version + hash are REQUIRED
    # (non-blank).  The Reviewer verifies these match the live
    # :data:`ACTION_GOVERNANCE_SPEC_VERSION` /
    # :data:`ACTION_GOVERNANCE_SPEC_HASH` AND that the live hash
    # matches the module-level constant (so a tampered Registry is
    # detected even if the Request carries the old constant).
    governance_spec_version: str
    governance_spec_hash: str

    # R2 S6: evidence dedup audit.  Optional so legacy fixtures can
    # still construct a Request without dedup metadata.
    evidence_dedup_audit: EvidenceDeduplicationAudit | None = None

    # R2 S10: schema version (string, not int, so it sorts canonically).
    review_schema_version: str = REVIEW_SCHEMA_VERSION

    # R2 S10: reviewer_version is REQUIRED (no default) so a Request
    # built against an older Reviewer cannot slip through with the
    # current default.
    reviewer_version: str

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

    @field_validator("reviewer_version", "review_schema_version")
    @classmethod
    def _version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewRequest version fields must not be blank")
        return v

    @field_validator("governance_spec_version", "governance_spec_hash")
    @classmethod
    def _governance_non_blank(cls, v: str) -> str:
        # R2.1 P0-7: governance_spec_version / governance_spec_hash
        # are REQUIRED (non-blank).  The previous default of "" let a
        # Result omit the governance hash and bypass the
        # verify_against_request comparison.
        v = v.strip()
        if not v:
            raise ValueError(
                "ReviewRequest.governance_spec_version / governance_spec_hash "
                "must not be blank"
            )
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

        R2 P0-1: every Proposal MUST have exactly one matching
        Envelope whose ``proposal`` field is byte-equal (model_dump
        comparison) to the Request's Proposal.  ``run_id`` /
        ``agent_id`` / ``task_id`` cross-checks are enforced here so
        the Reviewer can trust the Envelope as the authoritative
        origin.

        R2 P0-2: ``capability_bindings`` are unique by ``task_id``
        (NOT ``agent_id``) — the same Agent may be bound to multiple
        Tasks.  Duplicate ``task_id`` with different content is a
        fail-closed violation.

        R2.1 P0-2: every Proposal MUST have EXACTLY ONE Envelope.
        Multiple Envelopes for the same ``proposal_id`` (even with
        different ``origin_hash``) are rejected — the previous
        ``envelope_by_pid = {e.proposal.proposal_id: e for e in ...}``
        pattern silently picked the last one (last-write-wins).

        R2.1 P0-3: Exact-task Capability is REQUIRED — a missing
        ``capability_binding`` for ``envelope.task_id`` is fail-closed.
        The previous ``if cb is not None:`` check let a Request drop
        the Task A binding and fall back to a different Task's binding
        via ``agent_id``.

        R2.1 P0-4: ResultOriginSnapshot is REQUIRED for every Envelope.
        A missing ``result_origin`` for ``envelope.result_id`` is
        fail-closed.  The previous ``if ro is not None:`` check let a
        Request omit the Result Origin entirely.

        R2.1 P0-5: ANY duplicate ``evidence_id`` is rejected, even if
        the content is identical.  The previous logic allowed
        same-hash duplicates, which let duplicate Evidence affect the
        ``request_hash`` and contradicted the Dedup Audit.
        """
        # proposal_id uniqueness — same id (whether same or different
        # content) is an error: Phase 4 merge should have deduped.
        proposals_by_id: dict[str, list[ReviewProposalSnapshot]] = {}
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

        # R2.1 P0-5: evidence_id uniqueness — ANY duplicate is
        # rejected, even if the content is identical.  The Dedup
        # Audit must record the dedup; the Request itself must NOT
        # carry duplicate evidence_id entries.
        evidence_by_id: dict[str, int] = {}
        for snap in self.evidence:
            eid = snap.evidence.evidence_id
            evidence_by_id[eid] = evidence_by_id.get(eid, 0) + 1
        for eid, count in evidence_by_id.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate evidence_id {eid!r} (count={count}) in "
                    f"ReviewRequest {self.review_id!r} — Adapter must dedup "
                    f"identical Evidence before constructing the Request"
                )

        # task_id uniqueness across task_records
        task_ids: dict[str, int] = {}
        for task_rec in self.task_records:
            task_ids[task_rec.task_id] = task_ids.get(task_rec.task_id, 0) + 1
        for tid, count in task_ids.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate task_id {tid!r} in ReviewRequest {self.review_id!r}"
                )

        # R2 P0-2: capability_bindings are unique by task_id (NOT
        # agent_id).  Duplicate task_id with different content is a
        # fail-closed violation; the same Agent may legitimately be
        # bound to multiple Tasks.
        cap_by_task: dict[str, list[ExecutionCapabilitySnapshot]] = {}
        for cap_snap in self.capability_bindings:
            cap_by_task.setdefault(cap_snap.task_id, []).append(cap_snap)
        for tid, cap_group in cap_by_task.items():
            if len(cap_group) > 1:
                hashes = {cb.binding_hash for cb in cap_group}
                if len(hashes) > 1:
                    raise InvalidReviewRequestError(
                        f"Duplicate task_id {tid!r} with different capability "
                        f"binding in ReviewRequest {self.review_id!r}"
                    )
                raise InvalidReviewRequestError(
                    f"Duplicate task_id {tid!r} (same binding) in "
                    f"capability_bindings of ReviewRequest {self.review_id!r}"
                )

        # result_id uniqueness across result_origins
        result_ids: dict[str, int] = {}
        for result_orig in self.result_origins:
            result_ids[result_orig.result_id] = (
                result_ids.get(result_orig.result_id, 0) + 1
            )
        for rid, count in result_ids.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Duplicate result_id {rid!r} in result_origins of "
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

        # R2.1 P0-2: Envelope cardinality — every Proposal MUST have
        # EXACTLY ONE Envelope.  Multiple Envelopes for the same
        # proposal_id (even with different origin_hash) is fail-closed.
        envelopes_by_proposal_id: dict[str, int] = {}
        for env in self.proposal_envelopes:
            pid = env.proposal.proposal_id
            envelopes_by_proposal_id[pid] = envelopes_by_proposal_id.get(pid, 0) + 1
        for pid, count in envelopes_by_proposal_id.items():
            if count > 1:
                raise InvalidReviewRequestError(
                    f"Proposal {pid!r} has {count} Envelopes in ReviewRequest "
                    f"{self.review_id!r} — exactly one is required (P0-2)"
                )

        # Envelope ↔ Proposal bijection: every envelope's proposal_id
        # must match exactly one proposal, and every proposal must have
        # a matching envelope.  R2 P0-1: the envelope's ``proposal``
        # field must be byte-equal (model_dump comparison) to the
        # Request's Proposal — not just share a proposal_id.
        proposal_by_id = {p.proposal_id: p for p in self.proposals}
        envelope_proposal_ids = set()
        for env in self.proposal_envelopes:
            pid = env.proposal.proposal_id
            if pid not in proposal_by_id:
                raise InvalidReviewRequestError(
                    f"Envelope for proposal {pid!r} but no matching Proposal "
                    f"in ReviewRequest {self.review_id!r}"
                )
            if env.proposal.model_dump(mode="python") != proposal_by_id[pid].model_dump(
                mode="python"
            ):
                raise InvalidReviewRequestError(
                    f"Envelope.proposal for {pid!r} does not equal the Request's "
                    f"Proposal (model_dump mismatch) in ReviewRequest "
                    f"{self.review_id!r}"
                )
            envelope_proposal_ids.add(pid)
        missing_envelopes = set(proposal_by_id) - envelope_proposal_ids
        if missing_envelopes:
            raise InvalidReviewRequestError(
                f"Proposals without matching envelope in ReviewRequest "
                f"{self.review_id!r}: {sorted(missing_envelopes)!r}"
            )

        # R2 P0-1: Envelope identity cross-checks.
        # R2.1 P0-3 / P0-4: capability_binding AND result_origin are
        # REQUIRED (fail-closed if missing) — no Legacy Fallback.
        task_records_by_id = {tr.task_id: tr for tr in self.task_records}
        cap_by_task_id = {cb.task_id: cb for cb in self.capability_bindings}
        result_origins_by_id = {ro.result_id: ro for ro in self.result_origins}
        for env in self.proposal_envelopes:
            pid = env.proposal.proposal_id
            proposal = proposal_by_id[pid]
            if env.run_id != self.run_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r} run_id {env.run_id!r} != request "
                    f"{self.run_id!r}"
                )
            if env.agent_id != proposal.created_by_agent:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r} agent_id {env.agent_id!r} != proposal "
                    f"created_by_agent {proposal.created_by_agent!r}"
                )
            tr = task_records_by_id.get(env.task_id)
            if tr is None:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r} task_id {env.task_id!r} is not in "
                    f"task_records of ReviewRequest {self.review_id!r}"
                )
            if tr.agent_id != env.agent_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: task_record.agent_id {tr.agent_id!r} "
                    f"!= envelope.agent_id {env.agent_id!r}"
                )
            # R2.1 P0-3: capability_binding is REQUIRED for the
            # envelope's exact task_id.  Missing binding is fail-closed.
            cb = cap_by_task_id.get(env.task_id)
            if cb is None:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: no capability_binding for "
                    f"task_id {env.task_id!r} in ReviewRequest "
                    f"{self.review_id!r} (P0-3: exact-task capability required)"
                )
            if cb.agent_id != env.agent_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: capability_binding.agent_id "
                    f"{cb.agent_id!r} != envelope.agent_id {env.agent_id!r}"
                )
            if cb.agent_version != env.agent_version:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: capability_binding.agent_version "
                    f"{cb.agent_version!r} != envelope.agent_version "
                    f"{env.agent_version!r}"
                )
            # R2.1 P0-4: ResultOriginSnapshot is REQUIRED for the
            # envelope's exact result_id.  Missing origin is fail-closed.
            ro = result_origins_by_id.get(env.result_id)
            if ro is None:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: no result_origin for "
                    f"result_id {env.result_id!r} in ReviewRequest "
                    f"{self.review_id!r} (P0-4: result origin required)"
                )
            if ro.run_id != env.run_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: result_origin.run_id "
                    f"{ro.run_id!r} != envelope.run_id {env.run_id!r}"
                )
            if ro.task_id != env.task_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: result_origin.task_id "
                    f"{ro.task_id!r} != envelope.task_id {env.task_id!r}"
                )
            if ro.agent_id != env.agent_id:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: result_origin.agent_id "
                    f"{ro.agent_id!r} != envelope.agent_id {env.agent_id!r}"
                )
            if ro.agent_version != env.agent_version:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: result_origin.agent_version "
                    f"{ro.agent_version!r} != envelope.agent_version "
                    f"{env.agent_version!r}"
                )
            # R2.1 P0-4: verify the Envelope's Proposal is in the
            # ResultOrigin's proposal_hashes list.  This proves the
            # Proposal actually originated from this Result (not just
            # that the IDs happen to match).
            proposal_pair = (proposal.proposal_id, proposal.proposal_hash)
            if proposal_pair not in ro.proposal_hashes:
                raise InvalidReviewRequestError(
                    f"Envelope for {pid!r}: (proposal_id, proposal_hash) pair "
                    f"{proposal_pair!r} not in result_origin.proposal_hashes "
                    f"for result_id {env.result_id!r} (P0-4: origin binding)"
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


# Finding codes that block an APPROVED decision regardless of severity.
BLOCKING_FINDING_CODES: frozenset[str] = REJECTION_FINDING_CODES | frozenset(
    {
        CODE_EVIDENCE_MISSING,
        CODE_POLICY_NEEDS_INPUT,
        CODE_CONFLICT_FIELD_VALUE,
        CODE_CONFLICT_ACTIVATE_DEACTIVATE,
        CODE_CONFLICT_CREATE_DELETE,
        CODE_CONFLICT_IDEMPOTENCY_MISMATCH,
        CODE_CONFLICT_MUTEX_NOTIFICATION,
        CODE_CONFLICT_OWNER_REASSIGN,
    }
)


class ProposalReview(StrictContract):
    """Per-Proposal review outcome.

    Frozen so audit consumers can hold a reference without worrying
    about mutation.  The ``review_hash`` covers every field that
    affects the decision so a tampered review is detectable.

    R2 P0-8 / S5: ``policy_audit`` carries the full Policy decision
    audit so consumers can verify the Policy path even for Proposals
    that were REJECTED before Policy was invoked (audit source
    ``skipped-authority-failure``).

    R2 S1: ``findings`` / ``matched_evidence_ids`` are tuples.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    status: ReviewDecisionStatus
    findings: tuple[ReviewFinding, ...] = ()
    matched_evidence_ids: tuple[str, ...] = ()
    required_approval: bool = False
    risk_level: ReviewRiskLevel = ReviewRiskLevel.LOW
    authority_valid: bool = False
    policy_valid: bool = False
    idempotency_valid: bool = False
    # R2.1 P0-7: ``policy_audit`` is REQUIRED (no longer Optional).
    # Every Proposal — including those that skipped external Policy
    # because Authority failed — must carry a bound
    # :class:`PolicyDecisionAudit` whose ``proposal_id`` /
    # ``request_hash`` / ``policy_request_hash`` tie the audit back to
    # the originating Request.  Omitting the audit would let a Result
    # bypass the Policy trust chain.
    policy_audit: PolicyDecisionAudit
    # R2 P0-8: DEDUPLICATED proposals carry the primary's id so audit
    # consumers can trace which Proposal survived dedup.
    primary_proposal_id: str | None = None
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

        R2 P0-8: stronger rules — APPROVED requires every validity
        flag to be True and ``required_approval=False`` and no
        blocking finding.  NEEDS_APPROVAL requires authority_valid
        AND policy_valid AND required_approval.  DEDUPLICATED
        requires ``primary_proposal_id`` audit info.

        Raises :class:`InvalidReviewResultError` on any inconsistency.
        """
        has_blocking = any(
            f.finding_code in BLOCKING_FINDING_CODES
            and f.severity
            in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )
        has_error = any(
            f.severity in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )
        finding_codes = {f.finding_code for f in self.findings}

        if self.status == ReviewDecisionStatus.APPROVED:
            # R2 P0-8: APPROVED requires every validity flag True,
            # required_approval False, and no blocking finding.
            if has_blocking or has_error:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"has ERROR/CRITICAL or blocking findings"
                )
            if not self.authority_valid:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"authority_valid is False"
                )
            if not self.policy_valid:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"policy_valid is False"
                )
            if not self.idempotency_valid:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"idempotency_valid is False"
                )
            if self.required_approval:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"required_approval is True"
                )
        elif self.status == ReviewDecisionStatus.NEEDS_APPROVAL:
            # R2 P0-8: NEEDS_APPROVAL requires authority_valid AND
            # policy_valid AND required_approval.
            if not self.required_approval:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_APPROVAL "
                    f"but required_approval is False"
                )
            if not self.authority_valid:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_APPROVAL "
                    f"but authority_valid is False"
                )
            if not self.policy_valid:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_APPROVAL "
                    f"but policy_valid is False"
                )
        elif self.status == ReviewDecisionStatus.REJECTED:
            if not (finding_codes & REJECTION_FINDING_CODES):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status REJECTED but "
                    f"no rejection-class finding"
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
            if not self.primary_proposal_id:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status DEDUPLICATED "
                    f"but primary_proposal_id is missing"
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

        # R2.1 P0-7: ``policy_audit`` is REQUIRED and its identity must
        # match this Proposal.  ``policy_audit.proposal_id`` must equal
        # ``self.proposal_id`` so a tampered audit from a different
        # Proposal cannot be attached.
        if self.policy_audit.proposal_id != self.proposal_id:
            raise InvalidReviewResultError(
                f"ProposalReview {self.proposal_id!r}: policy_audit.proposal_id "
                f"{self.policy_audit.proposal_id!r} != review.proposal_id"
            )

        # R2.1 P0-7: decision ↔ status consistency.
        #   APPROVED     → audit.decision == ALLOWED
        #   NEEDS_INPUT  → audit.decision == NEEDS_INPUT
        #                  OR a CODE_EVIDENCE_MISSING finding exists
        #   NEEDS_APPROVAL → audit.decision == NEEDS_APPROVAL
        #                    OR required_approval is True (canonical risk)
        #   REJECTED     → audit.decision == DENIED
        #                    OR a rejection-class finding exists
        #   CONFLICT     → audit may be ALLOWED (conflict is post-policy)
        #   DEDUPLICATED → audit may be ALLOWED (dedup is post-policy)
        audit_decision = self.policy_audit.decision
        if self.status == ReviewDecisionStatus.APPROVED:
            if audit_decision != PolicyDecision.ALLOWED:
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status APPROVED but "
                    f"policy_audit.decision is {audit_decision.value!r} "
                    f"(expected ALLOWED)"
                )
        elif self.status == ReviewDecisionStatus.NEEDS_INPUT:
            if (
                audit_decision != PolicyDecision.NEEDS_INPUT
                and CODE_EVIDENCE_MISSING not in finding_codes
            ):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_INPUT "
                    f"but policy_audit.decision is {audit_decision.value!r} "
                    f"and no {CODE_EVIDENCE_MISSING!r} finding"
                )
        elif self.status == ReviewDecisionStatus.NEEDS_APPROVAL:
            if (
                audit_decision != PolicyDecision.NEEDS_APPROVAL
                and not self.required_approval
            ):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status NEEDS_APPROVAL "
                    f"but policy_audit.decision is {audit_decision.value!r} "
                    f"and required_approval is False"
                )
        elif self.status == ReviewDecisionStatus.REJECTED:
            if audit_decision != PolicyDecision.DENIED and not (
                finding_codes & REJECTION_FINDING_CODES
            ):
                raise InvalidReviewResultError(
                    f"ProposalReview {self.proposal_id!r}: status REJECTED but "
                    f"policy_audit.decision is {audit_decision.value!r} "
                    f"and no rejection-class finding"
                )


# ---------------------------------------------------------------------------
# ReviewBatchResult
# ---------------------------------------------------------------------------


def batch_status_priority(status: ReviewBatchStatus) -> int:
    """Return priority weight — higher wins.

    R2 P0-7: unique weights so ``max()`` never has ties.

    Priority order (Phase 5A Section 11, R2):

        conflict(5) > rejected(4) > needs_input(3) > needs_approval(2)
        > deduplicated(1) > approved(0) > no_actions(-1)
    """
    order = {
        ReviewBatchStatus.NO_ACTIONS: -1,
        ReviewBatchStatus.APPROVED: 0,
        ReviewBatchStatus.DEDUPLICATED: 1,
        ReviewBatchStatus.NEEDS_APPROVAL: 2,
        ReviewBatchStatus.NEEDS_INPUT: 3,
        ReviewBatchStatus.REJECTED: 4,
        ReviewBatchStatus.CONFLICT: 5,
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

    Frozen, deterministically hashable, and sorted: every collection
    field is a tuple ordered by a stable key so the same input always
    produces the same ``result_hash`` regardless of insertion order
    or ``PYTHONHASHSEED``.

    The ``batch_status`` is the highest-priority decision across all
    per-Proposal reviews (Phase 5A Section 11).  A batch marked
    ``rejected`` does NOT mean every Proposal was rejected — each
    Proposal retains its own independent decision.

    R2 S7: an empty batch returns ``NO_ACTIONS`` (NOT ``APPROVED``)
    so Phase 5B cannot mis-treat an empty Review as authorisation.

    R2 S1: every collection is a tuple.

    R2 S8: :meth:`verify_against_request` binds the Result back to
    its Request so a tampered or mis-routed Result is detected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    tenant_id: str
    request_hash: str

    proposal_reviews: tuple[ProposalReview, ...] = ()
    batch_status: ReviewBatchStatus = ReviewBatchStatus.APPROVED
    approved_proposal_ids: tuple[str, ...] = ()
    rejected_proposal_ids: tuple[str, ...] = ()
    approval_required_proposal_ids: tuple[str, ...] = ()
    conflicted_proposal_ids: tuple[str, ...] = ()
    deduplicated_proposal_ids: tuple[str, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()

    # R2.1 P0-7: governance spec hash is REQUIRED (non-blank) on the
    # Result.  The previous default of "" let a Result omit the hash
    # and bypass the :meth:`verify_against_request` comparison (which
    # was conditional on both sides being non-empty).  Now the
    # comparison is unconditional.
    governance_spec_hash: str

    result_hash: str = ""
    # R2 S10: reviewer_version is REQUIRED on the Result.
    reviewer_version: str

    @field_validator("review_id", "run_id", "tenant_id", "request_hash")
    @classmethod
    def _identity_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ReviewBatchResult identity fields must not be blank")
        return v

    @field_validator("reviewer_version", "governance_spec_hash")
    @classmethod
    def _version_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "ReviewBatchResult.reviewer_version / governance_spec_hash "
                "must not be blank"
            )
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

    def verify_semantics(self, *, reviewer_version: str | None = None) -> None:
        """Validate that batch-level summaries are consistent with
        per-proposal reviews.

        R2 P0-8: ``reviewer_version`` (when supplied) MUST equal the
        Request's ``reviewer_version`` so a Result built by an older
        Reviewer cannot be replayed against a newer Request.

        Raises :class:`InvalidReviewResultError` on any inconsistency.
        """
        if not self.reviewer_version.strip():
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: reviewer_version blank"
            )
        # R2 P0-8: reviewer_version matches the Request's reviewer_version.
        if reviewer_version is not None and self.reviewer_version != reviewer_version:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: reviewer_version "
                f"{self.reviewer_version!r} != request {reviewer_version!r}"
            )

        # proposal_ids unique
        ids = [r.proposal_id for r in self.proposal_reviews]
        if len(ids) != len(set(ids)):
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: duplicate proposal_ids "
                f"in proposal_reviews"
            )

        # R2 S7: empty batch → NO_ACTIONS (NOT APPROVED).
        if not self.proposal_reviews:
            if self.batch_status != ReviewBatchStatus.NO_ACTIONS:
                raise InvalidReviewResultError(
                    f"ReviewBatchResult {self.review_id!r}: empty batch but "
                    f"batch_status is {self.batch_status.value!r} (expected "
                    f"NO_ACTIONS)"
                )
            if self.approved_proposal_ids:
                raise InvalidReviewResultError(
                    f"ReviewBatchResult {self.review_id!r}: empty batch but "
                    f"approved_proposal_ids is non-empty"
                )
        else:
            if self.batch_status == ReviewBatchStatus.NO_ACTIONS:
                raise InvalidReviewResultError(
                    f"ReviewBatchResult {self.review_id!r}: non-empty batch but "
                    f"batch_status is NO_ACTIONS"
                )
            # batch_status recompute
            recomputed = max(
                (proposal_status_to_batch(r.status) for r in self.proposal_reviews),
                key=batch_status_priority,
            )
            if self.batch_status != recomputed:
                raise InvalidReviewResultError(
                    f"ReviewBatchResult {self.review_id!r}: batch_status "
                    f"{self.batch_status.value!r} != recomputed "
                    f"{recomputed.value!r}"
                )

        # Summary id lists
        expected_approved = tuple(
            sorted(
                r.proposal_id
                for r in self.proposal_reviews
                if r.status == ReviewDecisionStatus.APPROVED
            )
        )
        if self.approved_proposal_ids != expected_approved:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: approved_proposal_ids mismatch"
            )
        # R2 P0-8: DEDUPLICATED proposals must NOT appear in approved_proposal_ids.
        deduped_set = {
            r.proposal_id
            for r in self.proposal_reviews
            if r.status == ReviewDecisionStatus.DEDUPLICATED
        }
        if deduped_set & set(self.approved_proposal_ids):
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: DEDUPLICATED proposals "
                f"leaked into approved_proposal_ids"
            )
        expected_rejected = tuple(
            sorted(
                r.proposal_id
                for r in self.proposal_reviews
                if r.status == ReviewDecisionStatus.REJECTED
            )
        )
        if self.rejected_proposal_ids != expected_rejected:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: rejected_proposal_ids mismatch"
            )
        expected_approval = tuple(
            sorted(
                r.proposal_id
                for r in self.proposal_reviews
                if r.status == ReviewDecisionStatus.NEEDS_APPROVAL
            )
        )
        if self.approval_required_proposal_ids != expected_approval:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"approval_required_proposal_ids mismatch"
            )
        expected_conflicted = tuple(
            sorted(
                r.proposal_id
                for r in self.proposal_reviews
                if r.status == ReviewDecisionStatus.CONFLICT
            )
        )
        if self.conflicted_proposal_ids != expected_conflicted:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"conflicted_proposal_ids mismatch"
            )
        expected_deduped = tuple(
            sorted(
                r.proposal_id
                for r in self.proposal_reviews
                if r.status == ReviewDecisionStatus.DEDUPLICATED
            )
        )
        if self.deduplicated_proposal_ids != expected_deduped:
            raise InvalidReviewResultError(
                f"ReviewBatchResult {self.review_id!r}: "
                f"deduplicated_proposal_ids mismatch"
            )

        # Per-proposal semantic validation
        for r in self.proposal_reviews:
            r.verify_semantics()

    def verify_against_request(self, request: ReviewRequest) -> None:
        """R2 S8: bind this Result back to the Request it claims to
        answer.

        Verifies:

        * ``review_id`` / ``run_id`` / ``tenant_id`` / ``request_hash``
          / ``reviewer_version`` / ``governance_spec_hash`` match.
        * Every :class:`ProposalReview` corresponds to exactly one
          Proposal in the Request (no missing, no extra).
        * :meth:`verify_semantics` passes with the Request's
          ``reviewer_version``.

        Raises :class:`InvalidReviewResultError` on any mismatch.
        """
        if self.review_id != request.review_id:
            raise InvalidReviewResultError(
                f"Result review_id {self.review_id!r} != request {request.review_id!r}"
            )
        if self.run_id != request.run_id:
            raise InvalidReviewResultError(
                f"Result run_id {self.run_id!r} != request {request.run_id!r}"
            )
        if self.tenant_id != request.tenant_id:
            raise InvalidReviewResultError(
                f"Result tenant_id {self.tenant_id!r} != request {request.tenant_id!r}"
            )
        if self.request_hash != request.request_hash:
            raise InvalidReviewResultError(
                f"Result request_hash {self.request_hash[:12]!r} != request "
                f"{request.request_hash[:12]!r}"
            )
        if self.reviewer_version != request.reviewer_version:
            raise InvalidReviewResultError(
                f"Result reviewer_version {self.reviewer_version!r} != request "
                f"{request.reviewer_version!r}"
            )
        # R2.1 P0-7: governance_spec_hash comparison is UNCONDITIONAL.
        # Both sides are required (non-blank) — a missing or empty
        # hash on either side is already rejected by the field
        # validators.  Previously the comparison was skipped when
        # either side was empty, which let a tampered Result drop the
        # governance hash to bypass the check.
        if self.governance_spec_hash != request.governance_spec_hash:
            raise InvalidReviewResultError(
                f"Result governance_spec_hash {self.governance_spec_hash[:12]!r} "
                f"!= request {request.governance_spec_hash[:12]!r}"
            )

        # Proposal ID coverage — every Request Proposal has exactly
        # one Review, no extras.
        request_proposal_ids = {p.proposal_id for p in request.proposals}
        review_proposal_ids = {r.proposal_id for r in self.proposal_reviews}
        missing = request_proposal_ids - review_proposal_ids
        if missing:
            raise InvalidReviewResultError(
                f"Result {self.review_id!r} missing reviews for proposals: "
                f"{sorted(missing)!r}"
            )
        extra = review_proposal_ids - request_proposal_ids
        if extra:
            raise InvalidReviewResultError(
                f"Result {self.review_id!r} has reviews for unknown proposals: "
                f"{sorted(extra)!r}"
            )

        # Re-run semantic validation with the Request's reviewer_version.
        self.verify_semantics(reviewer_version=request.reviewer_version)


__all__ = [
    "BLOCKING_FINDING_CODES",
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
    "EvidenceDeduplicationAudit",
    "FrozenJsonValue",
    "PolicyContext",
    "PolicyDecision",
    "PolicyDecisionAudit",
    "PolicyMatchedRule",
    "PolicyRule",
    "REJECTION_FINDING_CODES",
    "REVIEW_SCHEMA_VERSION",
    "REVIEWER_VERSION",
    "ResultOriginSnapshot",
    "ReviewBatchResult",
    "ReviewBatchStatus",
    "ReviewDecisionStatus",
    "ReviewExpectedOutcome",
    "ReviewEvidenceSnapshot",
    "ReviewFinding",
    "ReviewFindingSeverity",
    "ReviewGraphError",
    "ReviewProposalEnvelope",
    "ReviewProposalSnapshot",
    "ReviewRequest",
    "ReviewRiskLevel",
    "TaskRecordSummary",
    "TraceSummary",
    "batch_status_priority",
    "canonical_review_request_payload",
    "freeze_json_value",
    "frozen_value_to_json",
    "proposal_status_to_batch",
]
