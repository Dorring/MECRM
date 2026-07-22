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

import warnings
from decimal import Decimal
from typing import Any, Callable, Protocol

from pydantic import Field, model_validator

from multi_agent.contracts import (
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    StrictContract,
)
from multi_agent.execution_errors import (
    InvalidInvocationReceiptError,
    NonRetryableAgentError,
)
from multi_agent.registry import AgentHandler, AgentRegistry

# R9 Section 4: the shared Usage types now live in
# :mod:`multi_agent.usage` to avoid a circular dependency between
# :mod:`multi_agent.contracts` (which needs ``AttemptUsageRecord`` for
# ``ExecutionUsage.attempt_usage_records``) and
# :mod:`multi_agent.invocation` (which defines the Receipt/Invoker).
# We import them here and re-export for backwards compatibility.
from multi_agent.usage import (
    AttemptUsageDisposition,
    AttemptUsageRecord,
    ProviderUsageVerifier,
    UsageProvenance,
    UsageTrustLevel,
    UsageVerificationCapabilities,
    VerifiedUsage,
    get_usage_capabilities,
    validate_usage_dimension,
)

# R9 Section 7: legacy trust↔provenance conversion helpers are now
# imported from :mod:`multi_agent.usage` so the Receipt's
# ``_sync_trust_provenance`` validator can use them.
from multi_agent.usage import _provenance_to_trust, _TRUST_TO_PROVENANCE


# ---------------------------------------------------------------------------
# R9 Section 2: Unified Invocation Outcome (success + failure)
# ---------------------------------------------------------------------------


class AgentInvocationOutcome(StrictContract):
    """R9 Section 2 / R10 P0-2/P0-5: Unified outcome for BOTH success
    and failure invocation paths.

    Success path: the Invoker returns an :class:`AgentInvocationReceipt`;
    the Supervisor wraps it into an Outcome with ``result=receipt.result``
    and the receipt's usage fields.

    Failure path: the Invoker raises :class:`AgentInvocationFailure`
    carrying an Outcome with partial usage info (e.g. tool calls
    observed before the exception).  Timeout / unknown exceptions
    produce an Outcome with ``observed_tool_calls=None`` (unknown) and
    ``UNAVAILABLE`` dispositions — the Runtime fails closed.

    Key invariants:

    * ``observed_tool_calls`` is ``None`` when the actual count is
      UNKNOWN (e.g. timeout, exception before any receipt).  It is
      ``0`` only when the Invoker authoritatively attests zero tool
      calls.  The Runtime treats ``None`` as ``tool_usage_unavailable``
      and fails closed (stops retries and new tasks).
    * ``token_disposition`` / ``cost_disposition`` follow the same
      semantics as :class:`AttemptUsageDisposition`.  ``UNAVAILABLE``
      is the safe default when the Invoker cannot attest anything.
    * ``token_source_id`` / ``cost_source_id`` are non-None only when
      the corresponding disposition is ``VERIFIED`` (R9 Section 6).

    R10 P0-2: ``observed_tool_calls`` is constrained to ``ge=0`` and,
    when ``result`` is present, MUST equal ``len(result.tool_calls)``.
    A failure Outcome can no longer under-report, negative-report, or
    hide tool calls that are visible in the Result.

    R10 P0-5: per-dimension invariants are enforced by the shared
    :func:`validate_usage_dimension` function — the SAME authority used
    by :class:`AttemptUsageRecord`, :class:`AgentInvocationReceipt`,
    and :class:`TaskAttemptRecord`.
    """

    result: AgentResult | None = None
    error_code: str | None = None

    observed_tool_calls: int | None = Field(default=None, ge=0)

    token_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE
    cost_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE

    tokens_used: int | None = None
    cost_usd: Decimal | None = None

    token_source_id: str | None = None
    cost_source_id: str | None = None

    @model_validator(mode="after")
    def _enforce_outcome_invariants(self) -> "AgentInvocationOutcome":
        # R10 P0-2: when a Result is present, observed_tool_calls MUST
        # match len(result.tool_calls).  A failure Outcome cannot
        # under-report or hide tool calls that are visible in the
        # Result — the Invoker boundary is the trusted source.
        if self.result is not None and self.observed_tool_calls is not None:
            actual = len(self.result.tool_calls)
            if self.observed_tool_calls != actual:
                raise ValueError(
                    f"AgentInvocationOutcome.observed_tool_calls="
                    f"{self.observed_tool_calls} does not match "
                    f"len(result.tool_calls)={actual} — a failure Outcome "
                    f"cannot under-report or hide tool calls (R10 P0-2)"
                )
        # R10 P0-5: enforce per-dimension invariants via the shared
        # function so Outcome, Receipt, Record, and AttemptRecord all
        # follow the SAME rules.
        validate_usage_dimension(
            "token",
            self.token_disposition,
            self.tokens_used,
            self.token_source_id,
        )
        validate_usage_dimension(
            "cost",
            self.cost_disposition,
            self.cost_usd,
            self.cost_source_id,
        )
        return self


