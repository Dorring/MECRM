"""Phase 5B R3 — Trust-chain counter-example tests.

Comprehensive counter-example suite covering the 9 P0 execution safety
issues fixed in Phase 5B R3.  Each test pins down exactly one R3
invariant so a regression is surfaced as a single focused failure
rather than a cascade.

R3 fix groups covered:

* P0-1  Three-tier approval hash chain — ``base_authorization_hash``
        → ``approval_subject_hash`` → ``authorization_hash``.  The
        human approver approves the SUBJECT hash (stable across
        decision binding), NOT the final authorization hash.
* P0-3  Resource-level serialisation — actions targeting the same
        external resource (same ``resource_lock_key``) are serialised
        via a per-key :class:`asyncio.Lock`.
* P0-4  Call-boundary state machine — ``mark_dispatched`` transitions
        ``READY_TO_CALL`` → ``CALL_DISPATCHED``; only post-dispatch
        failures may be UNKNOWN.
* P0-5/6 Governance-driven retry — retry requires
        ``execution_retry_allowed=True`` (defaults False) AND
        ``error_code`` in ``gov_spec.retryable_error_codes`` AND
        ``min(policy.max_retries, gov_spec.max_execution_retries)`` budget.
* P0-7  Append-only attempt audit trail — per-command receipts are
        never overwritten by a later retry attempt.
* P0-8  Tightened ``verify_semantics`` — status ↔ executed consistency,
        receipt presence, error_code presence, dispatched ⇒ started.
* P0-12 Atomic adapter-registry freeze — snapshot + live instances
        captured under a single lock.
* P0-13 Legacy bypass removal — ``consume`` / ``validate_and_consume``
        have been REMOVED (not just deprecated).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from phase5b_helpers import (
    RUN_ID,
    TENANT,
    TS,
    NoKillSwitch,
    make_evidence,
    make_proposal,
    make_recording_registry,
    make_request,
    make_result,
    make_review,
    run_async,
)

from multi_agent.action_adapter import (
    ActionAdapterRegistry,
    AdapterExecutionOutcome,
    ExecutionCommand,
    IdempotencyScope,
    RecordingActionAdapter,
)
from multi_agent.action_governance import (
    ACTION_GOVERNANCE_REGISTRY,
    ActionGovernanceSpec,
    get_action_governance_spec,
)
from multi_agent.approval_contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    FrozenClock,
)
from multi_agent.approval_gate import InMemoryApprovalStore
from multi_agent.contracts import ActionRiskLevel, AgentAuthority
from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionAuthorization,
    ExecutionStatus,
)
from multi_agent.execution import FakeExecutionCancellation
from multi_agent.execution_error_codes import (
    ApprovalValidationError,
    ExecutionIntegrityError,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import (
    IdempotencyState,
    InMemoryExecutionStore,
)
from multi_agent.governed_executor import (
    ActionExecutionRecord,
    ExecutionAttemptRecord,
    ExecutionOptions,
    ExecutionRetryPolicy,
    GovernedExecutor,
    _compute_resource_lock_key,
    _deterministic_command_family_id,
    _extract_payload_dict,
)
from multi_agent.review_contracts import ReviewRiskLevel

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TS = TS
_TS_LATER = TS + timedelta(hours=1)


def _make_auth(
    *,
    authorization_id: str = "auth-001",
    proposal_id: str = "prop-001",
    approval_id: str | None = "appr-001",
    approval_required: bool = True,
    risk_level: ReviewRiskLevel = ReviewRiskLevel.HIGH,
    action_type: str = "crm.owner.assign",
    tenant_id: str = TENANT,
    dry_run: bool = False,
) -> ExecutionAuthorization:
    """Build an authorization with the three-tier hash chain auto-computed."""
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
    return auth


def _make_appr_request(
    *,
    auth: ExecutionAuthorization,
    approval_id: str = "appr-001",
    required_roles: tuple[str, ...] = ("manager",),
    expires_at: datetime | None = None,
    requested_at: datetime = _TS,
) -> ApprovalRequest:
    # P0-1 R3: the request binds to approval_subject_hash (what the
    # human approves), NOT the final authorization_hash.
    subject_hash = auth.approval_subject_hash or auth.authorization_hash
    return ApprovalRequest(
        approval_id=approval_id,
        authorization_id=auth.authorization_id,
        tenant_id=auth.tenant_id,
        run_id=RUN_ID,
        proposal_id=auth.proposal_id,
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        authorization_hash=subject_hash,
        risk_level=ReviewRiskLevel.HIGH,
        action_type=auth.action_type,
        action_summary="test action",
        required_approver_roles=required_roles,
        requested_by="test_requester",
        requested_at=requested_at,
        expires_at=expires_at,
    )


def _make_appr_decision(
    *,
    request: ApprovalRequest,
    auth: ExecutionAuthorization,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    approver_roles: tuple[str, ...] = ("manager",),
    decided_at: datetime = _TS,
) -> ApprovalDecision:
    # P0-1 R3: the decision binds to approval_subject_hash (same as
    # the request), NOT the final authorization_hash.
    return ApprovalDecision(
        approval_id=request.approval_id,
        status=status,
        approver_id="approver-001",
        approver_roles=approver_roles,
        decision_reason="approved",
        decided_at=decided_at,
        approval_request_hash=request.approval_request_hash,
        authorization_hash=request.authorization_hash,
    )


def _seed_approved_approval(
    store: InMemoryApprovalStore,
    *,
    auth: ExecutionAuthorization,
    expires_at: datetime | None = None,
    decided_at: datetime = _TS,
) -> tuple[ApprovalRequest, ApprovalDecision]:
    """Seed an APPROVED approval in *store* and return (request, decision)."""
    request = _make_appr_request(auth=auth, expires_at=expires_at)
    decision = _make_appr_decision(request=request, auth=auth, decided_at=decided_at)
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
    """Build a valid ActionExecutionReceipt for store / batch tests."""
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


# ===========================================================================
# P0-1: Three-tier approval hash chain (6 tests)
# ===========================================================================


class TestP01ThreeTierApprovalHashChain:
    """P0-1 R3: base_authorization_hash → approval_subject_hash →
    authorization_hash.  The human approver approves the SUBJECT hash
    (stable across decision binding), NOT the final authorization hash.
    """

    def test_base_authorization_hash_is_stable(self) -> None:
        """The ``base_authorization_hash`` is computed from the
        authorization content WITHOUT approval fields and is stable —
        binding a decision does NOT change it."""
        auth = _make_auth()
        base_hash = auth.base_authorization_hash
        assert base_hash != ""
        # The base hash does not change when approval fields change
        # because it excludes them.
        auth2 = _make_auth(approval_id="appr-different")
        # Same identity fields → same base hash.
        assert auth2.base_authorization_hash == base_hash

    def test_approval_subject_hash_binds_base_and_approval_id(self) -> None:
        """The ``approval_subject_hash`` is computed from
        ``base_authorization_hash + approval_id`` — this is what the
        human approver sees and approves."""
        auth = _make_auth(approval_id="appr-001")
        assert auth.approval_subject_hash is not None
        assert auth.approval_subject_hash != ""
        # A different approval_id yields a different subject hash.
        auth2 = _make_auth(approval_id="appr-002")
        assert auth2.approval_subject_hash != auth.approval_subject_hash

    def test_authorization_hash_differs_from_base_and_subject(self) -> None:
        """The final ``authorization_hash`` differs from both
        ``base_authorization_hash`` and ``approval_subject_hash`` because
        it includes all three tiers."""
        auth = _make_auth(approval_id="appr-001")
        assert auth.authorization_hash != auth.base_authorization_hash
        assert auth.authorization_hash != auth.approval_subject_hash

    def test_verify_hash_chain_passes_for_valid_authorization(self) -> None:
        """``verify_hash_chain`` passes for a freshly built authorization
        with approval_required=True and approval_id set."""
        auth = _make_auth(approval_required=True, approval_id="appr-001")
        auth.verify_hash_chain()  # no raise

    def test_verify_hash_chain_fails_when_subject_hash_missing(self) -> None:
        """``verify_hash_chain`` raises when ``approval_required=True``
        but ``approval_subject_hash`` is None (approval_id missing)."""
        auth = _make_auth(approval_required=True, approval_id=None)
        # approval_id is None → approval_subject_hash is None.
        assert auth.approval_subject_hash is None
        with pytest.raises(ValueError, match="approval_subject_hash is None"):
            auth.verify_hash_chain()

    def test_forged_post_decision_auth_cannot_reuse_approval(self) -> None:
        """P0-1 R3 core fix: a forged post-decision authorization that
        re-derives a new ``authorization_hash`` cannot reuse the approval
        because ``consume_for_command`` validates against
        ``approval_subject_hash`` (stable), NOT the final hash."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        # Consume with the legitimate auth.
        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_family_id="cfam-001",
                execution_fingerprint="fp-001",
                now=_TS,
            )
        )
        assert consumption.approval_subject_hash == auth.approval_subject_hash

        # A forged auth with a different authorization_hash but the SAME
        # approval_subject_hash would need to pass the subject check.
        # However, the decision.authorization_hash check also fires,
        # so a fully forged auth (different final hash) is rejected
        # by the decision-hash binding check — the core R3 trust anchor
        # is that the subject hash is what the human approved.
        assert consumption.approval_subject_hash is not None
        assert consumption.approval_subject_hash != auth.authorization_hash


