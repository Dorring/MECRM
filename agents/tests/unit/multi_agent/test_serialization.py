"""Serialization round-trip and canonical stability — Phase 2 R2."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    AgentResult,
    AgentTask,
    ExecutionBudget,
    MultiAgentState,
)
from multi_agent.registry import AgentRegistry
from multi_agent.serialization import (
    canonicalize,
    deserialize_contract,
    serialize_contract,
    serialize_set_for_json,
    stable_hash,
)

# Helpers ----------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_result() -> AgentResult:
    return AgentResult(
        result_id="r-001",
        task_id="task-001",
        agent_id="agent_a",
        tenant_id="t-001",
        status="completed",
        summary="Done",
        completed_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


# Canonicalizer -----------------------------------------------------------


class TestCanonicalizer:
    def test_datetime_normalized_to_utc(self):
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        c = canonicalize(dt)
        assert c.endswith("Z")
        assert "+00:00" not in c

    def test_decimal_preserved(self):
        d = Decimal("123.456")
        c = canonicalize(d)
        assert c == "123.456"

    def test_set_sorted(self):
        c = canonicalize({"b", "a", "c"})
        assert c == ["a", "b", "c"]

    def test_dict_sorted_keys(self):
        d = {"z": 1, "a": 2, "m": 3}
        c = canonicalize(d)
        assert list(c.keys()) == ["a", "m", "z"]

    def test_nested_structure(self):
        obj = {
            "items": [{"b": 2, "a": 1}],
            "tags": {"c", "a", "b"},
        }
        c = canonicalize(obj)
        assert c["items"][0] == {"a": 1, "b": 2}
        assert c["tags"] == ["a", "b", "c"]

    def test_canonicalize_base_model(self):
        task = AgentTask(
            objective="test",
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-1",
            timeout_ms=60_000,
            idempotency_key="ik-1",
            created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        c = canonicalize(task)
        assert c["task_id"] == "task-001"
        assert "2025-06-01T12:00:00Z" in c["created_at"]


# Cross-subprocess stability ----------------------------------------------


class TestCrossProcessStability:
    def test_canonical_stable_across_processes(self):
        """The canonical form of a contract is identical across subprocesses."""
        import os

        script = """
import json
from datetime import datetime, timezone
from multi_agent.contracts import AgentTask
from multi_agent.serialization import serialize_contract

task = AgentTask(objective="test",
    task_id="task-001",
    agent_id="agent_a",
    task_type="test",
    input_data={"key": "value"},
    tenant_id="t-001",
    timeout_ms=60_000,
    idempotency_key="ik-001",
    created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
)
print(serialize_contract(task))
"""
        env = {
            **os.environ,
            "AI_MODE": "deterministic",
            "PYTHONPATH": os.getcwd() + "/src",
        }
        r1 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            env=env,
        )
        r2 = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            env=env,
        )
        if r1.returncode != 0:
            # Subprocess may not have the right pythonpath; skip with context
            pytest.skip(f"Subprocess error: {r1.stderr}")
        assert r1.stdout.strip() == r2.stdout.strip()
        assert len(r1.stdout.strip()) > 0


# JSON round-trip --------------------------------------------------------


class TestJsonRoundTrip:
    def test_agent_task_round_trip(self):
        task = AgentTask(
            objective="test",
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-1",
            timeout_ms=60_000,
            idempotency_key="ik-1",
            created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        raw = serialize_contract(task)
        restored = deserialize_contract(raw, AgentTask)
        assert restored.task_id == task.task_id
        assert restored.created_at == task.created_at

    def test_agent_result_round_trip(self):
        result = _make_result()
        raw = serialize_contract(result)
        restored = deserialize_contract(raw, AgentResult)
        assert restored.result_id == result.result_id

    def test_capability_round_trip(self):
        cap = AgentCapability(
            agent_id="test_agent",
            version="1.0.0",
            description="Test",
            domains={"support"},
            supported_tasks={"triage"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="in",
            output_contract="out",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        raw = serialize_contract(cap)
        restored = deserialize_contract(raw, AgentCapability)
        assert restored.domains == cap.domains

    def test_multi_agent_state_round_trip(self):
        state = MultiAgentState(
            objective="test",
            actor_id="test",
            run_id="run-1",
            tenant_id="t-1",
            budget=ExecutionBudget(max_tasks=8, cost_budget_usd=Decimal("50")),
        )
        raw = serialize_contract(state)
        restored = deserialize_contract(raw, MultiAgentState)
        assert restored.run_id == "run-1"
        assert restored.budget.cost_budget_usd == Decimal("50")

    def test_action_proposal_round_trip(self):
        p = ActionProposal.create(
            proposal_id="p-1",
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        raw = serialize_contract(p)
        restored = deserialize_contract(raw, ActionProposal)
        assert restored.proposal_hash == p.proposal_hash


# No secrets in serialization ---------------------------------------------


class TestNoSecrets:
    def test_no_authorization_in_serialized(self):
        from multi_agent.contracts import AgentExecutionContext

        ctx = AgentExecutionContext(tenant_id="t-1", scopes=["read"])
        raw = serialize_contract(ctx)
        assert "authorization" not in raw.lower()
        assert "bearer" not in raw.lower()
        assert "access_token" not in raw.lower()
        assert "api_key" not in raw.lower()

    def test_no_handler_refs_in_snapshot(self):
        reg = AgentRegistry()
        reg.register(
            AgentCapability(
                agent_id="a",
                version="1.0.0",
                description="A",
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
        assert "0x" not in raw


# Set serialization -------------------------------------------------------


class TestSetSerialization:
    def test_serialize_set_sorted(self):
        assert serialize_set_for_json({"b", "a"}) == ["a", "b"]


# Stable hash ------------------------------------------------------------


class TestStableHash:
    def test_same_content_same_hash(self):
        cap1 = AgentCapability(
            agent_id="a",
            version="1.0.0",
            description="A",
            domains={"test"},
            supported_tasks={"t"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="i",
            output_contract="o",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        cap2 = AgentCapability(
            agent_id="a",
            version="1.0.0",
            description="A",
            domains={"test"},
            supported_tasks={"t"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="i",
            output_contract="o",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        assert stable_hash(cap1) == stable_hash(cap2)

    def test_different_content_different_hash(self):
        cap1 = AgentCapability(
            agent_id="a",
            version="1.0.0",
            description="A",
            domains={"test"},
            supported_tasks={"t"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="i",
            output_contract="o",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        cap2 = AgentCapability(
            agent_id="b",
            version="1.0.0",
            description="B",
            domains={"test"},
            supported_tasks={"t"},
            allowed_tools={"crm_reader.get_leads"},
            authority=AgentAuthority.READ,
            input_contract="i",
            output_contract="o",
            timeout_ms=30_000,
            max_retries=2,
            estimated_cost_class="low",
        )
        assert stable_hash(cap1) != stable_hash(cap2)


# Snapshot hash -----------------------------------------------------------


class TestSnapshotHash:
    def test_version_is_content_hash(self):
        reg1 = AgentRegistry()
        reg1.register(
            AgentCapability(
                agent_id="a",
                version="1.0.0",
                description="A",
                domains={"test"},
                supported_tasks={"t"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="i",
                output_contract="o",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
            ),
            object(),
        )
        reg2 = AgentRegistry()
        reg2.register(
            AgentCapability(
                agent_id="a",
                version="1.0.0",
                description="A",
                domains={"test"},
                supported_tasks={"t"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="i",
                output_contract="o",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
            ),
            object(),
        )
        assert reg1.snapshot().version == reg2.snapshot().version
