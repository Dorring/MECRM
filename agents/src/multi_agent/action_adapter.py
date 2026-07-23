"""Phase 5B — Action Adapter Protocol, Registry, and contracts.

The adapter is the ONLY seam between the Governed Executor and the
outside world.  Every adapter MUST implement :class:`ActionAdapter`
and return an :class:`AdapterExecutionOutcome`.  The default
:class:`DeterministicNoopAdapter` produces no side-effects so the
whole stack runs deterministically under CI without any network,
CRM, Kafka, e-mail, or SMS.

Design rules (Phase 5B Section 9)
--------------------------------

* :class:`ExecutionCommand` is frozen and carries the
  :class:`ExecutionAuthorization` plus the frozen Proposal snapshot.
* :class:`AdapterExecutionOutcome` enforces the invariant
  ``SUCCEEDED → executed=True``, ``FAILED → executed=False``,
  ``UNKNOWN → executed=None``.
* :class:`ActionAdapterRegistry` is a mutable builder; once frozen
  via :meth:`freeze_snapshot` the bindings hash is immutable and any
  later ``register`` call does NOT change a previously-issued snapshot.
* ``compute_execution_fingerprint`` produces a stable SHA-256 over the
  full execution identity so the idempotency store can detect a
  replayed command with tampered content.
"""

from __future__ import annotations

from enum import StrEnum
from hmac import compare_digest
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.contracts import StrictContract
from multi_agent.execution_authorization import ExecutionAuthorization, ExecutionStatus
from multi_agent.review_contracts import FrozenJsonValue, freeze_json_value
from multi_agent.serialization import stable_hash

# ---------------------------------------------------------------------------
# Idempotency scope — how aggressively the adapter dedups.
# ---------------------------------------------------------------------------


class IdempotencyScope(StrEnum):
    """How aggressively an adapter deduplicates.

    ``GLOBAL`` — the same ``idempotency_key`` is unique across all
    tenants (adapter enforces tenant-bound keys itself).
    ``TENANT`` — the same key is unique within one tenant (default;
    the store namespaces by ``(tenant_id, key)``).
    ``NONE`` — the adapter is not idempotent; the store still records
    the attempt but cannot safely replay.
    """

    GLOBAL = "global"
    TENANT = "tenant"
    NONE = "none"


# ---------------------------------------------------------------------------
# ExecutionCommand
# ---------------------------------------------------------------------------


