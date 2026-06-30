import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "certification"

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"(?<![0-9a-fA-F])[+()]?\d[\d\s()/-]{8,}\d(?![0-9a-fA-F])")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scan(text: str) -> dict[str, list[str]]:
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


def _run(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    return {"cmd": " ".join(cmd), "exit_code": proc.returncode, "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


async def _export_audit_logs(*, database_url: str, limit: int = 2000) -> dict[str, Any]:
    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT created_at, tenant_id::text AS tenant_id, action, resource_type, resource_id::text AS resource_id,
                       actor_type, actor_id::text AS actor_id, new_value::text AS new_value
                FROM audit_logs
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        export_rows: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        for r in rows:
            new_value = r["new_value"] or ""
            scan = _scan(new_value)
            if scan["emails"] or scan["phones"]:
                violations.append({"tenant_id": r["tenant_id"], "action": r["action"], "pii": scan})
                for e in scan["emails"]:
                    new_value = new_value.replace(e, "[REDACTED]")
                for p in scan["phones"]:
                    new_value = new_value.replace(p, "[REDACTED]")
            export_rows.append(
                {
                    "created_at": str(r["created_at"]),
                    "tenant_id": r["tenant_id"],
                    "action": r["action"],
                    "resource_type": r["resource_type"],
                    "resource_id": r["resource_id"],
                    "actor_type": r["actor_type"],
                    "actor_id": r["actor_id"],
                    "new_value": new_value,
                }
            )
        return {"rows": export_rows, "violations": violations}
    finally:
        await pool.close()


async def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    database_url = os.environ.get("DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm")

    steps: list[dict[str, Any]] = []
    steps.append(_run(["python", "-m", "pytest", "tests/test_gdpr_forget.py", "-v"]))
    steps.append(_run(["python", "-m", "pytest", "tests/test_data_export.py", "-v"]))
    steps.append(_run(["python", "-m", "pytest", "tests/test_retention_policy.py", "-v"]))
    steps.append(_run(["python", "-m", "pytest", "tests/test_ai_data_guard.py", "-v"]))
    steps.append(_run(["python", "scripts/apply_retention_policies.py"]))
    steps.append(_run(["python", "scripts/generate_compliance_report.py"]))

    steps.append(_run(["python", "-m", "pytest", "tests/test_cache_isolation.py", "-v"]))
    steps.append(_run(["python", "-m", "pytest", "tests/test_cache_policy_invalidation.py", "-v"]))
    steps.append(_run(["python", "-m", "pytest", "tests/test_cache_security_report.py", "-v"]))
    steps.append(_run(["python", "scripts/cache_benchmark.py"]))

    steps.append(_run(["python", "-m", "pytest", "tests/dr/test_full_recovery.py", "-v"]))
    steps.append(_run(["python", "scripts/dr_generate_recovery_logs.py"]))
    steps.append(_run(["python", "scripts/dr_metrics_snapshot.py"]))

    steps.append(_run(["python", "scripts/platform_maturity_report.py"]))
    steps.append(_run(["python", "scripts/platform_alert_proof.py"]))

    audit_export = await _export_audit_logs(database_url=database_url)
    (REPORTS / "audit_log_export.json").write_text(json.dumps(audit_export, indent=2) + "\n", encoding="utf-8")

    evidence_paths = [
        ROOT / "reports" / "compliance" / "compliance_report.json",
        ROOT / "reports" / "cache" / "cache_security_report.json",
        ROOT / "reports" / "cache" / "perf_report.json",
        ROOT / "reports" / "dr" / "rpo_rto_report.json",
        ROOT / "reports" / "dr" / "full_recovery.json",
        ROOT / "reports" / "dr" / "recovery_logs.txt",
        ROOT / "reports" / "dr" / "metrics_snapshot.json",
        ROOT / "reports" / "platform" / "platform_maturity_report.json",
        ROOT / "reports" / "platform" / "alerts_loaded.json",
        ROOT / "reports" / "platform" / "alert_simulation.json",
    ]
    missing = [str(p) for p in evidence_paths if not p.exists()]

    all_ok = all(s["exit_code"] == 0 for s in steps) and not missing and not audit_export["violations"]
    report = {
        "phase": "final_certification",
        "timestamp": _now_iso(),
        "status": "PRODUCTION-READY" if all_ok else "NOT READY",
        "blockers": ([] if all_ok else {"missing_artifacts": missing, "failed_steps": [s for s in steps if s["exit_code"] != 0], "audit_pii_violations": audit_export["violations"]}),
        "steps": steps,
        "evidence": [str(p) for p in evidence_paths],
    }
    (REPORTS / "final_certification_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(REPORTS / "final_certification_report.json")}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

