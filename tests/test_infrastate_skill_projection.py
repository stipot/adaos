from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
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


def _load_infrastate_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "infrastate_skill" / "handlers" / "main.py"
    module_name = f"test_infrastate_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_infrastate_yjs_tabs_do_not_self_reference_sync_runtime():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"

    reliability = {
        "runtime": {
            "sync_runtime": {
                "webspaces": {
                    "default": {
                        "log_mode": "snapshot_plus_diff",
                        "update_log_entries": 1,
                        "replay_window_entries": 1,
                    }
                }
            }
        }
    }

    selected = mod._selected_yjs_webspace_id({}, reliability)
    items = mod._yjs_webspace_tabs(_Conf(), {}, reliability, {"kind": "local"})

    assert selected == "default"
    assert items
    assert items[0]["id"] == "default"


def test_infrastate_node_label_skips_webspace_like_noise():
    mod = _load_infrastate_module()

    label = mod._node_label(["default", "desktop", {"WEBSPACE_ID": "DEFAULT"}, "TE1"], fallback="hub")

    assert label == "TE1"


def test_infrastate_node_tabs_keep_offline_member_selected():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"
        node_names = ["Hub"]

    reliability = {
        "runtime": {
            "hub_member_connection_state": {
                "known_members": [
                    {
                        "node_id": "member-1",
                        "node_names": ["TE1"],
                        "connected": False,
                        "state": "offline",
                        "observed_via": "subnet_directory",
                    }
                ]
            }
        }
    }

    tabs, selected = mod._node_tabs(_Conf(), {"selected_node_id": "member-1"}, reliability)
    local_tab = next(item for item in tabs if item["id"] == "hub-1")
    member_tab = next(item for item in tabs if item["id"] == "member-1")

    assert any(item["id"] == "member-1" for item in tabs)
    assert selected["node_id"] == "member-1"
    assert selected["kind"] == "member"
    assert selected["connected"] is False
    assert local_tab["node_status"] == "online"
    assert member_tab["node_status"] == "offline"


def test_infrastate_compact_summary_keeps_full_description():
    mod = _load_infrastate_module()

    description = "state=" + ("ready|" * 1000)
    compact = mod._compact_summary_for_yjs({"description": description})

    assert compact["description"] == description
    assert "truncated; full diagnostics" not in compact["description"]


def test_infrastate_compact_snapshot_excludes_yjs_controls():
    mod = _load_infrastate_module()

    compact = mod._compact_snapshot_for_yjs(
        {
            "summary": {"description": "ok"},
            "core_actions": [{"id": "refresh", "title": "Refresh"}],
            "yjs_actions": [{"id": "yjs_reset", "title": "Yjs reset"}],
            "yjs_webspaces": [{"id": "default", "label": "default *"}],
        }
    )

    assert "core_actions" in compact
    assert "yjs_actions" not in compact
    assert "yjs_webspaces" not in compact


def test_infrastate_update_actions_use_member_label():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    reliability = {
        "runtime": {
            "hub_member_connection_state": {
                "known_members": [
                    {
                        "node_id": "member-1",
                        "node_label": "Edge One",
                    }
                ]
            }
        }
    }

    items = mod._update_actions(_Conf(), {"selected_node_id": "member-1"}, reliability)

    assert items
    assert items[0]["title"] == "Update skills & scenarios (Edge One)"


def test_infrastate_set_node_names_prefers_selected_member_over_injected_local_node(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    pushed: list[tuple[str, list[str]]] = []
    ui_updates: list[dict[str, object]] = []

    class _Manager:
        async def set_member_node_names(self, node_id: str, node_names: list[str]) -> None:
            pushed.append((node_id, list(node_names)))

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: _Manager()),
    )

    result = mod._perform_action(
        "set_node_names",
        _Conf(),
        {"node_id": "hub-1", "value": "Edge One"},
    )

    assert result["ok"] is True
    assert result["scope"] == "remote-member"
    assert result["node_id"] == "member-1"
    assert pushed == [("member-1", ["Edge One"])]
    assert ui_updates[-1]["selected_node_id"] == "member-1"


def test_infrastate_set_node_names_uses_sdk_layer_for_local_update(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    ui_updates: list[dict[str, object]] = []
    sdk_calls: list[tuple[Any]] = []
    node_config_calls: list[str] = []

    def _sdk_set_node_names(value: Any) -> dict[str, object]:
        names = [item.strip() for item in str(value).split(",") if item.strip()] if isinstance(value, str) else list(value)
        sdk_calls.append((value, names))
        return {"node_id": "hub-1", "node_names": names}

    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(mod._sdk_node, "set_node_names", _sdk_set_node_names)
    monkeypatch.setattr(
        mod._node_config,
        "save_config",
        lambda *args, **kwargs: node_config_calls.append("save_config"),
    )

    def _forbid_direct_node_names_set(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("node_config.set_node_names should not be called")

    monkeypatch.setattr(
        mod._node_config,
        "set_node_names",
        _forbid_direct_node_names_set,
    )

    result = mod._perform_action(
        "set_node_names",
        _Conf(),
        {"node_id": "hub-1", "value": "Hub Node"},
    )

    assert result["ok"] is True
    assert result["scope"] == "local"
    assert result["node_id"] == "hub-1"
    assert result["node_names"] == ["Hub Node"]
    assert not node_config_calls
    assert sdk_calls == [(["Hub Node"], ["Hub Node"])]
    assert ui_updates[-1]["last_action"] == "set_node_names"


def test_infrastate_marketplace_action_is_a_safe_noop(monkeypatch):
    mod = _load_infrastate_module()
    ui_updates: list[dict[str, object]] = []

    class _Conf:
        node_id = "hub-1"

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))

    result = mod._perform_action("marketplace", _Conf(), {})

    assert result == {"ok": True, "action": "marketplace", "selected_node_id": "member-1"}
    assert ui_updates[-1]["last_action"] == "marketplace"


def test_infrastate_marketplace_items_include_selected_node_target(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"kind": kind[:-1], "id": f"{kind[:-1]}_one", "name": f"{kind[:-1]}_one", "version": "1.0.0"}],
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {})

    items = mod._marketplace_items(webspace_id="default", selected_node_id="member-1", local_node_id="hub-1")

    assert items["skills"][0]["node_id"] == "member-1"
    assert items["skills"][0]["target_node_id"] == "member-1"


def test_infrastate_compact_snapshot_keeps_semantic_state_plane_contracts():
    mod = _load_infrastate_module()

    compact = mod._compact_snapshot_for_client(
        {
            "summary": {"label": "Infra"},
            "reliability": {
                "ok": True,
                "node": {"node_id": "hub-1"},
                "runtime": {
                    "assessment": {"state": "ready"},
                    "channel_overview": {"hub_root": {"effective_status": "ready"}},
                    "readiness_tree": {"route": {"status": "ready"}},
                    "connectivity": {
                        "required_upstream_link": {"kind": "hub_root", "transport_state": "ready"},
                    },
                    "state_sync": {
                        "webspace_id": "desktop",
                        "semantic_state": "stale",
                        "freshness_state": "aging",
                    },
                    "yjs_pressure": {
                        "owner": "_by_owner/skill_infrastate_skill",
                        "policy_state": "throttle",
                    },
                },
            },
        }
    )

    runtime = compact["reliability"]["runtime"]
    assert runtime["connectivity"]["required_upstream_link"]["kind"] == "hub_root"
    assert runtime["state_sync"]["semantic_state"] == "stale"
    assert runtime["yjs_pressure"]["policy_state"] == "throttle"