class ExecutionCommand(StrictContract):
    """Frozen, hash-stable command handed to an adapter.

    Carries the :class:`ExecutionAuthorization` (single-use) and the
    frozen Proposal snapshot so the adapter has everything it needs
    without re-reading the (mutable) Review state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    authorization: ExecutionAuthorization
    proposal_snapshot_hash: str
    proposal_origin_hash: str
    action_type: str
    adapter_id: str
    adapter_version: str
    dry_run: bool = False
    attempt: int = 1
    timeout_seconds: float = 30.0
    execution_fingerprint: str
    command_hash: str = ""

    @field_validator(
        "command_id",
        "proposal_snapshot_hash",
        "proposal_origin_hash",
        "action_type",
        "adapter_id",
        "adapter_version",
        "execution_fingerprint",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ExecutionCommand identity fields must not be blank")
        return v

    @field_validator("attempt")
    @classmethod
    def _attempt_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("ExecutionCommand.attempt must be >= 1")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("ExecutionCommand.timeout_seconds must be > 0")
        return float(v)

    @model_validator(mode="after")
    def _verify_command_hash(self) -> ExecutionCommand:
        expected = self.compute_hash()
        if not self.command_hash:
            object.__setattr__(self, "command_hash", expected)
        elif not compare_digest(self.command_hash, expected):
            raise ValueError(
                f"ExecutionCommand {self.command_id!r}: command_hash mismatch"
            )
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"command_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.command_hash, self.compute_hash()):
            raise ValueError(
                f"ExecutionCommand {self.command_id!r}: command_hash does not "
                f"match recomputed content"
            )


# ---------------------------------------------------------------------------
# AdapterExecutionOutcome
# ---------------------------------------------------------------------------


class AdapterExecutionOutcome(StrictContract):
    """Frozen outcome returned by an :class:`ActionAdapter`.

    Invariants (Phase 5B Section 9 — fail-closed):

    * ``SUCCEEDED`` → ``executed=True``.
    * ``FAILED`` → ``executed=False``.
    * ``UNKNOWN`` → ``executed=None``.
    * ``CANCELLED`` → ``executed=None``.
    * ``DEDUPLICATED`` → ``executed=True`` (a previous run succeeded
      and the cached result is being returned).

    Any other combination raises :class:`ValueError` at construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    adapter_id: str
    adapter_version: str
    status: ExecutionStatus
    executed: bool | None
    external_reference: str | None = None
    result_payload: FrozenJsonValue = None
    retryable: bool = False
    error_code: str | None = None
    error_message: str | None = None
    adapter_receipt_hash: str = ""

    @field_validator(
        "command_id",
        "adapter_id",
        "adapter_version",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "AdapterExecutionOutcome identity fields must not be blank"
            )
        return v

    @field_validator("result_payload")
    @classmethod
    def _freeze_payload(cls, v: Any) -> Any:
        return freeze_json_value(v)

    @model_validator(mode="after")
    def _verify_status_executed_consistency(self) -> AdapterExecutionOutcome:
        s = self.status
        if s == ExecutionStatus.SUCCEEDED and self.executed is not True:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: SUCCEEDED requires "
                f"executed=True (got {self.executed!r})"
            )
        if s == ExecutionStatus.DRY_RUN_SUCCEEDED and self.executed is not False:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: DRY_RUN_SUCCEEDED "
                f"requires executed=False (got {self.executed!r}) — dry-run "
                f"produces NO real side-effect (P0-1)"
            )
        if s == ExecutionStatus.FAILED and self.executed is not False:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: FAILED requires "
                f"executed=False (got {self.executed!r})"
            )
        if s in (ExecutionStatus.UNKNOWN, ExecutionStatus.CANCELLED) and (
            self.executed is not None
        ):
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: {s.value!r} "
                f"requires executed=None (got {self.executed!r})"
            )
        if s == ExecutionStatus.DEDUPLICATED and self.executed is not True:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: DEDUPLICATED "
                f"requires executed=True (got {self.executed!r})"
            )
        # SUCCEEDED / DRY_RUN_SUCCEEDED MUST NOT carry an error_code.
        if (
            s in (ExecutionStatus.SUCCEEDED, ExecutionStatus.DRY_RUN_SUCCEEDED)
            and self.error_code
        ):
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: {s.value!r} must "
                f"not carry an error_code"
            )
        # Populate the receipt hash.
        expected = self.compute_hash()
        if not self.adapter_receipt_hash:
            object.__setattr__(self, "adapter_receipt_hash", expected)
        elif not compare_digest(self.adapter_receipt_hash, expected):
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: "
                f"adapter_receipt_hash mismatch"
            )
        return self

    def verify_against_command(self, command: ExecutionCommand) -> None:
        """Bind the outcome back to the command that produced it (P0-4).

        Verifies ``command_id``, ``adapter_id``, and ``adapter_version``
        match the command.
        """
        if self.command_id != command.command_id:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: command_id "
                f"mismatch (command {command.command_id!r})"
            )
        if self.adapter_id != command.adapter_id:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: adapter_id "
                f"{self.adapter_id!r} != command {command.adapter_id!r}"
            )
        if self.adapter_version != command.adapter_version:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: adapter_version "
                f"{self.adapter_version!r} != command {command.adapter_version!r}"
            )

    def verify_against_binding(self, binding: ActionAdapterBinding) -> None:
        """Bind the outcome back to the frozen adapter binding (P0-4).

        Verifies ``adapter_id`` and ``adapter_version`` match the
        frozen :class:`ActionAdapterBinding` snapshot.
        """
        if self.adapter_id != binding.adapter_id:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: adapter_id "
                f"{self.adapter_id!r} != binding {binding.adapter_id!r}"
            )
        if self.adapter_version != binding.adapter_version:
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: adapter_version "
                f"{self.adapter_version!r} != binding {binding.adapter_version!r}"
            )

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"adapter_receipt_hash"})

    def verify_integrity(self) -> None:
        if not compare_digest(self.adapter_receipt_hash, self.compute_hash()):
            raise ValueError(
                f"AdapterExecutionOutcome {self.command_id!r}: "
                f"adapter_receipt_hash does not match recomputed content"
            )


