from __future__ import annotations

import time
import sys
from types import SimpleNamespace
import types

from pydantic import BaseModel

from adaos.services.system_model import (
    CANONICAL_KIND_REGISTRY,
    CANONICAL_RELATION_REGISTRY,
    CanonicalActionDescriptor,
    CanonicalKind,
    CanonicalObject,
    RelationKind,
    apply_governance_defaults,
    canonical_ref,
    CanonicalStatus,
    canonical_object_projection,
    canonical_object_inspector,
    canonical_inventory_projection,
    canonical_neighborhood_projection,
    canonical_overview_projection,
    canonical_task_packet,
    canonical_topology_projection,
    canonical_object_from_browser_session,
    canonical_object_from_capacity_snapshot,
    canonical_object_from_device_endpoint,
    canonical_object_from_integration_quota,
    canonical_object_from_protocol_traffic_budget,
    canonical_object_from_io_capacity_entry,
    canonical_object_from_node_status,
    canonical_object_from_supervisor_runtime,
    canonical_projection_from_reliability_snapshot,
    canonical_object_from_skill_status,
    canonical_object_from_subnet_directory_node,
    canonical_object_from_user_profile,
    canonical_object_from_workspace_manifest,
    normalize_kind,
    normalize_relation_kind,
    normalize_connectivity_status,
    normalize_operational_status,
)


