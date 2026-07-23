"""Phase 5B — Governed Executor (core entry point).

The :class:`GovernedExecutor` is the ONLY component that may invoke an
:class:`ActionAdapter`.  It enforces the fixed-order pipeline
(Phase 5B Section 16):

 1. Verify the ReviewRequest integrity.
 2. Verify the ReviewBatchResult integrity.
 3. Bind the Result back to the Request (``verify_against_request``).
 4. Verify the live governance spec hash matches the Request's.
 5. Select the executable Proposals (APPROVED or NEEDS_APPROVAL).
 6. For each Proposal: build an :class:`ExecutionAuthorization`.
 7. For each Proposal: verify the authorization against the Review.
 8. Resolve the approval requirement (high-risk / always-needs).
 9. When approval is required, create an :class:`ApprovalRequest`.
10. Consume the approval decision and bind it to the authorization.
11. Freeze the adapter registry snapshot.
12. Build the :class:`ExecutionCommand` with the execution fingerprint.
13. Reserve the idempotency slot.
14. Check the kill switch (fail-closed BEFORE the adapter call).
15. Mark the idempotency record as READY_TO_CALL.
16. Invoke the adapter (single attempt by default).
17. Mark the idempotency record terminal (SUCCEEDED / FAILED / UNKNOWN).
18. Build the :class:`ActionExecutionReceipt` and the batch result.

The executor NEVER bypasses a step, NEVER auto-retries UNKNOWN
outcomes, and NEVER calls the adapter without a valid authorization
+ idempotency reservation + kill-switch check.

Phase 5B R2 fixes:

* **P0-1** — ``pre_approval_authorization_hash`` on the authorization
  so the pre-approval → post-approval hash chain is verifiable.
* **P0-2** — ``validate_decision`` (read-only) + ``consume_for_command``
  (atomic consume bound to a specific command_id).
* **P0-3** — approval requests are created once (idempotent reuse via
  deterministic ``approval_id``).
* **P0-4** — :class:`FrozenActionAdapterRegistry` captures both the
  metadata snapshot AND the live adapter instances atomically.
* **P0-5** — strict call-boundary ordering: adapter lookup + verify
  BEFORE ``mark_started``; pre-call failures return
  ``adapter_call_started=False``.
* **P0-6** — ``CancelledError`` handling: ``mark_unknown`` if the
  call started, ``release_reservation`` otherwise.
* **P0-8** — dry-run + scope in every store call.
* **P0-10** — public :class:`ActionExecutionRecord` carrying full
  execution evidence; ``ExecutionBatchResult.action_records`` +
  ``verify_semantics``.
* **Retry safety** — retries require ALL conditions (no
  ``retry_only_when_safe`` bypass).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from hmac import compare_digest

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.action_adapter import (
    ActionAdapterRegistry,
    ExecutionCommand,
    FrozenActionAdapterRegistry,
    IdempotencyScope,
    compute_execution_fingerprint,
)
from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ActionGovernanceSpec,
    compute_live_governance_spec_hash,
    get_action_governance_spec,
)
from multi_agent.approval_contracts import (
    ApprovalConflictError,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Clock,
)
from multi_agent.approval_gate import (
    ApprovalGate,
    ApprovalStore,
)
from multi_agent.contracts import StrictContract
from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionAuthorization,
    ExecutionStatus,
    batch_execution_status_priority,
)
from multi_agent.execution_error_codes import (
    ACTION_NOT_SUPPORTED,
    ADAPTER_BINDING_DRIFT,
    ADAPTER_NOT_FOUND,
    APPROVAL_CONFLICT,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
    APPROVAL_REQUIRED,
    AUTHORIZATION_INTEGRITY_FAILED,
    EXECUTION_CANCELLED_BEFORE_CALL,
    EXECUTION_DEADLINE_EXCEEDED,
    EXECUTION_OUTCOME_UNKNOWN,
    GOVERNANCE_SPEC_DRIFT,
    KILL_SWITCH_ACTIVE,
    REVIEW_BINDING_MISMATCH,
    ApprovalRequiredError,
    ApprovalValidationError,
    ExecutionIntegrityError,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import (
    ExecutionStore,
    IdempotencyState,
)
from multi_agent.review_contracts import (
    ProposalReview,
    ReviewBatchResult,
    ReviewDecisionStatus,
    ReviewRequest,
    frozen_value_to_json,
)
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Retry policy + options
# ---------------------------------------------------------------------------


class ExecutionRetryPolicy(StrictContract):
    """Retry configuration for adapter calls.

    ``max_retries`` defaults to 0 — Phase 5B does NOT auto-retry by
    default.

    P0-Retry: the ``retry_only_when_safe`` bypass is REMOVED.  Retries
    now ALWAYS require ``adapter.retry_safe=True`` plus every other
    condition listed in :meth:`GovernedExecutor._execute_one_with_retry`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_retries: int = 0
    retryable_error_codes: frozenset[str] = frozenset()

    @field_validator("max_retries")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries must be >= 0")
        return v


class ExecutionOptions(StrictContract):
    """Per-batch execution options.

    P0-1: ``dry_run`` defaults to ``True`` — the default mode is
    CI-safe with NO real side-effects.  Production execution MUST
    explicitly set ``dry_run=False`` and inject a non-Noop adapter.

    P0-8: ``batch_deadline_seconds``, ``max_concurrency``, and
    ``retry_policy`` are ALL enforced at runtime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_deadline_seconds: float = 300.0
    per_action_timeout_seconds: float = 30.0
    max_concurrency: int = 4
    retry_policy: ExecutionRetryPolicy = ExecutionRetryPolicy()
    dry_run: bool = True

    @field_validator("batch_deadline_seconds", "per_action_timeout_seconds")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout / deadline must be > 0")
        return float(v)

    @field_validator("max_concurrency")
    @classmethod
    def _concurrency_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_concurrency must be >= 1")
        return v


# ---------------------------------------------------------------------------
# Per-attempt audit record (P0-7 R3 — append-only attempt trail)
# ---------------------------------------------------------------------------


class ExecutionAttemptRecord(StrictContract):
    """P0-7 R3: per-attempt audit record.  Append-only — never overwritten.

    One ``ExecutionAttemptRecord`` is produced for EVERY ``_execute_one``
    invocation inside the retry loop.  The trail is carried on
    :attr:`ActionExecutionRecord.attempts` so downstream audit consumers
    can reconstruct the full attempt history (failures + final outcome)
    without consulting the idempotency store.

    ``attempt_hash`` is auto-computed over every field (excluding itself)
    so a tampered attempt record is detected at the boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    command_family_id: str
    attempt: int
    status: ExecutionStatus
    adapter_call_started: bool
    adapter_call_dispatched: bool
    started_at: datetime | None = None
    completed_at: datetime | None = None
    outcome_hash: str | None = None
    receipt: ActionExecutionReceipt | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    executed: bool | None = None
    attempt_hash: str = ""

    @field_validator("command_id", "command_family_id")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ExecutionAttemptRecord identity fields must not be blank")
        return v

    @field_validator("attempt")
    @classmethod
    def _attempt_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("ExecutionAttemptRecord.attempt must be >= 1")
        return v

    @model_validator(mode="after")
    def _compute_attempt_hash(self) -> "ExecutionAttemptRecord":
        h = stable_hash(self, exclude={"attempt_hash"})
        if not self.attempt_hash:
            object.__setattr__(self, "attempt_hash", h)
        elif not compare_digest(self.attempt_hash, h):
            raise ValueError("ExecutionAttemptRecord.attempt_hash mismatch")
        return self


# ---------------------------------------------------------------------------
# Per-action execution record (P0-10 — public contract)
# ---------------------------------------------------------------------------


