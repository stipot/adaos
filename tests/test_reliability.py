from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from types import SimpleNamespace
import types

try:
    import nats  # noqa: F401
except Exception:
    sys.modules["nats"] = types.ModuleType("nats")
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from adaos.services.reliability import (
    ReadinessStatus,
    _event_model_phase0_communication_checkpoint,
    _hub_member_transport_evidence_snapshot,
    _enrich_required_upstream_link_with_sidecar,
    _request_yjs_replay_pressure_compaction,
    _state_sync_snapshot,
    assess_transport_diagnostics,
    hub_member_connection_state_snapshot,
    hub_member_semantic_channels_snapshot,
    media_plane_runtime_snapshot,
    observe_hub_root_route_runtime,
    mark_root_control_down,
    mark_root_control_up,
    mark_route_ready,
    note_root_control_reconnect,
    reliability_snapshot,
    reset_reliability_runtime_state,
    set_integration_readiness,
    sidecar_runtime_snapshot,
    supervisor_transition_runtime_snapshot,
    yjs_sync_runtime_snapshot,
)
from adaos.services.runtime_lifecycle import reset_runtime_lifecycle


def _reset_state() -> None:
    reset_runtime_lifecycle()
    reset_reliability_runtime_state()


def test_hub_reliability_snapshot_exposes_taxonomy_and_disables_root_bound_capabilities_until_ready() -> None:
    _reset_state()

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert "command" in snapshot["model"]["message_taxonomy"]
    assert "must_not_lose" in snapshot["model"]["delivery_classes"]
    assert snapshot["model"]["authority_boundaries"]["root"]["owns"]
    assert any(item["flow_id"] == "hub_root.control.lifecycle" for item in snapshot["model"]["flow_inventory"])

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["hub_local_core"]["status"] == "ready"
    assert tree["root_control"]["status"] == "unknown"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["execute_local_scenarios"]["allowed"] is True
    assert matrix["new_root_backed_member_admission"]["allowed"] is False
    assert matrix["root_routed_browser_proxy"]["allowed"] is False


def test_hub_reliability_snapshot_enables_route_and_integration_capabilities_when_signals_are_ready() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["root_control"]["status"] == "ready"
    assert tree["route"]["status"] == "ready"
    assert tree["integration"]["telegram"]["status"] == "degraded"
    assert snapshot["runtime"]["channel_diagnostics"]["root_control"]["stability"]["state"] == "stable"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["root_routed_browser_proxy"]["allowed"] is True
    assert matrix["telegram_action_completion"]["allowed"] is False

    set_integration_readiness(
        "telegram",
        status=ReadinessStatus.READY,
        summary="telegram delivery probe ok",
        observed=True,
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "ready"
    assert snapshot["runtime"]["degraded_matrix"]["telegram_action_completion"]["allowed"] is True


def test_hub_reliability_marks_root_backed_integration_as_stale_when_root_control_is_lost() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    set_integration_readiness(
        "telegram",
        status=ReadinessStatus.READY,
        summary="telegram delivery probe ok",
        observed=True,
    )

    ready_snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )
    assert ready_snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "ready"

    mark_root_control_down(details={"kind": "disconnected"})
    stale_snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert any(item["flow_id"] == "hub_root.integration.llm" for item in stale_snapshot["model"]["flow_inventory"])
    assert stale_snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "down"
    assert stale_snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "degraded"
    assert stale_snapshot["runtime"]["degraded_matrix"]["telegram_action_completion"]["allowed"] is False
    assert stale_snapshot["runtime"]["channel_diagnostics"]["root_control"]["stability"]["state"] == "down"


def test_hub_reliability_marks_flapping_root_channel_when_it_repeatedly_disconnects() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})
    mark_root_control_down(details={"kind": "disconnected"})
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_root_control_down(details={"kind": "disconnected"})
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "degraded"
    assert snapshot["runtime"]["readiness_tree"]["route"]["status"] == "degraded"
    diag = snapshot["runtime"]["channel_diagnostics"]["root_control"]
    assert diag["recent_non_ready_transitions_5m"] == 2
    assert diag["recent_transitions_5m"] >= 5
    assert diag["stability"]["state"] == "flapping"
    assert isinstance(diag["recent_history"], list) and len(diag["recent_history"]) >= 5


