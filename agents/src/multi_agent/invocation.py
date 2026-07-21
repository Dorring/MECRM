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
from enum import StrEnum
from typing import Any, Callable, Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from multi_agent.contracts import (
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ProviderMetadata,
    StrictContract,
    TokenUsage,
)
from multi_agent.execution_errors import (
    InvalidInvocationReceiptError,
    NonRetryableAgentError,
)
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


# ---------------------------------------------------------------------------
# R6 P0-4: Per-dimension Usage Provenance — replaces the single
# ``usage_trust`` field so Token and Cost trust are expressed
# independently.  A receipt can now carry verified tokens without
# verified cost (and vice versa), which the old single-string
# ``usage_trust`` could not express.
# ---------------------------------------------------------------------------


class UsageProvenance(StrictContract):
    """R6 P0-4: Per-dimension usage provenance for a receipt.

    Replaces the single ``usage_trust: UsageTrustLevel`` field that
    conflated Token and Cost trust into one string.  With
    :class:`UsageProvenance`, a receipt can declare:

    * ``tokens_verified=True`` — the token usage was attested by an
      authoritative :class:`ProviderUsageVerifier` (or a trusted
      adapter).  The value in ``receipt.tokens_used`` may be trusted
      for ``token_budget`` enforcement.
    * ``cost_verified=True`` — the cost usage was attested by an
      authoritative verifier.  The value in ``receipt.cost_usd`` may
      be trusted for ``cost_budget_usd`` enforcement.

    The two flags are independent: a verifier that only checks tokens
    sets ``tokens_verified=True, cost_verified=False``, and the
    accountant will record tokens but NOT cost (nor enforce
    ``cost_budget_usd``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = "unverified"
    tokens_verified: bool = False
    cost_verified: bool = False


# ---------------------------------------------------------------------------
# R7 P0-1: AttemptUsageDisposition — explicit, trusted declaration of
# what a committed agent call produced.  Replaces the R6 heuristic that
# inferred "no provider call" from ``provider_metadata is None`` (which
# a Handler could lie about by simply omitting the field).
# ---------------------------------------------------------------------------


class AttemptUsageDisposition(StrEnum):
    """R7 P0-1: Per-dimension usage disposition for a committed attempt.

    * ``VERIFIED`` — the dimension's usage was attested by an
      authoritative :class:`ProviderUsageVerifier` (or a trusted
      adapter).  The value in the receipt may be trusted for budget
      enforcement.
    * ``NO_PROVIDER_CALL`` — the invoker authoritatively attests that
      NO provider call was made for this attempt (deterministic mode).
      This disposition can ONLY be produced by an invoker with
      ``can_attest_no_provider_call=True`` — it cannot be inferred
      from ``provider_metadata is None``.
    * ``UNAVAILABLE`` — the dimension's usage is unknown.  This
      covers: (a) no receipt at all (timeout / exception), (b) invalid
      receipt, (c) ``provider_metadata`` absent and the invoker cannot
      attest no provider call (default ``RegistryAgentInvoker``), (d)
      ``provider_metadata`` present but the dimension is not verified
      and a budget is configured.
    """

    VERIFIED = "verified"
    NO_PROVIDER_CALL = "no_provider_call"
    UNAVAILABLE = "unavailable"


class AttemptUsageRecord(StrictContract):
    """R7 P0-3: Per-attempt usage record for independent Token/Cost
    coverage tracking.

    Each committed agent call produces exactly one
    :class:`AttemptUsageRecord`.  Token and Cost dispositions are
    independent — a single attempt can be ``VERIFIED`` for tokens but
    ``UNAVAILABLE`` for cost (or any other combination).

    The ``_BudgetAccountant`` maintains a list of these records and
    computes per-dimension coverage from them:

    * ``token_usage_applicable_attempts`` = count where
      ``token_disposition != NO_PROVIDER_CALL``
    * ``cost_usage_applicable_attempts`` = count where
      ``cost_disposition != NO_PROVIDER_CALL``
    * ``verified_token_attempts`` = count where
      ``token_disposition == VERIFIED``
    * ``verified_cost_attempts`` = count where
      ``cost_disposition == VERIFIED``
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    attempt: int
    token_disposition: AttemptUsageDisposition
    cost_disposition: AttemptUsageDisposition
    tokens_used: int | None = None
    cost_usd: Decimal | None = None
    source_id: str | None = None


