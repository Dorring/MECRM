"""Phase 5B — Governed Executor (core entry point).

The :class:`GovernedExecutor` is the ONLY component that may invoke an
:class:`ActionAdapter`.  It enforces the 18-step fixed-order pipeline
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
15. Mark the idempotency record as IN_PROGRESS.
16. Invoke the adapter (single attempt by default).
17. Mark the idempotency record terminal (SUCCEEDED / FAILED / UNKNOWN).
18. Build the :class:`ActionExecutionReceipt` and the batch result.

The executor NEVER bypasses a step, NEVER auto-retries UNKNOWN
outcomes, and NEVER calls the adapter without a valid authorization
+ idempotency reservation + kill-switch check.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from hmac import compare_digest

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.action_adapter import (
    ActionAdapter,
    ActionAdapterRegistry,
    ActionAdapterRegistrySnapshot,
    ExecutionCommand,
    compute_execution_fingerprint,
)
from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ActionGovernanceSpec,
    get_action_governance_spec,
)
from multi_agent.approval_contracts import (
    ApprovalDecision,
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
    ADAPTER_NOT_FOUND,
    APPROVAL_REQUIRED,
    AUTHORIZATION_INTEGRITY_FAILED,
    EXECUTION_OUTCOME_UNKNOWN,
    GOVERNANCE_SPEC_MISMATCH,
    KILL_SWITCH_ACTIVE,
    REVIEW_BINDING_MISMATCH,
    ApprovalRequiredError,
    ApprovalValidationError,
    ExecutionIntegrityError,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import (
    ExecutionStore,
    IdempotencyRecord,
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
    default.  ``retry_only_when_safe`` (default True) means retries are
    only attempted when the adapter declares ``retry_safe=True``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_retries: int = 0
    retryable_error_codes: frozenset[str] = frozenset()
    retry_only_when_safe: bool = True

    @field_validator("max_retries")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries must be >= 0")
        return v


class ExecutionOptions(StrictContract):
    """Per-batch execution options.

    All defaults are CI-safe: no network, bounded timeouts, low
    concurrency, dry-run off but safe to flip on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_deadline_seconds: float = 300.0
    per_action_timeout_seconds: float = 30.0
    max_concurrency: int = 4
    retry_policy: ExecutionRetryPolicy = ExecutionRetryPolicy()
    dry_run: bool = False

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
# Per-action result (internal)
# ---------------------------------------------------------------------------


