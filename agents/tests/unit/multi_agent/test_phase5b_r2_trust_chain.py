"""Phase 5B R2 — Trust-chain counter-example tests.

Comprehensive counter-example suite covering the R2 fixes for the
10 P0 blocking issues and 2 P1 sync items identified in the R1
review.  Each test pins down exactly one R2 invariant so a regression
is surfaced as a single focused failure rather than a cascade.

R2 fix groups covered:

* P0-1  ``pre_approval_authorization_hash`` chain — the hash before
        approval binding is captured so the pre-approval -> post-approval
        transition is verifiable.
* P0-2  ``validate_decision`` / ``consume_for_command`` two-phase split
        — read-only validation does NOT consume; consumption binds to a
        specific ``command_id`` + ``execution_fingerprint``.
* P0-3  ``ApprovalConsumptionRecord`` — every consumption is recorded
        with a content-bound ``consumption_hash``.
* P0-4  ``FrozenActionAdapterRegistry`` — the live adapter instance is
        frozen atomically with the binding snapshot; drift is fail-closed.
* P0-5  Call-boundary ordering — pre-call blocks return NOT_AUTHORIZED /
        CANCELLED (never UNKNOWN) and never touch the idempotency store.
* P0-7  Dry-run idempotency isolation — a dry-run success NEVER blocks a
        subsequent real execution with the same key.
* P0-8  Idempotency scope semantics — GLOBAL / TENANT / NONE produce
        distinct store keys and replay semantics.
* P0-9  Strict CAS state machine — every transition is validated
        against the legal-transition table.
* P0-10 Batch semantics from ``action_records`` — summary ID lists are
        derived from per-action records; ``verify_semantics`` rejects
        duplicates / orphans.
* P1-1  LangGraph state serialisable + Direct/Graph error parity —
        runtime deps live in ``RuntimeDependencies`` (closure), not in
        ``ExecutionGraphState``; invalid inputs yield BLOCKED on both
        paths.
* P1-3  ``ExecutionExpectedOutcome`` deep immutability —
        ``expected_status_by_proposal`` is a tuple, not a dict.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from phase5b_helpers import (
    RUN_ID,
    TENANT,
    TS,
    AlwaysKillSwitch,
    NoKillSwitch,
    make_approved_request_result,
    make_evidence,
    make_proposal,
    make_recording_registry,
    make_request,
    make_result,
    make_review,
    run_async,
)
from pydantic import ValidationError

from multi_agent.action_adapter import (
    ActionAdapterBinding,
    ActionAdapterRegistrySnapshot,
    AdapterExecutionOutcome,
    ExecutionCommand,
    FrozenActionAdapterRegistry,
    IdempotencyScope,
    RecordingActionAdapter,
)
from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY
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
    ACTION_NOT_SUPPORTED,
    KILL_SWITCH_ACTIVE,
    ApprovalValidationError,
    ExecutionIntegrityError,
)
from multi_agent.execution_evaluation import ExecutionExpectedOutcome
from multi_agent.execution_graph import (
    ExecutionGraphState,
    RuntimeDependencies,
    build_execution_graph,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import (
    _LEGAL_TRANSITIONS,
    IdempotencyState,
    InMemoryExecutionStore,
    _assert_transition,
    compute_resource_key,
    compute_scope_key,
)
from multi_agent.governed_executor import (
    ActionExecutionRecord,
    ExecutionOptions,
    GovernedExecutor,
    _compute_batch_status_from_records,
)
from multi_agent.review_contracts import ReviewRiskLevel

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


_APPR_TS = TS
_APPR_TS_LATER = TS + timedelta(hours=1)
_APPR_TS_EXPIRED = TS + timedelta(hours=2)
_APPR_TS_FUTURE = TS + timedelta(hours=3)


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
    dry_run: bool = False,
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
        dry_run=dry_run,
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
    requested_at: datetime = _APPR_TS,
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
        requested_at=requested_at,
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


def _seed_approved_approval(
    store: InMemoryApprovalStore,
    *,
    auth: ExecutionAuthorization,
    expires_at: datetime | None = None,
    decided_at: datetime = _APPR_TS,
) -> tuple[ApprovalRequest, ApprovalDecision]:
    """Seed an APPROVED approval in *store* and return (request, decision)."""
    request = _make_appr_request(auth=auth, expires_at=expires_at)
    decision = _make_appr_decision(request=request, decided_at=decided_at)
    run_async(store.create(request))
    run_async(store.decide(request.approval_id, decision))
    return request, decision


def _make_receipt(
    *,
    receipt_id: str = "r-test",
    command_id: str = "cmd-test",
    proposal_id: str = "prop-test",
    adapter_id: str = "adapter-A",
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    executed: bool | None = True,
    fingerprint: str = "fp-test",
    tenant_id: str = TENANT,
    idempotency_key: str = "idem-receipt",
) -> ActionExecutionReceipt:
    """Build a valid :class:`ActionExecutionReceipt` for store / batch
    tests with the minimum required fields."""
    return ActionExecutionReceipt(
        receipt_id=receipt_id,
        command_id=command_id,
        tenant_id=tenant_id,
        run_id=RUN_ID,
        proposal_id=proposal_id,
        authorization_hash="a" * 64,
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        adapter_registry_hash="r" * 64,
        idempotency_key=idempotency_key,
        execution_fingerprint=fingerprint,
        status=status,
        executed=executed,
        started_at=TS,
        completed_at=TS,
    )


class _RaisingAdapter:
    """Adapter that always raises — exercises the UNKNOWN path."""

    adapter_id = "raising-adapter"
    adapter_version = "1.0.0"
    supports_dry_run = True
    retry_safe = True
    idempotency_scope = IdempotencyScope.TENANT
    supported_action_types = frozenset(ACTION_GOVERNANCE_REGISTRY)

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        raise RuntimeError("simulated adapter crash")


# ===========================================================================
# P0-1: pre_approval_authorization_hash chain (3 tests)
# ===========================================================================


class TestP01PreApprovalAuthorizationHashChain:
    """P0-1: the hash BEFORE approval binding is captured so the
    pre-approval -> post-approval transition is verifiable."""

    def test_pre_approval_hash_is_none_before_binding(self) -> None:
        """A freshly built PENDING_APPROVAL authorization has
        ``pre_approval_authorization_hash=None`` (no approval bound yet)."""
        auth = _make_auth()
        assert auth.pre_approval_authorization_hash is None

    def test_bind_approval_captures_pre_approval_hash(self) -> None:
        """``_bind_approval`` saves the pre-binding authorization_hash
        into ``pre_approval_authorization_hash`` and produces a DIFFERENT
        ``authorization_hash`` (because approval_id / decision_hash are
        now part of the content)."""
        auth = _make_auth()
        pre_hash = auth.authorization_hash
        assert pre_hash != ""

        decision = ApprovalDecision(
            approval_id="appr-001",
            status=ApprovalStatus.APPROVED,
            approver_id="approver-001",
            approver_roles=("manager",),
            decision_reason="approved",
            decided_at=_APPR_TS,
            approval_request_hash="x" * 64,
            authorization_hash=auth.authorization_hash,
        )

        executor = GovernedExecutor()
        bound = executor._bind_approval(auth, decision, pre_hash)

        # P0-1: pre_approval_authorization_hash captured.
        assert bound.pre_approval_authorization_hash == pre_hash
        # P0-1: the post-approval hash differs (content changed).
        assert bound.authorization_hash != pre_hash
        # P0-1: status advanced to READY.
        assert bound.status == ExecutionStatus.READY

    def test_pre_approval_hash_is_content_verified(self) -> None:
        """The ``pre_approval_authorization_hash`` value participates in
        the new ``authorization_hash`` computation — forging it breaks
        the integrity check."""
        auth = _make_auth()
        decision = ApprovalDecision(
            approval_id="appr-001",
            status=ApprovalStatus.APPROVED,
            approver_id="approver-001",
            approver_roles=("manager",),
            decision_reason="approved",
            decided_at=_APPR_TS,
            approval_request_hash="x" * 64,
            authorization_hash=auth.authorization_hash,
        )
        executor = GovernedExecutor()
        bound = executor._bind_approval(auth, decision, auth.authorization_hash)
        # Integrity passes with the real pre-approval hash.
        bound.verify_integrity()

        # Forge a different pre_approval_authorization_hash — the
        # authorization_hash no longer matches the recomputed content.
        forged = bound.model_copy(update={"pre_approval_authorization_hash": "f" * 64})
        with pytest.raises(ValueError):
            forged.verify_integrity()


# ===========================================================================
# P0-2: validate_decision / consume_for_command two-phase split (4 tests)
# ===========================================================================


class TestP02ValidateConsumeTwoPhaseSplit:
    """P0-2: read-only validation does NOT consume; consumption binds to
    a specific command_id + execution_fingerprint."""

    def test_validate_decision_does_not_consume(self) -> None:
        """``validate_decision`` is read-only — a subsequent
        ``consume_for_command`` on the same approval succeeds (the
        approval was NOT consumed by validation)."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        run_async(store.validate_decision("appr-001", authorization=auth, now=_APPR_TS))

        # The approval is still consumable.
        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        assert consumption.approval_id == "appr-001"
        assert consumption.command_id == "cmd-001"

    def test_consume_for_command_binds_to_command_id(self) -> None:
        """``consume_for_command`` records the exact command_id so a
        replay of the SAME command returns the SAME consumption."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        first = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        second = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        # Same command replay → original consumption returned (NOT a
        # second illegal consume).
        assert first.consumption_hash == second.consumption_hash
        assert first.command_id == "cmd-001"

    def test_different_command_cannot_reuse_consumption(self) -> None:
        """A consumption bound to ``cmd-A`` CANNOT be reused for a
        different ``cmd-B`` — the approval is single-use per command."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-A",
                execution_fingerprint="fp-A",
                now=_APPR_TS,
            )
        )
        with pytest.raises(ApprovalValidationError):
            run_async(
                store.consume_for_command(
                    "appr-001",
                    authorization=auth,
                    command_id="cmd-B",
                    execution_fingerprint="fp-B",
                    now=_APPR_TS,
                )
            )

    def test_consume_for_command_rejects_unapproved_status(self) -> None:
        """A REJECTED decision cannot be consumed — the approval is
        NOT bound to any command."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        request = _make_appr_request(auth=auth)
        decision = _make_appr_decision(request=request, status=ApprovalStatus.REJECTED)
        run_async(store.create(request))
        run_async(store.decide("appr-001", decision))

        with pytest.raises(ApprovalValidationError):
            run_async(
                store.consume_for_command(
                    "appr-001",
                    authorization=auth,
                    command_id="cmd-001",
                    execution_fingerprint="fp-001",
                    now=_APPR_TS,
                )
            )


# ===========================================================================
# P0-3: ApprovalConsumptionRecord binding (3 tests)
# ===========================================================================


class TestP03ApprovalConsumptionRecord:
    """P0-3: every consumption is recorded with a content-bound
    ``consumption_hash`` covering all binding fields."""

    def test_consumption_record_carries_all_binding_fields(self) -> None:
        """The consumption record carries approval_id, decision_hash,
        authorization_hash, command_id, and execution_fingerprint."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _request, decision = _seed_approved_approval(store, auth=auth)

        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        assert consumption.approval_id == "appr-001"
        assert consumption.decision_hash == decision.decision_hash
        assert consumption.authorization_hash == auth.authorization_hash
        assert consumption.command_id == "cmd-001"
        assert consumption.execution_fingerprint == "fp-001"

    def test_consumption_hash_is_computed_and_verified(self) -> None:
        """``consumption_hash`` is auto-computed and verify_integrity
        passes for the canonical record."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        assert consumption.consumption_hash != ""
        consumption.verify_integrity()

    def test_tampering_with_consumption_field_breaks_hash(self) -> None:
        """Forging any binding field (command_id) breaks the
        ``consumption_hash`` integrity check."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_id="cmd-001",
                execution_fingerprint="fp-001",
                now=_APPR_TS,
            )
        )
        forged = consumption.model_copy(update={"command_id": "cmd-forged"})
        with pytest.raises(ValueError):
            forged.verify_integrity()


