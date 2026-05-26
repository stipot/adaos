from __future__ import annotations

import json
import logging
import gc
import os
import threading
import time
import tracemalloc
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Optional

import anyio
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
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
from adaos.services.desktop_status_cards import publish_desktop_status_card
from adaos.services.node_config import set_node_names as save_node_names_config
from adaos.services.reliability import (
    media_plane_runtime_snapshot,
    reliability_snapshot,
    yjs_sync_runtime_snapshot,
)
from adaos.services.projection_demand import (
    delete_client_subscription_record,
    demanded_projection_keys,
    projection_demand_snapshot,
    touch_client_subscription_record,
    write_client_subscription_record,
)
from adaos.services.projection_demand_mapper import (
    browser_surface_lifecycle_contract_snapshot,
    build_browser_projection_demand_record,
)
from adaos.services.projection_runtime_ownership import projection_runtime_ownership_contract_snapshot
from adaos.services.projection_dispatcher import (
    ProjectionRefreshContext,
    ProjectionRefreshResult,
    core_skill_refresh_contract_snapshot,
    dispatch_demanded_projection_refresh,
    projection_dispatcher_memory_contract_snapshot,
    projection_dispatcher_snapshot,
    register_projection_refresh_handler,
)
from adaos.services.projection_diagnostics import projection_operator_diagnostics
from adaos.services.projection_records import (
    browser_projection_record_snapshot,
    get_projection_record,
    projection_record_registry_snapshot,
    write_projection_record,
)
from adaos.services.projection_record_yjs import (
    materialize_projection_records_to_yjs,
    normalize_projection_record_keys,
    projection_records_node_multiplicity_contract_snapshot,
    read_projection_records_yjs_cache,
)
from adaos.services.projection_migration_inventory import (
    projection_migration_acceptance_summary,
    projection_migration_metrics,
    projection_migration_monolith_inventory,
    projection_migration_recommendations,
)
from adaos.services.platform_emitters import platform_emitter_contract_snapshot
from adaos.services.status_card_details import request_status_card_details_refresh
from adaos.services.status_card_registry import (
    ensure_status_card_dispatcher_handler,
    materialize_status_card_projection_records,
    publish_status_card,
    status_card_id_from_projection_key,
    status_card_projection_record,
    status_card_registry_snapshot,
    sweep_status_card_registry,
)
from adaos.services.infrascope_status_cards import (
    normalize_infrascope_status_card_ids,
    publish_infrascope_status_cards,
)
from adaos.services.infrastate_status_cards import publish_infrastate_status_cards
from adaos.services.runtime_status_cards import publish_runtime_status_card
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
from adaos.services.yjs.doc import async_read_ydoc
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.webspace import coerce_webspace_id, default_webspace_id

router = APIRouter()
_log = logging.getLogger("adaos.api.node_api")
INFRASCOPE_STATUS_CARD_WILDCARD_HANDLER = "status-card:infrascope-*"
_RELIABILITY_SUMMARY_METRICS_LOCK = threading.Lock()
_RELIABILITY_SUMMARY_METRICS: dict[str, Any] = {
    "requestTotal": 0,
    "responseBytesTotal": 0,
    "lastResponseBytes": 0,
    "unchangedTotal": 0,
    "notModifiedTotal": 0,
    "statusCodes": {},
    "byMode": {},
    "last": None,
}


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _coerce_node_webspace_id(value: Any = None) -> str:
    return coerce_webspace_id(value, fallback=default_webspace_id())


def _coerce_mapping_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def _read_infrascope_snapshot_for_status_cards(webspace_id: str) -> dict[str, Any] | None:
    try:
        async with async_read_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            snapshot = _coerce_mapping_dict(data_map.get("infrascope"))
    except Exception:
        _log.debug("failed to read infrascope snapshot from yjs webspace=%s", webspace_id, exc_info=True)
        return None
    return snapshot or None