def test_hub_reliability_marks_root_channel_unstable_after_reconnect_incident_without_explicit_down() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats", "ws_tag": "tag-a"})
    note_root_control_reconnect(
        details={"server": "wss://api.inimatic.com/nats", "previous_ws_tag": "tag-a", "ws_tag": "tag-b"}
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    diag = snapshot["runtime"]["channel_diagnostics"]["root_control"]
    assert snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "degraded"
    assert diag["recent_non_ready_transitions_5m"] == 1
    assert diag["stability"]["state"] in {"unstable", "flapping"}
    assert any(item["status"] == "reconnect" for item in diag["recent_history"])


def test_hub_reliability_snapshot_exposes_route_reset_runtime_details() -> None:
    _reset_state()
    observe_hub_root_route_runtime(
        last_reset_at=1_774_017_180.0,
        last_reset_reason="nats_reconnected",
        last_reset_closed_tunnels=3,
        last_reset_dropped_pending=11,
        last_reset_notified_browser=2,
        reset_total=4,
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    route_runtime = snapshot["runtime"]["hub_root_protocol"]["route_runtime"]
    assert route_runtime["last_reset_reason"] == "nats_reconnected"
    assert route_runtime["last_reset_closed_tunnels"] == 3
    assert route_runtime["last_reset_dropped_pending"] == 11
    assert route_runtime["last_reset_notified_browser"] == 2
    assert route_runtime["reset_total"] == 4
    assert route_runtime["last_reset_ago_s"] is not None


def test_hub_member_connection_state_uses_persisted_runtime_projection_for_linkless_members(monkeypatch) -> None:
    class _FakeDirectory:
        def list_known_nodes(self):
            return [
                {
                    "node_id": "member-2",
                    "subnet_id": "sn_1",
                    "roles": ["member"],
                    "hostname": "kitchen-member",
                    "node_state": "ready",
                    "last_seen": 1_700_000_050.0,
                    "online": True,
                    "capacity": {"io": [], "skills": [], "scenarios": []},
                    "runtime_projection": {
                        "captured_at": 1_700_000_040.0,
                        "node_names": ["Kitchen East"],
                        "primary_node_name": "Kitchen East",
                        "ready": True,
                        "node_state": "ready",
                        "snapshot": {
                            "captured_at": 1_700_000_040.0,
                            "node_state": "ready",
                            "build": {"runtime_version": "0.2.0", "runtime_git_short_commit": "abc1234"},
                            "update_status": {"state": "succeeded", "phase": "validate"},
                        },
                    },
                }
            ]

    monkeypatch.setattr(
        "adaos.services.subnet.link_manager.hub_link_manager_snapshot",
        lambda: {"members": [], "member_total": 0, "connected_total": 0, "updated_at": 1_700_000_060.0},
    )
    monkeypatch.setattr(
        "adaos.services.registry.subnet_directory.get_directory",
        lambda: _FakeDirectory(),
    )
    monkeypatch.setattr(
        "adaos.services.device_inventory.list_devices",
        lambda kind=None: [
            {
                "ref": "member:member-2",
                "kind": "member",
                "identity": {"node_id": "member-2"},
                "policy": {
                    "present": True,
                    "managed_state": "managed",
                    "display_name": "Kitchen tablet",
                    "effective_name": "Kitchen tablet",
                    "access_class": "device",
                    "lifetime_mode": "permanent",
                },
                "runtime": {"connected_to_subnet": True},
            }
        ],
    )
    monkeypatch.setattr(
        "adaos.services.reliability.time.time",
        lambda: 1_700_000_060.0,
    )

    snapshot = hub_member_connection_state_snapshot(
        role="hub",
        route_mode="hub",
        connected_to_hub=None,
        node_id="hub-1",
        node_names=["Main Hub"],
    )

    assert snapshot["assessment"]["reason"] == "known_members_without_links"
    assert snapshot["known_total"] == 1
    member = snapshot["known_members"][0]
    assert member["device_ref"] == "member:member-2"
    assert member["managed_state"] == "managed"
    assert member["display_name"] == "Kitchen tablet"
    assert member["effective_name"] == "Kitchen tablet"
    assert member["connected_to_subnet"] is True
    assert member["observed_via"] == "subnet_directory"
    assert member["label"] == "Kitchen tablet"
    assert member["node_names"] == ["Kitchen East"]
    assert member["snapshot_ready"] is True
    assert member["snapshot_state"] == "fresh"
    assert member["runtime_projection_freshness"]["state"] == "fresh"
    assert member["snapshot_update_state"] == "succeeded"
    assert member["snapshot_runtime_version"] == "0.2.0"


def test_assess_transport_diagnostics_marks_unstable_on_reader_termination_and_tag_change() -> None:
    now_ts = 1_774_017_180.0
    assessment = assess_transport_diagnostics(
        [
            {
                "ts": now_ts - 12.0,
                "source": "periodic",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": None,
            },
            {
                "ts": now_ts - 3.0,
                "source": "periodic",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": True},
                "err": None,
            },
            {
                "ts": now_ts - 1.0,
                "source": "periodic",
                "ws_tag": "tag-b",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": None,
            },
        ],
        now_ts=now_ts,
    )

    assert assessment["state"] in {"unstable", "flapping", "down"}
    assert assessment["recent_tag_changes_5m"] == 1
    assert assessment["recent_incidents_5m"] >= 1
    assert "reading_task_terminated" in assessment["last_incident_reasons"]


def test_assess_transport_diagnostics_marks_flapping_on_repeated_error_callbacks() -> None:
    now_ts = 1_774_017_300.0
    assessment = assess_transport_diagnostics(
        [
            {
                "ts": now_ts - 240.0,
                "source": "error_cb",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": "UnexpectedEOF: nats: unexpected EOF",
            },
            {
                "ts": now_ts - 120.0,
                "source": "error_cb",
                "ws_tag": "tag-b",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": "UnexpectedEOF: nats: unexpected EOF",
            },
            {
                "ts": now_ts - 5.0,
                "source": "periodic",
                "ws_tag": "tag-c",
                "nc_connected": True,
                "reading_task": {"done": True},
                "err": None,
            },
        ],
        now_ts=now_ts,
    )

    assert assessment["state"] in {"flapping", "down"}
    assert assessment["recent_error_records_5m"] >= 1
    assert assessment["recent_tag_changes_15m"] >= 2
    assert assessment["recent_hard_incidents_5m"] >= 1


def test_hub_member_semantic_channels_snapshot_exposes_media_route_contract() -> None:
    snapshot = hub_member_semantic_channels_snapshot(
        role="hub",
        route_mode="hub",
        connected_to_hub=None,
        hub_root_protocol={},
        transport_evidence={
            "webrtc_data:events": {"available": False},
            "webrtc_data:yjs": {"available": False},
            "ws": {"available": False},
            "yws": {"available": False},
            "root_route_proxy": {"available": False},
            "member_link_ws": {"available": False},
            "webrtc_media": {"available": True},
            "member_browser_webrtc_media": {
                "available": False,
                "possible": True,
                "admitted": False,
                "reason": "member_browser_direct_not_admitted",
                "candidate_member_total": 1,
                "candidate_members": ["member-1"],
                "preferred_member_id": "member-1",
                "browser_session_total": 1,
            },
            "root_media_relay": {"available": True},
        },
    )

    media = snapshot["channels"]["hub_member.media"]
    assert media["route_intent"] == "live_stream"
    assert media["delivery_topology"] == "hub_webrtc_loopback"
    assert media["producer_authority"] == "hub"
    assert media["preferred_member_id"] == "member-1"
    assert media["member_browser_direct"]["possible"] is True
    assert media["member_browser_direct"]["admitted"] is False
    assert media["member_browser_direct"]["candidate_members"] == ["member-1"]
    assert media["attempt"]["active_route"] == "hub_webrtc_loopback"
    assert media["attempt"]["sequence"] == 1
    assert media["fallback_chain"] == [
        "member_browser_direct",
        "hub_webrtc_loopback",
        "root_media_relay",
    ]


def test_hub_member_transport_evidence_counts_only_media_capable_members(monkeypatch) -> None:
    import adaos.services.media_capability as media_capability

    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        SimpleNamespace(
            gateway_transport_snapshot=lambda: {
                "transports": {},
                "ownership": {
                    "ws": {
                        "current_owner": "runtime",
                        "lifecycle_manager": "supervisor",
                        "planned_owner": "sidecar",
                        "migration_phase": "phase_2_route_tunnel_ownership",
                        "handoff_ready": False,
                        "handoff_blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                    },
                    "yws": {
                        "current_owner": "runtime",
                        "lifecycle_manager": "supervisor",
                        "planned_owner": "sidecar",
                        "migration_phase": "phase_2_route_tunnel_ownership",
                        "handoff_ready": False,
                        "handoff_blockers": ["Yjs websocket/session ownership still lives in the runtime gateway"],
                    },
                },
            },
            active_browser_session_snapshot=lambda: {
                "peers": [
                    {"device_id": "browser-1", "connection_state": "connected"},
                ]
            },
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.webrtc.peer",
        SimpleNamespace(
            webrtc_peer_snapshot=lambda: {
                "peer_total": 0,
                "connected_peers": 0,
                "incoming_audio_tracks": 0,
                "incoming_video_tracks": 0,
                "loopback_audio_tracks": 0,
                "loopback_video_tracks": 0,
                "open_events_channels": 0,
                "open_yjs_channels": 0,
            }
        ),
    )
    monkeypatch.setattr(
        media_capability,
        "_directory_nodes",
        lambda: [
            {
                "node_id": "member-capable",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "webrtc_media",
                            "capabilities": [
                                "webrtc:av",
                                "producer:member",
                                "topology:member_browser_direct",
                                "media:live_stream",
                                "state:available",
                            ],
                            "priority": 60,
                        }
                    ]
                },
            },
            {
                "node_id": "member-incapable",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "say",
                            "capabilities": ["text", "state:available"],
                            "priority": 40,
                        }
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(media_capability, "_live_member_links", lambda: [])

    evidence = _hub_member_transport_evidence_snapshot(
        role="hub",
        route_mode="hub",
        connected_to_hub=None,
        hub_root_protocol={},
    )

    member_browser = evidence["member_browser_webrtc_media"]
    assert member_browser["possible"] is True
    assert member_browser["candidate_member_total"] == 1
    assert member_browser["candidate_members"] == ["member-capable"]
    assert member_browser["preferred_member_id"] == "member-capable"
    assert evidence["ws"]["owner"] == "runtime"
    assert evidence["ws"]["planned_owner"] == "sidecar"
    assert evidence["yws"]["lifecycle_manager"] == "supervisor"


def test_sidecar_runtime_snapshot_exposes_scope_and_lifecycle_manager(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.realtime_sidecar",
        SimpleNamespace(
            realtime_sidecar_diag_path=lambda: tmp_path / "realtime_sidecar.jsonl",
            realtime_sidecar_enabled=lambda **kwargs: True,
            realtime_sidecar_listener_snapshot=lambda proc=None: {"listener_running": True, "listener_pid": 42},
            realtime_sidecar_local_url=lambda: "nats://127.0.0.1:7422",
            realtime_sidecar_route_tunnel_contract=lambda: {
                "current_support": "planned",
                "lifecycle_manager": "supervisor",
                "ownership_boundary": "transport_only",
                "ws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "delegation_mode": "not_implemented",
                    "handoff_ready": False,
                    "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                },
                "yws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "delegation_mode": "not_implemented",
                    "handoff_ready": False,
                    "blockers": ["Yjs websocket/session ownership still lives in the runtime gateway"],
                },
            },
        ),
    )

    snapshot = sidecar_runtime_snapshot(
        role="hub",
        readiness_tree={},
        hub_root_protocol={},
        transport_strategy={},
        media_runtime={
            "update_guard": {
                "hub_sidecar_continuity_required": True,
                "member_runtime_update": "defer",
                "hub_runtime_update": "preserve_sidecar",
                "observed_live_topology": "member_browser_direct",
                "reason": "member owns the active browser media path",
            }
        },
    )

    assert snapshot["enabled"] is True
    assert snapshot["enablement"]["enabled"] is True
    assert snapshot["enablement"]["source"] == "legacy_runtime"
    assert snapshot["transport_owner"] == "sidecar"
    assert snapshot["lifecycle_manager"] == "supervisor"
    assert snapshot["scope"]["current_boundaries"] == ["hub_root_transport"]
    assert snapshot["scope"]["runtime_fallback_boundaries"] == ["browser_events_ws", "browser_yjs_ws"]
    assert snapshot["scope"]["planned_next_boundaries"] == ["browser_events_ws", "browser_yjs_ws"]
    assert snapshot["continuity_contract"]["required"] is True
    assert snapshot["continuity_contract"]["member_runtime_update"] == "defer"
    assert snapshot["continuity_contract"]["hub_runtime_update"] == "preserve_sidecar"
    assert snapshot["continuity_contract"]["current_support"] == "planned"
    assert snapshot["continuity_contract"]["pending_boundaries"] == ["browser_events_ws", "browser_yjs_ws"]
    assert snapshot["progress"]["target"] == "first_browser_realtime_tunnel"
    assert snapshot["progress"]["completed_milestones"] == 2
    assert snapshot["progress"]["milestone_total"] == 4
    assert snapshot["progress"]["current_milestone"] == "browser_events_ws_handoff"
    assert snapshot["route_ready"] == "planned"
    assert snapshot["sync_ready"] == "planned"
    assert snapshot["delegations"]["route_tunnel_transport"] is False
    assert snapshot["delegations"]["sync_transport"] is False
    assert snapshot["route_tunnel_contract"]["ownership_boundary"] == "transport_only"
    assert snapshot["route_tunnel_contract"]["ws"]["planned_owner"] == "sidecar"
    assert snapshot["route_tunnel_contract"]["yws"]["delegation_mode"] == "not_implemented"


def test_sidecar_runtime_snapshot_promotes_route_tunnel_readiness_into_scope_and_continuity(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.realtime_sidecar",
        SimpleNamespace(
            realtime_sidecar_diag_path=lambda: tmp_path / "realtime_sidecar.jsonl",
            realtime_sidecar_enabled=lambda **kwargs: True,
            realtime_sidecar_listener_snapshot=lambda proc=None: {"listener_running": True, "listener_pid": 77},
            realtime_sidecar_local_url=lambda: "nats://127.0.0.1:7422",
            realtime_sidecar_route_tunnel_contract=lambda: {
                "current_support": "planned",
                "lifecycle_manager": "supervisor",
                "ownership_boundary": "transport_only",
                "ws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "local_ipc_proxy",
                    "listener_ready": True,
                    "handoff_ready": True,
                    "blockers": [],
                },
                "yws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "local_ipc_proxy",
                    "listener_ready": True,
                    "handoff_ready": True,
                    "blockers": [],
                },
            },
        ),
    )

    snapshot = sidecar_runtime_snapshot(
        role="hub",
        readiness_tree={},
        hub_root_protocol={},
        transport_strategy={},
        media_runtime={
            "update_guard": {
                "hub_sidecar_continuity_required": True,
                "member_runtime_update": "defer",
                "hub_runtime_update": "preserve_sidecar",
                "observed_live_topology": "member_browser_direct",
                "reason": "member owns the active browser media path",
            }
        },
    )

    assert snapshot["route_ready"] == "ready"
    assert snapshot["sync_ready"] == "ready"
    assert snapshot["enablement"]["enabled"] is True
    assert snapshot["delegations"]["route_tunnel_transport"] is True
    assert snapshot["delegations"]["sync_transport"] is True
    assert snapshot["scope"]["current_boundaries"] == [
        "hub_root_transport",
        "browser_events_ws",
        "browser_yjs_ws",
    ]
    assert snapshot["scope"]["planned_next_boundaries"] == []
    assert snapshot["continuity_contract"]["current_support"] == "ready"
    assert snapshot["continuity_contract"]["ready_boundaries"] == [
        "browser_events_ws",
        "browser_yjs_ws",
    ]


