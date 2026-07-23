"""Phase 5B — Action Adapter Registry tests.

Covers (Phase 5B Section 33):

* Registry snapshot hash is frozen — a later ``register`` call must
  NOT change a previously-issued snapshot.
* Each ``action_type`` may be bound to at most ONE adapter (no
  ambiguous routing).
* Adapter version mismatch is detected via binding comparison.
* Action not supported by the registry raises ``KeyError`` (caller
  maps to ``ACTION_NOT_SUPPORTED``).
* The default registry snapshot binds the DeterministicNoopAdapter
  to every action type in the governance registry.
* The default registry contains NO live production adapter (only noop).
* ``compute_execution_fingerprint`` is stable for identical inputs and
  differs when any field changes.
* ``AdapterExecutionOutcome`` enforces the status ↔ executed invariant.
* ``ExecutionCommand`` is frozen, hash-stable, and tamper-detecting.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from multi_agent.action_adapter import (
    ActionAdapterBinding,
    ActionAdapterRegistry,
    ActionAdapterRegistrySnapshot,
    AdapterExecutionOutcome,
    DeterministicNoopAdapter,
    ExecutionCommand,
    IdempotencyScope,
    RecordingActionAdapter,
    build_default_registry,
    build_default_registry_snapshot,
    compute_execution_fingerprint,
)
from multi_agent.execution_authorization import ExecutionAuthorization, ExecutionStatus

from phase5b_helpers import TENANT, RUN_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_authorization(
    *,
    proposal_id: str = "prop-001",
    idempotency_key: str = "idem-001",
) -> ExecutionAuthorization:
    return ExecutionAuthorization(
        authorization_id="auth-001",
        tenant_id=TENANT,
        run_id=RUN_ID,
        proposal_id=proposal_id,
        action_type="report.generate",
        review_request_hash="r" * 64,
        review_result_hash="s" * 64,
        proposal_review_hash="p" * 64,
        proposal_snapshot_hash="snap" + "0" * 60,
        proposal_origin_hash="orig" + "0" * 60,
        governance_spec_hash="g" * 64,
        adapter_registry_hash="reg" + "0" * 60,
        status=ExecutionStatus.READY,
        approval_required=False,
        idempotency_key=idempotency_key,
    )


def _make_command(
    *,
    command_id: str = "cmd-001",
    authorization: ExecutionAuthorization | None = None,
    adapter_id: str = "noop-adapter",
    adapter_version: str = "1.0.0",
) -> ExecutionCommand:
    auth = authorization or _make_authorization()
    return ExecutionCommand(
        command_id=command_id,
        authorization=auth,
        proposal_snapshot_hash=auth.proposal_snapshot_hash,
        proposal_origin_hash=auth.proposal_origin_hash,
        action_type=auth.action_type,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        execution_fingerprint="fp" + "0" * 62,
    )


# ---------------------------------------------------------------------------
# AdapterExecutionOutcome invariants
# ---------------------------------------------------------------------------


class TestAdapterExecutionOutcomeInvariants:
    def test_succeeded_requires_executed_true(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c1",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.SUCCEEDED,
                executed=False,
            )

    def test_failed_requires_executed_false(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c2",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.FAILED,
                executed=True,
            )

    def test_unknown_requires_executed_none(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c3",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.UNKNOWN,
                executed=True,
            )

    def test_cancelled_requires_executed_none(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c4",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.CANCELLED,
                executed=False,
            )

    def test_deduplicated_requires_executed_true(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c5",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.DEDUPLICATED,
                executed=False,
            )

    def test_succeeded_must_not_carry_error_code(self) -> None:
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome(
                command_id="c6",
                adapter_id="a1",
                adapter_version="1.0.0",
                status=ExecutionStatus.SUCCEEDED,
                executed=True,
                error_code="should_not_be_here",
            )

    def test_valid_succeeded_outcome(self) -> None:
        o = AdapterExecutionOutcome(
            command_id="c7",
            adapter_id="a1",
            adapter_version="1.0.0",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
            external_reference="ext-001",
        )
        assert o.executed is True
        assert len(o.adapter_receipt_hash) == 64

    def test_outcome_hash_tamper_detected(self) -> None:
        o = AdapterExecutionOutcome(
            command_id="c8",
            adapter_id="a1",
            adapter_version="1.0.0",
            status=ExecutionStatus.SUCCEEDED,
            executed=True,
        )
        dumped = o.model_dump(mode="python")
        dumped["adapter_receipt_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            AdapterExecutionOutcome.model_validate(dumped)


# ---------------------------------------------------------------------------
# ExecutionCommand tests
# ---------------------------------------------------------------------------


class TestExecutionCommand:
    def test_command_is_frozen(self) -> None:
        c = _make_command()
        with pytest.raises((ValidationError, TypeError)):
            c.command_id = "mutated"  # type: ignore[misc]

    def test_command_hash_is_stable(self) -> None:
        c1 = _make_command()
        c2 = _make_command()
        assert c1.command_hash == c2.command_hash

    def test_command_hash_tamper_detected(self) -> None:
        c = _make_command()
        dumped = c.model_dump(mode="python")
        dumped["command_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ExecutionCommand.model_validate(dumped)

    def test_attempt_must_be_positive(self) -> None:
        c = _make_command()
        dumped = c.model_dump(mode="python")
        dumped["attempt"] = 0
        dumped["command_hash"] = ""  # let validator recompute
        with pytest.raises(ValidationError):
            ExecutionCommand.model_validate(dumped)

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionCommand(
                command_id="cmd-t",
                authorization=_make_authorization(),
                proposal_snapshot_hash="snap" + "0" * 60,
                proposal_origin_hash="orig" + "0" * 60,
                action_type="report.generate",
                adapter_id="noop-adapter",
                adapter_version="1.0.0",
                timeout_seconds=0.0,
                execution_fingerprint="fp" + "0" * 62,
            )


# ---------------------------------------------------------------------------
# ActionAdapterRegistry tests
# ---------------------------------------------------------------------------


class TestRegistrySnapshotFrozen:
    """A previously-frozen snapshot must NOT change on later register."""

    def test_later_register_does_not_affect_frozen_snapshot(self) -> None:
        registry = ActionAdapterRegistry()
        adapter1 = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter1)
        snap1 = registry.freeze_snapshot()
        hash1 = snap1.registry_hash

        # Register a second adapter — the previous snapshot is unaffected.
        adapter2 = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"summary.compile"}),
            adapter_id="recording-adapter-2",
        )
        registry.register(adapter2)
        snap2 = registry.freeze_snapshot()
        # The new snapshot has a different hash (more bindings).
        assert snap2.registry_hash != hash1
        # But the previously-frozen snapshot's hash is unchanged.
        assert snap1.registry_hash == hash1

    def test_snapshot_hash_covers_bindings(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        snap1 = registry.freeze_snapshot()
        # New registry with the same binding → same hash.
        registry2 = ActionAdapterRegistry()
        adapter2 = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry2.register(adapter2)
        snap2 = registry2.freeze_snapshot()
        assert snap1.registry_hash == snap2.registry_hash

    def test_snapshot_hash_tamper_detected(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        snap = registry.freeze_snapshot()
        dumped = snap.model_dump(mode="python")
        dumped["registry_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ActionAdapterRegistrySnapshot.model_validate(dumped)


class TestActionTypeUniqueBinding:
    """Each action_type may be bound to at most ONE adapter."""

    def test_re_register_replaces_binding(self) -> None:
        registry = ActionAdapterRegistry()
        adapter1 = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
            adapter_id="adapter-v1",
            adapter_version="1.0.0",
        )
        registry.register(adapter1)
        snap1 = registry.freeze_snapshot()

        # Re-register the same action_type with a new adapter version.
        adapter2 = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
            adapter_id="adapter-v2",
            adapter_version="2.0.0",
        )
        registry.register(adapter2)
        snap2 = registry.freeze_snapshot()

        # The hash MUST differ — the binding changed.
        assert snap1.registry_hash != snap2.registry_hash

        # The new snapshot has exactly one binding for report.generate.
        bindings = [b for b in snap2.bindings if b.action_type == "report.generate"]
        assert len(bindings) == 1
        assert bindings[0].adapter_id == "adapter-v2"

    def test_one_action_type_per_binding(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate", "summary.compile"}),
        )
        registry.register(adapter)
        snap = registry.freeze_snapshot()
        # Each binding row maps to exactly one action_type.
        assert all(b.action_type for b in snap.bindings)
        # Both action types are present.
        types = {b.action_type for b in snap.bindings}
        assert types == {"report.generate", "summary.compile"}


class TestGetBinding:
    def test_get_binding_returns_match(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        snap = registry.freeze_snapshot()
        binding = registry.get_binding("report.generate", snap)
        assert binding.adapter_id == "recording-adapter"

    def test_action_not_supported_raises_keyerror(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset({"report.generate"}),
        )
        registry.register(adapter)
        snap = registry.freeze_snapshot()
        with pytest.raises(KeyError):
            registry.get_binding("nonexistent.action", snap)


class TestRegistryRejectsEmpty:
    def test_registering_adapter_with_no_action_types_raises(self) -> None:
        registry = ActionAdapterRegistry()
        adapter = RecordingActionAdapter(
            sink=[],
            supported_action_types=frozenset(),
        )
        with pytest.raises(ValueError):
            registry.register(adapter)


# ---------------------------------------------------------------------------
# Default registry tests
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_default_snapshot_binds_noop_to_every_governance_action(
        self,
    ) -> None:
        from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY

        snap = build_default_registry_snapshot()
        bound_types = {b.action_type for b in snap.bindings}
        # Every action in the governance registry is bound.
        for action_type in ACTION_GOVERNANCE_REGISTRY:
            assert action_type in bound_types
        # All bindings point to the noop adapter.
        for binding in snap.bindings:
            assert binding.adapter_id == "noop-adapter"

    def test_default_registry_has_live_noop_adapter(self) -> None:
        """build_default_registry returns a registry with the live noop."""
        registry = build_default_registry()
        live = registry._live_adapters  # type: ignore[attr-defined]
        assert "noop-adapter" in live
        assert isinstance(live["noop-adapter"], DeterministicNoopAdapter)

    def test_default_registry_has_no_live_production_adapter(self) -> None:
        """The default registry contains ONLY the noop adapter — no live
        production adapter (CRM, Kafka, e-mail, SMS)."""
        registry = build_default_registry()
        live = registry._live_adapters  # type: ignore[attr-defined]
        assert set(live.keys()) == {"noop-adapter"}

    def test_default_registry_noop_is_registered(self) -> None:
        registry = build_default_registry()
        snap = registry.freeze_snapshot()
        # get_binding works for at least one governance action type.
        from multi_agent.action_governance import ACTION_GOVERNANCE_REGISTRY

        any_action = next(iter(ACTION_GOVERNANCE_REGISTRY))
        binding = registry.get_binding(any_action, snap)
        assert binding.adapter_id == "noop-adapter"


# ---------------------------------------------------------------------------
# compute_execution_fingerprint tests
# ---------------------------------------------------------------------------


class TestComputeExecutionFingerprint:
    _BASE = dict(
        tenant_id=TENANT,
        proposal_id="prop-001",
        proposal_snapshot_hash="snap" + "0" * 60,
        proposal_origin_hash="orig" + "0" * 60,
        action_type="report.generate",
        canonical_payload={"k": "v"},
        adapter_id="noop-adapter",
        adapter_version="1.0.0",
        authorization_hash="a" * 64,
        governance_spec_hash="g" * 64,
        registry_hash="reg" + "0" * 60,
        idempotency_key="idem-001",
        dry_run=False,
    )

    def test_identical_inputs_yield_identical_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        fp2 = compute_execution_fingerprint(**self._BASE)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_different_proposal_id_changes_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        kwargs = dict(self._BASE)
        kwargs["proposal_id"] = "prop-002"
        fp2 = compute_execution_fingerprint(**kwargs)
        assert fp1 != fp2

    def test_different_payload_changes_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        kwargs = dict(self._BASE)
        kwargs["canonical_payload"] = {"k": "different"}
        fp2 = compute_execution_fingerprint(**kwargs)
        assert fp1 != fp2

    def test_different_dry_run_changes_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        kwargs = dict(self._BASE)
        kwargs["dry_run"] = True
        fp2 = compute_execution_fingerprint(**kwargs)
        assert fp1 != fp2

    def test_different_authorization_hash_changes_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        kwargs = dict(self._BASE)
        kwargs["authorization_hash"] = "b" * 64
        fp2 = compute_execution_fingerprint(**kwargs)
        assert fp1 != fp2

    def test_different_adapter_id_changes_fingerprint(self) -> None:
        fp1 = compute_execution_fingerprint(**self._BASE)
        kwargs = dict(self._BASE)
        kwargs["adapter_id"] = "different-adapter"
        fp2 = compute_execution_fingerprint(**kwargs)
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# ActionAdapterBinding tests
# ---------------------------------------------------------------------------


class TestActionAdapterBinding:
    def test_binding_hash_stable(self) -> None:
        b1 = ActionAdapterBinding(
            action_type="report.generate",
            adapter_id="noop-adapter",
            adapter_version="1.0.0",
            supports_dry_run=True,
            retry_safe=True,
            idempotency_scope=IdempotencyScope.TENANT,
        )
        b2 = ActionAdapterBinding(
            action_type="report.generate",
            adapter_id="noop-adapter",
            adapter_version="1.0.0",
            supports_dry_run=True,
            retry_safe=True,
            idempotency_scope=IdempotencyScope.TENANT,
        )
        assert b1.binding_hash == b2.binding_hash

    def test_binding_hash_tamper_detected(self) -> None:
        b = ActionAdapterBinding(
            action_type="report.generate",
            adapter_id="noop-adapter",
            adapter_version="1.0.0",
            supports_dry_run=True,
            retry_safe=True,
            idempotency_scope=IdempotencyScope.TENANT,
        )
        dumped = b.model_dump(mode="python")
        dumped["binding_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            ActionAdapterBinding.model_validate(dumped)

    def test_binding_blank_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActionAdapterBinding(
                action_type="   ",
                adapter_id="noop-adapter",
                adapter_version="1.0.0",
                supports_dry_run=True,
                retry_safe=True,
                idempotency_scope=IdempotencyScope.TENANT,
            )


# ---------------------------------------------------------------------------
# DeterministicNoopAdapter behaviour
# ---------------------------------------------------------------------------


class TestDeterministicNoopAdapter:
    def test_noop_returns_succeeded(self) -> None:
        import asyncio

        adapter = DeterministicNoopAdapter(
            supported_action_types=frozenset({"report.generate"})
        )
        cmd = _make_command()
        outcome = asyncio.run(adapter.execute(cmd))
        assert outcome.status == ExecutionStatus.SUCCEEDED
        assert outcome.executed is True
        assert outcome.external_reference == f"noop-{cmd.command_id}"

    def test_noop_supports_dry_run(self) -> None:
        adapter = DeterministicNoopAdapter(
            supported_action_types=frozenset({"report.generate"})
        )
        assert adapter.supports_dry_run is True

    def test_noop_is_retry_safe(self) -> None:
        adapter = DeterministicNoopAdapter(
            supported_action_types=frozenset({"report.generate"})
        )
        assert adapter.retry_safe is True

    def test_noop_default_supported_types_empty(self) -> None:
        adapter = DeterministicNoopAdapter()
        assert adapter.supported_action_types == frozenset()
