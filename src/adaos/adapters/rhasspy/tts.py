from __future__ import annotations

import os
import sys
import tempfile
import urllib.parse
import urllib.request
import subprocess


def _play_wav_fallback(path: str) -> None:
    # Windows
    if sys.platform.startswith("win"):
        try:
            import winsound  # type: ignore

            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        except Exception:
            pass

    # macOS
    if sys.platform == "darwin":
        try:
            subprocess.run(["afplay", path], check=False)
            return
        except Exception:
            pass

    # Linux / Unix
    for cmd in ("aplay", "paplay", "play"):
        try:
            subprocess.run([cmd, path], check=False)
            return
        except Exception:
            continue

    print(f"[Rhasspy] Не удалось воспроизвести WAV: {path}")


class RhasspyTTSAdapter:
    """
    Простой клиент Rhasspy TTS (HTTP API).
    Требуется работающий Rhasspy с включённым TTS.

    Базовый URL берём из:
      - аргумента конструктора
      - или переменной окружения ADAOS_RHASSPY_URL
      - или по умолчанию http://127.0.0.1:12101
    """

    def __init__(self, base_url: str | None = None, voice: str | None = None, lang: str | None = None):
        self.base_url = (base_url or os.getenv("ADAOS_RHASSPY_URL") or "http://127.0.0.1:12101").rstrip("/")
        self.voice = voice
        self.lang = lang

    def say(self, text: str) -> None:
        print("its RhasspyTTSAdapter")
        wav = self._synthesize(text)
        if not wav:
            print("[Rhasspy] Пустой ответ от /api/tts", file=sys.stderr)
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(wav)
            path = tmp.name
        _play_wav_fallback(path)

    def _synthesize(self, text: str) -> bytes:
        params: dict[str, str] = {}
        if self.voice:
            params["voice"] = self.voice
        if self.lang:
            # имя параметра зависит от TTS backend; часто подходит 'language'
            params["language"] = self.lang

        url = f"{self.base_url}/api/tts"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url=url,
            data=text.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
