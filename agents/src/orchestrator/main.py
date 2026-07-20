"""
AI Agent Orchestrator

Main entry point for the AI agent layer.
Consumes events from Kafka and routes them to appropriate agents.
"""

# load_dotenv() MUST run before any project import that reads environment
# variables (config.py, ai_mode.py, providers.py, etc.).  Without this,
# .env overrides are invisible to the Settings singleton.
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import re
import signal
import os
import uuid
from datetime import datetime, timezone
from typing import Any, cast

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import OffsetAndMetadata, TopicPartition
from aiohttp import web

from .router import AgentRouter
from .config import settings
from governance.agent_telemetry import agents_running, metrics_response
from governance.agent_telemetry import audit_queries_total, inc_dlq_routed
from intelligence.providers import provider_health_check, provider_metadata

from intelligence.search.search_agent import SearchAgent
from intelligence.chat.chat_agent import ChatAgent
from intelligence.chat.tool_executor import ChatToolExecutor
from intelligence.chat.tools import (
    CrmReader, CrmWriter, SearchAdapter, VectorSearch,
)
from intelligence.automation.automation_agent import AutomationAgent
from intelligence.compliance.audit_indexer import AuditIndexer
from intelligence.compliance.compliance_agent import (
    ComplianceIntelligenceAgent, AuditSearchFilters,
)
from intelligence.i18n.graph import process_multilingual_input
from intelligence.i18n.voice_ingest import AudioFormat

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger()

# Dead-letter topic for messages that failed agent processing. Tenant context
# is preserved via the CloudEvents `tenantid` field and the Kafka message key
# (tenant_id), so DLQ consumers stay within tenant boundaries.
DLQ_TOPIC = os.getenv("AGENTS_DLQ_TOPIC", "crm.dlq.agents")


# P0-9: PII redaction patterns applied to DLQ payloads. Order matters: SSN and
# credit-card are matched before the generic phone pattern so 16-digit card
# numbers are not partially redacted as phone numbers. Email is matched first
# so an address containing digits is not mangled by the phone pattern.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # email: user@domain.tld
    ("EMAIL", re.compile(
        r"(?P<addr>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")),
    # SSN: 123-45-6789 or 123456789
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b")),
    # credit card: 13-19 contiguous digits (optionally grouped by - or space)
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\d\b")),
    # phone: (123) 456-7890 / 123-456-7890 / +1 123 456 7890
    ("PHONE", re.compile(
        r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3,4}[\s.-]?\d{4}")),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_pii(text: str) -> str:
    """Redact known PII (email/SSN/credit-card/phone) from a string.

    Replaces each match with ``[REDACTED_<TYPE>]``. Used before writing the
    original payload or error reason into the DLQ so the dead-letter topic
    never carries raw PII while still preserving tenant context and the
    structure needed for forensics/replay.
    """
    if not text:
        return text
    redacted = text
    for label, pattern in _PII_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label}]", redacted)
    return redacted


