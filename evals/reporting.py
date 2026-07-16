"""Safe, deterministic rendering helpers for evaluation artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def dataset_digest(path: Path) -> str:
    """Return a stable SHA-256 digest for a versioned fixture file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_markdown_summary(report: dict[str, Any]) -> str:
    """Render the report without URLs, credentials, prompts, or record payloads."""
    metrics = report["metrics"]
    dataset = report["dataset"]
    return "\n".join(
        [
            "# AI Evaluation Summary",
            "",
            f"- Result: {'PASS' if report['passed'] else 'FAIL'}",
            f"- Evaluator: `{report['evaluator']}`",
            f"- Commit: `{report.get('git_commit') or 'unavailable'}`",
            f"- Duration: `{report['duration_ms']} ms`",
            f"- Corpus digest: `{dataset['corpus_sha256']}`",
            f"- Cases digest: `{dataset['cases_sha256']}`",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Cases | {metrics['case_count']} |",
            f"| Recall@5 | {metrics['recall_at_5']:.3f} |",
            f"| Precision@5 | {metrics['precision_at_5']:.3f} |",
            f"| Case pass rate | {metrics['case_pass_rate']:.3f} |",
            f"| Cross-tenant denial pass rate | {metrics['cross_tenant_denial_pass_rate']:.3f} |",
            f"| Tenant leaks | {metrics['tenant_leak_count']} |",
            "",
            "This report evaluates the real PostgreSQL/RLS structured retrieval path.",
            "It does not measure semantic retrieval or live-model answer quality.",
            "",
        ]
    )
