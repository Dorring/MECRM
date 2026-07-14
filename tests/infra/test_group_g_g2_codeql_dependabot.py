"""Group G G2 PR2 regression tests -- CodeQL + Dependabot + Node migration.

Covers:
  G2-21 -- codeql.yml exists
  G2-22 -- CodeQL triggers include pull_request, push main, schedule
  G2-23 -- CodeQL languages include javascript-typescript and python
  G2-24 -- CodeQL uses security-extended queries
  G2-25 -- CodeQL permissions include security-events:write and contents:read
  G2-26 -- dependabot.yml exists
  G2-27 -- Dependabot covers github-actions
  G2-28 -- Dependabot covers gateway npm
  G2-29 -- Dependabot covers frontend npm
  G2-30 -- Dependabot covers agents pip
  G2-30b -- Dependabot core_services pip is commented (no manifest yet)
  G2-31 -- Dependabot covers Docker directories
  G2-32 -- Dependabot PR limit is 5 per ecosystem
  G2-33 -- Dependabot groups minor/patch, not major
  G2-34 -- Node migration assessment doc exists
"""

import os
import unittest

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CODEQL_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "codeql.yml")
DEPENDABOT_PATH = os.path.join(REPO_ROOT, ".github", "dependabot.yml")
NODE_ASSESS_PATH = os.path.join(REPO_ROOT, "docs", "node-runtime-migration-assessment.md")


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# -- G2-21 through G2-25: CodeQL workflow --------------------------------

