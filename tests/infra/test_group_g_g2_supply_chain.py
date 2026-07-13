"""Group G G2 PR1 regression tests -- Trivy, SBOM, provenance, SARIF.

Covers:
  G2-01 -- Build and push uses sbom: true
  G2-02 -- Build and push uses provenance: true
  G2-03 -- Trivy scan step exists after Build and push (main push path)
  G2-04 -- Trivy scans the immutable digest image (main push), not a mutable tag
  G2-05 -- Trivy CRITICAL severity uses --exit-code 1 (fail build)
  G2-06 -- Trivy HIGH/MEDIUM/LOW first pass uses --exit-code 0
  G2-07 -- Trivy SARIF artifact is uploaded
  G2-08 -- Trivy SARIF is uploaded to GitHub Security on main push
  G2-09 -- CycloneDX SBOM artifact is extracted and uploaded
  G2-10 -- Build job has security-events: write permission
  G2-11 -- Build job has actions: read permission
  G2-12 -- .trivyignore file exists
  G2-13 -- PR-level security-scan job exists with Trivy CRITICAL gate
  G2-14 -- PR scans local (loaded) image, not pushed digest
  G2-15 -- Trivy JSON vulnerability report artifact exists
  G2-16 -- Trivy scanner image is pinned (NOT :latest)
  G2-17 -- GitHub Security SARIF upload only on main push (not PR)
  G2-18 -- CRITICAL gate ignores unfixed CVEs, while SARIF/JSON remain complete
  G2-19 -- gateway protobufjs lockfile version is fixed (>= 7.5.5)
  G2-20 -- gateway image applies Debian security upgrades in builder and runner

PR-only validation (no Docker daemon required):
  - YAML parsing of ci-cd.yml build job + security-scan job
  - Static analysis of workflow steps
"""

import os
import unittest

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CI_CD_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci-cd.yml")
TRIVYIGNORE_PATH = os.path.join(REPO_ROOT, ".trivyignore")
GATEWAY_PACKAGE_LOCK_PATH = os.path.join(REPO_ROOT, "gateway", "package-lock.json")
GATEWAY_DOCKERFILE_PATH = os.path.join(REPO_ROOT, "gateway", "Dockerfile")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_job(data, name):
    return data.get("jobs", {}).get(name, {})


def _get_steps(job):
    return job.get("steps", [])


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
        cls.steps = _get_steps(_get_job(_load_yaml(CI_CD_PATH), "build"))

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


# -- G2-03, G2-04, G2-05, G2-06: Trivy scan (main push) -----------------

class TestTrivyScanStep(unittest.TestCase):
    """Trivy scan must exist with correct severity gating."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_steps(_get_job(_load_yaml(CI_CD_PATH), "build"))

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
                      "G2-05: must have CRITICAL-only pass")
        self.assertIn("--exit-code 1", run,
                      "G2-05: CRITICAL pass must use --exit-code 1")

    def test_trivy_critical_gate_ignores_unfixed_only_once(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("--ignore-unfixed", run,
                      "G2-18: CRITICAL gate must ignore unfixed/fix_deferred CVEs")
        self.assertEqual(run.count("--ignore-unfixed"), 1,
                         "G2-18: --ignore-unfixed must only be on the CRITICAL gate, not SARIF/JSON")

    def test_trivy_full_severity_exit_code_0(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("--severity CRITICAL,HIGH,MEDIUM,LOW", run,
                      "G2-06: first pass must scan all severities")
        self.assertIn("--exit-code 0", run,
                      "G2-06: first pass must use --exit-code 0")

    def test_trivy_json_pass_exists(self):
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step)
        run = step.get("run", "")
        self.assertIn("--format json", run,
                      "G2-15: Trivy must have a JSON vulnerability report pass")


# -- G2-07, G2-08: SARIF -------------------------------------------------

class TestTrivySARIFUpload(unittest.TestCase):
    """SARIF must be uploaded as artifact and to GitHub Security."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_steps(_get_job(_load_yaml(CI_CD_PATH), "build"))

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
                      "G2-17: GitHub Security upload gates on main branch")
        self.assertIn("push", if_cond,
                      "G2-17: GitHub Security upload gates on push event")


# -- G2-15: JSON vulnerability report ------------------------------------

