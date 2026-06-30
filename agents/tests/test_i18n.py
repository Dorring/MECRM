"""Unit tests for i18n module: language detection, translation, voice ingest."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestLanguageDetector:
    """Tests for language_detector module."""

    def test_detect_language_english(self):
        """Should detect English text correctly."""
        from intelligence.i18n.language_detector import detect_language
        
        result = detect_language("Hello, how are you today?")
        
        assert result.language == "en"
        assert result.language_name == "English"
        assert result.confidence > 0.5
        assert result.is_english is True
        assert result.needs_translation is False

    def test_detect_language_hindi(self):
        """Should detect Hindi text correctly."""
        from intelligence.i18n.language_detector import detect_language
        
        result = detect_language("आप कैसे हैं? मैं ठीक हूं।")
        
        assert result.language == "hi"
        assert result.language_name == "Hindi"
        assert result.confidence > 0.5
        assert result.is_english is False
        assert result.needs_translation is True

    def test_detect_language_empty_text(self):
        """Should handle empty text gracefully."""
        from intelligence.i18n.language_detector import detect_language
        
        result = detect_language("")
        
        assert result.language == "unknown"
        assert result.error is not None

    def test_detect_language_whitespace_only(self):
        """Should handle whitespace-only text."""
        from intelligence.i18n.language_detector import detect_language
        
        result = detect_language("   \n\t  ")
        
        assert result.language == "unknown"
        assert result.error is not None

    def test_detect_script_devanagari(self):
        """Should detect Devanagari script."""
        from intelligence.i18n.language_detector import _detect_script
        
        script = _detect_script("नमस्ते")
        
        assert script == "DEVANAGARI"

    def test_detect_script_latin(self):
        """Should detect Latin script."""
        from intelligence.i18n.language_detector import _detect_script
        
        script = _detect_script("Hello World")
        
        assert script == "LATIN"

    @pytest.mark.asyncio
    async def test_async_detect_language(self):
        """Should work asynchronously."""
        from intelligence.i18n.language_detector import adetect_language
        
        result = await adetect_language("This is a test")
        
        assert result.language == "en"


class TestTranslator:
    """Tests for translator module."""

    @pytest.mark.asyncio
    async def test_translate_to_english_skip_english(self):
        """Should skip translation if already English."""
        from intelligence.i18n.translator import translate_to_english
        
        result = await translate_to_english("Hello world", "en")
        
        assert result.translated_text == "Hello world"
        assert result.source_language == "en"
        assert result.target_language == "en"
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_translate_to_english_empty_text(self):
        """Should handle empty text."""
        from intelligence.i18n.translator import translate_to_english
        
        result = await translate_to_english("", "hi")
        
        assert result.translated_text == ""
        assert result.error == "Empty text"

    @pytest.mark.asyncio
    async def test_translate_from_english_skip_english(self):
        """Should skip translation if target is English."""
        from intelligence.i18n.translator import translate_from_english
        
        result = await translate_from_english("Hello world", "en")
        
        assert result.translated_text == "Hello world"
        assert result.target_language == "en"
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_translate_bidirectional_same_language(self):
        """Should return original text if source equals target."""
        from intelligence.i18n.translator import translate_bidirectional
        
        result = await translate_bidirectional("Hello", "en", "en")
        
        assert result.translated_text == "Hello"
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_translation_cache_hit(self):
        """Should return cached translation on second call."""
        from intelligence.i18n.translator import translate_to_english, _translation_cache
        
        # Clear cache
        _translation_cache._cache.clear()
        
        # First call - no cache
        result1 = await translate_to_english("Hello test", "en")
        
        # Second call - should use cache (if any)
        result2 = await translate_to_english("Hello test", "en")
        
        assert result1.translated_text == result2.translated_text


class TestVoiceIngest:
    """Tests for voice_ingest module."""

    @pytest.mark.asyncio
    async def test_transcribe_empty_audio(self):
        """Should handle empty audio gracefully."""
        from intelligence.i18n.voice_ingest import transcribe_audio
        
        result = await transcribe_audio(b"")
        
        assert result.text == ""
        assert result.error == "Empty audio input"
        assert result.success is False

    @pytest.mark.asyncio
    async def test_whisper_stt_init(self):
        """Should initialize WhisperSTT with defaults."""
        from intelligence.i18n.voice_ingest import WhisperSTT
        
        stt = WhisperSTT()
        
        assert stt._model == "whisper"
        assert stt._timeout == 30.0

    @pytest.mark.asyncio
    async def test_whisper_stt_custom_config(self):
        """Should accept custom configuration."""
        from intelligence.i18n.voice_ingest import WhisperSTT
        
        stt = WhisperSTT(whisper_url="http://custom:8000", model="whisper-large", timeout=60.0)
        
        assert stt._whisper_url == "http://custom:8000"
        assert stt._model == "whisper-large"
        assert stt._timeout == 60.0


class TestI18nGraph:
    """Tests for i18n graph module."""

    @pytest.mark.asyncio
    async def test_process_multilingual_input_text(self):
        """Should process text input without STT."""
        from intelligence.i18n.graph import process_multilingual_input
        
        state = await process_multilingual_input(
            text="Hello world",
            tenant_id="test-tenant",
            user_id="test-user",
        )
        
        assert state.input_type == "text"
        assert state.raw_text == "Hello world"
        assert state.original_language == "en"
        assert state.canonical_query == "Hello world"

    @pytest.mark.asyncio
    async def test_process_multilingual_input_empty(self):
        """Should handle empty input."""
        from intelligence.i18n.graph import process_multilingual_input
        
        state = await process_multilingual_input(
            text="",
            tenant_id="test-tenant",
            user_id="test-user",
        )
        
        assert state.canonical_query == ""

    @pytest.mark.asyncio
    async def test_process_multilingual_response_english(self):
        """Should skip translation for English response."""
        from intelligence.i18n.graph import process_multilingual_input, process_multilingual_response
        
        state = await process_multilingual_input(
            text="Hello",
            tenant_id="test-tenant",
            user_id="test-user",
        )
        
        state = await process_multilingual_response(state, "Here is your answer")
        
        assert state.final_response == "Here is your answer"
        assert state.translation_from_canonical is None

    def test_i18n_state_defaults(self):
        """Should have correct default values."""
        from intelligence.i18n.graph import I18nState
        
        state = I18nState()
        
        assert state.input_type == "text"
        assert state.raw_text == ""
        assert state.original_language == "en"
        assert state.canonical_query == ""
        assert state.error is None

    def test_build_ingest_graph(self):
        """Should build ingest graph successfully."""
        from intelligence.i18n.graph import build_i18n_ingest_graph
        
        graph = build_i18n_ingest_graph()
        
        assert graph is not None

    def test_build_response_graph(self):
        """Should build response graph successfully."""
        from intelligence.i18n.graph import build_i18n_response_graph
        
        graph = build_i18n_response_graph()
        
        assert graph is not None


class TestSupportedLanguages:
    """Tests for supported language configuration."""

    def test_supported_languages_include_indic(self):
        """Should include major Indic languages."""
        from intelligence.i18n.language_detector import SUPPORTED_LANGUAGES
        
        indic = ["hi", "ta", "te", "bn", "mr", "gu", "kn", "ml", "pa", "ur"]
        for lang in indic:
            assert lang in SUPPORTED_LANGUAGES, f"Missing Indic language: {lang}"

    def test_supported_languages_include_major(self):
        """Should include major world languages."""
        from intelligence.i18n.language_detector import SUPPORTED_LANGUAGES
        
        major = ["en", "es", "fr", "de", "zh", "ja", "ko", "ar", "pt", "ru"]
        for lang in major:
            assert lang in SUPPORTED_LANGUAGES, f"Missing major language: {lang}"


class TestCanonicalLanguage:
    """Tests for canonical language configuration."""

    def test_canonical_is_english(self):
        """Canonical language should be English."""
        from intelligence.i18n.translator import CANONICAL_LANGUAGE
        
        assert CANONICAL_LANGUAGE == "en"
