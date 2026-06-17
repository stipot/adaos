from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Dict, Mapping, Optional

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.feedback_examples import save_feedback_example
from adaos.services.nlu.teacher_events import append_event, make_event, rebuild_events_by_candidate
from adaos.services.nlu.ycoerce import coerce_dict, iter_mappings
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.teacher.runtime")

_MAX_ITEMS = int(os.getenv("ADAOS_NLU_TEACHER_MAX", "200") or "200")
_ENABLED = os.getenv("ADAOS_NLU_TEACHER") == "1"


def _nlu_teacher_write_meta():
    return ystore_write_metadata(
        root_names=["data"],
        source="nlu.teacher_runtime",
        owner="core:nlu.teacher",
        channel="core.nlu.teacher.async",
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


def _read_teacher_obj(data_map: Any) -> dict[str, Any]:
    return coerce_dict(getattr(data_map, "get", lambda _k: None)("nlu_teacher"))


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping) or not isinstance(value, Iterable):
        return []
    out: list[dict[str, Any]] = []
    for item in iter_mappings(value):
        out.append(dict(item))
    return out


def _bounded(items: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    if max_items <= 0:
        return items
    if len(items) <= max_items:
        return items
    return items[-max_items:]


async def _get_current_scenario_id(webspace_id: str) -> str | None:
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            raw = ui_map.get("current_scenario")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    except Exception:
        return None
    return None


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _apply_revision_to_scenario_file(*, scenario_id: str, intent: str, examples: list[str]) -> None:
    if not scenario_id or not intent:
        return
    root = scenarios_loader.scenario_root(scenario_id)
    path = root / "scenario.json"
    if not path.exists():
        return

    try:
        raw = path.read_text(encoding="utf-8-sig")
        doc = json.loads(raw)
    except Exception:
        _log.warning("teacher revision apply: failed to read scenario.json scenario=%s", scenario_id, exc_info=True)
        return
    if not isinstance(doc, dict):
        return

    nlu = doc.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
        doc["nlu"] = nlu
    intents = nlu.get("intents")
    if not isinstance(intents, dict):
        intents = {}
        nlu["intents"] = intents

    spec = intents.get(intent)
    if not isinstance(spec, dict):
        spec = {"scope": "scenario", "examples": []}
        intents[intent] = spec

    existing = spec.get("examples")
    if not isinstance(existing, list):
        existing = []
    merged = _dedupe_keep_order([*(str(x) for x in existing if isinstance(x, str)), *examples])
    spec["examples"] = merged

    try:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        _log.warning("teacher revision apply: failed to write scenario.json scenario=%s", scenario_id, exc_info=True)
        return

    scenarios_loader.invalidate_cache(scenario_id=scenario_id, space="workspace")


async def _append_revision(webspace_id: str, revision: dict[str, Any]) -> None:
    async with _nlu_teacher_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _read_teacher_obj(data_map)
            revisions = _list_of_dicts(teacher.get("revisions"))
            revisions.append(dict(revision))
            revisions = _bounded(revisions, max_items=_MAX_ITEMS)
            teacher["revisions"] = revisions
            rebuild_events_by_candidate(teacher)
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)


async def _update_revision(
    webspace_id: str,
    *,
    revision_id: str,
    patch: dict[str, Any],
) -> Optional[dict[str, Any]]:
    async with _nlu_teacher_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _read_teacher_obj(data_map)
            revisions = teacher.get("revisions")
            if isinstance(revisions, (str, bytes, bytearray)) or isinstance(revisions, Mapping) or not isinstance(revisions, Iterable):
                return None

            cleaned: list[dict[str, Any]] = []
            updated: Optional[dict[str, Any]] = None
            for item in list(revisions):
                if not isinstance(item, Mapping):
                    continue
                d = dict(item)
                if d.get("id") == revision_id:
                    updated = dict(d)
                    updated.update(patch)
                    cleaned.append(updated)
                else:
                    cleaned.append(d)

            teacher["revisions"] = _bounded(cleaned, max_items=_MAX_ITEMS)
            rebuild_events_by_candidate(teacher)
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)
            return updated


