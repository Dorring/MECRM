"""Phase 1 tests: AI_MODE configuration, Provider factory, network isolation,
deterministic provider, and readiness.

All tests MUST pass without Ollama running and without an NVIDIA API key.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

# Ensure the src dir is on the path (needed when running tests/unit directly).
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(**kwargs: str) -> None:
    """Set env vars for a test; restore after."""
    for k, v in kwargs.items():
        os.environ[k] = v


def _del_env(*keys: str) -> None:
    for k in keys:
        os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


class TestAIModeResolution:
    """AI_MODE env var → AIMode enum."""

    def test_disabled_mode_resolves(self):
        _set_env(AI_MODE="disabled")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode
            assert resolve_ai_mode() is AIMode.DISABLED
        finally:
            _del_env("AI_MODE")

    def test_deterministic_mode_resolves(self):
        _set_env(AI_MODE="deterministic")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode
            assert resolve_ai_mode() is AIMode.DETERMINISTIC
        finally:
            _del_env("AI_MODE")

    def test_live_mode_resolves(self):
        _set_env(AI_MODE="live")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode
            assert resolve_ai_mode() is AIMode.LIVE
        finally:
            _del_env("AI_MODE")

    def test_default_is_deterministic_when_unset(self):
        _del_env("AI_MODE")
        from orchestrator.ai_mode import resolve_ai_mode, AIMode
        assert resolve_ai_mode() is AIMode.DETERMINISTIC

    def test_invalid_mode_defaults_to_deterministic(self):
        _set_env(AI_MODE="garbage")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode
            assert resolve_ai_mode() is AIMode.DETERMINISTIC
        finally:
            _del_env("AI_MODE")

    def test_live_without_provider_returns_none(self):
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, resolve_ai_provider, AIMode
            mode = resolve_ai_mode()
            assert mode is AIMode.LIVE
            assert resolve_ai_provider(mode) is None
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_disabled_mode_does_not_trigger_provider_connection_flag(self):
        _set_env(AI_MODE="disabled")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode
            mode = resolve_ai_mode()
            assert mode.requires_network is False
            assert mode.allows_model_init is False
        finally:
            _del_env("AI_MODE")

    def test_ai_provider_ignored_in_non_live_mode(self):
        """AI_PROVIDER=ollama with AI_MODE=deterministic should NOT resolve a provider."""
        _set_env(AI_MODE="deterministic", AI_PROVIDER="ollama")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, resolve_ai_provider
            mode = resolve_ai_mode()
            assert resolve_ai_provider(mode) is None
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Provider factory tests
# ---------------------------------------------------------------------------


class TestProviderFactory:
    """create_chat_model / create_embeddings with different AI_MODE values."""

    def test_disabled_returns_disabled_chat_provider(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import create_chat_model, DisabledChatProvider
            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DisabledChatProvider)
        finally:
            _del_env("AI_MODE")

    def test_disabled_returns_disabled_embeddings_provider(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import create_embeddings, DisabledEmbeddingsProvider
            provider = create_embeddings()
            assert isinstance(provider, DisabledEmbeddingsProvider)
        finally:
            _del_env("AI_MODE")

    def test_deterministic_returns_deterministic_chat_provider(self):
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import create_chat_model
            from intelligence.deterministic_provider import DeterministicChatProvider
            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DeterministicChatProvider)
        finally:
            _del_env("AI_MODE")

    def test_deterministic_returns_deterministic_embeddings_provider(self):
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import create_embeddings
            from intelligence.deterministic_provider import DeterministicEmbeddingsProvider
            provider = create_embeddings()
            assert isinstance(provider, DeterministicEmbeddingsProvider)
        finally:
            _del_env("AI_MODE")

    def test_live_without_provider_raises(self):
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from intelligence.providers import create_chat_model, ProviderConfigurationError
            with pytest.raises(ProviderConfigurationError):
                create_chat_model(temperature=0)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_no_implicit_fallback_from_deterministic_to_live(self):
        """AI_MODE=deterministic must not fall back to Ollama even if AI_PROVIDER=ollama."""
        _set_env(AI_MODE="deterministic", AI_PROVIDER="ollama")
        try:
            from intelligence.providers import create_chat_model
            from intelligence.deterministic_provider import DeterministicChatProvider
            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DeterministicChatProvider)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Network isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="socket.socket monkeypatching behaves differently on Windows",
)
class TestNetworkIsolation:
    """Confirm disabled/deterministic modes never create network connections."""

    def test_disabled_chat_does_not_create_socket(self, monkeypatch):
        import socket
        original = socket.socket
        created: list[socket.socket] = []

        def _tracking_socket(*args, **kwargs):
            s = original(*args, **kwargs)
            created.append(s)
            return s

        monkeypatch.setattr(socket, "socket", _tracking_socket)
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import create_chat_model
            create_chat_model(temperature=0)
            # The DisabledChatProvider should be created without opening any socket.
            assert len(created) == 0, (
                f"Expected 0 socket creations in disabled mode, got {len(created)}"
            )
        finally:
            _del_env("AI_MODE")

    def test_deterministic_chat_does_not_create_socket(self, monkeypatch):
        import socket
        original = socket.socket
        created: list[socket.socket] = []

        def _tracking_socket(*args, **kwargs):
            s = original(*args, **kwargs)
            created.append(s)
            return s

        monkeypatch.setattr(socket, "socket", _tracking_socket)
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import create_chat_model
            create_chat_model(temperature=0)
            assert len(created) == 0, (
                f"Expected 0 socket creations in deterministic mode, got {len(created)}"
            )
        finally:
            _del_env("AI_MODE")

    def test_default_config_does_not_create_socket(self, monkeypatch):
        """Default (AI_MODE unset) should not trigger socket creation."""
        import socket
        original = socket.socket
        created: list[socket.socket] = []

        def _tracking_socket(*args, **kwargs):
            s = original(*args, **kwargs)
            created.append(s)
            return s

        monkeypatch.setattr(socket, "socket", _tracking_socket)
        _del_env("AI_MODE", "AI_PROVIDER")
        try:
            from intelligence.providers import create_chat_model
            create_chat_model(temperature=0)
            assert len(created) == 0, (
                f"Expected 0 socket creations with default config, got {len(created)}"
            )
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Deterministic provider tests
# ---------------------------------------------------------------------------


class TestDeterministicChatProvider:
    """DeterministicChatProvider invariants."""

    @pytest.mark.asyncio
    async def test_same_input_same_output(self):
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        msgs = [HumanMessage(content="What is the renewal policy?")]
        r1 = await provider.ainvoke(msgs)
        r2 = await provider.ainvoke(msgs)
        assert r1.content == r2.content

    @pytest.mark.asyncio
    async def test_different_input_different_output(self):
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        r1 = await provider.ainvoke([HumanMessage(content="Query A")])
        r2 = await provider.ainvoke([HumanMessage(content="Query B")])
        assert r1.content != r2.content

    @pytest.mark.asyncio
    async def test_output_is_valid_json(self):
        import json
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        msgs = [HumanMessage(content="Test")]
        r = await provider.ainvoke(msgs)
        parsed = json.loads(r.content)
        assert "analysis" in parsed
        assert "confidence" in parsed
        assert "status" in parsed
        assert parsed["status"] == "completed"

    @pytest.mark.asyncio
    async def test_provider_metadata_stable(self):
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        r = await provider.ainvoke([HumanMessage(content="Test")])
        assert r.response_metadata["provider"] == "deterministic"
        assert r.response_metadata["model"] == "deterministic-chat-v1"
        assert "usage" in r.response_metadata
        usage = r.response_metadata["usage"]
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]

    @pytest.mark.asyncio
    async def test_fixture_registered_response(self):
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            register_fixture,
            clear_fixtures,
        )
        from langchain_core.messages import HumanMessage

        register_fixture("test-scenario", '{"result": "fixture-response"}')
        try:
            provider = DeterministicChatProvider()
            r = await provider.ainvoke(
                [HumanMessage(content="irrelevant")],
                scenario_id="test-scenario",
            )
            assert r.content == '{"result": "fixture-response"}'
        finally:
            clear_fixtures()

    # Fault injection tests

    @pytest.mark.asyncio
    async def test_fault_timeout_raises(self):
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            DeterministicTimeoutError,
        )
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        with pytest.raises(DeterministicTimeoutError):
            await provider.ainvoke(
                [HumanMessage(content="test")],
                scenario_id="error/timeout",
            )

    @pytest.mark.asyncio
    async def test_fault_empty_returns_empty_string(self):
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        r = await provider.ainvoke(
            [HumanMessage(content="test")],
            scenario_id="error/empty",
        )
        assert r.content == ""

    @pytest.mark.asyncio
    async def test_fault_malformed_returns_malformed(self):
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        r = await provider.ainvoke(
            [HumanMessage(content="test")],
            scenario_id="error/malformed",
        )
        assert r.content == "{not valid json [}"

    @pytest.mark.asyncio
    async def test_fault_low_confidence(self):
        import json
        from intelligence.deterministic_provider import DeterministicChatProvider
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        r = await provider.ainvoke(
            [HumanMessage(content="test")],
            scenario_id="error/low_confidence",
        )
        parsed = json.loads(r.content)
        assert parsed["confidence"] == 0.12
        assert parsed["status"] == "low_confidence"

    @pytest.mark.asyncio
    async def test_fault_provider_error_raises(self):
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            DeterministicProviderError,
        )
        from langchain_core.messages import HumanMessage

        provider = DeterministicChatProvider()
        with pytest.raises(DeterministicProviderError):
            await provider.ainvoke(
                [HumanMessage(content="test")],
                scenario_id="error/provider_error",
            )


class TestDeterministicEmbeddingsProvider:
    """DeterministicEmbeddingsProvider invariants."""

    @pytest.mark.asyncio
    async def test_same_input_same_vector(self):
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider

        provider = DeterministicEmbeddingsProvider()
        v1 = await provider.aembed_query("test text")
        v2 = await provider.aembed_query("test text")
        assert v1 == v2

    @pytest.mark.asyncio
    async def test_different_input_different_vector(self):
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider

        provider = DeterministicEmbeddingsProvider()
        v1 = await provider.aembed_query("text A")
        v2 = await provider.aembed_query("text B")
        assert v1 != v2

    @pytest.mark.asyncio
    async def test_vector_dimension_is_fixed(self):
        from intelligence.deterministic_provider import (
            DeterministicEmbeddingsProvider,
            DETERMINISTIC_EMBEDDING_DIM,
        )

        provider = DeterministicEmbeddingsProvider()
        v = await provider.aembed_query("test")
        assert len(v) == DETERMINISTIC_EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_unit_vector(self):
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider

        provider = DeterministicEmbeddingsProvider()
        v = await provider.aembed_query("test")
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-9

    @pytest.mark.asyncio
    async def test_aembed_documents(self):
        from intelligence.deterministic_provider import DeterministicEmbeddingsProvider

        provider = DeterministicEmbeddingsProvider()
        vecs = await provider.aembed_documents(["text A", "text B", "text C"])
        assert len(vecs) == 3
        assert vecs[0] != vecs[1]
        # Same as aembed_query for identical text
        v_query = await provider.aembed_query("text A")
        assert vecs[0] == v_query


# ---------------------------------------------------------------------------
# Readiness / health tests
# ---------------------------------------------------------------------------


class TestProviderHealthCheck:
    """provider_health_check returns correct status per mode."""

    @pytest.mark.asyncio
    async def test_disabled_returns_ready(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import provider_health_check
            result = await provider_health_check()
            assert result["status"] == "ready"
            assert result["checks"]["model"] == "skipped"
            assert result["checks"]["embeddings"] == "skipped"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_deterministic_returns_ready(self):
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import provider_health_check
            result = await provider_health_check()
            assert result["status"] == "ready"
            assert result["checks"]["chat"] == "available"
            assert result["checks"]["embeddings"] == "available"
            assert result["checks"]["embedding_dimension"] > 0
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_live_without_provider_returns_unavailable(self):
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from intelligence.providers import provider_health_check
            result = await provider_health_check()
            assert result["status"] == "unavailable"
            assert "error" in result
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    @pytest.mark.asyncio
    async def test_health_check_does_not_leak_api_key(self):
        _set_env(AI_MODE="live", AI_PROVIDER="nvidia_nim",
                  NVIDIA_API_KEY="sk-1234567890",
                  NVIDIA_CHAT_MODEL="meta/llama3-70b")
        try:
            from intelligence.providers import provider_health_check
            result = await provider_health_check()
            result_str = str(result)
            assert "sk-1234567890" not in result_str
        finally:
            _del_env("AI_MODE", "AI_PROVIDER", "NVIDIA_API_KEY", "NVIDIA_CHAT_MODEL")

    @pytest.mark.asyncio
    async def test_disabled_does_not_probe_ollama(self, monkeypatch):
        """AI_MODE=disabled must not make any HTTP call to Ollama."""
        import httpx

        calls: list[str] = []

        async def _tracking_get(*args, **kwargs):
            calls.append(f"GET {args[0] if args else ''}")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        _set_env(AI_MODE="disabled", AI_PROVIDER="ollama")
        try:
            from intelligence.providers import provider_health_check
            await provider_health_check()
            assert len(calls) == 0, (
                f"Expected 0 HTTP calls in disabled mode, got {len(calls)}: {calls}"
            )
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Disabled provider invocations
# ---------------------------------------------------------------------------


class TestDisabledProviderBehavior:
    """Disabled providers raise AIModeDisabledError on invocation."""

    @pytest.mark.asyncio
    async def test_disabled_chat_raises_on_invoke(self):
        from intelligence.providers import DisabledChatProvider, AIModeDisabledError

        provider = DisabledChatProvider()
        with pytest.raises(AIModeDisabledError, match="disabled"):
            await provider.ainvoke([{"content": "test"}])

    @pytest.mark.asyncio
    async def test_disabled_embeddings_raises_on_query(self):
        from intelligence.providers import DisabledEmbeddingsProvider, AIModeDisabledError

        provider = DisabledEmbeddingsProvider()
        with pytest.raises(AIModeDisabledError, match="disabled"):
            await provider.aembed_query("test")

    @pytest.mark.asyncio
    async def test_disabled_embeddings_raises_on_documents(self):
        from intelligence.providers import DisabledEmbeddingsProvider, AIModeDisabledError

        provider = DisabledEmbeddingsProvider()
        with pytest.raises(AIModeDisabledError, match="disabled"):
            await provider.aembed_documents(["test"])
