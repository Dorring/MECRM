from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .retriever import RetrievedResult


@dataclass(frozen=True)
class RankedResult:
    entity_type: str
    entity_id: str
    tenant_id: str
    title: str
    description: str | None
    url: str
    created_at: datetime | None
    updated_at: datetime | None
    final_score: float
    sources: list[Literal["structured", "semantic"]]
    score_components: dict[str, float]
    reasoning: dict[str, Any]
    metadata: dict[str, Any] | None


def rank_results(
    *,
    query: str,
    roles: list[str],
    module: str | None,
    results: list[RetrievedResult],
    limit: int,
) -> list[RankedResult]:
    now = datetime.now(timezone.utc)
    grouped: dict[tuple[str, str], list[RetrievedResult]] = {}
    for r in results:
        grouped.setdefault((r.entity_type, r.entity_id), []).append(r)

    ranked: list[RankedResult] = []
    for (entity_type, entity_id), items in grouped.items():
        base = _pick_base(items)
        semantic_score = max((i.semantic_score for i in items), default=0.0)
        structured_score = max((i.structured_score for i in items), default=0.0)
        role_weight = _role_weight(roles, entity_type)
        recency = _recency_score(now, base.updated_at or base.created_at)
        module_affinity = _module_affinity(module, entity_type)
        semantic = max(semantic_score, structured_score * 0.35)
        final_score = (semantic * 0.5) + (role_weight * 0.2) + (recency * 0.2) + (module_affinity * 0.1)

        ranked.append(
            RankedResult(
                entity_type=entity_type,
                entity_id=entity_id,
                tenant_id=base.tenant_id,
                title=base.title,
                description=base.description,
                url=_entity_url(entity_type, entity_id),
                created_at=base.created_at,
                updated_at=base.updated_at,
                final_score=float(final_score),
                sources=sorted({i.source for i in items}),
                score_components={
                    "semantic_score": float(semantic_score),
                    "structured_score": float(structured_score),
                    "role_weight": float(role_weight),
                    "recency": float(recency),
                    "module_affinity": float(module_affinity),
                },
                reasoning={
                    "weights": {"semantic": 0.5, "role": 0.2, "recency": 0.2, "module_affinity": 0.1},
                    "query": query,
                    "role_signals": _role_signals(roles, entity_type),
                    "module": module,
                },
                metadata=_merge_metadata(items),
            )
        )

    ranked.sort(key=lambda r: r.final_score, reverse=True)
    return ranked[: max(1, limit)]


def _pick_base(items: list[RetrievedResult]) -> RetrievedResult:
    items_sorted = sorted(
        items,
        key=lambda r: (
            1 if r.source == "structured" else 0,
            (r.updated_at or r.created_at or datetime.min),
        ),
        reverse=True,
    )
    return items_sorted[0]


def _role_weight(roles: list[str], entity_type: str) -> float:
    normalized = [r.lower() for r in roles]
    if any("super_admin" == r or "admin" in r for r in normalized):
        return 0.7
    if entity_type == "knowledge":
        return 0.8
    if entity_type in ("lead", "deal") and any("sales" in r for r in normalized):
        return 1.0
    if entity_type == "ticket" and any("support" in r for r in normalized):
        return 1.0
    if entity_type == "customer" and any(("support" in r or "sales" in r) for r in normalized):
        return 0.85
    return 0.45


def _role_signals(roles: list[str], entity_type: str) -> dict[str, Any]:
    return {"roles": roles, "entity_type": entity_type}


def _recency_score(now: datetime, ts: datetime | None) -> float:
    if not ts:
        return 0.3
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return float(1.0 / (1.0 + (age_days / 7.0)))


def _module_affinity(module: str | None, entity_type: str) -> float:
    if not module:
        return 0.4
    m = module.lower().strip("/")
    plural = _plural(entity_type)
    return 1.0 if m.endswith(plural) else 0.3


def _plural(entity_type: str) -> str:
    if entity_type == "knowledge":
        return "knowledge"
    if entity_type.endswith("s"):
        return entity_type
    return f"{entity_type}s"


def _entity_url(entity_type: str, entity_id: str) -> str:
    if entity_type == "knowledge":
        return f"/knowledge/articles?id={entity_id}"
    return f"/{_plural(entity_type)}?id={entity_id}"


def _merge_metadata(items: list[RetrievedResult]) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for it in items:
        if not it.metadata:
            continue
        merged.update(it.metadata)
    return merged or None