async def _refresh_infrascope_status_cards(
    *,
    webspace_id: str,
    snapshot: Mapping[str, Any] | None = None,
    source: str = "data/infrascope",
    card_ids: list[str] | None = None,
    demanded_only: bool = False,
) -> dict[str, Any]:
    requested_card_ids = normalize_infrascope_status_card_ids(card_ids)
    if demanded_only and requested_card_ids is None:
        requested_card_ids = normalize_infrascope_status_card_ids(
            demanded_projection_keys(webspace_id=webspace_id)
        )
    if demanded_only and not requested_card_ids:
        return {
            "source": "projection-demand",
            "card_total": 0,
            "cards": [],
            "skipped": True,
            "reason": "infrascope_demand_not_found",
            "demanded_only": True,
            "requested_card_ids": [],
        }
    effective_snapshot = _coerce_mapping_dict(snapshot)
    effective_source = source
    if not effective_snapshot:
        effective_source = "data/infrascope"
        effective_snapshot = await _read_infrascope_snapshot_for_status_cards(webspace_id) or {}
    if not effective_snapshot:
        return {
            "source": effective_source,
            "card_total": 0,
            "cards": [],
            "skipped": True,
            "reason": "infrascope_snapshot_not_found",
            "demanded_only": bool(demanded_only),
            "requested_card_ids": requested_card_ids,
        }
    cards = publish_infrascope_status_cards(
        effective_snapshot,
        webspace_id=webspace_id,
        updated_at=time.time(),
        card_ids=requested_card_ids,
    )
    return {
        "source": effective_source,
        "card_total": len(cards),
        "cards": [card.to_dict() for card in cards],
        "skipped": False,
        "demanded_only": bool(demanded_only),
        "requested_card_ids": requested_card_ids,
    }


def _compact_status_card_refresh(refresh: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        "source": str(refresh.get("source") or ""),
        "cardTotal": int(refresh.get("card_total") or 0),
        "skipped": bool(refresh.get("skipped")),
    }
    requested_card_ids = _coerce_list(refresh.get("requested_card_ids"))
    if bool(refresh.get("demanded_only")) or requested_card_ids:
        compact["demandedOnly"] = bool(refresh.get("demanded_only"))
        compact["requestedCardIds"] = requested_card_ids
    reason = str(refresh.get("reason") or "").strip()
    if reason:
        compact["reason"] = reason
    return compact


async def _refresh_infrascope_status_card_projection(
    context: ProjectionRefreshContext,
) -> ProjectionRefreshResult:
    try:
        card_id = status_card_id_from_projection_key(context.projection_key)
    except ValueError:
        card_id = ""
    card_ids = normalize_infrascope_status_card_ids([card_id])
    if not card_ids:
        return ProjectionRefreshResult(
            projection_key=context.projection_key,
            webspace_id=context.webspace_id,
            status="unavailable",
            reason="infrascope_card_not_supported",
        )
    refresh = await _refresh_infrascope_status_cards(
        webspace_id=context.webspace_id,
        card_ids=card_ids,
        demanded_only=True,
    )
    record = status_card_projection_record(
        card_id=card_ids[0],
        webspace_id=context.webspace_id,
        access={"visibility": "operator"},
        now=context.requested_at,
    )
    if record is None:
        return ProjectionRefreshResult(
            projection_key=context.projection_key,
            webspace_id=context.webspace_id,
            status="unavailable",
            reason=str(refresh.get("reason") or "status_card_missing"),
        )
    return ProjectionRefreshResult(
        projection_key=context.projection_key,
        webspace_id=context.webspace_id,
        status=record.status,
        record=record.to_dict(),
        reason=record.meta.lifecycle_reason,
    )


def _ensure_status_card_projection_handlers() -> None:
    ensure_status_card_dispatcher_handler()
    register_projection_refresh_handler(
        INFRASCOPE_STATUS_CARD_WILDCARD_HANDLER,
        _refresh_infrascope_status_card_projection,
    )


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


def _compact_runtime_reliability_payload(payload: dict[str, Any], *, webspace_id: str | None = None) -> dict[str, Any]:
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
    resolved_webspace_id = _coerce_node_webspace_id(
        webspace_id
        or runtime.get("webspace_id")
        or payload.get("webspace_id")
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
        },
        "supervisorRuntime": supervisor_runtime,
        "phase0Communication": _compact_phase0_checkpoint(runtime.get("event_model_phase0_communication")),
    }


