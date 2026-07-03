"""Integration/regression tests for the single migration runner.

These tests exercise the actual `scripts/migrate.sh` against a real PostgreSQL
instance. They are skipped when no database is available, so they run in CI
(where a postgres service is provided) but do not fail local developer machines
that lack Docker/PostgreSQL.

Coverage:
  - compose expand shows migrate POSTGRES_HOST=postgres
  - runner fails within a bounded time on invalid DB host (no hang)
  - empty DB migration succeeds
  - repeat migration is idempotent
  - drift-only exits 0 on a fully migrated DB
  - concurrent runners: second times out while first holds lock; succeeds after
    first releases the lock
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SH = REPO_ROOT / "scripts" / "migrate.sh"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


def _default_db_url() -> str:
    user = os.environ.get("POSTGRES_USER", "crm_user")
    password = os.environ.get("POSTGRES_PASSWORD", "crm_password")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "enterprise_crm")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("DATABASE_URL") or _default_db_url()


@pytest.fixture(scope="module")
def db_available(database_url: str):
    """Skip tests unless PostgreSQL is reachable."""
    try:
        subprocess.run(
            ["psql", database_url, "-c", "SELECT 1"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not available ({exc})")
    return database_url


def _psql(database_url: str, *, app_name: str = "mecrm-test", sql: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PGAPPNAME"] = app_name
    return subprocess.run(
        ["psql", database_url, "-At", "-c", sql],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _any_lock_holder(database_url: str) -> str:
    """Return diagnostic text if advisory lock 405011 is held; empty string if free."""
    result = _psql(
        database_url,
        app_name="mecrm-test-lock-inspector",
        sql="SELECT a.pid || ':' || a.application_name || ':' || a.state "
            "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.objid = 405011",
    )
    return result.stdout.strip() if result.returncode == 0 else ""


@pytest.fixture(autouse=True)
def ensure_no_leaked_lock(database_url: str):
    """Function-scoped autouse fixture: assert advisory lock 405011 is free before/after test.

    If psql is unavailable (no PostgreSQL client installed), silently passes.
    """
    try:
        lock_before = _any_lock_holder(database_url)
    except Exception:
        return  # psql not available; skip lock check
    if lock_before:
        try:
            diag = _psql(database_url, app_name="mecrm-test-diag",
                         sql="SELECT a.pid, a.application_name, a.state, a.query, l.granted "
                             "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                             "WHERE l.locktype = 'advisory' AND l.objid = 405011")
            diag_text = diag.stdout if diag.returncode == 0 else diag.stderr
        except Exception as e:
            diag_text = str(e)
        pytest.fail(
            f"Advisory lock 405011 held before test — previous test leaked a lock holder.\n"
            f"Lock info: {lock_before}\n"
            f"Diagnostics:\n{diag_text}"
        )
    yield
    try:
        lock_after = _any_lock_holder(database_url)
    except Exception:
        return
    if lock_after:
        try:
            diag = _psql(database_url, app_name="mecrm-test-diag",
                         sql="SELECT a.pid, a.application_name, a.state, a.query, l.granted "
                             "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                             "WHERE l.locktype = 'advisory' AND l.objid = 405011")
            diag_text = diag.stdout if diag.returncode == 0 else diag.stderr
        except Exception as e:
            diag_text = str(e)
        pytest.fail(
            f"Advisory lock 405011 held after test — runner cleanup did not release.\n"
            f"Lock info: {lock_after}\n"
            f"Diagnostics:\n{diag_text}"
        )


def _run_migrate(
    *,
    database_url: str,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run the migration runner with the given env/args and a hard timeout."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    # Derive POSTGRES_* from URL so the script and any psql invocation are consistent.
    # (The script can also parse DATABASE_URL, but explicit vars remove ambiguity.)
    env.setdefault("POSTGRES_USER", os.environ.get("POSTGRES_USER", "crm_user"))
    env.setdefault("POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "crm_password"))
    env.setdefault("POSTGRES_HOST", os.environ.get("POSTGRES_HOST", "localhost"))
    env.setdefault("POSTGRES_PORT", os.environ.get("POSTGRES_PORT", "5432"))
    env.setdefault("POSTGRES_DB", os.environ.get("POSTGRES_DB", "enterprise_crm"))
    env.setdefault("GATEWAY_DIR", str(REPO_ROOT / "gateway"))
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(MIGRATE_SH)] + (args or [])
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestComposeMigrateEnvironment:
    """Regression: compose expand must expose POSTGRES_HOST=postgres for migrate."""

    def test_migrate_postgres_host_literal(self):
        with open(COMPOSE_PATH, "r", encoding="utf-8") as f:
            compose = yaml.safe_load(f)
        env = compose["services"]["migrate"].get("environment") or []
        joined = " ".join(str(e) for e in env)
        assert "POSTGRES_HOST=postgres" in joined, (
            "migrate service must explicitly set POSTGRES_HOST=postgres so the runner "
            "connects to the in-stack postgres service, not localhost"
        )


class TestRunnerFailureModes:
    """Runner must fail safely and quickly when the database is unreachable."""

    def test_runner_fails_fast_on_invalid_host(self, database_url: str):
        """With an invalid host the runner must exit non-zero well before the test timeout."""
        env = {
            "POSTGRES_HOST": "invalid-host-that-does-not-exist.example",
            "PGCONNECT_TIMEOUT": "5",
        }
        start = time.monotonic()
        result = _run_migrate(database_url=database_url, extra_env=env, timeout=60)
        elapsed = time.monotonic() - start

        assert result.returncode != 0, (
            "Expected runner to fail on invalid host, but it exited 0.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert elapsed < 60, f"Runner hung for {elapsed:.1f}s on invalid host"
        assert (
            "lock" in result.stderr.lower()
            or "failed" in result.stderr.lower()
            or "connection" in result.stderr.lower()
        )


@pytest.mark.slow
class TestRunnerWithDatabase:
    """Real-database integration tests for the migration runner."""

    def test_lock_acquisition_printed_within_5_seconds(self, db_available: str):
        """With a real database the runner must emit the lock-hold message within 5s."""
        env = os.environ.copy()
        env["DATABASE_URL"] = db_available
        env.setdefault("POSTGRES_USER", os.environ.get("POSTGRES_USER", "crm_user"))
        env.setdefault("POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "crm_password"))
        env.setdefault("POSTGRES_HOST", os.environ.get("POSTGRES_HOST", "localhost"))
        env.setdefault("POSTGRES_PORT", os.environ.get("POSTGRES_PORT", "5432"))
        env.setdefault("POSTGRES_DB", os.environ.get("POSTGRES_DB", "enterprise_crm"))
        env.setdefault("GATEWAY_DIR", str(REPO_ROOT / "gateway"))

        proc = subprocess.Popen(
            ["bash", str(MIGRATE_SH)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            start = time.monotonic()
            found = False
            while time.monotonic() - start < 5:
                line = proc.stdout.readline()
                if not line:
                    break
                if "advisory lock acquired and held" in line:
                    found = True
                    break

            assert found, (
                "Runner did not print 'advisory lock acquired and held' within 5s; "
                "the lock marker may be hidden by batched psql output."
            )

            proc.wait(timeout=180)
            assert proc.returncode == 0, (
                f"Migration failed after lock acquisition:\n{proc.stdout.read()}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_empty_database_migration(self, db_available: str):
        result = _run_migrate(database_url=db_available, timeout=180)
        assert result.returncode == 0, (
            f"Empty-database migration failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "Migration complete" in result.stdout

    def test_repeat_migration_is_idempotent(self, db_available: str):
        # First run already exercised by the previous test; do it again.
        first = _run_migrate(database_url=db_available, timeout=180)
        assert first.returncode == 0, (
            f"First migration failed:\nstdout:\n{first.stdout}\nstderr:\n{first.stderr}"
        )
        second = _run_migrate(database_url=db_available, timeout=180)
        assert second.returncode == 0, (
            f"Repeat migration was not idempotent:\nstdout:\n{second.stdout}\n"
            f"stderr:\n{second.stderr}"
        )
        assert "Migration complete" in second.stdout

    def test_drift_only_passes(self, db_available: str):
        result = _run_migrate(
            database_url=db_available, args=["--drift-only"], timeout=120
        )
        assert result.returncode == 0, (
            f"drift-only failed on a fully migrated DB:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "all tenant tables have ENABLE+FORCE+ALL policy" in result.stdout

    def test_concurrent_runner_times_out(self, db_available: str):
        """Second runner must timeout while first holds lock; third succeeds after first exits."""
        hold_env = {"MIGRATE_LOCK_HOLD_SECONDS": "10"}

        first_env = os.environ.copy()
        first_env["DATABASE_URL"] = db_available
        first_env["MIGRATE_LOCK_HOLD_SECONDS"] = "10"
        first_env.setdefault("POSTGRES_USER", os.environ.get("POSTGRES_USER", "crm_user"))
        first_env.setdefault("POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "crm_password"))
        first_env.setdefault("POSTGRES_HOST", os.environ.get("POSTGRES_HOST", "localhost"))
        first_env.setdefault("POSTGRES_PORT", os.environ.get("POSTGRES_PORT", "5432"))
        first_env.setdefault("POSTGRES_DB", os.environ.get("POSTGRES_DB", "enterprise_crm"))
        first_env.setdefault("GATEWAY_DIR", str(REPO_ROOT / "gateway"))

        first = subprocess.Popen(
            ["bash", str(MIGRATE_SH)],
            cwd=str(REPO_ROOT),
            env=first_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            # Wait for the first runner to acquire the lock (print LOCK_ACQUIRED:<pid>).
            deadline = time.monotonic() + 30
            marker_found = False
            pid_extracted = None
            while time.monotonic() < deadline:
                line = first.stdout.readline()
                if not line:
                    break
                m = re.search(r"LOCK_ACQUIRED:(\d+)", line)
                if m:
                    marker_found = True
                    pid_extracted = m.group(1)
                    break
            assert marker_found, (
                f"First runner did not emit LOCK_ACQUIRED:<pid> within 30s.\n"
                f"Remaining stdout:\n{first.stdout.read()}"
            )
            print(f"First runner acquired lock (backend pid={pid_extracted}), starting second")

            second = _run_migrate(
                database_url=db_available,
                extra_env=hold_env,
                timeout=45,
            )
            assert second.returncode != 0, (
                "Second concurrent runner should have timed out, but it exited 0.\n"
                f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
            )
            assert (
                "failed to acquire advisory lock" in second.stderr
                or "failed to acquire advisory lock" in second.stdout
            ), f"Second runner error did not mention lock failure:\n{second.stderr}"
        finally:
            first.wait(timeout=30)

        assert first.returncode == 0, (
            f"First runner failed unexpectedly:\nstdout:\n{first.stdout.read()}\n"
            f"stderr:\n{first.stderr.read()}"
        )

        # After the first runner released the lock, a third runner should succeed.
        third = _run_migrate(
            database_url=db_available,
            extra_env=hold_env,
            timeout=180,
        )
        assert third.returncode == 0, (
            f"Third runner failed after first released the lock:\n"
            f"stdout:\n{third.stdout}\nstderr:\n{third.stderr}"
        )
