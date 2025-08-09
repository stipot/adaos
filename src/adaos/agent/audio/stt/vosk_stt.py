# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import json
import queue
from pathlib import Path
from typing import Callable, Optional

import sounddevice as sd
from vosk import Model, KaldiRecognizer  # type: ignore
from adaos.sdk.context import ADAOS_VOSK_MODEL

from adaos.agent.utils.model_manager import ensure_vosk_model


class VoskSTT:
    def __init__(
        self,
        model_path: Optional[str] = None,
        samplerate: int = 16000,
        device: Optional[int] = None,
        blocksize: int = 8000,
        lang: Optional[str] = None,
        model_zip: Optional[Path] = None,
    ):
        """
        model_path: если не задан, берём ADAOS_VOSK_MODEL, иначе auto‑ensure по lang (или 'en')
        lang: 'en' / 'ru' и т.п. — автозагрузка и выбор алиаса модели
        """
        """ if model_path:
            model_dir = Path(model_path)
        else:
            env_path = os.environ.get("ADAOS_VOSK_MODEL")
            if env_path:
                model_dir = Path(env_path)
            else:
                model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL, local_zip=model_zip) """
        model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL, local_zip=model_zip)
        if not model_dir.exists():
            raise RuntimeError(f"Vosk модель не найдена: {model_dir.resolve()}\n" "Укажи --model или ADAOS_VOSK_MODEL, либо используй --lang для авто‑загрузки.")

        self.model = Model(str(model_dir))
        self.samplerate = int(samplerate)
        self.device = device
        self.blocksize = int(blocksize)
        self._q: "queue.Queue[bytes]" = queue.Queue()

    def _audio_callback(self, indata, frames, time, status):  # noqa: N803
        if status:
            pass
        self._q.put(bytes(indata))

    def listen(self, on_text: Callable[[str], None]) -> None:
        rec = KaldiRecognizer(self.model, self.samplerate)
        rec.SetWords(True)

        with sd.RawInputStream(
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            device=self.device,
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        ):
            print("[Vosk] Слушаю... (Ctrl+C для выхода)")
            try:
                while True:
                    data = self._q.get()
                    if rec.AcceptWaveform(data):
                        result = rec.Result()
                        try:
                            payload = json.loads(result)
                        except Exception:
                            payload = {}
                        text = (payload.get("text") or "").strip()
                        if text:
                            on_text(text)
            except KeyboardInterrupt:
                print("\n[Vosk] Остановлено пользователем.")
