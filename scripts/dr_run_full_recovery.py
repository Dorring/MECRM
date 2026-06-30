import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from dr.backup_service import BackupService
from dr.object_store import LocalObjectStore
from dr.restore_service import RestoreService


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compose(args: list[str], *, timeout: int = 600) -> None:
    subprocess.run(["docker", "compose", *args], cwd=str(ROOT), check=True, timeout=timeout)


def _psql(db: str, sql: str, *, timeout: int = 180) -> None:
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        os.environ.get("POSTGRES_USER", "crm_user"),
        "-d",
        db,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    subprocess.run(cmd, cwd=str(ROOT), input=sql.encode("utf-8"), check=True, timeout=timeout)


def _apply_sql_file(db: str, path: Path) -> None:
    _psql(db, path.read_text(encoding="utf-8"))


async def _seed_minimal(database_url: str) -> dict[str, str]:
    tenant_id = str(uuid4())
    lead_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    conn = await asyncpg.connect(dsn=database_url)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug, settings, status, created_at, updated_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)", tenant_id, f"tenant-{tenant_id}", f"tenant-{tenant_id}", "{}", "active", now, now)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
        await conn.execute("INSERT INTO event_streams (tenant_id, stream_id, current_version) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", tenant_id, f"lead:{lead_id}", 2)
        await conn.execute(
            "INSERT INTO events (id, tenant_id, stream_id, version, event_id, event_type, payload, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
            str(uuid4()),
            tenant_id,
            f"lead:{lead_id}",
            1,
            str(uuid4()),
            "lead.created",
            json.dumps({"leadId": lead_id, "name": "DR Lead", "status": "new"}),
            now,
        )
        await conn.execute(
            "INSERT INTO events (id, tenant_id, stream_id, version, event_id, event_type, payload, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
            str(uuid4()),
            tenant_id,
            f"lead:{lead_id}",
            2,
            str(uuid4()),
            "lead.updated",
            json.dumps({"leadId": lead_id, "changes": {"score": 10, "status": "qualified"}}),
            now,
        )
        await conn.execute("UPDATE event_streams SET current_version=2, updated_at=now() WHERE tenant_id=$1 AND stream_id=$2", tenant_id, f"lead:{lead_id}")
    finally:
        await conn.close()
    return {"tenant_id": tenant_id, "lead_id": lead_id}


async def main() -> None:
    out_dir = ROOT / "reports" / "dr"
    out_dir.mkdir(parents=True, exist_ok=True)

    _compose(["up", "-d", "postgres", "redis", "opa"], timeout=600)

    base_db = os.environ.get("POSTGRES_DB", "enterprise_crm")
    dr_db = f"dr_run_{uuid4().hex[:10]}"
    _psql(base_db, f"DROP DATABASE IF EXISTS {dr_db};")
    _psql(base_db, f"CREATE DATABASE {dr_db};")

    prisma_migrations = [
        ROOT / "gateway" / "prisma" / "migrations" / "20260106121404_init" / "migration.sql",
        ROOT / "gateway" / "prisma" / "migrations" / "20260123100000_cqrs_outbox" / "migration.sql",
        ROOT / "gateway" / "prisma" / "migrations" / "20260124120000_agent_decisions" / "migration.sql",
        ROOT / "gateway" / "prisma" / "migrations" / "20260124190000_data_governance" / "migration.sql",
    ]
    for p in prisma_migrations:
        _apply_sql_file(dr_db, p)

    for p in [
        ROOT / "database" / "migrations" / "03-event-log.sql",
        ROOT / "database" / "migrations" / "04-aggregate-snapshots.sql",
        ROOT / "database" / "migrations" / "05-replay-jobs.sql",
        ROOT / "database" / "migrations" / "06-event-store.sql",
        ROOT / "database" / "migrations" / "08-read-models.sql",
        ROOT / "database" / "migrations" / "02-rls-policies.sql",
    ]:
        _apply_sql_file(dr_db, p)

    user = os.environ.get("POSTGRES_USER", "crm_user")
    pw = os.environ.get("POSTGRES_PASSWORD", "crm_password")
    host_url = f"postgresql://{user}:{pw}@localhost:5432/{dr_db}"
    container_url = f"postgresql://{user}:{pw}@postgres:5432/{dr_db}"

    pool = await asyncpg.create_pool(dsn=host_url, min_size=1, max_size=3)
    store = LocalObjectStore(base_dir=ROOT / "backups")
    try:
        backup_service = BackupService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))
        restore_service = RestoreService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))

        seed = await _seed_minimal(host_url)

        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", seed["tenant_id"])
            last_event_at = await conn.fetchval("SELECT max(created_at) FROM events")

        backup_started_at = datetime.now(timezone.utc)
        db_backup = await backup_service.create_db_backup(database_url=container_url)
        await backup_service.create_snapshot_backup(backup_id=db_backup.backup_id)

        rpo_seconds = (backup_started_at - last_event_at.replace(tzinfo=timezone.utc)).total_seconds() if last_event_at else None

        failure_started_at = time.perf_counter()
        _psql(dr_db, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        report = await restore_service.full_restore(backup_id=db_backup.backup_id, database_url=container_url, restore_snapshots=True, rebuild_read_models=True)
        rto_seconds = time.perf_counter() - failure_started_at

        full_recovery = {
            "phase": "dr_full_recovery",
            "timestamp": _now_iso(),
            "backup_id": db_backup.backup_id,
            "seed": seed,
            "restore": {
                "restore_database_seconds": report.restore_database_seconds,
                "restore_snapshots_seconds": report.restore_snapshots_seconds,
                "rebuild_read_models_seconds": report.rebuild_read_models_seconds,
                "integrity": report.integrity,
            },
            "rto_seconds": round(rto_seconds, 3),
        }

        rpo_rto = {
            "phase": "rpo_rto",
            "timestamp": _now_iso(),
            "backup_id": db_backup.backup_id,
            "rpo_seconds": round(rpo_seconds, 3) if rpo_seconds is not None else None,
            "rto_seconds": round(rto_seconds, 3),
            "targets": {"rpo_seconds": 300, "rto_seconds": 1800},
            "status": "met" if (rpo_seconds is not None and rpo_seconds <= 300 and rto_seconds <= 1800) else "not_met",
        }

        (out_dir / "full_recovery.json").write_text(json.dumps(full_recovery, indent=2) + "\n", encoding="utf-8")
        (out_dir / "rpo_rto_report.json").write_text(json.dumps(rpo_rto, indent=2) + "\n", encoding="utf-8")

        print(json.dumps(rpo_rto, indent=2))
    finally:
        await pool.close()
        _psql(base_db, f"DROP DATABASE IF EXISTS {dr_db};")


if __name__ == "__main__":
    asyncio.run(main())
