"""Mock voice IO adapters (TTS/STT) for local development."""

from __future__ import annotations

import sys

from .io_console import print as console_print


def tts_speak(text: str | None) -> dict[str, bool]:
    """Echo ``text`` to the console prefixed with ``[TTS]``."""

    console_print(f"[TTS] {text or ''}")
    return {"ok": True}


def stt_listen(timeout: str = "20s") -> dict[str, str]:
    """Read a line from stdin to emulate a STT response."""

    console_print("[STT] эмуляция ввода имени: введите имя и нажмите Enter")
    try:
        line = sys.stdin.readline()
    except Exception:
        return {}
    text = line.strip()
    return {"text": text} if text else {}


__all__ = ["tts_speak", "stt_listen"]
