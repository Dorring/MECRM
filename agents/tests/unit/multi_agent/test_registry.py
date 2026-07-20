"""AgentRegistry tests — registration, resolution, query, snapshot.

All tests run under AI_MODE=deterministic; no Ollama, no API keys.
"""

from __future__ import annotations

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
)
from multi_agent.errors import (
    DisabledAgentError,
    DuplicateAgentError,
    UnknownAgentError,
)
from multi_agent.registry import AgentRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    domains: set[str] | None = None,
    supported_tasks: set[str] | None = None,
    allowed_tools: set[str] | None = None,
    enabled: bool = True,
    **overrides,
) -> AgentCapability:
    defaults: dict = dict(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Test agent {agent_id}",
        domains=domains or {"test"},
        supported_tasks=supported_tasks or {"test_task"},
        allowed_tools=allowed_tools or {"crm_reader.get_leads"},
        authority=authority,
        input_contract="test_input",
        output_contract="test_output",
        timeout_ms=30_000,
        max_retries=2,
        estimated_cost_class="low",
        enabled=enabled,
    )
    defaults.update(overrides)
    return AgentCapability(**defaults)


class FakeHandler:
    """A fake AgentHandler that records calls for testing."""

    def __init__(self, agent_id: str = "test_agent"):
        self.agent_id = agent_id
        self.calls: list[tuple[AgentTask, AgentExecutionContext]] = []

    async def run(
        self, task: AgentTask, context: AgentExecutionContext
    ) -> AgentResult:
        self.calls.append((task, context))
        from datetime import datetime, timezone

        return AgentResult(
            result_id=f"r-{task.task_id}",
            task_id=task.task_id,
            agent_id=self.agent_id,
            tenant_id=task.tenant_id,
            status="completed",
            completed_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_success(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a")
        handler = FakeHandler("agent_a")
        reg.register(cap, handler)
        assert reg.is_registered("agent_a")

    def test_duplicate_raises(self):
        reg = AgentRegistry()
        cap1 = _make_capability(agent_id="agent_a")
        cap2 = _make_capability(agent_id="agent_a")
        reg.register(cap1, FakeHandler("agent_a"))
        with pytest.raises(DuplicateAgentError):
            reg.register(cap2, FakeHandler("agent_a-dup"))

    def test_replace_overwrites(self):
        reg = AgentRegistry()
        cap1 = _make_capability(agent_id="agent_a", version="1.0.0")
        cap2 = _make_capability(agent_id="agent_a", version="2.0.0")
        h1 = FakeHandler("agent_a-v1")
        h2 = FakeHandler("agent_a-v2")

        reg.register(cap1, h1)
        reg.replace(cap2, h2)

        resolved_cap, resolved_handler = reg.resolve("agent_a")
        assert resolved_cap.version == "2.0.0"
        assert resolved_handler is h2

    def test_replace_new_agent_allowed(self):
        """Replace is idempotent — it also works as upsert for convenience."""
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_new")
        reg.replace(cap, FakeHandler("agent_new"))
        assert reg.is_registered("agent_new")

    def test_unregister(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a")
        reg.register(cap, FakeHandler("agent_a"))
        assert reg.is_registered("agent_a")
        reg.unregister("agent_a")
        assert not reg.is_registered("agent_a")

    def test_unregister_idempotent(self):
        reg = AgentRegistry()
        reg.unregister("nonexistent")  # no-op


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_resolve_returns_capability_and_handler(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a")
        handler = FakeHandler("agent_a")
        reg.register(cap, handler)

        resolved_cap, resolved_handler = reg.resolve("agent_a")
        assert resolved_cap.agent_id == "agent_a"
        assert resolved_handler is handler

    def test_resolve_unregistered_raises(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownAgentError):
            reg.resolve("ghost")

    def test_resolve_disabled_raises(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a", enabled=False)
        reg.register(cap, FakeHandler("agent_a"))
        with pytest.raises(DisabledAgentError):
            reg.resolve("agent_a")

    def test_resolve_capability_only(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a")
        reg.register(cap, FakeHandler("agent_a"))
        resolved = reg.resolve_capability("agent_a")
        assert resolved.agent_id == "agent_a"
        assert resolved.version == "1.0.0"

    def test_resolve_capability_disabled_raises(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a", enabled=False)
        reg.register(cap, FakeHandler("agent_a"))
        with pytest.raises(DisabledAgentError):
            reg.resolve_capability("agent_a")

    def test_is_registered_includes_disabled(self):
        reg = AgentRegistry()
        cap = _make_capability(agent_id="agent_a", enabled=False)
        reg.register(cap, FakeHandler("agent_a"))
        assert reg.is_registered("agent_a") is True


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_list_by_domain(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="support1", domains={"support", "ticketing"}),
            FakeHandler("support1"),
        )
        reg.register(
            _make_capability(agent_id="sales1", domains={"sales", "deals"}),
            FakeHandler("sales1"),
        )
        reg.register(
            _make_capability(agent_id="disabled1", domains={"support"}, enabled=False),
            FakeHandler("disabled1"),
        )

        support_agents = reg.list_by_domain("support")
        assert len(support_agents) == 1
        assert support_agents[0].agent_id == "support1"

    def test_list_by_domain_empty(self):
        reg = AgentRegistry()
        assert reg.list_by_domain("nonexistent") == []

    def test_list_by_task(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="triage1", supported_tasks={"triage", "respond"}),
            FakeHandler("triage1"),
        )
        reg.register(
            _make_capability(agent_id="forecast1", supported_tasks={"forecast"}),
            FakeHandler("forecast1"),
        )

        triage_agents = reg.list_by_task("triage")
        assert len(triage_agents) == 1
        assert triage_agents[0].agent_id == "triage1"

    def test_list_by_task_disabled_excluded(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="enabled1",
                supported_tasks={"task_x"},
                enabled=True,
            ),
            FakeHandler("enabled1"),
        )
        reg.register(
            _make_capability(
                agent_id="disabled1",
                supported_tasks={"task_x"},
                enabled=False,
            ),
            FakeHandler("disabled1"),
        )

        agents = reg.list_by_task("task_x")
        assert len(agents) == 1
        assert agents[0].agent_id == "enabled1"


# ---------------------------------------------------------------------------
# Tool authority checks
# ---------------------------------------------------------------------------


class TestToolAuthorityChecks:
    def test_read_agent_cannot_use_propose_tool(self):
        """Pydantic model_validator rejects READ authority with propose tool."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            _make_capability(
                agent_id="reader",
                authority=AgentAuthority.READ,
                allowed_tools={"crm_writer.propose"},
            )
        assert "READ" in str(exc.value) or "propose" in str(exc.value).lower()

    def test_propose_agent_cannot_use_execute_tool(self):
        """Pydantic model_validator rejects PROPOSE authority with execute tool."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            _make_capability(
                agent_id="proposer",
                authority=AgentAuthority.PROPOSE,
                allowed_tools={"automation_executor.execute"},
            )
        assert "PROPOSE" in str(exc.value) or "execute" in str(exc.value).lower()

    def test_execute_agent_can_use_execute_tool(self):
        reg = AgentRegistry()
        cap = _make_capability(
            agent_id="executor",
            authority=AgentAuthority.EXECUTE,
            allowed_tools={"automation_executor.execute"},
        )
        reg.register(cap, FakeHandler("executor"))
        assert reg.is_registered("executor")

    def test_unknown_tool_allowed(self):
        """Unknown tools are allowed through (Phase 3+ can add strict mode)."""
        reg = AgentRegistry()
        cap = _make_capability(
            agent_id="reader",
            authority=AgentAuthority.READ,
            allowed_tools={"future.tool"},
        )
        reg.register(cap, FakeHandler("reader"))
        assert reg.is_registered("reader")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_has_capabilities(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="agent_a"),
            FakeHandler("agent_a"),
        )
        reg.register(
            _make_capability(agent_id="agent_b"),
            FakeHandler("agent_b"),
        )

        snap = reg.snapshot()
        assert "agent_a" in snap.agents
        assert "agent_b" in snap.agents
        assert snap.agents["agent_a"].agent_id == "agent_a"

    def test_snapshot_includes_disabled(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="enabled1", enabled=True),
            FakeHandler("enabled1"),
        )
        reg.register(
            _make_capability(agent_id="disabled1", enabled=False),
            FakeHandler("disabled1"),
        )

        snap = reg.snapshot()
        assert "enabled1" in snap.agents
        assert "disabled1" in snap.agents

    def test_snapshot_stable(self):
        """Two snapshots from same registry should be identical (except created_at)."""
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="agent_a"),
            FakeHandler("agent_a"),
        )

        snap1 = reg.snapshot()
        snap2 = reg.snapshot()

        assert snap1.version == snap2.version
        assert snap1.agents.keys() == snap2.agents.keys()

    def test_snapshot_version_changes(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="agent_a"),
            FakeHandler("agent_a"),
        )
        v1 = reg.snapshot().version

        reg.register(
            _make_capability(agent_id="agent_b"),
            FakeHandler("agent_b"),
        )
        v2 = reg.snapshot().version

        assert v1 != v2

    def test_snapshot_no_handler_references(self):
        """Snapshot must not contain handler objects or Python addresses."""
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="agent_a"),
            FakeHandler("agent_a"),
        )

        snap = reg.snapshot()
        snap_json = snap.model_dump_json()

        # No Python memory addresses
        assert "0x" not in snap_json
        # No handler references
        assert "FakeHandler" not in snap_json
        assert "handler" not in snap_json.lower()

    def test_registry_version_deterministic(self):
        """Same capabilities in same order → same version hash."""
        reg1 = AgentRegistry()
        reg1.register(
            _make_capability(agent_id="agent_a", version="1.0.0"),
            FakeHandler("agent_a"),
        )
        reg1.register(
            _make_capability(agent_id="agent_b", version="1.0.0"),
            FakeHandler("agent_b"),
        )

        reg2 = AgentRegistry()
        reg2.register(
            _make_capability(agent_id="agent_a", version="1.0.0"),
            FakeHandler("agent_a"),
        )
        reg2.register(
            _make_capability(agent_id="agent_b", version="1.0.0"),
            FakeHandler("agent_b"),
        )

        assert reg1.snapshot().version == reg2.snapshot().version
