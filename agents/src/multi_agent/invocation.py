"""Phase 4 Agent Invocation Boundary.

The Scheduler depends on :class:`AgentInvoker`, not on concrete Handler
implementations.  This keeps the Supervisor decoupled from individual
Specialist agents and makes deterministic testing possible.

Two implementations:

* :class:`RegistryAgentInvoker` — resolves the Handler via
  :class:`AgentRegistry` and extracts usage from the returned
  :class:`AgentResult`.
* :class:`DeterministicFakeInvoker` — test double that returns a
  preset receipt or computes one via a callable.

If the existing Handler Protocol signature ever changes, only
:class:`RegistryAgentInvoker` needs an adapter; the Scheduler and
Supervisor stay unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Literal, Protocol

from pydantic import ConfigDict, Field

from multi_agent.contracts import (
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    StrictContract,
)
from multi_agent.execution_errors import InvalidInvocationReceiptError
from multi_agent.registry import AgentHandler, AgentRegistry


# ---------------------------------------------------------------------------
# R3 P0-4 / R4 P0-2: Usage Trust Level + Verification Capabilities
# ---------------------------------------------------------------------------


UsageTrustLevel = Literal[
    # ``verified_provider`` — usage came from the LLM provider's
    # authoritative response (``result.provider_metadata`` is set and
    # ``result.token_usage`` reflects the real billing counter).  This
    # is the only level accepted for token_budget enforcement.
    "verified_provider",
    # ``trusted_adapter`` — usage came from a vetted adapter (e.g. a
    # future cost-reporting middleware that signs its reports).  This
    # is the only level accepted for cost_budget_usd enforcement.
    "trusted_adapter",
    # ``unverified`` — usage is self-reported by the Invoker with no
    # cryptographic or provider-level attestation.  Values of 0 or
    # None are treated as "no usage reported" and fail closed when a
    # budget is configured.
    "unverified",
]


class UsageVerificationCapabilities(StrictContract):
    """R4 P0-2: Immutable description of what an :class:`AgentInvoker`
    can *actually* verify about usage.

    R3's ``TrustedUsageInvoker`` marker Protocol was forgeable — any
    custom Invoker could set ``usage_trust="trusted_adapter"`` on its
    receipts without the Supervisor checking whether the Invoker
    itself was trusted.  R4 replaces the marker with this capability
    contract: the Supervisor reads ``invoker.usage_capabilities``
    (falling back to a fully-unverified default) and cross-checks it
    against every receipt's ``usage_trust``.

    * ``verifies_tokens=True`` — the Invoker's receipts may claim
      ``verified_provider`` or ``trusted_adapter`` for token usage.
    * ``verifies_cost=True`` — the Invoker's receipts may claim
      ``trusted_adapter`` for cost usage.
    * ``source_id`` — stable identifier for diagnostics (e.g.
      ``"registry_agent_invoker"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str


_UNVERIFIED_CAPABILITIES = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="unverified",
)


def get_usage_capabilities(invoker: object) -> UsageVerificationCapabilities:
    """R4 P0-2: extract :class:`UsageVerificationCapabilities` from
    *invoker*, defaulting to fully-unverified when the Invoker does
    not expose the property.

    Using ``getattr`` instead of ``isinstance`` means existing test
    fakes that don't define ``usage_capabilities`` are automatically
    treated as unverified — no Protocol conformance breakage.
    """
    caps = getattr(invoker, "usage_capabilities", None)
    if isinstance(caps, UsageVerificationCapabilities):
        return caps
    return _UNVERIFIED_CAPABILITIES


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class AgentInvocationReceipt(StrictContract):
    """Result of a single Handler invocation plus actual usage.

    ``tool_calls`` is the count of :class:`ToolCallRecord` entries the
    Handler reported.  ``tokens_used`` / ``cost_usd`` are ``None`` when
    the Handler did not report real usage (e.g. deterministic mode) —
    Phase 4 treats a configured budget with ``None`` usage as
    fail-closed rather than substituting a Phase 3 estimate.

    R3 P0-4: ``usage_trust`` declares the provenance of the reported
    usage.  When a budget (``token_budget`` or ``cost_budget_usd``) is
    configured, the Supervisor only accepts ``verified_provider`` or
    ``trusted_adapter`` receipts; an ``unverified`` receipt with zero
    or None usage fails closed with
    :class:`ExecutionUsageUnavailableError`.  This prevents a custom
    Invoker from under-reporting usage (e.g. ``cost_usd=Decimal("0")``)
    to bypass budget enforcement.
    """

    result: AgentResult
    tool_calls: int = Field(default=0, ge=0)
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    usage_trust: UsageTrustLevel = Field(default="unverified")


