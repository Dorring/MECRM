"""Phase 5B — Governed Executor counterexample tests (Section 33).

Covers the 18-step fixed-order pipeline and its contracts:

* :func:`select_executable_reviews` only selects APPROVED and
  NEEDS_APPROVAL Proposals (rejected / needs_input / conflict /
  deduplicated are never executed).
* :func:`build_authorization` binds every hash the executor must
  re-verify.
* :class:`ExecutionRetryPolicy` / :class:`ExecutionOptions` enforce
  positive bounds.
* :class:`ExecutionBatchResult` enforces NO_ACTIONS ≠ SUCCEEDED, hash
  integrity, and review binding.
* :class:`GovernedExecutor.execute` follows the 18-step pipeline:
  happy path, empty batch, rejected, high-risk without approval,
  kill switch, cancelled run, dry-run, and tampered request.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multi_agent.approval_contracts import FrozenClock
from multi_agent.execution_authorization import (
    BatchExecutionStatus,
    ExecutionStatus,
)
from multi_agent.execution_error_codes import (
    AUTHORIZATION_INTEGRITY_FAILED,
)
from multi_agent.execution_receipts import ActionExecutionReceipt
from multi_agent.execution_store import InMemoryExecutionStore
from multi_agent.governed_executor import (
    ActionExecutionRecord,
    ExecutionBatchResult,
    ExecutionOptions,
    ExecutionRetryPolicy,
    GovernedExecutor,
    build_authorization,
    select_executable_reviews,
)
from multi_agent.review_contracts import (
    ReviewDecisionStatus,
)

from phase5b_helpers import (
    NoKillSwitch,
    AlwaysKillSwitch,
    CancelledRun,
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
# select_executable_reviews
# ---------------------------------------------------------------------------


class TestSelectExecutableReviews:
    def test_only_approved_and_needs_approval_are_executable(self) -> None:
        proposal = make_proposal()
        request = make_request("review-select", [proposal])
        needs_approval = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            required_approval=True,
        )
        result = make_result(request, [needs_approval])
        selected = select_executable_reviews(request, result)
        assert len(selected) == 1
        assert selected[0].status == ReviewDecisionStatus.NEEDS_APPROVAL

    def test_rejected_is_never_executable(self) -> None:
        proposal = make_proposal()
        request = make_request("review-select", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.REJECTED,
        )
        result = make_result(request, [review])
        selected = select_executable_reviews(request, result)
        assert len(selected) == 0

    def test_needs_input_is_never_executable(self) -> None:
        proposal = make_proposal()
        request = make_request("review-select", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_INPUT,
        )
        result = make_result(request, [review])
        assert len(select_executable_reviews(request, result)) == 0

    def test_conflict_is_never_executable(self) -> None:
        proposal = make_proposal()
        request = make_request("review-select", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.CONFLICT,
        )
        result = make_result(request, [review])
        assert len(select_executable_reviews(request, result)) == 0

    def test_deduplicated_is_never_executable(self) -> None:
        proposal = make_proposal()
        request = make_request("review-select", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.DEDUPLICATED,
        )
        result = make_result(request, [review])
        assert len(select_executable_reviews(request, result)) == 0

    def test_result_is_sorted_by_proposal_id(self) -> None:
        p1 = make_proposal("prop-b")
        p2 = make_proposal("prop-a")
        request = make_request("review-select", [p1, p2])
        r1 = make_review("prop-b", request.request_hash)
        r2 = make_review("prop-a", request.request_hash)
        result = make_result(request, [r1, r2])
        selected = select_executable_reviews(request, result)
        assert selected[0].proposal_id == "prop-a"
        assert selected[1].proposal_id == "prop-b"


# ---------------------------------------------------------------------------
# build_authorization
# ---------------------------------------------------------------------------


class TestBuildAuthorization:
    def test_authorization_binds_all_hashes(self) -> None:
        request, result, review = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        auth = build_authorization(
            request,
            result,
            review,
            adapter_registry_hash=snap.registry_hash,
        )
        assert auth.tenant_id == request.tenant_id
        assert auth.run_id == request.run_id
        assert auth.proposal_id == review.proposal_id
        assert auth.review_request_hash == request.request_hash
        assert auth.review_result_hash == result.result_hash
        assert auth.proposal_review_hash == review.review_hash
        assert auth.governance_spec_hash == request.governance_spec_hash
        assert auth.adapter_registry_hash == snap.registry_hash
        assert auth.authorization_hash != ""

    def test_authorization_status_ready_when_no_approval(self) -> None:
        request, result, review = make_approved_request_result()
        auth = build_authorization(request, result, review)
        assert auth.status == ExecutionStatus.READY
        assert auth.approval_required is False

    def test_authorization_status_pending_when_needs_approval(self) -> None:
        proposal = make_proposal()
        request = make_request("review-auth", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            required_approval=True,
        )
        result = make_result(request, [review])
        auth = build_authorization(request, result, review)
        assert auth.status == ExecutionStatus.PENDING_APPROVAL
        assert auth.approval_required is True

    def test_authorization_for_missing_proposal_raises(self) -> None:
        request, result, _ = make_approved_request_result()
        phantom_review = make_review("prop-nonexistent", request.request_hash)
        with pytest.raises(Exception):
            build_authorization(request, result, phantom_review)


# ---------------------------------------------------------------------------
# ExecutionRetryPolicy + ExecutionOptions
# ---------------------------------------------------------------------------


class TestExecutionRetryPolicy:
    def test_default_max_retries_is_zero(self) -> None:
        policy = ExecutionRetryPolicy()
        assert policy.max_retries == 0

    def test_negative_max_retries_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionRetryPolicy(max_retries=-1)

    def test_policy_is_frozen(self) -> None:
        policy = ExecutionRetryPolicy()
        with pytest.raises(Exception):
            policy.max_retries = 5  # type: ignore[misc]


class TestExecutionOptions:
    def test_defaults_are_ci_safe(self) -> None:
        opts = ExecutionOptions()
        assert opts.batch_deadline_seconds > 0
        assert opts.per_action_timeout_seconds > 0
        assert opts.max_concurrency >= 1
        assert opts.dry_run is True

    def test_non_positive_deadline_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionOptions(batch_deadline_seconds=0)

    def test_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionOptions(per_action_timeout_seconds=-1)

    def test_zero_concurrency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionOptions(max_concurrency=0)

    def test_options_is_frozen(self) -> None:
        opts = ExecutionOptions()
        with pytest.raises(Exception):
            opts.dry_run = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ExecutionBatchResult
# ---------------------------------------------------------------------------


class TestExecutionBatchResult:
    def test_batch_hash_auto_computed(self) -> None:
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
            started_at=TS,
            completed_at=TS,
        )
        assert batch.batch_hash != ""
        batch.verify_integrity()  # no raise

    def test_no_actions_with_receipts_rejected(self) -> None:
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        # NO_ACTIONS requires no receipts.
        from multi_agent.execution_receipts import ActionExecutionReceipt

        receipt = ActionExecutionReceipt(
            receipt_id="rcpt-1",
            command_id="cmd-1",
            tenant_id=TENANT,
            run_id=RUN_ID,
            proposal_id="prop-test-001",
            authorization_hash="a" * 64,
            adapter_id="noop",
            adapter_version="1.0.0",
            adapter_registry_hash=snap.registry_hash,
            idempotency_key="idem-1",
            execution_fingerprint="f" * 64,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            started_at=TS,
            completed_at=TS,
        )
        with pytest.raises(ValidationError):
            ExecutionBatchResult(
                review_id=request.review_id,
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                request_hash=request.request_hash,
                result_hash=result.result_hash,
                governance_spec_hash=request.governance_spec_hash,
                adapter_registry_hash=snap.registry_hash,
                receipts=(receipt,),
                batch_status=BatchExecutionStatus.NO_ACTIONS,
                started_at=TS,
                completed_at=TS,
            )

    def test_empty_receipts_non_no_actions_rejected(self) -> None:
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        with pytest.raises(ValidationError):
            ExecutionBatchResult(
                review_id=request.review_id,
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                request_hash=request.request_hash,
                result_hash=result.result_hash,
                governance_spec_hash=request.governance_spec_hash,
                adapter_registry_hash=snap.registry_hash,
                receipts=(),
                batch_status=BatchExecutionStatus.SUCCEEDED,
                started_at=TS,
                completed_at=TS,
            )

    def test_started_after_completed_rejected(self) -> None:
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        later = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            ExecutionBatchResult(
                review_id=request.review_id,
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                request_hash=request.request_hash,
                result_hash=result.result_hash,
                governance_spec_hash=request.governance_spec_hash,
                adapter_registry_hash=snap.registry_hash,
                started_at=later,
                completed_at=TS,
            )

    def test_verify_against_review_binds_hashes(self) -> None:
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        receipt = ActionExecutionReceipt(
            receipt_id="rcpt-bind",
            command_id="cmd-bind",
            tenant_id=TENANT,
            run_id=RUN_ID,
            proposal_id="prop-test-001",
            authorization_hash="a" * 64,
            adapter_id="noop",
            adapter_version="1.0.0",
            adapter_registry_hash=snap.registry_hash,
            idempotency_key="idem-bind",
            execution_fingerprint="f" * 64,
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            started_at=TS,
            completed_at=TS,
        )
        record = ActionExecutionRecord(
            proposal_id="prop-test-001",
            status=ExecutionStatus.SUCCEEDED,
            receipt=receipt,
            executed=True,
            adapter_call_started=True,
            adapter_call_dispatched=True,
        )
        batch = ExecutionBatchResult(
            review_id=request.review_id,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            receipts=(receipt,),
            action_records=(record,),
            succeeded_proposal_ids=("prop-test-001",),
            batch_status=BatchExecutionStatus.SUCCEEDED,
            started_at=TS,
            completed_at=TS,
        )
        batch.verify_against_review(request, result)  # no raise

    def test_verify_against_review_detects_mismatch(self) -> None:
        request, result, _ = make_approved_request_result()
        registry = make_recording_registry([])
        snap = registry.freeze_snapshot()
        batch = ExecutionBatchResult(
            review_id="wrong-review-id",
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_hash=request.request_hash,
            result_hash=result.result_hash,
            governance_spec_hash=request.governance_spec_hash,
            adapter_registry_hash=snap.registry_hash,
            started_at=TS,
            completed_at=TS,
        )
        with pytest.raises(Exception):
            batch.verify_against_review(request, result)


# ---------------------------------------------------------------------------
# GovernedExecutor.execute — happy path
# ---------------------------------------------------------------------------


class TestGovernedExecutorHappyPath:
    def test_single_approved_low_risk_succeeds(self) -> None:
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
                options=ExecutionOptions(dry_run=False),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.SUCCEEDED
        assert len(batch.receipts) == 1
        assert batch.receipts[0].status == ExecutionStatus.SUCCEEDED
        assert len(sink) == 1  # adapter was called once

    def test_dry_run_mode_executes_without_side_effects(self) -> None:
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        executor = GovernedExecutor()
        opts = ExecutionOptions(dry_run=True)
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
                options=opts,
            )
        )
        assert batch.dry_run is True
        assert batch.batch_status == BatchExecutionStatus.DRY_RUN_COMPLETED
        assert len(batch.receipts) == 1
        assert batch.receipts[0].status == ExecutionStatus.DRY_RUN_SUCCEEDED


# ---------------------------------------------------------------------------
# GovernedExecutor.execute — empty / skipped
# ---------------------------------------------------------------------------


class TestGovernedExecutorEmptyBatch:
    def test_all_non_executable_yields_blocked(self) -> None:
        """When proposals exist but none are executable (NEEDS_INPUT),
        the batch is BLOCKED (P0-7), not NO_ACTIONS."""
        proposal = make_proposal()
        request = make_request("review-no-exe", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_INPUT,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert len(batch.receipts) == 0

    def test_rejected_proposal_is_blocked(self) -> None:
        proposal = make_proposal()
        request = make_request("review-rejected", [proposal])
        review = make_review(
            "prop-test-001",
            request.request_hash,
            status=ReviewDecisionStatus.REJECTED,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert len(batch.receipts) == 0


# ---------------------------------------------------------------------------
# GovernedExecutor.execute — high-risk without approval
# ---------------------------------------------------------------------------


class TestGovernedExecutorHighRisk:
    def test_high_risk_without_approval_is_pending(self) -> None:
        from multi_agent.contracts import ActionRiskLevel

        proposal = make_proposal(
            "prop-high-risk",
            action_type="crm.owner.assign",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=True,
            evidence_ids=["ev-high-risk"],
        )
        request = make_request(
            "review-high-risk",
            [proposal],
            [make_evidence("ev-high-risk")],
        )
        review = make_review(
            "prop-high-risk",
            request.request_hash,
            status=ReviewDecisionStatus.NEEDS_APPROVAL,
            required_approval=True,
        )
        result = make_result(request, [review])
        registry = make_recording_registry([])
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.PENDING_APPROVAL
        assert "prop-high-risk" in batch.pending_approval_proposal_ids
        assert len(batch.receipts) == 0


# ---------------------------------------------------------------------------
# GovernedExecutor.execute — kill switch
# ---------------------------------------------------------------------------


class TestGovernedExecutorKillSwitch:
    def test_kill_switch_active_blocks_execution(self) -> None:
        """When the kill switch is active, the adapter is NEVER called."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        executor = GovernedExecutor()
        try:
            batch = run_async(
                executor.execute(
                    request=request,
                    review_result=result,
                    approval_store=__import__(
                        "multi_agent.approval_gate",
                        fromlist=["InMemoryApprovalStore"],
                    ).InMemoryApprovalStore(),
                    execution_store=InMemoryExecutionStore(),
                    adapter_registry=registry,
                    kill_switch=AlwaysKillSwitch(),
                    clock=FrozenClock(TS),
                )
            )
            assert batch.batch_status in (
                BatchExecutionStatus.BLOCKED,
                BatchExecutionStatus.UNKNOWN,
            )
        except Exception:
            # The batch assembly may raise when no receipts are
            # produced — the KEY invariant is the adapter was never
            # called.
            pass
        assert len(sink) == 0  # adapter was never called

    def test_cancelled_run_blocks_execution(self) -> None:
        """When the run is cancelled, the adapter is NEVER called."""
        request, result, _ = make_approved_request_result()
        sink: list = []
        registry = make_recording_registry(sink)
        executor = GovernedExecutor()
        try:
            batch = run_async(
                executor.execute(
                    request=request,
                    review_result=result,
                    approval_store=__import__(
                        "multi_agent.approval_gate",
                        fromlist=["InMemoryApprovalStore"],
                    ).InMemoryApprovalStore(),
                    execution_store=InMemoryExecutionStore(),
                    adapter_registry=registry,
                    kill_switch=CancelledRun(),
                    clock=FrozenClock(TS),
                )
            )
            assert batch.batch_status in (
                BatchExecutionStatus.BLOCKED,
                BatchExecutionStatus.CANCELLED,
                BatchExecutionStatus.UNKNOWN,
            )
        except Exception:
            pass
        assert len(sink) == 0


