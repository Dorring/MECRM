"""Group F F2 regression tests -- dockerignore, HEALTHCHECK, cache mounts,
migrate context narrowing, gateway non-root user.

Covers:
  F-B2 -- agents .dockerignore includes .env / .env.*
  F-S1 -- frontend Dockerfile has HEALTHCHECK
  F-S2 -- all Dockerfiles use BuildKit cache mounts
  F-S3 -- migrate build context narrowed to ./gateway
  F-S4 -- agents .dockerignore includes tests/ and tooling caches
  F-S5 -- root .dockerignore expanded with dist/, docs/, tests/, caches
  F-S7 -- gateway runner uses USER node
"""

import os
import re
import unittest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

# -- F-B2 / F-S4: agents .dockerignore ----------------------------------

class TestAgentsDockerignore(unittest.TestCase):
    """agents/.dockerignore must exclude secrets, tests, and tooling caches."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "agents", ".dockerignore")
        with open(path, "r", encoding="utf-8") as fh:
            cls.patterns = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]

    def test_excludes_dotenv(self):
        self.assertIn(".env", self.patterns,
                      "F-B2: agents/.dockerignore must exclude .env")
        self.assertIn(".env.*", self.patterns,
                      "F-B2: agents/.dockerignore must exclude .env.*")

    def test_excludes_tests_dir(self):
        self.assertIn("tests/", self.patterns,
                      "F-S4: agents/.dockerignore must exclude tests/")

    def test_excludes_tooling_caches(self):
        for pattern in [".mypy_cache/", ".ruff_cache/"]:
            self.assertIn(pattern, self.patterns,
                          f"F-S4: agents/.dockerignore must exclude {pattern}")

    def test_excludes_test_artifacts(self):
        for pattern in ["test_output.txt", "conftest.py", "pytest.ini"]:
            self.assertIn(pattern, self.patterns,
                          f"F-S4: agents/.dockerignore must exclude {pattern}")

    def test_excludes_scripts(self):
        self.assertIn("scripts/", self.patterns,
                      "F-S4: agents/.dockerignore must exclude scripts/")

    def test_no_mojibake(self):
        path = os.path.join(REPO_ROOT, "agents", ".dockerignore")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        mojibake_chars = ["\ufffd", "\u301e", "\u6bcf", "\uff06", "\uff0a", "\u203b", "\u00a7", "\u3129"]
        for char in mojibake_chars:
            self.assertNotIn(char, content,
                             f"agents/.dockerignore must not contain Unicode char U+{ord(char):04X}")


# -- F-S5: root .dockerignore -------------------------------------------

class TestRootDockerignore(unittest.TestCase):
    """Root .dockerignore must exclude dist/, docs/, tests/, and tooling caches."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, ".dockerignore")
        with open(path, "r", encoding="utf-8") as fh:
            cls.patterns = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]

    def test_excludes_dist(self):
        self.assertIn("dist/", self.patterns,
                      "F-S5: root .dockerignore must exclude dist/")

    def test_excludes_docs(self):
        self.assertIn("docs/", self.patterns,
                      "F-S5: root .dockerignore must exclude docs/")

    def test_excludes_tests(self):
        self.assertIn("tests/", self.patterns,
                      "F-S5: root .dockerignore must exclude tests/")

    def test_excludes_assets(self):
        self.assertIn("assets/", self.patterns,
                      "F-S5: root .dockerignore must exclude assets/")

    def test_excludes_tooling_caches(self):
        for pattern in ["**/.mypy_cache/", "**/.ruff_cache/"]:
            self.assertIn(pattern, self.patterns,
                          f"F-S5: root .dockerignore must exclude {pattern}")

    def test_excludes_self_referential(self):
        self.assertIn("Dockerfile*", self.patterns,
                      "F-S5: root .dockerignore must exclude Dockerfile*")
        self.assertIn("docker-compose*.yml", self.patterns,
                      "F-S5: root .dockerignore must exclude docker-compose*.yml")


