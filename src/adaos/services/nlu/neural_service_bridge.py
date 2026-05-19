from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.skill.service_supervisor import get_service_supervisor
from .entity_resolver_runtime import build_entity_trace_stage
from .neural_usage_stats import record_neural_usage

_log = logging.getLogger("adaos.nlu.neural")

_SEMAPHORE = asyncio.Semaphore(2)
_START_LOCK = asyncio.Lock()
_PARSE_TIMEOUT_S = float(os.getenv("ADAOS_NLU_NEURAL_TIMEOUT_S", "6.0") or "6.0")
_ACCEPT_CONFIDENCE = float(
    os.getenv("ADAOS_NLU_NEURAL_ACCEPT_CONFIDENCE", os.getenv("ADAOS_NLU_NEURAL_MIN_CONFIDENCE", "0.80")) or "0.80"
)
_REJECT_CONFIDENCE = float(os.getenv("ADAOS_NLU_NEURAL_REJECT_CONFIDENCE", "0.45") or "0.45")


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
        token = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(token, str) and token.strip():
            return token.strip()
    token = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _request_locale(payload: Mapping[str, Any]) -> str | None:
    meta = payload.get("_meta") or {}
    if isinstance(meta, Mapping):
        token = meta.get("request_locale") or meta.get("locale")
        if isinstance(token, str) and token.strip():
            return token.strip()
    token = payload.get("request_locale") or payload.get("locale")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _preferred_locales(payload: Mapping[str, Any]) -> list[str]:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    raw = payload.get("preferred_locales") or meta.get("preferred_locales")
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    for item in items:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


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


def _emit_rasa_fallback(
    *,
    text: str,
    webspace_id: str | None,
    request_id: str | None,
    meta: Mapping[str, Any],
    neural_reason: str | None = None,
    locale: str | None = None,
    preferred_locales: list[str] | None = None,
) -> None:
    ctx = get_ctx()
    payload: Dict[str, Any] = {"text": text}
    if webspace_id:
        payload["webspace_id"] = webspace_id
    if request_id:
        payload["request_id"] = request_id
    if locale:
        payload["locale"] = locale
        payload["request_locale"] = locale
    if preferred_locales:
        payload["preferred_locales"] = list(preferred_locales)
    next_meta = dict(meta) if isinstance(meta, Mapping) else {}
    next_meta["neural_fallback"] = True
    if neural_reason:
        next_meta["neural_fallback_reason"] = neural_reason
    payload["_meta"] = next_meta
    bus_emit(ctx.bus, "nlp.intent.detect.rasa", payload, source="nlu.neural")


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
        "stage": "neural",
        "status": status,
        "text": text,
        "via": "neural",
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
        bus_emit(ctx.bus, "nlu.trace.stage", payload, source="nlu.neural")
    except Exception:
        pass


def _record_usage_safe(
    *,
    status: str,
    reason: str | None,
    text: str,
    webspace_id: str | None,
    request_id: str | None,
    intent: str | None = None,
    confidence: float | None = None,
    latency_ms: float | None = None,
    model_id: str | None = None,
    entity_resolution: Mapping[str, Any] | None = None,
    fallback_to_rasa: bool = False,
) -> None:
    try:
        record_neural_usage(
            status=status,
            reason=reason,
            text=text,
            webspace_id=webspace_id,
            request_id=request_id,
            intent=intent,
            confidence=confidence,
            latency_ms=latency_ms,
            model_id=model_id,
            entity_resolution=entity_resolution,
            fallback_to_rasa=fallback_to_rasa,
        )
    except Exception:
        _log.debug("failed to record neural usage stats", exc_info=True)


def _entity_resolution_for_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    existing = payload.get("entity_resolution") or payload.get("entities")
    if isinstance(existing, Mapping):
        return dict(existing)
    try:
        stage = build_entity_trace_stage(payload, include_miss=True)
    except Exception:
        _log.debug("failed to build neural entity-resolution payload", exc_info=True)
        return {}
    if not isinstance(stage, Mapping):
        return {}
    raw = stage.get("raw")
    return dict(raw) if isinstance(raw, Mapping) else {}


async def _ensure_neural_service_base_url(supervisor: Any) -> str | None:
    await supervisor.refresh_discovered(force=True)
    base_url = supervisor.resolve_base_url("neural_nlu_service_skill")
    if not base_url:
        return None
    async with _START_LOCK:
        base_url = supervisor.resolve_base_url("neural_nlu_service_skill")
        if not base_url:
            return None
        await supervisor.start("neural_nlu_service_skill")
        return supervisor.resolve_base_url("neural_nlu_service_skill")


