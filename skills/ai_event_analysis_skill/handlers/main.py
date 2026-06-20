from __future__ import annotations

import json
import re
import time
import asyncio
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data.events import publish as publish_event
from adaos.sdk.io.out import stream_publish

_RESULTS_RECEIVER = "ai_event_analysis.results"
_SKILL_ROOT = Path(__file__).resolve().parents[1]
_DATA_DIR = _SKILL_ROOT / "data"
_DEFAULT_EXPORT_PATH = _DATA_DIR / "event_windows.jsonl"
_MAX_AUTO_LOG_SOURCES = 4
_MAX_DEFAULT_LOG_LINES = 120
_MAX_EXPLICIT_LOG_LINES = 1000
_MAX_TOOL_WINDOWS = 64
_MAX_STREAM_ROWS = 12
_MAX_STREAM_POINTS = 24
_MAX_EVIDENCE_PER_WINDOW = 4
_MAX_SUBSCRIPTION_ROWS = 64
_TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)

_CLASSES = [
    "normal",
    "eventbus_backpressure",
    "projection_refresh_storm",
    "yjs_write_pressure",
    "browser_session_instability",
    "member_node_disconnect",
    "runtime_rebuild_churn",
]
_PROJECTION_FINGERPRINTS: dict[str, str] = {}
_PROJECTION_RULES_LOADED = False
_PROJECTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-event-analysis-projection")


class _LazyCtxSubnet:
    def set(self, *args: Any, **kwargs: Any) -> Any:
        from adaos.sdk.data import ctx_subnet as real_ctx_subnet

        return real_ctx_subnet.set(*args, **kwargs)


ctx_subnet = _LazyCtxSubnet()


def _ensure_projection_rules_loaded() -> None:
    global _PROJECTION_RULES_LOADED
    if _PROJECTION_RULES_LOADED:
        return
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        registry = getattr(ctx, "projections", None)
        load_entries = getattr(registry, "load_entries", None)
        if not callable(load_entries):
            return
        load_entries(_skill_projection_entries())
        _PROJECTION_RULES_LOADED = True
    except Exception:
        # Test and validation contexts may not bootstrap AgentContext.
        return