# Backwards-compatible mapping: old ``usage_trust`` → ``UsageProvenance``.
_TRUST_TO_PROVENANCE: dict[str, UsageProvenance] = {
    "verified_provider": UsageProvenance(
        source_id="verified_provider",
        tokens_verified=True,
        cost_verified=False,
    ),
    # R6 P0-4: ``trusted_adapter`` is primarily about COST trust —
    # a vetted adapter (e.g. a local billing system) signs its cost
    # reports.  It does NOT automatically elevate TOKEN trust: tokens
    # are attested by the LLM provider (``verified_provider``) or by
    # an explicit :class:`ProviderUsageVerifier`.  Setting
    # ``tokens_verified=False`` here means a legacy ``trusted_adapter``
    # receipt without ``provider_metadata`` no longer triggers the
    # ``tokens_verified=True requires provider_metadata`` check, and
    # the accountant will record cost but not tokens.
    "trusted_adapter": UsageProvenance(
        source_id="trusted_adapter",
        tokens_verified=False,
        cost_verified=True,
    ),
    "unverified": UsageProvenance(
        source_id="unverified",
        tokens_verified=False,
        cost_verified=False,
    ),
}


def _provenance_to_trust(prov: UsageProvenance) -> UsageTrustLevel:
    """Derive the legacy ``usage_trust`` string from provenance.

    R6 P0-4: ``trusted_adapter`` now means "cost verified" (with or
    without token verification).  A receipt with both tokens and cost
    verified also maps to ``trusted_adapter`` for backwards
    compatibility.
    """
    if prov.cost_verified:
        return "trusted_adapter"
    if prov.tokens_verified:
        return "verified_provider"
    return "unverified"


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
    * ``can_attest_no_provider_call`` — R7 P0-1: the Invoker can
      authoritatively attest that NO provider call was made for an
      attempt.  Only trusted deterministic invokers or runtime-mode
      adapters should set this to ``True``.  The default
      :class:`RegistryAgentInvoker` sets it to ``False`` because a
      real Handler can lie by omitting ``provider_metadata``.
    * ``bound_source_ids`` — R7 P0-6: the set of Verifier/Adapter
      source identities that this Invoker's receipts may reference in
      ``usage_provenance.source_id``.  When non-empty, the accountant
      rejects receipts whose ``source_id`` is not in the set.  This
      prevents a receipt from claiming verification by an unbound
      verifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str
    can_attest_no_provider_call: bool = False
    bound_source_ids: frozenset[str] = Field(default_factory=frozenset)