def _numeric_version(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _estimated_json_response_bytes(payload: Any) -> int:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        body = str(payload)
    return len(body.encode("utf-8"))


def _record_reliability_summary_metric(
    *,
    mode: str,
    webspace_id: str,
    status_code: int,
    payload: Any | None = None,
    response_bytes: int | None = None,
    unchanged: bool = False,
) -> None:
    mode_key = str(mode or "full").strip().lower() or "full"
    if response_bytes is None:
        response_bytes = 0 if payload is None else _estimated_json_response_bytes(payload)
    response_bytes = max(0, int(response_bytes))
    status_key = str(int(status_code))
    now_ms = int(time.time() * 1000)
    with _RELIABILITY_SUMMARY_METRICS_LOCK:
        _RELIABILITY_SUMMARY_METRICS["requestTotal"] += 1
        _RELIABILITY_SUMMARY_METRICS["responseBytesTotal"] += response_bytes
        _RELIABILITY_SUMMARY_METRICS["lastResponseBytes"] = response_bytes
        _RELIABILITY_SUMMARY_METRICS["statusCodes"][status_key] = (
            int(_RELIABILITY_SUMMARY_METRICS["statusCodes"].get(status_key) or 0) + 1
        )
        if unchanged:
            _RELIABILITY_SUMMARY_METRICS["unchangedTotal"] += 1
        if int(status_code) == 304:
            _RELIABILITY_SUMMARY_METRICS["notModifiedTotal"] += 1

        by_mode = _RELIABILITY_SUMMARY_METRICS["byMode"]
        mode_metrics = by_mode.setdefault(
            mode_key,
            {
                "requestTotal": 0,
                "responseBytesTotal": 0,
                "lastResponseBytes": 0,
                "unchangedTotal": 0,
                "notModifiedTotal": 0,
                "statusCodes": {},
            },
        )
        mode_metrics["requestTotal"] += 1
        mode_metrics["responseBytesTotal"] += response_bytes
        mode_metrics["lastResponseBytes"] = response_bytes
        mode_metrics["statusCodes"][status_key] = int(mode_metrics["statusCodes"].get(status_key) or 0) + 1
        if unchanged:
            mode_metrics["unchangedTotal"] += 1
        if int(status_code) == 304:
            mode_metrics["notModifiedTotal"] += 1

        _RELIABILITY_SUMMARY_METRICS["last"] = {
            "mode": mode_key,
            "webspaceId": webspace_id,
            "statusCode": int(status_code),
            "responseBytes": response_bytes,
            "unchanged": bool(unchanged),
            "at": now_ms,
        }


def _average_response_bytes(metrics: Mapping[str, Any]) -> float:
    request_total = int(metrics.get("requestTotal") or 0)
    if request_total <= 0:
        return 0.0
    return round(float(metrics.get("responseBytesTotal") or 0) / request_total, 3)


def _reliability_summary_mode_snapshot(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "requestTotal": int(metrics.get("requestTotal") or 0),
        "responseBytesTotal": int(metrics.get("responseBytesTotal") or 0),
        "lastResponseBytes": int(metrics.get("lastResponseBytes") or 0),
        "averageResponseBytes": _average_response_bytes(metrics),
        "unchangedTotal": int(metrics.get("unchangedTotal") or 0),
        "notModifiedTotal": int(metrics.get("notModifiedTotal") or 0),
        "statusCodes": dict(metrics.get("statusCodes") or {}),
    }


def _reliability_summary_payload_comparison(by_mode: Mapping[str, Any]) -> dict[str, Any]:
    full = by_mode.get("full")
    thin = by_mode.get("thin")
    if not isinstance(full, Mapping) or not isinstance(thin, Mapping):
        return {
            "available": False,
            "reason": "full_and_thin_samples_required",
        }
    full_average = _average_response_bytes(full)
    thin_average = _average_response_bytes(thin)
    reduction_bytes = max(0.0, full_average - thin_average)
    reduction_ratio = round(reduction_bytes / full_average, 6) if full_average > 0 else 0.0
    return {
        "available": True,
        "fullAverageResponseBytes": full_average,
        "thinAverageResponseBytes": thin_average,
        "estimatedReductionBytes": round(reduction_bytes, 3),
        "estimatedReductionRatio": reduction_ratio,
        "fullLastResponseBytes": int(full.get("lastResponseBytes") or 0),
        "thinLastResponseBytes": int(thin.get("lastResponseBytes") or 0),
    }


def _reliability_summary_metrics_snapshot() -> dict[str, Any]:
    with _RELIABILITY_SUMMARY_METRICS_LOCK:
        by_mode = {
            str(mode): _reliability_summary_mode_snapshot(metrics)
            for mode, metrics in dict(_RELIABILITY_SUMMARY_METRICS["byMode"]).items()
            if isinstance(metrics, Mapping)
        }
        return {
            "requestTotal": int(_RELIABILITY_SUMMARY_METRICS["requestTotal"]),
            "responseBytesTotal": int(_RELIABILITY_SUMMARY_METRICS["responseBytesTotal"]),
            "lastResponseBytes": int(_RELIABILITY_SUMMARY_METRICS["lastResponseBytes"]),
            "averageResponseBytes": _average_response_bytes(_RELIABILITY_SUMMARY_METRICS),
            "unchangedTotal": int(_RELIABILITY_SUMMARY_METRICS["unchangedTotal"]),
            "notModifiedTotal": int(_RELIABILITY_SUMMARY_METRICS["notModifiedTotal"]),
            "statusCodes": dict(_RELIABILITY_SUMMARY_METRICS["statusCodes"]),
            "byMode": by_mode,
            "payloadComparison": _reliability_summary_payload_comparison(by_mode),
            "last": dict(_RELIABILITY_SUMMARY_METRICS["last"] or {})
            if isinstance(_RELIABILITY_SUMMARY_METRICS.get("last"), Mapping)
            else None,
        }


def _reset_reliability_summary_metrics() -> None:
    with _RELIABILITY_SUMMARY_METRICS_LOCK:
        _RELIABILITY_SUMMARY_METRICS["requestTotal"] = 0
        _RELIABILITY_SUMMARY_METRICS["responseBytesTotal"] = 0
        _RELIABILITY_SUMMARY_METRICS["lastResponseBytes"] = 0
        _RELIABILITY_SUMMARY_METRICS["unchangedTotal"] = 0
        _RELIABILITY_SUMMARY_METRICS["notModifiedTotal"] = 0
        _RELIABILITY_SUMMARY_METRICS["statusCodes"] = {}
        _RELIABILITY_SUMMARY_METRICS["byMode"] = {}
        _RELIABILITY_SUMMARY_METRICS["last"] = None


def _reset_reliability_summary_metrics_for_tests() -> None:
    _reset_reliability_summary_metrics()


def _status_card_registry_etag(*, webspace_id: str, registry_version: int) -> str:
    escaped_webspace_id = str(webspace_id).replace("\\", "\\\\").replace('"', '\\"')
    return f'W/"status-card-registry:{escaped_webspace_id}:{int(registry_version)}"'


def _if_none_match_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match or not etag:
        return False

    def _weak_value(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        return candidate

    target = _weak_value(etag)
    for raw_candidate in if_none_match.split(","):
        candidate = raw_candidate.strip()
        if candidate == "*":
            return True
        if _weak_value(candidate) == target:
            return True
    return False


def _thin_reliability_summary(*, webspace_id: str, since_version: int | None = None) -> dict[str, Any]:
    publish_runtime_status_card(
        webspace_id=webspace_id,
        node_id=_local_node_id(),
        lifecycle=runtime_lifecycle_snapshot(),
    )
    registry = status_card_registry_snapshot(webspace_id=webspace_id)
    cards = [
        {
            "id": card.get("id"),
            "kind": card.get("kind"),
            "status": card.get("status"),
            "severity": card.get("severity"),
            "summary": card.get("summary"),
            "version": card.get("version"),
            "changedAt": card.get("changed_at"),
            "updatedAt": card.get("updated_at"),
            "cacheKey": f"status-card:{webspace_id}:{card.get('id')}",
            "detailsRef": card.get("details_ref"),
        }
        for card in registry.get("cards", [])
        if isinstance(card, Mapping)
    ]
    registry_version = int(
        registry.get("registry_version")
        or _coerce_dict(registry.get("stats")).get("registry_version")
        or 0
    )
    etag = _status_card_registry_etag(
        webspace_id=webspace_id,
        registry_version=registry_version,
    )
    unchanged = since_version is not None and registry_version <= int(since_version)
    return {
        "ok": True,
        "mode": "thin",
        "unchanged": bool(unchanged),
        "source": "api.node.reliability.summary.thin",
        "webspaceId": webspace_id,
        "updatedAt": int(time.time() * 1000),
        "maxVersion": registry_version,
        "registryVersion": registry_version,
        "cache": {
            "key": f"status-card-registry:{webspace_id}",
            "version": registry_version,
            "etag": etag,
            "sinceParam": "since_version",
            "modeParam": "mode=thin",
            "unchanged": bool(unchanged),
        },
        "cardTotal": int(registry.get("card_total") or 0),
        "readyTotal": int(registry.get("ready_total") or 0),
        "staleTotal": int(registry.get("stale_total") or 0),
        "stats": registry.get("stats") or {},
        "cards": [] if unchanged else cards,
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


class StatusCardProjectionRecordsMaterializeRequest(BaseModel):
    webspace_id: str | None = None
    card_ids: list[str] | None = None
    demanded_only: bool = False
    write_yjs: bool = False
    now: float | None = None


class ProjectionRecordsYjsMaterializeRequest(BaseModel):
    webspace_id: str | None = None
    projection_keys: list[str] | None = None
    demanded_only: bool = False
    now: float | None = None


class StatusCardPublishRequest(BaseModel):
    id: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    scope: dict[str, Any] | str | None = Field(default_factory=dict)
    status: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    webspace_id: str | None = None
    severity: str | None = None
    ttl_ms: int | None = None
    details_ref: dict[str, Any] | None = None
    incident_id: str | None = None
    updated_at: float | None = None


class InfrascopeStatusCardsRefreshRequest(BaseModel):
    webspace_id: str | None = None
    snapshot: dict[str, Any] | None = None
    card_ids: list[str] | None = None
    demanded_only: bool = False


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
async def node_control_plane_overview_projection(webspace_id: str | None = None) -> dict[str, Any]:
    projection = current_overview_projection(webspace_id=webspace_id)
    return {"ok": True, "projection": projection.to_dict()}


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


@router.get("/reliability", dependencies=[Depends(require_token)])
async def node_reliability() -> dict[str, Any]:
    return await _current_reliability_payload_async()


@router.get("/reliability/summary", dependencies=[Depends(require_token)])
async def node_reliability_summary(
    response: Response,
    webspace_id: str | None = None,
    mode: str | None = None,
    since_version: int | None = None,
    include_infrascope: bool = False,
    infrascope_demanded_only: bool = False,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Any:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    if str(mode or "").strip().lower() == "thin":
        refreshes: dict[str, dict[str, Any]] = {}
        if include_infrascope:
            refresh = await _refresh_infrascope_status_cards(
                webspace_id=target_webspace_id,
                demanded_only=infrascope_demanded_only,
            )
            refreshes["infrascope"] = _compact_status_card_refresh(refresh)
        payload = _thin_reliability_summary(
            webspace_id=target_webspace_id,
            since_version=since_version,
        )
        if refreshes:
            payload["refreshes"] = refreshes
        cache = _coerce_dict(payload.get("cache"))
        response.headers["Cache-Control"] = "no-cache"
        response.headers["ETag"] = str(cache.get("etag") or "")
        response.headers["X-AdaOS-Cache-Key"] = str(cache.get("key") or "")
        response.headers["X-AdaOS-Registry-Version"] = str(cache.get("version") or 0)
        if _if_none_match_matches(if_none_match, response.headers["ETag"]):
            _record_reliability_summary_metric(
                mode="thin",
                webspace_id=target_webspace_id,
                status_code=304,
                response_bytes=0,
                unchanged=True,
            )
            return Response(status_code=304, headers=dict(response.headers))
        _record_reliability_summary_metric(
            mode="thin",
            webspace_id=target_webspace_id,
            status_code=200,
            payload=payload,
            unchanged=bool(payload.get("unchanged")),
        )
        return payload
    reliability = await _current_reliability_payload_async(webspace_id=webspace_id)
    payload = _compact_runtime_reliability_payload(
        reliability,
        webspace_id=target_webspace_id,
    )
    _record_reliability_summary_metric(
        mode="full",
        webspace_id=target_webspace_id,
        status_code=200,
        payload=payload,
        unchanged=False,
    )
    return payload


@router.get("/reliability/summary/telemetry", dependencies=[Depends(require_token)])
async def node_reliability_summary_telemetry() -> dict[str, Any]:
    return {
        "ok": True,
        "source": "api.node.reliability.summary.telemetry",
        "telemetry": _reliability_summary_metrics_snapshot(),
    }


@router.post("/reliability/summary/telemetry/reset", dependencies=[Depends(require_token)])
async def node_reliability_summary_telemetry_reset() -> dict[str, Any]:
    previous = _reliability_summary_metrics_snapshot()
    _reset_reliability_summary_metrics()
    return {
        "ok": True,
        "source": "api.node.reliability.summary.telemetry.reset",
        "previous": previous,
        "telemetry": _reliability_summary_metrics_snapshot(),
    }


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
    status_cards: dict[str, Any] = {"ok": True, "cards": []}
    if isinstance(snapshot, dict):
        try:
            cards = publish_infrastate_status_cards(snapshot, webspace_id=target_webspace_id)
            status_cards = {
                "ok": True,
                "cards": [card.to_dict() for card in cards],
                "card_total": len(cards),
            }
        except Exception as exc:
            _log.warning(
                "node infrastate status-card publish failed webspace=%s",
                target_webspace_id,
                exc_info=True,
            )
            status_cards = {
                "ok": False,
                "cards": [],
                "card_total": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "degraded": bool(snapshot.get("fallback")) if isinstance(snapshot, dict) else False,
        "error": (snapshot.get("errors") or [None])[0] if isinstance(snapshot, dict) else None,
        "snapshot": snapshot,
        "status_cards": status_cards,
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


@router.get("/projection-demand", dependencies=[Depends(require_token)])
async def node_projection_demand_snapshot(
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


@router.get("/projection-demand/contract", dependencies=[Depends(require_token)])
async def node_projection_demand_contract() -> dict[str, Any]:
    return client_subscription_contract_snapshot(now=time.time())


@router.get("/projection-demand/surface-lifecycle-contract", dependencies=[Depends(require_token)])
async def node_projection_demand_surface_lifecycle_contract() -> dict[str, Any]:
    return browser_surface_lifecycle_contract_snapshot(now=time.time())


@router.post("/projection-demand/client", dependencies=[Depends(require_token)])
async def node_projection_demand_write(payload: ClientProjectionDemandRequest) -> dict[str, Any]:
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
                "updated_at": payload.updated_at or time.time(),
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


@router.post("/projection-demand/client/{client_id}/{session_id}/touch", dependencies=[Depends(require_token)])
async def node_projection_demand_touch(
    client_id: str,
    session_id: str,
    payload: ClientProjectionDemandTouchRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    body = payload or ClientProjectionDemandTouchRequest()
    record = touch_client_subscription_record(
        client_id=client_id,
        session_id=session_id,
        webspace_id=target_webspace_id,
        device_id=body.device_id,
        role=body.role,
        updated_at=body.updated_at,
    )
    response = {
        "ok": True,
        "accepted": record is not None,
        "webspace_id": target_webspace_id,
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }
    if record is None:
        response["reason"] = "client_subscription_not_found"
    else:
        response["record"] = record.to_dict()
    return response


@router.post("/projection-demand/browser-state", dependencies=[Depends(require_token)])
async def node_projection_demand_write_browser_state(payload: BrowserProjectionDemandStateRequest) -> dict[str, Any]:
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
    record = write_client_subscription_record(record)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "record": record.to_dict(),
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.delete("/projection-demand/client/{client_id}/{session_id}", dependencies=[Depends(require_token)])
async def node_projection_demand_delete(
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
        "accepted": True,
        "deleted": deleted,
        "webspace_id": target_webspace_id,
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id),
    }


@router.get("/projection-diagnostics", dependencies=[Depends(require_token)])
async def node_projection_diagnostics(
    webspace_id: str | None = None,
    include_runtime: bool = True,
    include_infrascope: bool = False,
    materialize_projection_records: bool = False,
    materialize_yjs_cache: bool = False,
    include_yjs_cache: bool = False,
    include_stale: bool = True,
    stale_after_s: float | None = None,
) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    refreshes: dict[str, Any] = {}
    if include_runtime:
        publish_runtime_status_card(
            webspace_id=target_webspace_id,
            node_id=_local_node_id(),
            lifecycle=runtime_lifecycle_snapshot(),
        )
    if include_infrascope:
        refreshes["infrascope"] = await _refresh_infrascope_status_cards(
            webspace_id=target_webspace_id,
            demanded_only=True,
        )
    if materialize_projection_records:
        refreshes["projection_records"] = materialize_status_card_projection_records(
            webspace_id=target_webspace_id,
            demanded_only=True,
            access={"visibility": "operator"},
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


@router.get("/projection-migration/monolith-inventory", dependencies=[Depends(require_token)])
async def node_projection_migration_monolith_inventory(include_non_browser: bool = False) -> dict[str, Any]:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    return projection_migration_monolith_inventory(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
    )


@router.get("/projection-migration/metrics", dependencies=[Depends(require_token)])
async def node_projection_migration_metrics(
    include_non_browser: bool = False,
    top_limit: int = 5,
) -> dict[str, Any]:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    return projection_migration_metrics(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        top_limit=top_limit,
    )


@router.get("/projection-migration/recommendations", dependencies=[Depends(require_token)])
async def node_projection_migration_recommendations(
    include_non_browser: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    return projection_migration_recommendations(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        limit=limit,
    )


@router.get("/projection-migration/acceptance-summary", dependencies=[Depends(require_token)])
async def node_projection_migration_acceptance_summary(
    include_non_browser: bool = False,
    top_limit: int = 5,
) -> dict[str, Any]:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    return projection_migration_acceptance_summary(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        top_limit=top_limit,
    )


@router.get("/status-cards", dependencies=[Depends(require_token)])
async def node_status_cards_snapshot(
    webspace_id: str | None = None,
    include_runtime: bool = True,
    include_desktop: bool = False,
    include_infrascope: bool = False,
    infrascope_demanded_only: bool = False,
) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    refreshes: dict[str, Any] = {}
    if include_runtime:
        publish_runtime_status_card(
            webspace_id=target_webspace_id,
            node_id=_local_node_id(),
            lifecycle=runtime_lifecycle_snapshot(),
        )
    if include_desktop:
        desktop = await WebDesktopService().get_snapshot_async(target_webspace_id)
        refreshes["desktop"] = publish_desktop_status_card(
            webspace_id=target_webspace_id,
            snapshot=desktop,
        ).to_dict()
    if include_infrascope:
        refreshes["infrascope"] = await _refresh_infrascope_status_cards(
            webspace_id=target_webspace_id,
            demanded_only=infrascope_demanded_only,
        )
    snapshot = status_card_registry_snapshot(webspace_id=target_webspace_id)
    if refreshes:
        snapshot["refreshes"] = refreshes
    return snapshot


@router.get("/projection-platform-emitters", dependencies=[Depends(require_token)])
async def node_projection_platform_emitters() -> dict[str, Any]:
    return platform_emitter_contract_snapshot()


@router.get("/event-envelope-contract", dependencies=[Depends(require_token)])
async def node_event_envelope_contract() -> dict[str, Any]:
    return event_envelope_contract_snapshot(now=time.time())


@router.get("/projection-runtime-ownership", dependencies=[Depends(require_token)])
async def node_projection_runtime_ownership() -> dict[str, Any]:
    return projection_runtime_ownership_contract_snapshot(now=time.time())


@router.post("/status-cards/runtime/refresh", dependencies=[Depends(require_token)])
async def node_status_cards_refresh_runtime(webspace_id: str | None = None) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    card = publish_runtime_status_card(
        webspace_id=target_webspace_id,
        node_id=_local_node_id(),
        lifecycle=runtime_lifecycle_snapshot(),
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "card": card.to_dict(),
        "snapshot": status_card_registry_snapshot(webspace_id=target_webspace_id),
    }


@router.post("/status-cards/desktop/refresh", dependencies=[Depends(require_token)])
async def node_status_cards_refresh_desktop(webspace_id: str | None = None) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    desktop = await WebDesktopService().get_snapshot_async(target_webspace_id)
    card = publish_desktop_status_card(
        webspace_id=target_webspace_id,
        snapshot=desktop,
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "card": card.to_dict(),
        "desktop": desktop.to_dict(),
        "snapshot": status_card_registry_snapshot(webspace_id=target_webspace_id),
    }


@router.post("/status-cards/sweep", dependencies=[Depends(require_token)])
async def node_status_cards_sweep(
    webspace_id: str | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    result = sweep_status_card_registry(
        webspace_id=target_webspace_id,
        now=now,
        dry_run=dry_run,
    )
    result["snapshot"] = status_card_registry_snapshot(webspace_id=target_webspace_id, now=now)
    return result


@router.post("/status-cards", dependencies=[Depends(require_token)])
async def node_status_cards_publish(payload: StatusCardPublishRequest) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(payload.webspace_id)
    try:
        card = publish_status_card(
            id=payload.id,
            owner=payload.owner,
            kind=payload.kind,
            scope=payload.scope if payload.scope is not None else {},
            webspace_id=target_webspace_id,
            status=payload.status,
            summary=payload.summary,
            severity=payload.severity,
            ttl_ms=payload.ttl_ms,
            details_ref=payload.details_ref,
            incident_id=payload.incident_id,
            updated_at=payload.updated_at,
        )
    except ValueError as exc:
        _raise_400(str(exc))
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "card": card.to_dict(),
        "snapshot": status_card_registry_snapshot(webspace_id=target_webspace_id),
    }


@router.post("/status-cards/infrascope/refresh", dependencies=[Depends(require_token)])
async def node_status_cards_refresh_infrascope(
    payload: InfrascopeStatusCardsRefreshRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    request_payload = payload or InfrascopeStatusCardsRefreshRequest()
    target_webspace_id = _coerce_node_webspace_id(request_payload.webspace_id or webspace_id)
    refresh = await _refresh_infrascope_status_cards(
        webspace_id=target_webspace_id,
        snapshot=request_payload.snapshot,
        source="request",
        card_ids=request_payload.card_ids,
        demanded_only=request_payload.demanded_only,
    )
    if refresh["skipped"]:
        raise HTTPException(status_code=404, detail=str(refresh.get("reason") or "infrascope_snapshot_not_found"))
    return {
        "ok": True,
        "accepted": True,
        "source": refresh["source"],
        "webspace_id": target_webspace_id,
        "card_total": refresh["card_total"],
        "demanded_only": refresh.get("demanded_only"),
        "requested_card_ids": refresh.get("requested_card_ids"),
        "cards": refresh["cards"],
        "snapshot": status_card_registry_snapshot(webspace_id=target_webspace_id),
    }


@router.get("/status-cards/{card_id}/projection", dependencies=[Depends(require_token)])
async def node_status_card_projection(card_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    record = status_card_projection_record(
        card_id=card_id,
        webspace_id=target_webspace_id,
        access={"visibility": "operator"},
    )
    if record is None:
        raise HTTPException(status_code=404, detail="status_card_not_found")
    return {
        "ok": True,
        "webspace_id": target_webspace_id,
        "record": record.to_dict(),
    }


@router.post("/status-cards/{card_id}/details/refresh", dependencies=[Depends(require_token)])
async def node_status_card_details_refresh(card_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    try:
        bus = getattr(get_ctx(), "bus", None)
    except Exception:
        bus = None
    result = request_status_card_details_refresh(
        card_id=card_id,
        webspace_id=target_webspace_id,
        bus=bus,
    )
    if result.get("reason") == "status_card_not_found":
        raise HTTPException(status_code=404, detail="status_card_not_found")
    return result


@router.get("/projection-records", dependencies=[Depends(require_token)])
async def node_projection_records_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return projection_record_registry_snapshot(webspace_id=target_webspace_id)


@router.get("/projection-records/browser-cache", dependencies=[Depends(require_token)], response_model=None)
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
    response.headers["Cache-Control"] = "no-cache"
    response.headers["ETag"] = str(payload.get("etag") or "")
    if _if_none_match_matches(if_none_match, response.headers["ETag"]):
        return Response(status_code=304, headers=dict(response.headers))
    return payload


@router.get("/projection-records/item", dependencies=[Depends(require_token)])
async def node_projection_record_item(projection_key: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    record = get_projection_record(webspace_id=target_webspace_id, projection_key=projection_key)
    if record is None:
        raise HTTPException(status_code=404, detail="projection_record_not_found")
    return {
        "ok": True,
        "webspace_id": target_webspace_id,
        "record": record.to_dict(),
    }


@router.get("/projection-records/yjs/cache", dependencies=[Depends(require_token)])
async def node_projection_records_yjs_cache(webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _coerce_node_webspace_id(webspace_id)
    return await read_projection_records_yjs_cache(webspace_id=target_webspace_id)


@router.get("/projection-records/node-multiplicity-contract", dependencies=[Depends(require_token)])
async def node_projection_records_node_multiplicity_contract() -> dict[str, Any]:
    return projection_records_node_multiplicity_contract_snapshot(now=time.time())


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
        access={"visibility": "operator"},
    )
    if request_payload.write_yjs:
        result["yjs"] = await materialize_projection_records_to_yjs(
            webspace_id=target_webspace_id,
            projection_keys=normalize_projection_record_keys(result.get("records") or []),
            demanded_only=False,
            now=request_payload.now,
        )
    return result


@router.post("/projection-records/yjs/materialize", dependencies=[Depends(require_token)])
async def node_projection_records_yjs_materialize(
    payload: ProjectionRecordsYjsMaterializeRequest | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    request_payload = payload or ProjectionRecordsYjsMaterializeRequest()
    target_webspace_id = _coerce_node_webspace_id(request_payload.webspace_id or webspace_id)
    return await materialize_projection_records_to_yjs(
        webspace_id=target_webspace_id,
        projection_keys=request_payload.projection_keys,
        demanded_only=request_payload.demanded_only,
        now=request_payload.now,
    )


@router.get("/projection-dispatcher", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_snapshot() -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
    return projection_dispatcher_snapshot()


@router.get("/projection-dispatcher/core-skill-contract", dependencies=[Depends(require_token)])
async def node_projection_dispatcher_core_skill_contract(
    webspace_id: str | None = None,
    projection_keys: list[str] | None = Query(default=None),
    include_hidden: bool = True,
    include_stale: bool = True,
) -> dict[str, Any]:
    _ensure_status_card_projection_handlers()
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
    _ensure_status_card_projection_handlers()
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
    status_card = publish_desktop_status_card(
        webspace_id=target_webspace_id,
        snapshot=desktop,
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "type": str(payload.type),
        "id": str(payload.id),
        "installed": installed.to_dict(),
        "desktop": desktop.to_dict(),
        "status_card": status_card.to_dict(),
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
    status_card = publish_desktop_status_card(
        webspace_id=target_webspace_id,
        snapshot=desktop,
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "status_card": status_card.to_dict(),
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
    status_card = publish_desktop_status_card(
        webspace_id=target_webspace_id,
        snapshot=desktop,
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "status_card": status_card.to_dict(),
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
    status_card = publish_desktop_status_card(
        webspace_id=target_webspace_id,
        snapshot=desktop,
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "status_card": status_card.to_dict(),
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
