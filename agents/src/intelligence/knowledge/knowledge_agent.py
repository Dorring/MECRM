from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog
from langchain_ollama import ChatOllama

from agents.base import BaseAgent
from orchestrator.config import settings
from intelligence.chat.memory import WeaviateChatMemory
from .graph import KnowledgeDraftDeps, KnowledgeDraftState, build_knowledge_draft_graph
from .publisher import KnowledgePublisher


logger = structlog.get_logger()


@dataclass(frozen=True)
class SourceEvent:
    topic: str
    tenant_id: str
    correlation_id: str | None
    data: dict[str, Any]


class KnowledgeAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="knowledge-agent",
            agent_type="knowledge",
            capabilities=["knowledge:draft", "knowledge:publish", "knowledge:embed"],
        )
        self._pool: Optional[asyncpg.Pool] = None
        self._llm = ChatOllama(base_url=settings.OLLAMA_URL, model=settings.OLLAMA_MODEL, temperature=0.1)
        self._graph = build_knowledge_draft_graph(deps=KnowledgeDraftDeps(llm=self._llm))
        self._chat_memory = WeaviateChatMemory(
            weaviate_url=settings.WEAVIATE_URL,
            ollama_url=settings.OLLAMA_URL,
            embedding_model=_env("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )
        self._publisher = KnowledgePublisher()

    async def initialize(self, producer):
        await super().initialize(producer)
        self._pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
        await self._publisher.start()

    async def cleanup(self):
        await super().cleanup()
        if self._pool:
            await self._pool.close()
        self._pool = None
        await self._publisher.close()

    async def process(self, event: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ignored"}

    async def handle_ticket_resolved(self, *, evt: SourceEvent) -> None:
        ticket_id = str(evt.data.get("ticketId") or "").strip()
        if not ticket_id:
            return
        ticket = await self._fetch_ticket(tenant_id=evt.tenant_id, ticket_id=ticket_id)
        if not ticket:
            return
        state = KnowledgeDraftState(
            source_type="ticket_resolved",
            tenant_id=evt.tenant_id,
            source_id=ticket_id,
            subject=str(ticket.get("subject") or ""),
            description=ticket.get("description"),
            resolution=ticket.get("resolution"),
        )
        final = await self._graph.ainvoke(state)
        await self._persist_draft_from_state(evt=evt, state=final)

    async def handle_conversation_closed(self, *, evt: SourceEvent) -> None:
        conversation_id = str(evt.data.get("conversationId") or "").strip()
        if not conversation_id:
            return
        transcript = await self._chat_memory.load_window(tenant_id=evt.tenant_id, conversation_id=conversation_id, limit=50)
        state = KnowledgeDraftState(source_type="conversation_closed", tenant_id=evt.tenant_id, source_id=conversation_id, transcript=transcript)
        final = await self._graph.ainvoke(state)
        await self._persist_draft_from_state(evt=evt, state=final)

    async def handle_knowledge_published(self, *, evt: SourceEvent) -> None:
        article_id = str(evt.data.get("articleId") or "").strip()
        if not article_id:
            return
        try:
            await self._publisher.embed_article(tenant_id=evt.tenant_id, article_id=article_id)
        except Exception as e:
            logger.warn("Knowledge embedding failed", error=str(e), tenant_id=evt.tenant_id, article_id=article_id)

    async def _persist_draft_from_state(self, *, evt: SourceEvent, state: KnowledgeDraftState) -> None:
        if not self._pool:
            return
        if not state.draft:
            return
        d = state.draft.draft
        classification = state.classification.parsed if state.classification else None

        source_ticket_id: str | None = state.source_id if state.source_type == "ticket_resolved" else None
        source_conversation_id: str | None = state.source_id if state.source_type == "conversation_closed" else None

        existing_id = await self._find_existing_draft(
            tenant_id=evt.tenant_id,
            source_ticket_id=source_ticket_id,
            source_conversation_id=source_conversation_id,
        )
        if existing_id:
            return

        draft_id = await self._insert_draft(
            tenant_id=evt.tenant_id,
            source_ticket_id=source_ticket_id,
            source_conversation_id=source_conversation_id,
            title=d.title,
            problem_summary=d.problem_summary,
            solution_steps=d.solution_steps,
            preconditions=d.preconditions,
            tags=(classification.tags if classification else d.tags),
            topic=(classification.topic if classification else "unknown"),
            confidence=float(classification.confidence if classification else d.confidence),
        )
        if not draft_id:
            return

        await self.emit_event(
            topic="crm.knowledge.draft.created",
            event_type="crm.knowledge.draft.created",
            tenant_id=evt.tenant_id,
            correlation_id=evt.correlation_id,
            data={
                "draftId": draft_id,
                "sourceTicketId": source_ticket_id,
                "sourceConversationId": source_conversation_id,
                "title": d.title,
                "topic": (classification.topic if classification else "unknown"),
                "tags": (classification.tags if classification else d.tags),
                "confidence": float(classification.confidence if classification else d.confidence),
                "createdAt": _utc_iso(),
            },
        )

    async def _fetch_ticket(self, *, tenant_id: str, ticket_id: str) -> dict[str, Any] | None:
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    SELECT id::text as id, tenant_id::text as tenant_id, subject, description, resolution, status, category, priority, resolved_at
                    FROM tickets
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    ticket_id,
                )
        return dict(row) if row else None

    async def _find_existing_draft(
        self,
        *,
        tenant_id: str,
        source_ticket_id: str | None,
        source_conversation_id: str | None,
    ) -> str | None:
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                if source_ticket_id:
                    row = await conn.fetchrow(
                        """
                        SELECT id::text as id
                        FROM knowledge_drafts
                        WHERE tenant_id = $1::uuid AND source_ticket_id = $2::uuid
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        tenant_id,
                        source_ticket_id,
                    )
                    return str(row.get("id")) if row else None
                if source_conversation_id:
                    row = await conn.fetchrow(
                        """
                        SELECT id::text as id
                        FROM knowledge_drafts
                        WHERE tenant_id = $1::uuid AND source_conversation_id = $2::text
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        tenant_id,
                        source_conversation_id,
                    )
                    return str(row.get("id")) if row else None
        return None

    async def _insert_draft(
        self,
        *,
        tenant_id: str,
        source_ticket_id: str | None,
        source_conversation_id: str | None,
        title: str,
        problem_summary: str,
        solution_steps: list[str],
        preconditions: list[str],
        tags: list[str],
        topic: str,
        confidence: float,
    ) -> str | None:
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_drafts (
                      tenant_id,
                      source_ticket_id,
                      source_conversation_id,
                      title,
                      problem_summary,
                      solution_steps,
                      preconditions,
                      tags,
                      topic,
                      confidence,
                      status,
                      created_by,
                      created_at,
                      updated_at
                    ) VALUES (
                      $1::uuid,
                      $2::uuid,
                      $3::text,
                      $4::text,
                      $5::text,
                      $6::jsonb,
                      $7::jsonb,
                      $8::jsonb,
                      $9::text,
                      $10::numeric,
                      'draft',
                      $11::text,
                      now(),
                      now()
                    )
                    RETURNING id::text as id
                    """,
                    tenant_id,
                    source_ticket_id,
                    source_conversation_id,
                    title[:500],
                    problem_summary[:10000],
                    json.dumps(solution_steps[:50], ensure_ascii=False),
                    json.dumps(preconditions[:50], ensure_ascii=False),
                    json.dumps(tags[:50], ensure_ascii=False),
                    topic[:50] if topic else "unknown",
                    max(0.0, min(1.0, float(confidence or 0.0))),
                    self.agent_id,
                )
        return str(row.get("id")) if row else None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env(key: str, default: str) -> str:
    import os

    val = os.getenv(key)
    return val.strip() if val and val.strip() else default

