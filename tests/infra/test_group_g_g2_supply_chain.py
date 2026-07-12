"""Group G G2 PR1 regression tests -- Trivy, SBOM, provenance, SARIF.

Covers:
  G2-01 -- Build and push uses sbom: true
  G2-02 -- Build and push uses provenance: true
  G2-03 -- Trivy scan step exists after Build and push
  G2-04 -- Trivy scans the immutable digest image, not a mutable tag
  G2-05 -- Trivy CRITICAL severity uses --exit-code 1 (fail build)
  G2-06 -- Trivy HIGH/MEDIUM/LOW first pass uses --exit-code 0
  G2-07 -- Trivy SARIF artifact is uploaded
  G2-08 -- Trivy SARIF is uploaded to GitHub Security on main push
  G2-09 -- CycloneDX SBOM artifact is extracted and uploaded
  G2-10 -- Build job has security-events: write permission
  G2-11 -- Build job has actions: read permission
  G2-12 -- .trivyignore file exists

PR-only validation (no Docker daemon required):
  - YAML parsing of ci-cd.yml build job
  - Static analysis of workflow steps
"""

import os
import unittest

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CI_CD_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci-cd.yml")
TRIVYIGNORE_PATH = os.path.join(REPO_ROOT, ".trivyignore")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_build_job(data):
    return data.get("jobs", {}).get("build", {})


def _get_build_steps(data):
    return _get_build_job(data).get("steps", [])


def _step_by_name(steps, name):
    for s in steps:
        if s.get("name") == name:
            return s
    return None


# -- G2-01, G2-02: SBOM + provenance -------------------------------------

class TestSBOMAndProvenance(unittest.TestCase):
    """build-push-action must enable sbom and provenance."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_build_push_has_sbom_true(self):
        step = _step_by_name(self.steps, "Build and push")
        self.assertIsNotNone(step)
        self.assertTrue(step.get("with", {}).get("sbom"),
                        "G2-01: build-push-action must have sbom: true")

    def test_build_push_has_provenance_true(self):
        step = _step_by_name(self.steps, "Build and push")
        self.assertIsNotNone(step)
        self.assertTrue(step.get("with", {}).get("provenance"),
                        "G2-02: build-push-action must have provenance: true")


# -- G2-03, G2-04, G2-05, G2-06: Trivy scan -----------------------------

class TestTrivyScanStep(unittest.TestCase):
    """Trivy scan must exist with correct severity gating."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_trivy_scan_step_exists(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step, "G2-03: Trivy scan image step missing")

    def test_trivy_after_build_push(self):
        names = [s.get("name", "") for s in self.steps]
        build_idx = names.index("Build and push")
        trivy_idx = names.index("Trivy scan image")
        self.assertLess(build_idx, trivy_idx,
                        "G2-03: Trivy scan must be after Build and push")

    def test_trivy_scans_digest_not_tag(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("@${{ steps.build-push.outputs.digest }}", run,
                      "G2-04: Trivy must scan by immutable digest")
        self.assertIn("IMAGE_FULL", run,
                      "G2-04: Trivy must reference IMAGE_FULL with @digest")

    def test_trivy_critical_exit_code_1(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("--severity CRITICAL", run,
                      "G2-05: must have CRITICAL-only second pass")
        self.assertIn("--exit-code 1", run,
                      "G2-05: CRITICAL pass must use --exit-code 1")

    def test_trivy_full_severity_exit_code_0(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("--severity CRITICAL,HIGH,MEDIUM,LOW", run,
                      "G2-06: first pass must scan all severities")
        self.assertIn("--exit-code 0", run,
                      "G2-06: first pass must use --exit-code 0")


# -- G2-07, G2-08: SARIF -------------------------------------------------

class TestTrivySARIFUpload(unittest.TestCase):
    """SARIF must be uploaded as artifact and to GitHub Security."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_sarif_artifact_upload_exists(self):
        step = _step_by_name(self.steps, "Upload Trivy SARIF artifact")
        self.assertIsNotNone(step, "G2-07: SARIF artifact step missing")
        self.assertEqual(step.get("with", {}).get("if-no-files-found"), "error",
                         "G2-07: SARIF artifact must error if no files")

    def test_sarif_github_security_upload_exists(self):
        step = _step_by_name(self.steps, "Upload Trivy SARIF to GitHub Security")
        self.assertIsNotNone(step, "G2-08: GitHub Security upload step missing")
        self.assertIn("upload-sarif", step.get("uses", ""),
                      "G2-08: must use upload-sarif action")
        self.assertIn("category", step.get("with", {}),
                      "G2-08: must set category for SARIF dedup")

    def test_sarif_github_security_main_push_only(self):
        step = _step_by_name(self.steps, "Upload Trivy SARIF to GitHub Security")
        self.assertIsNotNone(step)
        if_cond = step.get("if", "")
        self.assertIn("main", if_cond,
                      "G2-08: GitHub Security upload gates on main branch")
        self.assertIn("push", if_cond,
                      "G2-08: GitHub Security upload gates on push event")


# -- G2-09: CycloneDX SBOM -----------------------------------------------

class TestCycloneDXSBOM(unittest.TestCase):
    """SBOM must be extracted in CycloneDX format."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_build_steps(_load_yaml(CI_CD_PATH))

    def test_sbom_extract_step_exists(self):
        step = _step_by_name(self.steps, "Extract CycloneDX SBOM")
        self.assertIsNotNone(step, "G2-09: SBOM extract step missing")

    def test_sbom_uses_cyclonedx_format(self):
        step = _step_by_name(self.steps, "Extract CycloneDX SBOM")
        self.assertIsNotNone(step)
        self.assertIn("cyclonedx", step.get("run", ""),
                      "G2-09: SBOM must use CycloneDX format")

    def test_sbom_artifact_upload_exists(self):
        step = _step_by_name(self.steps, "Upload SBOM artifact")
        self.assertIsNotNone(step, "G2-09: SBOM upload step missing")
        self.assertIn("cdx.json", step.get("with", {}).get("path", ""),
                      "G2-09: SBOM artifact path must be .cdx.json")


# -- G2-10, G2-11: Permissions -------------------------------------------

class TestBuildJobPermissions(unittest.TestCase):
    """Build job must have security-events: write and actions: read."""

    @classmethod
    def setUpClass(cls):
        cls.build = _get_build_job(_load_yaml(CI_CD_PATH))

    def test_security_events_write(self):
        perms = self.build.get("permissions", {})
        self.assertEqual(perms.get("security-events"), "write",
                         "G2-10: security-events: write required")

    def test_actions_read(self):
        perms = self.build.get("permissions", {})
        self.assertEqual(perms.get("actions"), "read",
                         "G2-11: actions: read required")


# -- G2-12: .trivyignore -------------------------------------------------

class TestTrivyignore(unittest.TestCase):
    """.trivyignore must exist with a header comment."""

    def test_trivyignore_exists(self):
        self.assertTrue(os.path.isfile(TRIVYIGNORE_PATH),
                        "G2-12: .trivyignore must exist")

    def test_trivyignore_has_header(self):
        with open(TRIVYIGNORE_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("Trivy vulnerability exceptions", content,
                      "G2-12: .trivyignore must have a header comment")