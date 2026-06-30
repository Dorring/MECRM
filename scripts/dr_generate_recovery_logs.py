import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    out_dir = ROOT / "reports" / "dr"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "recovery_logs.txt"

    proc = subprocess.run(
        ["python", str(ROOT / "scripts" / "dr_run_full_recovery.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    log_path.write_text(
        f"timestamp={_now_iso()}\nexit_code={proc.returncode}\n\nSTDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n",
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()

