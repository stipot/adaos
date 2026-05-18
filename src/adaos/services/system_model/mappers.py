from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from adaos.services.registry.subnet_runtime_projection import (
    subnet_runtime_projection_freshness,
)
from adaos.services.system_model.model import (
    CanonicalActionDescriptor,
    CanonicalGovernance,
    CanonicalKind,
    CanonicalObject,
    CanonicalStatus,
    RelationKind,
    canonical_ref,
    compact_mapping,
    normalize_connectivity_status,
    normalize_installation_status,
    normalize_operational_status,
)


def coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    dict_dump = getattr(value, "dict", None)
    if callable(dict_dump):
        dumped = dict_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    if is_dataclass(value):
        dumped = asdict(value)
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    tuple_dump = getattr(value, "_asdict", None)
    if callable(tuple_dump):
        dumped = tuple_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    obj_dict = getattr(value, "__dict__", None)
    if isinstance(obj_dict, Mapping):
        return {str(key): item for key, item in obj_dict.items() if not str(key).startswith("_")}
    return {}


_BROWSER_TITLE_TOKENS = {
    "chrome": "Chrome",
    "chromium": "Chromium",
    "edge": "Edge",
    "firefox": "Firefox",
    "ios": "iOS",
    "iphone": "iPhone",
    "ipad": "iPad",
    "linux": "Linux",
    "mac": "Mac",
    "macos": "macOS",
    "safari": "Safari",
    "tablet": "Tablet",
    "windows": "Windows",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _title_token(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    folded = token.casefold().replace("_", " ").replace("-", " ")
    return _BROWSER_TITLE_TOKENS.get(folded, " ".join(part.capitalize() for part in folded.split()))


def _browser_session_draft_name(data: Mapping[str, Any]) -> str:
    browser = _title_token(
        data.get("browser_family")
        or data.get("browser_name")
        or data.get("browser")
    )
    os_name = _title_token(data.get("os_name") or data.get("os") or data.get("platform"))
    form_factor = _title_token(data.get("form_factor") or data.get("device_type"))
    if form_factor.casefold() in {"desktop", "computer", "pc"}:
        form_factor = ""
    if browser and os_name and form_factor:
        return f"{browser} on {os_name} {form_factor}"
    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser:
        return f"{browser} browser"
    if os_name:
        return f"Browser on {os_name}"
    return ""


def _browser_session_title(data: Mapping[str, Any], device_id: str) -> str:
    for key in ("display_name", "effective_name", "name", "hostname"):
        value = _text(data.get(key))
        if value:
            return value
    return _browser_session_draft_name(data) or device_id


def _connected_to_subnet_value(data: Mapping[str, Any] | None) -> bool | None:
    payload = data if isinstance(data, Mapping) else {}
    connected = payload.get("connected_to_subnet")
    if isinstance(connected, bool):
        return connected
    legacy = payload.get("connected_to_hub")
    return legacy if isinstance(legacy, bool) else None


def canonical_object_from_node_status(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    node_id = str(data.get("node_id") or "unknown").strip() or "unknown"
    role = str(data.get("role") or "node").strip().lower() or "node"
    subnet_id = str(data.get("subnet_id") or "").strip()
    primary_name = str(data.get("primary_node_name") or "").strip()
    node_names = [str(item or "").strip() for item in list(data.get("node_names") or []) if str(item or "").strip()]
    route_mode = str(data.get("route_mode") or "").strip() or None
    connected_to_subnet = _connected_to_subnet_value(data)
    ready = data.get("ready")
    draining = bool(data.get("draining"))
    node_state = str(data.get("node_state") or "").strip() or None

    status = normalize_operational_status(node_state)
    if ready is True and not draining:
        status = CanonicalStatus.ONLINE
    elif draining:
        status = CanonicalStatus.WARNING
    elif ready is False and connected_to_subnet is False:
        status = CanonicalStatus.OFFLINE
    elif ready is False and status == CanonicalStatus.UNKNOWN:
        status = CanonicalStatus.WARNING

    title = primary_name or (node_names[0] if node_names else node_id)
    relations = {RelationKind.SUBNET.value: [f"subnet:{subnet_id}"]} if subnet_id else {}
    health = compact_mapping(
        {
            "availability": status,
            "connectivity": normalize_connectivity_status(
                connected_to_subnet if connected_to_subnet is not None else route_mode
            ),
            "route_mode": route_mode,
        }
    )
    runtime = compact_mapping(
        {
            "node_state": node_state,
            "draining": draining,
            "connected_to_subnet": connected_to_subnet,
            "connected_to_hub": connected_to_subnet,
        }
    )
    representations = compact_mapping(
        {
            "llm": {
                "role": role,
                "node_names": node_names,
                "route_mode": route_mode,
            }
        }
    )
    summary = f"{role} node" + (f" in subnet {subnet_id}" if subnet_id else "")

    kind = role if role in {"hub", "member"} else CanonicalKind.NODE.value
    return CanonicalObject(
        id=canonical_ref(kind, node_id) or f"{kind}:{node_id}",
        kind=kind,
        title=title,
        summary=summary,
        status=status,
        health=health,
        relations=relations,
        runtime=runtime,
        representations=representations,
    )


def canonical_object_from_supervisor_runtime(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    runtime = coerce_mapping(data.get("runtime_state"))
    update_status = coerce_mapping(data.get("update_status"))
    update_attempt = coerce_mapping(data.get("update_attempt"))
    node_id = str(data.get("node_id") or "local").strip() or "local"
    active_slot = str(
        runtime.get("active_slot")
        or coerce_mapping(runtime.get("active_manifest")).get("slot")
        or update_status.get("target_slot")
        or "--"
    ).strip() or "--"
    runtime_state = str(runtime.get("runtime_state") or "unknown").strip().lower() or "unknown"
    update_state = str(update_status.get("state") or "").strip().lower()
    update_phase = str(update_status.get("phase") or "").strip().lower()
    update_action = str(update_status.get("action") or update_attempt.get("action") or "").strip().lower() or None
    attempt_state = str(update_attempt.get("state") or "").strip().lower()
    previous_slot = str(runtime.get("previous_slot") or "").strip().upper() or None
    runtime_api_ready = bool(runtime.get("runtime_api_ready"))
    managed_alive = bool(runtime.get("managed_alive"))
    desired_running = bool(runtime.get("desired_running")) if "desired_running" in runtime else None
    root_promotion_required = bool(runtime.get("root_promotion_required"))
    candidate_prewarm_state = (
        str(update_status.get("candidate_prewarm_state") or update_attempt.get("candidate_prewarm_state") or "")
        .strip()
        .lower()
        or None
    )
    candidate_prewarm_message = str(
        update_status.get("candidate_prewarm_message") or update_attempt.get("candidate_prewarm_message") or ""
    ).strip() or None
    candidate_prewarm_ready_at = update_status.get("candidate_prewarm_ready_at") or update_attempt.get("candidate_prewarm_ready_at")
    transition_title = (update_action or "transition").replace("_", " ")
    update_task_running = bool(runtime.get("update_task_running"))
    reschedule_allowed = update_state in {"planned", "preparing", "countdown"} or (
        update_task_running and update_phase in {"prepare", "countdown"}
    )
    cancel_allowed = reschedule_allowed or attempt_state == "planned"

    if update_state == "failed":
        status = CanonicalStatus.DEGRADED
    elif attempt_state == "awaiting_root_restart":
        status = CanonicalStatus.WARNING
    elif update_state in {"planned", "preparing", "countdown", "draining", "stopping", "restarting", "applying", "validated"}:
        status = CanonicalStatus.WARNING
    elif update_state == "succeeded" and update_phase == "root_promoted":
        status = CanonicalStatus.WARNING
    elif runtime_api_ready:
        status = CanonicalStatus.ONLINE
    elif managed_alive and (desired_running is not False):
        status = CanonicalStatus.WARNING
    elif desired_running and not managed_alive:
        status = CanonicalStatus.OFFLINE
    else:
        status = normalize_operational_status(runtime_state)
        if status == CanonicalStatus.UNKNOWN:
            status = CanonicalStatus.WARNING if managed_alive else CanonicalStatus.UNKNOWN

    assessment_state = attempt_state or update_state or ("ready" if runtime_api_ready else runtime_state) or "unknown"
    assessment_reason = str(
        update_status.get("message")
        or runtime.get("last_error")
        or runtime_state
        or "local supervisor runtime state"
    ).strip() or "local supervisor runtime state"

    subtitle_bits = [f"slot {active_slot}"]
    if update_action:
        subtitle_bits.append(update_action.replace("_", " "))
    target_version = str(update_status.get("target_version") or "").strip()
    if target_version:
        subtitle_bits.append(target_version)
    transition_mode = str(runtime.get("transition_mode") or "").strip()
    candidate_slot = str(runtime.get("candidate_slot") or "").strip()
    if transition_mode:
        subtitle_bits.append(transition_mode.replace("_", " "))
    if candidate_slot:
        subtitle_bits.append(f"candidate {candidate_slot}")
    if previous_slot:
        subtitle_bits.append(f"previous {previous_slot}")
    if candidate_prewarm_state and candidate_prewarm_state != "skipped":
        subtitle_bits.append(f"prewarm {candidate_prewarm_state.replace('_', ' ')}")

    actions = [
        CanonicalActionDescriptor(
            id="restart_runtime",
            title="restart runtime",
            risk="medium",
            metadata={"api_path": "/api/supervisor/runtime/restart"},
        )
    ]
    if cancel_allowed:
        actions.append(
            CanonicalActionDescriptor(
                id="cancel_transition",
                title=f"cancel {transition_title}",
                risk="medium",
                metadata={"api_path": "/api/supervisor/update/cancel"},
            )
        )
    if reschedule_allowed:
        actions.extend(
            [
                CanonicalActionDescriptor(
                    id="defer_transition_5m",
                    title=f"defer {transition_title} 5 min",
                    risk="low",
                    metadata={"api_path": "/api/supervisor/update/defer", "delay_sec": 300.0},
                ),
                CanonicalActionDescriptor(
                    id="defer_transition_15m",
                    title=f"defer {transition_title} 15 min",
                    risk="low",
                    metadata={"api_path": "/api/supervisor/update/defer", "delay_sec": 900.0},
                ),
            ]
        )
    if previous_slot and update_action != "rollback":
        actions.append(
            CanonicalActionDescriptor(
                id="rollback_runtime",
                title=f"rollback to slot {previous_slot}",
                risk="high",
                metadata={"api_path": "/api/supervisor/update/rollback", "target_slot": previous_slot},
            )
        )
    if root_promotion_required or (update_state == "validated" and update_phase == "root_promotion_pending"):
        actions.append(
            CanonicalActionDescriptor(
                id="promote_root",
                title="promote root",
                risk="high",
                metadata={"api_path": "/api/supervisor/update/promote-root"},
            )
        )

    return CanonicalObject(
        id=f"runtime:node:{node_id}/supervisor",
        kind=CanonicalKind.RUNTIME.value,
        title="Core runtime supervisor",
        summary="Supervisor-managed slot runtime transition state",
        status=status,
        health=compact_mapping(
            {
                "availability": status,
                "connectivity": normalize_connectivity_status(runtime_api_ready),
                "runtime_api_ready": runtime_api_ready,
                "managed_alive": managed_alive,
            }
        ),
        runtime=compact_mapping(
            {
                "scope": "core_runtime",
                "phase": update_phase or runtime_state,
                "active_slot": active_slot,
                "previous_slot": previous_slot,
                "action": update_action,
                "runtime_state": runtime_state,
                "runtime_instance_id": runtime.get("runtime_instance_id"),
                "transition_role": runtime.get("transition_role"),
                "attempt_state": attempt_state or None,
                "desired_running": desired_running,
                "managed_alive": managed_alive,
                "runtime_api_ready": runtime_api_ready,
                "root_promotion_required": root_promotion_required,
                "scheduled_for": update_status.get("scheduled_for"),
                "planned_reason": update_status.get("planned_reason"),
                "min_update_period_sec": update_status.get("min_update_period_sec"),
                "transition_mode": runtime.get("transition_mode"),
                "candidate_slot": runtime.get("candidate_slot"),
                "candidate_runtime_url": runtime.get("candidate_runtime_url"),
                "candidate_runtime_state": runtime.get("candidate_runtime_state"),
                "candidate_runtime_instance_id": runtime.get("candidate_runtime_instance_id"),
                "candidate_transition_role": runtime.get("candidate_transition_role"),
                "candidate_runtime_api_ready": runtime.get("candidate_runtime_api_ready"),
                "candidate_prewarm_state": candidate_prewarm_state,
                "candidate_prewarm_message": candidate_prewarm_message,
                "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                "warm_switch_supported": runtime.get("warm_switch_supported"),
                "warm_switch_allowed": runtime.get("warm_switch_allowed"),
                "warm_switch_reason": runtime.get("warm_switch_reason"),
                "subsequent_transition": bool(
                    update_status.get("subsequent_transition") or update_attempt.get("subsequent_transition")
                ),
                "assessment": {
                    "state": assessment_state,
                    "reason": assessment_reason,
                },
            }
        ),
        actual_state=compact_mapping(
            {
                "supervisor_url": runtime.get("supervisor_url"),
                "runtime_url": runtime.get("runtime_url"),
                "runtime_port": runtime.get("runtime_port"),
                "runtime_instance_id": runtime.get("runtime_instance_id"),
                "action": update_action,
                "previous_slot": previous_slot,
                "candidate_runtime_url": runtime.get("candidate_runtime_url"),
                "candidate_runtime_port": runtime.get("candidate_runtime_port"),
                "candidate_runtime_state": runtime.get("candidate_runtime_state"),
                "candidate_runtime_instance_id": runtime.get("candidate_runtime_instance_id"),
                "candidate_prewarm_state": candidate_prewarm_state,
                "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                "expected_managed_cwd": runtime.get("expected_managed_cwd"),
                "managed_matches_active_slot": runtime.get("managed_matches_active_slot"),
                "candidate_matches_candidate_slot": runtime.get("candidate_matches_candidate_slot"),
                "target_rev": update_status.get("target_rev"),
                "target_version": update_status.get("target_version"),
                "subsequent_transition_requested_at": (
                    update_attempt.get("subsequent_transition_requested_at")
                    or update_status.get("subsequent_transition_requested_at")
                ),
            }
        ),
        relations=compact_mapping(
            {
                RelationKind.HOSTED_ON.value: [canonical_ref(CanonicalKind.NODE, node_id) or f"node:{node_id}"],
            }
        ),
        actions=actions,
        representations=compact_mapping(
            {
                "operator": {
                    "title": "Core runtime supervisor",
                    "subtitle": " | ".join(subtitle_bits),
                    "update_action": update_action,
                    "update_state": update_state or None,
                    "update_phase": update_phase or None,
                    "candidate_prewarm_state": candidate_prewarm_state,
                }
            }
        ),
    )


def canonical_object_from_skill_status(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("name") or data.get("id") or "unknown").strip() or "unknown"
    slot = str(data.get("slot") or data.get("active_slot") or "").strip() or None
    update_available = bool(data.get("update_available"))
    local_version = str(data.get("version") or "").strip() or None
    remote_version = str(data.get("remote_version") or "").strip() or None

    status = CanonicalStatus.WARNING if update_available else CanonicalStatus.ONLINE if slot else CanonicalStatus.UNKNOWN
    runtime = compact_mapping(
        {
            "slot": slot,
            "installation_status": normalize_installation_status("active" if slot else "pending_update" if update_available else "installed"),
        }
    )
    versioning = compact_mapping(
        {
            "actual": local_version,
            "desired": remote_version,
            "drift": bool(update_available),
        }
    )

    return CanonicalObject(
        id=canonical_ref(CanonicalKind.SKILL, name) or f"skill:{name}",
        kind=CanonicalKind.SKILL.value,
        title=name,
        summary="Skill catalog object",
        status=status,
        runtime=runtime,
        versioning=versioning,
    )


def canonical_object_from_scenario_item(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("name") or data.get("id") or "unknown").strip() or "unknown"
    version = str(data.get("version") or "").strip() or None
    path = str(data.get("path") or "").strip() or None
    runtime = compact_mapping({"installation_status": normalize_installation_status("installed")})
    actual_state = compact_mapping({"path": path})
    versioning = compact_mapping({"actual": version})
    return CanonicalObject(
        id=canonical_ref(CanonicalKind.SCENARIO, name) or f"scenario:{name}",
        kind=CanonicalKind.SCENARIO.value,
        title=name,
        summary="Scenario catalog object",
        status=CanonicalStatus.UNKNOWN,
        runtime=runtime,
        actual_state=actual_state,
        versioning=versioning,
    )


def canonical_object_from_browser_session(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    device_id = str(data.get("device_id") or data.get("id") or "unknown").strip() or "unknown"
    title = _browser_session_title(data, device_id)
    webspace_id = str(data.get("webspace_id") or "").strip() or None
    connection_state = str(data.get("connection_state") or "").strip().lower() or None
    events_channel_state = str(data.get("events_channel_state") or "").strip().lower() or None
    yjs_channel_state = str(data.get("yjs_channel_state") or "").strip().lower() or None

    status = normalize_operational_status(connection_state)
    if connection_state in {"connecting", "new"}:
        status = CanonicalStatus.WARNING

    relations = compact_mapping(
        {
            RelationKind.WORKSPACE.value: [canonical_ref(CanonicalKind.WORKSPACE, webspace_id) or f"workspace:{webspace_id}"]
            if webspace_id
            else [],
        }
    )
    health = compact_mapping(
        {
            "connectivity": normalize_connectivity_status(connection_state),
            "events_channel": normalize_connectivity_status(events_channel_state),
            "yjs_channel": normalize_connectivity_status(yjs_channel_state),
        }
    )
    runtime = compact_mapping(
        {
            "connection_state": connection_state,
            "events_channel_state": events_channel_state,
            "yjs_channel_state": yjs_channel_state,
            "incoming_audio_tracks": data.get("incoming_audio_tracks"),
            "incoming_video_tracks": data.get("incoming_video_tracks"),
            "loopback_audio_tracks": data.get("loopback_audio_tracks"),
            "loopback_video_tracks": data.get("loopback_video_tracks"),
            "media_track_total": data.get("media_track_total"),
        }
    )
    summary = f"Browser session {device_id}" + (f" in webspace {webspace_id}" if webspace_id else "")
    return CanonicalObject(
        id=f"browser:{device_id}",
        kind=CanonicalKind.BROWSER_SESSION.value,
        title=title,
        summary=summary,
        status=status,
        health=health,
        relations=relations,
        runtime=runtime,
    )


def canonical_object_from_device_endpoint(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    identity = coerce_mapping(data.get("identity"))
    policy = coerce_mapping(data.get("policy"))
    observation = coerce_mapping(data.get("observation"))
    runtime_state = coerce_mapping(data.get("runtime"))

    if identity or policy or observation or runtime_state:
        device_kind = str(data.get("kind") or data.get("device_kind") or "device").strip().lower() or "device"
        link_id = str(
            identity.get("browser_device_id")
            if device_kind == "browser"
            else data.get("ref") or identity.get("node_id") or identity.get("link_id") or data.get("device_id") or data.get("id") or "unknown"
        ).strip() or "unknown"
        if device_kind == "browser":
            link_id = str(identity.get("browser_device_id") or identity.get("link_id") or data.get("device_id") or data.get("id") or "unknown").strip() or "unknown"
        workspace_ids = [
            str(item or "").strip()
            for item in list(data.get("workspace_ids") or [])
            if str(item or "").strip()
        ]
        last_webspace_id = str(observation.get("last_webspace_id") or "").strip()
        if last_webspace_id and last_webspace_id not in workspace_ids:
            workspace_ids.append(last_webspace_id)
        session_ids = [
            str(item or "").strip()
            for item in list(data.get("session_ids") or [])
            if str(item or "").strip()
        ]
        if not session_ids:
            if device_kind == "browser":
                browser_id = str(identity.get("browser_device_id") or identity.get("link_id") or "").strip()
                if browser_id:
                    session_ids.append(f"browser:{browser_id}")
            elif device_kind == "member":
                node_id = str(identity.get("node_id") or identity.get("link_id") or "").strip()
                if node_id:
                    session_ids.append(f"member:{node_id}")
        online = observation.get("online")
        last_seen = observation.get("last_seen_at")
        display_name = str(policy.get("display_name") or "").strip() or None
        effective_name = str(policy.get("effective_name") or "").strip() or link_id
        managed_state = str(policy.get("managed_state") or "").strip() or None
        connected_to_subnet = _connected_to_subnet_value(runtime_state)
        connectivity = online if isinstance(online, bool) else connected_to_subnet
        status = normalize_operational_status(connectivity)
        if managed_state == "revoked":
            status = CanonicalStatus.OFFLINE
        elif managed_state == "expired" and status == CanonicalStatus.UNKNOWN:
            status = CanonicalStatus.WARNING
        elif connectivity is None:
            status = CanonicalStatus.UNKNOWN if workspace_ids else CanonicalStatus.WARNING

        return CanonicalObject(
            id=canonical_ref(CanonicalKind.DEVICE, link_id) or f"device:{link_id}",
            kind=CanonicalKind.DEVICE.value,
            title=effective_name,
            summary=f"{device_kind} device endpoint",
            status=status,
            health=compact_mapping(
                {
                    "connectivity": normalize_connectivity_status(connectivity),
                    "route_mode": runtime_state.get("route_mode"),
                }
            ),
            relations=compact_mapping(
                {
                    RelationKind.WORKSPACE.value: [canonical_ref(CanonicalKind.WORKSPACE, item) or f"workspace:{item}" for item in workspace_ids],
                    RelationKind.CONNECTED_TO.value: session_ids,
                }
            ),
            runtime=compact_mapping(
                {
                    "device_kind": device_kind,
                    "binding_total": len(workspace_ids),
                    "session_total": len(session_ids),
                    "last_seen": last_seen,
                    "managed_state": managed_state,
                    "access_class": policy.get("access_class"),
                    "lifetime_mode": policy.get("lifetime_mode"),
                    "connection_state": observation.get("connection_state"),
                    "observation_source": observation.get("source"),
                    "route_mode": runtime_state.get("route_mode"),
                    "connected_to_subnet": connected_to_subnet,
                    "connected_to_hub": connected_to_subnet,
                    "runtime_version": runtime_state.get("runtime_version"),
                    "snapshot_state": runtime_state.get("snapshot_state"),
                }
            ),
            actual_state=compact_mapping(
                {
                    "device_ref": data.get("ref"),
                    "workspace_ids": workspace_ids,
                    "session_ids": session_ids,
                    "source": data.get("source"),
                    "display_name": display_name,
                    "hostname": identity.get("hostname"),
                    "node_names": identity.get("node_names"),
                    "base_url": identity.get("base_url"),
                }
            ),
        )

    device_id = str(data.get("device_id") or data.get("id") or "unknown").strip() or "unknown"
    device_kind = str(data.get("device_kind") or "device").strip().lower() or "device"
    workspace_ids = [
        str(item or "").strip()
        for item in list(data.get("workspace_ids") or [])
        if str(item or "").strip()
    ]
    session_ids = [
        str(item or "").strip()
        for item in list(data.get("session_ids") or [])
        if str(item or "").strip()
    ]
    online = data.get("online")
    last_seen = data.get("last_seen")
    status = normalize_operational_status(online)
    if online is None:
        status = CanonicalStatus.UNKNOWN if workspace_ids else CanonicalStatus.WARNING

    return CanonicalObject(
        id=canonical_ref(CanonicalKind.DEVICE, device_id) or f"device:{device_id}",
        kind=CanonicalKind.DEVICE.value,
        title=device_id,
        summary=f"{device_kind} device endpoint",
        status=status,
        health=compact_mapping(
            {
                "connectivity": normalize_connectivity_status(online),
            }
        ),
        relations=compact_mapping(
            {
                RelationKind.WORKSPACE.value: [canonical_ref(CanonicalKind.WORKSPACE, item) or f"workspace:{item}" for item in workspace_ids],
                RelationKind.CONNECTED_TO.value: session_ids,
            }
        ),
        runtime=compact_mapping(
            {
                "device_kind": device_kind,
                "binding_total": len(workspace_ids),
                "session_total": len(session_ids),
                "last_seen": last_seen,
            }
        ),
        actual_state=compact_mapping(
            {
                "workspace_ids": workspace_ids,
                "session_ids": session_ids,
                "source": data.get("source"),
            }
        ),
    )


def canonical_object_from_subnet_directory_node(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    node_id = str(data.get("node_id") or "unknown").strip() or "unknown"
    roles = [
        str(item or "").strip().lower()
        for item in list(data.get("roles") or [])
        if str(item or "").strip()
    ]
    role = next(
        (
            item
            for item in roles
            if item in {CanonicalKind.HUB.value, CanonicalKind.MEMBER.value, CanonicalKind.ROOT.value}
        ),
        CanonicalKind.NODE.value,
    )
    subnet_id = str(data.get("subnet_id") or "").strip() or None
    hostname = str(data.get("hostname") or "").strip() or None
    base_url = str(data.get("base_url") or "").strip() or None
    runtime_projection = (
        data.get("runtime_projection")
        if isinstance(data.get("runtime_projection"), Mapping)
        else {}
    )
    projection_node_state = str(runtime_projection.get("node_state") or "").strip() or None
    node_state = str(data.get("node_state") or projection_node_state or "").strip() or None
    online = data.get("online")
    runtime_projection_freshness = subnet_runtime_projection_freshness(
        runtime_projection,
        online=online if isinstance(online, bool) else None,
    )
    capacity = data.get("capacity") if isinstance(data.get("capacity"), Mapping) else {}
    node_names = [
        str(item or "").strip()
        for item in list(runtime_projection.get("node_names") or [])
        if str(item or "").strip()
    ]
    primary_node_name = str(runtime_projection.get("primary_node_name") or "").strip() or None
    connected_to_subnet = _connected_to_subnet_value(runtime_projection)

    status = normalize_operational_status(node_state)
    if online is True and status in {CanonicalStatus.UNKNOWN, CanonicalStatus.OFFLINE}:
        status = CanonicalStatus.ONLINE if not node_state or str(node_state).strip().lower() == "ready" else status
    elif online is False:
        status = CanonicalStatus.OFFLINE

    io_items = [item for item in list(capacity.get("io") or []) if isinstance(item, Mapping)]
    skill_items = [item for item in list(capacity.get("skills") or []) if isinstance(item, Mapping)]
    scenario_items = [item for item in list(capacity.get("scenarios") or []) if isinstance(item, Mapping)]
    title = primary_node_name or (node_names[0] if node_names else None) or hostname or node_id
    summary = f"{role} subnet node" + (f" in subnet {subnet_id}" if subnet_id else "")
    return CanonicalObject(
        id=canonical_ref(role, node_id) or f"{role}:{node_id}",
        kind=role,
        title=title,
        summary=summary,
        status=status,
        health=compact_mapping(
            {
                "connectivity": normalize_connectivity_status(online),
                "route_mode": str(runtime_projection.get("route_mode") or "").strip() or None,
                "runtime_freshness": str(runtime_projection_freshness.get("state") or "").strip() or None,
            }
        ),
        relations=compact_mapping(
            {
                RelationKind.SUBNET.value: [f"subnet:{subnet_id}"] if subnet_id else [],
            }
        ),
        resources=compact_mapping(
            {
                "io_total": len(io_items),
                "skill_total": len(skill_items),
                "scenario_total": len(scenario_items),
            }
        ),
        runtime=compact_mapping(
            {
                "roles": roles,
                "hostname": hostname,
                "base_url": base_url,
                "last_seen": data.get("last_seen"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "node_state": node_state,
                "node_names": node_names,
                "primary_node_name": primary_node_name,
                "ready": runtime_projection.get("ready"),
                "route_mode": runtime_projection.get("route_mode"),
                "connected_to_subnet": connected_to_subnet,
                "connected_to_hub": connected_to_subnet,
                "runtime_projection_captured_at": runtime_projection.get("captured_at"),
                "runtime_projection_freshness": runtime_projection_freshness,
                "build": runtime_projection.get("build") if isinstance(runtime_projection.get("build"), Mapping) else {},
                "update_status": (
                    runtime_projection.get("update_status")
                    if isinstance(runtime_projection.get("update_status"), Mapping)
                    else {}
                ),
                "last_result": (
                    runtime_projection.get("last_result")
                    if isinstance(runtime_projection.get("last_result"), Mapping)
                    else {}
                ),
            }
        ),
        actual_state=compact_mapping(
            {
                "online": online,
                "capacity": capacity,
                "runtime_projection": runtime_projection,
                "runtime_projection_freshness": runtime_projection_freshness,
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "roles": roles,
                    "hostname": hostname,
                    "base_url": base_url,
                    "node_names": node_names,
                    "primary_node_name": primary_node_name,
                    "node_state": node_state,
                    "ready": runtime_projection.get("ready"),
                    "runtime_projection_freshness": runtime_projection_freshness,
                    "build": runtime_projection.get("build"),
                    "update_status": runtime_projection.get("update_status"),
                }
            }
        ),
    )


def canonical_object_from_capacity_snapshot(
    payload: Any,
    *,
    node_id: str | None = None,
    title: str | None = None,
    summary: str | None = None,
) -> CanonicalObject:
    data = coerce_mapping(payload)
    io_items = [item for item in list(data.get("io") or []) if isinstance(item, Mapping)]
    skill_items = [item for item in list(data.get("skills") or []) if isinstance(item, Mapping)]
    scenario_items = [item for item in list(data.get("scenarios") or []) if isinstance(item, Mapping)]
    active_skill_total = sum(1 for item in skill_items if bool(item.get("active", True)))
    active_scenario_total = sum(1 for item in scenario_items if bool(item.get("active", True)))
    effective_title = title or "Local capacity"
    effective_summary = summary or "Local node capacity and runtime inventory"
    status = CanonicalStatus.ONLINE if io_items or skill_items or scenario_items else CanonicalStatus.UNKNOWN
    relations = compact_mapping(
        {
            RelationKind.HOSTED_ON.value: [canonical_ref(CanonicalKind.NODE, node_id) or f"node:{node_id}"] if node_id else []
        }
    )
    resources = compact_mapping(
        {
            "io_total": len(io_items),
            "skill_total": len(skill_items),
            "scenario_total": len(scenario_items),
            "active_skill_total": active_skill_total,
            "active_scenario_total": active_scenario_total,
        }
    )
    runtime = compact_mapping(
        {
            "io_types": [str(item.get("io_type") or item.get("type") or "").strip() for item in io_items if str(item.get("io_type") or item.get("type") or "").strip()],
            "skills": [str(item.get("name") or "").strip() for item in skill_items if str(item.get("name") or "").strip()],
            "scenarios": [str(item.get("name") or "").strip() for item in scenario_items if str(item.get("name") or "").strip()],
        }
    )
    return CanonicalObject(
        id=canonical_ref(CanonicalKind.CAPACITY, node_id) if node_id else canonical_ref(CanonicalKind.CAPACITY, "local") or "capacity:local",
        kind=CanonicalKind.CAPACITY.value,
        title=effective_title,
        summary=effective_summary,
        status=status,
        relations=relations,
        resources=resources,
        runtime=runtime,
        actual_state=compact_mapping(data),
    )


def canonical_object_from_io_capacity_entry(payload: Any, *, node_id: str | None = None) -> CanonicalObject:
    data = coerce_mapping(payload)
    io_type = str(data.get("io_type") or data.get("type") or "unknown").strip() or "unknown"
    capabilities = [str(item or "").strip() for item in list(data.get("capabilities") or []) if str(item or "").strip()]
    state_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("state:")), "")
    mode_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("mode:")), "")
    reason_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("reason:")), "")
    producer_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("producer:")), "")
    topology_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("topology:")), "")
    status = CanonicalStatus.OFFLINE if state_token == "unavailable" else CanonicalStatus.ONLINE
    if not capabilities and status == CanonicalStatus.ONLINE:
        status = CanonicalStatus.UNKNOWN
    return CanonicalObject(
        id=f"io:{node_id}:{io_type}" if node_id else f"io:{io_type}",
        kind=CanonicalKind.IO_ENDPOINT.value,
        title=io_type,
        summary=f"Local IO endpoint {io_type}",
        status=status,
        relations=compact_mapping(
            {
                RelationKind.HOSTED_ON.value: [canonical_ref(CanonicalKind.NODE, node_id) or f"node:{node_id}"] if node_id else []
            }
        ),
        runtime=compact_mapping(
            {
                "io_type": io_type,
                "priority": data.get("priority"),
                "mode": mode_token or None,
                "reason": reason_token or None,
                "producer": producer_token or None,
                "topology": topology_token or None,
            }
        ),
        actual_state=compact_mapping({"capabilities": capabilities}),
    )


