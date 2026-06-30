import json
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_values(path: Path, prefix: str) -> list[str]:
    out: list[str] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(prefix):
            out.append(s.split(":", 1)[1].strip().strip('"').strip("'"))
        elif s.startswith(f"- {prefix}"):
            out.append(s.split(":", 1)[1].strip().strip('"').strip("'"))
    return out


def main() -> None:
    alerts_path = ROOT / "observability" / "alerts" / "platform-alerts.yaml"
    slo_path = ROOT / "observability" / "slo" / "platform-slos.yaml"
    runbooks_dir = ROOT / "docs" / "runbooks"

    alerts = _extract_values(alerts_path, "alert:")
    slos = _extract_values(slo_path, "name:")
    runbooks = []
    if runbooks_dir.exists():
        runbooks = sorted([p.name for p in runbooks_dir.glob("*.md")])

    report = {
        "phase": "platform_maturity",
        "timestamp": _now_iso(),
        "slos_defined": slos,
        "alerts_defined": alerts,
        "runbooks": runbooks,
        "files": {
            "platform_maturity_doc": str((ROOT / "docs" / "platform-maturity.md").as_posix()),
            "developer_guidelines": str((ROOT / "docs" / "developer-guidelines.md").as_posix()),
            "alerts_file": str(alerts_path.as_posix()),
            "slo_file": str(slo_path.as_posix()),
        },
        "dashboards": [
            "observability/grafana/dashboards/platform-slo-dashboard.json",
            "observability/grafana/dashboards/cost-signals-dashboard.json",
        ],
    }

    out_dir = ROOT / "reports" / "platform"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "platform_maturity_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

