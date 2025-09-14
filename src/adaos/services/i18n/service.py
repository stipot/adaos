from __future__ import annotations
import os, json, inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any, Dict

from adaos.services.agent_context import AgentContext

DEFAULT_LANG = "en"


@dataclass(slots=True)
class I18nService:
    ctx: AgentContext
    _cache_global: Dict[str, Dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _cache_skill: Dict[tuple[str, str], Dict[str, str]] = field(default_factory=dict, init=False, repr=False)

    # ---------- public ----------
    def translate(
        self,
        key: str,
        *,
        lang: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
        skill_path: Optional[Path] = None,
        skill_id: Optional[str] = None,
        scope: Optional[str] = None,  # "global" | "skill" | None (auto)
    ) -> str:
        """Единая точка перевода. Без SDK-зависимостей."""
        lang = lang or getattr(self.ctx.settings, "lang", None) or os.getenv("ADAOS_LANG") or DEFAULT_LANG
        params = params or {}

        if scope == "global" or (scope is None and not key.startswith("prep.")):
            messages = self._load_global(lang)
        else:
            messages = self._load_skill(lang, skill_path=skill_path, skill_id=skill_id)

        text = messages.get(key, key)
        try:
            return text.format(**params)
        except Exception:
            # не валимся, если плейсхолдеры не сошлись
            return text

    # ---------- loaders ----------
    def _load_global(self, lang: str) -> Dict[str, str]:
        if lang in self._cache_global:
            return self._cache_global[lang]
        base = self.ctx.paths.locales_dir()
        dflt = base / f"{DEFAULT_LANG}.json"
        candidates = [(base / f"{lang}.json"), dflt]
        data: Dict[str, str] = {}
        for p in candidates:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                break
        self._cache_global[lang] = data
        return data

    def _load_skill(self, lang: str, *, skill_path: Optional[Path], skill_id: Optional[str]) -> Dict[str, str]:
        # ключ кэша: (skill_id|path, lang)
        key = ((skill_id or (skill_path.name if skill_path else "")), lang)
        if key in self._cache_skill:
            return self._cache_skill[key]

        # приоритет путей:
        # 1) <skill_path>/i18n/<lang>.json
        # 2) <skills_locales_dir>/<skill_id or folder>/<lang>.json
        data: Dict[str, str] = {}
        if skill_path:
            p1 = skill_path / "i18n" / f"{lang}.json"
            if p1.exists():
                data = json.loads(p1.read_text(encoding="utf-8"))
        if not data:
            base = self.ctx.paths.skills_locales_dir()
            sid = skill_id or (skill_path.name if skill_path else None)
            if sid:
                p2 = base / sid / f"{lang}.json"
                if p2.exists():
                    data = json.loads(p2.read_text(encoding="utf-8"))

        # fallback на DEFAULT_LANG в тех же местах
        if not data:
            if skill_path:
                p1d = skill_path / "i18n" / f"{DEFAULT_LANG}.json"
                if p1d.exists():
                    data = json.loads(p1d.read_text(encoding="utf-8"))
            if not data and (skill_id or skill_path):
                base = self.ctx.paths.skills_locales_dir()
                sid = skill_id or (skill_path.name if skill_path else None)
                if sid:
                    p2d = base / sid / f"{DEFAULT_LANG}.json"
                    if p2d.exists():
                        data = json.loads(p2d.read_text(encoding="utf-8"))

        self._cache_skill[key] = data
        return data