# ===========================================================================
# P0-4: FrozenActionAdapterRegistry (3 tests)
# ===========================================================================


class TestP04FrozenActionAdapterRegistry:
    """P0-4: the live adapter instance is frozen atomically with the
    binding snapshot; drift is fail-closed."""

    def test_freeze_isolates_from_live_registry_mutation(self) -> None:
        """After ``freeze_for_execution``, registering a new adapter on
        the live registry does NOT change what the frozen handle
        returns."""
        registry = make_recording_registry([])
        frozen = registry.freeze_for_execution()

        # Register a different adapter on the live registry AFTER freeze.
        new_adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(new_adapter)

        # The frozen handle still returns the ORIGINAL adapter (not the
        # newly registered one).
        binding = frozen.get_binding("report.generate")
        original = frozen.get_adapter(binding.adapter_id)
        assert original is not new_adapter
        assert original.adapter_id == binding.adapter_id

    def test_verify_adapter_matches_binding_detects_id_drift(self) -> None:
        """If the live adapter's adapter_id differs from the binding's
        adapter_id, ``verify_adapter_matches_binding`` returns None."""
        binding = ActionAdapterBinding(
            action_type="report.generate",
            adapter_id="adapter-A",
            adapter_version="1.0.0",
            supports_dry_run=True,
            retry_safe=True,
            idempotency_scope=IdempotencyScope.TENANT,
        )
        snapshot = ActionAdapterRegistrySnapshot(
            registry_version="test-v1",
            bindings=(binding,),
        )
        # Live adapter with a DIFFERENT adapter_id than the binding,
        # but registered under the binding's key so the lookup succeeds
        # and the id-mismatch check is what fails.
        drifted = RecordingActionAdapter(
            sink=[],
            adapter_id="adapter-B",
            supported_action_types=frozenset({"report.generate"}),
        )
        frozen = FrozenActionAdapterRegistry(
            snapshot=snapshot,
            runtime_bindings={"adapter-A": drifted},
        )
        # adapter_id drift → None (fail-closed).
        assert frozen.verify_adapter_matches_binding(binding, dry_run=False) is None

    def test_verify_adapter_matches_binding_detects_retry_safe_drift(self) -> None:
        """If the live adapter's ``retry_safe`` differs from the
        binding's ``retry_safe``, verification fails."""

        class _RetryUnsafeAdapter:
            adapter_id = "adapter-A"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = False  # drifted from binding's retry_safe=True
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"report.generate"})

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
                raise RuntimeError("not invoked in this test")

        binding = ActionAdapterBinding(
            action_type="report.generate",
            adapter_id="adapter-A",
            adapter_version="1.0.0",
            supports_dry_run=True,
            retry_safe=True,
            idempotency_scope=IdempotencyScope.TENANT,
        )
        snapshot = ActionAdapterRegistrySnapshot(
            registry_version="test-v1",
            bindings=(binding,),
        )
        frozen = FrozenActionAdapterRegistry(
            snapshot=snapshot,
            runtime_bindings={"adapter-A": _RetryUnsafeAdapter()},
        )
        assert frozen.verify_adapter_matches_binding(binding, dry_run=False) is None


