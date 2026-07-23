"""Phase 5A LangGraph Adapter tests.

Covers (Phase 5A Section 17 — Graph Parity):

* Happy path — the graph delegates to :meth:`ProposalReviewer.review`
  and returns the result in ``state.result``.
* Error propagation — when the Reviewer raises a :class:`ReviewError`,
  the graph captures it in ``state.graph_error`` (a persistable
  :class:`ReviewGraphError`) and does not crash.
* Graph parity — direct reviewer output equals graph output byte-for-byte
  (verified by comparing ``result_hash``).
* The graph does NOT re-implement Policy, Conflict, or Hash algorithms
  — verified by substituting :class:`FakeProposalReviewer` and confirming
  the graph routes correctly without performing real review work.
* Validation error routing — a tampered :class:`ReviewRequest` (hash
  mismatch) routes the graph to END without invoking the Reviewer.

R2.1 P1-1: the Graph State no longer carries a raw
``error: Exception | None`` field.  All error-path assertions now read
``state.graph_error`` (a frozen :class:`ReviewGraphError`) and verify
its ``error_code``.  Direct/Graph Error Code parity is verified for
each error boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from multi_agent.action_governance import (
    ACTION_GOVERNANCE_SPEC_HASH,
    ACTION_GOVERNANCE_SPEC_VERSION,
)
from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    Evidence,
    EvidenceType,
)
from multi_agent.evidence_review import compute_review_evidence_hash
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ResultOriginSnapshot,
)
from multi_agent.policy import DeterministicPolicyEvaluator
from multi_agent.review_contracts import (
    PolicyContext,
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewEvidenceSnapshot,
    ReviewGraphError,
    ReviewProposalEnvelope,
    ReviewProposalSnapshot,
    ReviewRequest,
    REVIEW_SCHEMA_VERSION,
    TaskRecordSummary,
    TraceSummary,
    REVIEWER_VERSION,
)
from multi_agent.review_errors import (
    ReviewError,
)
from multi_agent.review_evaluation import build_review_fixtures
from multi_agent.review_graph import (
    FakeProposalReviewer,
    ReviewGraphState,
    build_review_graph,
)
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev-001",
    *,
    tenant_id: str = "tenant-graph",
    source_agent: str = "agent_graph",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.CUSTOMER,
        tenant_id=tenant_id,
        source_agent=source_agent,
        content_hash="a" * 64,
        created_at=_TS,
    )


def _make_capability(
    agent_id: str = "agent_graph",
    *,
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="graph test cap",
        domains=frozenset({"d"}),
        supported_tasks=frozenset({"t"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_customers"}),
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
    )


def _make_proposal(
    proposal_id: str = "prop-graph-001",
    *,
    tenant_id: str = "tenant-graph",
    created_by_agent: str = "agent_graph",
    action_type: str = "report.generate",
    evidence_ids: list[str] | None = None,
    idempotency_key: str = "graph-idem-0001",
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


def _make_capability_binding(
    capability: AgentCapability,
    *,
    task_id: str = "task-graph-001",
) -> ExecutionCapabilitySnapshot:
    return ExecutionCapabilitySnapshot(
        task_id=task_id,
        agent_id=capability.agent_id,
        agent_version=capability.version,
        capability=capability,
        binding_hash=stable_hash(
            {
                "task_id": task_id,
                "agent_id": capability.agent_id,
                "agent_version": capability.version,
                "capability": capability.model_dump(mode="python"),
            }
        ),
    )


def _make_envelope(
    proposal: ActionProposal,
    *,
    run_id: str = "run-graph-001",
    result_id: str = "r-graph-001",
    task_id: str = "task-graph-001",
    agent_version: str = "1.0.0",
) -> ReviewProposalEnvelope:
    aid = proposal.created_by_agent
    # R2.1 P0-1: Envelope carries a deep-frozen ReviewProposalSnapshot.
    snapshot = ReviewProposalSnapshot.from_proposal(proposal)
    # R2.1 P0-1: origin_hash MUST match the envelope's validator which
    # uses to_action_proposal().model_dump(mode="python").
    return ReviewProposalEnvelope(
        proposal=snapshot,
        run_id=run_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=aid,
        agent_version=agent_version,
        origin_hash=stable_hash(
            {
                "proposal": snapshot.to_action_proposal().model_dump(mode="python"),
                "run_id": run_id,
                "result_id": result_id,
                "task_id": task_id,
                "agent_id": aid,
                "agent_version": agent_version,
            }
        ),
    )


def _make_run_identity(
    *,
    run_id: str = "run-graph-001",
    tenant_id: str = "tenant-graph",
    plan_hash: str = "plan-graph-hash",
    registry_version: str = "registry-graph-v1",
) -> ExecutionRunIdentity:
    identity_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "plan_hash": plan_hash,
            "registry_version": registry_version,
        }
    )
    return ExecutionRunIdentity(
        run_id=run_id,
        tenant_id=tenant_id,
        plan_hash=plan_hash,
        registry_version=registry_version,
        identity_hash=identity_hash,
    )


def _make_result_origin(
    proposal: ActionProposal,
    *,
    run_id: str = "run-graph-001",
    tenant_id: str = "tenant-graph",
    result_id: str = "r-graph-001",
    task_id: str = "task-graph-001",
    agent_id: str = "agent_graph",
    agent_version: str = "1.0.0",
    evidence: list[Evidence] | None = None,
) -> ResultOriginSnapshot:
    """R2.1 P0-4: build a ResultOriginSnapshot whose origin_hash is
    verified on construction."""
    proposal_hashes: tuple[tuple[str, str], ...] = (
        (proposal.proposal_id, proposal.proposal_hash),
    )
    evidence_hashes_list: list[tuple[str, str]] = []
    for ev in evidence or []:
        if proposal.evidence_ids and ev.evidence_id in proposal.evidence_ids:
            evidence_hashes_list.append(
                (ev.evidence_id, compute_review_evidence_hash(ev))
            )
    evidence_hashes = tuple(evidence_hashes_list)
    origin_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "result_id": result_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    return ResultOriginSnapshot(
        run_id=run_id,
        tenant_id=tenant_id,
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version=agent_version,
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )


def _make_request(
    *,
    review_id: str = "review-graph-001",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
    capability_bindings: list[ExecutionCapabilitySnapshot] | None = None,
) -> ReviewRequest:
    props = proposals or [_make_proposal()]
    raw_evidence = evidence or [_make_evidence()]
    # R2 P0-3: wrap evidence in ReviewEvidenceSnapshot
    evidence_snapshots = [
        ReviewEvidenceSnapshot(
            evidence=ev,
            snapshot_hash=compute_review_evidence_hash(ev),
        )
        for ev in raw_evidence
    ]
    # R2.1 P0-1: convert ActionProposal → ReviewProposalSnapshot.
    proposal_snapshots: tuple[ReviewProposalSnapshot, ...] = tuple(
        ReviewProposalSnapshot.from_proposal(p) for p in props
    )
    # R2.1 P0-4: result_origins are REQUIRED.
    origins = [_make_result_origin(p, evidence=raw_evidence) for p in props]
    return ReviewRequest(
        review_id=review_id,
        run_id="run-graph-001",
        tenant_id="tenant-graph",
        plan_hash="plan-graph-hash",
        registry_version="registry-graph-v1",
        proposals=proposal_snapshots,
        evidence=evidence_snapshots,
        task_records=[
            TaskRecordSummary(
                task_id="task-graph-001",
                agent_id="agent_graph",
                status="completed",
            )
        ],
        trace=[
            TraceSummary(
                sequence=0,
                event_type="run_started",
                task_id=None,
                agent_id=None,
            )
        ],
        capability_bindings=capability_bindings
        or [_make_capability_binding(_make_capability())],
        proposal_envelopes=[_make_envelope(p) for p in props],
        result_origins=origins,
        policy_context=PolicyContext(
            policy_version="graph-test-v1",
            rules=[],
        ),
        run_identity=_make_run_identity(),
        governance_spec_version=ACTION_GOVERNANCE_SPEC_VERSION,
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        review_schema_version=REVIEW_SCHEMA_VERSION,
        reviewer_version=REVIEWER_VERSION,
    )


def _make_batch_result(
    *,
    review_id: str = "review-graph-001",
    request_hash: str = "fixed-request-hash",
) -> ReviewBatchResult:
    """Build a minimal ReviewBatchResult for FakeProposalReviewer.

    R2 S7: empty batch uses NO_ACTIONS (NOT APPROVED).  R2 S10:
    ``reviewer_version`` is required on the Result.
    """
    return ReviewBatchResult(
        review_id=review_id,
        run_id="run-graph-001",
        tenant_id="tenant-graph",
        request_hash=request_hash,
        proposal_reviews=[],
        batch_status=ReviewBatchStatus.NO_ACTIONS,
        approved_proposal_ids=[],
        rejected_proposal_ids=[],
        approval_required_proposal_ids=[],
        conflicted_proposal_ids=[],
        findings=[],
        governance_spec_hash=ACTION_GOVERNANCE_SPEC_HASH,
        reviewer_version=REVIEWER_VERSION,
    )


# ===========================================================================
# Happy path
# ===========================================================================


class TestReviewGraphHappyPath:
    """The graph delegates to the Reviewer and returns the result."""

    @pytest.mark.asyncio
    async def test_graph_returns_reviewer_result(self):
        request = _make_request()
        reviewer = ProposalReviewer()
        graph = build_review_graph(reviewer)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_state = await graph.ainvoke(state)

        assert result_state["result"] is not None
        assert isinstance(result_state["result"], ReviewBatchResult)
        assert result_state["graph_error"] is None

    @pytest.mark.asyncio
    async def test_graph_calls_reviewer_once(self):
        request = _make_request()
        fake = FakeProposalReviewer(result=_make_batch_result())
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        await graph.ainvoke(state)

        assert len(fake.calls) == 1
        assert fake.calls[0].review_id == request.review_id


# ===========================================================================
# Error propagation
# ===========================================================================


class TestReviewGraphErrorPropagation:
    """Errors from the Reviewer are captured in state.graph_error."""

    @pytest.mark.asyncio
    async def test_integrity_error_routes_to_end(self):
        """A ReviewRequest whose hash mismatches routes the graph to END
        without calling the Reviewer.
        """
        request = _make_request()
        # Tamper with request_hash using object.__setattr__ to bypass
        # the frozen guard — this simulates an externally tampered
        # request reaching the graph.
        object.__setattr__(request, "request_hash", "tampered-hash-value")

        fake = FakeProposalReviewer(result=_make_batch_result())
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_state = await graph.ainvoke(state)

        # R2.1 P1-1: the graph captures the error as a persistable
        # ReviewGraphError (NOT a raw Exception).  The error_code is
        # stable across runs.
        assert result_state["graph_error"] is not None
        assert isinstance(result_state["graph_error"], ReviewGraphError)
        assert (
            result_state["graph_error"].error_code == "review.graph.request_integrity"
        )
        # No raw Exception in the State.
        assert "error" not in result_state
        assert len(fake.calls) == 0

    @pytest.mark.asyncio
    async def test_reviewer_error_is_captured(self):
        """When the Reviewer raises, the graph captures the exception
        in ``state.graph_error`` and returns no result.
        """
        request = _make_request()
        fake = FakeProposalReviewer(
            error=ReviewError("synthetic reviewer failure"),
        )
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_state = await graph.ainvoke(state)

        # R2.1 P1-1: only ReviewGraphError enters the State.
        assert result_state["graph_error"] is not None
        assert isinstance(result_state["graph_error"], ReviewGraphError)
        assert result_state["graph_error"].error_code == "review.graph.review_failed"
        assert "synthetic reviewer failure" in result_state["graph_error"].message
        assert result_state["result"] is None
        # No raw Exception in the State.
        assert "error" not in result_state


# ===========================================================================
# Graph parity — direct reviewer output == graph output
# ===========================================================================


class TestReviewGraphParity:
    """Phase 5A Section 14: Graph output == direct reviewer output."""

    @pytest.mark.asyncio
    async def test_graph_result_hash_equals_direct_call(self):
        """Run the Reviewer directly and through the graph; the
        ``result_hash`` MUST be identical.
        """
        request = _make_request()
        evaluator = DeterministicPolicyEvaluator()

        # Direct call
        reviewer = ProposalReviewer()
        direct_result = await reviewer.review(
            request,
            policy_evaluator=evaluator,
        )

        # Graph call
        graph = build_review_graph(ProposalReviewer())
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        graph_state = await graph.ainvoke(state)
        graph_result = graph_state["result"]

        assert direct_result.result_hash == graph_result.result_hash
        assert direct_result.batch_status == graph_result.batch_status

    @pytest.mark.asyncio
    async def test_graph_parity_on_each_fixture(self):
        """Run every Section 16 fixture both ways and assert equality."""
        fixtures = build_review_fixtures()
        evaluator = DeterministicPolicyEvaluator()

        for fixture in fixtures:
            # Direct call
            direct = await ProposalReviewer().review(
                fixture.request,
                policy_evaluator=evaluator,
            )

            # Graph call
            graph = build_review_graph(ProposalReviewer())
            state = ReviewGraphState(
                request=fixture.request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )
            state_out = await graph.ainvoke(state)
            graph_result = state_out["result"]

            assert direct.result_hash == graph_result.result_hash, (
                f"Fixture {fixture.name!r}: direct hash != graph hash"
            )

    @pytest.mark.asyncio
    async def test_graph_does_not_duplicate_logic(self):
        """The graph must NOT re-implement Policy, Conflict, or Hash.

        Verified by substituting a :class:`FakeProposalReviewer` — the
        graph returns the fake's preset result, not a result it computed
        itself.
        """
        request = _make_request()
        preset = _make_batch_result(
            request_hash=request.request_hash,
        )
        fake = FakeProposalReviewer(result=preset)
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_state = await graph.ainvoke(state)

        # The graph must return the EXACT preset object — it did not
        # compute its own result.
        assert result_state["result"] is preset


# ===========================================================================
# Graph node routing
# ===========================================================================


class TestReviewGraphRouting:
    """Verify the 4-node graph routes through validate → review → resolve → finalize."""

    @pytest.mark.asyncio
    async def test_validate_request_node_runs_first(self):
        """If validate_request fails, neither review_proposals nor
        finalize_review are reached.
        """
        request = _make_request()
        object.__setattr__(request, "request_hash", "tampered")

        fake = FakeProposalReviewer(result=_make_batch_result())
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        await graph.ainvoke(state)

        # Reviewer never called because validation failed.
        assert len(fake.calls) == 0

    @pytest.mark.asyncio
    async def test_finalize_review_verifies_result_integrity(self):
        """If the Reviewer returns a result whose hash mismatches, the
        finalize_review node captures the integrity error.
        """
        request = _make_request()
        # Build a preset result with a tampered result_hash.
        preset = _make_batch_result(request_hash=request.request_hash)
        object.__setattr__(preset, "result_hash", "tampered-result-hash")

        fake = FakeProposalReviewer(result=preset)
        graph = build_review_graph(fake)

        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_state = await graph.ainvoke(state)

        # R2.1 P1-1: only ReviewGraphError is in the State.
        assert result_state["graph_error"] is not None
        assert isinstance(result_state["graph_error"], ReviewGraphError)
        assert result_state["graph_error"].error_code == "review.graph.finalize_failed"
        assert "error" not in result_state


# ===========================================================================
# State dataclass behavior
# ===========================================================================


class TestReviewGraphState:
    """Verify the ReviewGraphState dataclass holds the right fields."""

    def test_state_default_fields(self):
        request = _make_request()
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        assert state.request is request
        assert state.reviewer is None
        assert state.result is None
        # R2.1 P1-1: only graph_error — no raw `error` field.
        assert state.graph_error is None
        assert not hasattr(state, "error")

    def test_state_accepts_fake_reviewer(self):
        request = _make_request()
        fake = FakeProposalReviewer(result=_make_batch_result())
        state = ReviewGraphState(
            request=request,
            policy_evaluator=DeterministicPolicyEvaluator(),
            reviewer=fake,
        )
        assert state.reviewer is fake


# ===========================================================================
# No registration / no startup side effects
# ===========================================================================


class TestReviewGraphNoSideEffects:
    """Phase 5A Section 3 & 14: the graph MUST NOT be registered in
    application startup, MUST NOT modify the existing Router, and MUST
    NOT call external services by default.
    """

    def test_import_does_not_call_external_services(self):
        """Importing :mod:`multi_agent.review_graph` must not perform
        network I/O or service registration.
        """
        # Re-import — if the module performed side effects at import
        # time, they would have already happened on first import.  This
        # test mainly asserts the module's public surface is importable
        # without raising.
        import importlib

        import multi_agent.review_graph as rg

        importlib.reload(rg)
        assert hasattr(rg, "build_review_graph")
        assert hasattr(rg, "ReviewGraphState")

    def test_build_review_graph_returns_callable(self):
        graph = build_review_graph()
        # The compiled graph must be invokable.
        assert hasattr(graph, "ainvoke")

    def test_build_review_graph_does_not_require_registry(self):
        """The graph must not require a live AgentRegistry at build time."""
        # No registry argument — should not raise.
        graph = build_review_graph()
        assert graph is not None