class _NodeStatusModel(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    node_names: list[str]
    primary_node_name: str
    ready: bool
    node_state: str
    draining: bool
    route_mode: str | None = None
    connected_to_hub: bool | None = None


def test_normalize_operational_status_maps_common_runtime_terms() -> None:
    assert normalize_operational_status("ready") == CanonicalStatus.ONLINE
    assert normalize_operational_status("down") == CanonicalStatus.OFFLINE
    assert normalize_operational_status("degraded") == CanonicalStatus.DEGRADED
    assert normalize_operational_status("draining") == CanonicalStatus.WARNING
    assert normalize_operational_status("not_applicable") == CanonicalStatus.UNKNOWN


def test_kind_and_relation_registry_normalize_and_build_refs() -> None:
    assert CANONICAL_KIND_REGISTRY["device"] == CanonicalKind.DEVICE
    assert CANONICAL_RELATION_REGISTRY["hosted_on"] == RelationKind.HOSTED_ON
    assert normalize_kind("BROWSER_SESSION") == "browser_session"
    assert normalize_relation_kind(RelationKind.WORKSPACE) == "workspace"
    assert canonical_ref(CanonicalKind.QUOTA, "telegram-outbox") == "quota:telegram-outbox"


def test_normalize_connectivity_status_handles_bool_and_transport_tokens() -> None:
    assert normalize_connectivity_status(True).value == "reachable"
    assert normalize_connectivity_status(False).value == "unreachable"
    assert normalize_connectivity_status("ws").value == "reachable"
    assert normalize_connectivity_status("open").value == "reachable"
    assert normalize_connectivity_status("none").value == "unreachable"


def test_canonical_object_from_node_status_accepts_pydantic_payload() -> None:
    payload = _NodeStatusModel(
        node_id="member-1",
        subnet_id="subnet-a",
        role="member",
        node_names=["Kitchen member"],
        primary_node_name="Kitchen member",
        ready=True,
        node_state="ready",
        draining=False,
        route_mode="ws",
        connected_to_hub=True,
    )

    obj = canonical_object_from_node_status(payload).to_dict()

    assert obj["id"] == "member:member-1"
    assert obj["kind"] == "member"
    assert obj["status"] == "online"
    assert obj["health"]["availability"] == "online"
    assert obj["relations"]["subnet"] == ["subnet:subnet-a"]
    assert obj["health"]["connectivity"] == "reachable"


def test_canonical_object_from_node_status_accepts_connected_to_subnet_alias() -> None:
    obj = canonical_object_from_node_status(
        {
            "node_id": "member-9",
            "subnet_id": "subnet-a",
            "role": "member",
            "primary_node_name": "Kitchen member",
            "ready": False,
            "node_state": "ready",
            "draining": False,
            "route_mode": "p2p",
            "connected_to_subnet": False,
        }
    ).to_dict()

    assert obj["status"] == "offline"
    assert obj["runtime"]["connected_to_subnet"] is False
    assert obj["runtime"]["connected_to_hub"] is False


def test_canonical_object_from_node_status_marks_draining_as_warning() -> None:
    obj = canonical_object_from_node_status(
        {
            "node_id": "hub-1",
            "subnet_id": "main",
            "role": "hub",
            "primary_node_name": "Hub Alpha",
            "ready": False,
            "node_state": "ready",
            "draining": True,
            "route_mode": "hub",
        }
    ).to_dict()

    assert obj["id"] == "hub:hub-1"
    assert obj["status"] == "warning"
    assert obj["runtime"]["draining"] is True


def test_canonical_object_from_skill_status_tracks_slot_and_version_drift() -> None:
    obj = canonical_object_from_skill_status(
        {
            "name": "weather_skill",
            "version": "1.0.0",
            "slot": "slot-a",
            "remote_version": "1.1.0",
            "update_available": True,
        }
    ).to_dict()

    assert obj["id"] == "skill:weather_skill"
    assert obj["status"] == "warning"
    assert obj["runtime"]["installation_status"] == "active"
    assert obj["versioning"]["drift"] is True


def test_canonical_object_from_workspace_manifest_uses_effective_properties() -> None:
    manifest = SimpleNamespace(
        workspace_id="desk",
        title="DEV: desk",
        effective_kind="dev",
        effective_home_scenario="web_desktop",
        effective_source_mode="dev",
        owner_scope="profile:ops",
        profile_scope="role:developer",
    )

    obj = canonical_object_from_workspace_manifest(manifest).to_dict()

    assert obj["id"] == "workspace:desk"
    assert obj["title"] == "DEV: desk"
    assert obj["relations"]["home_scenario"] == ["scenario:web_desktop"]
    assert obj["governance"]["owner_id"] == "profile:ops"


def test_canonical_object_from_workspace_manifest_includes_binding_and_overlay_state() -> None:
    manifest = SimpleNamespace(
        workspace_id="kitchen",
        title="Kitchen",
        effective_kind="workspace",
        effective_home_scenario="home",
        effective_source_mode="workspace",
        owner_scope="profile:owner",
        profile_scope="role:operator",
        device_binding="tablet-kitchen",
        has_ui_overlay=True,
        has_installed_overlay=True,
        has_pinned_widgets_overlay=False,
    )

    obj = canonical_object_from_workspace_manifest(manifest).to_dict()

    assert obj["relations"]["device_binding"] == ["device:tablet-kitchen"]
    assert obj["runtime"]["overlay"]["has_ui_overlay"] is True
    assert obj["actual_state"]["device_binding"] == "tablet-kitchen"


def test_canonical_object_from_user_profile_uses_preferred_name() -> None:
    obj = canonical_object_from_user_profile(
        SimpleNamespace(user_id="u-1", settings={"preferred_name": "Ada", "locale": "ru-RU"})
    ).to_dict()

    assert obj["id"] == "profile:u-1"
    assert obj["title"] == "Ada"
    assert obj["actual_state"]["settings"]["locale"] == "ru-RU"


def test_canonical_object_from_browser_session_tracks_workspace_and_channels() -> None:
    obj = canonical_object_from_browser_session(
        {
            "device_id": "browser-a",
            "webspace_id": "desk",
            "connection_state": "connected",
            "events_channel_state": "open",
            "yjs_channel_state": "open",
            "incoming_video_tracks": 1,
        }
    ).to_dict()

    assert obj["id"] == "browser:browser-a"
    assert obj["kind"] == "browser_session"
    assert obj["relations"]["workspace"] == ["workspace:desk"]
    assert obj["health"]["connectivity"] == "reachable"
    assert obj["runtime"]["incoming_video_tracks"] == 1


def test_canonical_object_from_browser_session_uses_metadata_draft_title() -> None:
    obj = canonical_object_from_browser_session(
        {
            "device_id": "dev_68e58ce1-2e6b-4615-9b0d-0e8cb46eccbb",
            "webspace_id": "desk",
            "connection_state": "connected",
            "browser_family": "Chrome",
            "os_name": "Windows",
            "form_factor": "Desktop",
        }
    ).to_dict()

    assert obj["id"] == "browser:dev_68e58ce1-2e6b-4615-9b0d-0e8cb46eccbb"
    assert obj["title"] == "Chrome on Windows"


def test_browser_session_catalog_unions_yws_and_webrtc_snapshots(monkeypatch) -> None:
    sys.modules.setdefault("nats", SimpleNamespace())
    sys.modules.setdefault(
        "y_py",
        SimpleNamespace(
            YDoc=type("YDoc", (), {}),
            apply_update=lambda *args, **kwargs: None,
            encode_state_as_update=lambda *args, **kwargs: b"",
            encode_state_vector=lambda *args, **kwargs: b"",
        ),
    )
    if "ypy_websocket.ystore" not in sys.modules:
        ystore_module = types.ModuleType("ypy_websocket.ystore")
        ystore_module.BaseYStore = type("BaseYStore", (), {})
        ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
        sys.modules["ypy_websocket.ystore"] = ystore_module
    if "ypy_websocket" not in sys.modules:
        pkg = types.ModuleType("ypy_websocket")
        pkg.ystore = sys.modules["ypy_websocket.ystore"]
        sys.modules["ypy_websocket"] = pkg
    from adaos.services.system_model import catalog

    monkeypatch.setattr(catalog, "_governed", lambda obj: obj)

    yws_mod = SimpleNamespace(
        active_browser_session_snapshot=lambda: {
            "peers": [
                {
                    "device_id": "browser-yws",
                    "webspace_id": "desk",
                    "connection_state": "connected",
                    "yjs_channel_state": "open",
                },
                {
                    "device_id": "browser-both",
                    "webspace_id": "desk",
                    "connection_state": "connected",
                    "yjs_channel_state": "open",
                },
            ]
        }
    )
    webrtc_mod = SimpleNamespace(
        webrtc_peer_snapshot=lambda: {
            "peers": [
                {
                    "device_id": "browser-both",
                    "webspace_id": "desk",
                    "connection_state": "connected",
                    "events_channel_state": "open",
                    "yjs_channel_state": "open",
                    "incoming_video_tracks": 2,
                }
            ]
        }
    )
    access_links_mod = SimpleNamespace(
        browser_snapshot=lambda: [
            {
                "id": "browser-yws",
                "display_name": "",
                "browser_family": "Chrome",
                "os_name": "Windows",
                "form_factor": "Desktop",
            },
            {
                "id": "browser-both",
                "display_name": "Dev Browser",
                "browser_family": "Firefox",
                "os_name": "Linux",
            },
            {
                "id": "offline-browser",
                "display_name": "Offline Browser",
                "browser_family": "Safari",
                "os_name": "macOS",
            },
        ]
    )
    monkeypatch.setitem(sys.modules, "adaos.services.yjs.gateway_ws", yws_mod)
    monkeypatch.setitem(sys.modules, "adaos.services.webrtc.peer", webrtc_mod)
    monkeypatch.setitem(sys.modules, "adaos.services.access_links", access_links_mod)

    objects = [item.to_dict() for item in catalog.browser_session_objects()]

    assert [item["id"] for item in objects] == ["browser:browser-both", "browser:browser-yws"]
    assert [item["title"] for item in objects] == ["Dev Browser", "Chrome on Windows"]
    assert objects[0]["runtime"]["incoming_video_tracks"] == 2
    assert objects[1]["health"]["yjs_channel"] == "reachable"


def test_device_catalog_uses_device_inventory_records_and_preserves_workspace_bindings(monkeypatch) -> None:
    sys.modules.setdefault("nats", SimpleNamespace())
    sys.modules.setdefault(
        "y_py",
        SimpleNamespace(
            YDoc=type("YDoc", (), {}),
            apply_update=lambda *args, **kwargs: None,
            encode_state_as_update=lambda *args, **kwargs: b"",
            encode_state_vector=lambda *args, **kwargs: b"",
        ),
    )
    if "ypy_websocket.ystore" not in sys.modules:
        ystore_module = types.ModuleType("ypy_websocket.ystore")
        ystore_module.BaseYStore = type("BaseYStore", (), {})
        ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
        sys.modules["ypy_websocket.ystore"] = ystore_module
    if "ypy_websocket" not in sys.modules:
        pkg = types.ModuleType("ypy_websocket")
        pkg.ystore = sys.modules["ypy_websocket.ystore"]
        sys.modules["ypy_websocket"] = pkg
    from adaos.services.system_model import catalog

    monkeypatch.setattr(catalog, "_governed", lambda obj: obj)
    monkeypatch.setattr(
        catalog,
        "list_device_inventory_records",
        lambda: [
            {
                "ref": "browser:tablet-kitchen",
                "kind": "browser",
                "identity": {
                    "link_id": "tablet-kitchen",
                    "browser_device_id": "tablet-kitchen",
                    "node_id": None,
                    "hostname": "tablet-host",
                    "node_names": [],
                    "base_url": None,
                },
                "policy": {
                    "present": True,
                    "managed_state": "managed",
                    "display_name": "Kitchen tablet",
                    "effective_name": "Kitchen tablet",
                    "access_class": "device",
                    "lifetime_mode": "permanent",
                    "revoked": False,
                },
                "observation": {
                    "online": True,
                    "connection_state": "connected",
                    "last_seen_at": 321.0,
                    "source": "browser_session",
                    "last_webspace_id": "desk",
                },
                "runtime": {
                    "connected_to_subnet": None,
                    "route_mode": None,
                    "runtime_version": None,
                    "snapshot_state": None,
                },
            },
            {
                "ref": "member:member-2",
                "kind": "member",
                "identity": {
                    "link_id": "member-2",
                    "browser_device_id": None,
                    "node_id": "member-2",
                    "hostname": "member-host",
                    "node_names": ["Kitchen East"],
                    "base_url": "http://member-2.local",
                },
                "policy": {
                    "present": False,
                    "managed_state": "observed_only",
                    "display_name": None,
                    "effective_name": "Kitchen East",
                    "access_class": "device",
                    "lifetime_mode": "permanent",
                    "revoked": False,
                },
                "observation": {
                    "online": True,
                    "connection_state": "connected",
                    "last_seen_at": 654.0,
                    "source": "member_link",
                    "last_webspace_id": None,
                },
                "runtime": {
                    "connected_to_subnet": True,
                    "route_mode": "p2p",
                    "runtime_version": "1.2.3",
                    "snapshot_state": "fresh",
                },
            },
        ],
    )
    monkeypatch.setattr(
        catalog.workspace_index,
        "list_workspaces",
        lambda: [
            SimpleNamespace(workspace_id="desk", device_binding="tablet-kitchen"),
            SimpleNamespace(workspace_id="kitchen", device_binding="tablet-kitchen"),
        ],
    )

    objects = [item.to_dict() for item in catalog.device_objects()]

    assert [item["id"] for item in objects] == ["device:member:member-2", "device:tablet-kitchen"]
    member_obj, browser_obj = objects
    assert member_obj["runtime"]["connected_to_subnet"] is True
    assert member_obj["relations"]["connected_to"] == ["member:member-2"]
    assert browser_obj["title"] == "Kitchen tablet"
    assert browser_obj["relations"]["workspace"] == ["workspace:desk", "workspace:kitchen"]
    assert browser_obj["relations"]["connected_to"] == ["browser:tablet-kitchen"]


def test_canonical_object_from_device_endpoint_merges_workspace_and_session_links() -> None:
    obj = canonical_object_from_device_endpoint(
        {
            "device_id": "tablet-kitchen",
            "device_kind": "browser",
            "workspace_ids": ["kitchen"],
            "session_ids": ["browser:tablet-kitchen"],
            "online": True,
            "source": "merged",
        }
    ).to_dict()

    assert obj["id"] == "device:tablet-kitchen"
    assert obj["kind"] == "device"
    assert obj["relations"]["workspace"] == ["workspace:kitchen"]
    assert obj["relations"]["connected_to"] == ["browser:tablet-kitchen"]
    assert obj["health"]["connectivity"] == "reachable"


def test_canonical_object_from_device_endpoint_accepts_device_record_shape() -> None:
    obj = canonical_object_from_device_endpoint(
        {
            "ref": "member:member-2",
            "kind": "member",
            "identity": {
                "link_id": "member-2",
                "node_id": "member-2",
                "hostname": "kitchen-member",
                "node_names": ["Kitchen East"],
                "base_url": "http://member-2.local",
            },
            "policy": {
                "present": True,
                "managed_state": "managed",
                "display_name": "Kitchen tablet",
                "effective_name": "Kitchen tablet",
                "access_class": "device",
                "lifetime_mode": "permanent",
                "revoked": False,
            },
            "observation": {
                "online": True,
                "connection_state": "connected",
                "last_seen_at": 123.0,
                "source": "member_link",
            },
            "runtime": {
                "route_mode": "p2p",
                "connected_to_subnet": True,
                "runtime_version": "1.2.3",
                "snapshot_state": "fresh",
            },
            "workspace_ids": [],
            "session_ids": ["member:member-2"],
            "source": "device_inventory",
        }
    ).to_dict()

    assert obj["id"] == "device:member:member-2"
    assert obj["title"] == "Kitchen tablet"
    assert obj["relations"]["connected_to"] == ["member:member-2"]
    assert obj["runtime"]["connected_to_subnet"] is True
    assert obj["runtime"]["connected_to_hub"] is True
    assert obj["runtime"]["route_mode"] == "p2p"
    assert obj["actual_state"]["device_ref"] == "member:member-2"


def test_canonical_object_from_subnet_directory_node_maps_capacity_and_presence() -> None:
    now = time.time()
    obj = canonical_object_from_subnet_directory_node(
        {
            "node_id": "member-2",
            "subnet_id": "main",
            "roles": ["member"],
            "hostname": "Kitchen Member",
            "base_url": "http://member-2.local",
            "node_state": "ready",
            "online": True,
            "capacity": {
                "io": [{"io_type": "say"}],
                "skills": [{"name": "weather"}],
                "scenarios": [{"name": "home"}],
            },
            "runtime_projection": {
                "captured_at": now - 5.0,
                "primary_node_name": "Kitchen East",
                "node_names": ["Kitchen East"],
                "ready": True,
                "route_mode": "ws",
                "connected_to_subnet": True,
                "build": {"runtime_version": "0.2.0"},
                "update_status": {"state": "succeeded"},
            },
        }
    ).to_dict()

    assert obj["id"] == "member:member-2"
    assert obj["kind"] == "member"
    assert obj["title"] == "Kitchen East"
    assert obj["resources"]["io_total"] == 1
    assert obj["runtime"]["hostname"] == "Kitchen Member"
    assert obj["runtime"]["node_names"] == ["Kitchen East"]
    assert obj["runtime"]["build"]["runtime_version"] == "0.2.0"
    assert obj["health"]["route_mode"] == "ws"
    assert obj["health"]["runtime_freshness"] == "fresh"
    assert obj["runtime"]["runtime_projection_freshness"]["state"] == "fresh"
    assert obj["runtime"]["connected_to_subnet"] is True
    assert obj["runtime"]["connected_to_hub"] is True
    assert obj["health"]["connectivity"] == "reachable"


def test_canonical_object_from_capacity_snapshot_summarizes_local_inventory() -> None:
    obj = canonical_object_from_capacity_snapshot(
        {
            "io": [{"io_type": "stdout"}, {"io_type": "say"}],
            "skills": [{"name": "weather", "active": True}, {"name": "music", "active": False}],
            "scenarios": [{"name": "home", "active": True}],
        },
        node_id="node-7",
    ).to_dict()

    assert obj["id"] == "capacity:node-7"
    assert obj["resources"]["io_total"] == 2
    assert obj["resources"]["active_skill_total"] == 1
    assert obj["runtime"]["skills"] == ["weather", "music"]


def test_canonical_object_from_io_capacity_entry_tracks_availability_tokens() -> None:
    obj = canonical_object_from_io_capacity_entry(
        {
            "io_type": "telegram",
            "priority": 60,
            "capabilities": ["text", "state:unavailable", "mode:webhook", "reason:token_missing"],
        },
        node_id="node-7",
    ).to_dict()

    assert obj["id"] == "io:node-7:telegram"
    assert obj["status"] == "offline"
    assert obj["runtime"]["mode"] == "webhook"
    assert obj["runtime"]["reason"] == "token_missing"


def test_canonical_object_from_io_capacity_entry_tracks_media_route_tokens() -> None:
    obj = canonical_object_from_io_capacity_entry(
        {
            "io_type": "webrtc_media",
            "priority": 60,
            "capabilities": [
                "webrtc:av",
                "producer:member",
                "topology:member_browser_direct",
                "state:available",
            ],
        },
        node_id="member-2",
    ).to_dict()

    assert obj["id"] == "io:member-2:webrtc_media"
    assert obj["status"] == "online"
    assert obj["runtime"]["producer"] == "member"
    assert obj["runtime"]["topology"] == "member_browser_direct"


def test_canonical_object_from_integration_quota_exposes_usage_and_pressure() -> None:
    obj = canonical_object_from_integration_quota(
        {
            "name": "telegram",
            "size": 12,
            "max_size": 10,
            "durable_store": False,
            "publish_fail": 2,
            "publish_ok": 1,
            "last_error": "network timeout",
            "updated_ago_s": 3.1,
        },
        node_id="hub-1",
        root_id="root:eu",
    ).to_dict()

    assert obj["id"] == "quota:telegram-outbox"
    assert obj["kind"] == "quota"
    assert obj["status"] == "warning"
    assert obj["relations"]["connected_to"] == ["root:eu"]
    assert obj["resources"]["used"] == 12


def test_canonical_object_from_protocol_traffic_budget_exposes_limits_and_pressure() -> None:
    obj = canonical_object_from_protocol_traffic_budget(
        {
            "traffic_class": "control",
            "policy": {
                "pending_msgs_limit": 4,
                "pending_bytes_limit": 4096,
                "worker_budget": 1,
            },
            "last_qsize": 4,
            "last_pending_bytes": 1024,
            "publish_fail": 0,
            "publish_ok": 3,
        },
        node_id="hub-1",
        root_id="root:eu",
    ).to_dict()

    assert obj["id"] == "quota:hub-protocol-control"
    assert obj["status"] == "warning"
    assert obj["resources"]["queue_limit"] == 4
    assert obj["relations"]["connected_to"] == ["root:eu"]


def test_apply_governance_defaults_merges_visibility_roles_and_action_defaults() -> None:
    obj = CanonicalObject(
        id="runtime:hub:alpha/sidecar",
        kind="runtime",
        title="Realtime sidecar",
        actions=[CanonicalActionDescriptor(id="restart_sidecar", title="restart sidecar")],
    )

    governed = apply_governance_defaults(
        obj,
        tenant_id="subnet:main",
        owner_id="profile:owner",
    ).to_dict()

    assert governed["governance"]["tenant_id"] == "subnet:main"
    assert governed["governance"]["owner_id"] == "profile:owner"
    assert "role:infra-operator" in governed["governance"]["roles_allowed"]
    assert governed["actions"][0]["requires_role"] == "role:infra-operator"


def test_canonical_projection_from_reliability_snapshot_builds_runtime_components() -> None:
    projection = canonical_projection_from_reliability_snapshot(
        {
            "node": {
                "node_id": "hub-1",
                "subnet_id": "main",
                "role": "hub",
                "node_names": ["Hub Alpha"],
                "ready": True,
                "node_state": "ready",
                "draining": False,
                "route_mode": "hub",
            },
            "runtime": {
                "readiness_tree": {
                    "hub_local_core": {"status": "ready"},
                    "root_control": {"status": "ready", "summary": "root control is healthy"},
                    "route": {"status": "ready", "summary": "route tunnels are healthy"},
                    "sync": {"status": "ready"},
                    "media": {"status": "unknown"},
                },
                "degraded_matrix": {
                    "root_routed_browser_proxy": {"allowed": False},
                    "execute_local_scenarios": {"allowed": True},
                },
                "channel_diagnostics": {
                    "root_control": {"stability": {"state": "stable", "score": 98}, "recent_transitions_5m": 1},
                    "route": {"stability": {"state": "stable", "score": 93}, "recent_transitions_5m": 1},
                },
                "hub_root_zone": {
                    "configured_zone_id": "eu",
                    "active_zone_id": "eu",
                    "selected_server": "wss://api.inimatic.com/nats",
                },
                "event_model_phase0_communication": {
                    "state": "in_progress",
                    "ready": False,
                    "tracked_tasks": [
                        "phase0.node_browser_ready",
                        "phase0.runtime_comm_ready",
                    ],
                    "completed_task_total": 0,
                    "task_total": 2,
                    "remaining_tasks": [
                        "phase0.node_browser_ready",
                        "phase0.runtime_comm_ready",
                    ],
                    "tasks": {
                        "phase0.node_browser_ready": {
                            "id": "phase0.node_browser_ready",
                            "status": "in_progress",
                            "summary": "browser/member communication prerequisites are not fully ready yet",
                            "completed_criteria": ["browser_member_semantic_channels"],
                            "pending_criteria": ["browser_yjs_ws_handoff"],
                            "pending_reasons": [
                                "Yjs websocket/session ownership still lives in the runtime gateway",
                            ],
                            "evidence": {
                                "browser_yjs_ws_handoff": {
                                    "state": "planned",
                                    "owner": "runtime",
                                    "planned_owner": "sidecar",
                                    "blocker": "Yjs websocket/session ownership still lives in the runtime gateway",
                                },
                            },
                        },
                        "phase0.runtime_comm_ready": {
                            "id": "phase0.runtime_comm_ready",
                            "status": "in_progress",
                            "summary": "runtime communication prerequisites are still open",
                            "completed_criteria": ["hub_root_class_a_hardening"],
                            "pending_criteria": [
                                "browser_events_ws_handoff",
                                "browser_yjs_ws_handoff",
                                "sidecar_continuity",
                                "browser_safe_supervisor_continuity",
                            ],
                            "pending_reasons": [
                                "browser route websocket still terminates in the runtime FastAPI app",
                                "Yjs websocket/session ownership still lives in the runtime gateway",
                                "sidecar.continuity.planned",
                                "supervisor.browser_safe_continuity.in_progress",
                            ],
                            "evidence": {
                                "hub_root_class_a": {
                                    "state": "complete",
                                    "covered_flows": 6,
                                    "total_flows": 6,
                                },
                                "browser_events_ws_handoff": {
                                    "state": "planned",
                                    "owner": "runtime",
                                    "planned_owner": "sidecar",
                                },
                                "browser_yjs_ws_handoff": {
                                    "state": "planned",
                                    "owner": "runtime",
                                    "planned_owner": "sidecar",
                                },
                                "sidecar_continuity": {
                                    "state": "planned",
                                    "hub_runtime_update": "preserve_sidecar",
                                },
                                "browser_safe_supervisor_continuity": {
                                    "state": "in_progress",
                                    "routed_browser_proxy": {
                                        "state": "ready",
                                        "source": "supervisor_public_status",
                                        "selected_ws_base": "ws://127.0.0.1:8777",
                                    },
                                },
                            },
                        },
                    },
                },
                "supervisor_runtime": {
                    "available": True,
                    "supervisor_url": "http://127.0.0.1:8776",
                    "status": {
                        "action": "update",
                        "state": "countdown",
                        "phase": "scheduled",
                    },
                    "attempt": {
                        "action": "update",
                        "state": "planned",
                    },
                    "runtime": {
                        "runtime_state": "ready",
                        "runtime_api_ready": True,
                        "managed_alive": True,
                        "active_slot": "A",
                        "runtime_instance_id": "rt-a-1",
                        "transition_role": "active",
                        "transition_mode": "warm_switch",
                        "candidate_slot": "B",
                        "candidate_runtime_url": "http://127.0.0.1:8778",
                        "candidate_runtime_state": "ready",
                        "candidate_runtime_instance_id": "rt-b-1",
                        "candidate_transition_role": "candidate",
                        "candidate_runtime_api_ready": True,
                        "warm_switch_supported": True,
                        "warm_switch_allowed": True,
                        "warm_switch_reason": "warm switch admitted",
                    },
                },
                "sidecar_runtime": {
                    "enabled": True,
                    "status": "ready",
                    "summary": "sidecar remote session is connected",
                    "phase": "nats_transport_sidecar",
                    "transport_owner": "sidecar",
                    "lifecycle_manager": "supervisor",
                    "local_listener_state": "ready",
                    "remote_session_state": "ready",
                    "transport_ready": True,
                    "control_ready": "ready",
                    "route_ready": "not_owned",
                    "sync_ready": "not_owned",
                    "media_ready": "not_owned",
                    "process": {"pid": 41},
                    "continuity_contract": {
                        "required": True,
                        "member_runtime_update": "defer",
                        "hub_runtime_update": "preserve_sidecar",
                        "current_support": "planned",
                    },
                    "progress": {
                        "target": "first_browser_realtime_tunnel",
                        "state": "in_progress",
                        "completed_milestones": 2,
                        "milestone_total": 4,
                        "current_milestone": "browser_events_ws_handoff",
                    },
                    "route_tunnel_contract": {
                        "current_support": "planned",
                        "ownership_boundary": "transport_only",
                        "ws": {
                            "current_owner": "runtime",
                            "planned_owner": "sidecar",
                            "delegation_mode": "not_implemented",
                        },
                        "yws": {
                            "current_owner": "runtime",
                            "planned_owner": "sidecar",
                            "delegation_mode": "not_implemented",
                        },
                    },
                    "transport_provenance": {"selected_server": "wss://api.inimatic.com/nats"},
                },
                "sync_runtime": {
                    "available": True,
                    "scope": "hub_local_only",
                    "selected_webspace_id": "desk",
                    "assessment": {"state": "nominal", "reason": "bounded sync runtime observed"},
                    "channel_contract": {
                        "channel_type": "sync_channel",
                        "recovery_model": "snapshot_plus_diff",
                        "replay_window": "bounded",
                        "awareness_semantics": "ephemeral",
                        "browser_local_persistence": "optional_indexeddb",
                        "completed_for_scope": True,
                    },
                    "transport": {"server_ready": True},
                    "ownership_boundaries": {
                        "state": "explicit",
                        "selector": {
                            "owner": "shared",
                            "current_scenario": "web_desktop",
                            "home_scenario": "web_desktop",
                        },
                        "effective_projection": {
                            "owner": "runtime",
                            "ready": True,
                            "readiness_state": "ready",
                        },
                        "compatibility_caches": {
                            "owner": "runtime",
                            "mode": "fallback_cache",
                        },
                        "transport_session": {
                            "owner": "runtime",
                            "planned_owner": "sidecar",
                        },
                    },
                    "action_overrides": {
                        "backup": {"enabled": True, "reason": "persist current state", "source_of_truth": "current_runtime"},
                        "reset": {"enabled": True, "reason": "hard reset", "source_of_truth": "scenario"},
                    },
                    "recovery_guidance": {"recommended_action": "backup"},
                    "recovery_playbook": {"default_action": "reload"},
                    "webspace_guidance": {"recommended_action": "go_home"},
                    "selected_webspace": {"webspace_id": "desk", "home_scenario": "web_desktop"},
                    "webspace_total": 2,
                    "active_webspace_total": 1,
                    "compacted_webspace_total": 1,
                    "update_log_total": 12,
                    "replay_window_total": 4,
                },
                "media_runtime": {
                    "available": True,
                    "scope": "hub_media",
                    "assessment": {
                        "state": "bounded_relay_available",
                        "reason": "media plane supports direct-local authority and bounded root relay authority",
                    },
                    "transport": {"direct_local_ready": True, "root_routed_ready": True, "broadcast_ready": False},
                    "counts": {"file_total": 2, "total_bytes": 1024, "live_peer_total": 0, "live_connected_peers": 0},
                    "recommended_path": "direct_local_http",
                    "update_guard": {
                        "member_runtime_update": "allow",
                        "hub_runtime_update": "allow",
                        "current_support": "not_applicable",
                    },
                },
                "hub_root_protocol": {
                    "traffic_classes": {
                        "control": {
                            "traffic_class": "control",
                            "policy": {"pending_msgs_limit": 4, "pending_bytes_limit": 4096, "worker_budget": 1},
                            "last_qsize": 1,
                            "last_pending_bytes": 512,
                            "publish_ok": 3,
                            "publish_fail": 0,
                        }
                    },
                    "integration_outboxes": {
                        "telegram": {"name": "telegram", "size": 1, "max_size": 10, "durable_store": True},
                        "llm": {"name": "llm", "size": 0, "max_size": 5, "durable_store": True},
                    }
                },
            },
        }
    ).to_dict()

    assert projection["id"] == "projection:hub:hub-1/reliability"
    assert projection["subject"]["health"]["root_control"] == "online"
    assert projection["subject"]["health"]["route"] == "online"
    assert projection["subject"]["health"]["sync"] == "online"
    assert projection["context"]["blocked_capabilities"] == ["root_routed_browser_proxy"]
    assert projection["context"]["event_model_phase0_communication"]["state"] == "in_progress"
    assert (
        projection["subject"]["runtime"]["event_model_phase0_communication"]["tasks"]["phase0.runtime_comm_ready"]["status"]
        == "in_progress"
    )
    assert (
        projection["subject"]["runtime"]["event_model_phase0_communication"]["tasks"]["phase0.runtime_comm_ready"]["evidence"]["browser_safe_supervisor_continuity"]["routed_browser_proxy"]["state"]
        == "ready"
    )

    objects = {item["id"]: item for item in projection["objects"]}
    assert objects["root:eu"]["kind"] == "root"
    assert objects["quota:hub-protocol-control"]["kind"] == "quota"
    assert objects["quota:telegram-outbox"]["kind"] == "quota"
    assert objects["connection:hub:hub-1/root-control"]["kind"] == "connection"
    assert objects["runtime:hub:hub-1/sidecar"]["health"]["availability"] == "online"
    assert objects["runtime:hub:hub-1/sidecar"]["runtime"]["transport_owner"] == "sidecar"
    assert objects["runtime:hub:hub-1/sidecar"]["actual_state"]["continuity_contract"]["hub_runtime_update"] == "preserve_sidecar"
    assert objects["runtime:hub:hub-1/sidecar"]["actual_state"]["progress"]["target"] == "first_browser_realtime_tunnel"
    assert (
        objects["runtime:hub:hub-1/sidecar"]["actual_state"]["route_tunnel_contract"]["ws"]["planned_owner"] == "sidecar"
    )
    assert objects["runtime:hub:hub-1/yjs-sync"]["health"]["availability"] == "online"
    assert objects["runtime:hub:hub-1/yjs-sync"]["relations"]["workspace"] == ["workspace:desk"]
    assert objects["runtime:hub:hub-1/yjs-sync"]["runtime"]["channel_contract"]["completed_for_scope"] is True
    assert objects["runtime:hub:hub-1/yjs-sync"]["actual_state"]["channel_contract"]["recovery_model"] == "snapshot_plus_diff"
    assert objects["runtime:hub:hub-1/yjs-sync"]["actual_state"]["ownership_boundaries"]["state"] == "explicit"
    assert objects["runtime:hub:hub-1/yjs-sync"]["runtime"]["ownership_boundaries"]["transport_session"]["planned_owner"] == "sidecar"
    assert objects["runtime:hub:hub-1/media-plane"]["resources"]["file_total"] == 2
    assert objects["runtime:hub:hub-1/media-plane"]["runtime"]["update_guard"]["current_support"] == "not_applicable"
    assert objects["runtime:node:hub-1/supervisor"]["kind"] == "runtime"
    assert objects["runtime:node:hub-1/supervisor"]["runtime"]["transition_mode"] == "warm_switch"
    assert objects["runtime:node:hub-1/supervisor"]["runtime"]["candidate_runtime_state"] == "ready"
    assert objects["runtime:node:hub-1/supervisor"]["actual_state"]["supervisor_url"] == "http://127.0.0.1:8776"


def test_canonical_projection_from_reliability_snapshot_maps_actions_and_incidents() -> None:
    projection = canonical_projection_from_reliability_snapshot(
        {
            "node": {
                "node_id": "hub-2",
                "subnet_id": "main",
                "role": "hub",
                "ready": True,
                "node_state": "ready",
                "draining": False,
            },
            "runtime": {
                "readiness_tree": {
                    "root_control": {"status": "degraded", "summary": "root control is unstable"},
                    "route": {"status": "down", "summary": "route is unavailable"},
                },
                "degraded_matrix": {"root_routed_browser_proxy": {"allowed": False}},
                "channel_diagnostics": {
                    "root_control": {"stability": {"state": "flapping", "score": 62}},
                    "route": {"stability": {"state": "down", "score": 15}},
                },
                "hub_root_zone": {},
                "sidecar_runtime": {
                    "enabled": True,
                    "status": "degraded",
                    "summary": "sidecar diagnostics are stale",
                    "remote_session_state": "stale",
                    "control_ready": "degraded",
                },
                "sync_runtime": {
                    "available": True,
                    "selected_webspace_id": "default",
                    "assessment": {"state": "pressure", "reason": "bounded replay window near limit"},
                    "transport": {"server_ready": False},
                    "action_overrides": {"restore": {"enabled": False, "reason": "snapshot missing"}},
                },
                "media_runtime": {
                    "available": False,
                    "assessment": {"state": "unavailable", "reason": "media runtime module is unavailable"},
                    "transport": {"direct_local_ready": False},
                },
                "hub_root_protocol": {
                    "traffic_classes": {
                        "integration": {
                            "traffic_class": "integration",
                            "policy": {"pending_msgs_limit": 2, "pending_bytes_limit": 2048, "worker_budget": 1},
                            "last_qsize": 2,
                            "last_pending_bytes": 2048,
                            "pressure_events": 1,
                            "publish_fail": 1,
                            "publish_ok": 0,
                        }
                    },
                    "integration_outboxes": {
                        "telegram": {"name": "telegram", "size": 5, "durable_store": False, "publish_fail": 1, "publish_ok": 0}
                    }
                },
            },
        }
    ).to_dict()

    objects = {item["id"]: item for item in projection["objects"]}
    sync_actions = {item["id"]: item for item in objects["runtime:hub:hub-2/yjs-sync"]["actions"]}

    assert sync_actions["restore"]["risk"] == "medium"
    assert objects["connection:hub:hub-2/route"]["status"] == "offline"
    assert objects["quota:hub-protocol-integration"]["status"] in {"warning", "degraded"}
    assert objects["quota:telegram-outbox"]["status"] in {"warning", "degraded"}
    assert objects["runtime:hub:hub-2/sidecar"]["health"]["availability"] == "degraded"
    assert objects["runtime:hub:hub-2/yjs-sync"]["health"]["availability"] == "degraded"
    assert any(item["object_id"] == "connection:hub:hub-2/route" for item in projection["incidents"])
    assert any(item["object_id"] == "runtime:hub:hub-2/sidecar" for item in projection["incidents"])


def test_canonical_inventory_projection_counts_objects_and_incidents() -> None:
    subject = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status=CanonicalStatus.ONLINE)
    objects = [
        CanonicalObject(id="workspace:desk", kind="workspace", title="Desk", status=CanonicalStatus.ONLINE),
        CanonicalObject(id="browser:b1", kind="browser_session", title="Browser 1", status=CanonicalStatus.WARNING),
        CanonicalObject(id="io:node:say", kind="io_endpoint", title="say", status=CanonicalStatus.OFFLINE),
    ]

    projection = canonical_inventory_projection(subject, objects).to_dict()

    assert projection["id"] == "projection:hub:alpha/inventory"
    assert projection["context"]["kind_totals"]["browser_session"] == 1
    assert projection["context"]["incident_total"] == 2
    assert len(projection["incidents"]) == 2