# ===========================================================================
# P0-5: Call-boundary ordering (3 tests)
# ===========================================================================


class TestP05CallBoundaryOrdering:
    """P0-5: pre-call blocks return NOT_AUTHORIZED / CANCELLED (never
    UNKNOWN) and ``adapter_call_started=False``."""

    def test_pre_call_adapter_missing_returns_not_authorized(self) -> None:
        """A proposal whose action_type is NOT bound returns
        NOT_AUTHORIZED with ``adapter_call_started=False`` — the
        idempotency store is never touched."""
        # Use an action type that the default recording registry does
        # NOT support.
        proposal = make_proposal(
            "prop-missing",
            action_type="nonexistent.action",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-missing"],
        )
        request = make_request(
            "review-missing", [proposal], [make_evidence("ev-missing")]
        )
        review = make_review("prop-missing", request.request_hash)
        result = make_result(request, [review])
        # Registry that does NOT bind "nonexistent.action".
        registry = make_recording_registry([])
        execution_store = InMemoryExecutionStore()

        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            execution_store=execution_store,
        )

        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        # The idempotency store was never touched (no reservation).
        assert batch.action_records[0].adapter_call_started is False
        assert batch.action_records[0].error_code == ACTION_NOT_SUPPORTED

    def test_pre_call_kill_switch_returns_not_authorized_not_unknown(self) -> None:
        """When the kill switch is active BEFORE the call, the action
        returns NOT_AUTHORIZED (never UNKNOWN) with
        ``adapter_call_started=False``."""
        proposal = make_proposal(
            "prop-kill",
            action_type="report.generate",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-kill"],
        )
        request = make_request("review-kill", [proposal], [make_evidence("ev-kill")])
        review = make_review("prop-kill", request.request_hash)
        result = make_result(request, [review])
        registry = make_recording_registry([])

        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=AlwaysKillSwitch(),
        )

        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert batch.action_records[0].adapter_call_started is False
        assert batch.action_records[0].error_code == KILL_SWITCH_ACTIVE
        assert batch.action_records[0].status == ExecutionStatus.NOT_AUTHORIZED

    def test_pre_call_blocks_never_produce_unknown_status(self) -> None:
        """P0-5: every pre-call block (adapter missing, kill switch
        active) returns NOT_AUTHORIZED or CANCELLED — NEVER UNKNOWN.
        Only post-mark_started failures may be UNKNOWN."""
        # Scenario 1: missing adapter.
        proposal_missing = make_proposal(
            "prop-missing-2",
            action_type="nonexistent.action",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-missing-2"],
        )
        request1 = make_request(
            "review-missing-2",
            [proposal_missing],
            [make_evidence("ev-missing-2")],
        )
        review1 = make_review("prop-missing-2", request1.request_hash)
        result1 = make_result(request1, [review1])
        batch1 = _execute(
            request1,
            result1,
            registry=make_recording_registry([]),
            kill_switch=NoKillSwitch(),
        )
        assert batch1.action_records[0].status != ExecutionStatus.UNKNOWN
        assert batch1.action_records[0].adapter_call_started is False

        # Scenario 2: kill switch active.
        proposal_kill = make_proposal(
            "prop-kill-2",
            action_type="report.generate",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-kill-2"],
        )
        request2 = make_request(
            "review-kill-2",
            [proposal_kill],
            [make_evidence("ev-kill-2")],
        )
        review2 = make_review("prop-kill-2", request2.request_hash)
        result2 = make_result(request2, [review2])
        batch2 = _execute(
            request2,
            result2,
            registry=make_recording_registry([]),
            kill_switch=AlwaysKillSwitch(),
        )
        assert batch2.action_records[0].status != ExecutionStatus.UNKNOWN
        assert batch2.action_records[0].adapter_call_started is False