def test_infrastate_reliability_compaction_strips_heavy_runtime_payloads():
    mod = _load_infrastate_module()

    compact = mod._compact_reliability_for_infrastate(
        {
            "ok": True,
            "runtime": {
                "sync_runtime": {
                    "selected_webspace_id": "desktop",
                    "assessment": {"state": "pressure"},
                    "transport": {"room_total": 1},
                    "load_mark": {
                        "assessment": {"state": "critical"},
                        "selected_webspace": {"recent_bytes_total": 42},
                        "webspaces": {"desktop": {"owners": {"huge": "tree"}}},
                    },
                    "webspaces": {
                        "desktop": {
                            "webspace_id": "desktop",
                            "update_log_entries": 7,
                            "oversized": {"x": list(range(20))},
                        },
                        "other": {"update_log_entries": 99},
                    },
                    "selected_webspace": {
                        "webspace_id": "desktop",
                        "rebuild": {
                            "status": "ready",
                            "materialization": {
                                "ready": True,
                                "registry": {"large": "omitted"},
                                "catalog_counts": {"apps": 3},
                            },
                        },
                    },
                },
                "hub_member_connection_state": {
                    "role": "hub",
                    "known_members": [
                        {
                            "node_id": "member-1",
                            "connected": True,
                            "node_snapshot": {
                                "build": {"version": "1"},
                                "desktop_catalog": {
                                    "apps": [{"id": "a"}],
                                    "registry": {"modals": {"m": {"very": "large"}}},
                                },
                            },
                        }
                    ],
                },
            },
        }
    )

    sync = compact["runtime"]["sync_runtime"]
    assert sync["webspaces"] == {"desktop": {"webspace_id": "desktop", "update_log_entries": 7}}
    assert "webspaces" not in sync["load_mark"]
    assert sync["selected_webspace"]["rebuild"]["materialization"] == {
        "ready": True,
        "catalog_counts": {"apps": 3},
    }
    member_snapshot = compact["runtime"]["hub_member_connection_state"]["known_members"][0]["node_snapshot"]
    assert member_snapshot["desktop_catalog"] == {"apps_total": 1, "modal_total": 1, "captured_at": None}


def test_infrastate_reliability_summary_note_prefers_semantic_state_plane_contracts():
    mod = _load_infrastate_module()

    note = mod._reliability_summary_note(
        {
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {
                        "kind": "hub_root",
                        "transport_state": "ready",
                        "transition_state": "waiting_restart",
                    },
                    "browser_control_route": {
                        "kind": "browser_control_route",
                        "transport_state": "degraded",
                        "transition_state": "reconnecting",
                    },
                },
                "state_sync": {
                    "semantic_state": "stale",
                    "freshness_state": "stale",
                    "first_sync_state": "timeout",
                    "replay": {"cursor": "3/32", "mode": "snapshot_plus_diff"},
                },
                "yjs_pressure": {
                    "policy_state": "throttle",
                    "observed_state": "critical",
                    "owner": "_by_owner/skill_infrastate_skill",
                },
            }
        },
        {},
    )

    assert "hub-root=ready/waiting_restart" in note
    assert "hub-root-browser=degraded/reconnecting" in note
    assert "upstream=hub_root:ready/waiting_restart" in note
    assert "state_sync=stale/stale" in note
    assert "first_sync=timeout" in note
    assert "yjs_pressure=throttle:critical" in note
    assert "yjs_owner=_by_owner/skill_infrastate_skill" in note


def test_infrastate_adaos_update_local_uses_shared_webspace_refresh(monkeypatch):
    mod = _load_infrastate_module()

    class _Repo:
        def get(self, name: str):
            return SimpleNamespace(version="1.2.3")

    class _ScenarioRepo:
        def get(self, name: str):
            return SimpleNamespace(version="4.5.6")

    class _Ctx:
        sql = object()
        git = object()
        paths = object()
        bus = None
        caps = object()
        settings = object()
        skills_repo = _Repo()
        scenarios_repo = _ScenarioRepo()

    class _SkillManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    refresh_calls: list[tuple[str, str]] = []
    scenario_capacity_calls: list[tuple[str, str, bool]] = []
    rebuild_calls: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(mod, "SkillManager", _SkillManager)
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(
        mod,
        "sync_workspace_sparse_to_registry",
        lambda ctx: {"ok": True, "skills": ["weather_skill"], "scenarios": ["web_desktop"]},
    )
    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda ctx: {"ok": True, "skills": ["weather_skill"], "scenarios": ["web_desktop"]},
    )
    monkeypatch.setattr(
        mod,
        "refresh_skill_runtime",
        lambda mgr, name, **kwargs: refresh_calls.append((name, str(kwargs.get("webspace_id") or ""))) or {
            "runtime_updated": True,
            "runtime_migrated": False,
        },
    )
    monkeypatch.setattr(
        mod,
        "install_scenario_in_capacity",
        lambda name, version, *, active=True, dev=False, base_dir=None: scenario_capacity_calls.append((name, version, active)),
    )
    monkeypatch.setattr(
        mod,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: rebuild_calls.append(dict(kwargs)) or {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )

    result = mod._adaos_update_local(dry_run=False)

    assert result["ok"] is True
    assert result["registry_reconciled"] is True
    assert result["runtime_updated"] == ["weather_skill"]
    assert result["scenario_capacity_updated"] == ["web_desktop"]
    expected_webspace_id = mod.default_webspace_id()
    assert refresh_calls == [("weather_skill", expected_webspace_id)]
    assert scenario_capacity_calls == [("web_desktop", "4.5.6", True)]
    assert rebuild_calls == [
        {
            "webspace_id": expected_webspace_id,
            "action": "infrastate_adaos_update_sync",
            "source_of_truth": "scenario_projection",
        }
    ]
    assert result["webspace_refresh"]["ok"] is True


def test_infrastate_remote_adaos_update_requests_member_snapshot_after_success(monkeypatch):
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"

    ui_updates: list[dict[str, object]] = []
    calls: list[tuple[str, str]] = []

    class _Manager:
        async def rpc_tools_call(self, node_id: str, *, tool: str, arguments: dict[str, object] | None, timeout: float | None, dev: bool):
            calls.append(("rpc", node_id))
            assert tool == "infrastate_skill:adaos_update"
            return {"ok": True, "updated": True}

        async def request_member_snapshot(self, node_id: str, reason: str) -> dict[str, object]:
            calls.append(("snapshot", f"{node_id}:{reason}"))
            return {"ok": True, "accepted": True}

    def _no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(mod, "_ui_state", lambda: {"selected_node_id": "member-1"})
    monkeypatch.setattr(mod, "_write_ui_state", lambda **kwargs: ui_updates.append(dict(kwargs)))
    monkeypatch.setattr(mod.asyncio, "get_running_loop", _no_loop)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: _Manager()),
    )

    result = mod._perform_action("adaos_update", _Conf(), {})

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["snapshot_requested"] is True
    assert calls == [
        ("rpc", "member-1"),
        ("snapshot", "member-1:infrastate.adaos_update"),
    ]
    assert ui_updates[-1]["selected_node_id"] == "member-1"


def test_infrastate_forget_subnet_clears_directory_and_requests_member_refresh(monkeypatch):
    mod = _load_infrastate_module()

    class _Directory:
        def __init__(self) -> None:
            self.cleared = False

        def list_known_nodes(self) -> list[dict[str, str]]:
            return [
                {"node_id": "member-1"},
                {"node_id": "member-2"},
            ]

        def clear_all(self) -> None:
            self.cleared = True

    class _Manager:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str]] = []

        def snapshot(self) -> dict[str, object]:
            return {"members": [{"node_id": "member-1"}]}

        async def request_member_snapshot(self, node_id: str, reason: str) -> None:
            self.requests.append((node_id, reason))

    directory = _Directory()
    manager = _Manager()

    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", subnet_id="sn-test"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.registry.subnet_directory",
        types.SimpleNamespace(get_directory=lambda: directory),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.subnet.link_manager",
        types.SimpleNamespace(get_hub_link_manager=lambda: manager),
    )

    result = mod._forget_subnet_local()

    assert directory.cleared is True
    assert result["ok"] is True
    assert result["forgotten_total"] == 2
    assert result["forgotten_node_ids"] == ["member-1", "member-2"]
    assert result["refresh_requested"] == 1
    assert manager.requests == [("member-1", "infrastate.forget_subnet")]


def test_infrastate_get_snapshot_projects_fallback_when_snapshot_crashes(monkeypatch):
    mod = _load_infrastate_module()
    projected: dict[str, object] = {}

    def _boom(*, webspace_id=None):
        raise UnboundLocalError("cannot access local variable 'sync_runtime' where it is not associated with a value")

    monkeypatch.setattr(mod, "_snapshot", _boom)
    monkeypatch.setattr(mod, "_project", lambda snapshot, webspace_id=None: projected.update({"snapshot": snapshot, "webspace_id": webspace_id}))
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready"})
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_reliability_snapshot",
        lambda conf, lifecycle: {
            "runtime": {
                "sync_runtime": {
                    "assessment": {"state": "nominal", "reason": "test"},
                    "selected_webspace_id": "default",
                }
            }
        },
    )
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod.get_snapshot(webspace_id="default")

    assert snapshot["fallback"] is True
    assert "sync_runtime" in snapshot["errors"][0]
    assert projected["webspace_id"] == "default"
    assert isinstance(projected["snapshot"], dict)
    assert projected["snapshot"]["fallback"] is True


