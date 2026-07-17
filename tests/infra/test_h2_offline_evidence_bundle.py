"""Regression tests for the H2 interview-safe evaluation evidence bundle."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _structured_report() -> dict:
    return {
        "evaluation_type": "structured_retrieval_baseline",
        "passed": True,
        "evaluator": "retrieval-v1",
        "dataset": {"cases": "cases.jsonl"},
        "metrics": {"case_count": 35, "tenant_leak_count": 0, "recall_at_5": 1.0, "precision_at_5": 0.95},
    }


def _safety_report() -> dict:
    return {
        "evaluation_type": "deterministic_safety_contract",
        "passed": True,
        "evaluator": "safety-v1",
        "dataset": {"cases": "safety.jsonl"},
        "metrics": {"case_count": 21, "unsafe_execution_count": 0, "structured_output_pass_rate": 1.0, "prompt_injection_block_rate": 1.0, "tool_route_contract_coverage": 1.0, "citation_coverage": 1.0},
    }


def test_bundle_keeps_offline_safety_and_retrieval_boundaries() -> None:
    from evals.aggregate_reports import build_evidence_bundle, render_bundle_summary

    bundle = build_evidence_bundle(_structured_report(), _safety_report())
    summary = render_bundle_summary(bundle)

    assert bundle["passed"] is True
    assert bundle["network_accessed"] is False
    assert bundle["live_model_quality_included"] is False
    assert bundle["hard_gates"] == {
        "tenant_leak_count": 0,
        "unsafe_execution_count": 0,
        "structured_output_pass_rate": 1.0,
        "prompt_injection_block_rate": 1.0,
    }
    assert "Live NVIDIA NIM calls: not included." in summary


def test_bundle_rejects_failed_or_wrong_type_inputs() -> None:
    from evals.aggregate_reports import build_evidence_bundle

    failed = _safety_report()
    failed["passed"] = False
    try:
        build_evidence_bundle(_structured_report(), failed)
    except ValueError as exc:
        assert "did not pass" in str(exc)
    else:
        raise AssertionError("expected failed safety report to be rejected")


def test_runner_writes_json_and_markdown(tmp_path: Path) -> None:
    structured = tmp_path / "structured.json"
    safety = tmp_path / "safety.json"
    output = tmp_path / "bundle.json"
    summary = tmp_path / "bundle.md"
    structured.write_text(json.dumps(_structured_report()), encoding="utf-8")
    safety.write_text(json.dumps(_safety_report()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "evals/run_h2_evidence_bundle.py",
            "--structured-report",
            str(structured),
            "--safety-report",
            str(safety),
            "--output",
            str(output),
            "--summary-output",
            str(summary),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["evaluation_type"] == "offline_evidence_bundle"
    assert "Semantic retrieval and answer-quality claims: not included." in summary.read_text(encoding="utf-8")


def test_ci_aggregates_and_uploads_both_offline_reports() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ai-evaluation-baseline.yml").read_text(encoding="utf-8")

    assert "offline-evidence-bundle:" in workflow
    assert "needs: [safety-contracts, structured-retrieval]" in workflow
    assert "evals/run_h2_evidence_bundle.py" in workflow
    assert "ai-eval-h2-offline-evidence" in workflow
