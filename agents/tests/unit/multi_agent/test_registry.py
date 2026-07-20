"""AgentRegistry + ToolCatalog tests — Phase 2 R3."""

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
    CapabilityValidationError,
    DisabledAgentError,
    DuplicateAgentError,
    DuplicateToolError,
    UnauthorizedToolError,
    UnknownAgentError,
    UnknownToolError,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor

# Helpers ----------------------------------------------------------------


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    domains: frozenset[str] | None = None,
    supported_tasks: frozenset[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
    enabled: bool = True,
    **overrides,
) -> AgentCapability:
    defaults: dict = dict(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        domains=domains or frozenset({"test"}),
        supported_tasks=supported_tasks or frozenset({"test_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_leads"}),
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

    def test_unknown_tool_raises(self):
        cat = ToolCatalog()
        with pytest.raises(UnknownToolError):
            cat.resolve("nonexistent")

    def test_duplicate_tool_raises(self):
        cat = ToolCatalog()
        cat.register(ToolDescriptor(tool_name="t1", authority=ToolAuthority.READ))
        with pytest.raises(DuplicateToolError):
            cat.register(
                ToolDescriptor(tool_name="t1", authority=ToolAuthority.EXECUTE)
            )

    def test_default_catalog_has_builtins(self):
        cat = ToolCatalog.default_catalog()
        assert cat.is_registered("crm_reader.get_leads")
        assert cat.is_registered("crm_writer.propose")

    def test_snapshot_sorted(self):
        cat = ToolCatalog()
        cat.register(ToolDescriptor(tool_name="b", authority=ToolAuthority.READ))
        cat.register(ToolDescriptor(tool_name="a", authority=ToolAuthority.READ))
        snap = cat.snapshot()
        assert [t.tool_name for t in snap] == ["a", "b"]


# Registration -----------------------------------------------------------


class TestRegistration:
    def test_register_success(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="aa"), FakeHandler("aa"))
        assert reg.is_registered("aa")

    def test_duplicate_raises(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="aa"), FakeHandler("aa"))
        with pytest.raises(DuplicateAgentError):
            reg.register(_make_capability(agent_id="aa"), FakeHandler("aa2"))

    def test_replace_only_known(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownAgentError):
            reg.replace(_make_capability(agent_id="ghost"), FakeHandler("ghost"))

    def test_replace_success(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="aa", version="1.0.0"), FakeHandler("a1")
        )
        reg.replace(_make_capability(agent_id="aa", version="2.0.0"), FakeHandler("a2"))
        cap, _ = reg.resolve("aa")
        assert cap.version == "2.0.0"


# Resolution -------------------------------------------------------------


class TestResolution:
    def test_resolve_unknown_raises(self):
        with pytest.raises(UnknownAgentError):
            AgentRegistry().resolve("ghost")

    def test_resolve_disabled_raises(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="aa", enabled=False), FakeHandler("aa"))
        with pytest.raises(DisabledAgentError):
            reg.resolve("aa")


# Tool authority ---------------------------------------------------------


class TestToolAuthority:
    def test_unknown_tool_rejected_at_register(self):
        reg = AgentRegistry()
        with pytest.raises(UnknownToolError):
            reg.register(
                _make_capability(
                    agent_id="aa", allowed_tools=frozenset({"evil.delete_all"})
                ),
                FakeHandler("aa"),
            )

    def test_read_agent_cannot_use_execute(self):
        reg = AgentRegistry()
        with pytest.raises(UnauthorizedToolError):
            reg.register(
                _make_capability(
                    agent_id="aa",
                    authority=AgentAuthority.READ,
                    allowed_tools=frozenset({"automation_executor.execute"}),
                ),
                FakeHandler("aa"),
            )


# validate_task / validate_tool_access ------------------------------------


class TestValidateTask:
    def test_valid_task_passes(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="aa", supported_tasks=frozenset({"triage"}), timeout_ms=60_000
            ),
            FakeHandler("aa"),
        )
        task = AgentTask(
            objective="test",
            task_id="t-1",
            agent_id="aa",
            task_type="triage",
            input_data={},
            tenant_id="t-1",
            timeout_ms=30_000,
            idempotency_key="ik-1",
        )
        result = reg.validate_task(task)
        assert result.agent_id == "aa"

    def test_unsupported_task_raises_capability_error(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="aa", supported_tasks=frozenset({"x"})),
            FakeHandler("aa"),
        )
        task = AgentTask(
            objective="test",
            task_id="t-1",
            agent_id="aa",
            task_type="y",
            input_data={},
            tenant_id="t-1",
            timeout_ms=30_000,
            idempotency_key="ik-1",
        )
        with pytest.raises(CapabilityValidationError):
            reg.validate_task(task)

    def test_timeout_exceeds_raises_capability_error(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="aa", timeout_ms=10_000), FakeHandler("aa")
        )
        task = AgentTask(
            objective="test",
            task_id="t-1",
            agent_id="aa",
            task_type="test_task",
            input_data={},
            tenant_id="t-1",
            timeout_ms=60_000,
            idempotency_key="ik-1",
        )
        with pytest.raises(CapabilityValidationError):
            reg.validate_task(task)


