from __future__ import annotations
import time
from typing import Optional

try:
    from jnius import autoclass, cast

    _HAS_JNIUS = True
except Exception:
    _HAS_JNIUS = False


class AndroidTTS:
    """Минимальная обёртка над Android TextToSpeech.
    Используем системные движки (Google TTS / Samsung / др)."""

    def __init__(self, lang_hint: str = "en-US"):
        if not _HAS_JNIUS:
            raise RuntimeError("pyjnius недоступен (не Android среда?)")

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        self.activity = PythonActivity.mActivity

        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        Locale = autoclass("java.util.Locale")

        # Выбираем локаль
        self.locale = Locale.forLanguageTag(lang_hint.replace("_", "-"))

        # Создаём экземпляр TTS
        self._ready = False
        self.tts = TextToSpeech(self.activity, self._init_listener())

        # Попробуем сразу выставить язык (некоторые движки ленивые)
        # Установка языка произойдёт в onInit, дублирование — не страшно
        self._set_language(lang_hint)

    def _init_listener(self):
        # onInitListener — интерфейс, создадим анонимный класс через Python
        from jnius import PythonJavaClass, java_method

        class OnInitListener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            def __init__(self, outer):
                super().__init__()
                self.outer = outer

            @java_method("(I)V")
            def onInit(self, status):
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                if status == TextToSpeech.SUCCESS:
                    self.outer._ready = True
                else:
                    self.outer._ready = False

        return OnInitListener(self)

    def _set_language(self, lang_hint: str):
        if not self._ready:
            # Ждём коротко и пробуем ещё раз — движок иногда инициализируется ~100-500 мс
            for _ in range(10):
                if self._ready:
                    break
                time.sleep(0.1)

        if self._ready:
            result = self.tts.setLanguage(self.locale)
            # result: -1, -2 — отсутствует язык/данные; 0/1 — ок
            return result
        return -1

    def say(self, text: str, flush: bool = True):
        if not self._ready:
            # последний шанс — иногда onInit приходит с задержкой
            for _ in range(20):
                if self._ready:
                    break
                time.sleep(0.05)
        if not self._ready:
            raise RuntimeError("Android TTS не готов")

        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        HashMap = autoclass("java.util.HashMap")
        params = HashMap()
        if flush:
            self.tts.stop()
        self.tts.speak(text, TextToSpeech.QUEUE_ADD, params)

    def shutdown(self):
        try:
            self.tts.stop()
            self.tts.shutdown()
        except Exception:
            pass
