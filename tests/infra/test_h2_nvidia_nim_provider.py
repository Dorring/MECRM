"""Static regression coverage for the NVIDIA NIM provider boundary."""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
AGENTS_SRC = ROOT / "agents" / "src"
if str(AGENTS_SRC) not in sys.path:
    sys.path.insert(0, str(AGENTS_SRC))

from intelligence import providers


@pytest.fixture
def provider_settings(monkeypatch: pytest.MonkeyPatch):
    original = {
        name: getattr(providers.settings, name)
        for name in (
            "AI_PROVIDER",
            "AI_REQUEST_TIMEOUT_SECONDS",
            "AI_MAX_RETRIES",
            "NVIDIA_BASE_URL",
            "NVIDIA_API_KEY",
            "NVIDIA_CHAT_MODEL",
            "NVIDIA_EMBED_MODEL",
        )
    }
    yield monkeypatch
    for name, value in original.items():
        monkeypatch.setattr(providers.settings, name, value)


def test_nvidia_provider_rejects_missing_key_before_network(provider_settings: pytest.MonkeyPatch) -> None:
    provider_settings.setattr(providers.settings, "AI_PROVIDER", "nvidia_nim")
    provider_settings.setattr(providers.settings, "NVIDIA_API_KEY", "")
    provider_settings.setattr(providers.settings, "NVIDIA_CHAT_MODEL", "nvidia/example-chat")

    with pytest.raises(providers.ProviderConfigurationError, match="NVIDIA_API_KEY"):
        providers.create_chat_model()


def test_nvidia_chat_and_embeddings_use_separate_model_settings(
    provider_settings: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append({"kind": "chat", **kwargs})

    class FakeOpenAIEmbeddings:
        def __init__(self, **kwargs: object) -> None:
            calls.append({"kind": "embedding", **kwargs})

    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = FakeChatOpenAI
    fake_module.OpenAIEmbeddings = FakeOpenAIEmbeddings
    provider_settings.setitem(sys.modules, "langchain_openai", fake_module)
    provider_settings.setattr(providers.settings, "AI_PROVIDER", "nvidia_nim")
    provider_settings.setattr(providers.settings, "NVIDIA_API_KEY", "test-key-not-a-secret")
    provider_settings.setattr(providers.settings, "NVIDIA_CHAT_MODEL", "nvidia/chat-model")
    provider_settings.setattr(providers.settings, "NVIDIA_EMBED_MODEL", "nvidia/embedding-model")
    provider_settings.setattr(providers.settings, "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1/")
    provider_settings.setattr(providers.settings, "AI_REQUEST_TIMEOUT_SECONDS", 17.0)
    provider_settings.setattr(providers.settings, "AI_MAX_RETRIES", 3)

    providers.create_chat_model(temperature=0.2)
    providers.create_embeddings()

    assert calls == [
        {
            "kind": "chat",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "test-key-not-a-secret",
            "model": "nvidia/chat-model",
            "temperature": 0.2,
            "timeout": 17.0,
            "max_retries": 3,
        },
        {
            "kind": "embedding",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "test-key-not-a-secret",
            "model": "nvidia/embedding-model",
            "request_timeout": 17.0,
            "max_retries": 3,
        },
    ]


def test_provider_metadata_never_contains_api_key(provider_settings: pytest.MonkeyPatch) -> None:
    provider_settings.setattr(providers.settings, "AI_PROVIDER", "nvidia_nim")
    provider_settings.setattr(providers.settings, "NVIDIA_API_KEY", "do-not-log-me")
    provider_settings.setattr(providers.settings, "NVIDIA_CHAT_MODEL", "nvidia/chat-model")
    provider_settings.setattr(providers.settings, "NVIDIA_EMBED_MODEL", "nvidia/embedding-model")

    metadata = providers.provider_metadata()

    assert metadata["provider"] == "nvidia_nim"
    assert metadata["chat_model"] == "nvidia/chat-model"
    assert metadata["embedding_model"] == "nvidia/embedding-model"
    assert "do-not-log-me" not in repr(metadata)


def test_agents_container_is_the_only_compose_service_receiving_nvidia_variables() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    agents_section = text.split("  agents:\n", 1)[1].split("  # Replay service:", 1)[0]

    assert "NVIDIA_API_KEY=${NVIDIA_API_KEY:-}" in agents_section
    assert "NVIDIA_API_KEY=${NVIDIA_API_KEY:-}" not in text.replace(agents_section, "")


def test_no_agent_module_constructs_a_provider_outside_boundary() -> None:
    for source in (ROOT / "agents" / "src").rglob("*.py"):
        if source.name == "providers.py":
            continue
        text = source.read_text(encoding="utf-8")
        assert "from langchain_ollama import" not in text, source
        assert "from langchain_openai import" not in text, source


def test_documentation_records_reindex_requirement() -> None:
    text = (ROOT / "docs" / "interview" / "nvidia-nim.md").read_text(encoding="utf-8")
    assert "NVIDIA_CHAT_MODEL" in text
    assert "NVIDIA_EMBED_MODEL" in text
    assert "rebuild" in text.lower()