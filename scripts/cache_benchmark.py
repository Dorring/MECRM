import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from cache.secure_cache import SecureCache, UserContext
from policy.opa_binding import OpaClient, compute_policy_hash, compute_roles_hash


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


def _p95(values_ms: list[float]) -> float:
    if not values_ms:
        return 0.0
    if len(values_ms) < 20:
        return sorted(values_ms)[max(0, int(len(values_ms) * 0.95) - 1)]
    return quantiles(values_ms, n=20)[18]


async def main() -> None:
    redis_url = _env("REDIS_URL", "redis://localhost:6379")
    opa_url = _env("OPA_URL", "http://localhost:8181")
    iterations = int(os.environ.get("CACHE_BENCH_ITERATIONS", "200"))

    tenant_id = os.environ.get("CACHE_BENCH_TENANT_ID", str(uuid4()))
    user_id = os.environ.get("CACHE_BENCH_USER_ID", "bench-user")
    roles = ["admin"]

    roles_hash = compute_roles_hash(roles=roles)
    input_obj = {
        "tenant_id": tenant_id,
        "user": {"id": user_id, "roles": roles},
        "action": "customers:read",
        "resource": {"type": "customers", "tenant_id": tenant_id},
    }
    policy_hash = compute_policy_hash(input_obj=input_obj)
    policy_id = "enterprise_crm/rbac"
    resource = "opa:customers:read"

    opa = OpaClient(opa_url, timeout_seconds=2.0)
    cache = SecureCache(redis_url)
    await cache.start()
    try:
        baseline_ms: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            decision = await opa.evaluate(policy_path=policy_id, input_obj=input_obj)
            baseline_ms.append((time.perf_counter() - t0) * 1000)
            if not decision.allow:
                raise RuntimeError("benchmark input unexpectedly denied")

        cached_path_ms: list[float] = []
        user_ctx = UserContext(user_id=user_id, roles_hash=roles_hash)

        await cache.invalidate_tenant(tenant_id)

        for _ in range(iterations):
            t0 = time.perf_counter()
            cached = await cache.get(tenant_id=tenant_id, user_ctx=user_ctx, resource=resource, policy_id=policy_id, policy_hash=policy_hash)
            if cached is None:
                decision = await opa.evaluate(policy_path=policy_id, input_obj=input_obj)
                if not decision.allow:
                    raise RuntimeError("benchmark input unexpectedly denied")
                await cache.set(
                    tenant_id=tenant_id,
                    user_ctx=user_ctx,
                    resource=resource,
                    policy_id=policy_id,
                    policy_hash=policy_hash,
                    value={"allow": True},
                    ttl_seconds=60,
                )
            cached_path_ms.append((time.perf_counter() - t0) * 1000)

        p95_before = _p95(baseline_ms)
        p95_after = _p95(cached_path_ms)
        improvement_pct = 0.0 if p95_before <= 0 else max(0.0, (p95_before - p95_after) / p95_before * 100.0)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "iterations": iterations,
            "tenant_id": tenant_id,
            "policy_id": policy_id,
            "p95_before_ms": round(p95_before, 3),
            "p95_after_ms": round(p95_after, 3),
            "improvement_pct": round(improvement_pct, 2),
        }

        out_dir = ROOT / "reports" / "cache"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "perf_report.json"
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

        print(json.dumps(report, indent=2))
    finally:
        await cache.close()


if __name__ == "__main__":
    asyncio.run(main())