class _ActionExecutionResult(StrictContract):
    """Internal per-action outcome carrying receipt + terminal status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    receipt: ActionExecutionReceipt | None = None
    status: ExecutionStatus
    error_code: str | None = None
    error_message: str | None = None
    skipped: bool = False


# ---------------------------------------------------------------------------
# ExecutionBatchResult
# ---------------------------------------------------------------------------


class ExecutionBatchResult(StrictContract):
    """Frozen, hash-stable aggregate result for one execution batch.

    ``batch_status`` is the highest-priority status across all
    per-action receipts.  ``NO_ACTIONS`` is NEVER equivalent to
    ``SUCCEEDED`` (Phase 5B Section 22).
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
    skipped_proposal_ids: tuple[str, ...] = ()
    blocked_proposal_ids: tuple[str, ...] = ()
    pending_approval_proposal_ids: tuple[str, ...] = ()
    failed_proposal_ids: tuple[str, ...] = ()
    unknown_proposal_ids: tuple[str, ...] = ()
    succeeded_proposal_ids: tuple[str, ...] = ()

    batch_status: BatchExecutionStatus = BatchExecutionStatus.NO_ACTIONS
    started_at: datetime
    completed_at: datetime
    dry_run: bool = False
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
        # NO_ACTIONS requires no receipts.
        if self.batch_status == BatchExecutionStatus.NO_ACTIONS and self.receipts:
            raise ValueError("batch_status NO_ACTIONS but receipts is non-empty")
        if not self.receipts and self.batch_status not in (
            BatchExecutionStatus.NO_ACTIONS,
            BatchExecutionStatus.BLOCKED,
            BatchExecutionStatus.PENDING_APPROVAL,
        ):
            raise ValueError(
                f"empty receipts but batch_status is "
                f"{self.batch_status.value!r} (expected NO_ACTIONS, "
                f"BLOCKED, or PENDING_APPROVAL)"
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

    def verify_against_review(
        self, request: ReviewRequest, result: ReviewBatchResult
    ) -> None:
        """Bind the batch result back to its Review."""
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

    auth = ExecutionAuthorization(
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
    # Re-bind the authorization_hash now that we know the approval
    # requirement — the hash covers approval_required, so a tampered
    # authorization cannot silently drop the approval flag.
    return auth


# ---------------------------------------------------------------------------
# GovernedExecutor
# ---------------------------------------------------------------------------


class GovernedExecutor:
    """The ONLY component that may invoke an :class:`ActionAdapter`.

    Stateless itself — every durable boundary (approval store,
    idempotency store, adapter registry, kill switch) is injected per
    call so tests stay deterministic and there is no hidden global
    state.
    """

    def __init__(self) -> None:
        self._approval_gate = ApprovalGate()

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
        """Execute the batch following the 18-step fixed-order pipeline."""
        opts = options or ExecutionOptions()
        started_at = clock.now()

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
        if request.governance_spec_hash != ACTION_GOVERNANCE_SPEC_HASH:
            return self._fail_batch(
                request,
                review_result,
                adapter_registry,
                opts,
                started_at,
                clock,
                error_code=GOVERNANCE_SPEC_MISMATCH,
                error_message="governance spec hash drift between request and live registry",
            )

        # ---- Step 5: select executable reviews ------------------------
        executable = select_executable_reviews(request, review_result)
        if not executable:
            return self._build_empty_batch(
                request, review_result, adapter_registry, opts, started_at, clock
            )

        # ---- Step 6-11: per-proposal authorization + approval ----------
        registry_snapshot = adapter_registry.freeze_snapshot()
        per_action: list[_ActionExecutionResult] = []
        skipped_ids: list[str] = []
        blocked_ids: list[str] = []
        pending_approval_ids: list[str] = []

        for review in executable:
            auth = build_authorization(
                request,
                review_result,
                review,
                adapter_registry_hash=registry_snapshot.registry_hash,
                dry_run=opts.dry_run,
            )
            # Step 7: verify authorization against the review.
            try:
                auth.verify_integrity()
                auth.verify_against_review(request, review_result, review)
            except Exception as e:
                per_action.append(
                    _ActionExecutionResult(
                        proposal_id=review.proposal_id,
                        status=ExecutionStatus.NOT_AUTHORIZED,
                        error_code=AUTHORIZATION_INTEGRITY_FAILED,
                        error_message=str(e),
                        skipped=True,
                    )
                )
                blocked_ids.append(review.proposal_id)
                continue

            # Resolve governance spec for this action.
            gov_spec = get_action_governance_spec(auth.action_type)
            if gov_spec is None:
                per_action.append(
                    _ActionExecutionResult(
                        proposal_id=review.proposal_id,
                        status=ExecutionStatus.NOT_AUTHORIZED,
                        error_code=ACTION_NOT_SUPPORTED,
                        error_message=f"unknown action_type {auth.action_type!r}",
                        skipped=True,
                    )
                )
                blocked_ids.append(review.proposal_id)
                continue

            # Step 8-10: approval gate.
            requirement = self._approval_gate.resolve_approval_requirement(
                review, auth, gov_spec
            )
            if requirement.required:
                if auth.approval_id is None:
                    pending_approval_ids.append(review.proposal_id)
                    per_action.append(
                        _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.PENDING_APPROVAL,
                            error_code=APPROVAL_REQUIRED,
                            error_message=requirement.reason,
                            skipped=True,
                        )
                    )
                    continue
                # Try to consume an existing approval decision.
                try:
                    decision = await approval_store.consume(
                        auth.approval_id, auth.authorization_hash
                    )
                except ApprovalRequiredError:
                    pending_approval_ids.append(review.proposal_id)
                    per_action.append(
                        _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.PENDING_APPROVAL,
                            error_code=APPROVAL_REQUIRED,
                            error_message="no decision yet",
                            skipped=True,
                        )
                    )
                    continue
                except ApprovalValidationError as e:
                    blocked_ids.append(review.proposal_id)
                    per_action.append(
                        _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.NOT_AUTHORIZED,
                            error_code=e.error_code,
                            error_message=str(e),
                            skipped=True,
                        )
                    )
                    continue
                # Validate the decision.
                approval_request = await approval_store.get(auth.approval_id)
                if approval_request is None:
                    blocked_ids.append(review.proposal_id)
                    per_action.append(
                        _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.NOT_AUTHORIZED,
                            error_code=APPROVAL_REQUIRED,
                            error_message="approval request not found",
                            skipped=True,
                        )
                    )
                    continue
                try:
                    self._approval_gate.validate_decision(
                        decision, approval_request, auth
                    )
                except ApprovalValidationError as e:
                    blocked_ids.append(review.proposal_id)
                    per_action.append(
                        _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.NOT_AUTHORIZED,
                            error_code=e.error_code,
                            error_message=str(e),
                            skipped=True,
                        )
                    )
                    continue
                # Bind the decision hash to the authorization.
                auth = self._bind_approval(auth, decision)

            # Step 11-18: execute the action.
            outcome = await self._execute_one(
                request=request,
                review_result=review_result,
                review=review,
                auth=auth,
                gov_spec=gov_spec,
                registry_snapshot=registry_snapshot,
                adapter_registry=adapter_registry,
                execution_store=execution_store,
                kill_switch=kill_switch,
                clock=clock,
                opts=opts,
            )
            per_action.append(outcome)
            if outcome.skipped and outcome.status == ExecutionStatus.SKIPPED:
                skipped_ids.append(review.proposal_id)

        return self._assemble_batch(
            request,
            review_result,
            registry_snapshot,
            per_action,
            opts,
            started_at,
            clock,
            dry_run=opts.dry_run,
        )

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
        registry_snapshot: ActionAdapterRegistrySnapshot,
        adapter_registry: ActionAdapterRegistry,
        execution_store: ExecutionStore,
        kill_switch,
        clock: Clock,
        opts: ExecutionOptions,
    ) -> _ActionExecutionResult:
        # Locate the proposal snapshot for the canonical payload.
        snapshot = None
        for snap in request.proposals:
            if snap.proposal_id == review.proposal_id:
                snapshot = snap
                break
        assert snapshot is not None

        # Step 12: build the command + fingerprint.
        try:
            binding = adapter_registry.get_binding(auth.action_type, registry_snapshot)
        except KeyError:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=ACTION_NOT_SUPPORTED,
                error_message=f"no adapter bound for {auth.action_type!r}",
                skipped=True,
            )
        # Verify adapter version matches the binding (fail-closed).
        if binding.adapter_id != binding.adapter_id:
            pass
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
            registry_hash=registry_snapshot.registry_hash,
            idempotency_key=auth.idempotency_key,
            dry_run=auth.dry_run,
        )
        command_id = f"cmd-{auth.proposal_id}-{uuid.uuid4().hex[:8]}"
        command = ExecutionCommand(
            command_id=command_id,
            authorization=auth,
            proposal_snapshot_hash=auth.proposal_snapshot_hash,
            proposal_origin_hash=auth.proposal_origin_hash,
            action_type=auth.action_type,
            adapter_id=binding.adapter_id,
            adapter_version=binding.adapter_version,
            dry_run=auth.dry_run,
            attempt=1,
            timeout_seconds=opts.per_action_timeout_seconds,
            execution_fingerprint=fingerprint,
        )

        # Step 13: reserve the idempotency slot.
        try:
            record = await execution_store.reserve(
                auth.tenant_id, auth.idempotency_key, fingerprint, command_id
            )
        except Exception as e:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=getattr(e, "error_code", REVIEW_BINDING_MISMATCH),
                error_message=str(e),
                skipped=True,
            )

        # Dedup shortcut: a previously SUCCEEDED record with the same
        # fingerprint returns the cached receipt_id (DEDUPLICATED).
        if record.state == IdempotencyState.SUCCEEDED and record.receipt_id:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.DEDUPLICATED,
                receipt=self._build_dedup_receipt(
                    command, record, auth, binding, clock
                ),
            )
        # UNKNOWN outcomes are NOT auto-retried (Phase 5B Section 17).
        if record.state == IdempotencyState.UNKNOWN:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="previous outcome was UNKNOWN — manual intervention required",
            )
        # IN_PROGRESS / FAILED with same fingerprint → blocked.
        if record.state == IdempotencyState.IN_PROGRESS:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="idempotency slot is IN_PROGRESS",
            )

        # Step 14: kill switch (BEFORE the adapter call).
        try:
            ks_active = await kill_switch.is_kill_switch_active(auth.tenant_id)
            cancelled = await kill_switch.is_cancelled(auth.run_id)
        except Exception:
            ks_active = True
            cancelled = False
        if ks_active:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=KILL_SWITCH_ACTIVE,
                error_message="kill switch active for tenant",
            )
        if cancelled:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.CANCELLED,
                error_code=KILL_SWITCH_ACTIVE,
                error_message="run cancelled",
            )

        # Step 15: mark IN_PROGRESS.
        try:
            record = await execution_store.mark_started(record, command_id)
        except Exception as e:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=str(e),
            )

        # Step 16: invoke the adapter.
        adapter = self._lookup_adapter(adapter_registry, binding)
        if adapter is None:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=ADAPTER_NOT_FOUND,
                error_message=f"adapter {binding.adapter_id!r} not registered",
            )
        started = clock.now()
        try:
            outcome = await asyncio.wait_for(
                adapter.execute(command),
                timeout=opts.per_action_timeout_seconds,
            )
            completed = clock.now()
        except asyncio.TimeoutError:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="adapter call timed out",
            )
        except Exception as e:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"adapter raised: {e}",
            )

        # Step 17: mark the idempotency record terminal.
        succeeded = outcome.status == ExecutionStatus.SUCCEEDED
        receipt_id = f"rcpt-{command_id}"
        try:
            if succeeded:
                await execution_store.complete(record, receipt_id, succeeded=True)
            elif outcome.status == ExecutionStatus.FAILED:
                await execution_store.complete(record, receipt_id, succeeded=False)
            else:
                # UNKNOWN — do NOT retry (Section 17).
                await execution_store.mark_unknown(record)
        except Exception:
            # Store failure → fail-closed UNKNOWN.
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="idempotency store failed to complete",
            )

        # Step 18: build the receipt.
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
            adapter_registry_hash=registry_snapshot.registry_hash,
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
        return _ActionExecutionResult(
            proposal_id=review.proposal_id,
            receipt=receipt,
            status=outcome.status,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _bind_approval(
        self, auth: ExecutionAuthorization, decision: ApprovalDecision
    ) -> ExecutionAuthorization:
        """Return a new authorization with the approval decision bound."""
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
            risk_level=auth.risk_level,
            idempotency_key=auth.idempotency_key,
            dry_run=auth.dry_run,
            created_by_agent=auth.created_by_agent,
            agent_version=auth.agent_version,
        )

    def _lookup_adapter(
        self, registry: ActionAdapterRegistry, binding
    ) -> ActionAdapter | None:
        """Return the adapter instance for *binding*, if registered.

        The :class:`ActionAdapterRegistry` only stores bindings (frozen
        metadata); the live adapter instances are held by the caller
        and looked up here.  For the default noop / recording
        adapters used in tests, the registry IS the adapter source —
        we keep a side-table of live adapters.
        """
        live = getattr(registry, "_live_adapters", None)
        if live is None:
            return None
        return live.get(binding.adapter_id)

    def _build_dedup_receipt(
        self,
        command: ExecutionCommand,
        record: IdempotencyRecord,
        auth: ExecutionAuthorization,
        binding,
        clock: Clock,
    ) -> ActionExecutionReceipt:
        now = clock.now()
        return ActionExecutionReceipt(
            receipt_id=record.receipt_id or f"rcpt-dedup-{command.command_id}",
            command_id=command.command_id,
            tenant_id=auth.tenant_id,
            run_id=auth.run_id,
            proposal_id=auth.proposal_id,
            authorization_hash=auth.authorization_hash,
            approval_decision_hash=auth.approval_decision_hash,
            adapter_id=binding.adapter_id,
            adapter_version=binding.adapter_version,
            adapter_registry_hash=auth.adapter_registry_hash,
            idempotency_key=auth.idempotency_key,
            execution_fingerprint=command.execution_fingerprint,
            status=ExecutionStatus.DEDUPLICATED,
            executed=True,
            external_reference=record.receipt_id,
            safe_result_summary={"deduplicated": True},
            started_at=now,
            completed_at=now,
            attempt=command.attempt,
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
        registry_snapshot: ActionAdapterRegistrySnapshot,
        per_action: list[_ActionExecutionResult],
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
        *,
        dry_run: bool,
    ) -> ExecutionBatchResult:
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
        succeeded = tuple(
            sorted(
                r.proposal_id
                for r in per_action
                if r.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.DEDUPLICATED)
            )
        )

        # Compute the batch status via priority.
        # PARTIAL_SUCCESS is special: it applies when at least one
        # action SUCCEEDED and at least one FAILED (or UNKNOWN), but
        # not when ALL failed (that's just FAILED).
        has_succeeded = bool(succeeded)
        has_failed = bool(failed)
        has_unknown = bool(unknown)

        if not per_action:
            batch_status = BatchExecutionStatus.NO_ACTIONS
        elif has_unknown:
            batch_status = BatchExecutionStatus.UNKNOWN
        elif has_succeeded and has_failed:
            # Mixed success/failure → PARTIAL_SUCCESS (Section 23)
            batch_status = BatchExecutionStatus.PARTIAL_SUCCESS
        elif has_failed and not has_succeeded:
            batch_status = BatchExecutionStatus.FAILED
        elif pending_approval and not has_succeeded:
            batch_status = BatchExecutionStatus.PENDING_APPROVAL
        elif blocked and not has_succeeded:
            batch_status = BatchExecutionStatus.BLOCKED
        elif has_succeeded:
            batch_status = BatchExecutionStatus.SUCCEEDED
        else:
            statuses: list[BatchExecutionStatus] = [
                self._action_status_to_batch(r.status) for r in per_action
            ]
            batch_status = max(statuses, key=batch_execution_status_priority)
        # When no adapter was actually invoked (all actions blocked
        # before execution), the batch is BLOCKED — not UNKNOWN —
        # because there are no receipts to carry the UNKNOWN outcome.
        # PENDING_APPROVAL is exempt: a batch where every action is
        # waiting for approval legitimately has no receipts.
        if not receipts and per_action and not pending_approval:
            batch_status = BatchExecutionStatus.BLOCKED

        error_code = None
        if any(r.status == ExecutionStatus.UNKNOWN for r in per_action):
            error_code = EXECUTION_OUTCOME_UNKNOWN
        elif any(r.status == ExecutionStatus.FAILED for r in per_action):
            error_code = per_action[0].error_code

        return ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=registry_snapshot.registry_hash,
            receipts=receipts,
            skipped_proposal_ids=skipped,
            blocked_proposal_ids=blocked,
            pending_approval_proposal_ids=pending_approval,
            failed_proposal_ids=failed,
            unknown_proposal_ids=unknown,
            succeeded_proposal_ids=succeeded,
            batch_status=batch_status,
            started_at=started_at,
            completed_at=clock.now(),
            dry_run=dry_run,
            error_code=error_code,
        )

    @staticmethod
    def _action_status_to_batch(status: ExecutionStatus) -> BatchExecutionStatus:
        mapping = {
            ExecutionStatus.SUCCEEDED: BatchExecutionStatus.SUCCEEDED,
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
        return mapping[status]


__all__ = [
    "ExecutionBatchResult",
    "ExecutionOptions",
    "ExecutionRetryPolicy",
    "GovernedExecutor",
    "build_authorization",
    "select_executable_reviews",
]
