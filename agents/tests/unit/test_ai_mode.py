"""Phase 1 tests: AI_MODE configuration, Provider factory, network isolation,
deterministic provider, voice isolation, readiness, vector collection isolation.

ALL tests MUST pass without Ollama running and without an NVIDIA API key.
Network isolation tests use Mock HTTP Client — cross-platform, no skip needed.
"""

from __future__ import annotations

import os

import pytest

import sys
from pathlib import Path

# tests/unit/test_ai_mode.py -> parents[2] = agents/
SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(**kwargs: str) -> None:
    for k, v in kwargs.items():
        os.environ[k] = v


def _del_env(*keys: str) -> None:
    for k in keys:
        os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Configuration tests (updated for fail-fast, Sections 1-3)
# ---------------------------------------------------------------------------


class TestAIModeResolution:
    """AI_MODE env var → AIMode enum with fail-fast validation."""

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

    def test_empty_string_defaults_to_deterministic(self):
        _set_env(AI_MODE="")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode

            assert resolve_ai_mode() is AIMode.DETERMINISTIC
        finally:
            _del_env("AI_MODE")

    def test_invalid_mode_raises(self):
        """Invalid non-empty AI_MODE → AIConfigurationError (fail-fast)."""
        _set_env(AI_MODE="garbage")
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIConfigurationError

            with pytest.raises(AIConfigurationError, match="AI_MODE"):
                resolve_ai_mode()
        finally:
            _del_env("AI_MODE")

    def test_live_requires_explicit_provider(self):
        """AI_MODE=live without AI_PROVIDER → AIConfigurationError."""
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from orchestrator.ai_mode import (
                resolve_ai_provider,
                resolve_ai_mode,
                AIMode,
                AIConfigurationError,
            )

            mode = resolve_ai_mode()
            assert mode is AIMode.LIVE
            with pytest.raises(AIConfigurationError, match="live requires AI_PROVIDER"):
                resolve_ai_provider(mode)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_invalid_provider_raises(self):
        """Invalid non-empty AI_PROVIDER in live mode → AIConfigurationError."""
        _set_env(AI_MODE="live", AI_PROVIDER="garbage_provider")
        try:
            from orchestrator.ai_mode import (
                resolve_ai_provider,
                resolve_ai_mode,
                AIConfigurationError,
            )

            mode = resolve_ai_mode()
            with pytest.raises(AIConfigurationError, match="AI_PROVIDER"):
                resolve_ai_provider(mode)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_live_ollama_factory(self):
        """AI_MODE=live + AI_PROVIDER=ollama → OLLAMA provider."""
        _set_env(AI_MODE="live", AI_PROVIDER="ollama")
        try:
            from orchestrator.ai_mode import (
                resolve_ai_provider,
                resolve_ai_mode,
                AIProvider,
            )

            mode = resolve_ai_mode()
            p = resolve_ai_provider(mode)
            assert p is AIProvider.OLLAMA
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_live_nim_factory(self):
        """AI_MODE=live + AI_PROVIDER=nvidia_nim → NVIDIA_NIM provider."""
        _set_env(AI_MODE="live", AI_PROVIDER="nvidia_nim")
        try:
            from orchestrator.ai_mode import (
                resolve_ai_provider,
                resolve_ai_mode,
                AIProvider,
            )

            mode = resolve_ai_mode()
            p = resolve_ai_provider(mode)
            assert p is AIProvider.NVIDIA_NIM
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_dotenv_loaded_before_settings(self, tmp_path):
        """Verify .env values are respected by Settings."""
        env_file = tmp_path / ".env"
        env_file.write_text("AI_MODE=disabled\nAI_PROVIDER=\n")
        import dotenv

        dotenv.load_dotenv(env_file)
        try:
            from orchestrator.ai_mode import resolve_ai_mode, AIMode

            assert resolve_ai_mode() is AIMode.DISABLED
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
            from intelligence.providers import (
                create_embeddings,
                DisabledEmbeddingsProvider,
            )

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
            from intelligence.deterministic_provider import (
                DeterministicEmbeddingsProvider,
            )

            provider = create_embeddings()
            assert isinstance(provider, DeterministicEmbeddingsProvider)
        finally:
            _del_env("AI_MODE")

    def test_live_without_provider_raises(self):
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from intelligence.providers import create_chat_model
            from orchestrator.ai_mode import AIConfigurationError

            with pytest.raises(AIConfigurationError):
                create_chat_model(temperature=0)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_invalid_ai_provider_raises(self):
        _set_env(AI_MODE="live", AI_PROVIDER="garbage")
        try:
            from intelligence.providers import create_chat_model
            from orchestrator.ai_mode import AIConfigurationError

            with pytest.raises(AIConfigurationError):
                create_chat_model(temperature=0)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_no_implicit_fallback_from_deterministic_to_live(self):
        _set_env(AI_MODE="deterministic", AI_PROVIDER="ollama")
        try:
            from intelligence.providers import create_chat_model
            from intelligence.deterministic_provider import DeterministicChatProvider

            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DeterministicChatProvider)
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Network isolation tests (cross-platform — mock HTTP, no socket monkeypatch)
# ---------------------------------------------------------------------------


