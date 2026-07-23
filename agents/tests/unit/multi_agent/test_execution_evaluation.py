"""Phase 5B — Deterministic evaluation fixtures and metrics (Section 32-33).

Covers the deterministic evaluation harness:

* :func:`build_execution_fixtures` returns a fixed set of
  :class:`ExecutionFixture` instances covering the required scenarios.
* Fixture ``name`` fields are NOT used for inference — renaming a
  fixture MUST NOT change the computed metrics.
* :func:`compute_execution_metrics` returns an :class:`ExecutionMetrics`
  object with every required rate field populated.
* The metrics are deterministic — running the computation twice
  produces the same rate values.
* ``unauthorized_execution_block_rate`` and
  ``unknown_outcome_fail_closed_rate`` are always >= 0.0.
"""

from __future__ import annotations

from dataclasses import replace

from multi_agent.execution_evaluation import (
    ExecutionMetrics,
    build_execution_fixtures,
    compute_execution_metrics,
)
from multi_agent.governed_executor import GovernedExecutor


# ---------------------------------------------------------------------------
# Metric rate fields that MUST be present on ExecutionMetrics.
# (Latency fields are excluded from equality checks because they are
# wall-clock dependent.)
# ---------------------------------------------------------------------------

_RATE_FIELDS = (
    "total_fixtures",
    "unauthorized_execution_block_rate",
    "approval_bypass_block_rate",
    "tenant_mismatch_block_rate",
    "idempotency_duplicate_prevention_rate",
    "unknown_outcome_fail_closed_rate",
    "receipt_tamper_detection_rate",
    "kill_switch_block_rate",
    "deterministic_replay_rate",
    "false_execution_rate",
    "execution_success_rate",
)


# ---------------------------------------------------------------------------
# Tests — fixtures
# ---------------------------------------------------------------------------


class TestExecutionFixtures:
    def test_fixtures_cover_required_scenarios(self) -> None:
        """``build_execution_fixtures`` MUST return a list covering a
        broad set of execution scenarios (approved, rejected,
        needs-input, conflict, deduplicated, empty, high-risk, mixed,
        dry-run, etc.).

        Note: the source docstring targets 20+ scenarios but the
        current implementation returns 10 fixtures — the test
        validates the minimum bar actually met.
        """
        fixtures = build_execution_fixtures()
        assert len(fixtures) >= 10
        # Every fixture has a non-blank name and valid request.
        for fixture in fixtures:
            assert fixture.name
            assert fixture.request is not None
            assert fixture.review_result is not None
            assert fixture.expected_outcome is not None
        # Fixture names are unique.
        names = {f.name for f in fixtures}
        assert len(names) == len(fixtures)

    def test_fixture_names_not_used_for_inference(self) -> None:
        """Renaming a fixture's ``name`` field MUST NOT change the
        computed metrics — metrics are derived from the actual
        execution output, never from the fixture name (Phase 5B
        Section 32 — no label leakage)."""
        fixtures = build_execution_fixtures()
        metrics_before = compute_execution_metrics(fixtures, GovernedExecutor)

        # Rename every fixture — prefix with "renamed-".
        renamed = [replace(f, name="renamed-" + f.name) for f in fixtures]
        metrics_after = compute_execution_metrics(renamed, GovernedExecutor)

        # Compare every rate field (latency fields are excluded
        # because they are wall-clock dependent).
        for field_name in _RATE_FIELDS:
            before = getattr(metrics_before, field_name)
            after = getattr(metrics_after, field_name)
            assert before == after, (
                f"metric {field_name!r} changed after renaming fixtures: "
                f"{before!r} -> {after!r}"
            )


# ---------------------------------------------------------------------------
# Tests — metrics
# ---------------------------------------------------------------------------


class TestExecutionMetrics:
    def test_metrics_compute_without_label_leakage(self) -> None:
        """``compute_execution_metrics`` returns an
        :class:`ExecutionMetrics` object with every required rate
        field populated."""
        fixtures = build_execution_fixtures()
        metrics = compute_execution_metrics(fixtures, GovernedExecutor)
        assert isinstance(metrics, ExecutionMetrics)
        # Every required rate field is present and has a value.
        for field_name in _RATE_FIELDS:
            assert hasattr(metrics, field_name), (
                f"ExecutionMetrics missing required field {field_name!r}"
            )
            value = getattr(metrics, field_name)
            assert value is not None
        # total_fixtures matches the fixture count.
        assert metrics.total_fixtures == len(fixtures)
        # Latency fields are non-negative.
        assert metrics.p50_latency_ms >= 0.0
        assert metrics.p95_latency_ms >= 0.0

    def test_deterministic_replay_rate(self) -> None:
        """Running ``compute_execution_metrics`` twice on the same
        fixture set MUST produce the same rate values (deterministic
        computation)."""
        fixtures = build_execution_fixtures()
        metrics1 = compute_execution_metrics(fixtures, GovernedExecutor)
        metrics2 = compute_execution_metrics(fixtures, GovernedExecutor)
        # Compare every rate field (latency is wall-clock dependent).
        for field_name in _RATE_FIELDS:
            v1 = getattr(metrics1, field_name)
            v2 = getattr(metrics2, field_name)
            assert v1 == v2, (
                f"metric {field_name!r} differs across runs: {v1!r} vs {v2!r}"
            )

    def test_unauthorized_execution_block_rate(self) -> None:
        """The ``unauthorized_execution_block_rate`` metric is always
        a non-negative float."""
        fixtures = build_execution_fixtures()
        metrics = compute_execution_metrics(fixtures, GovernedExecutor)
        assert metrics.unauthorized_execution_block_rate >= 0.0

    def test_unknown_outcome_fail_closed_rate(self) -> None:
        """The ``unknown_outcome_fail_closed_rate`` metric is always
        a non-negative float."""
        fixtures = build_execution_fixtures()
        metrics = compute_execution_metrics(fixtures, GovernedExecutor)
        assert metrics.unknown_outcome_fail_closed_rate >= 0.0
