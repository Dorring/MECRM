"""Serialization round-trip and hash stability tests.

All tests run under AI_MODE=deterministic; no Ollama, no API keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from multi_agent.contracts import (
    ActionProposal,
    AgentAuthority,
    AgentCapability,
    AgentResult,
    AgentTask,
    Evidence,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
    _compute_proposal_hash,
)
from multi_agent.registry import AgentRegistry, RegistrySnapshot
from multi_agent.serialization import (
    deserialize_contract,
    serialize_contract,
    serialize_set_for_json,
    stable_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_result() -> AgentResult:
    ev = Evidence(
        evidence_id="ev-001",
        evidence_type="opa_policy",
        tenant_id="t-001",
        source_agent="test_agent",
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    p = ActionProposal(
        proposal_id="p-001",
        proposal_hash=_compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent_a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={},
            priority="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-001",
        ),
        tenant_id="t-001",
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        priority="medium",
        idempotency_key="ik-001",
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    return AgentResult(
        result_id="r-001",
        task_id="task-001",
        agent_id="agent_a",
        tenant_id="t-001",
        status="completed",
        confidence=0.95,
        duration_ms=150.0,
        output={"summary": "done"},
        evidence=[ev],
        action_proposals=[p],
        token_usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        tool_calls=[ToolCallRecord(tool_name="crm_reader.get_leads", authority=ToolAuthority.READ, ok=True)],
        provider_metadata=ProviderMetadata(
            provider="ollama", chat_model="llama3.1", embedding_model="nomic", ai_mode="live"
        ),
        completed_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_agent_task_round_trip(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="agent_a",
            task_type="test",
            input_data={"key": "value"},
            tenant_id="t-001",
            timeout_ms=60_000,
            idempotency_key="ik-001",
            created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        raw = serialize_contract(task)
        restored = deserialize_contract(raw, AgentTask)
        assert restored.task_id == task.task_id
        assert restored.tenant_id == task.tenant_id
        assert restored.created_at == task.created_at

    def test_agent_result_round_trip(self):
        result = _make_result()
        raw = serialize_contract(result)
        restored = deserialize_contract(raw, AgentResult)
        assert restored.result_id == result.result_id
        assert restored.confidence == result.confidence
        assert len(restored.evidence) == 1
        assert len(restored.action_proposals) == 1

    def test_capability_round_trip(self):
        cap = AgentCapability(
            agent_id="test_agent",
            version="1.0.0",
            description="Test",
            domains={"support"},
            supported_tasks={"triage"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="test_in",
            output_contract="test_out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        raw = serialize_contract(cap)
        restored = deserialize_contract(raw, AgentCapability)
        assert restored.agent_id == cap.agent_id
        assert restored.domains == cap.domains
        assert restored.authority == cap.authority

    def test_evidence_round_trip(self):
        ev = Evidence(
            evidence_id="ev-001",
            evidence_type="opa_policy",
            tenant_id="t-001",
            source_agent="agent_a",
            content_hash="abc123",
            created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        raw = serialize_contract(ev)
        restored = deserialize_contract(raw, Evidence)
        assert restored.evidence_id == ev.evidence_id
        assert restored.content_hash == ev.content_hash

    def test_action_proposal_round_trip(self):
        now = _utc_now()
        h = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent_a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"amount": 100},
            priority="medium",
            justification="because",
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        p = ActionProposal(
            proposal_id="p-001",
            proposal_hash=h,
            tenant_id="t-001",
            created_by_agent="agent_a",
            action_type="create",
            target_entity="ticket",
            payload={"amount": 100},
            priority="medium",
            justification="because",
            idempotency_key="ik-1",
            created_at=now,
        )
        raw = serialize_contract(p)
        restored = deserialize_contract(raw, ActionProposal)
        assert restored.proposal_id == p.proposal_id
        assert restored.proposal_hash == p.proposal_hash


# ---------------------------------------------------------------------------
# datetime UTC
# ---------------------------------------------------------------------------


class TestUtcSerialization:
    def test_datetime_utc_round_trip(self):
        """UTC datetime should survive round-trip without offset shift."""
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="completed",
            completed_at=dt,
        )
        raw = serialize_contract(result)
        restored = deserialize_contract(raw, AgentResult)
        assert restored.completed_at is not None
        assert restored.completed_at.isoformat() == dt.isoformat()

    def test_utc_z_suffix(self):
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="completed",
            completed_at=dt,
        )
        raw = serialize_contract(result)
        # The ISO string with Z suffix should appear
        assert "2025-06-01T12:00:00Z" in raw


# ---------------------------------------------------------------------------
# Enum serialization
# ---------------------------------------------------------------------------


class TestEnumSerialization:
    def test_authority_serializes_as_value(self):
        cap = AgentCapability(
            agent_id="test_agent",
            version="1.0.0",
            description="Test",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.PROPOSE,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        raw = serialize_contract(cap)
        assert '"propose"' in raw

    def test_tool_authority_serialization(self):
        tcr = ToolCallRecord(tool_name="test", authority=ToolAuthority.EXECUTE, ok=True)
        raw = serialize_contract(tcr)
        assert '"execute"' in raw


# ---------------------------------------------------------------------------
# Set serialization
# ---------------------------------------------------------------------------


class TestSetSerialization:
    def test_serialize_set_sorted(self):
        result = serialize_set_for_json({"b", "a", "c"})
        assert result == ["a", "b", "c"]

    def test_serialize_set_empty(self):
        assert serialize_set_for_json(set()) == []

    def test_capability_set_fields_in_json(self):
        cap = AgentCapability(
            agent_id="test_agent",
            version="1.0.0",
            description="Test",
            domains={"support", "sales"},
            supported_tasks={"task_a", "task_b"},
            allowed_tools={"tool_x"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        raw = serialize_contract(cap)
        # Sets should be serialized as arrays (Pydantic JSON mode)
        parsed = json.loads(raw)
        assert set(parsed["domains"]) == {"sales", "support"}
        assert set(parsed["supported_tasks"]) == {"task_a", "task_b"}


# ---------------------------------------------------------------------------
# Snapshot hash stability
# ---------------------------------------------------------------------------


class TestSnapshotHash:
    def test_snapshot_hash_stable(self):
        reg1 = AgentRegistry()
        reg1.register(
            AgentCapability(
                agent_id="agent_a",
                version="1.0.0",
                description="Agent A",
                domains={"support"},
                supported_tasks={"triage"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="in",
                output_contract="out",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
            ),
            object(),  # Fake handler
        )

        snap1 = reg1.snapshot()
        snap2 = reg1.snapshot()
        assert snap1.version == snap2.version

    def test_serialize_snapshot(self):
        reg = AgentRegistry()
        reg.register(
            AgentCapability(
                agent_id="agent_a",
                version="1.0.0",
                description="Agent A",
                domains={"support"},
                supported_tasks={"triage"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="in",
                output_contract="out",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
            ),
            object(),
        )
        snap = reg.snapshot()
        raw = serialize_contract(snap)
        restored = deserialize_contract(raw, RegistrySnapshot)
        assert restored.version == snap.version


# ---------------------------------------------------------------------------
# No handler / secret leaks
# ---------------------------------------------------------------------------


class TestNoLeaks:
    def test_snapshot_no_python_objects(self):
        """Serialized snapshot must not contain handler refs."""
        reg = AgentRegistry()
        reg.register(
            AgentCapability(
                agent_id="agent_a",
                version="1.0.0",
                description="Agent A",
                domains={"test"},
                supported_tasks={"test_task"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="in",
                output_contract="out",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
            ),
            object(),
        )
        snap = reg.snapshot()
        raw = serialize_contract(snap)
        assert "object" not in raw.lower()
        assert "0x" not in raw

    def test_result_serialization_has_no_secrets(self):
        result = _make_result()
        raw = serialize_contract(result)
        assert "api_key" not in raw.lower()
        assert "password" not in raw.lower()
        assert "secret" not in raw.lower()


# ---------------------------------------------------------------------------
# Stable hash
# ---------------------------------------------------------------------------


class TestStableHash:
    def test_stable_hash_same_content_same_hash(self):
        cap1 = AgentCapability(
            agent_id="agent_a",
            version="1.0.0",
            description="Agent A",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        cap2 = AgentCapability(
            agent_id="agent_a",
            version="1.0.0",
            description="Agent A",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        assert stable_hash(cap1) == stable_hash(cap2)

    def test_stable_hash_different_content_different_hash(self):
        cap1 = AgentCapability(
            agent_id="agent_a",
            version="1.0.0",
            description="Agent A",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        cap2 = AgentCapability(
            agent_id="agent_b",  # different
            version="1.0.0",
            description="Agent B",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        assert stable_hash(cap1) != stable_hash(cap2)

    def test_stable_hash_with_exclude(self):
        cap = AgentCapability(
            agent_id="agent_a",
            version="1.0.0",
            description="Agent A",
            domains={"test"},
            supported_tasks={"test_task"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        h1 = stable_hash(cap)
        # Exclude a field that doesn't affect semantics
        h2 = stable_hash(cap, exclude={"description"})
        assert h1 != h2  # hash should differ when a field is excluded
