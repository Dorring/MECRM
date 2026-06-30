from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field


EntityType = Literal["lead", "deal", "ticket", "customer", "unknown"]
ActionType = Literal["view", "filter", "open", "search"]


class SearchIntent(BaseModel):
    entity: EntityType = "unknown"
    action: ActionType = "search"
    filters: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0


@dataclass(frozen=True)
class IntentParseResult:
    intent: SearchIntent
    raw: str | None
    error: str | None


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


async def parse_intent(*, llm: ChatOllama, query: str) -> IntentParseResult:
    prompt = (
        "You are an intent extractor for a multi-tenant CRM. "
        "Return ONLY valid JSON with keys: entity, action, filters, confidence. "
        "entity must be one of: lead, deal, ticket, customer, unknown. "
        "action must be one of: view, filter, open, search. "
        "confidence must be a number between 0 and 1. "
        "filters must be an object. "
        "Do not include any other keys.\n\n"
        f"User query: {query}"
    )

    try:
        msg = await llm.ainvoke(prompt)
        raw = (getattr(msg, "content", None) or "").strip()
        candidate = raw
        m = _JSON_BLOCK.search(raw)
        if m:
            candidate = m.group(0)
        obj = json.loads(candidate)
        intent = SearchIntent.model_validate(obj)
        if intent.confidence < 0:
            intent.confidence = 0.0
        if intent.confidence > 1:
            intent.confidence = 1.0
        return IntentParseResult(intent=intent, raw=raw, error=None)
    except Exception as e:
        return IntentParseResult(intent=SearchIntent(), raw=None, error=str(e))