class AgentOrchestrator:
    """Main orchestrator that routes events to agents."""

    def __init__(self):
        self.consumer: AIOKafkaConsumer = None
        self.producer: AIOKafkaProducer = None
        self.router = AgentRouter()
        self.running = False
        self._paused_partitions: dict[TopicPartition, str] = {}
        self._resume_task: asyncio.Task | None = None

    async def start(self):
        """Start the orchestrator."""
        logger.info("Starting Agent Orchestrator")

        # Initialize Kafka consumer
        self.consumer = AIOKafkaConsumer(
            *settings.CONSUME_TOPICS,
            bootstrap_servers=settings.KAFKA_BROKERS,
            group_id=settings.KAFKA_GROUP_ID,
            auto_offset_reset="latest",
            enable_auto_commit=False,
            value_deserializer=lambda m: m.decode("utf-8"),
        )

        # Initialize Kafka producer
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.KAFKA_BROKERS,
            value_serializer=lambda v: v.encode("utf-8"),
        )

        await self.consumer.start()
        await self.producer.start()
        await self.router.initialize(self.producer)

        self.running = True
        agents_running.set(1)
        logger.info("Agent Orchestrator started",
                    topics=settings.CONSUME_TOPICS)

        self._resume_task = asyncio.create_task(self._resume_loop())

        # Start consuming
        await self._consume_loop()

    async def _consume_loop(self):
        """Main consumption loop.

        Offset is committed only after successful processing OR after a failed
        message has been safely routed to the DLQ. A poison message that fails
        repeatedly would otherwise block its partition forever; routing it to
        the DLQ and advancing the offset isolates it without losing the data,
        satisfying the poison-message-isolation requirement (Phase 6).

        P0-8: If the DLQ *send itself* fails (transient broker outage), we do
        NOT advance the offset immediately. We retry the DLQ send with bounded
        exponential backoff (up to settings.DLQ_MAX_RETRIES). Only after the
        retry budget is exhausted do we advance past the message (logging the
        loss) to avoid an infinite partition stall. This guarantees a
        recoverable message is not silently dropped on a transient DLQ failure.
        """
        try:
            async for message in self.consumer:
                if not self.running:
                    break

                tp = TopicPartition(message.topic, message.partition)
                try:
                    processed = await self._process_message(message)
                    if processed:
                        await self.consumer.commit({
                            tp: OffsetAndMetadata(message.offset + 1, ""),
                        })
                except Exception as e:
                    logger.error(
                        "Failed to process message",
                        topic=message.topic,
                        offset=message.offset,
                        error=str(e),
                    )
                    advanced = await self._route_to_dlq_with_retry(message, e)
                    if advanced:
                        try:
                            await self.consumer.commit({
                                tp: OffsetAndMetadata(message.offset + 1, ""),
                            })
                        except Exception as commit_err:
                            logger.error(
                                "Failed to commit offset after DLQ routing",
                                topic=message.topic,
                                offset=message.offset,
                                error=str(commit_err),
                            )
                    # If not advanced (transient DLQ failure within retry
                    # budget AND we chose to keep the message), the offset is
                    # NOT committed: the next poll re-fetches this message so
                    # it is retried rather than lost. AIOKafkaConsumer with
                    # enable_auto_commit=False will re-deliver from the last
                    # committed offset on the next iteration.

        except asyncio.CancelledError:
            logger.info("Consumer loop cancelled")

    async def _route_to_dlq_with_retry(
            self, message, error: Exception) -> bool:
        """Route a failed message to the DLQ with bounded retry.

        Returns True once it is safe to advance past the message (DLQ send
        succeeded OR the retry budget was exhausted so we give up to avoid an
        infinite partition stall). Returns False only if a retry is still
        pending and the caller should re-deliver the message on the next poll.
        In the False case the offset is deliberately NOT committed so the
        message is not lost.
        """
        max_retries = max(
            1, int(getattr(settings, "DLQ_MAX_RETRIES", settings.MAX_RETRIES)))
        base_backoff = float(
            getattr(settings, "DLQ_RETRY_BACKOFF_SECONDS", 1.0))

        for attempt in range(1, max_retries + 1):
            dlq_sent = await self._send_to_dlq(message, error)
            if dlq_sent:
                return True
            if attempt >= max_retries:
                logger.critical(
                    "DLQ send exhausted retries; advancing offset to avoid "
                    "partition stall (message is logged but NOT in the DLQ)",
                    topic=message.topic,
                    partition=message.partition,
                    offset=message.offset,
                    attempts=attempt,
                )
                return True
            backoff = min(base_backoff * (2 ** (attempt - 1)), 30.0)
            logger.warning(
                "DLQ send failed, retrying",
                topic=message.topic,
                offset=message.offset,
                attempt=attempt,
                max_retries=max_retries,
                backoff_seconds=backoff,
            )
            await asyncio.sleep(backoff)

        # Defensive: should be unreachable.
        return True

    async def _send_to_dlq(self, message, error: Exception) -> bool:
        """Route a failed message to the dead-letter topic.

        Preserves tenant context (CloudEvents `tenantid` + Kafka key =
        tenant_id) so DLQ consumers never cross tenant boundaries. Carries the
        original message, the failure reason, and a timestamp for later replay
        or forensics. Returns True if the DLQ send succeeded.

        P0-9: The original payload (``original_value``) and the error reason
        are PII-redacted (email / phone / SSN / credit-card) before being
        written into the DLQ envelope, so the dead-letter topic never carries
        raw PII. Tenant context is preserved via the CloudEvents ``tenantid``
        field and the Kafka message key.
        """
        # Best-effort tenant extraction: prefer the parsed envelope, fall back
        # to None when the payload is not valid JSON (the raw value is still
        # preserved in the DLQ envelope).
        tenant_id: str | None = None
        original_value: str = message.value if isinstance(
            message.value, str) else ""
        try:
            event = json.loads(message.value) if isinstance(
                message.value, str) else None
            if isinstance(event, dict):
                tenant_id = event.get("tenantid") or event.get("tenantId") or (
                    event.get("data", {}) or {}).get("tenantId")
        except Exception:
            tenant_id = None

        # P0-9: redact known PII before persisting the payload in the DLQ.
        redacted_value = _redact_pii(original_value)
        reason = _redact_pii(f"{type(error).__name__}: {error}")
        # Truncate the redacted payload to bound DLQ record size; the full
        # payload is still recoverable from the source topic + offset.
        truncated = redacted_value[:65536]

        dlq_envelope = {
            "specversion": "1.0",
            "type": "crm.agents.dlq",
            "source": "/agents/orchestrator",
            "id": str(uuid.uuid4()),
            "time": _utc_now_iso(),
            "datacontenttype": "application/json",
            "tenantid": str(tenant_id) if tenant_id else "",
            "correlationid": str(uuid.uuid4()),
            "data": {
                "original_topic": message.topic,
                "original_partition": message.partition,
                "original_offset": message.offset,
                "original_value": truncated,
                "error_reason": reason[:2000],
                "failed_at": _utc_now_iso(),
                "routed_by": "agent-orchestrator",
            },
        }

        try:
            if not self.producer:
                logger.error(
                    "Cannot send to DLQ: producer not initialized",
                    topic=message.topic,
                )
                return False
            await self.producer.send_and_wait(
                DLQ_TOPIC,
                value=json.dumps(dlq_envelope),
                key=(str(tenant_id).encode("utf-8") if tenant_id else None),
            )
            inc_dlq_routed(topic=message.topic, reason=type(error).__name__)
            logger.warning(
                "Message routed to DLQ",
                dlq_topic=DLQ_TOPIC,
                original_topic=message.topic,
                partition=message.partition,
                offset=message.offset,
                tenant_id=tenant_id,
            )
            return True
        except Exception as send_err:
            inc_dlq_routed(topic=message.topic, reason="dlq_send_failed")
            logger.error(
                "Failed to send message to DLQ",
                dlq_topic=DLQ_TOPIC,
                original_topic=message.topic,
                partition=message.partition,
                offset=message.offset,
                tenant_id=tenant_id,
                error=str(send_err),
            )
            return False

    async def _process_message(self, message):
        """Process a single message."""
        logger.debug(
            "Received message",
            topic=message.topic,
            partition=message.partition,
            offset=message.offset,
        )

        tenant_id = None
        try:
            event = json.loads(message.value)
            tenant_id = event.get("tenantid") or event.get(
                "tenantId") or event.get("data", {}).get("tenantId")
        except Exception:
            tenant_id = None

        if tenant_id:
            decision = await self.router.kill_switch.decision(
                tenant_id=str(tenant_id),
                agent_id="agent-orchestrator",
            )
            if decision.blocked:
                tp = TopicPartition(message.topic, message.partition)
                self.consumer.pause([tp])
                await self.consumer.seek(tp, message.offset)
                self._paused_partitions[tp] = str(tenant_id)
                logger.warning(
                    "Paused partition due to kill switch",
                    tenant_id=str(tenant_id),
                    topic=message.topic,
                    partition=message.partition,
                    scope_key=decision.scope_key,
                    state=(
                        decision.status.state.value
                        if decision.status else None
                    ),
                )
                return False

        await self.router.route(message.topic, message.value)
        return True

    async def stop(self):
        """Stop the orchestrator."""
        logger.info("Stopping Agent Orchestrator")
        self.running = False
        agents_running.set(0)

        if self._resume_task:
            self._resume_task.cancel()
            self._resume_task = None

        if self.consumer:
            await self.consumer.stop()

        if self.producer:
            await self.producer.stop()

        logger.info("Agent Orchestrator stopped")

    async def _resume_loop(self) -> None:
        while self.running:
            if not self._paused_partitions:
                await asyncio.sleep(0.2)
                continue

            items = list(self._paused_partitions.items())
            for tp, tenant_id in items:
                decision = await self.router.kill_switch.decision(
                    tenant_id=tenant_id,
                    agent_id="agent-orchestrator",
                )
                if not decision.blocked:
                    self.consumer.resume([tp])
                    self._paused_partitions.pop(tp, None)
                    logger.info(
                        "Resumed partition after kill switch cleared",
                        tenant_id=tenant_id,
                        topic=tp.topic,
                        partition=tp.partition,
                    )
            await asyncio.sleep(0.2)


