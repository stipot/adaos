# src\adaos\sdk\i18n.py
# Adopted
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Any, Dict

from adaos.services.agent_context import get_ctx
from adaos.services.i18n.service import I18nService, DEFAULT_LANG
from adaos.sdk.context import get_current_skill


class I18n:
    def __init__(self, lang: Optional[str] = None):
        self.lang = lang or os.getenv("ADAOS_LANG") or DEFAULT_LANG

    def translate(self, key: str, **kwargs) -> str:
        ctx = get_ctx()
        svc = I18nService(ctx)

        cur = get_current_skill()  # ожидаем объект с полями name/path, как у тебя
        skill_path: Optional[Path] = getattr(cur, "path", None)
        skill_id: Optional[str] = getattr(cur, "name", None)

        # auto-scope: ключи с 'prep.' — считаем «локаль навыка»
        scope = "skill" if key.startswith("prep.") else "global"

        return svc.translate(
            key,
            lang=self.lang,
            params=kwargs,
            skill_path=skill_path,
            skill_id=skill_id,
            scope=scope,
        )


# короткий алиас
_ = I18n().translate
