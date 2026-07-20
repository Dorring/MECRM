"""AgentRegistry + ToolCatalog tests — Phase 2 R2."""

from __future__ import annotations

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ToolAuthority,
)
from multi_agent.errors import (
    DisabledAgentError,
    DuplicateAgentError,
    UnauthorizedToolError,
    UnknownAgentError,
    UnknownToolError,
)
from multi_agent.registry import (
    AgentRegistry,
    ToolCatalog,
    ToolDescriptor,
)


# Helpers ----------------------------------------------------------------


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
        description=f"Agent {agent_id}",
        domains=domains or {"test"},
        supported_tasks=supported_tasks or {"test_task"},
        allowed_tools=allowed_tools or {"crm_reader.get_leads"},
        authority=authority,
        input_contract="in",
        output_contract="out",
        timeout_ms=30_000,
        max_retries=2,
        estimated_cost_class="low",
        enabled=enabled,
    )
    defaults.update(overrides)
    return AgentCapability(**defaults)


class FakeHandler:
    def __init__(self, agent_id: str = "test"):
        self.agent_id = agent_id
        self.calls: list = []

    async def run(self, task: AgentTask, context: AgentExecutionContext) -> AgentResult:
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


# ToolCatalog ------------------------------------------------------------


class TestToolCatalog:
    def test_register_and_resolve(self):
        cat = ToolCatalog()
        cat.register(ToolDescriptor(tool_name="my.tool", authority=ToolAuthority.READ))
        t = cat.resolve("my.tool")
        assert t.tool_name == "my.tool"
        assert t.authority == ToolAuthority.READ

    def test_unknown_tool_raises(self):
        cat = ToolCatalog()
        with pytest.raises(UnknownToolError):
            cat.resolve("nonexistent")

    def test_default_catalog_has_builtins(self):
        cat = ToolCatalog.default_catalog()
        assert cat.is_registered("crm_reader.get_leads")
        assert cat.is_registered("crm_writer.propose")
        assert cat.is_registered("automation_executor.execute")

    def test_duplicate_tool_raises(self):
        cat = ToolCatalog()
        cat.register(ToolDescriptor(tool_name="t1", authority=ToolAuthority.READ))
        with pytest.raises(DuplicateAgentError):
            cat.register(
                ToolDescriptor(tool_name="t1", authority=ToolAuthority.EXECUTE)
            )

    def test_snapshot_sorted(self):
        cat = ToolCatalog()
        cat.register(ToolDescriptor(tool_name="b", authority=ToolAuthority.READ))
        cat.register(ToolDescriptor(tool_name="a", authority=ToolAuthority.READ))
        snap = cat.snapshot()
        assert [t.tool_name for t in snap] == ["a", "b"]


# Registry — Registration --------------------------------------------------