def test_sidecar_runtime_snapshot_reports_starting_when_enabled_without_diag(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.realtime_sidecar",
        SimpleNamespace(
            realtime_sidecar_diag_path=lambda: tmp_path / "realtime_sidecar.jsonl",
            realtime_sidecar_enabled=lambda **kwargs: True,
            realtime_sidecar_listener_snapshot=lambda proc=None: {
                "listener_running": True,
                "listener_pid": 42,
                "managed_alive": True,
            },
            realtime_sidecar_local_url=lambda: "nats://127.0.0.1:7422",
            realtime_sidecar_route_tunnel_contract=lambda: {},
        ),
    )

    snapshot = sidecar_runtime_snapshot(
        role="hub",
        readiness_tree={},
        hub_root_protocol={},
        transport_strategy={},
        media_runtime={},
    )

    assert snapshot["status"] == "unknown"
    assert snapshot["session_state"] == "starting"
    assert snapshot["status_reason"] == "sidecar process is running but has not emitted diagnostics yet"
    assert snapshot["diag_fresh"] is False


def test_sidecar_runtime_snapshot_reports_local_only_and_connect_error_details(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    diag_path = tmp_path / "realtime_sidecar.jsonl"
    diag_path.write_text(
        (
            '{"ts": 100.0, "local_connected_ago_s": 0.2, "remote_connect_fail_total": 2, '
            '"last_remote_connect_error": "ConnectError: dial tcp timeout"}\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("adaos.services.reliability.time.time", lambda: 105.0)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.realtime_sidecar",
        SimpleNamespace(
            realtime_sidecar_diag_path=lambda: diag_path,
            realtime_sidecar_enabled=lambda **kwargs: True,
            realtime_sidecar_listener_snapshot=lambda proc=None: {"listener_running": True, "listener_pid": 77},
            realtime_sidecar_local_url=lambda: "nats://127.0.0.1:7422",
            realtime_sidecar_route_tunnel_contract=lambda: {},
        ),
    )

    snapshot = sidecar_runtime_snapshot(
        role="hub",
        readiness_tree={},
        hub_root_protocol={},
        transport_strategy={},
        media_runtime={},
    )

    assert snapshot["status"] == "degraded"
    assert snapshot["session_state"] == "local_only"
    assert snapshot["status_reason"] == "local listener is active but remote session is not connected"
    assert snapshot["diag_fresh"] is True
    assert snapshot["transport_provenance"]["last_connect_error_class"] == "ConnectError"
    assert snapshot["transport_provenance"]["last_connect_error_message"] == "ConnectError: dial tcp timeout"
    assert snapshot["continuity_contract"]["pending_boundaries"] == []
    assert snapshot["progress"]["state"] == "in_progress"
    assert snapshot["progress"]["completed_milestones"] == 2
    assert snapshot["progress"]["current_milestone"] == "browser_events_ws_handoff"


def test_sidecar_runtime_snapshot_marks_websocket_handoffs_ready_after_cutover(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.realtime_sidecar",
        SimpleNamespace(
            realtime_sidecar_diag_path=lambda: tmp_path / "realtime_sidecar.jsonl",
            realtime_sidecar_enabled=lambda **kwargs: True,
            realtime_sidecar_listener_snapshot=lambda proc=None: {"listener_running": True, "listener_pid": 88},
            realtime_sidecar_local_url=lambda: "nats://127.0.0.1:7422",
            realtime_sidecar_route_tunnel_contract=lambda: {
                "current_support": "ready",
                "lifecycle_manager": "supervisor",
                "ownership_boundary": "transport_only",
                "ws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "local_ws_proxy",
                    "listener_ready": True,
                    "handoff_ready": True,
                    "blockers": [],
                },
                "yws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "local_ws_proxy",
                    "listener_ready": True,
                    "handoff_ready": True,
                    "blockers": [],
                },
            },
        ),
    )

    snapshot = sidecar_runtime_snapshot(
        role="hub",
        readiness_tree={},
        hub_root_protocol={},
        transport_strategy={},
        media_runtime={},
    )

    assert snapshot["route_ready"] == "ready"
    assert snapshot["sync_ready"] == "ready"
    assert snapshot["route_tunnel_contract"]["ws"]["listener_ready"] is True
    assert snapshot["route_tunnel_contract"]["yws"]["listener_ready"] is True
    assert snapshot["progress"]["state"] == "ready"
    assert snapshot["progress"]["current_milestone"] is None
    assert snapshot["scope"]["current_boundaries"] == [
        "hub_root_transport",
        "browser_events_ws",
        "browser_yjs_ws",
    ]