def test_canonical_neighborhood_projection_tracks_peers_and_incidents() -> None:
    subject = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status=CanonicalStatus.ONLINE)
    objects = [
        CanonicalObject(id="member:beta", kind="member", title="Member Beta", status=CanonicalStatus.ONLINE),
        CanonicalObject(id="member:gamma", kind="member", title="Member Gamma", status=CanonicalStatus.WARNING),
        CanonicalObject(id="root:eu", kind="root", title="Root EU", status=CanonicalStatus.ONLINE),
    ]

    projection = canonical_neighborhood_projection(subject, objects).to_dict()

    assert projection["id"] == "projection:hub:alpha/neighborhood"
    assert projection["context"]["peer_total"] == 2
    assert projection["context"]["online_peer_total"] == 1
    assert projection["context"]["incident_total"] == 1


def test_canonical_overview_projection_builds_health_strip_runtime_and_recent_changes() -> None:
    subject = CanonicalObject(
        id="hub:alpha",
        kind="hub",
        title="Hub Alpha",
        status=CanonicalStatus.WARNING,
        summary="Primary hub requires attention",
        versioning={"actual": "2026.04.07", "desired": "2026.04.08", "drift": True},
    )
    objects = [
        CanonicalObject(
            id="runtime:hub:alpha/yjs-sync",
            kind="runtime",
            title="Yjs sync",
            status=CanonicalStatus.DEGRADED,
            summary="bounded replay window near limit",
            runtime={"phase": "bounded_sync", "recent_transitions_5m": 2},
        ),
        CanonicalObject(
            id="quota:telegram-outbox",
            kind="quota",
            title="Telegram outbox",
            status=CanonicalStatus.WARNING,
            summary="publish backlog is growing",
            resources={"used": 9, "limit": 10},
        ),
    ]

    projection = canonical_overview_projection(subject, objects).to_dict()

    assert projection["id"] == "projection:hub:alpha/overview"
    assert projection["context"]["summary_tile"]["value"] == "degraded"
    assert projection["context"]["health_strip"][0]["object_id"] == "hub:alpha"
    assert projection["context"]["quota_summary"][0]["object_id"] == "quota:telegram-outbox"
    assert projection["context"]["active_runtimes"][0]["object_id"] == "runtime:hub:alpha/yjs-sync"
    assert any(item["category"] == "drift" for item in projection["context"]["recent_changes"])


