# src/adaos/apps/api/nlu_teacher_api.py
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu_lookup_tables import collect_desktop_lookup_tables_async
from adaos.services.nlu.probe import probe_phrase
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

router = APIRouter(tags=["nlu-teacher"])


def _resolve_webspace_id(token: Optional[str]) -> str:
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _teacher_obj(data_map: Any) -> dict:
    current = data_map.get("nlu_teacher")
    return dict(current) if isinstance(current, dict) else {}


class ApplyRevisionRequest(BaseModel):
    revision_id: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    examples: list[str] = Field(default_factory=list)
    slots: Dict[str, Any] = Field(default_factory=dict)


class SaveExampleTarget(BaseModel):
    type: str = Field(..., min_length=1)
    id: Optional[str] = None


class SaveExampleRequest(BaseModel):
    text: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    target: SaveExampleTarget
    slots: Dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None
    source: Optional[str] = None
    note: Optional[str] = None


class ProbePhraseRequest(BaseModel):
    text: str = Field(..., min_length=1)
    use_rasa: bool = True
    emit_trace: bool = True
    request_locale: Optional[str] = None
    preferred_locales: list[str] = Field(default_factory=list)


@router.get("/nlu/teacher/{webspace_id}", dependencies=[Depends(require_token)])
async def get_teacher_state(webspace_id: str):
    ws = _resolve_webspace_id(webspace_id)
    async with async_get_ydoc(ws) as ydoc:
        data_map = ydoc.get_map("data")
        return {"webspace_id": ws, "nlu_teacher": _teacher_obj(data_map)}


@router.get("/nlu/teacher/{webspace_id}/lookups", dependencies=[Depends(require_token)])
async def get_lookup_tables(webspace_id: str):
    ws = _resolve_webspace_id(webspace_id)
    try:
        return await collect_desktop_lookup_tables_async(webspace_id=ws, include_live=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to collect lookup tables: {exc}")


@router.post("/nlu/teacher/{webspace_id}/revision/apply", dependencies=[Depends(require_token)])
async def apply_revision(webspace_id: str, body: ApplyRevisionRequest):
    ws = _resolve_webspace_id(webspace_id)
    ctx = get_ctx()

    examples = [x.strip() for x in (body.examples or []) if isinstance(x, str) and x.strip()]
    payload = {
        "webspace_id": ws,
        "revision_id": body.revision_id.strip(),
        "intent": body.intent.strip(),
        "examples": examples,
        "slots": dict(body.slots or {}),
        "_meta": {"webspace_id": ws},
    }

    try:
        bus_emit(ctx.bus, "nlp.teacher.revision.apply", payload, source="api.nlu.teacher")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to emit apply event: {exc}")

    return {"ok": True, "webspace_id": ws, "revision_id": body.revision_id, "intent": body.intent}


@router.post("/nlu/teacher/{webspace_id}/example/save", dependencies=[Depends(require_token)])
async def save_example(webspace_id: str, body: SaveExampleRequest):
    ws = _resolve_webspace_id(webspace_id)
    ctx = get_ctx()
    payload = {
        "webspace_id": ws,
        "text": body.text.strip(),
        "intent": body.intent.strip(),
        "target": body.target.model_dump(exclude_none=True),
        "slots": dict(body.slots or {}),
        "request_id": body.request_id.strip() if isinstance(body.request_id, str) and body.request_id.strip() else None,
        "source": body.source.strip() if isinstance(body.source, str) and body.source.strip() else "api.nlu.teacher",
        "note": body.note.strip() if isinstance(body.note, str) and body.note.strip() else None,
        "_meta": {"webspace_id": ws},
    }

    try:
        bus_emit(ctx.bus, "nlp.teacher.example.save", payload, source="api.nlu.teacher")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to emit save example event: {exc}")

    return {"ok": True, "webspace_id": ws, "intent": payload["intent"], "target": payload["target"]}


@router.post("/nlu/teacher/{webspace_id}/probe", dependencies=[Depends(require_token)])
async def probe(webspace_id: str, body: ProbePhraseRequest):
    ws = _resolve_webspace_id(webspace_id)
    try:
        result = await probe_phrase(
            body.text,
            webspace_id=ws,
            use_rasa=bool(body.use_rasa),
            emit_trace=bool(body.emit_trace),
            request_locale=body.request_locale,
            preferred_locales=body.preferred_locales,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to probe phrase: {exc}")
    return result