class TestTrivyJSONUpload(unittest.TestCase):
    """JSON vulnerability report must be uploaded."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_steps(_get_job(_load_yaml(CI_CD_PATH), "build"))

    def test_json_artifact_upload_exists(self):
        step = _step_by_name(self.steps, "Upload Trivy JSON artifact")
        self.assertIsNotNone(step, "G2-15: JSON artifact step missing")
        with_config = step.get("with", {})
        self.assertIn("json", with_config.get("path", ""),
                      "G2-15: JSON artifact path must be .json")
        self.assertEqual(with_config.get("if-no-files-found"), "error",
                         "G2-15: JSON artifact must error if no files")


# -- G2-09: CycloneDX SBOM -----------------------------------------------

class TestCycloneDXSBOM(unittest.TestCase):
    """SBOM must be extracted in CycloneDX format, before the CRITICAL gate."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _get_steps(_get_job(_load_yaml(CI_CD_PATH), "build"))

    def test_sbom_in_trivy_step_before_critical_gate(self):
        """G3a: SBOM is generated in Pass 3 (inside Trivy scan image step),
        before Pass 4 CRITICAL gate, so the SBOM file always exists even when
        the gate fails.  The standalone 'Extract CycloneDX SBOM' step is removed
        in favor of this inline pass."""
        step = _step_by_name(self.steps, "Trivy scan image")
        self.assertIsNotNone(step, "Trivy scan image step must exist")
        run = step.get("run", "")
        self.assertIn("cyclonedx", run,
                      "G2-09/G3a: Trivy step must generate CycloneDX SBOM inline")
        self.assertIn("sbom-${{ matrix.project }}.cdx.json", run,
                      "G2-09/G3a: SBOM output path must use matrix project name")
        # SBOM pass (Pass 3) must appear before --exit-code 1 (Pass 4)
        sbom_idx = run.index("cyclonedx")
        gate_idx = run.index("--exit-code 1")
        self.assertLess(sbom_idx, gate_idx,
                        "G3a: SBOM generation (Pass 3) must precede CRITICAL gate (Pass 4)")

    def test_sbom_artifact_upload_exists(self):
        step = _step_by_name(self.steps, "Upload SBOM artifact")
        self.assertIsNotNone(step, "G2-09: SBOM upload step missing")
        self.assertIn("cdx.json", step.get("with", {}).get("path", ""),
                      "G2-09: SBOM artifact path must be .cdx.json")

    def test_sbom_upload_strict_file_check(self):
        step = _step_by_name(self.steps, "Upload SBOM artifact")
        self.assertIsNotNone(step)
        self.assertEqual(step.get("with", {}).get("if-no-files-found"), "error",
                         "G2-09/G3a: SBOM upload must remain strict (error if missing)")

    def test_no_standalone_sbom_extract_step(self):
        """The old standalone 'Extract CycloneDX SBOM' step must not exist;
        SBOM is now inline in the Trivy scan image step."""
        step = _step_by_name(self.steps, "Extract CycloneDX SBOM")
        self.assertIsNone(step,
                          "G3a: standalone Extract CycloneDX SBOM step must be removed (inline now)")


# -- G2-10, G2-11: Permissions -------------------------------------------

class TestBuildJobPermissions(unittest.TestCase):
    """Build job must have security-events: write and actions: read."""

    @classmethod
    def setUpClass(cls):
        cls.build = _get_job(_load_yaml(CI_CD_PATH), "build")

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


# -- G2-13, G2-14: PR-level security-scan job ----------------------------