def test_infrastate_snapshot_tolerates_section_failures(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_ensure_skill_data_projections", lambda: None)
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1"))
    monkeypatch.setattr(mod, "read_core_update_status", lambda: {"state": "idle"})
    monkeypatch.setattr(mod, "read_core_update_last_result", lambda: {})
    monkeypatch.setattr(mod, "slot_status", lambda: {})
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready"})
    monkeypatch.setattr(mod, "_build_meta", lambda: {})
    monkeypatch.setattr(mod, "_effective_runtime_projection", lambda status, last_result, slots_payload, build: (slots_payload, build))
    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_reliability_snapshot", lambda conf, lifecycle: {"runtime": {}})
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_yjs_webspace_tabs", lambda conf, ui_state, reliability, selected_node: [])
    monkeypatch.setattr(mod, "_selected_node_editor", lambda conf, selected_node: {})
    monkeypatch.setattr(
        mod,
        "_selected_node_projection",
        lambda *args, **kwargs: {"status": {"state": "idle"}, "last_result": {}, "slots_payload": {}, "lifecycle": {}, "build": {}, "selected_member": {}},
    )
    monkeypatch.setattr(mod, "_transport_diag_snapshot", lambda: {})
    monkeypatch.setattr(mod, "_read_json", lambda path: {})
    monkeypatch.setattr(mod, "_effective_update_log_report", lambda report, last_result: {})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": [], "active": []})
    monkeypatch.setattr(mod, "_summary", lambda *args, **kwargs: {"label": "Infra State", "value": "ready"})
    monkeypatch.setattr(mod, "_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_core_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_yjs_action_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_update_actions", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_build_items", lambda build: (_ for _ in ()).throw(FileNotFoundError("missing build file")))
    monkeypatch.setattr(mod, "_step_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_realtime_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_slot_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_skills_items", lambda: (_ for _ in ()).throw(FileNotFoundError("missing workspace registry")))
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_marketplace_items", lambda webspace_id=None: (_ for _ in ()).throw(FileNotFoundError("missing marketplace source")))
    monkeypatch.setattr(mod, "_status_log_items", lambda report: [])
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod._snapshot()

    assert snapshot["summary"]["value"] == "ready"
    assert snapshot["build"] == []
    assert snapshot["skills"] == []
    assert snapshot["marketplace"] == {"skills": [], "scenarios": []}


def test_infrastate_snapshot_tolerates_bootstrap_file_not_found(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_ensure_skill_data_projections", lambda: None)
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1", node_names=["hub"]))
    monkeypatch.setattr(mod, "read_core_update_status", lambda: (_ for _ in ()).throw(FileNotFoundError("missing core status")))
    monkeypatch.setattr(mod, "read_core_update_last_result", lambda: (_ for _ in ()).throw(FileNotFoundError("missing core result")))
    monkeypatch.setattr(mod, "slot_status", lambda: (_ for _ in ()).throw(FileNotFoundError("missing slots")))
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: (_ for _ in ()).throw(FileNotFoundError("missing lifecycle")))
    monkeypatch.setattr(mod, "_build_meta", lambda: (_ for _ in ()).throw(FileNotFoundError("missing build meta")))
    monkeypatch.setattr(mod, "_ui_state", lambda: {})
    monkeypatch.setattr(mod, "_reliability_snapshot", lambda conf, lifecycle: (_ for _ in ()).throw(FileNotFoundError("missing reliability")))
    monkeypatch.setattr(mod, "_transport_diag_snapshot", lambda: (_ for _ in ()).throw(FileNotFoundError("missing transport diag")))
    monkeypatch.setattr(mod, "_read_json", lambda path: (_ for _ in ()).throw(FileNotFoundError("missing report")))
    monkeypatch.setattr(mod, "_build_items", lambda build: [])
    monkeypatch.setattr(mod, "_step_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_realtime_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_slot_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_marketplace_items", lambda webspace_id=None: {"skills": [], "scenarios": []})
    monkeypatch.setattr(mod, "_status_log_items", lambda report: [])
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod._snapshot()

    assert snapshot.get("fallback") is not True
    assert snapshot["summary"]["label"] in {"Infra State", "Core update"}
    assert snapshot["skills"] == []
    assert snapshot["scenarios"] == []
    assert snapshot["marketplace"] == {"skills": [], "scenarios": []}


def test_infrastate_supervisor_transition_note_covers_root_promotion_and_restart():
    mod = _load_infrastate_module()

    pending = mod._supervisor_transition_note(
        {
            "state": "validated",
            "phase": "root_promotion_pending",
            "message": "validated slot is running; root promotion is pending",
        }
    )
    promoted = mod._supervisor_transition_note(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
        }
    )

    assert pending["status"] == "warn"
    assert "root promotion" in pending["description"]
    assert promoted["status"] == "warn"
    assert "restart adaos.service" in promoted["description"]


def test_infrastate_supervisor_transition_note_covers_planned_and_subsequent_update():
    mod = _load_infrastate_module()

    planned = mod._supervisor_transition_note(
        {
            "state": "planned",
            "phase": "scheduled",
            "message": "core update deferred until minimum update interval elapses",
            "planned_reason": "minimum_update_period",
            "scheduled_for": time.time() + 300.0,
            "subsequent_transition": True,
        }
    )

    assert planned["status"] == "warn"
    assert "minimum update interval" in planned["description"]
    assert "subsequent transition queued" in planned["description"]


def test_infrastate_highlight_changed_summary_text_marks_only_changed_segments():
    mod = _load_infrastate_module()

    rendered = mod._highlight_changed_summary_text(
        "countdown completed | pending_acks=2 | protocol=degraded | action: cancel_update",
        "countdown completed | pending_acks=1 | protocol=degraded | action: start_update",
    )

    assert "countdown completed" in rendered
    assert "𝐩𝐞𝐧𝐝𝐢𝐧𝐠_𝐚𝐜𝐤𝐬=𝟐" in rendered
    assert "protocol=degraded" in rendered
    assert "𝐚𝐜𝐭𝐢𝐨𝐧: 𝐜𝐚𝐧𝐜𝐞𝐥_𝐮𝐩𝐝𝐚𝐭𝐞" in rendered


def test_infrastate_summary_highlights_against_previous_render(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")

    common_kwargs = dict(
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member=None,
    )

    first = mod._summary(
        status={"state": "countdown", "message": "countdown completed", "phase": "countdown"},
        **common_kwargs,
    )
    second = mod._summary(
        status={"state": "restarting", "message": "countdown completed | pending_acks=2", "phase": "shutdown"},
        **common_kwargs,
    )

    assert first["value"] == "countdown"
    assert second["value"] == "𝐫𝐞𝐬𝐭𝐚𝐫𝐭𝐢𝐧𝐠"
    assert "countdown completed" in second["description"]
    assert "𝐩𝐞𝐧𝐝𝐢𝐧𝐠_𝐚𝐜𝐤𝐬=𝟐" in second["description"]

def test_infrastate_summary_exposes_semantic_state_plane_contracts(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")

    summary = mod._summary(
        status={"state": "ready", "message": "ok", "phase": "validate"},
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {"kind": "hub_root", "transport_state": "ready"},
                },
                "state_sync": {
                    "webspace_id": "desktop",
                    "semantic_state": "stale",
                },
                "yjs_pressure": {
                    "owner": "_by_owner/skill_infrastate_skill",
                    "policy_state": "block",
                },
            }
        },
        transport_diag={},
        selected_member=None,
    )

    assert summary["semantic_connectivity"]["required_upstream_link"]["kind"] == "hub_root"
    assert summary["semantic_state_sync"]["semantic_state"] == "stale"
    assert summary["semantic_yjs_pressure"]["policy_state"] == "block"