class ActionExecutionRecord(StrictContract):
    """P0-10 / R3: per-action audit record carrying full execution evidence.

    Replaces the internal ``_ActionExecutionResult`` with a public,
    frozen, hash-stable contract.  Every field that downstream audit
    consumers need (receipt, approval request, consumption hash, call
    boundary flags, retryability, attempt trail) is carried here so
    the batch result is self-describing.

    R3 additions (P0-7 / P0-8):

    * ``attempt`` — the 1-based attempt number for this record (append-
      only audit trail; retries produce SEPARATE records, never
      overwriting the previous attempt).
    * ``command_family_id`` — the stable family id binding the approval
      consumption so safe retries within the same family reuse the
      consumption (P0-5/6).
    * ``resource_lock_key`` — the serialisation key used for
      resource-level mutual exclusion (P0-3), carried for audit.
    * ``adapter_call_dispatched`` — True only after ``mark_dispatched``
      succeeded (P0-4 call-boundary state machine); distinguishes
      pre-dispatch failures (NOT_AUTHORIZED / CANCELLED) from
      post-dispatch failures (UNKNOWN).
    * ``verify_semantics()`` — validates status ↔ executed consistency,
      receipt presence for terminal states, and error_code presence
      for FAILED / UNKNOWN (P0-8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    status: ExecutionStatus
    receipt: ActionExecutionReceipt | None = None
    approval_request: ApprovalRequest | None = None
    approval_consumption_hash: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    adapter_call_started: bool = False
    adapter_call_dispatched: bool = False
    replayed: bool = False
    retryable: bool = False
    executed: bool | None = None
    skipped: bool = False
    dry_run_succeeded: bool = False
    # R3 fields
    attempt: int = 1
    command_family_id: str | None = None
    resource_lock_key: str | None = None
    # P0-7 R3: append-only per-attempt audit trail.
    attempts: tuple[ExecutionAttemptRecord, ...] = ()

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ActionExecutionRecord.proposal_id must not be blank")
        return v

    @field_validator("attempt")
    @classmethod
    def _attempt_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("ActionExecutionRecord.attempt must be >= 1")
        return v

    def verify_semantics(self) -> None:
        """P0-8 R3: validate the record's internal consistency.

        Checks (tightened in R3):

        * ``SUCCEEDED`` → ``executed is True``, no ``error_code``, AND
          ``receipt`` MUST be present with ``receipt.status == SUCCEEDED``.
        * ``DRY_RUN_SUCCEEDED`` → ``executed is False``, no
          ``error_code``, AND ``receipt`` MUST be present with
          ``receipt.status == DRY_RUN_SUCCEEDED``.
        * ``FAILED`` → ``executed is False`` and ``error_code`` set.
        * ``UNKNOWN`` → ``executed is None``; only allowed when
          ``adapter_call_dispatched`` is True (post-dispatch uncertain).
        * ``CANCELLED`` → ``executed is None``; no success receipt.
        * ``PENDING_APPROVAL`` → ``approval_request`` MUST be present
          and ``receipt`` MUST be absent.
        * ``NOT_AUTHORIZED`` / ``SKIPPED`` →
          ``adapter_call_dispatched`` is False (pre-dispatch block);
          no success receipt.
        * When ``receipt`` is present, ``receipt.status`` MUST match
          ``status``.
        * When ``adapter_call_dispatched`` is True,
          ``adapter_call_started`` MUST also be True.
        * ``replayed=True`` → ``receipt`` MUST be present (replay
          returns the original receipt).
        """
        s = self.status
        if s == ExecutionStatus.SUCCEEDED:
            if self.executed is not True:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: SUCCEEDED "
                    f"requires executed=True (got {self.executed!r})"
                )
            if self.error_code:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: SUCCEEDED "
                    f"must not carry error_code {self.error_code!r}"
                )
            # R3: SUCCEEDED MUST have a receipt.
            if self.receipt is None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: SUCCEEDED "
                    f"requires a receipt (receipt is None)"
                )
        if s == ExecutionStatus.DRY_RUN_SUCCEEDED:
            if self.executed is not False:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"DRY_RUN_SUCCEEDED requires executed=False"
                )
            if self.error_code:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"DRY_RUN_SUCCEEDED must not carry error_code"
                )
            # R3: DRY_RUN_SUCCEEDED MUST have a receipt.
            if self.receipt is None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"DRY_RUN_SUCCEEDED requires a receipt (receipt is None)"
                )
        if s == ExecutionStatus.FAILED:
            if self.executed is not False:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: FAILED "
                    f"requires executed=False (got {self.executed!r})"
                )
            if not self.error_code:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: FAILED "
                    f"requires error_code to be set"
                )
        if s == ExecutionStatus.UNKNOWN:
            if self.executed is not None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"UNKNOWN requires executed=None"
                )
            # R3: UNKNOWN is only valid after adapter call was dispatched.
            if not self.adapter_call_dispatched:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"UNKNOWN requires adapter_call_dispatched=True "
                    f"(pre-dispatch failures must NOT be UNKNOWN)"
                )
        if s == ExecutionStatus.CANCELLED:
            if self.executed is not None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"CANCELLED requires executed=None"
                )
            # R3: CANCELLED must not carry a success receipt.
            if self.receipt is not None and self.receipt.status in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.DRY_RUN_SUCCEEDED,
            ):
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"CANCELLED must not carry a success receipt"
                )
        if s == ExecutionStatus.PENDING_APPROVAL:
            # R3: PENDING_APPROVAL must have an approval_request and no receipt.
            if self.approval_request is None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"PENDING_APPROVAL requires approval_request to be set"
                )
            if self.receipt is not None:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"PENDING_APPROVAL must not carry a receipt"
                )
        if s == ExecutionStatus.SKIPPED:
            # SKIPPED actions never reach the adapter.
            if self.adapter_call_dispatched:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"SKIPPED must not have adapter_call_dispatched=True"
                )
        if s == ExecutionStatus.NOT_AUTHORIZED:
            # R3: must not carry a success receipt.  (NOT_AUTHORIZED may
            # have adapter_call_dispatched=True when the adapter itself
            # refuses to execute — e.g. Noop with dry_run=False.)
            if self.receipt is not None and self.receipt.status in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.DRY_RUN_SUCCEEDED,
            ):
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: "
                    f"NOT_AUTHORIZED must not carry a success receipt"
                )
        if self.adapter_call_dispatched and not self.adapter_call_started:
            raise ExecutionIntegrityError(
                f"ActionExecutionRecord {self.proposal_id!r}: "
                f"adapter_call_dispatched=True but "
                f"adapter_call_started=False"
            )
        if self.receipt is not None and self.receipt.status != self.status:
            raise ExecutionIntegrityError(
                f"ActionExecutionRecord {self.proposal_id!r}: receipt.status "
                f"{self.receipt.status.value!r} != record.status "
                f"{self.status.value!r}"
            )
        # R3: replayed=True requires a receipt (replay returns the original).
        if self.replayed and self.receipt is None:
            raise ExecutionIntegrityError(
                f"ActionExecutionRecord {self.proposal_id!r}: "
                f"replayed=True but receipt is None"
            )

        # P0-7 R3: validate the append-only attempt trail.
        if self.attempts:
            # Every attempt must pass attempt_hash integrity.
            for att in self.attempts:
                expected_att_hash = stable_hash(att, exclude={"attempt_hash"})
                if not compare_digest(att.attempt_hash, expected_att_hash):
                    raise ExecutionIntegrityError(
                        f"ActionExecutionRecord {self.proposal_id!r}: attempt "
                        f"{att.attempt} attempt_hash mismatch"
                    )
            # FAILED requires at least one FAILED attempt.
            if s == ExecutionStatus.FAILED and not any(
                a.status == ExecutionStatus.FAILED for a in self.attempts
            ):
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: FAILED but "
                    f"no attempt in trail has status FAILED"
                )
            # UNKNOWN requires at least one dispatched attempt.
            if s == ExecutionStatus.UNKNOWN and not any(
                a.adapter_call_dispatched for a in self.attempts
            ):
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: UNKNOWN but "
                    f"no attempt in trail has adapter_call_dispatched=True"
                )
            # SUCCEEDED / DRY_RUN_SUCCEEDED: last attempt must match.
            if s in (ExecutionStatus.SUCCEEDED, ExecutionStatus.DRY_RUN_SUCCEEDED):
                if self.attempts[-1].status != s:
                    raise ExecutionIntegrityError(
                        f"ActionExecutionRecord {self.proposal_id!r}: status "
                        f"{s.value!r} but last attempt status is "
                        f"{self.attempts[-1].status.value!r}"
                    )
            # replayed=True: exactly one entry with receipt set.
            if self.replayed:
                with_receipt = [a for a in self.attempts if a.receipt is not None]
                if len(with_receipt) != 1:
                    raise ExecutionIntegrityError(
                        f"ActionExecutionRecord {self.proposal_id!r}: "
                        f"replayed=True but attempts trail has "
                        f"{len(with_receipt)} entries with receipt (expected 1)"
                    )
            # self.attempt must equal the final attempt number.
            if self.attempt != self.attempts[-1].attempt:
                raise ExecutionIntegrityError(
                    f"ActionExecutionRecord {self.proposal_id!r}: attempt "
                    f"{self.attempt} != last attempt number "
                    f"{self.attempts[-1].attempt}"
                )


# ---------------------------------------------------------------------------
# ExecutionBatchResult
# ---------------------------------------------------------------------------


class ExecutionBatchResult(StrictContract):
    """Frozen, hash-stable aggregate result for one execution batch.

    P0-2: ``approval_requests`` carries the :class:`ApprovalRequest`
    objects created by the executor for proposals needing approval.

    P0-7: ``batch_status`` is derived from per-action outcomes, NOT
    from whether receipts exist.  ``UNKNOWN`` / ``FAILED`` /
    ``CANCELLED`` are valid even when ``receipts`` is empty.
    ``NO_ACTIONS`` is ONLY for empty ReviewRequests; a Review with
    all-rejected proposals yields ``BLOCKED``.

    P0-10: ``action_records`` carries the full per-action evidence.
    ``verify_semantics`` checks that the records are consistent with
    the review result and that all summary ID lists are correctly
    derived.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    tenant_id: str
    request_hash: str
    result_hash: str
    governance_spec_hash: str
    adapter_registry_hash: str

    receipts: tuple[ActionExecutionReceipt, ...] = ()
    approval_requests: tuple[ApprovalRequest, ...] = ()
    action_records: tuple[ActionExecutionRecord, ...] = ()
    skipped_proposal_ids: tuple[str, ...] = ()
    blocked_proposal_ids: tuple[str, ...] = ()
    pending_approval_proposal_ids: tuple[str, ...] = ()
    failed_proposal_ids: tuple[str, ...] = ()
    unknown_proposal_ids: tuple[str, ...] = ()
    succeeded_proposal_ids: tuple[str, ...] = ()
    dry_run_succeeded_proposal_ids: tuple[str, ...] = ()

    batch_status: BatchExecutionStatus = BatchExecutionStatus.NO_ACTIONS
    started_at: datetime
    completed_at: datetime
    dry_run: bool = True
    error_code: str | None = None
    batch_hash: str = ""

    @field_validator(
        "review_id",
        "run_id",
        "tenant_id",
        "request_hash",
        "result_hash",
        "governance_spec_hash",
        "adapter_registry_hash",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ExecutionBatchResult identity fields must not be blank")
        return v

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ExecutionBatchResult timestamps must be tz-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _verify_batch_invariants(self) -> ExecutionBatchResult:
        if self.started_at > self.completed_at:
            raise ValueError("started_at > completed_at")
        # P0-7: NO_ACTIONS requires no receipts AND no per-action results.
        if self.batch_status == BatchExecutionStatus.NO_ACTIONS and self.receipts:
            raise ValueError("batch_status NO_ACTIONS but receipts is non-empty")
        if self.batch_status == BatchExecutionStatus.NO_ACTIONS and self.action_records:
            raise ValueError("batch_status NO_ACTIONS but action_records is non-empty")
        # P0-7: empty receipts is valid for UNKNOWN, FAILED, CANCELLED,
        # BLOCKED, PENDING_APPROVAL, NO_ACTIONS, and DRY_RUN_COMPLETED.
        if not self.receipts and self.batch_status not in (
            BatchExecutionStatus.NO_ACTIONS,
            BatchExecutionStatus.BLOCKED,
            BatchExecutionStatus.PENDING_APPROVAL,
            BatchExecutionStatus.UNKNOWN,
            BatchExecutionStatus.FAILED,
            BatchExecutionStatus.CANCELLED,
        ):
            raise ValueError(
                f"empty receipts but batch_status is {self.batch_status.value!r}"
            )
        expected = self.compute_hash()
        if not self.batch_hash:
            object.__setattr__(self, "batch_hash", expected)
        elif not compare_digest(self.batch_hash, expected):
            raise ValueError("ExecutionBatchResult.batch_hash mismatch")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"batch_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.batch_hash, self.compute_hash()):
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: batch_hash mismatch"
            )

    def verify_semantics(self, result: ReviewBatchResult) -> None:
        """P0-10 / R3 P0-8: check that ``action_records`` are consistent
        with the review result and that all summary ID lists are
        correctly derived.

        R3 tightened checks:

        * Every record passes :meth:`ActionExecutionRecord.verify_semantics`.
        * Every record has a unique ``proposal_id``.
        * Every review proposal has EXACTLY ONE action record (complete
          coverage — no missing, no extra).
        * No non-terminal record (READY / IN_PROGRESS)
          in the final batch.
        * Summary ID lists are RECOMPUTED from records (not just
          mutually exclusive) — a forged list is rejected.
        * ``receipts`` tuple equals receipts extracted from records.
        * ``approval_requests`` tuple equals requests extracted from
          records.
        * ``batch_status`` is correctly derived from ``action_records``.
        """
        # P0-8: every record passes its own semantic checks first.
        for rec in self.action_records:
            rec.verify_semantics()

        # Every record has a unique proposal_id.
        seen: set[str] = set()
        for rec in self.action_records:
            if rec.proposal_id in seen:
                raise ExecutionIntegrityError(
                    f"ExecutionBatchResult {self.review_id!r}: duplicate "
                    f"action record for {rec.proposal_id!r}"
                )
            seen.add(rec.proposal_id)

        # R3: every review proposal has EXACTLY ONE action record.
        review_ids = {r.proposal_id for r in result.proposal_reviews}
        orphan = seen - review_ids
        if orphan:
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: action records "
                f"for unknown proposals: {sorted(orphan)!r}"
            )
        missing = review_ids - seen
        if missing:
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: review proposals "
                f"without action records: {sorted(missing)!r}"
            )

        # R3: no non-terminal record in the final batch.
        non_terminal = {
            ExecutionStatus.READY,
            ExecutionStatus.IN_PROGRESS,
        }
        for rec in self.action_records:
            if rec.status in non_terminal:
                raise ExecutionIntegrityError(
                    f"ExecutionBatchResult {self.review_id!r}: "
                    f"non-terminal status {rec.status.value!r} for "
                    f"{rec.proposal_id!r}"
                )

        # R3: recompute summary ID lists from records and compare.
        expected_skipped = tuple(
            sorted(r.proposal_id for r in self.action_records if r.skipped)
        )
        expected_blocked = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.NOT_AUTHORIZED and not r.skipped
            )
        )
        expected_pending = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.PENDING_APPROVAL
            )
        )
        expected_failed = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.FAILED
            )
        )
        expected_unknown = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.UNKNOWN
            )
        )
        expected_succeeded = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.SUCCEEDED
            )
        )
        expected_dry_run = tuple(
            sorted(
                r.proposal_id
                for r in self.action_records
                if r.status == ExecutionStatus.DRY_RUN_SUCCEEDED
            )
        )
        recomputed = {
            "skipped": expected_skipped,
            "blocked": expected_blocked,
            "pending_approval": expected_pending,
            "failed": expected_failed,
            "unknown": expected_unknown,
            "succeeded": expected_succeeded,
            "dry_run_succeeded": expected_dry_run,
        }
        actual = {
            "skipped": self.skipped_proposal_ids,
            "blocked": self.blocked_proposal_ids,
            "pending_approval": self.pending_approval_proposal_ids,
            "failed": self.failed_proposal_ids,
            "unknown": self.unknown_proposal_ids,
            "succeeded": self.succeeded_proposal_ids,
            "dry_run_succeeded": self.dry_run_succeeded_proposal_ids,
        }
        for name, expected_list in recomputed.items():
            actual_list = actual[name]
            if tuple(sorted(actual_list)) != expected_list:
                raise ExecutionIntegrityError(
                    f"ExecutionBatchResult {self.review_id!r}: "
                    f"{name}_proposal_ids does not match recomputed "
                    f"from records (expected {expected_list!r}, "
                    f"got {tuple(sorted(actual_list))!r})"
                )

        # R3: receipts tuple equals receipts extracted from records.
        expected_receipts = tuple(
            r.receipt for r in self.action_records if r.receipt is not None
        )
        if self.receipts != expected_receipts:
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: receipts tuple "
                f"does not match receipts extracted from action_records"
            )

        # R3: approval_requests tuple equals requests from records.
        expected_appr_reqs = tuple(
            r.approval_request
            for r in self.action_records
            if r.approval_request is not None
        )
        if self.approval_requests != expected_appr_reqs:
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: "
                f"approval_requests tuple does not match requests "
                f"extracted from action_records"
            )

        # batch_status is correctly derived from action_records.
        expected_status = _compute_batch_status_from_records(self.action_records)
        if self.batch_status != expected_status:
            raise ExecutionIntegrityError(
                f"ExecutionBatchResult {self.review_id!r}: batch_status "
                f"{self.batch_status.value!r} != derived "
                f"{expected_status.value!r}"
            )

    def verify_against_review(
        self, request: ReviewRequest, result: ReviewBatchResult
    ) -> None:
        """Bind the batch result back to its Review and verify semantics.

        Calls the existing binding checks (identity / hash matching)
        and then :meth:`verify_semantics` against ``result``.
        """
        if self.review_id != request.review_id:
            raise ExecutionIntegrityError(
                f"batch review_id {self.review_id!r} != request {request.review_id!r}"
            )
        if self.run_id != request.run_id:
            raise ExecutionIntegrityError("batch run_id mismatch")
        if self.tenant_id != request.tenant_id:
            raise ExecutionIntegrityError("batch tenant_id mismatch")
        if self.request_hash != request.request_hash:
            raise ExecutionIntegrityError("batch request_hash mismatch")
        if self.result_hash != result.result_hash:
            raise ExecutionIntegrityError("batch result_hash mismatch")
        if self.governance_spec_hash != request.governance_spec_hash:
            raise ExecutionIntegrityError("batch governance_spec_hash mismatch")
        if self.governance_spec_hash != result.governance_spec_hash:
            raise ExecutionIntegrityError(
                "batch governance_spec_hash != result governance_spec_hash"
            )
        self.verify_semantics(result)


