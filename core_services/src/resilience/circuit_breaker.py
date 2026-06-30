from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


try:
    from prometheus_client import Counter, Gauge, Histogram

    circuit_breaker_state = Gauge(
        "circuit_breaker_state",
        "Circuit breaker state (0=CLOSED, 1=OPEN, 2=HALF_OPEN)",
        labelnames=("breaker", "dependency", "tenant_id"),
    )

    circuit_breaker_transitions_total = Counter(
        "circuit_breaker_transitions_total",
        "Circuit breaker transitions",
        labelnames=("breaker", "dependency", "tenant_id", "from_state", "to_state"),
    )

    circuit_breaker_failures_total = Counter(
        "circuit_breaker_failures_total",
        "Circuit breaker recorded failures",
        labelnames=("breaker", "dependency", "tenant_id"),
    )

    circuit_breaker_success_total = Counter(
        "circuit_breaker_success_total",
        "Circuit breaker recorded successes",
        labelnames=("breaker", "dependency", "tenant_id"),
    )

    recovery_time_seconds = Histogram(
        "recovery_time_seconds",
        "Time from breaker OPEN to successful recovery (seconds)",
        labelnames=("breaker", "dependency", "tenant_id"),
        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
    )
except Exception:
    circuit_breaker_state = None
    circuit_breaker_transitions_total = None
    circuit_breaker_failures_total = None
    circuit_breaker_success_total = None
    recovery_time_seconds = None


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    pass


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 10.0
    half_open_max_calls: int = 1
    success_threshold: int = 1


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        dependency: str,
        config: CircuitBreakerConfig | None = None,
        tenant_id: str | None = None,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.dependency = dependency
        self.tenant_id = tenant_id or ""
        self.config = config or CircuitBreakerConfig()
        self._time = time_fn

        self._state = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._consecutive_failures = 0
        self._half_open_in_flight = 0
        self._half_open_successes = 0
        self._lock = asyncio.Lock()

        self._set_metric_state(self._state)

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        await self._pre_call()
        started = self._time()
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success(started)
            return result

    async def _pre_call(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if self._opened_at is not None and (self._time() - self._opened_at) >= self.config.recovery_timeout_seconds:
                    self._transition(CircuitState.HALF_OPEN)
                else:
                    raise CircuitOpenError(f"{self.name}:{self.dependency} circuit open")

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self.config.half_open_max_calls:
                    raise CircuitOpenError(f"{self.name}:{self.dependency} half-open limited")
                self._half_open_in_flight += 1

    async def _on_failure(self) -> None:
        async with self._lock:
            self._inc_failures()

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes = 0
                self._transition(CircuitState.OPEN)
                self._opened_at = self._time()
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                self._transition(CircuitState.OPEN)
                self._opened_at = self._time()

    async def _on_success(self, started: float) -> None:
        async with self._lock:
            self._inc_success()

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.success_threshold:
                    if self._opened_at is not None:
                        self._observe_recovery(self._time() - self._opened_at)
                    self._transition(CircuitState.CLOSED)
                    self._opened_at = None
                    self._consecutive_failures = 0
                    self._half_open_successes = 0
                return

            self._consecutive_failures = 0

    def _transition(self, to_state: CircuitState) -> None:
        if to_state == self._state:
            return
        from_state = self._state
        self._state = to_state
        self._set_metric_state(to_state)
        self._inc_transition(from_state, to_state)

        if to_state == CircuitState.HALF_OPEN:
            self._half_open_in_flight = 0
            self._half_open_successes = 0

    def _metric_labels(self) -> tuple[str, str, str]:
        return (self.name, self.dependency, self.tenant_id)

    def _set_metric_state(self, state: CircuitState) -> None:
        if not circuit_breaker_state:
            return
        value = 0
        if state == CircuitState.OPEN:
            value = 1
        elif state == CircuitState.HALF_OPEN:
            value = 2
        circuit_breaker_state.labels(*self._metric_labels()).set(value)

    def _inc_transition(self, from_state: CircuitState, to_state: CircuitState) -> None:
        if not circuit_breaker_transitions_total:
            return
        circuit_breaker_transitions_total.labels(
            self.name,
            self.dependency,
            self.tenant_id,
            from_state.value,
            to_state.value,
        ).inc()

    def _inc_failures(self) -> None:
        if circuit_breaker_failures_total:
            circuit_breaker_failures_total.labels(*self._metric_labels()).inc()

    def _inc_success(self) -> None:
        if circuit_breaker_success_total:
            circuit_breaker_success_total.labels(*self._metric_labels()).inc()

    def _observe_recovery(self, seconds: float) -> None:
        if recovery_time_seconds:
            recovery_time_seconds.labels(*self._metric_labels()).observe(max(0.0, seconds))


class TenantCircuitBreakerRegistry:
    def __init__(self, *, name: str, dependency: str, config: CircuitBreakerConfig | None = None):
        self._name = name
        self._dependency = dependency
        self._config = config
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def for_tenant(self, tenant_id: str) -> CircuitBreaker:
        async with self._lock:
            breaker = self._breakers.get(tenant_id)
            if breaker:
                return breaker
            breaker = CircuitBreaker(name=self._name, dependency=self._dependency, tenant_id=tenant_id, config=self._config)
            self._breakers[tenant_id] = breaker
            return breaker

