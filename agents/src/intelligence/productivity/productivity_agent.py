from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

import asyncpg
import redis.asyncio as redis
import structlog
from opentelemetry import trace

from agents.base import BaseAgent
from orchestrator.config import settings

from .detectors import days_between_ms, hours_between_ms, now_ms
from .graph import ProductivityDeps, ProductivityState, build_productivity_graph


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class ProductivityThresholds:
    lead_idle_days: int = 3
    followup_ignored_days: int = 3
    signal_suppression_hours: int = 24
    scan_interval_seconds: int = 300
    batch_size: int = 200


class ProductivitySignalsAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="productivity-signals-agent",
            agent_type="analytics",
            capabilities=["signals:detect"],
        )
        self._redis: redis.Redis | None = None
        self._scan_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._thresholds = ProductivityThresholds(
            lead_idle_days=int(_env("PRODUCTIVITY_LEAD_IDLE_DAYS", "3")),
            followup_ignored_days=int(_env("PRODUCTIVITY_FOLLOWUP_IGNORED_DAYS", "3")),
            signal_suppression_hours=int(_env("PRODUCTIVITY_SIGNAL_TTL_HOURS", "24")),
            scan_interval_seconds=int(_env("PRODUCTIVITY_SCAN_INTERVAL_SECONDS", "300")),
            batch_size=int(_env("PRODUCTIVITY_SCAN_BATCH_SIZE", "200")),
        )

    async def initialize(self, producer):
        await super().initialize(producer)
        self._redis = redis.from_url(settings.REDIS_URL, decode_responses=False)
        self._stop.clear()
        self._scan_task = asyncio.create_task(self._scan_loop())

    async def cleanup(self):
        self._stop.set()
        if self._scan_task:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._scan_task
        if self._redis:
            aclose = getattr(self._redis, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
            else:
                with contextlib.suppress(Exception):
                    await self._redis.close()
        await super().cleanup()

    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "skipped"}

    async def ingest_event(self, *, topic: str, event: Dict[str, Any]) -> None:
        with tracer.start_as_current_span("signal_detection") as span:
            span.set_attribute("topic", topic)
            await self._ingest_event(topic=topic, event=event)

    async def _ingest_event(self, *, topic: str, event: Dict[str, Any]) -> None:
        tenant_id = str(event.get("tenantid") or "")
        if not tenant_id:
            return
        if not self._redis:
            return
        await self._redis.sadd(_k_active_tenants(), tenant_id.encode("utf-8"))

        et = str(event.get("type") or "")
        data = event.get("data") or {}
        t_ms = now_ms()

        if topic == "crm.analytics.prediction-generated" and isinstance(data, dict):
            entity_type = str(data.get("entity_type") or data.get("entityType") or "")
            entity_id = str(data.get("entity_id") or data.get("entityId") or "")
            prediction_type = str(data.get("prediction_type") or data.get("predictionType") or "")
            risk_level = str(data.get("risk_level") or data.get("riskLevel") or "")
            explanation = str(data.get("explanation") or "")
            probability = data.get("probability")
            try:
                probability_f = float(probability) if probability is not None else 0.0
            except Exception:
                probability_f = 0.0

            if not entity_type or not entity_id or not prediction_type:
                return

            should_emit = False
            if prediction_type in {"churn", "sla", "escalation"} and risk_level in {"yellow", "red"}:
                should_emit = True
            if prediction_type == "conversion" and probability_f >= 0.6:
                should_emit = True
            if not should_emit:
                return

            key = _k_signal_suppress(tenant_id, f"prediction:{prediction_type}:{entity_type}:{entity_id}:{risk_level or 'na'}")
            if not await self._mark_once(key, ttl_seconds=self._thresholds.signal_suppression_hours * 3600):
                return

            await self._emit_signal(
                tenant_id=tenant_id,
                signal={
                    "type": f"prediction_{prediction_type}",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "prediction_type": prediction_type,
                    "probability": probability_f,
                    "risk_level": risk_level,
                    "explanation": explanation,
                },
            )
            return

        if topic.startswith("crm.leads.") and isinstance(data, dict):
            lead_id = str(data.get("leadId") or "")
            if not lead_id:
                return
            await self._redis.zadd(_k_lead_activity(tenant_id), {lead_id: t_ms})
            if "newStatus" in data:
                await self._redis.hset(_k_lead_meta(tenant_id, lead_id), mapping={"status": str(data.get("newStatus") or "")})
            if "assignedTo" in (data.get("changes") or {}):
                await self._redis.hset(_k_lead_meta(tenant_id, lead_id), mapping={"assigned_to": str((data.get("changes") or {}).get("assignedTo") or "")})
            return

        if topic.startswith("crm.tickets.") and isinstance(data, dict):
            ticket_id = str(data.get("ticketId") or "")
            if not ticket_id:
                return
            if et == "crm.tickets.created":
                sla_due_at = data.get("slaDueAt")
                if sla_due_at:
                    due_ms = _parse_dt_ms(str(sla_due_at))
                    if due_ms:
                        await self._redis.zadd(_k_ticket_sla_due(tenant_id), {ticket_id: due_ms})
                        await self._redis.hset(_k_ticket_meta(tenant_id, ticket_id), mapping={"status": "open"})
            if et == "crm.tickets.resolved":
                await self._redis.zrem(_k_ticket_sla_due(tenant_id), ticket_id)
                await self._redis.hset(_k_ticket_meta(tenant_id, ticket_id), mapping={"status": "resolved"})
            if et == "crm.tickets.updated":
                changes = data.get("changes") or {}
                if isinstance(changes, dict) and "status" in changes:
                    status = str(changes.get("status") or "")
                    await self._redis.hset(_k_ticket_meta(tenant_id, ticket_id), mapping={"status": status})
                    if status == "resolved":
                        await self._redis.zrem(_k_ticket_sla_due(tenant_id), ticket_id)
            return

        if topic == "crm.tasks.updated" and isinstance(data, dict):
            task_id = str(data.get("taskId") or data.get("task_id") or "")
            if not task_id:
                return
            due_at = data.get("dueDate") or data.get("due_date")
            if due_at:
                due_ms = _parse_dt_ms(str(due_at))
                if due_ms:
                    await self._redis.zadd(_k_task_due(tenant_id), {task_id: due_ms})
            status = str(data.get("status") or "")
            target_entity = str(data.get("targetEntity") or data.get("target_entity") or "")
            target_id = str(data.get("targetId") or data.get("target_id") or "")
            await self._redis.hset(
                _k_task_meta(tenant_id, task_id),
                mapping={"status": status, "target_entity": target_entity, "target_id": target_id},
            )
            if status in ("done", "completed", "closed", "resolved"):
                await self._redis.zrem(_k_task_due(tenant_id), task_id)
            return

        if topic == "crm.user.activity" and isinstance(data, dict):
            activity_type = str(data.get("activityType") or data.get("type") or "")
            direction = str(data.get("direction") or "")
            target_entity = str(data.get("targetEntity") or data.get("target_entity") or "")
            target_id = str(data.get("targetId") or data.get("target_id") or "")
            if not target_entity or not target_id:
                return
            user_id = str(data.get("userId") or data.get("user_id") or "")
            member = f"{target_entity}:{target_id}:{user_id or 'unknown'}"
            ts = data.get("timestamp") or data.get("ts")
            sent_ms = _parse_dt_ms(str(ts)) if ts else t_ms
            if sent_ms is None:
                return
            if activity_type in ("followup_sent", "email_sent", "whatsapp_sent") or (direction == "outbound" and activity_type == "message"):
                await self._redis.zadd(_k_followup_sent(tenant_id), {member: int(sent_ms)})
                await self._redis.hset(_k_followup_meta(tenant_id, member), mapping={"target_entity": target_entity, "target_id": target_id, "user_id": user_id})
            if activity_type in ("reply_received", "email_reply", "whatsapp_reply") or (direction == "inbound" and activity_type == "message"):
                await self._redis.zrem(_k_followup_sent(tenant_id), member)
            return

    async def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as e:
                logger.warning("productivity.scan_failed", error=str(e))
            await asyncio.sleep(max(5, self._thresholds.scan_interval_seconds))

    async def _scan_once(self) -> None:
        if not self._redis:
            return
        with tracer.start_as_current_span("signal_detection"):
            tenants_raw = await self._redis.smembers(_k_active_tenants())
            tenants = [t.decode("utf-8") if isinstance(t, (bytes, bytearray)) else str(t) for t in (tenants_raw or [])]
            t_ms = now_ms()
            for tenant_id in tenants:
                await self._emit_lead_idle_signals(tenant_id=tenant_id, now_ms=t_ms)
                await self._emit_ticket_aging_signals(tenant_id=tenant_id, now_ms=t_ms)
                await self._emit_task_overdue_signals(tenant_id=tenant_id, now_ms=t_ms)
                await self._emit_followup_ignored_signals(tenant_id=tenant_id, now_ms=t_ms)
            return

    async def _emit_lead_idle_signals(self, *, tenant_id: str, now_ms: int) -> None:
        if not self._redis:
            return
        cutoff_ms = now_ms - self._thresholds.lead_idle_days * 24 * 60 * 60 * 1000
        idle = await self._redis.zrangebyscore(_k_lead_activity(tenant_id), min=0, max=cutoff_ms, start=0, num=self._thresholds.batch_size)
        for lead_id_raw in idle or []:
            lead_id = lead_id_raw.decode("utf-8") if isinstance(lead_id_raw, (bytes, bytearray)) else str(lead_id_raw)
            last_ms = await self._redis.zscore(_k_lead_activity(tenant_id), lead_id)
            if last_ms is None:
                continue
            days_idle = days_between_ms(int(last_ms), now_ms)
            key = _k_signal_suppress(tenant_id, f"lead_idle:{lead_id}")
            if not await self._mark_once(key, ttl_seconds=self._thresholds.signal_suppression_hours * 3600):
                continue
            await self._emit_signal(
                tenant_id=tenant_id,
                signal={"type": "lead_idle", "lead_id": lead_id, "days_idle": days_idle},
            )

    async def _emit_ticket_aging_signals(self, *, tenant_id: str, now_ms: int) -> None:
        if not self._redis:
            return
        overdue = await self._redis.zrangebyscore(_k_ticket_sla_due(tenant_id), min=0, max=now_ms, start=0, num=self._thresholds.batch_size)
        for ticket_id_raw in overdue or []:
            ticket_id = ticket_id_raw.decode("utf-8") if isinstance(ticket_id_raw, (bytes, bytearray)) else str(ticket_id_raw)
            status_raw = await self._redis.hget(_k_ticket_meta(tenant_id, ticket_id), "status")
            status = status_raw.decode("utf-8") if isinstance(status_raw, (bytes, bytearray)) else (str(status_raw) if status_raw else "")
            if status and status != "open":
                continue
            due_ms = await self._redis.zscore(_k_ticket_sla_due(tenant_id), ticket_id)
            if due_ms is None:
                continue
            hours_over = hours_between_ms(int(due_ms), now_ms)
            key = _k_signal_suppress(tenant_id, f"ticket_aging:{ticket_id}")
            if not await self._mark_once(key, ttl_seconds=self._thresholds.signal_suppression_hours * 3600):
                continue
            await self._emit_signal(
                tenant_id=tenant_id,
                signal={"type": "ticket_aging", "ticket_id": ticket_id, "hours_over_sla": hours_over},
            )

    async def _emit_task_overdue_signals(self, *, tenant_id: str, now_ms: int) -> None:
        if not self._redis:
            return
        overdue = await self._redis.zrangebyscore(_k_task_due(tenant_id), min=0, max=now_ms, start=0, num=self._thresholds.batch_size)
        for task_id_raw in overdue or []:
            task_id = task_id_raw.decode("utf-8") if isinstance(task_id_raw, (bytes, bytearray)) else str(task_id_raw)
            status_raw = await self._redis.hget(_k_task_meta(tenant_id, task_id), "status")
            status = status_raw.decode("utf-8") if isinstance(status_raw, (bytes, bytearray)) else (str(status_raw) if status_raw else "")
            if status in ("done", "completed", "closed", "resolved"):
                await self._redis.zrem(_k_task_due(tenant_id), task_id)
                continue
            due_ms = await self._redis.zscore(_k_task_due(tenant_id), task_id)
            if due_ms is None:
                continue
            days_over = days_between_ms(int(due_ms), now_ms)
            key = _k_signal_suppress(tenant_id, f"task_overdue:{task_id}")
            if not await self._mark_once(key, ttl_seconds=self._thresholds.signal_suppression_hours * 3600):
                continue
            await self._emit_signal(
                tenant_id=tenant_id,
                signal={"type": "task_overdue", "task_id": task_id, "days_overdue": days_over},
            )

    async def _emit_followup_ignored_signals(self, *, tenant_id: str, now_ms: int) -> None:
        if not self._redis:
            return
        cutoff_ms = now_ms - self._thresholds.followup_ignored_days * 24 * 60 * 60 * 1000
        waiting = await self._redis.zrangebyscore(_k_followup_sent(tenant_id), min=0, max=cutoff_ms, start=0, num=self._thresholds.batch_size)
        for member_raw in waiting or []:
            member = member_raw.decode("utf-8") if isinstance(member_raw, (bytes, bytearray)) else str(member_raw)
            sent_ms = await self._redis.zscore(_k_followup_sent(tenant_id), member)
            if sent_ms is None:
                continue
            parts = member.split(":")
            target_entity = parts[0] if len(parts) > 0 else ""
            target_id = parts[1] if len(parts) > 1 else ""
            days_waiting = days_between_ms(int(sent_ms), now_ms)
            key = _k_signal_suppress(tenant_id, f"followup_ignored:{member}")
            if not await self._mark_once(key, ttl_seconds=self._thresholds.signal_suppression_hours * 3600):
                continue
            await self._emit_signal(
                tenant_id=tenant_id,
                signal={"type": "followup_ignored", "target_entity": target_entity, "target_id": target_id, "days_waiting": days_waiting},
            )

    async def _emit_signal(self, *, tenant_id: str, signal: dict[str, Any]) -> None:
        await self.emit_event(
            topic="crm.productivity.signal",
            event_type="crm.productivity.signal",
            tenant_id=tenant_id,
            data={
                "signal": signal,
                "detectedAt": _utc_now(),
                "detectedBy": self.agent_id,
            },
        )

    async def _mark_once(self, key: str, *, ttl_seconds: int) -> bool:
        if not self._redis:
            return False
        ok = await self._redis.set(key, b"1", nx=True, ex=max(60, int(ttl_seconds)))
        return bool(ok)


class ProductivityAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="productivity-agent",
            agent_type="productivity",
            capabilities=["proposals:generate"],
        )
        self._pool: asyncpg.Pool | None = None
        self._graph = None

    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "skipped"}

    async def handle_signal(self, event: Dict[str, Any]) -> Dict[str, Any]:
        tenant_id = str(event.get("tenantid") or "")
        if not tenant_id:
            return {"status": "skipped"}
        data = event.get("data") or {}
        signal = (data.get("signal") if isinstance(data, dict) else None) or {}
        if not isinstance(signal, dict) or not signal.get("type"):
            return {"status": "skipped"}

        with tracer.start_as_current_span("productivity_pipeline") as span:
            span.set_attribute("signal_type", str(signal.get("type") or ""))
            if not self._graph:
                deps = ProductivityDeps(llm=_LlmAdapter(self), context=_DbContext(self))
                self._graph = build_productivity_graph(deps=deps)

            state = ProductivityState(tenant_id=tenant_id, signal=signal)
            out = await self._graph.ainvoke(state)
        proposal = out.get("proposal") if isinstance(out, dict) else getattr(out, "proposal", None)
        if not proposal:
            return {"status": "skipped"}

        await self.emit_event(
            topic="crm.productivity.action-suggested",
            event_type="crm.productivity.action-suggested",
            tenant_id=tenant_id,
            data={
                "proposal_id": proposal.proposal_id,
                "tenant_id": proposal.tenant_id,
                "user_id": proposal.user_id,
                "action_type": proposal.action_type,
                "target_entity": proposal.target_entity,
                "target_id": proposal.target_id,
                "priority": proposal.priority,
                "drafts": proposal.drafts,
                "justification": proposal.justification,
                "created_at": proposal.created_at,
                "dedupe_key": proposal.dedupe_key,
                "signal_type": proposal.signal_type,
                "signal": proposal.signal,
            },
            correlation_id=event.get("correlationid"),
        )
        return {"status": "completed"}

    async def cleanup(self):
        if self._pool:
            await self._pool.close()
        self._pool = None
        await super().cleanup()


