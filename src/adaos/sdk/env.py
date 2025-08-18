from __future__ import annotations
import os
from functools import lru_cache

# Допустимые значения
_TTS_ALLOWED = {"native", "ovos"}
_STT_ALLOWED = {"native", "ovos", "rhasspy", "vosk"}
_AUDIO_OUT_ALLOWED = {"pyaudio", "sounddevice", "system"}


def _read(name: str, default: str) -> str:
    v = os.getenv(name, default).strip().lower()
    return v


@lru_cache
def get_tts_backend() -> str:
    v = _read("ADAOS_TTS", "native")
    if v not in _TTS_ALLOWED:
        raise RuntimeError(f"Invalid ADAOS_TTS='{v}'. Allowed: {sorted(_TTS_ALLOWED)}")
    return v


@lru_cache
def get_stt_backend() -> str:
    v = _read("ADAOS_STT", "native")
    if v not in _STT_ALLOWED:
        raise RuntimeError(f"Invalid ADAOS_STT='{v}'. Allowed: {sorted(_STT_ALLOWED)}")
    return v


@lru_cache
def get_audio_out_backend() -> str:
    v = _read("ADAOS_AUDIO_OUT", "system")
    if v not in _AUDIO_OUT_ALLOWED:
        raise RuntimeError(f"Invalid ADAOS_AUDIO_OUT='{v}'. Allowed: {sorted(_AUDIO_OUT_ALLOWED)}")
    return v
