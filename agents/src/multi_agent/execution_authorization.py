"""Phase 5B — Execution Authorization contract.

The :class:`ExecutionAuthorization` is the cryptographic binding between
a reviewed :class:`ProposalReview` and the actual adapter call.  It
carries every hash that must match before the executor may invoke an
adapter:

* ``review_request_hash`` / ``review_result_hash`` — bind to the Review.
* ``proposal_review_hash`` — bind to the exact per-Proposal decision.
* ``proposal_snapshot_hash`` / ``proposal_origin_hash`` — bind to the
  frozen Proposal content and its Phase 4 origin.
* ``governance_spec_hash`` — bind to the live governance registry.
* ``adapter_registry_hash`` — bind to the adapter registry snapshot.
* ``authorization_hash`` — bind the :class:`ActionExecutionReceipt`
  back to this authorization (single-use).

Any mismatch is fail-closed (Phase 5B Section 7).
"""

from __future__ import annotations

from enum import StrEnum
from hmac import compare_digest

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.contracts import StrictContract
from multi_agent.review_contracts import (
    ProposalReview,
    ReviewBatchResult,
    ReviewRequest,
    ReviewRiskLevel,
)
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExecutionStatus(StrEnum):
    """Lifecycle of a single action execution.

    Key invariant (Phase 5B): ``SUCCEEDED`` requires ``executed=True``,
    ``FAILED`` requires ``executed=False``, and ``UNKNOWN`` requires
    ``executed=None`` — they are NEVER interchangeable.

    ``DRY_RUN_SUCCEEDED`` — the action was executed in dry-run mode;
    ``executed=False`` because NO real side-effect was produced.  This
    is NEVER equivalent to ``SUCCEEDED`` (P0-1).
    """

    NOT_AUTHORIZED = "not_authorized"
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    DRY_RUN_SUCCEEDED = "dry_run_succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    DEDUPLICATED = "deduplicated"


class BatchExecutionStatus(StrEnum):
    """Aggregate status across a batch of action executions.

    ``NO_ACTIONS`` — the Review produced no executable Proposals
    (never equivalent to ``SUCCEEDED``).
    ``BLOCKED`` — execution could not even start (e.g. governance
    mismatch, kill switch).
    ``PENDING_APPROVAL`` — at least one Proposal is awaiting approval.
    ``PARTIAL_SUCCESS`` — at least one SUCCEEDED and at least one
    non-SUCCEEDED terminal.
    ``SUCCEEDED`` — every action SUCCEEDED.
    ``FAILED`` — at least one FAILED and no UNKNOWN.
    ``UNKNOWN`` — at least one UNKNOWN outcome (fail-closed: requires
    human intervention, never auto-retried).
    ``CANCELLED`` — the run was cancelled before completion.
    """

    NO_ACTIONS = "no_actions"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"
    PARTIAL_SUCCESS = "partial_success"
    SUCCEEDED = "succeeded"
    DRY_RUN_COMPLETED = "dry_run_completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


# Unique priority weights — highest wins (Phase 5B Section 22).
# UNKNOWN(8) > FAILED(7) > CANCELLED(6) > PARTIAL_SUCCESS(5) >
# PENDING_APPROVAL(4) > BLOCKED(3) > SUCCEEDED(2) > DRY_RUN_COMPLETED(1)
# > NO_ACTIONS(0).
_BATCH_PRIORITY: dict[BatchExecutionStatus, int] = {
    BatchExecutionStatus.NO_ACTIONS: 0,
    BatchExecutionStatus.DRY_RUN_COMPLETED: 1,
    BatchExecutionStatus.SUCCEEDED: 2,
    BatchExecutionStatus.BLOCKED: 3,
    BatchExecutionStatus.PENDING_APPROVAL: 4,
    BatchExecutionStatus.PARTIAL_SUCCESS: 5,
    BatchExecutionStatus.CANCELLED: 6,
    BatchExecutionStatus.FAILED: 7,
    BatchExecutionStatus.UNKNOWN: 8,
}


def batch_execution_status_priority(status: BatchExecutionStatus) -> int:
    """Return unique priority weight — higher wins.

    ``max()`` over a batch always yields a single winner because every
    weight is distinct (Phase 5B Section 22).
    """
    return _BATCH_PRIORITY[status]


# ---------------------------------------------------------------------------
# ExecutionAuthorization
# ---------------------------------------------------------------------------