def test_canonical_object_from_supervisor_runtime_surfaces_transition_state() -> None:
    obj = canonical_object_from_supervisor_runtime(
        {
            "node_id": "alpha",
            "runtime_state": {
                "active_slot": "A",
                "runtime_state": "spawned",
                "desired_running": True,
                "managed_alive": True,
                "runtime_api_ready": False,
                "supervisor_url": "http://127.0.0.1:8776",
                "runtime_url": "http://127.0.0.1:8777",
            },
            "update_status": {
                "state": "restarting",
                "phase": "shutdown",
                "message": "countdown completed; pending update written",
                "target_rev": "rev2026",
                "target_version": "0.1.0+40.deadbee",
            },
        }
    ).to_dict()

    assert obj["id"] == "runtime:node:alpha/supervisor"
    assert obj["kind"] == "runtime"
    assert obj["status"] == "warning"
    assert obj["runtime"]["assessment"]["state"] == "restarting"
    assert obj["runtime"]["phase"] == "shutdown"
    assert obj["runtime"]["active_slot"] == "A"
    assert obj["actions"][0]["id"] == "restart_runtime"


def test_canonical_object_from_supervisor_runtime_surfaces_reschedulable_transition_actions() -> None:
    obj = canonical_object_from_supervisor_runtime(
        {
            "node_id": "alpha",
            "runtime_state": {
                "active_slot": "B",
                "previous_slot": "A",
                "runtime_state": "ready",
                "desired_running": True,
                "managed_alive": True,
                "runtime_api_ready": True,
                "update_task_running": True,
            },
            "update_status": {
                "action": "update",
                "state": "preparing",
                "phase": "prepare",
                "message": "preparing inactive slot before restart",
            },
            "update_attempt": {
                "action": "update",
                "state": "active",
            },
        }
    ).to_dict()

    actions = {item["id"]: item for item in obj["actions"]}

    assert obj["status"] == "warning"
    assert actions["restart_runtime"]["metadata"]["api_path"] == "/api/supervisor/runtime/restart"
    assert actions["cancel_transition"]["title"] == "cancel update"
    assert actions["cancel_transition"]["metadata"]["api_path"] == "/api/supervisor/update/cancel"
    assert actions["defer_transition_5m"]["title"] == "defer update 5 min"
    assert actions["defer_transition_5m"]["metadata"]["delay_sec"] == 300.0
    assert actions["defer_transition_15m"]["title"] == "defer update 15 min"
    assert actions["defer_transition_15m"]["metadata"]["delay_sec"] == 900.0
    assert actions["rollback_runtime"]["title"] == "rollback to slot A"
    assert actions["rollback_runtime"]["metadata"]["api_path"] == "/api/supervisor/update/rollback"