# ---------------------------------------------------------------------------
# Batch-status derivation from action records (P0-10)
# ---------------------------------------------------------------------------


_ACTION_TO_BATCH: dict[ExecutionStatus, BatchExecutionStatus] = {
    ExecutionStatus.SUCCEEDED: BatchExecutionStatus.SUCCEEDED,
    ExecutionStatus.DRY_RUN_SUCCEEDED: BatchExecutionStatus.DRY_RUN_COMPLETED,
    ExecutionStatus.DEDUPLICATED: BatchExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED: BatchExecutionStatus.FAILED,
    ExecutionStatus.UNKNOWN: BatchExecutionStatus.UNKNOWN,
    ExecutionStatus.CANCELLED: BatchExecutionStatus.CANCELLED,
    ExecutionStatus.NOT_AUTHORIZED: BatchExecutionStatus.BLOCKED,
    ExecutionStatus.PENDING_APPROVAL: BatchExecutionStatus.PENDING_APPROVAL,
    ExecutionStatus.SKIPPED: BatchExecutionStatus.NO_ACTIONS,
    ExecutionStatus.READY: BatchExecutionStatus.SUCCEEDED,
    ExecutionStatus.IN_PROGRESS: BatchExecutionStatus.UNKNOWN,
}


def _action_status_to_batch(status: ExecutionStatus) -> BatchExecutionStatus:
    return _ACTION_TO_BATCH[status]


def _compute_batch_status_from_records(
    records: tuple[ActionExecutionRecord, ...],
) -> BatchExecutionStatus:
    """P0-10: derive the batch status from per-action records.

    Priority (highest wins):

        UNKNOWN > FAILED > CANCELLED > PARTIAL_SUCCESS >
        PENDING_APPROVAL > BLOCKED > SUCCEEDED > DRY_RUN_COMPLETED >
        NO_ACTIONS

    A batch with ANY non-success result alongside SUCCEEDED is
    PARTIAL_SUCCESS at best.
    """
    if not records:
        return BatchExecutionStatus.NO_ACTIONS

    statuses = [r.status for r in records]
    unique = set(statuses)

    # All the same status → that status.
    if len(unique) == 1:
        return _action_status_to_batch(statuses[0])

    has_unknown = ExecutionStatus.UNKNOWN in unique
    has_failed = ExecutionStatus.FAILED in unique
    has_cancelled = ExecutionStatus.CANCELLED in unique
    has_real_success = bool(
        unique & {ExecutionStatus.SUCCEEDED, ExecutionStatus.DEDUPLICATED}
    )
    has_dry_run = ExecutionStatus.DRY_RUN_SUCCEEDED in unique

    # UNKNOWN always wins (fail-closed).
    if has_unknown:
        return BatchExecutionStatus.UNKNOWN

    # FAILED + real success → PARTIAL_SUCCESS; FAILED alone (no success)
    # → FAILED.
    if has_failed:
        if has_real_success:
            return BatchExecutionStatus.PARTIAL_SUCCESS
        return BatchExecutionStatus.FAILED

    # CANCELLED + real success → PARTIAL_SUCCESS; CANCELLED alone →
    # CANCELLED.
    if has_cancelled:
        if has_real_success:
            return BatchExecutionStatus.PARTIAL_SUCCESS
        return BatchExecutionStatus.CANCELLED

    # Real success mixed with any non-success → PARTIAL_SUCCESS.
    if has_real_success:
        return BatchExecutionStatus.PARTIAL_SUCCESS

    # No real success.  Dry-run mixed with non-success → the
    # non-success status wins (higher priority than DRY_RUN_COMPLETED).
    # E.g. DRY_RUN_SUCCEEDED + BLOCKED → BLOCKED (NOT DRY_RUN_COMPLETED).
    if has_dry_run:
        non_dry_run = [s for s in statuses if s != ExecutionStatus.DRY_RUN_SUCCEEDED]
        if non_dry_run:
            return max(
                (_action_status_to_batch(s) for s in non_dry_run),
                key=batch_execution_status_priority,
            )
        return BatchExecutionStatus.DRY_RUN_COMPLETED

    # No success at all — use max priority among the present statuses.
    return max(
        (_action_status_to_batch(s) for s in statuses),
        key=batch_execution_status_priority,
    )


# ---------------------------------------------------------------------------
# Selection + authorization builders
# ---------------------------------------------------------------------------


def select_executable_reviews(
    request: ReviewRequest, result: ReviewBatchResult
) -> tuple[ProposalReview, ...]:
    """Return the Proposals that MAY execute.

    Per Phase 5B Section 16 step 5, only APPROVED and NEEDS_APPROVAL
    Proposals are executable.  REJECTED / NEEDS_INPUT / CONFLICT /
    DEDUPLICATED are skipped (never executed).

    The returned tuple is sorted by ``proposal_id`` for determinism.
    """
    executable: list[ProposalReview] = []
    for review in result.proposal_reviews:
        if review.status in (
            ReviewDecisionStatus.APPROVED,
            ReviewDecisionStatus.NEEDS_APPROVAL,
        ):
            executable.append(review)
    return tuple(sorted(executable, key=lambda r: r.proposal_id))


