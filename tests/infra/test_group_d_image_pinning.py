"""D2 regression tests — Compose image pinning and OPA version convergence.

These tests parse docker-compose.yml, docker-compose.chaos.yml, and the
CI workflow files directly with PyYAML. They do NOT run `docker compose config`
(the host has no Docker), so variable interpolation is NOT resolved — we
assert on the literal YAML structure.

Covers:
  D2-B1  — No unexempted :latest tags in docker-compose.yml / docker-compose.chaos.yml
  D2-S1  — OPA version is 0.70.0 in both main compose and chaos compose
  D2-AL  — CI workflows reference OPA 0.70.0 (ci-cd.yml, tenant-isolation.yml)
  D2-HR  — Helm values tag: latest is documented as deferred (not a D2 blocker)
"""

import os
import re
import unittest

import yaml

COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.yml"
)
CHAOS_COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.chaos.yml"
)
CICD_WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows", "ci-cd.yml"
)
TENANT_ISOLATION_WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows", "tenant-isolation.yml"
)
HELM_VALUES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "deploy", "helm", "enterprise-crm"
)

EXPECTED_OPA_VERSION = "0.70.0"
OPA_IMAGE_PREFIX = "openpolicyagent/opa:"

# -- D2-B1: No unexempted :latest tags ---------------------------------

