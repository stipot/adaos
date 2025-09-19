"""Skill-scoped key/value helpers exposed via the SDK facade."""

from __future__ import annotations

from typing import Any, Iterable, List

from ._runtime import require_ctx
from .context import get_current_skill

_GLOBAL_NAMESPACE = "global"


def _normalize_fragment(fragment: str) -> str:
    return fragment.lstrip("/")


def _namespace() -> str:
    skill = get_current_skill()
    name = getattr(skill, "name", None)
    if name:
        return f"skills/{name}"
    return _GLOBAL_NAMESPACE


def _qualified_key(key: str) -> str:
    fragment = _normalize_fragment(key)
    if not fragment:
        raise ValueError("memory key must be a non-empty string")
    return f"{_namespace()}/{fragment}"


def _qualified_prefix(prefix: str) -> str:
    fragment = _normalize_fragment(prefix)
    base = _namespace()
    return f"{base}/{fragment}" if fragment else f"{base}/"


def get(key: str, default: Any | None = None) -> Any:
    ctx = require_ctx("sdk.memory.get")
    return ctx.kv.get(_qualified_key(key), default)


def put(key: str, value: Any, ttl: int | None = None) -> None:  # noqa: D401 - thin facade
    ctx = require_ctx("sdk.memory.put")
    # TTL handling delegated to storage backend when supported.
    try:
        ctx.kv.set(_qualified_key(key), value, ttl=ttl)  # type: ignore[call-arg]
    except TypeError:
        ctx.kv.set(_qualified_key(key), value)


def delete(key: str) -> None:
    ctx = require_ctx("sdk.memory.delete")
    ctx.kv.delete(_qualified_key(key))


def list(prefix: str = "") -> List[str]:
    ctx = require_ctx("sdk.memory.list")
    qualified_prefix = _qualified_prefix(prefix)
    if hasattr(ctx.kv, "list"):
        keys: Iterable[str] = ctx.kv.list(prefix=qualified_prefix)  # type: ignore[arg-type]
    else:  # pragma: no cover - depends on backend capabilities
        raise NotImplementedError("KV backend does not support list() operation")

    scope_prefix = f"{_namespace()}/"
    result: List[str] = []
    for full_key in keys:
        if not isinstance(full_key, str):
            continue
        if not full_key.startswith(scope_prefix):
            continue
        result.append(full_key[len(scope_prefix) :])
    return result


__all__ = ["get", "put", "delete", "list"]
