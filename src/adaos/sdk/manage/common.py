"""Shared helpers for manage.* control-plane tools."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

from adaos.sdk.core import (
    SCHEMA_REQUEST_BASE,
    SCHEMA_RESULT_ENVELOPE,
    QuotaExceeded,
    SdkRuntimeNotInitialized,
    result_envelope,
)
from adaos.sdk.core._cap import require_cap as _require_cap
from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core._idem import load as _idem_load, save as _idem_save

__all__ = [
    "_require_cap",
    "_current_skill_id",
    "_safe_ns_key",
    "_load_request",
    "_save_request",
    "_meta_to_dict",
    "_wrap_quota",
    "_result",
    "SCHEMA_REQUEST_BASE",
    "SCHEMA_RESULT_ENVELOPE",
    "result_envelope",
]


def _current_skill_id() -> str:
    ctx = require_ctx("sdk.manage.skill")
    current = getattr(ctx, "skill_ctx", None)
    if current is None:
        raise SdkRuntimeNotInitialized("sdk.manage", "skill context port is not configured")
    skill = current.get()
    skill_id = getattr(skill, "name", None) if skill else None
    if not skill_id:
        raise SdkRuntimeNotInitialized("sdk.manage", "current skill is not set")
    return str(skill_id)


def _safe_ns_key(*parts: str) -> str:
    segments: list[str] = []
    for part in parts:
        if part is None:
            continue
        token = str(part).strip().strip("/")
        if not token:
            continue
        if ".." in token.split("/"):
            raise ValueError(f"unsafe namespace segment: {token}")
        segments.append(token)
    return "/".join(segments)


def _load_request(ctx: Any, namespace: str, request_id: str) -> Mapping[str, Any] | None:
    return _idem_load(ctx, namespace, request_id)


def _save_request(ctx: Any, namespace: str, request_id: str, payload: Mapping[str, Any] | MutableMapping[str, Any]) -> Mapping[str, Any]:
    return _idem_save(ctx, namespace, request_id, payload)


def _result(ctx: Any, request_id: str, status: str, *, dry_run: bool, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    trace_id = getattr(ctx, "trace_id", None)
    return result_envelope(
        request_id=request_id,
        status=status,
        dry_run=dry_run,
        result=dict(result) if result is not None else None,
        trace_id=str(trace_id) if trace_id else None,
    )


def _meta_to_dict(meta: Any) -> dict[str, Any] | None:
    if meta is None:
        return None
    if is_dataclass(meta):
        data = asdict(meta)
    elif hasattr(meta, "_asdict"):
        data = meta._asdict()  # type: ignore[attr-defined]
    else:
        data = {
            "id": getattr(meta, "id", None),
            "name": getattr(meta, "name", None),
            "version": getattr(meta, "version", None),
            "path": getattr(meta, "path", None),
        }
    if isinstance(data.get("id"), (str, bytes)):
        data["id"] = str(data["id"])
    elif hasattr(data.get("id"), "value"):
        data["id"] = str(getattr(data["id"], "value"))
    if "path" in data and isinstance(data["path"], (Path, bytes)):
        data["path"] = str(data["path"])
    return data


def _wrap_quota(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except QuotaExceeded:
            raise
        except Exception as exc:  # pragma: no cover - backend specific
            message = str(exc).lower()
            if "quota" in message:
                raise QuotaExceeded(detail=str(exc)) from exc
            raise

    return wrapper