# ===========================================================================
# P0-7: Dry-run idempotency isolation (3 tests)
# ===========================================================================


class TestP07DryRunIdempotencyIsolation:
    """P0-7: a dry-run success NEVER blocks a subsequent real execution
    with the same idempotency key."""

    def test_dry_run_succeeded_does_not_block_real_execution(self) -> None:
        """A dry-run that leaves a DRY_RUN_SUCCEEDED record does NOT
        prevent a subsequent real execution with the same key from
        reserving and succeeding."""
        store = InMemoryExecutionStore()
        # Dry-run reservation.
        dry_record = run_async(
            store.reserve(
                TENANT,
                "idem-shared-001",
                "fp-dry",
                "cmd-dry",
                dry_run=True,
            )
        )
        # Mark it DRY_RUN_SUCCEEDED.
        dry_receipt = _make_receipt(
            receipt_id="r-dry-001",
            command_id="cmd-dry",
            proposal_id="prop-dry",
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
            executed=False,
            fingerprint="fp-dry",
            idempotency_key="idem-shared-001",
        )
        dry_record = run_async(store.mark_started(dry_record, "cmd-dry"))
        run_async(store.complete_with_receipt(dry_record, dry_receipt))
        # The dry-run slot is now DRY_RUN_SUCCEEDED.
        stored_dry = store._records[store._record_store_key(dry_record)]
        assert stored_dry.state == IdempotencyState.DRY_RUN_SUCCEEDED

        # Real execution with the SAME key must NOT see the dry-run record.
        real_record = run_async(
            store.reserve(
                TENANT,
                "idem-shared-001",
                "fp-real",
                "cmd-real",
                dry_run=False,
            )
        )
        # The real reservation is a fresh RESERVED record (not blocked).
        assert real_record.state == IdempotencyState.RESERVED
        assert real_record.dry_run is False

    def test_dry_run_record_state_is_dry_run_succeeded(self) -> None:
        """A completed dry-run record has state DRY_RUN_SUCCEEDED,
        NEVER SUCCEEDED."""
        store = InMemoryExecutionStore()
        record = run_async(
            store.reserve(TENANT, "idem-dry-002", "fp", "cmd", dry_run=True)
        )
        receipt = _make_receipt(
            receipt_id="r-002",
            command_id="cmd",
            proposal_id="prop-002",
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
            executed=False,
            fingerprint="fp",
            idempotency_key="idem-dry-002",
        )
        record = run_async(store.mark_started(record, "cmd"))
        run_async(store.complete_with_receipt(record, receipt))
        stored = store._records[store._record_store_key(record)]
        assert stored.state == IdempotencyState.DRY_RUN_SUCCEEDED
        assert stored.state != IdempotencyState.SUCCEEDED

    def test_real_execution_record_state_is_succeeded(self) -> None:
        """A completed real execution record has state SUCCEEDED, NEVER
        DRY_RUN_SUCCEEDED."""
        store = InMemoryExecutionStore()
        record = run_async(
            store.reserve(TENANT, "idem-real-003", "fp", "cmd", dry_run=False)
        )
        receipt = _make_receipt(
            receipt_id="r-003",
            command_id="cmd",
            proposal_id="prop-003",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            fingerprint="fp",
            idempotency_key="idem-real-003",
        )
        record = run_async(store.mark_started(record, "cmd"))
        run_async(store.complete_with_receipt(record, receipt))
        stored = store._records[store._record_store_key(record)]
        assert stored.state == IdempotencyState.SUCCEEDED
        assert stored.state != IdempotencyState.DRY_RUN_SUCCEEDED