class TestGatewayDockerignore(unittest.TestCase):
    """gateway/.dockerignore must keep Jest config for Dockerized gateway tests."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "gateway", ".dockerignore")
        with open(path, "r", encoding="utf-8") as fh:
            cls.patterns = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]

    def test_keeps_jest_config_for_builder_target_tests(self):
        self.assertNotIn(
            "jest.config.js",
            self.patterns,
            "gateway/.dockerignore must not exclude jest.config.js; test-gateway runs npm test in Docker builder target",
        )
        self.assertNotIn(
            "jest.durability.config.js",
            self.patterns,
            "gateway/.dockerignore must not exclude jest.durability.config.js; durability tests need their Jest config",
        )


# -- F-S1: frontend HEALTHCHECK -----------------------------------------

class TestFrontendHealthcheck(unittest.TestCase):
    """frontend/Dockerfile must include a HEALTHCHECK instruction."""

    def test_has_healthcheck(self):
        path = os.path.join(REPO_ROOT, "frontend", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("HEALTHCHECK", content,
                      "F-S1: frontend/Dockerfile must have a HEALTHCHECK instruction")
        # Must use node fetch, not wget/curl
        self.assertIn("fetch(", content,
                      "F-S1: frontend HEALTHCHECK must use Node fetch, not wget/curl")


# -- F-S2: BuildKit cache mounts ----------------------------------------

class TestBuildKitCacheMounts(unittest.TestCase):
    """All Dockerfiles must use RUN --mount=type=cache for package managers."""

    def _check_file(self, dockerfile, expected_mount):
        path = os.path.join(REPO_ROOT, dockerfile)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        pattern = re.escape(expected_mount)
        self.assertRegex(content, pattern,
                         f"F-S2: {dockerfile} must have --mount=type=cache for {expected_mount}")

    def test_gateway_has_npm_cache(self):
        self._check_file("gateway/Dockerfile", "--mount=type=cache,target=/root/.npm")

    def test_frontend_has_npm_cache(self):
        self._check_file("frontend/Dockerfile", "--mount=type=cache,target=/root/.npm")

    def test_agents_has_pip_cache(self):
        self._check_file("agents/Dockerfile", "--mount=type=cache,target=/root/.cache/pip")

    def test_migrate_has_npm_cache(self):
        self._check_file("database/Dockerfile.migrate", "--mount=type=cache,target=/root/.npm")


# -- F-S2b: # syntax=docker/dockerfile:1.7 ------------------------------

class TestBuildKitSyntaxDirective(unittest.TestCase):
    """Dockerfiles with RUN --mount must begin with # syntax=docker/dockerfile:1.7."""

    def test_gateway_has_syntax(self):
        path = os.path.join(REPO_ROOT, "gateway", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "# syntax=docker/dockerfile:1.7",
                         "gateway/Dockerfile must start with # syntax=docker/dockerfile:1.7")

    def test_frontend_has_syntax(self):
        path = os.path.join(REPO_ROOT, "frontend", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "# syntax=docker/dockerfile:1.7",
                         "frontend/Dockerfile must start with # syntax=docker/dockerfile:1.7")

    def test_agents_has_syntax(self):
        path = os.path.join(REPO_ROOT, "agents", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "# syntax=docker/dockerfile:1.7",
                         "agents/Dockerfile must start with # syntax=docker/dockerfile:1.7")

    def test_migrate_has_syntax(self):
        path = os.path.join(REPO_ROOT, "database", "Dockerfile.migrate")
        with open(path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "# syntax=docker/dockerfile:1.7",
                         "database/Dockerfile.migrate must start with # syntax=docker/dockerfile:1.7")


# -- F-S7: gateway non-root USER ----------------------------------------