# Health check server
async def health_handler(request):
    """Health check endpoint."""
    return web.json_response({"status": "healthy"})


async def ready_handler(request):
    """Readiness endpoint — includes AI provider health.

    /health only signals process-alive + HTTP reachable.
    /ready signals whether the service can do useful work.

    HTTP 200 — ready
    HTTP 503 — degraded, unavailable, or misconfigured
    """
    from orchestrator.ai_mode import AIConfigurationError
    try:
        meta = provider_metadata()
    except AIConfigurationError as exc:
        meta = {
            "ai_mode": "unknown",
            "provider": "unknown",
            "chat_model": "unset",
            "embedding_model": "unset",
            "remote": False,
        }
        health: dict[str, Any] = {
            "status": "unavailable",
            "error": str(exc),
            "checks": {},
        }
    else:
        try:
            health = await provider_health_check()
        except AIConfigurationError as exc:
            health = {
                "status": "unavailable",
                "error": str(exc),
                "checks": {},
            }

    status = health.get("status", "unavailable")
    http_status = 200 if status == "ready" else 503
    return web.json_response({**meta, **health}, status=http_status)


async def metrics_handler(request):
    resp = metrics_response()
    # aiohttp rejects charset inside content_type; set header directly
    return web.Response(
        body=resp.body,
        headers={"Content-Type": resp.content_type},
    )


