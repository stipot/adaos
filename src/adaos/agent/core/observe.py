from __future__ import annotations
import asyncio, json, time, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from adaos.agent.core.node_config import load_config
from adaos.sdk.context import get_base_dir
from adaos.sdk import bus as bus_module  # будем мягко оборачивать emit

_LOG_TASK: Optional[asyncio.Task] = None
_QUEUE: "asyncio.Queue[Dict[str, Any]]" | None = None
_ORIG_EMIT = None
_LOG_FILE: Path | None = None


def _log_path() -> Path:
    p = Path(get_base_dir()) / "logs"
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
    if conf.role == "hub":
        _write_local(event)
    else:
        # member: в очередь для отправки на hub + дублируем локально
        _write_local(event)
        if _QUEUE:
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
    Идempotent: повторные вызовы безопасны.
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