def test_yjs_sync_runtime_snapshot_exposes_transport_ownership(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        SimpleNamespace(
            ystore_runtime_snapshot=lambda **kwargs: {
                "webspace_total": 1,
                "active_webspace_total": 1,
                "webspaces": {
                    "default": {
                        "log_mode": "snapshot_plus_diff",
                        "update_log_entries": 3,
                        "max_update_log_entries": 128,
                        "replay_window_entries": 2,
                        "replay_window_bytes": 512,
                        "compact_total": 0,
                        "runtime_compaction_eligible": True,
                        "backup_fast_path_total": 1,
                        "backup_skipped_total": 0,
                    }
                },
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        SimpleNamespace(
            gateway_transport_snapshot=lambda: {
                "transports": {
                    "yws": {
                        "active_connections": 2,
                        "last_close_ago_s": 4.0,
                        "recent_open_10s": 1,
                        "recent_open_60s": 2,
                        "storm_detected": False,
                        "hot_clients": [],
                        "room_open_total": 5,
                        "room_cold_open_total": 2,
                        "room_reuse_total": 3,
                        "room_single_pass_bootstrap_total": 2,
                    }
                },
                "servers": {"yws": {"requested": True, "started_event": True, "task_running": True, "ready": True, "room_total": 1}},
                "commands": {
                    "reload_total": 3,
                    "reload_duplicate_total": 2,
                    "reload_recent_60s": 2,
                    "reset_total": 1,
                    "reset_duplicate_total": 0,
                    "reset_recent_60s": 1,
                    "last_reload": {
                        "client": "http:/api/node/yjs/webspaces/default/reload:127.0.0.1:53301",
                        "webspace_id": "default",
                        "fingerprint": "abc123def456",
                        "duplicate_recent": True,
                        "age_s": 1.25,
                    },
                    "last_reset": {
                        "client": "events_ws:127.0.0.1:54421",
                        "webspace_id": "default",
                        "fingerprint": "rst123def456",
                        "duplicate_recent": False,
                        "age_s": 0.75,
                    },
                    "recent": [
                        {
                            "kind": "desktop.webspace.reload",
                            "webspace_id": "default",
                            "client": "http:/api/node/yjs/webspaces/default/reload:127.0.0.1:53301",
                            "fingerprint": "abc123def456",
                            "duplicate_recent": True,
                            "age_s": 1.25,
                        }
                    ],
                },
                "ownership": {
                    "yws": {
                        "current_owner": "runtime",
                        "lifecycle_manager": "supervisor",
                        "planned_owner": "sidecar",
                        "migration_phase": "phase_2_route_tunnel_ownership",
                        "handoff_ready": False,
                        "handoff_blockers": ["Yjs websocket/session ownership still lives in the runtime gateway"],
                    }
                },
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.webrtc.peer",
        SimpleNamespace(
            webrtc_peer_snapshot=lambda: {
                "peer_total": 1,
                "connected_peers": 1,
                "open_events_channels": 1,
                "open_yjs_channels": 1,
            }
        ),
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_selected_webspace_snapshot",
        lambda webspace_id: {"webspace_id": webspace_id or "default"},
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_recovery_policy",
        lambda selected_entry, selected_webspace: ({}, {}, {}),
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_webspace_guidance",
        lambda selected_webspace, action_overrides: {},
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.load_mark",
        SimpleNamespace(
            yjs_load_mark_snapshot=lambda **kwargs: {
                "window_sec": 60,
                "bucket_sec": 1,
                "thresholds": {"high_bps": 32768, "critical_bps": 131072},
                "assessment": {"state": "high", "reason": "selected_or_cached_webspaces_above_high_threshold"},
                "selected_webspace_id": kwargs.get("webspace_id") or "default",
                "selected_webspace": {
                    "webspace_id": kwargs.get("webspace_id") or "default",
                    "assessment": {"state": "high", "reason": "recent_root_flow_above_high_threshold"},
                    "items": [
                        {"root": "data", "status": "high", "avg_bps": 1024.0, "peak_bps": 4096.0},
                    ],
                },
                "webspace_total": 1,
                "active_root_total": 1,
                "webspaces": {
                    "default": {
                        "webspace_id": "default",
                        "assessment": {"state": "high", "reason": "recent_root_flow_above_high_threshold"},
                        "items": [
                            {"root": "data", "status": "high", "avg_bps": 1024.0, "peak_bps": 4096.0},
                        ],
                    }
                },
            }
        ),
    )

    snapshot = yjs_sync_runtime_snapshot(role="hub", webspace_id="default")
    contract = snapshot["channel_contract"]
    transport = snapshot["transport"]
    ownership = snapshot["ownership_boundaries"]

    assert snapshot["available"] is True
    assert contract["channel_type"] == "sync_channel"
    assert contract["recovery_model"] == "snapshot_plus_diff"
    assert contract["replay_window"] == "bounded"
    assert contract["awareness_semantics"] == "ephemeral"
    assert contract["completed_for_scope"] is True
    assert transport["owner"] == "runtime"
    assert transport["planned_owner"] == "sidecar"
    assert transport["lifecycle_manager"] == "supervisor"
    assert transport["migration_phase"] == "phase_2_route_tunnel_ownership"
    assert transport["handoff_ready"] is False
    assert transport["room_total"] == 1
    assert transport["room_cold_open_total"] == 2
    assert transport["room_reuse_total"] == 3
    assert transport["room_single_pass_bootstrap_total"] == 2
    assert transport["webrtc_peer_total"] == 1
    assert transport["webrtc_open_yjs_channels"] == 1
    assert transport["reload_command_total"] == 3
    assert transport["reload_duplicate_total"] == 2
    assert transport["last_reload_client"] == "http:/api/node/yjs/webspaces/default/reload:127.0.0.1:53301"
    assert transport["last_reset_client"] == "events_ws:127.0.0.1:54421"
    assert snapshot["compaction_eligible_webspace_total"] == 1
    assert snapshot["replay_window_byte_total"] == 512
    assert snapshot["backup_fast_path_total"] == 1
    assert snapshot["load_mark"]["assessment"]["state"] == "high"
    assert snapshot["selected_webspace"]["load_mark"]["items"][0]["root"] == "data"
    assert snapshot["webspaces"]["default"]["load_mark"]["assessment"]["state"] == "high"
    assert ownership["state"] == "explicit"
    assert ownership["selector"]["owner"] == "shared"
    assert ownership["selector"]["status"] == "unset"
    assert ownership["effective_projection"]["owner"] == "runtime"
    assert ownership["effective_projection"]["ready"] is False
    assert ownership["effective_projection"]["branch_total"] == 6
    assert ownership["effective_projection"]["branches"][0]["status"] == "tracked"
    assert ownership["compatibility_caches"]["mode"] == "not_applicable"
    assert ownership["transport_session"]["owner"] == "runtime"
    assert ownership["transport_session"]["planned_owner"] == "sidecar"
    assert snapshot["selected_webspace"]["command_trace"]["last_reload"]["fingerprint"] == "abc123def456"
    assert snapshot["selected_webspace"]["command_trace"]["last_reset"]["fingerprint"] == "rst123def456"


def test_event_model_phase0_communication_checkpoint_tracks_remaining_runtime_gaps() -> None:
    checkpoint = _event_model_phase0_communication_checkpoint(
        sync_runtime={
            "channel_contract": {
                "completed_for_scope": True,
            },
            "transport": {
                "owner": "runtime",
                "planned_owner": "sidecar",
            },
        },
        sidecar_runtime={
            "enabled": True,
            "continuity_contract": {
                "required": False,
                "hub_runtime_update": "preserve_sidecar",
                "current_support": "planned",
                "pending_boundaries": ["browser_events_ws"],
            },
            "progress": {
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
                    "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                },
                "yws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "sidecar_tunnel",
                    "handoff_ready": True,
                    "blockers": [],
                },
            },
        },
        hub_root_protocol={
            "hardening_coverage": {
                "state": "complete",
                "covered_flows": 6,
                "total_flows": 6,
            },
            "route_runtime": {
                "local_base_last_source": "supervisor_public_status",
                "local_base_last_value": "http://127.0.0.1:8777",
                "local_base_discovery_total": 2,
                "local_base_cache_hit_total": 1,
                "local_base_runtime_port_shortcut_total": 0,
                "local_base_error_total": 0,
                "last_open_base_total": 2,
            },
        },
        supervisor_runtime={
            "available": True,
            "source": "supervisor.public_update_status",
            "_served_by": "supervisor_fallback",
            "browser_safe_surface": {
                "state": "ready",
                "ready": True,
                "carried_by_reliability": True,
                "transition_state": "countdown",
                "transition_phase": "scheduled",
                "transition_mode_visible": True,
                "candidate_runtime_visible": True,
                "warm_switch_visible": True,
                "blockers": [],
            },
        },
    )

    assert checkpoint["state"] == "in_progress"
    assert checkpoint["ready"] is False
    assert checkpoint["completed_task_total"] == 1
    assert checkpoint["task_total"] == 2
    assert checkpoint["remaining_tasks"] == ["phase0.runtime_comm_ready"]
    node_browser = checkpoint["tasks"]["phase0.node_browser_ready"]
    runtime_comm = checkpoint["tasks"]["phase0.runtime_comm_ready"]
    assert node_browser["status"] == "done"
    assert node_browser["completed_criteria"] == [
        "browser_member_semantic_channels",
        "yjs_as_sync_channel",
        "browser_yjs_ws_handoff",
    ]
    assert node_browser["pending_reasons"] == []
    assert node_browser["evidence"]["browser_yjs_ws_handoff"]["state"] == "ready"
    assert runtime_comm["status"] == "in_progress"
    assert runtime_comm["completed_criteria"] == [
        "hub_root_class_a_hardening",
        "browser_yjs_ws_handoff",
        "sidecar_continuity",
        "browser_safe_supervisor_continuity",
    ]
    assert runtime_comm["pending_reasons"] == [
        "browser route websocket still terminates in the runtime FastAPI app",
    ]
    assert runtime_comm["evidence"]["hub_root_class_a"]["state"] == "complete"
    assert runtime_comm["evidence"]["sidecar_continuity"]["required"] is False
    assert runtime_comm["evidence"]["sidecar_continuity"]["hub_runtime_update"] == "preserve_sidecar"
    assert runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["state"] == "ready"
    assert runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["carried_by_reliability"] is True
    assert (
        runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["routed_browser_proxy"]["state"]
        == "ready"
    )
    assert (
        runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["routed_browser_proxy"]["source"]
        == "supervisor_public_status"
    )
    assert (
        runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["routed_browser_proxy"]["selected_ws_base"]
        == "ws://127.0.0.1:8777"
    )


def test_supervisor_transition_runtime_snapshot_surfaces_browser_safe_transition_contract(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PORT", "8776")

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
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
                    "transition_role": "member",
                    "transition_mode": "warm_switch",
                    "candidate_slot": "B",
                    "candidate_runtime_url": "http://127.0.0.1:8778",
                    "candidate_runtime_port": 8778,
                    "candidate_runtime_instance_id": "rt-b-1",
                    "candidate_runtime_state": "ready",
                    "candidate_runtime_api_ready": True,
                    "candidate_transition_role": "candidate",
                    "warm_switch_supported": True,
                    "warm_switch_allowed": True,
                    "warm_switch_reason": "warm switch admitted",
                    "member_hub_watchdog": {
                        "last_state": "ready",
                        "last_reason": "member-hub link is connected",
                        "reconnect_total": 2,
                        "cooldown_sec": 20,
                        "verify_timeout_sec": 10,
                    },
                    "required_upstream_link": {
                        "kind": "member_hub",
                        "role": "member",
                        "owner": "supervisor",
                        "state": "ready",
                        "reason": "member-hub link is connected",
                        "ready": True,
                        "visible": True,
                        "desired_state": "connected",
                        "current_owner": "runtime",
                        "planned_owner": "runtime",
                        "future_owner": "sidecar",
                        "continuity_mode": "runtime_bound",
                        "sidecar_enabled": False,
                        "reconnect_total": 2,
                        "cooldown_sec": 20,
                        "verify_timeout_sec": 10,
                        "served_by": "supervisor",
                        "watchdog": {
                            "last_state": "ready",
                        },
                        "blockers": [],
                    },
                },
                "_served_by": "supervisor_fallback",
            }

    class _FakeSession:
        trust_env = True

        def get(self, url, headers=None, timeout=None):
            assert url == "http://127.0.0.1:8776/api/supervisor/public/update-status"
            assert headers == {"Accept": "application/json"}
            assert timeout == 0.35
            return _FakeResponse()

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "requests",
        SimpleNamespace(Session=lambda: _FakeSession()),
    )

    snapshot = supervisor_transition_runtime_snapshot(timeout_sec=0.35)

    assert snapshot["available"] is True
    assert snapshot["source"] == "supervisor.public_update_status"
    assert snapshot["supervisor_url"] == "http://127.0.0.1:8776"
    assert snapshot["_served_by"] == "supervisor_fallback"
    assert snapshot["browser_safe_surface"]["state"] == "ready"
    assert snapshot["browser_safe_surface"]["ready"] is True
    assert snapshot["browser_safe_surface"]["carried_by_reliability"] is True
    assert snapshot["browser_safe_surface"]["transition_state"] == "countdown"
    assert snapshot["browser_safe_surface"]["transition_mode_visible"] is True
    assert snapshot["browser_safe_surface"]["candidate_runtime_visible"] is True
    assert snapshot["browser_safe_surface"]["warm_switch_visible"] is True
    assert snapshot["required_upstream_link"]["kind"] == "member_hub"
    assert snapshot["required_upstream_link"]["state"] == "ready"
    assert snapshot["required_upstream_link"]["ready"] is True
    assert snapshot["required_upstream_link"]["desired_state"] == "connected"
    assert snapshot["required_upstream_link"]["current_owner"] == "runtime"
    assert snapshot["required_upstream_link"]["future_owner"] == "sidecar"
    assert snapshot["required_upstream_link"]["reconnect_total"] == 2


def test_required_upstream_link_is_enriched_with_sidecar_handoff_contract() -> None:
    enriched = _enrich_required_upstream_link_with_sidecar(
        required_upstream_link={
            "kind": "member_hub",
            "role": "member",
            "owner": "supervisor",
            "state": "ready",
            "ready": True,
            "desired_state": "connected",
            "current_owner": "runtime",
            "planned_owner": "runtime",
            "future_owner": "sidecar",
            "continuity_mode": "runtime_bound",
            "blockers": [],
        },
        sidecar_runtime={
            "enabled": True,
            "lifecycle_manager": "supervisor",
            "continuity_contract": {
                "current_support": "planned",
            },
            "route_tunnel_contract": {
                "ws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "handoff_ready": False,
                    "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                },
                "yws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "handoff_ready": True,
                    "blockers": [],
                },
            },
        },
    )

    assert enriched["planned_owner"] == "sidecar"
    assert enriched["continuity_mode"] == "handoff_planned"
    assert enriched["current_support"] == "planned"
    assert enriched["handoff_state"] == "planned"
    assert enriched["handoff_ready"] is False
    assert enriched["recovery_policy"]["on_runtime_restart"] == "runtime_reconnect"
    assert any("browser_events_ws" in item for item in enriched["blockers"])


