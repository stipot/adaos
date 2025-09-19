"""Async event-bus helpers that stay import-safe until runtime."""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from adaos.sdk.core._ctx import require_ctx

__all__ = ["emit", "on", "get_meta", "BusNotAvailable"]


class BusNotAvailable(RuntimeError):
    """Raised when the runtime context does not provide an event bus."""


def _bus() -> Any:
    ctx = require_ctx("sdk.data.bus")
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise BusNotAvailable("AgentContext.bus is not initialized")
    return bus


def _positional_params(fn: Callable[..., Any]) -> int:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 0
    params = list(sig.parameters.values())
    return sum(1 for i, p in enumerate(params) if i > 0 and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


def get_meta(payload: dict) -> dict:
    return payload.get("_meta", {}) if isinstance(payload, dict) else {}


async def emit(topic: str, payload: dict, **kw: Any):
    bus = _bus()
    publish = getattr(bus, "publish")

    source = kw.pop("source", "")
    ts = float(kw.pop("ts", time.time()))
    extra_meta = dict(kw)

    pp = dict(payload) if isinstance(payload, dict) else {"value": payload}
    if extra_meta:
        pp["_meta"] = {**pp.get("_meta", {}), **extra_meta}

    npos = _positional_params(publish)
    try:
        sig = inspect.signature(publish)
    except (TypeError, ValueError):
        sig = None

    if npos >= 2:
        if sig and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            res = publish(topic, pp, source=source, ts=ts, **extra_meta)
        else:
            allowed = {}
            if sig:
                for name in ("source", "ts"):
                    if name in sig.parameters:
                        allowed[name] = locals()[name]
            try:
                res = publish(topic, pp, **allowed)
            except TypeError:
                res = publish(topic, pp)
        if inspect.iscoroutine(res):
            return await res
        return res

    try:
        from adaos.domain.types import Event as DomainEvent

        event = DomainEvent(type=topic, payload=pp, source=source, ts=ts)
    except Exception:
        event = SimpleNamespace(type=topic, payload=pp, source=source, ts=ts)

    try:
        res = publish(event)
    except TypeError:
        res = publish(topic, pp)

    if inspect.iscoroutine(res):
        return await res
    return res


async def on(topic: str, handler: Callable[[dict], Awaitable[Any]]):
    bus = _bus()
    subscribe = getattr(bus, "subscribe")

    async def _adapt(ev):
        if hasattr(ev, "payload"):
            data = getattr(ev, "payload")
        elif isinstance(ev, dict) and "payload" in ev and "type" in ev:
            data = ev.get("payload")
        else:
            data = ev
        if inspect.iscoroutinefunction(handler):
            return await handler(data)
        return handler(data)

    try:
        sig = inspect.signature(subscribe)
    except (TypeError, ValueError):
        sig = None

    if sig and len(sig.parameters) >= 3:
        res = subscribe(topic, _adapt)
    else:
        try:
            res = subscribe(_adapt)
        except TypeError:
            try:
                res = subscribe(topic=topic, handler=_adapt)
            except TypeError:
                res = subscribe(topic, _adapt)

    if inspect.iscoroutine(res):
        return await res
    return res
