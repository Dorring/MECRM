"""Static regression tests for the H2 structured-retrieval evaluation assets."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.structured_retrieval import EvalCase, load_cases, load_records, score_case, summarise
from evals.reporting import render_markdown_summary


DATASETS = ROOT / "evals" / "datasets"


def test_structured_retrieval_dataset_is_valid_and_has_positive_and_negative_cases() -> None:
    records = load_records(DATASETS / "structured_retrieval_corpus.jsonl")
    cases = load_cases(
        DATASETS / "structured_retrieval_cases.jsonl",
        {record.record_id for record in records},
    )

    assert {record.tenant for record in records} == {"acme", "globex"}
    assert {record.entity_type for record in records} == {"lead", "deal", "ticket", "customer"}
    assert len(cases) >= 35
    assert any(case.expected_record_ids for case in cases)
    assert sum(not case.expected_record_ids for case in cases) >= 2


def test_score_case_computes_recall_precision_and_negative_denial() -> None:
    positive = EvalCase(
        case_id="positive",
        tenant="acme",
        query="Aurora",
        entity_type="lead",
        expected_record_ids=("acme-lead",),
        limit=5,
    )
    negative = EvalCase(
        case_id="negative",
        tenant="acme",
        query="Private Beacon",
        entity_type="ticket",
        expected_record_ids=(),
        limit=5,
    )

    positive_score = score_case(positive, ["acme-lead"])
    negative_score = score_case(negative, [])
    summary = summarise([positive_score, negative_score], tenant_leak_count=0)

    assert positive_score.recall_at_k == 1.0
    assert positive_score.precision_at_k == 1.0
    assert negative_score.passed is True
    assert summary["recall_at_5"] == 1.0
    assert summary["cross_tenant_denial_pass_rate"] == 1.0
    assert summary["tenant_leak_count"] == 0


def test_runner_declares_no_semantic_or_llm_baseline() -> None:
    text = (ROOT / "evals" / "run_structured_retrieval_eval.py").read_text(encoding="utf-8")

    assert '"semantic_retrieval_included": False' in text
    assert '"llm_quality_included": False' in text
    assert "tenant_leak_count" in text


def test_ci_workflow_runs_real_db_baseline_and_uploads_report() -> None:
    text = (ROOT / ".github" / "workflows" / "ai-evaluation-baseline.yml").read_text(
        encoding="utf-8"
    )

    assert "docker compose up -d postgres" in text
    assert "bash ./scripts/migrate.sh" in text
    assert "evals/run_structured_retrieval_eval.py" in text
    assert "ai-eval-structured-retrieval" in text
    assert "--summary-output reports/ai-evals/structured-retrieval.md" in text


def test_ci_workflow_supplies_all_required_compose_interpolation_values() -> None:
    text = (ROOT / ".github" / "workflows" / "ai-evaluation-baseline.yml").read_text(
        encoding="utf-8"
    )

    for required_name in (
        "CRM_APP_PASSWORD",
        "JWT_SECRET",
        "KEYCLOAK_ADMIN_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
    ):
        assert f"  {required_name}:" in text
    assert "if: ${{ success() }}" in text
    assert "if: always()" not in text


def test_markdown_summary_exposes_metrics_and_dataset_digests_without_secrets() -> None:
    summary = render_markdown_summary(
        {
            "passed": True,
            "evaluator": "test-evaluator",
            "git_commit": "abc123",
            "duration_ms": 42,
            "dataset": {"corpus_sha256": "corpus-digest", "cases_sha256": "cases-digest"},
            "metrics": {
                "case_count": 35,
                "recall_at_5": 1.0,
                "precision_at_5": 0.95,
                "case_pass_rate": 1.0,
                "cross_tenant_denial_pass_rate": 1.0,
                "tenant_leak_count": 0,
            },
        }
    )

    assert "# AI Evaluation Summary" in summary
    assert "Recall@5 | 1.000" in summary
    assert "corpus-digest" in summary
    assert "postgresql://" not in summary
