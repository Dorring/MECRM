"""R9: Strict Usage Audit contracts — no circular dependencies.

This module is the **single source of truth** for Phase 4 Usage types.
It depends only on :mod:`multi_agent.contracts` (for
:class:`StrictContract`) and the standard library, so it can be imported
by both :mod:`multi_agent.contracts` (via ``TYPE_CHECKING``) and
:mod:`multi_agent.invocation` / :mod:`multi_agent.supervisor` without
creating a circular dependency.

R9 changes from R8:

* **Section 4** — The five shared Usage types
  (:class:`AttemptUsageDisposition`, :class:`UsageProvenance`,
  :class:`AttemptUsageRecord`, :class:`UsageVerificationCapabilities`,
  :class:`VerifiedUsage`) now live here instead of
  :mod:`multi_agent.invocation`.  This lets
  :class:`multi_agent.contracts.ExecutionUsage` and
  :class:`multi_agent.execution.TaskAttemptRecord` reference them with
  strict types instead of ``Any``.

* **Section 5** — :class:`AttemptUsageRecord` now enforces per-dimension
  Contract invariants: ``VERIFIED`` requires a non-None value AND a
  non-None ``source_id``; ``NO_PROVIDER_CALL`` requires both to be
  ``None``; ``UNAVAILABLE`` requires the value to be ``None``.

* **Section 6** — :class:`UsageProvenance` no longer mirrors the legacy
  single ``source_id`` into per-dimension fields.  ``tokens_verified``
  requires ``token_source_id``; ``cost_verified`` requires
  ``cost_source_id``.  The Accountant does not fall back to
  ``source_id`` when checking per-dimension bindings.

* **Section 7** — Legacy ``usage_trust`` handling is tightened: both
  ``usage_trust`` and ``usage_provenance`` provided simultaneously is
  ALWAYS a ``ValidationError``, even when the derived trust matches.

* **Section 8** — :class:`VerifiedUsage` no longer carries
  ``token_source_id`` / ``cost_source_id``.  The Invoker uses the
  Verifier's frozen ``source_id`` for both dimensions (Choice A).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from multi_agent.contracts import StrictContract

if TYPE_CHECKING:
    from multi_agent.contracts import ProviderMetadata, TokenUsage


# ---------------------------------------------------------------------------
# R3 P0-4: Legacy Usage Trust Level (DEPRECATED — use AttemptUsageDisposition)
# ---------------------------------------------------------------------------

UsageTrustLevel = Literal[
    "verified_provider",
    "trusted_adapter",
    "unverified",
]


# ---------------------------------------------------------------------------
# R7 P0-1 / R9 Section 5: AttemptUsageDisposition
# ---------------------------------------------------------------------------


class AttemptUsageDisposition(StrEnum):
    """Per-dimension usage disposition for a committed attempt.

    * ``VERIFIED`` — the dimension's usage was attested by an
      authoritative :class:`ProviderUsageVerifier` (or a trusted
      adapter).  The value in the receipt/outcome may be trusted for
      budget enforcement.
    * ``NO_PROVIDER_CALL`` — the invoker authoritatively attests that
      NO provider call was made for this attempt.  Only an Invoker with
      ``never_calls_provider=True`` may declare this, and ONLY via an
      explicit :class:`AgentInvocationOutcome` — it cannot be inferred
      from a missing receipt or from the static capability alone (R9
      Section 3).
    * ``UNAVAILABLE`` — the dimension's usage is unknown.  Covers:
      no receipt (timeout / exception), invalid receipt,
      ``provider_metadata`` absent and the invoker cannot attest no
      provider call, or ``provider_metadata`` present but not verified.
    """

    VERIFIED = "verified"
    NO_PROVIDER_CALL = "no_provider_call"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# R6 P0-4 / R8 P0-3 / R9 Section 6: UsageProvenance
# ---------------------------------------------------------------------------


class UsageProvenance(StrictContract):
    """Per-dimension usage provenance for a receipt.

    R9 Section 6: ``tokens_verified=True`` requires ``token_source_id``
    to be set; ``cost_verified=True`` requires ``cost_source_id`` to be
    set.  The legacy single ``source_id`` field is retained ONLY for
    backwards-compatible input migration — the runtime never falls back
    to it when checking per-dimension bindings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_source_id: str | None = None
    cost_source_id: str | None = None
    tokens_verified: bool = False
    cost_verified: bool = False
    # DEPRECATED — retained for backwards-compatible input migration.
    # New code must set ``token_source_id`` / ``cost_source_id``
    # directly.  The runtime does NOT fall back to this field.
    source_id: str = "unverified"

    @model_validator(mode="after")
    def _enforce_per_dimension_source(self) -> "UsageProvenance":
        # R9 Section 6: VERIFIED requires the corresponding per-dimension
        # source_id.  No fallback to the legacy ``source_id`` field.
        if self.tokens_verified and not self.token_source_id:
            raise ValueError(
                "UsageProvenance.tokens_verified=True requires "
                "token_source_id to be set (R9 Section 6: no cross-dimension "
                "or legacy fallback)"
            )
        if self.cost_verified and not self.cost_source_id:
            raise ValueError(
                "UsageProvenance.cost_verified=True requires "
                "cost_source_id to be set (R9 Section 6: no cross-dimension "
                "or legacy fallback)"
            )
        return self


