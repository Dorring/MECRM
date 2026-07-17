"""Build the H2 offline evidence bundle from independent evaluation reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.aggregate_reports import (  # noqa: E402
    build_evidence_bundle,
    render_bundle_summary,
)


def _report(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON report: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"report must be an object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structured-report", required=True)
    parser.add_argument("--safety-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-output", required=True)
    args = parser.parse_args()

    bundle = build_evidence_bundle(_report(args.structured_report), _report(args.safety_report))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = Path(args.summary_output)
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(render_bundle_summary(bundle), encoding="utf-8")
    return 0 if bundle["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
