"""High-level SDK helpers for publishing events."""

from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace
from typing import Any, Mapping

from ._runtime import require_ctx
from .errors import BusNotAvailable


def _ensure_bus(ctx: Any):
    bus = getattr(ctx, "bus", None)
    publish = getattr(bus, "publish", None)
    if publish is None:
        raise BusNotAvailable("Event bus is not available in current context")
    return publish


def publish(topic: str, payload: Mapping[str, Any] | None = None, **meta: Any) -> Any:
    """Publish an event via the runtime event bus."""

    ctx = require_ctx("sdk.events.publish")
    publish_fn = _ensure_bus(ctx)

    data = dict(payload or {})
    extra_meta = {k: v for k, v in meta.items() if k not in {"source", "ts"}}
    if extra_meta:
        meta_container = dict(data.get("_meta", {}))
        meta_container.update(extra_meta)
        data["_meta"] = meta_container

    source = str(meta.get("source", ""))
    ts = float(meta.get("ts", time.time()))

    try:
        sig = inspect.signature(publish_fn)
    except (TypeError, ValueError):  # pragma: no cover - exotic backends
        sig = None

    kwargs: dict[str, Any] = {}
    if sig:
        if "source" in sig.parameters:
            kwargs["source"] = source
        if "ts" in sig.parameters:
            kwargs["ts"] = ts

    try:
        result = publish_fn(topic, data, **kwargs)
    except TypeError:
        try:
            from adaos.domain.types import Event as DomainEvent

            event = DomainEvent(type=topic, payload=data, source=source, ts=ts)
        except Exception:  # pragma: no cover - fallback path
            event = SimpleNamespace(type=topic, payload=data, source=source, ts=ts)
        result = publish_fn(event)

    if inspect.isawaitable(result):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(result)
        return loop.create_task(result)
    return result


__all__ = ["publish"]