def test_infrastate_summary_uses_dev_slot_for_local_root_runtime(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(mod, "_node_tabs", lambda conf, ui_state, reliability: ([], {"kind": "local", "node_id": "hub-1", "label": "hub"}))
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")
    monkeypatch.setattr(mod, "_repo_root", lambda: Path("D:/git/adaos"))
    monkeypatch.setattr(mod.os, "getenv", lambda key, default=None: "dev" if key == "ENV_TYPE" else "")

    summary = mod._summary(
        status={"state": "ready", "message": "ok", "phase": "validate"},
        last_result={},
        slots_payload={},
        lifecycle={},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member=None,
    )

    assert summary["subtitle"] == "slot dev | 77fab7d"


def test_infrastate_summary_marks_disconnected_remote_node_offline(monkeypatch):
    mod = _load_infrastate_module()
    memory: dict[str, object] = {}

    monkeypatch.setattr(mod, "skill_memory_get", lambda key, default=None: memory.get(key, default))
    monkeypatch.setattr(mod, "skill_memory_set", lambda key, value: memory.__setitem__(key, value))
    monkeypatch.setattr(
        mod,
        "_node_tabs",
        lambda conf, ui_state, reliability: (
            [],
            {
                "kind": "member",
                "node_id": "member-1",
                "label": "Edge One",
                "node_compact_label": "E1",
            },
        ),
    )
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_migration_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_note", lambda report: "")
    monkeypatch.setattr(mod, "_skill_post_commit_checks_report", lambda status, last_result: {})
    monkeypatch.setattr(mod, "_skill_post_commit_checks_note", lambda report: "")
    monkeypatch.setattr(mod, "_supervisor_transition_note", lambda status: {})
    monkeypatch.setattr(mod, "_reliability_summary_note", lambda reliability, transport_diag: "")
    monkeypatch.setattr(mod, "_hub_root_strategy", lambda reliability, transport_diag: {})
    monkeypatch.setattr(mod, "_effective_channel_view", lambda *args, **kwargs: ("ready", "stable", {}))
    monkeypatch.setattr(mod, "_selected_yjs_webspace_id", lambda ui_state, reliability: "default")
    monkeypatch.setattr(mod, "_remote_control_payload", lambda snapshot, member: {})

    summary = mod._summary(
        status={"state": "countdown", "message": "countdown active", "phase": "countdown"},
        last_result={},
        slots_payload={"active_slot": "A"},
        lifecycle={"node_state": "offline"},
        conf=SimpleNamespace(role="hub", node_id="hub-1"),
        build={"runtime_git_short_commit": "77fab7d"},
        ui_state={},
        reliability={"runtime": {}},
        transport_diag={},
        selected_member={
            "connected": False,
            "state": "offline",
            "snapshot_state": "stale",
            "last_message_ago_s": 42,
            "last_seen_ago_s": 60,
        },
    )

    assert summary["value"] == "Offline"


def test_infrastate_summary_buttons_offer_defer_during_countdown():
    mod = _load_infrastate_module()

    buttons = mod._summary_buttons(
        {
            "state": "countdown",
            "phase": "countdown",
            "scheduled_for": time.time() + 60.0,
        }
    )

    button_ids = [str(item.get("id") or "") for item in buttons]
    assert "defer_update_5m" in button_ids
    assert "defer_update_15m" in button_ids


def test_infrastate_supervisor_transition_note_covers_planned_and_subsequent(monkeypatch):
    mod = _load_infrastate_module()
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)

    planned = mod._supervisor_transition_note(
        {
            "state": "planned",
            "phase": "scheduled",
            "message": "core update is scheduled",
            "planned_reason": "minimum_update_period",
            "scheduled_for": 400.0,
            "subsequent_transition": True,
        }
    )

    assert planned["status"] == "warn"
    assert "minimum update interval" in planned["description"]
    assert "subsequent transition queued" in planned["description"]


def test_infrastate_summary_buttons_include_defer_actions_for_planned(monkeypatch):
    mod = _load_infrastate_module()
    monkeypatch.setattr(mod.time, "time", lambda: 100.0)

    buttons = mod._summary_buttons(
        {
            "state": "planned",
            "scheduled_for": 400.0,
        }
    )

    ids = [item["id"] for item in buttons]
    assert "defer_update_5m" in ids
    assert "defer_update_15m" in ids
    assert "cancel_update" in ids


def test_infrastate_step_items_include_supervisor_transition():
    mod = _load_infrastate_module()

    items = mod._step_items(
        {
            "state": "validated",
            "phase": "root_promotion_pending",
            "message": "validated slot is running; root promotion is pending",
            "target_rev": "rev2026",
        },
        {"active_slot": "A", "previous_slot": "B"},
        {"node_state": "ready", "reason": "runtime nominal"},
        {"version": "0.1.0+40.deadbee", "runtime_git_short_commit": "deadbee", "runtime_git_branch": "rev2026"},
    )

    supervisor_item = next(item for item in items if item["id"] == "supervisor_transition")
    assert supervisor_item["status"] == "warn"
    assert "root promotion" in supervisor_item["description"]


def test_infrastate_scenario_items_only_show_installed_registry_entries(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    alpha_dir = workspace / "scenarios" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "scenario.yaml").write_text("id: alpha\nversion: '1.2.3'\n", encoding="utf-8")
    beta_dir = workspace / "scenarios" / "beta"
    beta_dir.mkdir(parents=True, exist_ok=True)
    (beta_dir / "scenario.yaml").write_text("id: beta\nversion: '2.0.0'\n", encoding="utf-8")

    class _ScenarioRecord:
        def __init__(self, name: str, active_version: str, last_updated: float | None = None):
            self.name = name
            self.active_version = active_version
            self.last_updated = last_updated

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            git=object(),
        ),
    )
    monkeypatch.setattr(
        mod,
        "SqliteScenarioRegistry",
        lambda sql: SimpleNamespace(
            list=lambda: [
                _ScenarioRecord("alpha", "1.0.0", 1.0),
                _ScenarioRecord("gamma", "3.0.0", 2.0),
            ]
        ),
    )
    monkeypatch.setattr(
        mod,
        "get_local_capacity",
        lambda: {"scenarios": [{"name": "delta", "version": "4.0.0", "active": True, "updated_at": 4.0}]},
    )

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", False)

    all_items = mod._scenario_items(include_all=True)
    assert [(item["name"], item["version"]) for item in all_items] == [
        ("alpha", "1.0.0"),
        ("delta", "4.0.0"),
        ("gamma", "3.0.0"),
    ]
    assert all_items[0]["workspace_source_version"] == "1.2.3"
    assert all_items[0]["has_drift"] is True

    default_items = mod._scenario_items()
    assert [(item["name"], item["version"]) for item in default_items] == [
        ("alpha", "1.0.0"),
        ("delta", "4.0.0"),
        ("gamma", "3.0.0"),
    ]

    drift_items = mod._filter_inventory_drift(default_items, drift_only=True)
    assert [item["name"] for item in drift_items] == ["alpha"]


def test_infrastate_inventory_stream_honors_drift_only_toggle(monkeypatch):
    mod = _load_infrastate_module()
    rows = [
        {"name": "aligned", "has_drift": False},
        {"name": "behind", "has_drift": True},
    ]
    monkeypatch.setattr(mod, "_skills_items", lambda *, include_all=True: list(rows))
    monkeypatch.setattr(mod, "_ui_state", lambda: {"inventory_drift_only": False})

    all_rows = mod._build_stream_payload_for_receiver(mod._skills_receiver())
    assert [item["name"] for item in all_rows] == ["aligned", "behind"]

    monkeypatch.setattr(mod, "_ui_state", lambda: {"inventory_drift_only": True})
    drift_rows = mod._build_stream_payload_for_receiver(mod._skills_receiver())
    assert [item["name"] for item in drift_rows] == ["behind"]


