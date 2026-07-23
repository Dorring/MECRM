"""Proposal hash computation — uses the shared canonicalizer.

This module lives outside ``contracts.py`` so that proposal hash computations
can import from ``serialization`` without creating a circular dependency
(serialization imports contracts).
"""

from __future__ import annotations

from typing import Any

from multi_agent.serialization import content_hash


def compute_proposal_hash(
    *,
    tenant_id: str,
    created_by_agent: str,
    action_type: str,
    target_entity: str,
    target_id: str | None,
    payload: dict[str, Any],
    priority: str,
    risk_level: str,
    justification: str | None,
    evidence_ids: list[str],
    requires_approval: bool,
) -> str:
    """Return a stable SHA-256 digest over canonical proposal content.

    Uses the shared ``canonicalize()`` → ``content_hash()`` pipeline.
    Fields excluded from the hash (by design):
      - proposal_id / proposal_hash (self-referential)
      - created_at (wall-clock)
      - idempotency_key (identity, not content)
    """
    return content_hash(
        {
            "tenant_id": tenant_id,
            "created_by_agent": created_by_agent,
            "action_type": action_type,
            "target_entity": target_entity,
            "target_id": target_id,
            "payload": payload,
            "priority": priority,
            "risk_level": risk_level,
            "justification": justification,
            "evidence_ids": sorted(evidence_ids),
            "requires_approval": requires_approval,
        }
    )


def compute_evidence_hash_from_payload(payload: dict[str, Any]) -> str:
    """R2.1 P0-4: stable SHA-256 over canonical Evidence content.

    Single source of truth for Evidence content integrity shared by
    Phase 4 (``SupervisorRuntime._finalize`` building
    ``ResultOriginSnapshot.evidence_hashes``) and Phase 5A
    (:func:`multi_agent.evidence_review.compute_review_evidence_hash`
    used by :class:`ReviewEvidenceSnapshot._verify_snapshot_hash`).

    Excludes the self-referential ``content_hash`` field so a tampered
    Evidence that keeps the old declared hash is still detected.
    """
    p = dict(payload)
    p.pop("content_hash", None)
    return content_hash(p)