class _LlmAdapter:
    def __init__(self, agent: ProductivityAgent):
        self._agent = agent

    async def call_llm(self, prompt: str, *, tenant_id: str) -> str:
        return await self._agent.call_llm(prompt, tenant_id=tenant_id)


class _DbContext:
    def __init__(self, agent: ProductivityAgent):
        self._agent = agent

    async def build_context(self, *, tenant_id: str, signal: dict[str, Any]) -> dict[str, Any]:
        typ = str(signal.get("type") or "")
        if typ == "lead_idle":
            lead_id = str(signal.get("lead_id") or "")
            return await self._lead_context(tenant_id=tenant_id, lead_id=lead_id)
        if typ == "ticket_aging":
            ticket_id = str(signal.get("ticket_id") or "")
            return await self._ticket_context(tenant_id=tenant_id, ticket_id=ticket_id)
        if typ == "task_overdue":
            task_id = str(signal.get("task_id") or "")
            return await self._task_context(tenant_id=tenant_id, task_id=task_id)
        if typ == "followup_ignored":
            target_entity = str(signal.get("target_entity") or "")
            target_id = str(signal.get("target_id") or "")
            return await self._generic_context(tenant_id=tenant_id, entity_type=target_entity, entity_id=target_id)
        if typ.startswith("prediction_"):
            entity_type = str(signal.get("entity_type") or "")
            entity_id = str(signal.get("entity_id") or "")
            return await self._generic_context(tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id)
        return {"entity_type": "unknown", "entity_id": "", "fallback_user_id": await self._fallback_user_id(tenant_id=tenant_id)}

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._agent._pool:
            return self._agent._pool
        self._agent._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
        return self._agent._pool

    async def _fallback_user_id(self, *, tenant_id: str) -> str:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    "SELECT id::text FROM users WHERE tenant_id = $1::uuid AND status = 'active' ORDER BY created_at ASC LIMIT 1",
                    tenant_id,
                )
        return str(row["id"]) if row and row.get("id") else ""

    async def _lead_context(self, *, tenant_id: str, lead_id: str) -> dict[str, Any]:
        pool = await self._ensure_pool()
        fallback = await self._fallback_user_id(tenant_id=tenant_id)
        if not lead_id:
            return {"entity_type": "lead", "entity_id": "", "fallback_user_id": fallback}
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    SELECT
                      id::text, name, email, company, status,
                      assigned_to::text as assigned_to,
                      created_by::text as created_by,
                      updated_at
                    FROM leads
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    lead_id,
                )
        if not row:
            return {"entity_type": "lead", "entity_id": lead_id, "fallback_user_id": fallback}
        assigned_to = row.get("assigned_to") or row.get("created_by") or fallback
        return {
            "entity_type": "lead",
            "entity_id": row.get("id") or lead_id,
            "lead": dict(row),
            "owner_user_id": assigned_to,
            "fallback_user_id": fallback,
        }

    async def _ticket_context(self, *, tenant_id: str, ticket_id: str) -> dict[str, Any]:
        pool = await self._ensure_pool()
        fallback = await self._fallback_user_id(tenant_id=tenant_id)
        if not ticket_id:
            return {"entity_type": "ticket", "entity_id": "", "fallback_user_id": fallback}
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    SELECT
                      id::text, subject, priority, status,
                      customer_id::text as customer_id,
                      assigned_to::text as assigned_to,
                      created_by::text as created_by,
                      sla_due_at
                    FROM tickets
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    ticket_id,
                )
                cust = None
                if row and row.get("customer_id"):
                    cust = await conn.fetchrow(
                        "SELECT id::text, name, email, segment, status FROM customers WHERE tenant_id = $1::uuid AND id = $2::uuid",
                        tenant_id,
                        row["customer_id"],
                    )
        if not row:
            return {"entity_type": "ticket", "entity_id": ticket_id, "fallback_user_id": fallback}
        assigned_to = row.get("assigned_to") or row.get("created_by") or fallback
        return {
            "entity_type": "ticket",
            "entity_id": row.get("id") or ticket_id,
            "ticket": dict(row),
            "customer": dict(cust) if cust else None,
            "owner_user_id": assigned_to,
            "fallback_user_id": fallback,
        }

    async def _task_context(self, *, tenant_id: str, task_id: str) -> dict[str, Any]:
        return {"entity_type": "task", "entity_id": task_id, "fallback_user_id": await self._fallback_user_id(tenant_id=tenant_id)}

    async def _customer_context(self, *, tenant_id: str, customer_id: str) -> dict[str, Any]:
        pool = await self._ensure_pool()
        fallback = await self._fallback_user_id(tenant_id=tenant_id)
        if not customer_id:
            return {"entity_type": "customer", "entity_id": "", "fallback_user_id": fallback}
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    SELECT
                      id::text, name, email, segment, status,
                      created_by::text as created_by,
                      updated_at
                    FROM customers
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    customer_id,
                )
        if not row:
            return {"entity_type": "customer", "entity_id": customer_id, "fallback_user_id": fallback}
        owner = row.get("created_by") or fallback
        return {
            "entity_type": "customer",
            "entity_id": row.get("id") or customer_id,
            "customer": dict(row),
            "owner_user_id": owner,
            "fallback_user_id": fallback,
        }

    async def _generic_context(self, *, tenant_id: str, entity_type: str, entity_id: str) -> dict[str, Any]:
        if entity_type == "lead":
            return await self._lead_context(tenant_id=tenant_id, lead_id=entity_id)
        if entity_type == "ticket":
            return await self._ticket_context(tenant_id=tenant_id, ticket_id=entity_id)
        if entity_type == "customer":
            return await self._customer_context(tenant_id=tenant_id, customer_id=entity_id)
        fallback = await self._fallback_user_id(tenant_id=tenant_id)
        return {"entity_type": entity_type or "unknown", "entity_id": entity_id, "fallback_user_id": fallback}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt_ms(val: str) -> int | None:
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _k_active_tenants() -> str:
    return "prod:tenants"