class TestRegistration:
    def test_register_success(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        assert reg.is_registered("a")

    def test_duplicate_raises(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        with pytest.raises(DuplicateAgentError):
            reg.register(_make_capability(agent_id="a"), FakeHandler("a2"))

    def test_replace_only_known_agent(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownAgentError):
            reg.replace(_make_capability(agent_id="ghost"), FakeHandler("ghost"))

    def test_replace_success(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a", version="1.0.0"), FakeHandler("a1"))
        reg.replace(_make_capability(agent_id="a", version="2.0.0"), FakeHandler("a2"))
        cap, _ = reg.resolve("a")
        assert cap.version == "2.0.0"

    def test_unregister(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        reg.unregister("a")
        assert not reg.is_registered("a")


# Registry — Resolution ---------------------------------------------------


class TestResolution:
    def test_resolve_returns_both(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        cap, handler = reg.resolve("a")
        assert cap.agent_id == "a"

    def test_resolve_unknown_raises(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownAgentError):
            reg.resolve("ghost")

    def test_resolve_disabled_raises(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a", enabled=False), FakeHandler("a"))
        with pytest.raises(DisabledAgentError):
            reg.resolve("a")

    def test_is_registered_includes_disabled(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a", enabled=False), FakeHandler("a"))
        assert reg.is_registered("a") is True


# Registry — Tool authority -----------------------------------------------


class TestToolAuthority:
    def test_unknown_tool_rejected(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownToolError):
            reg.register(
                _make_capability(agent_id="a", allowed_tools={"evil.delete_all"}),
                FakeHandler("a"),
            )

    def test_read_agent_cannot_use_execute_tool(self):
        reg = AgentRegistry()
        with pytest.raises(UnauthorizedToolError):
            reg.register(
                _make_capability(
                    agent_id="a",
                    authority=AgentAuthority.READ,
                    allowed_tools={"automation_executor.execute"},
                ),
                FakeHandler("a"),
            )


# Registry — validate_task / validate_tool_access --------------------------


class TestValidateTask:
    def test_valid_task_passes(self):
        reg = AgentRegistry()
        cap = _make_capability(
            agent_id="a", supported_tasks={"triage"}, timeout_ms=60_000
        )
        reg.register(cap, FakeHandler("a"))

        task = AgentTask(
            task_id="t-1",
            agent_id="a",
            task_type="triage",
            input_data={},
            tenant_id="t-1",
            timeout_ms=30_000,
            idempotency_key="ik-1",
        )
        result = reg.validate_task(task)
        assert result.agent_id == "a"

    def test_unsupported_task_type_raises(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="a", supported_tasks={"x"}), FakeHandler("a")
        )

        task = AgentTask(
            task_id="t-1",
            agent_id="a",
            task_type="y",  # not in supported_tasks
            input_data={},
            tenant_id="t-1",
            timeout_ms=30_000,
            idempotency_key="ik-1",
        )
        with pytest.raises(UnauthorizedToolError):
            reg.validate_task(task)

    def test_timeout_exceeds_capability_raises(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="a", timeout_ms=10_000), FakeHandler("a")
        )

        task = AgentTask(
            task_id="t-1",
            agent_id="a",
            task_type="test_task",
            input_data={},
            tenant_id="t-1",
            timeout_ms=60_000,  # > 10_000
            idempotency_key="ik-1",
        )
        with pytest.raises(UnauthorizedToolError):
            reg.validate_task(task)


class TestValidateToolAccess:
    def test_valid_access(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="a",
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
            ),
            FakeHandler("a"),
        )
        tool = reg.validate_tool_access("a", "crm_reader.get_leads")
        assert tool.tool_name == "crm_reader.get_leads"

    def test_tool_not_in_allowed_raises(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="a",
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
            ),
            FakeHandler("a"),
        )
        with pytest.raises(UnauthorizedToolError):
            reg.validate_tool_access("a", "crm_writer.propose")

    def test_unknown_tool_raises(self):
        """validate_tool_access with a tool not in the catalog raises UnknownToolError."""
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="a",
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
            ),
            FakeHandler("a"),
        )
        with pytest.raises(UnknownToolError):
            reg.validate_tool_access("a", "nonexistent.tool")

    def test_authority_too_low_raises(self):
        """Register with execute tool fails because READ authority can't use it."""
        with pytest.raises(UnauthorizedToolError):
            reg = AgentRegistry()
            reg.register(
                _make_capability(
                    agent_id="a",
                    allowed_tools={"automation_executor.execute"},
                    authority=AgentAuthority.READ,
                ),
                FakeHandler("a"),
            )


# Registry — Queries (sorted) ---------------------------------------------


class TestQueries:
    def test_list_by_domain_sorted(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="b", domains={"x"}), FakeHandler("b"))
        reg.register(_make_capability(agent_id="a", domains={"x"}), FakeHandler("a"))
        result = reg.list_by_domain("x")
        assert [c.agent_id for c in result] == ["a", "b"]

    def test_list_by_task_sorted(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="z", supported_tasks={"t"}), FakeHandler("z")
        )
        reg.register(
            _make_capability(agent_id="m", supported_tasks={"t"}), FakeHandler("m")
        )
        result = reg.list_by_task("t")
        assert [c.agent_id for c in result] == ["m", "z"]

    def test_list_all_sorted(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="c"), FakeHandler("c"))
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        assert [c.agent_id for c in reg.list_all()] == ["a", "c"]


# Registry — Snapshot -----------------------------------------------------


class TestSnapshot:
    def test_snapshot_no_handler_refs(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        snap = reg.snapshot()
        snap_json = snap.model_dump_json()
        assert "FakeHandler" not in snap_json
        assert "handler" not in snap_json.lower()

    def test_version_stable(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a", version="1.0.0"), FakeHandler("a"))
        v1 = reg.snapshot().version
        v2 = reg.snapshot().version
        assert v1 == v2

    def test_version_changes_on_register(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a"), FakeHandler("a"))
        v1 = reg.snapshot().version
        reg.register(_make_capability(agent_id="b"), FakeHandler("b"))
        v2 = reg.snapshot().version
        assert v1 != v2
