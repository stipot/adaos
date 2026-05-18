# src/adaos/services/observe.py
from __future__ import annotations
import asyncio, json, time, uuid, gzip, os, queue, threading, logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.settings import Settings
from adaos.sdk.data import bus as bus_module  # будем мягко оборачивать emit

_LOG = logging.getLogger("adaos.observe")

try:
    # get_ctx может быть недоступен/неинициализирован на момент импорта
    from adaos.services.agent_context import get_ctx  # type: ignore
except Exception:  # noqa: BLE001
    get_ctx = None  # type: ignore


def _resolve_base_dir() -> Path:
    if get_ctx:
        try:
            return Path(get_ctx().paths.base_dir())  # type: ignore[attr-defined]
        except Exception:
            pass
    # Fallback: derive from Settings to keep a single source of truth
    return Settings.from_sources().base_dir


BASE_DIR = _resolve_base_dir()
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_KEEP = 3

_LOG_TASK: Optional[asyncio.Task] = None
_QUEUE: "asyncio.Queue[Dict[str, Any]]" | None = None
_ORIG_EMIT = None
_LOG_FILE: Path | None = None
_LOCAL_LOG_QUEUE: "queue.Queue[Dict[str, Any] | None] | None" = None
_LOCAL_LOG_THREAD: threading.Thread | None = None
_LOCAL_LOG_DROPPED = 0


class EventBroadcaster:
    def __init__(self):
        self._subs: List[asyncio.Queue] = []
        self._dropped_subscribers = 0

    def _max_subscribers(self) -> int:
        try:
            raw = str(os.getenv("ADAOS_OBSERVE_BROADCAST_MAX_SUBSCRIBERS", "128") or "128").strip()
            return max(1, int(raw))
        except Exception:
            return 128

    @staticmethod
    def _drain(q: "asyncio.Queue[Dict[str, Any]]") -> None:
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return
            except Exception:
                return

    def _remove(self, q: "asyncio.Queue[Dict[str, Any]]") -> bool:
        try:
            self._subs.remove(q)
        except ValueError:
            return False
        except Exception:
            return False
        self._drain(q)
        return True

    async def publish(self, evt: Dict[str, Any]):
        for q in list(self._subs):
            try:
                if q.full():
                    _ = q.get_nowait()
                q.put_nowait(evt)
            except Exception:
                if self._remove(q):
                    self._dropped_subscribers += 1

    def subscribe(self, *, topic_prefix: str | None, node_id: str | None, since_ts: float | None) -> "asyncio.Queue[Dict[str, Any]]":
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        q._adaos_filter = {"topic_prefix": topic_prefix, "node_id": node_id, "since": since_ts}  # type: ignore[attr-defined]
        max_subscribers = self._max_subscribers()
        evicted = 0
        while len(self._subs) >= max_subscribers and self._subs:
            stale = self._subs.pop(0)
            self._drain(stale)
            evicted += 1
        if evicted:
            self._dropped_subscribers += evicted
            _LOG.warning(
                "observe broadcast subscriber cap reached; evicted=%s active=%s cap=%s",
                evicted,
                len(self._subs),
                max_subscribers,
            )
        self._subs.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Dict[str, Any]]") -> bool:
        return self._remove(q)

    def stats(self) -> Dict[str, Any]:
        return {
            "subscribers": len(self._subs),
            "dropped_subscribers": int(self._dropped_subscribers),
            "max_subscribers": self._max_subscribers(),
        }


BROADCAST = EventBroadcaster()


def _log_path() -> Path:
    p = BASE_DIR / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "events.log"


def _now_ts() -> float:
    return time.time()


def _ensure_trace(kwargs: Dict[str, Any]) -> str:
    trace = kwargs.get("trace_id") or kwargs.get("trace") or str(uuid.uuid4())
    kwargs["trace_id"] = trace
    return trace


