from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


@dataclass(frozen=True)
class MetricsResponse:
    body: bytes
    content_type: str


decision_latency_ms = Histogram(
    "agent_decision_latency_ms",
    "Time spent producing a decision or action",
    labelnames=("agent_id", "action_type", "risk_level", "status"),
    buckets=(10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)

tool_call_count = Counter(
    "agent_tool_call_count_total",
    "Number of tool calls executed by agents",
    labelnames=("agent_id", "tool_name"),
)

chat_latency_ms = Histogram(
    "chat_latency_ms",
    "End-to-end chat latency",
    labelnames=("agent_id", "status"),
    buckets=(10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)

tool_success_total = Counter(
    "tool_success_total",
    "Tool execution outcomes",
    labelnames=("agent_id", "tool_name", "status"),
)

proposal_total = Counter(
    "proposal_total",
    "Proposed write actions",
    labelnames=("agent_id", "entity", "operation"),
)

error_rate = Counter(
    "agent_error_total",
    "Agent errors",
    labelnames=("agent_id", "error_type"),
)

approval_denied_rate = Counter(
    "agent_approval_denied_total",
    "Approvals denied for agent actions",
    labelnames=("agent_id", "action_type"),
)

approval_required_total = Counter(
    "agent_approval_required_total",
    "Approvals required for agent actions",
    labelnames=("agent_id", "action_type"),
)

action_rollback_count = Counter(
    "agent_action_rollback_total",
    "Rollback requests issued for agent actions",
    labelnames=("agent_id", "action_type"),
)

policy_violation_flags = Counter(
    "agent_policy_violations_total",
    "OPA policy violations (denied actions)",
    labelnames=("agent_id", "action_type"),
)

kill_switch_activations = Counter(
    "agent_kill_switch_activations_total",
    "Kill switch blocks triggered",
    labelnames=("scope",),
)

data_governance_violations = Counter(
    "agent_data_governance_violations_total",
    "Agent data governance violations (deleted or forbidden subject access)",
    labelnames=("agent_id", "violation", "subject_type"),
)

agents_running = Gauge(
    "agent_runtime_running",
    "Agent runtime running state",
)

decisions_logged_total = Counter(
    "decisions_logged_total",
    "Decisions persisted to the audit store",
    labelnames=("agent_id", "action_type", "status"),
)

explanation_latency_ms = Histogram(
    "explanation_latency_ms",
    "Latency of explainability lookups",
    labelnames=("status",),
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)

audit_queries_total = Counter(
    "audit_queries_total",
    "Audit queries executed",
    labelnames=("type",),
)

knowledge_drafts_created_total = Counter(
    "knowledge_drafts_created_total",
    "Knowledge drafts created",
    labelnames=("source_type",),
)

knowledge_articles_embedded_total = Counter(
    "knowledge_articles_embedded_total",
    "Knowledge articles embedded into vector store",
    labelnames=("status",),
)

# Dead-letter queue: messages that failed processing and were routed to the DLQ
dlq_routed_total = Counter(
    "agent_dlq_routed_total",
    "Messages routed to the agent dead-letter topic after processing failure",
    labelnames=("topic", "reason"),
)

# Voice / i18n metrics
stt_latency_seconds = Histogram(
    "stt_latency_seconds",
    "Speech-to-text processing latency",
    labelnames=("status",),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

translation_latency_seconds = Histogram(
    "translation_latency_seconds",
    "Translation processing latency",
    labelnames=("direction", "status"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

voice_success_rate = Gauge(
    "voice_success_rate",
    "Rolling success rate for voice transcriptions",
)

language_distribution = Counter(
    "language_distribution_total",
    "Queries by detected language",
    labelnames=("language",),
)


def observe_decision_latency(*, agent_id: str, action_type: str, risk_level: str, status: str, duration_ms: float) -> None:
    decision_latency_ms.labels(agent_id=agent_id, action_type=action_type, risk_level=risk_level, status=status).observe(duration_ms)


def inc_tool_call(*, agent_id: str, tool_name: str) -> None:
    tool_call_count.labels(agent_id=agent_id, tool_name=tool_name).inc()


def observe_chat_latency(*, agent_id: str, status: str, duration_ms: float) -> None:
    chat_latency_ms.labels(agent_id=agent_id, status=status).observe(duration_ms)


def inc_tool_success(*, agent_id: str, tool_name: str, status: str) -> None:
    tool_success_total.labels(agent_id=agent_id, tool_name=tool_name, status=status).inc()


def inc_proposal(*, agent_id: str, entity: str, operation: str) -> None:
    proposal_total.labels(agent_id=agent_id, entity=entity or "unknown", operation=operation or "unknown").inc()


def inc_error(*, agent_id: str, error_type: str) -> None:
    error_rate.labels(agent_id=agent_id, error_type=error_type).inc()


def inc_approval_required(*, agent_id: str, action_type: str) -> None:
    approval_required_total.labels(agent_id=agent_id, action_type=action_type).inc()


def inc_approval_denied(*, agent_id: str, action_type: str) -> None:
    approval_denied_rate.labels(agent_id=agent_id, action_type=action_type).inc()


def inc_policy_violation(*, agent_id: str, action_type: str) -> None:
    policy_violation_flags.labels(agent_id=agent_id, action_type=action_type).inc()


def inc_kill_switch_block(*, scope: Optional[str]) -> None:
    kill_switch_activations.labels(scope=scope or "unknown").inc()


def inc_data_governance_violation(*, agent_id: str, violation: str, subject_type: str) -> None:
    data_governance_violations.labels(agent_id=agent_id, violation=violation, subject_type=subject_type).inc()


def inc_dlq_routed(*, topic: str, reason: str) -> None:
    dlq_routed_total.labels(topic=topic or "unknown", reason=reason or "unknown").inc()


def metrics_response() -> MetricsResponse:
    return MetricsResponse(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
