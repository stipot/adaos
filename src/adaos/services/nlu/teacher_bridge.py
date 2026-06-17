from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.nlu.ycoerce import coerce_dict, iter_mappings

_log = logging.getLogger("adaos.nlu.teacher")

_MAX_ITEMS = int(os.getenv("ADAOS_NLU_TEACHER_MAX", "200") or "200")
_ENABLED = os.getenv("ADAOS_NLU_TEACHER") == "1"
_TEACHER_EVIDENCE_FIELDS = ("intent", "confidence", "slots", "entities", "intent_ranking", "_raw")


def _nlu_teacher_bridge_write_meta():
    return ystore_write_metadata(
        root_names=["data"],
        source="nlu.teacher_bridge",
        owner="core:nlu.teacher_bridge",
        channel="core.nlu.teacher_bridge.async",
    )


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = coerce_dict(payload.get("_meta"))
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _list_of_dicts(value: Any) -> list[dict]:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping) or not isinstance(value, Iterable):
        return []
    return [dict(x) for x in iter_mappings(value)]


async def _append_teacher_item(webspace_id: str, item: dict) -> None:
    from adaos.services.yjs.doc import async_get_ydoc

    async with _nlu_teacher_bridge_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            current = data_map.get("nlu_teacher")
            teacher: dict = coerce_dict(current)
            items = _list_of_dicts(teacher.get("items"))
            items.append(item)
            if _MAX_ITEMS > 0 and len(items) > _MAX_ITEMS:
                items = items[-_MAX_ITEMS:]
            with ydoc.begin_transaction() as txn:
                teacher["items"] = items
                data_map.set(txn, "nlu_teacher", teacher)


@subscribe("nlp.intent.not_obtained")
async def _on_not_obtained(evt: Any) -> None:
    if not _ENABLED:
        return

    ctx = get_ctx()
    try:
        allow = bool(getattr(getattr(ctx.config, "root_settings", None), "llm", None).allow_nlu_teacher)  # type: ignore[attr-defined]
    except Exception:
        allow = True
    if not allow:
        return

    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    webspace_id = _resolve_webspace_id(payload)
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    reason = payload.get("reason") if isinstance(payload.get("reason"), str) else "unknown"
    via = payload.get("via") if isinstance(payload.get("via"), str) else None
    meta = coerce_dict(payload.get("_meta"))

    item = {
        "id": f"teach.{int(time.time()*1000)}",
        "ts": time.time(),
        "text": text,
        "reason": reason,
        "via": via,
        "request_id": request_id,
        "_meta": dict(meta),
    }
    for field in _TEACHER_EVIDENCE_FIELDS:
        value = payload.get(field)
        if value is not None:
            item[field] = value

    try:
        await _append_teacher_item(webspace_id, item)
    except Exception:
        _log.debug("failed to append nlu_teacher item webspace=%s", webspace_id, exc_info=True)

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=request_id,
                request_text=text,
                kind="not_obtained",
                title="Intent not obtained",
                subtitle=f"{reason} via={via}" if via else reason,
                raw=item,
                meta=meta,
            ),
        )
    except Exception:
        _log.debug("failed to append nlu_teacher event webspace=%s", webspace_id, exc_info=True)

    # Emit a single, generic event to be consumed by an external teacher (LLM).
    bus_emit(
        ctx.bus,
        "nlp.teacher.request",
        {"webspace_id": webspace_id, "request": item},
        source="nlu.teacher",
    )