async def run_health_server():
    """Run the health check HTTP server."""
    app = web.Application()
    app["search_agent"] = SearchAgent()
    app["chat_agent"] = None
    app["automation_agent"] = AutomationAgent()
    app["audit_indexer"] = AuditIndexer()
    app["compliance_intelligence_agent"] = ComplianceIntelligenceAgent()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_post("/api/v1/intelligence/query",
                        intelligence_query_handler)
    app.router.add_post("/api/v1/intelligence/voice", voice_handler)
    app.router.add_post("/api/v1/intelligence/voice/query",
                        voice_query_handler)
    app.router.add_post("/api/v1/automation/parse", automation_parse_handler)
    app.router.add_post("/api/v1/audit/search", audit_search_handler)

    async def _startup(app: web.Application) -> None:
        agent: SearchAgent = app["search_agent"]
        await agent.start()
        search_agent: SearchAgent = app["search_agent"]
        chat_agent = ChatAgent(
            tool_executor=ChatToolExecutor(
                crm_reader=CrmReader(gateway_url=settings.GATEWAY_URL),
                crm_writer=CrmWriter(),
                vector_search=VectorSearch(
                    weaviate_url=settings.WEAVIATE_URL,
                    ollama_url=settings.OLLAMA_URL,
                    embedding_model=settings.OLLAMA_EMBED_MODEL,
                ),
                search_adapter=SearchAdapter(search_agent=search_agent),
            )
        )
        await chat_agent.start()
        app["chat_agent"] = chat_agent
        indexer: AuditIndexer = app["audit_indexer"]
        await indexer.start()

    async def _cleanup(app: web.Application) -> None:
        agent: SearchAgent = app["search_agent"]
        await agent.close()
        chat_agent: ChatAgent | None = app.get("chat_agent")
        if chat_agent:
            await chat_agent.close()
        indexer: AuditIndexer | None = app.get("audit_indexer")
        if indexer:
            await indexer.stop()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", settings.HEALTH_PORT)
    await site.start()

    logger.info("Health server started", port=settings.HEALTH_PORT)
    return runner