# ---------------------------------------------------------------------------
# ActionAdapter Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ActionAdapter(Protocol):
    """Protocol every adapter MUST implement.

    An adapter is the ONLY seam between the Governed Executor and the
    outside world (CRM, Kafka, e-mail, SMS, …).  Adapters MUST be
    deterministic in dry-run mode and MUST surface a definitive
    outcome (never raise silently).
    """

    adapter_id: str
    adapter_version: str
    supported_action_types: frozenset[str]
    supports_dry_run: bool
    retry_safe: bool
    idempotency_scope: IdempotencyScope

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome: ...


# ---------------------------------------------------------------------------
# Registry contracts
# ---------------------------------------------------------------------------


class ActionAdapterBinding(StrictContract):
    """Frozen binding of one ``action_type`` to one adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: str
    adapter_id: str
    adapter_version: str
    supports_dry_run: bool
    retry_safe: bool
    idempotency_scope: IdempotencyScope
    binding_hash: str = ""

    @field_validator("action_type", "adapter_id", "adapter_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ActionAdapterBinding identity fields must not be blank")
        return v

    @model_validator(mode="after")
    def _verify_binding_hash(self) -> ActionAdapterBinding:
        expected = self.compute_hash()
        if not self.binding_hash:
            object.__setattr__(self, "binding_hash", expected)
        elif not compare_digest(self.binding_hash, expected):
            raise ValueError("ActionAdapterBinding.binding_hash mismatch")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"binding_hash"})


class ActionAdapterRegistrySnapshot(StrictContract):
    """Frozen, hash-stable snapshot of the whole adapter registry.

    Built by :meth:`ActionAdapterRegistry.freeze_snapshot`.  The
    ``registry_hash`` is the binding token carried by every
    :class:`ExecutionAuthorization` so the executor detects a registry
    drift between authorisation and execution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_version: str
    bindings: tuple[ActionAdapterBinding, ...] = ()
    registry_hash: str = ""

    @field_validator("registry_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ActionAdapterRegistrySnapshot.registry_version blank")
        return v

    @model_validator(mode="after")
    def _verify_registry_hash(self) -> ActionAdapterRegistrySnapshot:
        expected = self.compute_hash()
        if not self.registry_hash:
            object.__setattr__(self, "registry_hash", expected)
        elif not compare_digest(self.registry_hash, expected):
            raise ValueError("ActionAdapterRegistrySnapshot.registry_hash mismatch")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self, exclude={"registry_hash"})


# ---------------------------------------------------------------------------
# Frozen runtime handle (P0-4)
# ---------------------------------------------------------------------------


