from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Mapping
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.skill.service_supervisor import get_service_supervisor
from .neural_usage_stats import record_neural_fallback_outcome
from .rasa_skill_installer import is_rasa_nlu_enabled

_log = logging.getLogger("adaos.nlu.rasa")
_SEMAPHORE = asyncio.Semaphore(2)
_PARSE_TIMEOUT_S = float(os.getenv("ADAOS_NLU_RASA_PARSE_TIMEOUT_S", "8.0") or "8.0")
_START_LOCK = asyncio.Lock()
_ISSUE_WINDOW_S = float(os.getenv("ADAOS_NLU_RASA_ISSUE_WINDOW_S", "60") or "60")
_ISSUE_THRESHOLD = int(os.getenv("ADAOS_NLU_RASA_ISSUE_THRESHOLD", "3") or "3")
_MIN_CONFIDENCE = float(os.getenv("ADAOS_NLU_RASA_MIN_CONFIDENCE", "0.6") or "0.6")
_issue_times: dict[str, list[float]] = {}


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str | None:
    meta = payload.get("_meta") or {}
    if isinstance(meta, Mapping):
        meta_ws = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(meta_ws, str) and meta_ws.strip():
            return meta_ws.strip()
    token = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _emit_not_obtained(
    *,
    ctx: Any,
    text: str,
    webspace_id: str | None,
    request_id: str | None,
    meta: Mapping[str, Any],
    reason: str,
) -> None:
    out: Dict[str, Any] = {"reason": reason, "text": text, "via": "rasa"}
    if webspace_id:
        out["webspace_id"] = webspace_id
    if request_id:
        out["request_id"] = request_id
    if isinstance(meta, Mapping) and meta:
        out["_meta"] = dict(meta)
    bus_emit(ctx.bus, "nlp.intent.not_obtained", out, source="nlu.rasa")


def _emit_stage(
    *,
    ctx: Any,
    text: str,
    webspace_id: str | None,
    request_id: str | None,
    meta: Mapping[str, Any],
    status: str,
    reason: str | None = None,
    intent: str | None = None,
    confidence: float | None = None,
    slots: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
) -> None:
    payload: Dict[str, Any] = {
        "stage": "rasa",
        "status": status,
        "text": text,
        "via": "rasa",
    }
    if webspace_id:
        payload["webspace_id"] = webspace_id
    if request_id:
        payload["request_id"] = request_id
    if reason:
        payload["reason"] = reason
    if intent:
        payload["intent"] = intent
    if confidence is not None:
        payload["confidence"] = float(confidence)
    if slots:
        payload["slots"] = dict(slots)
    if raw:
        payload["raw"] = dict(raw)
    if isinstance(meta, Mapping) and meta:
        payload["_meta"] = dict(meta)
    try:
        bus_emit(ctx.bus, "nlu.trace.stage", payload, source="nlu.rasa")
    except Exception:
        pass


