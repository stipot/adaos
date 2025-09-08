from __future__ import annotations
import asyncio
import time
from collections import defaultdict
from threading import RLock
from typing import Callable, Awaitable, Any, DefaultDict, List

from adaos.domain import Event
from adaos.ports import EventBus

Handler = Callable[[Event], Any] | Callable[[Event], Awaitable[Any]]


class LocalEventBus(EventBus):
    """
    Простая синхронно-асинхронная шина по префиксам типов событий.
    - subscribe(prefix, handler)
    - publish(event)
    Особенности:
      * prefix = "" или "*" — подписка на всё.
      * Асинхендлеры исполняются через running loop (или блокирующе, если лупа нет).
    """

    def __init__(self) -> None:
        self._subs: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._lock = RLock()

    def subscribe(self, type_prefix: str, handler: Handler) -> None:
        with self._lock:
            self._subs[type_prefix].append(handler)

    def publish(self, event: Event) -> None:
        with self._lock:
            pairs = [(p, hs[:]) for p, hs in self._subs.items()]
        for prefix, handlers in pairs:
            if prefix == "*" or prefix == "" or event.type.startswith(prefix):
                for h in handlers:
                    res = h(event)
                    if asyncio.iscoroutine(res):
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            asyncio.run(res)  # нет активного лупа — выполним синхронно
                        else:
                            loop.create_task(res)


def emit(bus: EventBus, type_: str, payload: dict, source: str) -> None:
    bus.publish(Event(type=type_, payload=payload, source=source, ts=time.time()))