async def intelligence_query_handler(request: web.Request) -> web.Response:
    search_agent: SearchAgent = request.app["search_agent"]
    chat_agent: ChatAgent | None = request.app.get("chat_agent")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    query = str((body or {}).get("query") or "").strip()
    if not query:
        return web.json_response({"error": "missing_query"}, status=400)

    tenant_id = request.headers.get("X-Tenant-Id")
    user_id = request.headers.get("X-User-Id")
    roles_raw = request.headers.get("X-User-Roles") or ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    module = request.headers.get(
        "X-Client-Module") or (body or {}).get("module")
    correlation_id = request.headers.get("X-Correlation-Id")
    authorization = request.headers.get("Authorization")

    if not tenant_id or not user_id:
        return web.json_response({"error": "missing_context"}, status=400)

    is_chat = bool(
        (body or {}).get("conversation_id")
        or (body or {}).get("conversationId")
        or (body or {}).get("messages")
        or (body or {}).get("mode") == "chat"
    )
    if is_chat and chat_agent:
        conversation_id = (body or {}).get("conversation_id") or (
            body or {}).get("conversationId")
        resp = await chat_agent.chat(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            roles=roles,
            authorization=authorization,
            correlation_id=correlation_id,
            conversation_id=str(conversation_id) if conversation_id else None,
            query=query,
        )
        return web.json_response(resp)

    resp = await search_agent.search(
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        roles=roles,
        query=query,
        module=str(module) if module else None,
        correlation_id=correlation_id,
    )
    return web.json_response(resp)


