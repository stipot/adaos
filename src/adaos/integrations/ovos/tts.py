from __future__ import annotations

import warnings

# TODO Update OVOS
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API", category=UserWarning)
import tempfile
import sys
import subprocess

from ovos_plugin_manager.tts import OVOSTTSFactory
from ovos_config.config import Configuration


def _play_wav_fallback(path: str) -> None:
    if sys.platform.startswith("win"):
        try:
            import winsound  # type: ignore

            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            subprocess.run(["afplay", path], check=False)
            return
        except Exception:
            pass
    for cmd in ("aplay", "paplay", "play"):
        try:
            subprocess.run([cmd, path], check=False)
            return
        except Exception:
            continue
    print(f"[OVOS] Не удалось воспроизвести WAV: {path}")


class OVOSTTSAdapter:
    """
    OVOS TTS через ovos-config.
    Требует корректный ~/.config/mycroft/mycroft.conf с секцией "tts".
    """

    def __init__(self, override_voice: str | None = None):
        conf = Configuration()
        tts_cfg = conf.get("tts", {}) or {}
        self._tts = OVOSTTSFactory().create(config=tts_cfg)
        self._voice = override_voice

        # Если у плагина есть init — используем выбранный голос
        init = getattr(self._tts, "init", None)
        if callable(init):
            voice = self._voice or getattr(self._tts, "voice", None) or "default"
            self._tts.init(voice, tts_cfg)

    def say(self, text: str) -> None:
        print("its OVOSTTSAdapter")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            wav_path = tmp.name

        self._tts.get_tts(text, wav_path)

        play = getattr(self._tts, "play", None)
        if callable(play):
            play(wav_path)
        else:
            _play_wav_fallback(wav_path)


""" 
// ~/.config/mycroft/mycroft.conf
{
  "tts": {
    "module": "ovos-tts-plugin-mimic3",
    "ovos-tts-plugin-mimic3": {
      "voice": "en_UK/apope_low"
    }
  }
}

pip install ovos-tts-server ovos-tts-plugin-mimic3 ovos-config ovos-plugin-manager playsound==1.2.2
"""