def build_authorization(
    request: ReviewRequest,
    result: ReviewBatchResult,
    proposal_review: ProposalReview,
    *,
    adapter_registry_hash: str = "",
    dry_run: bool = False,
) -> ExecutionAuthorization:
    """Build an :class:`ExecutionAuthorization` for one Proposal.

    Binds every hash the executor must re-verify.  ``status`` is
    ``READY`` when no approval is required, else ``PENDING_APPROVAL``.

    P0-1: the deterministic ``approval_id`` is computed HERE (using
    the base authorization hash) and set on the auth so it is NEVER
    ``None``.  ``pre_approval_authorization_hash`` captures the hash
    of the authorization before any approval decision is bound, so
    the pre-approval → post-approval hash chain is verifiable.
    """
    # Locate the matching Proposal snapshot + Envelope.
    snapshot = None
    envelope = None
    for snap in request.proposals:
        if snap.proposal_id == proposal_review.proposal_id:
            snapshot = snap
            break
    for env in request.proposal_envelopes:
        if env.proposal.proposal_id == proposal_review.proposal_id:
            envelope = env
            break
    if snapshot is None or envelope is None:
        raise ExecutionIntegrityError(
            f"proposal {proposal_review.proposal_id!r} not found in request"
        )

    approval_required = (
        proposal_review.status == ReviewDecisionStatus.NEEDS_APPROVAL
        or proposal_review.required_approval
    )
    status = (
        ExecutionStatus.PENDING_APPROVAL if approval_required else ExecutionStatus.READY
    )

    # Step 1: construct the base auth WITHOUT approval_id /
    # pre_approval_authorization_hash so we can compute the base hash.
    base_auth = ExecutionAuthorization(
        authorization_id=f"auth-{proposal_review.proposal_id}-{request.request_hash[:12]}",
        tenant_id=request.tenant_id,
        run_id=request.run_id,
        proposal_id=proposal_review.proposal_id,
        action_type=snapshot.action_type,
        review_request_hash=request.request_hash,
        review_result_hash=result.result_hash,
        proposal_review_hash=proposal_review.review_hash,
        proposal_snapshot_hash=snapshot.snapshot_hash,
        proposal_origin_hash=envelope.origin_hash,
        governance_spec_hash=request.governance_spec_hash,
        adapter_registry_hash=adapter_registry_hash,
        status=status,
        approval_required=approval_required,
        risk_level=proposal_review.risk_level,
        idempotency_key=snapshot.idempotency_key,
        dry_run=dry_run,
        created_by_agent=snapshot.created_by_agent,
        agent_version=envelope.agent_version,
    )

    # Step 2: compute pre_approval_authorization_hash from the base auth
    # (the hash before approval_id / pre_approval_authorization_hash are
    # set, and before any approval decision is bound).
    pre_approval_hash = base_auth.authorization_hash

    # Step 3: compute the deterministic approval_id from the base hash.
    approval_id = _deterministic_approval_id_from_hash(
        base_auth.proposal_id, pre_approval_hash
    )

    # Step 4: construct the final auth with approval_id and
    # pre_approval_authorization_hash set.  The authorization_hash
    # changes (approval_id + pre_approval_authorization_hash now
    # participate), but approval_id remains stable because it was
    # derived from the base hash.
    auth = ExecutionAuthorization(
        authorization_id=base_auth.authorization_id,
        tenant_id=base_auth.tenant_id,
        run_id=base_auth.run_id,
        proposal_id=base_auth.proposal_id,
        action_type=base_auth.action_type,
        review_request_hash=base_auth.review_request_hash,
        review_result_hash=base_auth.review_result_hash,
        proposal_review_hash=base_auth.proposal_review_hash,
        proposal_snapshot_hash=base_auth.proposal_snapshot_hash,
        proposal_origin_hash=base_auth.proposal_origin_hash,
        governance_spec_hash=base_auth.governance_spec_hash,
        adapter_registry_hash=base_auth.adapter_registry_hash,
        status=base_auth.status,
        approval_required=base_auth.approval_required,
        approval_id=approval_id,
        approval_decision_hash=base_auth.approval_decision_hash,
        pre_approval_authorization_hash=pre_approval_hash,
        risk_level=base_auth.risk_level,
        idempotency_key=base_auth.idempotency_key,
        dry_run=base_auth.dry_run,
        created_by_agent=base_auth.created_by_agent,
        agent_version=base_auth.agent_version,
    )
    return auth  # noqa: RET504


# ---------------------------------------------------------------------------
# Deterministic ID helpers (P1-1 / P1-2)
# ---------------------------------------------------------------------------


def _deterministic_approval_id(auth: ExecutionAuthorization) -> str:
    """P1-1: derive a stable approval_id from the authorization.

    Uses ``auth.authorization_hash`` so the same authorization always
    produces the same approval_id.  When ``auth.approval_id`` is
    already set (from :func:`build_authorization`), callers SHOULD use
    that directly instead of recomputing.
    """
    return f"appr-{auth.proposal_id}-{auth.authorization_hash[:12]}"


def _deterministic_approval_id_from_hash(
    proposal_id: str, authorization_hash: str
) -> str:
    """Derive a stable approval_id from a known authorization hash.

    Used by :func:`build_authorization` where we have the base hash
    but not yet the final auth with ``approval_id`` set.
    """
    return f"appr-{proposal_id}-{authorization_hash[:12]}"


def _deterministic_command_id(
    auth: ExecutionAuthorization, fingerprint: str, attempt: int
) -> str:
    """P1-2: derive a stable command_id so replays produce the same id."""
    raw = f"{auth.authorization_hash}:{fingerprint}:{attempt}"
    return f"cmd-{auth.proposal_id}-{stable_hash(raw)[:12]}"


def _deterministic_command_family_id(
    auth: ExecutionAuthorization,
    binding_adapter_id: str,
) -> str:
    """P0-5/6 R3: derive a stable command_family_id for approval binding.

    The family id is stable across retries (it does NOT include the
    attempt number) so safe retries within the same family can reuse
    the approval consumption.  It binds to the proposal, the base
    authorization hash (stable identity), and the adapter — so a
    different adapter or a different proposal yields a different
    family (and thus cannot reuse the approval).
    """
    raw = f"{auth.proposal_id}:{auth.base_authorization_hash}:{binding_adapter_id}"
    return f"cfam-{stable_hash(raw)[:16]}"


def _compute_resource_lock_key(
    gov_spec: ActionGovernanceSpec,
    payload: dict,
    tenant_id: str,
) -> str | None:
    """P0-3 R3: compute a resource-level serialisation key.

    Returns ``None`` when the governance spec declares no
    ``resource_type`` (read-only / non-mutating actions need no
    serialisation).  Otherwise the key is
    ``{tenant}:{resource_type}:{resource_id}:{conflict_family}`` where
    ``resource_id`` is extracted from the payload using
    ``gov_spec.resource_id_fields``.

    Two actions that share the same key target the same external
    resource and MUST be serialised so the adapter never sees
    concurrent mutations on the same resource.
    """
    rt = gov_spec.resource_type
    if not rt:
        return None
    # Extract resource_id from the payload using the declared fields.
    rid_parts: list[str] = []
    for field_name in gov_spec.resource_id_fields:
        val = payload.get(field_name) if isinstance(payload, dict) else None
        if val is not None:
            rid_parts.append(str(val))
    resource_id = ":".join(rid_parts) if rid_parts else "unknown"
    conflict_family = gov_spec.conflict_family or rt
    return f"{tenant_id}:{rt}:{resource_id}:{conflict_family}"


def _extract_payload_dict(snapshot) -> dict:
    """Return the proposal payload as a plain dict for resource-id
    extraction.  Handles both frozen JSON values and plain dicts."""
    from multi_agent.review_contracts import frozen_value_to_json

    raw = frozen_value_to_json(snapshot.payload)
    if isinstance(raw, dict):
        return raw
    return {}


# ---------------------------------------------------------------------------
# GovernedExecutor
# ---------------------------------------------------------------------------