def _skill_projection_entries() -> list[dict[str, Any]]:
    try:
        import yaml

        manifest = yaml.safe_load((_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")) or {}
        entries = manifest.get("data_projections") or []
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


def _apply_projection(slot: str, value: Any, *, webspace_id: str) -> None:
    entries = _skill_projection_entries()
    if entries:
        try:
            from adaos.services.agent_context import get_ctx
            from adaos.services.scenario.projection_registry import ProjectionRegistry
            from adaos.services.scenario.projection_service import ProjectionService

            ctx = get_ctx()
            registry = ProjectionRegistry()
            registry.load_entries(entries)
            service = ProjectionService(ctx=ctx, registry=registry)

            async def _runner() -> None:
                await service.apply("subnet", slot, value, webspace_id=webspace_id)

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_runner())
            else:
                _PROJECTION_EXECUTOR.submit(lambda: asyncio.run(_runner())).result()
            return
        except Exception:
            pass

    _ensure_projection_rules_loaded()
    ctx_subnet.set(slot, value, webspace_id=webspace_id)


def _webspace_id_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return "desktop"
    raw = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(raw, str) and raw.strip() and not raw.strip().startswith("$"):
        return raw.strip()
    meta = payload.get("_meta")
    if isinstance(meta, Mapping):
        nested = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(nested, str) and nested.strip() and not nested.strip().startswith("$"):
            return nested.strip()
    return "desktop"


def _fingerprint(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _set_projection_if_changed(slot: str, value: Any, *, webspace_id: str, force: bool = False) -> bool:
    key = f"{webspace_id}:{slot}"
    fingerprint = _fingerprint(value)
    if not force and _PROJECTION_FINGERPRINTS.get(key) == fingerprint:
        return False
    _apply_projection(slot, value, webspace_id=webspace_id)
    _PROJECTION_FINGERPRINTS[key] = fingerprint
    return True


def _project_sections(sections: Mapping[str, Any], *, webspace_id: str, force: bool = False) -> dict[str, Any]:
    slot_by_section = {
        "summary": "ai_event_analysis.summary",
        "task": "ai_event_analysis.task",
        "dataset": "ai_event_analysis.dataset",
        "windows": "ai_event_analysis.windows",
        "metrics": "ai_event_analysis.metrics",
        "per_class": "ai_event_analysis.per_class",
        "chart": "ai_event_analysis.chart",
        "event_volume_chart": "ai_event_analysis.event_volume_chart",
        "class_distribution_chart": "ai_event_analysis.class_distribution_chart",
        "subscription_summary": "ai_event_analysis.subscription_summary",
        "subscription_edges": "ai_event_analysis.subscription_edges",
        "subscription_metrics": "ai_event_analysis.subscription_metrics",
        "subscription_chart": "ai_event_analysis.subscription_chart",
        "experiments": "ai_event_analysis.experiments",
        "agent_summary": "ai_event_analysis.agent_summary",
        "agent_plan": "ai_event_analysis.agent_plan",
        "agent_gates": "ai_event_analysis.agent_gates",
        "progress": "ai_event_analysis.progress",
    }
    pushed = False
    written: list[str] = []
    try:
        pushed = bool(set_current_skill("ai_event_analysis_skill"))
    except Exception:
        pushed = False
    try:
        for section, slot in slot_by_section.items():
            if section not in sections:
                continue
            try:
                if _set_projection_if_changed(slot, sections[section], webspace_id=webspace_id, force=force):
                    written.append(section)
            except Exception:
                continue
    finally:
        if pushed:
            try:
                clear_current_skill()
            except Exception:
                pass
    return {"ok": True, "written": written, "webspace_id": webspace_id}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _progress_payload(*, label: str, percent: int, status: str, description: str) -> dict[str, Any]:
    bounded = max(0, min(100, int(percent)))
    title = f"{label} - {bounded}%"
    return {
        "label": label,
        "value": f"{bounded}%",
        "subtitle": status,
        "description": description,
        "percent": bounded,
        "status": status,
        "updatedAt": _now_iso(),
        "items": [
            {
                "id": "current-progress",
                "title": title,
                "description": f"{status}: {description}",
                "status": status,
                "percent": bounded,
            }
        ],
    }


def _mark_progress(*, webspace_id: str, label: str, percent: int, status: str, description: str) -> None:
    _project_sections(
        {"progress": _progress_payload(label=label, percent=percent, status=status, description=description)},
        webspace_id=webspace_id,
        force=True,
    )


def _publish_run_notice(*, webspace_id: str, title: str, description: str, stage: str = "running") -> None:
    try:
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": f"{stage}-{int(time.time() * 1000)}",
                    "title": title,
                    "description": description,
                    "content": {"stage": stage, "updated_at": _now_iso()},
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass


def _parse_ts(raw: str) -> float | None:
    match = _TS_RE.search(raw or "")
    if not match:
        return None
    value = match.group("ts").replace(",", ".").replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", value):
        value = value[:-5] + value[-5:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _severity_from_text(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("critical", "fatal", "traceback", "exception")):
        return "critical"
    if any(token in lowered for token in ("error", "failed", "failure")):
        return "error"
    if any(token in lowered for token in ("warning", "warn", "degraded", "retry")):
        return "warning"
    return "info"


def _topic_from_text(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("admin shutdown", "cli.restart", "api.takeover", "service stopped", "starting service", "shutdown hooks")):
        return "runtime.lifecycle"
    if any(token in lowered for token in ("session close", "connection closed", "connection open", "websocket", "/ws", "/yws", "yws connection")):
        return "browser.session"
    if any(token in lowered for token in ("drop", "supersede", "backpressure", "queue", "remote quarantine", "nats reconnect")):
        return "eventbus.pressure"
    if any(token in lowered for token in ("projection", "refresh", "materializ")):
        return "projection.lifecycle"
    if any(token in lowered for token in ("yjs owner flow", "yjs write pressure", "ydoc pressure", "syncchannel pressure")):
        return "yjs.sync"
    if any(token in lowered for token in ("member", "subnet", "node disconnect", "offline")):
        return "member.connectivity"
    if any(token in lowered for token in ("rebuild", "runtime", "supervisor", "core update")):
        return "runtime.lifecycle"
    return "runtime.log"


def _redact_line(text: str, *, max_len: int = 240) -> str:
    value = re.sub(r"(?i)(token|secret|password|authorization)=\S+", r"\1=<redacted>", text or "")
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1<redacted>", value)
    value = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", value)
    value = re.sub(r"/(?:[\w.\-]+/){2,}[\w.\-]+", "<path>", value)
    return value[:max_len]


def _json_object_from_line(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "")
        return dict(payload) if isinstance(payload, Mapping) else {}
    except Exception:
        return {}


def _extract_structured_event_fields(line: str) -> dict[str, Any]:
    payload = _json_object_from_line(line)
    event_type = str(payload.get("type") or "").strip()
    source = str(payload.get("source") or "").strip()
    logger = str(payload.get("logger") or "").strip()
    trace = str(payload.get("trace") or payload.get("correlation_id") or payload.get("request_id") or "").strip()
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        trace = trace or str(nested.get("trace") or nested.get("correlation_id") or nested.get("request_id") or nested.get("trial_id") or "").strip()
    if not event_type:
        match = re.search(r"\btype=([A-Za-z0-9_.:\-]+)", line or "")
        if match:
            event_type = match.group(1).strip()
    if not source:
        match = re.search(r"\bsource=([A-Za-z0-9_.:\-]+)", line or "")
        if match:
            source = match.group(1).strip()
    return {
        "structured": bool(payload),
        "event_type": event_type,
        "event_source": source,
        "logger": logger,
        "trace": trace,
        "raw_msg": str(payload.get("msg") or "") if payload else "",
    }


def _log_candidates() -> list[Path]:
    roots = [
        Path.cwd() / ".adaos" / "state",
        Path.cwd() / ".adaos" / "runtime",
        Path.cwd() / ".adaos" / "logs",
        _SKILL_ROOT.parent / "infrastate_skill",
    ]
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("*.log", "*.jsonl", "*.txt"):
            out.extend(path for path in root.rglob(pattern) if path.is_file())
    return sorted({path.resolve() for path in out})[:64]


def _read_log_records(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return records
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return records
    bounded_lines = max(1, min(int(max_lines or _MAX_DEFAULT_LOG_LINES), _MAX_EXPLICIT_LOG_LINES))
    selected = lines[-bounded_lines:]
    try:
        base_ts = path.stat().st_mtime - len(selected)
    except OSError:
        base_ts = time.time() - len(selected)
    for index, line in enumerate(selected):
        if not line.strip():
            continue
        ts = _parse_ts(line)
        if ts is None:
            ts = base_ts + index
        severity = _severity_from_text(line)
        topic = _topic_from_text(line)
        fields = _extract_structured_event_fields(line)
        records.append(
            {
                "id": f"{path.name}:{index}",
                "ts": ts,
                "ts_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
                "source": "local_log",
                "source_path": str(path),
                "topic": topic,
                "severity": severity,
                **fields,
                "message": _redact_line(line),
            }
        )
    return records


def _records_to_windows(
    records: list[Mapping[str, Any]],
    *,
    window_seconds: int = 60,
    node_id: str = "local",
    subnet_id: str = "local",
    webspace_id: str = "desktop",
) -> list[dict[str, Any]]:
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    size = max(1, int(window_seconds or 60))
    for record in records:
        ts = _value(record, "ts")
        bucket = int(ts // size) * size
        buckets.setdefault(bucket, []).append(record)

    windows: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        items = buckets[bucket_start]
        topic_counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        for item in items:
            topic = str(item.get("topic") or "runtime.log")
            severity = str(item.get("severity") or "info")
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        features = {
            "event_total": len(items),
            "error_total": severity_counts.get("error", 0) + severity_counts.get("critical", 0),
            "critical_total": severity_counts.get("critical", 0),
            "drop_total": topic_counts.get("eventbus.pressure", 0),
            "supersede_total": sum(1 for item in items if "supersede" in str(item.get("message", "")).lower()),
            "projection_refresh_total": topic_counts.get("projection.lifecycle", 0),
            "same_projection_refresh_max": topic_counts.get("projection.lifecycle", 0),
            "yjs_write_total": topic_counts.get("yjs.sync", 0),
            "browser_reconnect_total": topic_counts.get("browser.session", 0),
            "member_disconnect_total": topic_counts.get("member.connectivity", 0),
            "runtime_rebuild_total": topic_counts.get("runtime.lifecycle", 0),
        }
        prediction = _rule_predict({"features": features})
        weak_label = _weak_label(features, topic_counts=topic_counts, severity_counts=severity_counts)
        windows.append(
            {
                "window_id": f"{node_id}:{bucket_start}-{bucket_start + size}",
                "scope": {
                    "node_id": node_id,
                    "subnet_id": subnet_id,
                    "webspace_id": webspace_id,
                },
                "time": {
                    "start_ts": bucket_start,
                    "end_ts": bucket_start + size,
                    "start": datetime.fromtimestamp(bucket_start, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "end": datetime.fromtimestamp(bucket_start + size, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "window_seconds": size,
                },
                "features": features,
                "evidence": [
                    {
                        "id": item.get("id"),
                        "topic": item.get("topic"),
                        "severity": item.get("severity"),
                        "message": item.get("message"),
                    }
                    for item in items[:_MAX_EVIDENCE_PER_WINDOW]
                ],
                "label": weak_label,
                "baseline_prediction": prediction,
            }
        )
    return windows


def _parse_subscription_declarations(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    skill_match = re.search(r"skill=([A-Za-z0-9_.\-<>]+)", text or "")
    subscriber = skill_match.group(1) if skill_match else "unknown"
    match = re.search(r"subscriptions=\[(.*?)\]", text or "")
    if not match:
        return rows
    body = match.group(1)
    for item in body.split(","):
        token = item.strip()
        if not token or ":" not in token:
            continue
        event_type, handler = token.split(":", 1)
        event_type = event_type.strip()
        handler = handler.strip()
        if event_type:
            rows.append({"subscriber": subscriber, "event_type": event_type, "handler": handler})
    return rows


def _parse_event_observation(text: str) -> dict[str, str] | None:
    raw = text or ""
    event_type = ""
    source = ""
    try:
        payload = json.loads(raw)
        if isinstance(payload, Mapping):
            event_type = str(payload.get("type") or "").strip()
            source = str(payload.get("source") or payload.get("logger") or "").strip()
    except Exception:
        pass
    if not event_type:
        match = re.search(r"\btype=([A-Za-z0-9_.:\-]+)", raw)
        if match:
            event_type = match.group(1).strip()
    if not source:
        match = re.search(r"\bsource=([A-Za-z0-9_.:\-]+)", raw)
        if match:
            source = match.group(1).strip()
    if not event_type:
        return None
    if len(event_type) < 5 or event_type.endswith("."):
        return None
    return {"event_type": event_type, "publisher": source or "unknown"}


def _subscription_flow_from_records(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    declarations: dict[tuple[str, str], dict[str, Any]] = {}
    event_counts: dict[str, int] = {}
    publishers: dict[str, set[str]] = {}
    samples: dict[str, str] = {}

    for record in records:
        message = str(record.get("message") or "")
        for decl in _parse_subscription_declarations(message):
            key = (decl["subscriber"], decl["event_type"])
            current = declarations.setdefault(
                key,
                {
                    "subscriber": decl["subscriber"],
                    "event_type": decl["event_type"],
                    "handler": decl.get("handler") or "",
                    "declared": 0,
                },
            )
            current["declared"] = int(current.get("declared") or 0) + 1
        observed = _parse_event_observation(message)
        if observed:
            event_type = observed["event_type"]
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            publishers.setdefault(event_type, set()).add(observed["publisher"])
            samples.setdefault(event_type, message[:160])

    rows: list[dict[str, Any]] = []
    subscribed_events = {event_type for _subscriber, event_type in declarations}
    for (_subscriber, event_type), decl in sorted(declarations.items(), key=lambda item: (item[0][1], item[0][0])):
        volume = event_counts.get(event_type, 0)
        state = "active"
        if volume == 0:
            state = "idle"
        elif volume >= 50:
            state = "noisy"
        rows.append(
            {
                "id": f"{decl['subscriber']}:{event_type}",
                "event_type": event_type,
                "publisher": ",".join(sorted(publishers.get(event_type, set()))) or "unknown",
                "subscriber": decl["subscriber"],
                "handler": decl.get("handler") or "",
                "events": volume,
                "fanout": sum(1 for _subscriber, subscribed in declarations if subscribed == event_type),
                "state": state,
                "risk": "warning" if state in {"idle", "noisy"} else "ok",
            }
        )

    for event_type, count in sorted(event_counts.items(), key=lambda item: (-item[1], item[0])):
        if event_type in subscribed_events:
            continue
        rows.append(
            {
                "id": f"missing:{event_type}",
                "event_type": event_type,
                "publisher": ",".join(sorted(publishers.get(event_type, set()))) or "unknown",
                "subscriber": "<none>",
                "handler": "",
                "events": count,
                "fanout": 0,
                "state": "missing_consumer",
                "risk": "critical" if count >= 5 else "warning",
                "sample": samples.get(event_type, ""),
            }
        )

    missing = sum(1 for row in rows if row["state"] == "missing_consumer")
    idle = sum(1 for row in rows if row["state"] == "idle")
    noisy = sum(1 for row in rows if row["state"] == "noisy")
    declared = len(declarations)
    event_types = len(event_counts)
    risk_score = round(min(1.0, (missing * 2 + idle + noisy) / max(1, len(rows))), 3)
    rows = rows[:_MAX_SUBSCRIPTION_ROWS]
    return {
        "summary": {
            "declared_subscriptions": declared,
            "observed_event_types": event_types,
            "missing_consumers": missing,
            "idle_subscriptions": idle,
            "noisy_subscriptions": noisy,
            "risk_score": risk_score,
        },
        "rows": rows,
        "chart": {
            "title": "Объём событий по типам",
            "unit": "events",
            "points": [
                {"ts": event_type[-24:], "value": count}
                for event_type, count in sorted(event_counts.items(), key=lambda item: (-item[1], item[0]))[:_MAX_STREAM_POINTS]
            ],
        },
        "metrics": {
            "items": [
                {"id": "declared", "metric": "Объявленные подписки", "value": declared, "target": "отслеживать", "status": "info"},
                {"id": "observed_types", "metric": "Наблюдаемые типы событий", "value": event_types, "target": "отслеживать", "status": "info"},
                {"id": "missing", "metric": "Нет потребителей", "value": missing, "target": "0", "status": "ok" if missing == 0 else "warning"},
                {"id": "idle", "metric": "Неактивные подписки", "value": idle, "target": "уменьшать", "status": "ok" if idle == 0 else "warning"},
                {"id": "noisy", "metric": "Шумные подписки", "value": noisy, "target": "проверить", "status": "ok" if noisy == 0 else "warning"},
                {"id": "risk_score", "metric": "Оценка риска маршрутизации", "value": risk_score, "target": "<= 0.15", "status": "ok" if risk_score <= 0.15 else "warning"},
            ]
        },
    }


def _project_subscription_flow(result: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    sections = {
        "subscription_summary": {"items": [
            {"id": key, "name": key.replace("_", " ").title(), "value": value}
            for key, value in (result.get("summary") or {}).items()
        ]},
        "subscription_edges": {"items": list(result.get("rows") or [])},
        "subscription_metrics": result.get("metrics") or {"items": []},
        "subscription_chart": result.get("chart") or {"title": "Объём событий по типам", "unit": "events", "points": []},
    }
    return _project_sections(sections, webspace_id=webspace_id, force=True)


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 3) if denominator else 0.0


def _score_from_counts(*counts: int, weight: float = 1.0) -> float:
    return round(1.0 / (1.0 + sum(max(0, count) for count in counts) * weight), 3)


def _observability_health_from_records(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(records)
    structured = sum(1 for item in records if bool(item.get("structured")))
    typed = sum(1 for item in records if str(item.get("event_type") or "").strip())
    sourced = sum(1 for item in records if str(item.get("event_source") or item.get("logger") or "").strip())
    timestamped = sum(1 for item in records if _value(item, "ts") > 0)
    correlated = sum(
        1
        for item in records
        if str(item.get("trace") or "").strip()
        or any(token in str(item.get("message") or "").lower() for token in ("trace", "correlation", "request_id", "trial_id"))
    )

    def count_text(*tokens: str) -> int:
        lowered = [str(item.get("message") or "").lower() for item in records]
        return sum(1 for text in lowered if any(token in text for token in tokens))

    eventbus_records = sum(1 for item in records if str(item.get("logger") or "") == "adaos.eventbus" or str(item.get("topic") or "") == "eventbus.pressure")
    projection_records = count_text("projection", "materializ")
    yjs_warnings = count_text("yjs owner flow", "blocked yjs", "throttled yjs", "yroom effective branches repaired")
    blocked_writes = count_text("write_amplification_blocked", "blocked yjs")
    throttled_writes = count_text("write_amplification", "throttled yjs")
    slow_tool_calls = count_text("tools.call slow")
    slow_handlers = count_text("slow async event handler")
    event_loop_lag = count_text("event loop lag")
    browser_sessions = count_text("browser.session", "websocket", "/ws", "/yws", "connection closed", "connection open")
    runtime_errors = sum(1 for item in records if str(item.get("severity") or "") in {"error", "critical"})
    repairs = count_text("repaired", "repair_effective", "initial_client_update_reconcile")

    schema_score = round((_ratio(typed, total) + _ratio(sourced, total) + _ratio(timestamped, total)) / 3.0, 3) if total else 0.0
    correlation_score = _ratio(correlated, total)
    projection_health = _score_from_counts(blocked_writes, repairs, weight=0.25)
    runtime_health = _score_from_counts(slow_tool_calls, slow_handlers, event_loop_lag, runtime_errors, weight=0.15)
    browser_health = _score_from_counts(browser_sessions, weight=0.02)
    overall = round((schema_score + correlation_score + projection_health + runtime_health + browser_health) / 5.0, 3)

    invariants = [
        {
            "id": "event_type_coverage",
            "metric": "Покрытие типов событий",
            "value": _ratio(typed, total),
            "target": ">= 0.90",
            "status": "ok" if _ratio(typed, total) >= 0.90 else "warning",
        },
        {
            "id": "source_coverage",
            "metric": "Покрытие источников",
            "value": _ratio(sourced, total),
            "target": ">= 0.95",
            "status": "ok" if _ratio(sourced, total) >= 0.95 else "warning",
        },
        {
            "id": "correlation_coverage",
            "metric": "Покрытие связей",
            "value": correlation_score,
            "target": ">= 0.50",
            "status": "ok" if correlation_score >= 0.50 else "warning",
        },
        {
            "id": "blocked_yjs_writes",
            "metric": "Заблокированные записи YJS/проекций",
            "value": blocked_writes,
            "target": "0",
            "status": "ok" if blocked_writes == 0 else "critical",
        },
        {
            "id": "slow_handlers",
            "metric": "Медленные обработчики событий",
            "value": slow_handlers,
            "target": "0",
            "status": "ok" if slow_handlers == 0 else "warning",
        },
        {
            "id": "event_loop_lag",
            "metric": "Предупреждения задержки цикла событий",
            "value": event_loop_lag,
            "target": "0",
            "status": "ok" if event_loop_lag == 0 else "warning",
        },
    ]
    metrics = {
        "items": [
            {"id": "observability_score", "metric": "Оценка наблюдаемости", "value": overall, "target": ">= 0.80", "status": "ok" if overall >= 0.80 else "warning"},
            {"id": "schema_score", "metric": "Покрытие схемы", "value": schema_score, "target": ">= 0.90", "status": "ok" if schema_score >= 0.90 else "warning"},
            {"id": "correlation_score", "metric": "Оценка связей", "value": correlation_score, "target": ">= 0.50", "status": "ok" if correlation_score >= 0.50 else "warning"},
            {"id": "projection_health", "metric": "Состояние проекций/YJS", "value": projection_health, "target": ">= 0.80", "status": "ok" if projection_health >= 0.80 else "warning"},
            {"id": "runtime_health", "metric": "Состояние инструментов/runtime", "value": runtime_health, "target": ">= 0.80", "status": "ok" if runtime_health >= 0.80 else "warning"},
            {"id": "browser_health", "metric": "Состояние браузера/сессии", "value": browser_health, "target": ">= 0.80", "status": "ok" if browser_health >= 0.80 else "warning"},
        ] + invariants
    }
    dataset = {
        "items": [
            {"id": "records", "name": "Нормализованные записи", "current": total, "target": "логи ядра, браузера и навыков", "notes": "Строки после очистки и разбора."},
            {"id": "structured", "name": "Структурированные JSON-записи", "current": structured, "target": "увеличивать", "notes": "Строки, разобранные как JSON-события логов."},
            {"id": "eventbus", "name": "Записи шины событий", "current": eventbus_records, "target": "видимый поток команд и событий", "notes": "Строки, созданные логированием шины событий."},
            {"id": "projection", "name": "Записи проекций/YJS", "current": projection_records, "target": "видимая материализация интерфейса", "notes": "Активность проекций, материализации и YJS."},
            {"id": "browser", "name": "Записи браузера/сессии", "current": browser_sessions, "target": "покрытие браузерных логов", "notes": "Записи браузера, WebSocket, YWS и сессий."},
            {"id": "labels", "name": "Предлагаемая политика разметки", "current": "invariant + operator review", "target": "ручное подтверждение", "notes": "Использовать нарушения инвариантов как слабую разметку, затем подтверждать или отклонять вручную."},
        ]
    }
    chart = {
        "title": "Наблюдаемость системы",
        "unit": "0..1",
        "points": [
            {"ts": "schema", "value": schema_score},
            {"ts": "correlation", "value": correlation_score},
            {"ts": "projection", "value": projection_health},
            {"ts": "runtime", "value": runtime_health},
            {"ts": "browser", "value": browser_health},
        ],
    }
    labels = [
        {"id": "blocked_projection", "class": "projection_write_blocked", "support": blocked_writes, "precision": "", "recall": "", "f1": "", "review": "accept when paired with UI stale/repair symptoms"},
        {"id": "slow_handler", "class": "slow_event_handler", "support": slow_handlers, "precision": "", "recall": "", "f1": "", "review": "accept when handler duration exceeds SLO"},
        {"id": "event_loop_lag", "class": "runtime_event_loop_lag", "support": event_loop_lag, "precision": "", "recall": "", "f1": "", "review": "accept when user-visible latency or tool slowdown co-occurs"},
        {"id": "browser_instability", "class": "browser_session_instability", "support": browser_sessions, "precision": "", "recall": "", "f1": "", "review": "accept when reconnect/repair bursts exceed baseline"},
    ]
    return {
        "mode": "observability_health",
        "record_count": total,
        "summary": {
            "observability_score": overall,
            "schema_score": schema_score,
            "correlation_score": correlation_score,
            "projection_health": projection_health,
            "runtime_health": runtime_health,
            "browser_health": browser_health,
            "yjs_warnings": yjs_warnings,
            "blocked_writes": blocked_writes,
            "throttled_writes": throttled_writes,
            "slow_tool_calls": slow_tool_calls,
            "slow_handlers": slow_handlers,
            "event_loop_lag": event_loop_lag,
        },
        "metrics": metrics,
        "dataset": dataset,
        "chart": chart,
        "labels": {"items": labels},
        "analyzed_at": _now_iso(),
    }


def _project_observability_health(result: Mapping[str, Any], *, webspace_id: str, windows: list[Mapping[str, Any]]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    sections = {
        "summary": {
            "label": "Анализ событий",
            "value": f"{_value(summary, 'observability_score'):.3f}",
            "subtitle": "оценка наблюдаемости",
            "description": (
                f"records={result.get('record_count')} schema={summary.get('schema_score')} "
                f"correlation={summary.get('correlation_score')} blocked_writes={summary.get('blocked_writes')}"
            ),
            "buttons": [
                {"id": "open", "label": "Открыть"},
                {"id": "analyze_health", "label": "Проверить состояние"},
                {"id": "run_real_trial", "label": "Запустить проверку на логах"},
            ],
        },
        "dataset": result.get("dataset") or {"items": []},
        "metrics": result.get("metrics") or {"items": []},
        "per_class": result.get("labels") or {"items": []},
        "chart": result.get("chart") or {"title": "Наблюдаемость системы", "unit": "0..1", "points": []},
        "windows": {"items": _window_rows(windows)},
        "event_volume_chart": {"title": "Объём логов по окнам", "unit": "records", "points": _event_volume_points(windows)},
        "class_distribution_chart": {"title": "Распределение найденных проблем", "unit": "windows", "points": _class_distribution_points(windows)},
    }
    return _project_sections(sections, webspace_id=webspace_id, force=True)


def _readiness_chart(
    result: Mapping[str, Any],
    *,
    title: str = "Готовность анализа",
    subscription_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    points = [
        {"ts": "classification", "value": _value(result, "macro_f1")},
        {"ts": "critical", "value": _value(result, "critical_recall")},
        {"ts": "normal precision", "value": round(1.0 - _value(result, "false_positive_rate"), 3)},
        {"ts": "reason quality", "value": _value(result, "top_reason_hit_rate")},
    ]
    if isinstance(subscription_result, Mapping):
        summary = subscription_result.get("summary") if isinstance(subscription_result.get("summary"), Mapping) else {}
        risk_score = _value(summary, "risk_score")
        observed = max(1.0, _value(summary, "observed_event_types"))
        missing = _value(summary, "missing_consumers")
        points.extend(
            [
                {"ts": "routing health", "value": round(max(0.0, 1.0 - risk_score), 3)},
                {"ts": "consumer coverage", "value": round(max(0.0, 1.0 - missing / observed), 3)},
            ]
        )
    return {"title": title, "unit": "0..1", "points": points}


def _weak_label(
    features: Mapping[str, Any],
    *,
    topic_counts: Mapping[str, int],
    severity_counts: Mapping[str, int],
) -> dict[str, Any]:
    error_total = int(_value(features, "error_total"))
    critical_total = int(_value(features, "critical_total"))
    event_total = int(_value(features, "event_total"))

    incident_type = "normal"
    reasons: list[str] = []
    if topic_counts.get("runtime.lifecycle", 0) >= 4:
        incident_type = "runtime_rebuild_churn"
        reasons = ["runtime.lifecycle"]
    elif topic_counts.get("eventbus.pressure", 0) >= 3 or _value(features, "supersede_total") >= 8:
        incident_type = "eventbus_backpressure"
        reasons = ["eventbus.pressure", "supersede_total"]
    elif topic_counts.get("projection.lifecycle", 0) >= 8:
        incident_type = "projection_refresh_storm"
        reasons = ["projection.lifecycle"]
    elif topic_counts.get("yjs.sync", 0) >= 5:
        incident_type = "yjs_write_pressure"
        reasons = ["yjs.sync"]
    elif topic_counts.get("browser.session", 0) >= 4:
        incident_type = "browser_session_instability"
        reasons = ["browser.session"]
    elif topic_counts.get("member.connectivity", 0) >= 2:
        incident_type = "member_node_disconnect"
        reasons = ["member.connectivity"]
    elif error_total >= 4 or critical_total >= 1 or event_total >= 180:
        incident_type = str(_rule_predict({"features": features}).get("incident_type") or "normal")
        reasons = _top_feature_names(features)

    severity = "info"
    if incident_type != "normal":
        severity = "critical" if critical_total > 0 or error_total >= 8 else "warning"
    return {
        "incident": incident_type != "normal",
        "incident_type": incident_type,
        "severity": severity,
        "reasons": reasons,
        "source": "codex_reviewed_log_heuristic",
        "label_quality": "reviewed_heuristic",
    }


def _class_distribution_points(windows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for window in windows:
        prediction = window.get("baseline_prediction") if isinstance(window.get("baseline_prediction"), Mapping) else {}
        label = window.get("label") if isinstance(window.get("label"), Mapping) else {}
        key = str(prediction.get("incident_type") or label.get("incident_type") or "normal")
        counts[key] = counts.get(key, 0) + 1
    return [{"ts": key, "value": value} for key, value in sorted(counts.items())]


def _event_volume_points(windows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index, window in enumerate(windows[:48]):
        features = window.get("features") if isinstance(window.get("features"), Mapping) else {}
        time_info = window.get("time") if isinstance(window.get("time"), Mapping) else {}
        label = str(time_info.get("start") or index)
        points.append({"ts": label[-13:-4] if len(label) > 12 else label, "value": _value(features, "event_total")})
    return points


def _window_rows(windows: list[Mapping[str, Any]], *, limit: int = 32) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in windows[:limit]:
        features = window.get("features") if isinstance(window.get("features"), Mapping) else {}
        prediction = window.get("baseline_prediction") if isinstance(window.get("baseline_prediction"), Mapping) else _rule_predict(window)
        rows.append(
            {
                "id": window.get("window_id"),
                "window_id": window.get("window_id"),
                "events": int(_value(features, "event_total")),
                "errors": int(_value(features, "error_total")),
                "label": (window.get("label") or {}).get("incident_type") if isinstance(window.get("label"), Mapping) else "",
                "prediction": prediction.get("incident_type"),
                "severity": prediction.get("severity"),
                "confidence": prediction.get("confidence"),
            }
        )
    return rows


def _compact_evaluation_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": result.get("model"),
        "window_count": result.get("window_count"),
        "accuracy": result.get("accuracy"),
        "macro_f1": result.get("macro_f1"),
        "false_positive_rate": result.get("false_positive_rate"),
        "critical_recall": result.get("critical_recall"),
        "avg_detection_delay_s": result.get("avg_detection_delay_s"),
        "top_reason_hit_rate": result.get("top_reason_hit_rate"),
        "per_class": list(result.get("per_class") or []),
        "evaluated_at": result.get("evaluated_at"),
    }


def _compact_dataset_result(result: Mapping[str, Any]) -> dict[str, Any]:
    def compact_chart(value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        chart = dict(value)
        points = chart.get("points")
        if isinstance(points, list):
            chart["points"] = points[:_MAX_STREAM_POINTS]
            chart["truncated"] = len(points) > _MAX_STREAM_POINTS
        return chart

    return {
        "window_count": result.get("window_count"),
        "record_count": result.get("record_count"),
        "window_seconds": result.get("window_seconds"),
        "rows": list(result.get("rows") or [])[:_MAX_STREAM_ROWS],
        "event_volume_chart": compact_chart(result.get("event_volume_chart")),
        "class_distribution_chart": compact_chart(result.get("class_distribution_chart")),
        "baseline": _compact_evaluation_result(result.get("baseline_result") or {}) if isinstance(result.get("baseline_result"), Mapping) else {},
        "built_at": result.get("built_at"),
    }


def _export_jsonl(windows: list[Mapping[str, Any]], path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for window in windows:
            fh.write(json.dumps(dict(window), ensure_ascii=False, sort_keys=True) + "\n")
    return {"path": str(path), "count": len(windows), "bytes": path.stat().st_size}


def _window(
    window_id: str,
    incident_type: str,
    severity: str,
    features: Mapping[str, float],
    *,
    first_symptom_s: float = 0.0,
    labeled_at_s: float = 60.0,
) -> dict[str, Any]:
    return {
        "window_id": window_id,
        "features": dict(features),
        "label": {
            "incident": incident_type != "normal",
            "incident_type": incident_type,
            "severity": severity,
            "reasons": _top_feature_names(features),
        },
        "timing": {
            "first_symptom_s": first_symptom_s,
            "labeled_at_s": labeled_at_s,
        },
    }


def _synthetic_windows() -> list[dict[str, Any]]:
    return [
        _window("demo-001", "normal", "info", {"event_total": 18, "error_total": 0, "drop_total": 0, "projection_refresh_total": 4, "same_projection_refresh_max": 2, "yjs_write_total": 3, "browser_reconnect_total": 0, "member_disconnect_total": 0, "runtime_rebuild_total": 0}),
        _window("demo-002", "normal", "info", {"event_total": 31, "error_total": 1, "drop_total": 0, "projection_refresh_total": 6, "same_projection_refresh_max": 3, "yjs_write_total": 5, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 0}),
        _window("demo-003", "eventbus_backpressure", "warning", {"event_total": 220, "error_total": 7, "drop_total": 18, "supersede_total": 42, "projection_refresh_total": 19, "same_projection_refresh_max": 7, "yjs_write_total": 8, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=12),
        _window("demo-004", "projection_refresh_storm", "warning", {"event_total": 140, "error_total": 3, "drop_total": 2, "supersede_total": 9, "projection_refresh_total": 96, "same_projection_refresh_max": 61, "yjs_write_total": 19, "browser_reconnect_total": 0, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=18),
        _window("demo-005", "yjs_write_pressure", "critical", {"event_total": 88, "error_total": 4, "drop_total": 1, "supersede_total": 4, "projection_refresh_total": 34, "same_projection_refresh_max": 15, "yjs_write_total": 168, "browser_reconnect_total": 2, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=9),
        _window("demo-006", "browser_session_instability", "warning", {"event_total": 64, "error_total": 2, "drop_total": 0, "supersede_total": 2, "projection_refresh_total": 12, "same_projection_refresh_max": 5, "yjs_write_total": 10, "browser_reconnect_total": 12, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=16),
        _window("demo-007", "member_node_disconnect", "critical", {"event_total": 52, "error_total": 5, "drop_total": 1, "supersede_total": 0, "projection_refresh_total": 8, "same_projection_refresh_max": 3, "yjs_write_total": 7, "browser_reconnect_total": 0, "member_disconnect_total": 3, "runtime_rebuild_total": 0}, first_symptom_s=5),
        _window("demo-008", "runtime_rebuild_churn", "warning", {"event_total": 104, "error_total": 3, "drop_total": 0, "supersede_total": 8, "projection_refresh_total": 28, "same_projection_refresh_max": 10, "yjs_write_total": 25, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 5}, first_symptom_s=21),
        _window("demo-009", "projection_refresh_storm", "critical", {"event_total": 260, "error_total": 9, "drop_total": 7, "supersede_total": 34, "projection_refresh_total": 180, "same_projection_refresh_max": 124, "yjs_write_total": 52, "browser_reconnect_total": 2, "member_disconnect_total": 0, "runtime_rebuild_total": 1}, first_symptom_s=8),
        _window("demo-010", "eventbus_backpressure", "critical", {"event_total": 420, "error_total": 21, "drop_total": 55, "supersede_total": 90, "projection_refresh_total": 44, "same_projection_refresh_max": 13, "yjs_write_total": 28, "browser_reconnect_total": 2, "member_disconnect_total": 1, "runtime_rebuild_total": 0}, first_symptom_s=4),
        _window("demo-011", "normal", "info", {"event_total": 46, "error_total": 0, "drop_total": 0, "supersede_total": 1, "projection_refresh_total": 9, "same_projection_refresh_max": 3, "yjs_write_total": 7, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 1}),
        _window("demo-012", "browser_session_instability", "critical", {"event_total": 94, "error_total": 8, "drop_total": 2, "supersede_total": 4, "projection_refresh_total": 18, "same_projection_refresh_max": 7, "yjs_write_total": 12, "browser_reconnect_total": 23, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=7),
    ]


def _trial_windows() -> list[dict[str, Any]]:
    return [
        _window("trial-001-normal-idle", "normal", "info", {"event_total": 24, "error_total": 0, "drop_total": 0, "supersede_total": 0, "projection_refresh_total": 5, "same_projection_refresh_max": 2, "yjs_write_total": 4, "browser_reconnect_total": 0, "member_disconnect_total": 0, "runtime_rebuild_total": 0}),
        _window("trial-002-normal-busy", "normal", "info", {"event_total": 74, "error_total": 1, "drop_total": 0, "supersede_total": 1, "projection_refresh_total": 9, "same_projection_refresh_max": 4, "yjs_write_total": 17, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 1}),
        _window("trial-003-eventbus-drop", "eventbus_backpressure", "critical", {"event_total": 390, "error_total": 13, "drop_total": 48, "supersede_total": 74, "projection_refresh_total": 32, "same_projection_refresh_max": 8, "yjs_write_total": 24, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=6),
        _window("trial-004-eventbus-supersede", "eventbus_backpressure", "warning", {"event_total": 260, "error_total": 5, "drop_total": 7, "supersede_total": 56, "projection_refresh_total": 17, "same_projection_refresh_max": 6, "yjs_write_total": 13, "browser_reconnect_total": 0, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=11),
        _window("trial-005-projection-loop", "projection_refresh_storm", "warning", {"event_total": 155, "error_total": 2, "drop_total": 1, "supersede_total": 5, "projection_refresh_total": 118, "same_projection_refresh_max": 70, "yjs_write_total": 18, "browser_reconnect_total": 0, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=17),
        _window("trial-006-yjs-pressure", "yjs_write_pressure", "critical", {"event_total": 112, "error_total": 10, "drop_total": 2, "supersede_total": 8, "projection_refresh_total": 28, "same_projection_refresh_max": 10, "yjs_write_total": 156, "browser_reconnect_total": 1, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=4),
        _window("trial-007-browser-reconnect", "browser_session_instability", "warning", {"event_total": 96, "error_total": 3, "drop_total": 0, "supersede_total": 3, "projection_refresh_total": 14, "same_projection_refresh_max": 4, "yjs_write_total": 15, "browser_reconnect_total": 15, "member_disconnect_total": 0, "runtime_rebuild_total": 0}, first_symptom_s=9),
        _window("trial-008-member-drop", "member_node_disconnect", "critical", {"event_total": 81, "error_total": 8, "drop_total": 1, "supersede_total": 0, "projection_refresh_total": 11, "same_projection_refresh_max": 5, "yjs_write_total": 9, "browser_reconnect_total": 1, "member_disconnect_total": 4, "runtime_rebuild_total": 0}, first_symptom_s=3),
        _window("trial-009-runtime-rebuild", "runtime_rebuild_churn", "warning", {"event_total": 132, "error_total": 4, "drop_total": 1, "supersede_total": 12, "projection_refresh_total": 31, "same_projection_refresh_max": 9, "yjs_write_total": 22, "browser_reconnect_total": 2, "member_disconnect_total": 0, "runtime_rebuild_total": 6}, first_symptom_s=19),
        _window("trial-010-combined-pressure", "eventbus_backpressure", "critical", {"event_total": 520, "error_total": 18, "drop_total": 62, "supersede_total": 104, "projection_refresh_total": 88, "same_projection_refresh_max": 24, "yjs_write_total": 91, "browser_reconnect_total": 7, "member_disconnect_total": 1, "runtime_rebuild_total": 2}, first_symptom_s=2),
    ]


def _trial_subscription_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [
        {
            "message": '{"level":"INFO","logger":"adaos.sdk.subscriptions","msg":"skill=web_desktop_skill subscriptions=[desktop.webspace.reload: on_reload, webio.stream.snapshot.requested: on_snapshot]"}',
            "severity": "info",
            "topic": "trial.subscription",
            "ts": 1,
        },
        {
            "message": '{"level":"INFO","logger":"adaos.sdk.subscriptions","msg":"skill=ai_event_analysis_skill subscriptions=[ai_event_analysis.evaluate_requested: on_eval, data.changed: on_data_changed]"}',
            "severity": "info",
            "topic": "trial.subscription",
            "ts": 2,
        },
        {
            "message": '{"level":"INFO","logger":"adaos.sdk.subscriptions","msg":"skill=diagnostics_skill subscriptions=[diagnostic.run.requested: on_run, sys.ready: on_ready]"}',
            "severity": "info",
            "topic": "trial.subscription",
            "ts": 3,
        },
    ]
    emitted = [
        ("desktop.webspace.reload", "desktop", 4),
        ("webio.stream.snapshot.requested", "webio", 8),
        ("sys.ready", "runtime", 2),
        ("data.changed", "projection.runtime", 58),
        ("diagnostic.unhandled.alert", "diagnostics", 7),
    ]
    ts = 10
    for event_type, source, count in emitted:
        for _index in range(count):
            records.append(
                {
                    "message": json.dumps({"level": "INFO", "logger": "adaos.events", "type": event_type, "source": source}, sort_keys=True),
                    "severity": "info",
                    "topic": "trial.event",
                    "ts": ts,
                }
            )
            ts += 1
    return records


def _trial_suite_result() -> dict[str, Any]:
    windows = _trial_windows()
    baseline_result = _evaluate(windows)
    subscription_result = _subscription_flow_from_records(_trial_subscription_records())
    readiness = _readiness_chart(baseline_result, title="Готовность синтетической проверки", subscription_result=subscription_result)
    scenario_classes = sorted({str((window.get("label") or {}).get("incident_type") or "normal") for window in windows})
    summary = subscription_result.get("summary") if isinstance(subscription_result.get("summary"), Mapping) else {}
    readiness_score = round(
        (
            _value(baseline_result, "macro_f1")
            + _value(baseline_result, "critical_recall")
            + max(0.0, 1.0 - _value(baseline_result, "false_positive_rate"))
            + max(0.0, 1.0 - _value(summary, "risk_score"))
        )
        / 4.0,
        3,
    )
    return {
        "mode": "trial_suite",
        "scenario_count": len(windows),
        "scenario_classes": scenario_classes,
        "window_count": len(windows),
        "record_count": len(_trial_subscription_records()),
        "label_source": "deterministic_trial_suite",
        "baseline_result": baseline_result,
        "subscription_result": subscription_result,
        "readiness_score": readiness_score,
        "chart": readiness,
        "windows": windows,
        "rows": _window_rows(windows, limit=48),
        "event_volume_chart": {
            "title": "Объём событий синтетической проверки",
            "unit": "events",
            "points": _event_volume_points(windows),
        },
        "class_distribution_chart": {
            "title": "Распределение классов синтетической проверки",
            "unit": "windows",
            "points": _class_distribution_points(windows),
        },
        "built_at": _now_iso(),
    }


def _emit_real_trial_events(*, webspace_id: str, trial_id: str, event_count: int) -> dict[str, Any]:
    emitted: list[dict[str, Any]] = []

    def emit(event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        body = {"trial_id": trial_id, "webspace_id": webspace_id}
        if payload:
            body.update(dict(payload))
        publish_event(event_type, body, source="ai_event_analysis_skill.real_trial")
        emitted.append({"type": event_type, "payload": body})

    emit("ai_event_analysis.real_trial.started", {"phase": "start"})
    for index in range(max(1, event_count)):
        emit("ai_event_analysis.real_trial.eventbus.backpressure.drop", {"index": index, "symptom": "drop queue backpressure"})
    for index in range(max(1, event_count // 2)):
        emit("ai_event_analysis.real_trial.projection.refresh.requested", {"index": index, "symptom": "projection refresh materialization"})
    for index in range(max(8, event_count // 2)):
        emit("ai_event_analysis.real_trial.service.failed", {"index": index, "phase": "check", "symptom": "controlled failure marker"})
    emit("ai_event_analysis.real_trial.completed", {"phase": "finish"})
    try:
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "real-trial-events",
                    "title": f"Real trial emitted {len(emitted)} AdaOS events",
                    "description": f"trial_id={trial_id} webspace_id={webspace_id}",
                    "content": {"trial_id": trial_id, "events": emitted[:8], "event_count": len(emitted)},
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass
    return {"trial_id": trial_id, "emitted_event_count": len(emitted), "sample": emitted[:8]}


def _call_workspace_skill_tool(
    skill_name: str,
    tool_name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    started = time.time()
    try:
        from adaos.services.agent_context import get_ctx
        from adaos.skills.runtime_runner import execute_tool

        ctx = get_ctx()
        skill_dir = Path(ctx.paths.skills_workspace_dir()) / skill_name
        previous = ctx.skill_ctx.get()
        try:
            ctx.skill_ctx.set(skill_name, skill_dir)
            result = execute_tool(skill_dir, module="handlers.main", attr=tool_name, payload=payload)
        finally:
            if previous is None:
                try:
                    ctx.skill_ctx.clear()
                except Exception:
                    pass
            else:
                try:
                    ctx.skill_ctx.set(previous.name, previous.path)
                except Exception:
                    pass
        return {
            "skill": skill_name,
            "tool": tool_name,
            "ok": bool(isinstance(result, Mapping) and result.get("ok") is not False),
            "duration_ms": round((time.time() - started) * 1000, 1),
            "result_keys": sorted(result.keys())[:8] if isinstance(result, Mapping) else [],
        }
    except Exception as exc:
        return {
            "skill": skill_name,
            "tool": tool_name,
            "ok": False,
            "duration_ms": round((time.time() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_cross_skill_probes(*, webspace_id: str, trial_id: str) -> list[dict[str, Any]]:
    probes = [
        ("demo_metrics_skill", "emit_demo_event", {"webspace_id": webspace_id, "action_id": f"ai_event_analysis:{trial_id}", "metric_id": "current"}),
        ("browsers_skill", "refresh_snapshot", {"webspace_id": webspace_id}),
        ("infrascope_skill", "refresh_snapshot", {"webspace_id": webspace_id, "task_goal": "ai_event_analysis_real_trial"}),
        ("subnet_env", "refresh_snapshot", {"webspace_id": webspace_id}),
        ("pair_new_device_skill", "create_pairing", {"webspace_id": webspace_id}),
    ]
    results: list[dict[str, Any]] = []
    for skill_name, tool_name, payload in probes:
        publish_event(
            "ai_event_analysis.real_trial.cross_skill.started",
            {"trial_id": trial_id, "webspace_id": webspace_id, "skill": skill_name, "tool": tool_name},
            source="ai_event_analysis_skill.real_trial",
        )
        outcome = _call_workspace_skill_tool(skill_name, tool_name, payload)
        results.append(outcome)
        publish_event(
            "ai_event_analysis.real_trial.cross_skill.completed",
            {
                "trial_id": trial_id,
                "webspace_id": webspace_id,
                "skill": skill_name,
                "tool": tool_name,
                "ok": outcome.get("ok"),
                "duration_ms": outcome.get("duration_ms"),
            },
            source="ai_event_analysis_skill.real_trial",
        )
    return results


def _value(features: Mapping[str, Any], key: str) -> float:
    raw = features.get(key, 0)
    try:
        return float(raw)
    except Exception:
        return 0.0


def _top_feature_names(features: Mapping[str, Any], limit: int = 3) -> list[str]:
    scored = sorted(
        ((key, abs(_value(features, key))) for key in features),
        key=lambda item: item[1],
        reverse=True,
    )
    return [key for key, value in scored[:limit] if value > 0]


def _rule_predict(window: Mapping[str, Any]) -> dict[str, Any]:
    features = window.get("features") if isinstance(window.get("features"), Mapping) else {}
    assert isinstance(features, Mapping)
    reasons: list[str] = []

    def mark(*names: str) -> list[str]:
        reasons.clear()
        reasons.extend(names)
        return list(reasons)

    if _value(features, "runtime_rebuild_total") >= 4:
        incident_type = "runtime_rebuild_churn"
        reason_codes = mark("runtime_rebuild_total", "event_total")
    elif _value(features, "drop_total") >= 12 or _value(features, "supersede_total") >= 40:
        incident_type = "eventbus_backpressure"
        reason_codes = mark("drop_total", "supersede_total", "event_total")
    elif _value(features, "yjs_write_total") >= 90:
        incident_type = "yjs_write_pressure"
        reason_codes = mark("yjs_write_total", "projection_refresh_total")
    elif _value(features, "same_projection_refresh_max") >= 12 or _value(features, "projection_refresh_total") >= 20:
        incident_type = "projection_refresh_storm"
        reason_codes = mark("same_projection_refresh_max", "projection_refresh_total")
    elif _value(features, "browser_reconnect_total") >= 4:
        incident_type = "browser_session_instability"
        reason_codes = mark("browser_reconnect_total")
    elif _value(features, "member_disconnect_total") >= 2:
        incident_type = "member_node_disconnect"
        reason_codes = mark("member_disconnect_total")
    else:
        incident_type = "normal"
        reason_codes = _top_feature_names(features, limit=2)

    severity = "info"
    if incident_type != "normal":
        severity = "critical" if _value(features, "error_total") >= 8 or _value(features, "drop_total") >= 40 or _value(features, "yjs_write_total") >= 140 else "warning"
    confidence = 0.95 if incident_type != "normal" and len(reason_codes) > 1 else 0.72 if incident_type != "normal" else 0.68
    return {
        "incident": incident_type != "normal",
        "incident_type": incident_type,
        "severity": severity,
        "confidence": confidence,
        "reasons": reason_codes,
    }


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _evaluate(windows: list[Mapping[str, Any]]) -> dict[str, Any]:
    predictions: list[dict[str, Any]] = []
    labels: list[str] = []
    predicted: list[str] = []
    for window in windows:
        label = window.get("label") if isinstance(window.get("label"), Mapping) else {}
        assert isinstance(label, Mapping)
        actual_type = str(label.get("incident_type") or "normal")
        pred = _rule_predict(window)
        predictions.append({"window_id": window.get("window_id"), "actual": actual_type, "predicted": pred})
        labels.append(actual_type)
        predicted.append(str(pred["incident_type"]))

    classes = sorted(set(_CLASSES) | set(labels) | set(predicted))
    confusion = {actual: {pred_class: 0 for pred_class in classes} for actual in classes}
    for actual, pred_class in zip(labels, predicted):
        confusion[actual][pred_class] += 1

    per_class = []
    for class_name in classes:
        tp = confusion[class_name][class_name]
        fp = sum(confusion[other][class_name] for other in classes if other != class_name)
        fn = sum(confusion[class_name][other] for other in classes if other != class_name)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_class.append(
            {
                "class": class_name,
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(_f1(precision, recall), 3),
                "support": labels.count(class_name),
            }
        )

    correct = sum(1 for actual, pred_class in zip(labels, predicted) if actual == pred_class)
    normal_total = sum(1 for actual in labels if actual == "normal")
    normal_fp = sum(1 for actual, pred_class in zip(labels, predicted) if actual == "normal" and pred_class != "normal")
    critical_total = 0
    critical_found = 0
    delays: list[float] = []
    reason_hits = 0
    reason_total = 0
    for window, pred in zip(windows, predictions):
        label = window.get("label") if isinstance(window.get("label"), Mapping) else {}
        timing = window.get("timing") if isinstance(window.get("timing"), Mapping) else {}
        actual_severity = str(label.get("severity") or "")
        if actual_severity == "critical":
            critical_total += 1
            if pred["predicted"]["incident_type"] == label.get("incident_type"):
                critical_found += 1
        if pred["predicted"]["incident"]:
            first_symptom = _value(timing, "first_symptom_s")
            delays.append(max(0.0, 60.0 - first_symptom))
        expected_reasons = set(label.get("reasons") or [])
        predicted_reasons = set(pred["predicted"].get("reasons") or [])
        if expected_reasons:
            reason_total += 1
            if expected_reasons & predicted_reasons:
                reason_hits += 1

    supported_rows = [row for row in per_class if row["support"] > 0]
    macro_f1 = sum(row["f1"] for row in supported_rows) / len(supported_rows) if supported_rows else 0.0
    return {
        "model": "rule_baseline_v1",
        "window_count": len(windows),
        "accuracy": round(correct / len(windows), 3) if windows else 0.0,
        "macro_f1": round(macro_f1, 3),
        "false_positive_rate": round(normal_fp / normal_total, 3) if normal_total else 0.0,
        "critical_recall": round(critical_found / critical_total, 3) if critical_total else 0.0,
        "avg_detection_delay_s": round(sum(delays) / len(delays), 1) if delays else 0.0,
        "top_reason_hit_rate": round(reason_hits / reason_total, 3) if reason_total else 0.0,
        "per_class": per_class,
        "confusion": confusion,
        "predictions": predictions,
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _metric_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": "macro_f1", "metric": "Macro-F1", "value": result.get("macro_f1"), "target": ">= 0.75", "status": "ok" if _value(result, "macro_f1") >= 0.75 else "warning"},
        {"id": "critical_recall", "metric": "Полнота критичных случаев", "value": result.get("critical_recall"), "target": ">= 0.85", "status": "ok" if _value(result, "critical_recall") >= 0.85 else "warning"},
        {"id": "false_positive_rate", "metric": "Ложные срабатывания нормы", "value": result.get("false_positive_rate"), "target": "<= 0.15", "status": "ok" if _value(result, "false_positive_rate") <= 0.15 else "warning"},
        {"id": "avg_detection_delay_s", "metric": "Средняя задержка обнаружения", "value": result.get("avg_detection_delay_s"), "target": "уменьшать", "status": "info"},
    ]


def _dataset_rows_for_real_logs(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    baseline = result.get("baseline_result") if isinstance(result.get("baseline_result"), Mapping) else {}
    assert isinstance(baseline, Mapping)
    return [
        {"id": "windows", "name": "Окна событий", "current": result.get("window_count"), "target": "500-1000+", "notes": "Построено по локальным логам AdaOS."},
        {"id": "records", "name": "Записи наблюдений", "current": result.get("record_count"), "target": "очищенные рабочие данные", "notes": "Исходные строки логов остаются локально, в интерфейс выводятся краткие записи."},
        {"id": "window_seconds", "name": "Размер окна", "current": result.get("window_seconds"), "target": "60/300/900 секунд", "notes": "Подбирается под эксперимент."},
        {"id": "label_source", "name": "Источник разметки", "current": "codex_reviewed_log_heuristic", "target": "ручная разметка", "notes": "Текущие метрики основаны на проверенной эвристике, а не на финальной ручной разметке."},
        {"id": "macro_f1", "name": "Macro-F1 по проверенной эвристике", "current": baseline.get("macro_f1"), "target": ">= 0.75", "notes": "Совпадение правила с проверенной эвристикой по логам."},
    ]


def _snapshot() -> dict[str, Any]:
    demo_windows = _synthetic_windows()
    demo_result = _evaluate(_synthetic_windows())
    return {
        "summary": {
            "label": "Анализ событий",
            "value": f"{demo_result['macro_f1']:.3f}",
            "subtitle": "Macro-F1 правиловой основы",
            "description": "Проверяемая задача анализа окон операционных событий.",
            "buttons": [
                {"id": "open", "label": "Открыть"},
                {"id": "run_demo", "label": "Запустить демо-проверку"},
            ],
        },
        "task": {
            "items": [
                {
                    "id": "objective",
                    "title": "Цель",
                    "description": "Разделить окна событий на нормальные и проблемные, а также показать главные признаки решения.",
                },
                {
                    "id": "dataset",
                    "title": "Состав данных",
                    "description": "Использовать компактные строки окон событий с числовыми признаками, разметкой и очищенными примерами.",
                },
                {
                    "id": "measurement",
                    "title": "Измерение",
                    "description": "Отслеживать Macro-F1, полноту критичных случаев, ложные срабатывания, задержку обнаружения и оценки по классам.",
                },
            ]
        },
        "dataset": {
            "items": [
                {"id": "windows", "name": "Размеченные окна", "current": len(demo_windows), "target": "500-1000+", "notes": "Одна строка на одно фиксированное окно событий."},
                {"id": "classes", "name": "Классы", "current": len(_CLASSES), "target": "6+", "notes": "Нормальное состояние и классы инцидентов."},
                {"id": "features", "name": "Группы признаков", "current": 9, "target": "eventbus/projection/Yjs/device/runtime", "notes": "Сводные числовые признаки для правила и ML-моделей."},
                {"id": "logs", "name": "Импорт локальных логов", "current": len(_log_candidates()), "target": "указанный путь и локальные кандидаты", "notes": "Ядро не меняется, навык читает локальные файлы только по запросу."},
            ]
        },
        "windows": {"items": _window_rows(demo_windows)},
        "metrics": {"items": _metric_rows(demo_result)},
        "per_class": {"items": demo_result["per_class"]},
        "chart": _readiness_chart(demo_result),
        "event_volume_chart": {
            "title": "Объём событий по окнам",
            "unit": "events",
            "points": _event_volume_points(demo_windows),
        },
        "class_distribution_chart": {
            "title": "Распределение классов по правилу",
            "unit": "windows",
            "points": _class_distribution_points(demo_windows),
        },
        "experiments": {
            "items": [
                {"id": "rule_baseline", "model": "Правиловая основа", "status": "реализовано", "macro_f1": demo_result["macro_f1"], "next_step": "Использовать как основу для сравнения будущих моделей."},
                {"id": "classical_ml", "model": "Классическая ML-модель", "status": "запланировано", "macro_f1": "", "next_step": "Обучить модель на импортированных окнах событий."},
                {"id": "neural_window_model", "model": "Нейросетевая модель окон", "status": "запланировано", "macro_f1": "", "next_step": "Проверить нейросетевой вариант на той же выборке."},
            ]
        },
        "details": {
            "result": demo_result,
            "success_criteria": {
                "macro_f1": ">= 0.75",
                "critical_recall": ">= 0.85",
                "false_positive_rate": "<= 0.15",
                "top_reasons": "required for every prediction",
            },
        },
    }


def _project_lab_snapshot(*, webspace_id: str = "desktop", force: bool = False) -> dict[str, Any]:
    return _project_sections(_snapshot(), webspace_id=webspace_id, force=force)


def _project_evaluation_result(result: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    sections = {
        "summary": {
            "label": "Анализ событий",
            "value": f"{_value(result, 'macro_f1'):.3f}",
            "subtitle": "Macro-F1 правиловой основы",
            "description": (
                f"accuracy={result.get('accuracy')} critical_recall={result.get('critical_recall')} "
                f"normal_fpr={result.get('false_positive_rate')}"
            ),
            "buttons": [
                {"id": "open", "label": "Открыть"},
                {"id": "run_demo", "label": "Запустить демо-проверку"},
                {"id": "analyze_logs", "label": "Проанализировать логи"},
            ],
        },
        "metrics": {"items": _metric_rows(result)},
        "per_class": {"items": list(result.get("per_class") or [])},
                "chart": _readiness_chart(result),
            }
    return _project_sections(sections, webspace_id=webspace_id)


def _project_windows_result(result: Mapping[str, Any], *, webspace_id: str, include_metrics: bool = False) -> dict[str, Any]:
    baseline = result.get("baseline_result") if isinstance(result.get("baseline_result"), Mapping) else {}
    sections = {
        "windows": {"items": list(result.get("rows") or [])},
        "event_volume_chart": result.get("event_volume_chart") or {"title": "Объём событий по окнам", "unit": "events", "points": []},
        "class_distribution_chart": result.get("class_distribution_chart") or {"title": "Распределение классов по правилу", "unit": "windows", "points": []},
        "dataset": {
            "items": _dataset_rows_for_real_logs(result)
        },
    }
    if include_metrics and baseline:
        sections.update(
            {
                "summary": {
                    "label": "Анализ событий",
                    "value": f"{_value(baseline, 'macro_f1'):.3f}",
                    "subtitle": "Macro-F1 по проверенной эвристике логов",
                    "description": (
                        f"real-log windows={result.get('window_count')} records={result.get('record_count')} "
                        "labels=codex_reviewed_log_heuristic"
                    ),
                    "buttons": [
                        {"id": "open", "label": "Открыть"},
                        {"id": "run_demo", "label": "Запустить демо-проверку"},
                        {"id": "analyze_logs", "label": "Проанализировать логи"},
                    ],
                },
                "metrics": {"items": _metric_rows(baseline)},
                "per_class": {"items": list(baseline.get("per_class") or [])},
                "chart": _readiness_chart(baseline, title="Готовность анализа реальных логов"),
            }
        )
    return _project_sections(sections, webspace_id=webspace_id)


def _project_trial_suite(result: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    baseline = result.get("baseline_result") if isinstance(result.get("baseline_result"), Mapping) else {}
    subscription = result.get("subscription_result") if isinstance(result.get("subscription_result"), Mapping) else {}
    sections = {
        "summary": {
            "label": "Анализ событий",
            "value": f"{_value(result, 'readiness_score'):.3f}",
            "subtitle": "оценка готовности сценариев",
            "description": (
                f"scenarios={result.get('scenario_count')} classes={len(result.get('scenario_classes') or [])} "
                f"routing_risk={((subscription.get('summary') or {}).get('risk_score') if isinstance(subscription.get('summary'), Mapping) else 'n/a')}"
            ),
            "buttons": [
                {"id": "open", "label": "Открыть"},
                {"id": "run_trials", "label": "Запустить синтетическую проверку"},
                {"id": "analyze_logs", "label": "Проанализировать логи"},
            ],
        },
        "dataset": {
            "items": [
                {"id": "trial_scenarios", "name": "Синтетические сценарии", "current": result.get("scenario_count"), "target": "норма + инциденты + маршрутизация", "notes": "Повторяемая локальная нагрузка для первой проверки."},
                {"id": "scenario_classes", "name": "Покрытые классы", "current": len(result.get("scenario_classes") or []), "target": "все классы правила", "notes": ", ".join(result.get("scenario_classes") or [])},
                {"id": "trial_windows", "name": "Окна событий", "current": result.get("window_count"), "target": "разные синтетические примеры", "notes": "Окна повторяемы и заранее размечены."},
                {"id": "subscription_records", "name": "События подписок", "current": result.get("record_count"), "target": "активные, простаивающие, шумные, без потребителя", "notes": "Проверяет анализ подписок без изменения ядра."},
                {"id": "label_source", "name": "Источник разметки", "current": result.get("label_source"), "target": "ручная разметка реальных данных", "notes": "Используется для быстрой проверки интерфейса и метрик."},
            ]
        },
        "windows": {"items": list(result.get("rows") or [])},
        "metrics": {"items": _metric_rows(baseline)},
        "per_class": {"items": list(baseline.get("per_class") or []) if isinstance(baseline, Mapping) else []},
        "chart": result.get("chart") or _readiness_chart(baseline, title="Готовность синтетической проверки", subscription_result=subscription),
        "event_volume_chart": result.get("event_volume_chart") or {"title": "Объём событий синтетической проверки", "unit": "events", "points": []},
        "class_distribution_chart": result.get("class_distribution_chart") or {"title": "Распределение классов синтетической проверки", "unit": "windows", "points": []},
        "subscription_summary": {"items": [
            {"id": key, "name": key.replace("_", " ").title(), "value": value}
            for key, value in ((subscription.get("summary") or {}) if isinstance(subscription, Mapping) else {}).items()
        ]},
        "subscription_edges": {"items": list(subscription.get("rows") or []) if isinstance(subscription, Mapping) else []},
        "subscription_metrics": subscription.get("metrics") if isinstance(subscription, Mapping) else {"items": []},
        "subscription_chart": subscription.get("chart") if isinstance(subscription, Mapping) else {"title": "Объём событий по типам", "unit": "events", "points": []},
        "experiments": {
            "items": [
                {"id": "synthetic_trial", "model": "Синтетическая проверка", "status": "реализовано", "macro_f1": baseline.get("macro_f1") if isinstance(baseline, Mapping) else "", "next_step": "Использовать перед проверкой реальных логов и ручной разметкой."},
                {"id": "real_log_review", "model": "Проверенная эвристика логов", "status": "реализовано", "macro_f1": "", "next_step": "Добавить ручную проверку разметки."},
                {"id": "subscription_routing", "model": "Анализ потока подписок", "status": "реализовано", "macro_f1": "", "next_step": "Добавить подтверждения доставки и задержки."},
            ]
        },
    }
    return _project_sections(sections, webspace_id=webspace_id, force=True)


def _project_real_trial_result(result: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    baseline = result.get("baseline_result") if isinstance(result.get("baseline_result"), Mapping) else {}
    sections = {
        "summary": {
            "label": "Анализ событий",
            "value": f"{_value(baseline, 'macro_f1'):.3f}",
            "subtitle": "анализ логов реальной проверки",
            "description": (
                f"trial_id={result.get('trial_id')} emitted={result.get('emitted_event_count')} "
                f"records={result.get('record_count')} windows={result.get('window_count')}"
            ),
            "buttons": [
                {"id": "open", "label": "Открыть"},
                {"id": "run_real_trial", "label": "Запустить проверку на логах"},
                {"id": "analyze_logs", "label": "Проанализировать логи"},
            ],
        },
        "dataset": {
            "items": [
                {"id": "trial_id", "name": "Идентификатор проверки", "current": result.get("trial_id"), "target": "уникален для запуска", "notes": "Помогает найти созданные события AdaOS в локальных логах."},
                {"id": "emitted", "name": "Опубликованные события AdaOS", "current": result.get("emitted_event_count"), "target": "реальная шина событий и логи", "notes": "События публикуются через SDK AdaOS, а не подставляются как готовые окна."},
                {"id": "cross_skill_probes", "name": "Проверки других навыков", "current": result.get("cross_skill_probe_count"), "target": "несколько существующих навыков", "notes": f"ok={result.get('cross_skill_ok_count')} exercises real tool/projection paths."},
                {"id": "records", "name": "Импортированные записи логов", "current": result.get("record_count"), "target": "содержит строки проверки", "notes": "Считывается из локальных логов после публикации событий."},
                {"id": "trial_records", "name": "Записи с меткой проверки", "current": result.get("trial_record_count"), "target": ">= опубликованных событий при включённом логировании", "notes": "Зависит от настроек логирования и хранения."},
                {"id": "windows", "name": "Окна событий", "current": result.get("window_count"), "target": ">= 1", "notes": "Построено по фактически импортированным логам."},
                {"id": "label_source", "name": "Источник разметки", "current": result.get("label_source"), "target": "ручная разметка для итоговой оценки", "notes": "Разметка реальной проверки пока остаётся проверенной эвристикой."},
            ]
        },
        "windows": {"items": list(result.get("rows") or [])},
        "metrics": {"items": _metric_rows(baseline)},
        "per_class": {"items": list(baseline.get("per_class") or []) if isinstance(baseline, Mapping) else []},
        "chart": _readiness_chart(baseline, title="Готовность проверки по логам"),
        "event_volume_chart": result.get("event_volume_chart") or {"title": "Объём событий проверки по окнам", "unit": "events", "points": []},
        "class_distribution_chart": result.get("class_distribution_chart") or {"title": "Распределение классов проверки", "unit": "windows", "points": []},
    }
    return _project_sections(sections, webspace_id=webspace_id, force=True)


def _publish_result(result: Mapping[str, Any], *, webspace_id: str) -> None:
    compact = _compact_evaluation_result(result)
    payload = [
        {
            "id": "summary",
            "title": f"Rule baseline: macro-F1 {result.get('macro_f1')}",
            "description": (
                f"accuracy={result.get('accuracy')} critical_recall={result.get('critical_recall')} "
                f"normal_fpr={result.get('false_positive_rate')} windows={result.get('window_count')}"
            ),
            "content": compact,
        },
        {
            "id": "criteria",
            "title": "Критерии успешности исследования",
            "description": "Macro-F1 >= 0,75, полнота критичных случаев >= 0,85, ложные срабатывания нормы <= 0,15.",
            "content": {"metrics": _metric_rows(result), "per_class": result.get("per_class")},
        },
    ]
    try:
        stream_publish(_RESULTS_RECEIVER, payload, _meta={"webspace_id": webspace_id})
    except Exception:
        # Tool calls should remain usable in validation and tests where the
        # AdaOS runtime context is intentionally not bootstrapped.
        pass


def _publish_dataset_result(result: Mapping[str, Any], *, webspace_id: str) -> None:
    compact = _compact_dataset_result(result)
    baseline = compact.get("baseline") if isinstance(compact.get("baseline"), Mapping) else {}
    payload = [
        {
            "id": "dataset-windows",
            "title": f"Built {result.get('window_count')} event windows",
            "description": (
                f"records={result.get('record_count')} window_seconds={result.get('window_seconds')} "
                f"reviewed_macro_f1={baseline.get('macro_f1') if baseline else 'n/a'} "
                "labels are heuristic until manually confirmed"
            ),
            "content": compact,
        }
    ]
    try:
        stream_publish(_RESULTS_RECEIVER, payload, _meta={"webspace_id": webspace_id})
    except Exception:
        pass


def _load_projection_relevance_agent() -> Any:
    module_name = "ai_event_analysis_projection_relevance_agent"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    module_path = _SKILL_ROOT / "handlers" / "projection_relevance_agent.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load projection relevance agent from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _projection_relevance_experiments(result: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
    baseline = result.get("rule_baseline") if isinstance(result.get("rule_baseline"), Mapping) else {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), Mapping) else {}
    if kind == "prediction":
        event = result.get("event") if isinstance(result.get("event"), Mapping) else {}
        return {
            "title": "Агент релевантности проекций",
            "updatedAt": _now_iso(),
            "items": [
                {
                    "id": "projection-relevance-prediction",
                    "label": "Рекомендованный план обновления",
                    "value": ", ".join(result.get("affected_projections") or []) or "обновление не требуется",
                    "note": f"event={event.get('event_type')} guardrail=advisory",
                }
            ],
            "details": result,
        }
    if kind == "summary":
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        return {
            "title": "Агент релевантности проекций",
            "updatedAt": _now_iso(),
            "items": [
                {
                    "id": "projection-relevance-decision",
                    "label": "Рекомендательное решение",
                    "value": result.get("decision"),
                    "note": f"ready={result.get('advisory_ready')}",
                },
                {
                    "id": "projection-relevance-summary-f1",
                    "label": "ML F1",
                    "value": metrics.get("f1"),
                    "note": f"rule baseline F1={baseline.get('f1')}",
                },
                {
                    "id": "projection-relevance-summary-critical",
                    "label": "Пропущенные критичные проекции",
                    "value": metrics.get("missed_critical_projection_total"),
                    "note": "обязательная проверка безопасности",
                },
            ],
            "details": result,
        }
    return {
        "title": "Агент релевантности проекций",
        "updatedAt": _now_iso(),
        "items": [
            {
                "id": "projection-relevance-f1",
                "label": "ML F1",
                "value": metrics.get("f1"),
                "note": f"rule baseline F1={baseline.get('f1')}",
            },
            {
                "id": "projection-relevance-write-reduction",
                "label": "Сокращение записей",
                "value": metrics.get("write_reduction_ratio"),
                "note": f"delta={comparison.get('write_reduction_delta')}",
            },
            {
                "id": "projection-relevance-critical-misses",
                "label": "Пропущенные критичные проекции",
                "value": metrics.get("missed_critical_projection_total"),
                "note": "защищённый диспетчер остаётся главным",
            },
        ],
        "details": result,
    }


def _projection_relevance_trial_event(*, event_type: str, webspace_id: str, agent: Any) -> dict[str, Any]:
    requested_type = str(event_type or "").strip()
    selected_type = requested_type if requested_type in agent.EVENT_TYPES else "core.update.status"
    targets = list(agent.BROAD_RULE_TARGETS.get(selected_type) or [])
    decoys = [key for key in agent.PROJECTION_KEYS if key not in targets]
    active_subscriptions = list(dict.fromkeys([*targets, *decoys[:1]]))
    entity_refs = {
        "skill.installed": "skill:infrascope",
        "subnet.member.link.down": "node:member-1",
        "core.update.status": "node:hub-1",
        "entity.alias.conflict.detected": "entity:room:kitchen",
        "projection.refresh.failed": "projection:platform:diagnostics",
        "browser.transport.not_ready": "webspace:desktop",
        "browser.session.changed": "webspace:desktop",
    }
    return {
        "event_type": selected_type,
        "event_class": agent.EVENT_CLASSES[selected_type],
        "entity_ref": entity_refs.get(selected_type, "node:hub-1"),
        "node_id": "node:hub-1",
        "webspace_id": webspace_id,
        "active_subscriptions": active_subscriptions,
        "projection_statuses": {
            key: "ready" for key in agent.PROJECTION_KEYS
        },
        "consumer_kinds": {
            key: ("modal" if key == "platform:notifications" else "page")
            for key in agent.PROJECTION_KEYS
        },
        "pinned_flags": {
            key: key == "platform:notifications"
            for key in agent.PROJECTION_KEYS
        },
    }


def _projection_relevance_trial_sections(result: Mapping[str, Any]) -> dict[str, Any]:
    event = result.get("event") if isinstance(result.get("event"), Mapping) else {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), Mapping) else {}
    safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
    agent_plan = result.get("agent_plan") if isinstance(result.get("agent_plan"), Mapping) else {}
    rule_baseline = result.get("rule_baseline") if isinstance(result.get("rule_baseline"), Mapping) else {}
    active = list(result.get("active_projection_set") or [])
    agent_refresh = list(agent_plan.get("refresh") or [])
    rule_refresh = list(rule_baseline.get("demanded_refresh") or [])
    missed_critical_total = int(safety.get("missed_critical_projection_total") or 0)
    action_agreement = float(comparison.get("action_agreement_ratio") or 0.0)
    return {
        "agent_summary": {
            "items": [
                {
                    "id": "trial_id",
                    "name": "Идентификатор проверки",
                    "value": result.get("trial_id"),
                    "notes": "Уникальный идентификатор управляемой проверки через интерфейс.",
                },
                {
                    "id": "event_type",
                    "name": "Операционное событие",
                    "value": event.get("event_type"),
                    "notes": "Контролируемые входные данные для рекомендательной модели.",
                },
                {
                    "id": "demand_source",
                    "name": "Источник спроса",
                    "value": result.get("demand_source"),
                    "notes": "Явный набор активных проекций для повторяемой демонстрации.",
                },
                {
                    "id": "active_projection_set",
                    "name": "Активный набор проекций",
                    "value": len(active),
                    "notes": ", ".join(active),
                },
                {
                    "id": "agent_refresh",
                    "name": "Рекомендации агента",
                    "value": len(agent_refresh),
                    "notes": ", ".join(agent_refresh) or "нет",
                },
                {
                    "id": "rule_refresh",
                    "name": "Обновления по правилу",
                    "value": len(rule_refresh),
                    "notes": ", ".join(rule_refresh) or "нет",
                },
                {
                    "id": "dispatch_applied",
                    "name": "Обновление применено",
                    "value": str(bool(safety.get("dispatch_applied"))).lower(),
                    "notes": "Агент остаётся вне основного контура обновления.",
                },
            ]
        },
        "agent_plan": {"items": list(result.get("plan_rows") or [])},
        "agent_gates": {
            "items": [
                {
                    "id": "critical",
                    "gate": "Нет пропущенных критичных проекций",
                    "value": missed_critical_total,
                    "target": "0",
                    "status": "ok" if missed_critical_total == 0 else "warning",
                    "notes": "Диагностика и уведомления не должны пропадать.",
                },
                {
                    "id": "agreement",
                    "gate": "Согласие с правиловым baseline",
                    "value": action_agreement,
                    "target": "проверить",
                    "status": "ok" if action_agreement >= 0.8 else "warning",
                    "notes": "Отличия остаются видимыми для проверки.",
                },
                {
                    "id": "advisory",
                    "gate": "Только рекомендательное выполнение",
                    "value": str(bool(safety.get("agent_is_advisory"))).lower(),
                    "target": "true",
                    "status": "ok" if safety.get("agent_is_advisory") is True else "warning",
                    "notes": "Защищённый диспетчер остаётся главным.",
                },
                {
                    "id": "dispatch",
                    "gate": "Нет автоматической записи проекций",
                    "value": str(bool(safety.get("dispatch_applied"))).lower(),
                    "target": "false",
                    "status": "ok" if safety.get("dispatch_applied") is False else "warning",
                    "notes": "Проверка показывает совет, но не меняет состояние проекций.",
                },
            ]
        },
    }


def _publish_projection_relevance_trial_result(result: Mapping[str, Any], *, webspace_id: str) -> None:
    try:
        _project_sections(
            _projection_relevance_trial_sections(result),
            webspace_id=webspace_id,
            force=True,
        )
    except Exception:
        pass
    try:
        event = result.get("event") if isinstance(result.get("event"), Mapping) else {}
        comparison = result.get("comparison") if isinstance(result.get("comparison"), Mapping) else {}
        safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
        agent_plan = result.get("agent_plan") if isinstance(result.get("agent_plan"), Mapping) else {}
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "projection-relevance-agent-trial",
                    "title": f"Проверка агента: {event.get('event_type')}",
                    "description": (
                        f"refresh={len(agent_plan.get('refresh') or [])} "
                        f"agreement={comparison.get('action_agreement_ratio')} "
                        f"missed_critical={safety.get('missed_critical_projection_total')} "
                        "advisory_only=true"
                    ),
                    "content": {
                        "trial_id": result.get("trial_id"),
                        "event": event,
                        "active_projection_set": result.get("active_projection_set"),
                        "agent_plan": agent_plan,
                        "rule_baseline": result.get("rule_baseline"),
                        "comparison": comparison,
                        "safety": safety,
                    },
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass


def _publish_projection_relevance_result(result: Mapping[str, Any], *, webspace_id: str, kind: str) -> None:
    try:
        _project_sections(
            {"experiments": _projection_relevance_experiments(result, kind=kind)},
            webspace_id=webspace_id,
            force=True,
        )
    except Exception:
        pass
    try:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        baseline = result.get("rule_baseline") if isinstance(result.get("rule_baseline"), Mapping) else {}
        if kind == "prediction":
            title = "Projection relevance prediction"
            description = (
                f"affected={len(result.get('affected_projections') or [])} "
                f"guardrail=advisory"
            )
        else:
            title = f"Projection relevance model F1 {metrics.get('f1')}"
            description = (
                f"rule_f1={baseline.get('f1')} write_reduction={metrics.get('write_reduction_ratio')} "
                f"critical_misses={metrics.get('missed_critical_projection_total')}"
            )
        if kind == "summary":
            title = f"Projection relevance agent {result.get('decision')}"
            description = (
                f"ready={result.get('advisory_ready')} f1={metrics.get('f1')} "
                f"write_reduction={metrics.get('write_reduction_ratio')}"
            )
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": f"projection-relevance-{kind}",
                    "title": title,
                    "description": description,
                    "content": result,
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass


@tool("get_lab_snapshot")
def get_lab_snapshot(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    if bool(body.get("project")):
        _project_lab_snapshot(webspace_id=_webspace_id_from_payload(body), force=bool(body.get("force")))
    return {"ok": True, "snapshot": _snapshot()}


@tool("refresh_snapshot")
def refresh_snapshot(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Обновление демо", percent=20, status="выполняется", description="Обновляем данные интерфейса анализа.")
    _publish_run_notice(webspace_id=webspace_id, title="Обновление демо запущено", description="Интерфейс получает актуальные данные.", stage="running")
    projected = _project_lab_snapshot(webspace_id=webspace_id, force=True)
    _mark_progress(webspace_id=webspace_id, label="Обновление демо", percent=100, status="готово", description="Демо-данные обновлены.")
    return {"ok": True, "projected": projected}


@tool("rehydrate")
def rehydrate(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    return refresh_snapshot(payload if isinstance(payload, Mapping) else {})


@tool("run_demo_evaluation")
def run_demo_evaluation(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    webspace_id = _webspace_id_from_payload(payload)
    _mark_progress(webspace_id=webspace_id, label="Демо-оценка", percent=15, status="выполняется", description="Проверяем подготовленный демонстрационный набор событий.")
    _publish_run_notice(webspace_id=webspace_id, title="Демо-оценка запущена", description="Считаем основные метрики на демонстрационных окнах событий.", stage="running")
    result = _evaluate(_synthetic_windows())
    _project_evaluation_result(result, webspace_id=webspace_id)
    _publish_result(result, webspace_id=webspace_id)
    try:
        publish_event(
            "ai_event_analysis.evaluation.completed",
            {"model": result["model"], "macro_f1": result["macro_f1"], "webspace_id": webspace_id},
            source="ai_event_analysis_skill",
        )
    except Exception:
        pass
    _mark_progress(webspace_id=webspace_id, label="Демо-оценка", percent=100, status="готово", description=f"Macro-F1={result.get('macro_f1')}, окон={result.get('window_count')}.")
    return {"ok": True, "result": result}


@tool("run_trial_suite")
def run_trial_suite(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    webspace_id = _webspace_id_from_payload(payload)
    _mark_progress(webspace_id=webspace_id, label="Синтетическая проверка", percent=10, status="выполняется", description="Генерируем синтетические сценарии и считаем метрики.")
    _publish_run_notice(webspace_id=webspace_id, title="Синтетическая проверка запущена", description="Формируем набор событий, окон и подписок для повторяемой проверки.", stage="running")
    result = _trial_suite_result()
    _project_trial_suite(result, webspace_id=webspace_id)
    baseline = result.get("baseline_result") if isinstance(result.get("baseline_result"), Mapping) else {}
    subscription = result.get("subscription_result") if isinstance(result.get("subscription_result"), Mapping) else {}
    summary = subscription.get("summary") if isinstance(subscription.get("summary"), Mapping) else {}
    try:
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "trial-suite",
                    "title": f"Trial suite readiness {result.get('readiness_score')}",
                    "description": (
                        f"scenarios={result.get('scenario_count')} macro_f1={baseline.get('macro_f1')} "
                        f"critical_recall={baseline.get('critical_recall')} routing_risk={summary.get('risk_score')}"
                    ),
                    "content": {
                        "readiness_score": result.get("readiness_score"),
                        "scenario_classes": result.get("scenario_classes"),
                        "baseline": _compact_evaluation_result(baseline),
                        "subscription_summary": summary,
                    },
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass
    _mark_progress(webspace_id=webspace_id, label="Синтетическая проверка", percent=100, status="готово", description=f"Готовность={result.get('readiness_score')}, сценариев={result.get('scenario_count')}.")
    return {"ok": True, "result": result}


@tool("run_real_trial")
def run_real_trial(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Проверка на логах", percent=10, status="выполняется", description="Публикуем события AdaOS и собираем локальные логи.")
    _publish_run_notice(webspace_id=webspace_id, title="Проверка на логах запущена", description="События отправляются через AdaOS SDK, затем анализируются локальные логи.", stage="running")
    trial_id = str(body.get("trial_id") or f"real-{int(time.time())}")
    event_count = max(12, min(24, int(_value(body, "event_count") or 14)))
    max_lines = max(_MAX_DEFAULT_LOG_LINES, min(_MAX_EXPLICIT_LOG_LINES, int(_value(body, "max_lines") or 600)))
    emitted = _emit_real_trial_events(webspace_id=webspace_id, trial_id=trial_id, event_count=event_count)
    _mark_progress(webspace_id=webspace_id, label="Проверка на логах", percent=35, status="выполняется", description=f"Опубликовано событий: {emitted['emitted_event_count']}.")
    cross_skill_enabled = body.get("cross_skill") is not False
    probe_results = _run_cross_skill_probes(webspace_id=webspace_id, trial_id=trial_id) if cross_skill_enabled else []
    _mark_progress(webspace_id=webspace_id, label="Проверка на логах", percent=60, status="выполняется", description="Читаем логи и строим окна событий.")
    time.sleep(float(body.get("settle_seconds") or 0.25))
    imported = import_local_logs({"max_lines": max_lines})
    records = [item for item in imported.get("records", []) if isinstance(item, Mapping)]
    trial_records = [
        item
        for item in records
        if trial_id in str(item.get("message") or "") or "ai_event_analysis.real_trial" in str(item.get("message") or "")
    ]
    analysis_records = trial_records or records
    windows = _records_to_windows(
        analysis_records,
        window_seconds=int(_value(body, "window_seconds") or 60),
        node_id=str(body.get("node_id") or "local-real-trial"),
        subnet_id=str(body.get("subnet_id") or "local"),
        webspace_id=webspace_id,
    )
    baseline_result = _evaluate(windows)
    result = {
        "mode": "real_trial",
        "trial_id": trial_id,
        "emitted_event_count": emitted["emitted_event_count"],
        "emitted_sample": emitted["sample"],
        "cross_skill_enabled": cross_skill_enabled,
        "cross_skill_probe_count": len(probe_results),
        "cross_skill_ok_count": sum(1 for item in probe_results if item.get("ok") is True),
        "cross_skill_results": probe_results,
        "record_count": len(records),
        "trial_record_count": len(trial_records),
        "used_record_count": len(analysis_records),
        "window_count": len(windows),
        "window_seconds": int(_value(body, "window_seconds") or 60),
        "label_source": "codex_reviewed_log_heuristic",
        "baseline_result": baseline_result,
        "windows": windows[:_MAX_TOOL_WINDOWS] if bool(body.get("include_windows")) else [],
        "rows": _window_rows(windows),
        "event_volume_chart": {
            "title": "Объём событий проверки по окнам",
            "unit": "events",
            "points": _event_volume_points(windows),
        },
        "class_distribution_chart": {
            "title": "Распределение классов проверки",
            "unit": "windows",
            "points": _class_distribution_points(windows),
        },
        "sources": (imported.get("summary") or {}).get("sources") if isinstance(imported.get("summary"), Mapping) else [],
        "built_at": _now_iso(),
    }
    _project_real_trial_result(result, webspace_id=webspace_id)
    _publish_dataset_result(result, webspace_id=webspace_id)
    try:
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "real-trial-analysis",
                    "title": f"Real trial analyzed {result['window_count']} windows",
                    "description": (
                        f"trial_id={trial_id} emitted={result['emitted_event_count']} "
                        f"trial_records={result['trial_record_count']} macro_f1={baseline_result.get('macro_f1')}"
                    ),
                    "content": {
                        "trial_id": trial_id,
                        "baseline": _compact_evaluation_result(baseline_result),
                        "cross_skill_results": probe_results,
                        "record_count": result["record_count"],
                        "trial_record_count": result["trial_record_count"],
                        "sources": result["sources"],
                    },
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass
    _mark_progress(webspace_id=webspace_id, label="Проверка на логах", percent=100, status="готово", description=f"Построено окон: {result['window_count']}, записей логов: {result['record_count']}.")
    return {"ok": True, "result": result}


@tool("train_projection_relevance_agent")
def train_projection_relevance_agent(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Обучение агента", percent=15, status="выполняется", description="Готовим синтетическую выборку и обучаем модель релевантности проекций.")
    agent = _load_projection_relevance_agent()
    result = agent.train_projection_relevance_agent(
        sample_count=int(_value(body, "sample_count") or 420),
        seed=int(_value(body, "seed") or 42),
        epochs=int(_value(body, "epochs") or 90),
        learning_rate=float(_value(body, "learning_rate") or 0.18),
        threshold=float(_value(body, "threshold") or 0.5),
    )
    result["webspace_id"] = webspace_id
    result["trained_at"] = _now_iso()
    _publish_projection_relevance_result(result, webspace_id=webspace_id, kind="training")
    _mark_progress(webspace_id=webspace_id, label="Обучение агента", percent=100, status="готово", description="Модель агента обучена и оценена.")
    return {"ok": True, "result": result}


@tool("predict_projection_refresh_plan")
def predict_projection_refresh_plan(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Прогноз агента", percent=30, status="выполняется", description="Оцениваем релевантность проекций для входного события.")
    event = body.get("event") if isinstance(body.get("event"), Mapping) else body
    agent = _load_projection_relevance_agent()
    result = agent.predict_projection_refresh_plan(
        event,
        threshold=float(_value(body, "threshold") or 0.5),
    )
    result["webspace_id"] = webspace_id
    result["predicted_at"] = _now_iso()
    _publish_projection_relevance_result(result, webspace_id=webspace_id, kind="prediction")
    _mark_progress(webspace_id=webspace_id, label="Прогноз агента", percent=100, status="готово", description=f"Рекомендаций: {len(result.get('affected_projections') or [])}.")
    return {"ok": True, "result": result}


@tool("run_projection_relevance_agent_trial")
def run_projection_relevance_agent_trial(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    body = dict(payload) if isinstance(payload, Mapping) else {}
    body.update(kwargs)
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Проверка агента", percent=15, status="выполняется", description="Агент анализирует событие, спрос интерфейса и активные проекции.")
    _publish_run_notice(webspace_id=webspace_id, title="Проверка агента запущена", description="Формируется рекомендованный план обновления проекций.", stage="running")
    trial_id = str(body.get("trial_id") or f"agent-{int(time.time())}")
    agent = _load_projection_relevance_agent()
    event = (
        dict(body.get("event"))
        if isinstance(body.get("event"), Mapping)
        else _projection_relevance_trial_event(
            event_type=str(body.get("event_type") or "core.update.status"),
            webspace_id=webspace_id,
            agent=agent,
        )
    )
    try:
        publish_event(
            "ai_event_analysis.projection_relevance_agent_trial.started",
            {
                "trial_id": trial_id,
                "event_type": event.get("event_type"),
                "webspace_id": webspace_id,
            },
            source="ai_event_analysis_skill",
        )
    except Exception:
        pass
    result = agent.build_projection_relevance_trial(
        event,
        threshold=float(_value(body, "threshold") or 0.5),
    )
    _mark_progress(webspace_id=webspace_id, label="Проверка агента", percent=70, status="выполняется", description="Сравниваем рекомендации агента с правиловой основой.")
    result.update(
        {
            "trial_id": trial_id,
            "webspace_id": webspace_id,
            "event_source": "controlled_operational_event",
            "demand_source": "controlled_web_ui_trial",
            "generated_at": _now_iso(),
        }
    )
    _publish_projection_relevance_trial_result(result, webspace_id=webspace_id)
    try:
        safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
        publish_event(
            "ai_event_analysis.projection_relevance_agent_trial.completed",
            {
                "trial_id": trial_id,
                "event_type": result["event"]["event_type"],
                "webspace_id": webspace_id,
                "missed_critical_projection_total": safety.get("missed_critical_projection_total"),
                "dispatch_applied": safety.get("dispatch_applied"),
            },
            source="ai_event_analysis_skill",
        )
    except Exception:
        pass
    safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
    _mark_progress(webspace_id=webspace_id, label="Проверка агента", percent=100, status="готово", description=f"Критичных пропусков: {safety.get('missed_critical_projection_total')}.")
    return {"ok": True, "result": result}


@tool("summarize_projection_relevance_agent")
def summarize_projection_relevance_agent(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Сводка агента", percent=15, status="выполняется", description="Обучаем и кратко оцениваем советующего агента.")
    agent = _load_projection_relevance_agent()
    result = agent.summarize_projection_relevance_agent(
        sample_count=int(_value(body, "sample_count") or 420),
        seed=int(_value(body, "seed") or 42),
        epochs=int(_value(body, "epochs") or 90),
        threshold=float(_value(body, "threshold") or 0.5),
    )
    result["webspace_id"] = webspace_id
    result["summarized_at"] = _now_iso()
    _publish_projection_relevance_result(result, webspace_id=webspace_id, kind="summary")
    _mark_progress(webspace_id=webspace_id, label="Сводка агента", percent=100, status="готово", description=f"Решение: {result.get('decision')}.")
    return {"ok": True, "result": result}


@tool("evaluate_windows")
def evaluate_windows(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Оценка окон", percent=25, status="выполняется", description="Считаем качество классификации окон событий.")
    raw_windows = body.get("windows")
    windows = [item for item in raw_windows if isinstance(item, Mapping)] if isinstance(raw_windows, list) else _synthetic_windows()
    result = _evaluate(windows)
    _project_evaluation_result(result, webspace_id=webspace_id)
    _publish_result(result, webspace_id=webspace_id)
    _mark_progress(webspace_id=webspace_id, label="Оценка окон", percent=100, status="готово", description=f"Оценено окон: {result.get('window_count')}.")
    return {"ok": True, "result": result}


@tool("import_local_logs")
def import_local_logs(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    max_lines = int(_value(body, "max_lines") or _MAX_DEFAULT_LOG_LINES)
    raw_path = str(body.get("path") or "").strip()
    paths = [Path(raw_path)] if raw_path else _log_candidates()[:_MAX_AUTO_LOG_SOURCES]
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in paths:
        rows = _read_log_records(path, max_lines=max_lines)
        if rows:
            records.extend(rows)
            sources.append({"path": str(path), "records": len(rows)})
    return {
        "ok": True,
        "records": records,
        "summary": {
            "source_count": len(sources),
            "record_count": len(records),
            "sources": sources,
            "imported_at": _now_iso(),
        },
    }


@tool("build_event_windows")
def build_event_windows(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Построение окон", percent=20, status="выполняется", description="Читаем события и группируем их во временные окна.")
    _publish_run_notice(webspace_id=webspace_id, title="Построение окон запущено", description="Логи разбиваются на компактные окна для анализа.", stage="running")
    raw_records = body.get("records")
    if isinstance(raw_records, list):
        records = [item for item in raw_records if isinstance(item, Mapping)]
    else:
        imported = import_local_logs(body)
        records = [item for item in imported.get("records", []) if isinstance(item, Mapping)]
    window_seconds = int(_value(body, "window_seconds") or 60)
    windows = _records_to_windows(
        records,
        window_seconds=window_seconds,
        node_id=str(body.get("node_id") or "local"),
        subnet_id=str(body.get("subnet_id") or "local"),
        webspace_id=webspace_id,
    )
    include_windows = bool(body.get("include_windows")) or isinstance(raw_records, list)
    result_windows = windows[:_MAX_TOOL_WINDOWS] if include_windows else []
    baseline_result = _evaluate(windows)
    result = {
        "window_count": len(windows),
        "record_count": len(records),
        "window_seconds": window_seconds,
        "label_source": "codex_reviewed_log_heuristic",
        "baseline_result": baseline_result,
        "windows": result_windows,
        "windows_truncated": include_windows and len(windows) > len(result_windows),
        "rows": _window_rows(windows),
        "event_volume_chart": {
            "title": "Объём событий по окнам",
            "unit": "events",
            "points": _event_volume_points(windows),
        },
        "class_distribution_chart": {
            "title": "Распределение классов по правилу",
            "unit": "windows",
            "points": _class_distribution_points(windows),
        },
        "built_at": _now_iso(),
    }
    _project_windows_result(result, webspace_id=webspace_id, include_metrics=True)
    _publish_dataset_result(result, webspace_id=webspace_id)
    _mark_progress(webspace_id=webspace_id, label="Построение окон", percent=100, status="готово", description=f"Построено окон: {len(windows)}.")
    return {"ok": True, "result": result}


@tool("analyze_local_logs")
def analyze_local_logs(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = dict(payload) if isinstance(payload, Mapping) else {}
    body.setdefault("window_seconds", 60)
    return build_event_windows(body)


@tool("analyze_subscription_flow")
def analyze_subscription_flow(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Проверка подписок", percent=25, status="выполняется", description="Анализируем связь событий, источников и потребителей.")
    _publish_run_notice(webspace_id=webspace_id, title="Проверка подписок запущена", description="Считаем активные, шумные и отсутствующие связи подписок.", stage="running")
    raw_records = body.get("records")
    if isinstance(raw_records, list):
        records = [item for item in raw_records if isinstance(item, Mapping)]
    else:
        imported = import_local_logs(body)
        records = [item for item in imported.get("records", []) if isinstance(item, Mapping)]
    result = _subscription_flow_from_records(records)
    result["record_count"] = len(records)
    result["analyzed_at"] = _now_iso()
    _project_subscription_flow(result, webspace_id=webspace_id)
    try:
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "subscription-flow",
                    "title": f"Риск потока подписок {result['summary']['risk_score']}",
                    "description": (
                        f"declared={result['summary']['declared_subscriptions']} "
                        f"observed_types={result['summary']['observed_event_types']} "
                        f"missing={result['summary']['missing_consumers']}"
                    ),
                    "content": result,
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass
    _mark_progress(webspace_id=webspace_id, label="Проверка подписок", percent=100, status="готово", description=f"Риск маршрутизации: {result['summary']['risk_score']}.")
    return {"ok": True, "result": result}


@tool("analyze_observability_health")
def analyze_observability_health(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Проверка состояния", percent=20, status="выполняется", description="Проверяем наблюдаемость логов, проекций и runtime.")
    _publish_run_notice(webspace_id=webspace_id, title="Проверка состояния запущена", description="Оцениваем качество данных для анализа событий.", stage="running")
    imported = import_local_logs({"path": body.get("path"), "max_lines": int(_value(body, "max_lines") or 800)} if body.get("path") else {"max_lines": int(_value(body, "max_lines") or 800)})
    records = [item for item in imported.get("records", []) if isinstance(item, Mapping)]
    window_seconds = int(_value(body, "window_seconds") or 60)
    windows = _records_to_windows(
        records,
        window_seconds=window_seconds,
        node_id=str(body.get("node_id") or "local-observability"),
        subnet_id=str(body.get("subnet_id") or "local"),
        webspace_id=webspace_id,
    )
    result = _observability_health_from_records(records)
    result["window_count"] = len(windows)
    result["window_seconds"] = window_seconds
    result["sources"] = (imported.get("summary") or {}).get("sources") if isinstance(imported.get("summary"), Mapping) else []
    _project_observability_health(result, webspace_id=webspace_id, windows=windows)
    try:
        summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
        stream_publish(
            _RESULTS_RECEIVER,
            [
                {
                    "id": "observability-health",
                    "title": f"Observability health {summary.get('observability_score')}",
                    "description": (
                        f"records={result.get('record_count')} windows={result.get('window_count')} "
                        f"schema={summary.get('schema_score')} correlation={summary.get('correlation_score')}"
                    ),
                    "content": result,
                }
            ],
            _meta={"webspace_id": webspace_id},
        )
    except Exception:
        pass
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    _mark_progress(webspace_id=webspace_id, label="Проверка состояния", percent=100, status="готово", description=f"Оценка наблюдаемости: {summary.get('observability_score')}.")
    return {"ok": True, "result": result}


@tool("export_event_windows_jsonl")
def export_event_windows_jsonl(payload: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    _mark_progress(webspace_id=webspace_id, label="Экспорт JSONL", percent=20, status="выполняется", description="Готовим окна событий для выгрузки в JSONL.")
    _publish_run_notice(webspace_id=webspace_id, title="Экспорт JSONL запущен", description="Формируется файл с подготовленными окнами событий.", stage="running")
    raw_windows = body.get("windows")
    if isinstance(raw_windows, list):
        windows = [item for item in raw_windows if isinstance(item, Mapping)]
    else:
        imported = import_local_logs(body)
        records = [item for item in imported.get("records", []) if isinstance(item, Mapping)]
        windows = _records_to_windows(
            records,
            window_seconds=int(_value(body, "window_seconds") or 60),
            node_id=str(body.get("node_id") or "local"),
            subnet_id=str(body.get("subnet_id") or "local"),
            webspace_id=webspace_id,
        )
    raw_path = str(body.get("path") or "").strip()
    path = Path(raw_path) if raw_path else _DEFAULT_EXPORT_PATH
    export = _export_jsonl(windows, path)
    _mark_progress(webspace_id=webspace_id, label="Экспорт JSONL", percent=100, status="готово", description=f"Файл создан: {export.get('path')}.")
    return {"ok": True, "export": export}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _RESULTS_RECEIVER:
        return
    webspace_id = _webspace_id_from_payload(payload)
    _publish_result(_evaluate(_synthetic_windows()), webspace_id=webspace_id)


@subscribe("ai_event_analysis.evaluate_requested")
def on_evaluate_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    run_demo_evaluation(payload if isinstance(payload, Mapping) else {})