def test_infrastate_catalog_record_exposes_source_and_commit(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mod._registry_catalog_cache.clear()
    mod._registry_catalog_meta_cache.clear()

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(mod, "_allow_marketplace_remote_fetch", lambda: False)
    monkeypatch.setattr(mod, "_allow_marketplace_git_ref_lookup", lambda: True)
    monkeypatch.setattr(
        mod,
        "_registry_payload_from_git_ref",
        lambda workspace_root: {"skills": [{"id": "demo_skill", "version": "1.2.3"}]},
    )
    monkeypatch.setattr(mod, "_registry_ref_config", lambda: ("origin", "main"))
    monkeypatch.setattr(mod, "_registry_git_ref_commit", lambda workspace_root, *, remote, branch: "abc123")
    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    record = mod._read_catalog_record(kind_plural="skills", artifact_id="demo_skill")

    assert record == {
        "version": "1.2.3",
        "catalog_source": "git_ref:origin/main",
        "catalog_commit": "abc123",
        "catalog_state": "available",
    }


def test_infrastate_catalog_record_reports_no_git(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mod._registry_catalog_cache.clear()
    mod._registry_catalog_meta_cache.clear()

    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(mod, "_allow_marketplace_remote_fetch", lambda: False)
    monkeypatch.setattr(mod, "_allow_marketplace_git_ref_lookup", lambda: True)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "_marketplace_catalog_entries", lambda kind: [])
    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    record = mod._read_catalog_record(kind_plural="skills", artifact_id="demo_skill")

    assert record["version"] == ""
    assert record["catalog_source"] == "no_git"
    assert record["catalog_state"] == "no_git"


def test_infrastate_version_status_marks_catalog_unknown_and_no_git():
    mod = _load_infrastate_module()

    unknown = mod._version_status(
        artifact_kind="skill",
        catalog_version="",
        catalog_source="git_ref:origin/main",
        catalog_state="unknown",
        workspace_source_version="1.0.0",
        active_version="1.0.0",
        active=True,
    )
    no_git = mod._version_status(
        artifact_kind="skill",
        catalog_version="",
        catalog_source="no_git",
        catalog_state="no_git",
        workspace_source_version="1.0.0",
        active_version="1.0.0",
        active=True,
    )

    assert unknown["status"] == "catalog_unknown"
    assert unknown["has_drift"] is True
    assert no_git["status"] == "git_unavailable"
    assert no_git["has_drift"] is True


def test_infrastate_skill_items_use_registry_and_workspace_versions(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.19.0'\n", encoding="utf-8")
    extra_dir = workspace / "skills" / "extra_skill"
    extra_dir.mkdir(parents=True, exist_ok=True)
    (extra_dir / "skill.yaml").write_text("id: extra_skill\nversion: '9.9.9'\n", encoding="utf-8")

    class _SkillRecord:
        def __init__(self, name: str, active_version: str, installed: bool = True):
            self.name = name
            self.active_version = active_version
            self.installed = installed

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord("infrastate_skill", "0.18.0")]))
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(
            runtime_status=lambda name: {"active_slot": "A", "version": "0.19.0", "runtime_bucket": "v0.19"}
        ),
    )
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(
        mod,
        "_registry_json_catalog_entries",
        lambda kind, workspace_root: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}]
        if kind == "skills"
        else [],
    )
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}] if kind == "skills" else [],
    )

    items = mod._skills_items()

    assert len(items) == 1
    item = items[0]
    assert item["name"] == "infrastate_skill"
    assert item["version"] == "0.19.0"
    assert item["active_version"] == "0.19.0"
    assert item["workspace_source_version"] == "0.19.0"
    assert item["catalog_version"] == "0.20.0"
    assert item["catalog_source"] == "registry_json"
    assert item["catalog_commit"] == ""
    assert item["catalog_state"] == "available"
    assert item["runtime_bucket"] == "v0.19"
    assert item["version_display"] == "0.19.0* (0.20.0)"
    assert item["slot"] == "A"
    assert item["remote_version"] == "0.20.0"
    assert item["update_available"] is True
    assert item["registry_mismatch"] is True
    assert item["has_drift"] is True
    assert item["can_activate"] is False
    assert item["status"] == "behind_catalog"
    assert item["status_icon"] == "cloud-download-outline"
    assert "behind catalog" in item["status_tooltip"]


def test_infrastate_skill_items_compare_remote_against_active_runtime_version(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.20.0'\n", encoding="utf-8")

    class _SkillRecord:
        name = "infrastate_skill"
        active_version = "0.19.0"
        installed = True

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord()]))
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(
            runtime_status=lambda name: {"active_slot": "A", "version": "0.19.0", "runtime_bucket": "v0.19"}
        ),
    )
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", True)
    monkeypatch.setattr(
        mod,
        "_registry_json_catalog_entries",
        lambda kind, workspace_root: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}]
        if kind == "skills"
        else [],
    )

    items = mod._skills_items()

    assert items[0]["version"] == "0.19.0"
    assert items[0]["remote_version"] == "0.20.0"
    assert items[0]["active_version"] == "0.19.0"
    assert items[0]["workspace_source_version"] == "0.20.0"
    assert items[0]["catalog_version"] == "0.20.0"
    assert items[0]["catalog_source"] == "registry_json"
    assert items[0]["catalog_state"] == "available"
    assert items[0]["runtime_bucket"] == "v0.19"
    assert items[0]["version_display"] == "0.19.0* (0.20.0)"
    assert items[0]["registry_mismatch"] is True
    assert items[0]["has_drift"] is True
    assert items[0]["can_activate"] is True


def test_infrastate_skill_items_skip_remote_version_probe_when_disabled(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.19.0'\n", encoding="utf-8")

    class _SkillRecord:
        def __init__(self, name: str, active_version: str):
            self.name = name
            self.active_version = active_version

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord("infrastate_skill", "0.18.0")]))
    monkeypatch.setattr(mod, "SkillManager", lambda **kwargs: SimpleNamespace(runtime_status=lambda name: {"active_slot": "A"}))
    monkeypatch.setattr(mod, "_REMOTE_VERSION_PROBE_ENABLED", False)
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (_ for _ in ()).throw(AssertionError("remote registry probe should be skipped when disabled")),
    )

    items = mod._skills_items()

    assert items[0]["remote_version"] == ""
    assert items[0]["update_available"] is False
    assert items[0]["registry_mismatch"] is False
    assert items[0]["version_display"] == "0.18.0"
    assert items[0]["workspace_source_version"] == "0.19.0"
    assert items[0]["has_drift"] is True
    assert items[0]["status"] == "workspace_runtime_differs"


def test_infrastate_marketplace_catalog_skips_remote_url_fetch_on_member(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="member"))
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mod,
        "rebuild_workspace_registry",
        lambda workspace_root: {
            "skills": [
                {"kind": "skill", "id": "local_skill", "name": "local_skill", "version": "0.1.0"},
            ]
        },
    )
    called = {"url": 0, "git": 0}

    def _fail_url():
        called["url"] += 1
        raise AssertionError("member snapshot path should not hit marketplace URL fetch")

    def _fail_git(*args, **kwargs):
        called["git"] += 1
        raise AssertionError("member snapshot path should not hit git ref registry lookup")

    monkeypatch.setattr(mod, "_registry_payload_from_url", _fail_url)
    monkeypatch.setattr(mod.subprocess, "run", _fail_git)

    items = mod._marketplace_catalog_entries("skills")

    assert [item["name"] for item in items] == ["local_skill"]
    assert called == {"url": 0, "git": 0}


def test_infrastate_adaos_update_uses_union_sparse_sync_and_installed_skill_names(monkeypatch):
    mod = _load_infrastate_module()
    runtime_updates: list[str] = []
    scenario_capacity_updates: list[tuple[str, str]] = []

    ctx = SimpleNamespace(
        sql=object(),
        git=object(),
        paths=SimpleNamespace(workspace_dir=lambda: Path(".")),
        bus=None,
        caps=object(),
        settings=object(),
        skills_repo=object(),
        scenarios_repo=object(),
    )

    monkeypatch.setattr(mod, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        mod,
        "sync_workspace_sparse_to_registry",
        lambda current_ctx: {"ok": True, "skills": ["installed_skill"], "scenarios": ["scene_one"], "fallback_used": {}},
    )
    monkeypatch.setattr(
        mod,
        "reconcile_workspace_db_to_materialized",
        lambda current_ctx: {"ok": True, "skills": ["installed_skill"], "scenarios": ["scene_one"]},
    )
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(runtime_update=lambda name, space="workspace": runtime_updates.append(name) or {"ok": True}),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(
        mod,
        "install_scenario_in_capacity",
        lambda name, version, *, active=True, dev=False, base_dir=None: scenario_capacity_updates.append((name, version)),
    )

    result = mod._adaos_update_local()

    assert result["ok"] is True
    assert result["skills_synced"] is True
    assert result["scenarios_synced"] is True
    assert result["registry_reconciled"] is True
    assert result["skills"] == ["installed_skill"]
    assert result["scenarios"] == ["scene_one"]
    assert result["scenario_capacity_updated"] == ["scene_one"]
    assert runtime_updates == ["installed_skill"]
    assert scenario_capacity_updates == [("scene_one", "unknown")]


