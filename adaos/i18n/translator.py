import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCALES_DIR = BASE_DIR / "locales"
DEFAULT_LANG = "en"


class Translator:
    def __init__(self, lang: str = None):
        self.lang = lang or os.getenv("ADAOS_LANG", DEFAULT_LANG)
        self.messages = self._load_messages(self.lang)

    def _load_messages(self, lang):
        file_path = LOCALES_DIR / f"{lang}.json"
        if not file_path.exists():
            file_path = LOCALES_DIR / f"{DEFAULT_LANG}.json"
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def t(self, key: str, **kwargs):
        """Возвращает переведённую строку по ключу"""
        text = self.messages.get(key, key)
        return text.format(**kwargs)


# Singleton для использования во всём проекте
translator = Translator()
_ = translator.t
