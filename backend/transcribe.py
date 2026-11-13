"""Local speech-to-English-text via faster-whisper (CTranslate2, CPU int8).

Fallback for sources with no usable captions — notably Bilibili (Chinese
audio, no English subs) and raw file uploads. Whisper's translate task emits
ENGLISH text regardless of the spoken language, which is what the Tier 2 LLM
needs to write English titles and descriptions.
"""
from pathlib import Path

from . import config

_MODEL = None
MAX_TRANSCRIBE_SECONDS = 30 * 60   # don't spend hours on a 12h walkthrough


def _model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        # "small" ~460MB int8: good accuracy/speed balance on this CPU
        _MODEL = WhisperModel("small", device="cpu", compute_type="int8",
                              download_root=str(config.MODELS_DIR))
    return _MODEL


def translate_to_english(path: str | Path, duration: float,
                         progress=None) -> list[dict]:
    """-> [{"t": seconds, "text": ...}] English transcript, or [] on failure."""
    if duration > MAX_TRANSCRIBE_SECONDS:
        return []
    try:
        segments, _info = _model().transcribe(
            str(path), task="translate", beam_size=1, vad_filter=True)
        out = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                out.append({"t": float(seg.start), "text": text})
            if progress:
                progress(min(0.99, seg.end / max(duration, 1.0)))
        return out
    except Exception:
        return []
