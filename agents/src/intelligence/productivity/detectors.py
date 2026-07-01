from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class LeadIdleSignal:
    type: str
    lead_id: str
    days_idle: int


@dataclass(frozen=True)
class TicketAgingSignal:
    type: str
    ticket_id: str
    hours_over_sla: int


@dataclass(frozen=True)
class TaskOverdueSignal:
    type: str
    task_id: str
    days_overdue: int


@dataclass(frozen=True)
class FollowupIgnoredSignal:
    type: str
    target_entity: str
    target_id: str
    days_waiting: int


def now_ms() -> int:
    return int(time.time() * 1000)


def days_between_ms(older_ms: int, newer_ms: int) -> int:
    return max(0, int((newer_ms - older_ms) / (1000 * 60 * 60 * 24)))


def hours_between_ms(older_ms: int, newer_ms: int) -> int:
    return max(0, int((newer_ms - older_ms) / (1000 * 60 * 60)))

