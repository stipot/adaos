from __future__ import annotations
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel
from typing import Any, Dict, List
import json, time
from pathlib import Path

from adaos.api.auth import require_token
from adaos.agent.core.node_config import load_config
from adaos.agent.core.observe import _log_path  # локальный writer совместим с форматом
import adaos.sdk.bus as bus

router = APIRouter()


class IngestBatch(BaseModel):
    node_id: str
    events: List[Dict[str, Any]]


@router.post("/observe/ingest", dependencies=[Depends(require_token)])
async def observe_ingest(batch: IngestBatch):
    """Приём батчей логов с member-нод. Доступно только на hub."""
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub accepts logs")

    logf = _log_path()
    with logf.open("a", encoding="utf-8") as f:
        for e in batch.events:
            # гарантируем наличие node_id (берём из батча — доверяем member)
            e.setdefault("node_id", batch.node_id)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"ok": True, "ingested": len(batch.events)}


@router.get("/observe/tail", dependencies=[Depends(require_token)])
async def observe_tail(lines: int = 200):
    """Простой tail для отладки. Не потоковый, просто последние N строк."""
    conf = load_config()
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
    return {"ok": True, "lines": out}


@router.post("/observe/test", dependencies=[Depends(require_token)])
async def observe_test(kind: str = "ping", note: str | None = None):
    """
    Сгенерировать тестовое событие на BUS без навыков.
    kind: префикс топика (ping → obs.test.ping)
    """
    topic = f"obs.test.{kind}"
    payload = {"note": note or "hello", "at": time.time()}
    await bus.emit(topic, payload, source="observe_api", actor="diagnostics")
    return {"ok": True, "topic": topic, "payload": payload}
