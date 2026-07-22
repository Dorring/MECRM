"""Phase 5A LangGraph Thin Adapter.

A 4-node LangGraph that wraps :class:`ProposalReviewer` so callers
that already use LangGraph can compose the Reviewer uniformly.

Nodes (Phase 5A Section 14):

* ``validate_request`` — run :meth:`ReviewRequest.verify_integrity`.
* ``review_proposals`` — hand off to :meth:`ProposalReviewer.review`.
* ``resolve_conflicts`` — no-op pass-through (conflict resolution is
  already inside :class:`ProposalReviewer`; this node exists for
  trace clarity and future extension points).
* ``finalize_review`` — run :meth:`ReviewBatchResult.verify_integrity`.

Important: the graph **does not** re-implement any Policy, Conflict,
or Hash algorithm.  Every node delegates to :class:`ProposalReviewer`.
A fake Reviewer can be substituted in tests to verify the graph
routes correctly without performing real work.

The graph is **not** registered in any application startup.  Phase 5B
will wire it into the orchestrator.

Graph output is byte-for-byte identical to direct
:meth:`ProposalReviewer.review` output — verified by
:func:`test_review_graph.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, StateGraph

from multi_agent.policy import PolicyEvaluator
from multi_agent.review_contracts import (
    ReviewBatchResult,
    ReviewRequest,
)
from multi_agent.review_errors import (
    InvalidReviewRequestError,
    InvalidReviewResultError,
    ReviewError,
)
from multi_agent.reviewer import ProposalReviewer


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


@dataclass
class ReviewGraphState:
    """Mutable state passed between graph nodes.

    The graph intentionally reuses the Reviewer's return value rather
    than mirroring fields — the state just carries inputs and the
    final result.
    """

    request: ReviewRequest
    policy_evaluator: PolicyEvaluator
    reviewer: ProposalReviewer | None = None
    result: ReviewBatchResult | None = None
    error: Exception | None = None


# ---------------------------------------------------------------------------
# Fake reviewer for tests
# ---------------------------------------------------------------------------


@dataclass
class FakeProposalReviewer:
    """Test double that records calls and returns a preset result.

    Tests substitute this for :class:`ProposalReviewer` to verify
    graph routing without performing real review work.
    """

    result: ReviewBatchResult | None = None
    error: Exception | None = None
    calls: list[ReviewRequest] = field(default_factory=list)

    async def review(
        self,
        request: ReviewRequest,
        *,
        policy_evaluator: PolicyEvaluator,
    ) -> ReviewBatchResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None, "FakeProposalReviewer.result must be set"
        return self.result


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


# Type alias for any object that quacks like ProposalReviewer.review.
# We accept the fake too, so tests do not need to monkeypatch.
_ReviewerLike = Any  # ProposalReviewer | FakeProposalReviewer


def build_review_graph(reviewer: _ReviewerLike | None = None):
    """Build a LangGraph that wraps *reviewer* (or a fresh
    :class:`ProposalReviewer` if None).

    The returned graph is compiled but not started.  Callers invoke
    it via ``graph.ainvoke(initial_state)``.

    The graph is **not** registered in any application startup —
    Phase 5B will wire it into the orchestrator.
    """

    async def validate_request(state: ReviewGraphState) -> dict[str, Any]:
        # The Reviewer itself re-verifies integrity, but this node
        # exists so the trace has a clear boundary and tests can
        # assert the node was visited.  We do not duplicate
        # validation here — we only surface integrity errors as
        # graph-state errors so the graph can route to END cleanly.
        if state.request is None:
            state.error = InvalidReviewRequestError(
                "ReviewGraphState.request must not be None"
            )
            return {"error": state.error}
        try:
            state.request.verify_integrity()
        except ReviewError as e:
            state.error = e
            return {"error": e}
        return {}

    async def review_proposals(state: ReviewGraphState) -> dict[str, Any]:
        if state.error is not None:
            return {"error": state.error}
        # Use the per-invocation state.reviewer if set, else fall back
        # to the graph-level reviewer captured at build time, else
        # create a fresh ProposalReviewer.
        r = state.reviewer or reviewer or ProposalReviewer()
        try:
            result = await r.review(
                state.request,
                policy_evaluator=state.policy_evaluator,
            )
        except ReviewError as e:
            state.error = e
            return {"error": e}
        state.result = result
        return {"result": result}

    async def resolve_conflicts(state: ReviewGraphState) -> dict[str, Any]:
        # No-op pass-through.  Conflict resolution is already inside
        # :class:`ProposalReviewer` (see :mod:`multi_agent.conflict_resolution`).
        # This node exists for trace clarity and as a future extension
        # point (e.g. a human-in-the-loop escalation hook in Phase 5B).
        if state.error is not None:
            return {"error": state.error}
        # The result is already final — no additional conflict work.
        return {}

    async def finalize_review(state: ReviewGraphState) -> dict[str, Any]:
        if state.error is not None:
            return {"error": state.error}
        if state.result is None:
            state.error = InvalidReviewResultError(
                "ReviewGraphState.result is None at finalize_review"
            )
            return {"error": state.error}
        try:
            state.result.verify_integrity()
        except ReviewError as e:
            state.error = e
            return {"error": e}
        return {"result": state.result}

    def _should_continue_after_validate(state: ReviewGraphState) -> str:
        if state.error is not None:
            return END
        return "review_proposals"

    def _should_continue_after_review(state: ReviewGraphState) -> str:
        if state.error is not None:
            return END
        return "resolve_conflicts"

    def _should_continue_after_resolve(state: ReviewGraphState) -> str:
        if state.error is not None:
            return END
        return "finalize_review"

    graph = StateGraph(ReviewGraphState)
    graph.add_node("validate_request", validate_request)
    graph.add_node("review_proposals", review_proposals)
    graph.add_node("resolve_conflicts", resolve_conflicts)
    graph.add_node("finalize_review", finalize_review)

    graph.set_entry_point("validate_request")
    graph.add_conditional_edges(
        "validate_request",
        _should_continue_after_validate,
        {
            "review_proposals": "review_proposals",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "review_proposals",
        _should_continue_after_review,
        {
            "resolve_conflicts": "resolve_conflicts",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "resolve_conflicts",
        _should_continue_after_resolve,
        {
            "finalize_review": "finalize_review",
            END: END,
        },
    )
    graph.add_edge("finalize_review", END)

    return graph.compile()


__all__ = [
    "FakeProposalReviewer",
    "ReviewGraphState",
    "build_review_graph",
]