_UNVERIFIED_CAPABILITIES = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="unverified",
    can_attest_no_provider_call=False,
    bound_source_ids=frozenset(),
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

    R6 P0-4: ``usage_provenance`` replaces the single ``usage_trust``
    string so Token and Cost trust are expressed independently.  The
    legacy ``usage_trust`` field is retained for backwards
    compatibility — it is auto-derived from ``usage_provenance`` and
    should not be set directly in new code.

    When a budget (``token_budget`` or ``cost_budget_usd``) is
    configured, the Supervisor only accepts receipts where the
    corresponding provenance flag is ``True``; an unverified receipt
    with zero or None usage fails closed with
    :class:`ExecutionUsageUnavailableError`.
    """

    result: AgentResult
    tool_calls: int = Field(default=0, ge=0)
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    # R6 P0-4: authoritative per-dimension provenance.
    usage_provenance: UsageProvenance = Field(default_factory=UsageProvenance)
    # R7 P1-1: DEPRECATED — retained for backwards compatibility.
    # Auto-derived from ``usage_provenance`` via
    # :func:`_provenance_to_trust`.  New code must set
    # ``usage_provenance`` directly and must NOT pass both fields
    # simultaneously (the ``_sync_trust_provenance`` validator lets
    # ``usage_provenance`` win when both are provided, but mixing the
    # two APIs is error-prone and will be removed in the next
    # incompatible version).  The runtime (Supervisor /
    # ``_BudgetAccountant``) only reads ``usage_provenance`` — this
    # field exists solely so legacy receipts constructed with
    # ``usage_trust=...`` continue to work.
    usage_trust: UsageTrustLevel = Field(default="unverified")

    @model_validator(mode="before")
    @classmethod
    def _sync_trust_provenance(cls, data: Any) -> Any:
        """R6 P0-4: ensure ``usage_trust`` and ``usage_provenance`` are
        consistent.  If only ``usage_trust`` is provided (legacy code),
        derive ``usage_provenance`` from it.  If both are provided,
        ``usage_provenance`` wins.  If only ``usage_provenance`` is
        provided, ``usage_trust`` is derived from it.
        """
        if not isinstance(data, dict):
            return data
        prov = data.get("usage_provenance")
        trust = data.get("usage_trust")
        if prov is not None and trust is None:
            # New code: derive trust from provenance.
            if isinstance(prov, UsageProvenance):
                data = dict(data)
                data["usage_trust"] = _provenance_to_trust(prov)
            elif isinstance(prov, dict):
                prov_obj = UsageProvenance(**prov)
                data = dict(data)
                data["usage_trust"] = _provenance_to_trust(prov_obj)
        elif prov is None and trust is not None:
            # Legacy code: derive provenance from trust.
            data = dict(data)
            data["usage_provenance"] = _TRUST_TO_PROVENANCE.get(
                trust, UsageProvenance(source_id=str(trust))
            )
        elif prov is not None and trust is not None:
            # Both provided: provenance wins, override trust.
            if isinstance(prov, UsageProvenance):
                data = dict(data)
                data["usage_trust"] = _provenance_to_trust(prov)
            elif isinstance(prov, dict):
                prov_obj = UsageProvenance(**prov)
                data = dict(data)
                data["usage_trust"] = _provenance_to_trust(prov_obj)
        return data


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


# ---------------------------------------------------------------------------
# R5 P0-5: Provider Usage Verifier
# ---------------------------------------------------------------------------


class VerifiedUsage(StrictContract):
    """R5 P0-5: Result of a Provider Usage Verifier check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_used: int = Field(default=0, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    verified: bool = False


class ProviderUsageVerifier(Protocol):
    """R5 P0-5: Authoritative Provider Usage verification boundary.

    A Provider Usage Verifier is an external adapter that can
    cryptographically or operationally verify that the token/cost
    usage reported by a Handler's ``provider_metadata`` matches
    the actual Provider billing record.  This cannot be self-attested
    by the Handler.

    R7 P0-5: ``verify()`` is now ``async`` so it can be awaited inside
    the event loop, bounded by ``asyncio.wait_for`` (task timeout +
    run deadline), and cancelled without blocking the event loop.  A
    synchronous verifier that blocks the thread would prevent the
    scheduler from cancelling sibling tasks, respecting the run
    deadline, or responding to cancellation — all of which are
    critical safety properties.  Synchronous verifier implementations
    must be wrapped in an async adapter (e.g. via
    ``asyncio.to_thread``) with explicit thread-lifecycle management.
    """

    source_id: str

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage: ...


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
    field — a configured ``cost_budget_usd`` fails closed unless a
    future trusted Invoker reports ``cost_usd`` with
    ``usage_trust="trusted_adapter"``.

    R5 P0-5: by default (no ``usage_verifier``), ``usage_trust`` is
    always ``unverified`` — the Handler's ``provider_metadata`` is
    self-attested and cannot be trusted.  When an authoritative
    :class:`ProviderUsageVerifier` is configured, ``usage_trust`` is
    ``verified_provider`` when ``result.provider_metadata`` is present.
    Cost verification also requires the verifier; without it,
    ``cost_budget_usd`` fails closed.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        usage_verifier: ProviderUsageVerifier | None = None,
    ) -> None:
        self._registry = registry
        self._usage_verifier = usage_verifier

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def usage_capabilities(self) -> UsageVerificationCapabilities:
        """R5 P0-5 + R7 P0-1/P0-6: by default (no ``usage_verifier``)
        the Invoker cannot verify tokens or cost — the Handler's
        ``provider_metadata`` is self-attested.  When a
        :class:`ProviderUsageVerifier` is configured, both tokens and
        cost are verifiable.

        R7 P0-1: ``can_attest_no_provider_call`` is always ``False``
        for :class:`RegistryAgentInvoker` — a real Handler can lie by
        omitting ``provider_metadata``, so the Invoker cannot
        authoritatively attest that no provider call was made.  Only
        trusted deterministic invokers (e.g.
        :class:`DeterministicFakeInvoker`) set this to ``True``.

        R7 P0-6: ``bound_source_ids`` contains the verifier's
        ``source_id`` when a verifier is configured, so the accountant
        can reject receipts that claim verification by an unbound
        verifier.
        """
        if self._usage_verifier is None:
            return UsageVerificationCapabilities(
                verifies_tokens=False,
                verifies_cost=False,
                source_id="registry_agent_invoker",
                can_attest_no_provider_call=False,
                bound_source_ids=frozenset(),
            )
        return UsageVerificationCapabilities(
            verifies_tokens=True,
            verifies_cost=True,
            source_id="registry_agent_invoker+provider_verifier",
            can_attest_no_provider_call=False,
            bound_source_ids=frozenset({self._usage_verifier.source_id}),
        )

    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:
        result = await handler.run(task, context)

        # R6 P0-1 + R7 P0-5: Actually call the ProviderUsageVerifier
        # when one is configured and the Handler returned
        # provider_metadata.  The verifier's result — not the
        # verifier's mere existence — determines whether usage is
        # trusted.  R7 P0-5: the verifier is now async and is awaited
        # directly (not called synchronously), so it can be bounded by
        # the outer ``asyncio.wait_for`` and cancelled without blocking
        # the event loop.
        if self._usage_verifier is not None and result.provider_metadata is not None:
            try:
                verified = await self._usage_verifier.verify(
                    provider_metadata=result.provider_metadata,
                    token_usage=result.token_usage,
                )
            except Exception as exc:
                # R6 P0-1: Verifier raised — fail closed.  The Handler's
                # self-reported usage must NOT be trusted when the
                # authoritative verifier could not confirm it.
                raise NonRetryableAgentError(
                    f"ProviderUsageVerifier ({self._usage_verifier.source_id}) "
                    f"raised {type(exc).__name__}: {exc}"
                ) from exc

            if not verified.verified:
                # R6 P0-1: Verifier rejected the usage — fail closed.
                raise NonRetryableAgentError(
                    f"ProviderUsageVerifier ({self._usage_verifier.source_id}) "
                    f"returned verified=False — handler self-reported "
                    f"usage is not trusted"
                )

            # R6 P0-1 + P0-4: Use the verifier's authoritative values,
            # not the Handler's self-reported ones.  Cost is only
            # verified when the verifier returned a non-None cost_usd.
            cost_verified = verified.cost_usd is not None
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=verified.tokens_used,
                cost_usd=verified.cost_usd,
                usage_provenance=UsageProvenance(
                    source_id=self._usage_verifier.source_id,
                    tokens_verified=True,
                    cost_verified=cost_verified,
                ),
            )

        # No verifier or no provider_metadata → unverified.
        # R7 P0-1: When ``provider_metadata`` is absent, the receipt
        # carries ``tokens_used=None`` and unverified provenance.  The
        # accountant determines the disposition: if the Invoker has
        # ``can_attest_no_provider_call=True`` → NO_PROVIDER_CALL;
        # otherwise → UNAVAILABLE (fail-closed when a budget is
        # configured).  :class:`RegistryAgentInvoker` always has
        # ``can_attest_no_provider_call=False``, so a Handler that
        # omits ``provider_metadata`` triggers fail-closed when a
        # budget is configured — it cannot self-attest "no provider
        # call" by simply leaving the field empty.
        if result.provider_metadata is not None:
            tokens_used: int | None = result.token_usage.total_tokens
        else:
            tokens_used = None
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            tokens_used=tokens_used,
            cost_usd=None,
            usage_provenance=UsageProvenance(
                source_id="registry_agent_invoker",
                tokens_verified=False,
                cost_verified=False,
            ),
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
            # R7 P0-1: a test double owns its receipts and can
            # authoritatively attest that no provider call was made
            # (deterministic mode).  This is the semantic opposite of
            # :class:`RegistryAgentInvoker`, which calls real Handlers
            # that can lie by omitting ``provider_metadata``.
            can_attest_no_provider_call=True,
            # R7 P0-6: empty ``bound_source_ids`` accepts any
            # ``source_id`` — the test double does not bind to a
            # specific verifier.  Tests that need to exercise source
            # binding pass explicit ``usage_capabilities`` with a
            # non-empty set.
            bound_source_ids=frozenset(),
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

    R6 P0-1 + P0-4: validation now uses per-dimension
    :class:`UsageProvenance` instead of the legacy ``usage_trust``
    string.  When ``tokens_verified=True``, the token count is
    authoritative (came from a :class:`ProviderUsageVerifier`) and
    may differ from the Handler's self-reported
    ``result.token_usage.total_tokens`` — so the consistency check
    is skipped in that case.  When ``tokens_verified=False`` but
    ``provider_metadata`` is present, the Handler's self-reported
    tokens must match the receipt (prevents under-reporting).

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

    # R6 P0-1: when tokens_verified=True, the verifier's tokens_used
    # is authoritative and may differ from the Handler's self-reported
    # total_tokens.  Skip the consistency check in that case.
    # When tokens_verified=False but provider_metadata is present,
    # the Handler's self-reported tokens must match the receipt.
    if (
        result.provider_metadata is not None
        and receipt.tokens_used is not None
        and not receipt.usage_provenance.tokens_verified
    ):
        actual_tokens = result.token_usage.total_tokens
        if receipt.tokens_used != actual_tokens:
            raise InvalidInvocationReceiptError(
                f"receipt.tokens_used={receipt.tokens_used} does not "
                f"match result.token_usage.total_tokens={actual_tokens} "
                f"(provider_metadata is present and tokens are not "
                f"verifier-attested, so Handler self-report is "
                f"authoritative)"
            )

    # R6 P0-4: ``tokens_verified=True`` provenance requires the
    # provider metadata to actually be present.  A receipt that claims
    # verified tokens without ``provider_metadata`` is lying about its
    # provenance — the verifier needs provider metadata to verify.
    if receipt.usage_provenance.tokens_verified and result.provider_metadata is None:
        raise InvalidInvocationReceiptError(
            "receipt.usage_provenance.tokens_verified=True but "
            "result.provider_metadata is None — provider attestation "
            "is required for verified token provenance"
        )

    # R6 P0-4: ``cost_verified=True`` provenance does NOT require
    # ``provider_metadata`` — a trusted adapter (e.g. a local billing
    # system) can verify cost without the LLM provider's attestation.
    # The per-dimension cross-check in ``_BudgetAccountant.record_receipt``
    # ensures the receipt's ``cost_verified`` does not exceed the
    # invoker's ``verifies_cost`` capability.


__all__ = [
    "AgentInvocationReceipt",
    "AgentInvoker",
    "AttemptUsageDisposition",
    "AttemptUsageRecord",
    "DeterministicFakeInvoker",
    "ProviderUsageVerifier",
    "RegistryAgentInvoker",
    "UsageProvenance",
    "UsageTrustLevel",
    "UsageVerificationCapabilities",
    "VerifiedUsage",
    "get_usage_capabilities",
    "validate_invocation_receipt",
]
