"""Group F F3 regression tests -- CI image metrics artifact.

Covers:
  F3-M1 -- CI build job has image metrics steps (Validate metrics script,
           Collect image metrics, Upload image metrics artifact)
  F3-M2 -- Metrics steps execute AFTER build-push (ordering guard)
  F3-M3 -- Artifact name is stable and per-matrix-project
  F3-M4 -- Artifact upload uses actions/upload-artifact@v4
  F3-M5 -- Build duration is recorded (build-start timestamp before build-push)
  F3-M6 -- ci-inspect-image.sh exists, is bash-parseable, and accepts --image/--output
  F3-M7 -- Script does not reference migrate image (only gateway/frontend/agents matrix)
  F3-M8 -- metrics artifact retention-days is set

PR-only validation (no Docker daemon required):
  - YAML parsing of ci-cd.yml build job
  - Static analysis of scripts/ci-inspect-image.sh
  - Step ordering enforcement
"""

import json
import os
import re
import unittest

import yaml

CI_CD_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows", "ci-cd.yml"
)
INSPECT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "ci-inspect-image.sh"
)

MATRIX_PROJECTS = ["gateway", "frontend", "agents"]


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_build_job(data):
    jobs = data.get("jobs", {})
    build = jobs.get("build", {})
    return build


def _get_build_steps(data):
    build = _get_build_job(data)
    return build.get("steps", [])


def _step_names(steps):
    return [s.get("name", "") for s in steps]


def _steps_after(steps, step_name):
    """Return step names that come after the named step."""
    found = False
    result = []
    for s in steps:
        if found:
            result.append(s.get("name", ""))
        if s.get("name") == step_name:
            found = True
    return result


# -- F3-M1, F3-M2, F3-M3, F3-M4: CI build job structure -----------------

class TestCIBuildJobHasMetricsSteps(unittest.TestCase):
    """ci-cd.yml build job must include metrics collection steps."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CI_CD_PATH)

    def test_build_job_exists(self):
        build = _get_build_job(self.data)
        self.assertIsNotNone(build, "build job must exist in ci-cd.yml")
        self.assertIn("steps", build, "build job must have steps")

    def test_has_validate_metrics_script_step(self):
        names = _step_names(_get_build_steps(self.data))
        self.assertIn("Validate metrics script", names,
                      "F3-M1: build job must have 'Validate metrics script' step")

    def test_has_collect_image_metrics_step(self):
        names = _step_names(_get_build_steps(self.data))
        self.assertIn("Collect image metrics", names,
                      "F3-M1: build job must have 'Collect image metrics' step")

    def test_has_upload_image_metrics_artifact_step(self):
        names = _step_names(_get_build_steps(self.data))
        self.assertIn("Upload image metrics artifact", names,
                      "F3-M1: build job must have 'Upload image metrics artifact' step")


class TestCIMetricsStepOrdering(unittest.TestCase):
    """Metrics steps must execute after Build and push."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_validate_metrics_script_after_build_push(self):
        after = _steps_after(self.steps, "Build and push")
        self.assertIn("Validate metrics script", after,
                      "F3-M2: 'Validate metrics script' must be after 'Build and push'")

    def test_collect_metrics_after_build_push(self):
        after = _steps_after(self.steps, "Build and push")
        self.assertIn("Collect image metrics", after,
                      "F3-M2: 'Collect image metrics' must be after 'Build and push'")

    def test_upload_artifact_after_collect(self):
        after = _steps_after(self.steps, "Collect image metrics")
        self.assertIn("Upload image metrics artifact", after,
                      "F3-M2: 'Upload image metrics artifact' must be after 'Collect image metrics'")


class TestCIBuildJobMatrix(unittest.TestCase):
    """build job matrix must include gateway, frontend, agents."""

    @classmethod
    def setUpClass(cls):
        build = _get_build_job(_load_yaml(CI_CD_PATH))
        strategy = build.get("strategy", {})
        matrix = strategy.get("matrix", {})
        cls.include = matrix.get("include", [])

    def test_matrix_has_gateway(self):
        projects = [e.get("project") for e in self.include]
        self.assertIn("gateway", projects,
                      "build matrix must include gateway")

    def test_matrix_has_frontend(self):
        projects = [e.get("project") for e in self.include]
        self.assertIn("frontend", projects,
                      "build matrix must include frontend")

    def test_matrix_has_agents(self):
        projects = [e.get("project") for e in self.include]
        self.assertIn("agents", projects,
                      "build matrix must include agents")

    def test_matrix_includes_migrate(self):
        projects = [e.get("project") for e in self.include]
        self.assertIn("migrate", projects,
                      "F3/G3a: build matrix must include migrate (G3a digest deploy chain)")


