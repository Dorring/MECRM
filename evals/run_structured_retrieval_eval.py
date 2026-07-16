"""Run the real Postgres/RLS-backed structured-retrieval evaluation.

The runner creates a small synthetic corpus in two ephemeral tenants, queries
the production HybridRetriever structured path with the runtime database role,
calculates retrieval metrics, writes a JSON report, and removes the corpus.
It deliberately does not call Weaviate or Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SRC = ROOT / "agents" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from intelligence.search.retriever import HybridRetriever  # noqa: E402

from evals.structured_retrieval import (  # noqa: E402
    EvalRecord,
    load_cases,
    load_records,
    score_case,
    summarise,
)


EVALUATOR_VERSION = "h2-structured-retrieval-v1"
THRESHOLDS = {
    "recall_at_5": 1.0,
    "precision_at_5": 0.95,
    "case_pass_rate": 1.0,
    "cross_tenant_denial_pass_rate": 1.0,
    "tenant_leak_count": 0,
}


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def _create_tenants(conn: asyncpg.Connection, tenant_keys: set[str]) -> dict[str, str]:
    tenant_ids: dict[str, str] = {}
    for key in sorted(tenant_keys):
        tenant_id = str(uuid4())
        suffix = tenant_id.split("-", 1)[0]
        await conn.execute(
            """
            INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
            VALUES ($1::uuid, $2, $3, 'active', NOW(), NOW())
            """,
            tenant_id,
            f"Evaluation {key.title()} {suffix}",
            f"eval-{key}-{suffix}",
        )
        tenant_ids[key] = tenant_id
    return tenant_ids


async def _insert_record(conn: asyncpg.Connection, *, tenant_id: str, record: EvalRecord) -> str:
    record_uuid = str(uuid4())
    fields = record.fields
    await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id)
    if record.entity_type == "lead":
        await conn.execute(
            """
            INSERT INTO leads (id, tenant_id, name, email, company, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, NOW(), NOW())
            """,
            record_uuid, tenant_id, fields["name"], fields["email"], fields["company"], fields["status"],
        )
    elif record.entity_type == "deal":
        await conn.execute(
            """
            INSERT INTO deals (id, tenant_id, name, stage, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, NOW(), NOW())
            """,
            record_uuid, tenant_id, fields["name"], fields["stage"],
        )
    elif record.entity_type == "ticket":
        await conn.execute(
            """
            INSERT INTO tickets (id, tenant_id, subject, description, status, priority, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, NOW(), NOW())
            """,
            record_uuid, tenant_id, fields["subject"], fields["description"], fields["status"], fields["priority"],
        )
    elif record.entity_type == "customer":
        await conn.execute(
            """
            INSERT INTO customers (id, tenant_id, name, email, company, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, NOW(), NOW())
            """,
            record_uuid, tenant_id, fields["name"], fields["email"], fields["company"], fields["status"],
        )
    else:  # Dataset validation rejects this before the runner starts.
        raise ValueError(f"unsupported entity type: {record.entity_type}")
    return record_uuid


async def _seed_corpus(
    admin_database_url: str, records: list[EvalRecord]
) -> tuple[dict[str, str], dict[str, str]]:
    conn = await asyncpg.connect(admin_database_url)
    try:
        tenant_ids = await _create_tenants(conn, {record.tenant for record in records})
        record_ids: dict[str, str] = {}
        for record in records:
            async with conn.transaction():
                record_ids[record.record_id] = await _insert_record(
                    conn, tenant_id=tenant_ids[record.tenant], record=record
                )
        return tenant_ids, record_ids
    finally:
        await conn.close()


async def _cleanup_corpus(admin_database_url: str, tenant_ids: dict[str, str]) -> None:
    if not tenant_ids:
        return
    conn = await asyncpg.connect(admin_database_url)
    try:
        for tenant_id in tenant_ids.values():
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id)
                for table in ("leads", "deals", "tickets", "customers"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id=$1::uuid", tenant_id)
                await conn.execute("DELETE FROM tenants WHERE id=$1::uuid", tenant_id)
    finally:
        await conn.close()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    corpus_path = Path(args.corpus).resolve()
    cases_path = Path(args.cases).resolve()
    records = load_records(corpus_path)
    cases = load_cases(cases_path, {record.record_id for record in records})
    tenant_ids: dict[str, str] = {}
    retriever = HybridRetriever(
        database_url=args.database_url,
        weaviate_url="http://unused.invalid",
        ollama_url="http://unused.invalid",
        embedding_model="unused",
    )
    try:
        tenant_ids, record_ids = await _seed_corpus(args.admin_database_url, records)
        reverse_record_ids = {value: key for key, value in record_ids.items()}
        scores = []
        tenant_leak_count = 0
        case_details: list[dict[str, Any]] = []
        await retriever.start()
        for case in cases:
            results = await retriever.structured_search(
                tenant_id=tenant_ids[case.tenant],
                query=case.query,
                entity=case.entity_type,
                filters=None,
                limit=case.limit,
            )
            returned_ids = [reverse_record_ids.get(result.entity_id, f"unknown:{result.entity_id}") for result in results]
            leaked = [result.entity_id for result in results if result.tenant_id != tenant_ids[case.tenant]]
            tenant_leak_count += len(leaked)
            score = score_case(case, returned_ids)
            scores.append(score)
            case_details.append(
                {
                    "case_id": score.case_id,
                    "tenant": case.tenant,
                    "query": case.query,
                    "entity_type": case.entity_type,
                    "expected_record_ids": list(score.expected_record_ids),
                    "returned_record_ids": list(score.returned_record_ids),
                    "recall_at_k": score.recall_at_k,
                    "precision_at_k": score.precision_at_k,
                    "passed": score.passed,
                    "tenant_leak_count": len(leaked),
                }
            )
        metrics = summarise(scores, tenant_leak_count=tenant_leak_count)
        passed = (
            metrics["recall_at_5"] >= THRESHOLDS["recall_at_5"]
            and metrics["precision_at_5"] >= THRESHOLDS["precision_at_5"]
            and metrics["case_pass_rate"] >= THRESHOLDS["case_pass_rate"]
            and metrics["cross_tenant_denial_pass_rate"] >= THRESHOLDS["cross_tenant_denial_pass_rate"]
            and metrics["tenant_leak_count"] == THRESHOLDS["tenant_leak_count"]
        )
        return {
            "schema_version": 1,
            "evaluator": EVALUATOR_VERSION,
            "evaluation_type": "structured_retrieval_baseline",
            "semantic_retrieval_included": False,
            "llm_quality_included": False,
            "dataset": {"corpus": corpus_path.name, "cases": cases_path.name},
            "git_commit": _git_commit(),
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "thresholds": THRESHOLDS,
            "metrics": metrics,
            "passed": passed,
            "cases": case_details,
        }
    finally:
        await retriever.close()
        await _cleanup_corpus(args.admin_database_url, tenant_ids)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        default=str(ROOT / "evals" / "datasets" / "structured_retrieval_corpus.jsonl"),
    )
    parser.add_argument(
        "--cases",
        default=str(ROOT / "evals" / "datasets" / "structured_retrieval_cases.jsonl"),
    )
    parser.add_argument("--output", required=True, help="Path for the JSON evaluation report")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--admin-database-url", default=os.getenv("ADMIN_DATABASE_URL", ""))
    args = parser.parse_args()
    args.database_url = args.database_url or _required_env("DATABASE_URL")
    args.admin_database_url = args.admin_database_url or _required_env("ADMIN_DATABASE_URL")
    return args


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = report["metrics"]
    print(
        "structured-retrieval: "
        f"recall@5={metrics['recall_at_5']:.3f} "
        f"precision@5={metrics['precision_at_5']:.3f} "
        f"tenant_leaks={metrics['tenant_leak_count']} "
        f"passed={report['passed']}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