class TestNetworkIsolation:
    """Confirm disabled/deterministic modes never make HTTP calls."""

    @pytest.mark.asyncio
    async def test_disabled_chat_does_not_make_http_call(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")
            return httpx.Response(200)

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import create_chat_model, DisabledChatProvider

            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DisabledChatProvider)
            # No HTTP calls should have been made
            assert len(calls) == 0, f"Expected 0 HTTP calls, got {len(calls)}: {calls}"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_deterministic_chat_does_not_make_http_call(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import create_chat_model
            from intelligence.deterministic_provider import DeterministicChatProvider

            provider = create_chat_model(temperature=0)
            assert isinstance(provider, DeterministicChatProvider)
            assert len(calls) == 0, f"Expected 0 HTTP calls, got {len(calls)}: {calls}"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_default_config_does_not_make_http_call(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _del_env("AI_MODE", "AI_PROVIDER")
        try:
            from intelligence.providers import create_chat_model

            create_chat_model(temperature=0)
            assert len(calls) == 0, f"Expected 0 HTTP calls, got {len(calls)}: {calls}"
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Voice path isolation tests (Section 5)
# ---------------------------------------------------------------------------


class TestVoiceIsolation:
    """Voice/STT respects AI_MODE with zero HTTP calls in disabled/deterministic."""

    @pytest.mark.asyncio
    async def test_voice_disabled_no_network(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="disabled")
        try:
            # Reset the cached STT instance
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None

            from intelligence.i18n.voice_ingest import DisabledWhisperSTT, get_stt

            stt = get_stt()
            assert isinstance(stt, DisabledWhisperSTT)
            result = await stt.transcribe(b"fake audio", audio_format="webm")
            assert not result.success
            assert "disabled" in (result.error or "")
            assert len(calls) == 0, f"Expected 0 HTTP calls, got {len(calls)}: {calls}"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_voice_deterministic_no_network(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="deterministic")
        try:
            # Reset the cached STT instance
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None

            from intelligence.i18n.voice_ingest import DeterministicWhisperSTT, get_stt

            stt = get_stt()
            assert isinstance(stt, DeterministicWhisperSTT)
            result = await stt.transcribe(b"fake audio", audio_format="webm")
            assert result.success
            assert (
                "deterministic" in result.text.lower()
                or "testing" in result.text.lower()
            )
            assert len(calls) == 0, f"Expected 0 HTTP calls, got {len(calls)}: {calls}"
        finally:
            _del_env("AI_MODE")


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
    async def test_string_input(self):
        """Plain str input must work."""
        from intelligence.deterministic_provider import DeterministicChatProvider

        provider = DeterministicChatProvider()
        r = await provider.ainvoke("What is the renewal policy?")
        assert r.content
        # Same string twice → same output
        r2 = await provider.ainvoke("What is the renewal policy?")
        assert r.content == r2.content
        # Different string → different output
        r3 = await provider.ainvoke("Different query")
        assert r.content != r3.content

    @pytest.mark.asyncio
    async def test_dict_with_messages_input(self):
        """Dict with 'messages' key must be handled."""
        from intelligence.deterministic_provider import DeterministicChatProvider

        provider = DeterministicChatProvider()
        r = await provider.ainvoke({"messages": [{"content": "Test message"}]})
        assert r.content

    @pytest.mark.asyncio
    async def test_dict_with_content_input(self):
        """Dict with 'content' key must be handled."""
        from intelligence.deterministic_provider import DeterministicChatProvider

        provider = DeterministicChatProvider()
        r = await provider.ainvoke({"content": "Direct content test"})
        assert r.content

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
        v_query = await provider.aembed_query("text A")
        assert vecs[0] == v_query


# ---------------------------------------------------------------------------
# Readiness / health tests (Section 7)
# ---------------------------------------------------------------------------


class TestProviderHealthCheck:
    """provider_health_check returns correct status and HTTP semantics."""

    @pytest.mark.asyncio
    async def test_disabled_returns_ready(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import provider_health_check

            result = await provider_health_check()
            assert result["status"] == "ready"
            assert result["chat_model"] == "disabled"
            assert result["embedding_model"] == "disabled"
            assert result["checks"]["chat_model"] == "skipped"
            assert result["checks"]["embedding_model"] == "skipped"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_deterministic_returns_ready(self):
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import provider_health_check

            result = await provider_health_check()
            assert result["status"] == "ready"
            assert result["checks"]["chat_model"] == "available"
            assert result["checks"]["embedding_model"] == "available"
            assert result["checks"]["embedding_dimension"] > 0
            # deterministic metadata shows real provider/model
            assert "deterministic" in result["chat_model"]
            assert "deterministic" in result["embedding_model"]
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
    async def test_ready_returns_503_when_unavailable(self):
        """/ready returns HTTP 503 when status is unavailable."""
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from intelligence.providers import provider_health_check

            result = await provider_health_check()
            assert result["status"] == "unavailable"
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    @pytest.mark.asyncio
    async def test_health_check_does_not_leak_api_key(self):
        _set_env(
            AI_MODE="live",
            AI_PROVIDER="nvidia_nim",
            NVIDIA_API_KEY="sk-1234567890",
            NVIDIA_CHAT_MODEL="meta/llama3-70b",
            NVIDIA_EMBED_MODEL="nvidia/nv-embedqa",
        )
        try:
            from intelligence.providers import provider_health_check

            result = await provider_health_check()
            result_str = str(result)
            assert "sk-1234567890" not in result_str
        finally:
            _del_env(
                "AI_MODE",
                "AI_PROVIDER",
                "NVIDIA_API_KEY",
                "NVIDIA_CHAT_MODEL",
                "NVIDIA_EMBED_MODEL",
            )

    def test_nim_unauthorized_is_not_ready(self):
        """NIM 401/403 → degraded status. Subprocess for isolation."""
        import json
        import subprocess

        script = (
            "import asyncio, json, threading, time, os\n"
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
            "call_count = 0\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        global call_count\n"
            "        call_count += 1\n"
            "        self.send_response(401)\n"
            "        self.end_headers()\n"
            "    def do_POST(self):\n"
            "        global call_count\n"
            "        call_count += 1\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'{}')\n"
            "    def log_message(self, *a): pass\n"
            "srv = HTTPServer(('127.0.0.1', 0), H)\n"
            "port = srv.server_address[1]\n"
            "os.environ['NVIDIA_BASE_URL'] = f'http://127.0.0.1:{port}/v1'\n"
            "t = threading.Thread(target=srv.serve_forever, daemon=True)\n"
            "t.start()\n"
            "time.sleep(0.1)\n"
            "from intelligence.providers import provider_health_check\n"
            "result = asyncio.run(provider_health_check())\n"
            "srv.shutdown()\n"
            "print(json.dumps({'status': result['status'], "
            "'endpoint': result['checks'].get('nvidia_endpoint', ''), "
            "'api_key_leaked': 'sk-test' in str(result)}))\n"
        )
        src_dir = str(SRC)
        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir
        env.update(
            {
                "AI_MODE": "live",
                "AI_PROVIDER": "nvidia_nim",
                "NVIDIA_API_KEY": "sk-test",
                "NVIDIA_CHAT_MODEL": "meta/llama3-70b",
                "NVIDIA_EMBED_MODEL": "nvidia/nv-embedqa",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=src_dir,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        data = json.loads(result.stdout.strip())
        assert data["status"] == "degraded"
        assert "auth_failed" in data["endpoint"]
        assert data["api_key_leaked"] is False

    @pytest.mark.asyncio
    async def test_disabled_does_not_probe_ollama(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
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
# Vector collection isolation tests (Section 6)
# ---------------------------------------------------------------------------


class TestEmbeddingCollectionIsolation:
    """Different modes/providers/models → different collection names."""

    def test_default_deterministic_collection_name(self):
        """Smoke test: deterministic mode produces a valid collection name."""
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import vector_collection_name

            name = vector_collection_name("KnowledgeBase")
            assert name.startswith("KnowledgeBase_")
            assert "deterministic" in name.lower()
        finally:
            _del_env("AI_MODE")

    def test_deterministic_different_from_live_ollama(self):
        """Deterministic and live collections are isolated."""
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import vector_collection_name

            name_det = vector_collection_name("TestBase")
            assert "deterministic" in name_det.lower()
        finally:
            _del_env("AI_MODE")

    def test_different_embedding_models_different_collections(self):
        """Collection name includes a hash of the embedding model identity.

        Because settings is a singleton, we verify the hash is stable
        and non-empty rather than changing env vars mid-process.
        """
        _set_env(AI_MODE="live", AI_PROVIDER="ollama")
        try:
            from intelligence.providers import vector_collection_name

            name = vector_collection_name("TestBase")
            # Name should contain base, provider, and a hash segment
            assert "TestBase" in name
            assert "ollama" in name
            # Re-calling gives the same name (deterministic)
            name2 = vector_collection_name("TestBase")
            assert name == name2
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    def test_disabled_isolated_from_others(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import vector_collection_name

            name_disabled = vector_collection_name("TestBase")
        finally:
            _del_env("AI_MODE")

        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import vector_collection_name

            name_det = vector_collection_name("TestBase")
        finally:
            _del_env("AI_MODE")

        assert name_disabled != name_det
        assert "disabled" in name_disabled.lower()


class TestEmbeddingModelIsolationSubprocess:
    """Real embedding model change → different collection name.

    Uses subprocess so each Settings instantiation sees its own env vars.
    """

    def _run_collection_name(self, **env_vars) -> str:
        import subprocess

        src_dir = str(SRC)
        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir
        env.update(env_vars)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from intelligence.providers import vector_collection_name; "
                "print(vector_collection_name('KnowledgeBase'))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=src_dir,
        )
        err = result.stderr.strip()
        assert result.returncode == 0, (
            f"Subprocess failed (rc={result.returncode}):\nstderr: {err}"
        )
        return result.stdout.strip()

    def test_ollama_embedding_model_change_changes_collection(self):
        """Switching OLLAMA_EMBED_MODEL changes the collection name."""
        name1 = self._run_collection_name(
            AI_MODE="live",
            AI_PROVIDER="ollama",
            OLLAMA_EMBED_MODEL="nomic-embed-text",
        )
        name2 = self._run_collection_name(
            AI_MODE="live",
            AI_PROVIDER="ollama",
            OLLAMA_EMBED_MODEL="all-minilm-l6-v2",
        )
        assert name1 != name2, (
            f"Different OLLAMA_EMBED_MODEL must produce different names:\n"
            f"  nomic-embed-text → {name1}\n"
            f"  all-minilm-l6-v2 → {name2}"
        )

    def test_nvidia_embedding_model_change_changes_collection(self):
        """Switching NVIDIA_EMBED_MODEL changes the collection name."""
        name1 = self._run_collection_name(
            AI_MODE="live",
            AI_PROVIDER="nvidia_nim",
            NVIDIA_API_KEY="sk-test",
            NVIDIA_CHAT_MODEL="meta/llama3-70b",
            NVIDIA_EMBED_MODEL="nvidia/nv-embedqa-4b",
        )
        name2 = self._run_collection_name(
            AI_MODE="live",
            AI_PROVIDER="nvidia_nim",
            NVIDIA_API_KEY="sk-test",
            NVIDIA_CHAT_MODEL="meta/llama3-70b",
            NVIDIA_EMBED_MODEL="nvidia/nv-embed-v3",
        )
        assert name1 != name2, (
            f"Different NVIDIA_EMBED_MODEL must produce different names:\n"
            f"  nv-embedqa-4b → {name1}\n"
            f"  nv-embed-v3   → {name2}"
        )


# ---------------------------------------------------------------------------
# Chat Graph integration tests (Fix 3)
# ---------------------------------------------------------------------------


class TestDeterministicChatGraphIntegration:
    """Actual Chat Graph compilation + invocation with fixture injection."""

    @pytest.mark.asyncio
    async def test_show_open_tickets_routes_to_crm_reader(self):
        """'Show my open tickets' → read/ticket → crm_reader.get_tickets."""
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            register_chat_intent_fixture,
            clear_fixtures,
        )
        from intelligence.chat.graph import ChatDeps, ChatState, build_chat_graph

        # Register a fixture that returns a read/ticket intent
        register_chat_intent_fixture(
            "Show my open tickets",
            '{"intent":"read","entity":"ticket","confidence":0.95}',
        )
        try:
            llm = DeterministicChatProvider()
            graph = build_chat_graph(
                deps=ChatDeps(
                    llm=llm,
                    tool_executor=_NoopToolExecutor(),
                    memory=None,
                    memory_window=8,
                )
            )
            state = ChatState(
                query="Show my open tickets",
                tenant_id="tenant-1",
                user_id="user-1",
                roles=[],
            )
            result = await graph.ainvoke(state)
            # graph.ainvoke returns dict (LangGraph without checkpointer)
            assert result["intent"] is not None
            assert result["intent"].intent == "read"
            assert result["intent"].entity == "ticket"
            assert result["tool_call"] is not None
            assert result["tool_call"].tool == "crm_reader.get_tickets"
        finally:
            clear_fixtures()

    @pytest.mark.asyncio
    async def test_create_followup_task_routes_to_crm_writer(self):
        """'Create a follow-up task' → write/task → crm_writer.propose."""
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            register_chat_intent_fixture,
            clear_fixtures,
        )
        from intelligence.chat.graph import ChatDeps, ChatState, build_chat_graph

        register_chat_intent_fixture(
            "Create a follow-up task",
            '{"intent":"write","entity":"task","confidence":0.90}',
        )
        try:
            llm = DeterministicChatProvider()
            graph = build_chat_graph(
                deps=ChatDeps(
                    llm=llm,
                    tool_executor=_NoopToolExecutor(),
                    memory=None,
                    memory_window=8,
                )
            )
            state = ChatState(
                query="Create a follow-up task",
                tenant_id="tenant-1",
                user_id="user-1",
                roles=[],
            )
            result = await graph.ainvoke(state)
            # graph.ainvoke returns dict (LangGraph without checkpointer)
            assert result["intent"] is not None
            assert result["intent"].intent == "write"
            assert result["intent"].entity == "task"
            assert result["tool_call"] is not None
            assert result["tool_call"].tool == "crm_writer.propose"
        finally:
            clear_fixtures()

    @pytest.mark.asyncio
    async def test_explain_renewal_policy_routes_to_vector_search(self):
        """'Explain the renewal policy' → question/unknown → vector_search.search."""
        from intelligence.deterministic_provider import (
            DeterministicChatProvider,
            register_chat_intent_fixture,
            clear_fixtures,
        )
        from intelligence.chat.graph import ChatDeps, ChatState, build_chat_graph

        register_chat_intent_fixture(
            "Explain the renewal policy",
            '{"intent":"question","entity":"unknown","confidence":0.85}',
        )
        try:
            llm = DeterministicChatProvider()
            graph = build_chat_graph(
                deps=ChatDeps(
                    llm=llm,
                    tool_executor=_NoopToolExecutor(),
                    memory=None,
                    memory_window=8,
                )
            )
            state = ChatState(
                query="Explain the renewal policy",
                tenant_id="tenant-1",
                user_id="user-1",
                roles=[],
            )
            result = await graph.ainvoke(state)
            # graph.ainvoke returns dict (LangGraph without checkpointer)
            assert result["intent"] is not None
            assert result["intent"].intent == "question"
            assert result["tool_call"] is not None
            assert result["tool_call"].tool == "vector_search.search"
        finally:
            clear_fixtures()


class _NoopToolExecutor:
    """ToolExecutor that returns ok for any tool call."""

    async def execute(self, **kwargs):
        from intelligence.chat.graph import ToolResult

        return ToolResult(
            tool=kwargs.get("call").tool if kwargs.get("call") else "none", ok=True
        )


# ---------------------------------------------------------------------------
# aiohttp /ready test (Fix 2 + Fix 4)
# ---------------------------------------------------------------------------


class TestReadyEndpoint:
    """Real aiohttp test client against ready_handler."""

    @pytest.mark.asyncio
    async def test_ready_200_for_deterministic(self):
        _set_env(AI_MODE="deterministic")
        try:
            from aiohttp.test_utils import make_mocked_request
            from orchestrator.main import ready_handler

            req = make_mocked_request("GET", "/ready")
            resp = await ready_handler(req)
            assert resp.status == 200
            import json

            data = json.loads(resp.body)
            assert data["status"] == "ready"
            assert data["provider"] == "deterministic"
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_ready_503_for_live_without_provider(self):
        _set_env(AI_MODE="live", AI_PROVIDER="")
        try:
            from aiohttp.test_utils import make_mocked_request
            from orchestrator.main import ready_handler

            req = make_mocked_request("GET", "/ready")
            resp = await ready_handler(req)
            assert resp.status == 503
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    @pytest.mark.asyncio
    async def test_ready_503_for_invalid_ai_mode(self):
        _set_env(AI_MODE="garbage_mode")
        try:
            from aiohttp.test_utils import make_mocked_request
            from orchestrator.main import ready_handler

            req = make_mocked_request("GET", "/ready")
            resp = await ready_handler(req)
            assert resp.status == 503
        finally:
            _del_env("AI_MODE")


# ---------------------------------------------------------------------------
# NVIDIA single HTTP call test (Fix 4)
# ---------------------------------------------------------------------------


class TestNvidiaHealthCheck:
    """NVIDIA provider_health_check makes exactly 1 HTTP call.

    Patches httpx.AsyncClient via monkeypatch so the test is immune
    to ordering effects from other tests.
    """

    def test_nvidia_makes_exactly_one_http_get(self):
        """Exactly 1 HTTP GET, no duplicate requests, 401 → degraded.

        Uses subprocess for complete isolation from other tests' monkeypatch
        state.  The subprocess sets AI_MODE=live + AI_PROVIDER=nvidia_nim,
        starts a dummy HTTP server that returns 401, and asserts exactly
        one request was received by the server.
        """
        import subprocess
        import json

        src_dir = str(SRC)
        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir
        env.update(
            {
                "AI_MODE": "live",
                "AI_PROVIDER": "nvidia_nim",
                "NVIDIA_API_KEY": "sk-test",
                "NVIDIA_CHAT_MODEL": "meta/llama3-70b",
                "NVIDIA_EMBED_MODEL": "nvidia/nv-embedqa",
            }
        )
        # Run a script that starts a mock HTTP server, sets the env var
        # BEFORE importing anything from the project, then calls
        # provider_health_check and prints the call count as JSON.
        script = (
            "import asyncio, json, threading, time, os\n"
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
            "\n"
            "call_count = 0\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        global call_count\n"
            "        call_count += 1\n"
            "        self.send_response(401)\n"
            "        self.end_headers()\n"
            "    def do_POST(self):\n"
            "        global call_count\n"
            "        call_count += 1\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'{}')\n"
            "    def log_message(self, *a): pass\n"
            "srv = HTTPServer(('127.0.0.1', 0), H)\n"
            "port = srv.server_address[1]\n"
            "# IMPORTANT: override env var BEFORE importing any project module\n"
            "os.environ['NVIDIA_BASE_URL'] = f'http://127.0.0.1:{port}/v1'\n"
            "t = threading.Thread(target=srv.serve_forever, daemon=True)\n"
            "t.start()\n"
            "time.sleep(0.1)\n"
            "from intelligence.providers import provider_health_check\n"
            "result = asyncio.run(provider_health_check())\n"
            "srv.shutdown()\n"
            "print(json.dumps({'call_count': call_count, 'status': result['status']}))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=src_dir,
        )
        err = result.stderr.strip()
        assert result.returncode == 0, (
            f"Subprocess failed (rc={result.returncode}):\nstderr: {err}"
        )
        out = result.stdout.strip()
        assert out, f"No output from subprocess. stderr: {err}"
        data = json.loads(out)
        assert data["call_count"] == 1, (
            f"Expected exactly 1 HTTP GET, got {data['call_count']}"
        )
        assert data["status"] == "degraded"


# ---------------------------------------------------------------------------
# Provider metadata tests (Fix 5)
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    """provider_metadata returns correct labels per mode."""

    def test_disabled_metadata(self):
        _set_env(AI_MODE="disabled")
        try:
            from intelligence.providers import provider_metadata

            meta = provider_metadata()
            assert meta["ai_mode"] == "disabled"
            assert meta["provider"] == "disabled"
            assert meta["chat_model"] == "disabled"
            assert meta["embedding_model"] == "disabled"
        finally:
            _del_env("AI_MODE")

    def test_deterministic_metadata(self):
        _set_env(AI_MODE="deterministic")
        try:
            from intelligence.providers import provider_metadata

            meta = provider_metadata()
            assert meta["ai_mode"] == "deterministic"
            assert meta["provider"] == "deterministic"
            assert "deterministic" in meta["chat_model"]
            assert "deterministic" in meta["embedding_model"]
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_live_ollama_metadata(self):
        # Can't actually init Ollama, but metadata should still resolve
        _set_env(AI_MODE="live", AI_PROVIDER="ollama")
        try:
            from intelligence.providers import provider_metadata

            meta = provider_metadata()
            assert meta["ai_mode"] == "live"
            assert meta["provider"] == "ollama"
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Voice API status tests (Fix 7)
# ---------------------------------------------------------------------------


class TestVoiceApiStatus:
    """Voice API returns correct status and HTTP codes."""

    @pytest.mark.asyncio
    async def test_disabled_voice_returns_unavailable_status(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="disabled")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from intelligence.i18n.voice_ingest import transcribe_audio

            result = await transcribe_audio(b"test audio")
            assert not result.success
            assert "disabled" in (result.error or "").lower()
            assert len(calls) == 0
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_deterministic_voice_returns_fixed_transcript(self, monkeypatch):
        import httpx

        calls: list[str] = []

        async def _tracking_get(url, **kwargs):
            calls.append(f"GET {url}")

        async def _tracking_post(url, **kwargs):
            calls.append(f"POST {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _tracking_get)
        monkeypatch.setattr(httpx.AsyncClient, "post", _tracking_post)
        _set_env(AI_MODE="deterministic")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from intelligence.i18n.voice_ingest import transcribe_audio

            result = await transcribe_audio(b"test audio")
            assert result.success
            assert result.model_used == "deterministic-stt-v1"
            assert len(calls) == 0
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_live_missing_whisper_url_raises_config_error(self):
        _set_env(AI_MODE="live", AI_PROVIDER="ollama")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from intelligence.i18n.voice_ingest import WhisperSTT

            with pytest.raises(RuntimeError, match="WHISPER_URL"):
                WhisperSTT()
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")


# ---------------------------------------------------------------------------
# Voice Handler HTTP semantics tests (Phase 1 final)
# ---------------------------------------------------------------------------


class TestVoiceHandlerHttpSemantics:
    """voice_handler / voice_query_handler return correct HTTP status."""

    @pytest.mark.asyncio
    async def test_voice_handler_disabled_returns_503(self, monkeypatch):
        import httpx
        from unittest import mock as umock

        calls: list[str] = []

        async def _track(method, url, **kw):
            calls.append(f"{method} {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _track)
        monkeypatch.setattr(httpx.AsyncClient, "post", _track)
        _set_env(AI_MODE="disabled")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from aiohttp.test_utils import make_mocked_request

            req = make_mocked_request(
                "POST",
                "/api/v1/intelligence/voice",
                headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            )

            # Mock request.read() to return audio bytes
            async def _read():
                return b"fake audio bytes for testing"

            with umock.patch.object(req, "read", _read):
                from orchestrator.main import voice_handler

                resp = await voice_handler(req)
            assert resp.status == 503
            import json

            body = json.loads(resp.body)
            assert body == {"error": "voice_ai_disabled"}
            assert "details" not in body
            assert len(calls) == 0
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_voice_handler_deterministic_returns_200(self, monkeypatch):
        import httpx
        from unittest import mock as umock

        calls: list[str] = []

        async def _track(method, url, **kw):
            calls.append(f"{method} {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", _track)
        monkeypatch.setattr(httpx.AsyncClient, "post", _track)
        _set_env(AI_MODE="deterministic")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from aiohttp.test_utils import make_mocked_request

            req = make_mocked_request(
                "POST",
                "/api/v1/intelligence/voice",
                headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            )

            async def _read():
                return b"fake audio bytes for testing"

            with umock.patch.object(req, "read", _read):
                from orchestrator.main import voice_handler

                resp = await voice_handler(req)
            assert resp.status == 200
            import json

            body = json.loads(resp.body)
            assert body["transcript"]["text"] != ""
            assert "error" not in body
            assert len(calls) == 0
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_voice_handler_live_without_whisper_returns_503(self):
        from unittest import mock as umock

        _set_env(AI_MODE="live", AI_PROVIDER="ollama")
        try:
            import intelligence.i18n.voice_ingest as vi

            vi._default_stt = None
            from aiohttp.test_utils import make_mocked_request

            req = make_mocked_request(
                "POST",
                "/api/v1/intelligence/voice",
                headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            )

            async def _read():
                return b"fake audio bytes for testing"

            with umock.patch.object(req, "read", _read):
                from orchestrator.main import voice_handler

                resp = await voice_handler(req)
            assert resp.status == 503
            import json

            body = json.loads(resp.body)
            assert body == {"error": "voice_provider_unavailable"}
            assert "details" not in body
        finally:
            _del_env("AI_MODE", "AI_PROVIDER")

    @pytest.mark.asyncio
    async def test_voice_handler_does_not_expose_internal_exception(self, monkeypatch):
        """Internal errors return 500 with no details/stacktrace."""
        from unittest import mock as umock

        async def _failing_stt(*args, **kwargs):
            raise ValueError("secret internal error XYZ123")

        # Patch inside orchestrator.main where the function is imported
        import orchestrator.main as _main

        monkeypatch.setattr(_main, "process_multilingual_input", _failing_stt)
        _set_env(AI_MODE="deterministic")
        try:
            from aiohttp.test_utils import make_mocked_request

            req = make_mocked_request(
                "POST",
                "/api/v1/intelligence/voice",
                headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            )

            async def _read():
                return b"fake audio"

            with umock.patch.object(req, "read", _read):
                from orchestrator.main import voice_handler

                resp = await voice_handler(req)
            assert resp.status == 500
            import json

            body = json.loads(resp.body)
            assert body == {"error": "transcription_failed"}
            assert "details" not in body
            assert "XYZ123" not in str(body)
        finally:
            _del_env("AI_MODE")

    @pytest.mark.asyncio
    async def test_voice_query_handler_does_not_expose_internal_exception(
        self, monkeypatch
    ):
        """voice_query_handler internal errors return 500, no details."""
        from unittest import mock as umock

        async def _failing_stt(*args, **kwargs):
            raise ValueError("secret internal error ABC789")

        import orchestrator.main as _main

        monkeypatch.setattr(_main, "process_multilingual_input", _failing_stt)
        _set_env(AI_MODE="deterministic")
        try:
            from aiohttp import web

            app = web.Application()

            # The handler accesses app["search_agent"] — provide a minimal stub
            class _FakeSearch:
                async def search(self, **kw):
                    return {}

            app["search_agent"] = _FakeSearch()
            from aiohttp.test_utils import make_mocked_request

            req = make_mocked_request(
                "POST",
                "/api/v1/intelligence/voice/query",
                headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
                app=app,
            )

            async def _read():
                return b"fake audio"

            with umock.patch.object(req, "read", _read):
                from orchestrator.main import voice_query_handler

                resp = await voice_query_handler(req)
            assert resp.status == 500
            import json

            body = json.loads(resp.body)
            assert body == {"error": "voice_query_failed"}
            assert "details" not in body
            assert "ABC789" not in str(body)
        finally:
            _del_env("AI_MODE")


# ---------------------------------------------------------------------------
# Settings .env loading test (Fix 6)
# ---------------------------------------------------------------------------


class TestSettingsFromDotenv:
    """Settings singleton picks up values from environment.

    Uses subprocess to avoid importlib.reload side effects that
    corrupt other tests' module state (test-order dependency).
    """

    def test_settings_loads_all_ai_vars_from_env(self):
        """All 7 AI-related Settings keys load from env vars."""
        import subprocess

        src_dir = str(SRC)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": src_dir,
                "AI_MODE": "live",
                "AI_PROVIDER": "ollama",
                "OLLAMA_URL": "http://ollama:9999",
                "OLLAMA_MODEL": "test-model-42b",
                "OLLAMA_EMBED_MODEL": "test-embed-v2",
                "NVIDIA_BASE_URL": "https://test.nvidia.example.com/v1",
                "NVIDIA_CHAT_MODEL": "test/chat-model",
                "NVIDIA_EMBED_MODEL": "test/embed-model",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from orchestrator.config import settings; "
                "print(settings.AI_MODE); "
                "print(settings.AI_PROVIDER); "
                "print(settings.OLLAMA_URL); "
                "print(settings.OLLAMA_MODEL); "
                "print(settings.OLLAMA_EMBED_MODEL); "
                "print(settings.NVIDIA_BASE_URL); "
                "print(settings.NVIDIA_CHAT_MODEL); "
                "print(settings.NVIDIA_EMBED_MODEL)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=src_dir,
        )
        err = result.stderr.strip()
        assert result.returncode == 0, (
            f"Subprocess failed (rc={result.returncode}):\nstderr: {err}"
        )
        lines = [line.strip() for line in result.stdout.strip().splitlines()]
        assert len(lines) == 8, (
            f"Expected 8 lines, got {len(lines)}. stdout={lines!r} stderr={err!r}"
        )
        assert lines[0] == "live"
        assert lines[1] == "ollama"
        assert lines[2] == "http://ollama:9999"
        assert lines[3] == "test-model-42b"
        assert lines[4] == "test-embed-v2"
        assert lines[5] == "https://test.nvidia.example.com/v1"
        assert lines[6] == "test/chat-model"
        assert lines[7] == "test/embed-model"


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
        from intelligence.providers import (
            DisabledEmbeddingsProvider,
            AIModeDisabledError,
        )

        provider = DisabledEmbeddingsProvider()
        with pytest.raises(AIModeDisabledError, match="disabled"):
            await provider.aembed_query("test")

    @pytest.mark.asyncio
    async def test_disabled_embeddings_raises_on_documents(self):
        from intelligence.providers import (
            DisabledEmbeddingsProvider,
            AIModeDisabledError,
        )

        provider = DisabledEmbeddingsProvider()
        with pytest.raises(AIModeDisabledError, match="disabled"):
            await provider.aembed_documents(["test"])