def _neural_artifact_root() -> Path:
    try:
        state_dir = Path(get_ctx().paths.state_dir()).expanduser().resolve()
        return state_dir / "nlu" / "neural"
    except Exception:
        base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
        if base_dir:
            return Path(base_dir).expanduser().resolve() / "state" / "nlu" / "neural"
        return Path.home() / ".adaos" / "state" / "nlu" / "neural"


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0}
    try:
        stat = path.stat()
    except Exception:
        return {"exists": True, "size": None}
    return {"exists": True, "size": int(stat.st_size), "mtime": float(stat.st_mtime)}


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _artifact_readiness(root: Path) -> dict[str, Any]:
    required = [
        "model.pt",
        "labels.json",
        "vocab.json",
        "examples_manifest.jsonl",
        "ranker_config.json",
        "metrics.json",
    ]
    optional = [
        "intents_manifest.json",
        "intent_map.json",
        "faiss.index",
        "faiss.index.json",
        "example_index.pt",
        "golden_report.json",
    ]
    files = {name: _file_snapshot(root / name) for name in [*required, *optional]}
    missing_required = [name for name in required if not bool(files[name].get("exists"))]
    faiss_ready = bool(files["faiss.index"].get("exists") and files["faiss.index.json"].get("exists"))
    torch_ready = bool(files["example_index.pt"].get("exists"))
    index_backend = "faiss" if faiss_ready else "torch_tensor" if torch_ready else None
    metrics = _read_json_file(root / "metrics.json") or {}
    golden = _read_json_file(root / "golden_report.json") or {}
    warnings: list[str] = []
    if not index_backend and not missing_required:
        warnings.append("example_index_missing_until_first_model_load")
    if faiss_ready and torch_ready:
        warnings.append("both_faiss_and_torch_indexes_present")
    return {
        "ok": not missing_required,
        "root": str(root),
        "missing_required": missing_required,
        "index_backend": index_backend,
        "files": files,
        "metrics": {
            "model_id": metrics.get("model_id"),
            "model_sha256": metrics.get("model_sha256"),
            "examples_total": metrics.get("examples_total"),
            "labels_total": metrics.get("labels_total"),
            "created_at": metrics.get("created_at"),
        },
        "golden_report": {
            "exists": bool(golden),
            "accuracy": golden.get("accuracy"),
            "passed": golden.get("passed"),
            "total": golden.get("total"),
        },
        "warnings": warnings,
    }


async def diagnose_readiness(*, start_service: bool = False, stop_after: bool = False) -> Dict[str, Any]:
    """Return operator-facing readiness for artifacts, service discovery, and health."""
    artifact_root = _neural_artifact_root()
    artifacts = _artifact_readiness(artifact_root)
    supervisor = get_service_supervisor()
    service: dict[str, Any] = {
        "name": "neural_nlu_service_skill",
        "installed": False,
        "base_url": None,
        "health": None,
    }
    warnings = list(artifacts.get("warnings") or [])
    try:
        await supervisor.refresh_discovered(force=True)
        base_url = supervisor.resolve_base_url("neural_nlu_service_skill")
    except Exception as exc:
        base_url = None
        warnings.append(f"service_discovery_failed:{type(exc).__name__}")

    if base_url:
        service["installed"] = True
        service["base_url"] = base_url
        if start_service:
            try:
                await supervisor.start("neural_nlu_service_skill")
                service["base_url"] = supervisor.resolve_base_url("neural_nlu_service_skill") or base_url
            except Exception as exc:
                warnings.append(f"service_start_failed:{type(exc).__name__}")
        if start_service:
            service["health"] = _http_get_json(f"{service['base_url']}/health", timeout_ms=2_000)
    else:
        warnings.append("service_base_url_unresolved")

    if stop_after and start_service:
        try:
            await supervisor.stop("neural_nlu_service_skill")
        except Exception as exc:
            warnings.append(f"service_stop_failed:{type(exc).__name__}")

    health = service.get("health") if isinstance(service.get("health"), Mapping) else {}
    health_ok = bool(health.get("ok")) if start_service else True
    model_loaded = bool(health.get("model_loaded")) if start_service else True
    ok = bool(artifacts.get("ok")) and bool(service.get("installed")) and health_ok and model_loaded
    return {
        "ok": ok,
        "artifacts": artifacts,
        "service": service,
        "checks": {
            "artifacts": bool(artifacts.get("ok")),
            "service_installed": bool(service.get("installed")),
            "health_ok": health_ok,
            "model_loaded": model_loaded,
        },
        "warnings": warnings,
    }