class TestPRSecurityScanJob(unittest.TestCase):
    """security-scan job must exist for PR-level Trivy scanning."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CI_CD_PATH)

    def test_security_scan_job_exists(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        self.assertTrue(job.get("name") or job.get("steps"),
                        "G2-13: security-scan job must exist")

    def test_security_scan_is_pull_request(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        if_cond = job.get("if", "")
        self.assertIn("pull_request", if_cond,
                      "G2-13: security-scan must gate on pull_request")

    def test_security_scan_builds_without_push(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        steps = _get_steps(job)
        build_step = _step_by_name(steps, "Build image (no push)")
        self.assertIsNotNone(build_step, "G2-14: must have 'Build image (no push)' step")
        with_config = build_step.get("with", {})
        self.assertFalse(with_config.get("push"),
                         "G2-14: PR build must have push: false")
        self.assertTrue(with_config.get("load"),
                        "G2-14: PR build must have load: true")

    def test_security_scan_has_trivy_critical_gate(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        steps = _get_steps(job)
        trivy_step = _step_by_name(steps, "Trivy scan PR image")
        self.assertIsNotNone(trivy_step, "G2-13: PR must have Trivy scan step")
        run = trivy_step.get("run", "")
        self.assertIn("--severity CRITICAL", run,
                      "G2-13: PR Trivy must have CRITICAL gate")
        self.assertIn("--exit-code 1", run,
                      "G2-13: PR Trivy CRITICAL must fail build")

    def test_security_scan_critical_gate_ignores_unfixed_only_once(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        steps = _get_steps(job)
        trivy_step = _step_by_name(steps, "Trivy scan PR image")
        self.assertIsNotNone(trivy_step, "G2-18: PR must have Trivy scan step")
        run = trivy_step.get("run", "")
        self.assertIn("--ignore-unfixed", run,
                      "G2-18: PR CRITICAL gate must ignore unfixed/fix_deferred CVEs")
        self.assertEqual(run.count("--ignore-unfixed"), 1,
                         "G2-18: PR --ignore-unfixed must only be on the CRITICAL gate, not SARIF/JSON")

    def test_security_scan_has_sbom_before_critical_gate(self):
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        steps = _get_steps(job)
        trivy_step = _step_by_name(steps, "Trivy scan PR image")
        self.assertIsNotNone(trivy_step, "G2-09/G3a: PR must have Trivy scan step")
        run = trivy_step.get("run", "")
        self.assertIn("cyclonedx", run,
                      "G2-09/G3a: PR Trivy must generate CycloneDX SBOM inline")
        # SBOM pass must appear before --exit-code 1
        sbom_idx = run.index("cyclonedx")
        gate_idx = run.index("--exit-code 1")
        self.assertLess(sbom_idx, gate_idx,
                        "G3a: PR SBOM generation must precede CRITICAL gate")

    def test_security_scan_no_github_security_upload(self):
        """PR job must NOT upload SARIF to GitHub Security (artifact only)."""
        data = _load_yaml(CI_CD_PATH)
        job = _get_job(data, "security-scan")
        steps = _get_steps(job)
        for s in steps:
            uses = s.get("uses", "")
            if "upload-sarif" in uses:
                self.fail("G2-17: PR security-scan must not use upload-sarif")


# -- G2-16: Trivy image pinned (not :latest) -----------------------------

class TestTrivyImagePinned(unittest.TestCase):
    """Trivy scanner image must be pinned, not :latest."""

    def test_no_trivy_latest_in_ci(self):
        with open(CI_CD_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("aquasec/trivy:latest", content,
                         "G2-16: Trivy image must not be :latest")
        self.assertNotIn("ghcr.io/aquasecurity/trivy:latest", content,
                         "G2-16: Trivy image must not be :latest")

    def test_trivy_version_pinned(self):
        with open(CI_CD_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("trivy:0.59.1", content,
                      "G2-16: Trivy image must be pinned to 0.59.1")


# -- G2-19: Fixable dependency CVE regression ----------------------------

class TestGatewayProtobufjsPatched(unittest.TestCase):
    """gateway package-lock must not regress protobufjs below fixed version."""

    def test_gateway_protobufjs_is_fixed_version(self):
        data = _load_yaml(GATEWAY_PACKAGE_LOCK_PATH)
        protobuf = data.get("packages", {}).get("node_modules/protobufjs", {})
        version = protobuf.get("version", "")
        self.assertRegex(version, r"^(7\.(5\.[5-9]|[6-9]\.)|[89]\.)",
                         "G2-19: protobufjs must be >= 7.5.5 to avoid CVE-2026-41242")

    def test_gateway_protobufjs_uses_official_registry(self):
        data = _load_yaml(GATEWAY_PACKAGE_LOCK_PATH)
        protobuf = data.get("packages", {}).get("node_modules/protobufjs", {})
        resolved = protobuf.get("resolved", "")
        self.assertIn("https://registry.npmjs.org/protobufjs/", resolved,
                      "G2-19: protobufjs resolved URL must use official npm registry")


# -- G2-20: Fixable OS CVE regression ------------------------------------

class TestGatewayDebianSecurityUpgrades(unittest.TestCase):
    """gateway Dockerfile must apply Debian security upgrades before Trivy gate."""

    @classmethod
    def setUpClass(cls):
        with open(GATEWAY_DOCKERFILE_PATH, "r", encoding="utf-8") as fh:
            cls.content = fh.read()
        cls.builder_stage = cls.content.split("FROM node:20-bullseye AS runner")[0]
        cls.runner_stage = cls.content.split("FROM node:20-bullseye AS runner", 1)[1]

    def test_gateway_builder_runs_apt_upgrade(self):
        self.assertIn("apt-get upgrade -y", self.builder_stage,
                      "G2-20: builder stage must apply Debian security upgrades")

    def test_gateway_runner_runs_apt_upgrade(self):
        self.assertIn("apt-get upgrade -y", self.runner_stage,
                      "G2-20: runner stage must apply Debian security upgrades")

    def test_gateway_runner_keeps_required_openssl_runtime(self):
        self.assertIn("apt-get install -y --no-install-recommends openssl libssl1.1",
                      self.runner_stage,
                      "G2-20: runner must explicitly include Prisma OpenSSL runtime packages")

    def test_gateway_apt_lists_removed_after_upgrade(self):
        self.assertGreaterEqual(self.content.count("rm -rf /var/lib/apt/lists/*"), 2,
                                "G2-20: both stages must remove apt lists after upgrades")
