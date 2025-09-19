from __future__ import annotations
from typing import Callable, Dict, List, Tuple, Optional
import inspect
from adaos.sdk.data.bus import on

# публичные реестры (стабильные имена)
subscriptions: List[Tuple[str, Callable]] = []
tools_registry: Dict[str, Dict[str, Callable]] = {}
tools_meta: Dict[str, dict] = {}  # по qualname функции
event_payloads: Dict[str, dict] = {}  # topic -> schema
emits_map: Dict[str, set[str]] = {}  # qualname -> {topics}
_registered: bool = False  # внутренняя защита от двойной регистрации
_SUBSCRIPTIONS = subscriptions
_TOOLS = tools_registry


def subscribe(topic: str):
    """Регистрирует обработчик; фактическая подписка делает register_subscriptions()."""

    def deco(fn: Callable):
        subscriptions.append((topic, fn))
        return fn

    return deco


async def register_subscriptions():
    """Фактическая подписка всех зарегистрированных обработчиков (однократно)."""
    global _registered
    if _registered:
        return
    for topic, fn in subscriptions:
        if inspect.iscoroutinefunction(fn):

            async def _wrap(evt, _fn=fn):
                return await _fn(evt)

        else:

            async def _wrap(evt, _fn=fn):
                _fn(evt)

        await on(topic, _wrap)
    _registered = True


def tool(
    public_name: Optional[str] = None,
    *,
    summary: str = "",
    stability: str = "experimental",
    idempotent: Optional[bool] = None,
    side_effects: Optional[str] = None,
    examples: Optional[list[str]] = None,
    since: Optional[str] = None,
    version: Optional[str] = None,
    input_schema: Optional[dict] = None,
    output_schema: Optional[dict] = None,
):
    """Маркер инструмента с публичным именем и метаданными."""

    def deco(fn: Callable):
        name = public_name or fn.__name__
        mod = fn.__module__
        tools_registry.setdefault(mod, {})[name] = fn
        qn = f"{mod}.{fn.__name__}"
        tools_meta[qn] = {
            "public_name": name,
            "summary": summary,
            "stability": stability,
            "idempotent": idempotent,
            "side_effects": side_effects,
            "examples": (examples or []),
            "since": since,
            "version": version,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }
        return fn

    return deco


def event_payload(topic: str, schema: dict):
    """Опишите форму payload для события (для экспорта)."""
    event_payloads[topic] = schema
    return lambda fn: fn


def emits(*topics: str):
    """Пометьте функцию как публикующую события (для карты событий)."""

    def _wrap(fn: Callable):
        qn = f"{fn.__module__}.{fn.__name__}"
        emits_map.setdefault(qn, set()).update(topics)
        return fn

    return _wrap


def resolve_tool(module_name: str, public_name: str) -> Callable | None:
    """Вернуть callable по публичному имени инструмента из модуля."""
    return (tools_registry.get(module_name) or {}).get(public_name)