class TestCodeQLWorkflow(unittest.TestCase):
    """codeql.yml must exist with correct configuration."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(CODEQL_PATH)

    def test_codeql_yaml_exists(self):
        self.assertTrue(os.path.isfile(CODEQL_PATH),
                        "G2-21: codeql.yml must exist")

    def test_codeql_triggers_pull_request(self):
        on = self.data.get(True) or self.data.get("on") or {}
        self.assertIn("pull_request", on,
                      "G2-22: CodeQL must trigger on pull_request")

    def test_codeql_triggers_push_main(self):
        on = self.data.get(True) or self.data.get("on") or {}
        push = on.get("push", {})
        branches = push.get("branches", []) if isinstance(push, dict) else []
        self.assertIn("main", branches,
                      "G2-22: CodeQL must trigger on push to main")

    def test_codeql_triggers_schedule(self):
        on = self.data.get(True) or self.data.get("on") or {}
        self.assertIn("schedule", on,
                      "G2-22: CodeQL must have a schedule trigger")

    def test_codeql_languages_include_javascript(self):
        jobs = self.data.get("jobs", {})
        analyze = jobs.get("analyze", {})
        strategy = analyze.get("strategy", {})
        matrix = strategy.get("matrix", {})
        languages = matrix.get("language", [])
        self.assertIn("javascript-typescript", languages,
                      "G2-23: CodeQL languages must include javascript-typescript")

    def test_codeql_languages_include_python(self):
        jobs = self.data.get("jobs", {})
        analyze = jobs.get("analyze", {})
        strategy = analyze.get("strategy", {})
        matrix = strategy.get("matrix", {})
        languages = matrix.get("language", [])
        self.assertIn("python", languages,
                      "G2-23: CodeQL languages must include python")

    def test_codeql_uses_security_extended(self):
        jobs = self.data.get("jobs", {})
        analyze = jobs.get("analyze", {})
        steps = analyze.get("steps", [])
        for s in steps:
            if s.get("name") == "Initialize CodeQL":
                with_config = s.get("with", {})
                self.assertEqual(with_config.get("queries"), "security-extended",
                                 "G2-24: CodeQL init must use security-extended")
                return
        self.fail("Initialize CodeQL step not found")

    def test_codeql_permissions_security_events_write(self):
        perms = self.data.get("permissions", {})
        self.assertEqual(perms.get("security-events"), "write",
                         "G2-25: CodeQL needs security-events: write")

    def test_codeql_permissions_contents_read(self):
        perms = self.data.get("permissions", {})
        self.assertEqual(perms.get("contents"), "read",
                         "G2-25: CodeQL needs contents: read")


# -- G2-26 through G2-33: Dependabot config ------------------------------

class TestDependabotConfig(unittest.TestCase):
    """.github/dependabot.yml must exist with correct configuration."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_yaml(DEPENDABOT_PATH)

    def test_dependabot_yaml_exists(self):
        self.assertTrue(os.path.isfile(DEPENDABOT_PATH),
                        "G2-26: dependabot.yml must exist")

    def _ecosystems(self):
        updates = self.data.get("updates", [])
        return {u.get("package-ecosystem", ""): u.get("directory", "")
                for u in updates}

    def test_covers_github_actions(self):
        ecos = {u.get("package-ecosystem") for u in self.data.get("updates", [])}
        self.assertIn("github-actions", ecos,
                      "G2-27: Dependabot must cover github-actions")

    def test_covers_gateway_npm(self):
        gateways = [u for u in self.data.get("updates", [])
                    if u.get("package-ecosystem") == "npm"
                    and u.get("directory") == "/gateway"]
        self.assertEqual(len(gateways), 1,
                         "G2-28: Dependabot must cover /gateway npm")

    def test_covers_frontend_npm(self):
        frontends = [u for u in self.data.get("updates", [])
                     if u.get("package-ecosystem") == "npm"
                     and u.get("directory") == "/frontend"]
        self.assertEqual(len(frontends), 1,
                         "G2-29: Dependabot must cover /frontend npm")

    def test_covers_agents_pip(self):
        agents = [u for u in self.data.get("updates", [])
                  if u.get("package-ecosystem") == "pip"
                  and u.get("directory") == "/agents"]
        self.assertEqual(len(agents), 1,
                         "G2-30: Dependabot must cover /agents pip")

    def test_core_services_pip_removed_no_manifest(self):
        core = [u for u in self.data.get("updates", [])
                if u.get("directory") == "/core_services"]
        self.assertEqual(len(core), 0,
                         "G2-30b: core_services pip must be removed (no requirements.txt)")

    def test_covers_docker_directories(self):
        docker_dirs = {u.get("directory") for u in self.data.get("updates", [])
                       if u.get("package-ecosystem") == "docker"}
        expected = {"/gateway", "/frontend", "/agents", "/database"}
        self.assertTrue(expected.issubset(docker_dirs),
                        f"G2-31: Dependabot Docker must cover {expected}, got {docker_dirs}")

    def test_pr_limit_is_3(self):
        for u in self.data.get("updates", []):
            limit = u.get("open-pull-requests-limit")
            self.assertEqual(limit, 3,
                             f"G2-32: {u.get('directory')} PR limit must be 3, got {limit}")

    def test_only_github_actions_groups_major_updates(self):
        for u in self.data.get("updates", []):
            groups = u.get("groups", {})
            for group_name, group_config in groups.items():
                update_types = group_config.get("update-types", [])
                if u.get("package-ecosystem") == "github-actions":
                    self.assertIn("major", update_types)
                else:
                    self.assertNotIn("major", update_types,
                                     f"G2-33: {u.get('directory')} group '{group_name}' must not include major")

    def test_frontend_defers_unplanned_toolchain_majors(self):
        frontend = next(u for u in self.data.get("updates", [])
                        if u.get("package-ecosystem") == "npm"
                        and u.get("directory") == "/frontend")
        ignored = {item.get("dependency-name"): item.get("versions", [])
                   for item in frontend.get("ignore", [])}
        self.assertIn(">=10", ignored.get("eslint", []))
        self.assertIn(">=4", ignored.get("tailwindcss", []))

    def test_dependabot_has_security_label(self):
        for u in self.data.get("updates", []):
            labels = u.get("labels", [])
            self.assertIn("security", labels,
                          f"G2-33: {u.get('directory')} must have security label")


# -- G2-34: Node migration assessment ------------------------------------

class TestNodeRuntimeAssessment(unittest.TestCase):
    """Node 20 migration assessment doc must exist."""

    def test_node_assessment_exists(self):
        self.assertTrue(os.path.isfile(NODE_ASSESS_PATH),
                        "G2-34: node-runtime-migration-assessment.md must exist")

    def test_node_assessment_mentions_node20(self):
        with open(NODE_ASSESS_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("Node 20", content,
                      "G2-34: assessment must mention Node 20 deprecation")

    def test_node_assessment_mentions_migration_target(self):
        with open(NODE_ASSESS_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("Node 24", content,
                      "G2-34: assessment must mention Node 24 migration target")