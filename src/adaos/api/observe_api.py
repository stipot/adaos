from __future__ import annotations
import asyncio
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Any, Dict, List, AsyncIterator
import json, time
from pathlib import Path

from adaos.api.auth import require_token
from adaos.agent.core.node_config import load_config
from adaos.agent.core.observe import _log_path, BROADCAST, pass_filters  # локальный writer совместим с форматом
import adaos.sdk.bus as bus

router = APIRouter(tags=["observe"], dependencies=[Depends(require_token)])


class IngestBatch(BaseModel):
    node_id: str
    events: List[Dict[str, Any]]


@router.post("/ingest", dependencies=[Depends(require_token)])
async def observe_ingest(batch: IngestBatch):
    """Приём батчей логов с member-нод (hub-only). Также публикуем в SSE."""
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub accepts logs")

    logf = _log_path()
    ingested = 0
    with logf.open("a", encoding="utf-8") as f:
        for e in batch.events:
            # гарантируем наличие node_id (берём из батча — доверяем member)
            e.setdefault("node_id", batch.node_id)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            ingested += 1
    # Публикуем полученные события (чтобы зрители SSE видели ленту)
    for e in batch.events:
        await BROADCAST.publish(e)
    return {"ok": True, "ingested": ingested}


@router.get("/tail", dependencies=[Depends(require_token)])
async def observe_tail(lines: int = 200, topic_prefix: str | None = None, node_id: str | None = None):
    """Последние N строк, можно фильтровать по topic_prefix и node_id (hub/member)."""
    logf = _log_path()
    if not logf.exists():
        return {"ok": True, "lines": []}
    # грубый tail без mmap
    with logf.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 4096
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
        out = [line.decode("utf-8", errors="ignore") for line in data.splitlines()[-lines:]]
    if not (topic_prefix or node_id):
        return {"ok": True, "lines": out}
    # фильтрация по JSON‑полям
    filtered: List[str] = []
    for ln in out:
        try:
            obj = json.loads(ln)
            if pass_filters(obj, topic_prefix, node_id, None):
                filtered.append(ln)
        except Exception:
            pass
    return {"ok": True, "lines": filtered}


async def _sse_iter(topic_prefix: str | None, node_id: str | None, since: float | None, replay_lines: int | None = 5) -> AsyncIterator[bytes]:
    """
    Итератор для SSE. Не держим старую историю (кроме опционального since для фильтра).
    """
    q = BROADCAST.subscribe(topic_prefix=topic_prefix, node_id=node_id, since_ts=since)
    # шлём комментарий раз в 15с, чтобы соединение не засыпало
    heartbeat_at = time.time()
    if replay_lines and not since:
        # прочитаем хвост и отправим их как "исторические"
        from adaos.agent.core.observe import _log_path

        try:
            with _log_path().open("r", encoding="utf-8") as f:
                tail = f.readlines()[-int(replay_lines) :]
            for ln in tail:
                try:
                    obj = json.loads(ln)
                    if pass_filters(obj, topic_prefix, node_id, None):
                        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                        yield b"event: adaos\n" + b"data: " + data + b"\n\n"
                except Exception:
                    pass
        except Exception:
            pass
    try:
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=5.0)  # type: ignore[name-defined]
                if pass_filters(evt, topic_prefix, node_id, since):
                    data = json.dumps(evt, ensure_ascii=False).encode("utf-8")
                    yield b"event: adaos\n" + b"data: " + data + b"\n\n"
            except asyncio.TimeoutError:  # type: ignore[name-defined]
                pass

            if time.time() - heartbeat_at >= 15:
                heartbeat_at = time.time()
                # комментарий — валидный SSE keep-alive
                yield b": keep-alive\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        # клиент закрыл соединение — выходим тихо
        return


@router.get("/stream", dependencies=[Depends(require_token)])
async def observe_stream(
    topic_prefix: str | None = None,
    node_id: str | None = None,
    since: float | None = None,
    replay_lines: int | None = None,
):
    """
    SSE‑стрим событий: text/event-stream.
    Примеры:
      /api/observe/stream?topic_prefix=net.subnet.
      /api/observe/stream?node_id=<uuid>
    """
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # TODO CORS можно включить по желанию:
        # "Access-Control-Allow-Origin": "*",
    }
    return StreamingResponse(_sse_iter(topic_prefix, node_id, since, replay_lines), media_type="text/event-stream", headers=headers)


@router.post("/test", dependencies=[Depends(require_token)])
async def observe_test(kind: str = "ping", note: str | None = None, topic: str | None = None):
    """
    Сгенерировать тестовое событие на BUS без навыков.
    - kind: префикс топика (ping → obs.test.ping)
    - topic="net.subnet.demo" (или "ui.demo", "obs.test.*") — явный топик из белого списка.
    """
    allow_prefixes = ("obs.test.", "net.subnet.", "ui.")
    if topic:
        t = topic.strip()
        if not any(t.startswith(p) for p in allow_prefixes):
            raise HTTPException(status_code=400, detail="topic prefix not allowed")
    else:
        t = f"obs.test.{(kind or 'ping').strip()}"
    payload = {"note": note or "hello", "at": time.time()}
    await bus.emit(t, payload, source="observe_api", actor="diagnostics")
    return {"ok": True, "topic": t, "payload": payload}
