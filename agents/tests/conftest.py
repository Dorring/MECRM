import sys
import os
import socket
from pathlib import Path
from urllib.parse import urlparse
import asyncpg
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_ROOT = ROOT.parent
# Note: core_services/src path is NOT added here because it contains a conflicting
# governance package. Tests that need core_services imports should add the path themselves.


DB_REQUIRED_BASENAMES = {
    "test_diff_correctness.py",
    "test_event_store.py",
    "test_idempotent_consumer.py",
    "test_intelligence_search.py",
    "test_replay_service_integration.py",
    "test_replay_tenant_isolation.py",
    "test_snapshot_store.py",
    "test_tenant_isolation.py",
}


def _parse_host_port(database_url: str) -> tuple[str, int]:
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = int(parsed.port or 5432)
    return host, port


def _tcp_reachable(host: str, port: int, timeout_s: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _db_reachable_for_tests() -> bool:
    if os.environ.get("CRM_TEST_REQUIRE_DB") == "1":
        return True

    url = os.environ.get("DATABASE_URL")
    if not url:
        url = "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    host, port = _parse_host_port(url)
    return _tcp_reachable(host, port)


def pytest_configure(config: pytest.Config):
    config._crm_db_reachable = _db_reachable_for_tests()  # type: ignore[attr-defined]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    if getattr(config, "_crm_db_reachable", True):
        return

    skip_db = pytest.mark.skip(
        reason="Postgres not reachable for DB-backed tests. Start docker-compose Postgres or set CRM_TEST_REQUIRE_DB=1."
    )
    for item in items:
        path = str(item.fspath)
        base = os.path.basename(path)
        if base in DB_REQUIRED_BASENAMES:
            item.add_marker(skip_db)
            continue
        if f"{os.sep}tests{os.sep}chaos{os.sep}" in path or f"{os.sep}tests{os.sep}integration{os.sep}" in path:
            item.add_marker(skip_db)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    return url


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    url = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"
    return url

@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def init_schema(admin_database_url: str):
    """
    Apply database migrations to the test database.
    This runs once per session.
    """
    # Create a connection to the database
    # Note: This assumes the database exists. 
    try:
        conn = await asyncpg.connect(admin_database_url)
    except Exception as e:
        print(f"Skipping schema initialization, could not connect to DB: {e}")
        return

    try:
        migrations_dir = REPO_ROOT / "database" / "migrations"
        if not migrations_dir.exists():
             print(f"Warning: Migrations directory not found at {migrations_dir}")
             return

        # Execute migrations in order
        sql_files = sorted(migrations_dir.glob("*.sql"))
        print(f"Applying {len(sql_files)} migrations from {migrations_dir}...")
        
        for sql_file in sql_files:
            print(f"Applying migration: {sql_file.name}")
            try:
                with open(sql_file, "r", encoding="utf-8") as f:
                    sql_content = f.read()
                    await conn.execute(sql_content)
            except Exception as e:
                # If table already exists, we might want to ignore or drop? 
                # For now, let's assume clean DB or idempotent scripts (if they have IF NOT EXISTS).
                # The provided scripts might not be idempotent. 
                # Given 'UndefinedTableError', the DB is likely empty or tables missing.
                # If we get 'DuplicateTableError', we can ignore.
                if "already exists" in str(e):
                    print(f"Migration {sql_file.name} might have been applied: {e}")
                else:
                    print(f"Error applying migration {sql_file.name}: {e}")
                    raise
    finally:
        await conn.close()
