from __future__ import annotations
import asyncio, json, time, uuid, gzip
from pathlib import Path
from typing import Any, Dict, List, Optional, AsyncIterator

import requests

from adaos.agent.core.node_config import load_config
from adaos.sdk import bus as bus_module  # будем мягко оборачивать emit
import os

try:
    # get_ctx может быть недоступен/неинициализирован на момент импорта
    from adaos.services.agent_context import get_ctx  # type: ignore
except Exception:  # noqa: BLE001
    get_ctx = None  # type: ignore


def _default_base_dir() -> Path:
    env = os.environ.get("ADAOS_BASE_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        # %LOCALAPPDATA%\AdaOS по умолчанию
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "AdaOS"
    return Path.home() / ".adaos"


def _resolve_base_dir() -> Path:
    # 1) пробуем контекст, если он уже инициализирован
    if get_ctx:
        try:
            return Path(get_ctx().paths.base)  # type: ignore[attr-defined]
        except Exception:
            pass
    # 2) безопасный фолбэк
    return _default_base_dir()


# важно: BASE_DIR теперь не зависит от ранней инициализации контекста
BASE_DIR = _resolve_base_dir()
# --- настройки ротации ---
_MAX_BYTES = 5 * 1024 * 1024  # 5MB на файл
_KEEP = 3  # events.log, .1.gz, .2.gz, .3.gz

_LOG_TASK: Optional[asyncio.Task] = None
_QUEUE: "asyncio.Queue[Dict[str, Any]]" | None = None
_ORIG_EMIT = None
_LOG_FILE: Path | None = None


# --- простой broadcaster для SSE ---
class EventBroadcaster:
    def __init__(self):
        self._subs: List[asyncio.Queue] = []

    async def publish(self, evt: Dict[str, Any]):
        # не блокируем продьюсер: мелкая очередь, дропаем по переполнению
        for q in list(self._subs):
            try:
                if q.full():
                    _ = q.get_nowait()
                q.put_nowait(evt)
            except Exception:
                try:
                    self._subs.remove(q)
                except Exception:
                    pass

    def subscribe(self, *, topic_prefix: str | None, node_id: str | None, since_ts: float | None) -> "asyncio.Queue[Dict[str, Any]]":
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        q._adaos_filter = {"topic_prefix": topic_prefix, "node_id": node_id, "since": since_ts}  # type: ignore[attr-defined]
        self._subs.append(q)
        return q


BROADCAST = EventBroadcaster()


def _log_path() -> Path:
    p = BASE_DIR / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "events.log"


def _now_ts() -> float:
    return time.time()


def _ensure_trace(kwargs: Dict[str, Any]) -> str:
    # пробуем взять trace_id из kwargs, иначе генерим
    trace = kwargs.get("trace_id") or kwargs.get("trace") or str(uuid.uuid4())
    kwargs["trace_id"] = trace
    return trace


def _serialize_event(topic: str, payload: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
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
            # сдвигаем .(n-1).gz → .n.gz
            for i in range(_KEEP, 0, -1):
                src = path.with_suffix(path.suffix + ("" if i == 1 else f".{i-1}.gz"))
                dst = path.with_suffix(path.suffix + f".{i}.gz")
                if i == 1:
                    # src = events.log -> compress to .1.gz
                    if path.exists():
                        data = path.read_bytes()
                        with gzip.open(path.with_suffix(path.suffix + ".1.gz"), "wb") as gz:
                            gz.write(data)
                        path.unlink(missing_ok=True)
                else:
                    if src.exists():
                        src.rename(dst)
    except Exception:
        pass


def _write_local(e: Dict[str, Any]) -> None:
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = _log_path()
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


async def _push_loop():
    """Фоновая отправка батчей логов на hub (для member)."""
    assert _QUEUE is not None
    conf = load_config()
    url = f"{conf.hub_url.rstrip('/')}/api/observe/ingest"
    headers = {"X-AdaOS-Token": conf.token, "Content-Type": "application/json"}

    batch: List[Dict[str, Any]] = []
    backoff = 1
    while True:
        try:
            # набираем небольшие батчи или ждём таймаут
            try:
                item = await asyncio.wait_for(_QUEUE.get(), timeout=1.0)
                batch.append(item)
                # собираем до 200 записей за раз
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
                # ошибка — подождём и попробуем снова (сохранив batch)
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
    # сначала вызов оригинального emit (чтобы не менять семантику)
    res = await _ORIG_EMIT(topic, payload, **kwargs)
    # затем логирование
    event = _serialize_event(topic, payload, kwargs)
    conf = load_config()
    # запись локально + публикация
    _write_local(event)
    await BROADCAST.publish(event)
    # отправка на hub (только у member)
    if conf.role == "member" and _QUEUE:
        try:
            _QUEUE.put_nowait(event)
        except Exception:
            pass
    return res


def attach_http_trace_headers(request_headers: Dict[str, str], response_headers: Dict[str, str]):
    """
    Удобный хук: прокидываем X-AdaOS-Trace в HTTP.
    Вызывай в эндпоинтах, чтобы включать trace_id в ответ.
    """
    incoming = request_headers.get("X-AdaOS-Trace")
    trace = incoming or str(uuid.uuid4())
    response_headers["X-AdaOS-Trace"] = trace
    return trace


async def start_observer():
    """
    Подключает обёртку над emit и поднимает фоновые задачи (для member).
    Идемпотентно подключает обёртку над emit и фоновые таски.
    """
    global _ORIG_EMIT, _QUEUE, _LOG_TASK
    if _ORIG_EMIT is not None:
        return  # уже подключены

    # оборачиваем emit
    _ORIG_EMIT = bus_module.emit
    bus_module.emit = _emit_wrapper  # type: ignore

    conf = load_config()
    # локальный лог всегда пишем; для member поднимаем sender
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
    if _ORIG_EMIT is not None:
        bus_module.emit = _ORIG_EMIT  # type: ignore
        _ORIG_EMIT = None


def pass_filters(evt: Dict[str, Any], topic_prefix: str | None, node_id: str | None, since_ts: float | None) -> bool:
    """Вспомогательное: фильтрация событий для подписчика (исп. в observe_api)"""
    if topic_prefix and not str(evt.get("topic", "")).startswith(topic_prefix):
        return False
    if node_id and str(evt.get("node_id")) != node_id:
        return False
    if since_ts and float(evt.get("ts", 0.0)) < float(since_ts):
        return False
    return True
