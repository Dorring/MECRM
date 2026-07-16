"""Regression checks for the H2-4 safe agent-run evidence boundary."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_gateway_projects_decisions_before_returning_them() -> None:
    source = _read("gateway/src/routes/governance.ts")

    assert "function toSafeAgentRun" in source
    assert "data: items.map((item) => toSafeAgentRun" in source
    assert "res.json(toSafeAgentRun(record.decision" in source
    assert "res.json(decision);" not in source


def test_gateway_safe_projection_does_not_return_raw_explainability_columns() -> None:
    source = _read("gateway/src/routes/governance.ts")
    projection = source.split("function toSafeAgentRun", 1)[1].split("function normalizeRunStatus", 1)[0]

    assert "decision.reasoning" not in projection
    assert "inputContext" not in projection
    assert "safeToolCalls" in projection
    assert "safeEvidence" in projection


def test_frontend_never_renders_reasoning_chain() -> None:
    source = _read("frontend/src/components/ExplainabilityPanel.tsx")

    assert "Reasoning chain" not in source
    assert "decision.reasoning" not in source
    assert "Safe agent run evidence" in source
    assert "retrievalEvidence" in source


def test_preflight_records_no_second_source_of_truth() -> None:
    source = _read("docs/preflight-h2-agent-run-evidence.md")

    assert "Do not create a new `agent_runs` table." in source
    assert "No chain of thought is persisted or rendered." in source
