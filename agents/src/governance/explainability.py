import json
import uuid
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from governance.agent_telemetry import decisions_logged_total, explanation_latency_ms


@dataclass(frozen=True)
class DecisionArtifact:
    id: str
    tenant_id: str
    agent_id: str
    action_type: str
    risk_level: str
    status: str
    confidence: Optional[float]
    input_context: dict[str, Any]
    reasoning: dict[str, Any]
    evidence: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    approval_id: Optional[str] = None
    correlation_id: Optional[str] = None


class ExplainabilityEngine:
    def __init__(self, database_url: str):
        self._database_url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def start(self) -> None:
        if self._pool:
            return
        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
        self._pool = None

    async def record_decision(self, decision: DecisionArtifact) -> str:
        if not self._pool:
            await self.start()
        assert self._pool

        input_context = _redact(decision.input_context)
        reasoning = _redact(decision.reasoning)
        evidence = _redact(decision.evidence)
        tool_calls = _redact(decision.tool_calls)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", decision.tenant_id)
                await conn.execute(
                    """
                    INSERT INTO agent_decisions (
                      id, tenant_id, agent_id, action_type, risk_level, status, confidence,
                      input_context, reasoning, evidence, tool_calls, approval_id, correlation_id, created_at
                    )
                    VALUES (
                      $1::uuid, $2::uuid, $3, $4, $5, $6, $7,
                      $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, $12::uuid, $13::uuid, $14
                    )
                    """,
                    uuid.UUID(decision.id),
                    uuid.UUID(decision.tenant_id),
                    decision.agent_id,
                    decision.action_type,
                    decision.risk_level,
                    decision.status,
                    decision.confidence,
                    json.dumps(input_context),
                    json.dumps(reasoning),
                    json.dumps(evidence),
                    json.dumps(tool_calls),
                    uuid.UUID(decision.approval_id) if decision.approval_id else None,
                    uuid.UUID(decision.correlation_id) if decision.correlation_id else None,
                    datetime.now(tz=timezone.utc).replace(tzinfo=None),
                )
        decisions_logged_total.labels(agent_id=decision.agent_id, action_type=decision.action_type, status=decision.status).inc()
        return decision.id

    async def explain_decision(self, decision_id: str, *, tenant_id: str) -> Optional[dict[str, Any]]:
        started = time.perf_counter()
        if not self._pool:
            await self.start()
        assert self._pool

        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                    row = await conn.fetchrow(
                        """
                        SELECT
                          id::text, tenant_id::text, agent_id, action_type, risk_level, status,
                          confidence, input_context, reasoning, evidence, tool_calls,
                          approval_id::text, correlation_id::text, created_at
                        FROM agent_decisions
                        WHERE tenant_id = $1::uuid AND id = $2::uuid
                        """,
                        uuid.UUID(tenant_id),
                        uuid.UUID(decision_id),
                    )
                    if not row:
                        explanation_latency_ms.labels(status="not_found").observe((time.perf_counter() - started) * 1000.0)
                        return None
                    explanation_latency_ms.labels(status="ok").observe((time.perf_counter() - started) * 1000.0)
                    return dict(row)
        except Exception:
            explanation_latency_ms.labels(status="error").observe((time.perf_counter() - started) * 1000.0)
            raise

    async def get_factors(self, decision_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
        decision = await self.explain_decision(decision_id, tenant_id=tenant_id)
        if not decision:
            return []
        reasoning = decision.get("reasoning") or {}
        factors = reasoning.get("factors")
        return factors if isinstance(factors, list) else []

    async def generate_audit_report(self, agent_id: str, time_range: dict[str, str], *, tenant_id: str) -> dict[str, Any]:
        if not self._pool:
            await self.start()
        assert self._pool

        since = time_range.get("since")
        until = time_range.get("until")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """
                    SELECT
                      id::text, action_type, risk_level, status, confidence, created_at
                    FROM agent_decisions
                    WHERE tenant_id = $1::uuid AND agent_id = $2
                      AND ($3::timestamptz IS NULL OR created_at >= $3::timestamptz)
                      AND ($4::timestamptz IS NULL OR created_at <= $4::timestamptz)
                    ORDER BY created_at DESC
                    LIMIT 500
                    """,
                    uuid.UUID(tenant_id),
                    agent_id,
                    since,
                    until,
                )

        return {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "time_range": time_range,
            "decisions": [dict(r) for r in rows],
        }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if any(s in lk for s in ("token", "password", "secret", "authorization", "apikey", "api_key")):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