def _http_post_json(url: str, payload: dict, *, timeout_ms: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def _http_get_json(url: str, *, timeout_ms: int) -> dict | None:
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _service_health_ok(base_url: str) -> bool:
    payload = _http_get_json(f"{base_url}/health", timeout_ms=1_000)
    return bool(payload and payload.get("ok") is True)


async def _ensure_rasa_service_base_url(supervisor: Any) -> str | None:
    if not is_rasa_nlu_enabled():
        return None

    await supervisor.refresh_discovered(force=True)
    base_url = supervisor.resolve_base_url("rasa_nlu_service_skill")
    if not base_url:
        return None
    if base_url and await asyncio.to_thread(_service_health_ok, base_url):
        return base_url

    async with _START_LOCK:
        base_url = supervisor.resolve_base_url("rasa_nlu_service_skill")
        if not base_url:
            return None
        if base_url and await asyncio.to_thread(_service_health_ok, base_url):
            return base_url
        await supervisor.start("rasa_nlu_service_skill")
        return supervisor.resolve_base_url("rasa_nlu_service_skill")


def _record_failure(kind: str) -> int:
    now = asyncio.get_running_loop().time()
    times = _issue_times.get(kind) or []
    window_s = _ISSUE_WINDOW_S if _ISSUE_WINDOW_S > 0 else 60.0
    times = [t for t in times if now - t <= window_s]
    times.append(now)
    _issue_times[kind] = times
    return len(times)


def _slots_from_entities(entities: Any) -> Dict[str, Any]:
    slots: Dict[str, Any] = {}
    if isinstance(entities, list):
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = ent.get("entity")
            value = ent.get("value")
            if isinstance(name, str) and name and value is not None:
                slots.setdefault(name, value)
    return slots


def _record_neural_fallback_outcome_safe(
    *,
    meta: Mapping[str, Any],
    request_id: str | None,
    result: Mapping[str, Any],
) -> None:
    if meta.get("neural_fallback") is not True:
        return
    try:
        record_neural_fallback_outcome(
            request_id=request_id,
            status="accepted" if result.get("ok") else "miss",
            reason=str(result.get("reason") or ("rasa_accepted" if result.get("ok") else "rasa_miss")),
            intent=result.get("intent") if isinstance(result.get("intent"), str) else None,
            confidence=result.get("confidence") if isinstance(result.get("confidence"), (int, float)) else None,
            via="rasa",
        )
    except Exception:
        _log.debug("failed to record downstream Rasa outcome for neural fallback", exc_info=True)


async def parse_text(
    text: str,
    *,
    webspace_id: str | None = None,
    request_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    meta = meta if isinstance(meta, Mapping) else {}

    if not is_rasa_nlu_enabled():
        return {"ok": False, "reason": "rasa_disabled", "via": "rasa"}

    supervisor = get_service_supervisor()

    try:
        base_url = await _ensure_rasa_service_base_url(supervisor)
    except Exception:
        _log.warning("failed to start rasa_nlu_service_skill service", exc_info=True)
        return {"ok": False, "reason": "rasa_start_failed", "via": "rasa"}

    if not base_url:
        _log.debug("rasa service base_url unresolved")
        return {"ok": False, "reason": "rasa_base_url_unresolved", "via": "rasa"}

    try:
        async with _SEMAPHORE:
            future = asyncio.to_thread(
                _http_post_json,
                f"{base_url}/parse",
                {"text": text},
                timeout_ms=int(_PARSE_TIMEOUT_S * 1000),
            )
            data = await asyncio.wait_for(future, timeout=_PARSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        count = _record_failure("timeout")
        if count >= max(_ISSUE_THRESHOLD, 1):
            _log.warning("rasa service parse timed out (x%d) timeout_s=%.1f", count, _PARSE_TIMEOUT_S)
            try:
                await supervisor.inject_issue(
                    "rasa_nlu_service_skill",
                    issue_type="rasa_timeout",
                    message="rasa parse timed out",
                    details={"timeout_s": _PARSE_TIMEOUT_S, "text": text, "request_id": request_id, "count": count},
                )
            except Exception:
                pass
        else:
            _log.debug("rasa service parse timed out (x%d) text=%r", count, text)
        return {"ok": False, "reason": "rasa_timeout", "via": "rasa"}
    except Exception:
        count = _record_failure("failed")
        if count >= max(_ISSUE_THRESHOLD, 1):
            _log.warning("rasa service parse failed (x%d) text=%r", count, text, exc_info=True)
            try:
                await supervisor.inject_issue(
                    "rasa_nlu_service_skill",
                    issue_type="rasa_failed",
                    message="rasa parse failed",
                    details={"text": text, "request_id": request_id, "count": count},
                )
            except Exception:
                pass
        else:
            _log.debug("rasa service parse failed (x%d) text=%r", count, text, exc_info=True)
        return {"ok": False, "reason": "rasa_failed", "via": "rasa"}

    if not isinstance(data, dict) or not data.get("ok"):
        _log.debug("rasa parse returned not-ok: %r", data)
        return {"ok": False, "reason": "rasa_not_ok", "via": "rasa", "raw": data if isinstance(data, dict) else {}}

    result = data.get("result") or {}
    if not isinstance(result, dict):
        _log.debug("rasa parse returned invalid result: %r", result)
        return {"ok": False, "reason": "rasa_invalid_result", "via": "rasa"}

    intent_block = result.get("intent") or {}
    intent_name = intent_block.get("name") if isinstance(intent_block, dict) else None
    confidence = intent_block.get("confidence") if isinstance(intent_block, dict) else None
    entities = result.get("entities") or []
    ranking = result.get("intent_ranking") or []
    slots = _slots_from_entities(entities)

    base: Dict[str, Any] = {
        "via": "rasa",
        "intent": intent_name if isinstance(intent_name, str) else None,
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "slots": slots,
        "entities": entities if isinstance(entities, list) else [],
        "intent_ranking": ranking if isinstance(ranking, list) else [],
        "raw": result,
    }
    if not isinstance(intent_name, str) or not intent_name.strip():
        return {"ok": False, "reason": "rasa_no_intent", **base}
    if isinstance(confidence, (int, float)) and float(confidence) < _MIN_CONFIDENCE:
        return {"ok": False, "reason": "rasa_low_confidence", **base}
    return {"ok": True, **base}


async def _parse_and_emit(
    *,
    text: str,
    webspace_id: str | None,
    request_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> None:
    ctx = get_ctx()
    meta = meta if isinstance(meta, Mapping) else {}

    if not is_rasa_nlu_enabled():
        _emit_stage(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, status="skipped", reason="rasa_disabled")
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_disabled")
        return

    result = await parse_text(text, webspace_id=webspace_id, request_id=request_id, meta=meta)
    _record_neural_fallback_outcome_safe(meta=meta, request_id=request_id, result=result)
    if not result.get("ok"):
        _emit_stage(
            ctx=ctx,
            text=text,
            webspace_id=webspace_id,
            request_id=request_id,
            meta=meta,
            status="miss",
            reason=str(result.get("reason") or "rasa_failed"),
            intent=result.get("intent") if isinstance(result.get("intent"), str) else None,
            confidence=result.get("confidence") if isinstance(result.get("confidence"), (int, float)) else None,
            slots=result.get("slots") if isinstance(result.get("slots"), Mapping) else None,
            raw=result.get("raw") if isinstance(result.get("raw"), Mapping) else None,
        )
        _emit_not_obtained(
            ctx=ctx,
            text=text,
            webspace_id=webspace_id,
            request_id=request_id,
            meta=meta,
            reason=str(result.get("reason") or "rasa_failed"),
        )
        return

    intent_name = str(result.get("intent") or "").strip()
    confidence = result.get("confidence")
    slots = result.get("slots") if isinstance(result.get("slots"), dict) else {}
    raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    _emit_stage(
        ctx=ctx,
        text=text,
        webspace_id=webspace_id,
        request_id=request_id,
        meta=meta,
        status="hit",
        intent=intent_name,
        confidence=confidence if isinstance(confidence, (int, float)) else None,
        slots=slots,
        raw=raw,
    )

    detected_payload: Dict[str, Any] = {
        "intent": intent_name,
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "slots": slots,
        "text": text,
        "_raw": raw,
        "_meta": dict(meta) if isinstance(meta, Mapping) else {},
    }
    if webspace_id:
        detected_payload["webspace_id"] = webspace_id

    if request_id:
        detected_payload["request_id"] = request_id
    detected_payload["via"] = "rasa"
    bus_emit(ctx.bus, "nlp.intent.detected", detected_payload, source="nlu.rasa")


@subscribe("nlp.intent.detect.rasa")
async def _on_nlp_intent_detect(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    webspace_id = _resolve_webspace_id(payload)
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    asyncio.create_task(
        _parse_and_emit(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta),
        name=f"adaos-nlu-rasa-parse:{request_id or 'noid'}",
    )
