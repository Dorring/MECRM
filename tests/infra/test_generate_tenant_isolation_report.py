"""Regression tests for the tenant isolation report generator.

Guards against Jest test name labels (e.g. ``[requires DB]``) breaking the
required-proof matching in ``scripts/generate_tenant_isolation_report.py``.
"""

import importlib.util
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_tenant_isolation_report.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("report_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jest_with_labels(tmp_dir: str) -> str:
    """Jest output where gateway test names include a [requires DB] label."""
    path = os.path.join(tmp_dir, "jest-results.json")
    data = {
        "testResults": [
            {
                "assertionResults": [
                    {
                        "fullName": "Tenant isolation proof (gateway) [requires DB] blocks tenant override for non-super-admin (JWT tampering attempt)",
                        "status": "passed",
                        "title": "blocks tenant override for non-super-admin (JWT tampering attempt)",
                    },
                    {
                        "fullName": "Tenant isolation proof (gateway) [requires DB] blocks cross-tenant resource access (tenant A cannot read tenant B)",
                        "status": "passed",
                        "title": "blocks cross-tenant resource access (tenant A cannot read tenant B)",
                    },
                    {
                        "fullName": "Tenant isolation proof (gateway) [requires DB] prevents cross-tenant cache key collisions (tenant-scoped keys)",
                        "status": "passed",
                        "title": "prevents cross-tenant cache key collisions (tenant-scoped keys)",
                    },
                    {
                        "fullName": "Tenant isolation proof (gateway) [requires DB] blocks websocket cross-tenant channel subscription",
                        "status": "passed",
                        "title": "blocks websocket cross-tenant channel subscription",
                    },
                ]
            }
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _write_junit(tmp_dir: str, classname: str = "agents.tests.test_tenant_isolation") -> str:
    path = os.path.join(tmp_dir, "pytest-results.xml")
    root = ET.Element("testsuite")
    cases = [
        "test_rls_select_enforcement",
        "test_rls_update_blocked_cross_tenant",
        "test_rls_delete_blocked_cross_tenant",
        "test_tenant_escape_kill_test_missing_context_fails_closed",
    ]
    for name in cases:
        tc = ET.SubElement(root, "testcase", {"classname": classname, "name": name})
        ET.SubElement(tc, "system-out").text = "ok"
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _write_opa(tmp_dir: str) -> str:
    path = os.path.join(tmp_dir, "opa-results.json")
    data = [{"name": "tenant_isolation", "package": "crm", "pass": True}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _report_env(tmp_dir: str, jest: str, junit: str, opa: str) -> dict[str, str]:
    return {
        "TENANT_ISO_JEST_JSON": jest,
        "TENANT_ISO_PYTEST_JUNIT": junit,
        "TENANT_ISO_OPA_JSON": opa,
        "TENANT_ISO_REPORT_OUT": os.path.join(tmp_dir, "tenant-isolation-report.json"),
    }


class TestTenantIsolationReportGenerator(unittest.TestCase):
    def test_strips_requires_db_label_and_reports_success(self):
        """Regression test: [requires DB] labels must not break required-proof matching."""
        module = _load_report_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _report_env(
                tmp_dir,
                jest=_write_jest_with_labels(tmp_dir),
                junit=_write_junit(tmp_dir),
                opa=_write_opa(tmp_dir),
            )
            with patch.dict(os.environ, env, clear=False):
                rc = module.main()
            self.assertEqual(rc, 0, "Report generator should return 0 when all required proofs pass")

    def test_matches_python_tests_without_agents_prefix(self):
        """Regression test: pytest classname without leading 'agents.' must still match."""
        module = _load_report_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _report_env(
                tmp_dir,
                jest=_write_jest_with_labels(tmp_dir),
                junit=_write_junit(tmp_dir, classname="tests.test_tenant_isolation"),
                opa=_write_opa(tmp_dir),
            )
            with patch.dict(os.environ, env, clear=False):
                rc = module.main()
            self.assertEqual(rc, 0, "Report generator should match required Python tests regardless of classname prefix")


if __name__ == "__main__":
    unittest.main()