class TestCIMetricsArtifactName(unittest.TestCase):
    """Artifact upload must produce stable per-project names."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def _get_upload_step(self):
        for s in self.steps:
            if s.get("name") == "Upload image metrics artifact":
                return s
        self.fail("Upload image metrics artifact step not found")

    def test_uses_upload_artifact_v4(self):
        step = self._get_upload_step()
        uses = step.get("uses", "")
        self.assertEqual(uses, "actions/upload-artifact@v4",
                         "F3-M4: must use actions/upload-artifact@v4")

    def test_artifact_name_is_per_project(self):
        step = self._get_upload_step()
        name = step.get("with", {}).get("name", "")
        self.assertIn("${{ matrix.project }}", name,
                      "F3-M3: artifact name must include ${{ matrix.project }} for per-project uniqueness")
        self.assertTrue(name.startswith("image-metrics-"),
                        f"F3-M3: artifact name must start with 'image-metrics-', got {name!r}")

    def test_if_no_files_found_is_error(self):
        step = self._get_upload_step()
        with_config = step.get("with", {})
        self.assertEqual(with_config.get("if-no-files-found"), "error",
                         "F3-M3: artifact upload must error if no files found")


class TestCIMetricsRetentionDays(unittest.TestCase):
    """Artifact retention-days must be set (F3 requirement)."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_retention_days_set(self):
        for s in self.steps:
            if s.get("name") == "Upload image metrics artifact":
                with_config = s.get("with", {})
                retention = with_config.get("retention-days")
                self.assertIsNotNone(retention,
                                     "F3-M8: retention-days must be set on artifact upload")
                self.assertGreater(retention, 0,
                                   f"retention-days must be positive, got {retention}")
                return
        self.fail("Upload image metrics artifact step not found")


