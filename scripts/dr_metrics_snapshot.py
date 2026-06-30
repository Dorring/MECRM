import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _query(prom_url: str, promql: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{prom_url.rstrip('/')}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        return resp.json()


async def main() -> None:
    prom_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    out_dir = ROOT / "reports" / "dr"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {"phase": "dr_metrics_snapshot", "timestamp": _now_iso(), "prometheus_url": prom_url, "reachable": True, "queries": {}}
    queries = {
        "backup_duration_seconds": "backup_duration_seconds_count",
        "restore_duration_seconds": "restore_duration_seconds_count",
        "rebuild_duration_seconds": "rebuild_duration_seconds_count",
        "recovery_success_total": "recovery_success_total",
        "recovery_failure_total": "recovery_failure_total",
    }
    try:
        for name, q in queries.items():
            payload["queries"][name] = await _query(prom_url, q)
    except Exception as e:
        payload["reachable"] = False
        payload["error"] = str(e)

    (out_dir / "metrics_snapshot.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"reachable": payload["reachable"], "output": str((out_dir / 'metrics_snapshot.json'))}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

