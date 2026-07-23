"""Phase 5A Reviewer & Governance Decision Layer errors.

All errors inherit :class:`multi_agent.errors.MultiAgentError` so callers
can catch the base class to handle any multi-agent failure, consistent
with :mod:`multi_agent.execution_errors`.

Design rule (Phase 5A Section 15):

* Business review failures (rejected proposal, missing evidence, policy
  deny, conflict) become :class:`ReviewFinding` entries on the
  :class:`ProposalReview` — they are **expected outcomes** of the
  review pipeline, not exceptions.
* Only Contract / Integrity / Infrastructure / unrecoverable
  configuration errors raise exceptions from this module.

Phase 5A never executes an :class:`ActionProposal`; raising any of
these errors therefore never produces a business side-effect.
"""

from __future__ import annotations

from multi_agent.errors import MultiAgentError

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class ReviewError(MultiAgentError):
    """Base for all Phase 5A Reviewer errors."""


# ---------------------------------------------------------------------------
# Request / Result integrity
# ---------------------------------------------------------------------------


class ReviewIntegrityError(ReviewError):
    """A :class:`ReviewRequest` or :class:`ReviewBatchResult` failed
    integrity verification (request_hash / result_hash mismatch).

    This is an infrastructure-level failure: the same canonical input
    must always produce the same hash.  A mismatch indicates the
    request was mutated after construction or the result was tampered
    with before reaching the caller.
    """


class InvalidReviewRequestError(ReviewError):
    """The :class:`ReviewRequest` is structurally invalid (missing
    required fields, empty proposal list, mismatched tenant identity,
    unparseable policy_context).

    Raised before any per-proposal review runs so the caller knows the
    entire batch is unusable.
    """


class InvalidReviewResultError(ReviewError):
    """The :class:`ReviewBatchResult` produced by the Reviewer failed
    post-computation validation (per-proposal review missing, batch
    status inconsistent with member statuses, hash mismatch).

    Indicates a Reviewer implementation bug rather than bad input.
    """


# ---------------------------------------------------------------------------
# Proposal / Evidence identity
# ---------------------------------------------------------------------------


class InvalidProposalIdentityError(ReviewError):
    """A Proposal's identity fields do not match the
    :class:`ReviewRequest` (run_id / tenant_id / task_id / agent_id
    mismatch, or duplicate proposal_id within the same request).

    Raised during pre-flight identity validation before per-proposal
    review.
    """


class InvalidEvidenceReferenceError(ReviewError):
    """A Proposal references Evidence that is missing, foreign-tenant,
    or has a tampered content_hash.

    Raised during Evidence integrity validation.  Per Phase 5A
    Section 7.2 the Reviewer must not silently ignore invalid Evidence.
    """


# ---------------------------------------------------------------------------
# Authority / Action / Policy
# ---------------------------------------------------------------------------


class AuthorityViolationError(ReviewError):
    """An Agent's Capability Snapshot authority is insufficient for
    the Action it proposed (e.g. a READ-only agent proposing a Write
    Action, or a PROPOSE agent proposing an EXECUTE Action).

    Raised during Authority validation against the frozen Capability
    Snapshot taken at Phase 4 pre-flight time — never against a live
    registry.
    """


class UnknownActionError(ReviewError):
    """The Proposal's ``action_type`` is not in the registered
    Action Allowlist, or the referenced Tool is not in the
    :class:`ToolCatalog`.

    Per Phase 5A Section 7.4 the Reviewer only validates Tool /
    Action shape — it never invokes the Tool.
    """


class PolicyEvaluationError(ReviewError):
    """A :class:`PolicyEvaluator` raised an unrecoverable
    infrastructure error (e.g. the OPA adapter could not reach the
    configured endpoint, or the deterministic evaluator received a
    malformed rule set).

    Distinct from a Policy *decision* of ``denied`` or
    ``needs_input`` — those are normal :class:`ReviewFinding` entries,
    not exceptions.
    """


# ---------------------------------------------------------------------------
# Conflict
# ---------------------------------------------------------------------------


class ReviewConflictError(ReviewError):
    """The Reviewer detected a Conflict between Proposals that
    prevents a deterministic batch decision (e.g. same resource +
    different target values, mutually exclusive actions).

    Per Phase 5A Section 10.3 conflicts are surfaced as
    ``ReviewDecisionStatus.CONFLICT`` on the affected Proposals rather
    than raised.  This exception is reserved for the rare case where
    conflict resolution itself fails (e.g. canonical key computation
    raised on a malformed Proposal).
    """


__all__ = [
    "AuthorityViolationError",
    "InvalidEvidenceReferenceError",
    "InvalidProposalIdentityError",
    "InvalidReviewRequestError",
    "InvalidReviewResultError",
    "PolicyEvaluationError",
    "ReviewConflictError",
    "ReviewError",
    "ReviewIntegrityError",
    "UnknownActionError",
]
