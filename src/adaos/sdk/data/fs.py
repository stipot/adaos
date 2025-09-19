"""Restricted filesystem helpers for skill sandboxes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, IO

from adaos.sdk.core._ctx import require_ctx


def _safe_join(root: Path, name: str) -> Path:
    rel = Path(name)
    if rel.is_absolute():
        raise ValueError("absolute paths are not allowed in the SDK sandbox")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("unsafe path traversal") from exc
    return target


def tmp_path() -> Path:
    ctx = require_ctx("sdk.fs.tmp_path")
    tmp = Path(ctx.paths.tmp_dir()).resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def save_bytes(name: str, data: bytes) -> Path:
    ctx = require_ctx("sdk.fs.save_bytes")
    base = Path(ctx.paths.tmp_dir()).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = _safe_join(base, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def open(name: str, mode: str = "rb", **kwargs: Any) -> IO[Any]:  # noqa: A002 - deliberate shadowing
    ctx = require_ctx("sdk.fs.open")
    base = Path(ctx.paths.tmp_dir()).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = _safe_join(base, name)
    if "w" in mode or "a" in mode or "+" in mode:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target.open(mode, **kwargs)


__all__ = ["tmp_path", "save_bytes", "open"]