def canonical_object_from_integration_quota(
    payload: Any,
    *,
    node_id: str | None = None,
    root_id: str | None = None,
) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("name") or data.get("id") or "unknown").strip() or "unknown"
    size = int(data.get("size") or 0)
    max_size = data.get("max_size")
    durable_store = bool(data.get("durable_store"))
    connected = data.get("connected")
    publish_fail = int(data.get("publish_fail") or 0)
    publish_ok = int(data.get("publish_ok") or 0)
    last_error = str(data.get("last_error") or "").strip() or None

    status = CanonicalStatus.ONLINE
    if isinstance(max_size, int) and max_size > 0 and size >= max_size:
        status = CanonicalStatus.WARNING
    elif size > 0 and not durable_store:
        status = CanonicalStatus.DEGRADED
    elif publish_fail > 0 and publish_fail >= max(1, publish_ok):
        status = CanonicalStatus.WARNING
    elif connected is False:
        status = CanonicalStatus.WARNING

    reasons: list[str] = []
    if isinstance(max_size, int) and max_size > 0:
        reasons.append(f"{size}/{max_size} buffered")
    elif size > 0:
        reasons.append(f"{size} buffered")
    if last_error:
        reasons.append(f"last error: {last_error}")
    summary = f"{name} integration outbox quota"
    if reasons:
        summary += f" ({'; '.join(reasons)})"

    return CanonicalObject(
        id=canonical_ref(CanonicalKind.QUOTA, f"{name}-outbox") or f"quota:{name}-outbox",
        kind=CanonicalKind.QUOTA.value,
        title=f"{name} outbox quota",
        summary=summary,
        status=status,
        relations=compact_mapping(
            {
                RelationKind.HOSTED_ON.value: [canonical_ref(CanonicalKind.NODE, node_id) or f"node:{node_id}"] if node_id else [],
                RelationKind.CONNECTED_TO.value: [root_id] if root_id else [],
            }
        ),
        resources=compact_mapping(
            {
                "used": size,
                "limit": max_size,
                "persisted": data.get("persisted_size"),
            }
        ),
        runtime=compact_mapping(
            {
                "connected": connected,
                "durable_store": durable_store,
                "idempotency_mode": data.get("idempotency_mode"),
                "publish_ok": publish_ok,
                "publish_fail": publish_fail,
                "dropped_total": data.get("dropped_total"),
                "drained_total": data.get("drained_total"),
                "cache_hit_total": data.get("cache_hit_total"),
                "cache_miss_total": data.get("cache_miss_total"),
                "conflict_total": data.get("conflict_total"),
                "updated_ago_s": data.get("updated_ago_s"),
                "last_error": last_error,
                "last_error_ago_s": data.get("last_error_ago_s"),
            }
        ),
        actual_state=compact_mapping(
            {
                "last_operation_key": data.get("last_operation_key"),
                "persist_path": data.get("persist_path"),
            }
        ),
        desired_state=compact_mapping({"max_size": max_size}),
    )


