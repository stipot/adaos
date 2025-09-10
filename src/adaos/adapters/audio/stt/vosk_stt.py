from __future__ import annotations
import json
import queue
from pathlib import Path
from typing import Generator, Optional
import typer
from typing import Generator, Optional, Iterable
import vosk
from adaos.apps.bootstrap import get_ctx

sd = None
_sd_error = None
try:
    import sounddevice as sd  # требует PortAudio
except OSError as e:
    _sd_error = e
    sd = None
except Exception as e:
    _sd_error = e
    sd = None


class VoskSTT:
    def __init__(
        self, model_path: Optional[str] = None, samplerate: int = 16000, device: Optional[int | str] = None, lang: str = "en", external_stream: Optional[Iterable[bytes]] = None
    ):

        ADAOS_VOSK_MODEL = str(get_ctx().paths.base / "models" / "vosk" / "en-us")  # TODO move to constants
        # Инициализация модели
        if model_path:
            model_dir = Path(model_path)
        else:
            # ваша ensure_vosk_model уже готова – используем
            from adaos.agent.utils.model_manager import ensure_vosk_model

            model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL)

        if not Path(model_dir).exists():
            raise RuntimeError(f"Vosk model not found: {model_dir}")
        if sd is None:
            raise RuntimeError(
                "Audio backend (sounddevice/PortAudio) is unavailable. "
                f"Original error: {_sd_error}. "
                "Install system libportaudio (and dev headers) or run without native audio."
            )
        self.model = vosk.Model(str(model_dir))
        self.samplerate = samplerate
        self.rec = vosk.KaldiRecognizer(self.model, self.samplerate)
        self.rec.SetWords(True)
        self._external_stream = external_stream
        self._q = None
        self.stream = None
        if external_stream is None:
            import queue, sounddevice as sd

            self._q = queue.Queue()
            self.stream = sd.RawInputStream(samplerate=self.samplerate, blocksize=8000, dtype="int16", channels=1, callback=self._on_audio, device=device)
            self.stream.start()
        else:
            # Очередь аудио-чанков из callback’а
            self._q: queue.Queue[bytes] = queue.Queue()

            # Аудио-поток
            self.stream = sd.RawInputStream(
                samplerate=self.samplerate, blocksize=8000, dtype="int16", channels=1, callback=self._on_audio, device=device
            )  # ≈0.5s при 16кГц int16 mono
            self.stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            # можно логировать при желании
            pass
        self._q.put(bytes(indata))

    def listen_stream(self) -> Generator[str, None, None]:
        """Бесконечный генератор итоговых фраз (final results)."""
        if self._external_stream is not None:
            for chunk in self._external_stream:
                if self.rec.AcceptWaveform(chunk):
                    res = json.loads(self.rec.Result())
                    text = (res.get("text") or "").strip()
                    if text:
                        yield text
            return

        # штатный режим через sounddevice очередь
        while True:
            data = self._q.get()
            if self.rec.AcceptWaveform(data):
                res = json.loads(self.rec.Result())
                text = (res.get("text") or "").strip()
                if text:
                    yield text

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
