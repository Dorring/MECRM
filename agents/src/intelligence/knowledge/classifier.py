from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field


Topic = Literal["billing", "onboarding", "integrations", "bugs", "usage", "unknown"]


class TopicClassification(BaseModel):
    topic: Topic = "unknown"
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.0


@dataclass(frozen=True)
class TopicClassificationResult:
    parsed: TopicClassification
    raw: str | None
    error: str | None


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


async def classify_topic(*, llm: ChatOllama, title: str, problem: str, resolution: str) -> TopicClassificationResult:
    prompt = (
        "You are a classifier for support knowledge base articles in a CRM.\n"
        "Return ONLY valid JSON with keys: topic, tags, confidence.\n"
        "topic must be one of: billing, onboarding, integrations, bugs, usage, unknown.\n"
        "tags must be a JSON array of short lowercase strings.\n"
        "confidence must be a number between 0 and 1.\n\n"
        f"Title: {title}\n"
        f"Problem: {problem}\n"
        f"Resolution: {resolution}\n"
    )

    try:
        msg = await llm.ainvoke(prompt)
        raw = (getattr(msg, "content", None) or "").strip()
        candidate = raw
        m = _JSON_BLOCK.search(raw)
        if m:
            candidate = m.group(0)
        obj = json.loads(candidate)
        parsed = TopicClassification.model_validate(obj)
        parsed.confidence = max(0.0, min(1.0, float(parsed.confidence or 0.0)))
        parsed.tags = _normalize_tags(parsed.tags)
        return TopicClassificationResult(parsed=parsed, raw=raw, error=None)
    except Exception as e:
        return TopicClassificationResult(parsed=TopicClassification(), raw=None, error=str(e))


def _normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip().lower()
        if not s:
            continue
        if len(s) > 48:
            s = s[:48]
        if s not in out:
            out.append(s)
    return out[:20]