def test_canonical_object_from_supervisor_runtime_keeps_root_restart_pending_visible() -> None:
    obj = canonical_object_from_supervisor_runtime(
        {
            "node_id": "alpha",
            "runtime_state": {
                "active_slot": "A",
                "runtime_state": "spawned",
                "desired_running": True,
                "managed_alive": True,
                "runtime_api_ready": True,
            },
            "update_status": {
                "state": "succeeded",
                "phase": "root_promoted",
                "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
            },
            "update_attempt": {
                "state": "awaiting_root_restart",
            },
        }
    ).to_dict()

    assert obj["status"] == "warning"
    assert obj["runtime"]["attempt_state"] == "awaiting_root_restart"
    assert obj["runtime"]["assessment"]["state"] == "awaiting_root_restart"


def test_canonical_object_from_supervisor_runtime_surfaces_planned_update_context() -> None:
    obj = canonical_object_from_supervisor_runtime(
        {
            "node_id": "alpha",
            "runtime_state": {
                "active_slot": "B",
                "runtime_state": "spawned",
                "runtime_instance_id": "rt-b-a-12345678",
                "transition_role": "active",
                "desired_running": True,
                "managed_alive": True,
                "runtime_api_ready": True,
                "transition_mode": "warm_switch",
                "candidate_slot": "A",
                "candidate_runtime_url": "http://127.0.0.1:8777",
                "candidate_runtime_port": 8777,
                "candidate_runtime_instance_id": "rt-a-c-87654321",
                "candidate_runtime_state": "ready",
                "candidate_transition_role": "candidate",
                "candidate_runtime_api_ready": True,
                "warm_switch_supported": True,
                "warm_switch_allowed": True,
                "warm_switch_reason": "warm switch admitted",
            },
            "update_status": {
                "action": "update",
                "state": "planned",
                "phase": "scheduled",
                "message": "core update deferred until minimum update interval elapses",
                "planned_reason": "minimum_update_period",
                "min_update_period_sec": 300.0,
                "scheduled_for": 1234.0,
                "subsequent_transition": True,
                "candidate_prewarm_state": "ready",
                "candidate_prewarm_message": "passive candidate runtime is ready on http://127.0.0.1:8777",
                "candidate_prewarm_ready_at": 1225.0,
            },
            "update_attempt": {
                "action": "update",
                "state": "planned",
                "subsequent_transition_requested_at": 1200.0,
            },
        }
    ).to_dict()

    assert obj["status"] == "warning"
    assert obj["runtime"]["action"] == "update"
    assert obj["runtime"]["planned_reason"] == "minimum_update_period"
    assert obj["runtime"]["min_update_period_sec"] == 300.0
    assert obj["runtime"]["scheduled_for"] == 1234.0
    assert obj["runtime"]["transition_mode"] == "warm_switch"
    assert obj["runtime"]["candidate_slot"] == "A"
    assert obj["runtime"]["runtime_instance_id"] == "rt-b-a-12345678"
    assert obj["runtime"]["transition_role"] == "active"
    assert obj["runtime"]["candidate_runtime_instance_id"] == "rt-a-c-87654321"
    assert obj["runtime"]["candidate_runtime_state"] == "ready"
    assert obj["runtime"]["candidate_transition_role"] == "candidate"
    assert obj["runtime"]["candidate_runtime_api_ready"] is True
    assert obj["runtime"]["candidate_prewarm_state"] == "ready"
    assert obj["runtime"]["candidate_prewarm_ready_at"] == 1225.0
    assert obj["runtime"]["warm_switch_allowed"] is True
    assert obj["runtime"]["subsequent_transition"] is True
    assert obj["actual_state"]["runtime_instance_id"] == "rt-b-a-12345678"
    assert obj["actual_state"]["action"] == "update"
    assert obj["actual_state"]["candidate_runtime_instance_id"] == "rt-a-c-87654321"
    assert obj["actual_state"]["candidate_runtime_state"] == "ready"
    assert obj["actual_state"]["candidate_prewarm_state"] == "ready"
    assert obj["actual_state"]["candidate_prewarm_ready_at"] == 1225.0
    assert obj["actual_state"]["subsequent_transition_requested_at"] == 1200.0
    assert obj["actual_state"]["candidate_runtime_port"] == 8777
    assert obj["representations"]["operator"]["update_action"] == "update"
    assert obj["representations"]["operator"]["candidate_prewarm_state"] == "ready"
    assert "previous_slot" not in obj["runtime"]
    assert "prewarm ready" in obj["representations"]["operator"]["subtitle"]
    action_ids = [item["id"] for item in obj["actions"]]
    assert "cancel_transition" in action_ids
    assert "defer_transition_5m" in action_ids
    assert "defer_transition_15m" in action_ids


