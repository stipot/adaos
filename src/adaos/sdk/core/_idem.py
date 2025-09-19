"""Helpers for idempotent request handling backed by the runtime KV store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping

from .errors import SdkRuntimeNotInitialized


def _ensure_kv(ctx: Any) -> Any:
    kv = getattr(ctx, "kv", None)
    if kv is None:
        raise SdkRuntimeNotInitialized("sdk.kv", "AgentContext.kv is not configured")
    return kv


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _key(namespace: str, request_id: str) -> str:
    ns = namespace.strip("/")
    rid = request_id.strip()
    if not rid:
        raise ValueError("request_id must be a non-empty string")
    return f"requests/{ns}/{rid}" if ns else f"requests/{rid}"


def load(ctx: Any, namespace: str, request_id: str) -> Mapping[str, Any] | None:
    """Load a previously persisted idempotent record for ``request_id``."""

    kv = _ensure_kv(ctx)
    key = _key(namespace, request_id)
    return kv.get(key)


def save(ctx: Any, namespace: str, request_id: str, payload: Mapping[str, Any] | MutableMapping[str, Any]) -> Mapping[str, Any]:
    """Persist ``payload`` under the idempotency namespace and return it."""

    kv = _ensure_kv(ctx)
    record = dict(payload)
    record.setdefault("request_id", request_id)
    record.setdefault("stored_at", _iso_now())
    key = _key(namespace, request_id)
    kv.set(key, record)
    return record


__all__ = ["load", "save"]
