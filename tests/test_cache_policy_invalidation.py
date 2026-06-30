import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from cache.secure_cache import SecureCache, UserContext


@pytest.mark.asyncio
async def test_cache_policy_invalidation(redis_url: str):
    cache = SecureCache(redis_url)
    await cache.start()
    try:
        tenant = str(uuid4())
        user_id = "user-1"
        policy_id = "enterprise_crm/http_authz"
        resource = "customers:list"

        user_ctx = UserContext(user_id=user_id, roles_hash="rh1")
        policy_hash = "ph1"
        await cache.set(tenant_id=tenant, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash=policy_hash, value={"ok": True}, ttl_seconds=60)
        assert await cache.get(tenant_id=tenant, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash=policy_hash) == {"ok": True}

        await cache.invalidate_policy(policy_id)
        assert await cache.get(tenant_id=tenant, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash=policy_hash) is None
    finally:
        await cache.close()
