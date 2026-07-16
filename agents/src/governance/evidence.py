"""Allow-list formatting for persisted agent evidence identifiers."""

from __future__ import annotations

import re
from typing import Any


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
VALID_RUN_STATUSES = frozenset({"completed", "pending_approval", "denied", "degraded", "failed"})


def is_valid_run_status(value: str) -> bool:
    return value in VALID_RUN_STATUSES


def safe_evidence_identifiers(value: Any) -> list[dict[str, str]]:
    """Keep only bounded evidence type/source identifiers, never raw payloads."""
    if not isinstance(value, list):
        return []
    safe: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        evidence_type = item.get("type")
        source_id = item.get("source_id") or item.get("sourceId") or item.get("id")
        if not isinstance(evidence_type, str) or not isinstance(source_id, str):
            continue
        if not _SAFE_IDENTIFIER.fullmatch(evidence_type) or not _SAFE_IDENTIFIER.fullmatch(source_id):
            continue
        safe.append({"type": evidence_type, "source_id": source_id})
    return safe


def safe_tool_call_summaries(value: Any) -> list[dict[str, str]]:
    """Keep tool route and outcome identifiers without arguments or payloads."""
    if not isinstance(value, list):
        return []
    safe: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool") or item.get("route")
        outcome = item.get("outcome") or item.get("status") or item.get("result")
        if not isinstance(name, str) or not _SAFE_IDENTIFIER.fullmatch(name):
            continue
        if not isinstance(outcome, str) or not _SAFE_IDENTIFIER.fullmatch(outcome):
            outcome = "recorded"
        safe.append({"name": name, "outcome": outcome})
    return safe
