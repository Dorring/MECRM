import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _compose(args: list[str], *, timeout: int = 300) -> None:
    cmd = ["docker", "compose", *args]
    subprocess.run(cmd, cwd=str(ROOT), check=True, timeout=timeout)


def _psql(sql: str, *, timeout: int = 120) -> None:
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
        os.environ.get("POSTGRES_DB", "enterprise_crm"),
        "-v",
        "ON_ERROR_STOP=1",
    ]
    subprocess.run(cmd, cwd=str(ROOT), input=sql.encode("utf-8"), check=True, timeout=timeout)


def _psql_scalar(sql: str, *, timeout: int = 60) -> str:
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
        os.environ.get("POSTGRES_DB", "enterprise_crm"),
        "-tA",
        "-c",
        sql,
    ]
    out = subprocess.check_output(cmd, cwd=str(ROOT), timeout=timeout)
    return out.decode("utf-8", errors="replace").strip()


def _apply_sql_file(path: Path) -> None:
    _psql(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm")

@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="session")
def opa_url() -> str:
    return os.environ.get("OPA_URL", "http://localhost:8181")


@pytest.fixture(scope="session", autouse=True)
def _infra_ready() -> None:
    _compose(["up", "-d", "postgres", "redis", "opa"], timeout=600)

    tenants_regclass = _psql_scalar("SELECT to_regclass('public.tenants')")
    if not tenants_regclass:
        prisma_migrations = [
            ROOT / "gateway" / "prisma" / "migrations" / "20260106121404_init" / "migration.sql",
            ROOT / "gateway" / "prisma" / "migrations" / "20260123100000_cqrs_outbox" / "migration.sql",
            ROOT / "gateway" / "prisma" / "migrations" / "20260124120000_agent_decisions" / "migration.sql",
            ROOT / "gateway" / "prisma" / "migrations" / "20260124190000_data_governance" / "migration.sql",
        ]
        for p in prisma_migrations:
            _apply_sql_file(p)

    _apply_sql_file(ROOT / "database" / "migrations" / "10-data-governance.sql")
    _apply_sql_file(ROOT / "database" / "migrations" / "02-rls-policies.sql")
