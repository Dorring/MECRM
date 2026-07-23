"""Phase 5A Conflict Resolution & Duplicate Detection.

Pure-function module.  No I/O, no network, no side-effects.

Per Phase 5A Section 10:

1. **Canonical Proposal Identity** — a stable key computed from
   ``tenant_id`` + ``resource_type`` + ``resource_id`` + ``action_type``
   + canonical parameters (payload, sorted).
2. **Duplicate detection** — Proposals with the same canonical key
   AND the same ``idempotency_key`` are duplicates.  One is kept as
   the primary; the rest are recorded as duplicates with an audit
   finding.  Duplicates are never silently deleted.
3. **Conflict detection** — Proposals targeting the same resource
   but with conflicting intent are flagged.  Conflict groups never
   auto-select a winner; every member is marked
   :class:`ReviewDecisionStatus.CONFLICT`.

All output is sorted by stable keys so the same input produces the
same output regardless of insertion order or ``PYTHONHASHSEED``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from multi_agent.action_governance import get_action_governance_spec
from multi_agent.contracts import ActionProposal
from multi_agent.review_contracts import (
    CODE_CONFLICT_ACTIVATE_DEACTIVATE,
    CODE_CONFLICT_CREATE_DELETE,
    CODE_CONFLICT_FIELD_VALUE,
    CODE_CONFLICT_IDEMPOTENCY_MISMATCH,
    CODE_CONFLICT_MUTEX_NOTIFICATION,
    CODE_CONFLICT_OWNER_REASSIGN,
    CODE_DUPLICATE_DEDUPED,
    ReviewFinding,
    ReviewFindingSeverity,
)
from multi_agent.serialization import canonicalize, content_hash


# ---------------------------------------------------------------------------
# R2.1 P0-7: conflict-family → conflict-type mapping.
#
# Local action-family sets (``_ACTIVATE_ACTIONS``, ``_CREATE_ACTIONS``,
# ``_NOTIFICATION_ACTIONS``, ``_OWNER_ASSIGN_ACTIONS``) are REMOVED.
# The sole source of truth for "which actions conflict with each other"
# is :attr:`ActionGovernanceSpec.conflict_family`, read via
# :func:`get_action_governance_spec`.
#
# Two Proposals conflict iff:
#   1. They target the same resource (same tenant + target_entity + target_id)
#   2. Both have the SAME non-None ``conflict_family``
#   3. Their payloads differ in a way that is meaningful for that family
# ---------------------------------------------------------------------------

_CONFLICT_FAMILY_TYPES: dict[str, str] = {
    "crm_field_update": "field_value",
    "crm_status_activate": "activate_deactivate",
    "crm_create": "create_delete",
    "notification_mutex": "mutex_notification",
    "crm_owner_reassign": "owner_reassign",
}


def _get_conflict_family(action_type: str) -> str | None:
    """R2.1 P0-7: return the ``conflict_family`` for *action_type* from
    the governance registry, or ``None`` if the action is unknown or
    has no conflict family."""
    spec = get_action_governance_spec(action_type)
    if spec is None:
        return None
    return spec.conflict_family


# ---------------------------------------------------------------------------
# Canonical key
# ---------------------------------------------------------------------------


def compute_canonical_key(proposal: ActionProposal) -> str:
    """Return a stable canonical key for *proposal*.

    The key is a SHA-256 over:

    * ``tenant_id``
    * ``target_entity`` (resource type)
    * ``target_id`` (resource id)
    * ``action_type``
    * canonical ``payload`` (sorted)

    Excludes ``proposal_id``, ``idempotency_key``, ``created_by_agent``,
    ``evidence_ids``, ``risk_level``, ``priority`` — two Proposals
    with the same resource + action + payload are considered the same
    *intent* regardless of which agent proposed them or which evidence
    they cite.
    """
    payload = {
        "tenant_id": proposal.tenant_id,
        "resource_type": proposal.target_entity,
        "resource_id": proposal.target_id,
        "action_type": proposal.action_type,
        "canonical_parameters": canonicalize(proposal.payload),
    }
    return content_hash(payload)


def _resource_key(proposal: ActionProposal) -> tuple[str, str, str | None]:
    """Return (tenant_id, target_entity, target_id) — the resource
    identity without the action or payload."""
    return (proposal.tenant_id, proposal.target_entity, proposal.target_id)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateGroup:
    """A group of Proposals that are exact duplicates.

    ``primary_proposal_id`` is the kept Proposal; ``duplicate_proposal_ids``
    are the excluded duplicates.  The primary is chosen deterministically
    as the lexicographically smallest ``proposal_id`` among the group
    so the choice is reproducible across runs.
    """

    primary_proposal_id: str
    duplicate_proposal_ids: tuple[str, ...]
    canonical_key: str
    idempotency_key: str

    @property
    def all_proposal_ids(self) -> tuple[str, ...]:
        return (self.primary_proposal_id, *self.duplicate_proposal_ids)


@dataclass
class DeduplicationResult:
    """Output of :func:`detect_duplicates`.

    ``deduped_proposal_ids`` is the set of Proposal IDs that survived
    deduplication (i.e. the primary of each duplicate group plus all
    non-duplicate Proposals).  ``excluded_proposal_ids`` is the set
    of Proposal IDs that were marked as duplicates.

    ``findings`` carries one ``CODE_DUPLICATE_DEDUPED`` finding per
    excluded Proposal.
    """

    deduped_proposal_ids: set[str] = field(default_factory=set)
    excluded_proposal_ids: set[str] = field(default_factory=set)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)


def detect_duplicates(
    proposals: Sequence[ActionProposal],
) -> DeduplicationResult:
    """Detect exact-duplicate Proposals and choose a deterministic primary.

    Two Proposals are duplicates iff they share the same
    :func:`compute_canonical_key` AND the same ``idempotency_key``.

    The primary is the lexicographically smallest ``proposal_id`` in
    the group.  All other members are excluded and recorded with an
    audited :class:`ReviewFinding` (``CODE_DUPLICATE_DEDUPED``).
    """
    result = DeduplicationResult()

    # Group by (canonical_key, idempotency_key)
    groups: dict[tuple[str, str], list[ActionProposal]] = {}
    for p in proposals:
        key = (compute_canonical_key(p), p.idempotency_key)
        groups.setdefault(key, []).append(p)

    for (canon_key, idem_key), group in sorted(groups.items()):
        # Sort by proposal_id for deterministic primary selection
        sorted_group = sorted(group, key=lambda p: p.proposal_id)
        if len(sorted_group) == 1:
            result.deduped_proposal_ids.add(sorted_group[0].proposal_id)
            continue

        # Multiple Proposals with same canonical key + idempotency key
        primary = sorted_group[0]
        duplicates = sorted_group[1:]
        result.deduped_proposal_ids.add(primary.proposal_id)
        for d in duplicates:
            result.excluded_proposal_ids.add(d.proposal_id)
            result.findings.append(
                ReviewFinding(
                    finding_code=CODE_DUPLICATE_DEDUPED,
                    severity=ReviewFindingSeverity.INFO,
                    message=(
                        f"Proposal {d.proposal_id!r} is a duplicate of "
                        f"primary {primary.proposal_id!r}; excluded from "
                        f"batch"
                    ),
                    proposal_id=d.proposal_id,
                    agent_id=d.created_by_agent,
                    policy_source="conflict_resolution@ma-05a",
                    details={
                        "primary_proposal_id": primary.proposal_id,
                        "duplicate_proposal_id": d.proposal_id,
                        "canonical_key": canon_key,
                        "idempotency_key": idem_key,
                    },
                )
            )
            result.duplicate_groups.append(
                DuplicateGroup(
                    primary_proposal_id=primary.proposal_id,
                    duplicate_proposal_ids=(d.proposal_id,),
                    canonical_key=canon_key,
                    idempotency_key=idem_key,
                )
            )

    return result


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictGroup:
    """A group of Proposals that conflict on the same resource.

    Every member of the group is marked CONFLICT — no auto-selection.
    """

    conflict_type: str
    proposal_ids: tuple[str, ...]
    resource_key: tuple[str, str, str | None]
    detail: str = ""


@dataclass
class ConflictResult:
    """Output of :func:`detect_conflicts`.

    ``conflicted_proposal_ids`` is the set of Proposal IDs that are
    part of at least one :class:`ConflictGroup`.  ``findings`` carries
    one ``CODE_CONFLICT_*`` finding per conflicted Proposal per group.
    """

    conflicted_proposal_ids: set[str] = field(default_factory=set)
    conflict_groups: list[ConflictGroup] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)


def _payload_field_conflict(p1: ActionProposal, p2: ActionProposal) -> bool:
    """Return True iff p1 and p2 write different values to the same
    payload field.

    A field is "written" if it appears in the payload dict.  Two
    Proposals conflict if they both write the same field name but
    with different values.
    """
    common = set(p1.payload.keys()) & set(p2.payload.keys())
    for k in common:
        if canonicalize(p1.payload[k]) != canonicalize(p2.payload[k]):
            return True
    return False


def _same_conflict_family(p1: ActionProposal, p2: ActionProposal) -> str | None:
    """R2.1 P0-7: return the shared ``conflict_family`` if both
    Proposals belong to the same conflict family, else ``None``.

    Reads :attr:`ActionGovernanceSpec.conflict_family` via
    :func:`get_action_governance_spec` — no local action-type sets.
    """
    fam1 = _get_conflict_family(p1.action_type)
    if fam1 is None:
        return None
    fam2 = _get_conflict_family(p2.action_type)
    if fam2 is None:
        return None
    if fam1 != fam2:
        return None
    return fam1


def _is_owner_reassign(p1: ActionProposal, p2: ActionProposal) -> bool:
    """Return True iff p1 and p2 both reassign owner of the same
    customer to different owners.

    R2.1 P0-7: uses ``conflict_family == "crm_owner_reassign"`` from
    the governance registry instead of a local action-type set.
    """
    family = _same_conflict_family(p1, p2)
    if family != "crm_owner_reassign":
        return False
    # Both assign owner — conflict if the owner value differs.
    owner1 = p1.payload.get("owner_id") or p1.payload.get("owner")
    owner2 = p2.payload.get("owner_id") or p2.payload.get("owner")
    if owner1 is None or owner2 is None:
        return False
    return canonicalize(owner1) != canonicalize(owner2)


def detect_idempotency_key_conflicts(
    proposals: Sequence[ActionProposal],
) -> list[tuple[str, str]]:
    """Return pairs of Proposal IDs that share an idempotency_key but
    have different canonical keys.

    Per Phase 5A Section 10.3: "重复但 Idempotency Key 不一致" is a
    conflict — the same idempotency_key MUST always map to the same
    intent.  Two Proposals with the same idempotency_key but different
    canonical keys indicate a misuse that must be surfaced as a
    conflict, not silently deduplicated.
    """
    by_idem: dict[str, list[ActionProposal]] = {}
    for p in proposals:
        by_idem.setdefault(p.idempotency_key, []).append(p)

    pairs: list[tuple[str, str]] = []
    for idem_key, group in sorted(by_idem.items()):
        if len(group) < 2:
            continue
        # Check if all members have the same canonical key
        keys = {compute_canonical_key(p) for p in group}
        if len(keys) > 1:
            # Conflict — sort pairs deterministically
            sorted_group = sorted(group, key=lambda p: p.proposal_id)
            for i in range(len(sorted_group)):
                for j in range(i + 1, len(sorted_group)):
                    pairs.append(
                        (sorted_group[i].proposal_id, sorted_group[j].proposal_id)
                    )
    return pairs


def detect_conflicts(
    proposals: Sequence[ActionProposal],
    *,
    excluded_proposal_ids: set[str] | None = None,
) -> ConflictResult:
    """Detect conflicts among *proposals*.

    ``excluded_proposal_ids`` (from :class:`DeduplicationResult`) is
    used to skip Proposals that were already marked as duplicates —
    a duplicate cannot also be a conflict participant.

    Conflict heuristics (Phase 5A Section 10.3):

    1. Same resource + different payload field values → field_value
    2. Same resource + activate/deactivate → activate_deactivate
    3. Same resource + create/delete → create_delete
    4. Same idempotency_key + different canonical key → idempotency_mismatch
    5. Same customer + mutex notifications → mutex_notification
    6. Same customer + different owner reassign → owner_reassign
    """
    result = ConflictResult()
    skip = excluded_proposal_ids or set()

    # Filter out duplicates
    active = [p for p in proposals if p.proposal_id not in skip]

    # Group by resource key
    by_resource: dict[tuple[str, str, str | None], list[ActionProposal]] = {}
    for p in active:
        by_resource.setdefault(_resource_key(p), []).append(p)

    # Check each resource group for conflicts
    for res_key, group in sorted(by_resource.items(), key=lambda x: str(x[0])):
        if len(group) < 2:
            continue
        sorted_group = sorted(group, key=lambda p: p.proposal_id)

        # Pairwise conflict detection
        for i in range(len(sorted_group)):
            for j in range(i + 1, len(sorted_group)):
                p1, p2 = sorted_group[i], sorted_group[j]
                conflict_type, detail = _classify_pair(p1, p2)
                if conflict_type is None:
                    continue
                cg = ConflictGroup(
                    conflict_type=conflict_type,
                    proposal_ids=(p1.proposal_id, p2.proposal_id),
                    resource_key=res_key,
                    detail=detail,
                )
                result.conflict_groups.append(cg)
                result.conflicted_proposal_ids.update(cg.proposal_ids)
                code = _CONFLICT_TYPE_CODES[conflict_type]
                for pid in cg.proposal_ids:
                    p = next(pp for pp in sorted_group if pp.proposal_id == pid)
                    other = p2 if pid == p1.proposal_id else p1
                    result.findings.append(
                        ReviewFinding(
                            finding_code=code,
                            severity=ReviewFindingSeverity.ERROR,
                            message=(
                                f"Proposal {pid!r} conflicts with "
                                f"{other.proposal_id!r} on resource "
                                f"{res_key!r}: {detail}"
                            ),
                            proposal_id=pid,
                            agent_id=p.created_by_agent,
                            policy_source="conflict_resolution@ma-05a",
                            details={
                                "conflict_type": conflict_type,
                                "other_proposal_id": other.proposal_id,
                                "resource_key": list(res_key),
                            },
                        )
                    )

    # Idempotency-key conflicts (cross-resource)
    idem_pairs = detect_idempotency_key_conflicts(active)
    for pid_a, pid_b in idem_pairs:
        cg = ConflictGroup(
            conflict_type="idempotency_mismatch",
            proposal_ids=(pid_a, pid_b),
            resource_key=("", "", None),
            detail=(
                f"Proposals {pid_a!r} and {pid_b!r} share an "
                f"idempotency_key but have different canonical keys"
            ),
        )
        result.conflict_groups.append(cg)
        result.conflicted_proposal_ids.update(cg.proposal_ids)
        for pid in cg.proposal_ids:
            other_pid = pid_b if pid == pid_a else pid_a
            result.findings.append(
                ReviewFinding(
                    finding_code=CODE_CONFLICT_IDEMPOTENCY_MISMATCH,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Proposal {pid!r} shares idempotency_key with "
                        f"{other_pid!r} but has a different canonical key"
                    ),
                    proposal_id=pid,
                    policy_source="conflict_resolution@ma-05a",
                    details={
                        "conflict_type": "idempotency_mismatch",
                        "other_proposal_id": other_pid,
                    },
                )
            )

    return result


_CONFLICT_TYPE_CODES: dict[str, str] = {
    "field_value": CODE_CONFLICT_FIELD_VALUE,
    "activate_deactivate": CODE_CONFLICT_ACTIVATE_DEACTIVATE,
    "create_delete": CODE_CONFLICT_CREATE_DELETE,
    "mutex_notification": CODE_CONFLICT_MUTEX_NOTIFICATION,
    "owner_reassign": CODE_CONFLICT_OWNER_REASSIGN,
}


def _classify_pair(
    p1: ActionProposal,
    p2: ActionProposal,
) -> tuple[str | None, str]:
    """Return (conflict_type, detail) for a pair of Proposals.

    Returns (None, "") if the pair does not conflict.

    R2.1 P0-7: conflict classification is driven SOLELY by
    :attr:`ActionGovernanceSpec.conflict_family` (read via
    :func:`get_action_governance_spec`).  Local action-type sets are
    removed.  Two Proposals conflict iff they share the same non-None
    ``conflict_family`` and their payloads differ meaningfully.
    """
    # 1. Idempotency-key mismatch on same canonical key is handled by
    #    detect_duplicates (treated as dedup, not conflict).  Here we
    #    only handle cross-canonical-key idempotency conflicts.

    # 2. R2.1 P0-7: check shared conflict_family from the governance
    #    registry.  No local action-type sets.
    family = _same_conflict_family(p1, p2)
    if family is None:
        # No shared conflict family — not a conflict.  Different
        # action_types without a shared family are not auto-conflicts.
        return (None, "")

    conflict_type = _CONFLICT_FAMILY_TYPES.get(family)
    if conflict_type is None:
        # Unknown conflict_family — not a conflict we recognise.
        return (None, "")

    # 3. Family-specific payload conflict checks.
    if conflict_type == "owner_reassign":
        if _is_owner_reassign(p1, p2):
            return (
                "owner_reassign",
                "same customer reassigned to different owners",
            )
        return (None, "")

    if conflict_type == "activate_deactivate":
        # Same conflict_family on same resource with different payload
        # field values → activate/deactivate conflict.
        if _payload_field_conflict(p1, p2):
            return (
                "activate_deactivate",
                "activate/deactivate on same resource with different states",
            )
        return (None, "")

    if conflict_type == "mutex_notification":
        # Mutex notifications: conflict if different action_types OR
        # different payload field values on the same resource.
        if p1.action_type != p2.action_type:
            return (
                "mutex_notification",
                f"mutex notifications {p1.action_type!r} vs {p2.action_type!r}",
            )
        if _payload_field_conflict(p1, p2):
            return (
                "mutex_notification",
                "mutex notifications on same customer with different payloads",
            )
        return (None, "")

    if conflict_type == "create_delete":
        # Same conflict_family (crm_create) on same resource — conflict
        # if the payloads differ.
        if _payload_field_conflict(p1, p2) or p1.action_type != p2.action_type:
            return (
                "create_delete",
                f"create/delete conflict: {p1.action_type!r} vs {p2.action_type!r}",
            )
        return (None, "")

    # field_value (crm_field_update): same resource + same family +
    # different payload field values.
    if _payload_field_conflict(p1, p2):
        return (
            "field_value",
            "same resource + same conflict family + different payload field values",
        )

    return (None, "")


__all__ = [
    "ConflictGroup",
    "ConflictResult",
    "DeduplicationResult",
    "DuplicateGroup",
    "compute_canonical_key",
    "detect_conflicts",
    "detect_duplicates",
    "detect_idempotency_key_conflicts",
]
