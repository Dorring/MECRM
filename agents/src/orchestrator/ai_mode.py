"""AI runtime mode and provider configuration model.

Central, single source of truth for AI_MODE, AI_PROVIDER, and agent
orchestration mode. No other module should read these env vars directly.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any


class AIMode(str, Enum):
    """AI runtime mode — controls whether and how models are initialised."""

    DISABLED = "disabled"
    DETERMINISTIC = "deterministic"
    LIVE = "live"

    @property
    def allows_model_init(self) -> bool:
        """True when the mode permits creating a real chat/embedding model."""
        return self is AIMode.LIVE

    @property
    def requires_network(self) -> bool:
        """True when the mode may open a network connection to a provider."""
        return self is AIMode.LIVE


class AIProvider(str, Enum):
    """Supported AI backend providers (only relevant when AI_MODE=live)."""

    OLLAMA = "ollama"
    NVIDIA_NIM = "nvidia_nim"


class AgentOrchestrationMode(str, Enum):
    """Controls which execution path is active (Phase 5+)."""

    LEGACY = "legacy"          # existing AgentRouter behaviour
    SHADOW = "shadow"          # legacy path active, supervisor path runs in parallel
    SUPERVISOR = "supervisor"  # only supervisor graph path active


def _env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def resolve_ai_mode() -> AIMode:
    """Resolve AI_MODE from the environment with safe default.

    Returns:
        AIMode.DETERMINISTIC when AI_MODE is unset or unrecognised.
        A compatibility warning is emitted when the env var is absent
        but AI_PROVIDER is set to a known value (legacy config).
    """
    raw = _env("AI_MODE").lower()
    if raw in ("disabled", "deterministic", "live"):
        return AIMode(raw)

    # Legacy compatibility: AI_PROVIDER is set but AI_MODE is not.
    provider_raw = _env("AI_PROVIDER").lower()
    if raw == "" and provider_raw in ("ollama", "nvidia_nim"):
        import structlog
        structlog.get_logger(__name__).warning(
            "ai_mode.legacy_fallback",
            ai_mode=raw or "<unset>",
            ai_provider=provider_raw,
            resolved_mode=AIMode.DETERMINISTIC.value,
            message=(
                "AI_MODE is not set but AI_PROVIDER=%s was detected. "
                "Defaulting to AI_MODE=deterministic. "
                "Set AI_MODE=live explicitly to enable real model inference."
            ),
        )
        return AIMode.DETERMINISTIC

    if raw:
        import structlog
        structlog.get_logger(__name__).warning(
            "ai_mode.unrecognised",
            ai_mode=raw,
            resolved_mode=AIMode.DETERMINISTIC.value,
        )
    return AIMode.DETERMINISTIC


def resolve_ai_provider(mode: AIMode) -> AIProvider | None:
    """Resolve AI_PROVIDER; returns None unless mode is LIVE."""
    if mode is not AIMode.LIVE:
        return None
    raw = _env("AI_PROVIDER").lower()
    if raw == "ollama":
        return AIProvider.OLLAMA
    if raw in ("nvidia_nim", "nvidia", "nim"):
        return AIProvider.NVIDIA_NIM
    return None


def resolve_orchestration_mode() -> AgentOrchestrationMode:
    raw = _env("AGENT_ORCHESTRATION_MODE").lower()
    if raw == "shadow":
        return AgentOrchestrationMode.SHADOW
    if raw == "supervisor":
        return AgentOrchestrationMode.SUPERVISOR
    if raw and raw != "legacy":
        import structlog
        structlog.get_logger(__name__).warning(
            "orchestration_mode.unrecognised",
            raw=raw,
            resolved=AgentOrchestrationMode.LEGACY.value,
        )
    return AgentOrchestrationMode.LEGACY


def build_ai_metadata(mode: AIMode, provider: AIProvider | None) -> dict[str, Any]:
    """Return safe-to-log AI runtime metadata (no credentials)."""
    meta: dict[str, Any] = {
        "ai_mode": mode.value,
        "provider": provider.value if provider else "none",
    }
    if mode is AIMode.LIVE and provider is not None:
        from orchestrator.config import settings
        meta["chat_model"] = (
            settings.NVIDIA_CHAT_MODEL
            if provider is AIProvider.NVIDIA_NIM
            else settings.OLLAMA_MODEL
        )
        meta["embedding_model"] = (
            settings.NVIDIA_EMBED_MODEL
            if provider is AIProvider.NVIDIA_NIM
            else settings.OLLAMA_EMBED_MODEL
        )
    return meta
