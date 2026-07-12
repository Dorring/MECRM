"""Group F F4 regression tests -- agents multi-stage Dockerfile conversion.

Covers:
  F4-M1 -- agents/Dockerfile contains at least two stages (builder + runner)
  F4-M2 -- build-essential only appears in builder stage, not runner
  F4-M3 -- runner stage has no apt-get install of build toolchain (gcc, g++, make, binutils, libc-dev)
  F4-M4 -- runner stage has USER app (non-root)
  F4-M5 -- builder pip --user output copied from /root/.local to /home/app/.local
  F4-M6 -- PATH includes /home/app/.local/bin
  F4-M7 -- PYTHONPATH includes /app/src
  F4-M8 -- CMD remains python -m orchestrator.main
  F4-M9 -- HEALTHCHECK is status-code aware (r.is_success / r.ok)
  F4-M10 -- .dockerignore still excludes .env and .env.* (no regression)
  F4-M11 -- COPY only copies src/ and sitecustomize.py (no tests, no scripts)
  F4-M12 -- syntax directive still present, cache mount still present (F2 regression guard)
"""

import os
import re
import unittest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DOCKERFILE_PATH = os.path.join(REPO_ROOT, "agents", "Dockerfile")
DOCKERIGNORE_PATH = os.path.join(REPO_ROOT, "agents", ".dockerignore")


