import os
import json
from pathlib import Path
import logging
import inspect

BASE_DIR = Path(__file__).resolve().parent
LOCALES_DIR = BASE_DIR / "locales"
DEFAULT_LANG = "en"


class Translator:
    def __init__(self, lang: str = None):
        self.lang = lang or os.getenv("ADAOS_LANG", DEFAULT_LANG)
        self.messages = self._load_messages(self.lang)
        self.skill_cache = {}  # кэш локалей навыков: {skill_path: messages}

    def _load_messages(self, lang):
        file_path = LOCALES_DIR / f"{lang}.json"
        if not file_path.exists():
            file_path = LOCALES_DIR / f"{DEFAULT_LANG}.json"
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _find_skill_path_in_stack(self) -> Path:
        """
        Ищет путь к навыку по стеку вызова.
        Предполагаем, что prepare.py находится в <skill_path>/prep/prepare.py
        """
        for frame_info in inspect.stack():
            filename = Path(frame_info.filename)
            if filename.name == "prepare.py" and "skills" in filename.parts:
                return filename.parent.parent
        return None

    def _load_or_create_skill_locale(self, skill_path: Path, locale: str):
        """
        Загружает локаль навыка или создаёт её из lang_res()
        """
        i18n_dir = skill_path / "i18n"
        locale_file = i18n_dir / f"{locale}.json"

        if locale_file.exists():
            return json.loads(locale_file.read_text(encoding="utf-8"))

        # Если файла нет — пробуем найти lang_res() в prepare.py
        prepare_file = skill_path / "prep" / "prepare.py"
        if not prepare_file.exists():
            logging.error(f"[i18n] prepare.py not found for skill at {skill_path}")
            return {}

        module = {}
        try:
            exec(prepare_file.read_text(encoding="utf-8"), module)
        except Exception as e:
            logging.error(f"[i18n] Error executing prepare.py for {skill_path}: {e}")
            return {}

        if "lang_res" not in module:
            logging.error(f"[i18n] lang_res() not found in {prepare_file}")
            return {}

        # Создаём словарь по умолчанию и сохраняем
        messages = module["lang_res"]()
        if not isinstance(messages, dict):
            logging.error(f"[i18n] lang_res() did not return a dict in {prepare_file}")
            return {}

        i18n_dir.mkdir(exist_ok=True)
        locale_file.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")
        return messages

    def t(self, key: str, **kwargs):
        """
        Возвращает строку локализации:
        - Если ключ без префикса prep. — глобальная локаль
        - Если ключ с префиксом prep. — локаль навыка
        """
        # Глобальные ключи
        if not key.startswith("prep."):
            text = self.messages.get(key, key)
            return text.format(**kwargs)

        # Локали навыка
        skill_path = self._find_skill_path_in_stack()
        if not skill_path:
            logging.error(f"[i18n] Unable to resolve skill path for key '{key}'")
            return key

        # Кэшируем локали навыка
        if skill_path not in self.skill_cache:
            self.skill_cache[skill_path] = self._load_or_create_skill_locale(skill_path, self.lang)

        messages = self.skill_cache.get(skill_path, {})
        text = messages.get(key)
        if text is None:
            logging.error(f"[i18n] Key '{key}' not found in skill at {skill_path}")
            return key

        return text.format(**kwargs)


# Singleton для использования во всём проекте
translator = Translator()
_ = translator.t
