"""Local JSON-backed storage scoped to the active skill context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.errors import SdkRuntimeNotInitialized

__all__ = ["get", "set"]


def _memory_path() -> Path:
    ctx = require_ctx("sdk.data.skill_memory")
    current = ctx.skill_ctx.get()
    if current is None or getattr(current, "path", None) is None:
        raise SdkRuntimeNotInitialized("sdk.data.skill_memory", "current skill is not set")
    path = Path(current.path) / ".skill_env.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get(key: str, default: Any | None = None) -> Any:
    mem_file = _memory_path()
    if mem_file.exists():
        try:
            data = json.loads(mem_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            return data.get(key, default)
    return default


def set(key: str, value: Any) -> None:
    mem_file = _memory_path()
    if mem_file.exists():
        try:
            data = json.loads(mem_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    mem_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
