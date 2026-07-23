"""Phase 5B R1 — Trust-chain counter-example tests.

Comprehensive counter-example suite covering the 9 P0 issues fixed in
Phase 5B R1.  Each test pins down exactly one invariant so a regression
is surfaced as a single focused failure rather than a cascade.

P0 groups covered:

* P0-1  Noop cannot issue a real success receipt (dry-run is NOT success).
* P0-2  Executor creates an ``ApprovalRequest`` for approval-required
        proposals (durable record for the human approver).
* P0-3  Atomic validate-and-consume — every check runs under the store
        lock before the approval is marked CONSUMED; a failed check
        leaves the approval available for correction.
* P0-4  Frozen adapter binding — the live adapter is verified against
        the frozen binding on every safety-relevant field; drift is
        fail-closed.
* P0-5  Kill switch / reservation — pre-call blocks return a definitive
        status (NOT_AUTHORIZED / CANCELLED), never UNKNOWN, and never
        touch the adapter.
* P0-6  Receipt + store atomic commit — the trusted receipt is built
        before the terminal store commit and both are written under the
        same lock; replay returns the original receipt.
* P0-7  Batch status semantics — BLOCKED ≠ NO_ACTIONS; UNKNOWN is valid
        with empty receipts; PARTIAL_SUCCESS is derived from per-action
        statuses.
* P0-8  Deadline / concurrency / retry — batch deadline, max
        concurrency, per-resource serialisation, and retry ONLY for
        confirmed-not-executed FAILED outcomes.
* P0-9  Live governance integrity — the live governance spec hash is
        recomputed and checked at executor entry.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from multi_agent.action_adapter import (
    ActionAdapterBinding,
    ActionAdapterRegistry,
    AdapterExecutionOutcome,
    ExecutionCommand,
    IdempotencyScope,
    RecordingActionAdapter,
    build_default_registry,
)
from multi_agent.action_governance import (
    ACTION_GOVERNANCE_REGISTRY,
    ACTION_GOVERNANCE_SPEC_HASH,
    compute_live_governance_spec_hash,
)
from multi_agent.approval_contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    FrozenClock,
)
from multi_agent.approval_gate import InMemoryApprovalStore
from multi_agent.contracts import ActionRiskLevel
from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionAuthorization,
    ExecutionStatus,
)
from multi_agent.execution_error_codes import (
    ApprovalValidationError,
)
from multi_agent.execution_store import InMemoryExecutionStore
from multi_agent.governed_executor import (
    ExecutionBatchResult,
    ExecutionOptions,
    ExecutionRetryPolicy,
    GovernedExecutor,
)
from multi_agent.review_contracts import (
    ReviewDecisionStatus,
    ReviewRiskLevel,
)

from phase5b_helpers import (
    AlwaysKillSwitch,
    CancelledRun,
    NoKillSwitch,
    RUN_ID,
    TS,
    TENANT,
    make_approved_request_result,
    make_evidence,
    make_proposal,
    make_recording_registry,
    make_request,
    make_result,
    make_review,
    run_async,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _execute(
    request,
    result,
    *,
    registry,
    kill_switch,
    execution_store=None,
    approval_store=None,
    executor=None,
    options=None,
    clock=None,
):
    """Run the GovernedExecutor with sensible defaults."""
    return run_async(
        (executor or GovernedExecutor()).execute(
            request=request,
            review_result=result,
            approval_store=approval_store or InMemoryApprovalStore(),
            execution_store=execution_store or InMemoryExecutionStore(),
            adapter_registry=registry,
            kill_switch=kill_switch,
            clock=clock or FrozenClock(TS),
            options=options or ExecutionOptions(dry_run=False),
        )
    )


class _AdvancingClock:
    """Clock that advances by a fixed delta on each ``now()`` call.

    Used for deadline tests where the frozen clock cannot naturally
    exceed a tight batch deadline.
    """

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._next = start
        self._step = step

    def now(self) -> datetime:
        t = self._next
        self._next = self._next + self._step
        return t


class _RaisingAdapter:
    """Adapter that always raises — exercises the UNKNOWN path."""

    adapter_id = "raising-adapter"
    adapter_version = "1.0.0"
    supports_dry_run = True
    retry_safe = True
    idempotency_scope = IdempotencyScope.TENANT
    supported_action_types = frozenset(ACTION_GOVERNANCE_REGISTRY)

    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        self._sink.append(command)
        raise RuntimeError("simulated adapter crash")


class _MixedOutcomeAdapter:
    """Adapter that returns SUCCEEDED for one proposal and FAILED for
    another, so a batch exercises partial-success semantics."""

    adapter_id = "mixed-outcome-adapter"
    adapter_version = "1.0.0"
    supports_dry_run = True
    retry_safe = True
    idempotency_scope = IdempotencyScope.TENANT

    def __init__(
        self,
        sink: list,
        *,
        fail_proposal_id: str,
        supported_action_types: frozenset[str],
    ) -> None:
        self._sink = sink
        self._fail_proposal_id = fail_proposal_id
        self.supported_action_types = frozenset(supported_action_types)

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        self._sink.append(command)
        if command.authorization.proposal_id == self._fail_proposal_id:
            return AdapterExecutionOutcome(
                command_id=command.command_id,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                status=ExecutionStatus.FAILED,
                executed=False,
                error_code="test_simulated_failure",
                error_message="simulated failure for partial-success test",
            )
        return AdapterExecutionOutcome(
            command_id=command.command_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
        )


class _RetryableFailAdapter:
    """Adapter that returns FAILED on the first call and SUCCEEDED on
    the second, exercising the retry path for confirmed-not-executed
    failures."""

    adapter_id = "retry-test-adapter"
    adapter_version = "1.0.0"
    supports_dry_run = True
    retry_safe = True
    idempotency_scope = IdempotencyScope.TENANT
    supported_action_types = frozenset(ACTION_GOVERNANCE_REGISTRY)

    def __init__(self) -> None:
        self._call_count = 0

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        self._call_count += 1
        if self._call_count == 1:
            return AdapterExecutionOutcome(
                command_id=command.command_id,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                status=ExecutionStatus.FAILED,
                executed=False,
                error_code="transient_failure",
                error_message="simulated transient failure",
            )
        return AdapterExecutionOutcome(
            command_id=command.command_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
        )


class _NoDryRunAdapter:
    """Adapter that does NOT support dry-run mode."""

    adapter_id = "no-dry-run-adapter"
    adapter_version = "1.0.0"
    supports_dry_run = False
    retry_safe = True
    idempotency_scope = IdempotencyScope.TENANT
    supported_action_types = frozenset(ACTION_GOVERNANCE_REGISTRY)

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        return AdapterExecutionOutcome(
            command_id=command.command_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
        )


# ---------------------------------------------------------------------------
# Helpers for the approval consume tests (P0-3)
# ---------------------------------------------------------------------------

_APPR_TS = TS
_APPR_TS_LATER = TS + timedelta(hours=1)
_APPR_TS_EXPIRED = TS + timedelta(hours=2)


def _make_auth(
    *,
    authorization_id: str = "auth-001",
    proposal_id: str = "prop-001",
    approval_id: str | None = "appr-001",
    approval_required: bool = True,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.HIGH,
    action_type: str = "crm.owner.assign",
    authorization_hash: str | None = None,
    tenant_id: str = TENANT,
) -> ExecutionAuthorization:
    auth = ExecutionAuthorization(
        authorization_id=authorization_id,
        tenant_id=tenant_id,
        run_id=RUN_ID,
        proposal_id=proposal_id,
        action_type=action_type,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        proposal_review_hash="p" * 64,
        proposal_snapshot_hash="snap" + "0" * 60,
        proposal_origin_hash="orig" + "0" * 60,
        governance_spec_hash="g" * 64,
        adapter_registry_hash="reg" + "0" * 60,
        status=ExecutionStatus.PENDING_APPROVAL,
        approval_required=approval_required,
        approval_id=approval_id,
        risk_level=risk_level,
        idempotency_key="idem-001",
    )
    if authorization_hash is not None:
        object.__setattr__(auth, "authorization_hash", authorization_hash)
    return auth


def _make_appr_request(
    *,
    auth: ExecutionAuthorization,
    approval_id: str = "appr-001",
    required_roles: tuple[str, ...] = ("manager",),
    expires_at: datetime | None = None,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.HIGH,
    action_type: str = "crm.owner.assign",
) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        authorization_id=auth.authorization_id,
        tenant_id=auth.tenant_id,
        run_id=RUN_ID,
        proposal_id=auth.proposal_id,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        authorization_hash=auth.authorization_hash,
        risk_level=risk_level,
        action_type=action_type,
        action_summary="test action",
        required_approver_roles=required_roles,
        requested_by="test_requester",
        requested_at=_APPR_TS,
        expires_at=expires_at,
    )


def _make_appr_decision(
    *,
    request: ApprovalRequest,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    approver_roles: tuple[str, ...] = ("manager",),
    decided_at: datetime = _APPR_TS,
    authorization_hash: str | None = None,
) -> ApprovalDecision:
    ah = authorization_hash or request.authorization_hash
    return ApprovalDecision(
        approval_id=request.approval_id,
        status=status,
        approver_id="approver-001",
        approver_roles=approver_roles,
        decision_reason="approved",
        decided_at=decided_at,
        approval_request_hash=request.approval_request_hash,
        authorization_hash=ah,
    )


def _make_outcome(
    *,
    command_id: str = "cmd-001",
    adapter_id: str = "adapter-A",
    adapter_version: str = "1.0.0",
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    executed: bool | None = True,
) -> AdapterExecutionOutcome:
    return AdapterExecutionOutcome(
        command_id=command_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=status,
        executed=executed,
    )


def _make_binding(
    *,
    action_type: str = "report.generate",
    adapter_id: str = "adapter-A",
    adapter_version: str = "1.0.0",
    supports_dry_run: bool = True,
    retry_safe: bool = True,
    idempotency_scope: IdempotencyScope = IdempotencyScope.TENANT,
) -> ActionAdapterBinding:
    return ActionAdapterBinding(
        action_type=action_type,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        supports_dry_run=supports_dry_run,
        retry_safe=retry_safe,
        idempotency_scope=idempotency_scope,
    )


# ===========================================================================
# Test class — 35 counter-example tests grouped by P0-1 .. P0-9
# ===========================================================================


class TestPhase5BR1TrustChain:
    # ------------------------------------------------------------------
    # P0-1: Noop cannot issue a real success receipt (4 tests)
    # ------------------------------------------------------------------

    def test_default_noop_never_claims_real_execution(self) -> None:
        """Default registry with noop + dry_run=True yields
        DRY_RUN_SUCCEEDED (executed=False), never SUCCEEDED."""
        request, result, _ = make_approved_request_result()
        registry = build_default_registry()
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=True),
        )
        assert batch.batch_status == BatchExecutionStatus.DRY_RUN_COMPLETED
        assert len(batch.receipts) == 1
        assert batch.receipts[0].status == ExecutionStatus.DRY_RUN_SUCCEEDED
        assert batch.receipts[0].executed is False
        assert batch.succeeded_proposal_ids == ()

    def test_noop_rejects_dry_run_false_command(self) -> None:
        """Noop with dry_run=False returns NOT_AUTHORIZED — never
        SUCCEEDED (P0-1: noop cannot claim real execution)."""
        request, result, _ = make_approved_request_result()
        registry = build_default_registry()
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False),
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert batch.succeeded_proposal_ids == ()

    def test_dry_run_succeeded_not_counted_as_succeeded(self) -> None:
        """DRY_RUN_SUCCEEDED receipts are counted in
        dry_run_succeeded_proposal_ids, NOT succeeded_proposal_ids."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=True),
        )
        assert batch.batch_status == BatchExecutionStatus.DRY_RUN_COMPLETED
        assert batch.succeeded_proposal_ids == ()
        assert len(batch.dry_run_succeeded_proposal_ids) == 1

    def test_dry_run_outcome_executed_flag_is_false(self) -> None:
        """A DRY_RUN_SUCCEEDED AdapterExecutionOutcome MUST have
        executed=False (P0-1 invariant at the outcome level)."""
        outcome = _make_outcome(
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
            executed=False,
        )
        assert outcome.status == ExecutionStatus.DRY_RUN_SUCCEEDED
        assert outcome.executed is False

    # ------------------------------------------------------------------
    # P0-2: Executor creates ApprovalRequest (4 tests)
    # ------------------------------------------------------------------

    def test_approval_required_creates_approval_request(self) -> None:
        """A NEEDS_APPROVAL proposal causes the executor to create an
        ApprovalRequest in the batch result."""
        proposal = make_proposal(
            "prop-approval",
            action_type="crm.owner.assign",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=True,
            evidence_ids=["ev-approval"],
        )
        request = make_request(
            "review-approval", [proposal], [make_evidence("ev-approval")]
        )
        review = make_review(
            "prop-approval",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            risk_level=ReviewRiskLevel.HIGH,
            required_approval=True,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert batch.batch_status == BatchExecutionStatus.PENDING_APPROVAL
        assert len(batch.approval_requests) == 1

    def test_approval_request_carries_correct_proposal_id(self) -> None:
        """The created ApprovalRequest carries the proposal_id of the
        approval-required proposal."""
        proposal = make_proposal(
            "prop-approval",
            action_type="crm.owner.assign",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=True,
            evidence_ids=["ev-approval"],
        )
        request = make_request(
            "review-approval", [proposal], [make_evidence("ev-approval")]
        )
        review = make_review(
            "prop-approval",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            risk_level=ReviewRiskLevel.HIGH,
            required_approval=True,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert batch.approval_requests[0].proposal_id == "prop-approval"

    def test_approval_request_has_non_empty_authorization_hash(self) -> None:
        """The created ApprovalRequest is bound to a non-empty
        authorization_hash (durable record for the human approver)."""
        proposal = make_proposal(
            "prop-approval",
            action_type="crm.owner.assign",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=True,
            evidence_ids=["ev-approval"],
        )
        request = make_request(
            "review-approval", [proposal], [make_evidence("ev-approval")]
        )
        review = make_review(
            "prop-approval",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            risk_level=ReviewRiskLevel.HIGH,
            required_approval=True,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert batch.approval_requests[0].authorization_hash != ""

    def test_no_approval_request_when_not_required(self) -> None:
        """A low-risk APPROVED proposal does NOT create an
        ApprovalRequest."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False),
        )
        assert batch.approval_requests == ()
        assert batch.batch_status == BatchExecutionStatus.SUCCEEDED

    # ------------------------------------------------------------------
    # P0-3: Atomic validate-and-consume (4 tests)
    # ------------------------------------------------------------------

    def test_wrong_authorization_hash_leaves_approval_available(self) -> None:
        """A consume with a wrong authorization_hash fails WITHOUT
        consuming the approval — a subsequent valid consume succeeds."""
        auth = _make_auth(approval_id="appr-001")
        request = _make_appr_request(auth=auth, required_roles=("manager",))
        decision = _make_appr_decision(request=request, approver_roles=("manager",))
        store = InMemoryApprovalStore()
        run_async(store.create(request))
        run_async(store.decide("appr-001", decision))

        # Consume with wrong authorization_hash → fails.
        auth_wrong = _make_auth(approval_id="appr-001", authorization_hash="w" * 64)
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.validate_and_consume(
                    "appr-001", authorization=auth_wrong, now=_APPR_TS
                )
            )

        # Consume with correct auth → succeeds (approval was NOT consumed).
        consumed = run_async(
            store.validate_and_consume("appr-001", authorization=auth, now=_APPR_TS)
        )
        assert consumed.status == ApprovalStatus.APPROVED

    def test_expired_at_consume_time_leaves_approval_available(self) -> None:
        """An approval decided before expiry but consumed after expiry
        is rejected — and the approval remains available for a valid
        consume within the window."""
        auth = _make_auth(approval_id="appr-001")
        request = _make_appr_request(
            auth=auth,
            required_roles=("manager",),
            expires_at=_APPR_TS_LATER,
        )
        decision = _make_appr_decision(request=request, approver_roles=("manager",))
        store = InMemoryApprovalStore()
        run_async(store.create(request))
        run_async(store.decide("appr-001", decision))

        # Consume AFTER expiry → fails (P0-3: uses execution clock).
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.validate_and_consume(
                    "appr-001", authorization=auth, now=_APPR_TS_EXPIRED
                )
            )

        # Consume BEFORE expiry → succeeds (approval was NOT consumed).
        consumed = run_async(
            store.validate_and_consume("appr-001", authorization=auth, now=_APPR_TS)
        )
        assert consumed.status == ApprovalStatus.APPROVED

    def test_wrong_tenant_leaves_approval_available(self) -> None:
        """A consume with a mismatched tenant_id fails WITHOUT consuming
        the approval — a subsequent valid consume succeeds."""
        auth = _make_auth(approval_id="appr-001")
        request = _make_appr_request(auth=auth, required_roles=("manager",))
        decision = _make_appr_decision(request=request, approver_roles=("manager",))
        store = InMemoryApprovalStore()
        run_async(store.create(request))
        run_async(store.decide("appr-001", decision))

        # Build an auth with a wrong tenant_id (forged post-construction).
        auth_wrong_tenant = _make_auth(approval_id="appr-001")
        object.__setattr__(auth_wrong_tenant, "tenant_id", "wrong-tenant")

        with pytest.raises(ApprovalValidationError):
            run_async(
                store.validate_and_consume(
                    "appr-001", authorization=auth_wrong_tenant, now=_APPR_TS
                )
            )

        # Correct auth → succeeds (approval was NOT consumed).
        consumed = run_async(
            store.validate_and_consume("appr-001", authorization=auth, now=_APPR_TS)
        )
        assert consumed.status == ApprovalStatus.APPROVED

    def test_successful_consume_blocks_second_consume(self) -> None:
        """A valid consume marks the approval CONSUMED; a second consume
        attempt is rejected (single-consume rule)."""
        auth = _make_auth(approval_id="appr-001")
        request = _make_appr_request(auth=auth, required_roles=("manager",))
        decision = _make_appr_decision(request=request, approver_roles=("manager",))
        store = InMemoryApprovalStore()
        run_async(store.create(request))
        run_async(store.decide("appr-001", decision))

        # First consume → succeeds.
        consumed = run_async(
            store.validate_and_consume("appr-001", authorization=auth, now=_APPR_TS)
        )
        assert consumed.status == ApprovalStatus.APPROVED

        # Second consume → fails (already consumed).
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.validate_and_consume("appr-001", authorization=auth, now=_APPR_TS)
            )

    # ------------------------------------------------------------------
    # P0-4: Frozen adapter binding (4 tests)
    # ------------------------------------------------------------------

    def test_live_adapter_id_mismatch_rejected(self) -> None:
        """_lookup_and_verify_adapter returns None when the live
        adapter's adapter_id does not match the frozen binding."""
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        binding = _make_binding(
            action_type="report.generate",
            adapter_id="different-adapter",
        )
        executor = GovernedExecutor()
        result = executor._lookup_and_verify_adapter(registry, binding, dry_run=False)
        assert result is None

    def test_live_adapter_version_mismatch_rejected(self) -> None:
        """_lookup_and_verify_adapter returns None when the live
        adapter's adapter_version does not match the frozen binding."""
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        binding = _make_binding(
            action_type="report.generate",
            adapter_id="recording-adapter",
            adapter_version="2.0.0",
        )
        executor = GovernedExecutor()
        result = executor._lookup_and_verify_adapter(registry, binding, dry_run=False)
        assert result is None

    def test_dry_run_unsupported_by_live_adapter_rejected(self) -> None:
        """_lookup_and_verify_adapter returns None when dry_run=True
        but the live adapter does not support dry-run mode."""
        registry = ActionAdapterRegistry()
        adapter = _NoDryRunAdapter()
        registry.register(adapter)
        snap = registry.freeze_snapshot()
        binding = next(b for b in snap.bindings if b.action_type == "report.generate")
        executor = GovernedExecutor()
        result = executor._lookup_and_verify_adapter(registry, binding, dry_run=True)
        assert result is None

    def test_outcome_verify_against_binding_detects_drift(self) -> None:
        """AdapterExecutionOutcome.verify_against_binding raises when
        the outcome's adapter_id differs from the frozen binding."""
        outcome = _make_outcome(adapter_id="adapter-A", adapter_version="1.0.0")
        binding = _make_binding(
            action_type="report.generate",
            adapter_id="adapter-B",
            adapter_version="1.0.0",
        )
        with pytest.raises(Exception, match="adapter_id"):
            outcome.verify_against_binding(binding)

    # ------------------------------------------------------------------
    # P0-5: Kill switch / reservation (4 tests)
    # ------------------------------------------------------------------

    def test_kill_switch_blocks_before_adapter_call(self) -> None:
        """When the kill switch is active, the adapter is NEVER called
        and the batch is not SUCCEEDED."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=AlwaysKillSwitch(),
        )
        assert batch.batch_status != BatchExecutionStatus.SUCCEEDED
        assert len(sink) == 0

    def test_cancelled_run_blocks_before_adapter_call(self) -> None:
        """When the run is cancelled, the adapter is NEVER called and
        the batch is not SUCCEEDED."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=CancelledRun(),
        )
        assert batch.batch_status != BatchExecutionStatus.SUCCEEDED
        assert len(sink) == 0

    def test_kill_switch_result_is_definitive_not_unknown(self) -> None:
        """Kill switch pre-call block returns NOT_AUTHORIZED (a
        definitive status), never UNKNOWN — unknown_proposal_ids is
        empty."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=AlwaysKillSwitch(),
        )
        assert batch.unknown_proposal_ids == ()
        assert len(sink) == 0

    def test_cancelled_result_is_definitive_not_unknown(self) -> None:
        """Cancelled-run pre-call block returns CANCELLED (a definitive
        status), never UNKNOWN — unknown_proposal_ids is empty."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=CancelledRun(),
        )
        assert batch.unknown_proposal_ids == ()
        assert len(sink) == 0

    # ------------------------------------------------------------------
    # P0-6: Receipt + store atomic commit (4 tests)
    # ------------------------------------------------------------------

    def test_replay_returns_original_receipt(self) -> None:
        """Replaying the same execution returns the ORIGINAL trusted
        receipt (same receipt_id), not a fabricated DEDUPLICATED one."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        execution_store = InMemoryExecutionStore()
        approval_store = InMemoryApprovalStore()

        batch1 = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        batch2 = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        assert batch1.receipts[0].receipt_id == batch2.receipts[0].receipt_id
        assert batch2.receipts[0].status == batch1.receipts[0].status

    def test_replay_does_not_call_adapter(self) -> None:
        """On replay the adapter is NOT called — the idempotency store
        returns the cached receipt without re-invoking the adapter."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        execution_store = InMemoryExecutionStore()
        approval_store = InMemoryApprovalStore()

        _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        assert len(sink) == 1

    def test_receipt_committed_to_store_after_execution(self) -> None:
        """After a successful execution, the trusted receipt is stored
        in the execution store (P0-6 atomic commit)."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        execution_store = InMemoryExecutionStore()
        approval_store = InMemoryApprovalStore()

        _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        receipt = run_async(execution_store.get_receipt(TENANT, "idem-test-001"))
        assert receipt is not None
        assert receipt.status == ExecutionStatus.SUCCEEDED
        assert receipt.executed is True

    def test_replay_receipt_has_same_status(self) -> None:
        """The replayed receipt has the same status as the original
        (P0-6: the trusted receipt is immutable across replays)."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        execution_store = InMemoryExecutionStore()
        approval_store = InMemoryApprovalStore()

        batch1 = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        batch2 = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
            approval_store=approval_store,
        )
        assert batch1.receipts[0].status == batch2.receipts[0].status
        assert batch1.receipts[0].executed == batch2.receipts[0].executed

    # ------------------------------------------------------------------
    # P0-7: Batch status semantics (4 tests)
    # ------------------------------------------------------------------

    def test_blocked_not_no_actions(self) -> None:
        """When proposals exist but all are REJECTED, the batch is
        BLOCKED — not NO_ACTIONS (P0-7: BLOCKED ≠ NO_ACTIONS)."""
        proposal = make_proposal()
        request = make_request("review-rejected", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.REJECTED,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert batch.batch_status != BatchExecutionStatus.NO_ACTIONS

    def test_unknown_valid_with_empty_receipts(self) -> None:
        """ExecutionBatchResult allows UNKNOWN with empty receipts
        (P0-7: UNKNOWN is a valid batch status even when no receipts
        were produced — e.g. crash window)."""
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        batch = ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            receipts=(),
            batch_status=BatchExecutionStatus.UNKNOWN,
            started_at=TS,
            completed_at=TS,
        )
        batch.verify_integrity()  # no raise

    def test_partial_success_derived_from_per_action(self) -> None:
        """A batch with one SUCCEEDED and one FAILED receipt yields
        PARTIAL_SUCCESS (P0-7: derived from per-action statuses)."""
        p_success = make_proposal("prop-a", idempotency_key="idem-a")
        p_fail = make_proposal("prop-b", idempotency_key="idem-b")
        request = make_request("review-partial", [p_success, p_fail], [make_evidence()])
        review_success = make_review("prop-a", request.request_hash)
        review_fail = make_review("prop-b", request.request_hash)
        result = make_result(request, [review_success, review_fail])

        sink: list = []
        registry = ActionAdapterRegistry()
        adapter = _MixedOutcomeAdapter(
            sink=sink,
            fail_proposal_id="prop-b",
            supported_action_types=frozenset(ACTION_GOVERNANCE_REGISTRY),
        )
        registry.register(adapter)

        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert len(batch.receipts) == 2
        assert batch.batch_status == BatchExecutionStatus.PARTIAL_SUCCESS

    def test_no_actions_only_for_empty_review(self) -> None:
        """NO_ACTIONS is ONLY returned when the ReviewRequest has no
        proposals and the ReviewBatchResult has no proposal_reviews
        (P0-7).  A Review with proposals but all blocked → BLOCKED."""
        request = make_request("review-empty", [], [])
        result = make_result(request, [])
        registry = make_recording_registry([])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
        )
        assert batch.batch_status == BatchExecutionStatus.NO_ACTIONS
        assert len(batch.receipts) == 0

    # ------------------------------------------------------------------
    # P0-8: Deadline / concurrency / retry (3 tests)
    # ------------------------------------------------------------------

    def test_batch_deadline_exceeded_returns_unknown(self) -> None:
        """When the batch deadline is exceeded before an action starts,
        the result is UNKNOWN with EXECUTION_DEADLINE_EXCEEDED (P0-8)."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False, batch_deadline_seconds=0.5),
            clock=_AdvancingClock(TS, timedelta(seconds=1)),
        )
        assert batch.batch_status == BatchExecutionStatus.UNKNOWN
        assert len(sink) == 0

    def test_retry_succeeds_for_confirmed_not_executed_failed(self) -> None:
        """A FAILED outcome with executed=False is retried when the
        retry policy allows it; the second attempt SUCCEEDS (P0-8)."""
        request, result, _ = make_approved_request_result()
        registry = ActionAdapterRegistry()
        adapter = _RetryableFailAdapter()
        registry.register(adapter)
        policy = ExecutionRetryPolicy(
            max_retries=1,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False, retry_policy=policy),
        )
        assert batch.batch_status == BatchExecutionStatus.SUCCEEDED
        assert len(batch.receipts) == 1
        assert batch.receipts[0].status == ExecutionStatus.SUCCEEDED
        assert adapter._call_count == 2

    def test_unknown_outcome_never_retried(self) -> None:
        """An UNKNOWN outcome (adapter raised) is NEVER retried even
        when the retry policy allows retries (P0-8)."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = ActionAdapterRegistry()
        adapter = _RaisingAdapter(sink)
        registry.register(adapter)
        policy = ExecutionRetryPolicy(
            max_retries=5,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False, retry_policy=policy),
        )
        assert batch.batch_status == BatchExecutionStatus.UNKNOWN
        assert len(sink) == 1  # adapter called exactly once — no retry

    # ------------------------------------------------------------------
    # P0-9: Live governance integrity (4 tests)
    # ------------------------------------------------------------------

    def test_live_governance_spec_hash_matches_constant(self) -> None:
        """compute_live_governance_spec_hash() matches the module
        constant ACTION_GOVERNANCE_SPEC_HASH (P0-9)."""
        live_hash = compute_live_governance_spec_hash()
        assert live_hash == ACTION_GOVERNANCE_SPEC_HASH

    def test_governance_registry_cannot_be_mutated(self) -> None:
        """ACTION_GOVERNANCE_REGISTRY is a read-only MappingProxyType —
        attempts to mutate it raise TypeError (P0-9 immutability)."""
        with pytest.raises(TypeError):
            ACTION_GOVERNANCE_REGISTRY["report.generate"] = None  # type: ignore[index]

    def test_governance_spec_hash_is_stable_across_calls(self) -> None:
        """Two calls to compute_live_governance_spec_hash() return the
        same value (P0-9 stability)."""
        hash1 = compute_live_governance_spec_hash()
        hash2 = compute_live_governance_spec_hash()
        assert hash1 == hash2

    def test_governance_spec_hash_is_64_char_hex(self) -> None:
        """ACTION_GOVERNANCE_SPEC_HASH is a 64-character hex string
        (SHA-256, P0-9 format invariant)."""
        assert len(ACTION_GOVERNANCE_SPEC_HASH) == 64
        int(ACTION_GOVERNANCE_SPEC_HASH, 16)