def test_infrastate_marketplace_filters_installed_and_marks_running_operations(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [
                {"kind": "skill", "id": "installed_skill", "name": "installed_skill", "version": "1.0.0"},
                {"kind": "skill", "id": "queued_skill", "name": "queued_skill", "version": "1.2.0"},
            ]
            if kind == "skills"
            else [{"kind": "scenario", "id": "new_scene", "name": "new_scene", "version": "0.5.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [{"name": "installed_skill", "version": "1.0.0", "slot": "A"}])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(
        mod,
        "get_operation_manager",
        lambda: SimpleNamespace(
            snapshot=lambda webspace_id=None: {
                "active_items": [
                    {
                        "target_kind": "skill",
                        "target_id": "queued_skill",
                        "status": "running",
                        "current_step": "skill.install",
                    }
                ]
            }
        ),
    )

    items = mod._marketplace_items(webspace_id="default")

    assert [item["id"] for item in items["skills"]] == ["queued_skill"]
    assert items["skills"][0]["install_disabled"] is True
    assert items["skills"][0]["operation_status"] == "running"
    assert [item["id"] for item in items["scenarios"]] == ["new_scene"]


def test_infrastate_marketplace_catalog_prefers_remote_registry_and_local_scan(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    scenario_dir = workspace / "scenarios" / "infrascope"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.yaml").write_text(
        "\n".join(
            [
                "id: infrascope",
                "name: Infrascope",
                "version: '0.2.0'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(mod, "_registry_payload_from_url", lambda: None)
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"scenarios":[{"kind":"scenario","id":"remote_scene","name":"remote_scene","version":"1.0.0"}]}',
            stderr="",
        ),
    )

    items = mod._marketplace_catalog_entries("scenarios")

    assert [item["name"] for item in items] == ["infrascope", "remote_scene"]


def test_infrastate_marketplace_catalog_uses_ttl_cache(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    calls = {"git": 0, "scan": 0}

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(mod, "_registry_payload_from_url", lambda: None)
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_MARKETPLACE_CACHE_TTL_S", 30.0)
    mod._marketplace_catalog_cache.clear()

    def _fake_git_run(*args, **kwargs):
        calls["git"] += 1
        return SimpleNamespace(
            returncode=0,
            stdout='{"skills":[{"kind":"skill","id":"remote_skill","name":"remote_skill","version":"1.0.0"}]}',
            stderr="",
        )

    def _fake_rebuild(workspace_root):
        calls["scan"] += 1
        return {"skills": [{"kind": "skill", "id": "local_skill", "name": "local_skill", "version": "0.1.0"}]}

    monkeypatch.setattr(mod.subprocess, "run", _fake_git_run)
    monkeypatch.setattr(mod, "rebuild_workspace_registry", _fake_rebuild)

    first = mod._marketplace_catalog_entries("skills")
    second = mod._marketplace_catalog_entries("skills")

    assert [item["name"] for item in first] == ["local_skill", "remote_skill"]
    assert [item["name"] for item in second] == ["local_skill", "remote_skill"]
    assert calls == {"git": 1, "scan": 1}


def test_infrastate_realtime_items_include_semantic_state_plane_cards():
    mod = _load_infrastate_module()

    items = mod._realtime_items(
        {
            "runtime": {
                "connectivity": {
                    "required_upstream_link": {
                        "kind": "hub_root",
                        "transport_state": "ready",
                        "transition_state": "waiting_restart",
                        "served_by": "supervisor",
                    },
                    "browser_control_route": {
                        "transport_state": "degraded",
                        "transition_state": "reconnecting",
                        "blockers": ["route.flapping"],
                    },
                },
                "state_sync": {
                    "webspace_id": "desktop",
                    "transport_state": "attached",
                    "first_sync_state": "timeout",
                    "semantic_state": "stale",
                    "freshness_state": "stale",
                    "fallback_mode": "hard_degraded_recovery",
                    "replay": {"cursor": "3/32", "mode": "snapshot_plus_diff"},
                },
                "yjs_pressure": {
                    "owner": "_by_owner/skill_infrastate_skill",
                    "policy_state": "throttle",
                    "observed_state": "critical",
                    "recent_bytes": 167296,
                    "recent_writes": 2,
                    "peak_bps": 167296.0,
                    "peak_wps": 2.0,
                    "reason": "write_amplification",
                    "throttled_total": 4,
                    "blocked_total": 1,
                    "last_policy_state": "block",
                    "last_reason": "write_amplification_blocked",
                },
            }
        },
        {},
    )

    by_id = {str(item.get("id") or ""): item for item in items}
    assert "semantic_connectivity" in by_id
    assert "semantic_state_sync" in by_id
    assert "semantic_yjs_pressure" in by_id
    assert "upstream=hub_root:ready/waiting_restart" in str(by_id["semantic_connectivity"]["description"])
    assert "semantic=stale" in str(by_id["semantic_state_sync"]["description"])
    assert "policy=throttle" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "throttled=4" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "blocked=1" in str(by_id["semantic_yjs_pressure"]["description"])
    assert "last=block:write_amplification_blocked" in str(by_id["semantic_yjs_pressure"]["subtitle"])


def test_infrastate_project_async_skips_snapshot_with_only_timestamp_changes(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str, object]] = []
    mod._projection_fingerprints.clear()
    mod._projection_diag.update({"apply_total": 0, "skip_total": 0, "cache_hit_total": 0})

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, slot, value))

    monkeypatch.setattr(mod, "ctx_subnet", SimpleNamespace(set_async=_fake_set_async))
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(mod, "_publish_snapshot_streams", lambda snapshot, webspace_id=None: None)

    first = {
        "summary": {"value": "ready", "updated_at": 10.0},
        "projection_diag": {"apply_total": 0},
        "last_refresh_ts": 10.0,
        "events": [],
    }
    second = {
        "summary": {"value": "ready", "updated_at": 11.0},
        "projection_diag": {"apply_total": 999},
        "last_refresh_ts": 11.0,
        "events": [],
    }

    asyncio.run(mod._project_async(first, webspace_id="default"))
    asyncio.run(mod._project_async(second, webspace_id="default"))

    assert applied == [("default", "infrastate.summary", {"value": "ready"})]
    assert mod._projection_diag["apply_total"] == 1
    assert mod._projection_diag["skip_total"] == 1


def test_infrastate_project_async_uses_throttled_interval_when_yjs_policy_requires(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str, object]] = []
    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._projection_diag.update(
        {
            "apply_total": 0,
            "skip_total": 0,
            "cache_hit_total": 0,
            "rate_limited_total": 0,
            "last_policy_state": "",
            "last_policy_owner": "",
            "last_policy_observed_state": "",
            "last_policy_webspace_id": "",
            "last_policy_throttled_roots": [],
        }
    )

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, slot, value))

    monkeypatch.setattr(mod, "ctx_subnet", SimpleNamespace(set_async=_fake_set_async))
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(mod, "_publish_snapshot_streams", lambda snapshot, webspace_id=None: None)
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {"policy_state": "throttle", "observed_state": "critical", "throttled_roots": ["data"]},
    )
    monkeypatch.setattr(mod, "_MIN_YJS_PROJECTION_INTERVAL_S", 0.1)
    monkeypatch.setattr(mod, "_THROTTLED_YJS_PROJECTION_INTERVAL_S", 2.0)

    first = {"summary": {"value": "ready-1"}}
    second = {"summary": {"value": "ready-2"}}

    asyncio.run(mod._project_async(first, webspace_id="default"))
    asyncio.run(mod._project_async(second, webspace_id="default"))

    assert applied == [("default", "infrastate.summary", {"value": "ready-1"})]
    assert mod._projection_diag["apply_total"] == 1
    assert mod._projection_diag["rate_limited_total"] == 1


def test_infrastate_project_async_blocks_primary_yjs_projection_but_keeps_streams(monkeypatch):
    mod = _load_infrastate_module()
    applied: list[tuple[str | None, str]] = []
    published: list[str] = []
    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._projection_diag.update(
        {
            "apply_total": 0,
            "skip_total": 0,
            "cache_hit_total": 0,
            "rate_limited_total": 0,
            "blocked_total": 0,
            "last_policy_state": "",
            "last_policy_owner": "",
            "last_policy_observed_state": "",
            "last_policy_webspace_id": "",
            "last_policy_throttled_roots": [],
            "last_policy_blocked_roots": [],
            "last_policy_reason": "",
        }
    )

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        applied.append((webspace_id, str(value.get("summary", {}).get("value") or "")))

    monkeypatch.setattr(mod, "ctx_subnet", SimpleNamespace(set_async=_fake_set_async))
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(
        mod,
        "_publish_snapshot_streams",
        lambda snapshot, webspace_id=None: published.append(str(snapshot.get("summary", {}).get("value") or "")),
    )
    monkeypatch.setattr(
        mod,
        "_projection_pressure_policy",
        lambda webspace_id=None: {
            "policy_state": "block",
            "observed_state": "critical",
            "blocked_roots": ["data"],
            "reason": "write_amplification_blocked",
        },
    )

    asyncio.run(mod._project_async({"summary": {"value": "ready"}}, webspace_id="default"))

    assert applied == []
    assert published == ["ready"]
    assert mod._projection_diag["blocked_total"] == 1


def test_infrastate_get_snapshot_project_false_does_not_project_fallback(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda **_kwargs: {
            "fallback": True,
            "summary": {"value": "degraded"},
            "projection_diag": {},
        },
    )
    monkeypatch.setattr(
        mod,
        "_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("project should not run")),
    )

    result = mod.get_snapshot(webspace_id="desktop", project=False)

    assert result["summary"]["value"] == "degraded"