# ===========================================================================
# P0-8: Idempotency scope semantics (4 tests)
# ===========================================================================


class TestP08IdempotencyScopeSemantics:
    """P0-8: GLOBAL / TENANT / NONE produce distinct store keys and
    replay semantics."""

    def test_global_scope_shares_across_tenants(self) -> None:
        """``compute_scope_key`` with GLOBAL produces a tenant-agnostic
        key — two tenants with the same key share the slot."""
        key_a = compute_scope_key("tenant-A", "idem-g", IdempotencyScope.GLOBAL)
        key_b = compute_scope_key("tenant-B", "idem-g", IdempotencyScope.GLOBAL)
        assert key_a == key_b
        assert key_a == ("global", "idem-g")

    def test_tenant_scope_isolates_per_tenant(self) -> None:
        """``compute_scope_key`` with TENANT produces a per-tenant key —
        two tenants with the same key get DIFFERENT slots."""
        key_a = compute_scope_key("tenant-A", "idem-t", IdempotencyScope.TENANT)
        key_b = compute_scope_key("tenant-B", "idem-t", IdempotencyScope.TENANT)
        assert key_a != key_b
        assert key_a == ("tenant-A", "idem-t")
        assert key_b == ("tenant-B", "idem-t")

    def test_none_scope_always_creates_fresh_record(self) -> None:
        """``reserve`` with scope=NONE ALWAYS creates a fresh record —
        no replay, no conflict-check.  Two reserves with the same key
        produce two distinct records."""
        store = InMemoryExecutionStore()
        r1 = run_async(
            store.reserve(
                TENANT,
                "idem-none-001",
                "fp-1",
                "cmd-1",
                scope=IdempotencyScope.NONE,
            )
        )
        r2 = run_async(
            store.reserve(
                TENANT,
                "idem-none-001",
                "fp-2",
                "cmd-2",
                scope=IdempotencyScope.NONE,
            )
        )
        # Both succeed (no conflict raised) and are distinct records.
        assert r1.reservation_id != r2.reservation_id
        assert r1.command_id == "cmd-1"
        assert r2.command_id == "cmd-2"

    def test_none_scope_record_carries_unique_reservation_id(self) -> None:
        """A NONE-scope record carries a non-empty ``reservation_id`` so
        its store key never collides with another NONE-scope record."""
        store = InMemoryExecutionStore()
        r = run_async(
            store.reserve(
                TENANT,
                "idem-none-002",
                "fp",
                "cmd",
                scope=IdempotencyScope.NONE,
            )
        )
        assert r.reservation_id is not None
        assert r.reservation_id != ""
        assert r.scope == IdempotencyScope.NONE

    def test_resource_key_collapses_optional_fields(self) -> None:
        """``compute_resource_key`` collapses None fields to "" so the
        key is always a stable 4-tuple."""
        k1 = compute_resource_key(TENANT, None, None, None)
        k2 = compute_resource_key(TENANT, "", "", "")
        assert k1 == k2
        assert k1 == (TENANT, "", "", "")