def canonical_object_from_protocol_traffic_budget(
    payload: Any,
    *,
    node_id: str | None = None,
    root_id: str | None = None,
) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("traffic_class") or data.get("name") or data.get("id") or "integration").strip().lower() or "integration"
    policy = data.get("policy") if isinstance(data.get("policy"), Mapping) else {}
    pending_msgs_limit = int(policy.get("pending_msgs_limit") or 0) if str(policy.get("pending_msgs_limit") or "").strip() else 0
    pending_bytes_limit = int(policy.get("pending_bytes_limit") or 0) if str(policy.get("pending_bytes_limit") or "").strip() else 0
    worker_budget = int(policy.get("worker_budget") or 0) if str(policy.get("worker_budget") or "").strip() else 0
    last_qsize = data.get("last_qsize")
    last_pending_bytes = data.get("last_pending_bytes")
    pressure_events = int(data.get("pressure_events") or 0)
    publish_fail = int(data.get("publish_fail") or 0)
    publish_ok = int(data.get("publish_ok") or 0)
    handler_errors = int(data.get("handler_errors") or 0)
    last_error = str(data.get("last_error") or "").strip() or None

    status = CanonicalStatus.ONLINE
    if pending_msgs_limit > 0 and isinstance(last_qsize, (int, float)) and float(last_qsize) >= pending_msgs_limit:
        status = CanonicalStatus.WARNING
    elif pending_bytes_limit > 0 and isinstance(last_pending_bytes, (int, float)) and float(last_pending_bytes) >= pending_bytes_limit:
        status = CanonicalStatus.WARNING
    elif handler_errors > 0 or pressure_events > 0:
        status = CanonicalStatus.DEGRADED
    elif publish_fail > max(0, publish_ok):
        status = CanonicalStatus.WARNING

    reasons: list[str] = []
    if pending_msgs_limit > 0 and isinstance(last_qsize, (int, float)):
        reasons.append(f"queue {int(last_qsize)}/{pending_msgs_limit}")
    if pending_bytes_limit > 0 and isinstance(last_pending_bytes, (int, float)):
        reasons.append(f"bytes {int(last_pending_bytes)}/{pending_bytes_limit}")
    if worker_budget > 0:
        reasons.append(f"workers {worker_budget}")
    if last_error:
        reasons.append(f"last error: {last_error}")
    summary = f"{name} traffic budget"
    if reasons:
        summary += f" ({'; '.join(reasons)})"

    return CanonicalObject(
        id=canonical_ref(CanonicalKind.QUOTA, f"hub-protocol-{name}") or f"quota:hub-protocol-{name}",
        kind=CanonicalKind.QUOTA.value,
        title=f"{name} traffic budget",
        summary=summary,
        status=status,
        relations=compact_mapping(
            {
                RelationKind.HOSTED_ON.value: [canonical_ref(CanonicalKind.NODE, node_id) or f"node:{node_id}"] if node_id else [],
                RelationKind.CONNECTED_TO.value: [root_id] if root_id else [],
            }
        ),
        resources=compact_mapping(
            {
                "queue_used": last_qsize,
                "queue_limit": pending_msgs_limit or None,
                "pending_bytes": last_pending_bytes,
                "pending_bytes_limit": pending_bytes_limit or None,
                "worker_budget": worker_budget or None,
            }
        ),
        runtime=compact_mapping(
            {
                "active_subscriptions": data.get("active_subscriptions"),
                "subjects": data.get("subjects"),
                "dispatch_count": data.get("dispatch_count"),
                "publish_ok": publish_ok,
                "publish_fail": publish_fail,
                "handler_errors": handler_errors,
                "pressure_events": pressure_events,
                "max_qsize": data.get("max_qsize"),
                "max_pending_bytes": data.get("max_pending_bytes"),
                "last_message_bytes": data.get("last_message_bytes"),
                "last_error": last_error,
                "last_dispatch_ago_s": data.get("last_dispatch_ago_s"),
                "last_publish_ago_s": data.get("last_publish_ago_s"),
                "last_error_ago_s": data.get("last_error_ago_s"),
            }
        ),
        desired_state=compact_mapping(policy),
    )


