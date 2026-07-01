"""Language Detection Module.

Detects the language of input text using lightweight detection libraries.
Supports multiple Indic languages + English.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

# Supported languages with their codes
SUPPORTED_LANGUAGES = {
    "en": "English",
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

LanguageCode = Literal[
    "en", "hi", "ta", "te", "bn", "mr", "gu", "kn", "ml", "pa", "ur",
    "es", "fr", "de", "zh", "ja", "ko", "ar", "pt", "ru", "unknown"
]


@dataclass
class LanguageResult:
    """Result of language detection."""
    
    language: LanguageCode
    language_name: str
    confidence: float
    script: str | None = None
    error: str | None = None
    
    @property
    def is_english(self) -> bool:
        return self.language == "en"
    
    @property
    def needs_translation(self) -> bool:
        return self.language != "en" and self.language != "unknown"


def _detect_with_langdetect(text: str) -> LanguageResult:
    """Detect language using langdetect library."""
    try:
        from langdetect import detect_langs, DetectorFactory
        
        # Make detection deterministic
        DetectorFactory.seed = 0
        
        results = detect_langs(text)
        if not results:
            return LanguageResult(
                language="unknown",
                language_name="Unknown",
                confidence=0.0,
                error="No language detected",
            )
        
        top = results[0]
        lang_code = str(top.lang)
        confidence = float(top.prob)
        
        # Map to our supported languages
        # If the detected language is not in our supported list and text uses Latin script,
        # default to English (langdetect often misdetects short English as other languages)
        if lang_code not in SUPPORTED_LANGUAGES:
            script = _detect_script(text)
            if script == "LATIN":
                lang_code = "en"
                confidence = min(confidence, 0.7)  # Lower confidence for fallback
            else:
                lang_code = "unknown"
        
        return LanguageResult(
            language=lang_code,  # type: ignore
            language_name=SUPPORTED_LANGUAGES.get(lang_code, "Unknown"),
            confidence=confidence,
        )
    except Exception as e:
        logger.warning(f"langdetect failed: {e}")
        return LanguageResult(
            language="unknown",
            language_name="Unknown",
            confidence=0.0,
            error=str(e),
        )


def _detect_with_fasttext(text: str) -> LanguageResult:
    """Detect language using fasttext (if available)."""
    try:
        import fasttext
        import os
        
        model_path = os.environ.get("FASTTEXT_MODEL_PATH", "/models/lid.176.bin")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"FastText model not found at {model_path}")
        
        model = fasttext.load_model(model_path)
        predictions = model.predict(text.replace("\n", " "), k=1)
        
        if not predictions or not predictions[0]:
            return LanguageResult(
                language="unknown",
                language_name="Unknown",
                confidence=0.0,
                error="No prediction",
            )
        
        # FastText returns labels like "__label__en"
        label = predictions[0][0].replace("__label__", "")
        confidence = float(predictions[1][0])
        
        if label not in SUPPORTED_LANGUAGES:
            label = "unknown"
        
        return LanguageResult(
            language=label,  # type: ignore
            language_name=SUPPORTED_LANGUAGES.get(label, "Unknown"),
            confidence=confidence,
        )
    except Exception as e:
        logger.warning(f"fasttext failed: {e}")
        return LanguageResult(
            language="unknown",
            language_name="Unknown",
            confidence=0.0,
            error=str(e),
        )


def _detect_script(text: str) -> str | None:
    """Detect the script of the text (Latin, Devanagari, etc.)."""
    import unicodedata
    
    script_counts: dict[str, int] = {}
    
    for char in text:
        if char.isalpha():
            try:
                script = unicodedata.name(char).split()[0]
                script_counts[script] = script_counts.get(script, 0) + 1
            except ValueError:
                pass
    
    if not script_counts:
        return None
    
    return max(script_counts, key=script_counts.get)  # type: ignore


def detect_language(text: str, *, use_fasttext: bool = False) -> LanguageResult:
    """Detect the language of input text.
    
    Args:
        text: Input text to analyze
        use_fasttext: If True, prefer fasttext over langdetect
        
    Returns:
        LanguageResult with language code, name, confidence, and script
    """
    if not text or not text.strip():
        return LanguageResult(
            language="unknown",
            language_name="Unknown",
            confidence=0.0,
            error="Empty text",
        )
    
    # Clean text for detection
    clean_text = text.strip()[:1000]  # Limit to first 1000 chars
    
    # Try detection
    if use_fasttext:
        result = _detect_with_fasttext(clean_text)
        if result.error:
            result = _detect_with_langdetect(clean_text)
    else:
        result = _detect_with_langdetect(clean_text)
        if result.error:
            result = _detect_with_fasttext(clean_text)
    
    # Add script detection
    result.script = _detect_script(clean_text)
    
    return result


async def adetect_language(text: str, *, use_fasttext: bool = False) -> LanguageResult:
    """Async wrapper for language detection."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: detect_language(text, use_fasttext=use_fasttext))
