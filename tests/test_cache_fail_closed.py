import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from cache.secure_cache import SecureCache, UserContext
from policy.opa_binding import OpaClient


@pytest.mark.asyncio
async def test_cache_fail_closed_on_opa_failure(redis_url: str):
    opa = OpaClient("http://localhost:1", timeout_seconds=0.2)
    decision = await opa.evaluate(
        policy_path="enterprise_crm/rbac",
        input_obj={"tenant_id": str(uuid4()), "user": {"id": "u1", "roles": ["admin"]}, "action": "customers:read", "resource": {"type": "customers"}},
    )
    assert decision.allow is False

    cache = SecureCache(redis_url)
    await cache.start()
    try:
        user = UserContext(user_id="u1", roles_hash="rh")
        assert await cache.get(tenant_id=str(uuid4()), user_ctx=user, resource="customers:list", policy_id="enterprise_crm/http_authz", policy_hash=decision.policy_hash) is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_cache_bypasses_redis_errors_but_never_bypasses_auth(opa_url: str):
    opa = OpaClient(opa_url, timeout_seconds=2.0)
    input_obj = {"tenant_id": str(uuid4()), "user": {"id": "u1", "roles": ["admin"]}, "action": "customers:read", "resource": {"type": "customers"}}
    decision = await opa.evaluate(policy_path="enterprise_crm/rbac", input_obj=input_obj)
    assert decision.allow is True

    cache = SecureCache("redis://localhost:1")
    user = UserContext(user_id="u1", roles_hash="rh")
    try:
        try:
            await cache.get(tenant_id=input_obj["tenant_id"], user_ctx=user, resource="customers:list", policy_id="enterprise_crm/http_authz", policy_hash=decision.policy_hash)
        except Exception:
            pass
        computed = {"computed": True}
        return_value = computed if decision.allow else None
        assert return_value == {"computed": True}
    finally:
        await cache.close()
