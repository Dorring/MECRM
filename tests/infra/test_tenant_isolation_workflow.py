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
