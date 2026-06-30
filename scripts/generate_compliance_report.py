import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx


ROOT = Path(__file__).resolve().parents[1]

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"(?<![0-9a-fA-F])[+()]?\d[\d\s()/-]{8,}\d(?![0-9a-fA-F])")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scan(text: str) -> dict:
    emails = sorted(set(EMAIL_RE.findall(text)))
    phones: set[str] = set()
    for cand in PHONE_CANDIDATE_RE.findall(text):
        if "." in cand:
            continue
        if ISO_DATE_RE.search(cand):
            continue
        if UUID_RE.search(cand):
            continue
        digits = sum(1 for ch in cand if ch.isdigit())
        if 10 <= digits <= 15:
            phones.add(cand)
    return {"emails": emails, "phones": sorted(phones)}


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


async def _fetch_metrics(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            scan = _scan(resp.text)
            return {"url": url, "reachable": True, "pii": scan}
    except Exception as e:
        return {"url": url, "reachable": False, "error": str(e)}


async def _audit_pii_scan(*, database_url: str) -> dict:
    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tenant_id::text AS tenant_id, action, new_value::text AS new_value FROM audit_logs ORDER BY created_at DESC LIMIT 500"
            )
        violations: list[dict] = []
        for r in rows:
            scan = _scan(r["new_value"] or "")
            if scan["emails"] or scan["phones"]:
                violations.append({"tenant_id": r["tenant_id"], "action": r["action"], "pii": scan})
        return {"rows_scanned": len(rows), "violations": violations}
    finally:
        await pool.close()


async def main() -> None:
    reports_dir = ROOT / "reports" / "compliance"
    gdpr_forget = _read_json(reports_dir / "gdpr_forget.json")
    data_export = _read_json(reports_dir / "data_export.json")
    retention = _read_json(reports_dir / "retention_policy.json")

    database_url = os.environ.get("DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm")

    metrics_gateway = await _fetch_metrics(os.environ.get("GATEWAY_METRICS_URL", "http://localhost:4000/metrics"))
    metrics_opa = await _fetch_metrics(os.environ.get("OPA_METRICS_URL", "http://localhost:8181/metrics"))
    audit_scan = await _audit_pii_scan(database_url=database_url)

    artifact_scan = {
        "gdpr_forget.json": _scan(json.dumps(gdpr_forget or {}, ensure_ascii=False, default=str)),
        "data_export.json": _scan(json.dumps(data_export or {}, ensure_ascii=False, default=str)),
        "retention_policy.json": _scan(json.dumps(retention or {}, ensure_ascii=False, default=str)),
    }

    report = {
        "phase": "compliance_phase6",
        "timestamp": _now_iso(),
        "artifacts_present": {
            "gdpr_forget": gdpr_forget is not None,
            "data_export": data_export is not None,
            "retention_policy": retention is not None,
        },
        "artifacts": {"gdpr_forget": gdpr_forget, "data_export": data_export, "retention_policy": retention},
        "pii_checks": {
            "metrics_gateway": metrics_gateway,
            "metrics_opa": metrics_opa,
            "audit_logs_new_value_scan": audit_scan,
            "artifact_scan": artifact_scan,
        },
        "status": "pass"
        if all(v.get("emails") == [] and v.get("phones") == [] for v in artifact_scan.values())
        and audit_scan.get("violations") == []
        else "needs_review",
    }

    out_path = reports_dir / "compliance_report.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out_path), "status": report["status"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