def test_event_model_phase0_communication_checkpoint_keeps_supervisor_and_optional_continuity_out_of_pending_reasons() -> None:
    checkpoint = _event_model_phase0_communication_checkpoint(
        sync_runtime={
            "channel_contract": {
                "completed_for_scope": True,
            },
        },
        sidecar_runtime={
            "enabled": True,
            "continuity_contract": {
                "required": False,
                "hub_runtime_update": "preserve_sidecar",
                "current_support": "planned",
            },
            "route_tunnel_contract": {
                "current_support": "planned",
                "ownership_boundary": "transport_only",
                "ws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "delegation_mode": "not_implemented",
                    "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                },
                "yws": {
                    "current_owner": "sidecar",
                    "planned_owner": "sidecar",
                    "delegation_mode": "sidecar_tunnel",
                    "handoff_ready": True,
                    "blockers": [],
                },
            },
        },
        hub_root_protocol={
            "hardening_coverage": {
                "state": "complete",
                "covered_flows": 6,
                "total_flows": 6,
            },
            "route_runtime": {
                "local_base_last_source": "runtime_port_env",
                "local_base_last_value": "http://127.0.0.1:8777",
                "local_base_runtime_port_shortcut_total": 1,
            },
        },
        supervisor_runtime={
            "available": True,
            "source": "supervisor.public_update_status",
            "_served_by": "supervisor_fallback",
            "browser_safe_surface": {
                "state": "ready",
                "ready": True,
                "carried_by_reliability": True,
                "transition_state": "countdown",
                "transition_phase": "scheduled",
                "transition_mode_visible": True,
                "candidate_runtime_visible": True,
                "warm_switch_visible": True,
                "blockers": [],
            },
        },
    )

    runtime_comm = checkpoint["tasks"]["phase0.runtime_comm_ready"]

    assert runtime_comm["pending_reasons"] == [
        "browser route websocket still terminates in the runtime FastAPI app",
    ]
    assert "sidecar_continuity" not in runtime_comm["pending_criteria"]
    assert "browser_safe_supervisor_continuity" not in runtime_comm["pending_criteria"]
    assert (
        runtime_comm["evidence"]["browser_safe_supervisor_continuity"]["routed_browser_proxy"]["selection_mode"]
        == "runtime_port_env"
    )


