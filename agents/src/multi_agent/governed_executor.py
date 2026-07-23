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
from datetime import datetime, timedelta, timezone
from hmac import compare_digest

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.action_adapter import (
    ActionAdapter,
    ActionAdapterBinding,
    ActionAdapterRegistry,
    ActionAdapterRegistrySnapshot,
    ExecutionCommand,
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
    dry_run_succeeded: bool = False


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
# Deterministic ID helpers (P1-1 / P1-2)
# ---------------------------------------------------------------------------


def _deterministic_approval_id(auth: ExecutionAuthorization) -> str:
    """P1-1: derive a stable approval_id from the authorization."""
    return f"appr-{auth.proposal_id}-{auth.authorization_hash[:12]}"


def _deterministic_command_id(
    auth: ExecutionAuthorization, fingerprint: str, attempt: int
) -> str:
    """P1-2: derive a stable command_id so replays produce the same id."""
    raw = f"{auth.authorization_hash}:{fingerprint}:{attempt}"
    return f"cmd-{auth.proposal_id}-{stable_hash(raw)[:12]}"


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
        # P0-8: Phase 1 (sequential prepare) builds authorizations and
        # resolves approvals; Phase 2 (concurrent execute) runs the
        # adapter calls under a Semaphore + per-resource Lock with the
        # batch deadline and retry policy enforced.
        registry_snapshot = adapter_registry.freeze_snapshot()
        per_action: list[_ActionExecutionResult] = []
        approval_requests_created: list[ApprovalRequest] = []

        ready: list[
            tuple[ProposalReview, ExecutionAuthorization, ActionGovernanceSpec]
        ] = []

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
                continue

            # Step 8-10: approval gate.
            requirement = self._approval_gate.resolve_approval_requirement(
                review, auth, gov_spec
            )
            if requirement.required:
                if auth.approval_id is None:
                    # P0-2: create an ApprovalRequest so the approval
                    # store has a durable record the human approver can
                    # act on.  The approval_id is deterministic so a
                    # replay does not create a duplicate request.
                    approval_id = _deterministic_approval_id(auth)
                    approval_req = self._build_approval_request(
                        auth, review, gov_spec, approval_id, clock
                    )
                    try:
                        await approval_store.create(approval_req)
                    except ApprovalConflictError as e:
                        per_action.append(
                            _ActionExecutionResult(
                                proposal_id=review.proposal_id,
                                status=ExecutionStatus.NOT_AUTHORIZED,
                                error_code=APPROVAL_CONFLICT,
                                error_message=str(e),
                                skipped=True,
                            )
                        )
                        continue
                    approval_requests_created.append(approval_req)
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
                # P0-3: atomically validate-and-consume.  ALL checks
                # (tenant, run, proposal, authorization_hash, request
                # hash, approver role, expiry, status) run under the
                # store lock before the approval is marked CONSUMED.
                try:
                    decision = await approval_store.validate_and_consume(
                        auth.approval_id,
                        authorization=auth,
                        now=clock.now(),
                    )
                except ApprovalRequiredError:
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

            ready.append((review, auth, gov_spec))

        # ---- Phase 2: concurrent execute (P0-8) ----------------------
        if ready:
            concurrent_results = await self._execute_concurrent(
                ready_actions=ready,
                request=request,
                review_result=review_result,
                registry_snapshot=registry_snapshot,
                adapter_registry=adapter_registry,
                execution_store=execution_store,
                kill_switch=kill_switch,
                clock=clock,
                opts=opts,
                started_at=started_at,
            )
            per_action.extend(concurrent_results)

        return self._assemble_batch(
            request,
            review_result,
            registry_snapshot,
            per_action,
            opts,
            started_at,
            clock,
            dry_run=opts.dry_run,
            approval_requests=approval_requests_created,
        )

    # -----------------------------------------------------------------
    # Concurrent execution (P0-8)
    # -----------------------------------------------------------------

    async def _execute_concurrent(
        self,
        *,
        ready_actions: list[
            tuple[ProposalReview, ExecutionAuthorization, ActionGovernanceSpec]
        ],
        request: ReviewRequest,
        review_result: ReviewBatchResult,
        registry_snapshot: ActionAdapterRegistrySnapshot,
        adapter_registry: ActionAdapterRegistry,
        execution_store: ExecutionStore,
        kill_switch,
        clock: Clock,
        opts: ExecutionOptions,
        started_at: datetime,
    ) -> list[_ActionExecutionResult]:
        """P0-8: run ready actions concurrently under a Semaphore +
        per-resource Lock, enforcing the batch deadline and retry policy.

        Concurrency rules:

        * ``asyncio.Semaphore(max_concurrency)`` bounds the number of
          in-flight adapter calls.
        * Actions sharing the same ``(tenant_id, idempotency_key)``
          resource key are serialized via a per-key ``asyncio.Lock``
          so the same resource is never touched twice in parallel.
        * The batch deadline (``started_at + batch_deadline_seconds``)
          is checked before each action starts; an action that would
          start after the deadline returns ``UNKNOWN`` with
          ``EXECUTION_DEADLINE_EXCEEDED``.
        * Per-action timeout is ``min(per_action_timeout_seconds,
          remaining_deadline)`` so the adapter call never exceeds the
          batch deadline.
        """
        semaphore = asyncio.Semaphore(opts.max_concurrency)
        resource_locks: dict[str, asyncio.Lock] = {}
        deadline = started_at + timedelta(seconds=opts.batch_deadline_seconds)

        async def _run_one(
            review: ProposalReview,
            auth: ExecutionAuthorization,
            gov_spec: ActionGovernanceSpec,
        ) -> _ActionExecutionResult:
            # P0-8: serialize actions on the same resource key.
            resource_key = f"{auth.tenant_id}:{auth.idempotency_key}"
            lock = resource_locks.setdefault(resource_key, asyncio.Lock())
            async with semaphore:
                async with lock:
                    # Check batch deadline against the injected clock.
                    now = clock.now()
                    remaining = (deadline - now).total_seconds()
                    if remaining <= 0:
                        return _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.UNKNOWN,
                            error_code=EXECUTION_DEADLINE_EXCEEDED,
                            error_message=(
                                "batch deadline exceeded before action start"
                            ),
                        )
                    per_action_timeout = min(opts.per_action_timeout_seconds, remaining)
                    try:
                        return await asyncio.wait_for(
                            self._execute_one_with_retry(
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
                                attempt=1,
                                per_action_timeout=per_action_timeout,
                                batch_deadline=deadline,
                            ),
                            timeout=per_action_timeout + 2.0,
                        )
                    except asyncio.TimeoutError:
                        return _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.UNKNOWN,
                            error_code=EXECUTION_OUTCOME_UNKNOWN,
                            error_message="action exceeded deadline / timeout",
                        )
                    except Exception as e:
                        return _ActionExecutionResult(
                            proposal_id=review.proposal_id,
                            status=ExecutionStatus.UNKNOWN,
                            error_code=EXECUTION_OUTCOME_UNKNOWN,
                            error_message=f"unexpected execution error: {e}",
                        )

        tasks = [_run_one(r, a, g) for r, a, g in ready_actions]
        return await asyncio.gather(*tasks)

    async def _execute_one_with_retry(
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
        attempt: int,
        per_action_timeout: float,
        batch_deadline: datetime,
    ) -> _ActionExecutionResult:
        """P0-8: wrap ``_execute_one`` with the retry policy.

        Retry conditions (ALL must hold):

        * ``adapter.retry_safe=True`` (or ``retry_only_when_safe=False``).
        * The outcome was ``FAILED`` with ``executed=False`` (confirmed
          no side-effect — UNKNOWN is NEVER retried).
        * ``error_code`` is in ``retryable_error_codes``.
        * ``attempt <= max_retries``.
        * Batch deadline not exceeded.
        * Kill switch not active.

        Never retried: UNKNOWN, CANCELLED, PENDING_APPROVAL,
        NOT_AUTHORIZED, SUCCEEDED, DRY_RUN_SUCCEEDED, DEDUPLICATED,
        SKIPPED.
        """
        policy = opts.retry_policy
        max_attempts = policy.max_retries + 1

        while True:
            result = await self._execute_one(
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
                attempt=attempt,
                per_action_timeout=per_action_timeout,
            )

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
                return result

            # Only FAILED may be retried, and only under strict conditions.
            if result.status != ExecutionStatus.FAILED:
                return result
            if attempt >= max_attempts:
                return result
            # retry_only_when_safe: look up the adapter binding to check
            # retry_safe.  If the adapter cannot be resolved, do not retry.
            try:
                binding = adapter_registry.get_binding(
                    auth.action_type, registry_snapshot
                )
            except Exception:
                return result
            if policy.retry_only_when_safe and not binding.retry_safe:
                return result
            if result.error_code is None:
                return result
            if result.error_code not in policy.retryable_error_codes:
                return result
            # Check batch deadline and kill switch before retrying.
            if clock.now() > batch_deadline:
                return result
            try:
                ks_active = await kill_switch.is_kill_switch_active(auth.tenant_id)
            except Exception:
                ks_active = True
            if ks_active:
                return result
            attempt += 1

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
        attempt: int = 1,
        per_action_timeout: float | None = None,
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
        # P0-4: the live adapter is verified against the binding AFTER
        # the adapter call (see _lookup_and_verify_adapter).  No noop
        # self-comparison here — the binding was already validated at
        # freeze_snapshot time.
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
        # P1-2: deterministic command_id so a replay produces the same id.
        # P0-8: attempt is parameterized so retries get a distinct id.
        effective_timeout = (
            per_action_timeout
            if per_action_timeout is not None
            else opts.per_action_timeout_seconds
        )
        command_id = _deterministic_command_id(auth, fingerprint, attempt)
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

        # P0-5: check kill switch BEFORE reserving the idempotency slot.
        # Pre-call blocks return BLOCKED / CANCELLED (NOT UNKNOWN) and
        # do NOT touch the idempotency store — no slot has been reserved
        # yet, so there is nothing to mark UNKNOWN.
        try:
            ks_active = await kill_switch.is_kill_switch_active(auth.tenant_id)
            cancelled = await kill_switch.is_cancelled(auth.run_id)
        except Exception:
            ks_active = True
            cancelled = False
        if ks_active:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.NOT_AUTHORIZED,
                error_code=KILL_SWITCH_ACTIVE,
                error_message="kill switch active for tenant",
                skipped=True,
            )
        if cancelled:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.CANCELLED,
                error_code=EXECUTION_CANCELLED_BEFORE_CALL,
                error_message="run cancelled before adapter call",
                skipped=True,
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

        # P0-6: return the ORIGINAL trusted receipt on replay.  A
        # previously SUCCEEDED record with the same fingerprint must
        # return the cached receipt — NOT a fabricated DEDUPLICATED
        # one.  If the store claims SUCCEEDED but has no receipt, the
        # record is marked UNKNOWN (crash window).
        if record.state == IdempotencyState.SUCCEEDED and record.receipt_id:
            original_receipt = await execution_store.get_receipt(
                auth.tenant_id, auth.idempotency_key
            )
            if original_receipt is not None:
                return _ActionExecutionResult(
                    proposal_id=review.proposal_id,
                    receipt=original_receipt,
                    status=original_receipt.status,
                    dry_run_succeeded=(
                        original_receipt.status == ExecutionStatus.DRY_RUN_SUCCEEDED
                    ),
                )
            # Store says SUCCEEDED but no receipt → UNKNOWN (crash window).
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="SUCCEEDED record but no stored receipt",
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

        # P0-5: re-check kill switch after reservation, before call.
        # If the kill switch was activated between reservation and the
        # adapter call, the slot is already RESERVED (not IN_PROGRESS)
        # so we do NOT call mark_unknown — the RESERVED slot can be
        # reused on a later attempt.
        try:
            ks_active = await kill_switch.is_kill_switch_active(auth.tenant_id)
            cancelled = await kill_switch.is_cancelled(auth.run_id)
        except Exception:
            ks_active = True
            cancelled = False
        if ks_active or cancelled:
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=(
                    ExecutionStatus.CANCELLED
                    if cancelled
                    else ExecutionStatus.NOT_AUTHORIZED
                ),
                error_code=KILL_SWITCH_ACTIVE,
                error_message="kill switch activated after reservation",
                skipped=True,
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
        # P0-4: verify the live adapter matches the frozen binding.
        adapter = self._lookup_and_verify_adapter(
            adapter_registry, binding, dry_run=auth.dry_run
        )
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
                timeout=effective_timeout,
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

        # P0-4: verify the outcome against the command and the frozen
        # binding BEFORE any store commit.  A tampered or mis-routed
        # outcome is detected here and marked UNKNOWN (fail-closed).
        try:
            outcome.verify_integrity()
            outcome.verify_against_command(command)
            outcome.verify_against_binding(binding)
        except Exception as e:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=ADAPTER_BINDING_DRIFT,
                error_message=f"adapter outcome verification failed: {e}",
            )

        # P0-6: build receipt BEFORE store terminal commit.  The receipt
        # is verified against the command and authorization before it is
        # committed atomically with the terminal idempotency state.  This
        # prevents the crash-window where the store is SUCCEEDED but no
        # trusted receipt exists.
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
            receipt.verify_integrity()
            receipt.verify_against_command(command)
            receipt.verify_against_authorization(auth)
        except Exception as e:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message=f"receipt construction failed: {e}",
            )

        # P0-6: atomically commit state + receipt.
        try:
            await execution_store.complete_with_receipt(record, receipt)
        except Exception:
            await execution_store.mark_unknown(record)
            return _ActionExecutionResult(
                proposal_id=review.proposal_id,
                status=ExecutionStatus.UNKNOWN,
                error_code=EXECUTION_OUTCOME_UNKNOWN,
                error_message="idempotency store failed to commit receipt",
            )

        return _ActionExecutionResult(
            proposal_id=review.proposal_id,
            receipt=receipt,
            status=outcome.status,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            dry_run_succeeded=(outcome.status == ExecutionStatus.DRY_RUN_SUCCEEDED),
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

    def _lookup_and_verify_adapter(
        self,
        registry: ActionAdapterRegistry,
        binding: ActionAdapterBinding,
        dry_run: bool,
    ) -> ActionAdapter | None:
        """P0-4: return the live adapter for *binding* after verifying it
        matches the frozen binding on every field that affects safety.

        The :class:`ActionAdapterRegistry` stores frozen bindings
        (metadata); the live adapter instances are held in a side-table
        on the registry.  This method verifies the live adapter's
        ``adapter_id``, ``adapter_version``, ``supported_action_types``,
        ``supports_dry_run``, ``retry_safe``, and ``idempotency_scope``
        all match the frozen binding — a drifted adapter is rejected
        (returns ``None`` → caller marks the record UNKNOWN).
        """
        live = getattr(registry, "_live_adapters", None)
        if live is None:
            return None
        adapter = live.get(binding.adapter_id)
        if adapter is None:
            return None
        if adapter.adapter_id != binding.adapter_id:
            return None
        if adapter.adapter_version != binding.adapter_version:
            return None
        if binding.action_type not in adapter.supported_action_types:
            return None
        if dry_run and not adapter.supports_dry_run:
            return None
        if adapter.retry_safe != binding.retry_safe:
            return None
        if adapter.idempotency_scope != binding.idempotency_scope:
            return None
        return adapter

    def _build_approval_request(
        self,
        auth: ExecutionAuthorization,
        review: ProposalReview,
        gov_spec: ActionGovernanceSpec,
        approval_id: str,
        clock: Clock,
    ) -> ApprovalRequest:
        """P0-2: build an :class:`ApprovalRequest` for a proposal that
        requires human approval."""
        now = clock.now()
        expires_at = now + timedelta(hours=24)
        return ApprovalRequest(
            approval_id=approval_id,
            authorization_id=auth.authorization_id,
            tenant_id=auth.tenant_id,
            run_id=auth.run_id,
            proposal_id=auth.proposal_id,
            review_request_hash=auth.review_request_hash,
            review_result_hash=auth.review_result_hash,
            authorization_hash=auth.authorization_hash,
            risk_level=review.risk_level,
            action_type=auth.action_type,
            action_summary=f"Approval required for {auth.action_type}",
            required_approver_roles=("approver", "admin"),
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
        registry_snapshot: ActionAdapterRegistrySnapshot,
        per_action: list[_ActionExecutionResult],
        opts: ExecutionOptions,
        started_at: datetime,
        clock: Clock,
        *,
        dry_run: bool,
        approval_requests: list[ApprovalRequest] | None = None,
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
        # P0-1: DRY_RUN_SUCCEEDED is NOT counted as real SUCCEEDED.
        dry_run_succeeded_ids = tuple(
            sorted(r.proposal_id for r in per_action if r.dry_run_succeeded)
        )
        # Only real SUCCEEDED (not DRY_RUN_SUCCEEDED) counts as succeeded.
        succeeded = tuple(
            sorted(
                r.proposal_id
                for r in per_action
                if r.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.DEDUPLICATED)
                and not r.dry_run_succeeded
            )
        )

        # P0-7: compute batch status from per-action outcomes.
        has_real_succeeded = bool(succeeded)
        has_dry_run = bool(dry_run_succeeded_ids)
        has_failed = bool(failed)
        has_unknown = bool(unknown)
        has_blocked = bool(blocked)
        has_pending = bool(pending_approval)

        if not per_action:
            batch_status = BatchExecutionStatus.NO_ACTIONS
        elif has_unknown:
            batch_status = BatchExecutionStatus.UNKNOWN
        elif has_failed and has_real_succeeded:
            batch_status = BatchExecutionStatus.PARTIAL_SUCCESS
        elif has_failed and not has_real_succeeded:
            batch_status = BatchExecutionStatus.FAILED
        elif has_pending and not has_real_succeeded:
            batch_status = BatchExecutionStatus.PENDING_APPROVAL
        elif has_blocked and not has_real_succeeded and not has_dry_run:
            batch_status = BatchExecutionStatus.BLOCKED
        elif has_real_succeeded and not has_failed and not has_unknown:
            batch_status = BatchExecutionStatus.SUCCEEDED
        elif (
            has_dry_run
            and not has_real_succeeded
            and not has_failed
            and not has_unknown
        ):
            batch_status = BatchExecutionStatus.DRY_RUN_COMPLETED
        elif has_real_succeeded or has_dry_run:
            # Mixed real + dry_run → use priority.
            statuses: list[BatchExecutionStatus] = [
                self._action_status_to_batch(r.status) for r in per_action
            ]
            batch_status = max(statuses, key=batch_execution_status_priority)
        else:
            batch_status = BatchExecutionStatus.BLOCKED

        error_code = None
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
            adapter_registry_hash=registry_snapshot.registry_hash,
            receipts=receipts,
            approval_requests=tuple(approval_requests or []),
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

    @staticmethod
    def _action_status_to_batch(status: ExecutionStatus) -> BatchExecutionStatus:
        mapping = {
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
        return mapping[status]


__all__ = [
    "ExecutionBatchResult",
    "ExecutionOptions",
    "ExecutionRetryPolicy",
    "GovernedExecutor",
    "build_authorization",
    "select_executable_reviews",
]