class FrozenActionAdapterRegistry:
    """P0-4: frozen snapshot + live adapter instances, captured atomically.

    Built by :meth:`ActionAdapterRegistry.freeze_for_execution`.  The
    frozen registry captures both the metadata snapshot (bindings) AND
    the actual adapter instances in one atomic operation.  After
    freezing, ``register()`` on the live registry does NOT affect this
    frozen handle — the current batch reads only from the frozen
    instances.

    The executor MUST use this instead of accessing
    ``registry._live_adapters`` directly.

    This is a plain Python class (NOT a Pydantic model) with
    ``__slots__`` because it holds :class:`ActionAdapter` protocol
    instances which are not serializable.
    """

    __slots__ = ("_registry_hash", "_runtime", "_snapshot")

    def __init__(
        self,
        snapshot: ActionAdapterRegistrySnapshot,
        runtime_bindings: dict[str, ActionAdapter],
    ) -> None:
        self._snapshot = snapshot
        # Defensive copy — the live registry cannot mutate this.
        self._runtime: dict[str, ActionAdapter] = dict(runtime_bindings)
        self._registry_hash = snapshot.registry_hash

    @property
    def snapshot(self) -> ActionAdapterRegistrySnapshot:
        """The frozen metadata snapshot."""
        return self._snapshot

    @property
    def registry_hash(self) -> str:
        """Shortcut to the snapshot's registry_hash."""
        return self._registry_hash

    def get_adapter(self, adapter_id: str) -> ActionAdapter | None:
        """Return the frozen adapter instance for *adapter_id*, or None.

        This is the ONLY way the executor may obtain an adapter instance.
        After freezing, re-registering an adapter with the same metadata
        on the live registry does NOT change what this method returns.
        """
        return self._runtime.get(adapter_id)

    def get_binding(
        self,
        action_type: str,
    ) -> ActionAdapterBinding:
        """Look up the binding for *action_type* in the frozen snapshot.

        Raises :class:`KeyError` if the action type is not bound.
        """
        at = action_type.strip()
        for binding in self._snapshot.bindings:
            if binding.action_type == at:
                return binding
        raise KeyError(f"action_type {at!r} not bound in frozen registry")

    def verify_adapter_matches_binding(
        self,
        binding: ActionAdapterBinding,
        dry_run: bool,
    ) -> ActionAdapter | None:
        """Verify the frozen adapter matches the frozen binding on every
        safety-relevant field.  Returns the adapter instance or None.

        Checks: adapter_id, adapter_version, supported_action_types,
        supports_dry_run, retry_safe, idempotency_scope.
        """
        adapter = self._runtime.get(binding.adapter_id)
        if adapter is None:
            return None
        if adapter.adapter_id != binding.adapter_id:
            return None
        if adapter.adapter_version != binding.adapter_version:
            return None
        if binding.action_type not in adapter.supported_action_types:
            return None
        if dry_run and not adapter.supports_dry_run:
            return None
        if adapter.retry_safe != binding.retry_safe:
            return None
        if adapter.idempotency_scope != binding.idempotency_scope:
            return None
        return adapter


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


class ActionAdapterRegistry:
    """Mutable builder for :class:`ActionAdapterRegistrySnapshot`.

    ``register`` adds (or replaces) a binding for one ``action_type``;
    each ``action_type`` may be bound to at most ONE adapter (Phase 5B
    Section 9 — no ambiguous routing).  :meth:`freeze_snapshot`
    returns an immutable snapshot whose ``registry_hash`` captures the
    full binding set.
    """

    def __init__(self, *, registry_version: str = "ma-05b.adapters.1.0") -> None:
        self._registry_version = registry_version.strip()
        if not self._registry_version:
            raise ValueError("registry_version must not be blank")
        self._bindings: dict[str, ActionAdapterBinding] = {}
        # Live adapter instances keyed by adapter_id — used by the
        # GovernedExecutor to look up the adapter for a binding.  This
        # is NOT a global sink: it lives on the registry instance the
        # caller constructed and injected.
        self._live_adapters: dict[str, ActionAdapter] = {}

    def register(
        self,
        adapter: ActionAdapter,
    ) -> ActionAdapterBinding:
        """Register *adapter* for every action type it supports.

        Returns the first binding created.  Re-registering the same
        ``(adapter_id, adapter_version)`` for an action type replaces
        the prior binding (so an upgrade takes effect for FUTURE
        snapshots; previously-frozen snapshots are unaffected).
        """
        if not adapter.supported_action_types:
            raise ValueError(f"Adapter {adapter.adapter_id!r} supports no action types")
        # Keep the live instance so the executor can look it up.
        self._live_adapters[adapter.adapter_id] = adapter
        binding = ActionAdapterBinding(
            action_type=next(iter(adapter.supported_action_types)),
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            supports_dry_run=adapter.supports_dry_run,
            retry_safe=adapter.retry_safe,
            idempotency_scope=adapter.idempotency_scope,
        )
        for action_type in adapter.supported_action_types:
            at = action_type.strip()
            if not at:
                raise ValueError("action_type must not be blank")
            self._bindings[at] = ActionAdapterBinding(
                action_type=at,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
                supports_dry_run=adapter.supports_dry_run,
                retry_safe=adapter.retry_safe,
                idempotency_scope=adapter.idempotency_scope,
            )
        return binding

    def freeze_snapshot(self) -> ActionAdapterRegistrySnapshot:
        """Return an immutable snapshot of the current bindings."""
        bindings = tuple(self._bindings[k] for k in sorted(self._bindings))
        return ActionAdapterRegistrySnapshot(
            registry_version=self._registry_version,
            bindings=bindings,
        )

    def freeze_for_execution(self) -> FrozenActionAdapterRegistry:
        """P0-4: atomically freeze the snapshot AND the live adapter instances.

        After this call, ``register()`` on the live registry does NOT
        affect the returned frozen handle — the current batch reads only
        from the frozen instances.
        """
        snapshot = self.freeze_snapshot()
        # Copy the live adapters dict so later register() calls don't
        # mutate what the frozen handle sees.
        return FrozenActionAdapterRegistry(
            snapshot=snapshot,
            runtime_bindings=dict(self._live_adapters),
        )

    def get_binding(
        self,
        action_type: str,
        snapshot: ActionAdapterRegistrySnapshot,
    ) -> ActionAdapterBinding:
        """Look up the binding for *action_type* in *snapshot*.

        Raises :class:`KeyError` if the action type is not bound —
        callers translate this into ``ACTION_NOT_SUPPORTED``.
        """
        at = action_type.strip()
        for binding in snapshot.bindings:
            if binding.action_type == at:
                return binding
        raise KeyError(f"action_type {at!r} not bound in registry snapshot")