def test_canonical_object_inspector_collects_actions_topology_and_task_packet() -> None:
    subject = CanonicalObject(
        id="hub:alpha",
        kind="hub",
        title="Hub Alpha",
        status=CanonicalStatus.WARNING,
        summary="Primary hub has drift",
        relations={"connected_to": ["root:eu"], "uses": ["skill:weather"]},
        desired_state={"version": "2026.04.08"},
        actual_state={"version": "2026.04.07"},
        actions=[CanonicalActionDescriptor(id="restart", title="restart", risk="medium")],
    )
    objects = [
        CanonicalObject(id="root:eu", kind="root", title="Root EU", status=CanonicalStatus.ONLINE, relations={"connected_to": ["hub:alpha"]}),
        CanonicalObject(id="skill:weather", kind="skill", title="weather", status=CanonicalStatus.DEGRADED, summary="remote version drift"),
    ]

    projection = canonical_object_inspector(subject, objects, task_goal="diagnose drift").to_dict()

    assert projection["id"] == "projection:hub:alpha/inspector"
    assert projection["context"]["inspector"]["value"] == "warning"
    assert projection["context"]["actions"][0]["id"] == "restart"
    assert projection["context"]["topology"]["edges"][0]["source"] == "hub:alpha"
    assert projection["context"]["task_packet"]["context"]["task_goal"] == "diagnose drift"
    assert projection["context"]["inspector"]["subnet_planning"]["summary"]["node_total"] == 1