def _read_dockerfile():
    with open(DOCKERFILE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_dockerignore():
    with open(DOCKERIGNORE_PATH, "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]


# -- F4-M1: Multi-stage structure ---------------------------------------

class TestAgentsMultiStageStructure(unittest.TestCase):
    """agents/Dockerfile must have at least builder and runner stages."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_dockerfile()
        cls.lines = cls.content.splitlines()

    def test_has_builder_stage(self):
        self.assertIn("FROM python:3.11-slim AS builder", self.content,
                      "F4-M1: Dockerfile must have a builder stage")

    def test_has_runner_stage(self):
        self.assertIn("FROM python:3.11-slim AS runner", self.content,
                      "F4-M1: Dockerfile must have a runner stage")

    def test_builder_before_runner(self):
        builder_idx = runner_idx = -1
        for i, line in enumerate(self.lines):
            if "AS builder" in line and "FROM" in line:
                builder_idx = i
            if "AS runner" in line and "FROM" in line:
                runner_idx = i
        self.assertLess(builder_idx, runner_idx,
                        "F4-M1: builder stage must come before runner stage")

    def test_two_from_statements(self):
        from_count = sum(1 for line in self.lines if line.startswith("FROM "))
        self.assertGreaterEqual(from_count, 2,
                                "F4-M1: Dockerfile must have at least 2 FROM instructions")


# -- F4-M2, F4-M3: build-essential isolated to builder -------------------

class TestAgentsNoBuildToolchainInRunner(unittest.TestCase):
    """build-essential must only exist in builder, never in runner."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_dockerfile()
        cls.lines = cls.content.splitlines()

    def _runner_lines(self):
        """Return lines between 'AS runner' and end of file."""
        in_runner = False
        result = []
        for line in self.lines:
            if "AS runner" in line and "FROM" in line:
                in_runner = True
                continue
            if in_runner:
                result.append(line)
        return result

    def _builder_lines(self):
        """Return lines between 'AS builder' and next FROM that starts runner."""
        in_builder = False
        result = []
        for line in self.lines:
            if "AS builder" in line and "FROM" in line:
                in_builder = True
                continue
            if in_builder:
                if line.startswith("FROM "):
                    break
                result.append(line)
        return result

    def test_build_essential_in_builder(self):
        builder_text = "\n".join(self._builder_lines())
        self.assertIn("build-essential", builder_text,
                      "F4-M2: builder stage must install build-essential")

    def test_no_build_essential_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("build-essential", runner_text,
                         "F4-M3: runner stage must NOT install build-essential")

    def test_no_gcc_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotRegex(runner_text, r'\bgcc\b',
                            "F4-M3: runner must not reference gcc")

    def test_no_gpp_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotRegex(runner_text, r'\bg\+\+\b',
                            "F4-M3: runner must not reference g++")

    def test_no_make_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotRegex(runner_text, r'\bmake\b',
                            "F4-M3: runner must not reference make")

    def test_no_binutils_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("binutils", runner_text,
                         "F4-M3: runner must not reference binutils")

    def test_no_libc_dev_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("libc-dev", runner_text,
                         "F4-M3: runner must not reference libc-dev / libc6-dev")

    def test_no_dev_headers_in_runner(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotRegex(runner_text, r'python.*-dev',
                            "F4-M3: runner must not reference python-dev")

    def test_no_apt_get_install_in_runner(self):
        """Runner should not run apt-get install at all."""
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("apt-get install", runner_text,
                         "F4-M3: runner stage must not run apt-get install")


# -- F4-M4: Non-root user ------------------------------------------------

class TestAgentsNonRootUser(unittest.TestCase):
    """Runner stage must use USER app."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_dockerfile()
        cls.lines = cls.content.splitlines()

    def test_has_user_app(self):
        self.assertIn("USER app", self.content,
                      "F4-M4: runner must have USER app")

    def test_app_user_uid_1001(self):
        self.assertRegex(self.content, r"adduser.*--uid 1001",
                         "F4-M4: app user must have uid 1001")

    def test_chown_before_user(self):
        chown_idx = None
        user_idx = None
        for i, line in enumerate(self.lines):
            if "chown -R app:app" in line:
                chown_idx = i
            if line.strip() == "USER app":
                user_idx = i
        self.assertIsNotNone(chown_idx, "chown -R app:app not found")
        self.assertIsNotNone(user_idx, "USER app not found")
        self.assertLess(chown_idx, user_idx,
                        "F4-M4: chown must come BEFORE USER app")


# -- F4-M5, F4-M6, F4-M7: COPY and env ----------------------------------

class TestAgentsCopyAndEnv(unittest.TestCase):
    """Runner COPY and ENV must use /home/app/.local."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_dockerfile()

    def test_copies_local_from_builder(self):
        self.assertIn("COPY --from=builder /root/.local /home/app/.local", self.content,
                      "F4-M5: must COPY /root/.local from builder to /home/app/.local")

    def test_path_includes_home_app_local_bin(self):
        self.assertIn("PATH=/home/app/.local/bin", self.content,
                      "F4-M6: PATH must include /home/app/.local/bin")

    def test_pythonpath_includes_app_src(self):
        self.assertIn("PYTHONPATH=/app/src", self.content,
                      "F4-M7: PYTHONPATH must include /app/src")


# -- F4-M8: Entry point --------------------------------------------------

class TestAgentsEntryPoint(unittest.TestCase):
    """CMD must remain orchestrator.main."""

    def test_cmd_is_orchestrator_main(self):
        content = _read_dockerfile()
        self.assertIn('CMD ["python", "-m", "orchestrator.main"]', content,
                      "F4-M8: CMD must remain python -m orchestrator.main")


# -- F4-M9: Healthcheck --------------------------------------------------

class TestAgentsHealthcheck(unittest.TestCase):
    """Healthcheck must be status-code aware."""

    def test_healthcheck_exists(self):
        content = _read_dockerfile()
        self.assertIn("HEALTHCHECK", content,
                      "F4-M9: Dockerfile must have HEALTHCHECK")

    def test_healthcheck_is_status_aware(self):
        content = _read_dockerfile()
        # Must check the response status, not just that the request completed
        self.assertTrue(
            "r.is_success" in content or "r.ok" in content,
            "F4-M9: HEALTHCHECK must check response status code, not just complete the request"
        )

    def test_healthcheck_uses_httpx(self):
        content = _read_dockerfile()
        self.assertIn("httpx", content,
                      "F4-M9: HEALTHCHECK must use httpx (consistent with existing pattern)")


# -- F4-M10: .dockerignore still excludes .env ---------------------------

class TestAgentsDockerignoreStillExcludesSecrets(unittest.TestCase):
    """agents/.dockerignore must still exclude .env and .env.* (no regression)."""

    def test_excludes_dotenv(self):
        patterns = _read_dockerignore()
        self.assertIn(".env", patterns,
                      "F4-M10: agents/.dockerignore must still exclude .env")
        self.assertIn(".env.*", patterns,
                      "F4-M10: agents/.dockerignore must still exclude .env.*")


# -- F4-M11: Selective COPY -- only src/ and sitecustomize.py ------------

class TestAgentsSelectiveCopy(unittest.TestCase):
    """Runner stage must COPY only src/ and sitecustomize.py, not tests or scripts."""

    @classmethod
    def setUpClass(cls):
        cls.content = _read_dockerfile()
        cls.lines = cls.content.splitlines()

    def _runner_lines(self):
        in_runner = False
        result = []
        for line in self.lines:
            if "AS runner" in line and "FROM" in line:
                in_runner = True
                continue
            if in_runner:
                result.append(line)
        return result

    def test_copies_src_directory(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertIn("COPY src/ /app/src/", runner_text,
                      "F4-M11: runner must COPY src/ directory")

    def test_copies_sitecustomize(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertIn("sitecustomize.py", runner_text,
                      "F4-M11: runner must COPY sitecustomize.py")

    def test_does_not_copy_tests(self):
        runner_text = "\n".join(self._runner_lines())
        copy_lines = [l for l in self._runner_lines() if l.startswith("COPY ") and "tests" in l]
        self.assertEqual(len(copy_lines), 0,
                        f"F4-M11: runner must not COPY tests directory: {copy_lines}")

    def test_does_not_copy_scripts(self):
        runner_text = "\n".join(self._runner_lines())
        copy_lines = [l for l in self._runner_lines() if l.startswith("COPY ") and "scripts" in l]
        self.assertEqual(len(copy_lines), 0,
                        f"F4-M11: runner must not COPY scripts directory: {copy_lines}")

    def test_does_not_copy_conftest(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("conftest.py", runner_text,
                         "F4-M11: runner must not COPY conftest.py")

    def test_does_not_copy_pytest_ini(self):
        runner_text = "\n".join(self._runner_lines())
        self.assertNotIn("pytest.ini", runner_text,
                         "F4-M11: runner must not COPY pytest.ini")

    def test_no_copy_dot(self):
        """Runner must not use COPY . . -- that copies everything."""
        runner_text = "\n".join(self._runner_lines())
        copy_all = [l for l in self._runner_lines() if l.strip() == "COPY . ."]
        self.assertEqual(len(copy_all), 0,
                         "F4-M11: runner must not use COPY . . (selective COPY only)")


# -- F4-M12: F2 regression guards (syntax, cache mount) ------------------

class TestAgentsF2RegressionGuards(unittest.TestCase):
    """F2 changes must survive: syntax directive, cache mount."""

    def test_syntax_directive_still_first_line(self):
        with open(DOCKERFILE_PATH, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "# syntax=docker/dockerfile:1.7",
                         "F4-M12: syntax directive must remain as first line")

    def test_cache_mount_still_present(self):
        content = _read_dockerfile()
        self.assertIn("--mount=type=cache,target=/root/.cache/pip", content,
                      "F4-M12: BuildKit cache mount for pip must be preserved")


# -- F4-M13: Runtime source coverage (replay, orchestrator) ---------------

class TestAgentsRuntimeSourceCoverage(unittest.TestCase):
    """Runner must include all runtime-needed modules inside src/."""

    def test_orchestrator_in_src(self):
        orchestrator_init = os.path.join(REPO_ROOT, "agents", "src", "orchestrator", "__init__.py")
        self.assertTrue(os.path.isfile(orchestrator_init),
                        "orchestrator package must exist in src/orchestrator/")

    def test_replay_in_src(self):
        replay_init = os.path.join(REPO_ROOT, "agents", "src", "replay", "__init__.py")
        self.assertTrue(os.path.isfile(replay_init),
                        "replay package must exist in src/replay/ -- "
                        "needed by replay-service in docker-compose.yml")

    def test_src_contains_all_runtime_packages(self):
        """All top-level packages under src/ are covered by 'COPY src/ /app/src/'."""
        src_dir = os.path.join(REPO_ROOT, "agents", "src")
        packages = [
            d for d in os.listdir(src_dir)
            if os.path.isdir(os.path.join(src_dir, d)) and not d.startswith("__")
        ]
        expected = ["orchestrator", "agents", "intelligence", "replay",
                    "governance", "policy", "projections", "resilience", "schema"]
        missing = [p for p in expected if p not in packages]
        self.assertEqual(missing, [],
                         f"Expected runtime packages missing from src/: {missing}")


# -- F4-M14: No mojibake in Dockerfile -----------------------------------

class TestAgentsDockerfileNoMojibake(unittest.TestCase):
    """agents/Dockerfile must be ASCII-safe (no corruption)."""

    def test_no_mojibake(self):
        with open(DOCKERFILE_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        mojibake_chars = [
            "�", "〞", "每", "＆",
            "＊", "※", "§", "ㄩ",
            "—",  # em-dash
        ]
        for char in mojibake_chars:
            self.assertNotIn(char, content,
                             f"agents/Dockerfile must not contain Unicode char U+{ord(char):04X}")
