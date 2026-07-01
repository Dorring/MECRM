from __future__ import annotations

from dataclasses import dataclass

from .intent_parser import SearchIntent


@dataclass(frozen=True)
class Suggestion:
    label: str
    query: str
    reason: str


def generate_suggestions(*, intent: SearchIntent, roles: list[str], top_entity_type: str | None) -> list[Suggestion]:
    normalized_roles = [r.lower() for r in roles]
    suggestions: list[Suggestion] = []

    if any("sales" in r for r in normalized_roles):
        suggestions.extend(
            [
                Suggestion(label="Recent leads", query="recent leads", reason="Common sales workflow"),
                Suggestion(label="Qualified leads", query="qualified leads", reason="Pipeline focus"),
                Suggestion(label="Prospecting deals", query="prospecting deals", reason="Pipeline stage"),
            ]
        )

    if any("support" in r for r in normalized_roles):
        suggestions.extend(
            [
                Suggestion(label="Open tickets", query="open tickets", reason="Common support workflow"),
                Suggestion(label="High priority tickets", query="high priority tickets", reason="Triage"),
            ]
        )

    if intent.entity != "unknown":
        suggestions.insert(
            0,
            Suggestion(
                label=f"Recent {intent.entity}s",
                query=f"recent {intent.entity}s",
                reason="Based on detected entity",
            ),
        )

    if top_entity_type and top_entity_type != "unknown":
        suggestions.append(
            Suggestion(
                label=f"More {top_entity_type}s",
                query=f"recent {top_entity_type}s",
                reason="Based on top result type",
            )
        )

    dedup: dict[str, Suggestion] = {}
    for s in suggestions:
        dedup[s.query] = s
    return list(dedup.values())[:6]

