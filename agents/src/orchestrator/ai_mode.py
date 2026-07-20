"""AI runtime mode and provider configuration model.

Central, single source of truth for AI_MODE, AI_PROVIDER, and agent
orchestration mode. No other module should read these env vars directly.

Validation rules (Phase 1 review):
  - AI_MODE unset or empty → deterministic (safe default).
  - AI_MODE set to a recognised value → accepted.
  - AI_MODE set to an unrecognised non-empty value → AIConfigurationError.
  - AI_PROVIDER only consulted when AI_MODE=live.
  - AI_PROVIDER unrecognised non-empty value (live) → AIConfigurationError.
  - AI_PROVIDER empty (live) → AIConfigurationError.
  - No silent fallback, no implicit Ollama connection.

load_dotenv() MUST be called before this module is first imported.
The call is in orchestrator/main.py before any intelligence/agents imports.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any


class AIConfigurationError(RuntimeError):
    """Raised at startup when AI_MODE or AI_PROVIDER is invalid.

    The error message is safe to log and return over HTTP; it never
    includes raw env-var values that could contain credentials.
    """


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
    """Supported AI backend providers (only relevant when AI_MODE=live).

    These are NOT default values — a provider is only resolved when
    the env var is set explicitly AND AI_MODE=live.
    """

    OLLAMA = "ollama"
    NVIDIA_NIM = "nvidia_nim"


class AgentOrchestrationMode(str, Enum):
    """Controls which execution path is active (Phase 5+)."""

    LEGACY = "legacy"          # existing AgentRouter behaviour
    SHADOW = "shadow"          # legacy path active, supervisor path runs in parallel
    SUPERVISOR = "supervisor"  # only supervisor graph path active


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

_VALID_AI_MODES = frozenset({"disabled", "deterministic", "live"})
_VALID_AI_PROVIDERS = frozenset({"ollama", "nvidia_nim", "nvidia", "nim"})


def _env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def resolve_ai_mode() -> AIMode:
    """Resolve AI_MODE from the environment.

    Returns:
        AIMode.DETERMINISTIC when AI_MODE is unset or empty.

    Raises:
        AIConfigurationError when AI_MODE is set to an unrecognised value.
    """
    raw = _env("AI_MODE").lower()

    if raw == "":
        return AIMode.DETERMINISTIC

    if raw in _VALID_AI_MODES:
        return AIMode(raw)

    raise AIConfigurationError(
        f"AI_MODE={raw!r} is not a valid AI runtime mode. "
        f"Valid values: {', '.join(sorted(_VALID_AI_MODES))}"
    )


def resolve_ai_provider(mode: AIMode) -> AIProvider | None:
    """Resolve AI_PROVIDER; returns None unless mode is LIVE.

    Raises:
        AIConfigurationError when AI_MODE=live and AI_PROVIDER is invalid.
    """
    if mode is not AIMode.LIVE:
        return None

    raw = _env("AI_PROVIDER").lower()

    if raw == "":
        raise AIConfigurationError(
            "AI_MODE=live requires AI_PROVIDER to be set to "
            "'ollama' or 'nvidia_nim'"
        )

    if raw == "ollama":
        return AIProvider.OLLAMA

    if raw in ("nvidia_nim", "nvidia", "nim"):
        return AIProvider.NVIDIA_NIM

    raise AIConfigurationError(
        f"AI_PROVIDER={raw!r} is not a recognised provider. "
        "Valid values: ollama, nvidia_nim"
    )


def resolve_orchestration_mode() -> AgentOrchestrationMode:
    raw = _env("AGENT_ORCHESTRATION_MODE").lower()
    if raw == "" or raw == "legacy":
        return AgentOrchestrationMode.LEGACY
    if raw == "shadow":
        return AgentOrchestrationMode.SHADOW
    if raw == "supervisor":
        return AgentOrchestrationMode.SUPERVISOR
    # Unrecognised → warn but don't fail (Phase 5+ feature flag).
    import structlog
    structlog.get_logger(__name__).warning(
        "orchestration_mode.unrecognised",
        raw=raw,
        resolved=AgentOrchestrationMode.LEGACY.value,
    )
    return AgentOrchestrationMode.LEGACY


# ---------------------------------------------------------------------------
# Backward-compatibility warning (only when AI_MODE is unset)
# ---------------------------------------------------------------------------

_COMPAT_WARNED = False


def _emit_compat_warning_if_needed() -> None:
    """Emit a single compatibility warning when AI_PROVIDER is set without AI_MODE.

    Idempotent — only fires once per process lifetime.
    """
    global _COMPAT_WARNED
    if _COMPAT_WARNED:
        return
    _COMPAT_WARNED = True

    ai_mode_raw = _env("AI_MODE").lower()
    provider_raw = _env("AI_PROVIDER").lower()
    if ai_mode_raw == "" and provider_raw in ("ollama", "nvidia_nim"):
        import structlog
        structlog.get_logger(__name__).warning(
            "ai_mode.legacy_compat",
            ai_mode="<unset>",
            ai_provider=provider_raw,
            resolved_mode=AIMode.DETERMINISTIC.value,
            message=(
                "AI_PROVIDER is set but AI_MODE is not. "
                "Defaulting to AI_MODE=deterministic. "
                "Set AI_MODE=live explicitly to enable real model inference."
            ),
        )


# Emit at import time so the warning appears once at startup.
_emit_compat_warning_if_needed()


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
    elif mode is AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            DeterministicEmbeddingsProvider,
        )
        meta["chat_model"] = DeterministicChatProvider.MODEL
        meta["embedding_model"] = DeterministicEmbeddingsProvider.MODEL
    else:
        meta["chat_model"] = "disabled"
        meta["embedding_model"] = "disabled"
    return meta