def test_infrastate_project_async_excludes_stream_sections_from_yjs(monkeypatch):
    mod = _load_infrastate_module()
    projected: list[tuple[str, object]] = []
    published: list[tuple[str, object, str | None]] = []
    mod._projection_fingerprints.clear()
    mod._projection_diag.update({"apply_total": 0, "skip_total": 0, "cache_hit_total": 0})

    async def _fake_set_async(slot, value, *, user_id=None, webspace_id=None):
        projected.append((slot, value))

    monkeypatch.setattr(mod, "ctx_subnet", SimpleNamespace(set_async=_fake_set_async))
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: ["default"])
    monkeypatch.setattr(
        mod,
        "_publish_stream_payload",
        lambda *, receiver, data, webspace_id=None, force=False: published.append((receiver, data, webspace_id)),
    )

    snapshot = {
        "summary": {"value": "ready"},
        "operations": {"items": [{"id": "op-1"}], "active": [{"id": "op-1"}]},
        "logs": [{"id": "log-1"}],
        "events": [{"id": "evt-1"}],
        "yjs_runtime": {"load_mark": {"selected_webspace": {"items": [{"root": "data"}]}}},
    }

    asyncio.run(mod._project_async(snapshot, webspace_id="default"))

    assert projected == [
        ("infrastate.summary", {"value": "ready"}),
        ("infrastate.operations.active", [{"id": "op-1"}]),
    ]
    assert published == [
        ("infrastate.operations.active", [{"id": "op-1"}], "default"),
    ]


def test_infrastate_webspace_reload_invalidates_projection_and_stream_state(monkeypatch):
    mod = _load_infrastate_module()
    scheduled: list[dict[str, object]] = []

    mod._projection_fingerprints.clear()
    mod._projection_last_applied_at.clear()
    mod._stream_fingerprints.clear()
    mod._stream_last_published_at.clear()
    mod._projection_fingerprints["desktop"] = "fp-1"
    mod._projection_last_applied_at["desktop"] = 123.0
    stream_key = mod._stream_cache_key("desktop", "infrastate.logs.recent")
    mod._stream_fingerprints[stream_key] = "stream-fp"
    mod._stream_last_published_at[stream_key] = 456.0

    monkeypatch.setattr(mod, "_schedule_snapshot_refresh", lambda **kwargs: scheduled.append(dict(kwargs)))

    evt = SimpleNamespace(type="desktop.webspace.reloaded", payload={"webspace_id": "desktop"})
    mod.on_webspace_reload(evt)

    assert "desktop" not in mod._projection_fingerprints
    assert "desktop" not in mod._projection_last_applied_at
    assert stream_key not in mod._stream_fingerprints
    assert stream_key not in mod._stream_last_published_at
    assert scheduled == [{"webspace_id": "desktop", "reason": "desktop.webspace.reloaded"}]


def test_infrastate_stream_snapshot_request_publishes_requested_receiver(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    cache_flags: list[bool] = []

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda webspace_id=None, allow_cache=True: (
            cache_flags.append(bool(allow_cache)),
            {
                "operations": {"items": [{"id": "op-1"}], "active": [{"id": "op-1"}]},
                "logs": [{"id": "log-1"}],
                "events": [{"id": "evt-1"}],
                "yjs_runtime": {"load_mark": {"selected_webspace": {"items": [{"root": "data"}]}}},
            },
        )[1],
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.logs.recent",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.logs.recent", [{"id": "log-1"}], "default"),
    ]
    assert cache_flags == [True]


def test_infrastate_operations_stream_request_uses_direct_sdk_builder(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("operations stream should not build full snapshot")),
    )
    monkeypatch.setattr(
        mod,
        "get_operation_manager",
        lambda: SimpleNamespace(
            snapshot=lambda webspace_id=None: {
                "active_items": [{"id": "op-1", "webspace_id": webspace_id}],
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.operations.active",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.operations.active", [{"id": "op-1", "webspace_id": "default"}], "default"),
    ]


def test_infrastate_stream_snapshot_request_bypasses_noncritical_guardrail(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []
    suppressions: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda webspace_id=None, allow_cache=True: {"logs": [{"id": "log-1"}]},
    )
    monkeypatch.setattr(
        mod,
        "_active_noncritical_stream_guardrail",
        lambda webspace_id, receiver: {"active": True, "reason": "yjs_pressure"},
    )
    monkeypatch.setattr(
        mod,
        "_record_noncritical_stream_guardrail_suppression",
        lambda **kwargs: suppressions.append(kwargs),
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None: published.append((receiver, data, (_meta or {}).get("webspace_id"))),
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.logs.recent",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        ("infrastate.logs.recent", [{"id": "log-1"}], "default"),
    ]
    assert suppressions == []


def test_infrastate_cached_snapshot_coalesces_concurrent_stream_requests(monkeypatch):
    mod = _load_infrastate_module()
    calls: list[str | None] = []
    start = threading.Event()

    monkeypatch.setattr(mod, "_SNAPSHOT_CACHE_TTL_S", 30.0)
    mod._snapshot_cache.clear()

    def _slow_snapshot(*, webspace_id=None):
        calls.append(webspace_id)
        time.sleep(0.05)
        return {"summary": {"value": "ready"}, "webspace_id": webspace_id}

    monkeypatch.setattr(mod, "_snapshot_or_fallback", _slow_snapshot)

    results: list[dict[str, object]] = []

    def _worker() -> None:
        start.wait(timeout=5)
        results.append(mod._snapshot_or_fallback_cached(webspace_id="default", allow_cache=True))

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 5
    assert calls == ["default"]


def test_infrastate_stream_snapshot_request_supports_yjs_load_mark(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda webspace_id=None, allow_cache=True: {
            "yjs_runtime": {"load_mark": {"selected_webspace": {"items": [{"root": "ui", "peak_bps": 12.0}]}}},
        },
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.yjs.load_mark",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        (
            "infrastate.yjs.load_mark",
            [{"root": "ui", "peak_bps": 12.0, "kind": "root", "id": "ui", "display": "ui"}],
            "default",
        ),
    ]


