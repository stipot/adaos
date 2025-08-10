from __future__ import annotations
import json
import queue
from pathlib import Path
from typing import Generator, Optional
import typer

from adaos.sdk.context import ADAOS_VOSK_MODEL

import sounddevice as sd
import vosk


class VoskSTT:
    def __init__(self, model_path: str = typer.Option(None), samplerate: int = 16000, device: Optional[int | str] = None, lang: str = "en"):
        # Инициализация модели
        if model_path:
            model_dir = Path(model_path)
        else:
            # ваша ensure_vosk_model уже готова – используем
            from adaos.agent.utils.model_manager import ensure_vosk_model

            model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL)

        if not Path(model_dir).exists():
            raise RuntimeError(f"Vosk model not found: {model_dir}")

        self.model = vosk.Model(str(model_dir))
        self.samplerate = samplerate
        self.rec = vosk.KaldiRecognizer(self.model, self.samplerate)
        self.rec.SetWords(True)

        # Очередь аудио-чанков из callback’а
        self._q: queue.Queue[bytes] = queue.Queue()

        # Аудио-поток
        self.stream = sd.RawInputStream(samplerate=self.samplerate, blocksize=8000, dtype="int16", channels=1, callback=self._on_audio, device=device)  # ≈0.5s при 16кГц int16 mono
        self.stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            # можно логировать при желании
            pass
        self._q.put(bytes(indata))

    def listen_stream(self) -> Generator[str, None, None]:
        """Бесконечный генератор итоговых фраз (final results)."""
        while True:
            data = self._q.get()
            if self.rec.AcceptWaveform(data):
                res = json.loads(self.rec.Result())
                text = (res.get("text") or "").strip()
                if text:
                    yield text
            else:
                # partial = json.loads(self.rec.PartialResult()).get("partial", "")
                # при желании можно отдавать partial через другой канал
                pass

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
