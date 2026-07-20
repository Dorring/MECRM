"""Multi-agent contract and registry errors.

All errors inherit from :class:`MultiAgentError` so callers can catch a single
base when they need to handle any Phase 2 contract / registry / merge failure.
"""

from __future__ import annotations


class MultiAgentError(Exception):
    """Base for all multi-agent contract, registry, and merge errors."""


class DuplicateAgentError(MultiAgentError):
    """Raised when registering an agent_id that already exists."""


class UnknownAgentError(MultiAgentError):
    """Raised when resolving an agent_id that is not registered."""


class DisabledAgentError(MultiAgentError):
    """Raised when attempting to resolve a disabled agent."""


class UnauthorizedToolError(MultiAgentError):
    """Raised when an agent attempts a tool outside its authority level."""


class ForeignTenantEvidenceError(MultiAgentError):
    """Raised when evidence from a different tenant is submitted."""


class ProposalHashMismatchError(MultiAgentError):
    """Raised when a stored proposal hash does not match recomputed content."""


class CapabilityValidationError(MultiAgentError):
    """Raised when an AgentCapability fails structural validation."""


class MergeConflictError(MultiAgentError):
    """Raised when parallel results cannot be safely merged."""