def test_canonical_object_topology_and_task_packet_projections_share_selected_object_context() -> None:
    subject = CanonicalObject(
        id="hub:alpha",
        kind="hub",
        title="Hub Alpha",
        status=CanonicalStatus.WARNING,
        summary="Primary hub has drift",
        relations={"connected_to": ["root:eu"], "uses": ["skill:weather"]},
        desired_state={"version": "2026.04.08"},
        actual_state={"version": "2026.04.07"},
        actions=[CanonicalActionDescriptor(id="restart", title="restart", risk="medium")],
    )
    objects = [
        CanonicalObject(id="root:eu", kind="root", title="Root EU", status=CanonicalStatus.ONLINE, relations={"connected_to": ["hub:alpha"]}),
        CanonicalObject(id="skill:weather", kind="skill", title="weather", status=CanonicalStatus.DEGRADED, summary="remote version drift"),
    ]

    object_projection = canonical_object_projection(subject, objects).to_dict()
    topology_projection = canonical_topology_projection(subject, objects).to_dict()
    task_packet = canonical_task_packet(subject, objects, task_goal="diagnose drift").to_dict()

    assert object_projection["id"] == "projection:hub:alpha/object"
    assert object_projection["context"]["narrative"]["risk_summary"] == "2 active incident(s)"
    assert object_projection["representations"]["operator"]["actions"][0]["object_id"] == "hub:alpha"
    assert topology_projection["id"] == "projection:hub:alpha/topology"
    assert topology_projection["context"]["edge_total"] >= 2
    assert task_packet["id"] == "projection:hub:alpha/task-packet"
    assert task_packet["context"]["task_goal"] == "diagnose drift"
    assert task_packet["context"]["gap"]["version"]["desired"] == "2026.04.08"
    assert task_packet["context"]["allowed_actions"][0]["id"] == "restart"


