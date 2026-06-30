from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from prometheus_client import Counter

T = TypeVar("T")


retry_attempts_total = Counter(
    "retry_attempts_total",
    "Retry attempts performed",
    labelnames=("operation", "dependency", "tenant_id"),
)

retry_failures_total = Counter(
    "retry_failures_total",
    "Retry operations that exhausted attempts or elapsed time",
    labelnames=("operation", "dependency", "tenant_id"),
)


class RetryExhaustedError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 5.0
    max_elapsed_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def delay_seconds(self, attempt: int) -> float:
        exp = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt)))
        jitter = exp * self.jitter_ratio
        return max(0.0, min(self.max_delay_seconds, exp + random.uniform(-jitter, jitter)))


def default_retryable(exc: BaseException) -> bool:
    retryable_names = (
        "TimeoutError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "ServerDisconnectedError",
        "OSError",
    )
    return exc.__class__.__name__ in retryable_names


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    is_retryable: Callable[[BaseException], bool] = default_retryable,
    operation: str = "operation",
    dependency: str = "dependency",
    tenant_id: str | None = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    pol = policy or RetryPolicy()
    started = time.monotonic()
    last_exc: BaseException | None = None

    for attempt in range(0, max(0, pol.max_retries) + 1):
        retry_attempts_total.labels(operation, dependency, tenant_id or "").inc()
        try:
            return await fn()
        except BaseException as e:
            last_exc = e
            if not is_retryable(e):
                raise

            elapsed = time.monotonic() - started
            if attempt >= pol.max_retries or elapsed >= pol.max_elapsed_seconds:
                retry_failures_total.labels(operation, dependency, tenant_id or "").inc()
                raise RetryExhaustedError(f"retry exhausted for {operation}:{dependency}") from e

            delay = pol.delay_seconds(attempt)
            if on_retry:
                on_retry(attempt, e, delay)
            await asyncio.sleep(delay)

    retry_failures_total.labels(operation, dependency, tenant_id or "").inc()
    raise RetryExhaustedError(f"retry exhausted for {operation}:{dependency}") from last_exc

