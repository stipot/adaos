from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from typing import Any, Dict

from adaos.services.i18n.service import DEFAULT_LANG, I18nService

from adaos.sdk._ctx import require_ctx
from adaos.sdk.context import get_current_skill
from adaos.sdk.errors import SdkRuntimeNotInitialized


class I18n:
    """Thin facade over :class:`adaos.services.i18n.service.I18nService`."""

    def __init__(self, lang: str | None = None):
        self.lang = lang or os.getenv("ADAOS_LANG") or DEFAULT_LANG

    def translate(self, key: str, **kwargs: Any) -> str:
        try:
            ctx = require_ctx("SDK i18n used before runtime initialization.")
        except SdkRuntimeNotInitialized:
            return self._translate_pre_bootstrap(key, **kwargs)

        svc = I18nService(ctx)
        current = get_current_skill()
        skill_path = getattr(current, "path", None)
        skill_id = getattr(current, "name", None)
        scope = "skill" if key.startswith("prep.") else "global"

        return svc.translate(
            key,
            lang=self.lang,
            params=kwargs,
            skill_path=skill_path,
            skill_id=skill_id,
            scope=scope,
        )

    def _translate_pre_bootstrap(self, key: str, **kwargs: Any) -> str:
        messages = self._load_package_messages(self.lang)
        text = messages.get(key, key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    @staticmethod
    @lru_cache(maxsize=None)
    def _load_package_messages(lang: str) -> Dict[str, str]:
        package = "adaos.locales"
        candidates = [lang, DEFAULT_LANG]
        for candidate in candidates:
            if not candidate:
                continue
            resource_name = f"{candidate}.json"
            try:
                file = resources.files(package).joinpath(resource_name)
            except AttributeError:  # pragma: no cover - fallback for legacy importlib
                try:
                    with resources.open_text(package, resource_name, encoding="utf-8") as stream:
                        return json.load(stream)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    continue
            else:
                if file.is_file():
                    try:
                        with file.open("r", encoding="utf-8") as stream:
                            return json.load(stream)
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        continue
        return {}


_ = I18n().translate