def test_canonical_neighborhood_and_task_packet_include_subnet_planning_context() -> None:
    now = time.time()
    subject = canonical_object_from_subnet_directory_node(
        {
            "node_id": "hub-1",
            "subnet_id": "main",
            "roles": ["hub"],
            "hostname": "Hub Alpha",
            "node_state": "ready",
            "online": True,
            "capacity": {"io": [], "skills": [], "scenarios": []},
            "runtime_projection": {
                "captured_at": now - 10.0,
                "node_names": ["Hub Alpha"],
                "primary_node_name": "Hub Alpha",
                "ready": True,
                "route_mode": "hub",
                "build": {"runtime_version": "0.4.0"},
                "update_status": {"state": "succeeded", "phase": "validate"},
            },
        }
    )
    member = canonical_object_from_subnet_directory_node(
        {
            "node_id": "member-2",
            "subnet_id": "main",
            "roles": ["member"],
            "hostname": "Kitchen Member",
            "node_state": "ready",
            "online": True,
            "capacity": {"io": [], "skills": [], "scenarios": []},
            "runtime_projection": {
                "captured_at": now - 20.0,
                "node_names": ["Kitchen East"],
                "primary_node_name": "Kitchen East",
                "ready": True,
                "route_mode": "ws",
                "build": {"runtime_version": "0.2.0", "runtime_git_short_commit": "abc1234"},
                "update_status": {"state": "succeeded", "phase": "validate"},
            },
        }
    )

    neighborhood = canonical_neighborhood_projection(subject, [member]).to_dict()
    task_packet = canonical_task_packet(subject, [member], task_goal="plan subnet rollout").to_dict()

    assert neighborhood["context"]["subnet_runtime_summary"]["node_total"] == 2
    assert neighborhood["context"]["subnet_runtime_summary"]["freshness_totals"]["fresh"] == 2
    assert task_packet["context"]["subnet_planning"]["summary"]["node_total"] == 2
    assert task_packet["context"]["subnet_planning"]["summary"]["route_mode_totals"]["ws"] == 1
    assert task_packet["context"]["subnet_planning"]["nodes"][1]["runtime_git_short_commit"] == "abc1234"


def test_canonical_projection_from_reliability_snapshot_keeps_media_route_contract() -> None:
    projection = canonical_projection_from_reliability_snapshot(
        {
            "node": {
                "node_id": "hub-3",
                "subnet_id": "main",
                "role": "hub",
                "ready": True,
                "node_state": "ready",
                "draining": False,
            },
            "runtime": {
                "readiness_tree": {},
                "channel_diagnostics": {},
                "channel_overview": {},
                "media_runtime": {
                    "available": True,
                    "scope": "hub_media",
                    "assessment": {
                        "state": "relay_and_webrtc_media_available",
                        "reason": "media plane supports direct-local authority, bounded root relay authority, and live WebRTC audio/video loopback",
                    },
                    "transport": {
                        "direct_local_ready": True,
                        "root_routed_ready": True,
                        "broadcast_ready": True,
                    },
                    "counts": {
                        "file_total": 1,
                        "total_bytes": 128,
                        "live_peer_total": 1,
                        "live_connected_peers": 1,
                    },
                    "recommended_path": "direct_local_http",
                    "route_intent": {
                        "route_intent": "scenario_response_media",
                        "active_route": "local_http",
                        "preferred_member_id": "member-2",
                    },
                    "preferred_member_id": "member-2",
                    "producer_authority": "hub",
                    "producer_target": {"kind": "hub", "webspace_id": "desk"},
                    "delivery_topology": "local_http",
                    "selection_reason": "local_hub_api_authority_available",
                    "degradation_reason": None,
                    "attempt": {
                        "sequence": 2,
                        "active_route": "local_http",
                        "delivery_topology": "local_http",
                        "preferred_route": "member_browser_direct",
                        "previous_route": "member_browser_direct",
                        "previous_member_id": "member-1",
                        "switch_total": 1,
                    },
                    "monitoring": {
                        "refresh_cause": "browser.session.changed",
                        "observed_failure": "browser_session_closed",
                    },
                    "member_browser_direct": {
                        "possible": True,
                        "admitted": False,
                        "ready": False,
                        "reason": "member_browser_direct_policy_not_admitted_yet",
                    },
                    "update_guard": {
                        "member_runtime_update": "allow",
                        "hub_runtime_update": "allow",
                        "current_support": "not_applicable",
                    },
                },
            },
        }
    ).to_dict()

    objects = {item["id"]: item for item in projection["objects"]}
    media = objects["runtime:hub:hub-3/media-plane"]
    assert media["runtime"]["route_intent"]["route_intent"] == "scenario_response_media"
    assert media["runtime"]["preferred_member_id"] == "member-2"
    assert media["runtime"]["producer_authority"] == "hub"
    assert media["runtime"]["delivery_topology"] == "local_http"
    assert media["runtime"]["attempt"]["sequence"] == 2
    assert media["runtime"]["attempt"]["previous_route"] == "member_browser_direct"
    assert media["runtime"]["monitoring"]["refresh_cause"] == "browser.session.changed"
    assert media["runtime"]["member_browser_direct"]["possible"] is True
    assert media["runtime"]["update_guard"]["current_support"] == "not_applicable"
