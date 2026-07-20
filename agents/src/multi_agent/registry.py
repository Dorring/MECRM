"""Single AgentRegistry for the MECRM multi-agent system.

There must be exactly ONE registry per process.  The registry owns the
mapping from ``agent_id`` → (``AgentCapability``, ``AgentHandler``) and
enforces all Phase 2 authority / tool rules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, Field

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentResult,
    AgentTask,
    AgentExecutionContext,
    ToolAuthority,
)
from multi_agent.errors import (
    DisabledAgentError,
    DuplicateAgentError,
    UnauthorizedToolError,
    UnknownAgentError,
)

# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


class AgentHandler(Protocol):
    """A callable that executes an AgentTask and returns an AgentResult.

    In Phase 2 the handler is a test Fake; real agents are wired in Phase 5+.
    """

    async def run(
        self,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentResult: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class RegistrySnapshot(BaseModel):
    """A point-in-time snapshot of the registry state.

    Contains agent capabilities only — NO handler references, NO Python
    memory addresses, NO sensitive config.
    """

    agents: dict[str, AgentCapability] = Field(default_factory=dict)
    version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentRegistry:
    """The single registry of agent capabilities and handlers.

    Rules (all enforced):
      - Agent IDs are unique; duplicate register → DuplicateAgentError
      - Replace overwrites; must be explicit
      - Disabled agents are excluded from resolve() but visible in snapshot()
      - resolve() validates tool authority against AgentAuthority
      - No LLM-driven registration
      - No dynamic code loading
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentCapability] = {}
        self._handlers: dict[str, AgentHandler] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self, capability: AgentCapability, handler: AgentHandler
    ) -> None:
        """Register a new agent.  Raises DuplicateAgentError if *capability.agent_id*
        is already known — use ``replace()`` for explicit overwrites.
        """
        agent_id = capability.agent_id
        if agent_id in self._agents:
            raise DuplicateAgentError(
                f"Agent {agent_id!r} is already registered; use replace() to overwrite"
            )
        self._validate_tool_authority(capability)
        self._agents[agent_id] = capability
        self._handlers[agent_id] = handler

    def replace(
        self, capability: AgentCapability, handler: AgentHandler
    ) -> None:
        """Replace an existing agent's capability and handler.

        No-op if the agent_id is unknown (matches explicit-replace semantics:
        replacing something that does not exist yet is a caller bug, but for
        idempotency we allow it without raising).
        """
        agent_id = capability.agent_id
        self._validate_tool_authority(capability)
        self._agents[agent_id] = capability
        self._handlers[agent_id] = handler

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry.  Idempotent."""
        self._agents.pop(agent_id, None)
        self._handlers.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, agent_id: str) -> tuple[AgentCapability, AgentHandler]:
        """Return the capability and handler for *agent_id*.

        Raises:
            UnknownAgentError: agent_id is not registered.
            DisabledAgentError: agent is registered but disabled.
        """
        capability = self._agents.get(agent_id)
        if capability is None:
            raise UnknownAgentError(
                f"Agent {agent_id!r} is not registered"
            )
        if not capability.enabled:
            raise DisabledAgentError(
                f"Agent {agent_id!r} is disabled"
            )
        handler = self._handlers[agent_id]
        return capability, handler

    def resolve_capability(self, agent_id: str) -> AgentCapability:
        """Return only the capability (no handler).  Same rules as ``resolve()``."""
        capability = self._agents.get(agent_id)
        if capability is None:
            raise UnknownAgentError(
                f"Agent {agent_id!r} is not registered"
            )
        if not capability.enabled:
            raise DisabledAgentError(
                f"Agent {agent_id!r} is disabled"
            )
        return capability

    def is_registered(self, agent_id: str) -> bool:
        """Return True if *agent_id* is known (regardless of enabled state)."""
        return agent_id in self._agents

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_by_domain(self, domain: str) -> list[AgentCapability]:
        """Return enabled agents whose ``domains`` include *domain*."""
        return [
            c
            for c in self._agents.values()
            if c.enabled and domain in c.domains
        ]

    def list_by_task(self, task_type: str) -> list[AgentCapability]:
        """Return enabled agents whose ``supported_tasks`` include *task_type*."""
        return [
            c
            for c in self._agents.values()
            if c.enabled and task_type in c.supported_tasks
        ]

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        """Return a deterministic, JSON-safe snapshot of the registry.

        The snapshot includes ALL agents (including disabled) and a version
        hash computed from sorted capability data.  No handler references,
        memory addresses, or secrets are included.
        """
        raw: dict[str, Any] = {}
        for agent_id in sorted(self._agents):
            cap = self._agents[agent_id]
            raw[agent_id] = json.loads(
                cap.model_dump_json(exclude_defaults=False)
            )

        canonical = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        return RegistrySnapshot(
            agents=dict(sorted(self._agents.items(), key=lambda kv: kv[0])),
            version=version,
            created_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_authorities() -> dict[str, ToolAuthority]:
        """Return the built-in tool→authority mapping (delegates to contracts)."""
        return AgentCapability._tool_authorities()

    @classmethod
    def _validate_tool_authority(cls, capability: AgentCapability) -> None:
        """Raise UnauthorizedToolError if any allowed_tool exceeds the agent's
        authority level.
        """
        tool_map = cls._tool_authorities()
        for tool in capability.allowed_tools:
            tool_auth = tool_map.get(tool)
            if tool_auth is None:
                # Unknown tool — allow registration but warn in telemetry.
                # Phase 3+ can add strict mode.
                continue

            if capability.authority == AgentAuthority.READ:
                if tool_auth != ToolAuthority.READ:
                    raise UnauthorizedToolError(
                        f"READ agent {capability.agent_id!r} cannot use "
                        f"{tool_auth.value}-level tool {tool!r}"
                    )
            elif capability.authority == AgentAuthority.PROPOSE:
                if tool_auth == ToolAuthority.EXECUTE:
                    raise UnauthorizedToolError(
                        f"PROPOSE agent {capability.agent_id!r} cannot use "
                        f"execute-level tool {tool!r}"
                    )
            # EXECUTE agents can use any tool level