async def _append_dataset_item(webspace_id: str, item: dict[str, Any]) -> None:
    async with _nlu_teacher_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _read_teacher_obj(data_map)
            dataset = _list_of_dicts(teacher.get("dataset"))
            dataset.append(dict(item))
            dataset = _bounded(dataset, max_items=_MAX_ITEMS)
            teacher["dataset"] = dataset
            rebuild_events_by_candidate(teacher)
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)


@subscribe("nlp.teacher.example.save")
async def _on_example_save(evt: Any) -> None:
    """
    Save an operator-approved positive example into its owning artifact.

    Payload:
      - text: example phrase
      - intent: canonical intent
      - target: {"type": "skill"|"scenario"|"system_action", "id": "..."}
      - slots: optional slot/entity evidence
      - request_id / source / note / _meta: audit metadata
    """
    ctx = get_ctx()
    try:
        allow = bool(getattr(getattr(ctx.config, "root_settings", None), "llm", None).allow_nlu_teacher)  # type: ignore[attr-defined]
    except Exception:
        allow = True

    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    if not allow:
        try:
            bus_emit(
                ctx.bus,
                "nlp.teacher.example.save.failed",
                {"webspace_id": webspace_id, "reason": "nlu_teacher_disabled", "_meta": dict(meta)},
                source="nlu.teacher.runtime",
            )
        except Exception:
            pass
        return

    text = payload.get("text") or payload.get("example")
    intent = payload.get("intent")
    if not isinstance(text, str) or not text.strip() or not isinstance(intent, str) or not intent.strip():
        return
    text = text.strip()
    intent = intent.strip()
    slots = payload.get("slots") if isinstance(payload.get("slots"), Mapping) else {}
    target = payload.get("target") if isinstance(payload.get("target"), Mapping) else None
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    audit = {
        "webspace_id": webspace_id,
        "request_id": request_id,
        "source": payload.get("source") if isinstance(payload.get("source"), str) else "nlu_teacher",
        "note": payload.get("note") if isinstance(payload.get("note"), str) else None,
        "meta": dict(meta),
        "accepted_at": time.time(),
    }

    result = save_feedback_example(
        ctx=ctx,
        target=target,
        intent=intent,
        example=text,
        slots=slots,
        audit=audit,
    )
    dataset_item = {
        "id": f"ds.{int(time.time()*1000)}",
        "ts": time.time(),
        "status": "positive_feedback" if result.get("ok") else "positive_feedback_failed",
        "intent": intent,
        "examples": [text],
        "slots": dict(slots),
        "target": result.get("target") or (dict(target) if isinstance(target, Mapping) else {}),
        "request_id": request_id,
        "audit": audit,
        "result": result,
    }
    try:
        await _append_dataset_item(webspace_id, dataset_item)
    except Exception:
        _log.debug("failed to append saved example dataset item webspace=%s", webspace_id, exc_info=True)

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=request_id,
                request_text=text,
                kind="example.saved" if result.get("ok") else "example.save_failed",
                title="Example saved" if result.get("ok") else "Example save failed",
                subtitle=intent,
                raw=dataset_item,
                meta=meta,
            ),
        )
    except Exception:
        _log.debug("failed to append teacher example event webspace=%s", webspace_id, exc_info=True)

    event_name = "nlp.teacher.example.saved" if result.get("ok") else "nlp.teacher.example.save.failed"
    bus_emit(
        ctx.bus,
        event_name,
        {"webspace_id": webspace_id, "dataset_item": dataset_item, "result": result, "_meta": dict(meta)},
        source="nlu.teacher.runtime",
    )


