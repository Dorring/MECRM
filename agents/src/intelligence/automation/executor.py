from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict
from uuid import uuid4

import asyncpg
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _get_field(payload: dict[str, Any], field: str) -> Any:
    if not field:
        return None
    parts = field.split(".")
    cur: Any = payload
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def _eval_condition(payload: dict[str, Any], cond: dict[str, Any]) -> bool:
    field = str(cond.get("field") or "")
    op = str(cond.get("operator") or "")
    value = cond.get("value")
    actual = _get_field(payload, field)
    if not op:
        return False
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if actual is None or value is None:
        return False
    try:
        if op == ">":
            return float(actual) > float(value)
        if op == ">=":
            return float(actual) >= float(value)
        if op == "<":
            return float(actual) < float(value)
        if op == "<=":
            return float(actual) <= float(value)
    except Exception:
        return False
    if op == "contains":
        return str(value) in str(actual)
    if op == "in":
        try:
            return actual in value
        except Exception:
            return False
    return False


@dataclass(frozen=True)
class _AutomationPolicyRow:
    id: str
    created_by: str
    trigger_type: str
    compiled: dict[str, Any]


class AutomationExecutorAgent(BaseAgent):
    def __init__(self):
        super().__init__(agent_id="automation-executor-agent", agent_type="automation", capabilities=["automations:execute"])
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, list[_AutomationPolicyRow]]] = {}
        self._cache_ttl_seconds = 2.0

    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "skipped"}

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool:
            return self._pool
        async with self._pool_lock:
            if self._pool:
                return self._pool
            self._pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=10)
            return self._pool

    async def _get_active_policies(self, *, tenant_id: str) -> list[_AutomationPolicyRow]:
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached and (now - cached[0]) < self._cache_ttl_seconds:
            return cached[1]

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                rows = await conn.fetch(
                    """
                    SELECT id::text as id, created_by::text as created_by, trigger_type, compiled_json
                    FROM automation_policies
                    WHERE tenant_id = $1::uuid AND status = 'active'
                    """,
                    tenant_id,
                )
        policies: list[_AutomationPolicyRow] = []
        for r in rows:
            compiled = r.get("compiled_json")
            if isinstance(compiled, str):
                try:
                    compiled = json.loads(compiled)
                except Exception:
                    compiled = {}
            if not isinstance(compiled, dict):
                compiled = {}
            policies.append(
                _AutomationPolicyRow(
                    id=str(r.get("id")),
                    created_by=str(r.get("created_by")),
                    trigger_type=str(r.get("trigger_type") or ""),
                    compiled=compiled,
                )
            )
        self._cache[tenant_id] = (now, policies)
        return policies

    async def ingest_trigger_event(self, *, topic: str, event: Dict[str, Any]) -> Dict[str, Any]:
        tenant_id = str(event.get("tenantid") or "")
        if not tenant_id:
            return {"status": "skipped"}
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        with tracer.start_as_current_span("workflow_execution") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("kafka_topic", topic)

            policies = await self._get_active_policies(tenant_id=tenant_id)
            if not policies:
                return {"status": "completed", "executed": 0}

            executed = 0
            for p in policies:
                compiled = p.compiled or {}
                trigger_topics = compiled.get("trigger_topics") or []
                if not isinstance(trigger_topics, list) or topic not in trigger_topics:
                    continue

                conditions = compiled.get("conditions") or []
                if not isinstance(conditions, list):
                    conditions = []
                ok = True
                for c in conditions:
                    if not isinstance(c, dict) or not _eval_condition(data, c):
                        ok = False
                        break
                if not ok:
                    continue

                actions = compiled.get("actions") or []
                if not isinstance(actions, list) or not actions:
                    continue

                execution_id = str(uuid4())
                emitted_actions: list[dict[str, Any]] = []
                for idx, a in enumerate(actions):
                    if not isinstance(a, dict):
                        continue
                    # action_type reserved for future filtering/audit
                    # action_type = str(a.get("type") or "")
                    payload = dict(a)
                    emitted_actions.append(payload)
                    await self.emit_event(
                        topic="crm.automation.action.requested",
                        event_type="crm.automation.action.requested",
                        tenant_id=tenant_id,
                        correlation_id=event.get("correlationid"),
                        data={
                            "execution_id": execution_id,
                            "policy_id": p.id,
                            "policy_created_by": p.created_by,
                            "trigger_type": p.trigger_type,
                            "trigger_event_id": event.get("id"),
                            "kafka_topic": topic,
                            "action_index": idx,
                            "action": payload,
                            "event_data": data,
                        },
                    )

                await self.emit_event(
                    topic="crm.automation.executed",
                    event_type="crm.automation.executed",
                    tenant_id=tenant_id,
                    correlation_id=event.get("correlationid"),
                    data={
                        "execution_id": execution_id,
                        "policy_id": p.id,
                        "trigger_type": p.trigger_type,
                        "trigger_event_id": event.get("id"),
                        "kafka_topic": topic,
                        "actions": emitted_actions,
                        "dry_run": False,
                    },
                )
                executed += 1

            return {"status": "completed", "executed": executed}

    async def cleanup(self):
        if self._pool:
            await self._pool.close()
        self._pool = None
        await super().cleanup()

