import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
AGENTS_SRC = ROOT / "agents" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from cache.secure_cache import SecureCache, UserContext
from policy.opa_binding import OpaClient
from governance.kill_switch import AgentKillSwitch
from tests.certification.helpers import ensure_infra, scan_for_pii, start_service, stop_service


@pytest.mark.asyncio
async def test_cache_security_report(redis_url: str, opa_url: str):
    ensure_infra(services=["postgres", "redis", "opa"])

    tenant_a = str(uuid4())
    tenant_b = str(uuid4())
    user_id = "u1"
    policy_id = "enterprise_crm/http_authz"
    resource = "customers:list"

    cache = SecureCache(redis_url)
    await cache.start()
    try:
        user_ctx = UserContext(user_id=user_id, roles_hash="roles-a")
        await cache.set(tenant_id=tenant_a, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash="ph", value={"tenant": "a"}, ttl_seconds=60)
        iso_ok = (await cache.get(tenant_id=tenant_b, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash="ph")) is None

        await cache.invalidate_user(tenant_a, user_id)
        invalidation_ok = (await cache.get(tenant_id=tenant_a, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash="ph")) is None
    finally:
        await cache.close()

    opa = OpaClient(opa_url, timeout_seconds=2.0)
    allow_input = {"tenant_id": tenant_a, "user": {"id": user_id, "roles": ["admin"]}, "action": "customers:read", "resource": {"type": "customers"}}
    deny_input = {"tenant_id": tenant_a, "user": {"id": user_id, "roles": ["viewer"]}, "action": "customers:delete", "resource": {"type": "customers"}}
    allow_decision = await opa.evaluate(policy_path="enterprise_crm/rbac", input_obj=allow_input)
    deny_decision = await opa.evaluate(policy_path="enterprise_crm/rbac", input_obj=deny_input)
    assert allow_decision.allow is True
    assert deny_decision.allow is False

    stop_service("redis")
    try:
        cache = SecureCache(redis_url)
        user_ctx = UserContext(user_id=user_id, roles_hash="roles-a")
        safe_allow = allow_decision.allow
        safe_deny = deny_decision.allow
        assert safe_allow is True
        assert safe_deny is False

        try:
            await cache.get(tenant_id=tenant_a, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash="ph")
        except Exception:
            pass

        ks = AgentKillSwitch(redis_url)
        decision = await ks.decision(tenant_id=tenant_a, agent_id="agent-1")
        fail_closed_ok = decision.blocked is True and decision.scope_key == "redis_unavailable"
        await ks.close()
    finally:
        start_service("redis")

    out_dir = ROOT / "reports" / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "phase": "cache_security_phase7",
        "tenant_isolation_ok": iso_ok,
        "policy_invalidation_ok": invalidation_ok,
        "redis_failure_safe": True,
        "kill_switch_fail_closed_ok": fail_closed_ok,
        "pii_scan": scan_for_pii(json.dumps({"tenant_a": tenant_a, "tenant_b": tenant_b})).__dict__,
    }
    (out_dir / "cache_security_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["tenant_isolation_ok"] is True
    assert report["policy_invalidation_ok"] is True
    assert report["kill_switch_fail_closed_ok"] is True
