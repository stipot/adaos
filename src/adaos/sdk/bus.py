# src\adaos\sdk\bus.py
from __future__ import annotations
from typing import Callable, Awaitable, Any
from adaos.services.agent_context import get_ctx


async def emit(topic: str, payload: dict, **kw: Any):
    """Публикация события через текущий EventBus из AgentContext."""
    return await get_ctx().bus.publish(topic, payload, **kw)


async def on(topic: str, handler: Callable[[dict], Awaitable[Any]]):
    """Подписка на события через текущий EventBus из AgentContext."""
    return await get_ctx().bus.subscribe(topic, handler)
