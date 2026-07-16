"""Translation Module.

Translates text to/from English (canonical language) using local LLM.
Supports caching and fallback strategies.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from intelligence.providers import AsyncChatModel, create_chat_model

logger = logging.getLogger(__name__)

# Canonical language for processing
CANONICAL_LANGUAGE = "en"

# Translation-capable models in order of preference
TRANSLATION_MODELS = ["aya", "llama3.1", "mistral", "gemma2"]


@dataclass
class TranslationResult:
    """Result of a translation operation."""
    
    original_text: str
    translated_text: str
    source_language: str
    target_language: str
    confidence: float
    model_used: str | None = None
    error: str | None = None
    cached: bool = False
    
    @property
    def success(self) -> bool:
        return self.error is None and bool(self.translated_text)


class TranslationCache:
    """Simple in-memory translation cache."""
    
    def __init__(self, max_size: int = 1000):
        self._cache: dict[str, TranslationResult] = {}
        self._max_size = max_size
    
    def _key(self, text: str, source: str, target: str) -> str:
        content = f"{source}:{target}:{text}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]
    
    def get(self, text: str, source: str, target: str) -> TranslationResult | None:
        key = self._key(text, source, target)
        result = self._cache.get(key)
        if result:
            result.cached = True
        return result
    
    def set(self, result: TranslationResult) -> None:
        if len(self._cache) >= self._max_size:
            # Remove oldest entries (simple strategy)
            keys_to_remove = list(self._cache.keys())[:100]
            for k in keys_to_remove:
                del self._cache[k]
        
        key = self._key(result.original_text, result.source_language, result.target_language)
        self._cache[key] = result


# Global cache instance
_translation_cache = TranslationCache()


def _get_llm() -> AsyncChatModel:
    """Get the configured translation LLM through the provider boundary."""
    return create_chat_model(temperature=0.1)


def _model_name(llm: object) -> str | None:
    """Read model metadata without assuming an Ollama-specific attribute."""
    return getattr(llm, "model", None) or getattr(llm, "model_name", None)


async def translate_to_english(
    text: str,
    source_language: str,
    *,
    use_cache: bool = True,
) -> TranslationResult:
    """Translate text from source language to English.
    
    Args:
        text: Text to translate
        source_language: Source language code (e.g., "hi", "ta")
        use_cache: Whether to use translation cache
        
    Returns:
        TranslationResult with translated text
    """
    if not text or not text.strip():
        return TranslationResult(
            original_text=text,
            translated_text=text,
            source_language=source_language,
            target_language=CANONICAL_LANGUAGE,
            confidence=1.0,
            error="Empty text",
        )
    
    # Skip if already English
    if source_language == CANONICAL_LANGUAGE:
        return TranslationResult(
            original_text=text,
            translated_text=text,
            source_language=source_language,
            target_language=CANONICAL_LANGUAGE,
            confidence=1.0,
        )
    
    # Check cache
    if use_cache:
        cached = _translation_cache.get(text, source_language, CANONICAL_LANGUAGE)
        if cached:
            logger.debug(f"Translation cache hit for {source_language} -> en")
            return cached
    
    try:
        llm = _get_llm()
        
        prompt = f"""Translate the following text from {source_language} to English.
Return ONLY the translated text, no explanations or additional content.

Text to translate:
{text}

