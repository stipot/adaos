# src/adaos/facades/i18n_lazy.py
from __future__ import annotations
import json, os
from importlib import resources
from adaos.services.agent_context import get_ctx


def _preboot_translate(key: str, **kw) -> str:
    lang = os.getenv("ADAOS_LANG", "en")
    try:
        with resources.files("adaos").joinpath("locales").joinpath(f"{lang}.json").open("r", encoding="utf-8") as f:
            messages = json.load(f)
    except Exception:
        messages = {}
    text = messages.get(key, key)
    try:
        return text.format(**kw)
    except Exception:
        return text


def _(key: str, **kw) -> str:
    try:
        return get_ctx().i18n.translate(key, params=kw)
    except Exception:
        return _preboot_translate(key, **kw)