def _k_lead_activity(tenant_id: str) -> str:
    return f"prod:lead:last_activity:{tenant_id}"


def _k_lead_meta(tenant_id: str, lead_id: str) -> str:
    return f"prod:lead:meta:{tenant_id}:{lead_id}"


def _k_ticket_sla_due(tenant_id: str) -> str:
    return f"prod:ticket:sla_due:{tenant_id}"


def _k_ticket_meta(tenant_id: str, ticket_id: str) -> str:
    return f"prod:ticket:meta:{tenant_id}:{ticket_id}"

def _k_task_due(tenant_id: str) -> str:
    return f"prod:task:due:{tenant_id}"


def _k_task_meta(tenant_id: str, task_id: str) -> str:
    return f"prod:task:meta:{tenant_id}:{task_id}"


def _k_followup_sent(tenant_id: str) -> str:
    return f"prod:followup:sent:{tenant_id}"


def _k_followup_meta(tenant_id: str, member: str) -> str:
    return f"prod:followup:meta:{tenant_id}:{member}"


def _k_signal_suppress(tenant_id: str, key: str) -> str:
    return f"prod:signal:sent:{tenant_id}:{key}"


def _env(key: str, default: str) -> str:
    import os

    v = os.getenv(key)
    return v.strip() if v and v.strip() else default