async def voice_handler(request: web.Request) -> web.Response:
    """Handle voice transcription requests.

    HTTP semantics:
      - 200  success
      - 400  missing audio / context
      - 503  AI disabled or provider unavailable
      - 500  unexpected internal error (safe, no details)
    """
    try:
        audio_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "failed_to_read_audio"}, status=400)

    if not audio_bytes:
        return web.json_response({"error": "empty_audio"}, status=400)

    tenant_id = request.headers.get("X-Tenant-Id") or ""
    user_id = request.headers.get("X-User-Id") or ""
    audio_format = _normalize_audio_format(
        request.headers.get("X-Audio-Format"),
    )

    if not tenant_id or not user_id:
        return web.json_response({"error": "missing_context"}, status=400)

    try:
        # Process through i18n pipeline
        state = await process_multilingual_input(
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )

        # Check for STT-level error codes
        transcript = state.transcript
        if transcript is not None:
            err_code = getattr(transcript, "error_code", None)
            if err_code == "voice_ai_disabled":
                return web.json_response(
                    {"error": "voice_ai_disabled"}, status=503,
                )
            if err_code == "voice_provider_unavailable":
                return web.json_response(
                    {"error": "voice_provider_unavailable"}, status=503,
                )

        return web.json_response({
            "transcript": {
                "text": (
                    state.transcript.text if state.transcript else ""),
                "language": (
                    state.transcript.language if state.transcript else None),
                "confidence": (
                    state.transcript.confidence if state.transcript else 0),
                "duration_seconds": (
                    state.transcript.duration_seconds
                    if state.transcript else 0),
                "processing_time_ms": (
                    state.transcript.processing_time_ms
                    if state.transcript else 0),
            } if state.transcript else None,
            "original_language": state.original_language,
            "canonical_query": state.canonical_query,
            "stt_latency_ms": state.stt_latency_ms,
            "detection_latency_ms": state.detection_latency_ms,
            "translation_latency_ms": state.translation_latency_ms,
            "total_latency_ms": state.total_latency_ms,
        })
    except RuntimeError as exc:
        # Live mode with WHISPER_URL unset → 503
        msg = str(exc).lower()
        if "whisper_url" in msg:
            logger.error("Voice provider unavailable: WHISPER_URL not set")
            return web.json_response(
                {"error": "voice_provider_unavailable"}, status=503)
        logger.exception("Voice transcription failed (RuntimeError)")
        return web.json_response(
            {"error": "transcription_failed"}, status=500)
    except Exception:
        logger.exception("Voice transcription failed")
        return web.json_response(
            {"error": "transcription_failed"}, status=500)


async def voice_query_handler(request: web.Request) -> web.Response:
    """Handle full voice query pipeline: transcribe, detect, translate.

    Routes the canonical query to search/chat.

    HTTP semantics:
      - 200  success
      - 400  missing audio / context / no transcript
      - 503  AI disabled
      - 500  unexpected internal error (safe, no details)
    """
    search_agent: SearchAgent = request.app["search_agent"]

    try:
        audio_bytes = await request.read()
    except Exception:
        return web.json_response({"error": "failed_to_read_audio"}, status=400)

    if not audio_bytes:
        return web.json_response({"error": "empty_audio"}, status=400)

    tenant_id = request.headers.get("X-Tenant-Id") or ""
    user_id = request.headers.get("X-User-Id") or ""
    roles_raw = request.headers.get("X-User-Roles") or ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    module = request.headers.get("X-Client-Module")
    correlation_id = request.headers.get("X-Correlation-Id")
    audio_format = _normalize_audio_format(
        request.headers.get("X-Audio-Format"),
    )

    if not tenant_id or not user_id:
        return web.json_response({"error": "missing_context"}, status=400)

    try:
        # Process through i18n pipeline
        state = await process_multilingual_input(
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )

        # Check for STT-level error codes
        transcript = state.transcript
        if transcript is not None:
            err_code = getattr(transcript, "error_code", None)
            if err_code == "voice_ai_disabled":
                return web.json_response(
                    {"error": "voice_ai_disabled"}, status=503,
                )

        if not state.canonical_query:
            return web.json_response({
                "error": "no_transcript",
                "transcript": (
                    state.transcript.text if state.transcript else ""),
                "original_language": state.original_language,
            }, status=400)

        # Execute search with canonical query
        resp = await search_agent.search(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            roles=roles,
            query=state.canonical_query,
            module=str(module) if module else None,
            correlation_id=correlation_id,
        )

        # Add voice metadata to response
        resp["voice"] = {
            "transcript": state.transcript.text if state.transcript else "",
            "original_language": state.original_language,
            "canonical_query": state.canonical_query,
            "stt_latency_ms": state.stt_latency_ms,
            "detection_latency_ms": state.detection_latency_ms,
            "translation_latency_ms": state.translation_latency_ms,
        }

        return web.json_response(resp)

    except RuntimeError as exc:
        msg = str(exc).lower()
        if "whisper_url" in msg:
            logger.error("Voice provider unavailable: WHISPER_URL not set")
            return web.json_response(
                {"error": "voice_provider_unavailable"}, status=503)
        logger.exception("Voice query failed (RuntimeError)")
        return web.json_response(
            {"error": "voice_query_failed"}, status=500)
    except Exception:
        logger.exception("Voice query failed")
        return web.json_response(
            {"error": "voice_query_failed"}, status=500)