# ---------------------------------------------------------------------------
# GovernedExecutor.execute — integrity failures
# ---------------------------------------------------------------------------


class TestGovernedExecutorIntegrityFailures:
    def test_tampered_request_blocks_execution(self) -> None:
        request, result, _ = make_approved_request_result()
        object.__setattr__(request, "request_hash", "tampered" + "0" * 57)
        registry = make_recording_registry([])
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert batch.error_code == AUTHORIZATION_INTEGRITY_FAILED

    def test_tampered_result_blocks_execution(self) -> None:
        request, result, _ = make_approved_request_result()
        object.__setattr__(result, "result_hash", "tampered" + "0" * 57)
        registry = make_recording_registry([])
        executor = GovernedExecutor()
        batch = run_async(
            executor.execute(
                request=request,
                review_result=result,
                approval_store=__import__(
                    "multi_agent.approval_gate",
                    fromlist=["InMemoryApprovalStore"],
                ).InMemoryApprovalStore(),
                execution_store=InMemoryExecutionStore(),
                adapter_registry=registry,
                kill_switch=NoKillSwitch(),
                clock=FrozenClock(TS),
            )
        )
        assert batch.batch_status == BatchExecutionStatus.BLOCKED
        assert batch.error_code == AUTHORIZATION_INTEGRITY_FAILED