def test_yjs_sync_runtime_snapshot_marks_reconnect_storm_as_pressure(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        SimpleNamespace(
            ystore_runtime_snapshot=lambda **kwargs: {
                "webspace_total": 1,
                "active_webspace_total": 1,
                "webspaces": {
                    "default": {
                        "log_mode": "snapshot_plus_diff",
                        "update_log_entries": 1,
                        "max_update_log_entries": 128,
                        "replay_window_entries": 0,
                        "replay_window_bytes": 0,
                        "compact_total": 1,
                        "runtime_compaction_eligible": False,
                        "backup_fast_path_total": 0,
                        "backup_skipped_total": 0,
                    }
                },
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        SimpleNamespace(
            gateway_transport_snapshot=lambda: {
                "transports": {
                    "yws": {
                        "active_connections": 1,
                        "recent_open_10s": 9,
                        "recent_open_60s": 12,
                        "storm_detected": True,
                        "hot_clients": [{"dev_id": "dev-1", "open_15s": 9}],
                    }
                },
                "servers": {"yws": {"requested": True, "started_event": True, "task_running": True, "ready": True, "room_total": 1}},
                "ownership": {"yws": {}},
            }
        ),
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_selected_webspace_snapshot",
        lambda webspace_id: {"webspace_id": webspace_id or "default"},
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_recovery_policy",
        lambda selected_entry, selected_webspace: ({}, {}, {}),
    )
    monkeypatch.setattr(
        "adaos.services.reliability._build_yjs_webspace_guidance",
        lambda selected_webspace, action_overrides: {},
    )

    snapshot = yjs_sync_runtime_snapshot(role="hub", webspace_id="default")

    assert snapshot["assessment"]["state"] == "pressure"
    assert "browser_yjs_reconnect_storm" in str(snapshot["assessment"]["reason"] or "")
    assert snapshot["transport"]["storm_detected"] is True
    assert snapshot["transport"]["hot_client_total"] == 1


def test_media_plane_runtime_snapshot_exposes_live_update_guard(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.media_library",
        SimpleNamespace(
            media_runtime_snapshot=lambda: {
                "available": True,
                "paths": {
                    "direct_local_http": {"ready": True},
                    "root_routed_http": {"ready": True},
                    "webrtc_tracks": {"ready": True},
                },
                "member_browser_direct": {
                    "ready": True,
                    "admitted": True,
                    "browser_session_total": 1,
                    "connected_browser_session_total": 1,
                },
                "counts": {
                    "live_connected_peers": 0,
                    "incoming_audio_tracks": 0,
                    "incoming_video_tracks": 0,
                    "loopback_audio_tracks": 0,
                    "loopback_video_tracks": 0,
                },
                "route_intent": {"active_route": "member_browser_direct", "preferred_route": "member_browser_direct"},
                "attempt": {"active_route": "member_browser_direct", "preferred_route": "member_browser_direct"},
            }
        ),
    )

    snapshot = media_plane_runtime_snapshot(role="hub", route_mode="hub", connected_to_hub=None)
    guard = snapshot["update_guard"]

    assert guard["live_session_present"] is True
    assert guard["observed_live_topology"] == "member_browser_direct"
    assert guard["member_runtime_update"] == "defer"
    assert guard["hub_runtime_update"] == "preserve_sidecar"
    assert guard["hub_sidecar_continuity_required"] is True
    assert guard["current_support"] == "planned"


def test_member_reliability_snapshot_uses_connected_to_hub_for_route_and_sync() -> None:
    _reset_state()

    disconnected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="none",
        connected_to_hub=False,
    )
    assert disconnected["runtime"]["readiness_tree"]["root_control"]["status"] == "not_applicable"
    assert disconnected["runtime"]["readiness_tree"]["route"]["status"] == "down"
    assert disconnected["runtime"]["readiness_tree"]["sync"]["status"] == "down"

    connected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="ws",
        connected_to_hub=True,
    )
    assert connected["runtime"]["readiness_tree"]["route"]["status"] == "ready"
    assert connected["runtime"]["readiness_tree"]["sync"]["status"] == "ready"
    assert connected["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True
    assert connected["node"]["connected_to_subnet"] is True
    assert connected["node"]["connected_to_hub"] is True
    assert connected["runtime"]["media_runtime"]["transport"]["connected_to_subnet"] is True
    assert connected["runtime"]["media_runtime"]["transport"]["connected_to_hub"] is True


def test_member_transport_evidence_exposes_connected_to_subnet_alias() -> None:
    evidence = _hub_member_transport_evidence_snapshot(
        role="member",
        route_mode="p2p",
        connected_to_hub=False,
        hub_root_protocol={},
    )

    member_link = evidence["member_link_ws"]
    assert member_link["connected_to_subnet"] is False
    assert member_link["connected_to_hub"] is False


def test_node_reliability_endpoint_exposes_model_and_runtime_state(monkeypatch) -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    fake_bootstrap = types.ModuleType("adaos.services.bootstrap")
    fake_bootstrap.is_ready = lambda: True
    fake_bootstrap.load_config = lambda: SimpleNamespace(node_id="node-1", subnet_id="sn_1", role="hub")
    fake_bootstrap.request_hub_root_reconnect = lambda *args, **kwargs: {"ok": True}
    fake_bootstrap.request_member_hub_reconnect = lambda *args, **kwargs: {"ok": True, "accepted": True}
    fake_bootstrap.request_hub_root_route_reset = lambda *args, **kwargs: {"ok": True}

    async def _fake_switch_role(*args, **kwargs):
        return fake_bootstrap.load_config()

    fake_bootstrap.switch_role = _fake_switch_role
    monkeypatch.setitem(sys.modules, "adaos.services.bootstrap", fake_bootstrap)

    fake_link_client_mod = types.ModuleType("adaos.services.subnet.link_client")
    fake_link_client_mod.get_member_link_client = lambda: SimpleNamespace(is_connected=lambda: False)
    monkeypatch.setitem(sys.modules, "adaos.services.subnet.link_client", fake_link_client_mod)

    sys.modules.pop("adaos.apps.api.node_api", None)
    node_api = importlib.import_module("adaos.apps.api.node_api")
    require_token = importlib.import_module("adaos.apps.api.auth").require_token

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    monkeypatch.setattr(
        node_api,
        "current_reliability_payload",
        lambda: reliability_snapshot(
            node_id="node-1",
            subnet_id="sn_1",
            role="hub",
            local_ready=True,
            node_state="ready",
            draining=False,
            route_mode="hub",
            connected_to_hub=None,
        ),
    )

    client = TestClient(app)
    response = client.get("/api/node/reliability")
    assert response.status_code == 200

    payload = response.json()
    assert payload["model"]["authority_boundaries"]["sidecar"]["must_not_own"]
    assert payload["runtime"]["readiness_tree"]["root_control"]["status"] == "ready"
    assert payload["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True
    assert payload["runtime"]["connectivity"]["required_upstream_link"]["kind"] == "hub_root"
    assert payload["runtime"]["state_sync"]["replay"]["mode"] == "snapshot_plus_diff"
    assert payload["runtime"]["yjs_pressure"]["target"] == "primary_shared_doc"


def test_node_reliability_summary_endpoint_returns_compact_runtime_snapshot(monkeypatch) -> None:
    from adaos.apps.api.node_api import require_token, router

    monkeypatch.setattr(
        "adaos.apps.api.node_api.current_reliability_payload",
        lambda webspace_id=None: {
            "runtime": {
                "hub_root_protocol": {
                    "hardening_coverage": {
                        "state": "complete",
                        "covered_flows": 6,
                        "total_flows": 6,
                        "flows": [{"id": "flow-1"}],
                    }
                },
                "sidecar_runtime": {
                    "enablement": {
                        "enabled": True,
                        "default_enabled": False,
                        "explicit": True,
                        "source": "env_override",
                        "env_var": "ADAOS_REALTIME_ENABLE",
                        "env_value": "1",
                        "role": "hub",
                        "reason": "ADAOS_REALTIME_ENABLE=1",
                    },
                    "continuity_contract": {
                        "current_support": "ready",
                        "hub_runtime_update": "preserve_sidecar",
                        "required": True,
                        "pending_boundaries": [],
                        "ready_boundaries": ["browser_yjs_ws"],
                        "blockers": [],
                    },
                    "progress": {
                        "state": "complete",
                        "percent": 100,
                        "completed_milestones": 4,
                        "milestone_total": 4,
                        "current_milestone": "done",
                    },
                    "route_tunnel_contract": {
                        "current_support": "ready",
                        "ownership_boundary": "sidecar",
                        "ws": {"current_owner": "sidecar", "handoff_ready": True, "blockers": []},
                        "yws": {"current_owner": "sidecar", "handoff_ready": True, "blockers": []},
                    },
                },
                "supervisor_runtime": {
                    "status": {"state": "countdown", "phase": "scheduled"},
                    "runtime": {"transition_mode": "warm_switch"},
                },
                "connectivity": {
                    "required_upstream_link": {
                        "kind": "hub_root",
                        "scope_id": "node-1",
                        "transport_state": "ready",
                        "transition_state": "waiting_restart",
                        "planned_transition": {"active": True, "reason": "update"},
                        "reason": None,
                        "blockers": [],
                        "served_by": "supervisor_fallback",
                    },
                    "browser_control_route": {
                        "kind": "browser_control_route",
                        "scope_id": "node-1",
                        "transport_state": "degraded",
                        "transition_state": "reconnecting",
                        "planned_transition": {"active": False, "reason": None},
                        "reason": "flapping",
                        "blockers": ["route.flapping"],
                        "served_by": "runtime",
                    },
                },
                "state_sync": {
                    "webspace_id": "desktop",
                    "transport_state": "attached",
                    "first_sync_state": "complete",
                    "semantic_state": "ready",
                    "freshness_state": "fresh",
                    "last_good_sync_at": 1778055331.0,
                    "last_materialization_at": 1778055331.0,
                    "replay": {"mode": "snapshot_plus_diff", "cursor": "3/32"},
                    "fallback_mode": "off",
                    "blockers": [],
                },
                "yjs_pressure": {
                    "webspace_id": "desktop",
                    "owner": "_by_owner/skill_infrastate_skill",
                    "recent_bytes": 167296,
                    "recent_writes": 1,
                    "peak_bps": 167296.0,
                    "peak_wps": 1.0,
                    "policy_state": "warn",
                    "target": "primary_shared_doc",
                    "reason": "write_amplification",
                    "blocked_roots": [],
                    "observed_state": "critical",
                },
                "event_model_phase0_communication": {
                    "state": "complete",
                    "ready": True,
                    "tracked_tasks": ["phase0.node_browser_ready"],
                    "completed_task_total": 2,
                    "task_total": 2,
                    "remaining_tasks": [],
                    "tasks": {
                        "phase0.node_browser_ready": {
                            "id": "phase0.node_browser_ready",
                            "status": "done",
                            "summary": "ready",
                            "completed_criteria": ["browser_member_semantic_channels"],
                            "pending_criteria": [],
                            "pending_reasons": [],
                            "evidence": {},
                        }
                    },
                },
            }
        },
    )

    app = FastAPI()
    app.dependency_overrides[require_token] = lambda: True
    app.include_router(router, prefix="/api/node")
    client = TestClient(app)

    response = client.get("/api/node/reliability/summary")
    assert response.status_code == 200
    payload = response.json()

    assert "runtime" not in payload
    assert "model" not in payload
    assert payload["source"] == "api.node.reliability.summary"
    assert payload["hubRootHardening"]["coveredFlows"] == 6
    assert payload["sidecarEnablement"]["enabled"] is True
    assert payload["sidecarEnablement"]["defaultEnabled"] is False
    assert payload["sidecarEnablement"]["source"] == "env_override"
    assert payload["sidecarContinuity"]["currentSupport"] == "ready"
    assert payload["browserYwsHandoffReady"] is True
    assert payload["connectivity"]["requiredUpstreamLink"]["transitionState"] == "waiting_restart"
    assert payload["stateSync"]["replay"]["cursor"] == "3/32"
    assert payload["yjsPressure"]["policyState"] == "warn"
    assert payload["phase0Communication"]["tasks"]["nodeBrowserReady"]["status"] == "done"


def test_node_reliability_summary_thin_mode_uses_status_cards(monkeypatch) -> None:
    from adaos.apps.api import node_api
    from adaos.apps.api.node_api import require_token, router
    from adaos.services.status_card_registry import clear_status_card_registry, publish_status_card

    clear_status_card_registry()
    monkeypatch.setattr(node_api, "_local_node_id", lambda: "node-a")
    monkeypatch.setattr(
        node_api,
        "runtime_lifecycle_snapshot",
        lambda: {
            "node_state": "ready",
            "accepting_new_work": True,
            "draining": False,
            "reason": None,
        },
    )

    app = FastAPI()
    app.dependency_overrides[require_token] = lambda: True
    app.include_router(router, prefix="/api/node")
    client = TestClient(app)

    first = client.get("/api/node/reliability/summary", params={"webspace_id": "desktop", "mode": "thin"})
    unchanged = client.get(
        "/api/node/reliability/summary",
        params={"webspace_id": "desktop", "mode": "thin", "since_version": 1},
    )
    publish_status_card(
        id="infrastate-yjs",
        owner="skill:infrastate_skill",
        kind="yjs",
        scope={"section": "yjs"},
        webspace_id="desktop",
        status="warning",
        summary="Yjs pressure warn",
        updated_at=20.0,
    )
    changed = client.get(
        "/api/node/reliability/summary",
        params={"webspace_id": "desktop", "mode": "thin", "since_version": 1},
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["source"] == "api.node.reliability.summary.thin"
    assert payload["mode"] == "thin"
    assert payload["cardTotal"] == 1
    assert payload["maxVersion"] == 1
    assert payload["registryVersion"] == 1
    assert payload["cache"] == {
        "key": "status-card-registry:desktop",
        "version": 1,
        "etag": 'W/"status-card-registry:desktop:1"',
        "sinceParam": "since_version",
        "modeParam": "mode=thin",
        "unchanged": False,
    }
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["etag"] == 'W/"status-card-registry:desktop:1"'
    assert first.headers["x-adaos-cache-key"] == "status-card-registry:desktop"
    assert first.headers["x-adaos-registry-version"] == "1"
    assert payload["cards"][0]["id"] == "runtime"
    assert payload["cards"][0]["cacheKey"] == "status-card:desktop:runtime"
    assert payload["cards"][0]["summary"] == "Runtime ready"
    assert payload["stats"]["last_publish_latency_ms"] is not None
    assert unchanged.status_code == 200
    assert unchanged.json()["unchanged"] is True
    assert unchanged.json()["cards"] == []
    assert changed.status_code == 200
    assert changed.json()["unchanged"] is False
    assert changed.json()["registryVersion"] == 2
    assert changed.json()["cache"]["version"] == 2
    assert changed.headers["etag"] == 'W/"status-card-registry:desktop:2"'
    assert {card["id"] for card in changed.json()["cards"]} == {"runtime", "infrastate-yjs"}


def test_state_sync_keeps_ready_semantics_for_bounded_replay_maintenance_pressure() -> None:
    snapshot = _state_sync_snapshot(
        {
            "available": True,
            "selected_webspace_id": "desktop",
            "assessment": {
                "state": "pressure",
                "reason": "bounded_replay_window_near_limit",
            },
            "transport": {
                "server_ready": True,
                "active_yws_connections": 1,
            },
            "channel_contract": {
                "recovery_model": "snapshot_plus_diff",
            },
            "selected_webspace": {
                "webspace_id": "desktop",
                "rebuild": {
                    "finished_at": 1778055331.0,
                    "materialization": {
                        "ready": True,
                        "readiness_state": "ready",
                        "missing_branches": [],
                    },
                },
            },
            "webspaces": {
                "desktop": {
                    "replay_window_entries": 32,
                    "replay_window_limit": 32,
                },
            },
        }
    )

    assert snapshot["transport_state"] == "attached"
    assert snapshot["first_sync_state"] == "complete"
    assert snapshot["semantic_state"] == "ready"
    assert snapshot["freshness_state"] == "fresh"
    assert snapshot["replay"]["cursor"] == "32/32"
    assert snapshot["blockers"] == ["bounded_replay_window_near_limit"]


def test_replay_pressure_compaction_request_schedules_background_task(monkeypatch) -> None:
    async def _run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class _Store:
            async def request_runtime_compaction(self, **kwargs):
                calls.append(("desktop", dict(kwargs)))
                return True

        monkeypatch.setenv("ADAOS_YSTORE_AUTOCOMPACT_REPLAY_PRESSURE_QUIET_SEC", "0.25")
        monkeypatch.setitem(
            sys.modules,
            "adaos.services.yjs.store",
            SimpleNamespace(get_ystore_for_webspace=lambda webspace_id: _Store()),
        )

        requested = _request_yjs_replay_pressure_compaction(
            "desktop",
            {
                "runtime_compaction_eligible": True,
                "replay_window_entries": 32,
                "replay_window_limit": 32,
            },
            assessment_state="pressure",
            reasons=["bounded_replay_window_near_limit"],
        )

        assert requested is True
        await asyncio.sleep(0)
        assert calls == [
            (
                "desktop",
                {
                    "reason": "replay_pressure",
                    "min_quiet_sec": 0.25,
                },
            )
        ]

    asyncio.run(_run())


def test_replay_pressure_compaction_request_runs_without_event_loop(monkeypatch) -> None:
    done = threading.Event()
    calls: list[tuple[str, dict[str, object]]] = []

    class _Store:
        async def request_runtime_compaction(self, **kwargs):
            calls.append(("desktop", dict(kwargs)))
            done.set()
            return True

    monkeypatch.setenv("ADAOS_YSTORE_AUTOCOMPACT_REPLAY_PRESSURE_QUIET_SEC", "0.25")
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        SimpleNamespace(get_ystore_for_webspace=lambda webspace_id: _Store()),
    )

    requested = _request_yjs_replay_pressure_compaction(
        "desktop",
        {
            "runtime_compaction_eligible": True,
            "replay_window_entries": 32,
            "replay_window_limit": 32,
            "last_write_ago_s": 10.0,
            "auto_backup_inflight": False,
        },
        assessment_state="pressure",
        reasons=["bounded_replay_window_near_limit"],
    )

    assert requested is True
    assert done.wait(timeout=2.0)
    assert calls == [
        (
            "desktop",
            {
                "reason": "replay_pressure",
                "min_quiet_sec": 0.25,
            },
        )
    ]


def test_replay_pressure_compaction_request_respects_snapshot_quiet_gate(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_AUTOCOMPACT_REPLAY_PRESSURE_QUIET_SEC", "2.0")

    requested = _request_yjs_replay_pressure_compaction(
        "desktop",
        {
            "runtime_compaction_eligible": True,
            "replay_window_entries": 32,
            "replay_window_limit": 32,
            "last_write_ago_s": 0.25,
            "auto_backup_inflight": False,
        },
        assessment_state="pressure",
        reasons=["bounded_replay_window_near_limit"],
    )

    assert requested is False


def test_node_members_endpoint_returns_hub_member_connection_state(monkeypatch) -> None:
    from adaos.apps.api.node_api import require_token, router

    monkeypatch.setattr(
        "adaos.apps.api.node_api.load_config",
        lambda: SimpleNamespace(node_id="hub-1", subnet_id="sn_1", role="hub", node_names=[]),
    )
    monkeypatch.setattr("adaos.apps.api.node_api.route_info", lambda role: ("hub", None))
    monkeypatch.setattr("adaos.apps.api.node_api.runtime_lifecycle_snapshot", lambda: {"node_state": "ready", "draining": False})
    monkeypatch.setattr("adaos.apps.api.node_api.is_ready", lambda: True)
    monkeypatch.setattr(
        "adaos.apps.api.node_api.reliability_snapshot",
        lambda **kwargs: {
            "runtime": {
                "hub_member_connection_state": {
                    "assessment": {"state": "nominal", "reason": "linked"},
                    "member_total": 1,
                    "known_total": 1,
                }
            }
        },
    )

    app = FastAPI()
    app.dependency_overrides[require_token] = lambda: True
    app.include_router(router, prefix="/api/node")
    client = TestClient(app)

    response = client.get("/api/node/members")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["hub_member_connection_state"]["member_total"] == 1
    assert payload["hub_member_connection_state"]["assessment"]["state"] == "nominal"


def test_reliability_snapshot_times_out_slow_sync_and_media_sections(monkeypatch) -> None:
    _reset_state()

    def _slow_sync(*, role: str, webspace_id: str | None = None):
        import time as _time

        _time.sleep(0.2)
        return {"available": True, "assessment": {"state": "nominal", "reason": "ok"}}

    def _slow_media(*, role: str, route_mode: str | None, connected_to_hub: bool | None):
        import time as _time

        _time.sleep(0.2)
        return {"available": True, "assessment": {"state": "nominal", "reason": "ok"}}

    monkeypatch.setattr("adaos.services.reliability.yjs_sync_runtime_snapshot", _slow_sync)
    monkeypatch.setattr("adaos.services.reliability.media_plane_runtime_snapshot", _slow_media)
    monkeypatch.setenv("ADAOS_RELIABILITY_RUNTIME_SECTION_TIMEOUT_SEC", "0.05")

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["sync_runtime"]["available"] is False
    assert snapshot["runtime"]["sync_runtime"]["_timed_out"] is True
    assert snapshot["runtime"]["media_runtime"]["available"] is False
    assert snapshot["runtime"]["media_runtime"]["_timed_out"] is True


def test_node_reliability_cli_prints_runtime_summary(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(
        node_cli,
        "_control_get_json",
        lambda **kwargs: (
            200,
            {
                "node": {"node_id": "node-1", "role": "hub", "ready": True, "node_state": "ready"},
                "runtime": {
                    "readiness_tree": {
                        "hub_local_core": {"status": "ready"},
                        "root_control": {"status": "ready"},
                        "route": {"status": "degraded"},
                        "sync": {"status": "ready"},
                        "media": {"status": "unknown"},
                        "integration": {
                            "telegram": {"status": "ready"},
                            "github": {"status": "degraded"},
                            "llm": {"status": "unknown"},
                        },
                    },
                    "channel_diagnostics": {
                        "root_control": {"stability": {"state": "flapping", "score": 62}, "recent_non_ready_transitions_5m": 2},
                        "route": {"stability": {"state": "degraded", "score": 71}, "recent_non_ready_transitions_5m": 1},
                    },
                    "degraded_matrix": {
                        "new_root_backed_member_admission": {"allowed": True},
                        "root_routed_browser_proxy": {"allowed": False},
                        "telegram_action_completion": {"allowed": True},
                        "github_action_completion": {"allowed": False},
                        "llm_action_completion": {"allowed": False},
                        "core_update_coordination_via_root": {"allowed": True},
                    },
                    "connectivity": {
                        "required_upstream_link": {
                            "kind": "hub_root",
                            "transport_state": "ready",
                            "transition_state": "ready",
                            "planned_transition": {"active": False, "reason": None},
                            "served_by": "supervisor",
                        },
                        "browser_control_route": {
                            "kind": "browser_control_route",
                            "transport_state": "degraded",
                            "transition_state": "reconnecting",
                            "planned_transition": {"active": False, "reason": "flapping"},
                            "served_by": "runtime",
                        },
                    },
                    "state_sync": {
                        "webspace_id": "desktop",
                        "transport_state": "attached",
                        "first_sync_state": "complete",
                        "semantic_state": "degraded",
                        "freshness_state": "aging",
                        "replay": {"mode": "snapshot_plus_diff", "cursor": "3/32"},
                        "fallback_mode": "off",
                        "blockers": ["bounded_replay_window_near_limit"],
                    },
                    "yjs_pressure": {
                        "webspace_id": "desktop",
                        "owner": "_by_owner/skill_infrastate_skill",
                        "observed_state": "high",
                        "policy_state": "warn",
                        "recent_bytes": 65536,
                        "recent_writes": 2,
                        "peak_bps": 65536.0,
                        "peak_wps": 2.0,
                        "reason": "write_amplification",
                        "throttled_total": 3,
                        "blocked_total": 1,
                        "last_policy_state": "block",
                        "last_reason": "write_amplification_blocked",
                        "last_path": "data/infrastate",
                    },
                },
            },
        ),
    )

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert "root_control: ready" in result.output
    assert "integration.telegram: ready" in result.output
    assert "diag.root_control: flapping score=62 recent_non_ready_5m=2" in result.output
    assert "root_routed_browser_proxy: blocked" in result.output
    assert "connectivity.required_upstream_link: kind=hub_root transport=ready transition=ready" in result.output
    assert "state_sync: webspace=desktop transport=attached first_sync=complete semantic=degraded freshness=aging" in result.output
    assert "yjs_pressure: webspace=desktop owner=_by_owner/skill_infrastate_skill state=high policy=warn" in result.output
    assert "throttled=3 blocked=1" in result.output
    assert "yjs_pressure.last: policy=block reason=write_amplification_blocked path=data/infrastate" in result.output


def test_node_reliability_cli_prints_sidecar_scope_and_sync_owner(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(
        node_cli,
        "_control_get_json",
        lambda **kwargs: (
            200,
            {
                "node": {"node_id": "node-1", "role": "hub", "ready": True, "node_state": "ready"},
                "runtime": {
                    "readiness_tree": {},
                    "sidecar_runtime": {
                        "phase": "nats_transport_sidecar",
                        "enabled": True,
                        "status": "ready",
                        "transport_owner": "sidecar",
                        "lifecycle_manager": "supervisor",
                        "local_listener_state": "ready",
                        "remote_session_state": "ready",
                        "control_ready": "ready",
                        "route_ready": "not_owned",
                        "transport_ready": True,
                        "local_url": "nats://127.0.0.1:7422",
                        "diag_age_s": 1.5,
                        "transport_provenance": {
                            "remote_connect_total": 2,
                            "remote_connect_fail_total": 0,
                            "superseded_total": 0,
                        },
                        "process": {"listener_pid": 12345},
                        "scope": {
                            "planned_next_boundaries": ["browser_events_ws", "browser_yjs_ws"],
                        },
                        "continuity_contract": {
                            "required": False,
                            "current_support": "planned",
                            "hub_runtime_update": "preserve_sidecar",
                        },
                        "progress": {
                            "target": "first_browser_realtime_tunnel",
                            "state": "in_progress",
                            "completed_milestones": 2,
                            "milestone_total": 4,
                            "percent": 50,
                            "current_milestone": "browser_events_ws_handoff",
                            "next_blocker": "browser route websocket still terminates in the runtime FastAPI app",
                        },
                        "route_tunnel_contract": {
                            "current_support": "planned",
                            "ownership_boundary": "transport_only",
                            "ws": {
                                "current_owner": "runtime",
                                "planned_owner": "sidecar",
                                "delegation_mode": "not_implemented",
                                "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                            },
                            "yws": {
                                "current_owner": "sidecar",
                                "planned_owner": "sidecar",
                                "delegation_mode": "sidecar_tunnel",
                                "handoff_ready": True,
                                "blockers": [],
                            },
                        },
                    },
                    "supervisor_runtime": {
                        "available": True,
                        "source": "supervisor.public_update_status",
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
                            "transition_mode": "warm_switch",
                            "candidate_runtime_state": "ready",
                            "warm_switch_reason": "warm switch admitted",
                        },
                        "_served_by": "supervisor_fallback",
                        "browser_safe_surface": {
                            "state": "ready",
                            "ready": True,
                            "carried_by_reliability": True,
                            "transition_state": "countdown",
                            "transition_phase": "scheduled",
                            "transition_mode_visible": True,
                            "candidate_runtime_visible": True,
                            "warm_switch_visible": True,
                            "blockers": [],
                        },
                        "required_upstream_link": {
                            "kind": "member_hub",
                            "owner": "supervisor",
                            "state": "ready",
                            "ready": True,
                            "desired_state": "connected",
                            "current_owner": "runtime",
                            "planned_owner": "runtime",
                            "future_owner": "sidecar",
                            "continuity_mode": "runtime_bound",
                            "reconnect_total": 2,
                            "served_by": "supervisor_fallback",
                            "blockers": [],
                        },
                    },
                    "sync_runtime": {
                        "assessment": {"state": "nominal"},
                        "channel_contract": {
                            "channel_type": "sync_channel",
                            "recovery_model": "snapshot_plus_diff",
                            "replay_window": "bounded",
                            "awareness_semantics": "ephemeral",
                            "browser_local_persistence": "optional_indexeddb",
                            "completed_for_scope": True,
                        },
                        "webspace_total": 1,
                        "active_webspace_total": 1,
                        "compacted_webspace_total": 0,
                        "compaction_eligible_webspace_total": 1,
                        "update_log_total": 3,
                        "replay_window_total": 2,
                        "replay_window_byte_total": 512,
                        "webspaces": {
                            "default": {
                                "log_mode": "snapshot_plus_diff",
                                "update_log_entries": 3,
                                "max_update_log_entries": 128,
                            }
                        },
                        "transport": {
                            "active_yws_connections": 2,
                            "room_total": 1,
                            "room_cold_open_total": 2,
                            "room_reuse_total": 3,
                            "room_single_pass_bootstrap_total": 2,
                            "storm_detected": False,
                            "owner": "runtime",
                            "planned_owner": "sidecar",
                            "recent_open_10s": 1,
                            "reload_recent_60s": 4,
                            "reload_command_total": 5,
                            "reload_duplicate_total": 3,
                            "reset_recent_60s": 2,
                            "reset_command_total": 4,
                            "reset_duplicate_total": 1,
                            "last_reload_client": "http:/api/node/yjs/webspaces/default/reload:127.0.0.1:53301",
                            "last_reload_webspace_id": "default",
                            "last_reload_age_s": 1.25,
                            "last_reload_duplicate_recent": True,
                            "last_reload_fingerprint": "abc123def456",
                            "last_reset_client": "events_ws:127.0.0.1:54421",
                            "last_reset_webspace_id": "default",
                            "last_reset_age_s": 0.75,
                            "last_reset_duplicate_recent": False,
                            "last_reset_fingerprint": "rst123def456",
                        },
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
                    },
                    "event_model_phase0_communication": {
                        "state": "in_progress",
                        "ready": False,
                        "tracked_tasks": [
                            "phase0.node_browser_ready",
                            "phase0.runtime_comm_ready",
                        ],
                        "completed_task_total": 1,
                        "task_total": 2,
                        "remaining_tasks": ["phase0.runtime_comm_ready"],
                        "tasks": {
                            "phase0.node_browser_ready": {
                                "id": "phase0.node_browser_ready",
                                "status": "done",
                                "pending_reasons": [],
                                "evidence": {
                                    "yjs_sync_channel_ready": True,
                                    "browser_yjs_ws_handoff": {
                                        "state": "ready",
                                        "owner": "sidecar",
                                        "planned_owner": "sidecar",
                                        "blocker": None,
                                    },
                                },
                            },
                            "phase0.runtime_comm_ready": {
                                "id": "phase0.runtime_comm_ready",
                                "status": "in_progress",
                                "pending_reasons": [
                                    "browser route websocket still terminates in the runtime FastAPI app",
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
                                        "state": "ready",
                                        "owner": "sidecar",
                                        "planned_owner": "sidecar",
                                    },
                                    "sidecar_continuity": {
                                        "state": "planned",
                                        "required": False,
                                        "hub_runtime_update": "preserve_sidecar",
                                    },
                                    "browser_safe_supervisor_continuity": {
                                        "state": "ready",
                                        "routed_browser_proxy": {
                                            "state": "ready",
                                            "source": "supervisor_public_status",
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "media_runtime": {
                        "assessment": {"state": "nominal"},
                        "counts": {
                            "file_total": 0,
                            "total_bytes": 0,
                            "live_peer_total": 1,
                            "live_connected_peers": 1,
                        },
                        "paths": {
                            "direct_local_http": {"ready": True},
                            "root_routed_http": {"ready": True, "playback": "full"},
                            "webrtc_tracks": {"ready": True},
                        },
                        "transport": {
                            "control_readiness_impact": "none",
                        },
                        "update_guard": {
                            "live_session_present": True,
                            "criticality": "member_live_media",
                            "member_runtime_update": "defer",
                            "hub_runtime_update": "preserve_sidecar",
                            "current_support": "planned",
                            "observed_live_topology": "member_browser_direct",
                        },
                    },
                },
            },
        ),
    )

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert "owner=sidecar manager=supervisor" in result.output
    assert "continuity=planned:preserve_sidecar" in result.output
    assert "next=browser_events_ws,browser_yjs_ws" in result.output
    assert "sidecar.progress: target=first_browser_realtime_tunnel state=in_progress done=2/4 percent=50 current=browser_events_ws_handoff" in result.output
    assert "sidecar.progress.blocker: browser route websocket still terminates in the runtime FastAPI app" in result.output
    assert "sidecar.route_tunnel: support=planned boundary=transport_only" in result.output
    assert "ws=runtime->sidecar:not_implemented" in result.output
    assert "yws=sidecar->sidecar:sidecar_tunnel" in result.output
    assert "status=ready session=-" in result.output
    assert "sidecar.route_tunnel.ws_blocker: browser route websocket still terminates in the runtime FastAPI app" in result.output
    assert "sidecar.route_tunnel.yws_blocker" not in result.output
    assert "eligible=1" in result.output
    assert "replay=2/512B" in result.output
    assert "reloads=4/5 dup=3 resets=2/4 rdup=1" in result.output
    assert "sync_runtime.reload_last: client=http:/api/node/yjs/webspaces/default/reload:127.0.0.1:53301" in result.output
    assert "sync_runtime.reset_last: client=events_ws:127.0.0.1:54421" in result.output
    assert "sync_runtime.contract: type=sync_channel recovery=snapshot_plus_diff replay=bounded awareness=ephemeral persistence=optional_indexeddb done=yes" in result.output
    assert "sync_runtime.boundaries: selector=shared:web_desktop effective=runtime:ready compat=runtime:fallback_cache transport=runtime->sidecar" in result.output
    assert "rooms=1 opens=2/3 single=2 storm=no" in result.output
    assert "owner=runtime->sidecar" in result.output
    assert "event_model.phase0.communication: state=in_progress done=1/2 open=phase0.runtime_comm_ready" in result.output
    assert "event_model.phase0.node_browser_ready: status=done yjs=yes yws=ready owner=sidecar->sidecar" in result.output
    assert "event_model.phase0.runtime_comm_ready: status=in_progress class_a=complete:6/6 ws=planned yws=ready continuity=planned supervisor=ready route-supervisor=ready:supervisor_public_status" in result.output
    assert "event_model.phase0.runtime_comm_ready.blockers: browser route websocket still terminates in the runtime FastAPI app" in result.output
    assert "supervisor_runtime: available=True state=countdown phase=scheduled mode=warm_switch candidate=ready warm_switch=warm switch admitted surface=ready served_by=supervisor_fallback" in result.output
    assert "supervisor_runtime.upstream_link: kind=member_hub owner=supervisor state=ready desired=connected current_owner=runtime planned_owner=sidecar continuity=handoff_planned support=planned handoff=planned restart_policy=runtime_reconnect ready=True reconnects=2 served_by=supervisor_fallback" in result.output
    assert "media.update_guard: live=yes" in result.output
    assert "member=defer hub=preserve_sidecar" in result.output


def test_node_reliability_cli_reports_timeout_detail(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(
        node_cli,
        "_control_get_json",
        lambda **kwargs: (None, {"error": "timeout", "detail": "Read timed out"}),
    )

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 2
    assert "timed out" in result.output


def test_node_reliability_cli_falls_back_to_supervisor_transition(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))

    calls: list[str] = []

    def _fake_get_json(**kwargs):
        calls.append(str(kwargs.get("path") or ""))
        if kwargs.get("path") == "/api/node/reliability":
            return None, {"error": "connection_error", "detail": "connection refused"}
        if kwargs.get("path") == "/api/supervisor/public/memory-status":
            return (
                200,
                {
                    "ok": True,
                    "memory": {
                        "current_profile_mode": "normal",
                        "profile_control_mode": "phase2_supervisor_restart",
                        "suspicion_state": "idle",
                        "sessions_total": 1,
                        "last_session": {
                            "session_id": "mem-001",
                            "session_state": "planned",
                            "profile_mode": "sampled_profile",
                            "publish_state": "local_only",
                        },
                    },
                },
            )
        return (
            200,
            {
                "ok": True,
                "status": {
                    "state": "succeeded",
                    "phase": "root_promoted",
                    "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
                },
                "attempt": {"state": "awaiting_root_restart"},
                "runtime": {"active_slot": "A"},
            },
        )

    monkeypatch.setattr(node_cli, "_control_get_json", _fake_get_json)

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert "/api/node/reliability" in calls
    assert "/api/supervisor/public/update-status" in calls
    assert "/api/supervisor/public/memory-status" in calls
    assert "runtime_restarting_under_supervisor: yes" in result.output
    assert "supervisor.attempt: awaiting_root_restart" in result.output
    assert "supervisor.memory: mode=normal control=phase2_supervisor_restart suspicion=idle sessions=1" in result.output


def test_node_reliability_cli_falls_back_to_supervisor_runtime_candidate(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(node_cli, "_resolve_node_control_base_url", lambda explicit=None: "http://127.0.0.1:8778")

    calls: list[tuple[str, str]] = []

    def _fake_get_json(**kwargs):
        control = str(kwargs.get("control") or "")
        path = str(kwargs.get("path") or "")
        calls.append((control, path))
        if path == "/api/supervisor/public/update-status":
            return (
                200,
                {
                    "ok": True,
                    "runtime": {
                        "runtime_url": "http://127.0.0.1:8777",
                        "candidate_runtime_url": "http://127.0.0.1:8778",
                    },
                },
            )
        if path == "/api/node/reliability" and control == "http://127.0.0.1:8778":
            return None, {"error": "connection_error", "detail": "connection refused"}
        if path == "/api/node/reliability" and control == "http://127.0.0.1:8777":
            return (
                200,
                {
                    "ok": True,
                    "node": {"node_id": "node-1", "role": "hub", "ready": True, "node_state": "ready"},
                    "runtime": {},
                },
            )
        return 404, {"detail": "unexpected"}

    monkeypatch.setattr(node_cli, "_control_get_json", _fake_get_json)

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert ("http://127.0.0.1:8778", "/api/node/reliability") in calls
    assert ("http://127.0.0.1:8776", "/api/supervisor/public/update-status") in calls
    assert ("http://127.0.0.1:8777", "/api/node/reliability") in calls
    assert "reliability.control: requested=http://127.0.0.1:8778 selected=http://127.0.0.1:8777 reason=supervisor_runtime_fallback" in result.output
