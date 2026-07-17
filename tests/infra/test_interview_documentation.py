"""Regression checks for the H2 interview-facing documentation.

These checks keep the public project entry points aligned with the verified
Docker Desktop workflow and make it harder for obsolete bootstrap instructions
to reappear after future infrastructure changes.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
INTERVIEW_DIR = ROOT / "docs" / "interview"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_uses_current_compose_and_migration_flow() -> None:
    text = _text(README)

    assert "docker compose up -d --build --wait" in text
    assert "docker compose --profile migrate run --rm migrate" in text
    assert "docker-compose exec gateway npx prisma migrate deploy" not in text
    assert "/docker-entrypoint-initdb.d/02-rls-policies.sql" not in text


def test_readme_describes_ollama_as_opt_in() -> None:
    text = _text(README)

    assert "does **not** download or start Ollama" in text
    assert "docker compose --profile local-llm up -d ollama" in text


def test_interview_docs_cover_required_architecture_topics() -> None:
    required = {
        "architecture.md": [
            "## Context",
            "## Containers and ownership",
            "## Canonical flow",
            "## Failure behavior",
            "## Scaling path",
        ],
        "demo-script.md": ["## Five-minute primary demo", "## Required evidence"],
        "project-briefing.md": ["## Problem", "## What is verified", "## Metrics and their boundaries"],
        "evidence-capture-map.md": ["## Before capture", "## Capture map", "## Video walkthrough"],
        "engineering-tradeoffs.md": ["## Why agent workflows"],
        "limitations.md": ["# Current Limitations"],
        "interview-qa.md": ["# Interview Q&A"],
        "capture-checklist.md": ["# Evidence Capture Checklist"],
    }

    for name, headings in required.items():
        path = INTERVIEW_DIR / name
        assert path.is_file(), f"missing interview document: {path}"
        text = _text(path)
        for heading in headings:
            assert heading in text, f"{name} missing heading: {heading}"


def test_governance_demo_has_no_screenshot_placeholders() -> None:
    text = _text(ROOT / "docs" / "ai-governance-demo.md")

    assert "Screenshot placeholders:" not in text
    assert "capture-checklist.md" in text


def test_interview_docs_do_not_claim_the_missing_demo_runner_exists() -> None:
    demo_script = _text(INTERVIEW_DIR / "demo-script.md")
    capture_map = _text(INTERVIEW_DIR / "evidence-capture-map.md")

    assert "python scripts/interview_demo.py" not in demo_script
    assert "not implemented yet" in demo_script
    assert "Do not substitute an invented chat answer." in capture_map


def test_readme_links_to_the_interview_briefing_and_capture_map() -> None:
    text = _text(README)

    assert "docs/interview/project-briefing.md" in text
    assert "docs/interview/evidence-capture-map.md" in text
