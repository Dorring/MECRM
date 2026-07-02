import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any


def _normalize_test_name(name: str) -> str:
    """Strip test labels (e.g. [requires DB]) so required IDs stay stable."""
    return re.sub(r"\s*\[[^\]]+\]\s*", " ", name).strip()


def _read_json(path: str) -> Any | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def _parse_junit(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    tree = ET.parse(path)
    root = tree.getroot()

    out: dict[str, dict[str, Any]] = {}
    for tc in root.iter("testcase"):
        classname = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        failed = tc.find("failure") is not None or tc.find("error") is not None
        out[test_id] = {"passed": not failed}
    return out


def _parse_jest(path: str) -> dict[str, dict[str, Any]]:
    data = _read_json(path)
    if not data:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for suite in data.get("testResults", []):
        for a in suite.get("assertionResults", []):
            name = a.get("fullName") or a.get("title") or "unknown"
            name = _normalize_test_name(name)
            status = a.get("status")
            out[name] = {"passed": status == "passed"}
    return out


def _parse_opa(path: str) -> dict[str, dict[str, Any]]:
    data = _read_json(path)
    if not data:
        return {}

    out: dict[str, dict[str, Any]] = {}

    if isinstance(data, list):
        for item in data:
            name = item.get("name") or item.get("test") or "unknown"
            pkg = item.get("package") or item.get("pkg") or ""
            passed = item.get("pass")
            if passed is None:
                result = item.get("result")
                if result is not None:
                    passed = result == "pass"
            if passed is None:
                passed = not any(k in item for k in ("fail", "failure", "error"))
            test_id = f"{pkg}::{name}" if pkg else name
            out[test_id] = {"passed": bool(passed)}
        return out

    if isinstance(data, dict) and "tests" in data and isinstance(data["tests"], list):
        for item in data["tests"]:
            name = item.get("name") or item.get("test") or "unknown"
            pkg = item.get("package") or item.get("pkg") or ""
            passed = item.get("pass")
            if passed is None:
                result = item.get("result")
                if result is not None:
                    passed = result == "pass"
            if passed is None:
                passed = not any(k in item for k in ("fail", "failure", "error"))
            test_id = f"{pkg}::{name}" if pkg else name
            out[test_id] = {"passed": bool(passed)}
        return out

    return out


def _count(results: dict[str, dict[str, Any]]) -> tuple[int, int]:
    passed = sum(1 for r in results.values() if r.get("passed"))
    failed = sum(1 for r in results.values() if not r.get("passed"))
    return passed, failed


def _require_all_passed(ids: list[str], results: dict[str, dict[str, Any]]) -> bool:
    if not ids:
        return False
    for test_id in ids:
        r = results.get(test_id)
        if not r or not r.get("passed"):
            return False
    return True


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    junit_path = os.environ.get(
        "TENANT_ISO_PYTEST_JUNIT",
        os.path.join(repo_root, "reports", "security", "pytest-results.xml"),
    )
    jest_path = os.environ.get(
        "TENANT_ISO_JEST_JSON",
        os.path.join(repo_root, "reports", "security", "jest-results.json"),
    )
    opa_path = os.environ.get(
        "TENANT_ISO_OPA_JSON",
        os.path.join(repo_root, "reports", "security", "opa-results.json"),
    )

    py = _parse_junit(junit_path)
    js = _parse_jest(jest_path)
    opa = _parse_opa(opa_path)

    py_passed, py_failed = _count(py)
    js_passed, js_failed = _count(js)
    opa_passed, opa_failed = _count(opa)

    required_python = {
        "rls_select_blocked": ["agents.tests.test_tenant_isolation::test_rls_select_enforcement"],
        "rls_update_blocked": ["agents.tests.test_tenant_isolation::test_rls_update_blocked_cross_tenant"],
        "rls_delete_blocked": ["agents.tests.test_tenant_isolation::test_rls_delete_blocked_cross_tenant"],
        "no_tenant_context_leaks": [
            "agents.tests.test_tenant_isolation::test_tenant_escape_kill_test_missing_context_fails_closed"
        ],
    }

    required_gateway = {
        "jwt_tampering_blocked": ["Tenant isolation proof (gateway) blocks tenant override for non-super-admin (JWT tampering attempt)"],
        "id_enumeration_blocked": ["Tenant isolation proof (gateway) blocks cross-tenant resource access (tenant A cannot read tenant B)"],
        "cache_isolation_passed": ["Tenant isolation proof (gateway) prevents cross-tenant cache key collisions (tenant-scoped keys)"],
        "websocket_isolation_passed": ["Tenant isolation proof (gateway) blocks websocket cross-tenant channel subscription"],
    }

    opa_ok = opa_failed == 0 and opa_passed > 0

    results = {
        "rls_select_blocked": _require_all_passed(required_python["rls_select_blocked"], py),
        "rls_update_blocked": _require_all_passed(required_python["rls_update_blocked"], py),
        "rls_delete_blocked": _require_all_passed(required_python["rls_delete_blocked"], py),
        "opa_policies_passed": opa_ok,
        "jwt_tampering_blocked": _require_all_passed(required_gateway["jwt_tampering_blocked"], js),
        "id_enumeration_blocked": _require_all_passed(required_gateway["id_enumeration_blocked"], js),
        "cache_isolation_passed": _require_all_passed(required_gateway["cache_isolation_passed"], js),
        "websocket_isolation_passed": _require_all_passed(required_gateway["websocket_isolation_passed"], js),
        "no_tenant_context_leaks": _require_all_passed(required_python["no_tenant_context_leaks"], py),
    }

    report = {
        "phase": "tenant_isolation_proof",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": results,
        "tests": {
            "python": {"passed": py_passed, "failed": py_failed},
            "gateway": {"passed": js_passed, "failed": js_failed},
            "opa": {"passed": opa_passed, "failed": opa_failed},
        },
    }

    out_path = os.path.join(repo_root, "reports", "security", "tenant-isolation-report.json")
    _write_json(out_path, report)

    failed_proofs = [name for name, passed in report["results"].items() if not passed]
    if failed_proofs:
        print("Tenant isolation proof failed or missing:")
        for name in failed_proofs:
            print(f"  - {name}")
        print("Available Jest test IDs:")
        for test_id in sorted(js):
            print(f"  - {test_id}")

    all_true = all(report["results"].values())
    return 0 if all_true else 2


if __name__ == "__main__":
    sys.exit(main())