def test_infrastate_stream_snapshot_request_supports_yjs_load_mark_from_reliability_runtime(monkeypatch):
    mod = _load_infrastate_module()
    published: list[tuple[str, object, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback_cached",
        lambda webspace_id=None, allow_cache=True: {
            "reliability": {
                "runtime": {
                    "sync_runtime": {
                        "load_mark": {
                            "selected_webspace": {
                                "items": [{"root": "registry", "avg_bps": 7.0}],
                            }
                        }
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data, _meta=None, **kwargs: published.append((receiver, data, (_meta or {}).get("webspace_id"))) or {"ok": True},
    )

    mod.on_webio_stream_snapshot_requested(
        SimpleNamespace(
            payload={
                "receiver": "infrastate.yjs.load_mark",
                "webspace_id": "default",
            }
        )
    )

    assert published == [
        (
            "infrastate.yjs.load_mark",
            [{"root": "registry", "avg_bps": 7.0, "kind": "root", "id": "registry", "display": "registry"}],
            "default",
        ),
    ]


def test_infrastate_runtime_event_invalidates_snapshot_cache(monkeypatch):
    mod = _load_infrastate_module()
    invalidated: list[str | None] = []
    refreshed: list[tuple[str | None, str]] = []
    appended: list[str] = []

    monkeypatch.setattr(
        mod,
        "_invalidate_runtime_caches",
        lambda *, webspace_id=None, marketplace=False: invalidated.append(webspace_id),
    )
    monkeypatch.setattr(mod, "_append_event", lambda event_type, payload: appended.append(event_type))
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda *, webspace_id=None, reason="runtime.event": refreshed.append((webspace_id, reason)),
    )

    asyncio.run(
        mod.on_runtime_event(
            SimpleNamespace(
                type="core.update.status",
                payload={
                    "state": "succeeded",
                    "webspace_id": "default",
                },
            )
        )
    )

    assert invalidated == ["default"]
    assert appended == ["core.update.status"]
    assert refreshed == [("default", "core.update.status")]


def test_infrastate_marketplace_hides_skills_installed_via_scenario_dependencies(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [{"kind": "skill", "id": "prompt_engineer_skill", "name": "prompt_engineer_skill", "version": "0.5.0"}]
            if kind == "skills"
            else [{"kind": "scenario", "id": "prompt_engineer_scenario", "name": "prompt_engineer_scenario", "version": "0.2.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {"depends": ["prompt_engineer_skill"]} if name == "prompt_engineer_scenario" else {})

    items = mod._marketplace_items(webspace_id="default")

    assert items["skills"] == []
    assert [item["id"] for item in items["scenarios"]] == ["prompt_engineer_scenario"]


def test_infrastate_effective_runtime_projection_prefers_validated_target_slot():
    mod = _load_infrastate_module()

    slots_payload = {
        "active_slot": "A",
        "previous_slot": "B",
        "slots": {
            "A": {"manifest": {"slot": "A", "git_short_commit": "ddeb33f", "git_commit": "ddeb33f-old"}},
            "B": {"manifest": {"slot": "B", "git_short_commit": "stale-b"}},
        },
    }
    build = {
        "runtime_version": "old",
        "runtime_git_commit": "ddeb33f-old",
        "runtime_git_short_commit": "ddeb33f",
        "runtime_git_branch": "HEAD",
        "runtime_git_subject": "old subject",
    }
    status = {
        "state": "succeeded",
        "phase": "validate",
        "target_slot": "B",
        "manifest": {
            "slot": "B",
            "target_version": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_commit": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_short_commit": "8dd3543",
            "git_branch": "HEAD",
            "git_subject": "feat: add skill-aware infra_access publication and Root MCP token lifecycle management",
        },
    }

    effective_slots, effective_build = mod._effective_runtime_projection(status, {}, slots_payload, build)

    assert effective_slots["active_slot"] == "B"
    assert effective_slots["previous_slot"] == "A"
    assert effective_slots["slots"]["B"]["manifest"]["git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_commit"] == "8dd3543c72f912ef0d7932f4c5754ce4c6700849"


def test_infrastate_skill_runtime_migration_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_migration_report(
        {},
        {
            "manifest": {
                "skill_runtime_migration": {
                    "total": 2,
                    "failed_total": 1,
                    "lifecycle_failed_total": 1,
                    "rollback_total": 1,
                    "skills": [
                        {"skill": "weather_skill", "ok": True},
                        {"skill": "voice_skill", "ok": False, "failure_kind": "lifecycle", "failed_stage": "rehydrate"},
                    ],
                }
            }
        },
    )
    note = mod._skill_runtime_migration_note(report)

    assert report["failed_total"] == 1
    assert "skill_migration=1/2" in note
    assert "voice_skill:lifecycle/rehydrate" in note
    assert "lifecycle_failed=1" in note
    assert "rollback=1" in note


def test_infrastate_skill_runtime_rollback_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_rollback_report(
        {
            "skill_runtime_rollback": {
                "total": 3,
                "failed_total": 1,
                "rollback_total": 2,
                "skipped_total": 1,
                "skills": [
                    {"skill": "weather_skill", "ok": True},
                    {"skill": "voice_skill", "ok": False, "error": "broken rollback"},
                    {"skill": "maps_skill", "ok": True, "skipped": True},
                ],
            }
        },
        {},
    )
    note = mod._skill_runtime_rollback_note(report)

    assert report["failed_total"] == 1
    assert "skill_rollback=2/3" in note
    assert "failed=voice_skill" in note
    assert "skipped=1" in note


def test_infrastate_skill_post_commit_helpers_report_deactivations():
    mod = _load_infrastate_module()

    report = mod._skill_post_commit_checks_report(
        {
            "skill_post_commit_checks": {
                "total": 2,
                "failed_total": 1,
                "lifecycle_failed_total": 1,
                "deactivated_total": 1,
                "skills": [
                    {"skill": "weather_skill", "ok": True},
                    {
                        "skill": "voice_skill",
                        "ok": False,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                        "deactivated": True,
                        "deactivation": {
                            "committed_core_switch": True,
                            "failure_kind": "lifecycle",
                            "failed_stage": "rehydrate",
                        },
                    },
                ],
            }
        },
        {},
    )
    note = mod._skill_post_commit_checks_note(report)

    assert report["deactivated_total"] == 1
    assert "skill_post_commit=1/2" in note
    assert "voice_skill:lifecycle/rehydrate" in note
    assert "lifecycle_failed=1" in note
    assert "deactivated=1" in note
    assert "quarantine=voice_skill:lifecycle/rehydrate" in note


def test_infrastate_skill_post_commit_helpers_report_existing_quarantine():
    mod = _load_infrastate_module()

    note = mod._skill_post_commit_checks_note(
        {
            "total": 1,
            "failed_total": 0,
            "deactivated_total": 1,
            "skills": [
                {
                    "skill": "voice_skill",
                    "ok": True,
                    "skipped": True,
                    "deactivated": True,
                    "deactivation": {
                        "committed_core_switch": True,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                    },
                }
            ],
        }
    )

    assert "skill_post_commit=1/1" in note
    assert "quarantine=voice_skill:lifecycle/rehydrate" in note


def test_infrastate_core_update_diagnostics_include_required_local_payloads(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()

    base_dir = tmp_path / ".adaos"
    (base_dir / "state" / "supervisor").mkdir(parents=True, exist_ok=True)
    runtime_path = base_dir / "state" / "supervisor" / "runtime.json"
    runtime_path.write_text('{"runtime_state":"spawned","managed_matches_active_slot":false}', encoding="utf-8")
    slot_dir = base_dir / "state" / "core_slots" / "slots" / "B"
    (slot_dir / "repo" / "src").mkdir(parents=True, exist_ok=True)
    (slot_dir / "venv" / "bin").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod, "_base_dir", lambda: base_dir)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="journal tail line\nsecond line", stderr="", returncode=0),
    )

    last_result = {
        "state": "failed",
        "phase": "apply",
        "target_slot": "B",
        "message": "core update command failed",
    }
    status = {"state": "idle"}
    slots_payload = {"inactive_slot": "B"}

    items = mod._core_update_diagnostic_items(status, last_result, slots_payload, local_node=True)
    actions = mod._core_update_diagnostic_actions(items)

    by_id = {item["id"]: item for item in items}
    assert "core-update-last-result" in by_id
    assert "supervisor-runtime" in by_id
    assert "target-slot-tree" in by_id
    assert "adaos-service-journal" in by_id
    assert "journal tail line" in by_id["adaos-service-journal"]["content"]
    assert "repo/src" in by_id["target-slot-tree"]["content"]
    assert any(item["id"] == "copy_core_update_diag_bundle" for item in actions)
    assert any(item["id"] == "copy_core_update_diag_commands" for item in actions)


def test_infrastate_core_update_diagnostics_skip_local_files_for_remote_member(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(mod, "_base_dir", lambda: Path("/base"))
    items = mod._core_update_diagnostic_items(
        {"state": "idle"},
        {"state": "failed", "target_slot": "B"},
        {"inactive_slot": "B"},
        local_node=False,
    )

    ids = [item["id"] for item in items]
    assert "core-update-diagnostic-commands" in ids
    assert "core-update-last-result" in ids
    assert "supervisor-runtime" not in ids
    assert "target-slot-tree" not in ids


def test_infrastate_post_local_admin_prefers_supervisor_for_update_routes(monkeypatch):
    mod = _load_infrastate_module()

    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _Resp({"ok": True, "_served_by": "supervisor"})

    monkeypatch.setattr(mod.requests, "post", _post)
    monkeypatch.setenv("ADAOS_SUPERVISOR_HOST", "127.0.0.1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PORT", "8776")
    monkeypatch.setattr(mod, "_self_base_url", lambda conf: "http://127.0.0.1:8777")

    payload = mod._post_local_admin(SimpleNamespace(token="dev-token"), "/api/admin/update/start", {"reason": "test"})

    assert payload["_served_by"] == "supervisor"
    assert calls == ["http://127.0.0.1:8776/api/supervisor/update/start"]


def test_infrastate_post_local_admin_falls_back_to_runtime_admin_when_supervisor_is_unavailable(monkeypatch):
    mod = _load_infrastate_module()

    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if "8776" in url:
            raise RuntimeError("supervisor unavailable")
        return _Resp({"ok": True, "_served_by": "runtime"})

    monkeypatch.setattr(mod.requests, "post", _post)
    monkeypatch.setenv("ADAOS_SUPERVISOR_HOST", "127.0.0.1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PORT", "8776")
    monkeypatch.setattr(mod, "_self_base_url", lambda conf: "http://127.0.0.1:8777")

    payload = mod._post_local_admin(SimpleNamespace(token="dev-token"), "/api/admin/update/cancel", {"reason": "test"})

    assert payload["_served_by"] == "runtime"
    assert calls == [
        "http://127.0.0.1:8776/api/supervisor/update/cancel",
        "http://127.0.0.1:8777/api/admin/update/cancel",
    ]