# ---------------------------------------------------------------------------
# R7 P0-3 / R8 P0-3 / R9 Section 5: AttemptUsageRecord
# ---------------------------------------------------------------------------


class AttemptUsageRecord(StrictContract):
    """Per-attempt usage record for independent Token/Cost coverage.

    R9 Section 5 invariants (per-dimension):

    * ``VERIFIED`` → value (``tokens_used`` / ``cost_usd``) is non-None
      AND the corresponding ``source_id`` is non-None.
    * ``NO_PROVIDER_CALL`` → value is None AND ``source_id`` is None.
    * ``UNAVAILABLE`` → value is None.

    Token and Cost are validated independently — a single record can be
    ``VERIFIED`` for tokens but ``UNAVAILABLE`` for cost (mixed
    disposition).  R9 Section 1 requires the Accountant to commit such
    mixed records rather than discarding the verified dimension.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    attempt: int
    token_disposition: AttemptUsageDisposition
    cost_disposition: AttemptUsageDisposition
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    token_source_id: str | None = None
    cost_source_id: str | None = None

    @model_validator(mode="after")
    def _enforce_token_invariants(self) -> "AttemptUsageRecord":
        return self._enforce_dimension(
            "token",
            self.token_disposition,
            self.tokens_used,
            self.token_source_id,
        )

    @model_validator(mode="after")
    def _enforce_cost_invariants(self) -> "AttemptUsageRecord":
        return self._enforce_dimension(
            "cost",
            self.cost_disposition,
            self.cost_usd,
            self.cost_source_id,
        )

    def _enforce_dimension(
        self,
        dim: str,
        disposition: AttemptUsageDisposition,
        value: object,
        source_id: str | None,
    ) -> "AttemptUsageRecord":
        if disposition == AttemptUsageDisposition.VERIFIED:
            if value is None:
                raise ValueError(
                    f"AttemptUsageRecord.{dim}_disposition=VERIFIED but "
                    f"{dim}_value is None — VERIFIED requires a non-None "
                    f"actual value (R9 Section 5)"
                )
            if source_id is None:
                raise ValueError(
                    f"AttemptUsageRecord.{dim}_disposition=VERIFIED but "
                    f"{dim}_source_id is None — VERIFIED requires a "
                    f"non-None source_id (R9 Section 5)"
                )
        elif disposition == AttemptUsageDisposition.NO_PROVIDER_CALL:
            if value is not None and value != 0 and value != Decimal("0"):
                raise ValueError(
                    f"AttemptUsageRecord.{dim}_disposition=NO_PROVIDER_CALL "
                    f"but {dim}_value={value} — no provider call means no "
                    f"usage (R9 Section 5)"
                )
            if source_id is not None:
                raise ValueError(
                    f"AttemptUsageRecord.{dim}_disposition=NO_PROVIDER_CALL "
                    f"but {dim}_source_id={source_id!r} — no provider call "
                    f"means no source (R9 Section 5)"
                )
        elif disposition == AttemptUsageDisposition.UNAVAILABLE:
            if value is not None and value != 0 and value != Decimal("0"):
                raise ValueError(
                    f"AttemptUsageRecord.{dim}_disposition=UNAVAILABLE but "
                    f"{dim}_value={value} — UNAVAILABLE means the actual "
                    f"value is unknown (R9 Section 5)"
                )
        return self


# Backwards-compatible mapping: old ``usage_trust`` → ``UsageProvenance``.
_TRUST_TO_PROVENANCE: dict[str, UsageProvenance] = {
    "verified_provider": UsageProvenance(
        token_source_id="verified_provider",
        cost_source_id=None,
        tokens_verified=True,
        cost_verified=False,
    ),
    "trusted_adapter": UsageProvenance(
        token_source_id=None,
        cost_source_id="trusted_adapter",
        tokens_verified=False,
        cost_verified=True,
    ),
    "unverified": UsageProvenance(
        token_source_id=None,
        cost_source_id=None,
        tokens_verified=False,
        cost_verified=False,
    ),
}


def _provenance_to_trust(prov: UsageProvenance) -> UsageTrustLevel:
    """Derive the legacy ``usage_trust`` string from provenance."""
    if prov.cost_verified:
        return "trusted_adapter"
    if prov.tokens_verified:
        return "verified_provider"
    return "unverified"


# ---------------------------------------------------------------------------
# R4 P0-2 / R8 P0-2 / R9 Section 3: UsageVerificationCapabilities
# ---------------------------------------------------------------------------


class UsageVerificationCapabilities(StrictContract):
    """Immutable description of what an :class:`AgentInvoker` can
    *actually* verify about usage.

    R9 Section 3: ``never_calls_provider`` replaces the R7
    ``can_attest_no_provider_call`` field.  The semantic is stricter:
    it means the Invoker's ANY path never calls a Provider — only pure
    deterministic Invokers may set it to ``True``.  This field is used
    to VALIDATE ``NO_PROVIDER_CALL`` dispositions declared in Outcomes
    and Receipts; it is NOT used to INFER them.  The runtime only
    accepts ``NO_PROVIDER_CALL`` when the Invoker explicitly declares
    it via an :class:`AgentInvocationOutcome` (R9 Section 3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str
    # R9 Section 3: renamed from ``can_attest_no_provider_call``.  True
    # only for pure deterministic Invokers whose every path skips the
    # Provider.  Used for VALIDATION, not INFERENCE.
    never_calls_provider: bool = False
    bound_token_source_ids: frozenset[str] = Field(default_factory=frozenset)
    bound_cost_source_ids: frozenset[str] = Field(default_factory=frozenset)
    # DEPRECATED — retained for backwards-compatible input migration.
    bound_source_ids: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _sync_bound_source_ids(self) -> "UsageVerificationCapabilities":
        # Mirror legacy single ``bound_source_ids`` into per-dimension
        # sets when the caller did not specify them explicitly.
        if self.bound_source_ids and not self.bound_token_source_ids:
            object.__setattr__(self, "bound_token_source_ids", self.bound_source_ids)
        if self.bound_source_ids and not self.bound_cost_source_ids:
            object.__setattr__(self, "bound_cost_source_ids", self.bound_source_ids)
        # Contract invariant — declaring ``verifies_*=True`` without any
        # bound source for that dimension is a programming error.
        if self.verifies_tokens and not self.bound_token_source_ids:
            raise ValueError(
                "verifies_tokens=True requires a non-empty "
                "bound_token_source_ids — an Invoker that can verify "
                "tokens must bind the Verifier/Adapter sources it accepts"
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
    never_calls_provider=False,
    bound_token_source_ids=frozenset(),
    bound_cost_source_ids=frozenset(),
    bound_source_ids=frozenset(),
)


def get_usage_capabilities(invoker: object) -> UsageVerificationCapabilities:
    """Extract :class:`UsageVerificationCapabilities` from *invoker*,
    defaulting to fully-unverified when the Invoker does not expose the
    property.
    """
    caps = getattr(invoker, "usage_capabilities", None)
    if isinstance(caps, UsageVerificationCapabilities):
        return caps
    return _UNVERIFIED_CAPABILITIES


# ---------------------------------------------------------------------------
# R5 P0-5 / R8 P0-2 / R9 Section 8: VerifiedUsage (Choice A)
# ---------------------------------------------------------------------------


class VerifiedUsage(StrictContract):
    """Result of a Provider Usage Verifier check.

    R9 Section 8 (Choice A): the ``token_source_id`` / ``cost_source_id``
    fields have been REMOVED.  The Invoker uses the Verifier's frozen
    ``source_id`` (from :attr:`ProviderUsageVerifier.source_id`) for
    both dimensions.  This eliminates the unused per-dimension source
    fields that R8 added but the Invoker never consumed.

    R8 P0-2: the single ``verified: bool`` field is DEPRECATED and
    auto-derived as ``tokens_verified or cost_verified``.

    Invariants:

    * ``tokens_verified=True`` → ``tokens_used is not None``
    * ``cost_verified=True`` → ``cost_usd is not None``
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_verified: bool = False
    cost_verified: bool = False
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    # DEPRECATED: auto-derived as ``tokens_verified or cost_verified``.
    verified: bool = False

    @model_validator(mode="after")
    def _enforce_per_dimension_invariants(self) -> "VerifiedUsage":
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
    """Authoritative Provider Usage verification boundary.

    R9 Section 8 (Choice A): the Verifier exposes a single frozen
    ``source_id``.  The Invoker binds this ``source_id`` to both the
    Token and Cost dimensions in its
    :class:`UsageVerificationCapabilities` — there are no per-dimension
    source ids on :class:`VerifiedUsage`.
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


__all__ = [
    "AttemptUsageDisposition",
    "AttemptUsageRecord",
    "ProviderUsageVerifier",
    "UsageProvenance",
    "UsageTrustLevel",
    "UsageVerificationCapabilities",
    "VerifiedUsage",
    "get_usage_capabilities",
]


# ---------------------------------------------------------------------------
# R9 Section 4: resolve the ``list[AttemptUsageRecord]`` forward reference
# in :class:`multi_agent.contracts.ExecutionUsage`.  This module imports
# ``StrictContract`` from :mod:`multi_agent.contracts` (one-way), so by the
# time this code runs, ``contracts.py`` is fully loaded and
# ``ExecutionUsage`` exists with an unresolved forward reference.  We inject
# ``AttemptUsageRecord`` into the ``contracts`` module namespace and call
# ``model_rebuild()`` so Pydantic can resolve the annotation.
# ---------------------------------------------------------------------------

import multi_agent.contracts as _contracts  # noqa: E402

# Inject ``AttemptUsageRecord`` so Pydantic can resolve the forward
# reference in ``ExecutionUsage.attempt_usage_records``.  ``setattr`` is
# used (rather than direct assignment) to satisfy mypy — the contracts
# module has no static attribute with this name.
setattr(_contracts, "AttemptUsageRecord", AttemptUsageRecord)
_contracts.ExecutionUsage.model_rebuild()
del _contracts