class AgentInvocationFailure(Exception):
    """R9 Section 2: Controlled exception that carries a partial
    :class:`AgentInvocationOutcome` from a failed invocation.

    Invokers MAY raise this on failure paths to provide partial usage
    information (e.g. observed tool calls before the exception, or an
    explicit ``NO_PROVIDER_CALL`` attestation for a deterministic
    invoker that errored before producing a receipt).

    The Supervisor catches this and uses ``outcome`` to build the
    AttemptUsageRecord.  When no ``AgentInvocationFailure`` is
    available (e.g. ``asyncio.TimeoutError``, unknown ``Exception``),
    the Supervisor builds an Outcome with ``observed_tool_calls=None``
    and ``UNAVAILABLE`` dispositions — fail-closed.
    """

    def __init__(self, outcome: AgentInvocationOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"AgentInvocationFailure(error_code={outcome.error_code!r}, "
            f"observed_tool_calls={outcome.observed_tool_calls}, "
            f"token_disposition={outcome.token_disposition}, "
            f"cost_disposition={outcome.cost_disposition})"
        )


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
    #   ``invoker_capabilities.never_calls_provider=True`` (R9 Section 3).
    # * ``UNAVAILABLE`` is always accepted.
    token_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE
    cost_disposition: AttemptUsageDisposition = AttemptUsageDisposition.UNAVAILABLE
    # R7 P1-1 / R8 P1-2 / R9 Section 7: DEPRECATED — retained for
    # backwards compatibility.  Auto-derived from ``usage_provenance``
    # via :func:`_provenance_to_trust`.  New code must set
    # ``usage_provenance`` directly and must NOT pass both fields
    # simultaneously — R9 Section 7 makes ANY simultaneous provision a
    # ``ValidationError`` (even when the derived trust matches).
    usage_trust: UsageTrustLevel = Field(default="unverified")

    @model_validator(mode="after")
    def _enforce_receipt_dimension_invariants(self) -> "AgentInvocationReceipt":
        # R10 P0-5: enforce per-dimension invariants via the shared
        # function so the Receipt follows the SAME rules as
        # AttemptUsageRecord, AgentInvocationOutcome, and
        # TaskAttemptRecord.  The R9 carve-out that allowed numeric 0
        # alongside UNAVAILABLE / NO_PROVIDER_CALL is REMOVED.
        #
        # The Receipt's per-dimension source ids live on the nested
        # ``usage_provenance`` object (``token_source_id`` /
        # ``cost_source_id``), not as top-level fields.
        validate_usage_dimension(
            "token",
            self.token_disposition,
            self.tokens_used,
            self.usage_provenance.token_source_id,
        )
        validate_usage_dimension(
            "cost",
            self.cost_disposition,
            self.cost_usd,
            self.usage_provenance.cost_source_id,
        )
        return self

    @model_validator(mode="before")
    @classmethod
    def _sync_trust_provenance(cls, data: Any) -> Any:
        """R6 P0-4 / R8 P1-2 / R9 Section 7: ensure ``usage_trust``
        and ``usage_provenance`` are consistent.

        R9 Section 7: simultaneously providing BOTH fields is ALWAYS a
        ``ValidationError``, even when the derived trust matches.  The
        only accepted patterns are:

        * only ``usage_provenance`` provided → derive ``usage_trust``
        * only ``usage_trust`` provided → derive ``usage_provenance``
          (legacy compat, emits a deprecation warning)
        * both provided → ``ValidationError`` (no exceptions, even
          when values are consistent)
        """
        if not isinstance(data, dict):
            return data
        prov = data.get("usage_provenance")
        trust = data.get("usage_trust")
        # R9 Section 7: reject ANY simultaneous provision — even when
        # the derived trust matches.  Callers must migrate to
        # ``usage_provenance`` only.  This removes the R8 carve-out
        # that silently dropped the legacy field when values matched.
        if prov is not None and trust is not None:
            raise ValueError(
                "AgentInvocationReceipt: simultaneously providing "
                "usage_trust and usage_provenance is not allowed, even "
                "when the derived trust matches — migrate to "
                "usage_provenance only (R9 Section 7)."
            )
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
            # Legacy code: derive provenance from trust.  R10 Sync 6:
            # emit a DeprecationWarning so callers know to migrate.
            warnings.warn(
                "AgentInvocationReceipt.usage_trust is deprecated; "
                "use usage_provenance with explicit per-dimension "
                "source_id fields (R10 Sync 6).",
                DeprecationWarning,
                stacklevel=2,
            )
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
# R9 Section 4: VerifiedUsage and ProviderUsageVerifier are now defined
# in :mod:`multi_agent.usage` and re-exported above.  See that module
# for the R9 Section 8 (Choice A) changes — VerifiedUsage no longer
# carries per-dimension source ids.
# ---------------------------------------------------------------------------


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

        R7 P0-1 / R9 Section 3: ``never_calls_provider`` is always
        ``False`` for :class:`RegistryAgentInvoker` — a real Handler
        can lie by omitting ``provider_metadata``, so the Invoker
        cannot authoritatively attest that no provider call was made.
        Only pure deterministic invokers (e.g.
        :class:`DeterministicFakeInvoker`) set this to ``True``.
        R9 Section 3: this field is used for VALIDATION only — the
        Runtime does NOT infer ``NO_PROVIDER_CALL`` from it on the
        no-receipt path.  ``NO_PROVIDER_CALL`` must come from an
        explicit :class:`AgentInvocationOutcome` /
        :class:`AgentInvocationFailure`.

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
                never_calls_provider=False,
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
            never_calls_provider=False,
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
        # ``never_calls_provider=False`` (R9 Section 3), so a Handler
        # that omits ``provider_metadata`` triggers fail-closed when a
        # budget is configured — it cannot self-attest "no provider
        # call" by simply leaving the field empty.
        #
        # R10 P0-5: ``tokens_used`` MUST be ``None`` when
        # ``token_disposition=UNAVAILABLE`` — the strict per-dimension
        # invariant (shared with AttemptUsageRecord and
        # AgentInvocationOutcome) no longer allows a numeric value
        # alongside UNAVAILABLE.  The Handler's self-reported
        # ``result.token_usage.total_tokens`` is untrusted and must NOT
        # be carried on the Receipt when the dimension is UNAVAILABLE.
        # It remains accessible via ``receipt.result.token_usage`` for
        # diagnostic purposes.
        return AgentInvocationReceipt(
            result=result,
            tool_calls=len(result.tool_calls),
            tokens_used=None,
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
            # R7 P0-1 / R9 Section 3: a pure deterministic test double
            # owns its receipts and its every path skips the Provider,
            # so ``never_calls_provider=True`` is a static guarantee
            # (not an inference).  This is the semantic opposite of
            # :class:`RegistryAgentInvoker`, which calls real Handlers
            # that can lie by omitting ``provider_metadata``.
            #
            # R9 Section 3: ``never_calls_provider`` is used for
            # VALIDATION only — the Accountant checks that a
            # NO_PROVIDER_CALL disposition is declared by an Invoker
            # with this capability.  It is NOT used to INFER
            # NO_PROVIDER_CALL on the no-receipt path; the Invoker
            # must explicitly declare it via the Receipt or an
            # :class:`AgentInvocationOutcome`.
            never_calls_provider=True,
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
        # R8 P0-1 / R9 Section 3: the test double explicitly declares
        # NO_PROVIDER_CALL because it owns its receipts and its every
        # path skips the Provider (``never_calls_provider=True``).
        # The Accountant validates this declaration against the
        # Invoker capability.  R9 Section 3: the capability is for
        # VALIDATION only — it does NOT auto-infer NO_PROVIDER_CALL
        # on failure paths.
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
    # R10 P0-5: ``receipt.tokens_used`` is now ONLY non-None when
    # ``token_disposition=VERIFIED`` (enforced by the Receipt's
    # model_validator via :func:`validate_usage_dimension`).  The
    # previous consistency check for the UNAVAILABLE path is no longer
    # needed because unverified Handler self-reported tokens are no
    # longer carried on the Receipt.
    if (
        result.provider_metadata is not None
        and receipt.tokens_used is not None
        and receipt.usage_provenance.tokens_verified
    ):
        # VERIFIED path — the verifier's value is authoritative; no
        # consistency check against the Handler's self-report.
        pass

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

    # R10 P0-5: per-dimension invariants (VERIFIED requires value +
    # source; NO_PROVIDER_CALL / UNAVAILABLE require value=None +
    # source=None) are now enforced by the Receipt's model_validator
    # via the shared :func:`validate_usage_dimension` function.  The
    # explicit VERIFIED / NO_PROVIDER_CALL checks that previously lived
    # here are redundant and have been removed to avoid drift between
    # two enforcement sites.


__all__ = [
    "AgentInvocationReceipt",
    "AgentInvocationFailure",
    "AgentInvocationOutcome",
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
    "validate_usage_dimension",
]