def canonical_object_from_user_profile(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    user_id = str(data.get("user_id") or "unknown").strip() or "unknown"
    settings = data.get("settings") if isinstance(data.get("settings"), Mapping) else {}
    title = str(settings.get("preferred_name") or settings.get("full_name") or user_id).strip() or user_id
    representations = compact_mapping({"user": settings})
    governance = CanonicalGovernance(owner_id=f"profile:{user_id}")
    return CanonicalObject(
        id=canonical_ref(CanonicalKind.PROFILE, user_id) or f"profile:{user_id}",
        kind=CanonicalKind.PROFILE.value,
        title=title,
        summary="User profile object",
        status=CanonicalStatus.UNKNOWN,
        governance=governance,
        representations=representations,
        actual_state=compact_mapping({"settings": settings}),
    )


def canonical_object_from_workspace_manifest(payload: Any) -> CanonicalObject:
    workspace_id = str(getattr(payload, "workspace_id", "") or "").strip() or str(
        coerce_mapping(payload).get("workspace_id") or "default"
    ).strip()
    title = str(getattr(payload, "title", "") or "").strip() or workspace_id
    effective_kind = str(getattr(payload, "effective_kind", "") or "").strip() or "workspace"
    effective_home_scenario = str(getattr(payload, "effective_home_scenario", "") or "").strip() or None
    effective_source_mode = str(getattr(payload, "effective_source_mode", "") or "").strip() or None
    owner_scope = str(getattr(payload, "owner_scope", "") or "").strip() or None
    profile_scope = str(getattr(payload, "profile_scope", "") or "").strip() or None
    device_binding = str(getattr(payload, "device_binding", "") or "").strip() or None
    has_ui_overlay = bool(getattr(payload, "has_ui_overlay", False))
    has_installed_overlay = bool(getattr(payload, "has_installed_overlay", False))
    has_pinned_widgets_overlay = bool(getattr(payload, "has_pinned_widgets_overlay", False))

    relations = compact_mapping(
        {
            RelationKind.HOME_SCENARIO.value: [canonical_ref(CanonicalKind.SCENARIO, effective_home_scenario) or f"scenario:{effective_home_scenario}"]
            if effective_home_scenario
            else [],
            RelationKind.DEVICE_BINDING.value: [canonical_ref(CanonicalKind.DEVICE, device_binding) or f"device:{device_binding}"]
            if device_binding
            else [],
        }
    )
    governance = CanonicalGovernance(
        owner_id=owner_scope,
        visibility=[profile_scope] if profile_scope else [],
        metadata=compact_mapping({"workspace_kind": effective_kind}),
    )
    actual_state = compact_mapping({"source_mode": effective_source_mode, "device_binding": device_binding})
    runtime = compact_mapping(
        {
            "overlay": {
                "has_ui_overlay": has_ui_overlay,
                "has_installed_overlay": has_installed_overlay,
                "has_pinned_widgets_overlay": has_pinned_widgets_overlay,
            }
        }
    )

    return CanonicalObject(
        id=canonical_ref(CanonicalKind.WORKSPACE, workspace_id) or f"workspace:{workspace_id}",
        kind=CanonicalKind.WORKSPACE.value,
        title=title,
        summary=f"{effective_kind} workspace",
        status=CanonicalStatus.ONLINE if effective_home_scenario else CanonicalStatus.UNKNOWN,
        relations=relations,
        runtime=runtime,
        governance=governance,
        actual_state=actual_state,
    )


__all__ = [
    "canonical_object_from_node_status",
    "canonical_object_from_supervisor_runtime",
    "canonical_object_from_browser_session",
    "canonical_object_from_capacity_snapshot",
    "canonical_object_from_device_endpoint",
    "canonical_object_from_integration_quota",
    "canonical_object_from_protocol_traffic_budget",
    "canonical_object_from_io_capacity_entry",
    "canonical_object_from_scenario_item",
    "canonical_object_from_skill_status",
    "canonical_object_from_subnet_directory_node",
    "canonical_object_from_user_profile",
    "canonical_object_from_workspace_manifest",
    "coerce_mapping",
]
