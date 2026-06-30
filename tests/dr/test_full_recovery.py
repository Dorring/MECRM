import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from dr.backup_service import BackupService
from dr.object_store import LocalObjectStore
from dr.restore_service import RestoreService


def _compose(args: list[str], *, timeout: int = 300) -> None:
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


def _db_urls(db: str) -> tuple[str, str]:
    user = os.environ.get("POSTGRES_USER", "crm_user")
    pw = os.environ.get("POSTGRES_PASSWORD", "crm_password")
    host_url = f"postgresql://{user}:{pw}@localhost:5432/{db}"
    container_url = f"postgresql://{user}:{pw}@postgres:5432/{db}"
    return host_url, container_url


async def _seed(db_url: str) -> dict[str, str]:
    tenant_id = str(uuid4())
    lead_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    conn = await asyncpg.connect(dsn=db_url)
    try:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, settings, status, created_at, updated_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)",
            tenant_id,
            f"tenant-{tenant_id}",
            f"tenant-{tenant_id}",
            "{}",
            "active",
            now,
            now,
        )
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
        await conn.execute(
            "INSERT INTO event_streams (tenant_id, stream_id, current_version) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            tenant_id,
            f"lead:{lead_id}",
            2,
        )
        await conn.execute(
            "INSERT INTO events (id, tenant_id, stream_id, version, event_id, event_type, payload, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
            str(uuid4()),
            tenant_id,
            f"lead:{lead_id}",
            1,
            str(uuid4()),
            "lead.created",
            '{"leadId":"' + lead_id + '","name":"DR Lead","status":"new"}',
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
            '{"leadId":"' + lead_id + '","changes":{"score":10,"status":"qualified"}}',
            now,
        )
        await conn.execute(
            "UPDATE event_streams SET current_version=2, updated_at=now() WHERE tenant_id=$1 AND stream_id=$2",
            tenant_id,
            f"lead:{lead_id}",
        )
    finally:
        await conn.close()
    return {"tenant_id": tenant_id, "lead_id": lead_id}


@pytest.mark.asyncio
async def test_full_recovery_db_wiped_and_rebuilt(tmp_path: Path):
    _compose(["up", "-d", "postgres", "redis", "opa"], timeout=600)

    dr_db = f"dr_test_{uuid4().hex[:10]}"
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"CREATE DATABASE {dr_db};")

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

    host_url, container_url = _db_urls(dr_db)
    store = LocalObjectStore(base_dir=tmp_path / "obj")

    pool = await asyncpg.create_pool(dsn=host_url, min_size=1, max_size=3)
    try:
        seed = await _seed(host_url)

        backup = BackupService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))
        restore = RestoreService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))

        b = await backup.create_db_backup(database_url=container_url)
        await backup.create_snapshot_backup(backup_id=b.backup_id)

        _psql(dr_db, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

        r = await restore.full_restore(backup_id=b.backup_id, database_url=container_url, restore_snapshots=True, rebuild_read_models=True)
        assert r.integrity["tenants"][seed["tenant_id"]]["events"] == 2
        assert r.integrity["tenants"][seed["tenant_id"]]["lead_read_model"] == 1

        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", seed["tenant_id"])
            row = await conn.fetchrow("SELECT status, score, version FROM lead_read_model WHERE tenant_id=$1 AND lead_id=$2", seed["tenant_id"], seed["lead_id"])
            assert row is not None
            assert row["status"] == "qualified"
            assert int(row["score"] or 0) == 10
            assert int(row["version"] or 0) == 2
    finally:
        await pool.close()
        _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")


@pytest.mark.asyncio
async def test_rebuild_when_read_models_wiped(tmp_path: Path):
    _compose(["up", "-d", "postgres", "redis", "opa"], timeout=600)

    dr_db = f"dr_test_{uuid4().hex[:10]}"
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"CREATE DATABASE {dr_db};")

    for p in [
        ROOT / "gateway" / "prisma" / "migrations" / "20260106121404_init" / "migration.sql",
        ROOT / "gateway" / "prisma" / "migrations" / "20260123100000_cqrs_outbox" / "migration.sql",
        ROOT / "database" / "migrations" / "06-event-store.sql",
        ROOT / "database" / "migrations" / "08-read-models.sql",
        ROOT / "database" / "migrations" / "02-rls-policies.sql",
    ]:
        _apply_sql_file(dr_db, p)

    host_url, container_url = _db_urls(dr_db)
    store = LocalObjectStore(base_dir=tmp_path / "obj")
    pool = await asyncpg.create_pool(dsn=host_url, min_size=1, max_size=3)
    try:
        seed = await _seed(host_url)
        backup = BackupService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))
        restore = RestoreService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))

        b = await backup.create_db_backup(database_url=container_url)
        _psql(dr_db, "TRUNCATE lead_read_model;")
        await restore.restore_database(backup_id=b.backup_id, database_url=container_url)

        _psql(dr_db, "TRUNCATE lead_read_model;")
        t0 = time.perf_counter()
        await restore.rebuild_read_models()
        assert (time.perf_counter() - t0) < 300

        integrity = await restore.validate_integrity()
        assert integrity["tenants"][seed["tenant_id"]]["lead_read_model"] == 1
    finally:
        await pool.close()
        _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")


@pytest.mark.asyncio
async def test_missing_snapshot_falls_back_to_rebuild(tmp_path: Path):
    _compose(["up", "-d", "postgres", "redis", "opa"], timeout=600)

    dr_db = f"dr_test_{uuid4().hex[:10]}"
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")
    _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"CREATE DATABASE {dr_db};")

    for p in [
        ROOT / "gateway" / "prisma" / "migrations" / "20260106121404_init" / "migration.sql",
        ROOT / "gateway" / "prisma" / "migrations" / "20260123100000_cqrs_outbox" / "migration.sql",
        ROOT / "database" / "migrations" / "06-event-store.sql",
        ROOT / "database" / "migrations" / "08-read-models.sql",
        ROOT / "database" / "migrations" / "02-rls-policies.sql",
    ]:
        _apply_sql_file(dr_db, p)

    host_url, container_url = _db_urls(dr_db)
    store = LocalObjectStore(base_dir=tmp_path / "obj")
    pool = await asyncpg.create_pool(dsn=host_url, min_size=1, max_size=3)
    try:
        seed = await _seed(host_url)
        backup = BackupService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))
        restore = RestoreService(pool=pool, object_store=store, docker_compose_project_dir=str(ROOT))

        b = await backup.create_db_backup(database_url=container_url)
        _psql(dr_db, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

        r = await restore.full_restore(backup_id=b.backup_id, database_url=container_url, restore_snapshots=True, rebuild_read_models=True)
        assert r.integrity["tenants"][seed["tenant_id"]]["lead_read_model"] == 1
    finally:
        await pool.close()
        _psql(os.environ.get("POSTGRES_DB", "enterprise_crm"), f"DROP DATABASE IF EXISTS {dr_db};")
