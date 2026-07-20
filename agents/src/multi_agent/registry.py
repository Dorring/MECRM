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
)
from multi_agent.errors import (
    DisabledAgentError,
    DuplicateAgentError,
    UnauthorizedToolError,
    UnknownAgentError,
    UnknownToolError,
)
from multi_agent.serialization import content_hash

# ---------------------------------------------------------------------------
# Tool Catalog
# ---------------------------------------------------------------------------


class ToolDescriptor(StrictContract):
    """Describes a single tool's authority, contract, and metadata."""

    tool_name: str
    authority: ToolAuthority
    description: str = ""
    input_contract: str = ""
    output_contract: str = ""


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
            raise DuplicateAgentError(f"Tool {tool.tool_name!r} is already registered")
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
    def default_catalog(cls) -> "ToolCatalog":
        """Factory for the built-in Phase 2 tool catalog."""
        return cls(
            tools=[
                # Read tools
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
                # Propose tools
                ToolDescriptor(
                    tool_name="crm_writer.propose",
                    authority=ToolAuthority.PROPOSE,
                    description="Propose a CRM write",
                ),
                # Execute tools
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
    agents: dict[str, AgentCapability] = {}
    version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Single registry of agent capabilities and handlers."""

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
        self._agents[agent_id] = capability
        self._handlers[agent_id] = handler

    def replace(self, capability: AgentCapability, handler: AgentHandler) -> None:
        agent_id = capability.agent_id
        if agent_id not in self._agents:
            raise UnknownAgentError(
                f"Agent {agent_id!r} is not registered; use register() for new agents"
            )
        self._validate_tool_authority(capability)
        self._agents[agent_id] = capability
        self._handlers[agent_id] = handler

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
        self._handlers.pop(agent_id, None)

    # -- Resolution ---------------------------------------------------------

    def resolve(self, agent_id: str) -> tuple[AgentCapability, AgentHandler]:
        cap = self._agents.get(agent_id)
        if cap is None:
            raise UnknownAgentError(f"Agent {agent_id!r} is not registered")
        if not cap.enabled:
            raise DisabledAgentError(f"Agent {agent_id!r} is disabled")
        return cap, self._handlers[agent_id]

    def resolve_capability(self, agent_id: str) -> AgentCapability:
        cap, _ = self.resolve(agent_id)
        return cap

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self._agents

    # -- Validation ---------------------------------------------------------

    def validate_task(self, task: AgentTask) -> AgentCapability:
        """Validate that *task* can be dispatched to its target agent.

        Returns the resolved AgentCapability on success.
        """
        cap = self.resolve_capability(task.agent_id)
        if task.task_type not in cap.supported_tasks:
            raise UnauthorizedToolError(
                f"Agent {task.agent_id!r} does not support task type {task.task_type!r}"
            )
        if task.timeout_ms > cap.timeout_ms:
            raise UnauthorizedToolError(
                f"Task timeout {task.timeout_ms}ms exceeds agent capability {cap.timeout_ms}ms"
            )
        # Authority check: a task requiring execute must go to an execute agent.
        # We infer the required authority from task_type naming convention for now;
        # Phase 3+ can formalize this.
        return cap

    def validate_tool_access(
        self,
        agent_id: str,
        tool_name: str,
    ) -> ToolDescriptor:
        """Validate that *agent_id* is authorised to use *tool_name*.

        Returns the ToolDescriptor on success.
        """
        cap = self.resolve_capability(agent_id)
        tool = self._tool_catalog.resolve(tool_name)

        if tool_name not in cap.allowed_tools:
            raise UnauthorizedToolError(
                f"Agent {agent_id!r} is not allowed to use tool {tool_name!r}"
            )

        # Authority hierarchy check
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

    # -- Queries (sorted by agent_id for determinism) -----------------------

    def list_by_domain(self, domain: str) -> list[AgentCapability]:
        return sorted(
            (c for c in self._agents.values() if c.enabled and domain in c.domains),
            key=lambda c: c.agent_id,
        )

    def list_by_task(self, task_type: str) -> list[AgentCapability]:
        return sorted(
            (
                c
                for c in self._agents.values()
                if c.enabled and task_type in c.supported_tasks
            ),
            key=lambda c: c.agent_id,
        )

    def list_all(self) -> list[AgentCapability]:
        return sorted(self._agents.values(), key=lambda c: c.agent_id)

    # -- Snapshot -----------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        raw: dict[str, Any] = {}
        for agent_id in sorted(self._agents):
            cap = self._agents[agent_id]
            raw[agent_id] = cap.model_dump(mode="json")
        version = content_hash(raw)
        return RegistrySnapshot(
            agents=dict(sorted(self._agents.items(), key=lambda kv: kv[0])),
            version=version,
            created_at=datetime.now(timezone.utc),
        )

    # -- Internal -----------------------------------------------------------

    def _validate_tool_authority(self, capability: AgentCapability) -> None:
        for tool_name in capability.allowed_tools:
            tool = self._tool_catalog.resolve(tool_name)  # raises UnknownToolError
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
