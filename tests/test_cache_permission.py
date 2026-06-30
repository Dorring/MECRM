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
async def test_cache_respects_policy_hash_and_invalidation(redis_url: str):
    cache = SecureCache(redis_url)
    await cache.start()
    try:
        tenant = str(uuid4())
        user_id = "user-1"
        resource = "customers:get:123"
        policy_id = "enterprise_crm/http_authz"

        user_ctx_v1 = UserContext(user_id=user_id, roles_hash="roles-v1")
        policy_hash_v1 = "policyhash-v1"
        await cache.set(
            tenant_id=tenant,
            user_ctx=user_ctx_v1,
            resource=resource,
            policy_id=policy_id,
            policy_hash=policy_hash_v1,
            value={"allowed": True, "roles": "v1"},
            ttl_seconds=60,
        )
        assert (
            await cache.get(tenant_id=tenant, user_ctx=user_ctx_v1, resource=resource, policy_id=policy_id, policy_hash=policy_hash_v1)
            == {"allowed": True, "roles": "v1"}
        )

        user_ctx_v2 = UserContext(user_id=user_id, roles_hash="roles-v2")
        policy_hash_v2 = "policyhash-v2"
        assert await cache.get(tenant_id=tenant, user_ctx=user_ctx_v2, resource=resource, policy_id=policy_id, policy_hash=policy_hash_v2) is None

        await cache.invalidate_user(tenant, user_id)
        assert await cache.get(tenant_id=tenant, user_ctx=user_ctx_v1, resource=resource, policy_id=policy_id, policy_hash=policy_hash_v1) is None

        await cache.set(
            tenant_id=tenant,
            user_ctx=user_ctx_v1,
            resource=resource,
            policy_id=policy_id,
            policy_hash=policy_hash_v1,
            value={"allowed": True, "roles": "v1-new"},
            ttl_seconds=60,
        )
        assert (
            await cache.get(tenant_id=tenant, user_ctx=user_ctx_v1, resource=resource, policy_id=policy_id, policy_hash=policy_hash_v1)
            == {"allowed": True, "roles": "v1-new"}
        )

        await cache.invalidate_policy(policy_id)
        assert await cache.get(tenant_id=tenant, user_ctx=user_ctx_v1, resource=resource, policy_id=policy_id, policy_hash=policy_hash_v1) is None
    finally:
        await cache.close()