async def parse_text(
    text: str,
    *,
    webspace_id: str | None = None,
    request_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
    locale: str | None = None,
    preferred_locales: list[str] | tuple[str, ...] | None = None,
    entity_resolution: Mapping[str, Any] | None = None,
    record_usage_stats: bool = True,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    clean_text = str(text or "").strip()
    if not clean_text:
        return {"ok": False, "accepted": False, "reason": "empty_text", "via": "neural"}

    meta = meta if isinstance(meta, Mapping) else {}
    preferred = [str(item).strip() for item in list(preferred_locales or []) if str(item).strip()]
    if isinstance(entity_resolution, Mapping):
        entity_payload = dict(entity_resolution)
    else:
        entity_payload = _entity_resolution_for_payload(
            {
                "text": clean_text,
                "webspace_id": webspace_id,
                "request_id": request_id,
                "request_locale": locale,
                "preferred_locales": preferred,
                "_meta": dict(meta) if isinstance(meta, Mapping) else {},
            }
        )

    def record_usage(
        *,
        status: str,
        reason: str | None,
        intent: str | None = None,
        confidence: float | None = None,
        model_id: str | None = None,
        fallback_to_rasa: bool = False,
    ) -> None:
        if not record_usage_stats:
            return
        _record_usage_safe(
            status=status,
            reason=reason,
            text=clean_text,
            webspace_id=webspace_id,
            request_id=request_id,
            intent=intent,
            confidence=confidence,
            latency_ms=(time.perf_counter() - started_at) * 1000.0,
            model_id=model_id,
            entity_resolution=entity_payload,
            fallback_to_rasa=fallback_to_rasa,
        )

    supervisor = get_service_supervisor()
    try:
        base_url = await _ensure_neural_service_base_url(supervisor)
    except KeyError:
        record_usage(status="unavailable", reason="neural_service_not_installed", fallback_to_rasa=True)
        return {
            "ok": False,
            "accepted": False,
            "reason": "neural_service_not_installed",
            "via": "neural",
            "fallback_to_rasa": True,
            "entity_resolution": entity_payload,
        }
    except Exception:
        _log.warning("failed to start neural_nlu_service_skill", exc_info=True)
        record_usage(status="error", reason="neural_start_failed", fallback_to_rasa=True)
        return {
            "ok": False,
            "accepted": False,
            "reason": "neural_start_failed",
            "via": "neural",
            "fallback_to_rasa": True,
            "entity_resolution": entity_payload,
        }

    if not base_url:
        record_usage(status="unavailable", reason="neural_base_url_unresolved", fallback_to_rasa=True)
        return {
            "ok": False,
            "accepted": False,
            "reason": "neural_base_url_unresolved",
            "via": "neural",
            "fallback_to_rasa": True,
            "entity_resolution": entity_payload,
        }

    req_payload: Dict[str, Any] = {"text": clean_text}
    if webspace_id:
        req_payload["webspace_id"] = webspace_id
    if locale:
        req_payload["locale"] = locale
        req_payload["request_locale"] = locale
    if preferred:
        req_payload["preferred_locales"] = list(preferred)
    if entity_payload:
        req_payload["entities"] = entity_payload
        normalized = entity_payload.get("normalized_text")
        if isinstance(normalized, str) and normalized.strip():
            req_payload["canonicalized_text"] = normalized.strip()

    try:
        async with _SEMAPHORE:
            future = asyncio.to_thread(
                _http_post_json,
                f"{base_url}/parse",
                req_payload,
                timeout_ms=int(_PARSE_TIMEOUT_S * 1000),
            )
            data = await asyncio.wait_for(future, timeout=_PARSE_TIMEOUT_S)
    except Exception:
        _log.debug("neural parse failed", exc_info=True)
        record_usage(status="error", reason="neural_parse_failed", fallback_to_rasa=True)
        return {
            "ok": False,
            "accepted": False,
            "reason": "neural_parse_failed",
            "via": "neural",
            "fallback_to_rasa": True,
            "entity_resolution": entity_payload,
        }

    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, Mapping):
        result = data if isinstance(data, Mapping) else {}

    top_intent = result.get("top_intent") or result.get("intent")
    confidence = result.get("confidence")
    confidence_val = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    model_id = result.get("model_id") if isinstance(result.get("model_id"), str) else None
    slots = dict(result.get("slots")) if isinstance(result.get("slots"), Mapping) else {}

    base_result: Dict[str, Any] = {
        "via": "neural",
        "raw": dict(result) if isinstance(result, Mapping) else {},
        "slots": slots,
        "confidence": confidence_val,
        "model_id": model_id,
        "entity_resolution": entity_payload,
    }
    if not isinstance(top_intent, str) or not top_intent.strip():
        record_usage(
            status="abstained",
            reason="neural_abstained",
            confidence=confidence_val if isinstance(confidence, (int, float)) else None,
            model_id=model_id,
            fallback_to_rasa=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "reason": "neural_abstained",
            "fallback_to_rasa": True,
            **base_result,
        }

    intent = top_intent.strip()
    if confidence_val < _ACCEPT_CONFIDENCE:
        reason = "neural_rejected" if confidence_val < _REJECT_CONFIDENCE else "neural_low_confidence"
        record_usage(
            status="rejected" if confidence_val < _REJECT_CONFIDENCE else "low_confidence",
            reason=reason,
            intent=intent,
            confidence=confidence_val,
            model_id=model_id,
            fallback_to_rasa=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "reason": reason,
            "intent": intent,
            "fallback_to_rasa": True,
            **base_result,
        }

    record_usage(
        status="accepted",
        reason="neural_accepted",
        intent=intent,
        confidence=confidence_val,
        model_id=model_id,
        fallback_to_rasa=False,
    )
    return {
        "ok": True,
        "accepted": True,
        "intent": intent,
        "fallback_to_rasa": False,
        **base_result,
    }