def _serialize_event(topic: str, payload: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ctx = get_ctx()
        conf = getattr(ctx, "config", None) or load_config()
        try:
            if getattr(ctx, "config", None) is None:
                object.__setattr__(ctx, "config", conf)
        except Exception:
            pass
    except Exception:
        conf = load_config()
    return {
        "ts": _now_ts(),
        "topic": topic,
        "payload": payload,
        "trace": kwargs.get("trace_id"),
        "source": kwargs.get("source"),
        "actor": kwargs.get("actor"),
        "node_id": conf.node_id,
        "role": conf.role,
    }


def _rotate_if_needed(path: Path):
    try:
        if path.exists() and path.stat().st_size >= _MAX_BYTES:
            oldest = path.with_suffix(path.suffix + f".{_KEEP}.gz")
            oldest.unlink(missing_ok=True)
            for i in range(_KEEP - 1, 0, -1):
                src = path.with_suffix(path.suffix + f".{i}.gz")
                dst = path.with_suffix(path.suffix + f".{i + 1}.gz")
                if src.exists():
                    src.rename(dst)
            data = path.read_bytes()
            with gzip.open(path.with_suffix(path.suffix + ".1.gz"), "wb") as gz:
                gz.write(data)
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _write_local(e: Dict[str, Any]) -> None:
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = _log_path()
    _rotate_if_needed(_LOG_FILE)
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _local_log_queue_max() -> int:
    try:
        raw = str(os.getenv("ADAOS_OBSERVE_LOCAL_QUEUE_MAX", "5000") or "5000").strip()
        value = int(raw)
    except Exception:
        value = 5000
    return max(100, value)


def _local_log_enabled() -> bool:
    raw = os.getenv("ADAOS_OBSERVE_LOCAL_LOG")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def _local_log_worker(q: "queue.Queue[Dict[str, Any] | None]") -> None:
    while True:
        item = q.get()
        try:
            if item is None:
                return
            _write_local(item)
        except Exception:
            pass
        finally:
            try:
                q.task_done()
            except Exception:
                pass


def _ensure_local_log_worker() -> None:
    global _LOCAL_LOG_QUEUE, _LOCAL_LOG_THREAD
    if not _local_log_enabled():
        return
    if _LOCAL_LOG_QUEUE is None:
        _LOCAL_LOG_QUEUE = queue.Queue(maxsize=_local_log_queue_max())
    if _LOCAL_LOG_THREAD is not None and _LOCAL_LOG_THREAD.is_alive():
        return
    _LOCAL_LOG_THREAD = threading.Thread(
        target=_local_log_worker,
        args=(_LOCAL_LOG_QUEUE,),
        name="adaos-observe-local-log",
        daemon=True,
    )
    _LOCAL_LOG_THREAD.start()


def _enqueue_local(e: Dict[str, Any]) -> None:
    global _LOCAL_LOG_DROPPED
    if not _local_log_enabled():
        return
    try:
        _ensure_local_log_worker()
        q = _LOCAL_LOG_QUEUE
        if q is None:
            return
        q.put_nowait(e)
    except queue.Full:
        _LOCAL_LOG_DROPPED += 1
    except Exception:
        pass


def _stop_local_log_worker() -> None:
    global _LOCAL_LOG_QUEUE, _LOCAL_LOG_THREAD
    q = _LOCAL_LOG_QUEUE
    thread = _LOCAL_LOG_THREAD
    _LOCAL_LOG_QUEUE = None
    _LOCAL_LOG_THREAD = None
    if q is None:
        return
    try:
        q.put_nowait(None)
    except Exception:
        pass
    if thread is not None and thread.is_alive():
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass


async def _push_loop():
    """Фоновая отправка батчей логов на hub (для member)."""
    assert _QUEUE is not None
    try:
        ctx = get_ctx()
        conf = getattr(ctx, "config", None) or load_config()
        try:
            if getattr(ctx, "config", None) is None:
                object.__setattr__(ctx, "config", conf)
        except Exception:
            pass
    except Exception:
        conf = load_config()
    url = f"{conf.hub_url.rstrip('/')}/api/observe/ingest"
    headers = {"X-AdaOS-Token": conf.token, "Content-Type": "application/json"}

    batch: List[Dict[str, Any]] = []
    backoff = 1
    while True:
        try:
            try:
                item = await asyncio.wait_for(_QUEUE.get(), timeout=1.0)
                batch.append(item)
                while not _QUEUE.empty() and len(batch) < 200:
                    batch.append(_QUEUE.get_nowait())
            except asyncio.TimeoutError:
                pass

            if not batch:
                continue

            payload = {"node_id": conf.node_id, "events": batch}
            r = requests.post(url, json=payload, headers=headers, timeout=3)
            if r.status_code == 200:
                batch.clear()
                backoff = 1
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _emit_wrapper(topic: str, payload: Dict[str, Any], **kwargs):
    """Оборачиваем sdk.bus.emit: добавляем trace_id, логируем и шлём дальше в оригинал."""
    assert _ORIG_EMIT is not None
    trace = _ensure_trace(kwargs)
    res = await _ORIG_EMIT(topic, payload, **kwargs)
    event = _serialize_event(topic, payload, kwargs)
    try:
        ctx = get_ctx()
        conf = getattr(ctx, "config", None) or load_config()
        try:
            if getattr(ctx, "config", None) is None:
                object.__setattr__(ctx, "config", conf)
        except Exception:
            pass
    except Exception:
        conf = load_config()
    _enqueue_local(event)
    await BROADCAST.publish(event)
    if conf.role == "member" and _QUEUE:
        try:
            _QUEUE.put_nowait(event)
        except Exception:
            pass
    return res


def attach_http_trace_headers(request_headers: Dict[str, str], response_headers: Dict[str, str]):
    """
    Прокидываем X-AdaOS-Trace в HTTP-ответ.
    Совместима со старыми эндпоинтами.
    """
    incoming = request_headers.get("X-AdaOS-Trace")
    trace = incoming or str(uuid.uuid4())
    response_headers["X-AdaOS-Trace"] = trace
    return trace


async def start_observer():
    """
    Идемпотентно подключает обёртку над emit и поднимает фоновые задачи (для member).
    """
    global _ORIG_EMIT, _QUEUE, _LOG_TASK
    if _ORIG_EMIT is not None:
        return

    _ORIG_EMIT = bus_module.emit
    bus_module.emit = _emit_wrapper  # type: ignore
    _ensure_local_log_worker()

    try:
        ctx = get_ctx()
        conf = getattr(ctx, "config", None) or load_config()
        try:
            if getattr(ctx, "config", None) is None:
                object.__setattr__(ctx, "config", conf)
        except Exception:
            pass
    except Exception:
        conf = load_config()
    if conf.role == "member":
        _QUEUE = _QUEUE or asyncio.Queue(maxsize=5000)
        if not _LOG_TASK:
            _LOG_TASK = asyncio.create_task(_push_loop(), name="adaos-observe-push")


async def stop_observer():
    """Отключить фоновые задачи и вернуть оригинальный emit."""
    global _ORIG_EMIT, _LOG_TASK, _QUEUE
    if _LOG_TASK:
        _LOG_TASK.cancel()
        try:
            await _LOG_TASK
        except Exception:
            pass
        _LOG_TASK = None
    _QUEUE = None
    _stop_local_log_worker()
    if _ORIG_EMIT is not None:
        bus_module.emit = _ORIG_EMIT  # type: ignore
        _ORIG_EMIT = None


def pass_filters(evt: Dict[str, Any], topic_prefix: str | None, node_id: str | None, since_ts: float | None) -> bool:
    if topic_prefix and not str(evt.get("topic", "")).startswith(topic_prefix):
        return False
    if node_id and str(evt.get("node_id")) != node_id:
        return False
    if since_ts and float(evt.get("ts", 0.0)) < float(since_ts):
        return False
    return True
