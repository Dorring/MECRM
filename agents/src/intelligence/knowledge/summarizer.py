from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from intelligence.providers import AsyncChatModel
from pydantic import BaseModel, Field


class DraftKbArticle(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    problem_summary: str = Field(min_length=10, max_length=10000)
    solution_steps: list[str] = Field(default_factory=list, max_length=50)
    preconditions: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = 0.0


@dataclass(frozen=True)
class DraftResult:
    draft: DraftKbArticle
    raw: str | None
    error: str | None


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


async def generate_draft_from_ticket(
    *,
    llm: AsyncChatModel,
    subject: str,
    description: str | None,
    resolution: str | None,
) -> DraftResult:
    prompt = (
        "You are a senior support engineer writing reusable internal knowledge base drafts.\n"
        "Tone: concise, instructional, reusable.\n"
        "Return ONLY valid JSON with keys: title, problem_summary, solution_steps, preconditions, tags, confidence.\n"
        "solution_steps and preconditions must be JSON arrays of strings.\n"
        "tags must be JSON array of short lowercase strings.\n"
        "confidence must be between 0 and 1.\n"
        "Never mention tenant names, user emails, or secrets.\n\n"
        f"Ticket subject: {subject}\n"
        f"Ticket description: {description or ''}\n"
        f"Ticket resolution: {resolution or ''}\n"
    )
    return await _invoke(llm=llm, prompt=prompt, fallback_title=subject)


async def generate_draft_from_conversation(
    *,
    llm: AsyncChatModel,
    conversation_id: str,
    transcript: list[dict[str, Any]],
) -> DraftResult:
    transcript_text = _format_transcript(transcript)
    prompt = (
        "You are a senior support engineer converting a resolved support chat into a reusable knowledge base draft.\n"
        "Tone: concise, instructional, reusable.\n"
        "Return ONLY valid JSON with keys: title, problem_summary, solution_steps, preconditions, tags, confidence.\n"
        "solution_steps and preconditions must be JSON arrays of strings.\n"
        "tags must be JSON array of short lowercase strings.\n"
        "confidence must be between 0 and 1.\n"
        "Never include secrets, tokens, passwords, or personal data.\n\n"
        f"Conversation ID: {conversation_id}\n"
        f"Transcript:\n{transcript_text}\n"
    )
    return await _invoke(llm=llm, prompt=prompt, fallback_title=f"Conversation {conversation_id}")


async def _invoke(*, llm: AsyncChatModel, prompt: str, fallback_title: str) -> DraftResult:
    try:
        msg = await llm.ainvoke(prompt)
        raw = (getattr(msg, "content", None) or "").strip()
        candidate = raw
        m = _JSON_BLOCK.search(raw)
        if m:
            candidate = m.group(0)
        obj = json.loads(candidate)
        draft = DraftKbArticle.model_validate(obj)
        draft.confidence = max(0.0, min(1.0, float(draft.confidence or 0.0)))
        draft.solution_steps = _normalize_list(draft.solution_steps)
        draft.preconditions = _normalize_list(draft.preconditions)
        draft.tags = _normalize_tags(draft.tags)
        if not draft.title.strip():
            draft.title = fallback_title[:500]
        return DraftResult(draft=draft, raw=raw, error=None)
    except Exception as e:
        heuristic = _heuristic_draft(fallback_title=fallback_title)
        return DraftResult(draft=heuristic, raw=None, error=str(e))


def _format_transcript(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for it in items[-60:]:
        role = str(it.get("role") or "").strip() or "unknown"
        msg = str(it.get("message") or "").strip()
        if not msg:
            continue
        if len(msg) > 2000:
            msg = msg[:2000]
        lines.append(f"{role}: {msg}")
    return "\n".join(lines)


def _normalize_list(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items or []:
        s = str(x).strip()
        if not s:
            continue
        if len(s) > 500:
            s = s[:500]
        out.append(s)
    return out[:30]


def _normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip().lower()
        if not s:
            continue
        s = re.sub(r"[^a-z0-9_\\-]+", "-", s).strip("-")
        if not s:
            continue
        if len(s) > 48:
            s = s[:48]
        if s not in out:
            out.append(s)
    return out[:20]


def _heuristic_draft(*, fallback_title: str) -> DraftKbArticle:
    return DraftKbArticle(
        title=(fallback_title or "Knowledge draft")[:500],
        problem_summary="Auto-generated draft (LLM unavailable). Please edit to include clear symptoms and scope.",
        solution_steps=["Add explicit resolution steps here.", "Verify the issue is resolved and document verification steps."],
        preconditions=["Confirm environment and prerequisites.", "Confirm user permissions and tenant context."],
        tags=["draft"],
        confidence=0.2,
    )

