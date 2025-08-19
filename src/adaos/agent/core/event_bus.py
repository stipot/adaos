import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, DefaultDict
from collections import defaultdict


@dataclass
class Event:
    id: str
    topic: str
    ts: float
    source: str
    actor: str
    payload: Dict[str, Any]
    trace_id: str | None = None


class EventBus:
    def __init__(self) -> None:
        self._subs: DefaultDict[str, List[Callable[[Event], Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, payload: Dict[str, Any], *, source="core", actor="system", trace_id=None) -> Event:
        evt = Event(
            id=str(uuid.uuid4()),
            topic=topic,
            ts=asyncio.get_event_loop().time(),
            source=source,
            actor=actor,
            payload=payload,
            trace_id=trace_id,
        )
        async with self._lock:
            handlers = list(self._subs.get(topic, [])) + list(self._subs.get("*", []))
        for h in handlers:
            asyncio.create_task(self._safe_call(h, evt))
        return evt

    async def _safe_call(self, handler: Callable[[Event], Any], evt: Event):
        try:
            res = handler(evt)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            # TODO: заменить на нормальный логгер/observability
            print(f"[bus] handler error on {evt.topic}: {e}")

    async def subscribe(self, topic: str, handler: Callable[[Event], Any]):
        async with self._lock:
            self._subs[topic].append(handler)


BUS = EventBus()