# ---------------------------------------------------------------------------
# Invoker Protocol
# ---------------------------------------------------------------------------


class AgentInvoker(Protocol):
    """Invocation boundary between the Scheduler and a concrete Handler.

    ``invoke`` must:

    * call the Handler exactly once;
    * return an :class:`AgentInvocationReceipt` with the *actual* usage
      reported by the Handler (never a Phase 3 estimate);
    * raise :class:`multi_agent.execution_errors.RetryableAgentError`
      for transient failures so the Retry loop can react.
    """

    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt: ...


class TrustedUsageInvoker(AgentInvoker, Protocol):
    """R3 P0-4 / R4 P0-2: marker Protocol for Invokers that report
    *verified* usage.

    R4 P0-2: the Supervisor no longer relies solely on this marker.
    It reads ``invoker.usage_capabilities`` (a
    :class:`UsageVerificationCapabilities` contract) and cross-checks
    every receipt's ``usage_trust`` against the Invoker's actual
    verification capabilities.  This Protocol is kept for documentation
    and for tests that want to assert an Invoker is trusted, but the
    runtime enforcement uses :func:`get_usage_capabilities`.

    Concrete implementations must expose ``usage_capabilities`` with
    ``verifies_tokens=True`` and/or ``verifies_cost=True`` and ensure
    every receipt they produce carries ``usage_trust="verified_provider"``
    or ``usage_trust="trusted_adapter"``.
    """

    usage_capabilities: UsageVerificationCapabilities


# ---------------------------------------------------------------------------
# RegistryAgentInvoker
# ---------------------------------------------------------------------------