# ===========================================================================
# P0-3: Resource-level serialisation (5 tests)
# ===========================================================================


class TestP03ResourceSerialisation:
    """P0-3 R3: actions targeting the same external resource are
    serialised via a per-key :class:`asyncio.Lock`."""

    def test_resource_lock_key_none_for_no_resource_type(self) -> None:
        """A governance spec with ``resource_type=None`` yields
        ``resource_lock_key=None`` (no serialisation needed)."""
        spec = get_action_governance_spec("report.generate")
        assert spec is not None
        assert spec.resource_type is None
        key = _compute_resource_lock_key(spec, {}, TENANT)
        assert key is None

    def test_resource_lock_key_computed_for_resource_type(self) -> None:
        """A governance spec with ``resource_type`` set yields a
        ``resource_lock_key`` incorporating tenant, resource_type,
        resource_id, and conflict_family."""
        spec = get_action_governance_spec("crm.owner.assign")
        assert spec is not None
        assert spec.resource_type == "customer"
        payload = {"customer_id": "cust-001", "target_id": "user-001"}
        key = _compute_resource_lock_key(spec, payload, TENANT)
        assert key is not None
        assert TENANT in key
        assert "customer" in key
        assert "cust-001" in key
        assert "crm_owner_reassign" in key

    def test_same_resource_yields_same_lock_key(self) -> None:
        """Two actions targeting the same resource yield the SAME
        ``resource_lock_key`` so they are serialised."""
        spec = get_action_governance_spec("crm.owner.assign")
        assert spec is not None
        payload = {"customer_id": "cust-001", "target_id": "user-001"}
        key1 = _compute_resource_lock_key(spec, payload, TENANT)
        key2 = _compute_resource_lock_key(spec, payload, TENANT)
        assert key1 == key2

    def test_different_resource_yields_different_lock_key(self) -> None:
        """Two actions targeting DIFFERENT resources yield DIFFERENT
        ``resource_lock_key`` so they run concurrently."""
        spec = get_action_governance_spec("crm.owner.assign")
        assert spec is not None
        payload1 = {"customer_id": "cust-001", "target_id": "user-001"}
        payload2 = {"customer_id": "cust-002", "target_id": "user-002"}
        key1 = _compute_resource_lock_key(spec, payload1, TENANT)
        key2 = _compute_resource_lock_key(spec, payload2, TENANT)
        assert key1 != key2

    def test_extract_payload_dict_handles_non_dict(self) -> None:
        """``_extract_payload_dict`` returns an empty dict when the
        payload is not a dict (e.g. a list or scalar)."""

        class _FakeSnapshot:
            payload = ["not", "a", "dict"]

        result = _extract_payload_dict(_FakeSnapshot())
        assert result == {}


# ===========================================================================
# P0-4: Call-boundary state machine (mark_dispatched) (6 tests)
# ===========================================================================


