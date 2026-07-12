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
    """Assert advisory lock 405011 is free before/after each test."""
    try:
        lock_before = _any_lock_holder(database_url)
    except FileNotFoundError:
        yield
        return
    except Exception as exc:
        pytest.fail(f"psql error in lock-before check: {exc}")
        return
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
            f"Advisory lock 405011 held before test.\n"
            f"Lock info: {lock_before}\n"
            f"Diagnostics:\n{diag_text}"
        )
    yield
    try:
        lock_after = _any_lock_holder(database_url)
    except FileNotFoundError:
        return
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
            f"Advisory lock 405011 held after test -- runner cleanup did not release.\n"
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
        encoding="utf-8",
        errors="replace",
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
        combined_output = f"{result.stdout}\n{result.stderr}".lower()
        if "\x00" in combined_output and "w\x00s\x00l" in combined_output:
            pytest.skip("Windows WSL bash stub is present but WSL is unavailable")
        assert (
            "lock" in combined_output
            or "failed" in combined_output
            or "connection" in combined_output
            or "could not" in combined_output
            or "error" in combined_output
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
            deadline = time.monotonic() + 5
            found = False
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                if "LOCK_ACQUIRED:" in line:
                    found = True
                    break

            assert found, (
                "Runner did not print 'LOCK_ACQUIRED:<pid>' within 5s; "
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
        """Second runner must timeout while an independent holder holds the lock.

        The holder keeps a psql connection open *after* acquiring the advisory lock
        (stdin stays open, no pg_sleep).  This is how PostgreSQL session-level locks
        are meant to be held in tests -- via an idle-in-transaction connection that
        can be torn down instantly by closing stdin / pg_terminate_backend.
        """
        holder_env = os.environ.copy()
        holder_env["PGAPPNAME"] = "mecrm-test-independent-lock-holder"

        holder = subprocess.Popen(
            ["psql", db_available, "-v", "ON_ERROR_STOP=1", "-f", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=holder_env,
        )
        holder_backend_pid = None

        try:
            # Acquire the lock, then leave stdin open -- the connection stays alive
            # and holds the lock until we close stdin or kill the connection.
            holder.stdin.write("SELECT pg_advisory_lock(405011);\n")
            holder.stdin.flush()

            # Poll pg_locks until the lock appears, saving the backend PID.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                lock_info = _psql(db_available, app_name="mecrm-test-lock-scanner",
                                  sql="SELECT pid FROM pg_locks "
                                      "WHERE locktype='advisory' AND objid=405011 "
                                      "AND granted='t'")  # noqa: E501
                if lock_info.returncode == 0 and lock_info.stdout.strip():
                    holder_backend_pid = lock_info.stdout.strip()
                    break
                if holder.poll() is not None:
                    break
                time.sleep(0.25)

            assert holder_backend_pid, (
                f"Independent lock holder did not acquire lock within 15s.\n"
                f"Holder stderr: {holder.stderr.read() if holder.stderr else 'N/A'}"
            )

            # Second runner must time out within 30+5s (statement_timeout).
            second = _run_migrate(
                database_url=db_available,
                timeout=40,
            )
            assert second.returncode != 0, (
                "Second concurrent runner should have timed out "
                f"(holder pid={holder_backend_pid} holds lock), but exited 0.\n"
                f"{second.stdout}\n{second.stderr}"
            )
            assert (
                "failed to acquire advisory lock" in second.stderr
                or "failed to acquire advisory lock" in second.stdout
            ), f"Second runner did not mention lock failure:\n{second.stderr}"

        finally:
            # Graceful: send \q to the holder so it releases the lock cleanly.
            try:
                if holder.stdin and not holder.stdin.closed:
                    holder.stdin.write("\\q\n")
                    holder.stdin.close()
            except OSError:
                pass

            # If still alive, terminate its specific backend directly.
            if holder.poll() is None:
                if holder_backend_pid:
                    _psql(db_available, app_name="mecrm-test-cleanup",
                          sql=f"SELECT pg_terminate_backend({holder_backend_pid})")
                holder.kill()
            holder.wait(timeout=5)

            # Verify the advisory lock is released after holder cleanup.
            lock_diag = _any_lock_holder(db_available)
            if lock_diag:
                diag = _psql(db_available, app_name="mecrm-test-diag",
                             sql="SELECT a.pid, a.application_name, a.state, l.granted "
                                 "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                                 "WHERE l.locktype = 'advisory' AND l.objid = 405011")
                pytest.fail(
                    f"Advisory lock 405011 still held after holder cleanup.\n"
                    f"Lock info: {lock_diag}\n"
                    f"Diagnostics:\n{diag.stdout if diag.returncode == 0 else diag.stderr}"
                )

    def test_runner_succeeds_after_lock_released(self, db_available: str):
        """After a lock holder exits, a new runner should succeed."""
        # The autouse fixture asserts no leaked lock.
        third = _run_migrate(database_url=db_available, timeout=180)
        assert third.returncode == 0, (
            f"Third runner failed after lock released:\n{third.stdout}\n{third.stderr}"
        )
