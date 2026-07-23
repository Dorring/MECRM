"""Phase 5A Review Evaluation tests.

Covers (Phase 5A Section 13 & 16):

* :func:`build_review_request` — Phase 4 SupervisorRunResult → ReviewRequest
  * defensive deep copy (mutating the result does not affect the request)
  * identity preservation (run_id, plan_hash, registry_version, tenant_id)
  * does not modify the input SupervisorRunResult
  * rejects empty SupervisorRunResult
* :func:`build_review_fixtures` — 12 deterministic fixtures
  * every fixture is constructible
  * expected_blocked / expected_conflicted sets are non-empty where intended
  * fixture hashes are stable across runs
* :func:`compute_review_metrics` — 8 metrics over the fixture set
  * all 12 fixtures run without exception
  * deterministic_replay_rate == 1.0
  * false_approval_rate == 0.0
  * false_rejection_rate == 0.0
  * invalid_proposal_block_rate >= 0.99
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    AgentResult,
    Evidence,
    EvidenceType,
    ExecutionUsage,
    TokenUsage,
)
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ExecutionTraceEvent,
    ResultOriginSnapshot,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskExecutionRecord,
)
from multi_agent.state import MergedState
from multi_agent.review_contracts import (
    PolicyContext,
    PolicyDecision,
    PolicyRule,
    REVIEWER_VERSION,
)
from multi_agent.review_errors import InvalidReviewRequestError
from multi_agent.review_evaluation import (
    ReviewMetrics,
    build_review_fixtures,
    build_review_request,
    compute_review_metrics,
    default_policy_context,
)
from multi_agent.evidence_review import compute_review_evidence_hash
from multi_agent.policy import DeterministicPolicyEvaluator
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers — build a minimal but realistic SupervisorRunResult
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-001",
    *,
    tenant_id: str = "tenant-test",
    source_agent: str = "agent_test",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.CUSTOMER,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash="a" * 64,
        created_at=_TS,
    )


def _make_proposal(
    proposal_id: str = "prop-001",
    *,
    tenant_id: str = "tenant-test",
    created_by_agent: str = "agent_test",
    action_type: str = "report.generate",
    evidence_ids: list[str] | None = None,
    idempotency_key: str = "idem-key-0001",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity="report",
        target_id=None,
        payload={},
        priority="medium",
        risk_level=ActionRiskLevel.LOW,
        evidence_ids=evidence_ids or [],
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _make_agent_result(
    *,
    result_id: str = "r-001",
    task_id: str = "task-001",
    agent_id: str = "agent_test",
    tenant_id: str = "tenant-test",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
) -> AgentResult:
    return AgentResult(
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id,
        status="completed",
        confidence=1.0,
        duration_ms=0.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        errors=[],
        token_usage=TokenUsage(),
        provider_metadata=None,
        tool_calls=[],
        completed_at=_TS,
    )


def _make_supervisor_result(
    *,
    run_id: str = "run-001",
    tenant_id: str = "tenant-test",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
    agent_id: str = "agent_test",
    task_id: str = "task-001",
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
) -> SupervisorRunResult:
    proposals = proposals or [_make_proposal(tenant_id=tenant_id)]
    evidence = evidence or [_make_evidence(tenant_id=tenant_id)]
    # R2.1 P0-3: every envelope's task_id MUST have a matching
    # capability_binding.  Default to a READ-only capability so the
    # Reviewer's exact-task authority check passes for report-style
    # proposals.  Tests that need a different authority pass their
    # own capability_bindings.
    if capability_bindings is None:
        default_cap = AgentCapability(
            agent_id=agent_id,
            version="1.0.0",
            description="default test capability",
            domains=frozenset({"test"}),
            supported_tasks=frozenset({"test_task"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=300_000,
            max_retries=0,
            estimated_cost_class="low",
        )
        capability_bindings = [
            ExecutionCapabilitySnapshot(
                task_id=task_id,
                agent_id=agent_id,
                agent_version="1.0.0",
                capability=default_cap,
                binding_hash=stable_hash(
                    {
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "agent_version": "1.0.0",
                        "capability": default_cap.model_dump(mode="python"),
                    }
                ),
            )
        ]
    result = _make_agent_result(
        proposals=proposals,
        evidence=evidence,
        task_id=task_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )
    merged = MergedState(
        results=[result],
        merged_evidence=evidence,
        merged_proposals=proposals,
        conflicts=[],
        merged_at=_TS,
    )
    # R2.1 P0-4: SupervisorRunResult MUST carry run_identity and
    # result_origins.  The Phase 5A Adapter copies them verbatim and
    # refuses to regenerate them.
    identity_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "plan_hash": "plan-hash-test",
            "registry_version": "registry-v1",
        }
    )
    run_identity = ExecutionRunIdentity(
        run_id=run_id,
        tenant_id=tenant_id,
        plan_hash="plan-hash-test",
        registry_version="registry-v1",
        identity_hash=identity_hash,
    )
    # Build a ResultOriginSnapshot whose origin_hash matches the
    # Supervisor's canonical computation (run_id + tenant_id + result
    # identity + proposal_hashes + evidence_hashes).
    proposal_hashes = tuple((p.proposal_id, p.proposal_hash) for p in proposals)
    evidence_hashes = tuple(
        (ev.evidence_id, compute_review_evidence_hash(ev)) for ev in evidence
    )
    origin_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "result_id": result.result_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": "1.0.0",
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    result_origin = ResultOriginSnapshot(
        run_id=run_id,
        tenant_id=tenant_id,
        result_id=result.result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )
    return SupervisorRunResult(
        run_id=run_id,
        plan_hash="plan-hash-test",
        registry_version="registry-v1",
        status=SupervisorRunStatus.COMPLETED,
        task_records=[
            TaskExecutionRecord(
                task_id=task_id,
                agent_id=agent_id,
                status="completed",
                attempts=[],
                result=result,
                skip_reason=None,
            )
        ],
        merged_state=merged,
        usage=ExecutionUsage(),
        trace=[
            ExecutionTraceEvent(
                sequence=0,
                event_type="run_started",
                run_id=run_id,
                occurred_at=_TS,
            )
        ],
        capability_bindings=capability_bindings,
        run_identity=run_identity,
        result_origins=(result_origin,),
        started_at=_TS,
        completed_at=_TS,
        duration_ms=0,
    )


# ===========================================================================
# default_policy_context
# ===========================================================================


class TestDefaultPolicyContext:
    """Verify the default PolicyContext used by fixtures and the adapter."""

    def test_default_policy_context_is_deterministic(self):
        ctx_a = default_policy_context()
        ctx_b = default_policy_context()
        assert ctx_a == ctx_b
        assert ctx_a.policy_version == "ma-05a-default"
        # R2 P0-6: rules is a tuple[PolicyRule, ...]
        assert ctx_a.rules == ()
        # R2.1 P0-1: tenant_overrides is deep-frozen — empty dict {}
        # freezes to empty tuple () (dict → tuple of (k, v) tuples).
        assert ctx_a.tenant_overrides == ()

    def test_default_policy_context_is_frozen(self):
        ctx = default_policy_context()
        with pytest.raises(ValidationError):
            ctx.policy_version = "tampered"  # type: ignore[misc]


# ===========================================================================
# build_review_request — Phase 4 Adapter
# ===========================================================================


class TestBuildReviewRequest:
    """Verify the Phase 4 → Phase 5A adapter contract."""

    def test_preserves_identity_fields(self):
        result = _make_supervisor_result(
            run_id="run-identity-001",
            tenant_id="tenant-identity",
        )
        request = build_review_request(result, review_id="rev-001")

        assert request.review_id == "rev-001"
        assert request.run_id == "run-identity-001"
        assert request.tenant_id == "tenant-identity"
        assert request.plan_hash == "plan-hash-test"
        assert request.registry_version == "registry-v1"
        assert request.reviewer_version == REVIEWER_VERSION

    def test_preserves_proposals_and_evidence(self):
        proposal = _make_proposal("prop-preserve-001")
        evidence = _make_evidence("ev-preserve-001")
        result = _make_supervisor_result(
            proposals=[proposal],
            evidence=[evidence],
        )
        request = build_review_request(result, review_id="rev-001")

        assert len(request.proposals) == 1
        assert request.proposals[0].proposal_id == "prop-preserve-001"
        assert len(request.evidence) == 1
        # R2 P0-3: evidence is wrapped in ReviewEvidenceSnapshot
        assert request.evidence[0].evidence.evidence_id == "ev-preserve-001"

    def test_carries_task_records_and_trace(self):
        result = _make_supervisor_result()
        request = build_review_request(result, review_id="rev-001")

        assert len(request.task_records) == 1
        assert request.task_records[0].task_id == "task-001"
        assert request.task_records[0].agent_id == "agent_test"
        assert request.task_records[0].status == "completed"
        assert len(request.trace) == 1
        assert request.trace[0].event_type == "run_started"

    def test_request_hash_is_computed(self):
        result = _make_supervisor_result()
        request = build_review_request(result, review_id="rev-001")

        assert request.request_hash != ""
        # verify_integrity does not raise
        request.verify_integrity()

    def test_defensive_deep_copy_proposals(self):
        """Mutating the SupervisorRunResult's merged_state after building
        the request MUST NOT affect the ReviewRequest.
        """
        result = _make_supervisor_result()
        request = build_review_request(result, review_id="rev-001")
        original_hash = request.request_hash

        # Mutate the original result's merged_state (this should not
        # propagate to the request because the adapter deep-copies).
        result.merged_state.merged_proposals.clear()
        result.merged_state.merged_evidence.clear()

        # The request hash is recomputed from the request, not the
        # mutated source — verify integrity still passes.
        assert request.request_hash == original_hash
        request.verify_integrity()

    def test_does_not_modify_input(self):
        result = _make_supervisor_result()
        original_proposal_count = len(result.merged_state.merged_proposals)
        original_evidence_count = len(result.merged_state.merged_evidence)

        _ = build_review_request(result, review_id="rev-001")

        # The source SupervisorRunResult is unchanged.
        assert len(result.merged_state.merged_proposals) == original_proposal_count
        assert len(result.merged_state.merged_evidence) == original_evidence_count

    def test_carries_capability_bindings(self):
        """R1: build_review_request reads capability_bindings from the
        SupervisorRunResult (no longer accepts a capability_snapshots
        parameter).  The bindings are preserved on the ReviewRequest.
        """
        cap = AgentCapability(
            agent_id="agent_test",
            version="1.0.0",
            description="t",
            domains=frozenset({"d"}),
            supported_tasks=frozenset({"t"}),
            allowed_tools=frozenset({"crm_reader.get_customers"}),
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=300_000,
            max_retries=0,
            estimated_cost_class="low",
        )
        binding = ExecutionCapabilitySnapshot(
            task_id="task-001",
            agent_id="agent_test",
            agent_version="1.0.0",
            capability=cap,
            binding_hash=stable_hash(
                {
                    "task_id": "task-001",
                    "agent_id": "agent_test",
                    "agent_version": "1.0.0",
                    "capability": cap.model_dump(mode="python"),
                }
            ),
        )

        result = _make_supervisor_result(capability_bindings=[binding])
        request = build_review_request(result, review_id="rev-001")

        assert len(request.capability_bindings) == 1
        assert request.capability_bindings[0].agent_id == "agent_test"

    def test_accepts_custom_policy_context(self):
        # R2 P0-6: rules are strictly-typed PolicyRule, not raw dicts.
        ctx = PolicyContext(
            policy_version="custom-001",
            rules=(
                PolicyRule(
                    rule_id="r1",
                    rule_version="custom-001",
                    priority=50,
                    effect=PolicyDecision.ALLOWED,
                    action_type="report.generate",
                ),
            ),
        )
        result = _make_supervisor_result()
        request = build_review_request(
            result,
            review_id="rev-001",
            policy_context=ctx,
        )
        assert request.policy_context.policy_version == "custom-001"

    def test_rejects_empty_supervisor_result(self):
        """An empty SupervisorRunResult (no proposals/evidence/results)
        cannot derive a tenant_id — must raise InvalidReviewRequestError.
        """
        empty_merged = MergedState(
            results=[],
            merged_evidence=[],
            merged_proposals=[],
            conflicts=[],
            merged_at=_TS,
        )
        empty_result = SupervisorRunResult(
            run_id="run-empty",
            plan_hash="plan-empty",
            registry_version="registry-empty",
            status=SupervisorRunStatus.COMPLETED,
            task_records=[],
            merged_state=empty_merged,
            usage=ExecutionUsage(),
            trace=[],
            started_at=_TS,
            completed_at=_TS,
            duration_ms=0,
        )
        with pytest.raises(InvalidReviewRequestError):
            build_review_request(empty_result, review_id="rev-001")

    def test_request_hash_stable_across_invocations(self):
        """Two adapters over the same input MUST produce the same hash."""
        result_a = _make_supervisor_result()
        result_b = _make_supervisor_result()

        request_a = build_review_request(result_a, review_id="rev-001")
        request_b = build_review_request(result_b, review_id="rev-001")

        assert request_a.request_hash == request_b.request_hash


# ===========================================================================
# build_review_fixtures
# ===========================================================================


class TestBuildReviewFixtures:
    """Verify the 12 deterministic fixtures defined in Section 16."""

    def test_returns_twelve_fixtures(self):
        fixtures = build_review_fixtures()
        assert len(fixtures) == 12

    def test_fixture_names_unique(self):
        fixtures = build_review_fixtures()
        names = {f.name for f in fixtures}
        assert len(names) == 12

    def test_every_fixture_request_is_valid(self):
        fixtures = build_review_fixtures()
        for fixture in fixtures:
            # Each fixture's ReviewRequest must pass its own integrity
            # check (the model_validator computes the hash on construction).
            fixture.request.verify_integrity()

    def test_blocked_fixtures_have_expected_ids(self):
        """Fixtures 2,4,5,6,7,8,9,10,11 must declare at least one blocked id."""
        fixtures = build_review_fixtures()
        blocking_fixtures = {
            "missing_evidence",
            "foreign_tenant_evidence",
            "authority_violation",
            "unknown_action",
            "short_idempotency_key",
            "high_risk_needs_approval",
            "policy_deny",
            "exact_duplicate",
            "same_resource_conflict",
        }
        for fixture in fixtures:
            if fixture.name in blocking_fixtures:
                assert len(fixture.expected_blocked_proposal_ids) > 0, (
                    f"Fixture {fixture.name!r} must declare expected_blocked_proposal_ids"
                )

    def test_clean_fixtures_have_no_blocked_ids(self):
        """Fixtures 1, 3, 12 must declare zero blocked ids."""
        fixtures = build_review_fixtures()
        clean_fixtures = {
            "valid_low_risk",
            "dangling_evidence",
            "multiple_independent_valid",
        }
        for fixture in fixtures:
            if fixture.name in clean_fixtures:
                assert len(fixture.expected_blocked_proposal_ids) == 0, (
                    f"Fixture {fixture.name!r} must declare zero blocked ids"
                )

    def test_conflict_fixtures_declare_conflicted_ids(self):
        fixtures = build_review_fixtures()
        for fixture in fixtures:
            if fixture.name == "same_resource_conflict":
                assert len(fixture.expected_conflicted_proposal_ids) > 0
            elif fixture.name == "exact_duplicate":
                # R1: exact duplicates are DEDUPLICATED, not CONFLICT
                assert len(fixture.expected_conflicted_proposal_ids) == 0

    def test_fixture_hashes_stable_across_calls(self):
        """Calling build_review_fixtures twice yields identical hashes."""
        fixtures_a = build_review_fixtures()
        fixtures_b = build_review_fixtures()
        hashes_a = {f.name: f.request.request_hash for f in fixtures_a}
        hashes_b = {f.name: f.request.request_hash for f in fixtures_b}
        assert hashes_a == hashes_b


# ===========================================================================
# compute_review_metrics
# ===========================================================================


class TestComputeReviewMetrics:
    """Verify the metrics computation over the fixture set.

    Note: :func:`compute_review_metrics` is a synchronous function
    that internally uses ``asyncio.run`` — these tests are NOT
    async to avoid nesting event loops.
    """

    def test_metrics_run_without_exception(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert isinstance(metrics, ReviewMetrics)

    def test_metrics_totals(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        # Every fixture counts as one review and one replay.
        assert metrics.total_reviews == 12
        assert metrics.total_proposals >= 12

    def test_deterministic_replay_rate_is_one(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        # Same input MUST produce same hash on the second run.
        assert metrics.deterministic_replay_rate == 1.0

    def test_false_approval_rate_is_zero(self):
        """No fixture should approve a Proposal expected to be blocked."""
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert metrics.false_approval_rate == 0.0

    def test_false_rejection_rate_is_zero(self):
        """No fixture should block a Proposal expected to be approved."""
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert metrics.false_rejection_rate == 0.0

    def test_invalid_proposal_block_rate_above_threshold(self):
        """The reviewer should block >= 99% of the Proposals that the
        fixtures mark as expected-to-block."""
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert metrics.invalid_proposal_block_rate >= 0.99

    def test_evidence_error_detection_rate_above_threshold(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        # missing_evidence + foreign_tenant_evidence
        assert metrics.evidence_error_detection_rate >= 0.99

    def test_authority_violation_detection_rate(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert metrics.authority_violation_detection_rate >= 0.99

    def test_conflict_detection_rate(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        assert metrics.conflict_detection_rate >= 0.99

    def test_review_latency_is_finite(self):
        fixtures = build_review_fixtures()
        metrics = compute_review_metrics(fixtures)
        # Deterministic evaluator is in-process — must be very fast.
        assert metrics.review_latency_ms >= 0.0
        assert metrics.review_latency_ms < 5_000.0  # generous bound for CI

    def test_accepts_custom_reviewer_and_evaluator(self):
        """compute_review_metrics should accept injected dependencies."""
        fixtures = build_review_fixtures()
        reviewer = ProposalReviewer()
        evaluator = DeterministicPolicyEvaluator()
        metrics = compute_review_metrics(
            fixtures,
            reviewer=reviewer,
            policy_evaluator=evaluator,
        )
        assert metrics.deterministic_replay_rate == 1.0
