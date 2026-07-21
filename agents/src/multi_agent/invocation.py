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
    """R6 P0-4 / R8 P0-3: Per-dimension usage provenance for a
    receipt.

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

    R8 P0-3: provenance now carries per-dimension ``token_source_id``
    / ``cost_source_id`` so that source binding is enforced
    per-dimension.  The legacy single ``source_id`` field is retained
    for backwards compatibility and is auto-derived as
    ``token_source_id or cost_source_id or "unverified"``.

    The two flags are independent: a verifier that only checks tokens
    sets ``tokens_verified=True, cost_verified=False``, and the
    accountant will record tokens but NOT cost (nor enforce
    ``cost_budget_usd``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # R8 P0-3: per-dimension source ids.  ``source_id`` (legacy) is
    # auto-derived in a model_validator so existing callers that only
    # set ``source_id`` still work — the value is mirrored into both
    # ``token_source_id`` and ``cost_source_id``.
    token_source_id: str | None = None
    cost_source_id: str | None = None
    tokens_verified: bool = False
    cost_verified: bool = False
    # R8 P0-3: legacy single ``source_id`` — retained for backwards
    # compatibility.  When set, it mirrors into both per-dimension
    # fields when those are ``None``.  When both per-dimension fields
    # are set, ``source_id`` is derived as the token source (or cost
    # source if token is ``None``).
    source_id: str = "unverified"

    @model_validator(mode="after")
    def _sync_source_ids(self) -> "UsageProvenance":
        # Mirror legacy single ``source_id`` into per-dimension fields
        # when the caller did not specify them explicitly.
        token_src = self.token_source_id
        cost_src = self.cost_source_id
        legacy = self.source_id
        if token_src is None and cost_src is None and legacy != "unverified":
            token_src = legacy
            cost_src = legacy
            object.__setattr__(self, "token_source_id", token_src)
            object.__setattr__(self, "cost_source_id", cost_src)
        # Derive ``source_id`` from per-dimension fields when it was
        # not explicitly set (i.e. still "unverified" but per-dim
        # fields are populated).
        if legacy == "unverified":
            derived = token_src or cost_src
            if derived is not None:
                object.__setattr__(self, "source_id", derived)
        return self


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
    """R7 P0-3 / R8 P0-3 / R8 P1-1: Per-attempt usage record for
    independent Token/Cost coverage tracking.

    Each committed agent call produces exactly one
    :class:`AttemptUsageRecord`.  Token and Cost dispositions are
    independent — a single attempt can be ``VERIFIED`` for tokens but
    ``UNAVAILABLE`` for cost (or any other combination).

    R8 P0-3: the record carries per-dimension ``token_source_id`` /
    ``cost_source_id`` for auditing which Verifier/Adapter attested
    each dimension.  The legacy single ``source_id`` field is retained
    for backwards compatibility and is auto-derived as
    ``token_source_id or cost_source_id``.

    R8 P1-1: these records are now exposed via
    :attr:`ExecutionUsage.attempt_usage_records` (and
    :attr:`SupervisorRunResult.usage.attempt_usage_records`) so
    external audit consumers can inspect them after the run finishes.

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
    # R8 P0-3: per-dimension source ids for auditing.
    token_source_id: str | None = None
    cost_source_id: str | None = None
    # R8 P0-3: legacy single ``source_id`` — derived as
    # ``token_source_id or cost_source_id``.
    source_id: str | None = None

    @model_validator(mode="after")
    def _sync_legacy_source_id(self) -> "AttemptUsageRecord":
        if self.source_id is None:
            derived = self.token_source_id or self.cost_source_id
            object.__setattr__(self, "source_id", derived)
        return self


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
    """R4 P0-2 / R8 P0-2 / R8 P0-3: Immutable description of what an
    :class:`AgentInvoker` can *actually* verify about usage.

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
    * ``bound_source_ids`` — R7 P0-6 / R8 P0-3 DEPRECATED: the set
      of Verifier/Adapter source identities that this Invoker's
      receipts may reference in ``usage_provenance.source_id``.
      Retained for backwards compatibility; mirrors into both
      ``bound_token_source_ids`` and ``bound_cost_source_ids`` when
      those are empty.
    * ``bound_token_source_ids`` — R8 P0-3: per-dimension binding for
      Token provenance.  When non-empty, a receipt may claim
      ``tokens_verified=True`` only if its ``token_source_id`` is in
      this set.  Must be non-empty when ``verifies_tokens=True``.
    * ``bound_cost_source_ids`` — R8 P0-3: per-dimension binding for
      Cost provenance.  When non-empty, a receipt may claim
      ``cost_verified=True`` only if its ``cost_source_id`` is in
      this set.  Must be non-empty when ``verifies_cost=True``.

    R8 P0-2: the two ``verifies_*`` flags are independent — a
    cost-only verifier sets ``verifies_cost=True,
    verifies_tokens=False``, and the accountant will reject any
    receipt that claims ``tokens_verified=True`` from such an
    Invoker.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str
    can_attest_no_provider_call: bool = False
    # R8 P0-3: per-dimension bound source sets.
    bound_token_source_ids: frozenset[str] = Field(default_factory=frozenset)
    bound_cost_source_ids: frozenset[str] = Field(default_factory=frozenset)
    # R7 P0-6 / R8 P0-3 DEPRECATED: legacy single bound set.  When
    # non-empty and the per-dimension sets are empty, mirrors into
    # both per-dimension sets.
    bound_source_ids: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _sync_bound_source_ids(self) -> "UsageVerificationCapabilities":
        # R8 P0-3: mirror legacy single ``bound_source_ids`` into
        # per-dimension sets when the caller did not specify them
        # explicitly.
        if self.bound_source_ids and not self.bound_token_source_ids:
            object.__setattr__(self, "bound_token_source_ids", self.bound_source_ids)
        if self.bound_source_ids and not self.bound_cost_source_ids:
            object.__setattr__(self, "bound_cost_source_ids", self.bound_source_ids)
        # R8 P0-2: Contract invariant — declaring ``verifies_*=True``
        # without any bound source for that dimension is a programming
        # error.  The Invoker is claiming it can verify a dimension
        # but has not bound any Verifier/Adapter source for it, so a
        # receipt could claim verification from an arbitrary source.
        if self.verifies_tokens and not self.bound_token_source_ids:
            raise ValueError(
                "verifies_tokens=True requires a non-empty "
                "bound_token_source_ids — an Invoker that can verify "
                "tokens must bind the Verifier/Adapter sources it "
                "accepts"
            )
        if self.verifies_cost and not self.bound_cost_source_ids:
            raise ValueError(
                "verifies_cost=True requires a non-empty "
                "bound_cost_source_ids — an Invoker that can verify "
                "cost must bind the Verifier/Adapter sources it accepts"
            )
        return self


_UNVERIFIED_CAPABILITIES = UsageVerificationCapabilities(
    verifies_tokens=False,
    verifies_cost=False,
    source_id="unverified",
    can_attest_no_provider_call=False,
    bound_token_source_ids=frozenset(),
    bound_cost_source_ids=frozenset(),
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

    R8 P0-1: the Receipt now carries per-attempt explicit
    :class:`AttemptUsageDisposition` fields (``token_disposition`` /
    ``cost_disposition``) produced by the Invoker boundary — NOT
    inferred by the Accountant from ``provider_metadata is None``.
    The Invoker is the trusted boundary that knows whether a Provider
    call was made; the Accountant only VALIDATES the declared
    dispositions against Invoker capabilities (e.g. an Invoker with
    ``can_attest_no_provider_call=False`` may not declare
    ``NO_PROVIDER_CALL``).

    R8 P1-2: simultaneously providing both ``usage_trust`` (legacy)
    and ``usage_provenance`` (new) now raises ``ValidationError``
    instead of silently letting ``usage_provenance`` win.  Callers
    must migrate to ``usage_provenance``; passing both is a
    programming error that previously allowed conflicting inputs to
    silently override each other.

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
    # R8 P0-1: explicit per-attempt dispositions declared by the
    # Invoker boundary.  Defaults to ``UNAVAILABLE`` so a Receipt
    # constructed without explicit dispositions (e.g. legacy test
    # helpers) cannot accidentally claim ``VERIFIED`` or
    # ``NO_PROVIDER_CALL``.  The Accountant validates these against
    # Invoker capabilities:
    #
    # * ``VERIFIED`` requires ``usage_provenance.{dim}_verified=True``
    #   AND ``invoker_capabilities.verifies_{dim}=True`` AND (when
    #   ``bound_{dim}_source_ids`` is non-empty) the corresponding
    #   ``{dim}_source_id`` must be in the bound set.
    # * ``NO_PROVIDER_CALL`` requires
    #   ``invoker_capabilities.can_attest_no_provider_call=True``.
    # * ``UNAVAILABLE`` is always accepted.
    token_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE
    cost_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE
    # R7 P1-1 / R8 P1-2: DEPRECATED — retained for backwards
    # compatibility.  Auto-derived from ``usage_provenance`` via
    # :func:`_provenance_to_trust`.  New code must set
    # ``usage_provenance`` directly and must NOT pass both fields
    # simultaneously — R8 P1-2 makes simultaneous provision a
    # ``ValidationError`` instead of a silent override.
    usage_trust: UsageTrustLevel = Field(default="unverified")

    @model_validator(mode="before")
    @classmethod
    def _sync_trust_provenance(cls, data: Any) -> Any:
        """R6 P0-4 / R8 P1-2: ensure ``usage_trust`` and
        ``usage_provenance`` are consistent.

        R8 P1-2: simultaneously providing both fields now raises
        ``ValidationError``.  Callers must migrate to
        ``usage_provenance``; passing both is a programming error
        that previously allowed conflicting inputs to silently
        override each other.
        """
        if not isinstance(data, dict):
            return data
        prov = data.get("usage_provenance")
        trust = data.get("usage_trust")
        # R8 P1-2: reject simultaneous provision.
        if prov is not None and trust is not None:
            # Allow the case where the caller passes the auto-derived
            # default ``usage_trust="unverified`` together with an
            # explicit ``usage_provenance`` whose derived trust is
            # also ``"unverified"`` — this is the common legacy
            # pattern where ``usage_trust`` was not explicitly set.
            # We detect this by checking whether the derived trust
            # matches the provided trust.
            if isinstance(prov, UsageProvenance):
                derived = _provenance_to_trust(prov)
            elif isinstance(prov, dict):
                derived = _provenance_to_trust(UsageProvenance(**prov))
            else:
                derived = "unverified"
            if derived != trust:
                raise ValueError(
                    "AgentInvocationReceipt: simultaneously providing "
                    "usage_trust and usage_provenance with conflicting "
                    "values is not allowed — migrate to usage_provenance "
                    "only (R8 P1-2)."
                )
            # Conflicts resolved — drop the legacy field so the
            # derived value wins.
            data = dict(data)
            data.pop("usage_trust", None)
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
    """R5 P0-5 / R8 P0-2: Result of a Provider Usage Verifier check.

    R8 P0-2: the single ``verified: bool`` field is DEPRECATED and
    replaced by independent per-dimension ``tokens_verified`` /
    ``cost_verified`` flags.  A cost-only verifier sets
    ``cost_verified=True, tokens_verified=False`` — the accountant
    will NOT mark the Token dimension as VERIFIED on the basis of a
    cost-only verification.  The legacy ``verified`` field is
    auto-derived as ``tokens_verified or cost_verified`` for backwards
    compatibility.

    Invariants enforced by a model_validator:

    * ``tokens_verified=True`` → ``tokens_used is not None``
    * ``cost_verified=True`` → ``cost_usd is not None``

    R8 P0-2: the verifier also exposes per-dimension source ids so
    the Invoker can bind them in its
    :class:`UsageVerificationCapabilities`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_verified: bool = False
    cost_verified: bool = False
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    token_source_id: str | None = None
    cost_source_id: str | None = None
    # R8 P0-2 DEPRECATED: retained for backwards compatibility.
    # Auto-derived as ``tokens_verified or cost_verified``.
    verified: bool = False

    @model_validator(mode="after")
    def _enforce_per_dimension_invariants(self) -> "VerifiedUsage":
        # R8 P0-2: VERIFIED requires a non-None value for that dim.
        if self.tokens_verified and self.tokens_used is None:
            raise ValueError(
                "VerifiedUsage.tokens_verified=True requires tokens_used to be non-None"
            )
        if self.cost_verified and self.cost_usd is None:
            raise ValueError(
                "VerifiedUsage.cost_verified=True requires cost_usd to be non-None"
            )
        object.__setattr__(
            self,
            "verified",
            self.tokens_verified or self.cost_verified,
        )
        return self