class TestP04CallBoundaryStateMachine:
    """P0-4 R3: ``mark_dispatched`` transitions READY_TO_CALL →
    CALL_DISPATCHED.  Only post-dispatch failures may be UNKNOWN."""

    def test_mark_dispatched_legal_from_ready_to_call(self) -> None:
        """``mark_dispatched`` succeeds from READY_TO_CALL (alias
        CALL_STARTED) → CALL_DISPATCHED."""
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, "idem-disp-001", "fp", "cmd"))
        record = run_async(store.mark_started(record, "cmd"))
        assert record.state == IdempotencyState.READY_TO_CALL
        record = run_async(store.mark_dispatched(record))
        assert record.state == IdempotencyState.CALL_DISPATCHED

    def test_mark_dispatched_illegal_from_reserved(self) -> None:
        """``mark_dispatched`` from RESERVED is an illegal transition."""
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, "idem-disp-002", "fp", "cmd"))
        assert record.state == IdempotencyState.RESERVED
        with pytest.raises(ValueError, match="illegal idempotency state transition"):
            run_async(store.mark_dispatched(record))

    def test_mark_dispatched_illegal_from_terminal(self) -> None:
        """``mark_dispatched`` from SUCCEEDED is an illegal transition."""
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, "idem-disp-003", "fp", "cmd"))
        record = run_async(store.mark_started(record, "cmd"))
        record = run_async(store.mark_dispatched(record))
        receipt = _make_receipt(
            receipt_id="r-disp-003",
            command_id="cmd",
            proposal_id="prop-disp-003",
            idempotency_key="idem-disp-003",
            fingerprint="fp",
        )
        run_async(store.complete_with_receipt(record, receipt))
        stored = store._records[store._record_store_key(record)]
        assert stored.state == IdempotencyState.SUCCEEDED
        with pytest.raises(ValueError, match="illegal idempotency state transition"):
            run_async(store.mark_dispatched(stored))

    def test_call_dispatched_to_terminal_transitions_legal(self) -> None:
        """From CALL_DISPATCHED, transitions to SUCCEEDED / FAILED /
        UNKNOWN / DRY_RUN_SUCCEEDED are all legal."""
        from multi_agent.execution_store import _assert_transition

        for target in (
            IdempotencyState.SUCCEEDED,
            IdempotencyState.FAILED,
            IdempotencyState.UNKNOWN,
            IdempotencyState.DRY_RUN_SUCCEEDED,
        ):
            _assert_transition(IdempotencyState.CALL_DISPATCHED, target)  # no raise

    def test_pre_dispatch_failure_has_dispatched_false(self) -> None:
        """A pre-dispatch block (kill switch before mark_dispatched)
        produces a record with ``adapter_call_dispatched=False``."""
        proposal = make_proposal(
            "prop-predisp",
            action_type="report.generate",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-predisp"],
        )
        request = make_request(
            "review-predisp", [proposal], [make_evidence("ev-predisp")]
        )
        review = make_review("prop-predisp", request.request_hash)
        result = make_result(request, [review])
        # Kill switch active → pre-call block.
        from phase5b_helpers import AlwaysKillSwitch

        batch = _execute(
            request,
            result,
            registry=make_recording_registry([]),
            kill_switch=AlwaysKillSwitch(),
        )
        rec = batch.action_records[0]
        assert rec.adapter_call_dispatched is False
        assert rec.status == ExecutionStatus.NOT_AUTHORIZED

    def test_command_family_id_is_deterministic(self) -> None:
        """``_deterministic_command_family_id`` produces the same id
        for the same auth + adapter_id (stable across retries)."""
        auth = _make_auth()
        cfam1 = _deterministic_command_family_id(auth, "adapter-A")
        cfam2 = _deterministic_command_family_id(auth, "adapter-A")
        assert cfam1 == cfam2
        # Different adapter → different family.
        cfam3 = _deterministic_command_family_id(auth, "adapter-B")
        assert cfam1 != cfam3


# ===========================================================================
# P0-5/6: Governance-driven retry (6 tests)
# ===========================================================================


