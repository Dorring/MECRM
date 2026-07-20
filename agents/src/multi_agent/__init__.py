"""Multi-agent contracts, registry, state merge, and serialization.

This package is the foundation for Phase 3+ (Complexity Gate, Planner,
Supervisor).  It does NOT modify any existing agent, route, or Kafka
consumer — it only provides the data contracts and registry that future
phases will consume.
"""

from multi_agent.contracts import (
    ActionProposal,
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    Evidence,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
    _compute_proposal_hash,
    from_crm_writer_proposal,
    from_productivity_proposal,
)
from multi_agent.errors import (
    CapabilityValidationError,
    DisabledAgentError,
    DuplicateAgentError,
    ForeignTenantEvidenceError,
    MergeConflictError,
    MultiAgentError,
    ProposalHashMismatchError,
    UnauthorizedToolError,
    UnknownAgentError,
)
from multi_agent.registry import AgentHandler, AgentRegistry, RegistrySnapshot
from multi_agent.serialization import (
    deserialize_contract,
    serialize_contract,
    serialize_set_for_json,
    stable_hash,
)
from multi_agent.state import MergeConflict, MergedState, merge_parallel_results

__all__ = [
    # Contracts
    "ActionProposal",
    "AgentAuthority",
    "AgentCapability",
    "AgentError",
    "AgentExecutionContext",
    "AgentResult",
    "AgentTask",
    "Evidence",
    "ProviderMetadata",
    "TokenUsage",
    "ToolAuthority",
    "ToolCallRecord",
    "_compute_proposal_hash",
    "from_crm_writer_proposal",
    "from_productivity_proposal",
    # Errors
    "CapabilityValidationError",
    "DisabledAgentError",
    "DuplicateAgentError",
    "ForeignTenantEvidenceError",
    "MergeConflictError",
    "MultiAgentError",
    "ProposalHashMismatchError",
    "UnauthorizedToolError",
    "UnknownAgentError",
    # Registry
    "AgentHandler",
    "AgentRegistry",
    "RegistrySnapshot",
    # Serialization
    "deserialize_contract",
    "serialize_contract",
    "serialize_set_for_json",
    "stable_hash",
    # State
    "MergeConflict",
    "MergedState",
    "merge_parallel_results",
]
