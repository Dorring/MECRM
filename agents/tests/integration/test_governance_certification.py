import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from governance.agent_telemetry import metrics_response, observe_decision_latency
from governance.approval_service import ApprovalService, PendingAction
from governance.kill_switch import AgentKillSwitch
from orchestrator.router import AgentRouter


REPO_ROOT = Path(__file__).resolve().parents[3]


def _enabled() -> bool:
    return os.environ.get("CERTIFICATION_ENV", "").lower() in ("local", "ci", "staging")


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is required")
    return v


@pytest.mark.asyncio
async def test_kill_switch_propagation_under_1s():
    if not _enabled():
        pytest.skip("CERTIFICATION_ENV not enabled")

    redis_url = _env("REDIS_URL", "redis://localhost:6379")
    tenant_id = str(uuid.uuid4())
    agent_id = "sales-agent"

    ks_writer = AgentKillSwitch(redis_url)
    ks_reader = AgentKillSwitch(redis_url)
    await ks_writer.start()
    await ks_reader.start()
    try:
        t0 = time.perf_counter()
        await ks_writer.pause_all_agents(tenant_id, reason="certification")

        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            decision = await ks_reader.decision(tenant_id=tenant_id, agent_id=agent_id)
            if decision.blocked:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("kill switch did not propagate within 1s")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        assert elapsed_ms <= 1000
    finally:
        await ks_writer.close()
        await ks_reader.close()


@pytest.mark.asyncio
async def test_approval_decision_replays_pending_action_and_creates_explainability_artifact():
    if not _enabled():
        pytest.skip("CERTIFICATION_ENV not enabled")

    database_url = _env("DATABASE_URL", "postgresql://crm_app:crm_password@localhost:5432/enterprise_crm")
    kafka_brokers = _env("KAFKA_BROKERS", "localhost:9094")
    redis_url = _env("REDIS_URL", "redis://localhost:6379")

    tenant_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    aggregate_id = str(uuid.uuid4())

    svc = ApprovalService(redis_url)
    await svc.start()

    pending = PendingAction(
        tenant_id=tenant_id,
        agent_id="sales-agent",
        approval_id=approval_id,
        action_type="leads:qualify",
        topic="crm.leads.events",
        event_type="crm.leads.updated",
        data={
            "approvalId": approval_id,
            "aggregate_type": "lead",
            "aggregate_id": aggregate_id,
            "event_type": "lead.updated",
            "version": 2,
            "schema_version": 1,
            "payload": {"leadId": aggregate_id, "changes": {"status": "qualified"}},
            "confidence": 0.5,
            "reasoning": "certification approval replay",
            "factors": [{"name": "low_confidence", "value": 0.5}],
            "riskLevel": "HIGH",
        },
        correlation_id=str(uuid.uuid4()),
    )

    await svc.request_approval(pending, ttl_seconds=60)

    consumer = AIOKafkaConsumer(
        "crm.leads.events",
        bootstrap_servers=kafka_brokers.split(","),
        group_id=f"cert-approval-{uuid.uuid4()}",
        auto_offset_reset="latest",
        enable_auto_commit=False,
        value_deserializer=lambda m: m.decode("utf-8"),
    )
    producer = AIOKafkaProducer(bootstrap_servers=kafka_brokers.split(","), value_serializer=lambda v: v.encode("utf-8"))

    await consumer.start()
    await producer.start()
    try:
        router = AgentRouter()
        await router.initialize(producer)

        event = {"tenantid": tenant_id, "data": {"approvalId": approval_id, "decision": "approved"}}
        await router._handle_approval_decision(event)

        msg = await asyncio.wait_for(consumer.getone(), timeout=15)
        produced = json.loads(msg.value)
        assert produced.get("tenantid") == tenant_id
        assert produced.get("data", {}).get("approvalId") == approval_id

        again = await router.approval_service.pop_pending(approval_id)
        assert again is None

        pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=3)
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                    row = await conn.fetchrow(
                        """
                        SELECT count(*) AS c
                        FROM agent_decisions
                        WHERE tenant_id = $1::uuid
                          AND agent_id = $2
                          AND action_type = $3
                        """,
                        UUID(tenant_id),
                        "sales-agent",
                        "crm.leads.updated",
                    )
                    assert int(row["c"]) >= 1
        finally:
            await pool.close()

        os.makedirs(REPO_ROOT / "reports" / "security", exist_ok=True)
        report = {
            "phase": "ai_governance_certification",
            "tenant_id": tenant_id,
            "approval_id": approval_id,
            "event_type": "crm.leads.updated",
            "topic": "crm.leads.events",
            "pending_replayed": True,
            "explainability_artifact_written": True,
        }
        (REPO_ROOT / "reports" / "security" / "governance-enforcement-report.json").write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        await producer.stop()
        await consumer.stop()


def test_metrics_export_contains_required_series():
    if not _enabled():
        pytest.skip("CERTIFICATION_ENV not enabled")

    observe_decision_latency(agent_id="test", action_type="test", risk_level="test", status="ok", duration_ms=1.0)
    resp = metrics_response()
    body = resp.body.decode("utf-8", errors="replace")
    required = [
        "agent_decision_latency_ms_bucket",
        "agent_tool_call_count_total",
        "agent_error_total",
        "agent_approval_required_total",
        "agent_policy_violations_total",
        "agent_kill_switch_activations_total",
    ]
    missing = [m for m in required if m not in body]
    assert missing == []
