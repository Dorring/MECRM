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

from orchestrator.ai_mode import (
    AIMode, AIProvider, AIConfigurationError,
    resolve_ai_mode, resolve_ai_provider,
)
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

    if mode == AIMode.DISABLED:
        return DisabledChatProvider()  # type: ignore[return-value]

    if mode == AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import DeterministicChatProvider
        return DeterministicChatProvider()  # type: ignore[return-value]

    # AI_MODE=live — real provider
    provider = resolve_ai_provider(mode)
    if provider is None:
        raise ProviderConfigurationError(
            "AI_MODE=live requires a valid AI_PROVIDER (ollama or nvidia_nim); "
            f"received {settings.AI_PROVIDER!r}"
        )

    if provider == AIProvider.OLLAMA:
        return ChatOllama(
            base_url=settings.OLLAMA_URL,
            model=settings.OLLAMA_MODEL,
            temperature=temperature,
        )
    if provider == AIProvider.NVIDIA_NIM:
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

    if mode == AIMode.DISABLED:
        return DisabledEmbeddingsProvider()  # type: ignore[return-value]

    if mode == AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider
        return DeterministicEmbeddingsProvider()  # type: ignore[return-value]

    # AI_MODE=live — real provider
    provider = resolve_ai_provider(mode)
    if provider is None:
        raise ProviderConfigurationError(
            "AI_MODE=live requires a valid AI_PROVIDER (ollama or nvidia_nim); "
            f"received {settings.AI_PROVIDER!r}"
        )

    if provider == AIProvider.OLLAMA:
        return OllamaEmbeddings(
            base_url=resolved_url or settings.OLLAMA_URL,
            model=resolved_model or settings.OLLAMA_EMBED_MODEL,
        )
    if provider == AIProvider.NVIDIA_NIM:
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

    if mode == AIMode.DISABLED:
        chat_model = "disabled"
        embedding_model = "disabled"
    elif mode is AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            DeterministicEmbeddingsProvider,
        )
        chat_model = DeterministicChatProvider.MODEL
        embedding_model = DeterministicEmbeddingsProvider.MODEL
    elif provider == AIProvider.NVIDIA_NIM:
        chat_model = settings.NVIDIA_CHAT_MODEL
        embedding_model = settings.NVIDIA_EMBED_MODEL
    elif provider == AIProvider.OLLAMA:
        chat_model = settings.OLLAMA_MODEL
        embedding_model = settings.OLLAMA_EMBED_MODEL
    else:
        chat_model = "disabled"
        embedding_model = "disabled"

    return {
        "ai_mode": mode.value,
        "provider": _provider_label(mode, provider),
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "remote": provider == AIProvider.NVIDIA_NIM,
    }


