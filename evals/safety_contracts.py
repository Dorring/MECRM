"""Provider-free evaluation of AI input and evidence safety contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from governance.evidence import (
    is_valid_run_status,
    safe_evidence_identifiers,
    safe_tool_call_summaries,
)
from governance.input_safety import assess_untrusted_text


@dataclass(frozen=True)
class SafetyCase:
    case_id: str
    category: str
    text: str | None
    evidence: list[dict[str, Any]] | None
    tool_calls: list[dict[str, Any]] | None
    status: str | None
    expected: Any


def load_safety_cases(path: Path) -> list[SafetyCase]:
    cases: list[SafetyCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected object")
        case_id = row.get("case_id")
        category = row.get("category")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"{path}:{line_number}: case_id must be unique and non-empty")
        if category not in {"prompt_injection", "evidence", "tool_route", "run_status"}:
            raise ValueError(f"{case_id}: unsupported category {category!r}")
        expected = row.get("expected")
        if category == "prompt_injection" and not isinstance(row.get("text"), str):
            raise ValueError(f"{case_id}: prompt_injection requires text")
        if category == "evidence" and not isinstance(row.get("evidence"), list):
            raise ValueError(f"{case_id}: evidence requires an array")
        if category == "tool_route" and not isinstance(row.get("tool_calls"), list):
            raise ValueError(f"{case_id}: tool_route requires an array")
        if category == "run_status" and not isinstance(row.get("status"), str):
            raise ValueError(f"{case_id}: run_status requires status")
        seen.add(case_id)
        cases.append(
            SafetyCase(
                case_id=case_id,
                category=category,
                text=row.get("text"),
                evidence=row.get("evidence"),
                tool_calls=row.get("tool_calls"),
                status=row.get("status"),
                expected=expected,
            )
        )
    if not cases:
        raise ValueError("safety contract dataset must not be empty")
    return cases


def evaluate_safety_cases(cases: list[SafetyCase]) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    malicious_total = 0
    malicious_blocked = 0
    unsafe_execution_count = 0
    expected_citations = 0
    retained_citations = 0
    expected_routes = 0
    retained_routes = 0

    for case in cases:
        if case.category == "prompt_injection":
            decision = assess_untrusted_text(case.text or "")
            expected_allowed = bool(case.expected["allowed"])
            passed = decision.allowed is expected_allowed
            if not expected_allowed:
                malicious_total += 1
                if not decision.allowed:
                    malicious_blocked += 1
                else:
                    unsafe_execution_count += 1
            actual: Any = {"allowed": decision.allowed, "reason_code": decision.reason_code}
        elif case.category == "evidence":
            actual = safe_evidence_identifiers(case.evidence)
            expected = case.expected["evidence"]
            passed = actual == expected
            expected_citations += len(expected)
            retained_citations += len(actual)
        elif case.category == "tool_route":
            actual = safe_tool_call_summaries(case.tool_calls)
            expected = case.expected["tool_calls"]
            passed = actual == expected
            expected_routes += len(expected)
            retained_routes += len(actual)
        else:
            actual = {"valid": is_valid_run_status(case.status or "")}
            passed = actual["valid"] is bool(case.expected["valid"])
        details.append({"case_id": case.case_id, "category": case.category, "passed": passed, "actual": actual})

    if not malicious_total:
        raise ValueError("safety contract dataset requires prompt-injection cases")
    if not expected_citations:
        raise ValueError("safety contract dataset requires valid evidence cases")
    if not expected_routes:
        raise ValueError("safety contract dataset requires valid tool-route cases")
    pass_count = sum(detail["passed"] for detail in details)
    return {
        "case_count": len(cases),
        "structured_output_pass_rate": pass_count / len(cases),
        "prompt_injection_block_rate": malicious_blocked / malicious_total,
        "unsafe_execution_count": unsafe_execution_count,
        "citation_coverage": retained_citations / expected_citations,
        "tool_route_contract_coverage": retained_routes / expected_routes,
        "failed_case_count": len(cases) - pass_count,
        "cases": details,
    }
