"""Multi-agent contract, registry, and merge errors."""

from __future__ import annotations


class MultiAgentError(Exception):
    """Base for all multi-agent errors."""


# Registry ---------------------------------------------------------------


class DuplicateAgentError(MultiAgentError):
    """Registering an agent_id that already exists."""


class DuplicateToolError(MultiAgentError):
    """Registering a tool_name that already exists in the ToolCatalog."""


class UnknownAgentError(MultiAgentError):
    """Resolving an agent_id that is not registered."""


class UnknownToolError(MultiAgentError):
    """A tool name is not in the ToolCatalog."""


class DisabledAgentError(MultiAgentError):
    """Resolving a disabled agent."""


class UnauthorizedToolError(MultiAgentError):
    """Agent attempts a tool outside its authority or allowed_tools."""


class CapabilityValidationError(MultiAgentError):
    """Task type or timeout exceeds agent capability."""


# Contracts --------------------------------------------------------------


class ProposalHashMismatchError(MultiAgentError):
    """Stored proposal_hash does not match recomputed content."""


class ForeignTenantEvidenceError(MultiAgentError):
    """Evidence from a foreign tenant was submitted."""


# Merge -----------------------------------------------------------------


class MergeConflictError(MultiAgentError):
    """Parallel results cannot be safely merged."""


# Budget ----------------------------------------------------------------


class BudgetExceededError(MultiAgentError):
    """Execution budget exceeded."""


# Serialization ----------------------------------------------------------


class SerializationError(MultiAgentError):
    """Contract serialization / deserialization failure."""
