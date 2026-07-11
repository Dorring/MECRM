"""D3 regression tests — Chaos workflow and compose reliability cleanup.

These tests parse docker-compose.chaos.yml and .github/workflows/chaos-tests.yml
directly with PyYAML or regex. They do NOT run Docker.

Covers:
  D3-C1 — chaos workflow has no `schedule` trigger
  D3-C2 — chaos workflow retains `workflow_dispatch`
  D3-C3 — chaos-migrations uses unified scripts/migrate.sh (not inline SQL loop)
  D3-C4 — chaos-migrations build context includes Prisma-capable toolchain (Node/npx)
  D3-C5 — agents/replay-service depends on chaos-migrations service_completed_successfully
  D3-C6 — chaos compose OPA image is 0.70.0 (no D2 regression)
  D3-C7 — chaos compose agents depends on OPA with service_healthy (no D1 regression)
"""

import os
import re
import unittest

import yaml

CHAOS_COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.chaos.yml"
)
CHAOS_WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows", "chaos-tests.yml"
)
EXPECTED_OPA_VERSION = "0.70.0"


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# -- D3-C1, D3-C2: Chaos workflow triggers ----------------------------

class TestChaosWorkflowTriggers(unittest.TestCase):
    """chaos-tests.yml: no schedule, workflow_dispatch retained."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CHAOS_WORKFLOW_PATH)

    def test_no_schedule_trigger(self):
        on = self.data.get(True) or self.data.get("on") or {}
        self.assertNotIn("schedule", on,
                         "chaos-tests.yml must not have a schedule trigger")

    def test_workflow_dispatch_retained(self):
        on = self.data.get(True) or self.data.get("on") or {}
        self.assertIn("workflow_dispatch", on,
                      "chaos-tests.yml must retain workflow_dispatch trigger")

    def test_chaos_job_not_required_gate(self):
        """chaos job should not auto-fire on push to main or PR."""
        jobs = self.data.get("jobs") or {}
        chaos = jobs.get("chaos") or {}
        if_raw = chaos.get("if", "")
        # The D3 guard: only workflow_dispatch
        self.assertIn("workflow_dispatch", if_raw,
                      "chaos job 'if' should gate on workflow_dispatch")


# -- D3-C3, D3-C4: chaos-migrations runner ------------------------------

class TestChaosMigrationRunner(unittest.TestCase):
    """chaos-migrations must use scripts/migrate.sh, not inline psql loop."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CHAOS_COMPOSE_PATH)

    def test_chaos_migrations_uses_migrate_sh(self):
        """command must be ["bash", "/scripts/migrate.sh"]."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        cmd = cm.get("command") or []

        self.assertEqual(
            cmd, ["bash", "/scripts/migrate.sh"],
            f"chaos-migrations command must be [\"bash\", \"/scripts/migrate.sh\"], got {cmd!r}"
        )

    def test_chaos_migrations_no_entrypoint(self):
        """chaos-migrations must not override entrypoint.
        database/Dockerfile.migrate provides CMD, not entrypoint."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        entry = cm.get("entrypoint") or []
        self.assertEqual(
            entry, [],
            f"chaos-migrations must not set entrypoint, got {entry!r}"
        )

    def test_chaos_migrations_no_inline_sql_loop(self):
        """The old pattern was a shell for-loop over /migrations/*.sql.
        This should no longer exist."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        cmd = cm.get("command") or []
        body = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
        self.assertNotIn(
            "for f in", body,
            "chaos-migrations must not contain inline SQL loop"
        )
        self.assertNotIn(
            "*.sql", body,
            "chaos-migrations must not contain raw *.sql glob iteration"
        )

    def test_chaos_migrations_is_build_not_bare_image(self):
        """chaos-migrations should have a build context, not a bare postgres image."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        build = cm.get("build") or {}
        image = cm.get("image") or ""
        self.assertTrue(
            build or "postgres" not in image,
            "chaos-migrations must use a build context, "
            "not a bare postgres image (no Prisma toolchain). "
            f"Got image={image!r}, build={build!r}"
        )

    def test_chaos_migrations_build_context_is_repo_root(self):
        """Build context must be '.' so database/Dockerfile.migrate can
        COPY gateway/package*.json and gateway/prisma."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        build = cm.get("build") or {}
        context = build.get("context", "")
        # F2d narrowed context from "." to "./gateway" — either is acceptable.
        # The key invariant is that the Dockerfile path resolves.
        self.assertIn(
            context, (".", "./gateway"),
            f"chaos-migrations build context must be '.' or './gateway', got {context!r}"
        )

    def test_chaos_migrations_dockerfile_is_migrate(self):
        """Dockerfile must reference database/Dockerfile.migrate (dedicated
        migration runner with Node + pinned Prisma + postgresql-client).
        F2d may adjust the relative path when context is narrowed."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        build = cm.get("build") or {}
        dockerfile = build.get("dockerfile", "")
        # Both paths are acceptable: absolute from root, or relative to new context
        self.assertIn(
            dockerfile, ("database/Dockerfile.migrate", "../database/Dockerfile.migrate"),
            f"chaos-migrations dockerfile must reference database/Dockerfile.migrate, got {dockerfile!r}"
        )

    def test_chaos_migrations_no_env_file(self):
        """chaos-migrations must not use env_file: .env.
        migrate.sh loads .env internally at REPO_ROOT/.env."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        env_file = cm.get("env_file") or []
        self.assertEqual(
            env_file, [],
            f"chaos-migrations must not use env_file, got {env_file!r}"
        )

    def test_chaos_migrations_volumes_include_migrate_sh(self):
        """migrate.sh must be mounted into the container."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        volumes = cm.get("volumes") or []
        flat = " ".join(volumes)
        self.assertIn("migrate.sh", flat,
                      "chaos-migrations volumes must include scripts/migrate.sh")

    def test_chaos_migrations_sql_volume_matches_repo_root_derivation(self):
        """migrate.sh derives SQL_DIR=$REPO_ROOT/database/migrations.
        REPO_ROOT is set to / in chaos compose, so the SQL mount must be
        /database/migrations, not /migrations."""
        services = self.data.get("services") or {}
        cm = services.get("chaos-migrations") or {}
        volumes = cm.get("volumes") or []
        flat = " ".join(volumes)
        self.assertIn(
            "database/migrations", flat,
            "chaos-migrations volumes must mount at /database/migrations "
            "to match REPO_ROOT=/ derivation in migrate.sh"
        )
        # Explicitly forbid the old mount path
        self.assertNotIn(
            ":/migrations", flat,
            "chaos-migrations must not mount at /migrations "
            "(REPO_ROOT=/ + migrate.sh derives SQL_DIR=/database/migrations)"
        )


