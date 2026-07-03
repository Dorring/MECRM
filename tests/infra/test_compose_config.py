"""Regression tests for docker-compose.yml (P0-2, P0-5, P0-6 + secret/DB-password hygiene).

These tests parse docker-compose.yml directly with PyYAML. They do NOT run
`docker compose config` (the host has no Docker), so variable interpolation
(`${VAR:-default}`) is NOT resolved -- we assert on the literal strings as
written, including the `${...}` markers where appropriate.

Covers:
  P0-2 -- migrate is a TRUE single runner: Prisma -> SQL 01-11 -> RLS verify,
          built from database/Dockerfile.migrate (node + prisma + psql). The old
          postgres-image version only ran SQL and silently skipped Prisma.
  P0-5 -- agents command is `python -m orchestrator.main` (matches Dockerfile).
  P0-6 -- healthchecks check response status code, not just request completion.
  DB-password hygiene -- gateway/agents/migrate DATABASE_URL is DERIVED from
          POSTGRES_PASSWORD (single source of truth), not a separate var that
          can drift from POSTGRES_PASSWORD.
  Secret hygiene -- Keycloak/Grafana use env vars (no hardcoded admin/admin);
          JWT_SECRET not hardcoded.
  Smoke test -- smoke-test runs the real scripts/smoke-test.sh (auth+write+read),
          not a placeholder health check.
"""

import re
import unittest

import yaml

COMPOSE_PATH = "docker-compose.yml"


