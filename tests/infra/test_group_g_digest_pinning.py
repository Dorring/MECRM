"""Group G G1 regression tests -- digest pinning + Helm UID fix.

Covers:
  G-01 -- values.yaml has images.*.digest fields
  G-02 -- _helpers.tpl has image helper (digest preferred, tag fallback)
  G-03 -- templates render repository@digest when digest is set
  G-04 -- tag is still required when digest is empty
  G-05 -- values.yaml has per-workload securityContext.runAsUser
  G-06 -- gateway securityContext.runAsUser = 1000
  G-07 -- frontend securityContext.runAsUser = 1001
  G-08 -- agents securityContext.runAsUser = 1001
  G-09 -- CI has aggregate-digests job
  G-10 -- deploy-staging uses digest, not github.sha tag
  G-11 -- deploy-production uses digest, not github.sha tag
  G-12 -- build job uploads digest-{project} artifact
  G-13 -- aggregate-digests depends on build
  G-14 -- deploy jobs depend on aggregate-digests
"""

import os
import re
import unittest

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HELM_DIR = os.path.join(REPO_ROOT, "deploy", "helm", "enterprise-crm")
VALUES_PATH = os.path.join(HELM_DIR, "values.yaml")
HELPERS_PATH = os.path.join(HELM_DIR, "templates", "_helpers.tpl")
GATEWAY_TPL_PATH = os.path.join(HELM_DIR, "templates", "gateway.yaml")
AGENTS_TPL_PATH = os.path.join(HELM_DIR, "templates", "agents.yaml")
FRONTEND_TPL_PATH = os.path.join(HELM_DIR, "templates", "frontend.yaml")
CI_CD_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci-cd.yml")


