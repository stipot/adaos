from __future__ import annotations
from typing import Callable, Awaitable, Any
import inspect, time
from types import SimpleNamespace
from adaos.services.agent_context import get_ctx

from .errors import BusNotAvailable


def _bus():
    ctx = get_ctx()
    if not getattr(ctx, "bus", None):
        raise BusNotAvailable("AgentContext.bus is not initialized")
    return ctx.bus


def _positional_params(fn) -> int:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 0
    params = list(sig.parameters.values())
    # считаем позиционные кроме self
    return sum(1 for i, p in enumerate(params) if i > 0 and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


def get_meta(payload: dict) -> dict:
    return payload.get("_meta", {}) if isinstance(payload, dict) else {}


async def emit(topic: str, payload: dict, **kw: Any):
    """
    Универсальная публикация события.
    - Если bus.publish ожидает (topic, payload[, **kw]) — используем этот путь.
    - Если ожидает единый объект — создаём adaos.domain.Event(type,payload,source,ts).
      Все прочие kwargs складываем в payload["_meta"].
    """
    bus = _bus()
    publish = getattr(bus, "publish")

    # разберём известные поля Event и «прочие» в meta
    source = kw.pop("source", "")
    ts = float(kw.pop("ts", time.time()))
    extra_meta = dict(kw)  # actor, correlation_id, etc.

    # аккуратная копия payload + _meta
    pp = dict(payload) if isinstance(payload, dict) else {"value": payload}
    if extra_meta:
        pp["_meta"] = {**pp.get("_meta", {}), **extra_meta}

    # ветка 1: publish(self, topic, payload[, **kw])
    npos = _positional_params(publish)
    try:
        sig = inspect.signature(publish)
    except (TypeError, ValueError):
        sig = None

    if npos >= 2:
        if sig and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            res = publish(topic, pp, source=source, ts=ts, **extra_meta)
        else:
            # передадим только те именованные, которые явно поддерживает реализация
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

    # ветка 2: publish(self, event)
    # попробуем создать настоящий Event, иначе — неймспейс с теми же атрибутами
    try:
        from adaos.domain.types import Event as DomainEvent

        event = DomainEvent(type=topic, payload=pp, source=source, ts=ts)
    except Exception:
        event = SimpleNamespace(type=topic, payload=pp, source=source, ts=ts)

    try:
        res = publish(event)
    except TypeError:
        # запасной вариант: (topic, payload) без именованных
        res = publish(topic, pp)

    if inspect.iscoroutine(res):
        return await res
    return res


async def on(topic: str, handler: Callable[[dict], Awaitable[Any]]):
    """
    Универсальная подписка:
      - оборачиваем хендлер так, чтобы он всегда получал payload dict
        (из Event.payload или из event["payload"]).
      - допускаем синхронный subscribe().
    """
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

    # подписи subscribe различаются; не всегда это корутина
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
