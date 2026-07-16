"""Dataset parsing and metrics for the structured-retrieval baseline.

This module is intentionally provider-free.  It evaluates the existing
Postgres/RLS-backed structured search path, not semantic retrieval or LLM answer
quality.  Keeping the scoring pure makes it testable in the normal static test
suite; the runner supplies actual database results in CI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_ENTITIES = {"lead", "deal", "ticket", "customer"}


@dataclass(frozen=True)
class EvalRecord:
    record_id: str
    tenant: str
    entity_type: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    tenant: str
    query: str
    entity_type: str | None
    expected_record_ids: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    expected_record_ids: tuple[str, ...]
    returned_record_ids: tuple[str, ...]
    recall_at_k: float | None
    precision_at_k: float | None
    passed: bool


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        rows.append(row)
    return rows


def load_records(path: Path) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    seen: set[str] = set()
    for row in _read_jsonl(path):
        record_id = row.get("record_id")
        tenant = row.get("tenant")
        entity_type = row.get("entity_type")
        fields = row.get("fields")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("record_id must be a non-empty string")
        if record_id in seen:
            raise ValueError(f"duplicate record_id: {record_id}")
        if not isinstance(tenant, str) or not tenant:
            raise ValueError(f"{record_id}: tenant must be a non-empty string")
        if entity_type not in SUPPORTED_ENTITIES:
            raise ValueError(f"{record_id}: unsupported entity_type {entity_type!r}")
        if not isinstance(fields, dict):
            raise ValueError(f"{record_id}: fields must be an object")
        seen.add(record_id)
        records.append(EvalRecord(record_id, tenant, entity_type, fields))
    if not records:
        raise ValueError("retrieval corpus must not be empty")
    return records


def load_cases(path: Path, known_record_ids: set[str]) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for row in _read_jsonl(path):
        case_id = row.get("case_id")
        tenant = row.get("tenant")
        query = row.get("query")
        entity_type = row.get("entity_type")
        expected = row.get("expected_record_ids")
        limit = row.get("limit", 5)
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        if not isinstance(tenant, str) or not tenant:
            raise ValueError(f"{case_id}: tenant must be a non-empty string")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{case_id}: query must be a non-empty string")
        if entity_type is not None and entity_type not in SUPPORTED_ENTITIES:
            raise ValueError(f"{case_id}: unsupported entity_type {entity_type!r}")
        if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
            raise ValueError(f"{case_id}: expected_record_ids must be a string array")
        if not isinstance(limit, int) or limit < 1 or limit > 20:
            raise ValueError(f"{case_id}: limit must be an integer between 1 and 20")
        unknown = set(expected) - known_record_ids
        if unknown:
            raise ValueError(f"{case_id}: unknown expected record IDs: {sorted(unknown)}")
        seen.add(case_id)
        cases.append(EvalCase(case_id, tenant, query, entity_type, tuple(expected), limit))
    if not cases:
        raise ValueError("retrieval case dataset must not be empty")
    return cases


def score_case(case: EvalCase, returned_record_ids: list[str]) -> CaseScore:
    returned = tuple(returned_record_ids[: case.limit])
    expected = set(case.expected_record_ids)
    returned_set = set(returned)

    if not expected:
        return CaseScore(
            case_id=case.case_id,
            expected_record_ids=case.expected_record_ids,
            returned_record_ids=returned,
            recall_at_k=None,
            precision_at_k=None,
            passed=not returned,
        )

    hits = expected & returned_set
    recall = len(hits) / len(expected)
    precision = len(hits) / len(returned) if returned else 0.0
    return CaseScore(
        case_id=case.case_id,
        expected_record_ids=case.expected_record_ids,
        returned_record_ids=returned,
        recall_at_k=recall,
        precision_at_k=precision,
        passed=expected.issubset(returned_set),
    )


def summarise(scores: list[CaseScore], *, tenant_leak_count: int) -> dict[str, Any]:
    positive = [score for score in scores if score.recall_at_k is not None]
    negative = [score for score in scores if score.recall_at_k is None]
    if not positive:
        raise ValueError("evaluation requires at least one positive retrieval case")
    return {
        "case_count": len(scores),
        "positive_case_count": len(positive),
        "negative_case_count": len(negative),
        "case_pass_rate": sum(score.passed for score in scores) / len(scores),
        "recall_at_5": sum(score.recall_at_k or 0.0 for score in positive) / len(positive),
        "precision_at_5": sum(score.precision_at_k or 0.0 for score in positive) / len(positive),
        "cross_tenant_denial_pass_rate": (
            sum(score.passed for score in negative) / len(negative) if negative else 1.0
        ),
        "tenant_leak_count": tenant_leak_count,
    }