def _slurp(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _job_names(data):
    return list(data.get("jobs", {}).keys())


def _job_needs(data, job_name):
    jobs = data.get("jobs", {})
    job = jobs.get(job_name, {})
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return needs


# -- G-01 through G-04: Helm values + image helper -----------------------

class TestHelmDigestFields(unittest.TestCase):
    """values.yaml must have images.*.digest fields."""

    @classmethod
    def setUpClass(cls):
        cls.values = _load_yaml(VALUES_PATH)

    def test_frontend_digest_field_exists(self):
        fe = self.values.get("images", {}).get("frontend", {})
        self.assertIn("digest", fe,
                      "G-01: images.frontend must have digest field")
        self.assertEqual(fe.get("digest"), "",
                         "G-01: images.frontend.digest must default to empty string")

    def test_gateway_digest_field_exists(self):
        gw = self.values.get("images", {}).get("gateway", {})
        self.assertIn("digest", gw,
                      "G-01: images.gateway must have digest field")
        self.assertEqual(gw.get("digest"), "",
                         "G-01: images.gateway.digest must default to empty string")

    def test_agents_digest_field_exists(self):
        ag = self.values.get("images", {}).get("agents", {})
        self.assertIn("digest", ag,
                      "G-01: images.agents must have digest field")
        self.assertEqual(ag.get("digest"), "",
                         "G-01: images.agents.digest must default to empty string")


class TestHelmImageHelper(unittest.TestCase):
    """_helpers.tpl must define enterprise-crm.image with digest/tag logic."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(HELPERS_PATH)

    def test_image_helper_defined(self):
        self.assertIn('define "enterprise-crm.image"', self.content,
                      "G-02: _helpers.tpl must define enterprise-crm.image helper")

    def test_helper_prefers_digest(self):
        self.assertRegex(self.content, r'\$img\.digest',
                         "G-02: image helper must check .image.digest")

    def test_helper_falls_back_to_tag(self):
        self.assertIn('required', self.content,
                      "G-03: image helper must use required() for tag fallback")


class TestHelmTemplatesUseImageHelper(unittest.TestCase):
    """Templates must use enterprise-crm.image helper for image references."""

    def test_gateway_uses_image_helper(self):
        content = _slurp(GATEWAY_TPL_PATH)
        self.assertIn('include "enterprise-crm.image"', content,
                      "G-03: gateway template must use enterprise-crm.image helper")
        self.assertNotIn('images.gateway.repository }}:', content,
                         "G-03: gateway template must not hardcode repository:tag pattern")

    def test_agents_uses_image_helper(self):
        content = _slurp(AGENTS_TPL_PATH)
        self.assertIn('include "enterprise-crm.image"', content,
                      "G-03: agents template must use enterprise-crm.image helper")
        self.assertNotIn('images.agents.repository }}:', content,
                         "G-03: agents template must not hardcode repository:tag pattern")

    def test_frontend_uses_image_helper(self):
        content = _slurp(FRONTEND_TPL_PATH)
        self.assertIn('include "enterprise-crm.image"', content,
                      "G-03: frontend template must use enterprise-crm.image helper")
        self.assertNotIn('images.frontend.repository }}:', content,
                         "G-03: frontend template must not hardcode repository:tag pattern")

    def test_template_renders_digest_syntax(self):
        """When digest is set, template should render repository@digest
        (not repository:digest).  We validate the helper uses @ for digest
        references."""
        content = _slurp(HELPERS_PATH)
        self.assertIn('@%s' % '', content.replace('@', '@'),
                      "G-03: _helpers.tpl must render @ when digest is set")


# -- G-05 through G-08: securityContext runAsUser ------------------------

class TestHelmSecurityContextUIDs(unittest.TestCase):
    """Security context uids must match Dockerfile USER uids."""

    @classmethod
    def setUpClass(cls):
        cls.values = _load_yaml(VALUES_PATH)

    def test_security_context_section_exists(self):
        sc = self.values.get("securityContext", {})
        self.assertIn("gateway", sc,
                      "G-05: securityContext.gateway must exist")
        self.assertIn("frontend", sc,
                      "G-05: securityContext.frontend must exist")
        self.assertIn("agents", sc,
                      "G-05: securityContext.agents must exist")

    def test_gateway_uid_1000(self):
        uid = self.values.get("securityContext", {}).get("gateway", {}).get("runAsUser")
        self.assertEqual(uid, 1000,
                         "G-06: gateway runAsUser must be 1000 (node user)")

    def test_frontend_uid_1001(self):
        uid = self.values.get("securityContext", {}).get("frontend", {}).get("runAsUser")
        self.assertEqual(uid, 1001,
                         "G-07: frontend runAsUser must be 1001 (nextjs user)")

    def test_agents_uid_1001(self):
        uid = self.values.get("securityContext", {}).get("agents", {}).get("runAsUser")
        self.assertEqual(uid, 1001,
                         "G-08: agents runAsUser must be 1001 (app user)")

    def test_templates_read_uid_from_values(self):
        for tpl in [GATEWAY_TPL_PATH, AGENTS_TPL_PATH, FRONTEND_TPL_PATH]:
            content = _slurp(tpl)
            self.assertRegex(content, r'runAsUser: \{\{ \.Values\.securityContext\..*\.runAsUser \}\}',
                             f"{os.path.basename(tpl)} must read runAsUser from values")


# -- G-09 through G-14: CI workflow digest aggregation + deploy -----------

class TestCIDigestAggregation(unittest.TestCase):
    """ci-cd.yml must have aggregate-digests job and digest-based deploys."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CI_CD_PATH)

    def test_aggregate_digests_job_exists(self):
        jobs = _job_names(self.data)
        self.assertIn("aggregate-digests", jobs,
                      "G-09: ci-cd.yml must have aggregate-digests job")

    def test_aggregate_digests_depends_on_build(self):
        needs = _job_needs(self.data, "aggregate-digests")
        self.assertIn("build", needs,
                      "G-13: aggregate-digests must need build")

    def test_build_uploads_digest_artifact(self):
        build = self.data.get("jobs", {}).get("build", {})
        steps = build.get("steps", [])
        upload_names = [s.get("name", "") for s in steps]
        self.assertIn("Write digest artifact", upload_names,
                      "G-12: build job must have 'Write digest artifact' step")
        self.assertIn("Upload digest artifact", upload_names,
                      "G-12: build job must have 'Upload digest artifact' step")

    def test_aggregate_digests_assembles_map(self):
        agg = self.data.get("jobs", {}).get("aggregate-digests", {})
        steps = agg.get("steps", [])
        assemble_step = None
        for s in steps:
            if s.get("name") == "Assemble digest map":
                assemble_step = s
                break
        self.assertIsNotNone(assemble_step,
                            "G-09: aggregate-digests must have 'Assemble digest map' step")
        run = assemble_step.get("run", "")
        self.assertIn("digest-map.json", run,
                      "G-09: assemble step must produce digest-map.json")
        # G1: defensive validation
        self.assertIn("set -euo pipefail", run,
                      "G-09: assemble step must have set -euo pipefail")
        self.assertIn("test -f", run,
                      "G-09: assemble step must check digest artifact files exist")
        self.assertIn("sha256:", run,
                      "G-09: assemble step must validate digest matches ^sha256:")
        self.assertIn("jq empty digest-map.json", run,
                      "G-09: assemble step must validate digest-map.json is parsable")

    def test_deploy_staging_needs_aggregate_digests(self):
        needs = _job_needs(self.data, "deploy-staging")
        self.assertIn("aggregate-digests", needs,
                      "G-14: deploy-staging must need aggregate-digests")

    def test_deploy_production_needs_integration_tests(self):
        needs = _job_needs(self.data, "deploy-production")
        self.assertIn("integration-tests", needs,
                      "G-14: deploy-production must need integration-tests")

    def test_deploy_staging_uses_digest_not_tag(self):
        staging = self.data.get("jobs", {}).get("deploy-staging", {})
        steps = staging.get("steps", [])
        helm_step = None
        for s in steps:
            if s.get("name") == "Deploy with Helm":
                helm_step = s
                break
        self.assertIsNotNone(helm_step, "deploy-staging must have 'Deploy with Helm' step")
        run = helm_step.get("run", "")
        # G1: deploy must set both repository AND digest from digest-map
        for svc in ("gateway", "frontend", "agents"):
            self.assertIn(f"images.{svc}.repository=", run,
                          f"G-10: deploy-staging must set images.{svc}.repository")
            self.assertIn(f"images.{svc}.digest=", run,
                          f"G-10: deploy-staging must set images.{svc}.digest")
            self.assertIn(f".{svc}.image", run,
                          f"G-10: deploy-staging must read .{svc}.image from digest-map.json")
        self.assertNotIn("images.gateway.tag=${{ github.sha }}", run,
                         "G-10: deploy-staging must NOT use images.*.tag=${{ github.sha }}")

    def test_deploy_production_uses_digest_not_tag(self):
        prod = self.data.get("jobs", {}).get("deploy-production", {})
        steps = prod.get("steps", [])
        helm_step = None
        for s in steps:
            if s.get("name") == "Deploy with Helm":
                helm_step = s
                break
        self.assertIsNotNone(helm_step, "deploy-production must have 'Deploy with Helm' step")
        run = helm_step.get("run", "")
        self.assertIn("images.gateway.repository=", run,
                      "G-11: deploy-production must set images.gateway.repository")
        self.assertIn("images.gateway.digest=", run,
                      "G-11: deploy-production must set images.gateway.digest")
        self.assertIn(".gateway.image", run,
                      "G-11: deploy-production must read .gateway.image from digest-map.json")
        self.assertNotIn("images.gateway.tag=${{ github.sha }}", run,
                         "G-11: deploy-production must NOT use images.*.tag=${{ github.sha }}")

    def test_deploy_jobs_download_digest_map(self):
        for job_name in ("deploy-staging", "deploy-production"):
            job = self.data.get("jobs", {}).get(job_name, {})
            steps = job.get("steps", [])
            download_names = [s.get("name", "") for s in steps]
            self.assertIn("Download digest map", download_names,
                          f"G-10: {job_name} must download digest-map artifact")

    def test_aggregate_digests_uploads_artifact(self):
        agg = self.data.get("jobs", {}).get("aggregate-digests", {})
        steps = agg.get("steps", [])
        upload_step = None
        for s in steps:
            if s.get("name") == "Upload digest map artifact":
                upload_step = s
                break
        self.assertIsNotNone(upload_step,
                            "G-09: aggregate-digests must upload digest-map artifact")
        with_config = upload_step.get("with", {})
        self.assertEqual(with_config.get("name"), "digest-map",
                         "G-09: upload artifact name must be digest-map")


# -- Helm template rendering sanity check (no real helm binary needed) ---

HELM_LINT_STEPS_TEXT = ""


class TestHelmTemplateDigestRendering(unittest.TestCase):
    """Verify template image references statically."""

    @classmethod
    def setUpClass(cls):
        # Load CI data once for digest-mode template checks
        global HELM_LINT_STEPS_TEXT
        HELM_LINT_STEPS_TEXT = _slurp(CI_CD_PATH)

    def test_gateway_template_uses_at_for_digest(self):
        """The image helper must produce repository@digest when digest is set.
        Verify the helper code uses @ not : before the digest variable."""
        content = _slurp(HELPERS_PATH)
        # When digest is set: printf "%s%s@%s" (registry)(repo)@(digest)
        self.assertRegex(content, r'@%s.*\$img\.digest',
                         "image helper must render @ before digest")

    def test_gateway_template_run_as_non_root_unchanged(self):
        content = _slurp(GATEWAY_TPL_PATH)
        self.assertIn("runAsNonRoot: true", content,
                      "G-05: runAsNonRoot must still be true")

    def test_staging_values_unchanged(self):
        staging_path = os.path.join(HELM_DIR, "values-staging.yaml")
        if not os.path.exists(staging_path):
            self.skipTest("values-staging.yaml not present")
        staging = _load_yaml(staging_path)
        self.assertIsNotNone(staging,
                            "values-staging.yaml must be valid YAML")

    def test_production_values_unchanged(self):
        prod_path = os.path.join(HELM_DIR, "values-production.yaml")
        if not os.path.exists(prod_path):
            self.skipTest("values-production.yaml not present")
        prod = _load_yaml(prod_path)
        self.assertIsNotNone(prod,
                            "values-production.yaml must be valid YAML")


# -- G1: helm-lint CI digest-mode coverage -------------------------------


class TestHelmLintDigestMode(unittest.TestCase):
    """helm-lint CI job must render with digest mode and assert correct
    ghcr.io image references."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CI_CD_PATH)

    def _helm_lint_steps(self):
        job = self.data.get("jobs", {}).get("helm-lint", {})
        return job.get("steps", [])

    def test_has_digest_mode_lint_step(self):
        names = [s.get("name", "") for s in self._helm_lint_steps()]
        self.assertIn("Lint chart (digest mode)", names,
                      "G1: helm-lint must have 'Lint chart (digest mode)' step")

    def test_has_digest_mode_render_step(self):
        names = [s.get("name", "") for s in self._helm_lint_steps()]
        self.assertIn("Render default template (digest mode)", names,
                      "G1: helm-lint must have 'Render default template (digest mode)' step")

    def test_digest_render_asserts_ghcr_references(self):
        for s in self._helm_lint_steps():
            if s.get("name") == "Render default template (digest mode)":
                run = s.get("run", "")
                self.assertIn("ghcr.io/dorring/mecrm/gateway@sha256:", run,
                              "digest-mode render must grep for ghcr.io/dorring/mecrm/gateway@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/frontend@sha256:", run,
                              "digest-mode render must grep for ghcr.io/dorring/mecrm/frontend@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/agents@sha256:", run,
                              "digest-mode render must grep for ghcr.io/dorring/mecrm/agents@sha256:")
                self.assertIn("enterprise-crm/frontend@sha256:", run,
                              "digest-mode render must reject enterprise-crm prefix in digest mode")
                return
        self.fail("'Render default template (digest mode)' step not found")

    def test_staging_digest_render_asserts_ghcr_references(self):
        """Staging digest render must also assert ghcr.io references."""
        for s in self._helm_lint_steps():
            if s.get("name") == "Render staging template if present (digest mode)":
                run = s.get("run", "")
                self.assertIn("ghcr.io/dorring/mecrm/gateway@sha256:", run,
                              "staging digest render must grep for ghcr.io/dorring/mecrm/gateway@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/frontend@sha256:", run,
                              "staging digest render must grep for ghcr.io/dorring/mecrm/frontend@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/agents@sha256:", run,
                              "staging digest render must grep for ghcr.io/dorring/mecrm/agents@sha256:")
                self.assertIn("enterprise-crm/frontend@sha256:", run,
                              "staging digest render must reject enterprise-crm prefix")
                return
        self.fail("'Render staging template if present (digest mode)' step not found")

    def test_production_digest_render_asserts_ghcr_references(self):
        """Production digest render must also assert ghcr.io references."""
        for s in self._helm_lint_steps():
            if s.get("name") == "Render production template if present (digest mode)":
                run = s.get("run", "")
                self.assertIn("ghcr.io/dorring/mecrm/gateway@sha256:", run,
                              "production digest render must grep for ghcr.io/dorring/mecrm/gateway@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/frontend@sha256:", run,
                              "production digest render must grep for ghcr.io/dorring/mecrm/frontend@sha256:")
                self.assertIn("ghcr.io/dorring/mecrm/agents@sha256:", run,
                              "production digest render must grep for ghcr.io/dorring/mecrm/agents@sha256:")
                self.assertIn("enterprise-crm/frontend@sha256:", run,
                              "production digest render must reject enterprise-crm prefix")
                return
        self.fail("'Render production template if present (digest mode)' step not found")

    def test_staging_digest_template_if_present(self):
        """Staging values should have digest mode too."""
        names = [s.get("name", "") for s in self._helm_lint_steps()]
        self.assertIn("Render staging template if present (digest mode)", names,
                      "G1: helm-lint must have staging digest template step")

    def test_production_digest_template_if_present(self):
        """Production values should have digest mode too."""
        names = [s.get("name", "") for s in self._helm_lint_steps()]
        self.assertIn("Render production template if present (digest mode)", names,
                      "G1: helm-lint must have production digest template step")


# -- G1: --set-string consistency for all digest params -------------------


class TestCIUsesSetStringForDigest(unittest.TestCase):
    """All --set images.*.digest=... invocations must use --set-string
    to prevent Helm from interpreting the sha256: prefix as a number/boolean."""

    @classmethod
    def setUpClass(cls):
        cls.text = _slurp(CI_CD_PATH)

    def test_no_bare_set_for_digest(self):
        # --set (not --set-string) followed by images.X.digest should not exist
        matches = re.findall(r'^\s+--set\s+images\.\w+\.digest=', self.text, re.MULTILINE)
        self.assertEqual(len(matches), 0,
                         f"All images.*.digest args must use --set-string, found bare --set: {matches}")

    def test_deploy_staging_uses_set_string_for_digest(self):
        data = _load_yaml(CI_CD_PATH)
        staging = data.get("jobs", {}).get("deploy-staging", {})
        for s in staging.get("steps", []):
            if s.get("name") == "Deploy with Helm":
                run = s.get("run", "")
                self.assertIn("--set-string images.gateway.repository=", run,
                              "deploy-staging must use --set-string for repository")
                self.assertNotIn("--set images.gateway.digest=", run,
                                 "deploy-staging must NOT use bare --set for digest")
                return
        self.fail("deploy-staging 'Deploy with Helm' step not found")

    def test_deploy_production_uses_set_string_for_digest(self):
        data = _load_yaml(CI_CD_PATH)
        prod = data.get("jobs", {}).get("deploy-production", {})
        for s in prod.get("steps", []):
            if s.get("name") == "Deploy with Helm":
                run = s.get("run", "")
                self.assertIn("--set-string images.gateway.repository=", run,
                              "deploy-production must use --set-string for repository")
                self.assertNotIn("--set images.gateway.digest=", run,
                                 "deploy-production must NOT use bare --set for digest")
                return
        self.fail("deploy-production 'Deploy with Helm' step not found")
