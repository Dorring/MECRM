"""Static regression tests for the tenant-isolation workflow.

Guards against the canonical DB-required flag drifting or being shadowed by a
derived variable.
"""

import unittest

import yaml

WORKFLOW_PATH = ".github/workflows/tenant-isolation.yml"


def _load_workflow():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env_list(svc):
    env = svc.get("env") or svc.get("environment") or []
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return []


class TestTenantIsolationWorkflowEnv(unittest.TestCase):
    def test_job_env_requires_db_switch(self):
        workflow = _load_workflow()
        job_env = workflow.get("env", {})
        self.assertIn(
            "CRM_TEST_REQUIRE_DB",
            job_env,
            "tenant-isolation workflow must set CRM_TEST_REQUIRE_DB",
        )
        self.assertEqual(
            str(job_env["CRM_TEST_REQUIRE_DB"]),
            "1",
            "CRM_TEST_REQUIRE_DB must be '1' to enable Gateway DB-backed proofs",
        )

    def test_job_env_does_not_set_derived_crm_db_available(self):
        workflow = _load_workflow()
        job_env = workflow.get("env", {})
        self.assertNotIn(
            "CRM_DB_AVAILABLE",
            job_env,
            "CRM_DB_AVAILABLE is derived by gateway/src/jest.setup.ts and must not be set by workflow",
        )

    def test_uses_unified_migration_runner_and_runtime_login_probe(self):
        workflow = _load_workflow()
        steps = workflow["jobs"]["tenant-isolation"]["steps"]
        by_name = {step.get("name"): step for step in steps}

        migration = by_name["Apply migrations via single runner"]
        self.assertIn("bash ./scripts/migrate.sh", migration["run"])
        self.assertEqual(
            migration["env"]["DATABASE_URL"],
            "${{ env.ADMIN_DATABASE_URL }}",
        )

        probe = by_name["Verify runtime database role login"]
        self.assertIn('psql "${DATABASE_URL}"', probe["run"])
        self.assertIn('"crm_app"', probe["run"])

    def test_legacy_partial_migration_path_is_removed(self):
        workflow_text = open(WORKFLOW_PATH, encoding="utf-8").read()
        self.assertNotIn("- name: Run Prisma migrations", workflow_text)
        self.assertNotIn("- name: Apply RLS policies", workflow_text)
        self.assertNotIn("-f database/migrations/02-rls-policies.sql", workflow_text)

    def test_no_step_sets_crm_db_available(self):
        workflow = _load_workflow()
        for name, step in enumerate(workflow["jobs"]["tenant-isolation"]["steps"]):
            env = _env_list(step)
            joined = "\n".join(env)
            self.assertNotIn(
                "CRM_DB_AVAILABLE",
                joined,
                f"Step {name} must not set CRM_DB_AVAILABLE (it is derived from CRM_TEST_REQUIRE_DB)",
            )


if __name__ == "__main__":
    unittest.main()
