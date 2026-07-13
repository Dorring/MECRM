"""Group G G3a migration Job regression tests.

Covers:
  G3-01 -- migration-job.yaml template exists
  G3-02 -- migration Job kind is batch/v1 Job (not Deployment)
  G3-03 -- pre-install/pre-upgrade hook annotations exist
  G3-04 -- hook delete policy exists
  G3-05 -- backoffLimit and activeDeadlineSeconds are configurable
  G3-06 -- DATABASE_URL uses secretKeyRef
  G3-07 -- values has migration.database.existingSecret and urlKey
  G3-08 -- migration image supports digest field
  G3-09 -- Helm CI digest-mode includes migration image
  G3-10 -- no plaintext DATABASE_URL in values files
  G3-11 -- migration Job has non-root securityContext
  G3-12 -- resources configurable
  G3-13 -- migration Job renders when migration.enabled=true, absent when false
  G3-14 -- migration image uses enterprise-crm.image helper
  G3-15 -- migration pod restartPolicy is Never
  G3-16 -- Dockerfile.migrate is self-contained (scripts + SQL baked in)
"""

import os
import re
import unittest

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HELM_DIR = os.path.join(REPO_ROOT, "deploy", "helm", "enterprise-crm")
MIGRATION_TPL_PATH = os.path.join(HELM_DIR, "templates", "migration-job.yaml")
VALUES_PATH = os.path.join(HELM_DIR, "values.yaml")
HELPERS_PATH = os.path.join(HELM_DIR, "templates", "_helpers.tpl")
CI_CD_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci-cd.yml")
MIGRATE_DOCKERFILE_PATH = os.path.join(REPO_ROOT, "database", "Dockerfile.migrate")