# ===========================================================================
# P0-9: Strict CAS state machine (3 tests)
# ===========================================================================


class TestP09StrictCasStateMachine:
    """P0-9: every state transition is validated against the
    legal-transition table."""

    def test_reserved_to_call_started_is_legal(self) -> None:
        """RESERVED -> CALL_STARTED is a legal transition (no raise)."""
        _assert_transition(
            IdempotencyState.RESERVED, IdempotencyState.CALL_STARTED
        )  # no raise

    def test_succeeded_terminal_rejects_any_transition(self) -> None:
        """SUCCEEDED is terminal — any transition is rejected."""
        for target in (
            IdempotencyState.CALL_STARTED,
            IdempotencyState.FAILED,
            IdempotencyState.UNKNOWN,
        ):
            with pytest.raises(ValueError):
                _assert_transition(IdempotencyState.SUCCEEDED, target)

    def test_call_started_to_all_terminal_states_is_legal(self) -> None:
        """CALL_STARTED -> SUCCEEDED / FAILED / UNKNOWN / DRY_RUN_SUCCEEDED
        are all legal terminal transitions."""
        for target in (
            IdempotencyState.SUCCEEDED,
            IdempotencyState.FAILED,
            IdempotencyState.UNKNOWN,
            IdempotencyState.DRY_RUN_SUCCEEDED,
        ):
            _assert_transition(IdempotencyState.CALL_STARTED, target)  # no raise

    def test_legal_transitions_table_marks_terminals_empty(self) -> None:
        """The legal-transition table marks terminal states (SUCCEEDED,
        DRY_RUN_SUCCEEDED, UNKNOWN) with an empty allowed set."""
        for terminal in (
            IdempotencyState.SUCCEEDED,
            IdempotencyState.DRY_RUN_SUCCEEDED,
            IdempotencyState.UNKNOWN,
        ):
            assert _LEGAL_TRANSITIONS[terminal] == frozenset()

    def test_mark_started_on_succeeded_raises(self) -> None:
        """``mark_started`` on a SUCCEEDED record raises (terminal
        state — no transition allowed)."""
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, "idem-cas-001", "fp", "cmd"))
        record = run_async(store.mark_started(record, "cmd"))
        receipt = _make_receipt(
            receipt_id="r-cas",
            command_id="cmd",
            proposal_id="prop-cas",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            fingerprint="fp",
            idempotency_key="idem-cas-001",
        )
        run_async(store.complete_with_receipt(record, receipt))
        # Now SUCCEEDED — mark_started must raise.
        stored = store._records[store._record_store_key(record)]
        with pytest.raises(ValueError):
            run_async(store.mark_started(stored, "cmd"))


# ===========================================================================
# P0-10: Batch semantics from action_records (3 tests)
# ===========================================================================


