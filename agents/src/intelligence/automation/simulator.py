from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

import asyncpg
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings
from .workflow_compiler import compile_workflow
from .rule_parser import WorkflowSpec


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


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
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
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
        if actual is None:
            return False
        return str(value) in str(actual)
    if op == "in":
        try:
            return actual in value
        except Exception:
            return False
    return False


class AutomationSimulationAgent(BaseAgent):
    def __init__(self):
        super().__init__(agent_id="automation-simulation-agent", agent_type="automation", capabilities=["automations:simulate"])
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()

    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "skipped"}

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool:
            return self._pool
        async with self._pool_lock:
            if self._pool:
                return self._pool
            self._pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=5)
            return self._pool

    async def handle_simulation_request(self, event: Dict[str, Any]) -> Dict[str, Any]:
        tenant_id = str(event.get("tenantid") or "")
        data = event.get("data") or {}
        if not tenant_id or not isinstance(data, dict):
            return {"status": "skipped"}

        policy_id = str(data.get("policy_id") or data.get("policyId") or "")
        if not policy_id:
            return {"status": "skipped"}

        from_ts = _parse_ts(data.get("from_ts") or data.get("fromTs"))
        to_ts = _parse_ts(data.get("to_ts") or data.get("toTs"))
        if not to_ts:
            to_ts = datetime.now(timezone.utc)
        if not from_ts:
            from_ts = to_ts - timedelta(days=30)
        if from_ts > to_ts:
            from_ts, to_ts = to_ts, from_ts

        correlation_id = event.get("correlationid")
        simulation_id = str(uuid4())

        with tracer.start_as_current_span("simulation_run") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("policy_id", policy_id)

            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                    row = await conn.fetchrow(
                        "SELECT workflow_json, compiled_json, trigger_type FROM automation_policies WHERE tenant_id = $1::uuid AND id = $2::uuid",
                        tenant_id,
                        policy_id,
                    )
                    if not row:
                        await self.emit_event(
                            topic="crm.automation.simulation.result",
                            event_type="crm.automation.simulation.result",
                            tenant_id=tenant_id,
                            correlation_id=correlation_id,
                            data={
                                "simulation_id": simulation_id,
                                "policy_id": policy_id,
                                "status": "not_found",
                                "result": {"policy_id": policy_id, "would_have_triggered": 0, "sample_actions": [], "estimated_impact": {}, "warnings": ["policy_not_found"]},
                                "from_ts": from_ts.isoformat(),
                                "to_ts": to_ts.isoformat(),
                            },
                        )
                        return {"status": "completed", "simulated": 0}

                    compiled = row.get("compiled_json")
                    trigger_type = str(row.get("trigger_type") or "customer_updated")
                    if isinstance(compiled, str):
                        compiled = json.loads(compiled)
                    if not isinstance(compiled, dict):
                        workflow = row.get("workflow_json")
                        if isinstance(workflow, str):
                            workflow = json.loads(workflow)
                        wf = WorkflowSpec.model_validate(workflow) if isinstance(workflow, dict) else WorkflowSpec(trigger=trigger_type)
                        compiled = compile_workflow(wf).to_dict()

                    trigger_topics = compiled.get("trigger_topics") or []
                    if not isinstance(trigger_topics, list):
                        trigger_topics = []

                    rows = await conn.fetch(
                        """
                        SELECT event_id::text as event_id, kafka_topic, ts, payload
                        FROM event_log
                        WHERE tenant_id = $1::uuid
                          AND ts >= $2
                          AND ts <= $3
                          AND kafka_topic = ANY($4::text[])
                        ORDER BY ts ASC
                        """,
                        tenant_id,
                        from_ts,
                        to_ts,
                        trigger_topics,
                    )

                    matched = 0
                    sample_actions: list[dict[str, Any]] = []
                    impact = {"tasks_created": 0, "notifications": 0, "followups": 0}
                    conditions = compiled.get("conditions") or []
                    actions = compiled.get("actions") or []
                    if not isinstance(conditions, list):
                        conditions = []
                    if not isinstance(actions, list):
                        actions = []

                    for r in rows:
                        payload = r.get("payload") or {}
                        if not isinstance(payload, dict):
                            continue
                        ok = True
                        for c in conditions:
                            if not isinstance(c, dict) or not _eval_condition(payload, c):
                                ok = False
                                break
                        if not ok:
                            continue
                        matched += 1
                        for a in actions:
                            if not isinstance(a, dict):
                                continue
                            typ = str(a.get("type") or "")
                            out = {"type": typ, "policy_id": policy_id, "event_id": r.get("event_id"), "trigger_type": trigger_type}
                            if typ == "notify":
                                impact["notifications"] += 1
                                out["role"] = a.get("role")
                                out["message"] = a.get("message")
                            elif typ == "create_task":
                                impact["tasks_created"] += 1
                                out["task"] = a.get("task")
                                out["assignee_role"] = a.get("assignee_role")
                                out["priority"] = a.get("priority")
                            elif typ == "propose_followup":
                                impact["followups"] += 1
                                out["entity_type"] = a.get("entity_type")
                                out["note"] = a.get("note")
                            if len(sample_actions) < 10:
                                sample_actions.append(out)

                    result = {
                        "policy_id": policy_id,
                        "would_have_triggered": matched,
                        "sample_actions": sample_actions,
                        "estimated_impact": impact,
                        "warnings": compiled.get("warnings") or [],
                    }

                    await self.emit_event(
                        topic="crm.automation.simulation.result",
                        event_type="crm.automation.simulation.result",
                        tenant_id=tenant_id,
                        correlation_id=correlation_id,
                        data={
                            "simulation_id": simulation_id,
                            "policy_id": policy_id,
                            "status": "completed",
                            "result": result,
                            "from_ts": from_ts.isoformat(),
                            "to_ts": to_ts.isoformat(),
                        },
                    )

                    return {"status": "completed", "simulated": len(rows), "matched": matched}

    async def cleanup(self):
        if self._pool:
            await self._pool.close()
        self._pool = None
        await super().cleanup()

