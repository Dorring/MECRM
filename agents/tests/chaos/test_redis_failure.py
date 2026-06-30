import os
import uuid

import pytest

from .utils import compose, endpoints


def _skip_if_disabled():
    if os.getenv("CHAOS_TESTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        pytest.skip("CHAOS_TESTS_ENABLED is not true")
    if os.getenv("CHAOS_ENVIRONMENT", "").lower() not in ("local", "ci", "staging"):
        pytest.skip("CHAOS_ENVIRONMENT not in {local,ci,staging}")


@pytest.mark.asyncio
async def test_redis_down_kill_switch_fails_closed():
    _skip_if_disabled()

    compose_file = "docker-compose.chaos.yml"
    compose(compose_file, ["up", "-d", "--build"], timeout=900)

    ep = endpoints()
    compose(compose_file, ["stop", "redis"], timeout=60)

    from governance.kill_switch import AgentKillSwitch

    ks = AgentKillSwitch(ep.redis_url)
    decision = await ks.decision(tenant_id=str(uuid.uuid4()), agent_id="sales-agent")
    assert decision.blocked is True
    assert decision.status is not None
    assert decision.status.state.value in ("killed", "paused")

    compose(compose_file, ["start", "redis"], timeout=120)