@subscribe("nlp.intent.detect.neural")
async def _on_nlp_intent_detect_neural(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    webspace_id = _resolve_webspace_id(payload)
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    locale = _request_locale(payload)
    preferred_locales = _preferred_locales(payload)
    ctx = get_ctx()
    entity_resolution = _entity_resolution_for_payload(
        {
            **dict(payload),
            "text": text,
            "webspace_id": webspace_id,
            "request_id": request_id,
            "request_locale": locale,
            "preferred_locales": preferred_locales,
        }
    )
    result = await parse_text(
        text,
        webspace_id=webspace_id,
        request_id=request_id,
        meta=meta,
        locale=locale,
        preferred_locales=preferred_locales,
        entity_resolution=entity_resolution,
        record_usage_stats=True,
    )

    raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
    slots = dict(result.get("slots")) if isinstance(result.get("slots"), Mapping) else {}
    confidence = result.get("confidence")
    confidence_val = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    intent = str(result.get("intent") or "").strip()

    if not result.get("ok"):
        _emit_stage(
            ctx=ctx,
            text=text,
            webspace_id=webspace_id,
            request_id=request_id,
            meta=meta,
            status="miss",
            reason=str(result.get("reason") or "neural_failed"),
            intent=intent or None,
            confidence=confidence_val if isinstance(confidence, (int, float)) else None,
            slots=slots,
            raw=raw,
        )
        _emit_rasa_fallback(
            text=text,
            webspace_id=webspace_id,
            request_id=request_id,
            meta=meta,
            neural_reason=str(result.get("reason") or "neural_failed"),
            locale=locale,
            preferred_locales=preferred_locales,
        )
        return

    out: Dict[str, Any] = {
        "intent": intent,
        "confidence": confidence_val,
        "slots": slots,
        "text": text,
        "via": "neural",
        "_raw": dict(raw),
    }
    if webspace_id:
        out["webspace_id"] = webspace_id
    if request_id:
        out["request_id"] = request_id
    if isinstance(meta, Mapping) and meta:
        out["_meta"] = dict(meta)

    _emit_stage(
        ctx=ctx,
        text=text,
        webspace_id=webspace_id,
        request_id=request_id,
        meta=meta,
        status="hit",
        intent=intent,
        confidence=confidence_val,
        slots=slots,
        raw=raw,
    )
    bus_emit(ctx.bus, "nlp.intent.detected", out, source="nlu.neural")