class TestValidateToolAccess:
    def test_valid_access(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="aa",
                allowed_tools=frozenset({"crm_reader.get_leads"}),
                authority=AgentAuthority.READ,
            ),
            FakeHandler("aa"),
        )
        tool = reg.validate_tool_access("aa", "crm_reader.get_leads")
        assert tool.tool_name == "crm_reader.get_leads"

    def test_tool_not_allowed_raises(self):
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="aa",
                allowed_tools=frozenset({"crm_reader.get_leads"}),
                authority=AgentAuthority.READ,
            ),
            FakeHandler("aa"),
        )
        with pytest.raises(UnauthorizedToolError):
            reg.validate_tool_access("aa", "crm_writer.propose")


# Capability frozen -------------------------------------------------------


class TestCapabilityFrozen:
    def test_registry_cannot_be_mutated_through_capability(self):
        reg = AgentRegistry()
        cap = _make_capability(
            agent_id="support1",
            authority=AgentAuthority.READ,
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg.register(cap, object())
        resolved = reg.resolve_capability("support1")
        # frozen — can't change
        with pytest.raises(Exception):
            resolved.authority = AgentAuthority.EXECUTE  # type: ignore[misc]

    def test_snapshot_mutation_does_not_change_registry(self):
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a1"), object())
        snap = reg.snapshot()
        snap.agents.clear()
        assert reg.is_registered("a1")


# ============================================================================
# R6: Registry deep copy — metadata mutation does not leak
# ============================================================================


class TestRegistryDeepCopy:
    def test_original_capability_mutation_does_not_change_registry(self):
        """Mutating the original capability after register doesn't affect registry."""
        reg = AgentRegistry()
        cap = _make_capability(agent_id="aa", metadata={"key": "original"})
        reg.register(cap, object())
        cap.metadata["key"] = "mutated"  # mutate original
        resolved = reg.resolve_capability("aa")
        assert resolved.metadata["key"] == "original"

    def test_resolved_metadata_mutation_does_not_change_registry(self):
        """Mutating a resolved capability doesn't affect the registry."""
        reg = AgentRegistry()
        reg.register(
            _make_capability(agent_id="aa", metadata={"key": "original"}), object()
        )
        resolved1 = reg.resolve_capability("aa")
        resolved1.metadata["key"] = "mutated"
        resolved2 = reg.resolve_capability("aa")
        assert resolved2.metadata["key"] == "original"

    def test_metadata_mutation_does_not_change_snapshot_version(self):
        """Snapshot version is stable even if resolved capability metadata is mutated."""
        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="aa", metadata={"key": "v1"}), object())
        v1 = reg.snapshot().version
        resolved = reg.resolve_capability("aa")
        resolved.metadata["key"] = "v2"
        v2 = reg.snapshot().version
        assert v1 == v2


# ============================================================================
# R7: All read APIs return copies — list_by_domain / list_by_task / list_all
# ============================================================================


class TestRegistryReadApiCopies:
    def _registry_with_two(self) -> AgentRegistry:
        reg = AgentRegistry()
        reg.register(
            _make_capability(
                agent_id="aa",
                domains=frozenset({"support"}),
                supported_tasks=frozenset({"triage"}),
                metadata={"key": "original"},
            ),
            object(),
        )
        reg.register(
            _make_capability(
                agent_id="bb",
                domains=frozenset({"support"}),
                supported_tasks=frozenset({"triage"}),
                metadata={"key": "original"},
            ),
            object(),
        )
        return reg

    def test_list_all_metadata_mutation_does_not_change_registry(self):
        reg = self._registry_with_two()
        caps = reg.list_all()
        assert len(caps) == 2
        # Mutate returned copies
        for cap in caps:
            cap.metadata["key"] = "mutated"
        # Re-read — registry internals unchanged
        fresh = reg.list_all()
        assert all(c.metadata["key"] == "original" for c in fresh)

    def test_list_by_domain_mutation_does_not_change_registry(self):
        reg = self._registry_with_two()
        caps = reg.list_by_domain("support")
        assert len(caps) == 2
        for cap in caps:
            cap.metadata["key"] = "mutated"
        fresh = reg.list_by_domain("support")
        assert all(c.metadata["key"] == "original" for c in fresh)

    def test_list_by_task_mutation_does_not_change_registry(self):
        reg = self._registry_with_two()
        caps = reg.list_by_task("triage")
        assert len(caps) == 2
        for cap in caps:
            cap.metadata["key"] = "mutated"
        fresh = reg.list_by_task("triage")
        assert all(c.metadata["key"] == "original" for c in fresh)

    def test_query_result_mutation_does_not_change_snapshot_version(self):
        """Mutating capabilities returned by list_all / list_by_domain /
        list_by_task must NOT affect the snapshot version."""
        reg = self._registry_with_two()
        v1 = reg.snapshot().version

        # Mutate copies returned by every read API
        for cap in reg.list_all():
            cap.metadata["key"] = "v2"
        for cap in reg.list_by_domain("support"):
            cap.metadata["key"] = "v3"
        for cap in reg.list_by_task("triage"):
            cap.metadata["key"] = "v4"

        v2 = reg.snapshot().version
        assert v1 == v2
