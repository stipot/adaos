from __future__ import annotations

import hashlib
import json
import logging
import gc
import os
import time
import threading
import tracemalloc
from functools import partial
from typing import Any, Mapping, Optional

import anyio
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from adaos.domain import Event, client_subscription_contract_snapshot, event_envelope_contract_snapshot
from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.api.auth import ensure_token, require_token, resolve_presented_token
from adaos.services.agent_context import get_ctx
from adaos.services.bootstrap import (
    is_ready,
    load_config,
    request_hub_root_reconnect,
    request_member_hub_reconnect,
    request_hub_root_route_reset,
    switch_role,
)
from adaos.services.node_display import node_display_from_config
from adaos.services.io_web.desktop import WebDesktopInstalled, WebDesktopService, WebDesktopSnapshot
from adaos.services.media_library import (
    ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
    guess_media_type,
    list_media_files,
    media_capabilities,
    media_file_path,
    media_snapshot,
)
from adaos.services.node_config import set_node_names as save_node_names_config
from adaos.services.reliability import (
    media_plane_runtime_snapshot,
    reliability_snapshot,
    yjs_sync_runtime_snapshot,
)
from adaos.services.operations import submit_install_operation
from adaos.services.scenario.webspace_runtime import (
    WebspaceService,
    describe_webspace_operational_state,
    describe_webspace_validation_state,
    describe_webspace_overlay_state,
    describe_webspace_projection_state,
    describe_webspace_rebuild_state,
    ensure_dev_webspace_for_scenario,
    go_home_webspace,
    reload_webspace_from_scenario,
    restore_webspace_from_snapshot,
    set_current_webspace_home,
    switch_webspace_scenario,
)
from adaos.services.skill.manager import SkillManager
from adaos.services.realtime_sidecar import (
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
)
from adaos.services.root_mcp.logs import list_local_logs, normalize_log_category
from adaos.services.ui_runtime_diagnostics import ingest_ui_runtime_diagnostics
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.system_model.service import (
    current_inventory_projection,
    current_neighborhood_projection,
    current_node_object,
    current_node_status_payload,
    current_object_inspector,
    current_object_projection,
    current_overview_projection,
    current_reliability_payload,
    current_reliability_projection,
    current_subnet_planning_context,
    current_task_packet,
    current_topology_projection,
    route_info,
)
from adaos.services.system_model.projections import compact_overview_projection_dict
from adaos.services.status.guard_cards import guard_status_cards_from_runtime
from adaos.services.projection_demand import (
    delete_client_subscription_record,
    projection_demand_snapshot,
    touch_client_subscription_record,
    write_client_subscription_record,
)
from adaos.services.projection_demand_mapper import (
    browser_surface_lifecycle_contract_snapshot,
    build_browser_projection_demand_record,
)
from adaos.services.projection_demand_restore import projection_demand_restore_contract_snapshot
from adaos.services.projection_diagnostics import projection_operator_diagnostics
from adaos.services.projection_dispatcher import (
    core_skill_refresh_contract_snapshot,
    dispatch_demanded_projection_refresh,
    projection_dispatcher_memory_contract_snapshot,
    projection_dispatcher_snapshot,
)
from adaos.services.projection_pilot_readiness import projection_pilot_readiness_contract_snapshot
from adaos.services.projection_record_yjs import (
    materialize_projection_records_to_yjs,
    normalize_projection_record_keys,
    projection_records_node_multiplicity_contract_snapshot,
    read_projection_records_yjs_cache,
)
from adaos.services.projection_records import (
    browser_projection_adapter_contract_snapshot,
    browser_projection_record_snapshot,
    get_projection_record,
    projection_record_registry_snapshot,
    write_projection_record,
)
from adaos.services.projection_runtime_ownership import projection_runtime_ownership_contract_snapshot
from adaos.services.status_projection import (
    ensure_status_card_projection_handler,
    materialize_status_card_projection_records,
    platform_emitter_contract_snapshot,
)
from adaos.services.yjs.doc import async_read_ydoc
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.webspace import coerce_webspace_id, default_webspace_id

router = APIRouter()
_log = logging.getLogger("adaos.api.node_api")

_RELIABILITY_SUMMARY_METRICS_LOCK = threading.RLock()
_RELIABILITY_SUMMARY_METRICS: dict[str, Any] = {
    "schema": "adaos.reliability_summary.metrics.v1",
    "started_at": time.time(),
    "updated_at": None,
    "total": {
        "response_total": 0,
        "not_modified_total": 0,
        "body_bytes_total": 0,
    },
    "modes": {},
}


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _coerce_node_webspace_id(value: Any = None) -> str:
    return coerce_webspace_id(value, fallback=default_webspace_id())


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _local_node_id() -> str:
    try:
        conf = load_config()
        node_id = str(getattr(conf, "node_id", "") or "").strip()
        if node_id:
            return node_id
        nested = str(getattr(getattr(conf, "node_settings", None), "id", "") or "").strip()
        if nested:
            return nested
    except Exception:
        pass
    return "hub"


def _local_node_label() -> str:
    try:
        conf = load_config()
        return str(node_display_from_config(conf).get("node_label") or "").strip() or _local_node_id()
    except Exception:
        return _local_node_id()


def _local_node_display() -> dict[str, Any]:
    try:
        return node_display_from_config(load_config())
    except Exception:
        return {
            "node_label": _local_node_label(),
            "node_compact_label": "N0",
            "node_index": 0,
            "node_color": "",
            "node_color_index": 0,
        }



def _read_node_scoped_scenario_entry(scenarios_root: Any, scenario_id: str, *, node_id: str | None = None) -> dict[str, Any]:
    root = _coerce_dict(scenarios_root or {})
    target_node_id = str(node_id or "").strip() or _local_node_id()
    local_bucket = _coerce_dict(root.get(target_node_id) or {})
    local_entry = _coerce_dict(local_bucket.get(scenario_id) or {})
    if local_entry:
        return local_entry
    for maybe_bucket in root.values():
        bucket = _coerce_dict(maybe_bucket or {})
        entry = _coerce_dict(bucket.get(scenario_id) or {})
        if entry:
            return entry
    return {}


async def _current_reliability_payload_async(*, webspace_id: str | None = None) -> dict[str, Any]:
    if webspace_id is None:
        return await anyio.to_thread.run_sync(current_reliability_payload)
    return await anyio.to_thread.run_sync(partial(current_reliability_payload, webspace_id=webspace_id))