@subscribe("nlp.teacher.request")
async def _on_teacher_request(evt: Any) -> None:
    if not _ENABLED:
        return

    ctx = None
    webspace_id = None
    try:
        ctx = get_ctx()
        try:
            allow = bool(getattr(getattr(ctx.config, "root_settings", None), "llm", None).allow_nlu_teacher)  # type: ignore[attr-defined]
        except Exception:
            allow = True
        if not allow:
            return
        payload = _payload(evt)
        webspace_id = _resolve_webspace_id(payload)

        req = payload.get("request") if isinstance(payload.get("request"), Mapping) else None
        if not req:
            return

        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        revision = {
            "id": f"rev.{int(time.time()*1000)}",
            "ts": time.time(),
            "status": "proposed",
            "request_id": req.get("request_id"),
            "text": text.strip(),
            "meta": dict(req.get("_meta") or {}) if isinstance(req.get("_meta"), Mapping) else {},
            "proposal": {
                "intent": None,
                "examples": [text.strip()],
                "slots": {},
            },
            "note": "Awaiting teacher/LLM suggestion (stub).",
        }

        try:
            await _append_revision(webspace_id, revision)
        except Exception:
            _log.debug("failed to append teacher revision webspace=%s", webspace_id, exc_info=True)
            return

        try:
            await append_event(
                webspace_id,
                make_event(
                    webspace_id=webspace_id,
                    request_id=revision.get("request_id") if isinstance(revision.get("request_id"), str) else None,
                    request_text=text.strip(),
                    kind="revision.proposed",
                    title="Revision proposed",
                    subtitle="pending",
                    raw=revision,
                    meta=revision.get("meta") if isinstance(revision.get("meta"), Mapping) else {},
                ),
            )
        except Exception:
            _log.debug("failed to append nlu_teacher event webspace=%s", webspace_id, exc_info=True)

        bus_emit(
            ctx.bus,
            "nlp.teacher.revision.proposed",
            {"webspace_id": webspace_id, "revision": revision},
            source="nlu.teacher.runtime",
        )
    except Exception:
        _log.warning("teacher runtime handler crashed webspace=%s", webspace_id, exc_info=True)
        return


@subscribe("nlp.teacher.revision.apply")
async def _on_revision_apply(evt: Any) -> None:
    if not _ENABLED:
        return

    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)

    revision_id = payload.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id.strip():
        return
    revision_id = revision_id.strip()

    intent = payload.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return
    intent = intent.strip()

    examples = payload.get("examples")
    if isinstance(examples, list):
        examples = [x.strip() for x in examples if isinstance(x, str) and x.strip()]
    else:
        examples = []

    slots = payload.get("slots") if isinstance(payload.get("slots"), Mapping) else {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}

    apply_patch = {
        "status": "applied",
        "applied_at": time.time(),
        "applied": {
            "intent": intent,
            "examples": examples,
            "slots": dict(slots),
        },
    }
    try:
        updated = await _update_revision(webspace_id, revision_id=revision_id, patch=apply_patch)
    except Exception:
        _log.debug("failed to apply teacher revision webspace=%s revision_id=%s", webspace_id, revision_id, exc_info=True)
        return

    dataset_item = {
        "id": f"ds.{int(time.time()*1000)}",
        "ts": time.time(),
        "status": "revision",
        "intent": intent,
        "examples": examples,
        "slots": dict(slots),
        "revision_id": revision_id,
    }
    try:
        await _append_dataset_item(webspace_id, dataset_item)
    except Exception:
        _log.debug("failed to append teacher dataset item webspace=%s", webspace_id, exc_info=True)

    try:
        scenario_id = meta.get("scenario_id") if isinstance(meta.get("scenario_id"), str) else None
        if not scenario_id:
            scenario_id = await _get_current_scenario_id(webspace_id)
        if scenario_id:
            _apply_revision_to_scenario_file(scenario_id=scenario_id, intent=intent, examples=examples)
    except Exception:
        _log.debug("teacher revision apply: scenario update failed webspace=%s", webspace_id, exc_info=True)

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=updated.get("request_id") if isinstance(updated, dict) and isinstance(updated.get("request_id"), str) else None,
                request_text=updated.get("text") if isinstance(updated, dict) and isinstance(updated.get("text"), str) else intent,
                kind="revision.applied",
                title="Revision applied",
                subtitle=intent,
                raw=updated if isinstance(updated, Mapping) else {"revision_id": revision_id, "intent": intent},
                meta=updated.get("meta") if isinstance(updated, dict) and isinstance(updated.get("meta"), Mapping) else {},
            ),
        )
    except Exception:
        _log.debug("failed to append nlu_teacher event webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.revision.applied",
        {"webspace_id": webspace_id, "revision": updated, "dataset_item": dataset_item},
        source="nlu.teacher.runtime",
    )

    # Optional: kick auto-train if enabled (service skill decides by env flag).
    try:
        bus_emit(ctx.bus, "nlp.rasa.train", {"webspace_id": webspace_id}, source="nlu.teacher.runtime")
    except Exception:
        _log.debug("failed to emit nlp.rasa.train webspace=%s", webspace_id, exc_info=True)