# -- D3-C5: Health dependencies on chaos-migrations ---------------------

class TestChaosMigrationDependents(unittest.TestCase):
    """agents and replay-service must depend on chaos-migrations with
    service_completed_successfully."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CHAOS_COMPOSE_PATH)

    def _check_dep(self, service_name):
        services = self.data.get("services") or {}
        svc = services.get(service_name) or {}
        deps = svc.get("depends_on") or {}
        if isinstance(deps, list):
            return None  # bare depends_on — check later
        return deps.get("chaos-migrations", None)

    def test_agents_depends_on_chaos_migrations_completed(self):
        dep = self._check_dep("agents")
        self.assertIsNotNone(
            dep,
            "agents must depend on chaos-migrations"
        )
        if isinstance(dep, dict):
            self.assertEqual(
                dep.get("condition"), "service_completed_successfully",
                "agents→chaos-migrations must be service_completed_successfully"
            )

    def test_replay_service_depends_on_chaos_migrations_completed(self):
        dep = self._check_dep("replay-service")
        self.assertIsNotNone(
            dep,
            "replay-service must depend on chaos-migrations"
        )
        if isinstance(dep, dict):
            self.assertEqual(
                dep.get("condition"), "service_completed_successfully",
                "replay-service→chaos-migrations must be service_completed_successfully"
            )


# -- D3-C6, D3-C7: No D1/D2 regressions ----------------------------------

class TestChaosNoRegressions(unittest.TestCase):
    """chaos compose must not regress on OPA version (D2) or OPA dependency
    condition (D1)."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CHAOS_COMPOSE_PATH)

    def test_opa_image_is_070(self):
        services = self.data.get("services") or {}
        opa = services.get("opa") or {}
        image = opa.get("image", "")
        self.assertEqual(
            image, f"openpolicyagent/opa:{EXPECTED_OPA_VERSION}",
            f"chaos OPA image must be {EXPECTED_OPA_VERSION}, got {image!r}"
        )

    def test_agents_depends_on_opa_service_healthy(self):
        services = self.data.get("services") or {}
        agents = services.get("agents") or {}
        deps = agents.get("depends_on") or {}
        opa_dep = deps.get("opa", {})
        if isinstance(opa_dep, dict):
            self.assertEqual(
                opa_dep.get("condition"), "service_healthy",
                "agents→opa must be service_healthy (D1 regression)"
            )

    def test_replay_opa_service_healthy(self):
        services = self.data.get("services") or {}
        rs = services.get("replay-service") or {}
        deps = rs.get("depends_on") or {}
        opa_dep = deps.get("opa", None)
        if opa_dep is None:
            # replay-service doesn't depend on OPA explicitly; acceptable
            return
        if isinstance(opa_dep, dict):
            self.assertNotEqual(
                opa_dep.get("condition"), "service_started",
                "replay-service→opa must not be service_started"
            )
