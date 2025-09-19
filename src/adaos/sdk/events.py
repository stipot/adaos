from __future__ import annotations

from typing import Callable

from adaos.sdk._ctx import require_ctx
from adaos.sdk.decorators import _SUBSCRIPTIONS


def publish(topic: str, payload: dict, **meta: object) -> None:
    ctx = require_ctx("SDK events used before runtime initialization.")
    message = dict(payload)
    if meta:
        message |= dict(meta)
    ctx.bus.publish(topic, message)


def subscribe(topic: str) -> Callable[[Callable], Callable]:
    def decorator(handler: Callable):
        _SUBSCRIPTIONS.append((topic, handler))
        return handler

    return decorator