def build_default_registry_snapshot() -> ActionAdapterRegistrySnapshot:
    """Return the default snapshot binding the :class:`DeterministicNoopAdapter`
    to every action type in the governance registry.

    The default registry contains NO live adapter — only the noop.
    Production wiring is a deployment concern (Phase 5C).
    """
    registry = build_default_registry()
    return registry.freeze_snapshot()


def build_default_registry() -> ActionAdapterRegistry:
    """Return a registry with the :class:`DeterministicNoopAdapter`
    bound to every action type in the governance registry.

    The returned registry holds the live noop instance so the
    GovernedExecutor can look it up.  Production wiring is a deployment
    concern (Phase 5C).
    """
    from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY

    registry = ActionAdapterRegistry()
    noop = DeterministicNoopAdapter(
        supported_action_types=frozenset(ACTION_GOVERNANCE_REGISTRY),
    )
    registry.register(noop)
    return registry


# ---------------------------------------------------------------------------
# Execution fingerprint
# ---------------------------------------------------------------------------


def compute_execution_fingerprint(
    *,
    tenant_id: str,
    proposal_id: str,
    proposal_snapshot_hash: str,
    proposal_origin_hash: str,
    action_type: str,
    canonical_payload: Any,
    adapter_id: str,
    adapter_version: str,
    authorization_hash: str,
    governance_spec_hash: str,
    registry_hash: str,
    idempotency_key: str,
    dry_run: bool,
) -> str:
    """Stable SHA-256 over the full execution identity.

    The fingerprint captures every field that distinguishes one
    execution from another so the idempotency store can detect a
    replayed ``idempotency_key`` with tampered content (Phase 5B
    Section 11 — same key + different fingerprint → conflict).
    """
    return stable_hash(
        {
            "tenant_id": tenant_id,
            "proposal_id": proposal_id,
            "proposal_snapshot_hash": proposal_snapshot_hash,
            "proposal_origin_hash": proposal_origin_hash,
            "action_type": action_type,
            "canonical_payload": freeze_json_value(canonical_payload),
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "authorization_hash": authorization_hash,
            "governance_spec_hash": governance_spec_hash,
            "registry_hash": registry_hash,
            "idempotency_key": idempotency_key,
            "dry_run": dry_run,
        }
    )


# ---------------------------------------------------------------------------
# DeterministicNoopAdapter
# ---------------------------------------------------------------------------


