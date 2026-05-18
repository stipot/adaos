from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.services.runtime_paths import current_state_dir

_LOCK = threading.RLock()
_RECENT_MAX = int(os.getenv("ADAOS_NLU_NEURAL_STATS_RECENT_MAX", "100") or "100")
_SAMPLES_MAX = int(os.getenv("ADAOS_NLU_NEURAL_STATS_SAMPLES_MAX", "50") or "50")


def neural_usage_stats_path() -> Path:
    return (current_state_dir() / "nlu" / "neural_usage.json").resolve()


def _empty_stats() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": 0.0,
        "totals": {
            "requests": 0,
            "fallback_to_rasa": 0,
        },
        "by_status": {},
        "by_reason": {},
        "by_intent": {},
        "confidence_bands": {},
        "canonicalization": {},
        "latency_ms": {
            "count": 0,
            "sum": 0.0,
            "min": None,
            "max": None,
            "avg": None,
            "last": None,
        },
        "recent": [],
        "review_samples": [],
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_stats()
    if not isinstance(payload, dict):
        return _empty_stats()
    merged = _empty_stats()
    for key, value in payload.items():
        merged[key] = value
    for key, value in _empty_stats().items():
        if key not in merged:
            merged[key] = value
    return merged


def read_neural_usage_stats(path: Path | None = None) -> dict[str, Any]:
    return _read_json(path or neural_usage_stats_path())


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _bump(container: dict[str, Any], key: str, amount: int = 1) -> None:
    token = str(key or "unknown").strip() or "unknown"
    container[token] = int(container.get(token) or 0) + int(amount)


def _confidence_band(confidence: float | None) -> str | None:
    if confidence is None:
        return None
    value = max(0.0, min(1.0, float(confidence)))
    if value < 0.25:
        return "0.00-0.24"
    if value < 0.45:
        return "0.25-0.44"
    if value < 0.80:
        return "0.45-0.79"
    return "0.80-1.00"


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _canonicalization_bucket(entity_resolution: Mapping[str, Any] | None) -> str:
    if not isinstance(entity_resolution, Mapping) or not entity_resolution:
        return "unknown"
    ambiguities = entity_resolution.get("ambiguities")
    if isinstance(ambiguities, list) and ambiguities:
        return "ambiguity"
    unresolved = entity_resolution.get("unresolved_entity_spans")
    if isinstance(unresolved, list) and unresolved:
        return "unresolved"
    resolved = entity_resolution.get("resolved_entities")
    normalized = entity_resolution.get("normalized_text")
    if (isinstance(resolved, list) and resolved) or (isinstance(normalized, str) and "{" in normalized and "}" in normalized):
        return "hit"
    return "miss"


def _trim(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return items
    return items[-limit:]


def record_neural_usage(
    *,
    status: str,
    reason: str | None = None,
    text: str | None = None,
    webspace_id: str | None = None,
    request_id: str | None = None,
    intent: str | None = None,
    confidence: float | None = None,
    latency_ms: float | None = None,
    model_id: str | None = None,
    entity_resolution: Mapping[str, Any] | None = None,
    fallback_to_rasa: bool = False,
) -> dict[str, Any]:
    """
    Persist compact aggregate Neural NLU usage statistics.

    The file is intentionally node-local runtime state. It is used for rollout
    decisions and Teacher review hints, not as a source-controlled artifact.
    """
    normalized_status = str(status or "unknown").strip() or "unknown"
    normalized_reason = str(reason or normalized_status).strip() or normalized_status
    confidence_value = _float_or_none(confidence)
    latency_value = _float_or_none(latency_ms)
    canonicalization = _canonicalization_bucket(entity_resolution)
    now = time.time()

    path = neural_usage_stats_path()
    with _LOCK:
        stats = _read_json(path)
        stats["schema_version"] = 1
        stats["updated_at"] = now

        totals = stats.setdefault("totals", {})
        totals["requests"] = int(totals.get("requests") or 0) + 1
        if fallback_to_rasa:
            totals["fallback_to_rasa"] = int(totals.get("fallback_to_rasa") or 0) + 1
        requests = max(1, int(totals.get("requests") or 1))
        totals["fallback_ratio"] = round(float(totals.get("fallback_to_rasa") or 0) / requests, 6)

        _bump(stats.setdefault("by_status", {}), normalized_status)
        _bump(stats.setdefault("by_reason", {}), normalized_reason)
        _bump(stats.setdefault("canonicalization", {}), canonicalization)

        band = _confidence_band(confidence_value)
        if band:
            _bump(stats.setdefault("confidence_bands", {}), band)

        if intent:
            by_intent = stats.setdefault("by_intent", {})
            intent_key = str(intent).strip()
            if intent_key:
                entry = by_intent.setdefault(intent_key, {})
                _bump(entry, normalized_status)

        latency = stats.setdefault("latency_ms", {})
        if latency_value is not None:
            count = int(latency.get("count") or 0) + 1
            total = float(latency.get("sum") or 0.0) + max(0.0, latency_value)
            last = round(max(0.0, latency_value), 3)
            old_min = latency.get("min")
            old_max = latency.get("max")
            latency["count"] = count
            latency["sum"] = round(total, 3)
            latency["last"] = last
            latency["min"] = last if not isinstance(old_min, (int, float)) else round(min(float(old_min), last), 3)
            latency["max"] = last if not isinstance(old_max, (int, float)) else round(max(float(old_max), last), 3)
            latency["avg"] = round(total / count, 3)

        recent_item = {
            "ts": now,
            "status": normalized_status,
            "reason": normalized_reason,
            "intent": str(intent).strip() if isinstance(intent, str) and intent.strip() else None,
            "confidence": round(confidence_value, 6) if confidence_value is not None else None,
            "latency_ms": round(latency_value, 3) if latency_value is not None else None,
            "model_id": str(model_id).strip() if isinstance(model_id, str) and model_id.strip() else None,
            "webspace_id": str(webspace_id).strip() if isinstance(webspace_id, str) and webspace_id.strip() else None,
            "request_id": str(request_id).strip() if isinstance(request_id, str) and request_id.strip() else None,
            "canonicalization": canonicalization,
            "fallback_to_rasa": bool(fallback_to_rasa),
        }
        recent = stats.setdefault("recent", [])
        if not isinstance(recent, list):
            recent = []
            stats["recent"] = recent
        recent.append(recent_item)
        stats["recent"] = _trim(recent, _RECENT_MAX)

        if normalized_status != "accepted" and isinstance(text, str) and text.strip():
            samples = stats.setdefault("review_samples", [])
            if not isinstance(samples, list):
                samples = []
                stats["review_samples"] = samples
            samples.append({**recent_item, "text": text.strip()})
            stats["review_samples"] = _trim(samples, _SAMPLES_MAX)

        _write_json(path, stats)
        return stats


__all__ = [
    "neural_usage_stats_path",
    "read_neural_usage_stats",
    "record_neural_usage",
]