class GovernedExecutor:
    """The ONLY component that may invoke an :class:`ActionAdapter`.

    Stateless itself — every durable boundary (approval store,
    idempotency store, adapter registry, kill switch) is injected per
    call so tests stay deterministic and there is no hidden global
    state.

    R3 P0-3: the executor owns a per-call resource-lock registry so
    actions targeting the same external resource (same
    ``resource_lock_key``) are serialised via :class:`asyncio.Lock`.
    The registry is rebuilt on every ``execute()`` call (it is NOT
    shared across batches) so there is no cross-batch lock leakage.
    """

    def __init__(self) -> None:
        self._approval_gate = ApprovalGate()
        # P0-3 R3: per-call resource locks (reset on every execute()).
        self._resource_locks: dict[str, asyncio.Lock] = {}
        self._resource_locks_guard = asyncio.Lock()

    async def _get_resource_lock(self, key: str) -> asyncio.Lock:
        """P0-3 R3: return (or create) the asyncio.Lock for *key*.

        The locks are created lazily under a guard lock so concurrent
        coroutines do not create duplicate locks for the same key.
        """
        async with self._resource_locks_guard:
            lock = self._resource_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._resource_locks[key] = lock
            return lock

    async def execute(
        self,
        *,
        request: ReviewRequest,
        review_result: ReviewBatchResult,
        approval_store: ApprovalStore,
        execution_store: ExecutionStore,
        adapter_registry: ActionAdapterRegistry,
        kill_switch,
        clock: Clock,
        options: ExecutionOptions | None = None,
    ) -> ExecutionBatchResult:
        """Execute the batch following the fixed-order pipeline."""
        opts = options or ExecutionOptions()
        started_at = clock.now()

        # P0-3 R3: reset the per-call resource-lock registry so locks
        # from a previous batch never leak into this one.
        self._resource_locks = {}

        # ---- Step 1: verify request integrity -------------------------
        try:
            request.verify_integrity()
        except Exception as e:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=AUTHORIZATION_INTEGRITY_FAILED,
                error_message=f"request integrity failed: {e}",
            )

        # ---- Step 2: verify result integrity --------------------------
        try:
            review_result.verify_integrity()
        except Exception as e:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=AUTHORIZATION_INTEGRITY_FAILED,
                error_message=f"result integrity failed: {e}",
            )

        # ---- Step 3: bind result to request ---------------------------
        try:
            review_result.verify_against_request(request)
        except Exception as e:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=REVIEW_BINDING_MISMATCH,
                error_message=f"result-request binding failed: {e}",
            )

        # ---- Step 4: verify governance spec hash ----------------------
        # P0-9: verify the LIVE governance spec hash matches the module
        # constant, the request, and the review result (all three must
        # agree — a tampered registry or a stale request is detected
        # here, before any authorization is built).
        try:
            live_hash = compute_live_governance_spec_hash()
        except Exception as e:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=GOVERNANCE_SPEC_DRIFT,
                error_message=f"failed to compute live governance spec hash: {e}",
            )
        if live_hash != ACTION_GOVERNANCE_SPEC_HASH:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=GOVERNANCE_SPEC_DRIFT,
                error_message="live governance spec hash drifts from module constant",
            )
        if live_hash != request.governance_spec_hash:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=GOVERNANCE_SPEC_DRIFT,
                error_message="live governance spec hash != request",
            )
        if live_hash != review_result.governance_spec_hash:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=GOVERNANCE_SPEC_DRIFT,
                error_message="live governance spec hash != review result",
            )

        # ---- Step 5: select executable reviews ------------------------
        executable = select_executable_reviews(request, review_result)
        if not executable:
            # P0-7: distinguish NO_ACTIONS (ReviewRequest produced no
            # proposals at all) from BLOCKED (proposals exist but none
            # are executable — all REJECTED / NEEDS_INPUT / etc.).
            if not review_result.proposal_reviews:
                return self._build_empty_batch(
                    request, review_result, adapter_registry, opts, started_at, clock
                )
            # Proposals exist but all were REJECTED / NEEDS_INPUT / etc.
            return self._build_blocked_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                blocked_ids=tuple(
                    sorted(r.proposal_id for r in review_result.proposal_reviews)
                ),
            )

        # ---- Step 6-11: per-proposal authorization + approval ----------
        # P0-4: freeze the adapter registry (snapshot + live adapters)
        # ONCE for the whole batch so a concurrent register() cannot
        # affect any action.
        frozen_registry = adapter_registry.freeze_for_execution()

        per_action: list[ActionExecutionRecord] = []
        approval_requests_created: list[ApprovalRequest] = []

        ready: list[
            tuple[
                ProposalReview, ExecutionAuthorization, ActionGovernanceSpec, str | None
            ]
        ] = []

        for review in executable:
            auth = build_authorization(
                request,
                review_result,
                review,
                adapter_registry_hash=frozen_registry.registry_hash,
                dry_run=opts.dry_run,
            )
            # Step 7: verify authorization against the review.
            try:
                auth.verify_integrity()
                auth.verify_against_review(request, review_result, review)
            except Exception as e:
                per_action.append(
                    ActionExecutionRecord(
                        proposal_id=review.proposal_id,
                        status=ExecutionStatus.NOT_AUTHORIZED,
                        error_code=AUTHORIZATION_INTEGRITY_FAILED,
                        error_message=str(e),
                        skipped=True,
                    )
                )
                continue

            # Resolve governance spec for this action.
            gov_spec = get_action_governance_spec(auth.action_type)
            if gov_spec is None:
                per_action.append(
                    ActionExecutionRecord(
                        proposal_id=review.proposal_id,
                        status=ExecutionStatus.NOT_AUTHORIZED,
                        error_code=ACTION_NOT_SUPPORTED,
                        error_message=f"unknown action_type {auth.action_type!r}",
                        skipped=True,
                    )
                )
                continue

            # Step 8-10: approval lifecycle (P0-1 / P0-2 / P0-3).
            early_result, updated_auth, approval_req = await self._resolve_approval(
                review, auth, gov_spec, approval_store, clock
            )
            if early_result is not None:
                per_action.append(early_result)
                if approval_req is not None:
                    approval_requests_created.append(approval_req)
                continue

            # P0-3 R3: compute the resource-level lock key for this
            # action so concurrent actions targeting the same resource
            # are serialised.
            _snapshot = None
            for snap in request.proposals:
                if snap.proposal_id == review.proposal_id:
                    _snapshot = snap
                    break
            _payload_dict = _extract_payload_dict(_snapshot) if _snapshot else {}
            resource_lock_key = _compute_resource_lock_key(
                gov_spec, _payload_dict, auth.tenant_id
            )

            # updated_auth is not None — ready to execute.
            assert updated_auth is not None
            ready.append((review, updated_auth, gov_spec, resource_lock_key))

        # ---- Phase 2: concurrent execute (P0-8) ----------------------
        if ready:
            concurrent_results = await self._execute_concurrent(
                ready_actions=ready,
                request=request,
                review_result=review_result,
                frozen_registry=frozen_registry,
                approval_store=approval_store,
                execution_store=execution_store,
                kill_switch=kill_switch,
                clock=clock,
                opts=opts,
                started_at=started_at,
            )
            per_action.extend(concurrent_results)

        batch = self._assemble_batch(
            request,
            review_result,
            frozen_registry,
            per_action,
            opts,
            started_at,
            clock,
            dry_run=opts.dry_run,
            approval_requests=approval_requests_created,
        )
        # R3 P0-8: enforce integrity + review coverage before returning.
        batch.verify_integrity()
        batch.verify_against_review(request, review_result)
        return batch

    # -----------------------------------------------------------------
    # Approval lifecycle (P0-1 / P0-2 / P0-3)
    # -----------------------------------------------------------------

    async def _resolve_approval(
        self,
        review: ProposalReview,
        auth: ExecutionAuthorization,
        gov_spec: ActionGovernanceSpec,
        approval_store: ApprovalStore,
        clock: Clock,
    ) -> tuple[
        ActionExecutionRecord | None,
        ExecutionAuthorization | None,
        ApprovalRequest | None,
    ]:
        """P0-1: full approval lifecycle.

        Returns ``(early_result, updated_auth, approval_request)``.
        If ``early_result`` is not None, the action is done
        (PENDING_APPROVAL or NOT_AUTHORIZED).  If ``updated_auth`` is
        not None, the action is ready to execute.

        P0-3: the approval request is queried first (idempotent
        reuse).  If it already exists, the decision is checked.  If no
        request exists, one is created (only once).

        P0-2: ``validate_decision`` (read-only) is used to check the
        decision BEFORE consumption.  The actual consumption
        (``consume_for_command``) happens later in ``_execute_one``
        after the idempotency reservation.
        """
        requirement = self._approval_gate.resolve_approval_requirement(
            review, auth, gov_spec
        )
        if not requirement.required:
            # No approval needed — auth is ready to execute as-is.
            return (None, auth, None)

        # approval_id is already set on auth by build_authorization.
        approval_id = auth.approval_id or _deterministic_approval_id(auth)
        pre_approval_hash = auth.authorization_hash

        # P0-3: query existing request first (idempotent reuse).
        existing_request = await approval_store.get(approval_id)

        if existing_request is not None:
            # Request already exists — check for a decision.
            decision = await approval_store.get_decision(approval_id)
            if decision is None:
                # No decision yet — still pending.
                return (
                    ActionExecutionRecord(
                        proposal_id=review.proposal_id,
                        status=ExecutionStatus.PENDING_APPROVAL,
                        approval_request=existing_request,
                        error_code=APPROVAL_REQUIRED,
                        error_message="no decision yet",
                        skipped=True,
                    ),
                    None,
                    existing_request,
                )

            if decision.status == ApprovalStatus.APPROVED:
                # P0-2: validate_decision (read-only, no consume yet).
                try:
                    await approval_store.validate_decision(
                        approval_id,
                        authorization=auth,
                        now=clock.now(),
                    )
                except (ApprovalRequiredError, ApprovalValidationError) as e:
                    return (
                        ActionExecutionRecord(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.NOT_AUTHORIZED,
                            error_code=getattr(e, "error_code", APPROVAL_REQUIRED),
                            error_message=str(e),
                            skipped=True,
                        ),
                        None,
                        existing_request,
                    )
                # Validated — bind approval to auth for later consumption.
                updated_auth = self._bind_approval(auth, decision, pre_approval_hash)
                return (None, updated_auth, existing_request)

            # REJECTED / EXPIRED / REVOKED
            if decision.status == ApprovalStatus.REJECTED:
                error_code = APPROVAL_REJECTED
            else:
                error_code = APPROVAL_EXPIRED
            return (
                ActionExecutionRecord(
                    proposal_id=review.proposal_id,
                    status=ExecutionStatus.NOT_AUTHORIZED,
                    error_code=error_code,
                    error_message=f"approval {decision.status.value}",
                    skipped=True,
                ),
                None,
                existing_request,
            )

        # No existing request — create one (P0-3: only created once).
        approval_req = self._build_approval_request(
            auth, review, gov_spec, approval_id, clock
        )
        try:
            created = await approval_store.create(approval_req)
        except ApprovalConflictError as e:
            return (
                ActionExecutionRecord(
                    proposal_id=review.proposal_id,
                    status=ExecutionStatus.NOT_AUTHORIZED,
                    error_code=APPROVAL_CONFLICT,
                    error_message=str(e),
                    skipped=True,
                ),
                None,
                None,
            )
        return (
            ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.PENDING_APPROVAL,
                approval_request=created,
                error_code=APPROVAL_REQUIRED,
                error_message="approval request created",
                skipped=True,
            ),
            None,
            created,
        )

    # -----------------------------------------------------------------
    # Concurrent execution (P0-8)
    # -----------------------------------------------------------------

    async def _execute_concurrent(
        self,
        *,
        ready_actions: list[
            tuple[
                ProposalReview, ExecutionAuthorization, ActionGovernanceSpec, str | None
            ]
        ],
        request: ReviewRequest,
        review_result: ReviewBatchResult,
        frozen_registry: FrozenActionAdapterRegistry,
        approval_store: ApprovalStore,
        execution_store: ExecutionStore,
        kill_switch,
        clock: Clock,
        opts: ExecutionOptions,
        started_at: datetime,
    ) -> list[ActionExecutionRecord]:
        """P0-8 / P0-3 R3: run ready actions concurrently under a
        Semaphore, enforcing the batch deadline, retry policy, AND
        resource-level serialisation.

        P0-3 R3: actions that share the same ``resource_lock_key`` are
        serialised via a per-key :class:`asyncio.Lock` so the adapter
        never sees concurrent mutations on the same external resource.
        Actions with ``resource_lock_key=None`` (read-only / no
        ``resource_type``) run without resource-level serialisation.

        P0-6: the outer ``asyncio.wait_for`` wrapper is REMOVED.  Each
        attempt's timeout is handled internally by
        ``_execute_one_with_retry`` so the per-attempt deadline is
        ``min(per_action_timeout, remaining_batch_deadline)``.

        Concurrency rules:

        * ``asyncio.Semaphore(max_concurrency)`` bounds the number of
          in-flight adapter calls.
        * Per-resource :class:`asyncio.Lock` serialises actions on the
          same resource (P0-3 R3).
        * The batch deadline is checked before each action starts; an
          action that would start after the deadline returns
          ``UNKNOWN`` with ``EXECUTION_DEADLINE_EXCEEDED``.
        """
        semaphore = asyncio.Semaphore(opts.max_concurrency)
        deadline = started_at + timedelta(seconds=opts.batch_deadline_seconds)

        async def _run_one(
            review: ProposalReview,
            auth: ExecutionAuthorization,
            gov_spec: ActionGovernanceSpec,
            resource_lock_key: str | None,
        ) -> ActionExecutionRecord:
            async with semaphore:
                # P0-3 R3: acquire the resource-level lock (if any)
                # so concurrent actions on the same resource are
                # serialised.  The lock is released when the async-with
                # block exits (after the action completes or fails).
                if resource_lock_key is not None:
                    resource_lock = await self._get_resource_lock(resource_lock_key)
                    async with resource_lock:
                        return await self._execute_one_with_retry(
                            request=request,
                            review_result=review_result,
                            review=review,
                            auth=auth,
                            gov_spec=gov_spec,
                            frozen_registry=frozen_registry,
                            approval_store=approval_store,
                            execution_store=execution_store,
                            kill_switch=kill_switch,
                            clock=clock,
                            opts=opts,
                            attempt=1,
                            batch_deadline=deadline,
                            resource_lock_key=resource_lock_key,
                        )
                return await self._execute_one_with_retry(
                    request=request,
                    review_result=review_result,
                    review=review,
                    auth=auth,
                    gov_spec=gov_spec,
                    frozen_registry=frozen_registry,
                    approval_store=approval_store,
                    execution_store=execution_store,
                    kill_switch=kill_switch,
                    clock=clock,
                    opts=opts,
                    attempt=1,
                    batch_deadline=deadline,
                    resource_lock_key=resource_lock_key,
                )

        tasks = [_run_one(r, a, g, k) for r, a, g, k in ready_actions]
        return await asyncio.gather(*tasks)

    async def _execute_one_with_retry(
        self,
        *,
        request: ReviewRequest,
        review_result: ReviewBatchResult,
        review: ProposalReview,
        auth: ExecutionAuthorization,
        gov_spec: ActionGovernanceSpec,
        frozen_registry: FrozenActionAdapterRegistry,
        approval_store: ApprovalStore,
        execution_store: ExecutionStore,
        kill_switch,
        clock: Clock,
        opts: ExecutionOptions,
        attempt: int,
        batch_deadline: datetime,
        resource_lock_key: str | None = None,
    ) -> ActionExecutionRecord:
        """P0-8 / P0-5/6 R3: wrap ``_execute_one`` with the retry policy.

        Retry requires ALL of the following (no bypass):

        * ``result.status == FAILED``
        * ``result.executed == False`` (confirmed no side-effect)
        * ``result.retryable == True`` (from AdapterExecutionOutcome)
        * ``adapter.retry_safe == True`` (from the frozen binding)
        * ``binding.idempotency_scope != IdempotencyScope.NONE``
        * ``gov_spec.execution_retry_allowed == True`` (R3: defaults to
          False — governance-level opt-IN, not opt-OUT)
        * ``current_attempt < min(policy.max_retries, gov_spec.max_execution_retries) + 1``
          (R3: both budgets apply; the tighter one wins)
        * ``result.error_code in policy.retryable_error_codes``
          AND ``result.error_code in gov_spec.retryable_error_codes``
          (R3: both sets must allow the error code)
        * Batch deadline not exceeded.
        * Kill switch not active.

        Never retried: UNKNOWN, CANCELLED, PENDING_APPROVAL,
        NOT_AUTHORIZED, SUCCEEDED, DRY_RUN_SUCCEEDED, DEDUPLICATED,
        SKIPPED.
        """
        policy = opts.retry_policy
        # R3 P0-5/6: effective max attempts = min(policy, gov_spec) + 1.
        gov_max_retries = getattr(gov_spec, "max_execution_retries", 0)
        effective_max_retries = min(policy.max_retries, gov_max_retries)
        max_attempts = effective_max_retries + 1

        current_attempt = attempt
        # P0-7 R3: append-only attempt audit trail.
        collected_attempts: list[ExecutionAttemptRecord] = []
        while True:
            # P0-6: per-attempt timeout = min(per_action_timeout,
            # remaining_batch_deadline).
            now = clock.now()
            remaining = (batch_deadline - now).total_seconds()
            if remaining <= 0:
                # P0-4 R3: batch deadline exceeded BEFORE the attempt
                # starts is a PRE-DISPATCH failure — the adapter was
                # never called.  Must NOT be UNKNOWN (UNKNOWN is only
                # valid after adapter_call_dispatched=True).
                rec = ActionExecutionRecord(
                    proposal_id=review.proposal_id,
                    status=ExecutionStatus.CANCELLED,
                    executed=None,
                    error_code=EXECUTION_DEADLINE_EXCEEDED,
                    error_message="batch deadline exceeded before attempt",
                    adapter_call_started=False,
                    adapter_call_dispatched=False,
                    attempt=current_attempt,
                    resource_lock_key=resource_lock_key,
                )
                return rec.model_copy(update={"attempts": tuple(collected_attempts)})
            per_action_timeout = min(opts.per_action_timeout_seconds, remaining)

            result = await self._execute_one(
                request=request,
                review_result=review_result,
                review=review,
                auth=auth,
                gov_spec=gov_spec,
                frozen_registry=frozen_registry,
                approval_store=approval_store,
                execution_store=execution_store,
                kill_switch=kill_switch,
                clock=clock,
                opts=opts,
                attempt=current_attempt,
                per_action_timeout=per_action_timeout,
                resource_lock_key=resource_lock_key,
            )

            # P0-7 R3: build an append-only attempt record from this
            # result and add it to the trail.  The trail is carried on
            # the final ActionExecutionRecord.attempts so downstream
            # audit consumers can reconstruct the full attempt history.
            attempt_rec = ExecutionAttemptRecord(
                command_id=(
                    result.receipt.command_id
                    if result.receipt is not None
                    else f"cmd-{review.proposal_id}-{current_attempt}"
                ),
                command_family_id=(
                    result.command_family_id
                    if result.command_family_id is not None
                    else f"cfam-{review.proposal_id}"
                ),
                attempt=current_attempt,
                status=result.status,
                adapter_call_started=result.adapter_call_started,
                adapter_call_dispatched=result.adapter_call_dispatched,
                started_at=(
                    result.receipt.started_at if result.receipt is not None else None
                ),
                completed_at=(
                    result.receipt.completed_at if result.receipt is not None else None
                ),
                receipt=result.receipt,
                error_code=result.error_code,
                error_message=result.error_message,
                retryable=result.retryable,
                executed=result.executed,
            )
            collected_attempts.append(attempt_rec)

            # Terminal states that are NEVER retried.
            if result.status in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.DRY_RUN_SUCCEEDED,
                ExecutionStatus.DEDUPLICATED,
                ExecutionStatus.UNKNOWN,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.PENDING_APPROVAL,
                ExecutionStatus.NOT_AUTHORIZED,
                ExecutionStatus.SKIPPED,
            ):
                return result.model_copy(update={"attempts": tuple(collected_attempts)})

            # Only FAILED may be retried, and only under strict conditions.
            if result.status != ExecutionStatus.FAILED:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})

            # --- ALL retry conditions (no bypass) ---------------------
            # 1. result.executed == False (confirmed no side-effect).
            if result.executed is not False:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 2. result.retryable == True (from AdapterExecutionOutcome).
            if not result.retryable:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 3. attempt budget (R3: min of policy and gov_spec).
            if current_attempt >= max_attempts:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 4. adapter.retry_safe == True (from the frozen binding).
            try:
                binding = frozen_registry.get_binding(auth.action_type)
            except Exception:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            if not binding.retry_safe:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 5. binding.idempotency_scope != NONE.
            if binding.idempotency_scope == IdempotencyScope.NONE:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 6. R3 P0-5/6: gov_spec.execution_retry_allowed == True
            #    (defaults to False — governance-level opt-IN).
            if not getattr(gov_spec, "execution_retry_allowed", False):
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 7. R3 P0-5/6: error_code in BOTH policy.retryable_error_codes
            #    AND gov_spec.retryable_error_codes.
            if result.error_code is None:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            gov_retryable: frozenset[str] = getattr(
                gov_spec, "retryable_error_codes", frozenset()
            )
            if result.error_code not in policy.retryable_error_codes:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            if result.error_code not in gov_retryable:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 8. Batch deadline not exceeded.
            if clock.now() > batch_deadline:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})
            # 9. Kill switch not active (R3 P0-11: scope-aware check).
            try:
                ks_active = await kill_switch.is_kill_switch_active_for_scope(
                    tenant_id=auth.tenant_id,
                    run_id=auth.run_id,
                    action_type=auth.action_type,
                    adapter_id=binding.adapter_id,
                )
            except Exception:
                ks_active = True
            if ks_active:
                return result.model_copy(update={"attempts": tuple(collected_attempts)})

            current_attempt += 1

    # -----------------------------------------------------------------
    # Single-action execution (steps 12-18)
    # -----------------------------------------------------------------

    async def _execute_one(
        self,
        *,
        request: ReviewRequest,
        review_result: ReviewBatchResult,
        review: ProposalReview,
        auth: ExecutionAuthorization,
        gov_spec: ActionGovernanceSpec,
        frozen_registry: FrozenActionAdapterRegistry,
        approval_store: ApprovalStore,
        execution_store: ExecutionStore,
        kill_switch,
        clock: Clock,
        opts: ExecutionOptions,
        attempt: int = 1,
        per_action_timeout: float | None = None,
        resource_lock_key: str | None = None,
    ) -> ActionExecutionRecord:
        """P0-5 / P0-4 R3: strict call-boundary ordering.

        1. Frozen Adapter lookup + verify (BEFORE mark_started)
        2. Deadline check
        3. Kill Switch check (pre-call)
        4. Idempotency reservation
        5. Approval consumption (consume_for_command) — AFTER reservation
        6. mark_started (→ READY_TO_CALL)
        7. Kill Switch re-check
        8. mark_dispatched (→ CALL_DISPATCHED)  [R3 P0-4]
        9. Adapter await (with CancelledError handling)
        10. Outcome verification
        11. Receipt construction
        12. complete_with_receipt

        Pre-call failures (steps 1-5) return BLOCKED / CANCELLED /
        FAILED with ``adapter_call_started=False``.  Failures between
        mark_started and mark_dispatched (step 6-7) return UNKNOWN
        with ``adapter_call_started=True, adapter_call_dispatched=False``.
        Only failures after mark_dispatched (step 8+) have
        ``adapter_call_dispatched=True``.
        """
        # Locate the proposal snapshot for the canonical payload.
        snapshot = None
        for snap in request.proposals:
            if snap.proposal_id == review.proposal_id:
                snapshot = snap
                break
        assert snapshot is not None

        # ---- Step 1: Frozen adapter lookup + verify (P0-4) ----------
        # This happens BEFORE mark_started so a missing / drifted
        # adapter is a pre-call failure (BLOCKED, not UNKNOWN).
        try:
            binding = frozen_registry.get_binding(auth.action_type)
        except KeyError:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=ACTION_NOT_SUPPORTED,
                error_message=f"no adapter bound for {auth.action_type!r}",
                skipped=True,
                attempt=attempt,
                resource_lock_key=resource_lock_key,
            )
        adapter = frozen_registry.verify_adapter_matches_binding(
            binding, dry_run=auth.dry_run
        )
        if adapter is None:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=ADAPTER_NOT_FOUND,
                error_message=(
                    f"adapter {binding.adapter_id!r} not registered or drifted"
                ),
                skipped=True,
                attempt=attempt,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 12: build the command + fingerprint ---------------
        canonical_payload = frozen_value_to_json(snapshot.payload)
        fingerprint = compute_execution_fingerprint(
            tenant_id=auth.tenant_id,
            proposal_id=auth.proposal_id,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            action_type=auth.action_type,
            canonical_payload=canonical_payload,
            adapter_id=binding.adapter_id,
            adapter_version=binding.adapter_version,
            authorization_hash=auth.authorization_hash,
            governance_spec_hash=auth.governance_spec_hash,
            registry_hash=frozen_registry.registry_hash,
            idempotency_key=auth.idempotency_key,
            dry_run=auth.dry_run,
        )
        # P1-2: deterministic command_id so a replay produces the same id.
        # P0-8: attempt is parameterized so retries get a distinct id.
        effective_timeout = (
            per_action_timeout
            if per_action_timeout is not None
            else opts.per_action_timeout_seconds
        )
        command_id = _deterministic_command_id(auth, fingerprint, attempt)
        # R3 P0-5/6: deterministic command_family_id for approval binding.
        command_family_id = _deterministic_command_family_id(auth, binding.adapter_id)
        command = ExecutionCommand(
            command_id=command_id,
            authorization=auth,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            action_type=auth.action_type,
            adapter_id=binding.adapter_id,
            adapter_version=binding.adapter_version,
            dry_run=auth.dry_run,
            attempt=attempt,
            timeout_seconds=effective_timeout,
            execution_fingerprint=fingerprint,
        )

        # ---- Step 2: deadline check ---------------------------------
        # (per-attempt deadline is already computed by the caller;
        # this is a safety net for the case where _execute_one is
        # called directly.)
        effective_timeout = (
            per_action_timeout
            if per_action_timeout is not None
            else opts.per_action_timeout_seconds
        )

        # ---- Step 3: kill switch check (pre-call) -------------------
        # Pre-call blocks return BLOCKED / CANCELLED (NOT UNKNOWN) and
        # do NOT touch the idempotency store.
        # R3 P0-11: scope-aware check across all 5 dimensions.
        try:
            ks_active = await kill_switch.is_kill_switch_active_for_scope(
                tenant_id=auth.tenant_id,
                run_id=auth.run_id,
                action_type=auth.action_type,
                adapter_id=binding.adapter_id,
            )
            cancelled = await kill_switch.is_cancelled(auth.run_id)
        except Exception:
            ks_active = True
            cancelled = False
        if ks_active:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=KILL_SWITCH_ACTIVE,
                error_message="kill switch active for tenant",
                skipped=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )
        if cancelled:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.CANCELLED,
                error_code=EXECUTION_CANCELLED_BEFORE_CALL,
                error_message="run cancelled before adapter call",
                skipped=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 4: idempotency reservation (P0-7 / P0-8) -----------
        # P0-7: dry-run executions reserve under the dry-run namespace.
        # P0-8: scope controls the store key shape.
        try:
            record = await execution_store.reserve(
                auth.tenant_id,
                auth.idempotency_key,
                fingerprint,
                command_id,
                dry_run=auth.dry_run,
                scope=binding.idempotency_scope,
            )
        except Exception as e:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=getattr(e, "error_code", REVIEW_BINDING_MISMATCH),
                error_message=str(e),
                skipped=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Replay check (P0-6 / P0-7) ------------------------------
        # Return the ORIGINAL trusted receipt on replay.
        if (
            record.state
            in (IdempotencyState.SUCCEEDED, IdempotencyState.DRY_RUN_SUCCEEDED)
            and record.receipt_id
        ):
            original_receipt = await execution_store.get_receipt(
                auth.tenant_id,
                auth.idempotency_key,
                dry_run=auth.dry_run,
                scope=binding.idempotency_scope,
            )
            if original_receipt is not None:
                return ActionExecutionRecord(
                    proposal_id=review.proposal_id,
                    receipt=original_receipt,
                    status=original_receipt.status,
                    replayed=True,
                    executed=original_receipt.executed,
                    dry_run_succeeded=(
                        original_receipt.status == ExecutionStatus.DRY_RUN_SUCCEEDED
                    ),
                    attempt=attempt,
                    command_family_id=command_family_id,
                    resource_lock_key=resource_lock_key,
                )
            # Store says terminal success but no receipt → UNKNOWN.
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="terminal record but no stored receipt",
                adapter_call_started=False,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )
        # UNKNOWN outcomes are NOT auto-retried (Phase 5B Section 17).
        if record.state == IdempotencyState.UNKNOWN:
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=(
                    "previous outcome was UNKNOWN — manual intervention required"
                ),
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )
        # CALL_DISPATCHED / READY_TO_CALL with same fingerprint → blocked.
        if record.state in (
            IdempotencyState.CALL_DISPATCHED,
            IdempotencyState.READY_TO_CALL,
        ):
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"idempotency slot is {record.state.value!r}",
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 5: approval consumption (P0-2 R3) ----------------
        # AFTER reservation, BEFORE mark_started.  If consumption
        # fails, release the reservation (approval not consumed).
        # R3: consume_for_command binds to command_family_id (not
        # command_id) so safe retries within the same family reuse
        # the consumption.
        approval_consumption_hash: str | None = None
        if auth.approval_required:
            assert auth.approval_id is not None, (
                "approval_required but approval_id is None"
            )
            try:
                consumption = await approval_store.consume_for_command(
                    auth.approval_id,
                    authorization=auth,
                    command_family_id=command_family_id,
                    execution_fingerprint=fingerprint,
                    now=clock.now(),
                )
                approval_consumption_hash = consumption.consumption_hash
            except (
                ApprovalRequiredError,
                ApprovalValidationError,
            ) as e:
                # Release the reservation — approval not consumed.
                with contextlib.suppress(Exception):
                    await execution_store.release_reservation(record)
                return ActionExecutionRecord(
                    proposal_id=review.proposal_id,
                    status=ExecutionStatus.NOT_AUTHORIZED,
                    error_code=getattr(e, "error_code", APPROVAL_REQUIRED),
                    error_message=str(e),
                    adapter_call_started=False,
                    attempt=attempt,
                    command_family_id=command_family_id,
                    resource_lock_key=resource_lock_key,
                )

        # ---- Step 6: mark_started (→ READY_TO_CALL) ----------------
        # P0-5 / P0-6: from this point, a failure can be UNKNOWN.
        try:
            record = await execution_store.mark_started(record, command_id)
        except Exception as e:
            # mark_started failed before the call started — release
            # the reservation (it is still RESERVED).
            with contextlib.suppress(Exception):
                await execution_store.release_reservation(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"mark_started failed: {e}",
                adapter_call_started=False,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 7: kill switch re-check (post mark_started) ------
        # If the kill switch was activated between mark_started and
        # mark_dispatched, the slot is READY_TO_CALL.  We cannot
        # mark_unknown from READY_TO_CALL (illegal CAS transition),
        # so we release the reservation instead.  This is a pre-dispatch
        # failure: adapter_call_dispatched=False.
        # R3 P0-11: scope-aware check across all 5 dimensions.
        try:
            ks_active = await kill_switch.is_kill_switch_active_for_scope(
                tenant_id=auth.tenant_id,
                run_id=auth.run_id,
                action_type=auth.action_type,
                adapter_id=binding.adapter_id,
            )
            cancelled = await kill_switch.is_cancelled(auth.run_id)
        except Exception:
            ks_active = True
            cancelled = False
        if ks_active or cancelled:
            # READY_TO_CALL can transition to FAILED (pre-dispatch
            # fail-closed) — use mark_unknown only if we can, otherwise
            # the record stays in READY_TO_CALL which blocks replays.
            # Per the CAS table, READY_TO_CALL → FAILED is legal.
            with contextlib.suppress(Exception):
                await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=(
                    ExecutionStatus.CANCELLED if cancelled else ExecutionStatus.UNKNOWN
                ),
                error_code=KILL_SWITCH_ACTIVE,
                error_message="kill switch activated after mark_started",
                adapter_call_started=True,
                adapter_call_dispatched=False,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 8: mark_dispatched (→ CALL_DISPATCHED) [R3 P0-4] -
        # This MUST succeed before adapter.execute() is entered.
        # Only after this state may an uncertain outcome become UNKNOWN.
        try:
            record = await execution_store.mark_dispatched(record)
        except Exception as e:
            # mark_dispatched failed — the slot is still READY_TO_CALL.
            # We cannot mark_unknown from READY_TO_CALL; release is
            # also not legal from READY_TO_CALL.  Best effort: try
            # mark_unknown (will raise if state is wrong, which is
            # caught by suppress).  The record stays READY_TO_CALL
            # which correctly blocks concurrent attempts.
            with contextlib.suppress(Exception):
                await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"mark_dispatched failed: {e}",
                adapter_call_started=True,
                adapter_call_dispatched=False,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 9: adapter await (P0-6 CancelledError) ------------
        # From here, adapter_call_dispatched=True — uncertain outcomes
        # are UNKNOWN (not CANCELLED / NOT_AUTHORIZED).
        started = clock.now()
        try:
            outcome = await asyncio.wait_for(
                adapter.execute(command),
                timeout=effective_timeout,
            )
            completed = clock.now()
        except asyncio.CancelledError:
            # P0-6: call was dispatched → mark UNKNOWN.
            await execution_store.mark_unknown(record)
            raise
        except asyncio.TimeoutError:
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="adapter call timed out",
                adapter_call_started=True,
                adapter_call_dispatched=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )
        except Exception as e:
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"adapter raised: {e}",
                adapter_call_started=True,
                adapter_call_dispatched=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 10: outcome verification (P0-4) -------------------
        try:
            outcome.verify_integrity()
            outcome.verify_against_command(command)
            outcome.verify_against_binding(binding)
        except Exception as e:
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=ADAPTER_BINDING_DRIFT,
                error_message=f"adapter outcome verification failed: {e}",
                adapter_call_started=True,
                adapter_call_dispatched=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 11: receipt construction (P0-6) -------------------
        receipt_id = f"rcpt-{command_id}"
        try:
            receipt = ActionExecutionReceipt(
                receipt_id=receipt_id,
                command_id=command_id,
                tenant_id=auth.tenant_id,
                run_id=auth.run_id,
                proposal_id=auth.proposal_id,
                authorization_hash=auth.authorization_hash,
                approval_decision_hash=auth.approval_decision_hash,
                adapter_id=binding.adapter_id,
                adapter_version=binding.adapter_version,
                adapter_registry_hash=frozen_registry.registry_hash,
                idempotency_key=auth.idempotency_key,
                execution_fingerprint=fingerprint,
                status=outcome.status,
                executed=outcome.executed,
                external_reference=outcome.external_reference,
                safe_result_summary=outcome.result_payload,
                started_at=started,
                completed_at=completed,
                attempt=command.attempt,
                error_code=outcome.error_code,
            )
            receipt.verify_integrity()
            receipt.verify_against_command(command)
            receipt.verify_against_authorization(auth)
        except Exception as e:
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"receipt construction failed: {e}",
                adapter_call_started=True,
                adapter_call_dispatched=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        # ---- Step 12: complete_with_receipt (P0-6) ------------------
        try:
            await execution_store.complete_with_receipt(record, receipt)
        except Exception:
            await execution_store.mark_unknown(record)
            return ActionExecutionRecord(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="idempotency store failed to commit receipt",
                adapter_call_started=True,
                adapter_call_dispatched=True,
                attempt=attempt,
                command_family_id=command_family_id,
                resource_lock_key=resource_lock_key,
            )

        return ActionExecutionRecord(
            proposal_id=review.proposal_id,
            receipt=receipt,
            status=outcome.status,
            approval_consumption_hash=approval_consumption_hash,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            adapter_call_started=True,
            adapter_call_dispatched=True,
            retryable=outcome.retryable,
            executed=outcome.executed,
            dry_run_succeeded=(outcome.status == ExecutionStatus.DRY_RUN_SUCCEEDED),
            attempt=attempt,
            command_family_id=command_family_id,
            resource_lock_key=resource_lock_key,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _bind_approval(
        self,
        auth: ExecutionAuthorization,
        decision: ApprovalDecision,
        pre_approval_hash: str,
    ) -> ExecutionAuthorization:
        """P0-1: return a new authorization with the approval decision
        bound and ``pre_approval_authorization_hash`` saved."""
        return ExecutionAuthorization(
            authorization_id=auth.authorization_id,
            tenant_id=auth.tenant_id,
            run_id=auth.run_id,
            proposal_id=auth.proposal_id,
            action_type=auth.action_type,
            review_request_hash=auth.review_request_hash,
            review_result_hash=auth.review_result_hash,
            proposal_review_hash=auth.proposal_review_hash,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            governance_spec_hash=auth.governance_spec_hash,
            adapter_registry_hash=auth.adapter_registry_hash,
            status=ExecutionStatus.READY,
            approval_required=True,
            approval_id=decision.approval_id,
            approval_decision_hash=decision.decision_hash,
            pre_approval_authorization_hash=pre_approval_hash,
            risk_level=auth.risk_level,
            idempotency_key=auth.idempotency_key,
            dry_run=auth.dry_run,
            created_by_agent=auth.created_by_agent,
            agent_version=auth.agent_version,
        )

    def _build_approval_request(
        self,
        auth: ExecutionAuthorization,
        review: ProposalReview,
        gov_spec: ActionGovernanceSpec,
        approval_id: str,
        clock: Clock,
    ) -> ApprovalRequest:
        """P0-1 / P0-9 R3: build an :class:`ApprovalRequest` for a
        proposal that requires human approval.

        R3 fixes:

        * ``authorization_hash`` stores ``approval_subject_hash`` (what
          the human approver sees and approves) — NOT the final
          ``authorization_hash`` (which changes after the decision is
          bound via :meth:`_bind_approval`).
        * ``required_approver_roles`` and ``approval_ttl_seconds`` come
          from the governance spec — NO hardcoded defaults.
        """
        now = clock.now()
        # P0-1: the request binds to approval_subject_hash (stable
        # across decision binding), NOT the final authorization_hash.
        subject_hash = auth.approval_subject_hash or auth.authorization_hash
        # P0-9: roles and TTL come from governance spec.
        expires_at = now + timedelta(seconds=gov_spec.approval_ttl_seconds)
        return ApprovalRequest(
            approval_id=approval_id,
            authorization_id=auth.authorization_id,
            tenant_id=auth.tenant_id,
            run_id=auth.run_id,
            proposal_id=auth.proposal_id,
            review_request_hash=auth.review_request_hash,
            review_result_hash=auth.review_result_hash,
            authorization_hash=subject_hash,
            risk_level=review.risk_level,
            action_type=auth.action_type,
            action_summary=f"Approval required for {auth.action_type}",
            required_approver_roles=gov_spec.required_approver_roles,
            requested_by=auth.created_by_agent,
            requested_at=now,
            expires_at=expires_at,
        )

    def _build_blocked_batch(
        self,
        request: ReviewRequest,
        result: ReviewBatchResult,
        adapter_registry: ActionAdapterRegistry,
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
        *,
        blocked_ids: tuple[str, ...] = (),
    ) -> ExecutionBatchResult:
        """P0-7: build a BLOCKED batch result for proposals that exist
        but cannot execute (all REJECTED / NEEDS_INPUT / etc.)."""
        snap = adapter_registry.freeze_snapshot()
        completed = clock.now()
        return ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            receipts=(),
            blocked_proposal_ids=blocked_ids,
            batch_status=BatchExecutionStatus.BLOCKED,
            started_at=started_at,
            completed_at=completed,
            dry_run=opts.dry_run,
        )

    def _fail_batch(
        self,
        request: ReviewRequest,
        result: ReviewBatchResult,
        adapter_registry: ActionAdapterRegistry,
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
        *,
        error_code: str,
        error_message: str,
    ) -> ExecutionBatchResult:
        snap = adapter_registry.freeze_snapshot()
        completed = clock.now()
        return ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            receipts=(),
            batch_status=BatchExecutionStatus.BLOCKED,
            started_at=started_at,
            completed_at=completed,
            dry_run=opts.dry_run,
            error_code=error_code,
        )

    def _build_empty_batch(
        self,
        request: ReviewRequest,
        result: ReviewBatchResult,
        adapter_registry: ActionAdapterRegistry,
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
    ) -> ExecutionBatchResult:
        snap = adapter_registry.freeze_snapshot()
        completed = clock.now()
        return ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            receipts=(),
            batch_status=BatchExecutionStatus.NO_ACTIONS,
            started_at=started_at,
            completed_at=completed,
            dry_run=opts.dry_run,
        )

    def _assemble_batch(
        self,
        request: ReviewRequest,
        result: ReviewBatchResult,
        frozen_registry: FrozenActionAdapterRegistry,
        per_action: list[ActionExecutionRecord],
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
        *,
        dry_run: bool,
        approval_requests: list[ApprovalRequest] | None = None,
    ) -> ExecutionBatchResult:
        """P0-10: populate ``action_records`` and derive ALL summary
        fields from the records."""
        action_records = tuple(per_action)

        # Derive ALL summary ID lists from records.
        receipts = tuple(r.receipt for r in per_action if r.receipt is not None)
        skipped = tuple(sorted(r.proposal_id for r in per_action if r.skipped))
        blocked = tuple(
            sorted(
                r.proposal_id
                for r in per_action
                if r.status == ExecutionStatus.NOT_AUTHORIZED and not r.skipped
            )
        )
        pending_approval = tuple(
            sorted(
                r.proposal_id
                for r in per_action
                if r.status == ExecutionStatus.PENDING_APPROVAL
            )
        )
        failed = tuple(
            sorted(
                r.proposal_id for r in per_action if r.status == ExecutionStatus.FAILED
            )
        )
        unknown = tuple(
            sorted(
                r.proposal_id for r in per_action if r.status == ExecutionStatus.UNKNOWN
            )
        )
        # P0-1: DRY_RUN_SUCCEEDED is NOT counted as real SUCCEEDED.
        dry_run_succeeded_ids = tuple(
            sorted(r.proposal_id for r in per_action if r.dry_run_succeeded)
        )
        # Only real SUCCEEDED / DEDUPLICATED (not DRY_RUN_SUCCEEDED)
        # counts as succeeded.
        succeeded = tuple(
            sorted(
                r.proposal_id
                for r in per_action
                if r.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.DEDUPLICATED)
                and not r.dry_run_succeeded
            )
        )

        # P0-10: compute batch status from records.
        batch_status = _compute_batch_status_from_records(action_records)

        error_code: str | None = None
        has_unknown = bool(unknown)
        has_failed = bool(failed)
        if has_unknown:
            error_code = EXECUTION_OUTCOME_UNKNOWN
        elif has_failed:
            for r in per_action:
                if r.status == ExecutionStatus.FAILED and r.error_code:
                    error_code = r.error_code
                    break

        return ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=frozen_registry.registry_hash,
            receipts=receipts,
            approval_requests=tuple(approval_requests or []),
            action_records=action_records,
            skipped_proposal_ids=skipped,
            blocked_proposal_ids=blocked,
            pending_approval_proposal_ids=pending_approval,
            failed_proposal_ids=failed,
            unknown_proposal_ids=unknown,
            succeeded_proposal_ids=succeeded,
            dry_run_succeeded_proposal_ids=dry_run_succeeded_ids,
            batch_status=batch_status,
            started_at=started_at,
            completed_at=clock.now(),
            dry_run=dry_run,
            error_code=error_code,
        )


__all__ = [
    "ActionExecutionRecord",
    "ExecutionAttemptRecord",
    "ExecutionBatchResult",
    "ExecutionOptions",
    "ExecutionRetryPolicy",
    "GovernedExecutor",
    "build_authorization",
    "select_executable_reviews",
]