class TestCIBuildDurationRecorded(unittest.TestCase):
    """Build duration must be recorded with a start-time marker before build."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_build_start_marker_exists(self):
        names = _step_names(self.steps)
        self.assertIn("Mark build start time", names,
                      "F3-M5: build job must have 'Mark build start time' step")

    def test_build_start_before_build_push(self):
        after_start = _steps_after(self.steps, "Mark build start time")
        self.assertIn("Build and push", after_start,
                      "F3-M5: 'Build and push' must be after 'Mark build start time'")

    def test_record_build_duration_exists(self):
        names = _step_names(self.steps)
        self.assertIn("Record build duration", names,
                      "F3-M5: build job must have 'Record build duration' step")

    def test_record_duration_after_build_push(self):
        after = _steps_after(self.steps, "Build and push")
        self.assertIn("Record build duration", after,
                      "F3-M5: 'Record build duration' must be after 'Build and push'")

    def test_collect_metrics_passes_build_duration_to_script(self):
        for s in self.steps:
            if s.get("name") == "Collect image metrics":
                run = s.get("run", "")
                self.assertIn("--build-duration-seconds", run,
                              "F3-M5: metrics script must receive build duration")
                self.assertIn("steps.build-duration.outputs.duration_seconds", run,
                              "F3-M5: metrics script must use the recorded build duration output")
                return
        self.fail("Collect image metrics step not found")


# -- F3-M6: ci-inspect-image.sh static checks ----------------------------

class TestCIInspectImageScript(unittest.TestCase):
    """scripts/ci-inspect-image.sh must exist and be valid."""

    def test_script_exists(self):
        self.assertTrue(os.path.isfile(INSPECT_SCRIPT_PATH),
                        "F3-M6: scripts/ci-inspect-image.sh must exist")

    def test_script_is_non_empty(self):
        size = os.path.getsize(INSPECT_SCRIPT_PATH)
        self.assertGreater(size, 0,
                          "F3-M6: scripts/ci-inspect-image.sh must be non-empty")

    def test_script_is_readable(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("set -euo pipefail", content,
                      "F3-M6: script must use 'set -euo pipefail'")

    def test_script_accepts_image_flag(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("--image", content,
                      "F3-M6: script must accept --image flag")
        self.assertIn("--output", content,
                      "F3-M6: script must accept --output flag")
        self.assertIn("--build-duration-seconds", content,
                      "F3-M6: script must accept --build-duration-seconds flag")

    def test_script_uses_docker_image_inspect(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("docker image inspect", content,
                      "F3-M6: script must use docker image inspect")

    def test_script_uses_docker_history(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("docker history", content,
                      "F3-M6: script must use docker history")

    def test_script_outputs_json(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("json.dump", content,
                      "F3-M6: script must output JSON")

    def test_script_includes_fields(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        required_fields = [
            "uncompressedSize",
            "layerCount",
            "digest",
            "imageName",
            "buildDurationS",
            "dockerHistory",
        ]
        for field in required_fields:
            self.assertIn(field, content,
                          f"F3-M6: script must output field '{field}'")

    def test_script_does_not_double_prefix_sha256_digest(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("elif not digest.startswith('sha256:')", content,
                      "F3-M6: script must guard against double sha256: prefixes")
        self.assertIn("'digest': digest", content,
                      "F3-M6: output digest should use normalized digest directly")
        self.assertNotIn("'digest': f'sha256:{digest}'", content,
                         "F3-M6: output digest must not blindly prefix sha256:")

    def test_no_mojibake(self):
        with open(INSPECT_SCRIPT_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        # Subtle Unicode corruption chars that have appeared in this repo
        mojibake_chars = [
            "\ufffd", "\u301e", "\u6bcf", "\uff06",
            "\uff0a", "\u203b", "\u00a7", "\u3129",
            "\u2014",  # em-dash
        ]
        for char in mojibake_chars:
            self.assertNotIn(char, content,
                             f"ci-inspect-image.sh must not contain Unicode char U+{ord(char):04X}")


# -- F3-M7: Script must not reference migrate ----------------------------

class TestCIMetricsIncludesMigrateProject(unittest.TestCase):
    """G3a: migrate is now in the build matrix; ci-inspect-image.sh covers it."""

    def test_ci_build_matrix_includes_migrate(self):
        build = _get_build_job(_load_yaml(CI_CD_PATH))
        strategy = build.get("strategy", {})
        matrix = strategy.get("matrix", {})
        include = matrix.get("include", [])
        projects = [e.get("project") for e in include]
        self.assertIn("migrate", projects,
                     "F3/G3a: build matrix must include migrate")


# -- F3: CI build job only pushes on main push -- -----------------------

class TestCIBuildJobCondition(unittest.TestCase):
    """build job should push only on main push, but PR must be able to
    validate the script and workflow."""

    @classmethod
    def setUpClass(cls):
        cls.build = _get_build_job(_load_yaml(CI_CD_PATH))

    def test_build_job_has_if_guard(self):
        """build job has an if: guard limiting push to main branch."""
        if_guard = self.build.get("if", "")
        self.assertIn("main", if_guard,
                      "build job should gate on main branch")
        self.assertIn("push", if_guard.lower(),
                      "build job should gate on push event")

    def test_metrics_steps_dont_require_push(self):
        """Metrics collection steps don't add their own if: guards;
        they inherit the job-level guard. The steps themselves are
        additive and don't gate on secrets or environment."""
        steps = self.build.get("steps", [])
        for s in steps:
            name = s.get("name", "")
            if name in ("Validate metrics script", "Collect image metrics",
                        "Upload image metrics artifact"):
                step_if = s.get("if", "")
                self.assertNotIn(
                    "push", step_if,
                    f"'{name}' step should not add its own push guard; "
                    "it inherits the job-level guard"
                )


class TestCIPullRequestCanValidate(unittest.TestCase):
    """PRs cannot push images, but can still validate the metrics script
    path and bash syntax via the static check step."""

    @classmethod
    def setUpClass(cls):
        cls.build = _get_build_job(_load_yaml(CI_CD_PATH))

    def test_validate_metrics_step_uses_bash_n(self):
        """The Validate metrics script step runs 'bash -n' which only
        checks syntax and does not require Docker."""
        steps = self.build.get("steps", [])
        for s in steps:
            if s.get("name") == "Validate metrics script":
                run = s.get("run", "")
                self.assertIn("bash -n", run,
                              "Validate step should use bash -n (syntax check only)")
                self.assertIn("scripts/ci-inspect-image.sh", run,
                              "Validate step should reference ci-inspect-image.sh")
                return
        self.fail("Validate metrics script step not found")