class RegistryAgentInvoker:
    """Default invoker: calls ``handler.run(task, context)`` and
    extracts usage from the returned :class:`AgentResult`.

    Usage extraction rules:

    * ``tool_calls`` = ``len(result.tool_calls)`` (always reported).
    * ``tokens_used`` = ``result.token_usage.total_tokens`` when the
      Handler set ``provider_metadata`` (live mode); ``None`` otherwise
      (deterministic mode did not talk to a real provider).
    * ``cost_usd`` is always ``None`` — :class:`AgentResult` does not
      carry a cost field.  A configured ``cost_budget_usd`` therefore
      fails closed unless a future ``AgentResult`` extension reports
      cost.

    R3 P0-4: ``usage_trust`` is set to ``verified_provider`` when
    ``result.provider_metadata`` is present (the LLM provider
    attested the token usage); ``unverified`` otherwise.  Cost is
    always ``unverified`` because :class:`AgentResult` has no cost
    field — a configured ``cost_budget_usd`` fails closed unless the
    caller uses a :class:`TrustedUsageInvoker` that reports
    ``cost_usd`` with ``usage_trust="trusted_adapter"``.
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        """R4 P0-2: RegistryAgentInvoker can verify tokens (extracted
        from ``result.provider_metadata``) but NOT cost (AgentResult
        has no cost field)."""
        return UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=False,
            source_id="registry_agent_invoker",
        )

    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:
        result = await handler.run(task, context)
        if result.provider_metadata is not None:
            tokens_used: int | None = result.token_usage.total_tokens
            # Provider attested token usage — verified.
            usage_trust: UsageTrustLevel = "verified_provider"
        else:
            tokens_used = None
            usage_trust = "unverified"
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            tokens_used=tokens_used,
            cost_usd=None,
            usage_trust=usage_trust,
        )


# ---------------------------------------------------------------------------
# DeterministicFakeInvoker
# ---------------------------------------------------------------------------


ReceiptFactory = Callable[[AgentTask, AgentExecutionContext], AgentInvocationReceipt]


class DeterministicFakeInvoker:
    """Test double that returns a preset receipt.

    Three construction modes:

    1. ``DeterministicFakeInvoker(receipt=receipt)`` — every call
       returns the same receipt (after re-binding ``task_id`` /
       ``agent_id`` / ``tenant_id`` to the actual task).
    2. ``DeterministicFakeInvoker(factory=fn)`` — each call dispatches
       to ``fn(task, context)`` which returns a fresh receipt.
    3. ``DeterministicFakeInvoker(result=result)`` — convenience: wrap
       an :class:`AgentResult` into a receipt with zeroed usage.

    The invoker records every call in ``invocations`` so tests can
    assert ordering, attempt counts, and concurrency.
    """

    def __init__(
        self,
        *,
        receipt: AgentInvocationReceipt | None = None,
        factory: ReceiptFactory | None = None,
        result: AgentResult | None = None,
        usage_capabilities: UsageVerificationCapabilities | None = None,
    ) -> None:
        provided = sum(1 for x in (receipt, factory, result) if x is not None)
        if provided != 1:
            raise ValueError(
                "DeterministicFakeInvoker requires exactly one of "
                "receipt / factory / result"
            )
        self._receipt = receipt
        self._factory = factory
        self._result = result
        self._usage_capabilities = usage_capabilities or UsageVerificationCapabilities(
            verifies_tokens=False,
            verifies_cost=False,
            source_id="deterministic_fake_invoker",
        )
        self.invocations: list[tuple[AgentTask, AgentExecutionContext]] = []

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        return self._usage_capabilities

    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:
        # ``handler`` is ignored — the fake owns its own result.
        self.invocations.append((task, context))
        if self._factory is not None:
            return self._factory(task, context)
        if self._receipt is not None:
            return self._receipt
        assert self._result is not None
        return AgentInvocationReceipt(
            result=self._result,
            tool_calls=len(self._result.tool_calls),
            tokens_used=None,
            cost_usd=None,
        )


# ---------------------------------------------------------------------------
# R1 P0-4: Receipt consistency validation
# ---------------------------------------------------------------------------


def validate_invocation_receipt(receipt: AgentInvocationReceipt) -> None:
    """Validate that *receipt* is internally consistent.

    Phase 4 budget enforcement trusts ``receipt.tool_calls`` and
    (when configured) ``receipt.tokens_used`` to charge against the
    Run budget.  A custom AgentInvoker that under-reports usage would
    silently bypass ``max_tool_calls`` / ``token_budget``.

    Checks:

    * ``receipt.tool_calls == len(receipt.result.tool_calls)`` — the
      receipt must report exactly the number of :class:`ToolCallRecord`
      entries the Handler returned.
    * When ``receipt.result.provider_metadata is not None`` and the
      receipt reports ``tokens_used`` — the value must equal
      ``receipt.result.token_usage.total_tokens``.  This prevents a
      custom Invoker from under-reporting tokens while the Result
      carries authoritative provider usage.

    R3 P0-4: ``usage_trust`` provenance is validated against the
    receipt contents:

    * ``verified_provider`` requires ``result.provider_metadata`` to
      be present (the LLM provider attested the usage).
    * ``unverified`` is always allowed structurally, but the
      :class:`_BudgetAccountant` rejects it for budget enforcement.

    Cost is intentionally **not** validated here because
    :class:`AgentResult` does not carry a cost field; cost trust is
    solely a property of the chosen Invoker (RegistryAgentInvoker
    reports ``None``, future trusted Invokers may report real cost).

    Raises :class:`InvalidInvocationReceiptError` on any mismatch.
    """
    result = receipt.result
    actual_tool_calls = len(result.tool_calls)
    if receipt.tool_calls != actual_tool_calls:
        raise InvalidInvocationReceiptError(
            f"receipt.tool_calls={receipt.tool_calls} does not match "
            f"len(result.tool_calls)={actual_tool_calls}"
        )

    if result.provider_metadata is not None and receipt.tokens_used is not None:
        actual_tokens = result.token_usage.total_tokens
        if receipt.tokens_used != actual_tokens:
            raise InvalidInvocationReceiptError(
                f"receipt.tokens_used={receipt.tokens_used} does not "
                f"match result.token_usage.total_tokens={actual_tokens} "
                f"(provider_metadata is present, so token usage is "
                f"authoritative)"
            )

    # R3 P0-4: ``verified_provider`` provenance requires the provider
    # metadata to actually be present.  A receipt that claims
    # ``verified_provider`` without ``provider_metadata`` is lying
    # about its provenance.
    if receipt.usage_trust == "verified_provider" and result.provider_metadata is None:
        raise InvalidInvocationReceiptError(
            "receipt.usage_trust='verified_provider' but "
            "result.provider_metadata is None — provider attestation "
            "is required for verified_provider provenance"
        )


__all__ = [
    "AgentInvocationReceipt",
    "AgentInvoker",
    "DeterministicFakeInvoker",
    "RegistryAgentInvoker",
    "TrustedUsageInvoker",
    "UsageTrustLevel",
    "UsageVerificationCapabilities",
    "get_usage_capabilities",
    "validate_invocation_receipt",
]