class ProviderUsageVerifier(Protocol):
    """R5 P0-5 / R8 P0-2: Authoritative Provider Usage verification
    boundary.

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

    R8 P0-2: the verifier exposes INDEPENDENT per-dimension
    capabilities (``verifies_tokens`` / ``verifies_cost``).  A
    cost-only verifier sets ``verifies_cost=True,
    verifies_tokens=False`` — its :class:`VerifiedUsage` results must
    set ``cost_verified=True, tokens_verified=False`` so the Invoker
    cannot accidentally claim Token verification from a cost-only
    verifier.
    """

    source_id: str
    verifies_tokens: bool
    verifies_cost: bool

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

    R8 P0-1: the Invoker now produces explicit per-attempt
    :class:`AttemptUsageDisposition` fields on the Receipt based on
    the verifier's per-dimension result.  The Accountant no longer
    infers ``NO_PROVIDER_CALL`` from ``provider_metadata is None``.

    R8 P0-2: the Invoker's capabilities are derived from the
    verifier's per-dimension ``verifies_tokens`` / ``verifies_cost``
    flags — a cost-only verifier no longer elevates Token trust.
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
        """R5 P0-5 + R7 P0-1 + R8 P0-2/P0-3: by default (no
        ``usage_verifier``) the Invoker cannot verify tokens or cost
        — the Handler's ``provider_metadata`` is self-attested.  When
        a :class:`ProviderUsageVerifier` is configured, the
        per-dimension ``verifies_tokens`` / ``verifies_cost`` flags
        are taken from the verifier independently.

        R7 P0-1: ``can_attest_no_provider_call`` is always ``False``
        for :class:`RegistryAgentInvoker` — a real Handler can lie by
        omitting ``provider_metadata``, so the Invoker cannot
        authoritatively attest that no provider call was made.  Only
        trusted deterministic invokers (e.g.
        :class:`DeterministicFakeInvoker`) set this to ``True``.

        R8 P0-2/P0-3: ``bound_token_source_ids`` and
        ``bound_cost_source_ids`` are populated independently based
        on the verifier's per-dimension capabilities.  A cost-only
        verifier only binds its source to the cost dimension.
        """
        if self._usage_verifier is None:
            return UsageVerificationCapabilities(
                verifies_tokens=False,
                verifies_cost=False,
                source_id="registry_agent_invoker",
                can_attest_no_provider_call=False,
                bound_token_source_ids=frozenset(),
                bound_cost_source_ids=frozenset(),
                bound_source_ids=frozenset(),
            )
        verifier = self._usage_verifier
        # R8 P0-2: derive per-dimension capabilities from the verifier.
        verifies_tokens = getattr(verifier, "verifies_tokens", False)
        verifies_cost = getattr(verifier, "verifies_cost", False)
        # R8 P0-3: bind the verifier's source_id only to the
        # dimensions it actually verifies.
        bound_token = (
            frozenset({verifier.source_id}) if verifies_tokens else frozenset()
        )
        bound_cost = frozenset({verifier.source_id}) if verifies_cost else frozenset()
        return UsageVerificationCapabilities(
            verifies_tokens=verifies_tokens,
            verifies_cost=verifies_cost,
            source_id="registry_agent_invoker+provider_verifier",
            can_attest_no_provider_call=False,
            bound_token_source_ids=bound_token,
            bound_cost_source_ids=bound_cost,
            bound_source_ids=frozenset(),
        )

    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt:
        result = await handler.run(task, context)

        # R6 P0-1 + R7 P0-5 + R8 P0-2: Actually call the
        # ProviderUsageVerifier when one is configured and the Handler
        # returned provider_metadata.  The verifier's per-dimension
        # result — not the verifier's mere existence — determines
        # which dimensions are VERIFIED.  R7 P0-5: the verifier is now
        # async and is awaited directly.  R8 P0-2: a cost-only
        # verifier no longer elevates Token trust.
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

            # R8 P0-2: reject a verifier that returns verified=False
            # for BOTH dimensions — no usage can be trusted.
            if not (verified.tokens_verified or verified.cost_verified):
                raise NonRetryableAgentError(
                    f"ProviderUsageVerifier ({self._usage_verifier.source_id}) "
                    f"returned tokens_verified=False AND cost_verified=False "
                    f"— handler self-reported usage is not trusted"
                )

            # R8 P0-2: build per-dimension provenance from the
            # verifier's independent flags.  A cost-only verifier
            # produces ``cost_verified=True, tokens_verified=False``.
            token_disp = (
                AttemptUsageDisposition.VERIFIED
                if verified.tokens_verified
                else AttemptUsageDisposition.UNAVAILABLE
            )
            cost_disp = (
                AttemptUsageDisposition.VERIFIED
                if verified.cost_verified
                else AttemptUsageDisposition.UNAVAILABLE
            )
            return AgentInvocationReceipt(
                result=result,
                tool_calls=len(result.tool_calls),
                tokens_used=verified.tokens_used,
                cost_usd=verified.cost_usd,
                usage_provenance=UsageProvenance(
                    token_source_id=(
                        self._usage_verifier.source_id
                        if verified.tokens_verified
                        else None
                    ),
                    cost_source_id=(
                        self._usage_verifier.source_id
                        if verified.cost_verified
                        else None
                    ),
                    tokens_verified=verified.tokens_verified,
                    cost_verified=verified.cost_verified,
                ),
                token_disposition=token_disp,
                cost_disposition=cost_disp,
            )

        # No verifier or no provider_metadata → unverified.
        # R8 P0-1: the Invoker declares UNAVAILABLE explicitly — the
        # Accountant no longer infers this from ``provider_metadata
        # is None``.  :class:`RegistryAgentInvoker` always has
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
                token_source_id=None,
                cost_source_id=None,
                tokens_verified=False,
                cost_verified=False,
            ),
            token_disposition=AttemptUsageDisposition.UNAVAILABLE,
            cost_disposition=AttemptUsageDisposition.UNAVAILABLE,
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
            # R8 P0-3: empty per-dimension ``bound_*_source_ids``
            # accept any ``source_id`` — the test double does not bind
            # to a specific verifier.  Tests that need to exercise
            # source binding pass explicit ``usage_capabilities`` with
            # a non-empty set.  Since ``verifies_tokens=False`` and
            # ``verifies_cost=False``, the empty sets do not violate
            # the R8 P0-2 contract invariant.
            bound_token_source_ids=frozenset(),
            bound_cost_source_ids=frozenset(),
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
        # R8 P0-1: the test double explicitly declares
        # NO_PROVIDER_CALL because it owns its receipts and can
        # authoritatively attest that no provider call was made
        # (``can_attest_no_provider_call=True``).  The Accountant
        # validates this declaration against the Invoker capability.
        return AgentInvocationReceipt(
            result=self._result,
            tool_calls=len(self._result.tool_calls),
            tokens_used=None,
            cost_usd=None,
            token_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
            cost_disposition=AttemptUsageDisposition.NO_PROVIDER_CALL,
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

    # R8 P0-1: validate that the explicit per-attempt dispositions on
    # the Receipt are consistent with the provenance flags.  The
    # Invoker is the trusted boundary that declares these; the
    # Accountant validates them against Invoker capabilities.  Here
    # we only check internal Receipt consistency.
    if receipt.token_disposition == AttemptUsageDisposition.VERIFIED:
        if not receipt.usage_provenance.tokens_verified:
            raise InvalidInvocationReceiptError(
                "receipt.token_disposition=VERIFIED but "
                "usage_provenance.tokens_verified=False — a VERIFIED "
                "disposition requires the corresponding provenance flag"
            )
        if receipt.tokens_used is None:
            raise InvalidInvocationReceiptError(
                "receipt.token_disposition=VERIFIED but tokens_used is "
                "None — a VERIFIED token disposition requires a "
                "non-None tokens_used value"
            )
    if receipt.cost_disposition == AttemptUsageDisposition.VERIFIED:
        if not receipt.usage_provenance.cost_verified:
            raise InvalidInvocationReceiptError(
                "receipt.cost_disposition=VERIFIED but "
                "usage_provenance.cost_verified=False — a VERIFIED "
                "disposition requires the corresponding provenance flag"
            )
        if receipt.cost_usd is None:
            raise InvalidInvocationReceiptError(
                "receipt.cost_disposition=VERIFIED but cost_usd is "
                "None — a VERIFIED cost disposition requires a "
                "non-None cost_usd value"
            )
    # R8 P0-1: NO_PROVIDER_CALL dispositions must not carry usage
    # values — if no provider call was made, there is no usage to
    # report.  A Receipt that declares NO_PROVIDER_CALL but also
    # reports tokens_used/cost_usd is internally inconsistent.
    if receipt.token_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL:
        if receipt.tokens_used is not None and receipt.tokens_used != 0:
            raise InvalidInvocationReceiptError(
                "receipt.token_disposition=NO_PROVIDER_CALL but "
                f"tokens_used={receipt.tokens_used} — no provider "
                "call means no token usage"
            )
    if receipt.cost_disposition == AttemptUsageDisposition.NO_PROVIDER_CALL:
        if receipt.cost_usd is not None and receipt.cost_usd != Decimal("0"):
            raise InvalidInvocationReceiptError(
                "receipt.cost_disposition=NO_PROVIDER_CALL but "
                f"cost_usd={receipt.cost_usd} — no provider call "
                "means no cost usage"
            )


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
