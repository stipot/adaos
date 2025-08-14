import os
import json
from pathlib import Path
import logging
from typing import Dict, Optional
from adaos.sdk.context import LOCALES_DIR, DEFAULT_LANG


class I18n:
    def __init__(self, lang: str = None):
        self.lang = lang or os.getenv("ADAOS_LANG", DEFAULT_LANG)
        self._load_translations()

    def _load_translations(self) -> None:
        """Загружает глобальные переводы из LOCALES_DIR."""
        file_path = Path(LOCALES_DIR) / f"{self.lang}.json"
        if not file_path.exists():
            file_path = Path(LOCALES_DIR) / f"{DEFAULT_LANG}.json"
        self.translations = json.loads(file_path.read_text(encoding="utf-8"))

    def translate(self, key: str, **kwargs) -> str:
        """Основной метод для перевода (аналог `t` из текущей реализации)."""
        text = self.translations.get(key, key)
        return text.format(**kwargs)


# Экспортируемые объекты для SDK
_ = I18n().translate  # Короткий алиас