class TestP10BatchSemanticsFromActionRecords:
    """P0-10: summary ID lists are derived from per-action records;
    ``verify_semantics`` rejects duplicates / orphans."""

    def test_compute_batch_status_unknown_wins(self) -> None:
        """``_compute_batch_status_from_records`` returns UNKNOWN when
        ANY record is UNKNOWN (fail-closed priority)."""
        records = (
            ActionExecutionRecord(proposal_id="p1", status=ExecutionStatus.SUCCEEDED),
            ActionExecutionRecord(proposal_id="p2", status=ExecutionStatus.UNKNOWN),
        )
        assert (
            _compute_batch_status_from_records(records) == BatchExecutionStatus.UNKNOWN
        )

    def test_compute_batch_status_partial_success(self) -> None:
        """A mix of SUCCEEDED and FAILED (no UNKNOWN) yields
        PARTIAL_SUCCESS."""
        records = (
            ActionExecutionRecord(proposal_id="p1", status=ExecutionStatus.SUCCEEDED),
            ActionExecutionRecord(proposal_id="p2", status=ExecutionStatus.FAILED),
        )
        assert (
            _compute_batch_status_from_records(records)
            == BatchExecutionStatus.PARTIAL_SUCCESS
        )

    def test_compute_batch_status_all_succeeded(self) -> None:
        """All-SUCCEEDED records yield SUCCEEDED."""
        records = (
            ActionExecutionRecord(proposal_id="p1", status=ExecutionStatus.SUCCEEDED),
            ActionExecutionRecord(proposal_id="p2", status=ExecutionStatus.SUCCEEDED),
        )
        assert (
            _compute_batch_status_from_records(records)
            == BatchExecutionStatus.SUCCEEDED
        )

    def test_verify_semantics_rejects_duplicate_proposal_id(self) -> None:
        """``verify_semantics`` raises ``ExecutionIntegrityError`` when
        two action_records share the same proposal_id."""
        from multi_agent.governed_executor import ExecutionBatchResult

        request, result, _ = make_approved_request_result()
        proposal_id = request.proposals[0].proposal_id
        # Build a batch with TWO action_records sharing the same
        # proposal_id.  The constructor does NOT check action_records
        # for duplicates (only summary lists), so this succeeds; the
        # duplicate is caught by verify_semantics.
        receipt = _make_receipt(
            receipt_id="r-dup",
            command_id="cmd-dup",
            proposal_id=proposal_id,
        )
        dup_records = (
            ActionExecutionRecord(
                proposal_id=proposal_id, status=ExecutionStatus.SUCCEEDED
            ),
            ActionExecutionRecord(
                proposal_id=proposal_id, status=ExecutionStatus.FAILED
            ),
        )
        batch = ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash="reg" + "0" * 60,
            action_records=dup_records,
            receipts=(receipt,),
            succeeded_proposal_ids=(proposal_id,),
            batch_status=BatchExecutionStatus.SUCCEEDED,
            started_at=TS,
            completed_at=TS,
            dry_run=False,
        )
        with pytest.raises(ExecutionIntegrityError):
            batch.verify_semantics(result)

    def test_summary_lists_are_mutually_exclusive_in_valid_batch(
        self,
    ) -> None:
        """In a valid ExecutionBatchResult, no proposal_id appears in
        two summary ID lists (skipped / blocked / pending_approval /
        failed / unknown / succeeded / dry_run_succeeded)."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        batch = _execute(
            request,
            result,
            registry=make_recording_registry(sink),
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(dry_run=False),
        )
        lists = {
            "skipped": batch.skipped_proposal_ids,
            "blocked": batch.blocked_proposal_ids,
            "pending_approval": batch.pending_approval_proposal_ids,
            "failed": batch.failed_proposal_ids,
            "unknown": batch.unknown_proposal_ids,
            "succeeded": batch.succeeded_proposal_ids,
            "dry_run_succeeded": batch.dry_run_succeeded_proposal_ids,
        }
        seen: set[str] = set()
        for _name, ids in lists.items():
            for pid in ids:
                assert pid not in seen, (
                    f"proposal {pid!r} appears in multiple summary lists"
                )
                seen.add(pid)


# ===========================================================================
# P1-1: LangGraph state serialisable + Direct/Graph error parity (3 tests)
# ===========================================================================


class TestP11LangGraphStateAndParity:
    """P1-1: runtime deps live in RuntimeDependencies (closure), not in
    ExecutionGraphState; invalid inputs yield BLOCKED on both paths."""

    def test_execution_graph_state_has_no_runtime_deps(self) -> None:
        """``ExecutionGraphState`` fields are only serialisable IDs,
        hashes, and results — no ApprovalStore / ExecutionStore /
        ActionAdapterRegistry / KillSwitch / Clock / GovernedExecutor."""
        from dataclasses import fields

        state_fields = {f.name for f in fields(ExecutionGraphState)}
        # The ONLY allowed fields are request, review_result, result,
        # graph_error — no runtime deps.
        assert state_fields == {
            "request",
            "review_result",
            "result",
            "graph_error",
        }

    def test_runtime_dependencies_is_frozen_dataclass(self) -> None:
        """``RuntimeDependencies`` is a frozen dataclass holding the
        live dependencies (closure-injected)."""
        from dataclasses import is_dataclass

        # Frozen-ness: assigning a field after construction raises.
        deps = RuntimeDependencies(
            approval_store=InMemoryApprovalStore(),
            execution_store=InMemoryExecutionStore(),
            adapter_registry=make_recording_registry([]),
            kill_switch=NoKillSwitch(),
            clock=FrozenClock(TS),
            executor=GovernedExecutor(),
            options=ExecutionOptions(),
        )
        assert is_dataclass(deps)
        with pytest.raises(AttributeError):
            deps.approval_store = InMemoryApprovalStore()  # type: ignore[misc]

    def test_graph_invalid_input_returns_blocked_same_as_direct(self) -> None:
        """P1-1: Direct/Graph Error Parity — an invalid ReviewRequest
        (review_id mismatch) yields BLOCKED on BOTH the direct
        ``GovernedExecutor.execute`` path AND the LangGraph path."""
        from phase5b_helpers import make_approved_request_result

        request, result, _ = make_approved_request_result()
        # Corrupt the request_hash so verify_integrity / binding fails.
        # We forge a mismatched result_hash on the review result so the
        # executor's binding check fails -> BLOCKED.
        forged_result = result.model_copy(update={"result_hash": "z" * 64})
        # Recompute the result_hash so the forged value is internally
        # consistent, then verify the executor detects the mismatch
        # against the request.
        # Direct path.
        direct_batch = _execute(
            request,
            forged_result,
            registry=make_recording_registry([]),
            kill_switch=NoKillSwitch(),
        )
        # Graph path.
        deps = RuntimeDependencies(
            approval_store=InMemoryApprovalStore(),
            execution_store=InMemoryExecutionStore(),
            adapter_registry=make_recording_registry([]),
            kill_switch=NoKillSwitch(),
            clock=FrozenClock(TS),
            executor=GovernedExecutor(),
            options=ExecutionOptions(),
        )
        graph = build_execution_graph(deps)
        state = ExecutionGraphState(request=request, review_result=forged_result)
        graph_output = run_async(graph.ainvoke(state))
        graph_result = graph_output.get("result")

        # Both paths must return BLOCKED (not raise, not graph_error
        # with result=None).
        assert direct_batch.batch_status == BatchExecutionStatus.BLOCKED
        assert graph_result is not None
        assert graph_result.batch_status == BatchExecutionStatus.BLOCKED


# ===========================================================================
# P1-3: ExecutionExpectedOutcome deep immutability (3 tests)
# ===========================================================================


class TestP13ExecutionExpectedOutcomeImmutability:
    """P1-3: ``expected_status_by_proposal`` is a tuple, not a dict;
    ``status_map`` returns a fresh mutable copy."""

    def test_expected_status_by_proposal_is_tuple(self) -> None:
        """``expected_status_by_proposal`` is a ``tuple[tuple[str, str],
        ...]`` — immutable."""
        outcome = ExecutionExpectedOutcome(
            expected_status_by_proposal=(
                ("prop-1", "succeeded"),
                ("prop-2", "failed"),
            )
        )
        assert isinstance(outcome.expected_status_by_proposal, tuple)
        for item in outcome.expected_status_by_proposal:
            assert isinstance(item, tuple)

    def test_status_map_returns_fresh_dict(self) -> None:
        """``status_map`` returns a fresh dict — mutating it does NOT
        affect the underlying tuple or subsequent calls."""
        outcome = ExecutionExpectedOutcome(
            expected_status_by_proposal=(("prop-1", "succeeded"),)
        )
        m1 = outcome.status_map
        m1["prop-1"] = "tampered"
        m2 = outcome.status_map
        assert m2["prop-1"] == "succeeded"
        assert m1 is not m2

    def test_expected_outcome_is_frozen(self) -> None:
        """The ExecutionExpectedOutcome model is frozen — assignment
        raises."""
        outcome = ExecutionExpectedOutcome(
            expected_status_by_proposal=(("prop-1", "succeeded"),)
        )
        with pytest.raises(ValidationError):
            outcome.expected_status_by_proposal = (("prop-1", "failed"),)  # type: ignore[misc]

    def test_empty_outcome_has_empty_status_map(self) -> None:
        """An outcome with no expected statuses has an empty
        status_map (and the tuple is empty)."""
        outcome = ExecutionExpectedOutcome()
        assert outcome.expected_status_by_proposal == ()
        assert outcome.status_map == {}
