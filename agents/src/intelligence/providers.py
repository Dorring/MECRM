"""Provider boundary for managed and local AI inference.

Only the agents service imports this module. It keeps provider credentials and
transport details outside agent workflows, so switching inference backends does
not alter tenant, policy, or event-processing behavior.

AI_MODE gating (Phase 1):
  - disabled      → returns Disabled* providers; no network
  - deterministic → returns Deterministic* providers; no network
  - live          → returns real ChatOllama / ChatOpenAI etc.
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain_ollama import ChatOllama, OllamaEmbeddings
from pydantic import SecretStr

from orchestrator.ai_mode import AIMode, AIProvider, resolve_ai_mode, resolve_ai_provider
from orchestrator.config import settings


class ProviderConfigurationError(RuntimeError):
    """Raised before a remote provider can be called with unsafe config."""


class AIModeDisabledError(RuntimeError):
    """Raised when AI functionality is invoked but AI_MODE=disabled."""


class AsyncChatModel(Protocol):
    async def ainvoke(self, input: Any, **kwargs: Any) -> Any: ...


class AsyncEmbeddings(Protocol):
    async def aembed_query(self, text: str) -> list[float]: ...

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Disabled providers (AI_MODE=disabled)
# ---------------------------------------------------------------------------


class DisabledChatProvider:
    """Chat provider that always returns a disabled/unavailable response."""

    async def ainvoke(self, input: Any, **kwargs: Any) -> Any:
        raise AIModeDisabledError(
            "AI is disabled (AI_MODE=disabled). "
            "Chat model inference is not available."
        )


class DisabledEmbeddingsProvider:
    """Embeddings provider that always raises."""

    async def aembed_query(self, text: str) -> list[float]:
        raise AIModeDisabledError(
            "AI is disabled (AI_MODE=disabled). "
            "Embeddings are not available."
        )

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AIModeDisabledError(
            "AI is disabled (AI_MODE=disabled). "
            "Embeddings are not available."
        )


def create_chat_model(*, temperature: float = 0.0) -> AsyncChatModel:
    """Create the configured chat provider without exposing credentials.

    AI_MODE gating (Phase 1):
      - disabled      → DisabledChatProvider (raises on invoke)
      - deterministic → DeterministicChatProvider (no network)
      - live          → real ChatOllama / ChatOpenAI
    """
    mode = resolve_ai_mode()

    if mode is AIMode.DISABLED:
        return DisabledChatProvider()  # type: ignore[return-value]

    if mode is AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import DeterministicChatProvider
        return DeterministicChatProvider()  # type: ignore[return-value]

    # AI_MODE=live — real provider
    provider = resolve_ai_provider(mode)
    if provider is None:
        raise ProviderConfigurationError(
            "AI_MODE=live requires a valid AI_PROVIDER (ollama or nvidia_nim); "
            f"received {settings.AI_PROVIDER!r}"
        )

    if provider is AIProvider.OLLAMA:
        return ChatOllama(
            base_url=settings.OLLAMA_URL,
            model=settings.OLLAMA_MODEL,
            temperature=temperature,
        )
    if provider is AIProvider.NVIDIA_NIM:
        _validate_nvidia("NVIDIA_CHAT_MODEL")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url=settings.NVIDIA_BASE_URL.rstrip("/"),
            api_key=SecretStr(settings.NVIDIA_API_KEY),
            model=settings.NVIDIA_CHAT_MODEL,
            temperature=temperature,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
            max_retries=settings.AI_MAX_RETRIES,
        )
    raise ProviderConfigurationError(
        "AI_PROVIDER must be 'ollama' or 'nvidia_nim'; "
        f"received {settings.AI_PROVIDER!r}"
    )


def create_embeddings(
    *,
    base_url: str | None = None,
    model: str | None = None,
    # Deprecated parameter names — kept for backward compatibility.
    # Prefer *base_url* and *model* in new code.
    ollama_url: str | None = None,
    embedding_model: str | None = None,
) -> AsyncEmbeddings:
    """Create embeddings from the configured provider family.

    Switching the embedding model requires re-indexing existing Weaviate data;
    callers deliberately do not silently mix vector spaces.

    AI_MODE gating (Phase 1):
      - disabled      → DisabledEmbeddingsProvider (raises on invoke)
      - deterministic → DeterministicEmbeddingsProvider (no network)
      - live          → real OllamaEmbeddings / OpenAIEmbeddings
    """
    # Resolve deprecated parameter names
    resolved_url = base_url or ollama_url
    resolved_model = model or embedding_model

    mode = resolve_ai_mode()

    if mode is AIMode.DISABLED:
        return DisabledEmbeddingsProvider()  # type: ignore[return-value]

    if mode is AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider
        return DeterministicEmbeddingsProvider()  # type: ignore[return-value]

    # AI_MODE=live — real provider
    provider = resolve_ai_provider(mode)
    if provider is None:
        raise ProviderConfigurationError(
            "AI_MODE=live requires a valid AI_PROVIDER (ollama or nvidia_nim); "
            f"received {settings.AI_PROVIDER!r}"
        )

    if provider is AIProvider.OLLAMA:
        return OllamaEmbeddings(
            base_url=resolved_url or settings.OLLAMA_URL,
            model=resolved_model or settings.OLLAMA_EMBED_MODEL,
        )
    if provider is AIProvider.NVIDIA_NIM:
        _validate_nvidia("NVIDIA_EMBED_MODEL")
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            base_url=settings.NVIDIA_BASE_URL.rstrip("/"),
            api_key=SecretStr(settings.NVIDIA_API_KEY),
            model=settings.NVIDIA_EMBED_MODEL,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
            max_retries=settings.AI_MAX_RETRIES,
        )
    raise ProviderConfigurationError(
        "AI_PROVIDER must be 'ollama' or 'nvidia_nim'; "
        f"received {settings.AI_PROVIDER!r}"
    )


def provider_metadata() -> dict[str, str | bool]:
    """Return safe-to-log provider metadata; never include credentials."""
    mode = resolve_ai_mode()
    provider = resolve_ai_provider(mode)
    meta: dict[str, str | bool] = {
        "ai_mode": mode.value,
        "provider": provider.value if provider else "none",
        "chat_model": (
            settings.NVIDIA_CHAT_MODEL
            if provider is AIProvider.NVIDIA_NIM
            else settings.OLLAMA_MODEL if provider is AIProvider.OLLAMA
            else "disabled"
        ),
        "embedding_model": (
            settings.NVIDIA_EMBED_MODEL
            if provider is AIProvider.NVIDIA_NIM
            else settings.OLLAMA_EMBED_MODEL if provider is AIProvider.OLLAMA
            else "disabled"
        ),
        "remote": provider is AIProvider.NVIDIA_NIM,
    }
    return meta


async def provider_health_check() -> dict[str, Any]:
    """Check readiness of the configured AI provider.

    Returns a dict with:
      - status: "ready" | "degraded" | "unavailable"
      - ai_mode: the resolved AI_MODE
      - provider: the active provider name
      - checks: per-endpoint health detail (live mode only)
      - error: present only when a check fails

    Never returns API keys or secrets.
    """
    import httpx

    mode = resolve_ai_mode()
    provider = resolve_ai_provider(mode)

    base: dict[str, Any] = {
        "ai_mode": mode.value,
        "provider": provider.value if provider else "none",
    }

    if mode is AIMode.DISABLED:
        base["status"] = "ready"
        base["checks"] = {"model": "skipped", "embeddings": "skipped"}
        return base

    if mode is AIMode.DETERMINISTIC:
        # Verify deterministic provider can initialise
        try:
            from intelligence.deterministic_provider import (
                DeterministicChatProvider,
                DeterministicEmbeddingsProvider,
            )
            chat = DeterministicChatProvider()
            embed = DeterministicEmbeddingsProvider()
            # Quick smoke test
            test_vec = await embed.aembed_query("health-check")
            base["status"] = "ready"
            base["checks"] = {
                "chat": "available",
                "embeddings": "available",
                "embedding_dimension": len(test_vec),
            }
        except Exception as exc:
            base["status"] = "degraded"
            base["error"] = f"deterministic_provider_init_failed: {exc}"
            base["checks"] = {"chat": "failed", "embeddings": "failed"}
        return base

    # AI_MODE=live — check the configured provider
    if provider is None:
        base["status"] = "unavailable"
        base["error"] = (
            "AI_MODE=live requires AI_PROVIDER to be set to "
            "'ollama' or 'nvidia_nim'"
        )
        return base

    checks: dict[str, str] = {}
    status = "ready"

    if provider is AIProvider.OLLAMA:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.OLLAMA_URL}/api/tags")
                if resp.status_code == 200:
                    checks["ollama_endpoint"] = "reachable"
                else:
                    checks["ollama_endpoint"] = f"unexpected_status_{resp.status_code}"
                    status = "degraded"
        except Exception as exc:
            checks["ollama_endpoint"] = f"unreachable: {exc}"
            status = "degraded"
        # Check chat model
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{settings.OLLAMA_URL}/api/show",
                    json={"name": settings.OLLAMA_MODEL},
                )
                if resp.status_code == 200:
                    checks["chat_model"] = "available"
                else:
                    checks["chat_model"] = f"not_found ({settings.OLLAMA_MODEL})"
                    status = "degraded"
        except Exception as exc:
            checks["chat_model"] = f"check_failed: {exc}"
            status = "degraded"

    elif provider is AIProvider.NVIDIA_NIM:
        try:
            _validate_nvidia("NVIDIA_CHAT_MODEL")
            checks["nvidia_config"] = "valid"
        except ProviderConfigurationError as exc:
            checks["nvidia_config"] = str(exc)
            status = "degraded"
        if settings.NVIDIA_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{settings.NVIDIA_BASE_URL.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {settings.NVIDIA_API_KEY}"},
                    )
                    checks["nvidia_endpoint"] = (
                        "reachable" if resp.status_code < 500 else f"status_{resp.status_code}"
                    )
                    if resp.status_code >= 500:
                        status = "degraded"
            except Exception as exc:
                checks["nvidia_endpoint"] = f"unreachable: {exc}"
                status = "degraded"

    base["status"] = status
    base["checks"] = checks
    return base


def vector_collection_name(base_name: str) -> str:
    """Return a provider-aware Weaviate collection name.

    Keeps vectors from different embedding providers in separate
    collections so switching providers does not silently mix vector spaces.
    """
    mode = resolve_ai_mode()
    if mode is not AIMode.LIVE:
        return f"{base_name}_{mode.value}"
    provider = resolve_ai_provider(mode)
    suffix = provider.value if provider else "unknown"
    return f"{base_name}_{suffix}"


def _validate_nvidia(required_model_env: str) -> None:
    missing: list[str] = []
    if not settings.NVIDIA_API_KEY.strip():
        missing.append("NVIDIA_API_KEY")
    if required_model_env == "NVIDIA_CHAT_MODEL" and not settings.NVIDIA_CHAT_MODEL.strip():
        missing.append("NVIDIA_CHAT_MODEL")
    if required_model_env == "NVIDIA_EMBED_MODEL" and not settings.NVIDIA_EMBED_MODEL.strip():
        missing.append("NVIDIA_EMBED_MODEL")
    if missing:
        raise ProviderConfigurationError(
            "AI_PROVIDER=nvidia_nim requires " + ", ".join(missing)
        )