class DeterministicNoopAdapter:
    """Adapter that produces NO side-effect (P0-1).

    Only accepts ``dry_run=True`` commands.  Returns
    ``DRY_RUN_SUCCEEDED`` with ``executed=False`` — this is NEVER
    equivalent to ``SUCCEEDED`` and MUST NOT be counted as real
    execution.

    A ``dry_run=False`` command is rejected with ``NOT_AUTHORIZED``
    because the noop cannot produce a real side-effect.  Production
    execution MUST inject a non-Noop adapter and explicitly set
    ``dry_run=False``.

    ``supports_dry_run`` is True; ``retry_safe`` is True (the noop is
    safely idempotent); ``idempotency_scope`` is TENANT.
    """

    adapter_id: str = "noop-adapter"
    adapter_version: str = "1.0.0"
    supported_action_types: frozenset[str] = frozenset()
    supports_dry_run: bool = True
    retry_safe: bool = True
    idempotency_scope: IdempotencyScope = IdempotencyScope.TENANT

    def __init__(self, *, supported_action_types: frozenset[str] | None = None) -> None:
        if supported_action_types is not None:
            self.supported_action_types = frozenset(supported_action_types)

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        if not command.dry_run:
            # P0-1: Noop MUST NOT claim real execution.  A dry_run=False
            # command targeting the noop is fail-closed NOT_AUTHORIZED.
            return AdapterExecutionOutcome(
                command_id=command.command_id,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                status=ExecutionStatus.NOT_AUTHORIZED,
                executed=False,
                external_reference=None,
                result_payload={
                    "noop": True,
                    "rejected": True,
                    "reason": "noop adapter only accepts dry_run=True",
                },
                retryable=False,
                error_code="execution_not_authorized",
                error_message=(
                    "DeterministicNoopAdapter only accepts dry_run=True "
                    "commands; real execution requires a non-Noop adapter"
                ),
            )
        return AdapterExecutionOutcome(
            command_id=command.command_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=ExecutionStatus.DRY_RUN_SUCCEEDED,
            executed=False,
            external_reference=f"noop-dry-{command.command_id}",
            result_payload={
                "noop": True,
                "dry_run": True,
                "command_id": command.command_id,
            },
            retryable=False,
        )


class RecordingActionAdapter:
    """Test adapter that records every command into an injected list.

    Configurable to return a success / failure / timeout (UNKNOWN) /
    cancellation outcome so tests can exercise every branch of the
    Governed Executor without touching real infrastructure.

    The injected ``sink`` is the ONLY mutable state owned by the
    caller — there is NO global sink (Phase 5B constraint).
    """

    adapter_id: str = "recording-adapter"
    adapter_version: str = "1.0.0"
    supports_dry_run: bool = True
    retry_safe: bool = True
    idempotency_scope: IdempotencyScope = IdempotencyScope.TENANT

    def __init__(
        self,
        *,
        sink: list[ExecutionCommand],
        supported_action_types: frozenset[str] | None = None,
        outcome_status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
        error_code: str | None = None,
        error_message: str | None = None,
        external_reference: str | None = None,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
    ) -> None:
        self._sink = sink
        self.supported_action_types = frozenset(supported_action_types or set())
        self._outcome_status = outcome_status
        self._error_code = error_code
        self._error_message = error_message
        self._external_reference = external_reference
        if adapter_id is not None:
            self.adapter_id = adapter_id
        if adapter_version is not None:
            self.adapter_version = adapter_version

    async def execute(self, command: ExecutionCommand) -> AdapterExecutionOutcome:
        # Record a defensive copy of the command id so later mutation
        # of the sink does not affect the receipt.
        self._sink.append(command)
        status = self._outcome_status
        # P0-1: dry_run=True with SUCCEEDED → DRY_RUN_SUCCEEDED (executed=False).
        if command.dry_run and status == ExecutionStatus.SUCCEEDED:
            status = ExecutionStatus.DRY_RUN_SUCCEEDED
            executed = False
        elif status == ExecutionStatus.SUCCEEDED:
            executed = True
        elif status == ExecutionStatus.FAILED:
            executed = False
        else:
            executed = None
        return AdapterExecutionOutcome(
            command_id=command.command_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=status,
            executed=executed,
            external_reference=self._external_reference or f"rec-{command.command_id}",
            result_payload={
                "recorded": True,
                "command_id": command.command_id,
                "dry_run": command.dry_run,
            },
            retryable=False,
            error_code=self._error_code,
            error_message=self._error_message,
        )


__all__ = [
    "ActionAdapter",
    "ActionAdapterBinding",
    "ActionAdapterRegistry",
    "ActionAdapterRegistrySnapshot",
    "AdapterExecutionOutcome",
    "DeterministicNoopAdapter",
    "ExecutionCommand",
    "FrozenActionAdapterRegistry",
    "IdempotencyScope",
    "RecordingActionAdapter",
    "build_default_registry",
    "build_default_registry_snapshot",
    "compute_execution_fingerprint",
]
