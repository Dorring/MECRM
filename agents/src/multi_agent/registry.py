"""Single AgentRegistry with mandatory ToolCatalog.

The registry owns agent_id → (AgentCapability, AgentHandler) and enforces
tool permissions against an injected ToolCatalog.  There must be exactly
ONE registry per process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import Field

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    StrictContract,
    ToolAuthority,
    ToolDescriptor,
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
from multi_agent.serialization import content_hash

# ---------------------------------------------------------------------------
# Tool Catalog
# ---------------------------------------------------------------------------


class ToolCatalog:
    """Single source of truth for tool → authority mapping.

    The catalog is injected into AgentRegistry at construction time.
    Unknown tools are rejected by default (fail-closed).
    """

    def __init__(self, tools: list[ToolDescriptor] | None = None) -> None:
        self._tools: dict[str, ToolDescriptor] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: ToolDescriptor) -> None:
        if tool.tool_name in self._tools:
            raise DuplicateToolError(f"Tool {tool.tool_name!r} is already registered")
        self._tools[tool.tool_name] = tool

    def resolve(self, tool_name: str) -> ToolDescriptor:
        try:
            return self._tools[tool_name]
        except KeyError:
            raise UnknownToolError(f"Tool {tool_name!r} is not in the catalog")

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def snapshot(self) -> list[ToolDescriptor]:
        return sorted(self._tools.values(), key=lambda t: t.tool_name)

    @classmethod
    def default_catalog(cls) -> ToolCatalog:
        """Factory for the built-in Phase 2 tool catalog."""
        return cls(
            tools=[
                ToolDescriptor(
                    tool_name="crm_reader.get_leads",
                    authority=ToolAuthority.READ,
                    description="Read leads",
                ),
                ToolDescriptor(
                    tool_name="crm_reader.get_deals",
                    authority=ToolAuthority.READ,
                    description="Read deals",
                ),
                ToolDescriptor(
                    tool_name="crm_reader.get_tickets",
                    authority=ToolAuthority.READ,
                    description="Read tickets",
                ),
                ToolDescriptor(
                    tool_name="crm_reader.get_customers",
                    authority=ToolAuthority.READ,
                    description="Read customers",
                ),
                ToolDescriptor(
                    tool_name="crm_reader.get_tasks",
                    authority=ToolAuthority.READ,
                    description="Read tasks",
                ),
                ToolDescriptor(
                    tool_name="crm_reader.get_invoices",
                    authority=ToolAuthority.READ,
                    description="Read invoices",
                ),
                ToolDescriptor(
                    tool_name="vector_search.search",
                    authority=ToolAuthority.READ,
                    description="Vector semantic search",
                ),
                ToolDescriptor(
                    tool_name="search_adapter.search",
                    authority=ToolAuthority.READ,
                    description="Keyword / full-text search",
                ),
                ToolDescriptor(
                    tool_name="crm_writer.propose",
                    authority=ToolAuthority.PROPOSE,
                    description="Propose a CRM write",
                ),
                ToolDescriptor(
                    tool_name="automation_executor.execute",
                    authority=ToolAuthority.EXECUTE,
                    description="Execute an automation workflow",
                ),
                ToolDescriptor(
                    tool_name="kafka.emit_event",
                    authority=ToolAuthority.EXECUTE,
                    description="Emit a Kafka event",
                ),
                ToolDescriptor(
                    tool_name="governance.approve",
                    authority=ToolAuthority.EXECUTE,
                    description="Approve a governance decision",
                ),
            ]
        )


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


class AgentHandler(Protocol):
    async def run(
        self,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentResult: ...


# ---------------------------------------------------------------------------
# Registry Snapshot
# ---------------------------------------------------------------------------


class RegistrySnapshot(StrictContract):
    agents: dict[str, AgentCapability] = Field(default_factory=dict)
    version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Single registry of agent capabilities and handlers.

    Capabilities are frozen (immutable) so resolve_capability() returns the
    same object safely.  Re-registration requires replace() which re-validates.
    """

    def __init__(self, tool_catalog: ToolCatalog | None = None) -> None:
        self._tool_catalog = tool_catalog or ToolCatalog.default_catalog()
        self._agents: dict[str, AgentCapability] = {}
        self._handlers: dict[str, AgentHandler] = {}

    @property
    def tool_catalog(self) -> ToolCatalog:
        return self._tool_catalog

    # -- Registration -------------------------------------------------------

    def register(self, capability: AgentCapability, handler: AgentHandler) -> None:
        agent_id = capability.agent_id
        if agent_id in self._agents:
            raise DuplicateAgentError(
                f"Agent {agent_id!r} is already registered; use replace() to overwrite"
            )
        self._validate_tool_authority(capability)
        # Store a deep copy so external mutation cannot change registry state
        self._agents[agent_id] = self._copy_capability(capability)
        self._handlers[agent_id] = handler

    def replace(self, capability: AgentCapability, handler: AgentHandler) -> None:
        agent_id = capability.agent_id
        if agent_id not in self._agents:
            raise UnknownAgentError(
                f"Agent {agent_id!r} is not registered; use register() for new agents"
            )
        self._validate_tool_authority(capability)
        self._agents[agent_id] = self._copy_capability(capability)
        self._handlers[agent_id] = handler

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
        self._handlers.pop(agent_id, None)

    # -- Copy helper --------------------------------------------------------

    @staticmethod
    def _copy_capability(capability: AgentCapability) -> AgentCapability:
        """Return a deep copy of *capability* via Python-mode round-trip.

        All public read APIs funnel through this helper so callers can never
        obtain a reference to the registry's internal object graph.

        R6 P0-1 — uses ``mode="python"`` instead of ``mode="json"`` so
        that ``frozenset`` fields (``domains`` / ``supported_tasks`` /
        ``allowed_tools``) are preserved as ``frozenset`` and reach the
        Canonicalizer's set/frozenset branch (which sorts) instead of
        being converted to plain lists with process-random iteration
        order.
        """
        return AgentCapability.model_validate(capability.model_dump(mode="python"))

    # -- Resolution ---------------------------------------------------------

    def resolve(self, agent_id: str) -> tuple[AgentCapability, AgentHandler]:
        cap = self._agents.get(agent_id)
        if cap is None:
            raise UnknownAgentError(f"Agent {agent_id!r} is not registered")
        if not cap.enabled:
            raise DisabledAgentError(f"Agent {agent_id!r} is disabled")
        # Return a deep copy so callers cannot mutate registry internals
        return self._copy_capability(cap), self._handlers[agent_id]

    def resolve_capability(self, agent_id: str) -> AgentCapability:
        cap, _ = self.resolve(agent_id)
        # Already a deep copy from resolve()
        return cap

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self._agents

    # -- Validation ---------------------------------------------------------

    def validate_task(self, task: AgentTask) -> AgentCapability:
        cap = self.resolve_capability(task.agent_id)
        if task.task_type not in cap.supported_tasks:
            raise CapabilityValidationError(
                f"Agent {task.agent_id!r} does not support task type {task.task_type!r}"
            )
        if task.timeout_ms > cap.timeout_ms:
            raise CapabilityValidationError(
                f"Task timeout {task.timeout_ms}ms exceeds agent capability {cap.timeout_ms}ms"
            )
        return cap

    def validate_tool_access(self, agent_id: str, tool_name: str) -> ToolDescriptor:
        cap = self.resolve_capability(agent_id)
        tool = self._tool_catalog.resolve(tool_name)

        if tool_name not in cap.allowed_tools:
            raise UnauthorizedToolError(
                f"Agent {agent_id!r} is not allowed to use tool {tool_name!r}"
            )

        if (
            cap.authority == AgentAuthority.READ
            and tool.authority != ToolAuthority.READ
        ):
            raise UnauthorizedToolError(
                f"READ agent {agent_id!r} cannot use {tool.authority.value}-level tool {tool_name!r}"
            )
        if (
            cap.authority == AgentAuthority.PROPOSE
            and tool.authority == ToolAuthority.EXECUTE
        ):
            raise UnauthorizedToolError(
                f"PROPOSE agent {agent_id!r} cannot use execute-level tool {tool_name!r}"
            )
        return tool

    # -- Queries ------------------------------------------------------------

    def list_by_domain(self, domain: str) -> list[AgentCapability]:
        return [
            self._copy_capability(c)
            for c in sorted(
                (c for c in self._agents.values() if c.enabled and domain in c.domains),
                key=lambda c: c.agent_id,
            )
        ]

    def list_by_task(self, task_type: str) -> list[AgentCapability]:
        return [
            self._copy_capability(c)
            for c in sorted(
                (
                    c
                    for c in self._agents.values()
                    if c.enabled and task_type in c.supported_tasks
                ),
                key=lambda c: c.agent_id,
            )
        ]

    def list_all(self) -> list[AgentCapability]:
        return [
            self._copy_capability(c)
            for c in sorted(self._agents.values(), key=lambda c: c.agent_id)
        ]

    # -- Snapshot -----------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        raw: dict[str, Any] = {}
        for agent_id in sorted(self._agents):
            cap = self._agents[agent_id]
            # R6 P0-1 — mode="python" preserves frozenset fields as
            # frozenset so content_hash/raw's canonicalization sorts
            # them.  mode="json" emitted plain lists with process-random
            # iteration order, producing different snapshot.version
            # across PYTHONHASHSEED values.
            raw[agent_id] = cap.model_dump(mode="python")
        version = content_hash(raw)
        # Return copies so mutation doesn't affect registry
        agents_copy = {
            aid: self._copy_capability(cap) for aid, cap in self._agents.items()
        }
        return RegistrySnapshot(
            agents=dict(sorted(agents_copy.items(), key=lambda kv: kv[0])),
            version=version,
            created_at=datetime.now(timezone.utc),
        )

    # -- Internal -----------------------------------------------------------

    def _validate_tool_authority(self, capability: AgentCapability) -> None:
        for tool_name in capability.allowed_tools:
            tool = self._tool_catalog.resolve(tool_name)
            if (
                capability.authority == AgentAuthority.READ
                and tool.authority != ToolAuthority.READ
            ):
                raise UnauthorizedToolError(
                    f"READ agent {capability.agent_id!r} cannot use "
                    f"{tool.authority.value}-level tool {tool_name!r}"
                )
            if (
                capability.authority == AgentAuthority.PROPOSE
                and tool.authority == ToolAuthority.EXECUTE
            ):
                raise UnauthorizedToolError(
                    f"PROPOSE agent {capability.agent_id!r} cannot use "
                    f"execute-level tool {tool_name!r}"
                )
