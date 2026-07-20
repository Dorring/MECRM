"""Deterministic provider for offline/CI use.

Supports structured output, scenario fixtures, and fault injection —
all without touching a network or requiring an API key.

Used when AI_MODE=deterministic. Never connects to Ollama, NVIDIA NIM,
or any other remote model service.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

import structlog

from orchestrator.ai_mode import AIMode

logger = structlog.get_logger(__name__)

# Fixed embedding dimension — large enough to be distinguishable across
# different inputs, small enough to avoid unrealistic memory pressure.
DETERMINISTIC_EMBEDDING_DIM = 768


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


@dataclass
class DeterministicResponse:
    """Mimics a LangChain AIMessage so callers that access ``.content`` work."""

    content: str
    """The generated text content."""

    response_metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata (model name, usage, etc.)."""

    @property
    def usage_metadata(self) -> dict[str, int]:
        return self.response_metadata.get("usage", {})


@dataclass
class DeterministicEmbeddingResult:
    """Thin wrapper so callers can inspect embedding metadata."""

    vector: list[float]
    dimension: int
    provider: str = "deterministic"


# ---------------------------------------------------------------------------
# Fault scenarios
# ---------------------------------------------------------------------------

# Scenario IDs that trigger fault injection. Prefix-matched so callers can
# append arbitrary suffixes (e.g. "error/timeout/sales-agent").
FAULT_TIMEOUT = "error/timeout"
FAULT_EMPTY = "error/empty"
FAULT_MALFORMED = "error/malformed"
FAULT_LOW_CONFIDENCE = "error/low_confidence"
FAULT_PROVIDER_ERROR = "error/provider_error"


class DeterministicTimeoutError(TimeoutError):
    """Raised when scenario requests a timeout simulation."""


class DeterministicProviderError(RuntimeError):
    """Raised when scenario requests a provider-level error."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_hash(text: str, length: int = 32) -> str:
    """Return a stable hex digest for *text* (SHA-256, truncated)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _stable_float_vector(text: str, dim: int) -> list[float]:
    """Derive a deterministic float vector from *text*.

    Uses SHA-256 to seed a simple deterministic expansion so identical
    input always produces the identical unit vector.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    # Expand the 32-byte digest to *dim* floats via a simple LCG-style step.
    state = int.from_bytes(digest, "big")
    for i in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        # Map to [-1, 1]
        vec.append(((state / 0xFFFFFFFFFFFFFFFF) * 2.0) - 1.0)
    # Normalise to unit length
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _extract_query_text(messages: list[Any]) -> str:
    """Extract the last human message text from a list of LangChain messages."""
    texts: list[str] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    texts.append(part["text"])
                elif isinstance(part, str):
                    texts.append(part)
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Fixture registry
# ---------------------------------------------------------------------------

_FIXTURES: dict[str, str] = {}
"""Global fixture registry: scenario_id -> response text."""


def register_fixture(scenario_id: str, response_text: str) -> None:
    """Register a deterministic fixture response."""
    _FIXTURES[scenario_id] = response_text


def clear_fixtures() -> None:
    """Remove all registered fixtures (useful between tests)."""
    _FIXTURES.clear()


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class DeterministicChatProvider:
    """Chat model substitute that never makes a network call.

    Resolution order for generating a response:
    1. If *scenario_id* matches a registered fixture, return that text.
    2. If *scenario_id* starts with a known fault prefix, simulate the fault.
    3. Otherwise return a stable structured response derived from the input.
    """

    PROVIDER: ClassVar[str] = "deterministic"
    MODEL: ClassVar[str] = "deterministic-chat-v1"

    def _resolve(self, scenario_id: str | None, messages_text: str) -> str:
        # 1. Registered fixture (exact match)
        if scenario_id and scenario_id in _FIXTURES:
            return _FIXTURES[scenario_id]

        # 2. Fault injection (prefix match)
        if scenario_id:
            sid = scenario_id
            if sid.startswith(FAULT_TIMEOUT):
                raise DeterministicTimeoutError(
                    f"Deterministic timeout injected for scenario={sid}"
                )
            if sid.startswith(FAULT_EMPTY):
                return ""
            if sid.startswith(FAULT_MALFORMED):
                return "{not valid json [}"
            if sid.startswith(FAULT_LOW_CONFIDENCE):
                return json.dumps({
                    "confidence": 0.12,
                    "summary": "Uncertain response from deterministic provider",
                    "status": "low_confidence",
                })
            if sid.startswith(FAULT_PROVIDER_ERROR):
                raise DeterministicProviderError(
                    f"Deterministic provider error injected for scenario={sid}"
                )

        # 3. Default: stable structured response
        h = _stable_hash(messages_text, 16)
        return json.dumps({
            "analysis": (
                f"Deterministic analysis based on input hash {h}. "
                f"This is a stable, repeatable response for CI and testing."
            ),
            "confidence": 0.85,
            "status": "completed",
            "input_hash": h,
            "provider": self.PROVIDER,
        })

    async def ainvoke(
        self,
        input: Any,
        *,
        scenario_id: str | None = None,
        **kwargs: Any,
    ) -> DeterministicResponse:
        """Async entry point matching LangChain's ``ainvoke`` signature.

        *input* is typically a list of LangChain messages or a dict.
        """
        messages: list[Any] = input if isinstance(input, list) else [input]
        query_text = _extract_query_text(messages)

        started = time.monotonic()
        content = self._resolve(scenario_id, query_text)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        # Compute a deterministic token-usage estimate based on text lengths.
        input_chars = len(query_text)
        output_chars = len(content)
        input_tokens = max(1, input_chars // 4)
        output_tokens = max(1, output_chars // 4)

        return DeterministicResponse(
            content=content,
            response_metadata={
                "model": self.MODEL,
                "provider": self.PROVIDER,
                "ai_mode": AIMode.DETERMINISTIC.value,
                "scenario_id": scenario_id,
                "latency_ms": round(elapsed_ms, 2),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
        )


class DeterministicEmbeddingsProvider:
    """Embeddings substitute that never makes a network call.

    Same input text always produces the identical vector (unit length,
    dimension = DETERMINISTIC_EMBEDDING_DIM).
    """

    PROVIDER: ClassVar[str] = "deterministic"
    MODEL: ClassVar[str] = "deterministic-embed-v1"

    async def aembed_query(self, text: str) -> list[float]:
        return _stable_float_vector(text, DETERMINISTIC_EMBEDDING_DIM)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_stable_float_vector(t, DETERMINISTIC_EMBEDDING_DIM) for t in texts]

    @property
    def dimension(self) -> int:
        return DETERMINISTIC_EMBEDDING_DIM
