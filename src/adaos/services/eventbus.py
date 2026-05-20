from __future__ import annotations
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from threading import RLock
from typing import Callable, Awaitable, Any, DefaultDict, List

from adaos.domain import Event
from adaos.ports import EventBus


Handler = Callable[[Event], Any] | Callable[[Event], Awaitable[Any]]

_log = logging.getLogger("adaos.eventbus")
_WEBIO_STREAM_CONTROL_EVENTS = {
    "webio.stream.snapshot.requested",
    "webio.stream.subscription.changed",
}
_BROWSER_SESSION_CHANGED_EVENT = "browser.session.changed"


def _trace_subscribe_enabled() -> bool:
    raw = str(os.getenv("ADAOS_EVENTBUS_TRACE_SUBSCRIBE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _handler_label(handler: Handler) -> str:
    """
    Build a human-readable label for a handler, including optional skill/topic
    hints injected by the SDK decorators.
    """
    mod = getattr(handler, "__module__", None) or "<?>"
    name = getattr(handler, "__name__", None) or repr(handler)
    skill = getattr(handler, "_adaos_skill", None)
    topic = getattr(handler, "_adaos_topic", None)
    adapted = getattr(handler, "_adaos_handler", None)
    parts = [f"{mod}.{name}"]
    if adapted:
        parts.append(f"adapted={adapted}")
    if skill:
        parts.append(f"skill={skill}")
    if topic:
        parts.append(f"topic={topic}")
    return " ".join(parts)


def _slow_handler_threshold_s(kind: str, default: float) -> float:
    env_name = f"ADAOS_EVENTBUS_SLOW_{kind.upper()}_WARN_S"
    try:
        value = float(os.getenv(env_name, str(default)) or str(default))
    except Exception:
        value = default
    return max(0.0, value)


def _pending_backlog_warn_threshold() -> int:
    try:
        value = int(str(os.getenv("ADAOS_EVENTBUS_PENDING_WARN_THRESHOLD", "64") or "64").strip())
    except Exception:
        value = 64
    return max(1, min(value, 100000))


def _pending_backlog_warn_interval_s() -> float:
    try:
        value = float(str(os.getenv("ADAOS_EVENTBUS_PENDING_WARN_INTERVAL_S", "5.0") or "5.0").strip())
    except Exception:
        value = 5.0
    return max(0.0, min(value, 300.0))


def _bounded_event_topics() -> tuple[str, ...]:
    raw = str(
        os.getenv(
            "ADAOS_EVENTBUS_BOUNDED_TOPICS",
            "webio.stream.snapshot.requested,webio.stream.subscription.changed,"
            "subnet.member.snapshot.changed,browser.session.changed",
        )
        or ""
    ).strip()
    items = [str(item or "").strip() for item in raw.split(",") if str(item or "").strip()]
    return tuple(items)


def _bounded_supersede_by_handler_topics() -> tuple[str, ...]:
    raw = str(
        os.getenv(
            "ADAOS_EVENTBUS_SUPERSEDE_BY_HANDLER_TOPICS",
            "webio.stream.snapshot.requested,webio.stream.subscription.changed,browser.session.changed",
        )
        or ""
    ).strip()
    items = [str(item or "").strip() for item in raw.split(",") if str(item or "").strip()]
    return tuple(items)


def _bounded_event_concurrency() -> int:
    try:
        value = int(str(os.getenv("ADAOS_EVENTBUS_BOUNDED_CONCURRENCY", "1") or "1").strip())
    except Exception:
        value = 1
    return max(1, min(value, 32))


def _bounded_event_queue_limit() -> int:
    try:
        value = int(str(os.getenv("ADAOS_EVENTBUS_BOUNDED_QUEUE_LIMIT", "128") or "128").strip())
    except Exception:
        value = 128
    return max(1, min(value, 100000))


async def _run_coro_with_timing(coro: Awaitable[Any], handler: Handler, event: Event) -> None:
    """
    Wrapper for async handlers that records execution time and logs slow/crashing
    handlers for debugging high CPU usage in the hub.
    """
    started = time.perf_counter()
    try:
        await coro
    except Exception:  # pragma: no cover - defensive logging
        _log.warning(
            "event handler crashed handler=%s type=%s",
            _handler_label(handler),
            getattr(event, "type", "<unknown>"),
            exc_info=True,
        )
    else:
        duration = time.perf_counter() - started
        if duration >= _slow_handler_threshold_s("async", 0.25):
            _log.warning(
                "slow async event handler handler=%s type=%s duration=%.3fs",
                _handler_label(handler),
                getattr(event, "type", "<unknown>"),
                duration,
            )


class LocalEventBus(EventBus):
    """
    Локальная неблокирующая шина событий для одного процесса.
      - subscribe(prefix, handler)
      - publish(event)

    Особенности:
      * prefix = "" или "*" — подписка на все события.
      * вызовы обработчиков делаются в текущем или уже запущенном event loop.

    Дополнительно эта реализация логирует медленные/падающие обработчики,
    чтобы упростить отладку случаев, когда какой‑то skill «крутит» CPU.
    """

    def __init__(self) -> None:
        self._subs: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._lock = RLock()
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._pending_task_meta: dict[asyncio.Task[Any], tuple[str, str, float]] = {}
        self._pending_by_type: DefaultDict[str, int] = defaultdict(int)
        self._pending_by_handler: DefaultDict[str, int] = defaultdict(int)
        self._pending_peak = 0
        self._last_pending_warn_at = 0.0
        self._incoming_total = 0
        self._incoming_by_type: DefaultDict[str, int] = defaultdict(int)
        self._bounded_topics = _bounded_event_topics()
        self._bounded_supersede_by_handler_topics = _bounded_supersede_by_handler_topics()
        self._bounded_concurrency = _bounded_event_concurrency()
        self._bounded_queue_limit = _bounded_event_queue_limit()
        self._bounded_queues: DefaultDict[str, deque[tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None]]] = defaultdict(deque)
        self._bounded_worker_tasks: set[asyncio.Task[Any]] = set()
        self._bounded_active_workers: DefaultDict[str, int] = defaultdict(int)
        self._bounded_peak_workers: DefaultDict[str, int] = defaultdict(int)
        self._bounded_queued_by_type: DefaultDict[str, int] = defaultdict(int)
        self._bounded_queued_by_handler: DefaultDict[str, int] = defaultdict(int)
        self._bounded_dropped_by_topic: DefaultDict[str, int] = defaultdict(int)
        self._bounded_dropped_by_type: DefaultDict[str, int] = defaultdict(int)
        self._bounded_superseded_by_topic: DefaultDict[str, int] = defaultdict(int)
        self._bounded_superseded_by_type: DefaultDict[str, int] = defaultdict(int)
        self._bounded_queue_peak = 0
        self._webio_stream_control_stats: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def _bounded_topic_key(self, event_type: str) -> str | None:
        for spec in self._bounded_topics:
            if spec.endswith("*"):
                prefix = spec[:-1]
                if prefix and event_type.startswith(prefix):
                    return spec
            elif event_type == spec:
                return spec
        return None

    def _bounded_supersede_by_handler_enabled(self, event_type: str) -> bool:
        for spec in self._bounded_supersede_by_handler_topics:
            if spec.endswith("*"):
                prefix = spec[:-1]
                if prefix and event_type.startswith(prefix):
                    return True
            elif event_type == spec:
                return True
        return False

    def _event_field(self, event: Any, *names: str) -> Any:
        payload = getattr(event, "payload", None)
        for candidate in (payload, event):
            if candidate is None:
                continue
            getter = getattr(candidate, "get", None)
            if callable(getter):
                for name in names:
                    try:
                        value = getter(name)
                    except Exception:
                        value = None
                    if value not in (None, ""):
                        return value
            for name in names:
                try:
                    value = getattr(candidate, name)
                except Exception:
                    value = None
                if value not in (None, ""):
                    return value
        return None

    def _bounded_supersede_key(self, event_type: str, event: Event) -> tuple[Any, ...] | None:
        if event_type in _WEBIO_STREAM_CONTROL_EVENTS:
            return self._webio_stream_control_key(event_type, event)
        if event_type == "subnet.member.snapshot.changed":
            node_id = str(self._event_field(event, "target_node_id", "node_id", "member_id") or "").strip()
            webspace_id = str(self._event_field(event, "webspace_id") or "").strip()
            return (event_type, node_id, webspace_id)
        if event_type == _BROWSER_SESSION_CHANGED_EVENT:
            webspace_id = str(self._event_field(event, "webspace_id", "workspace_id") or "default").strip() or "default"
            device_id = str(
                self._event_field(event, "device_id", "dev_id", "browser_key_id", "session_id") or ""
            ).strip()
            return (event_type, webspace_id, device_id)
        return None

    def _webio_stream_control_key(
        self,
        event_type: str,
        event: Event,
    ) -> tuple[str, str, str, str, str] | None:
        if event_type not in _WEBIO_STREAM_CONTROL_EVENTS:
            return None
        webspace_id = str(self._event_field(event, "webspace_id") or "default").strip() or "default"
        target_node_id = str(self._event_field(event, "target_node_id", "node_id") or "").strip()
        stream_id = str(self._event_field(event, "stream_id", "receiver", "id") or "").strip()
        source = str(self._event_field(event, "source") or getattr(event, "source", "") or "").strip()
        return (event_type, webspace_id, target_node_id, stream_id, source)

    def _prune_webio_stream_control_stats_locked(self, *, limit: int = 500) -> None:
        if len(self._webio_stream_control_stats) <= limit:
            return
        stale = sorted(
            self._webio_stream_control_stats.items(),
            key=lambda item: float(item[1].get("last_at") or 0.0),
        )
        for key, _item in stale[: max(0, len(stale) - limit)]:
            self._webio_stream_control_stats.pop(key, None)

    def _record_webio_stream_control_locked(
        self,
        event_type: str,
        event: Event,
        field: str,
        *,
        handler_name: str | None = None,
    ) -> None:
        key = self._webio_stream_control_key(event_type, event)
        if key is None:
            return
        current = dict(self._webio_stream_control_stats.get(key) or {})
        current["event_type"] = key[0]
        current["webspace_id"] = key[1]
        current["target_node_id"] = key[2] or None
        current["receiver"] = key[3] or None
        current["source"] = key[4] or None
        current["last_action"] = str(self._event_field(event, "action", "change") or "").strip() or None
        if handler_name:
            current["last_handler"] = handler_name
        current["last_at"] = time.time()
        counter = f"{field}_total"
        current[counter] = int(current.get(counter) or 0) + 1
        self._webio_stream_control_stats[key] = current
        self._prune_webio_stream_control_stats_locked()

    def _bounded_remove_superseded_locked(
        self,
        topic_key: str,
        supersede_key: tuple[Any, ...] | None,
    ) -> Awaitable[Any] | None:
        if not supersede_key:
            return None
        queue = self._bounded_queues.get(topic_key)
        if not queue:
            return None
        kept: deque[tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None]] = deque()
        removed: tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None] | None = None
        while queue:
            item = queue.popleft()
            if removed is None and item[5] == supersede_key:
                removed = item
                continue
            kept.append(item)
        self._bounded_queues[topic_key] = kept
        if removed is None:
            return None
        removed_type = self._bounded_decrement_queued_locked(removed)
        self._bounded_superseded_by_topic[topic_key] += 1
        self._bounded_superseded_by_type[removed_type] += 1
        self._record_webio_stream_control_locked(
            removed_type,
            removed[2],
            "superseded",
            handler_name=removed[4],
        )
        return removed[0]

    def _bounded_decrement_queued_locked(
        self,
        item: tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None],
    ) -> str:
        removed_type = str(item[3] or "")
        self._bounded_queued_by_type[removed_type] = max(0, int(self._bounded_queued_by_type.get(removed_type, 0)) - 1)
        if self._bounded_queued_by_type[removed_type] <= 0:
            self._bounded_queued_by_type.pop(removed_type, None)
        handler_name = str(item[4] or "")
        self._bounded_queued_by_handler[handler_name] = max(0, int(self._bounded_queued_by_handler.get(handler_name, 0)) - 1)
        if self._bounded_queued_by_handler[handler_name] <= 0:
            self._bounded_queued_by_handler.pop(handler_name, None)
        return removed_type

    def _bounded_remove_handler_superseded_locked(
        self,
        topic_key: str,
        event_type: str,
        handler_name: str,
        supersede_key: tuple[Any, ...] | None,
    ) -> list[Awaitable[Any]]:
        if not handler_name or not self._bounded_supersede_by_handler_enabled(event_type):
            return []
        queue = self._bounded_queues.get(topic_key)
        if not queue:
            return []
        kept: deque[tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None]] = deque()
        removed: list[tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None]] = []
        while queue:
            item = queue.popleft()
            if item[3] == event_type and item[4] == handler_name:
                removed.append(item)
                continue
            kept.append(item)
        self._bounded_queues[topic_key] = kept
        for item in removed:
            removed_type = self._bounded_decrement_queued_locked(item)
            self._bounded_superseded_by_topic[topic_key] += 1
            self._bounded_superseded_by_type[removed_type] += 1
            self._record_webio_stream_control_locked(
                removed_type,
                item[2],
                "superseded",
                handler_name=item[4],
            )
        return [item[0] for item in removed]

    def _pending_backlog_snapshot_locked(self) -> dict[str, Any]:
        now = time.monotonic()
        oldest_age_s = 0.0
        for pending_task, (_event_type, _handler_name, started) in self._pending_task_meta.items():
            if pending_task.done():
                continue
            oldest_age_s = max(oldest_age_s, max(0.0, now - float(started or now)))
        top_types = sorted(
            ((event_type, count) for event_type, count in self._pending_by_type.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_handlers = sorted(
            ((handler_name, count) for handler_name, count in self._pending_by_handler.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        bounded_queue_total = sum(len(queue) for queue in self._bounded_queues.values())
        bounded_active_workers = sum(int(count or 0) for count in self._bounded_active_workers.values())
        top_queued_types = sorted(
            ((event_type, count) for event_type, count in self._bounded_queued_by_type.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_queued_handlers = sorted(
            ((handler_name, count) for handler_name, count in self._bounded_queued_by_handler.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_bounded_topics = sorted(
            ((topic, len(queue)) for topic, queue in self._bounded_queues.items() if len(queue) > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_bounded_drops = sorted(
            ((topic, count) for topic, count in self._bounded_dropped_by_topic.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_bounded_superseded_topics = sorted(
            ((topic, count) for topic, count in self._bounded_superseded_by_topic.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_bounded_superseded_types = sorted(
            ((event_type, count) for event_type, count in self._bounded_superseded_by_type.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_incoming_types = sorted(
            ((event_type, count) for event_type, count in self._incoming_by_type.items() if count > 0),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        top_webio_stream_controls = sorted(
            (dict(item) for item in self._webio_stream_control_stats.values()),
            key=lambda item: (
                -int(item.get("superseded_total") or 0),
                -int(item.get("dropped_total") or 0),
                -int(item.get("incoming_total") or 0),
                str(item.get("event_type") or ""),
                str(item.get("receiver") or ""),
            ),
        )[:10]
        return {
            "pending_tasks": len(self._pending_tasks),
            "pending_peak": int(self._pending_peak),
            "oldest_age_s": float(oldest_age_s),
            "top_types": top_types,
            "top_handlers": top_handlers,
            "incoming_total": int(self._incoming_total),
            "top_incoming_types": top_incoming_types,
            "bounded_topics": list(self._bounded_topics),
            "bounded_concurrency": int(self._bounded_concurrency),
            "bounded_queue_limit": int(self._bounded_queue_limit),
            "bounded_queue_total": int(bounded_queue_total),
            "bounded_queue_peak": int(self._bounded_queue_peak),
            "bounded_active_workers": int(bounded_active_workers),
            "top_queued_types": top_queued_types,
            "top_queued_handlers": top_queued_handlers,
            "top_bounded_topics": top_bounded_topics,
            "top_bounded_drops": top_bounded_drops,
            "top_bounded_superseded_topics": top_bounded_superseded_topics,
            "top_bounded_superseded_types": top_bounded_superseded_types,
            "top_webio_stream_controls": top_webio_stream_controls,
        }

    def backlog_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._pending_backlog_snapshot_locked()

    def _maybe_log_pending_backlog_locked(self) -> None:
        pending_total = len(self._pending_tasks)
        bounded_queue_total = sum(len(queue) for queue in self._bounded_queues.values())
        if pending_total < _pending_backlog_warn_threshold() and bounded_queue_total < _pending_backlog_warn_threshold():
            return
        now = time.monotonic()
        if now - float(self._last_pending_warn_at or 0.0) < _pending_backlog_warn_interval_s():
            return
        snapshot = self._pending_backlog_snapshot_locked()
        self._last_pending_warn_at = now
        _log.warning(
            "eventbus backlog pending_tasks=%s peak_pending_tasks=%s oldest_age_s=%.3fs "
            "bounded_queue_total=%s bounded_queue_peak=%s bounded_active_workers=%s "
            "top_types=%s top_handlers=%s top_queued_types=%s top_queued_handlers=%s "
            "top_bounded_topics=%s top_bounded_drops=%s top_bounded_superseded_topics=%s",
            int(snapshot["pending_tasks"]),
            int(snapshot["pending_peak"]),
            float(snapshot["oldest_age_s"]),
            int(snapshot["bounded_queue_total"]),
            int(snapshot["bounded_queue_peak"]),
            int(snapshot["bounded_active_workers"]),
            snapshot["top_types"],
            snapshot["top_handlers"],
            snapshot["top_queued_types"],
            snapshot["top_queued_handlers"],
            snapshot["top_bounded_topics"],
            snapshot["top_bounded_drops"],
            snapshot["top_bounded_superseded_topics"],
        )

    def _track_task(self, task: asyncio.Task[Any], handler: Handler, event: Event) -> None:
        event_type = str(getattr(event, "type", "<unknown>") or "<unknown>")
        handler_name = _handler_label(handler)
        started = time.monotonic()
        with self._lock:
            self._pending_tasks.add(task)
            self._pending_task_meta[task] = (event_type, handler_name, started)
            self._pending_by_type[event_type] += 1
            self._pending_by_handler[handler_name] += 1
            if len(self._pending_tasks) > self._pending_peak:
                self._pending_peak = len(self._pending_tasks)
            self._maybe_log_pending_backlog_locked()

        def _cleanup(done: asyncio.Task[Any]) -> None:
            with self._lock:
                self._pending_tasks.discard(done)
                meta = self._pending_task_meta.pop(done, None)
                if meta is None:
                    return
                done_event_type, done_handler_name, _done_started = meta
                if done_event_type in self._pending_by_type:
                    self._pending_by_type[done_event_type] = max(0, int(self._pending_by_type[done_event_type]) - 1)
                    if self._pending_by_type[done_event_type] <= 0:
                        self._pending_by_type.pop(done_event_type, None)
                if done_handler_name in self._pending_by_handler:
                    self._pending_by_handler[done_handler_name] = max(0, int(self._pending_by_handler[done_handler_name]) - 1)
                    if self._pending_by_handler[done_handler_name] <= 0:
                        self._pending_by_handler.pop(done_handler_name, None)

        task.add_done_callback(_cleanup)

    async def _bounded_worker(self, topic_key: str) -> None:
        try:
            while True:
                queued: tuple[Awaitable[Any], Handler, Event, str, str, tuple[Any, ...] | None] | None = None
                with self._lock:
                    queue = self._bounded_queues.get(topic_key)
                    if queue:
                        queued = queue.popleft()
                    if queued is not None:
                        _coro, _handler, _event, event_type, handler_name, _supersede_key = queued
                        if event_type in self._bounded_queued_by_type:
                            self._bounded_queued_by_type[event_type] = max(0, int(self._bounded_queued_by_type[event_type]) - 1)
                            if self._bounded_queued_by_type[event_type] <= 0:
                                self._bounded_queued_by_type.pop(event_type, None)
                        if handler_name in self._bounded_queued_by_handler:
                            self._bounded_queued_by_handler[handler_name] = max(0, int(self._bounded_queued_by_handler[handler_name]) - 1)
                            if self._bounded_queued_by_handler[handler_name] <= 0:
                                self._bounded_queued_by_handler.pop(handler_name, None)
                    if queued is None:
                        break
                coro, handler, event, _event_type, _handler_name, _supersede_key = queued
                await _run_coro_with_timing(coro, handler, event)
        finally:
            with self._lock:
                task = asyncio.current_task()
                if task is not None:
                    self._bounded_worker_tasks.discard(task)
                if topic_key in self._bounded_active_workers:
                    self._bounded_active_workers[topic_key] = max(0, int(self._bounded_active_workers[topic_key]) - 1)
                    if self._bounded_active_workers[topic_key] <= 0:
                        self._bounded_active_workers.pop(topic_key, None)
                queue = self._bounded_queues.get(topic_key)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if queue and loop is not None:
                    self._ensure_bounded_workers_locked(loop, topic_key)

    def _ensure_bounded_workers_locked(self, loop: asyncio.AbstractEventLoop, topic_key: str) -> None:
        queue = self._bounded_queues.get(topic_key)
        if not queue:
            return
        active = int(self._bounded_active_workers.get(topic_key) or 0)
        target = min(self._bounded_concurrency, len(queue))
        while active < target:
            task = loop.create_task(self._bounded_worker(topic_key), name=f"eventbus-bounded:{topic_key}")
            self._bounded_worker_tasks.add(task)
            active += 1
            self._bounded_active_workers[topic_key] = active
            if active > int(self._bounded_peak_workers.get(topic_key) or 0):
                self._bounded_peak_workers[topic_key] = active

    async def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """
        Wait until all async handlers spawned by ``publish()`` finish.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            with self._lock:
                pending = [task for task in self._pending_tasks if not task.done()]
                worker_pending = [task for task in self._bounded_worker_tasks if not task.done()]
                bounded_queued = sum(len(queue) for queue in self._bounded_queues.values())
            if not pending and not worker_pending and bounded_queued <= 0:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            wait_on = pending + worker_pending
            if wait_on:
                await asyncio.wait(wait_on, timeout=min(0.1, remaining), return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(min(0.1, remaining))

    def subscribe(self, type_prefix: str, handler: Handler) -> None:
        with self._lock:
            self._subs[type_prefix].append(handler)
        if _trace_subscribe_enabled():
            _log.debug("bus.subscribe prefix=%r handler=%s", type_prefix, _handler_label(handler))

    def publish(self, event: Event) -> None:
        event_type = str(getattr(event, "type", "<unknown>") or "<unknown>")
        with self._lock:
            self._incoming_total += 1
            self._incoming_by_type[event_type] += 1
            self._record_webio_stream_control_locked(event_type, event, "incoming")
            pairs = [(p, hs[:]) for p, hs in self._subs.items()]

        if _log.isEnabledFor(logging.DEBUG):
            total_handlers = sum(
                len(hs) for p, hs in pairs if p == "" or p == "*" or event.type.startswith(p)
            )
            _log.debug(
                "bus.publish type=%s source=%s handlers=%d",
                getattr(event, "type", "<unknown>"),
                getattr(event, "source", "<unknown>"),
                total_handlers,
            )

        for prefix, handlers in pairs:
            if prefix != "*" and prefix != "" and not event.type.startswith(prefix):
                continue
            for h in handlers:
                started = time.perf_counter()
                try:
                    res = h(event)
                except Exception:  # pragma: no cover - defensive logging
                    _log.warning(
                        "event handler crashed handler=%s type=%s",
                        _handler_label(h),
                        getattr(event, "type", "<unknown>"),
                        exc_info=True,
                    )
                    continue

                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        # Если нет текущего цикла, fallback на asyncio.run (CLI/скрипты).
                        asyncio.run(res)
                    else:
                        topic_key = self._bounded_topic_key(event_type)
                        if topic_key:
                            handler_name = _handler_label(h)
                            dropped_total = 0
                            superseded_coros: list[Awaitable[Any]] = []
                            supersede_key = self._bounded_supersede_key(event_type, event)
                            with self._lock:
                                queue = self._bounded_queues[topic_key]
                                superseded_coro = self._bounded_remove_superseded_locked(topic_key, supersede_key)
                                if superseded_coro is not None:
                                    superseded_coros.append(superseded_coro)
                                superseded_coros.extend(
                                    self._bounded_remove_handler_superseded_locked(
                                        topic_key,
                                        event_type,
                                        handler_name,
                                        supersede_key,
                                    )
                                )
                                queue = self._bounded_queues[topic_key]
                                if len(queue) >= self._bounded_queue_limit:
                                    self._bounded_dropped_by_topic[topic_key] += 1
                                    self._bounded_dropped_by_type[event_type] += 1
                                    self._record_webio_stream_control_locked(
                                        event_type,
                                        event,
                                        "dropped",
                                        handler_name=handler_name,
                                    )
                                    dropped_total = int(self._bounded_dropped_by_topic[topic_key] or 0)
                                else:
                                    queue.append((res, h, event, event_type, handler_name, supersede_key))
                                    self._bounded_queued_by_type[event_type] += 1
                                    self._bounded_queued_by_handler[handler_name] += 1
                                    self._record_webio_stream_control_locked(
                                        event_type,
                                        event,
                                        "queued",
                                        handler_name=handler_name,
                                    )
                                    bounded_total = sum(len(items) for items in self._bounded_queues.values())
                                    if bounded_total > self._bounded_queue_peak:
                                        self._bounded_queue_peak = bounded_total
                                    self._ensure_bounded_workers_locked(loop, topic_key)
                                    self._maybe_log_pending_backlog_locked()
                            for superseded_coro in superseded_coros:
                                try:
                                    superseded_coro.close()
                                except Exception:
                                    pass
                            if dropped_total > 0:
                                try:
                                    res.close()
                                except Exception:
                                    pass
                                if dropped_total == 1 or dropped_total % 25 == 0:
                                    _log.warning(
                                        "eventbus bounded queue dropped topic=%s type=%s handler=%s dropped_total=%s queue_limit=%s",
                                        topic_key,
                                        event_type,
                                        handler_name,
                                        dropped_total,
                                        int(self._bounded_queue_limit),
                                    )
                                continue
                        else:
                            task = loop.create_task(_run_coro_with_timing(res, h, event))
                            self._track_task(task, h, event)
                else:
                    duration = time.perf_counter() - started
                    if duration >= _slow_handler_threshold_s("sync", 0.1):
                        _log.warning(
                            "slow sync event handler handler=%s type=%s duration=%.3fs",
                            _handler_label(h),
                            getattr(event, "type", "<unknown>"),
                            duration,
                        )


def emit(bus: EventBus, type_: str, payload: dict, source: str) -> None:
    bus.publish(Event(type=type_, payload=payload, source=source, ts=time.time()))