async def provider_health_check() -> dict[str, Any]:
    """Check readiness of the configured AI provider.

    Returns a dict with:
      - status: "ready" | "degraded" | "unavailable"
      - ai_mode: the resolved AI_MODE
      - provider: the active provider name
      - chat_model: model name (safe, no credentials)
      - embedding_model: model name (safe, no credentials)
      - checks: per-endpoint health detail
      - error: present only when a check fails (safe, no secrets)

    Never returns API keys, Authorization headers, or raw internal exceptions.
    """
    import httpx

    def _safe_err(exc: Exception) -> str:
        """Return a safe error label; never include raw exception internals."""
        return type(exc).__name__

    try:
        mode = resolve_ai_mode()
        provider = resolve_ai_provider(mode)
    except AIConfigurationError as exc:
        # Fail-fast validation error → unavailable
        return {
            "ai_mode": (settings.AI_MODE or "deterministic"),
            "provider": "unknown",
            "status": "unavailable",
            "chat_model": "unset",
            "embedding_model": "unset",
            "error": str(exc),
        }

    base: dict[str, Any] = {
        "ai_mode": mode.value,
        "provider": _provider_label(mode, provider),
    }

    # -- disabled -----------------------------------------------------------
    if mode == AIMode.DISABLED:
        base["status"] = "ready"
        base["chat_model"] = "disabled"
        base["embedding_model"] = "disabled"
        base["checks"] = {"chat_model": "skipped", "embedding_model": "skipped"}
        return base

    # -- deterministic -----------------------------------------------------
    if mode == AIMode.DETERMINISTIC:
        try:
            from intelligence.deterministic_provider import (
                DeterministicChatProvider,
                DeterministicEmbeddingsProvider,
            )
            chat = DeterministicChatProvider()
            embed = DeterministicEmbeddingsProvider()
            test_vec = await embed.aembed_query("health-check")
            base["status"] = "ready"
            base["chat_model"] = DeterministicChatProvider.MODEL
            base["embedding_model"] = DeterministicEmbeddingsProvider.MODEL
            base["checks"] = {
                "chat_model": "available",
                "embedding_model": "available",
                "embedding_dimension": len(test_vec),
            }
        except Exception as exc:
            base["status"] = "degraded"
            base["chat_model"] = "deterministic-chat-v1"
            base["embedding_model"] = "deterministic-embed-v1"
            base["error"] = f"deterministic_provider_init_failed: {_safe_err(exc)}"
            base["checks"] = {"chat_model": "failed", "embedding_model": "failed"}
        return base

    # -- live (no provider) ------------------------------------------------
    if provider is None:
        base["status"] = "unavailable"
        base["chat_model"] = "unset"
        base["embedding_model"] = "unset"
        base["error"] = (
            "AI_MODE=live requires AI_PROVIDER to be set to "
            "'ollama' or 'nvidia_nim'"
        )
        return base

    # -- live + ollama -----------------------------------------------------
    if provider == AIProvider.OLLAMA:
        base["chat_model"] = settings.OLLAMA_MODEL
        base["embedding_model"] = settings.OLLAMA_EMBED_MODEL
        checks: dict[str, str] = {}
        status = "ready"

        # 1. Ollama endpoint reachable
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.OLLAMA_URL}/api/tags")
                if resp.status_code == 200:
                    checks["ollama_endpoint"] = "reachable"
                else:
                    checks["ollama_endpoint"] = f"http_{resp.status_code}"
                    status = "degraded"
        except Exception as exc:
            checks["ollama_endpoint"] = _safe_err(exc)
            status = "degraded"

        # 2. Chat model available
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{settings.OLLAMA_URL}/api/show",
                    json={"name": settings.OLLAMA_MODEL},
                )
                if resp.status_code == 200:
                    checks["chat_model"] = "available"
                else:
                    checks["chat_model"] = f"http_{resp.status_code}"
                    status = "degraded"
        except Exception as exc:
            checks["chat_model"] = _safe_err(exc)
            status = "degraded"

        # 3. Embedding model available (separate model!)
        if settings.OLLAMA_EMBED_MODEL != settings.OLLAMA_MODEL:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{settings.OLLAMA_URL}/api/show",
                        json={"name": settings.OLLAMA_EMBED_MODEL},
                    )
                    if resp.status_code == 200:
                        checks["embedding_model"] = "available"
                    else:
                        checks["embedding_model"] = f"http_{resp.status_code}"
                        status = "degraded"
            except Exception as exc:
                checks["embedding_model"] = _safe_err(exc)
                status = "degraded"
        else:
            checks["embedding_model"] = "same_as_chat"

        base["status"] = status
        base["checks"] = checks
        return base

    # -- live + nvidia_nim -------------------------------------------------
    if provider == AIProvider.NVIDIA_NIM:
        base["chat_model"] = settings.NVIDIA_CHAT_MODEL or "unset"
        base["embedding_model"] = settings.NVIDIA_EMBED_MODEL or "unset"
        checks: dict[str, str] = {}
        status = "ready"

        # Validate config
        try:
            _validate_nvidia("NVIDIA_CHAT_MODEL")
            _validate_nvidia("NVIDIA_EMBED_MODEL")
            checks["nvidia_config"] = "valid"
        except ProviderConfigurationError as exc:
            checks["nvidia_config"] = _safe_err(exc)
            status = "degraded"

        # Probe endpoint if API key is set (single real request only)
        if settings.NVIDIA_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{settings.NVIDIA_BASE_URL.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {settings.NVIDIA_API_KEY}"},
                    )
                    if resp.status_code == 200:
                        checks["nvidia_endpoint"] = "reachable"
                    elif resp.status_code in (401, 403):
                        checks["nvidia_endpoint"] = f"auth_failed_{resp.status_code}"
                        status = "degraded"
                    elif resp.status_code == 404:
                        checks["nvidia_endpoint"] = "endpoint_not_found"
                        status = "degraded"
                    elif resp.status_code >= 500:
                        checks["nvidia_endpoint"] = f"server_error_{resp.status_code}"
                        status = "degraded"
                    else:
                        checks["nvidia_endpoint"] = f"http_{resp.status_code}"
                        status = "degraded"
            except Exception as exc:
                checks["nvidia_endpoint"] = _safe_err(exc)
                status = "degraded"

        base["status"] = status
        base["checks"] = checks
        return base

    # fallback (should be unreachable)
    base["status"] = "unavailable"
    base["error"] = "unknown provider"
    return base


def vector_collection_name(base_name: str) -> str:
    """Return a provider-and-model-aware Weaviate collection name.

    Keeps vectors from different embedding providers AND different models
    in separate collections. Switching embedding models requires re-indexing.

    Naming scheme: {base}_{mode_or_provider}_{model_fingerprint}
    """
    import hashlib

    mode = resolve_ai_mode()

    # -- disabled -----------------------------------------------------------
    if mode == AIMode.DISABLED:
        safe_name = _sanitise_weaviate_name(base_name)
        return f"{safe_name}_disabled"

    # -- deterministic ------------------------------------------------------
    if mode == AIMode.DETERMINISTIC:
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider
        ident = DeterministicEmbeddingsProvider.MODEL
        safe_name = _sanitise_weaviate_name(base_name)
        safe_ident = _sanitise_weaviate_name(ident)
        return f"{safe_name}_deterministic_{safe_ident}"

    # -- live — resolve provider (may raise AIConfigurationError) ------------
    provider = resolve_ai_provider(mode)  # can raise — let it propagate

    if provider == AIProvider.OLLAMA:
        ident = settings.OLLAMA_EMBED_MODEL
    elif provider == AIProvider.NVIDIA_NIM:
        ident = settings.NVIDIA_EMBED_MODEL
    else:
        ident = "unknown"

    provider_str = provider.value
    model_hash = hashlib.sha256(ident.encode()).hexdigest()[:8]
    safe_name = _sanitise_weaviate_name(base_name)
    ident_str = f"{provider_str}_{model_hash}"
    safe_ident = _sanitise_weaviate_name(ident_str)
    return f"{safe_name}_{safe_ident}"


def _sanitise_weaviate_name(raw: str) -> str:
    """Replace characters that Weaviate dislikes in class names."""
    return raw.replace(" ", "_").replace("-", "_").replace("/", "_").replace(".", "_")


def _provider_label(mode: AIMode, provider: AIProvider | None) -> str:
    """Return the canonical provider label for any mode/provider combination."""
    if mode == AIMode.DISABLED:
        return "disabled"
    if mode == AIMode.DETERMINISTIC:
        return "deterministic"
    if provider == AIProvider.OLLAMA:
        return "ollama"
    if provider == AIProvider.NVIDIA_NIM:
        return "nvidia_nim"
    return "unknown"


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