def _slurp(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# -- G3-01, G3-02: Template existence and kind -----------------------------

class TestMigrationTemplateExists(unittest.TestCase):
    """migration-job.yaml must exist and be a valid Job template."""

    def test_migration_template_exists(self):
        self.assertTrue(os.path.isfile(MIGRATION_TPL_PATH),
                        "G3-01: migration-job.yaml must exist")

    def test_migration_template_is_job_kind(self):
        content = _slurp(MIGRATION_TPL_PATH)
        self.assertIn("kind: Job", content,
                      "G3-02: migration template must be a Kubernetes Job")
        self.assertIn("apiVersion: batch/v1", content,
                      "G3-02: migration template must use batch/v1 apiVersion")

    def test_migration_template_not_deployment(self):
        content = _slurp(MIGRATION_TPL_PATH)
        self.assertNotIn("kind: Deployment", content,
                         "G3-02: migration must NOT be a Deployment")

    def test_migration_template_uses_image_helper(self):
        content = _slurp(MIGRATION_TPL_PATH)
        self.assertIn('include "enterprise-crm.image"', content,
                      "G3-14: migration template must use enterprise-crm.image helper")


# -- G3-03, G3-04: Hook annotations ----------------------------------------

class TestMigrationHookAnnotations(unittest.TestCase):
    """Migration Job must have correct Helm hook annotations."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)

    def test_pre_install_hook(self):
        self.assertIn("helm.sh/hook: pre-install,pre-upgrade", self.content,
                      "G3-03: migration must have pre-install,pre-upgrade hook")

    def test_hook_weight(self):
        self.assertIn("helm.sh/hook-weight: ", self.content,
                      "G3-03: migration must have hook-weight annotation")

    def test_hook_delete_policy(self):
        self.assertIn("helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded",
                      self.content,
                      "G3-04: migration must have hook delete policy")

    def test_restart_policy_never(self):
        self.assertIn("restartPolicy: Never", self.content,
                      "G3-15: migration pod must have restartPolicy: Never")


# -- G3-05: Configurable backoffLimit & activeDeadlineSeconds ---------------

class TestMigrationConfigurable(unittest.TestCase):
    """backoffLimit and activeDeadlineSeconds must be configurable."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)
        cls.values = _load_yaml(VALUES_PATH)

    def test_backoff_limit_from_values(self):
        self.assertIn("{{ .Values.migration.backoffLimit }}", self.content,
                      "G3-05: backoffLimit must read from values")

    def test_active_deadline_from_values(self):
        self.assertIn("{{ .Values.migration.activeDeadlineSeconds }}", self.content,
                      "G3-05: activeDeadlineSeconds must read from values")

    def test_backoff_limit_default_value(self):
        val = self.values.get("migration", {}).get("backoffLimit")
        self.assertIsInstance(val, int,
                             "G3-05: migration.backoffLimit must be an integer")
        self.assertEqual(val, 2,
                         "G3-05: migration.backoffLimit default must be 2")

    def test_active_deadline_default_value(self):
        val = self.values.get("migration", {}).get("activeDeadlineSeconds")
        self.assertIsInstance(val, int,
                             "G3-05: migration.activeDeadlineSeconds must be an integer")
        self.assertEqual(val, 600,
                         "G3-05: migration.activeDeadlineSeconds default must be 600")


# -- G3-06: DATABASE_URL uses secretKeyRef ---------------------------------

class TestMigrationDatabaseSecret(unittest.TestCase):
    """DATABASE_URL must come from secretKeyRef, never plaintext."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)
        cls.values = _load_yaml(VALUES_PATH)

    def test_database_url_uses_secret_key_ref(self):
        self.assertIn("secretKeyRef", self.content,
                      "G3-06: DATABASE_URL must use secretKeyRef")

    def test_database_url_secret_name_from_values(self):
        self.assertRegex(self.content, r'name: \{\{ \.Values\.migration\.database\.existingSecret',
                         "G3-06: secret name must read from values")

    def test_database_url_secret_key_from_values(self):
        self.assertRegex(self.content, r'key: \{\{ \.Values\.migration\.database\.urlKey',
                         "G3-06: secret key must read from values")

    def test_no_plaintext_database_url_in_migration_template(self):
        self.assertNotRegex(self.content, r'DATABASE_URL.*postgresql://',
                            "G3-06: migration template must not contain plaintext DATABASE_URL")

    def test_no_plaintext_database_url_in_values(self):
        content = _slurp(VALUES_PATH)
        # DSN examples are allowed in comments; block only uncommented secrets.
        non_comment_lines = [l for l in content.splitlines()
                            if not l.strip().startswith('#') and l.strip()]
        for line in non_comment_lines:
            self.assertNotRegex(line, r'postgresql://',
                                f"G3-10: values.yaml must not contain plaintext DSN in non-comment line: '{line.strip()}'")

    def test_migration_existing_secret_has_default(self):
        val = self.values.get("migration", {}).get("database", {}).get("existingSecret")
        self.assertIsNotNone(val,
                            "G3-07: migration.database.existingSecret must have a default")
        self.assertEqual(val, "crm-postgresql-secret",
                         "G3-07: migration.database.existingSecret default should be crm-postgresql-secret")

    def test_migration_url_key_has_default(self):
        val = self.values.get("migration", {}).get("database", {}).get("urlKey")
        self.assertEqual(val, "connection-string",
                         "G3-07: migration.database.urlKey default should be connection-string")

    def test_migration_template_requires_existing_secret(self):
        content = _slurp(MIGRATION_TPL_PATH)
        self.assertIn('required "migration.database.existingSecret is required"', content,
                      "G3-07: migration template must use required() for existingSecret")


# -- G3-08: Migration image digest support ----------------------------------

class TestMigrationImageDigest(unittest.TestCase):
    """migration image must support digest field."""

    @classmethod
    def setUpClass(cls):
        cls.values = _load_yaml(VALUES_PATH)

    def test_migration_image_has_digest_field(self):
        img = self.values.get("migration", {}).get("image", {})
        self.assertIn("digest", img,
                      "G3-08: migration.image must have digest field")
        self.assertEqual(img.get("digest"), "",
                         "G3-08: migration.image.digest must default to empty string")

    def test_migration_image_has_tag_field(self):
        img = self.values.get("migration", {}).get("image", {})
        self.assertIn("tag", img,
                      "G3-08: migration.image must have tag field")
        self.assertEqual(img.get("tag"), "",
                         "G3-08: migration.image.tag must default to empty string")

    def test_migration_image_has_repository_field(self):
        img = self.values.get("migration", {}).get("image", {})
        self.assertIn("repository", img,
                      "G3-08: migration.image must have repository field")
        self.assertEqual(img.get("repository"), "enterprise-crm/migrate",
                         "G3-08: migration.image.repository default must be enterprise-crm/migrate")

    def test_migration_image_has_pull_policy(self):
        img = self.values.get("migration", {}).get("image", {})
        self.assertIn("pullPolicy", img,
                      "G3-08: migration.image must have pullPolicy field")

    def test_migration_helper_prefers_digest(self):
        content = _slurp(HELPERS_PATH)
        self.assertIn('$img.digest', content,
                      "G3-08: image helper must check digest, shared with migration image")


# -- G3-09: CI helm-lint digest-mode covers migration -----------------------

class TestCIDigestModeMigrationCoverage(unittest.TestCase):
    """helm-lint CI job digest-mode must include migration image."""

    @classmethod
    def setUpClass(cls):
        cls.text = _slurp(CI_CD_PATH)

    def test_digest_mode_lint_includes_migration(self):
        self.assertIn("migration.image.repository=", self.text,
                      "G3-09: digest-mode lint must set migration.image.repository")
        self.assertIn("migration.image.digest=", self.text,
                      "G3-09: digest-mode lint must set migration.image.digest")

    def test_digest_mode_render_includes_migration(self):
        self.assertIn("ghcr.io/dorring/mecrm/migrate@sha256:", self.text,
                      "G3-09: digest-mode render must assert ghcr.io/dorring/mecrm/migrate@sha256:")

    def test_digest_mode_rejects_enterprise_crm_migrate_prefix(self):
        """All digest renders must reject enterprise-crm/migrate@sha256."""
        content = _slurp(CI_CD_PATH)
        self.assertIn("enterprise-crm/migrate@sha256:", content,
                      "G3-09: digest-mode renders must reject enterprise-crm/migrate prefix")

    def test_digest_mode_env_has_migrate_digest(self):
        self.assertIn("DIGEST_MG: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                      self.text,
                      "G3-09: digest-mode env must declare DIGEST_MG")

    def test_tag_mode_renders_with_migration(self):
        """All tag-mode renders must also include migration.image.tag."""
        content = _slurp(CI_CD_PATH)
        self.assertIn("migration.image.tag=ci-test", content,
                      "G3-09: tag-mode renders must set migration.image.tag")

    def test_staging_digest_mode_covers_migration(self):
        content = _slurp(CI_CD_PATH)
        # Staging digest render must grep for migrate image
        self.assertIn("ghcr\\.io/dorring/mecrm/migrate@sha256:", content,
                      "G3-09: staging digest render must grep for migrate")

    def test_production_digest_mode_covers_migration(self):
        content = _slurp(CI_CD_PATH)
        # Production digest render must grep for migrate image
        self.assertIn("ghcr\\.io/dorring/mecrm/migrate@sha256:", content,
                      "G3-09: production digest render must grep for migrate")


# -- G3-11: Non-root securityContext ----------------------------------------

class TestMigrationSecurityContext(unittest.TestCase):
    """Migration Job must have non-root securityContext."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)
        cls.values = _load_yaml(VALUES_PATH)

    def test_run_as_non_root(self):
        self.assertIn("runAsNonRoot: true", self.content,
                      "G3-11: migration Job must set runAsNonRoot: true")

    def test_run_as_user_from_values(self):
        self.assertIn(".Values.migration.podSecurityContext.runAsUser", self.content,
                      "G3-11: migration runAsUser must read from values")

    def test_run_as_user_default_is_1000(self):
        uid = self.values.get("migration", {}).get("podSecurityContext", {}).get("runAsUser")
        self.assertEqual(uid, 1000,
                         "G3-11: migration runAsUser default must be 1000 (node user)")


# -- G3-12: Configurable resources ------------------------------------------

class TestMigrationResources(unittest.TestCase):
    """Migration Job resources must be configurable."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)
        cls.values = _load_yaml(VALUES_PATH)

    def test_resources_from_values(self):
        self.assertIn(".Values.migration.resources", self.content,
                      "G3-12: migration resources must read from values")

    def test_resources_default_limits(self):
        limits = self.values.get("migration", {}).get("resources", {}).get("limits", {})
        self.assertIn("cpu", limits,
                     "G3-12: migration resource limits must include cpu")
        self.assertIn("memory", limits,
                     "G3-12: migration resource limits must include memory")

    def test_resources_default_requests(self):
        requests = self.values.get("migration", {}).get("resources", {}).get("requests", {})
        self.assertIn("cpu", requests,
                     "G3-12: migration resource requests must include cpu")
        self.assertIn("memory", requests,
                     "G3-12: migration resource requests must include memory")


# -- G3-13: enabled/disabled toggle -----------------------------------------

class TestMigrationEnabledToggle(unittest.TestCase):
    """migration.enabled controls whether the Job is rendered."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)

    def test_template_guards_on_migration_enabled(self):
        self.assertIn(".Values.migration.enabled", self.content,
                      "G3-13: migration template must guard on .Values.migration.enabled")

    def test_migration_enabled_defaults_true(self):
        values = _load_yaml(VALUES_PATH)
        enabled = values.get("migration", {}).get("enabled")
        self.assertTrue(enabled,
                        "G3-13: migration.enabled must default to true")


# -- G3-16: Dockerfile.migrate is self-contained ----------------------------

class TestMigrateDockerfileSelfContained(unittest.TestCase):
    """database/Dockerfile.migrate must bake in scripts + SQL (no K8s volume mounts)."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATE_DOCKERFILE_PATH)

    def test_contains_scripts_migrate_sh(self):
        self.assertIn("migrate.sh", self.content,
                      "G3-16: Dockerfile.migrate must COPY scripts/migrate.sh")

    def test_contains_database_migrations(self):
        self.assertIn("/database/migrations/", self.content,
                      "G3-16: Dockerfile.migrate must COPY database/migrations/")

    def test_contains_postgresql_client(self):
        self.assertIn("postgresql-client", self.content,
                      "G3-16: Dockerfile.migrate must install postgresql-client (psql)")

    def test_contains_npm_ci(self):
        self.assertIn("npm ci", self.content,
                      "G3-16: Dockerfile.migrate must run npm ci for Prisma CLI")

    def test_contains_apt_get_upgrade(self):
        self.assertIn("apt-get upgrade -y", self.content,
                      "G3-16: Dockerfile.migrate must run apt-get upgrade -y to patch OS CVEs")

    def test_removes_apt_lists(self):
        self.assertIn("rm -rf /var/lib/apt/lists/*", self.content,
                      "G3-16: Dockerfile.migrate must remove apt lists after install")


# -- G3-17: values has no migration-specific plaintext secrets --------------

class TestMigrationNoPlaintextSecrets(unittest.TestCase):
    """values.yaml and other static files must not leak migration DB credentials."""

    def test_values_yaml_no_plaintext_database_url(self):
        content = _slurp(VALUES_PATH)
        self.assertNotRegex(content, r'(?:DATABASE_URL|database_url|databaseUrl).*(?:postgresql|postgres)://',
                            "G3-10: values.yaml must not contain DATABASE_URL connection string")

    def test_migration_template_no_plaintext_database_env(self):
        content = _slurp(MIGRATION_TPL_PATH)
        # DATABASE_URL must use secretKeyRef, not value:
        for line in content.splitlines():
            stripped = line.strip()
            if 'DATABASE_URL' in stripped or 'POSTGRES_PASSWORD' in stripped:
                self.assertNotIn('value:', stripped,
                    f"G3-10: sensitive env {stripped} must use secretKeyRef, not value:")


# -- G3-17: Blocker 2 -- POSTGRES_PASSWORD not incorrectly using urlKey ----

class TestMigrationEnvDoesNotLeakURLKeyAsPassword(unittest.TestCase):
    """Blocker 2 fix: POSTGRES_PASSWORD must not reuse DATABASE_URL key."""

    @classmethod
    def setUpClass(cls):
        cls.content = _slurp(MIGRATION_TPL_PATH)

    def test_no_postgres_password_env(self):
        self.assertNotIn("POSTGRES_PASSWORD", self.content,
                         "G3-17: migration Job must not have POSTGRES_PASSWORD env "
                         "(migrate.sh uses DATABASE_URL directly)")

    def test_no_postgres_host_env(self):
        self.assertNotIn("POSTGRES_HOST", self.content,
                         "G3-17: migration Job must not have POSTGRES_HOST env")

    def test_no_postgres_user_env(self):
        self.assertNotIn("POSTGRES_USER", self.content,
                         "G3-17: migration Job must not have POSTGRES_USER env")

    def test_only_database_url_and_gateway_dir(self):
        lines = [l.strip() for l in self.content.splitlines()
                 if '- name:' in l and 'name:' in l]
        env_names = set()
        for line in lines:
            m = re.search(r'name:\s*(\S+)', line)
            if m:
                env_names.add(m.group(1))
        # Only container env vars -- exclude the container name itself ("migrate")
        env_only = {n for n in env_names if n != "migrate"}
        self.assertSetEqual(env_only, {"DATABASE_URL", "GATEWAY_DIR"},
                            f"G3-17: migration env must be DATABASE_URL + GATEWAY_DIR only, got {env_only}")


# -- G3-18 through G3-24: Blocker 1 -- migrate in real digest deploy chain --

def _build_matrix_projects(yaml_data, job_name):
    """Extract project names from a job's matrix.include."""
    job = yaml_data.get("jobs", {}).get(job_name, {})
    strategy = job.get("strategy", {})
    matrix = strategy.get("matrix", {})
    includes = matrix.get("include", [])
    return [(entry.get("project"), entry.get("context"), entry.get("dockerfile"))
            for entry in includes]


def _deploy_step_run(deploy_job, step_name):
    """Get the run script of a deploy job step."""
    for s in deploy_job.get("steps", []):
        if s.get("name") == step_name:
            return s.get("run", "")
    return ""


class TestCIDigestDeployChainIncludesMigrate(unittest.TestCase):
    """Blocker 1: migrate must be in the real build/deploy pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CI_CD_PATH)

    def test_build_matrix_includes_migrate(self):
        projects = [p for p, _, _ in _build_matrix_projects(self.data, "build")]
        self.assertIn("migrate", projects,
                      "G3-18: build matrix must include migrate")
        migrate_entry = [(c, d) for p, c, d in _build_matrix_projects(self.data, "build")
                         if p == "migrate"]
        self.assertEqual(len(migrate_entry), 1)
        self.assertEqual(migrate_entry[0][0], ".",
                         "G3-18: migrate build context must be '.' (repo root)")
        self.assertEqual(migrate_entry[0][1], "database/Dockerfile.migrate",
                         "G3-18: migrate dockerfile must be database/Dockerfile.migrate")

    def test_security_scan_matrix_includes_migrate(self):
        projects = [p for p, _, _ in _build_matrix_projects(self.data, "security-scan")]
        self.assertIn("migrate", projects,
                      "G3-19: security-scan matrix must include migrate")
        # Verify context/dockerfile for migrate in security-scan
        migrate_entry = [(c, d) for p, c, d in _build_matrix_projects(self.data, "security-scan")
                         if p == "migrate"]
        self.assertEqual(len(migrate_entry), 1)
        self.assertEqual(migrate_entry[0][1], "database/Dockerfile.migrate",
                         "G3-19: security-scan migrate must use database/Dockerfile.migrate")

    def test_aggregate_digests_includes_migrate(self):
        agg = self.data.get("jobs", {}).get("aggregate-digests", {})
        assemble_step = None
        for s in agg.get("steps", []):
            if s.get("name") == "Assemble digest map":
                assemble_step = s
                break
        self.assertIsNotNone(assemble_step)
        run = assemble_step.get("run", "")
        self.assertIn("REQUIRED_PROJECTS=\"gateway frontend agents migrate\"", run,
                      "G3-20: REQUIRED_PROJECTS must include migrate")

    def test_deploy_staging_reads_migrate_digest(self):
        staging = self.data.get("jobs", {}).get("deploy-staging", {})
        run = _deploy_step_run(staging, "Deploy with Helm")
        self.assertIn('.migrate.image', run,
                      "G3-21: deploy-staging must read .migrate.image from digest-map")
        self.assertIn('.migrate.digest', run,
                      "G3-21: deploy-staging must read .migrate.digest from digest-map")
        self.assertIn('MG_IMAGE=', run,
                      "G3-21: deploy-staging must set MG_IMAGE from digest-map")
        self.assertIn('MG_DIGEST=', run,
                      "G3-21: deploy-staging must set MG_DIGEST from digest-map")
        self.assertIn('migration.image.repository="${MG_IMAGE}"', run,
                      "G3-21: deploy-staging must --set-string migration.image.repository")
        self.assertIn('migration.image.digest="${MG_DIGEST}"', run,
                      "G3-21: deploy-staging must --set-string migration.image.digest")

    def test_deploy_production_reads_migrate_digest(self):
        prod = self.data.get("jobs", {}).get("deploy-production", {})
        run = _deploy_step_run(prod, "Deploy with Helm")
        self.assertIn('.migrate.image', run,
                      "G3-22: deploy-production must read .migrate.image from digest-map")
        self.assertIn('.migrate.digest', run,
                      "G3-22: deploy-production must read .migrate.digest from digest-map")
        self.assertIn('MG_IMAGE=', run,
                      "G3-22: deploy-production must set MG_IMAGE from digest-map")
        self.assertIn('MG_DIGEST=', run,
                      "G3-22: deploy-production must set MG_DIGEST from digest-map")
        self.assertIn('migration.image.repository="${MG_IMAGE}"', run,
                      "G3-22: deploy-production must --set-string migration.image.repository")
        self.assertIn('migration.image.digest="${MG_DIGEST}"', run,
                      "G3-22: deploy-production must --set-string migration.image.digest")
