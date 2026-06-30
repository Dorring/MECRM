import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compose(args: list[str], *, timeout: int = 600) -> None:
    import subprocess

    subprocess.run(["docker", "compose", *args], cwd=str(ROOT), check=True, timeout=timeout)


async def _prom_get(prom_url: str, path: str, *, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{prom_url.rstrip('/')}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def main() -> None:
    prom_url = "http://localhost:9090"
    out_dir = ROOT / "reports" / "platform"
    out_dir.mkdir(parents=True, exist_ok=True)

    _compose(["up", "-d", "prometheus", "gateway", "postgres", "redis", "opa"], timeout=900)

    time.sleep(20)

    rules = await _prom_get(prom_url, "/api/v1/rules")
    (out_dir / "alerts_loaded.json").write_text(json.dumps({"timestamp": _now_iso(), "rules": rules}, indent=2) + "\n", encoding="utf-8")

    up_before = await _prom_get(prom_url, "/api/v1/query", params={"query": 'up{job="gateway"}'})
    _compose(["stop", "gateway"], timeout=300)
    time.sleep(20)
    up_after = await _prom_get(prom_url, "/api/v1/query", params={"query": 'up{job="gateway"}'})
    _compose(["start", "gateway"], timeout=300)

    simulation = {
        "timestamp": _now_iso(),
        "simulation": "GatewayDown",
        "prometheus_url": prom_url,
        "up_before": up_before,
        "up_after": up_after,
    }
    (out_dir / "alert_simulation.json").write_text(json.dumps(simulation, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"alerts_loaded": str(out_dir / "alerts_loaded.json"), "alert_simulation": str(out_dir / "alert_simulation.json")}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