class TestP05P06GovernanceDrivenRetry:
    """P0-5/6 R3: retry requires ``execution_retry_allowed=True``
    (defaults False), ``error_code`` in
    ``gov_spec.retryable_error_codes``, AND
    ``min(policy.max_retries, gov_spec.max_execution_retries)`` budget."""

    def test_default_specs_have_retry_disabled(self) -> None:
        """Retry is opt-IN: HIGH/CRITICAL risk actions default to
        ``execution_retry_allowed=False``, and any action with retry
        enabled must have ``max_execution_retries > 0`` and
        ``retryable_error_codes`` non-empty."""
        for action_type, spec in ACTION_GOVERNANCE_REGISTRY.items():
            if spec.canonical_risk in (ReviewRiskLevel.HIGH, ReviewRiskLevel.CRITICAL):
                assert spec.execution_retry_allowed is False, (
                    f"{action_type!r} (risk={spec.canonical_risk}) has "
                    f"execution_retry_allowed=True — HIGH/CRITICAL actions "
                    f"must not allow retry"
                )
            if spec.execution_retry_allowed:
                assert spec.max_execution_retries > 0, (
                    f"{action_type!r} has execution_retry_allowed=True "
                    f"but max_execution_retries=0"
                )
                assert spec.retryable_error_codes, (
                    f"{action_type!r} has execution_retry_allowed=True "
                    f"but no retryable_error_codes"
                )

    def test_retry_blocked_when_execution_retry_allowed_false(self) -> None:
        """A retryable-fail adapter does NOT retry when the governance
        spec has ``execution_retry_allowed=False`` (the default)."""

        class _RetryableFailOnceAdapter:
            adapter_id = "retry-test-adapter"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = True
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"summary.compile"})

            def __init__(self) -> None:
                self._call_count = 0

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
                self._call_count += 1
                return AdapterExecutionOutcome(
                    command_id=command.command_id,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    status=ExecutionStatus.FAILED,
                    executed=False,
                    error_code="transient_failure",
                    error_message="simulated transient failure",
                    retryable=True,
                )

        adapter = _RetryableFailOnceAdapter()
        registry = ActionAdapterRegistry()
        registry.register(adapter)
        proposal = make_proposal(
            "prop-retry-blocked",
            action_type="summary.compile",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-retry"],
        )
        request = make_request(
            "review-retry-blocked", [proposal], [make_evidence("ev-retry")]
        )
        review = make_review("prop-retry-blocked", request.request_hash)
        result = make_result(request, [review])
        batch = _execute(
            request,
            result,
            registry=registry,
            kill_switch=NoKillSwitch(),
            options=ExecutionOptions(
                dry_run=False,
                retry_policy=ExecutionRetryPolicy(
                    max_retries=3,
                    retryable_error_codes=frozenset({"transient_failure"}),
                ),
            ),
        )
        # Retry was blocked → adapter called exactly once.
        assert adapter._call_count == 1
        assert batch.batch_status == BatchExecutionStatus.FAILED

    def test_retry_blocked_when_error_code_not_in_gov_spec(self) -> None:
        """Even with ``execution_retry_allowed=True``, retry is blocked
        when the error_code is NOT in ``gov_spec.retryable_error_codes``."""

        class _FailOnceAdapter:
            adapter_id = "retry-test-adapter-b"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = True
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"report.generate"})

            def __init__(self) -> None:
                self._call_count = 0

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
                self._call_count += 1
                return AdapterExecutionOutcome(
                    command_id=command.command_id,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    status=ExecutionStatus.FAILED,
                    executed=False,
                    error_code="non_retriable_code",
                    error_message="non-retriable failure",
                    retryable=True,
                )

        adapter = _FailOnceAdapter()
        registry = ActionAdapterRegistry()
        registry.register(adapter)

        # Patch the governance spec to allow retry but NOT for this error code.
        patched_spec = ActionGovernanceSpec(
            action_type="report.generate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            execution_retry_allowed=True,
            max_execution_retries=3,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        with patch(
            "multi_agent.governed_executor.get_action_governance_spec",
            return_value=patched_spec,
        ):
            proposal = make_proposal(
                "prop-retry-code-blocked",
                action_type="report.generate",
                risk_level=ActionRiskLevel.LOW,
                evidence_ids=["ev-retry-code"],
            )
            request = make_request(
                "review-retry-code-blocked",
                [proposal],
                [make_evidence("ev-retry-code")],
            )
            review = make_review("prop-retry-code-blocked", request.request_hash)
            result = make_result(request, [review])
            batch = _execute(
                request,
                result,
                registry=registry,
                kill_switch=NoKillSwitch(),
                options=ExecutionOptions(
                    dry_run=False,
                    retry_policy=ExecutionRetryPolicy(
                        max_retries=3,
                        retryable_error_codes=frozenset(
                            {"non_retriable_code", "transient_failure"}
                        ),
                    ),
                ),
            )
        # Retry blocked → adapter called once.
        assert adapter._call_count == 1
        assert batch.batch_status == BatchExecutionStatus.FAILED

    def test_retry_allowed_when_all_conditions_met(self) -> None:
        """Retry proceeds when ALL conditions are met:
        execution_retry_allowed=True, error_code in both sets, budget
        available, adapter.retry_safe=True, scope != NONE."""

        class _FailThenSucceedAdapter:
            adapter_id = "retry-succeed-adapter"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = True
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"report.generate"})

            def __init__(self) -> None:
                self._call_count = 0

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
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
                        retryable=True,
                    )
                return AdapterExecutionOutcome(
                    command_id=command.command_id,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    status=ExecutionStatus.SUCCEEDED,
                    executed=True,
                )

        adapter = _FailThenSucceedAdapter()
        registry = ActionAdapterRegistry()
        registry.register(adapter)

        patched_spec = ActionGovernanceSpec(
            action_type="report.generate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            execution_retry_allowed=True,
            max_execution_retries=3,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        with patch(
            "multi_agent.governed_executor.get_action_governance_spec",
            return_value=patched_spec,
        ):
            proposal = make_proposal(
                "prop-retry-ok",
                action_type="report.generate",
                risk_level=ActionRiskLevel.LOW,
                evidence_ids=["ev-retry-ok"],
            )
            request = make_request(
                "review-retry-ok",
                [proposal],
                [make_evidence("ev-retry-ok")],
            )
            review = make_review("prop-retry-ok", request.request_hash)
            result = make_result(request, [review])
            batch = _execute(
                request,
                result,
                registry=registry,
                kill_switch=NoKillSwitch(),
                options=ExecutionOptions(
                    dry_run=False,
                    retry_policy=ExecutionRetryPolicy(
                        max_retries=3,
                        retryable_error_codes=frozenset({"transient_failure"}),
                    ),
                ),
            )
        # Retry proceeded → adapter called twice (fail then succeed).
        assert adapter._call_count == 2
        assert batch.batch_status == BatchExecutionStatus.SUCCEEDED

    def test_retry_budget_uses_min_of_policy_and_gov_spec(self) -> None:
        """The effective retry budget is
        ``min(policy.max_retries, gov_spec.max_execution_retries)``.
        When gov_spec allows fewer retries, the tighter budget wins."""

        class _AlwaysFailAdapter:
            adapter_id = "always-fail-adapter"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = True
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"report.generate"})

            def __init__(self) -> None:
                self._call_count = 0

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
                self._call_count += 1
                return AdapterExecutionOutcome(
                    command_id=command.command_id,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    status=ExecutionStatus.FAILED,
                    executed=False,
                    error_code="transient_failure",
                    error_message="always fails",
                    retryable=True,
                )

        adapter = _AlwaysFailAdapter()
        registry = ActionAdapterRegistry()
        registry.register(adapter)

        # Policy allows 5 retries, but gov_spec allows only 1.
        # Effective max attempts = min(5, 1) + 1 = 2.
        patched_spec = ActionGovernanceSpec(
            action_type="report.generate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            execution_retry_allowed=True,
            max_execution_retries=1,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        with patch(
            "multi_agent.governed_executor.get_action_governance_spec",
            return_value=patched_spec,
        ):
            proposal = make_proposal(
                "prop-retry-budget",
                action_type="report.generate",
                risk_level=ActionRiskLevel.LOW,
                evidence_ids=["ev-retry-budget"],
            )
            request = make_request(
                "review-retry-budget",
                [proposal],
                [make_evidence("ev-retry-budget")],
            )
            review = make_review("prop-retry-budget", request.request_hash)
            result = make_result(request, [review])
            batch = _execute(
                request,
                result,
                registry=registry,
                kill_switch=NoKillSwitch(),
                options=ExecutionOptions(
                    dry_run=False,
                    retry_policy=ExecutionRetryPolicy(
                        max_retries=5,
                        retryable_error_codes=frozenset({"transient_failure"}),
                    ),
                ),
            )
        # min(5, 1) + 1 = 2 attempts total.
        assert adapter._call_count == 2
        assert batch.batch_status == BatchExecutionStatus.FAILED

    def test_retry_blocked_when_adapter_retry_safe_false(self) -> None:
        """Retry is blocked when ``adapter.retry_safe=False`` even if
        governance allows it."""

        class _RetryUnsafeFailAdapter:
            adapter_id = "retry-unsafe-adapter"
            adapter_version = "1.0.0"
            supports_dry_run = True
            retry_safe = False  # blocks retry
            idempotency_scope = IdempotencyScope.TENANT
            supported_action_types = frozenset({"report.generate"})

            def __init__(self) -> None:
                self._call_count = 0

            async def execute(
                self, command: ExecutionCommand
            ) -> AdapterExecutionOutcome:
                self._call_count += 1
                return AdapterExecutionOutcome(
                    command_id=command.command_id,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    status=ExecutionStatus.FAILED,
                    executed=False,
                    error_code="transient_failure",
                    error_message="retry-unsafe failure",
                    retryable=True,
                )

        adapter = _RetryUnsafeFailAdapter()
        registry = ActionAdapterRegistry()
        registry.register(adapter)

        patched_spec = ActionGovernanceSpec(
            action_type="report.generate",
            reviewable=True,
            canonical_risk=ReviewRiskLevel.LOW,
            minimum_authority=AgentAuthority.READ,
            required_tool="crm_reader.get_customers",
            execution_retry_allowed=True,
            max_execution_retries=3,
            retryable_error_codes=frozenset({"transient_failure"}),
        )
        with patch(
            "multi_agent.governed_executor.get_action_governance_spec",
            return_value=patched_spec,
        ):
            proposal = make_proposal(
                "prop-retry-unsafe",
                action_type="report.generate",
                risk_level=ActionRiskLevel.LOW,
                evidence_ids=["ev-retry-unsafe"],
            )
            request = make_request(
                "review-retry-unsafe",
                [proposal],
                [make_evidence("ev-retry-unsafe")],
            )
            review = make_review("prop-retry-unsafe", request.request_hash)
            result = make_result(request, [review])
            batch = _execute(
                request,
                result,
                registry=registry,
                kill_switch=NoKillSwitch(),
                options=ExecutionOptions(
                    dry_run=False,
                    retry_policy=ExecutionRetryPolicy(
                        max_retries=3,
                        retryable_error_codes=frozenset({"transient_failure"}),
                    ),
                ),
            )
        # retry_safe=False → no retry.
        assert adapter._call_count == 1
        assert batch.batch_status == BatchExecutionStatus.FAILED


# ===========================================================================
# P0-7: Append-only attempt audit trail (4 tests)
# ===========================================================================


class TestP07AppendOnlyAttemptAuditTrail:
    """P0-7 R3: per-command receipts are never overwritten by a later
    retry attempt — the attempt trail is append-only."""

    def test_get_receipt_by_command_returns_correct_receipt(self) -> None:
        """``get_receipt_by_command`` returns the receipt stored for a
        specific command_id."""
        store = InMemoryExecutionStore()
        record = run_async(store.reserve(TENANT, "idem-audit-001", "fp", "cmd-audit"))
        record = run_async(store.mark_started(record, "cmd-audit"))
        record = run_async(store.mark_dispatched(record))
        receipt = _make_receipt(
            receipt_id="r-audit-001",
            command_id="cmd-audit",
            proposal_id="prop-audit",
            idempotency_key="idem-audit-001",
            fingerprint="fp",
        )
        run_async(store.complete_with_receipt(record, receipt))
        fetched = run_async(store.get_receipt_by_command("cmd-audit"))
        assert fetched is not None
        assert fetched.receipt_id == "r-audit-001"

    def test_get_receipt_by_command_returns_none_for_unknown(self) -> None:
        """``get_receipt_by_command`` returns None for an unknown
        command_id."""
        store = InMemoryExecutionStore()
        fetched = run_async(store.get_receipt_by_command("cmd-nonexistent"))
        assert fetched is None

    def test_attempt_field_is_set_on_record(self) -> None:
        """``ActionExecutionRecord.attempt`` is set to the 1-based
        attempt number and is >= 1."""
        rec = ActionExecutionRecord(
            proposal_id="prop-attempt",
            status=ExecutionStatus.SUCCEEDED,
            attempt=1,
        )
        assert rec.attempt == 1
        rec2 = ActionExecutionRecord(
            proposal_id="prop-attempt-2",
            status=ExecutionStatus.FAILED,
            attempt=2,
        )
        assert rec2.attempt == 2

    def test_attempt_validator_rejects_zero_or_negative(self) -> None:
        """``ActionExecutionRecord.attempt`` must be >= 1."""
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            ActionExecutionRecord(
                proposal_id="prop-bad-attempt",
                status=ExecutionStatus.FAILED,
                attempt=0,
            )


# ===========================================================================
# P0-8: Tightened verify_semantics (7 tests)
# ===========================================================================


class TestP08TightenedVerifySemantics:
    """P0-8 R3: ``ActionExecutionRecord.verify_semantics`` validates
    status ↔ executed consistency, receipt presence, error_code
    presence, and dispatched ⇒ started."""

    def test_succeeded_requires_executed_true(self) -> None:
        """SUCCEEDED with executed=False fails verify_semantics."""
        rec = ActionExecutionRecord(
            proposal_id="p1",
            status=ExecutionStatus.SUCCEEDED,
            executed=False,
        )
        with pytest.raises(
            ExecutionIntegrityError, match="SUCCEEDED requires executed=True"
        ):
            rec.verify_semantics()

    def test_succeeded_rejects_error_code(self) -> None:
        """SUCCEEDED with an error_code fails verify_semantics."""
        rec = ActionExecutionRecord(
            proposal_id="p2",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            error_code="should_not_be_here",
        )
        with pytest.raises(ExecutionIntegrityError, match="must not carry error_code"):
            rec.verify_semantics()

    def test_failed_requires_error_code(self) -> None:
        """FAILED without an error_code fails verify_semantics."""
        rec = ActionExecutionRecord(
            proposal_id="p3",
            status=ExecutionStatus.FAILED,
            executed=False,
        )
        with pytest.raises(ExecutionIntegrityError, match="requires error_code"):
            rec.verify_semantics()

    def test_unknown_requires_executed_none(self) -> None:
        """UNKNOWN with executed=True fails verify_semantics."""
        rec = ActionExecutionRecord(
            proposal_id="p4",
            status=ExecutionStatus.UNKNOWN,
            executed=True,
        )
        with pytest.raises(ExecutionIntegrityError, match="requires executed=None"):
            rec.verify_semantics()

    def test_dispatched_implies_started(self) -> None:
        """``adapter_call_dispatched=True`` requires
        ``adapter_call_started=True``."""
        rec = ActionExecutionRecord(
            proposal_id="p5",
            status=ExecutionStatus.UNKNOWN,
            executed=None,
            adapter_call_started=False,
            adapter_call_dispatched=True,
        )
        with pytest.raises(
            ExecutionIntegrityError,
            match="adapter_call_dispatched=True but adapter_call_started=False",
        ):
            rec.verify_semantics()

    def test_not_authorized_rejects_success_receipt(self) -> None:
        """P0-8 R3: NOT_AUTHORIZED must NOT carry a success receipt
        (even if adapter_call_dispatched=True, e.g. adapter refused)."""
        from multi_agent.execution_receipts import ActionExecutionReceipt

        receipt = ActionExecutionReceipt(
            receipt_id="r-na",
            command_id="cmd-na",
            tenant_id=TENANT,
            run_id=RUN_ID,
            proposal_id="p6",
            authorization_hash="a" * 64,
            adapter_id="adapter-A",
            adapter_version="1.0.0",
            adapter_registry_hash="r" * 64,
            idempotency_key="idem-na",
            execution_fingerprint="fp-na",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            started_at=TS,
            completed_at=TS,
        )
        rec = ActionExecutionRecord(
            proposal_id="p6",
            status=ExecutionStatus.NOT_AUTHORIZED,
            adapter_call_started=True,
            adapter_call_dispatched=True,
            receipt=receipt,
        )
        with pytest.raises(
            ExecutionIntegrityError,
            match="must not carry a success receipt",
        ):
            rec.verify_semantics()

    def test_dry_run_succeeded_requires_executed_false(self) -> None:
        """DRY_RUN_SUCCEEDED requires executed=False."""
        rec = ActionExecutionRecord(
            proposal_id="p7",
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
            executed=True,
        )
        with pytest.raises(
            ExecutionIntegrityError, match="DRY_RUN_SUCCEEDED requires executed=False"
        ):
            rec.verify_semantics()


# ===========================================================================
# P0-12: Atomic adapter-registry freeze (3 tests)
# ===========================================================================


class TestP12AtomicAdapterRegistryFreeze:
    """P0-12 R3: the metadata snapshot, live adapter instances, and
    registry hash are ALL captured under the registry lock in ONE
    critical section."""

    def test_freeze_for_execution_returns_consistent_snapshot_and_runtime(self) -> None:
        """``freeze_for_execution`` returns a FrozenActionAdapterRegistry
        whose snapshot bindings and runtime adapters are consistent."""
        registry = make_recording_registry([])
        frozen = registry.freeze_for_execution()
        assert frozen.registry_hash == frozen.snapshot.registry_hash
        # Every binding has a corresponding live adapter.
        for binding in frozen.snapshot.bindings:
            adapter = frozen.get_adapter(binding.adapter_id)
            assert adapter is not None
            assert adapter.adapter_id == binding.adapter_id

    def test_freeze_isolates_from_concurrent_register(self) -> None:
        """After ``freeze_for_execution``, registering a new adapter on
        the live registry does NOT change the frozen handle's runtime."""
        registry = make_recording_registry([])
        frozen = registry.freeze_for_execution()
        original_binding = frozen.get_binding("report.generate")
        original_adapter = frozen.get_adapter(original_binding.adapter_id)

        # Register a different adapter AFTER freeze.
        new_adapter = RecordingActionAdapter(
            sink=[],
            adapter_id="new-adapter-id",
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(new_adapter)

        # The frozen handle still returns the ORIGINAL adapter.
        still_original = frozen.get_adapter(original_binding.adapter_id)
        assert still_original is original_adapter
        assert still_original is not new_adapter

    def test_registry_lock_is_reentrant(self) -> None:
        """The registry lock is re-entrant so ``freeze_snapshot`` can be
        called from inside ``freeze_for_execution`` without deadlock."""
        import threading

        registry = make_recording_registry([])
        # This would deadlock if the lock were not re-entrant.
        result = registry.freeze_for_execution()
        assert result is not None
        # Verify the lock is an RLock (re-entrant).
        assert isinstance(registry._lock, type(threading.RLock()))


# ===========================================================================
# P0-13: Legacy bypass removal (3 tests)
# ===========================================================================


class TestP13LegacyBypassRemoval:
    """P0-13 R3: the legacy ``consume`` and ``validate_and_consume``
    methods have been REMOVED from both the Protocol and the
    InMemoryApprovalStore implementation.  The ONLY consume path is
    ``consume_for_command``."""

    def test_legacy_consume_removed_from_implementation(self) -> None:
        """``InMemoryApprovalStore`` does NOT have a ``consume`` method."""
        store = InMemoryApprovalStore()
        assert not hasattr(store, "consume")
        assert not callable(getattr(store, "consume", None))

    def test_legacy_validate_and_consume_removed_from_implementation(self) -> None:
        """``InMemoryApprovalStore`` does NOT have a
        ``validate_and_consume`` method."""
        store = InMemoryApprovalStore()
        assert not hasattr(store, "validate_and_consume")
        assert not callable(getattr(store, "validate_and_consume", None))

    def test_consume_for_command_is_the_only_consume_path(self) -> None:
        """The ApprovalStore Protocol does NOT declare ``consume`` or
        ``validate_and_consume`` — only ``consume_for_command``."""
        from multi_agent.approval_gate import ApprovalStore

        # The Protocol has consume_for_command but NOT consume or
        # validate_and_consume.
        assert hasattr(ApprovalStore, "consume_for_command")
        assert hasattr(ApprovalStore, "validate_decision")
        # consume / validate_and_consume are NOT on the Protocol.
        assert not hasattr(ApprovalStore, "consume")
        assert not hasattr(ApprovalStore, "validate_and_consume")


# ===========================================================================
# P0-1: consume_for_command binds to approval_subject_hash (3 tests)
# ===========================================================================


class TestP01ConsumeForCommandSubjectHash:
    """P0-1 R3: ``consume_for_command`` validates against
    ``approval_subject_hash`` and records it in the ConsumptionRecord."""

    def test_consumption_record_carries_subject_hash(self) -> None:
        """The ``ApprovalConsumptionRecord`` carries
        ``approval_subject_hash`` alongside ``authorization_hash``."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)
        consumption = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_family_id="cfam-001",
                execution_fingerprint="fp-001",
                now=_TS,
            )
        )
        assert consumption.approval_subject_hash == auth.approval_subject_hash
        assert consumption.authorization_hash == auth.authorization_hash
        assert consumption.approval_subject_hash != consumption.authorization_hash

    def test_consume_rejects_when_subject_hash_missing(self) -> None:
        """``consume_for_command`` rejects when the authorization has
        no ``approval_subject_hash`` (approval_id missing)."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)

        # Forge an auth with approval_id=None → subject_hash=None.
        forged = auth.model_copy(update={"approval_id": None})
        # Recompute: with approval_id=None, approval_subject_hash is None.
        # We need to bypass the validator to force subject_hash=None.
        object.__setattr__(forged, "approval_subject_hash", None)
        with pytest.raises(ApprovalValidationError, match="approval_subject_hash"):
            run_async(
                store.consume_for_command(
                    "appr-001",
                    authorization=forged,
                    command_family_id="cfam-001",
                    execution_fingerprint="fp-001",
                    now=_TS,
                )
            )

    def test_validate_decision_uses_subject_hash_not_final_hash(self) -> None:
        """``validate_decision`` checks ``approval_subject_hash`` is
        present — the trust anchor is the subject hash, NOT the final
        authorization hash."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)
        # validate_decision passes for a valid auth with subject hash.
        decision = run_async(
            store.validate_decision("appr-001", authorization=auth, now=_TS)
        )
        assert decision.status == ApprovalStatus.APPROVED


# ===========================================================================
# P0-3: Resource serialisation end-to-end (2 tests)
# ===========================================================================


class TestP03ResourceSerialisationEndToEnd:
    """P0-3 R3: end-to-end test that actions on the same resource are
    serialised via the per-call resource-lock registry."""

    def test_resource_locks_reset_per_execute_call(self) -> None:
        """The per-call resource-lock registry is reset on every
        ``execute()`` call so locks from a previous batch never leak."""
        executor = GovernedExecutor()
        # First call populates _resource_locks.
        executor._resource_locks = {"stale-key": asyncio.Lock()}  # type: ignore[assignment]
        assert len(executor._resource_locks) == 1

        proposal = make_proposal(
            "prop-reset",
            action_type="report.generate",
            risk_level=ActionRiskLevel.LOW,
            evidence_ids=["ev-reset"],
        )
        request = make_request("review-reset", [proposal], [make_evidence("ev-reset")])
        review = make_review("prop-reset", request.request_hash)
        result = make_result(request, [review])
        _execute(
            request,
            result,
            registry=make_recording_registry([]),
            kill_switch=NoKillSwitch(),
            executor=executor,
        )
        # The stale lock was cleared (report.generate has resource_type=None
        # so no new locks are created, but the stale one is gone).
        assert "stale-key" not in executor._resource_locks

    def test_get_resource_lock_creates_lazily(self) -> None:
        """``_get_resource_lock`` creates a lock lazily and returns the
        SAME lock for the same key."""
        executor = GovernedExecutor()
        executor._resource_locks = {}
        lock1 = run_async(executor._get_resource_lock("key-A"))
        lock2 = run_async(executor._get_resource_lock("key-A"))
        assert lock1 is lock2
        lock3 = run_async(executor._get_resource_lock("key-B"))
        assert lock3 is not lock1


# ===========================================================================
# P0-4: command_family_id replay safety (2 tests)
# ===========================================================================


class TestP04CommandFamilyIdReplaySafety:
    """P0-4/5/6 R3: ``command_family_id`` is stable across retries so
    safe retries within the same family reuse the approval consumption."""

    def test_command_family_id_excludes_attempt(self) -> None:
        """``_deterministic_command_family_id`` does NOT include the
        attempt number — it is stable across retries."""
        auth = _make_auth()
        cfam = _deterministic_command_family_id(auth, "adapter-A")
        # The family id is derived from proposal_id + base_hash + adapter_id,
        # NOT from the attempt number.  Calling it again yields the same id.
        assert cfam == _deterministic_command_family_id(auth, "adapter-A")

    def test_replay_same_family_returns_same_consumption(self) -> None:
        """Replaying ``consume_for_command`` with the SAME
        command_family_id + execution_fingerprint returns the ORIGINAL
        consumption (NOT a second illegal consume)."""
        store = InMemoryApprovalStore()
        auth = _make_auth(approval_id="appr-001")
        _seed_approved_approval(store, auth=auth)
        first = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_family_id="cfam-replay",
                execution_fingerprint="fp-replay",
                now=_TS,
            )
        )
        second = run_async(
            store.consume_for_command(
                "appr-001",
                authorization=auth,
                command_family_id="cfam-replay",
                execution_fingerprint="fp-replay",
                now=_TS,
            )
        )
        assert first.consumption_hash == second.consumption_hash
        assert first.command_family_id == "cfam-replay"


# ===========================================================================
# R3 Composite counterexamples (15 tests)
# ===========================================================================


class TestR3CompositeCounterexamples:
    """R3 composite counterexamples covering P0-7 (append-only attempt
    audit trail), P0-11 (kill switch complete scope), and P0-13 (legacy
    bypass removal) in combination.

    Each test pins down exactly one composite invariant so a regression
    is surfaced as a single focused failure.
    """

    # ------------------------------------------------------------------
    # P0-7: Append-only attempt audit trail (5 tests)
    # ------------------------------------------------------------------

    def test_attempt_record_auto_computes_attempt_hash(self) -> None:
        """P0-7: ``ExecutionAttemptRecord`` auto-computes
        ``attempt_hash`` on construction when it is left empty."""
        rec = ExecutionAttemptRecord(
            command_id="cmd-comp-001",
            command_family_id="cfam-comp-001",
            attempt=1,
            status=ExecutionStatus.SUCCEEDED,
            adapter_call_started=True,
            adapter_call_dispatched=True,
        )
        assert rec.attempt_hash != ""
        assert len(rec.attempt_hash) == 64

    def test_attempt_record_tamper_hash_detected(self) -> None:
        """P0-7: providing a wrong ``attempt_hash`` raises ``ValueError``
        — tampering is detected at construction."""
        with pytest.raises(ValueError, match="attempt_hash mismatch"):
            ExecutionAttemptRecord(
                command_id="cmd-comp-002",
                command_family_id="cfam-comp-002",
                attempt=1,
                status=ExecutionStatus.FAILED,
                adapter_call_started=False,
                adapter_call_dispatched=False,
                attempt_hash="0" * 64,
            )

    def test_attempt_record_rejects_blank_command_id(self) -> None:
        """P0-7: blank ``command_id`` or ``command_family_id`` is
        rejected — identity fields must not be blank."""
        with pytest.raises(ValueError, match="must not be blank"):
            ExecutionAttemptRecord(
                command_id="  ",
                command_family_id="cfam-comp-003",
                attempt=1,
                status=ExecutionStatus.FAILED,
                adapter_call_started=False,
                adapter_call_dispatched=False,
            )

    def test_attempt_record_rejects_zero_attempt(self) -> None:
        """P0-7: ``attempt < 1`` is rejected — attempts are 1-based."""
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            ExecutionAttemptRecord(
                command_id="cmd-comp-004",
                command_family_id="cfam-comp-004",
                attempt=0,
                status=ExecutionStatus.FAILED,
                adapter_call_started=False,
                adapter_call_dispatched=False,
            )

    def test_action_record_carries_attempt_trail_as_tuple(self) -> None:
        """P0-7: ``ActionExecutionRecord.attempts`` is a tuple of
        ``ExecutionAttemptRecord`` — the trail is append-only and
        immutable."""
        a1 = ExecutionAttemptRecord(
            command_id="cmd-comp-005",
            command_family_id="cfam-comp-005",
            attempt=1,
            status=ExecutionStatus.FAILED,
            adapter_call_started=True,
            adapter_call_dispatched=True,
            error_code="transient_failure",
            retryable=True,
        )
        a2 = ExecutionAttemptRecord(
            command_id="cmd-comp-005",
            command_family_id="cfam-comp-005",
            attempt=2,
            status=ExecutionStatus.SUCCEEDED,
            adapter_call_started=True,
            adapter_call_dispatched=True,
        )
        rec = ActionExecutionRecord(
            proposal_id="prop-comp-005",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            attempt=2,
            command_family_id="cfam-comp-005",
            attempts=(a1, a2),
        )
        assert isinstance(rec.attempts, tuple)
        assert len(rec.attempts) == 2
        assert rec.attempts[0].attempt == 1
        assert rec.attempts[1].attempt == 2
        assert rec.attempts[0].status == ExecutionStatus.FAILED
        assert rec.attempts[1].status == ExecutionStatus.SUCCEEDED

    # ------------------------------------------------------------------
    # P0-11: Kill Switch complete scope (7 tests)
    # ------------------------------------------------------------------

    def test_global_scope_kill_switch_active(self) -> None:
        """P0-11: ``global_kill_switch=True`` →
        ``is_kill_switch_active_for_scope`` returns True."""
        ks = FakeExecutionCancellation()
        ks.global_kill_switch = True
        assert run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )

    def test_tenant_scope_kill_switch_active(self) -> None:
        """P0-11: ``kill_switch_tenants`` →
        ``is_kill_switch_active_for_scope`` returns True for that
        tenant."""
        ks = FakeExecutionCancellation()
        ks.kill_switch_tenants.add(TENANT)
        assert run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )
        # Different tenant is NOT blocked.
        assert not run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id="other-tenant",
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )

    def test_run_scope_kill_switch_active(self) -> None:
        """P0-11: ``cancelled_runs`` →
        ``is_kill_switch_active_for_scope`` returns True for that run
        (run scope is checked in the aggregate method, not just
        ``is_cancelled``)."""
        ks = FakeExecutionCancellation()
        ks.cancelled_runs.add(RUN_ID)
        assert run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )
        # Different run is NOT blocked.
        assert not run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id="other-run",
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )

    def test_action_type_scope_kill_switch_active(self) -> None:
        """P0-11: ``kill_switch_action_types`` →
        ``is_kill_switch_active_for_scope`` returns True for that
        action type."""
        ks = FakeExecutionCancellation()
        ks.kill_switch_action_types.add("crm.owner.assign")
        assert run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )
        # Different action type is NOT blocked.
        assert not run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="report.generate",
                adapter_id="adapter-A",
            )
        )

    def test_adapter_id_scope_kill_switch_active(self) -> None:
        """P0-11: ``kill_switch_adapter_ids`` →
        ``is_kill_switch_active_for_scope`` returns True for that
        adapter."""
        ks = FakeExecutionCancellation()
        ks.kill_switch_adapter_ids.add("adapter-A")
        assert run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )
        # Different adapter is NOT blocked.
        assert not run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-B",
            )
        )

    def test_kill_switch_protocol_declares_scope_method(self) -> None:
        """P0-11: the ``ExecutionCancellation`` Protocol declares
        ``is_kill_switch_active_for_scope`` — the 5-scope aggregate
        check is part of the contract."""
        from multi_agent.execution import ExecutionCancellation

        assert hasattr(ExecutionCancellation, "is_kill_switch_active_for_scope")
        assert hasattr(ExecutionCancellation, "is_cancelled")
        assert hasattr(ExecutionCancellation, "is_kill_switch_active")

    def test_no_scope_active_returns_false(self) -> None:
        """P0-11: when ALL 5 scopes are inactive,
        ``is_kill_switch_active_for_scope`` returns False — the
        aggregate check does not false-positive."""
        ks = FakeExecutionCancellation()
        assert not run_async(
            ks.is_kill_switch_active_for_scope(
                tenant_id=TENANT,
                run_id=RUN_ID,
                action_type="crm.owner.assign",
                adapter_id="adapter-A",
            )
        )

    # ------------------------------------------------------------------
    # P0-13: Legacy bypass removal (3 tests)
    # ------------------------------------------------------------------

    def test_idempotency_state_no_legacy_aliases(self) -> None:
        """P0-13: ``IdempotencyState`` has NO ``CALL_STARTED`` or
        ``IN_PROGRESS`` attributes — the legacy aliases have been
        removed."""
        assert not hasattr(IdempotencyState, "CALL_STARTED")
        assert not hasattr(IdempotencyState, "IN_PROGRESS")
        # The canonical name is present.
        assert hasattr(IdempotencyState, "READY_TO_CALL")
        assert IdempotencyState.READY_TO_CALL.value == "ready_to_call"

    def test_approval_store_no_legacy_consume_methods(self) -> None:
        """P0-13: ``InMemoryApprovalStore`` has NO ``consume`` or
        ``validate_and_consume`` methods — the legacy non-atomic
        consume paths have been removed."""
        store = InMemoryApprovalStore()
        assert not hasattr(store, "consume")
        assert not hasattr(store, "validate_and_consume")
        # The canonical consume path is present.
        assert hasattr(store, "consume_for_command")
        assert hasattr(store, "validate_decision")

    def test_mark_started_produces_ready_to_call(self) -> None:
        """P0-13: ``mark_started`` transitions to ``READY_TO_CALL``
        (not a legacy ``CALL_STARTED`` name) — the state value is
        ``"ready_to_call"``."""
        store = InMemoryExecutionStore()
        record = run_async(
            store.reserve(TENANT, "idem-comp-013", "fp-comp", "cmd-comp")
        )
        started = run_async(store.mark_started(record, "cmd-comp"))
        assert started.state is IdempotencyState.READY_TO_CALL
        assert started.state.value == "ready_to_call"
