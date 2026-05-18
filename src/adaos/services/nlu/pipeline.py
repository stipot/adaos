from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.yjs.doc import async_read_ydoc
from adaos.services.yjs.webspace import default_webspace_id

from .ycoerce import coerce_dict, iter_mappings
from .regex_usage_runtime import record_regex_rule_hit

_log = logging.getLogger("adaos.nlu.pipeline")

_RECENT_TTL_S = 60.0
_recent: dict[str, float] = {}

# NOTE: Keep patterns ASCII-safe by using explicit unicode escapes.
# "погода" = \u043f\u043e\u0433\u043e\u0434\u0430
# "какая"  = \u043a\u0430\u043a\u0430\u044f
# "в"      = \u0432
# "во"     = \u0432\u043e
_WEATHER_KEYWORD_RE = re.compile(r"\b(?:\u043f\u043e\u0433\u043e\u0434\u0430|weather)\b", re.IGNORECASE | re.UNICODE)
_WEATHER_CITY_RU_RE = re.compile(
    r"\b(?:\u043a\u0430\u043a\u0430\u044f\s+)?\u043f\u043e\u0433\u043e\u0434\u0430\b(?:\s+(?:\u0432|\u0432\u043e)\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)
_WEATHER_CITY_EN_RE = re.compile(
    r"\bweather\b(?:\s+in\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)

_RULES_CACHE_TTL_S = 2.0
_rules_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_rules_lock = asyncio.Lock()
_USE_NEURAL_STAGE = os.getenv("ADAOS_NLU_NEURAL", "0").strip().lower() in {"1", "true", "yes", "on"}


def invalidate_dynamic_regex_cache(*, webspace_id: str | None = None) -> None:
    if webspace_id is None:
        _rules_cache.clear()
        return
    _rules_cache.pop(str(webspace_id), None)


def describe_builtin_regex_rules() -> list[dict[str, Any]]:
    """
    A compact description of built-in regex rules used by the pipeline.

    Intended for observability / UI / LLM teacher context.
    """
    return [
        {
            "id": "builtin.weather.keyword",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_KEYWORD_RE.pattern,
            "notes": "Keyword gate for the built-in weather rule.",
        },
        {
            "id": "builtin.weather.ru",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_CITY_RU_RE.pattern,
            "notes": "RU weather queries, optional city captured as (?P<city>...).",
        },
        {
            "id": "builtin.weather.en",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_CITY_EN_RE.pattern,
            "notes": "EN weather queries, optional city captured as (?P<city>...).",
        },
    ]


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _request_locale(payload: Mapping[str, Any]) -> str | None:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    token = payload.get("request_locale") or payload.get("locale") or meta.get("request_locale") or meta.get("locale")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _preferred_locales(payload: Mapping[str, Any]) -> list[str]:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
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


def _request_id(payload: Mapping[str, Any], *, text: str, webspace_id: str) -> str:
    rid = payload.get("request_id") or payload.get("id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    seed = f"{webspace_id}:{text}:{payload.get('ts') or ''}"
    return "auto." + hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _seen_recent(rid: str) -> bool:
    now = time.time()
    if len(_recent) > 512:
        cutoff = now - _RECENT_TTL_S
        for k, ts in list(_recent.items()):
            if ts < cutoff:
                _recent.pop(k, None)
    ts = _recent.get(rid)
    if ts is not None and now - ts < _RECENT_TTL_S:
        return True
    _recent[rid] = now
    return False


def _clean_city(city: str | None) -> str | None:
    if not isinstance(city, str):
        return None
    value = city.strip().strip(" \t\r\n'\"()[]{}")
    return value if value else None


def _clean_slots(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if not isinstance(k, str) or not k:
            continue
        if not isinstance(v, str):
            continue
        cleaned = v.strip().strip(" \t\r\n'\"()[]{}")
        if cleaned:
            out[k] = cleaned
    return out


def _emit_stage(
    ctx: Any,
    *,
    stage: str,
    status: str,
    text: str,
    webspace_id: str,
    request_id: str,
    via: str | None = None,
    intent: str | None = None,
    confidence: float | None = None,
    slots: Mapping[str, Any] | None = None,
    reason: str | None = None,
    raw: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "text": text,
        "webspace_id": webspace_id,
        "request_id": request_id,
    }
    if via:
        payload["via"] = via
    if intent:
        payload["intent"] = intent
    if confidence is not None:
        payload["confidence"] = float(confidence)
    if slots:
        payload["slots"] = dict(slots)
    if reason:
        payload["reason"] = reason
    if raw:
        payload["raw"] = dict(raw)
    if meta:
        payload["_meta"] = dict(meta)
    try:
        bus_emit(ctx.bus, "nlu.trace.stage", payload, source="nlu.pipeline")
    except Exception:
        pass


async def _resolve_current_scenario_id(webspace_id: str) -> str | None:
    try:
        async with async_read_ydoc(webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            token = ui_map.get("current_scenario")
    except Exception:
        return None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _iter_rules_from_scenario(scenario_id: str) -> list[dict[str, Any]]:
    try:
        content = scenarios_loader.read_content(scenario_id)
    except Exception:
        return []
    if not isinstance(content, dict):
        return []
    nlu = content.get("nlu")
    if not isinstance(nlu, dict):
        return []
    rules = nlu.get("regex_rules")
    return [dict(x) for x in rules if isinstance(x, dict)] if isinstance(rules, list) else []


def _iter_rules_from_all_scenarios() -> list[dict[str, Any]]:
    ctx = get_ctx()
    root = Path(ctx.paths.scenarios_dir())
    out: list[dict[str, Any]] = []
    try:
        dirs = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        return out
    for d in dirs:
        sid = d.name
        for rule in _iter_rules_from_scenario(sid):
            out.append({**dict(rule), "scenario_id": sid})
    return out


def _iter_rules_from_skills() -> list[dict[str, Any]]:
    ctx = get_ctx()
    skills_dir = Path(ctx.paths.skills_dir())
    out: list[dict[str, Any]] = []
    try:
        candidates = list(skills_dir.glob("*/skill.yaml"))
    except Exception:
        return out

    for path in candidates:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        nlu = payload.get("nlu")
        if not isinstance(nlu, dict):
            continue
        rules = nlu.get("regex_rules")
        if not isinstance(rules, list):
            continue
        for item in rules:
            if isinstance(item, dict):
                out.append(dict(item))
    return out


async def _load_dynamic_regex_rules(webspace_id: str) -> list[dict[str, Any]]:
    """
    Load compiled regex rules for the given webspace.

    Primary storage (workspace):
      - scenario.json:nlu.regex_rules
      - skill.yaml:nlu.regex_rules

    Backward-compatible storage (per-webspace/YJS):
      - data.nlu.regex_rules
    """
    now = time.time()
    cached = _rules_cache.get(webspace_id)
    if cached and now - cached[0] < _RULES_CACHE_TTL_S:
        return cached[1]

    async with _rules_lock:
        cached = _rules_cache.get(webspace_id)
        if cached and now - cached[0] < _RULES_CACHE_TTL_S:
            return cached[1]

        compiled: list[dict[str, Any]] = []

        rules: list[dict[str, Any]] = []

        # Collect scenario rules from all installed workspace scenarios. If we can
        # resolve the active scenario for this webspace, we'll use it to scope
        # scenario-owned rules during matching.
        rules.extend(_iter_rules_from_all_scenarios())
        rules.extend(_iter_rules_from_skills())

        # Backward-compatible: per-webspace rules (will be deprecated).
        try:
            async with async_read_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                nlu_obj = data_map.get("nlu")
                nlu_obj = coerce_dict(nlu_obj)
                for item in iter_mappings(nlu_obj.get("regex_rules")):
                    rules.append(dict(item))
        except Exception:
            pass

        for item in rules:
            if not item.get("enabled", True):
                continue
            intent = item.get("intent")
            pattern = item.get("pattern")
            if not isinstance(intent, str) or not intent.strip():
                continue
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
            except re.error:
                continue
            compiled.append(
                {
                    "id": item.get("id"),
                    "intent": intent.strip(),
                    "pattern": pattern,
                    "rx": rx,
                    "scenario_id": item.get("scenario_id"),
                }
            )

        _rules_cache[webspace_id] = (now, compiled)
        return compiled


async def _try_regex_intent(text: str, *, webspace_id: str) -> tuple[str | None, dict, str, dict]:
    """
    Very small, fast regex stage (MVP).

    Goal: quickly extract intent/slots for weather queries without calling
    external interpreters.
    """
    # 1) Dynamic rules (LLM/teacher-applied) take precedence.
    current_scenario = await _resolve_current_scenario_id(webspace_id)
    for rule in await _load_dynamic_regex_rules(webspace_id):
        scoped = rule.get("scenario_id")
        if isinstance(scoped, str) and scoped and current_scenario and scoped != current_scenario:
            continue
        rx = rule.get("rx")
        if not isinstance(rx, re.Pattern):
            continue
        m = rx.search(text)
        if not m:
            continue
        intent = rule.get("intent")
        if not isinstance(intent, str) or not intent:
            continue
        slots = _clean_slots(m.groupdict())
        raw = {"rule_id": rule.get("id"), "pattern": rule.get("pattern"), "slots": slots}
        try:
            record_regex_rule_hit(
                webspace_id=webspace_id,
                scenario_id=current_scenario,
                rule_id=str(rule.get("id") or ""),
                intent=intent,
                pattern=str(rule.get("pattern") or ""),
                text=text,
                slots=slots,
                raw=raw,
                via="regex.dynamic",
            )
        except Exception:
            pass
        return (intent, slots, "regex.dynamic", raw)

    # 2) Built-in fallback (desktop weather MVP)
    if not _WEATHER_KEYWORD_RE.search(text):
        return (None, {}, "regex", {})

    city: str | None = None
    m_ru = _WEATHER_CITY_RU_RE.search(text)
    if m_ru:
        city = _clean_city(m_ru.group("city"))
    if city is None:
        m_en = _WEATHER_CITY_EN_RE.search(text)
        if m_en:
            city = _clean_city(m_en.group("city"))

    slots = {"city": city} if city else {}
    return ("desktop.open_weather", slots, "regex", {"builtin": "weather"})


@subscribe("nlp.intent.detect.request")
async def _on_detect_request(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    locale = _request_locale(payload)
    preferred_locales = _preferred_locales(payload)

    ctx = get_ctx()
    webspace_id = _resolve_webspace_id(payload)
    rid = _request_id(payload, text=text, webspace_id=webspace_id)
    if _seen_recent(rid):
        return

    intent, slots, via, raw = await _try_regex_intent(text, webspace_id=webspace_id)
    if intent:
        _emit_stage(
            ctx,
            stage="regex",
            status="hit",
            text=text,
            webspace_id=webspace_id,
            request_id=rid,
            via=via,
            intent=intent,
            confidence=1.0,
            slots=slots,
            raw=raw,
            meta=meta,
        )
        bus_emit(
            ctx.bus,
            "nlp.intent.detected",
            {
                "intent": intent,
                "confidence": 1.0,
                "slots": slots,
                "text": text,
                "webspace_id": webspace_id,
                "request_id": rid,
                "via": via,
                "_raw": raw,
                "_meta": meta,
            },
            source="nlu.pipeline",
        )
        return

    _emit_stage(
        ctx,
        stage="regex",
        status="miss",
        text=text,
        webspace_id=webspace_id,
        request_id=rid,
        via=via or "regex",
        reason="no_match",
        raw=raw,
        meta=meta,
    )
    downstream_event = "nlp.intent.detect.neural" if _USE_NEURAL_STAGE else "nlp.intent.detect.rasa"
    _emit_stage(
        ctx,
        stage="pipeline",
        status="delegate",
        text=text,
        webspace_id=webspace_id,
        request_id=rid,
        via="neural" if _USE_NEURAL_STAGE else "rasa",
        reason=downstream_event,
        meta=meta,
    )
    downstream_payload: dict[str, Any] = {"text": text, "webspace_id": webspace_id, "request_id": rid, "_meta": meta}
    if locale:
        downstream_payload["locale"] = locale
        downstream_payload["request_locale"] = locale
    if preferred_locales:
        downstream_payload["preferred_locales"] = preferred_locales
    bus_emit(ctx.bus, downstream_event, downstream_payload, source="nlu.pipeline")
