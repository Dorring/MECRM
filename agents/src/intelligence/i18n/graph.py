"""i18n Processing Graph.

LangGraph workflow for voice/multilingual query processing:
STT → Language Detection → Translation → Agent Routing → Response Translation
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import StateGraph

from .language_detector import LanguageResult, adetect_language
from .translator import TranslationResult, translate_to_english, translate_from_english
from .voice_ingest import TranscriptResult, transcribe_audio, AudioFormat

logger = logging.getLogger(__name__)

CANONICAL_LANG = "en"


@dataclass
class I18nState:
    """State for i18n processing graph."""
    
    # Input
    input_type: Literal["text", "voice"] = "text"
    raw_text: str = ""
    audio_bytes: bytes = field(default_factory=bytes)
    audio_format: AudioFormat = "webm"
    tenant_id: str = ""
    user_id: str = ""
    
    # STT results
    transcript: TranscriptResult | None = None
    
    # Language detection
    detected_language: LanguageResult | None = None
    original_language: str = "en"
    
    # Translation
    canonical_query: str = ""
    translation_to_canonical: TranslationResult | None = None
    
    # Agent response
    agent_response: str = ""
    agent_response_language: str = "en"
    
    # Output
    final_response: str = ""
    translation_from_canonical: TranslationResult | None = None
    
    # Metrics
    total_latency_ms: float = 0.0
    stt_latency_ms: float = 0.0
    detection_latency_ms: float = 0.0
    translation_latency_ms: float = 0.0
    
    # Error handling
    error: str | None = None


@dataclass
class I18nDeps:
    """Dependencies for i18n graph."""
    pass


async def _transcribe_node(state: I18nState) -> dict[str, Any]:
    """Transcribe voice input to text."""
    if state.input_type != "voice" or not state.audio_bytes:
        return {"raw_text": state.raw_text}
    
    start = time.time()
    
    result = await transcribe_audio(
        state.audio_bytes,
        audio_format=state.audio_format,
    )
    
    latency = (time.time() - start) * 1000
    
    if result.error:
        logger.error(f"STT failed: {result.error}")
        return {
            "transcript": result,
            "stt_latency_ms": latency,
            "error": f"Speech-to-text failed: {result.error}",
        }
    
    return {
        "transcript": result,
        "raw_text": result.text,
        "stt_latency_ms": latency,
    }


async def _detect_language_node(state: I18nState) -> dict[str, Any]:
    """Detect the language of the input text."""
    if not state.raw_text:
        return {
            "detected_language": LanguageResult(
                language="unknown",
                language_name="Unknown",
                confidence=0.0,
                error="No text to detect",
            ),
            "original_language": "en",
        }
    
    start = time.time()
    
    result = await adetect_language(state.raw_text)
    
    latency = (time.time() - start) * 1000
    
    # If detection failed or is unknown, assume English
    if result.language == "unknown":
        result.language = "en"
        result.language_name = "English"
    
    return {
        "detected_language": result,
        "original_language": result.language,
        "detection_latency_ms": latency,
    }


async def _translate_to_canonical_node(state: I18nState) -> dict[str, Any]:
    """Translate input to canonical language (English)."""
    if not state.raw_text:
        return {"canonical_query": ""}
    
    # Skip translation if already in canonical language
    if state.original_language == CANONICAL_LANG:
        return {
            "canonical_query": state.raw_text,
            "translation_to_canonical": None,
        }
    
    start = time.time()
    
    result = await translate_to_english(state.raw_text, state.original_language)
    
    latency = (time.time() - start) * 1000
    
    canonical = result.translated_text if result.success else state.raw_text
    
    return {
        "canonical_query": canonical,
        "translation_to_canonical": result,
        "translation_latency_ms": state.translation_latency_ms + latency,
    }


async def _translate_response_node(state: I18nState) -> dict[str, Any]:
    """Translate agent response back to original language."""
    if not state.agent_response:
        return {"final_response": ""}
    
    # Skip translation if original language is English
    if state.original_language == CANONICAL_LANG:
        return {
            "final_response": state.agent_response,
            "translation_from_canonical": None,
        }
    
    start = time.time()
    
    result = await translate_from_english(state.agent_response, state.original_language)
    
    latency = (time.time() - start) * 1000
    
    final = result.translated_text if result.success else state.agent_response
    
    return {
        "final_response": final,
        "translation_from_canonical": result,
        "translation_latency_ms": state.translation_latency_ms + latency,
    }


def build_i18n_ingest_graph(*, deps: I18nDeps | None = None) -> StateGraph:
    """Build the i18n ingest graph (STT → Detect → Translate to canonical).
    
    This graph processes input and prepares it for agent routing.
    """
    graph = StateGraph(I18nState)
    
    graph.add_node("transcribe", _transcribe_node)
    graph.add_node("detect_language", _detect_language_node)
    graph.add_node("translate_to_canonical", _translate_to_canonical_node)
    
    graph.set_entry_point("transcribe")
    graph.add_edge("transcribe", "detect_language")
    graph.add_edge("detect_language", "translate_to_canonical")
    graph.set_finish_point("translate_to_canonical")
    
    return graph.compile()


def build_i18n_response_graph(*, deps: I18nDeps | None = None) -> StateGraph:
    """Build the i18n response graph (Translate response back).
    
    This graph processes agent response for output.
    """
    graph = StateGraph(I18nState)
    
    graph.add_node("translate_response", _translate_response_node)
    
    graph.set_entry_point("translate_response")
    graph.set_finish_point("translate_response")
    
    return graph.compile()


async def process_multilingual_input(
    *,
    text: str = "",
    audio_bytes: bytes = b"",
    audio_format: AudioFormat = "webm",
    tenant_id: str = "",
    user_id: str = "",
) -> I18nState:
    """Process multilingual input through the i18n pipeline.
    
    Args:
        text: Text input (if not using voice)
        audio_bytes: Audio input (if using voice)
        audio_format: Audio format
        tenant_id: Tenant ID
        user_id: User ID
        
    Returns:
        I18nState with processed input ready for agent routing
    """
    start = time.time()
    
    input_type: Literal["text", "voice"] = "voice" if audio_bytes else "text"
    
    state = I18nState(
        input_type=input_type,
        raw_text=text,
        audio_bytes=audio_bytes,
        audio_format=audio_format,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    
    graph = build_i18n_ingest_graph()
    result = await graph.ainvoke(state)
    
    result["total_latency_ms"] = (time.time() - start) * 1000
    
    # Convert dict back to state if needed
    if isinstance(result, dict):
        for key, value in result.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state
    
    return result


async def process_multilingual_response(
    state: I18nState,
    agent_response: str,
) -> I18nState:
    """Process agent response for multilingual output.
    
    Args:
        state: i18n state from input processing
        agent_response: Response from agent
        
    Returns:
        I18nState with translated response
    """
    state.agent_response = agent_response
    state.agent_response_language = CANONICAL_LANG
    
    graph = build_i18n_response_graph()
    result = await graph.ainvoke(state)
    
    if isinstance(result, dict):
        for key, value in result.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state
    
    return result