class TestNoUnexemptedLatestTags(unittest.TestCase):
    """docker-compose.yml and docker-compose.chaos.yml must have no
    unexempted `:latest` image tags."""

    @staticmethod
    def _image_lines(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("image:"):
                    yield stripped

    def _assert_no_latest(self, path, label):
        violations = []
        for line in self._image_lines(path):
            # Match 'image: something:latest' — allow comments containing ':latest'
            if re.search(r"image:\s+\S+:latest\s*$", line):
                violations.append(line.strip())
        if violations:
            self.fail(
                f"{label} ({path}) has unexempted :latest image tags:\n"
                + "\n".join(f"  {v}" for v in violations)
            )

    def test_main_compose_no_latest_tags(self):
        self._assert_no_latest(COMPOSE_PATH, "docker-compose.yml")

    def test_chaos_compose_no_latest_tags(self):
        self._assert_no_latest(CHAOS_COMPOSE_PATH, "docker-compose.chaos.yml")


# -- D2-S1: OPA version convergence -----------------------------------

class TestOPAVersionConvergence(unittest.TestCase):
    """main compose, chaos compose, CI workflows all use OPA {EXPECTED_OPA_VERSION}."""

    def _load_compose(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def _find_opa_image(self, compose_data):
        services = compose_data.get("services") or {}
        opa = services.get("opa") or {}
        return opa.get("image", "")

    def test_main_compose_opa_version(self):
        data = self._load_compose(COMPOSE_PATH)
        image = self._find_opa_image(data)
        self.assertTrue(
            image.startswith(OPA_IMAGE_PREFIX),
            f"main compose OPA image '{image}' does not start with '{OPA_IMAGE_PREFIX}'"
        )
        version = image[len(OPA_IMAGE_PREFIX):]
        self.assertEqual(
            version, EXPECTED_OPA_VERSION,
            f"main compose OPA version is {version!r}, expected {EXPECTED_OPA_VERSION!r}"
        )

    def test_chaos_compose_opa_version(self):
        data = self._load_compose(CHAOS_COMPOSE_PATH)
        image = self._find_opa_image(data)
        self.assertTrue(
            image.startswith(OPA_IMAGE_PREFIX),
            f"chaos compose OPA image '{image}' does not start with '{OPA_IMAGE_PREFIX}'"
        )
        version = image[len(OPA_IMAGE_PREFIX):]
        self.assertEqual(
            version, EXPECTED_OPA_VERSION,
            f"chaos compose OPA version is {version!r}, expected {EXPECTED_OPA_VERSION!r}"
        )

    def _find_opa_version_in_workflow(self, path):
        """Parse a GitHub Actions workflow YAML for the OPA setup version."""
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        jobs = data.get("jobs") or {}
        for _job_name, job in jobs.items():
            steps = job.get("steps") or []
            for step in steps:
                uses = step.get("uses", "")
                if "setup-opa" in uses:
                    version = step.get("with", {}).get("version", "")
                    if version:
                        return version
        return None

    def test_ci_cd_workflow_opa_version(self):
        version = self._find_opa_version_in_workflow(CICD_WORKFLOW_PATH)
        self.assertIsNotNone(
            version,
            f"Could not find open-policy-agent/setup-opa step in {CICD_WORKFLOW_PATH}"
        )
        self.assertEqual(
            version, EXPECTED_OPA_VERSION,
            f"CI/CD workflow OPA version is {version!r}, expected {EXPECTED_OPA_VERSION!r}"
        )

    def test_tenant_isolation_workflow_opa_version(self):
        version = self._find_opa_version_in_workflow(TENANT_ISOLATION_WORKFLOW_PATH)
        self.assertIsNotNone(
            version,
            f"Could not find open-policy-agent/setup-opa step in {TENANT_ISOLATION_WORKFLOW_PATH}"
        )
        self.assertEqual(
            version, EXPECTED_OPA_VERSION,
            f"tenant-isolation workflow OPA version is {version!r}, expected {EXPECTED_OPA_VERSION!r}"
        )


# -- D2-HR: Helm latest fallback is deferred -------------------------

class TestHelmLatestDeferred(unittest.TestCase):
    """D2 deferred Helm `latest` fallbacks to Phase 4.
    E1 replaced `tag: latest` with `tag: ""` + `required` fail-fast in templates.
    The Helm fallback is now empty-string instead of `latest` — CI still overrides.
    """

    def _find_latest_tags(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        tags = []
        images = data.get("images") or {}
        for svc in ["frontend", "gateway", "agents"]:
            tag = images.get(svc, {}).get("tag", "")
            if tag == "latest":
                tags.append(svc)
        return tags

    def _find_empty_tags(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        tags = []
        images = data.get("images") or {}
        for svc in ["frontend", "gateway", "agents"]:
            tag = images.get(svc, {}).get("tag", "")
            if tag == "":
                tags.append(svc)
        return tags

    def test_values_yaml_tag_is_empty_not_latest(self):
        """E1: values.yaml tags are empty string (fail-fast), not 'latest'."""
        path = os.path.join(HELM_VALUES_DIR, "values.yaml")
        latest_tags = self._find_latest_tags(path)
        self.assertEqual(len(latest_tags), 0,
                         f"values.yaml: should have ZERO 'tag: latest', found {latest_tags}")
        empty_tags = self._find_empty_tags(path)
        self.assertEqual(len(empty_tags), 3,
                         f"values.yaml: expected 3 empty tags, found {len(empty_tags)}")

    def test_values_staging_yaml_tag_is_empty_not_latest(self):
        """E1: staging values tags are empty string, not 'latest'."""
        path = os.path.join(HELM_VALUES_DIR, "values-staging.yaml")
        latest_tags = self._find_latest_tags(path)
        self.assertEqual(len(latest_tags), 0,
                         f"values-staging.yaml: should have ZERO 'tag: latest', found {latest_tags}")
        empty_tags = self._find_empty_tags(path)
        self.assertEqual(len(empty_tags), 3,
                         f"values-staging.yaml: expected 3 empty tags, found {len(empty_tags)}")

    def test_values_production_yaml_tag_is_empty_not_latest(self):
        """E1: production values tags are empty string, not 'latest'."""
        path = os.path.join(HELM_VALUES_DIR, "values-production.yaml")
        latest_tags = self._find_latest_tags(path)
        self.assertEqual(len(latest_tags), 0,
                         f"values-production.yaml: should have ZERO 'tag: latest', found {latest_tags}")
        empty_tags = self._find_empty_tags(path)
        self.assertEqual(len(empty_tags), 3,
                         f"values-production.yaml: expected 3 empty tags, found {len(empty_tags)}")

    def test_ci_overrides_helm_tags(self):
        """G1: CI deploy steps now use --set images.*.digest=... (immutable digest)
        instead of --set images.*.tag=${{ github.sha }} (mutable tag).
        Digest pinning is stronger than tag-based pinning."""
        with open(CICD_WORKFLOW_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        # staging deploy uses digest, not tag
        has_staging_digest = (
            "--set images.gateway.digest=" in content
            and "--set images.frontend.digest=" in content
            and "--set images.agents.digest=" in content
        )
        self.assertTrue(
            has_staging_digest,
            "CI/CD staging deploy must override Helm images with digest (G1 digest pinning)"
        )
        # production deploy — same pattern
        staging_count = content.count("--set images.gateway.digest=")
        self.assertGreaterEqual(
            staging_count, 2,
            "Expected at least 2 occurrences (staging + production) of "
            "--set images.gateway.digest="
        )