def _current_status_registry_snapshot(
    *,
    webspace_id: str | None = None,
    owner: str | None = None,
    scope: str | None = None,
    include_stale: bool = True,
) -> dict[str, Any]:
    now = time.time()
    try:
        registry = get_ctx().status_registry
        snapshot = registry.snapshot(
            webspace_id=webspace_id,
            owner=owner,
            scope=scope,
            include_stale=include_stale,
            now_ts=now,
        )
        snapshot["available"] = True
        return snapshot
    except Exception as exc:
        return {
            "schema": "adaos.status_registry.v1",
            "available": False,
            "updated_at": now,
            "cards": [],
            "total": 0,
            "diagnostics": {
                "schema": "adaos.status_registry.diagnostics.v1",
                "card_count": 0,
                "publish_total": 0,
                "changed_total": 0,
                "unchanged_total": 0,
                "stale_count": 0,
                "last_publish_latency_ms": 0.0,
                "last_changed_at": None,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }


def _compact_status_card(value: Any) -> dict[str, Any]:
    card = _coerce_dict(value)
    return {
        "id": str(card.get("id") or "").strip() or "unknown",
        "owner": str(card.get("owner") or "").strip() or "unknown",
        "kind": str(card.get("kind") or "").strip() or "status",
        "scope": str(card.get("scope") or "").strip() or "runtime",
        "status": str(card.get("status") or "unknown").strip() or "unknown",
        "summary": str(card.get("summary") or "").strip() or None,
        "severity": str(card.get("severity") or "unknown").strip() or "unknown",
        "webspaceId": str(card.get("webspace_id") or "").strip() or None,
        "updatedAt": card.get("updated_at"),
        "ttlMs": _coerce_optional_int(card.get("ttl_ms")),
        "stale": bool(card.get("stale")),
        "version": int(card.get("version") or 1),
        "fingerprint": str(card.get("fingerprint") or "").strip() or None,
        "changedAt": card.get("changed_at"),
        "incidentId": str(card.get("incident_id") or "").strip() or None,
        "detailsRef": _coerce_dict(card.get("details_ref")),
        "route": _coerce_dict(card.get("route")),
        "guardRef": _coerce_dict(card.get("guard_ref")),
    }


def _status_card_key(card: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(card.get("scope") or "").strip(),
        str(card.get("owner") or "").strip(),
        str(card.get("webspace_id") or "").strip(),
        str(card.get("id") or "").strip(),
    )


def _with_derived_status_cards(snapshot: dict[str, Any], cards: list[Any]) -> dict[str, Any]:
    if not cards:
        return snapshot
    merged = dict(snapshot)
    rows = [dict(item) for item in _coerce_list(snapshot.get("cards")) if isinstance(item, dict)]
    seen = {_status_card_key(row) for row in rows}
    derived_rows: list[dict[str, Any]] = []
    for card in cards:
        payload = card.to_dict() if hasattr(card, "to_dict") else _coerce_dict(card)
        if not payload:
            continue
        key = _status_card_key(payload)
        if key in seen:
            continue
        seen.add(key)
        rows.append(payload)
        derived_rows.append(payload)
    diagnostics = _coerce_dict(snapshot.get("diagnostics"))
    diagnostics["derived_card_count"] = int(diagnostics.get("derived_card_count") or 0) + len(derived_rows)
    merged["cards"] = rows
    merged["total"] = len(rows)
    merged["diagnostics"] = diagnostics
    return merged


def _compact_status_registry_payload(
    snapshot: dict[str, Any],
    *,
    webspace_id: str | None = None,
    limit: int | None = 50,
    source: str = "api.node.status.cards",
) -> dict[str, Any]:
    diagnostics = _coerce_dict(snapshot.get("diagnostics"))
    cards = [_compact_status_card(card) for card in _coerce_list(snapshot.get("cards")) if isinstance(card, dict)]
    limit_value = max(0, min(int(limit if limit is not None else 50), 500))
    cards = cards[:limit_value]
    return {
        "ok": True,
        "available": bool(snapshot.get("available", True)),
        "schema": str(snapshot.get("schema") or "adaos.status_registry.v1"),
        "source": source,
        "webspaceId": str(webspace_id or "").strip() or None,
        "updatedAt": int(float(snapshot.get("updated_at") or time.time()) * 1000),
        "total": int(snapshot.get("total") or len(cards)),
        "returned": len(cards),
        "diagnostics": {
            "cardCount": int(diagnostics.get("card_count") or 0),
            "publishTotal": int(diagnostics.get("publish_total") or 0),
            "changedTotal": int(diagnostics.get("changed_total") or 0),
            "unchangedTotal": int(diagnostics.get("unchanged_total") or 0),
            "maxCardBytes": _coerce_optional_int(diagnostics.get("max_card_bytes")),
            "maxCardBytesObserved": int(diagnostics.get("max_card_bytes_observed") or 0),
            "oversizedCardTotal": int(diagnostics.get("oversized_card_total") or 0),
            "lastOversizedCard": _coerce_dict(diagnostics.get("last_oversized_card")),
            "staleCount": int(diagnostics.get("stale_count") or 0),
            "derivedCardCount": int(diagnostics.get("derived_card_count") or 0),
            "lastPublishLatencyMs": float(diagnostics.get("last_publish_latency_ms") or 0.0),
            "lastChangedAt": diagnostics.get("last_changed_at"),
        },
        "cards": cards,
        "error": str(snapshot.get("error") or "").strip() or None,
    }


def _strip_summary_etag_volatiles(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_summary_etag_volatiles(item)
            for key, item in value.items()
            if str(key)
            not in {
                "age_s",
                "expires_at",
                "updated_at",
                "updatedAt",
                "changedAt",
                "lastChangedAt",
                "lastPublishLatencyMs",
                "maxCardBytesObserved",
            }
        }
    if isinstance(value, list):
        return [_strip_summary_etag_volatiles(item) for item in value]
    return value


def _summary_etag(payload: Mapping[str, Any]) -> str:
    stable = _strip_summary_etag_volatiles(payload)
    raw = json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f'W/"{hashlib.sha1(raw.encode("utf-8")).hexdigest()}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    tokens = [item.strip() for item in str(header or "").split(",") if item.strip()]
    return "*" in tokens or etag in tokens


def _summary_body_size(payload: Mapping[str, Any]) -> int:
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        return len(raw.encode("utf-8"))
    except Exception:
        return 0


def _record_reliability_summary_metric(
    *,
    mode: str,
    status_code: int,
    body_bytes: int,
    cache_hit: bool,
    etag: str,
) -> None:
    now = time.time()
    mode_id = str(mode or "unknown").strip() or "unknown"
    with _RELIABILITY_SUMMARY_METRICS_LOCK:
        total = _coerce_dict(_RELIABILITY_SUMMARY_METRICS.get("total"))
        total["response_total"] = int(total.get("response_total") or 0) + 1
        total["not_modified_total"] = int(total.get("not_modified_total") or 0) + (1 if status_code == 304 else 0)
        total["body_bytes_total"] = int(total.get("body_bytes_total") or 0) + max(0, int(body_bytes or 0))
        modes = _coerce_dict(_RELIABILITY_SUMMARY_METRICS.get("modes"))
        row = _coerce_dict(modes.get(mode_id))
        row["response_total"] = int(row.get("response_total") or 0) + 1
        row["not_modified_total"] = int(row.get("not_modified_total") or 0) + (1 if status_code == 304 else 0)
        row["body_bytes_total"] = int(row.get("body_bytes_total") or 0) + max(0, int(body_bytes or 0))
        row["last_status_code"] = int(status_code)
        row["last_body_bytes"] = max(0, int(body_bytes or 0))
        row["last_cache_hit"] = bool(cache_hit)
        row["last_etag"] = str(etag or "").strip() or None
        row["last_at"] = now
        modes[mode_id] = row
        _RELIABILITY_SUMMARY_METRICS["total"] = total
        _RELIABILITY_SUMMARY_METRICS["modes"] = modes
        _RELIABILITY_SUMMARY_METRICS["updated_at"] = now


def _compact_status_registry_metrics(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> dict[str, Any]:
    diagnostics = _coerce_dict(snapshot.get("diagnostics"))
    return {
        "schema": "adaos.status_registry.acceptance_metrics.v1",
        "available": bool(snapshot.get("available", True)),
        "webspace_id": str(webspace_id or "").strip() or None,
        "total": int(snapshot.get("total") or 0),
        "diagnostics": {
            "card_count": int(diagnostics.get("card_count") or 0),
            "publish_total": int(diagnostics.get("publish_total") or 0),
            "changed_total": int(diagnostics.get("changed_total") or 0),
            "unchanged_total": int(diagnostics.get("unchanged_total") or 0),
            "stale_count": int(diagnostics.get("stale_count") or 0),
            "max_card_bytes": _coerce_optional_int(diagnostics.get("max_card_bytes")),
            "max_card_bytes_observed": int(diagnostics.get("max_card_bytes_observed") or 0),
            "oversized_card_total": int(diagnostics.get("oversized_card_total") or 0),
            "last_oversized_card": _coerce_dict(diagnostics.get("last_oversized_card")),
            "last_publish_latency_ms": float(diagnostics.get("last_publish_latency_ms") or 0.0),
            "last_changed_at": diagnostics.get("last_changed_at"),
        },
        "error": str(snapshot.get("error") or "").strip() or None,
    }


def _current_webio_stream_guard_metrics(
    *,
    webspace_id: str | None = None,
    receiver: str | None = None,
    owner: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        from adaos.services.router.service import webio_stream_guard_snapshot

        payload = webio_stream_guard_snapshot(
            webspace_id=webspace_id,
            receiver=receiver,
            owner=owner,
            limit=limit,
        )
        result = dict(payload) if isinstance(payload, dict) else {}
        result["available"] = True
        result.setdefault("items", [])
        result.setdefault("totals", {})
        return result
    except Exception as exc:
        return {
            "schema": "adaos.webio_stream_guard.v1",
            "available": False,
            "webspace_id": str(webspace_id or "").strip() or None,
            "receiver": str(receiver or "").strip() or None,
            "owner": str(owner or "").strip() or None,
            "items": [],
            "total": 0,
            "totals": {
                "attempted": 0,
                "published": 0,
                "suppressed": 0,
                "throttled": 0,
                "published_fanout": 0,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }


def _compact_webio_stream_guard_metrics(payload: dict[str, Any], *, limit: int = 20) -> dict[str, Any]:
    totals = _coerce_dict(payload.get("totals"))
    rows = [
        {
            "webspace_id": str(row.get("webspace_id") or "").strip() or None,
            "receiver": str(row.get("receiver") or "").strip() or None,
            "owner": str(row.get("owner") or "").strip() or None,
            "surface": str(row.get("surface") or "").strip() or None,
            "attempted": int(row.get("attempted_total") or 0),
            "published": int(row.get("published_total") or 0),
            "suppressed": int(row.get("suppressed_total") or 0),
            "throttled": int(row.get("throttled_total") or 0),
            "published_fanout": int(row.get("published_fanout_total") or 0),
            "last_fanout": int(row.get("last_fanout_total") or 0),
            "last_payload_bytes": int(row.get("last_payload_bytes") or 0),
            "last_effective_bytes": int(row.get("last_effective_bytes") or 0),
            "declared_max_payload_bytes": _coerce_optional_int(row.get("declared_max_payload_bytes")),
            "last_policy_state": str(row.get("last_policy_state") or "").strip() or None,
            "last_reason": str(row.get("last_reason") or "").strip() or None,
            "last_at": row.get("last_at"),
        }
        for row in _coerce_list(payload.get("items"))[: max(0, min(int(limit or 20), 100))]
        if isinstance(row, dict)
    ]
    return {
        "schema": "adaos.webio_stream_guard.acceptance_metrics.v1",
        "available": bool(payload.get("available", True)),
        "webspace_id": str(payload.get("webspace_id") or "").strip() or None,
        "receiver": str(payload.get("receiver") or "").strip() or None,
        "owner": str(payload.get("owner") or "").strip() or None,
        "total": int(payload.get("total") or len(rows)),
        "totals": {
            "attempted": int(totals.get("attempted") or 0),
            "published": int(totals.get("published") or 0),
            "suppressed": int(totals.get("suppressed") or 0),
            "throttled": int(totals.get("throttled") or 0),
            "published_fanout": int(totals.get("published_fanout") or 0),
        },
        "items": rows,
        "error": str(payload.get("error") or "").strip() or None,
    }


def _current_yjs_owner_guard_metrics(
    *,
    webspace_id: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    try:
        from adaos.services.yjs.governance import primary_doc_governance_snapshot

        payload = primary_doc_governance_snapshot(webspace_id=webspace_id, owner=owner)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _compact_yjs_owner_guard_metrics(
    payload: dict[str, Any],
    *,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    owner_guard = _coerce_dict(data.get("owner_guard"))
    active_quarantines = _coerce_list(owner_guard.get("active_quarantines"))
    remaining = data.get("quarantine_remaining_s")
    return {
        "schema": "adaos.yjs_owner_guard.acceptance_metrics.v1",
        "available": bool(data.get("available", bool(data))) and bool(data.get("enabled", True)),
        "webspace_id": str(data.get("webspace_id") or webspace_id or "").strip() or None,
        "owner": str(data.get("owner") or "").strip() or None,
        "attempted": int(data.get("attempted_total") or 0),
        "allowed": int(data.get("allowed_total") or 0),
        "blocked": int(data.get("blocked_total") or 0),
        "throttled": int(data.get("throttled_total") or 0),
        "quarantined": bool(data.get("quarantined")),
        "quarantine_enabled": bool(data.get("quarantine_enabled")),
        "quarantine_total": int(data.get("quarantine_total") or 0),
        "quarantine_denied_total": int(data.get("quarantine_denied_total") or 0),
        "active_quarantine_total": len(active_quarantines),
        "quarantine_remaining_s": round(float(remaining or 0.0), 3) if remaining is not None else None,
        "quarantine_reason": str(data.get("quarantine_reason") or "").strip() or None,
        "quarantine_trigger": str(data.get("quarantine_trigger") or "").strip() or None,
        "quarantine_path": str(data.get("quarantine_path") or "").strip() or None,
        "quarantine_tool": str(data.get("quarantine_tool") or "").strip() or None,
        "last_decision": str(data.get("last_decision") or "").strip() or None,
        "last_policy_state": str(data.get("last_policy_state") or "").strip() or None,
        "last_reason": str(data.get("last_reason") or "").strip() or None,
        "last_path": str(data.get("last_path") or "").strip() or None,
        "last_source": str(data.get("last_source") or "").strip() or None,
        "last_channel": str(data.get("last_channel") or "").strip() or None,
        "last_update_bytes": int(data.get("last_update_bytes") or 0),
        "error": str(data.get("error") or "").strip() or None,
    }


def _current_eventbus_backlog_metrics() -> dict[str, Any]:
    try:
        bus = getattr(get_ctx(), "bus", None)
        snapshot_fn = getattr(bus, "backlog_snapshot", None)
        if callable(snapshot_fn):
            payload = snapshot_fn()
            result = dict(payload) if isinstance(payload, dict) else {}
            result["available"] = True
            result.setdefault("top_webio_stream_controls", [])
            return result
    except Exception as exc:
        return {
            "available": False,
            "top_webio_stream_controls": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "available": False,
        "top_webio_stream_controls": [],
    }


def _compact_webio_stream_control_metrics(
    backlog: dict[str, Any],
    *,
    webspace_id: str | None = None,
    receiver: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    token_ws = str(webspace_id or "").strip()
    token_receiver = str(receiver or "").strip()
    rows: list[dict[str, Any]] = []
    for raw in _coerce_list(backlog.get("top_webio_stream_controls")):
        if not isinstance(raw, dict):
            continue
        row_ws = str(raw.get("webspace_id") or "").strip()
        row_receiver = str(raw.get("receiver") or "").strip()
        if token_ws and row_ws != token_ws:
            continue
        if token_receiver and row_receiver != token_receiver:
            continue
        event_type = str(raw.get("event_type") or "").strip()
        incoming = int(raw.get("incoming_total") or 0)
        superseded = int(raw.get("superseded_total") or 0)
        rows.append(
            {
                "event_type": event_type or None,
                "webspace_id": row_ws or None,
                "target_node_id": str(raw.get("target_node_id") or "").strip() or None,
                "receiver": row_receiver or None,
                "source": str(raw.get("source") or "").strip() or None,
                "incoming": incoming,
                "snapshot_requested": incoming if event_type == "webio.stream.snapshot.requested" else 0,
                "queued": int(raw.get("queued_total") or 0),
                "coalesced": superseded,
                "superseded": superseded,
                "dropped": int(raw.get("dropped_total") or 0),
                "last_action": str(raw.get("last_action") or "").strip() or None,
                "last_handler": str(raw.get("last_handler") or "").strip() or None,
                "last_at": raw.get("last_at"),
            }
        )
    rows.sort(
        key=lambda item: (
            -int(item.get("coalesced") or 0),
            -int(item.get("dropped") or 0),
            -int(item.get("snapshot_requested") or 0),
            str(item.get("receiver") or ""),
        )
    )
    max_items = max(0, min(int(limit or 20), 100))
    limited = rows[:max_items]
    return {
        "schema": "adaos.webio_stream_control.acceptance_metrics.v1",
        "available": bool(backlog.get("available")),
        "webspace_id": token_ws or None,
        "receiver": token_receiver or None,
        "pending_tasks": int(backlog.get("pending_tasks") or 0),
        "pending_peak": int(backlog.get("pending_peak") or 0),
        "bounded_queue_total": int(backlog.get("bounded_queue_total") or 0),
        "bounded_queue_peak": int(backlog.get("bounded_queue_peak") or 0),
        "bounded_active_workers": int(backlog.get("bounded_active_workers") or 0),
        "totals": {
            "incoming": sum(int(item.get("incoming") or 0) for item in rows),
            "snapshot_requested": sum(int(item.get("snapshot_requested") or 0) for item in rows),
            "queued": sum(int(item.get("queued") or 0) for item in rows),
            "coalesced": sum(int(item.get("coalesced") or 0) for item in rows),
            "superseded": sum(int(item.get("superseded") or 0) for item in rows),
            "dropped": sum(int(item.get("dropped") or 0) for item in rows),
        },
        "items": limited,
        "error": str(backlog.get("error") or "").strip() or None,
    }


def _stream_receiver_acceptance_metrics(
    *,
    stream_guard: dict[str, Any],
    stream_controls: dict[str, Any],
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def _receiver_row(webspace_id: Any, receiver: Any) -> dict[str, Any]:
        key = (
            str(webspace_id or "").strip(),
            str(receiver or "").strip(),
        )
        if key not in rows:
            rows[key] = {
                "webspace_id": key[0] or None,
                "receiver": key[1] or None,
                "owner": None,
                "surface": None,
                "attempted": 0,
                "published": 0,
                "suppressed": 0,
                "throttled": 0,
                "published_fanout": 0,
                "snapshot_requested": 0,
                "queued": 0,
                "coalesced": 0,
                "superseded": 0,
                "dropped": 0,
            }
        return rows[key]

    for item in _coerce_list(stream_guard.get("items")):
        if not isinstance(item, dict):
            continue
        row = _receiver_row(item.get("webspace_id"), item.get("receiver"))
        row["owner"] = row.get("owner") or item.get("owner")
        row["surface"] = row.get("surface") or item.get("surface")
        for field in ("attempted", "published", "suppressed", "throttled", "published_fanout"):
            row[field] = int(row.get(field) or 0) + int(item.get(field) or 0)

    for item in _coerce_list(stream_controls.get("items")):
        if not isinstance(item, dict):
            continue
        row = _receiver_row(item.get("webspace_id"), item.get("receiver"))
        for field in ("snapshot_requested", "queued", "coalesced", "superseded", "dropped"):
            row[field] = int(row.get(field) or 0) + int(item.get(field) or 0)

    result = list(rows.values())
    result.sort(
        key=lambda item: (
            -int(item.get("suppressed") or 0),
            -int(item.get("coalesced") or 0),
            -int(item.get("snapshot_requested") or 0),
            -int(item.get("published_fanout") or 0),
            str(item.get("receiver") or ""),
        )
    )
    return result[: max(0, min(int(limit or 20), 100))]


def _acceptance_observability_metrics(
    *,
    webspace_id: str | None = None,
    receiver: str | None = None,
    owner: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    resolved_webspace_id = _coerce_node_webspace_id(webspace_id) if webspace_id is not None else None
    max_items = max(1, min(int(limit or 20), 100))
    status_registry = _compact_status_registry_metrics(
        _current_status_registry_snapshot(webspace_id=resolved_webspace_id),
        webspace_id=resolved_webspace_id,
    )
    stream_guard = _compact_webio_stream_guard_metrics(
        _current_webio_stream_guard_metrics(
            webspace_id=resolved_webspace_id,
            receiver=receiver,
            owner=owner,
            limit=max_items,
        ),
        limit=max_items,
    )
    yjs_guard = _compact_yjs_owner_guard_metrics(
        _current_yjs_owner_guard_metrics(
            webspace_id=resolved_webspace_id,
            owner=owner,
        ),
        webspace_id=resolved_webspace_id,
    )
    stream_controls = _compact_webio_stream_control_metrics(
        _current_eventbus_backlog_metrics(),
        webspace_id=resolved_webspace_id,
        receiver=receiver,
        limit=max_items,
    )
    return {
        "schema": "adaos.reliability_summary.acceptance_metrics.v1",
        "webspace_id": resolved_webspace_id,
        "receiver": str(receiver or "").strip() or None,
        "owner": str(owner or "").strip() or None,
        "status_registry": status_registry,
        "yjs_guard": yjs_guard,
        "stream_guard": stream_guard,
        "stream_controls": stream_controls,
        "stream_receivers": _stream_receiver_acceptance_metrics(
            stream_guard=stream_guard,
            stream_controls=stream_controls,
            limit=max_items,
        ),
        "notes": {
            "unchanged_source": "status_registry.unchanged_total and summary not_modified_total; router cannot observe skill-side unchanged stream dedupe unless the skill publishes that diagnostic",
            "coalesced_source": "eventbus bounded superseded controls are reported as coalesced for soak readability",
            "yjs_guard_source": "primary-doc governance and owner quarantine counters; observability only, not a data-route replacement",
        },
    }


def _reliability_summary_metrics_snapshot(
    *,
    webspace_id: str | None = None,
    receiver: str | None = None,
    owner: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    with _RELIABILITY_SUMMARY_METRICS_LOCK:
        payload = json.loads(json.dumps(_RELIABILITY_SUMMARY_METRICS, ensure_ascii=True, default=str))
    payload["acceptance"] = _acceptance_observability_metrics(
        webspace_id=webspace_id,
        receiver=receiver,
        owner=owner,
        limit=limit,
    )
    return payload


def _json_response_with_etag(
    payload: dict[str, Any],
    *,
    if_none_match: str | None = None,
    mode: str,
) -> Response:
    etag = _summary_etag(payload)
    cache_hit = _etag_matches(if_none_match, etag)
    body_bytes = 0 if cache_hit else _summary_body_size(payload)
    headers = {
        "Cache-Control": "no-cache",
        "ETag": etag,
        "X-AdaOS-Summary-Mode": mode,
        "X-AdaOS-Summary-Cache": "hit" if cache_hit else "miss",
        "X-AdaOS-Summary-Body-Bytes": str(body_bytes),
    }
    _record_reliability_summary_metric(
        mode=mode,
        status_code=304 if cache_hit else 200,
        body_bytes=body_bytes,
        cache_hit=cache_hit,
        etag=etag,
    )
    _log.debug(
        "reliability summary response mode=%s status=%s bytes=%s cache=%s",
        mode,
        304 if cache_hit else 200,
        body_bytes,
        "hit" if cache_hit else "miss",
    )
    if cache_hit:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


def _thin_runtime_reliability_payload(
    status_registry: dict[str, Any],
    *,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved_webspace_id = _coerce_node_webspace_id(webspace_id)
    status_plane = _compact_status_registry_payload(
        status_registry,
        webspace_id=resolved_webspace_id,
        limit=50,
        source="api.node.reliability.summary.status_plane",
    )
    diagnostics = _coerce_dict(status_plane.get("diagnostics"))
    status_plane["diagnostics"] = {
        "cardCount": int(diagnostics.get("cardCount") or 0),
        "staleCount": int(diagnostics.get("staleCount") or 0),
        "derivedCardCount": int(diagnostics.get("derivedCardCount") or 0),
        "maxCardBytes": _coerce_optional_int(diagnostics.get("maxCardBytes")),
        "maxCardBytesObserved": int(diagnostics.get("maxCardBytesObserved") or 0),
        "oversizedCardTotal": int(diagnostics.get("oversizedCardTotal") or 0),
        "lastOversizedCard": _coerce_dict(diagnostics.get("lastOversizedCard")),
        "lastChangedAt": diagnostics.get("lastChangedAt"),
    }
    return {
        "ok": True,
        "available": bool(status_plane.get("available", True)),
        "schema": "adaos.reliability_summary.thin.v1",
        "source": "api.node.reliability.summary",
        "mode": "thin",
        "webspaceId": resolved_webspace_id,
        "updatedAt": status_plane.get("updatedAt"),
        "statusPlane": status_plane,
        "detailsRef": {
            "summaryFull": "/api/node/reliability/summary?mode=full",
            "runtime": "/api/node/reliability",
        },
        "cache": {
            "etag": True,
            "ifNoneMatch": True,
        },
    }


def _compact_phase0_task(value: Any) -> dict[str, Any] | None:
    payload = _coerce_dict(value)
    if not payload:
        return None
    return {
        "id": str(payload.get("id") or "").strip(),
        "status": str(payload.get("status") or "unknown").strip() or "unknown",
        "summary": str(payload.get("summary") or "").strip(),
        "completedCriteria": _coerce_list(payload.get("completed_criteria")),
        "pendingCriteria": _coerce_list(payload.get("pending_criteria")),
        "pendingReasons": _coerce_list(payload.get("pending_reasons")),
        "evidence": _coerce_dict(payload.get("evidence")),
    }


def _compact_phase0_checkpoint(value: Any) -> dict[str, Any] | None:
    payload = _coerce_dict(value)
    if not payload:
        return None
    tasks = _coerce_dict(payload.get("tasks"))
    return {
        "state": str(payload.get("state") or "unknown").strip() or "unknown",
        "ready": bool(payload.get("ready")),
        "trackedTasks": _coerce_list(payload.get("tracked_tasks")),
        "completedTaskTotal": int(payload.get("completed_task_total") or 0),
        "taskTotal": int(payload.get("task_total") or 0),
        "remainingTasks": _coerce_list(payload.get("remaining_tasks")),
        "tasks": {
            "nodeBrowserReady": _compact_phase0_task(tasks.get("phase0.node_browser_ready")),
            "runtimeCommReady": _compact_phase0_task(tasks.get("phase0.runtime_comm_ready")),
        },
    }


def _compact_route_tunnel_state(value: Any) -> str:
    payload = _coerce_dict(value)
    current_owner = str(payload.get("current_owner") or "").strip().lower()
    planned_owner = str(payload.get("planned_owner") or "").strip().lower()
    current_support = str(payload.get("current_support") or "").strip().lower()
    delegation_mode = str(payload.get("delegation_mode") or "").strip().lower()
    listener_ready = bool(payload.get("listener_ready"))
    handoff_ready = bool(payload.get("handoff_ready"))
    if current_owner == "sidecar":
        if handoff_ready:
            return "ready"
        if listener_ready:
            return "starting"
        return "degraded"
    if planned_owner == "sidecar":
        if listener_ready or current_support == "proxy_ready" or delegation_mode in {"local_tcp_proxy", "local_ws_proxy"}:
            return "proxy_ready" if listener_ready or current_support == "proxy_ready" else "planned"
        return "disabled" if current_support == "disabled" else "planned"
    if current_owner == "runtime":
        if listener_ready or current_support == "proxy_ready" or delegation_mode in {"local_tcp_proxy", "local_ws_proxy"}:
            return "proxy_ready" if listener_ready or current_support == "proxy_ready" else "not_owned"
        return "not_owned"
    return "unknown"


def _compact_runtime_reliability_payload(
    payload: dict[str, Any],
    *,
    webspace_id: str | None = None,
    status_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = _coerce_dict(payload.get("runtime"))
    hub_root_protocol = _coerce_dict(runtime.get("hub_root_protocol"))
    sidecar_runtime = _coerce_dict(runtime.get("sidecar_runtime"))
    sidecar_enablement = _coerce_dict(sidecar_runtime.get("enablement"))
    hardening = _coerce_dict(hub_root_protocol.get("hardening_coverage"))
    continuity = _coerce_dict(sidecar_runtime.get("continuity_contract"))
    progress = _coerce_dict(sidecar_runtime.get("progress"))
    route_tunnel = _coerce_dict(sidecar_runtime.get("route_tunnel_contract"))
    ws = _coerce_dict(route_tunnel.get("ws"))
    yws = _coerce_dict(route_tunnel.get("yws"))
    supervisor_runtime = _coerce_dict(runtime.get("supervisor_runtime"))
    connectivity = _coerce_dict(runtime.get("connectivity"))
    required_upstream_link = _coerce_dict(connectivity.get("required_upstream_link"))
    browser_control_route = _coerce_dict(connectivity.get("browser_control_route"))
    state_sync = _coerce_dict(runtime.get("state_sync"))
    replay = _coerce_dict(state_sync.get("replay"))
    yjs_pressure = _coerce_dict(runtime.get("yjs_pressure"))
    webio_stream_guard = _coerce_dict(runtime.get("webio_stream_guard"))
    webio_stream_guard_totals = _coerce_dict(webio_stream_guard.get("totals"))
    webio_stream_guard_items = _coerce_list(webio_stream_guard.get("items"))
    webio_stream_guard_top = _coerce_dict(
        webio_stream_guard_items[0] if webio_stream_guard_items else {}
    )
    eventbus_backlog = _coerce_dict(runtime.get("eventbus_backlog"))
    webio_control_items = _coerce_list(eventbus_backlog.get("top_webio_stream_controls"))
    resolved_webspace_id = _coerce_node_webspace_id(
        webspace_id
        or runtime.get("webspace_id")
        or payload.get("webspace_id")
    )
    status_snapshot = _with_derived_status_cards(
        status_registry or _current_status_registry_snapshot(webspace_id=resolved_webspace_id),
        guard_status_cards_from_runtime(runtime, webspace_id=resolved_webspace_id),
    )
    return {
        "ok": True,
        "updatedAt": int(time.time() * 1000),
        "available": True,
        "source": "api.node.reliability.summary",
        "webspaceId": resolved_webspace_id,
        "hubRootHardening": {
            "state": str(hardening.get("state") or "unknown").strip() or "unknown",
            "coveredFlows": int(hardening.get("covered_flows") or 0),
            "totalFlows": int(hardening.get("total_flows") or 0),
            "flows": _coerce_list(hardening.get("flows")),
        },
        "sidecarContinuity": {
            "currentSupport": str(continuity.get("current_support") or "unknown").strip() or "unknown",
            "hubRuntimeUpdate": str(continuity.get("hub_runtime_update") or "unknown").strip() or "unknown",
            "required": bool(continuity.get("required")),
            "pendingBoundaries": _coerce_list(continuity.get("pending_boundaries")),
            "readyBoundaries": _coerce_list(continuity.get("ready_boundaries")),
            "blockers": _coerce_list(continuity.get("blockers")),
        },
        "sidecarEnablement": {
            "enabled": bool(sidecar_enablement.get("enabled")),
            "defaultEnabled": bool(sidecar_enablement.get("default_enabled")),
            "explicit": bool(sidecar_enablement.get("explicit")),
            "source": str(sidecar_enablement.get("source") or "unknown").strip() or "unknown",
            "role": str(sidecar_enablement.get("role") or "").strip() or None,
            "envVar": str(sidecar_enablement.get("env_var") or "").strip() or None,
            "envValue": str(sidecar_enablement.get("env_value") or "").strip() or None,
            "reason": str(sidecar_enablement.get("reason") or "").strip() or None,
        },
        "sidecarProgress": {
            "state": str(progress.get("state") or "unknown").strip() or "unknown",
            "percent": float(progress.get("percent") or 0),
            "completedMilestones": int(progress.get("completed_milestones") or 0),
            "milestoneTotal": int(progress.get("milestone_total") or 0),
            "currentMilestone": str(progress.get("current_milestone") or "").strip() or None,
            "nextBlocker": str(progress.get("next_blocker") or "").strip() or None,
        },
        "routeTunnel": {
            "currentSupport": str(route_tunnel.get("current_support") or "unknown").strip() or "unknown",
            "ownershipBoundary": str(route_tunnel.get("ownership_boundary") or "unknown").strip() or "unknown",
            "ws": ws,
            "yws": yws,
        },
        "browserWsHandoffReady": str(ws.get("current_owner") or "").strip().lower() == "sidecar" and bool(ws.get("handoff_ready")),
        "browserYwsHandoffReady": str(yws.get("current_owner") or "").strip().lower() == "sidecar" and bool(yws.get("handoff_ready")),
        "browserWsHandoffState": _compact_route_tunnel_state(ws),
        "browserYwsHandoffState": _compact_route_tunnel_state(yws),
        "browserWsHandoffBlocker": (str((_coerce_list(ws.get("blockers"))[:1] or [""])[0]).strip() or None),
        "browserYwsHandoffBlocker": (str((_coerce_list(yws.get("blockers"))[:1] or [""])[0]).strip() or None),
        "connectivity": {
            "requiredUpstreamLink": {
                "kind": str(required_upstream_link.get("kind") or "").strip() or None,
                "scopeId": str(required_upstream_link.get("scope_id") or "").strip() or None,
                "transportState": str(required_upstream_link.get("transport_state") or "unknown").strip() or "unknown",
                "transitionState": str(required_upstream_link.get("transition_state") or "unknown").strip() or "unknown",
                "plannedTransition": _coerce_dict(required_upstream_link.get("planned_transition")),
                "reason": str(required_upstream_link.get("reason") or "").strip() or None,
                "blockers": _coerce_list(required_upstream_link.get("blockers")),
                "servedBy": str(required_upstream_link.get("served_by") or "").strip() or None,
            },
            "browserControlRoute": {
                "kind": str(browser_control_route.get("kind") or "").strip() or "browser_control_route",
                "scopeId": str(browser_control_route.get("scope_id") or "").strip() or None,
                "transportState": str(browser_control_route.get("transport_state") or "unknown").strip() or "unknown",
                "transitionState": str(browser_control_route.get("transition_state") or "unknown").strip() or "unknown",
                "plannedTransition": _coerce_dict(browser_control_route.get("planned_transition")),
                "reason": str(browser_control_route.get("reason") or "").strip() or None,
                "blockers": _coerce_list(browser_control_route.get("blockers")),
                "servedBy": str(browser_control_route.get("served_by") or "").strip() or None,
            },
        },
        "stateSync": {
            "webspaceId": str(state_sync.get("webspace_id") or resolved_webspace_id).strip() or resolved_webspace_id,
            "transportState": str(state_sync.get("transport_state") or "unknown").strip() or "unknown",
            "firstSyncState": str(state_sync.get("first_sync_state") or "unknown").strip() or "unknown",
            "semanticState": str(state_sync.get("semantic_state") or "unknown").strip() or "unknown",
            "freshnessState": str(state_sync.get("freshness_state") or "unknown").strip() or "unknown",
            "lastGoodSyncAt": state_sync.get("last_good_sync_at"),
            "lastMaterializationAt": state_sync.get("last_materialization_at"),
            "replay": {
                "mode": str(replay.get("mode") or "snapshot_plus_diff").strip() or "snapshot_plus_diff",
                "cursor": str(replay.get("cursor") or "0/0").strip() or "0/0",
            },
            "fallbackMode": str(state_sync.get("fallback_mode") or "off").strip() or "off",
            "blockers": _coerce_list(state_sync.get("blockers")),
        },
        "yjsPressure": {
            "webspaceId": str(yjs_pressure.get("webspace_id") or resolved_webspace_id).strip() or resolved_webspace_id,
            "owner": str(yjs_pressure.get("owner") or "").strip() or None,
            "recentBytes": int(yjs_pressure.get("recent_bytes") or 0),
            "recentWrites": int(yjs_pressure.get("recent_writes") or 0),
            "peakBps": float(yjs_pressure.get("peak_bps") or 0.0),
            "peakWps": float(yjs_pressure.get("peak_wps") or 0.0),
            "policyState": str(yjs_pressure.get("policy_state") or "ok").strip() or "ok",
            "target": str(yjs_pressure.get("target") or "primary_shared_doc").strip() or "primary_shared_doc",
            "reason": str(yjs_pressure.get("reason") or "").strip() or None,
            "blockedRoots": _coerce_list(yjs_pressure.get("blocked_roots")),
            "observedState": str(yjs_pressure.get("observed_state") or "idle").strip() or "idle",
            "lastRoute": _coerce_dict(yjs_pressure.get("last_route")),
            "lastProjection": _coerce_dict(yjs_pressure.get("last_projection")),
        },
        "webioStreamGuard": {
            "available": bool(webio_stream_guard.get("available")),
            "webspaceId": str(webio_stream_guard.get("webspace_id") or resolved_webspace_id).strip() or resolved_webspace_id,
            "total": int(webio_stream_guard.get("total") or 0),
            "totals": {
                "attempted": int(webio_stream_guard_totals.get("attempted") or 0),
                "published": int(webio_stream_guard_totals.get("published") or 0),
                "suppressed": int(webio_stream_guard_totals.get("suppressed") or 0),
                "throttled": int(webio_stream_guard_totals.get("throttled") or 0),
                "publishedFanout": int(webio_stream_guard_totals.get("published_fanout") or 0),
            },
            "top": {
                "receiver": str(webio_stream_guard_top.get("receiver") or "").strip() or None,
                "owner": str(webio_stream_guard_top.get("owner") or "").strip() or None,
                "surface": str(webio_stream_guard_top.get("surface") or "").strip() or None,
                "attempted": int(webio_stream_guard_top.get("attempted_total") or 0),
                "published": int(webio_stream_guard_top.get("published_total") or 0),
                "suppressed": int(webio_stream_guard_top.get("suppressed_total") or 0),
                "throttled": int(webio_stream_guard_top.get("throttled_total") or 0),
                "declaredMaxPayloadBytes": _coerce_optional_int(
                    webio_stream_guard_top.get("declared_max_payload_bytes")
                ),
                "lastReason": str(webio_stream_guard_top.get("last_reason") or "").strip() or None,
            },
        },
        "eventbusBacklog": {
            "available": bool(eventbus_backlog.get("available")),
            "pendingTasks": int(eventbus_backlog.get("pending_tasks") or 0),
            "pendingPeak": int(eventbus_backlog.get("pending_peak") or 0),
            "boundedQueueTotal": int(eventbus_backlog.get("bounded_queue_total") or 0),
            "boundedQueuePeak": int(eventbus_backlog.get("bounded_queue_peak") or 0),
            "boundedActiveWorkers": int(eventbus_backlog.get("bounded_active_workers") or 0),
            "topWebioStreamControls": [
                {
                    "eventType": str(item.get("event_type") or "").strip() or None,
                    "webspaceId": str(item.get("webspace_id") or resolved_webspace_id).strip() or resolved_webspace_id,
                    "targetNodeId": str(item.get("target_node_id") or "").strip() or None,
                    "receiver": str(item.get("receiver") or "").strip() or None,
                    "source": str(item.get("source") or "").strip() or None,
                    "incoming": int(item.get("incoming_total") or 0),
                    "queued": int(item.get("queued_total") or 0),
                    "superseded": int(item.get("superseded_total") or 0),
                    "dropped": int(item.get("dropped_total") or 0),
                    "lastAction": str(item.get("last_action") or "").strip() or None,
                }
                for item in webio_control_items[:5]
                if isinstance(item, dict)
            ],
        },
        "supervisorRuntime": supervisor_runtime,
        "phase0Communication": _compact_phase0_checkpoint(runtime.get("event_model_phase0_communication")),
        "statusPlane": _compact_status_registry_payload(
            status_snapshot,
            webspace_id=resolved_webspace_id,
            limit=20,
            source="api.node.reliability.summary.status_plane",
        ),
    }


def _env_flag_enabled(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _supervisor_enabled() -> bool:
    raw = str(os.getenv("ADAOS_SUPERVISOR_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _supervisor_base_url() -> str | None:
    raw = str(os.getenv("ADAOS_SUPERVISOR_URL") or "").strip()
    if raw:
        return raw.rstrip("/")
    host = str(os.getenv("ADAOS_SUPERVISOR_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = str(os.getenv("ADAOS_SUPERVISOR_PORT") or "8776").strip() or "8776"
    return f"http://{host}:{port}"


async def _proxy_supervisor_json(
    *,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if not _supervisor_enabled():
        raise HTTPException(status_code=503, detail="supervisor-backed control surface is unavailable")
    base_url = _supervisor_base_url()
    if not base_url:
        raise HTTPException(status_code=503, detail="supervisor control URL is unavailable")

    headers = {"Accept": "application/json"}
    token = str(os.getenv("ADAOS_TOKEN") or "").strip()
    if token:
        headers["X-AdaOS-Token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = f"{base_url}{path}"

    def _send() -> dict[str, Any]:
        session = requests.Session()
        try:
            try:
                session.trust_env = False
            except Exception:
                pass
            response = session.request(
                str(method or "GET").upper(),
                url,
                headers=headers,
                json=payload,
                timeout=float(timeout),
            )
            if int(response.status_code or 0) >= 400:
                try:
                    detail: Any = response.json()
                except Exception:
                    detail = (response.text or f"supervisor returned HTTP {response.status_code}").strip()[:500]
                if isinstance(detail, dict) and set(detail.keys()) == {"detail"}:
                    detail = detail["detail"]
                raise HTTPException(status_code=int(response.status_code), detail=detail)
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("supervisor returned a non-object payload")
            return body
        finally:
            try:
                session.close()
            except Exception:
                pass

    try:
        return await anyio.to_thread.run_sync(_send)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"supervisor API unavailable: {type(exc).__name__}: {exc}") from exc


def _publish_yjs_control_event(
    *,
    action: str,
    webspace_id: str,
    result: dict[str, Any],
    scenario_id: str | None = None,
) -> None:
    payload = {
        "action": str(action or "").strip(),
        "webspace_id": _coerce_node_webspace_id(webspace_id),
        "scenario_id": str(scenario_id or result.get("scenario_id") or "").strip() or None,
        "ok": bool(result.get("ok")),
        "accepted": bool(result.get("accepted")),
        "source_of_truth": str(result.get("source_of_truth") or "").strip() or None,
        "home_scenario": str(result.get("home_scenario") or "").strip() or None,
        "background_rebuild": bool(result.get("background_rebuild")),
        "switch_skipped": bool(result.get("switch_skipped")),
        "skip_reason": str(result.get("skip_reason") or "").strip() or None,
        "error": str(result.get("error") or "").strip() or None,
    }
    event_type = "node.yjs.control.completed" if payload["ok"] and payload["accepted"] else "node.yjs.control.failed"
    try:
        get_ctx().bus.publish(
            Event(
                type=event_type,
                payload=payload,
                source="node.api",
                ts=time.time(),
            )
        )
    except Exception:
        _log.debug("failed to publish %s for action=%s webspace=%s", event_type, action, webspace_id, exc_info=True)


def _request_client_label(request: Request, *, endpoint: str) -> str:
    client = request.client
    host = str(getattr(client, "host", "") or "").strip() or "-"
    port = getattr(client, "port", None)
    remote = f"{host}:{port}" if port is not None else host
    return f"http:{endpoint}:{remote}"


def _trace_yjs_control_ingress(
    *,
    request: Request,
    kind: str,
    webspace_id: str,
    scenario_id: str | None = None,
    recreate_room: bool = False,
) -> dict[str, Any]:
    endpoint = str(request.url.path or "").strip() or "/api/node/yjs"
    payload: dict[str, Any] = {"webspace_id": webspace_id}
    if scenario_id:
        payload["scenario_id"] = scenario_id
    if recreate_room:
        payload["recreate_room"] = True
    meta = {
        "cmd_id": str(request.headers.get("x-request-id") or request.headers.get("x-trace-id") or "").strip() or None,
        "gateway_client": _request_client_label(request, endpoint=endpoint),
        "trace_id": str(request.headers.get("x-trace-id") or request.headers.get("x-request-id") or "").strip() or None,
        "device_id": str(request.headers.get("x-adaos-device-id") or "").strip() or None,
    }
    try:
        from adaos.services.yjs.gateway_ws import _record_command_trace

        trace = _record_command_trace(
            kind=kind,
            cmd_id=meta["cmd_id"],
            payload=payload,
            device_id=meta["device_id"],
            webspace_id=webspace_id,
            client_label=meta["gateway_client"],
        )
        meta["gateway_command_seq"] = int(trace.get("seq") or 0)
        meta["gateway_command_fingerprint"] = str(trace.get("fingerprint") or "").strip() or None
        _log.warning(
            "%s ingress via control_api cmd=%s seq=%s webspace=%s client=%s scenario=%s recreate_room=%s dup_recent=%s dup10s=%s fp=%s",
            kind,
            meta["cmd_id"] or "-",
            meta.get("gateway_command_seq") or 0,
            webspace_id,
            meta["gateway_client"] or "-",
            scenario_id or "-",
            "yes" if recreate_room else "no",
            "yes" if trace.get("duplicate_recent") else "no",
            trace.get("duplicate_count_10s") or 0,
            meta.get("gateway_command_fingerprint") or "-",
        )
    except Exception:
        _log.debug("failed to trace %s ingress for webspace=%s", kind, webspace_id, exc_info=True)
    payload["_meta"] = meta
    return payload


def _attach_runtime_and_rebuild(
    result: dict[str, Any],
    *,
    role: str,
    webspace_id: str,
    include_rebuild: bool = False,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(result.get("webspace_id") or webspace_id)
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=role,
        webspace_id=target_webspace_id,
    )
    if include_rebuild:
        result["rebuild"] = describe_webspace_rebuild_state(target_webspace_id)
    return result


def _attach_wait_for_rebuild_guard(
    result: dict[str, Any],
    *,
    requested: bool,
    effective: bool,
    reason: str,
) -> dict[str, Any]:
    if requested == effective:
        return result
    guards = result.get("guards")
    if not isinstance(guards, dict):
        guards = {}
        result["guards"] = guards
    guards["wait_for_rebuild"] = {
        "requested": requested,
        "effective": effective,
        "reason": reason,
    }
    return result


def _runtime_debug_slice(runtime: Mapping[str, Any] | None) -> dict[str, Any]:
    runtime_map = dict(runtime) if isinstance(runtime, Mapping) else {}
    transport = runtime_map.get("transport") if isinstance(runtime_map.get("transport"), Mapping) else {}
    assessment = runtime_map.get("assessment") if isinstance(runtime_map.get("assessment"), Mapping) else {}
    selected = runtime_map.get("selected_webspace") if isinstance(runtime_map.get("selected_webspace"), Mapping) else {}
    return {
        "assessment": {
            "state": str(assessment.get("state") or "").strip() or None,
            "reason": str(assessment.get("reason") or "").strip() or None,
        },
        "transport": {
            "active_yws_connections": int(transport.get("active_yws_connections") or 0),
            "active_clients": list(transport.get("active_clients") or []),
            "recent_open_10s": int(transport.get("recent_open_10s") or 0),
            "recent_open_60s": int(transport.get("recent_open_60s") or 0),
            "storm_detected": bool(transport.get("storm_detected")),
            "guard": dict(transport.get("guard") or {}) if isinstance(transport.get("guard"), Mapping) else {},
            "room_total": int(transport.get("room_total") or 0),
            "active_room_total": int(transport.get("active_room_total") or 0),
            "room_reset_total": int(transport.get("room_reset_total") or 0),
            "room_drop_total": int(transport.get("room_drop_total") or 0),
            "room_generation_max": int(transport.get("room_generation_max") or 0),
            "update_stream_buffer_used_total": int(transport.get("update_stream_buffer_used_total") or 0),
            "update_stream_waiting_send_total": int(transport.get("update_stream_waiting_send_total") or 0),
            "update_stream_waiting_receive_total": int(transport.get("update_stream_waiting_receive_total") or 0),
            "server_ready": bool(transport.get("server_ready")),
            "server_error": str(transport.get("server_error") or "").strip() or None,
        },
        "selected_webspace": {
            "id": str(runtime_map.get("selected_webspace_id") or "").strip() or None,
            "runtime_compaction_eligible": bool(selected.get("runtime_compaction_eligible")),
            "update_log_entries": int(selected.get("update_log_entries") or 0),
            "replay_window_entries": int(selected.get("replay_window_entries") or 0),
            "replay_window_bytes": int(selected.get("replay_window_bytes") or 0),
            "gateway_room": dict(selected.get("gateway_room") or {})
            if isinstance(selected.get("gateway_room"), Mapping)
            else {},
            "weather_observer": dict(selected.get("weather_observer") or {})
            if isinstance(selected.get("weather_observer"), Mapping)
            else {},
        },
    }


def _attach_yjs_action_debug(
    result: dict[str, Any],
    *,
    requested_endpoint: str,
    recreate_room_requested: bool,
    runtime_before: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reset_room = result.get("reset_room") if isinstance(result.get("reset_room"), Mapping) else {}
    result["action_debug"] = {
        "requested_endpoint": str(requested_endpoint or "").strip() or None,
        "requested_action": str(result.get("action") or requested_endpoint or "").strip() or None,
        "recreate_room_requested": bool(recreate_room_requested),
        "room_recreated": bool(reset_room.get("room_dropped")),
        "reset_room": dict(reset_room) if reset_room else None,
        "runtime_before": _runtime_debug_slice(runtime_before),
        "runtime_after": _runtime_debug_slice(result.get("runtime")),
    }
    return result


def _collect_materialization_missing_branches(
    *,
    has_ui_application: bool,
    has_desktop_config: bool,
    has_desktop_page_schema: bool,
    has_apps_catalog_modal: bool,
    has_widgets_catalog_modal: bool,
    has_catalog_apps: bool,
    has_catalog_widgets: bool,
) -> list[str]:
    missing: list[str] = []
    if not has_ui_application:
        missing.append("ui.application")
    if not has_desktop_config:
        missing.append("ui.application.desktop")
    if not has_desktop_page_schema:
        missing.append("ui.application.desktop.pageSchema")
    if not has_apps_catalog_modal:
        missing.append("ui.application.modals.apps_catalog")
    if not has_widgets_catalog_modal:
        missing.append("ui.application.modals.widgets_catalog")
    if not has_catalog_apps:
        missing.append("data.catalog.apps")
    if not has_catalog_widgets:
        missing.append("data.catalog.widgets")
    return missing


def _derive_materialization_readiness_state(
    *,
    ready: bool,
    current_scenario: str | None,
    has_ui_application: bool,
    has_desktop_config: bool,
    has_desktop_page_schema: bool,
    has_apps_catalog_modal: bool,
    has_widgets_catalog_modal: bool,
    has_catalog_apps: bool,
    has_catalog_widgets: bool,
) -> str:
    if ready:
        return "ready"
    if has_desktop_page_schema and has_catalog_apps and has_catalog_widgets:
        return "interactive"
    if has_desktop_page_schema and (
        has_catalog_apps or has_catalog_widgets or has_apps_catalog_modal or has_widgets_catalog_modal
    ):
        return "hydrating"
    if has_desktop_page_schema:
        return "first_paint"
    if current_scenario or has_ui_application or has_desktop_config:
        return "pending_structure"
    return "degraded"


def _collect_compatibility_cache_required_branches(current_scenario: str | None) -> list[str]:
    scenario_id = str(current_scenario or "").strip()
    if not scenario_id:
        return []
    node_id = _local_node_id()
    return [
        f"ui.scenarios.{node_id}.{scenario_id}.application",
        f"registry.scenarios.{node_id}.{scenario_id}",
        f"data.scenarios.{node_id}.{scenario_id}.catalog",
    ]


def _describe_compatibility_caches(
    *,
    current_scenario: str | None,
    has_scenario_ui_application: bool,
    has_scenario_registry_entry: bool,
    has_scenario_catalog: bool,
    effective_ready: bool,
    rebuild_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    required_branches = _collect_compatibility_cache_required_branches(current_scenario)
    present_flags = (
        has_scenario_ui_application,
        has_scenario_registry_entry,
        has_scenario_catalog,
    )
    present_branches = [path for path, present in zip(required_branches, present_flags) if present]
    missing_branches = [path for path, present in zip(required_branches, present_flags) if not present]
    resolver = (
        rebuild_state.get("resolver")
        if isinstance(rebuild_state, Mapping) and isinstance(rebuild_state.get("resolver"), Mapping)
        else {}
    )
    legacy_fallback_active = bool(resolver.get("legacy_fallback"))
    switch_writes_enabled = False
    runtime_removal_blockers: list[str] = []
    if not str(current_scenario or "").strip():
        runtime_removal_blockers.append("current_scenario_missing")
    if not effective_ready:
        runtime_removal_blockers.append("effective_materialization_not_ready")
    if legacy_fallback_active:
        runtime_removal_blockers.append("resolver_legacy_fallback_active")
    return {
        "current_scenario": str(current_scenario or "").strip() or None,
        "required_branches": required_branches,
        "present_branches": present_branches,
        "missing_branches": missing_branches,
        "present_count": len(present_branches),
        "required_count": len(required_branches),
        "present": bool(present_branches),
        "complete": bool(required_branches) and not missing_branches,
        "client_fallback_readable": bool(str(current_scenario or "").strip() and has_scenario_ui_application),
        "switch_writes_enabled": switch_writes_enabled,
        "legacy_fallback_active": legacy_fallback_active,
        "runtime_removal_ready": not runtime_removal_blockers,
        "runtime_removal_blockers": runtime_removal_blockers,
    }


def _cached_materialization_from_rebuild(
    rebuild_state: Mapping[str, Any] | None,
    *,
    max_age_sec: float = 1.0,
) -> dict[str, Any] | None:
    state = rebuild_state if isinstance(rebuild_state, Mapping) else {}
    cached = state.get("materialization") if isinstance(state.get("materialization"), Mapping) else {}
    if not cached:
        return None
    pending = bool(state.get("pending"))
    observed_at = cached.get("observed_at")
    try:
        age_sec = max(0.0, time.time() - float(observed_at)) if observed_at is not None else None
    except Exception:
        age_sec = None
    if pending:
        return dict(cached)
    if age_sec is not None and age_sec <= max(float(max_age_sec or 0.0), 0.0):
        return dict(cached)
    return None


async def _describe_yjs_materialization(
    webspace_id: str,
    *,
    rebuild_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    cached = _cached_materialization_from_rebuild(rebuild_state)
    if cached:
        return cached
    try:
        async with async_read_ydoc(target_webspace_id, prefer_live_room=False) as ydoc:
            ui_map = ydoc.get_map("ui")
            data_map = ydoc.get_map("data")
            registry_map = ydoc.get_map("registry")
            application = _coerce_dict(ui_map.get("application") or {})
            desktop = _coerce_dict(application.get("desktop") or {})
            modals = _coerce_dict(application.get("modals") or {})
            catalog = _coerce_dict(data_map.get("catalog") or {})
            apps = _coerce_list(catalog.get("apps"))
            widgets = _coerce_list(catalog.get("widgets"))
            page_schema = _coerce_dict(desktop.get("pageSchema") or {})
            page_widgets = _coerce_list(page_schema.get("widgets"))
            topbar = _coerce_list(desktop.get("topbar"))
            current_scenario = str(ui_map.get("current_scenario") or "").strip() or None
            scenarios_ui = _coerce_dict(ui_map.get("scenarios") or {})
            scenario_ui_entry = _read_node_scoped_scenario_entry(scenarios_ui, current_scenario) if current_scenario else {}
            scenario_ui_application = _coerce_dict(scenario_ui_entry.get("application") or {})
            scenario_registry_map = _coerce_dict(registry_map.get("scenarios") or {})
            scenario_registry_entry = _read_node_scoped_scenario_entry(scenario_registry_map, current_scenario) if current_scenario else {}
            scenario_data_map = _coerce_dict(data_map.get("scenarios") or {})
            scenario_data_entry = _read_node_scoped_scenario_entry(scenario_data_map, current_scenario) if current_scenario else {}
            scenario_catalog = _coerce_dict(scenario_data_entry.get("catalog") or {})

            has_ui_application = bool(application)
            has_desktop_config = bool(desktop)
            has_desktop_page_schema = bool(page_schema)
            has_apps_catalog_modal = "apps_catalog" in modals
            has_widgets_catalog_modal = "widgets_catalog" in modals
            has_catalog_apps = isinstance(catalog.get("apps"), list)
            has_catalog_widgets = isinstance(catalog.get("widgets"), list)
            missing_branches = _collect_materialization_missing_branches(
                has_ui_application=has_ui_application,
                has_desktop_config=has_desktop_config,
                has_desktop_page_schema=has_desktop_page_schema,
                has_apps_catalog_modal=has_apps_catalog_modal,
                has_widgets_catalog_modal=has_widgets_catalog_modal,
                has_catalog_apps=has_catalog_apps,
                has_catalog_widgets=has_catalog_widgets,
            )
            ready = not missing_branches
            readiness_state = _derive_materialization_readiness_state(
                ready=ready,
                current_scenario=current_scenario,
                has_ui_application=has_ui_application,
                has_desktop_config=has_desktop_config,
                has_desktop_page_schema=has_desktop_page_schema,
                has_apps_catalog_modal=has_apps_catalog_modal,
                has_widgets_catalog_modal=has_widgets_catalog_modal,
                has_catalog_apps=has_catalog_apps,
                has_catalog_widgets=has_catalog_widgets,
            )
            compatibility_caches = _describe_compatibility_caches(
                current_scenario=current_scenario,
                has_scenario_ui_application=bool(scenario_ui_application),
                has_scenario_registry_entry=bool(scenario_registry_entry),
                has_scenario_catalog=bool(scenario_catalog),
                effective_ready=ready,
                rebuild_state=rebuild_state,
            )

            return {
                "ready": ready,
                "readiness_state": readiness_state,
                "missing_branches": missing_branches,
                "compatibility_caches": compatibility_caches,
                "webspace_id": target_webspace_id,
                "current_scenario": current_scenario,
                "has_ui_application": has_ui_application,
                "has_desktop_config": has_desktop_config,
                "has_desktop_page_schema": has_desktop_page_schema,
                "has_apps_catalog_modal": has_apps_catalog_modal,
                "has_widgets_catalog_modal": has_widgets_catalog_modal,
                "has_catalog_apps": has_catalog_apps,
                "has_catalog_widgets": has_catalog_widgets,
                "catalog_counts": {
                    "apps": len(apps),
                    "widgets": len(widgets),
                },
                "topbar_count": len(topbar),
                "page_widget_count": len(page_widgets),
                "snapshot_source": "live_ydoc",
                "observed_at": time.time(),
                "stale": False,
            }
    except Exception as exc:
        missing_branches = _collect_materialization_missing_branches(
            has_ui_application=False,
            has_desktop_config=False,
            has_desktop_page_schema=False,
            has_apps_catalog_modal=False,
            has_widgets_catalog_modal=False,
            has_catalog_apps=False,
            has_catalog_widgets=False,
        )
        compatibility_caches = _describe_compatibility_caches(
            current_scenario=None,
            has_scenario_ui_application=False,
            has_scenario_registry_entry=False,
            has_scenario_catalog=False,
            effective_ready=False,
            rebuild_state=rebuild_state,
        )
        return {
            "ready": False,
            "readiness_state": "degraded",
            "missing_branches": missing_branches,
            "compatibility_caches": compatibility_caches,
            "webspace_id": target_webspace_id,
            "current_scenario": None,
            "has_ui_application": False,
            "has_desktop_config": False,
            "has_desktop_page_schema": False,
            "has_apps_catalog_modal": False,
            "has_widgets_catalog_modal": False,
            "has_catalog_apps": False,
            "has_catalog_widgets": False,
            "catalog_counts": {"apps": 0, "widgets": 0},
            "topbar_count": 0,
            "page_widget_count": 0,
            "snapshot_source": "live_ydoc_error",
            "observed_at": time.time(),
            "stale": True,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


async def _read_live_catalog_items(webspace_id: str, kind: str) -> list[dict[str, Any]]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    bucket = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    try:
        async with async_read_ydoc(target_webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            catalog = _coerce_dict(data_map.get("catalog") or {})
            items = catalog.get(bucket)
            return [dict(it) for it in _coerce_list(items) if isinstance(it, dict)]
    except Exception:
        return []


async def _materialize_catalog_items(webspace_id: str, kind: str) -> list[dict[str, Any]]:
    bucket = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    raw_items = await _read_live_catalog_items(webspace_id, bucket)
    desktop_snapshot = await WebDesktopService().get_snapshot_async(webspace_id)
    installed_ids = set(
        list(getattr(getattr(desktop_snapshot, "installed", None), "apps", []) or [])
        if bucket == "apps"
        else list(getattr(getattr(desktop_snapshot, "installed", None), "widgets", []) or [])
    )
    pinned_ids = {
        str(item.get("id") or "").strip()
        for item in list(getattr(desktop_snapshot, "pinned_widgets", []) or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    default_icon = "apps-outline" if bucket == "apps" else "layers-outline"
    materialized: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            continue
        scenario_id = str(raw.get("scenario_id") or "").strip()
        launch_modal = str(raw.get("launchModal") or "").strip()
        source = str(raw.get("source") or raw.get("origin") or "").strip()
        installed_now = item_id in installed_ids
        pinned_now = bucket == "widgets" and item_id in pinned_ids
        kind_label = ""
        if scenario_id:
            kind_label = "Scenario"
        elif launch_modal:
            kind_label = "Modal"
        elif bucket == "widgets":
            kind_label = "Widget"
        materialized.append(
            {
                "id": item_id,
                "title": str(raw.get("title") or item_id).strip() or item_id,
                "icon": str(raw.get("icon") or "").strip() or default_icon,
                "subtitle": str(raw.get("subtitle") or "").strip() or scenario_id or launch_modal or source or "",
                "kindLabel": kind_label,
                "installType": "app" if bucket == "apps" else "widget",
                "installable": True,
                "installed": installed_now,
                "pinnable": bucket == "widgets" and (installed_now or pinned_now),
                "pinned": pinned_now,
                "scenario_id": scenario_id or None,
                "launchModal": launch_modal or None,
                "source": source or None,
                "origin": str(raw.get("origin") or "").strip() or None,
                "dev": bool(raw.get("dev")),
                "node_id": str(raw.get("node_id") or "").strip() or None,
                "node_label": str(raw.get("node_label") or "").strip() or None,
                "node_compact_label": str(raw.get("node_compact_label") or "").strip() or None,
                "node_color": str(raw.get("node_color") or "").strip() or None,
                "node_index": _coerce_optional_int(raw.get("node_index")),
                "node_local_id": str(raw.get("node_local_id") or raw.get("remote_id") or "").strip() or None,
            }
        )
    return materialized


class NodeStatus(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    node_names: list[str] = Field(default_factory=list)
    primary_node_name: str = ""
    node_label: str = ""
    node_compact_label: str = ""
    node_index: int | None = None
    node_color: str | None = None
    ready: bool
    node_state: str = "ready"
    draining: bool = False
    route_mode: Optional[str] = None
    connected_to_subnet: Optional[bool] = None
    connected_to_hub: Optional[bool] = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class RoleChangeRequest(BaseModel):
    role: str = Field(..., pattern="^(hub|member)$")
    hub_url: Optional[str] = None  # deprecated; ignored
    subnet_id: Optional[str] = None


class RoleChangeResponse(BaseModel):
    ok: bool
    node: NodeStatus
    diagnostics: dict


class HubRootReconnectRequest(BaseModel):
    transport: Optional[str] = Field(None, pattern="^(ws|tcp|nats)?$")
    url_override: Optional[str] = None


class MemberHubReconnectRequest(BaseModel):
    force: bool = False


class HubRootRouteResetRequest(BaseModel):
    reason: str | None = None
    notify_browser: bool = True


class SidecarRestartRequest(BaseModel):
    reconnect_hub_root: bool = True


class NodeNamesUpdateRequest(BaseModel):
    node_names: list[str] | None = None
    value: str | None = None


class MemberUpdateRequest(BaseModel):
    action: str = Field(..., pattern="^(update|start|cancel|rollback)$")
    target_rev: str | None = None
    target_version: str | None = None
    countdown_sec: float | None = None
    drain_timeout_sec: float | None = None
    signal_delay_sec: float | None = None
    reason: str | None = None


class WebspaceYjsActionRequest(BaseModel):
    scenario_id: str | None = None
    scenario_ref: dict[str, Any] | None = None
    home_scenario_ref: dict[str, Any] | None = None
    set_home: bool | None = None
    wait_for_rebuild: bool | None = None
    include_runtime: bool | None = None
    include_rebuild: bool | None = None
    recreate_room: bool | None = None
    requested_id: str | None = None
    title: str | None = None


class WebspaceCreateRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    scenario_id: str | None = None
    scenario_ref: dict[str, Any] | None = None
    dev: bool = False


class WebspaceUpdateRequest(BaseModel):
    title: str | None = None
    home_scenario: str | None = None
    home_scenario_ref: dict[str, Any] | None = None


class WebspaceToggleInstallRequest(BaseModel):
    type: str = Field(..., pattern="^(app|widget)$")
    id: str = Field(..., min_length=1)


class WebspacePinnedWidgetsRequest(BaseModel):
    pinnedWidgets: list[dict[str, Any]] = Field(default_factory=list)


class WebspaceDesktopUpdateRequest(BaseModel):
    installed: dict[str, Any] | None = None
    pinnedWidgets: list[dict[str, Any]] | None = None
    topbar: list[Any] | None = None
    pageSchema: dict[str, Any] | None = None
    iconOrder: list[str] | None = None
    widgetOrder: list[str] | None = None
    hiddenSections: list[str] | None = None


class InfrastateActionRequest(BaseModel):
    id: str = Field(..., min_length=1)
    webspace_id: str | None = None
    node_id: str | None = None
    target_node_id: str | None = None
    value: Any | None = None


class InfraAccessActionRequest(BaseModel):
    id: str = Field(..., min_length=1)
    webspace_id: str | None = None
    target_id: str | None = None
    capability_profile: str | None = None
    ttl_seconds: int | None = None


class UiRuntimeDiagnosticsRequest(BaseModel):
    webspace_id: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class ClientProjectionDemandRequest(BaseModel):
    client_id: str = Field(..., min_length=1)
    device_id: str = ""
    session_id: str = Field(..., min_length=1)
    webspace_id: str | None = None
    role: str = "operator"
    subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: float | None = None


class ClientProjectionDemandTouchRequest(BaseModel):
    device_id: str | None = None
    role: str | None = None
    updated_at: float | None = None


class BrowserProjectionDemandStateRequest(BaseModel):
    client_id: str = Field(..., min_length=1)
    device_id: str = ""
    session_id: str = Field(..., min_length=1)
    webspace_id: str | None = None
    role: str = "operator"
    page: dict[str, Any] | str | None = None
    widgets: list[dict[str, Any] | str] = Field(default_factory=list)
    modals: list[dict[str, Any] | str] = Field(default_factory=list)
    pinnedPanels: list[dict[str, Any] | str] = Field(default_factory=list)
    pinned_panels: list[dict[str, Any] | str] | None = None
    updated_at: float | None = None


class ProjectionDispatchRequest(BaseModel):
    type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "api.node"
    ts: float | None = None
    webspace_ids: list[str] | None = None
    projection_keys: list[str] | None = None


class ProjectionRecordWriteRequest(BaseModel):
    status: str = "ready"
    data: Any = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | str | None = None


class ProjectionRecordsYjsMaterializeRequest(BaseModel):
    webspace_id: str | None = None
    projection_keys: list[str] | None = None
    demanded_only: bool = False
    now: float | None = None


class StatusCardProjectionRecordsMaterializeRequest(BaseModel):
    webspace_id: str | None = None
    card_ids: list[str] | None = None
    demanded_only: bool = False
    write_yjs: bool = False
    now: float | None = None


def _raise_400(detail: str) -> None:
    raise HTTPException(status_code=400, detail=detail)


async def _require_request_token(
    request: Request,
    *,
    authorization: str | None = Header(default=None),
    x_adaos_token: str | None = Header(default=None),
) -> None:
    ensure_token(
        resolve_presented_token(
            x_adaos_token=x_adaos_token,
            authorization=authorization,
            query_token=str(request.query_params.get("token") or "").strip() or None,
        )
    )


def _node_status_payload() -> dict[str, Any]:
    return current_node_status_payload()


@router.get("/status", response_model=NodeStatus, dependencies=[Depends(require_token)])
async def node_status():
    return NodeStatus(**_node_status_payload())


@router.get("/control-plane/objects/self", dependencies=[Depends(require_token)])
async def node_control_plane_object_self() -> dict[str, Any]:
    canonical = current_node_object()
    return {"ok": True, "object": canonical.to_dict()}


@router.get("/control-plane/projections/reliability", dependencies=[Depends(require_token)])
async def node_control_plane_reliability_projection(webspace_id: str | None = None) -> dict[str, Any]:
    projection = current_reliability_projection(webspace_id=webspace_id)
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/overview", dependencies=[Depends(require_token)])
async def node_control_plane_overview_projection(webspace_id: str | None = None, mode: str = "compact") -> dict[str, Any]:
    projection = current_overview_projection(webspace_id=webspace_id)
    token = str(mode or "compact").strip().lower()
    if token in {"compact", "thin"}:
        return {"ok": True, "mode": "compact", "projection": compact_overview_projection_dict(projection)}
    if token in {"full", "compat"}:
        return {"ok": True, "mode": "full", "projection": projection.to_dict()}
    raise HTTPException(status_code=400, detail="mode must be compact or full")


@router.get("/control-plane/projections/inventory", dependencies=[Depends(require_token)])
async def node_control_plane_inventory_projection() -> dict[str, Any]:
    projection = current_inventory_projection()
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/neighborhood", dependencies=[Depends(require_token)])
async def node_control_plane_neighborhood_projection(object_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        projection = current_neighborhood_projection(object_id=object_id, webspace_id=webspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/object", dependencies=[Depends(require_token)])
async def node_control_plane_object_projection(object_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        projection = current_object_projection(object_id, webspace_id=webspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/object-inspector", dependencies=[Depends(require_token)])
async def node_control_plane_object_inspector(object_id: str, task_goal: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        projection = current_object_inspector(object_id, task_goal=task_goal, webspace_id=webspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/topology", dependencies=[Depends(require_token)])
async def node_control_plane_topology_projection(object_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        projection = current_topology_projection(object_id, webspace_id=webspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/projections/task-packet", dependencies=[Depends(require_token)])
async def node_control_plane_task_packet(object_id: str, task_goal: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        projection = current_task_packet(object_id, task_goal=task_goal, webspace_id=webspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/control-plane/contexts/subnet-planning", dependencies=[Depends(require_token)])
async def node_control_plane_subnet_planning_context(
    object_id: str | None = None,
    task_goal: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    try:
        context = current_subnet_planning_context(
            object_id=object_id,
            task_goal=task_goal,
            webspace_id=webspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown control-plane object: {exc.args[0]}") from exc
    return {"ok": True, "context": context}


def _ensure_projection_runtime_handlers() -> None:
    ensure_status_card_projection_handler()


@router.get("/event-envelope-contract", dependencies=[Depends(require_token)])
async def node_event_envelope_contract() -> dict[str, Any]:
    return event_envelope_contract_snapshot(now=time.time())


@router.get("/projection-runtime-ownership", dependencies=[Depends(require_token)])
async def node_projection_runtime_ownership() -> dict[str, Any]:
    return projection_runtime_ownership_contract_snapshot(now=time.time())


@router.get("/projection-platform-emitters", dependencies=[Depends(require_token)])
async def node_projection_platform_emitters() -> dict[str, Any]:
    return platform_emitter_contract_snapshot(now=time.time())


@router.get("/projection-pilot/readiness-contract", dependencies=[Depends(require_token)])
async def node_projection_pilot_readiness_contract() -> dict[str, Any]:
    return projection_pilot_readiness_contract_snapshot(now=time.time())


@router.get("/projection-demand/contract", dependencies=[Depends(require_token)])
async def node_projection_demand_contract() -> dict[str, Any]:
    return client_subscription_contract_snapshot(now=time.time())


@router.get("/projection-demand/surface-lifecycle-contract", dependencies=[Depends(require_token)])
async def node_projection_demand_surface_lifecycle_contract() -> dict[str, Any]:
    return browser_surface_lifecycle_contract_snapshot(now=time.time())


@router.get("/projection-demand/restore-contract", dependencies=[Depends(require_token)])
async def node_projection_demand_restore_contract() -> dict[str, Any]:
    return projection_demand_restore_contract_snapshot(now=time.time())


@router.get("/projection-demand", dependencies=[Depends(require_token)])
async def node_projection_demand(
    webspace_id: str | None = None,
    include_stale: bool = True,
    stale_after_s: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return projection_demand_snapshot(
        webspace_id=target_webspace_id,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
    )


@router.post("/projection-demand/client", dependencies=[Depends(require_token)])
async def node_projection_demand_client(payload: ClientProjectionDemandRequest) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(payload.webspace_id)
    try:
        record = write_client_subscription_record(
            {
                "client_id": payload.client_id,
                "device_id": payload.device_id,
                "session_id": payload.session_id,
                "webspace_id": target_webspace_id,
                "role": payload.role,
                "subscriptions": payload.subscriptions,
                "updated_at": payload.updated_at,
            }
        )
    except ValueError as exc:
        _raise_400(str(exc))
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "record": record.to_dict(),
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.post("/projection-demand/browser-state", dependencies=[Depends(require_token)])
async def node_projection_demand_browser_state(payload: BrowserProjectionDemandStateRequest) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(payload.webspace_id)
    record = build_browser_projection_demand_record(
        client_id=payload.client_id,
        device_id=payload.device_id,
        session_id=payload.session_id,
        webspace_id=target_webspace_id,
        role=payload.role,
        page=payload.page,
        widgets=payload.widgets,
        modals=payload.modals,
        pinned_panels=payload.pinned_panels if payload.pinned_panels is not None else payload.pinnedPanels,
        updated_at=payload.updated_at,
    )
    stored = write_client_subscription_record(record)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "record": stored.to_dict(),
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.post("/projection-demand/client/{client_id}/{session_id}/touch", dependencies=[Depends(require_token)])
async def node_projection_demand_client_touch(
    client_id: str,
    session_id: str,
    payload: ClientProjectionDemandTouchRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    request_payload = payload or ClientProjectionDemandTouchRequest()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    record = touch_client_subscription_record(
        client_id=client_id,
        session_id=session_id,
        webspace_id=target_webspace_id,
        device_id=request_payload.device_id,
        role=request_payload.role,
        updated_at=request_payload.updated_at,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="projection_demand_session_not_found")
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "record": record.to_dict(),
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.delete("/projection-demand/client/{client_id}/{session_id}", dependencies=[Depends(require_token)])
async def node_projection_demand_client_delete(
    client_id: str,
    session_id: str,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    deleted = delete_client_subscription_record(
        client_id=client_id,
        session_id=session_id,
        webspace_id=target_webspace_id,
    )
    return {
        "ok": True,
        "accepted": deleted,
        "webspace_id": target_webspace_id,
        "deleted": deleted,
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.get("/projection-records", dependencies=[Depends(require_token)])
async def node_projection_records(webspace_id: str | None = None) -> dict[str, Any]:
    return projection_record_registry_snapshot(webspace_id=_coerce_node_webspace_id(webspace_id))


@router.post("/projection-records", dependencies=[Depends(require_token)])
async def node_projection_record_write(payload: ProjectionRecordWriteRequest) -> dict[str, Any]:
    try:
        record = write_projection_record(
            {
                "status": payload.status,
                "data": payload.data,
                "meta": payload.meta,
                "error": payload.error,
            }
        )
    except ValueError as exc:
        _raise_400(str(exc))
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": record.meta.webspace_id,
        "record": record.to_dict(),
        "snapshot": projection_record_registry_snapshot(webspace_id=record.meta.webspace_id),
    }


@router.get("/projection-records/item", dependencies=[Depends(require_token)])
async def node_projection_record_item(projection_key: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    record = get_projection_record(webspace_id=target_webspace_id, projection_key=projection_key)
    if record is None:
        raise HTTPException(status_code=404, detail="projection_record_not_found")
    return {"ok": True, "webspace_id": target_webspace_id, "record": record.to_dict()}


@router.get("/projection-records/browser-cache", dependencies=[Depends(require_token)])
async def node_projection_records_browser_cache(
    response: Response,
    webspace_id: str | None = None,
    client_id: str | None = None,
    session_id: str | None = None,
    projection_keys: list[str] | None = Query(default=None),
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Any:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    payload = browser_projection_record_snapshot(
        webspace_id=target_webspace_id,
        client_id=client_id,
        session_id=session_id,
        projection_keys=projection_keys,
        include_hidden=include_hidden,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
    )
    etag = str(payload.get("etag") or "")
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    response.headers.update(headers)
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return payload


@router.get("/projection-records/browser-adapter-contract", dependencies=[Depends(require_token)])
async def node_projection_records_browser_adapter_contract() -> dict[str, Any]:
    return browser_projection_adapter_contract_snapshot(now=time.time())


@router.get("/projection-records/node-multiplicity-contract", dependencies=[Depends(require_token)])
async def node_projection_records_node_multiplicity_contract() -> dict[str, Any]:
    return projection_records_node_multiplicity_contract_snapshot(now=time.time())


@router.get("/projection-records/yjs/cache", dependencies=[Depends(require_token)])
async def node_projection_records_yjs_cache(webspace_id: str | None = None) -> dict[str, Any]:
    return await read_projection_records_yjs_cache(webspace_id=_coerce_node_webspace_id(webspace_id))


@router.post("/projection-records/yjs/materialize", dependencies=[Depends(require_token)])
async def node_projection_records_yjs_materialize(
    payload: ProjectionRecordsYjsMaterializeRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    request_payload = payload or ProjectionRecordsYjsMaterializeRequest()
    return await materialize_projection_records_to_yjs(
        webspace_id=_coerce_node_webspace_id(request_payload.webspace_id or webspace_id),
        projection_keys=request_payload.projection_keys,
        demanded_only=request_payload.demanded_only,
        now=request_payload.now,
    )


@router.post("/projection-records/status-cards/materialize", dependencies=[Depends(require_token)])
async def node_projection_records_materialize_status_cards(
    payload: StatusCardProjectionRecordsMaterializeRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    request_payload = payload or StatusCardProjectionRecordsMaterializeRequest()
    target_webspace_id = _coerce_node_webspace_id(request_payload.webspace_id or webspace_id)
    result = materialize_status_card_projection_records(
        webspace_id=target_webspace_id,
        card_ids=request_payload.card_ids,
        demanded_only=request_payload.demanded_only,
        now=request_payload.now,
        access={"audience": "shared", "visibility": "operator"},
    )
    if request_payload.write_yjs:
        result["yjs"] = await materialize_projection_records_to_yjs(
            webspace_id=target_webspace_id,
            projection_keys=normalize_projection_record_keys(result.get("records") or []),
            demanded_only=False,
            now=request_payload.now,
        )
    return result


@router.get("/projection-dispatcher", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_snapshot() -> dict[str, Any]:
    _ensure_projection_runtime_handlers()
    return projection_dispatcher_snapshot()


@router.get("/projection-dispatcher/core-skill-contract", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_core_skill_contract(
    webspace_id: str | None = None,
    projection_keys: list[str] | None = Query(default=None),
    include_hidden: bool = True,
    include_stale: bool = True,
) -> dict[str, Any]:
    _ensure_projection_runtime_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return core_skill_refresh_contract_snapshot(
        webspace_ids=[target_webspace_id],
        projection_keys=projection_keys,
        include_hidden=include_hidden,
        include_stale=include_stale,
    )


@router.get("/projection-dispatcher/memory-contract", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_memory_contract() -> dict[str, Any]:
    return projection_dispatcher_memory_contract_snapshot(now=time.time())


@router.post("/projection-dispatcher/dispatch", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_dispatch(payload: ProjectionDispatchRequest) -> dict[str, Any]:
    _ensure_projection_runtime_handlers()
    event = Event(
        type=payload.type,
        payload=payload.payload,
        source=payload.source,
        ts=float(payload.ts if payload.ts is not None else time.time()),
    )
    report = await dispatch_demanded_projection_refresh(
        event,
        webspace_ids=payload.webspace_ids,
        projection_keys=payload.projection_keys,
    )
    return {
        "ok": True,
        "accepted": True,
        "report": report.to_dict(),
        "dispatcher": projection_dispatcher_snapshot(),
    }


@router.get("/projection-diagnostics", dependencies=[Depends(require_token)])
async def node_projection_diagnostics(
    webspace_id: str | None = None,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    include_yjs_cache: bool = False,
    materialize_yjs_cache: bool = False,
    materialize_status_cards: bool = False,
) -> dict[str, Any]:
    _ensure_projection_runtime_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    refreshes: dict[str, Any] = {}
    if materialize_status_cards:
        refreshes["status_cards"] = materialize_status_card_projection_records(
            webspace_id=target_webspace_id,
            demanded_only=True,
            access={"audience": "shared", "visibility": "operator"},
        )
    if materialize_yjs_cache:
        refreshes["projection_records_yjs"] = await materialize_projection_records_to_yjs(
            webspace_id=target_webspace_id,
            demanded_only=True,
        )
    yjs_cache = None
    if include_yjs_cache or materialize_yjs_cache:
        yjs_cache = await read_projection_records_yjs_cache(webspace_id=target_webspace_id)
    diagnostics = projection_operator_diagnostics(
        webspace_id=target_webspace_id,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        yjs_cache=yjs_cache,
    )
    if refreshes:
        diagnostics["refreshes"] = refreshes
    return diagnostics


@router.get("/reliability", dependencies=[Depends(require_token)])
async def node_reliability() -> dict[str, Any]:
    return await _current_reliability_payload_async()


@router.get("/reliability/summary", dependencies=[Depends(require_token)])
async def node_reliability_summary(
    webspace_id: str | None = None,
    mode: str | None = None,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    requested_mode = str(mode or "compat").strip().lower()
    if requested_mode in {"thin", "status", "status_plane"}:
        resolved_webspace_id = _coerce_node_webspace_id(webspace_id)
        status_registry = _current_status_registry_snapshot(webspace_id=resolved_webspace_id)
        payload = _thin_runtime_reliability_payload(
            status_registry,
            webspace_id=resolved_webspace_id,
        )
        return _json_response_with_etag(
            payload,
            if_none_match=if_none_match,
            mode="thin",
        )

    reliability = await _current_reliability_payload_async(webspace_id=webspace_id)
    status_registry = _current_status_registry_snapshot(webspace_id=webspace_id)
    payload = _compact_runtime_reliability_payload(
        reliability,
        webspace_id=webspace_id,
        status_registry=status_registry,
    )
    payload["mode"] = "full" if requested_mode == "full" else "compat"
    return _json_response_with_etag(
        payload,
        if_none_match=if_none_match,
        mode=str(payload["mode"]),
    )


@router.get("/reliability/summary/metrics", dependencies=[Depends(require_token)])
async def node_reliability_summary_metrics(
    webspace_id: str | None = None,
    receiver: str | None = None,
    owner: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    return {
        "ok": True,
        "metrics": _reliability_summary_metrics_snapshot(
            webspace_id=webspace_id,
            receiver=receiver,
            owner=owner,
            limit=limit,
        ),
    }


@router.get("/status/cards", dependencies=[Depends(require_token)])
async def node_status_cards(
    webspace_id: str | None = None,
    owner: str | None = None,
    scope: str | None = None,
    include_stale: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    snapshot = _current_status_registry_snapshot(
        webspace_id=webspace_id,
        owner=owner,
        scope=scope,
        include_stale=include_stale,
    )
    return _compact_status_registry_payload(
        snapshot,
        webspace_id=webspace_id,
        limit=limit,
        source="api.node.status.cards",
    )


@router.post("/hub-root/reconnect", dependencies=[Depends(require_token)])
async def hub_root_reconnect(payload: HubRootReconnectRequest) -> dict[str, Any]:
    return await request_hub_root_reconnect(transport=payload.transport, url_override=payload.url_override)


@router.post("/member-hub/reconnect", dependencies=[Depends(require_token)])
async def member_hub_reconnect(payload: MemberHubReconnectRequest) -> dict[str, Any]:
    return await request_member_hub_reconnect(force=bool(payload.force))


@router.post("/hub-root/route-reset", dependencies=[Depends(require_token)])
async def hub_root_route_reset(payload: HubRootRouteResetRequest) -> dict[str, Any]:
    return await request_hub_root_route_reset(
        reason=str(payload.reason or "").strip() or "supervisor_route_watchdog",
        notify_browser=bool(payload.notify_browser),
    )


@router.get("/sidecar/status", dependencies=[Depends(require_token)])
async def sidecar_status(request: Request) -> dict[str, Any]:
    if _supervisor_enabled():
        return await _proxy_supervisor_json(method="GET", path="/api/supervisor/sidecar/status", timeout=3.0)
    conf = await anyio.to_thread.run_sync(load_config)
    reliability = await _current_reliability_payload_async()
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    process = realtime_sidecar_listener_snapshot(
        getattr(request.app.state, "realtime_sidecar_proc", None),
        role=conf.role,
    )
    return {
        "ok": True,
        "runtime": runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {},
        "process": process,
    }


@router.post("/sidecar/restart", dependencies=[Depends(require_token)])
async def sidecar_restart(request: Request, payload: SidecarRestartRequest) -> dict[str, Any]:
    if _supervisor_enabled():
        return await _proxy_supervisor_json(
            method="POST",
            path="/api/supervisor/sidecar/restart",
            payload={"reconnect_hub_root": bool(payload.reconnect_hub_root)},
            timeout=10.0,
        )
    conf = await anyio.to_thread.run_sync(load_config)
    proc = getattr(request.app.state, "realtime_sidecar_proc", None)
    new_proc, restart_result = await restart_realtime_sidecar_subprocess(proc=proc, role=conf.role)
    request.app.state.realtime_sidecar_proc = new_proc
    reconnect_result: dict[str, Any] | None = None
    if bool(payload.reconnect_hub_root) and str(conf.role or "").strip().lower() == "hub":
        reconnect_result = await request_hub_root_reconnect()
    reliability = await _current_reliability_payload_async()
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    return {
        "ok": True,
        "restart": restart_result,
        "reconnect": reconnect_result,
        "runtime": runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {},
        "process": realtime_sidecar_listener_snapshot(new_proc, role=conf.role),
    }


@router.post("/role", response_model=RoleChangeResponse, dependencies=[Depends(require_token)])
async def node_change_role(req: Request, payload: RoleChangeRequest):
    """
    Switch local node role.

    Backward-compatibility: `hub_url` is accepted but ignored (deprecated).
    """
    new_role = payload.role.lower().strip()
    sub_id = payload.subnet_id
    deprecated_fields: list[str] = ["hub_url"] if payload.hub_url else []

    conf = await switch_role(req.app, new_role, hub_url=None, subnet_id=sub_id)
    route_mode, connected = route_info(conf.role)
    display = _local_node_display()

    diags = {
        "requested_role": new_role,
        "subnet_id_used": sub_id,
        "now_ready": is_ready(),
        "node_state": runtime_lifecycle_snapshot().get("node_state", "ready"),
        "route_mode": route_mode,
        "connected_to_subnet": connected,
        "connected_to_hub": connected,
        "deprecated_fields": deprecated_fields,
    }
    return RoleChangeResponse(
        ok=True,
        node=NodeStatus(
            node_id=conf.node_id,
            subnet_id=conf.subnet_id,
            role=conf.role,
            node_names=list(getattr(conf, "node_names", []) or []),
            primary_node_name=str(getattr(conf, "primary_node_name", "") or ""),
            node_label=str(display.get("node_label") or ""),
            node_compact_label=str(display.get("node_compact_label") or ""),
            node_index=display.get("node_index"),
            node_color=display.get("node_color"),
            ready=is_ready(),
            node_state=str(runtime_lifecycle_snapshot().get("node_state") or "ready"),
            draining=bool(runtime_lifecycle_snapshot().get("draining")),
            route_mode=route_mode,
            connected_to_subnet=connected,
            connected_to_hub=connected,
        ),
        diagnostics=diags,
    )


@router.get("/names", dependencies=[Depends(require_token)])
async def node_names() -> dict[str, Any]:
    conf = load_config()
    display = _local_node_display()
    return {
        "ok": True,
        "node_id": conf.node_id,
        "role": conf.role,
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
        "node_label": display.get("node_label"),
        "node_compact_label": display.get("node_compact_label"),
        "node_index": display.get("node_index"),
        "node_color": display.get("node_color"),
    }


@router.post("/names", dependencies=[Depends(require_token)])
async def update_node_names(payload: NodeNamesUpdateRequest) -> dict[str, Any]:
    source = payload.node_names if payload.node_names is not None else payload.value
    conf = save_node_names_config(source)
    display = _local_node_display()
    return {
        "ok": True,
        "node_id": conf.node_id,
        "role": conf.role,
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
        "node_label": display.get("node_label"),
        "node_compact_label": display.get("node_compact_label"),
        "node_index": display.get("node_index"),
        "node_color": display.get("node_color"),
    }


@router.get("/yjs/runtime", dependencies=[Depends(require_token)])
async def node_yjs_runtime(webspace_id: str | None = None) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return {
        "ok": True,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/memory/status", dependencies=[Depends(require_token)])
async def node_memory_status() -> dict[str, Any]:
    """Return a cheap runtime-local memory snapshot.

    This endpoint intentionally does not depend on the supervisor memory bridge:
    when route/profiler plumbing is degraded, operators still need a bounded
    process RSS signal through the active runtime API.
    """
    pid = os.getpid()
    now = time.time()
    process: dict[str, Any] = {
        "pid": pid,
        "rss_bytes": None,
        "vms_bytes": None,
        "create_time": None,
        "uptime_s": None,
        "num_threads": None,
        "children_total": 0,
        "children_rss_bytes": 0,
        "family_rss_bytes": None,
    }
    psutil_error = ""
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        mem = proc.memory_info()
        rss = int(getattr(mem, "rss", 0) or 0)
        vms = int(getattr(mem, "vms", 0) or 0)
        create_time = float(proc.create_time())
        children = proc.children(recursive=True)
        children_rss = 0
        for child in children:
            try:
                children_rss += int(child.memory_info().rss)
            except Exception:
                continue
        process.update(
            {
                "rss_bytes": rss,
                "vms_bytes": vms,
                "create_time": create_time,
                "uptime_s": round(max(0.0, now - create_time), 3),
                "num_threads": int(proc.num_threads()),
                "children_total": len(children),
                "children_rss_bytes": children_rss,
                "family_rss_bytes": rss + children_rss,
            }
        )
    except Exception as exc:
        psutil_error = f"{type(exc).__name__}: {exc}"

    tracing = bool(tracemalloc.is_tracing())
    traced_current = None
    traced_peak = None
    if tracing:
        try:
            traced_current, traced_peak = tracemalloc.get_traced_memory()
        except Exception:
            traced_current = None
            traced_peak = None

    return {
        "ok": True,
        "ts": now,
        "node": _local_node_display(),
        "process": process,
        "python": {
            "gc_count": list(gc.get_count()),
            "gc_threshold": list(gc.get_threshold()),
            "tracemalloc_tracing": tracing,
            "tracemalloc_current_bytes": traced_current,
            "tracemalloc_peak_bytes": traced_peak,
        },
        "errors": {"psutil": psutil_error} if psutil_error else {},
    }


@router.get("/infrastate/snapshot", dependencies=[Depends(require_token)])
async def node_infrastate_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    ctx = get_ctx()
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )

    def _fallback_snapshot(exc: Exception) -> dict[str, Any]:
        lifecycle = runtime_lifecycle_snapshot()
        yjs_runtime = yjs_sync_runtime_snapshot(
            role=str(getattr(conf, "role", "") or ""),
            webspace_id=target_webspace_id,
        )
        error_text = f"{type(exc).__name__}: {exc}"
        return {
            "summary": {
                "label": "Infra State",
                "value": str(lifecycle.get("node_state") or "degraded"),
                "subtitle": f"webspace {target_webspace_id}",
                "description": f"fallback snapshot: {error_text}",
                "updated_at": time.time(),
            },
            "actions": [],
            "update_actions": [],
            "nodes": [],
            "yjs_webspaces": [],
            "node_editor": {"names_csv": "", "editable": False, "scope": "fallback"},
            "build": [],
            "steps": [
                {
                    "id": "lifecycle",
                    "title": "Lifecycle",
                    "status": str(lifecycle.get("node_state") or "degraded"),
                    "description": str(lifecycle.get("reason") or "runtime fallback snapshot"),
                },
                {
                    "id": "yjs_runtime",
                    "title": "Yjs runtime",
                    "status": "ok" if yjs_runtime else "idle",
                    "description": str(
                        (yjs_runtime.get("assessment") or {}).get("state")
                        if isinstance(yjs_runtime, dict)
                        else "unknown"
                    ),
                },
            ],
            "realtime": [],
            "slots": [],
            "skills": [],
            "logs": [
                {
                    "id": "snapshot-error",
                    "title": "snapshot-error",
                    "status": "warn",
                    "preview": error_text,
                    "content": error_text,
                }
            ],
            "events": [],
            "lifecycle": lifecycle,
            "yjs_runtime": yjs_runtime,
            "last_refresh_ts": time.time(),
            "fallback": True,
            "errors": [error_text],
        }

    def _load_snapshot() -> dict[str, Any]:
        try:
            result = mgr.run_tool(
                "infrastate_skill",
                "get_snapshot",
                {"webspace_id": target_webspace_id, "project": False},
            )
            return result if isinstance(result, dict) else {"summary": {}, "raw": result}
        except Exception as exc:
            _log.warning("node infrastate snapshot fallback webspace=%s", target_webspace_id, exc_info=True)
            return _fallback_snapshot(exc)

    snapshot = await anyio.to_thread.run_sync(_load_snapshot)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "degraded": bool(snapshot.get("fallback")) if isinstance(snapshot, dict) else False,
        "error": (snapshot.get("errors") or [None])[0] if isinstance(snapshot, dict) else None,
        "snapshot": snapshot,
    }


@router.get("/logs/{category}", dependencies=[Depends(require_token)])
async def node_logs(
    category: str,
    limit: int = 5,
    lines: int = 200,
    contains: str | None = None,
    skill: str | None = None,
    file: str | None = None,
) -> dict[str, Any]:
    try:
        category_token = normalize_log_category(category)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown log category: {category}") from exc

    def _load_logs() -> dict[str, Any]:
        return list_local_logs(
            category=category_token,
            limit=limit,
            lines=lines,
            contains=contains,
            skill=skill,
            file=file,
            source_mode="node_local_logs_dir",
        )

    return {"ok": True, "logs": await anyio.to_thread.run_sync(_load_logs)}


@router.post("/ui/diagnostics", dependencies=[Depends(require_token)])
async def node_ui_runtime_diagnostics(payload: UiRuntimeDiagnosticsRequest) -> dict[str, Any]:
    return await ingest_ui_runtime_diagnostics(
        {"webspace_id": payload.webspace_id, "events": payload.events},
        webspace_id=payload.webspace_id,
    )


@router.post("/infrastate/action", dependencies=[Depends(require_token)])
async def node_infrastate_action(payload: InfrastateActionRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(payload.webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    ctx = get_ctx()
    action_id = str(payload.id or "").strip()
    if action_id == "marketplace_install":
        value = payload.value if isinstance(payload.value, dict) else {}
        target_kind = str(value.get("kind") or value.get("target_kind") or "").strip().lower()
        target_id = str(value.get("id") or value.get("target_id") or "").strip()
        target_node_id = str(
            value.get("target_node_id")
            or value.get("node_id")
            or payload.target_node_id
            or payload.node_id
            or ""
        ).strip()
        if target_kind not in {"skill", "scenario"} or not target_id:
            return {
                "ok": False,
                "accepted": False,
                "webspace_id": target_webspace_id,
                "action": action_id,
                "error": "marketplace_install_requires_target",
            }
        operation = submit_install_operation(
            target_kind=target_kind,
            target_id=target_id,
            webspace_id=target_webspace_id,
            initiator={
                "kind": "api.node",
                "id": "marketplace_install",
                "target_node_id": target_node_id or None,
            },
            ctx=ctx,
        )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": target_webspace_id,
            "action": action_id,
            "target_node_id": target_node_id or None,
            "operation_id": operation.get("operation_id"),
            "result": {
                "ok": True,
                "accepted": True,
                "operation_id": operation.get("operation_id"),
                "operation": operation,
            },
            "snapshot": {},
        }
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )
    event_payload: dict[str, Any] = {
        "id": action_id,
        "webspace_id": target_webspace_id,
    }
    node_id = str(payload.node_id or payload.target_node_id or "").strip()
    target_node_id = str(payload.target_node_id or payload.node_id or "").strip()
    value = payload.value
    if node_id:
        event_payload["node_id"] = node_id
    if target_node_id:
        event_payload["target_node_id"] = target_node_id
    if value is not None:
        event_payload["value"] = value
    ctx.bus.publish(Event(type="infrastate.action", payload=event_payload, source="api.node", ts=time.time()))
    waiter = getattr(ctx.bus, "wait_for_idle", None)
    if callable(waiter):
        try:
            await waiter(timeout=2.5)
        except Exception:
            _log.debug("wait_for_idle failed after infrastate.action", exc_info=True)

    def _load_snapshot() -> dict[str, Any]:
        result = mgr.run_tool(
            "infrastate_skill",
            "get_snapshot",
            {"webspace_id": target_webspace_id, "project": False},
        )
        return result if isinstance(result, dict) else {"summary": {}, "raw": result}

    snapshot = await anyio.to_thread.run_sync(_load_snapshot)
    ui_state = snapshot.get("ui_state") if isinstance(snapshot.get("ui_state"), dict) else {}
    action_result = ui_state.get("last_result") if isinstance(ui_state.get("last_result"), dict) else {}
    if str(ui_state.get("last_action") or "").strip() != event_payload["id"]:
        action_result = {}
    action_operation = action_result.get("operation") if isinstance(action_result.get("operation"), dict) else {}
    operation_id = (
        str(action_result.get("operation_id") or action_operation.get("operation_id") or "").strip() or None
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "action": event_payload["id"],
        "operation_id": operation_id,
        "result": action_result,
        "snapshot": snapshot,
    }


@router.post("/infra_access/action", dependencies=[Depends(require_token)])
async def node_infra_access_action(payload: InfraAccessActionRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(payload.webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    ctx = get_ctx()
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )
    action_id = str(payload.id or "").strip().lower()
    target_id = str(payload.target_id or "").strip() or None

    def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        if action_id == "refresh":
            snapshot = mgr.run_tool(
                "infra_access_skill",
                "refresh_snapshot",
                {
                    "webspace_id": target_webspace_id,
                    "target_id": target_id,
                },
            )
            return (
                {"ok": True, "accepted": True, "action": action_id},
                snapshot if isinstance(snapshot, dict) else {"raw": snapshot},
            )
        if action_id == "issue_codex_session":
            result = mgr.run_tool(
                "infra_access_skill",
                "issue_codex_connection",
                {
                    "webspace_id": target_webspace_id,
                    "target_id": target_id,
                    "capability_profile": str(payload.capability_profile or "ProfileOpsRead"),
                    "ttl_seconds": int(payload.ttl_seconds or 28_800),
                },
            )
            snapshot = mgr.run_tool(
                "infra_access_skill",
                "get_snapshot",
                {
                    "webspace_id": target_webspace_id,
                    "target_id": target_id,
                },
            )
            return (
                result if isinstance(result, dict) else {"ok": True, "accepted": True, "action": action_id, "raw": result},
                snapshot if isinstance(snapshot, dict) else {"raw": snapshot},
            )
        raise HTTPException(status_code=400, detail=f"unsupported infra_access action: {action_id}")

    try:
        result, snapshot = await anyio.to_thread.run_sync(_run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning("node infra_access action failed webspace=%s action=%s", target_webspace_id, action_id, exc_info=True)
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "action": action_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": bool(result.get("ok", True)),
        "accepted": True,
        "webspace_id": target_webspace_id,
        "action": action_id,
        "result": result,
        "snapshot": snapshot,
    }


@router.get("/yjs/webspaces", dependencies=[Depends(require_token)])
async def node_yjs_webspaces() -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    items = [
        {
            "id": item.id,
            "title": item.title,
            "created_at": item.created_at,
            "kind": item.kind,
            "home_scenario": item.home_scenario,
            "home_scenario_ref": getattr(item, "home_scenario_ref", None),
            "source_mode": item.source_mode,
            "node_id": getattr(item, "node_id", None) or _local_node_id(),
            "node_label": getattr(item, "node_label", None) or _local_node_label(),
            "node_compact_label": getattr(item, "node_compact_label", None),
            "node_index": getattr(item, "node_index", None),
            "node_color": getattr(item, "node_color", None),
            "current_scenario": getattr(item, "current_scenario", None),
            "stored_home_scenario_exists": getattr(item, "stored_home_scenario_exists", None),
            "home_scenario_exists": getattr(item, "home_scenario_exists", True),
            "current_scenario_exists": getattr(item, "current_scenario_exists", None),
            "degraded": getattr(item, "degraded", False),
            "validation_reason": getattr(item, "validation_reason", None),
            "recommended_action": getattr(item, "recommended_action", None),
        }
        for item in WebspaceService().list(mode="mixed")
    ]
    return {
        "ok": True,
        "accepted": True,
        "items": items,
    }


@router.post("/yjs/webspaces", dependencies=[Depends(require_token)])
async def node_yjs_create_webspace(payload: WebspaceCreateRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip() or "web_desktop"
    info = await WebspaceService().create(
        str(payload.id or "").strip() or None,
        str(payload.title or "").strip() or None,
        scenario_id=scenario_id,
        scenario_ref=payload.scenario_ref if isinstance(payload.scenario_ref, dict) else None,
        dev=bool(payload.dev),
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace": {
            "id": info.id,
            "title": info.title,
            "created_at": info.created_at,
            "kind": info.kind,
            "home_scenario": info.home_scenario,
            "home_scenario_ref": getattr(info, "home_scenario_ref", None),
            "source_mode": info.source_mode,
        },
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=info.id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/runtime", dependencies=[Depends(require_token)])
async def node_yjs_webspace_runtime(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return {
        "ok": True,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}", dependencies=[Depends(require_token)])
async def node_yjs_webspace_state(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    state = await describe_webspace_operational_state(target_webspace_id)
    validation = await describe_webspace_validation_state(target_webspace_id)
    overlay = describe_webspace_overlay_state(target_webspace_id)
    projection = await describe_webspace_projection_state(target_webspace_id)
    rebuild = describe_webspace_rebuild_state(target_webspace_id)
    desktop = (await WebDesktopService().get_snapshot_async(target_webspace_id)).to_dict()
    materialization = await _describe_yjs_materialization(target_webspace_id, rebuild_state=rebuild)
    return {
        "ok": True,
        "accepted": True,
        "webspace": state.to_dict(),
        "validation": validation,
        "overlay": overlay,
        "desktop": desktop,
        "projection": projection,
        "rebuild": rebuild,
        "materialization": materialization,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/validation", dependencies=[Depends(require_token)])
async def node_yjs_webspace_validation_state(webspace_id: str) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "validation": await describe_webspace_validation_state(target_webspace_id),
    }


@router.get("/yjs/webspaces/{webspace_id}/rebuild", dependencies=[Depends(require_token)])
async def node_yjs_webspace_rebuild_state(
    webspace_id: str,
    include_runtime: bool = False,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    rebuild = describe_webspace_rebuild_state(target_webspace_id)
    result = {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "rebuild": rebuild,
    }
    if include_runtime:
        result["runtime"] = yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        )
    return result


@router.get("/yjs/webspaces/{webspace_id}/materialization", dependencies=[Depends(require_token)])
async def node_yjs_webspace_materialization_state(
    webspace_id: str,
    include_runtime: bool = False,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    rebuild = describe_webspace_rebuild_state(target_webspace_id)
    materialization = await _describe_yjs_materialization(target_webspace_id, rebuild_state=rebuild)
    result = {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "materialization": materialization,
        "rebuild": rebuild,
    }
    if include_runtime:
        result["runtime"] = yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        )
    return result


@router.patch("/yjs/webspaces/{webspace_id}", dependencies=[Depends(require_token)])
async def node_yjs_update_webspace(webspace_id: str, payload: WebspaceUpdateRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    update_kwargs: dict[str, Any] = {
        "title": str(payload.title or "").strip() or None,
        "home_scenario": str(payload.home_scenario or "").strip() or None,
    }
    if "home_scenario_ref" in getattr(payload, "model_fields_set", set()):
        update_kwargs["home_scenario_ref"] = payload.home_scenario_ref
    info = await WebspaceService().update_metadata(
        target_webspace_id,
        **update_kwargs,
    )
    if info is None:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "webspace_not_found",
        }
    return {
        "ok": True,
        "accepted": True,
        "webspace": {
            "id": info.id,
            "title": info.title,
            "created_at": info.created_at,
            "kind": info.kind,
            "home_scenario": info.home_scenario,
            "home_scenario_ref": getattr(info, "home_scenario_ref", None),
            "source_mode": info.source_mode,
        },
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.post("/yjs/webspaces/{webspace_id}/backup", dependencies=[Depends(require_token)])
async def node_yjs_backup(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    store = get_ystore_for_webspace(target_webspace_id)
    await store.backup_to_disk()
    result = {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }
    _publish_yjs_control_event(
        action="backup",
        webspace_id=target_webspace_id,
        result=result,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/reload", dependencies=[Depends(require_token)])
async def node_yjs_reload(webspace_id: str, payload: WebspaceYjsActionRequest, request: Request) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip() or None
    recreate_room_requested = bool(payload.recreate_room)
    requested_action = "reset" if recreate_room_requested else "reload"
    event_payload = _trace_yjs_control_ingress(
        request=request,
        kind="desktop.webspace.reload",
        webspace_id=target_webspace_id,
        scenario_id=scenario_id,
        recreate_room=recreate_room_requested,
    )
    runtime_before = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=target_webspace_id,
    )
    result = await reload_webspace_from_scenario(
        target_webspace_id,
        scenario_id=scenario_id,
        action=requested_action,
        event_payload=event_payload,
    )
    result = _attach_runtime_and_rebuild(
        result,
        role=conf.role,
        webspace_id=target_webspace_id,
        include_rebuild=recreate_room_requested,
    )
    result = _attach_yjs_action_debug(
        result,
        requested_endpoint="reload",
        recreate_room_requested=recreate_room_requested,
        runtime_before=runtime_before,
    )
    _publish_yjs_control_event(
        action="reload",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=scenario_id,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/toggle-install", dependencies=[Depends(require_token)])
async def node_yjs_toggle_install(webspace_id: str, payload: WebspaceToggleInstallRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    svc.toggle_install_with_live_room(str(payload.type), str(payload.id), target_webspace_id)
    installed = await svc.get_installed_async(target_webspace_id)
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "type": str(payload.type),
        "id": str(payload.id),
        "installed": installed.to_dict(),
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/desktop", dependencies=[Depends(require_token)])
async def node_yjs_desktop_state(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    desktop = await WebDesktopService().get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/catalog/{kind}", dependencies=[Depends(require_token)])
async def node_yjs_catalog_state(webspace_id: str, kind: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    normalized_kind = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    rebuild = describe_webspace_rebuild_state(target_webspace_id)
    materialization = await _describe_yjs_materialization(target_webspace_id, rebuild_state=rebuild)
    items = await _materialize_catalog_items(target_webspace_id, normalized_kind)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "kind": normalized_kind,
        "items": items,
        "materialization": materialization,
        "rebuild": rebuild,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.post("/yjs/webspaces/{webspace_id}/desktop/pinned-widgets", dependencies=[Depends(require_token)])
async def node_yjs_set_pinned_widgets(
    webspace_id: str,
    payload: WebspacePinnedWidgetsRequest,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    svc.set_pinned_widgets_with_live_room(list(payload.pinnedWidgets or []), target_webspace_id)
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.patch("/yjs/webspaces/{webspace_id}/desktop", dependencies=[Depends(require_token)])
async def node_yjs_update_desktop(
    webspace_id: str,
    payload: WebspaceDesktopUpdateRequest,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    current = await svc.get_snapshot_async(target_webspace_id)
    next_snapshot = WebDesktopSnapshot(
        installed=current.installed,
        pinned_widgets=current.pinned_widgets,
        topbar=current.topbar,
        page_schema=current.page_schema,
        icon_order=current.icon_order,
        widget_order=current.widget_order,
    )
    if payload.installed is not None:
        installed = payload.installed if isinstance(payload.installed, dict) else {}
        next_snapshot.installed = WebDesktopInstalled(
            apps=list(installed.get("apps") or []),
            widgets=list(installed.get("widgets") or []),
        )
    if payload.pinnedWidgets is not None:
        next_snapshot.pinned_widgets = list(payload.pinnedWidgets or [])
    if payload.topbar is not None:
        next_snapshot.topbar = list(payload.topbar or [])
    if payload.pageSchema is not None:
        next_snapshot.page_schema = dict(payload.pageSchema or {})
    if payload.iconOrder is not None:
        next_snapshot.icon_order = [str(item or "").strip() for item in payload.iconOrder if str(item or "").strip()]
    if payload.widgetOrder is not None:
        next_snapshot.widget_order = [str(item or "").strip() for item in payload.widgetOrder if str(item or "").strip()]
    if payload.hiddenSections is not None:
        next_snapshot.hidden_sections = [str(item or "").strip() for item in payload.hiddenSections if str(item or "").strip()]
    svc.set_snapshot_with_live_room(next_snapshot, target_webspace_id)
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.post("/yjs/webspaces/{webspace_id}/scenario", dependencies=[Depends(require_token)])
async def node_yjs_switch_scenario(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "scenario_id_required",
        }
    requested_wait_for_rebuild = bool(payload.wait_for_rebuild) if payload.wait_for_rebuild is not None else False
    effective_wait_for_rebuild = False
    result = await switch_webspace_scenario(
        target_webspace_id,
        scenario_id,
        set_home=payload.set_home,
        wait_for_rebuild=effective_wait_for_rebuild,
    )
    result = _attach_wait_for_rebuild_guard(
        result,
        requested=requested_wait_for_rebuild,
        effective=effective_wait_for_rebuild,
        reason="scenario_switch_rebuild_runs_in_background_to_protect_route_budget",
    )
    if bool(payload.include_runtime) or bool(payload.include_rebuild):
        result = _attach_runtime_and_rebuild(
            result,
            role=conf.role,
            webspace_id=target_webspace_id,
            include_rebuild=bool(payload.include_rebuild),
        )
    _publish_yjs_control_event(
        action="scenario",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=scenario_id,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/go-home", dependencies=[Depends(require_token)])
async def node_yjs_go_home(
    webspace_id: str,
    payload: WebspaceYjsActionRequest | None = None,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    requested_wait_for_rebuild = bool(payload.wait_for_rebuild) if payload and payload.wait_for_rebuild is not None else False
    effective_wait_for_rebuild = False
    result = await go_home_webspace(
        target_webspace_id,
        wait_for_rebuild=effective_wait_for_rebuild,
    )
    result = _attach_wait_for_rebuild_guard(
        result,
        requested=requested_wait_for_rebuild,
        effective=effective_wait_for_rebuild,
        reason="go_home_rebuild_runs_in_background_to_protect_route_budget",
    )
    if payload and (bool(payload.include_runtime) or bool(payload.include_rebuild)):
        result = _attach_runtime_and_rebuild(
            result,
            role=conf.role,
            webspace_id=target_webspace_id,
            include_rebuild=bool(payload.include_rebuild),
        )
    _publish_yjs_control_event(
        action="go_home",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=str(result.get("scenario_id") or result.get("home_scenario") or "").strip() or None,
    )
    return result


@router.post("/yjs/dev-webspaces/ensure", dependencies=[Depends(require_token)])
async def node_yjs_ensure_dev(payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_id_required",
        }
    result = await ensure_dev_webspace_for_scenario(
        scenario_id,
        requested_id=str(payload.requested_id or "").strip() or None,
        title=str(payload.title or "").strip() or None,
    )
    target_webspace_id = _coerce_node_webspace_id(result.get("webspace_id"))
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=target_webspace_id,
    )
    _publish_yjs_control_event(
        action="ensure_dev",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=scenario_id,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/set-home", dependencies=[Depends(require_token)])
async def node_yjs_set_home(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "scenario_id_required",
        }
    set_home_kwargs: dict[str, Any] = {}
    if "home_scenario_ref" in getattr(payload, "model_fields_set", set()):
        set_home_kwargs["home_scenario_ref"] = payload.home_scenario_ref
    elif "scenario_ref" in getattr(payload, "model_fields_set", set()):
        set_home_kwargs["home_scenario_ref"] = payload.scenario_ref
    info = await WebspaceService().set_home_scenario(
        target_webspace_id,
        scenario_id,
        **set_home_kwargs,
    )
    result: dict[str, Any]
    if info is None:
        result = {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "scenario_id": scenario_id,
            "error": "webspace_not_found",
        }
    else:
        result = {
            "ok": True,
            "accepted": True,
            "webspace_id": info.id,
            "scenario_id": scenario_id,
            "home_scenario": info.home_scenario,
            "home_scenario_ref": getattr(info, "home_scenario_ref", None),
        }
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=target_webspace_id,
    )
    _publish_yjs_control_event(
        action="set_home",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=scenario_id,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/set-home-current", dependencies=[Depends(require_token)])
async def node_yjs_set_home_current(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    result = await set_current_webspace_home(target_webspace_id)
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=target_webspace_id,
    )
    _publish_yjs_control_event(
        action="set_home_current",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=str(result.get("scenario_id") or result.get("home_scenario") or "").strip() or None,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/reset", dependencies=[Depends(require_token)])
async def node_yjs_reset(webspace_id: str, payload: WebspaceYjsActionRequest, request: Request) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    runtime_before = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=target_webspace_id,
    )
    event_payload = _trace_yjs_control_ingress(
        request=request,
        kind="desktop.webspace.reset",
        webspace_id=target_webspace_id,
        scenario_id=str(payload.scenario_id or "").strip() or None,
        recreate_room=True,
    )
    result = await reload_webspace_from_scenario(
        target_webspace_id,
        scenario_id=str(payload.scenario_id or "").strip() or None,
        action="reset",
        event_payload=event_payload,
    )
    result = _attach_runtime_and_rebuild(
        result,
        role=conf.role,
        webspace_id=target_webspace_id,
        include_rebuild=True,
    )
    result = _attach_yjs_action_debug(
        result,
        requested_endpoint="reset",
        recreate_room_requested=True,
        runtime_before=runtime_before,
    )
    _publish_yjs_control_event(
        action="reset",
        webspace_id=target_webspace_id,
        result=result,
        scenario_id=str(payload.scenario_id or "").strip() or None,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/restore", dependencies=[Depends(require_token)])
async def node_yjs_restore(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    result = await restore_webspace_from_snapshot(target_webspace_id)
    result = _attach_runtime_and_rebuild(
        result,
        role=conf.role,
        webspace_id=target_webspace_id,
        include_rebuild=True,
    )
    _publish_yjs_control_event(
        action="restore",
        webspace_id=target_webspace_id,
        result=result,
    )
    return result


@router.get("/media/files", dependencies=[Depends(require_token)])
async def list_media_library() -> dict[str, Any]:
    snapshot = media_snapshot()
    snapshot["proxy_limits"] = {
        "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
        "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    }
    return snapshot


@router.get("/media/runtime", dependencies=[Depends(require_token)])
async def media_runtime() -> dict[str, Any]:
    conf = load_config()
    runtime = media_plane_runtime_snapshot(
        role=str(getattr(conf, "role", "") or ""),
        route_mode=None,
        connected_to_hub=None,
    )
    runtime["ok"] = True
    runtime["proxy_limits"] = {
        "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
        "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    }
    runtime["capabilities"] = media_capabilities()
    runtime["files"] = {
        "items": list_media_files(),
    }
    return runtime


@router.put("/media/files/{filename}", dependencies=[Depends(require_token)])
async def upload_media_file(filename: str, request: Request) -> dict[str, Any]:
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))

    replaced = target.exists()
    tmp_path = target.with_name(f"{target.name}.upload-{os.getpid()}-{id(request)}.part")
    total_bytes = 0
    try:
        with tmp_path.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                handle.write(chunk)
                total_bytes += len(chunk)
        tmp_path.replace(target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "filename": target.name,
        "size_bytes": total_bytes,
        "mime_type": guess_media_type(target.name),
        "replaced": replaced,
    }


@router.delete("/media/files/{filename}", dependencies=[Depends(require_token)])
async def delete_media_file(filename: str) -> dict[str, Any]:
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))
    existed = target.exists()
    if existed:
        target.unlink()
    return {
        "ok": True,
        "filename": target.name,
        "deleted": existed,
        "items": list_media_files(),
    }


@router.get("/media/files/content/{filename}")
async def media_file_content(
    filename: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_adaos_token: str | None = Header(default=None),
):
    await _require_request_token(
        request,
        authorization=authorization,
        x_adaos_token=x_adaos_token,
    )
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="media_file_not_found")
    return FileResponse(
        path=target,
        media_type=guess_media_type(target.name),
        filename=target.name,
    )


@router.get("/members", dependencies=[Depends(require_token)])
async def node_members() -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    reliability = reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        local_ready=is_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
        node_names=list(getattr(conf, "node_names", []) or []),
    )
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    return {
        "ok": True,
        "hub_member_connection_state": (
            runtime.get("hub_member_connection_state")
            if isinstance(runtime.get("hub_member_connection_state"), dict)
            else {}
        ),
    }


@router.post("/members/{node_id}/snapshot/request", dependencies=[Depends(require_token)])
async def request_member_snapshot(node_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "node_id": node_id,
            "error": "hub_role_required",
        }
    from adaos.services.subnet.link_manager import get_hub_link_manager

    return await get_hub_link_manager().request_member_snapshot(node_id, reason="node_api")


@router.post("/members/{node_id}/update", dependencies=[Depends(require_token)])
async def request_member_update(node_id: str, payload: MemberUpdateRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "node_id": node_id,
            "error": "hub_role_required",
        }
    action = "update" if str(payload.action or "").strip().lower() == "start" else str(payload.action or "").strip().lower()
    from adaos.services.subnet.link_manager import get_hub_link_manager

    return await get_hub_link_manager().request_member_update(
        node_id,
        action=action,
        target_rev=str(payload.target_rev or ""),
        target_version=str(payload.target_version or ""),
        countdown_sec=payload.countdown_sec,
        drain_timeout_sec=payload.drain_timeout_sec,
        signal_delay_sec=payload.signal_delay_sec,
        reason=str(payload.reason or "node_api.member_update"),
    )