class TestGatewayNonRootUser(unittest.TestCase):
    """gateway/Dockerfile runner stage must use USER node."""

    def test_has_user_node(self):
        path = os.path.join(REPO_ROOT, "gateway", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("RUN chown -R node:node /app", content,
                      "F-S7: gateway runner must chown /app to node before USER switch")
        self.assertIn("USER node", content,
                      "F-S7: gateway runner must have USER node")

    def test_chown_before_user(self):
        path = os.path.join(REPO_ROOT, "gateway", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        chown_index = None
        user_index = None
        for i, line in enumerate(lines):
            if "chown -R node:node" in line:
                chown_index = i
            if line.strip() == "USER node":
                user_index = i
        self.assertIsNotNone(chown_index, "chown not found")
        self.assertIsNotNone(user_index, "USER node not found")
        self.assertLess(chown_index, user_index,
                        "chown must come BEFORE USER node")


# -- F-S1b: frontend HEALTHCHECK env combine ----------------------------

class TestFrontendEnvCombine(unittest.TestCase):
    """frontend/Dockerfile runner stage must combine PORT and HOSTNAME ENV."""

    def test_env_combined(self):
        path = os.path.join(REPO_ROOT, "frontend", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("ENV PORT=3000 HOSTNAME=0.0.0.0", content,
                      "F-D4: frontend runner should combine PORT and HOSTNAME into one ENV")

    def test_addgroup_adduser_combined(self):
        path = os.path.join(REPO_ROOT, "frontend", "Dockerfile")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        # addgroup and adduser should be in the same RUN line (combined with &&)
        self.assertRegex(content, r"RUN addgroup.*&&.*\n\s*adduser",
                         "F-D4: addgroup + adduser should be one RUN instruction (find a multiline pattern with &&)")


# -- F-S3: migrate context narrowing ------------------------------------

class TestMigrateContextNarrowing(unittest.TestCase):
    """docker-compose.yml and docker-compose.chaos.yml must use ./gateway context."""

    def _load_yaml(self, path):
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_main_compose_migrate_context(self):
        import yaml
        path = os.path.join(REPO_ROOT, "docker-compose.yml")
        data = self._load_yaml(path)
        migrate = data.get("services", {}).get("migrate", {})
        build = migrate.get("build", {})
        self.assertEqual(build.get("context"), "./gateway",
                         "F-S3: docker-compose.yml migrate context must be ./gateway")
        self.assertEqual(build.get("dockerfile"), "../database/Dockerfile.migrate",
                         "F-S3: docker-compose.yml migrate dockerfile must be ../database/Dockerfile.migrate")

    def test_chaos_compose_migrate_context(self):
        import yaml
        path = os.path.join(REPO_ROOT, "docker-compose.chaos.yml")
        data = self._load_yaml(path)
        chaos_migrate = data.get("services", {}).get("chaos-migrations", {})
        build = chaos_migrate.get("build", {})
        self.assertEqual(build.get("context"), "./gateway",
                         "F-S3: docker-compose.chaos.yml chaos-migrations context must be ./gateway")
        self.assertEqual(build.get("dockerfile"), "../database/Dockerfile.migrate",
                         "F-S3: docker-compose.chaos.yml chaos-migrations dockerfile must be ../database/Dockerfile.migrate")

    def test_migrate_dockerfile_copy_paths(self):
        path = os.path.join(REPO_ROOT, "database", "Dockerfile.migrate")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        # COPY must reference bare package*.json and prisma (no gateway/ prefix)
        self.assertIn("COPY package*.json ./", content,
                      "F-S3: Dockerfile.migrate must COPY package*.json (no gateway/ prefix)")
        self.assertIn("COPY prisma ./prisma", content,
                      "F-S3: Dockerfile.migrate must COPY prisma (no gateway/ prefix)")
        # No remaining gateway/ prefix in COPY instructions
        copy_lines = [line for line in content.splitlines() if "COPY gateway/" in line]
        self.assertEqual(len(copy_lines), 0,
                         f"F-S3: Dockerfile.migrate must not have COPY gateway/ lines: {copy_lines}")

    def test_migrate_volumes_unchanged(self):
        import yaml
        path = os.path.join(REPO_ROOT, "docker-compose.yml")
        data = self._load_yaml(path)
        migrate = data.get("services", {}).get("migrate", {})
        volumes = migrate.get("volumes", [])
        # Runtime volumes must still reference repo-relative paths
        has_migrations = any("./database/migrations" in v for v in volumes)
        has_script = any("migrate.sh" in v for v in volumes)
        self.assertTrue(has_migrations,
                        "F-S3: migrate service must still mount ./database/migrations")
        self.assertTrue(has_script,
                        "F-S3: migrate service must still mount migrate.sh")
