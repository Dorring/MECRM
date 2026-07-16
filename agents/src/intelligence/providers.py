"""Provider boundary for managed and local AI inference.

Only the agents service imports this module. It keeps provider credentials and
transport details outside agent workflows, so switching inference backends does
not alter tenant, policy, or event-processing behavior.
"""
from __future__ import annotations

from typing import Any, Protocol

from langchain_ollama import ChatOllama, OllamaEmbeddings

from orchestrator.config import settings


class ProviderConfigurationError(RuntimeError):
    """Raised before a remote provider can be called with unsafe config."""


class AsyncChatModel(Protocol):
    async def ainvoke(self, input: Any, **kwargs: Any) -> Any: ...


class AsyncEmbeddings(Protocol):
    async def aembed_query(self, text: str) -> list[float]: ...

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...


def create_chat_model(*, temperature: float = 0.0) -> AsyncChatModel:
    """Create the configured chat provider without exposing credentials."""
    if settings.AI_PROVIDER == "ollama":
        return ChatOllama(
            base_url=settings.OLLAMA_URL,
            model=settings.OLLAMA_MODEL,
            temperature=temperature,
        )
    if settings.AI_PROVIDER == "nvidia_nim":
        _validate_nvidia("NVIDIA_CHAT_MODEL")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url=settings.NVIDIA_BASE_URL.rstrip("/"),
            api_key=settings.NVIDIA_API_KEY,
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
    ollama_url: str | None = None,
    embedding_model: str | None = None,
) -> AsyncEmbeddings:
    """Create embeddings from the configured provider family.

    Switching the embedding model requires re-indexing existing Weaviate data;
    callers deliberately do not silently mix vector spaces.
    """
    if settings.AI_PROVIDER == "ollama":
        return OllamaEmbeddings(
            base_url=ollama_url or settings.OLLAMA_URL,
            model=embedding_model or settings.OLLAMA_EMBED_MODEL,
        )
    if settings.AI_PROVIDER == "nvidia_nim":
        _validate_nvidia("NVIDIA_EMBED_MODEL")
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            base_url=settings.NVIDIA_BASE_URL.rstrip("/"),
            api_key=settings.NVIDIA_API_KEY,
            model=settings.NVIDIA_EMBED_MODEL,
            request_timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
            max_retries=settings.AI_MAX_RETRIES,
        )
    raise ProviderConfigurationError(
        "AI_PROVIDER must be 'ollama' or 'nvidia_nim'; "
        f"received {settings.AI_PROVIDER!r}"
    )


def provider_metadata() -> dict[str, str | bool]:
    """Return safe-to-log provider metadata; never include credentials."""
    return {
        "provider": settings.AI_PROVIDER,
        "chat_model": (
            settings.NVIDIA_CHAT_MODEL
            if settings.AI_PROVIDER == "nvidia_nim"
            else settings.OLLAMA_MODEL
        ),
        "embedding_model": (
            settings.NVIDIA_EMBED_MODEL
            if settings.AI_PROVIDER == "nvidia_nim"
            else settings.OLLAMA_EMBED_MODEL
        ),
        "remote": settings.AI_PROVIDER == "nvidia_nim",
    }


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