def _load_compose():
    with open(COMPOSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _healthcheck_test_str(svc):
    hc = svc.get("healthcheck") or {}
    test = hc.get("test")
    if isinstance(test, list):
        return " ".join(str(x) for x in test)
    if isinstance(test, str):
        return test
    return ""


def _command_str(svc):
    cmd = svc.get("command")
    if isinstance(cmd, list):
        return " ".join(str(x) for x in cmd)
    if isinstance(cmd, str):
        return cmd
    return ""


def _env_list(svc):
    """Return the service environment as a list of raw strings."""
    env = svc.get("environment") or []
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return []


class TestMigrateService(unittest.TestCase):
    """P0-2: one-shot migration runner must run Prisma -> SQL 01-11 -> RLS verify."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})
        cls.migrate = cls.services.get("migrate")
        with open("scripts/migrate.sh", encoding="utf-8") as f:
            cls.migrate_script = f.read()

    def test_migrate_service_exists(self):
        self.assertIsNotNone(self.migrate, "migrate service is missing")

    def test_migrate_command_uses_bash(self):
        cmd = self.migrate.get("command") or []
        self.assertIsInstance(cmd, list, "migrate command must be a list (exec form)")
        self.assertEqual(
            cmd[:1],
            ["bash"],
            "migrate command must explicitly invoke bash; bind mounts on Windows do not "
            "preserve the executable bit, so a bare script path causes Node images to "
            "prefix it with 'node' and fail with SyntaxError",
        )
        self.assertIn("/scripts/migrate.sh", cmd)
        self.assertNotEqual(
            cmd,
            ["/scripts/migrate.sh"],
            "migrate command must not directly exec the mounted script without an interpreter",
        )

    def test_migrate_builds_from_dedicated_dockerfile(self):
        build = self.migrate.get("build") or {}
        self.assertEqual(
            build.get("dockerfile"),
            "database/Dockerfile.migrate",
            "migrate must build from database/Dockerfile.migrate (node+prisma+psql) "
            "so ONE image can run both Prisma and raw SQL tracks",
        )

    def test_migrate_mounts_script_and_migrations(self):
        volumes = self.migrate.get("volumes", []) or []
        joined = " ".join(str(v) for v in volumes)
        self.assertIn(
            "database/migrations",
            joined,
            "migrate must mount ./database/migrations so SQL files are available",
        )
        self.assertIn(
            "scripts/migrate.sh",
            joined,
            "migrate must mount scripts/migrate.sh into the container",
        )

    def test_migrate_runs_prisma_first(self):
        script = self.migrate_script
        self.assertIn(
            "prisma migrate deploy",
            script,
            "migrate script must run `npx prisma migrate deploy` BEFORE the SQL track",
        )
        self.assertLess(
            script.index("run_prisma_migrate"),
            script.index("run_sql_migrations"),
            "Prisma migrate deploy must precede the raw SQL sequence",
        )

    def test_migrate_applies_full_sql_sequence(self):
        script = self.migrate_script
        self.assertIn("00-advisory-lock.sql", script)
        self.assertIn("02-rls-policies.sql", script, "RLS SQL (02) must be applied")
        self.assertIn(
            "12-type-convergence.sql",
            script,
            "migrate must run the full 00-12 sequence",
        )

    def test_migrate_runs_rls_verification(self):
        script = self.migrate_script
        self.assertIn(
            "detect_drift",
            script,
            "migrate script must call drift/RLS verification after applying migrations",
        )
        self.assertIn(
            "RLS enforcement audit",
            script,
            "migrate script must run an RLS verification audit",
        )

    def test_migrate_database_url_derived_from_postgres_password(self):
        env = _env_list(self.migrate)
        joined = " ".join(env)
        self.assertIn(
            "POSTGRES_PASSWORD",
            joined,
            "migrate DATABASE_URL must be derived from POSTGRES_PASSWORD (not a "
            "separate DATABASE_URL var that can drift)",
        )

    def test_migrate_depends_on_postgres_healthy(self):
        deps = self.migrate.get("depends_on", {})
        self.assertIn("postgres", deps)
        pg = deps["postgres"]
        if isinstance(pg, dict):
            self.assertEqual(pg.get("condition"), "service_healthy")
        else:
            self.fail("migrate depends_on[postgres] must use condition: service_healthy")


class TestDatabaseUrlDerivation(unittest.TestCase):
    """DB-password hygiene: gateway/agents DATABASE_URL derived from POSTGRES_PASSWORD."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def _db_url_env(self, svc_name):
        svc = self.services.get(svc_name)
        self.assertIsNotNone(svc, f"{svc_name} service missing")
        for e in _env_list(svc):
            if e.startswith("DATABASE_URL="):
                return e
        return ""

    def test_gateway_db_url_uses_runtime_app_role(self):
        url = self._db_url_env("gateway")
        self.assertIn("postgresql://crm_app:", url, "gateway must use the RLS-enforced crm_app role")
        self.assertIn("CRM_APP_PASSWORD", url, "gateway must source the crm_app password from the environment")
        self.assertNotIn(
            "${DATABASE_URL}",
            url,
            "gateway must not read a separate DATABASE_URL var (can drift from POSTGRES_PASSWORD)",
        )

    def test_agents_db_url_uses_runtime_app_role(self):
        url = self._db_url_env("agents")
        self.assertIn("postgresql://crm_app:", url, "agents must use the RLS-enforced crm_app role")
        self.assertIn("CRM_APP_PASSWORD", url, "agents must source the crm_app password from the environment")


class TestGatewayPolicyMounts(unittest.TestCase):
    """Gateway audit policy visibility requires the repository policy bundle."""

    @classmethod
    def setUpClass(cls):
        cls.services = _load_compose().get("services", {})

    def test_gateway_services_mount_policies_read_only(self):
        for service_name in ("gateway", "test-gateway"):
            service = self.services.get(service_name)
            self.assertIsNotNone(service, f"{service_name} service missing")
            volumes = " ".join(str(v) for v in service.get("volumes", []))
            self.assertIn(
                "./policies:/app/policies:ro",
                volumes,
                f"{service_name} must mount policies read-only at /app/policies",
            )


class TestSecretsUseEnvVars(unittest.TestCase):
    """Keycloak/Grafana/JWT must use env vars, not hardcoded admin/supersecret."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def test_keycloak_uses_env_var_password(self):
        kc = self.services.get("keycloak")
        self.assertIsNotNone(kc, "keycloak service missing")
        env = _env_list(kc)
        joined = " ".join(env)
        self.assertNotIn(
            "KEYCLOAK_ADMIN_PASSWORD=admin",
            joined,
            "Keycloak admin password must NOT be hardcoded to admin",
        )
        self.assertIn(
            "KEYCLOAK_ADMIN_PASSWORD=${",
            joined,
            "Keycloak admin password must be sourced from an env var",
        )

    def test_grafana_uses_env_var_password(self):
        gf = self.services.get("grafana")
        self.assertIsNotNone(gf, "grafana service missing")
        env = _env_list(gf)
        joined = " ".join(env)
        self.assertNotIn(
            "GF_SECURITY_ADMIN_PASSWORD=admin",
            joined,
            "Grafana admin password must NOT be hardcoded to admin",
        )
        self.assertIn(
            "GRAFANA_ADMIN_PASSWORD",
            joined,
            "Grafana admin password must be sourced from an env var",
        )

    def test_no_hardcoded_jwt_secret(self):
        text = yaml.safe_dump(self.compose)
        self.assertNotIn("JWT_SECRET=supersecret", text)
        for line in text.splitlines():
            if "JWT_SECRET" in line and "=" in line:
                rhs = line.split("=", 1)[1].strip().strip('"').strip("'")
                if rhs and not rhs.startswith("${"):
                    self.fail(f"JWT_SECRET hardcoded: {line.strip()!r}")


class TestAgentsCommand(unittest.TestCase):
    """P0-5: compose agents command must match the Dockerfile entrypoint."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def test_agents_command_matches_dockerfile(self):
        agents = self.services.get("agents")
        self.assertIsNotNone(agents, "agents service missing")
        self.assertEqual(agents.get("command"), ["python", "-m", "orchestrator.main"])

    def test_agents_has_gateway_url(self):
        """P1-3: agents container must have GATEWAY_URL injected (not localhost fallback)."""
        env = _env_list(self.services.get("agents", {}))
        joined = " ".join(env)
        self.assertIn("GATEWAY_URL=http://gateway:4000", joined,
                      "agents must have GATEWAY_URL=http://gateway:4000 injected")


class TestHealthcheckStatusCodes(unittest.TestCase):
    """P0-6: healthchecks must fail on HTTP 5xx, not just on connection errors."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def test_replay_healthcheck_checks_status_code(self):
        replay = self.services.get("replay-service")
        self.assertIsNotNone(replay, "replay-service missing")
        test = _healthcheck_test_str(replay)
        self.assertIn("is_success", test)
        self.assertIn("sys.exit", test)

    def test_no_bare_httpx_get_without_status_check(self):
        for name, svc in self.services.items():
            test = _healthcheck_test_str(svc)
            if "httpx" not in test:
                continue
            self.assertIn(
                "is_success", test,
                f"service {name!r} healthcheck uses httpx without status check",
            )


class TestSmokeTestReal(unittest.TestCase):
    """smoke-test must run the real auth+write+read script, not a placeholder."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def test_smoke_test_mounts_real_script(self):
        sm = self.services.get("smoke-test")
        self.assertIsNotNone(sm, "smoke-test service missing")
        volumes = sm.get("volumes", []) or []
        joined = " ".join(str(v) for v in volumes)
        self.assertIn(
            "scripts/smoke-test.sh",
            joined,
            "smoke-test must mount scripts/smoke-test.sh (the real auth+write+read script)",
        )

    def test_smoke_test_runs_script_not_placeholder(self):
        sm = self.services.get("smoke-test")
        entrypoint = sm.get("entrypoint") or sm.get("command") or ""
        if isinstance(entrypoint, list):
            entrypoint = " ".join(str(x) for x in entrypoint)
        self.assertIn(
            "smoke-test.sh",
            str(entrypoint),
            "smoke-test must invoke smoke-test.sh",
        )
        # Must NOT be the old placeholder (TODO / health-only).
        text = yaml.safe_dump(sm)
        self.assertNotIn(
            "TODO replace with authenticated write",
            text,
            "smoke-test still has the placeholder TODO; must run the real script",
        )

    def test_smoke_test_depends_on_gateway_healthy(self):
        sm = self.services.get("smoke-test")
        deps = sm.get("depends_on", {})
        self.assertIn("gateway", deps)
        gw = deps["gateway"]
        if isinstance(gw, dict):
            self.assertEqual(gw.get("condition"), "service_healthy")


class TestSmokeTestRegisterHasTenantName(unittest.TestCase):
    """Smoke register payload must include tenantName (Gateway requires it)."""

    def test_sh_register_includes_tenant_name(self):
        with open("scripts/smoke-test.sh", encoding="utf-8") as f:
            sh = f.read()
        # The register JSON payload must contain tenantName.
        self.assertIn(
            "tenantName",
            sh,
            "smoke-test.sh register payload must include tenantName "
            "(gateway/src/routes/auth.ts:193 requires it; without it register returns 400)",
        )

    def test_ps1_register_includes_tenant_name(self):
        with open("scripts/smoke-test.ps1", encoding="utf-8") as f:
            ps1 = f.read()
        self.assertIn(
            "tenantName",
            ps1,
            "smoke-test.ps1 register payload must include tenantName",
        )


class TestMigrateDockerfileKeepsPrismaCli(unittest.TestCase):
    """Dockerfile.migrate must NOT use --omit=dev (prisma CLI is a devDependency)."""

    def test_no_omit_dev(self):
        with open("database/Dockerfile.migrate", encoding="utf-8") as f:
            df = f.read()
        # Inspect RUN commands (not comments, which may mention --omit=dev to
        # explain why it is NOT used).
        run_lines = [ln for ln in df.splitlines() if ln.strip().startswith("RUN")]
        self.assertTrue(
            any("npm ci" in ln for ln in run_lines),
            "Dockerfile.migrate must run `npm ci` (full) so the locked prisma CLI is present",
        )
        for ln in run_lines:
            if "npm ci" in ln:
                self.assertNotIn(
                    "--omit=dev",
                    ln,
                    "Dockerfile.migrate `npm ci` must NOT use --omit=dev: prisma CLI is in "
                    "gateway/devDependencies; omitting dev makes `npx prisma migrate deploy` "
                    "download an UNLOCKED prisma version at runtime (not pinned by lockfile)",
                )


class TestMigrateRlsFailsOnViolation(unittest.TestCase):
    """RLS verification must exit non-zero when RLS is missing, not just print."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.migrate = cls.compose["services"]["migrate"]
        with open("scripts/migrate.sh", encoding="utf-8") as f:
            cls.migrate_script = f.read()

    def test_rls_check_has_fail_path(self):
        script = self.migrate_script
        self.assertIn(
            "drift/RLS audit failed",
            script,
            "migrate RLS verification must fail (exit 1) when a tenant table lacks "
            "ENABLE+FORCE+ALL policy, not just print the status. A bare SELECT always exits 0 "
            "and would let RLS gaps pass migration silently.",
        )
        self.assertIn("exit 1", script)

    def test_migrate_command_does_not_embed_rls_sql(self):
        """The compose command must delegate to the script; RLS logic lives in the script."""
        cmd = _command_str(self.migrate)
        self.assertNotIn(
            "relforcerowsecurity",
            cmd,
            "RLS verification logic must live in scripts/migrate.sh, not be duplicated "
            "in docker-compose.yml command",
        )


class TestTestGatewayService(unittest.TestCase):
    """A dedicated test service must exist (gateway final image can't run Jest)."""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose.get("services", {})

    def test_test_gateway_service_exists(self):
        tg = self.services.get("test-gateway")
        self.assertIsNotNone(
            tg,
            "test-gateway service must exist: the gateway PRODUCTION image runs "
            "npm ci --omit=dev and has no test source, so `exec gateway npm test` fails",
        )

    def test_test_gateway_targets_builder_stage(self):
        tg = self.services["test-gateway"]
        build = tg.get("build") or {}
        self.assertEqual(
            build.get("target"),
            "builder",
            "test-gateway must build the `builder` stage (full devDependencies + test source)",
        )

    def test_test_gateway_enables_db_tests(self):
        tg = self.services["test-gateway"]
        env = _env_list(tg)
        joined = " ".join(env)
        self.assertIn(
            "CRM_TEST_REQUIRE_DB=1",
            joined,
            "test-gateway must set CRM_TEST_REQUIRE_DB=1 so DB-dependent Jest suites run "
            "(jest.setup.ts maps it to CRM_DB_AVAILABLE=1)",
        )

    def test_test_gateway_uses_test_profile(self):
        tg = self.services["test-gateway"]
        profiles = tg.get("profiles") or []
        self.assertIn(
            "test",
            profiles,
            "test-gateway must be behind the 'test' profile so it does not start with the default stack",
        )


if __name__ == "__main__":
    unittest.main()
