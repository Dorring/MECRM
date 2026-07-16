"""Static regression tests for the H2 structured-retrieval evaluation assets."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.structured_retrieval import EvalCase, load_cases, load_records, score_case, summarise


DATASETS = ROOT / "evals" / "datasets"


def test_structured_retrieval_dataset_is_valid_and_has_positive_and_negative_cases() -> None:
    records = load_records(DATASETS / "structured_retrieval_corpus.jsonl")
    cases = load_cases(
        DATASETS / "structured_retrieval_cases.jsonl",
        {record.record_id for record in records},
    )

    assert {record.tenant for record in records} == {"acme", "globex"}
    assert {record.entity_type for record in records} == {"lead", "deal", "ticket", "customer"}
    assert len(cases) >= 10
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