async def automation_parse_handler(request: web.Request) -> web.Response:
    automation_agent: AutomationAgent = request.app["automation_agent"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    nl_rule_text = str((body or {}).get("nl_rule_text") or (
        body or {}).get("nlRuleText") or "").strip()
    if not nl_rule_text:
        return web.json_response({"error": "missing_nl_rule_text"}, status=400)

    tenant_id = request.headers.get("X-Tenant-Id")
    user_id = request.headers.get("X-User-Id")
    roles_raw = request.headers.get("X-User-Roles") or ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    if not tenant_id or not user_id:
        return web.json_response({"error": "missing_context"}, status=400)
    if "admin" not in roles and "super_admin" not in roles:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        out = await automation_agent.parse(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            roles=roles,
            nl_rule_text=nl_rule_text,
        )
        return web.json_response(out)
    except Exception as e:
        logger.error("Automation parse failed", error=str(e))
        return web.json_response({"error": "parse_failed"}, status=500)


async def audit_search_handler(request: web.Request) -> web.Response:
    agent: ComplianceIntelligenceAgent = request.app[
        "compliance_intelligence_agent"
    ]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    query = str((body or {}).get("query") or "").strip()
    if not query:
        return web.json_response({"error": "missing_query"}, status=400)

    tenant_id = request.headers.get("X-Tenant-Id")
    user_id = request.headers.get("X-User-Id")
    roles_raw = request.headers.get("X-User-Roles") or ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    if not tenant_id or not user_id:
        return web.json_response({"error": "missing_context"}, status=400)
    allowed_roles = {"admin", "super_admin", "auditor"}
    if not allowed_roles.intersection(roles):
        return web.json_response({"error": "forbidden"}, status=403)

    filters = AuditSearchFilters(
        from_ts=(body or {}).get("from_ts") or (body or {}).get("fromTs"),
        to_ts=(body or {}).get("to_ts") or (body or {}).get("toTs"),
        agent_name=(body or {}).get("agent_name") or (
            body or {}).get("agentName"),
        action_type=(body or {}).get("action_type") or (
            body or {}).get("actionType"),
        status=(body or {}).get("status"),
        risk_level=(body or {}).get("risk_level") or (
            body or {}).get("riskLevel"),
    )

    try:
        out = await agent.semantic_audit_search(
            tenant_id=str(tenant_id),
            query=query,
            filters=filters,
            top_k=20,
        )
        audit_queries_total.labels(type="semantic").inc()
        return web.json_response(out)
    except Exception as e:
        logger.error("Audit search failed", error=str(e))
        return web.json_response({"error": "search_failed"}, status=500)


_AUDIO_FORMATS: set[AudioFormat] = {
    "webm", "wav", "mp3", "ogg", "flac", "m4a",
}


def _normalize_audio_format(fmt: str | None) -> AudioFormat:
    """Return a valid AudioFormat, defaulting to webm."""
    if fmt in _AUDIO_FORMATS:
        return cast(AudioFormat, fmt)
    return "webm"


async def main():
    """Main entry point."""
    orchestrator = AgentOrchestrator()
    health_runner = None

    # Setup signal handlers (platform-specific)
    loop = asyncio.get_event_loop()

    def signal_handler(*args):
        logger.info("Received shutdown signal")
        loop.create_task(orchestrator.stop())

    # Windows doesn't support add_signal_handler, use signal.signal instead
    if os.name == 'nt':  # Windows
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    else:  # Unix
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, signal_handler)

    try:
        # Start health check server
        health_runner = await run_health_server()

        # Start orchestrator
        await orchestrator.start()

    except Exception as e:
        logger.error("Orchestrator failed", error=str(e))
        raise

    finally:
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