English translation:"""
        
        response = await llm.ainvoke(prompt)
        translated = str(response.content).strip()
        
        # Clean up common LLM artifacts
        if translated.startswith('"') and translated.endswith('"'):
            translated = translated[1:-1]
        
        result = TranslationResult(
            original_text=text,
            translated_text=translated,
            source_language=source_language,
            target_language=CANONICAL_LANGUAGE,
            confidence=0.85,  # Reasonable default for LLM translation
            model_used=_model_name(llm),
        )
        
        if use_cache:
            _translation_cache.set(result)
        
        return result
        
    except Exception as e:
        logger.error(f"Translation to English failed: {e}")
        return TranslationResult(
            original_text=text,
            translated_text=text,  # Fallback: return original
            source_language=source_language,
            target_language=CANONICAL_LANGUAGE,
            confidence=0.0,
            error=str(e),
        )


async def translate_from_english(
    text: str,
    target_language: str,
    *,
    use_cache: bool = True,
) -> TranslationResult:
    """Translate text from English to target language.
    
    Args:
        text: English text to translate
        target_language: Target language code (e.g., "hi", "ta")
        use_cache: Whether to use translation cache
        
    Returns:
        TranslationResult with translated text
    """
    if not text or not text.strip():
        return TranslationResult(
            original_text=text,
            translated_text=text,
            source_language=CANONICAL_LANGUAGE,
            target_language=target_language,
            confidence=1.0,
            error="Empty text",
        )
    
    # Skip if target is English
    if target_language == CANONICAL_LANGUAGE:
        return TranslationResult(
            original_text=text,
            translated_text=text,
            source_language=CANONICAL_LANGUAGE,
            target_language=target_language,
            confidence=1.0,
        )
    
    # Check cache
    if use_cache:
        cached = _translation_cache.get(text, CANONICAL_LANGUAGE, target_language)
        if cached:
            logger.debug(f"Translation cache hit for en -> {target_language}")
            return cached
    
    # Language name mapping for better prompts
    language_names = {
        "hi": "Hindi",
        "ta": "Tamil",
        "te": "Telugu",
        "bn": "Bengali",
        "mr": "Marathi",
        "gu": "Gujarati",
        "kn": "Kannada",
        "ml": "Malayalam",
        "pa": "Punjabi",
        "ur": "Urdu",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "zh": "Chinese",
        "ja": "Japanese",
        "ko": "Korean",
        "ar": "Arabic",
        "pt": "Portuguese",
        "ru": "Russian",
    }
    
    target_name = language_names.get(target_language, target_language)
    
    try:
        llm = _get_llm()
        
        prompt = f"""Translate the following English text to {target_name}.
Return ONLY the translated text, no explanations or additional content.

English text:
{text}

{target_name} translation:"""
        
        response = await llm.ainvoke(prompt)
        translated = str(response.content).strip()
        
        # Clean up common LLM artifacts
        if translated.startswith('"') and translated.endswith('"'):
            translated = translated[1:-1]
        
        result = TranslationResult(
            original_text=text,
            translated_text=translated,
            source_language=CANONICAL_LANGUAGE,
            target_language=target_language,
            confidence=0.85,
            model_used=_model_name(llm),
        )
        
        if use_cache:
            _translation_cache.set(result)
        
        return result
        
    except Exception as e:
        logger.error(f"Translation from English failed: {e}")
        return TranslationResult(
            original_text=text,
            translated_text=text,  # Fallback: return original
            source_language=CANONICAL_LANGUAGE,
            target_language=target_language,
            confidence=0.0,
            error=str(e),
        )


async def translate_bidirectional(
    text: str,
    source_language: str,
    target_language: str,
    *,
    use_cache: bool = True,
) -> TranslationResult:
    """Translate text between any two languages via English pivot.
    
    Args:
        text: Text to translate
        source_language: Source language code
        target_language: Target language code
        use_cache: Whether to use translation cache
        
    Returns:
        TranslationResult with translated text
    """
    # Same language - no translation needed
    if source_language == target_language:
        return TranslationResult(
            original_text=text,
            translated_text=text,
            source_language=source_language,
            target_language=target_language,
            confidence=1.0,
        )
    
    # Source to English
    if source_language != CANONICAL_LANGUAGE:
        to_english = await translate_to_english(text, source_language, use_cache=use_cache)
        if to_english.error:
            return to_english
        intermediate = to_english.translated_text
    else:
        intermediate = text
    
    # English to target
    if target_language != CANONICAL_LANGUAGE:
        from_english = await translate_from_english(intermediate, target_language, use_cache=use_cache)
        return TranslationResult(
            original_text=text,
            translated_text=from_english.translated_text,
            source_language=source_language,
            target_language=target_language,
            confidence=min(0.85, from_english.confidence),
            model_used=from_english.model_used,
            error=from_english.error,
        )
    
    return TranslationResult(
        original_text=text,
        translated_text=intermediate,
        source_language=source_language,
        target_language=target_language,
        confidence=0.85,
    )
