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
async def test_cache_isolation_by_tenant(redis_url: str):
    cache = SecureCache(redis_url)
    await cache.start()
    try:
        tenant_a = str(uuid4())
        tenant_b = str(uuid4())
        user = UserContext(user_id="user-1", roles_hash="roles-a")
        policy_id = "enterprise_crm/http_authz"
        policy_hash = "ph"
        resource = "customers:list"

        await cache.set(tenant_id=tenant_a, user_ctx=user, resource=resource, policy_id=policy_id, policy_hash=policy_hash, value={"v": 1}, ttl_seconds=60)
        assert await cache.get(tenant_id=tenant_a, user_ctx=user, resource=resource, policy_id=policy_id, policy_hash=policy_hash) == {"v": 1}
        assert await cache.get(tenant_id=tenant_b, user_ctx=user, resource=resource, policy_id=policy_id, policy_hash=policy_hash) is None
    finally:
        await cache.close()
