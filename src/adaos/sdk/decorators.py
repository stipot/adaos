from typing import Callable, List, Tuple, Dict
import inspect
from adaos.sdk.bus import on

# Глобальный реестр подписок, наполняется при импорте модулей навыков
_SUBSCRIPTIONS: List[Tuple[str, Callable]] = []
_REGISTERED: bool = False
# глобальный реестр инструментов: {module_name: {public_name: callable}}
_TOOLS: Dict[str, Dict[str, Callable]] = {}


def subscribe(topic: str):
    """Регистрирует обработчик в реестре; фактическая подписка выполняется в register_subscriptions()."""

    def deco(fn: Callable):
        _SUBSCRIPTIONS.append((topic, fn))
        return fn

    return deco


async def register_subscriptions():
    """
    Выполнить фактическую подписку всех зарегистрированных обработчиков.
    Вызывайте один раз на старте процесса, когда event loop уже запущен.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    for topic, fn in _SUBSCRIPTIONS:
        if inspect.iscoroutinefunction(fn):

            async def _wrap(evt, _fn=fn):
                return await _fn(evt)

        else:

            async def _wrap(evt, _fn=fn):
                _fn(evt)

        await on(topic, _wrap)
    _REGISTERED = True


def tool(public_name: str | None = None):
    """
    Маркер инструмента с публичным именем.
    'public_name' — то имя, по которому инструмент будет вызываться извне (через ОС).
    Если не задано — берём fn.__name__.
    """

    def deco(fn: Callable):
        name = public_name or fn.__name__
        setattr(fn, "__adaos_tool__", True)
        setattr(fn, "__adaos_tool_name__", name)
        # сохраним в глобальный реестр
        mod = fn.__module__
        _TOOLS.setdefault(mod, {})[name] = fn
        return fn

    return deco


def resolve_tool(module_name: str, public_name: str) -> Callable | None:
    """Вернуть callable по публичному имени инструмента из модуля."""
    mod_tools = _TOOLS.get(module_name) or {}
    return mod_tools.get(public_name)
