import uuid

import pytest


class FakeRedis:
    def __init__(self):
        self._kv: dict[bytes, bytes] = {}

    async def get(self, key):
        k = key.encode("utf-8") if isinstance(key, str) else key
        return self._kv.get(k)

    async def set(self, key, value, ex=None):
        k = key.encode("utf-8") if isinstance(key, str) else key
        v = value.encode("utf-8") if isinstance(value, str) else value
        self._kv[k] = v

    async def delete(self, key):
        k = key.encode("utf-8") if isinstance(key, str) else key
        self._kv.pop(k, None)

    async def scan_iter(self, match: str):
        return
        yield


@pytest.mark.asyncio
async def test_approval_pending_action_round_trip():
    from governance.approval_service import ApprovalService, PendingAction

    fake = FakeRedis()
    svc = ApprovalService("redis://fake", redis_client=fake)
    await svc.start()

    approval_id = str(uuid.uuid4())
    pending = PendingAction(
        tenant_id="tenant-1",
        agent_id="sales-agent",
        approval_id=approval_id,
        action_type="leads:qualify",
        topic="crm.leads.qualified",
        event_type="crm.leads.qualified",
        data={"leadId": "lead-1", "approvalId": approval_id},
        correlation_id=str(uuid.uuid4()),
    )

    await svc.request_approval(pending, ttl_seconds=60)

    popped = await svc.pop_pending(approval_id)
    assert popped is not None
    assert popped.approval_id == approval_id
    assert popped.topic == "crm.leads.qualified"

    popped2 = await svc.pop_pending(approval_id)
    assert popped2 is None