class ExecutionAuthorization(StrictContract):
    """Frozen, hash-stable authorisation to execute ONE Proposal.

    Built by :func:`governed_executor.build_authorization` from a
    :class:`ProposalReview` and bound to every hash the executor must
    re-verify before calling an adapter.  ``status`` starts at
    ``PENDING_APPROVAL`` (when approval is required) or ``READY``.

    ``authorization_hash`` is the single-use token: the
    :class:`ActionExecutionReceipt` carries it so a replayed or
    mis-routed receipt is detected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: str
    tenant_id: str
    run_id: str
    proposal_id: str
    action_type: str

    # Binding hashes — every one must match before the adapter call.
    review_request_hash: str
    review_result_hash: str
    proposal_review_hash: str
    proposal_snapshot_hash: str
    proposal_origin_hash: str
    governance_spec_hash: str
    adapter_registry_hash: str = ""

    # Approval binding.
    status: ExecutionStatus = ExecutionStatus.PENDING_APPROVAL
    approval_required: bool = False
    approval_id: str | None = None
    approval_decision_hash: str | None = None

    # P0-1 R3: three-tier approval hash chain.
    #
    # base_authorization_hash
    #   → hash of the authorization WITHOUT approval fields (approval_id,
    #     approval_decision_hash, status).  This is the "what the action
    #     IS" identity and never changes once built.
    #
    # approval_subject_hash
    #   → hash(base_authorization_hash + approval_id).  This is what the
    #     human approver sees and approves.  The ApprovalRequest and
    #     ApprovalDecision bind to THIS hash, NOT the final
    #     authorization_hash.
    #
    # authorization_hash
    #   → final hash including approval_subject_hash +
    #     approval_decision_hash + status.  This is what the Receipt /
    #     Command / Consumption bind to.
    #
    # pre_approval_authorization_hash is DEPRECATED (R2 legacy); kept as
    # an alias of base_authorization_hash for backward compatibility.
    base_authorization_hash: str = ""
    approval_subject_hash: str | None = None
    pre_approval_authorization_hash: str | None = None

    risk_level: ReviewRiskLevel = ReviewRiskLevel.LOW
    idempotency_key: str = ""
    dry_run: bool = False

    # Authority provenance.
    created_by_agent: str = ""
    agent_version: str = ""

    authorization_hash: str = ""

    # Fields excluded from the base hash (everything approval-lifecycle
    # related plus the hash fields themselves).
    _BASE_EXCLUDE: frozenset[str] = frozenset(
        {
            "approval_id",
            "approval_decision_hash",
            "status",
            "base_authorization_hash",
            "approval_subject_hash",
            "pre_approval_authorization_hash",
            "authorization_hash",
        }
    )

    @field_validator(
        "authorization_id",
        "tenant_id",
        "run_id",
        "proposal_id",
        "action_type",
        "review_request_hash",
        "review_result_hash",
        "proposal_review_hash",
        "proposal_snapshot_hash",
        "proposal_origin_hash",
        "governance_spec_hash",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "ExecutionAuthorization identity / binding fields must not be blank"
            )
        return v

    @model_validator(mode="after")
    def _verify_authorization_hash(self) -> ExecutionAuthorization:
        # Tier 1: base_authorization_hash (identity without approval).
        base_expected = self.compute_base_hash()
        if not self.base_authorization_hash:
            object.__setattr__(self, "base_authorization_hash", base_expected)
        elif not compare_digest(self.base_authorization_hash, base_expected):
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: stored "
                f"base_authorization_hash {self.base_authorization_hash[:12]!r} "
                f"!= computed {base_expected[:12]!r}"
            )
        # Backward-compat alias.
        if self.pre_approval_authorization_hash is None:
            object.__setattr__(
                self, "pre_approval_authorization_hash", self.base_authorization_hash
            )

        # Tier 2: approval_subject_hash (base + approval_id).
        if self.approval_id is not None:
            subject_expected = self.compute_approval_subject_hash()
            if self.approval_subject_hash is None:
                object.__setattr__(self, "approval_subject_hash", subject_expected)
            elif not compare_digest(self.approval_subject_hash, subject_expected):
                raise ValueError(
                    f"ExecutionAuthorization {self.authorization_id!r}: stored "
                    f"approval_subject_hash {self.approval_subject_hash[:12]!r} "
                    f"!= computed {subject_expected[:12]!r}"
                )

        # Tier 3: authorization_hash (everything except itself).
        auth_expected = self.compute_hash()
        if not self.authorization_hash:
            object.__setattr__(self, "authorization_hash", auth_expected)
        elif not compare_digest(self.authorization_hash, auth_expected):
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: stored "
                f"authorization_hash {self.authorization_hash[:12]!r} != "
                f"computed {auth_expected[:12]!r}"
            )
        return self

    def compute_base_hash(self) -> str:
        """Stable SHA-256 over the authorization WITHOUT approval fields.

        Excludes ``approval_id``, ``approval_decision_hash``, ``status``,
        and all hash fields.  This is the stable "what the action IS"
        identity.
        """
        return stable_hash(self, exclude=self._BASE_EXCLUDE)

    def compute_approval_subject_hash(self) -> str:
        """Stable SHA-256 over ``base_authorization_hash + approval_id``.

        This is the hash the human approver sees and approves.  Returns
        an empty string when ``approval_id`` is None (no approval
        subject).
        """
        if self.approval_id is None:
            return ""
        return stable_hash(
            {
                "base_authorization_hash": self.base_authorization_hash,
                "approval_id": self.approval_id,
            }
        )

    def compute_hash(self) -> str:
        """Stable SHA-256 over the FULL canonical authorization content.

        Excludes ``authorization_hash`` (self-referential) but includes
        ``base_authorization_hash`` and ``approval_subject_hash`` so the
        full hash chain is cryptographically bound.
        """
        return stable_hash(self, exclude={"authorization_hash"})

    def verify_integrity(self) -> None:
        """Recompute and compare the full hash chain."""
        if not compare_digest(self.base_authorization_hash, self.compute_base_hash()):
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"base_authorization_hash does not match recomputed content"
            )
        if self.approval_id is not None:
            subject = self.compute_approval_subject_hash()
            if self.approval_subject_hash is None or not compare_digest(
                self.approval_subject_hash, subject
            ):
                raise ValueError(
                    f"ExecutionAuthorization {self.authorization_id!r}: "
                    f"approval_subject_hash does not match recomputed content"
                )
        if not compare_digest(self.authorization_hash, self.compute_hash()):
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"authorization_hash does not match recomputed content"
            )

    def verify_hash_chain(self) -> None:
        """Explicit three-tier chain verification (P0-1 R3).

        Validates: base → subject → final authorization.
        """
        self.verify_integrity()
        if self.approval_required:
            if self.approval_subject_hash is None:
                raise ValueError(
                    f"ExecutionAuthorization {self.authorization_id!r}: "
                    f"approval_required but approval_subject_hash is None"
                )
        if self.approval_required and self.approval_decision_hash is not None:
            # The final authorization_hash MUST differ from the subject
            # hash (decision was bound).
            if compare_digest(
                self.authorization_hash, self.approval_subject_hash or ""
            ):
                raise ValueError(
                    f"ExecutionAuthorization {self.authorization_id!r}: "
                    f"authorization_hash == approval_subject_hash after "
                    f"approval decision was bound"
                )

    def verify_against_review(
        self,
        request: ReviewRequest,
        result: ReviewBatchResult,
        proposal_review: ProposalReview,
    ) -> None:
        """Fail-closed binding check against the originating Review.

        Verifies (Phase 5B Section 7):

        * ``tenant_id`` / ``run_id`` match the Request.
        * ``review_request_hash`` / ``review_result_hash`` match.
        * ``governance_spec_hash`` matches the Request AND the Result.
        * ``proposal_id`` matches the :class:`ProposalReview`.
        * ``proposal_review_hash`` matches the review's ``review_hash``.
        * ``risk_level`` matches the review's ``risk_level``.
        * ``action_type`` matches the matching Proposal snapshot.
        * ``proposal_snapshot_hash`` / ``proposal_origin_hash`` match
          the Request's Proposal snapshot and Envelope.

        Any mismatch raises :class:`ValueError` (fail-closed).
        """
        if self.tenant_id != request.tenant_id:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: tenant_id "
                f"{self.tenant_id!r} != request {request.tenant_id!r}"
            )
        if self.run_id != request.run_id:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: run_id "
                f"{self.run_id!r} != request {request.run_id!r}"
            )
        if self.review_request_hash != request.request_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"review_request_hash mismatch"
            )
        if self.review_result_hash != result.result_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"review_result_hash mismatch"
            )
        if self.governance_spec_hash != request.governance_spec_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"governance_spec_hash != request"
            )
        if self.governance_spec_hash != result.governance_spec_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"governance_spec_hash != result"
            )
        if self.proposal_id != proposal_review.proposal_id:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"proposal_id {self.proposal_id!r} != review "
                f"{proposal_review.proposal_id!r}"
            )
        if self.proposal_review_hash != proposal_review.review_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"proposal_review_hash mismatch"
            )
        if self.risk_level != proposal_review.risk_level:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: risk_level "
                f"{self.risk_level.value!r} != review "
                f"{proposal_review.risk_level.value!r}"
            )

        # Locate the matching Proposal snapshot + Envelope in the Request.
        matching_snapshot = None
        matching_envelope = None
        for snap in request.proposals:
            if snap.proposal_id == self.proposal_id:
                matching_snapshot = snap
                break
        if matching_snapshot is None:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: proposal "
                f"{self.proposal_id!r} not found in ReviewRequest proposals"
            )
        if matching_snapshot.action_type != self.action_type:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: action_type "
                f"{self.action_type!r} != proposal {matching_snapshot.action_type!r}"
            )
        if self.proposal_snapshot_hash != matching_snapshot.snapshot_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"proposal_snapshot_hash mismatch"
            )
        for env in request.proposal_envelopes:
            if env.proposal.proposal_id == self.proposal_id:
                matching_envelope = env
                break
        if matching_envelope is None:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: no envelope "
                f"for proposal {self.proposal_id!r}"
            )
        if self.proposal_origin_hash != matching_envelope.origin_hash:
            raise ValueError(
                f"ExecutionAuthorization {self.authorization_id!r}: "
                f"proposal_origin_hash mismatch"
            )


__all__ = [
    "BatchExecutionStatus",
    "ExecutionAuthorization",
    "ExecutionStatus",
    "batch_execution_status_priority",
]
