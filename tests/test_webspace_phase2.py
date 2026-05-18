from __future__ import annotations

import asyncio
import sys
import time
import types
import importlib
from types import SimpleNamespace

import pytest

from adaos.services.agent_context import get_ctx

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.services.scenario import webspace_runtime as webspace_runtime_module
from adaos.services.workspaces import (
    ensure_workspace,
    get_workspace,
    set_workspace_installed_overlay,
    set_workspace_manifest,
    set_workspace_pinned_widgets_overlay,
    set_workspace_topbar_overlay,
    set_workspace_page_schema_overlay,
)


def test_build_local_desktop_catalog_snapshot_uses_runtime_skill_decls(monkeypatch) -> None:
    captured_modes: list[str] = []

    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Node 1",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="member", node_id="node-1", node_settings=SimpleNamespace(node_names=[])),
    )

    def _fake_collect(self, mode: str = "mixed", *, include_remote: bool = True) -> list[dict[str, object]]:  # noqa: ARG001
        captured_modes.append(mode)
        return [
            {
                "skill": "member_skill",
                "space": "default",
                "apps": [{"id": "member_app", "title": "Member App"}],
                "widgets": [{"id": "member_widget", "title": "Member Widget"}],
            }
        ]

    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", _fake_collect)

    snapshot = webspace_runtime_module.build_local_desktop_catalog_snapshot(mode="workspace")

    assert captured_modes == ["workspace"]
    assert snapshot["apps"][0]["id"] == "member_app"
    assert snapshot["apps"][0]["node_id"] == "node-1"
    assert snapshot["apps"][0]["node_label"] == "Node 1"
    assert snapshot["widgets"][0]["id"] == "member_widget"


def test_build_local_desktop_catalog_snapshot_prefers_live_ydoc_values_over_decl_defaults(monkeypatch) -> None:
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Node 1",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="member", node_id="node-1", node_settings=SimpleNamespace(node_names=[])),
    )

    def _fake_collect(self, mode: str = "mixed", *, include_remote: bool = True) -> list[dict[str, object]]:  # noqa: ARG001
        return [
            {
                "skill": "infrastate_skill",
                "space": "default",
                "apps": [],
                "widgets": [],
                "ydoc_defaults": {
                    "data/infrastate/summary": {
                        "label": "Core update",
                        "value": "idle",
                        "subtitle": "slot --",
                        "description": "No update in progress",
                    }
                },
            }
        ]

    class _Map:
        def __init__(self, data):
            self._data = data

        def get(self, key):
            value = self._data.get(key)
            if isinstance(value, dict):
                return _Map(value)
            return value

        def items(self):
            return self._data.items()

        def items(self):
            return self._data.items()

        def items(self):
            return self._data.items()

        def items(self):
            return self._data.items()

        def items(self):
            return self._data.items()

    class _YDoc:
        def __init__(self, data):
            self._data = data

        def get_map(self, key):
            value = self._data.get(key, {})
            return _Map(value if isinstance(value, dict) else {})

    class _CtxMgr:
        def __enter__(self):
            return _YDoc(
                {
                    "data": {
                        "nodes": {
                            "node-1": {
                                "infrastate": {
                                    "summary": {
                                        "label": "Core update",
                                        "value": "succeeded",
                                        "subtitle": "slot B | 2ac1fa3",
                                        "description": "runtime boot validated on slot B",
                                    }
                                }
                            }
                        }
                    }
                }
            )

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", _fake_collect)
    monkeypatch.setattr(webspace_runtime_module, "get_ydoc", lambda webspace_id: _CtxMgr())

    snapshot = webspace_runtime_module.build_local_desktop_catalog_snapshot(mode="workspace")

    assert snapshot["ydoc_defaults"]["data/nodes/node-1/infrastate/summary"] == {
        "label": "Core update",
        "value": "succeeded",
        "subtitle": "slot B | 2ac1fa3",
        "description": "runtime boot validated on slot B",
    }


def test_build_local_desktop_catalog_snapshot_reads_node_scoped_defaults_from_local_unscoped_skill_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Node 1",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="member", node_id="node-1", node_settings=SimpleNamespace(node_names=[])),
    )

    def _fake_collect(self, mode: str = "mixed", *, include_remote: bool = True) -> list[dict[str, object]]:  # noqa: ARG001
        return [
            {
                "skill": "infrastate_skill",
                "space": "default",
                "apps": [],
                "widgets": [],
                "ydoc_defaults": {
                    "data/infrastate": {
                        "summary": {
                            "label": "Core update",
                            "value": "idle",
                            "subtitle": "slot --",
                            "description": "No update in progress",
                        },
                        "update_actions": [
                            {
                                "id": "marketplace",
                                "title": "Marketplace",
                            }
                        ],
                    }
                },
            }
        ]

    class _Map:
        def __init__(self, data):
            self._data = data

        def get(self, key):
            value = self._data.get(key)
            if isinstance(value, dict):
                return _Map(value)
            return value

        def items(self):
            return self._data.items()

    class _YDoc:
        def __init__(self, data):
            self._data = data

        def get_map(self, key):
            value = self._data.get(key, {})
            return _Map(value if isinstance(value, dict) else {})

    class _CtxMgr:
        def __enter__(self):
            return _YDoc(
                {
                    "data": {
                        "infrastate": {
                            "summary": {
                                "label": "Core update",
                                "value": "validated",
                                "subtitle": "slot A | c8d43f5",
                                "description": "runtime boot validated on slot A; root promotion pending",
                            },
                            "update_actions": [
                                {
                                    "id": "marketplace",
                                    "title": "Marketplace",
                                },
                                {
                                    "id": "start_update",
                                    "title": "Start update",
                                },
                            ],
                        }
                    }
                }
            )

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", _fake_collect)
    monkeypatch.setattr(webspace_runtime_module, "get_ydoc", lambda _webspace_id: _CtxMgr())

    snapshot = webspace_runtime_module.build_local_desktop_catalog_snapshot()

    assert snapshot["ydoc_defaults"]["data/nodes/node-1/infrastate"] == {
        "summary": {
            "label": "Core update",
            "value": "validated",
            "subtitle": "slot A | c8d43f5",
            "description": "runtime boot validated on slot A; root promotion pending",
        },
        "update_actions": [
            {
                "id": "marketplace",
                "title": "Marketplace",
            },
            {
                "id": "start_update",
                "title": "Start update",
            },
        ],
    }


def test_build_local_desktop_catalog_snapshot_prefers_live_room_doc_without_sync_get_ydoc(monkeypatch) -> None:
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Node 1",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="member", node_id="node-1", node_settings=SimpleNamespace(node_names=[])),
    )

    def _fake_collect(self, mode: str = "mixed", *, include_remote: bool = True) -> list[dict[str, object]]:  # noqa: ARG001
        return [
            {
                "skill": "infrastate_skill",
                "space": "default",
                "apps": [],
                "widgets": [],
                "ydoc_defaults": {
                    "data/infrastate": {
                        "summary": {
                            "label": "Core update",
                            "value": "idle",
                            "subtitle": "slot --",
                            "description": "No update in progress",
                        }
                    }
                },
            }
        ]

    class _Map:
        def __init__(self, data):
            self._data = data

        def get(self, key):
            value = self._data.get(key)
            if isinstance(value, dict):
                return _Map(value)
            return value

        def items(self):
            return self._data.items()

    class _YDoc:
        def __init__(self, data):
            self._data = data

        def get_map(self, key):
            value = self._data.get(key, {})
            return _Map(value if isinstance(value, dict) else {})

    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", _fake_collect)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_resolve_live_room_ydoc",
        lambda _webspace_id: _YDoc(
            {
                "data": {
                    "infrastate": {
                        "summary": {
                            "label": "Core update",
                            "value": "succeeded",
                            "subtitle": "slot A | c8d43f5",
                            "description": "runtime boot validated on slot A",
                        }
                    }
                }
            }
        ),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "get_ydoc",
        lambda _webspace_id: (_ for _ in ()).throw(AssertionError("sync get_ydoc should not run when live room doc exists")),
    )

    snapshot = webspace_runtime_module.build_local_desktop_catalog_snapshot()

    assert snapshot["ydoc_defaults"]["data/nodes/node-1/infrastate"] == {
        "summary": {
            "label": "Core update",
            "value": "succeeded",
            "subtitle": "slot A | c8d43f5",
            "description": "runtime boot validated on slot A",
        }
    }


@pytest.mark.asyncio
async def test_build_local_desktop_catalog_snapshot_async_reads_local_unscoped_skill_projection(monkeypatch) -> None:
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Node 1",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="member", node_id="node-1", node_settings=SimpleNamespace(node_names=[])),
    )

    def _fake_collect(self, mode: str = "mixed", *, include_remote: bool = True) -> list[dict[str, object]]:  # noqa: ARG001
        return [
            {
                "skill": "infrastate_skill",
                "space": "default",
                "apps": [],
                "widgets": [],
                "ydoc_defaults": {
                    "data/infrastate": {
                        "summary": {
                            "label": "Core update",
                            "value": "idle",
                            "subtitle": "slot --",
                            "description": "No update in progress",
                        }
                    }
                },
            }
        ]

    class _Map:
        def __init__(self, data):
            self._data = data

        def get(self, key):
            value = self._data.get(key)
            if isinstance(value, dict):
                return _Map(value)
            return value

        def items(self):
            return self._data.items()

    class _YDoc:
        def __init__(self, data):
            self._data = data

        def get_map(self, key):
            value = self._data.get(key, {})
            return _Map(value if isinstance(value, dict) else {})

    class _AsyncCtxMgr:
        async def __aenter__(self):
            return _YDoc(
                {
                    "data": {
                        "infrastate": {
                            "summary": {
                                "label": "Core update",
                                "value": "succeeded",
                                "subtitle": "slot A | c8d43f5",
                                "description": "runtime boot validated on slot A",
                            }
                        }
                    }
                }
            )

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", _fake_collect)
    monkeypatch.setattr(webspace_runtime_module, "_resolve_live_room_ydoc", lambda _webspace_id: None)
    monkeypatch.setattr(webspace_runtime_module, "async_read_ydoc", lambda _webspace_id: _AsyncCtxMgr())

    snapshot = await webspace_runtime_module.build_local_desktop_catalog_snapshot_async()

    assert snapshot["ydoc_defaults"]["data/nodes/node-1/infrastate"] == {
        "summary": {
            "label": "Core update",
            "value": "succeeded",
            "subtitle": "slot A | c8d43f5",
            "description": "runtime boot validated on slot A",
        }
    }


def test_member_snapshot_changed_rebuilds_shared_workspaces_with_rate_limit(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        webspace_runtime_module.workspace_index,
        "list_workspaces",
        lambda: [
            SimpleNamespace(workspace_id="desktop", is_dev=False),
            SimpleNamespace(workspace_id="dev-infrascope", is_dev=True),
        ],
    )
    monkeypatch.setattr(webspace_runtime_module, "_member_snapshot_rebuild_min_interval_s", lambda: 60.0)
    webspace_runtime_module._MEMBER_SNAPSHOT_REBUILD_AT.clear()

    async def _fake_rebuild(webspace_id: str, *, action: str, source_of_truth: str, **_kwargs):
        calls.append((webspace_id, action, source_of_truth))
        return {"accepted": True}

    monkeypatch.setattr(webspace_runtime_module, "rebuild_webspace_from_sources", _fake_rebuild)
    webspace_runtime_module._MEMBER_SNAPSHOT_REBUILD_TASKS.clear()

    async def _exercise() -> None:
        await webspace_runtime_module._on_subnet_member_snapshot_changed({"node_id": "member-1"})
        await asyncio.sleep(0)
        await webspace_runtime_module._on_subnet_member_snapshot_changed({"node_id": "member-1"})
        await asyncio.sleep(0)

    asyncio.run(_exercise())

    assert calls == [("desktop", "subnet_member_snapshot_sync", "member_runtime_snapshot")]


def test_remote_member_catalog_entries_are_node_scoped_and_auto_installed(monkeypatch) -> None:
    previous_directory_module = sys.modules.get("adaos.services.registry.subnet_directory")
    directory_module = types.ModuleType("adaos.services.registry.subnet_directory")
    directory_module.get_directory = lambda: SimpleNamespace(
        list_known_nodes=lambda: [
            {
                "node_id": "member-1",
                "roles": ["member"],
                "node_label": "Node 1",
                "node_compact_label": "N1",
                "runtime_projection": {
                    "snapshot": {
                        "desktop_catalog": {
                            "apps": [{"id": "infrastate_app", "title": "Infra State"}],
                            "widgets": [{"id": "infrastate_widget", "title": "Infra State"}],
                        }
                    }
                },
            }
        ]
    )
    sys.modules["adaos.services.registry.subnet_directory"] = directory_module
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="hub", node_id="hub-1", node_settings=SimpleNamespace(node_names=[])),
    )
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub-1")
    try:
        runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace())
        decls = runtime._collect_remote_skill_decls()
    finally:
        if previous_directory_module is None:
            sys.modules.pop("adaos.services.registry.subnet_directory", None)
        else:
            sys.modules["adaos.services.registry.subnet_directory"] = previous_directory_module
        importlib.invalidate_caches()

    assert len(decls) == 1
    decl = decls[0]
    assert decl["apps"][0]["id"] == "node:member-1:infrastate_app"
    assert decl["apps"][0]["node_local_id"] == "infrastate_app"
    assert decl["apps"][0]["node_label"] == "Node 1"
    assert decl["widgets"][0]["id"] == "node:member-1:infrastate_widget"
    assert decl["contributions"] == [
        {
            "extensionPoint": "desktop.apps",
            "type": "app",
            "id": "node:member-1:infrastate_app",
            "autoInstall": True,
        },
        {
            "extensionPoint": "desktop.widgets",
            "type": "widget",
            "id": "node:member-1:infrastate_widget",
            "autoInstall": True,
        },
    ]


def test_remote_member_catalog_entries_collapse_existing_node_scope(monkeypatch) -> None:
    previous_directory_module = sys.modules.get("adaos.services.registry.subnet_directory")
    directory_module = types.ModuleType("adaos.services.registry.subnet_directory")
    directory_module.get_directory = lambda: SimpleNamespace(
        list_known_nodes=lambda: [
            {
                "node_id": "member-1",
                "roles": ["member"],
                "runtime_projection": {
                    "snapshot": {
                        "desktop_catalog": {
                            "apps": [
                                {
                                    "id": "node:member-1:weather_skill",
                                    "title": "Weather",
                                    "launchModal": "node:old-hub:node:member-1:weather_modal",
                                    "node_local_id": "node:member-1:weather_skill",
                                    "remote_id": "node:member-1:weather_skill",
                                }
                            ],
                            "registry": {
                                "modals": {
                                    "node:member-1:weather_modal": {
                                        "title": "Weather Settings",
                                    }
                                }
                            },
                        }
                    }
                },
            }
        ]
    )
    sys.modules["adaos.services.registry.subnet_directory"] = directory_module
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="hub", node_id="hub-1", node_settings=SimpleNamespace(node_names=[])),
    )
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub-1")
    try:
        runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace())
        decls = runtime._collect_remote_skill_decls()
    finally:
        if previous_directory_module is None:
            sys.modules.pop("adaos.services.registry.subnet_directory", None)
        else:
            sys.modules["adaos.services.registry.subnet_directory"] = previous_directory_module
        importlib.invalidate_caches()

    assert len(decls) == 1
    app = decls[0]["apps"][0]
    assert app["id"] == "node:member-1:weather_skill"
    assert app["launchModal"] == "node:member-1:weather_modal"
    assert app["node_local_id"] == "weather_skill"
    assert app["remote_id"] == "weather_skill"
    assert "node:member-1:weather_modal" in decls[0]["registry"]["modals"]


def test_remote_member_catalog_skips_foreign_relay_entries(monkeypatch) -> None:
    previous_directory_module = sys.modules.get("adaos.services.registry.subnet_directory")
    directory_module = types.ModuleType("adaos.services.registry.subnet_directory")
    directory_module.get_directory = lambda: SimpleNamespace(
        list_known_nodes=lambda: [
            {
                "node_id": "hub-relay",
                "roles": ["hub"],
                "runtime_projection": {
                    "snapshot": {
                        "desktop_catalog": {
                            "apps": [
                                {
                                    "id": "node:member-1:weather_skill",
                                    "title": "Weather",
                                    "origin": "skill:subnet.member.member-1",
                                }
                            ],
                            "ydoc_defaults": {
                                "data/nodes/member-1/weather/current": {"city": "Berlin"},
                            },
                        }
                    }
                },
            }
        ]
    )
    sys.modules["adaos.services.registry.subnet_directory"] = directory_module
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="hub", node_id="hub-1", node_settings=SimpleNamespace(node_names=[])),
    )
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub-1")
    try:
        runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace())
        decls = runtime._collect_remote_skill_decls()
    finally:
        if previous_directory_module is None:
            sys.modules.pop("adaos.services.registry.subnet_directory", None)
        else:
            sys.modules["adaos.services.registry.subnet_directory"] = previous_directory_module
        importlib.invalidate_caches()

    assert decls == []


def test_member_snapshot_change_seeds_ydoc_defaults_without_waiting_for_rebuild(monkeypatch) -> None:
    seeded: dict[str, object] = {}

    class _FakeDataMap:
        def __init__(self) -> None:
            self._data: dict[str, object] = {}

        def to_json(self) -> dict[str, object]:
            return self._data

        def get(self, key: str) -> object:
            return self._data.get(key)

        def set(self, _txn: object, key: str, value: object) -> None:
            self._data[key] = value

    class _FakeDoc:
        def __init__(self) -> None:
            self.data_map = _FakeDataMap()

        def get_map(self, name: str) -> _FakeDataMap:
            assert name == "data"
            return self.data_map

        class _Txn:
            def __enter__(self) -> object:
                return object()

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        def begin_transaction(self) -> "_FakeDoc._Txn":
            return _FakeDoc._Txn()

    fake_doc = _FakeDoc()

    class _AsyncDocCtx:
        async def __aenter__(self) -> _FakeDoc:
            return fake_doc

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _AsyncMetaCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    previous_directory_module = sys.modules.get("adaos.services.registry.subnet_directory")
    directory_module = types.ModuleType("adaos.services.registry.subnet_directory")
    directory_module.get_directory = lambda: SimpleNamespace(
        get_node=lambda _node_id: {
            "runtime_projection": {
                "snapshot": {
                    "desktop_catalog": {
                        "ydoc_defaults": {
                            "data/nodes/member-1/weather/current": {"city": "Berlin"},
                            "data/nodes/member-1/infrastate": {"state": "succeeded"},
                        }
                    }
                }
            }
        }
    )
    sys.modules["adaos.services.registry.subnet_directory"] = directory_module

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda *_args, **_kwargs: _AsyncDocCtx())
    monkeypatch.setattr(webspace_runtime_module, "_webspace_runtime_async_write_meta", lambda **_kwargs: _AsyncMetaCtx())
    monkeypatch.setattr(
        webspace_runtime_module.workspace_index,
        "list_workspaces",
        lambda: [SimpleNamespace(workspace_id="desktop", is_dev=False)],
    )
    monkeypatch.setattr(webspace_runtime_module, "_member_snapshot_rebuild_min_interval_s", lambda: 60.0)
    webspace_runtime_module._MEMBER_SNAPSHOT_REBUILD_AT.clear()
    webspace_runtime_module._MEMBER_SNAPSHOT_REBUILD_TASKS.clear()

    async def _fake_rebuild(webspace_id: str, *, action: str, source_of_truth: str, **_kwargs):
        seeded["rebuild"] = (webspace_id, action, source_of_truth)
        return {"accepted": True}

    monkeypatch.setattr(webspace_runtime_module, "rebuild_webspace_from_sources", _fake_rebuild)

    async def _exercise() -> None:
        await webspace_runtime_module._on_subnet_member_snapshot_changed({"node_id": "member-1"})
        await asyncio.sleep(0)

    try:
        asyncio.run(_exercise())
    finally:
        if previous_directory_module is not None:
            sys.modules["adaos.services.registry.subnet_directory"] = previous_directory_module
        else:
            sys.modules.pop("adaos.services.registry.subnet_directory", None)

    nodes_bucket = fake_doc.data_map.to_json().get("nodes")
    assert isinstance(nodes_bucket, dict)
    assert nodes_bucket["member-1"]["weather"]["current"]["city"] == "Berlin"
    assert nodes_bucket["member-1"]["infrastate"]["state"] == "succeeded"
    assert seeded["rebuild"] == ("desktop", "subnet_member_snapshot_sync", "member_runtime_snapshot")


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeMap(dict):
    def set(self, txn, key: str, value: object) -> None:  # noqa: ARG002
        self[key] = value


class _CountingMap(_FakeMap):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.set_count = 0

    def set(self, txn, key: str, value: object) -> None:  # noqa: ARG002
        self.set_count += 1
        super().set(txn, key, value)


class _FakeDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state
        self.transaction_count = 0

    def get_map(self, name: str) -> _FakeMap:
        return self._state.setdefault(name, _FakeMap())

    def begin_transaction(self) -> _FakeTxn:
        self.transaction_count += 1
        return _FakeTxn()


class _FakeAsyncDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    async def __aenter__(self) -> _FakeDoc:
        return _FakeDoc(self._state)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def test_ydoc_defaults_create_node_scoped_nested_skill_state() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    fake_state = {
        "data": _FakeMap(
            {
                "nodes": {
                    "member-1": {
                        "weather": {
                            "current": {"city": "Paris"},
                        }
                    }
                }
            }
        )
    }
    fake_doc = _FakeDoc(fake_state)

    runtime._apply_ydoc_defaults_in_txn(
        fake_doc,
        _FakeTxn(),
        [
            {
                "skill": "weather_skill",
                "node_id": "member-1",
                "ydoc_defaults": {
                    "data/weather/current": {"city": "Moscow"},
                    "data/weather/cities": ["Moscow", "Paris"],
                },
            }
        ],
    )

    weather = fake_state["data"]["nodes"]["member-1"]["weather"]
    assert weather["current"] == {"city": "Paris"}
    assert weather["cities"] == ["Moscow", "Paris"]


def test_ydoc_defaults_keep_shared_skill_state_when_ui_owner_is_shared() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    fake_state = {
        "data": _FakeMap({})
    }
    fake_doc = _FakeDoc(fake_state)

    runtime._apply_ydoc_defaults_in_txn(
        fake_doc,
        _FakeTxn(),
        [
            {
                "skill": "demo_metrics_skill",
                "node_id": "member-1",
                "ui_owner": "shared",
                "ydoc_defaults": {
                    "data/demo_metrics/table": {"items": [{"id": "cpu"}]},
                },
            }
        ],
    )

    assert fake_state["data"]["demo_metrics"]["table"] == {"items": [{"id": "cpu"}]}
    assert "nodes" not in fake_state["data"] or "demo_metrics" not in fake_state["data"].get("nodes", {})


def test_describe_webspace_operational_state_exposes_manifest_and_current_scenario(monkeypatch) -> None:
    webspace_id = "phase2-describe"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_runtime"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(
        webspace_runtime_module,
        "_scenario_exists_for_switch",
        lambda scenario_id, *, space: scenario_id in {"prompt_engineer_scenario", "prompt_engineer_runtime"},
    )

    result = asyncio.run(webspace_runtime_module.describe_webspace_operational_state(webspace_id))

    assert result.webspace_id == webspace_id
    assert result.kind == "dev"
    assert result.source_mode == "dev"
    assert result.stored_home_scenario == "prompt_engineer_scenario"
    assert result.effective_home_scenario == "prompt_engineer_scenario"
    assert result.current_scenario == "prompt_engineer_runtime"
    assert result.to_dict()["current_matches_home"] is False
    assert result.stored_home_scenario_exists is True
    assert result.current_scenario_exists is True
    assert result.degraded is False


def test_describe_webspace_projection_state_reports_active_layer(monkeypatch) -> None:
    webspace_id = "phase4-projection-describe"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Projection Lab",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    class _Projections:
        def snapshot(self) -> dict[str, object]:
            return {
                "active_scenario_id": "prompt_engineer_scenario",
                "active_space": "workspace",
                "base_rule_count": 2,
                "scenario_rule_count": 1,
            }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace(projections=_Projections()))

    result = asyncio.run(webspace_runtime_module.describe_webspace_projection_state(webspace_id))

    assert result["webspace_id"] == webspace_id
    assert result["target_scenario"] == "prompt_engineer_scenario"
    assert result["target_space"] == "workspace"
    assert result["active_scenario"] == "prompt_engineer_scenario"
    assert result["active_space"] == "workspace"
    assert result["active_matches_target"] is True
    assert result["base_rule_count"] == 2
    assert result["scenario_rule_count"] == 1


def test_describe_webspace_projection_state_detects_space_mismatch(monkeypatch) -> None:
    webspace_id = "phase4-projection-dev-mismatch"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    class _Projections:
        def snapshot(self) -> dict[str, object]:
            return {
                "active_scenario_id": "prompt_engineer_scenario",
                "active_space": "workspace",
                "base_rule_count": 2,
                "scenario_rule_count": 1,
            }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace(projections=_Projections()))

    result = asyncio.run(webspace_runtime_module.describe_webspace_projection_state(webspace_id))

    assert result["target_space"] == "dev"
    assert result["active_space"] == "workspace"
    assert result["active_matches_target"] is False


def test_resolve_webspace_merges_webio_receivers_into_compact_runtime_contract(monkeypatch) -> None:
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    runtime = webspace_runtime_module.WebspaceScenarioRuntime()

    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="default",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={"desktop": {"pageSchema": {"id": "desktop", "layout": {"type": "single", "areas": [{"id": "main"}]}, "widgets": []}}},
            scenario_catalog={"apps": [], "widgets": []},
            scenario_registry={"modals": [], "widgets": []},
            overlay_snapshot={},
            live_state={},
            skill_decls=[
                {
                    "skill": "telemetry_skill",
                    "space": "default",
                    "node_id": "node-1",
                    "widgets": [
                        {
                            "id": "telemetry_widget",
                            "title": "Telemetry",
                            "dataSource": {"kind": "stream", "receiver": "telemetry_feed"},
                        }
                    ],
                    "webio": {
                        "receivers": {
                            "telemetry_feed": {
                                "mode": "append",
                                "collectionKey": "items",
                                "maxItems": 50,
                                "initialState": {"items": []},
                            }
                        }
                    },
                }
            ],
            desktop_scenarios=[],
        )
    )

    assert resolved.webio == {
        "receivers": {
            "telemetry_feed": {
                "id": "telemetry_feed",
                "mode": "append",
                "collectionKey": "items",
                "maxItems": 50,
                "initialState": {"items": []},
                "origin": "skill:telemetry_skill",
            }
        }
    }
    assert resolved.catalog["widgets"][0]["dataSource"]["nodeId"] == "node-1"


def test_collect_remote_skill_decls_uses_member_desktop_catalog_snapshot(monkeypatch) -> None:
    import adaos.services.registry.subnet_directory as subnet_directory_module

    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="hub", node_id="hub-1", node_names=["Hub"]),
    )
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub-1")

    class _Directory:
        def list_known_nodes(self) -> list[dict[str, object]]:
            return [
                {
                    "node_id": "member-1",
                    "roles": ["member"],
                    "display_index": 2,
                    "accent_index": 5,
                    "runtime_projection": {
                        "node_names": ["Edge One"],
                        "primary_node_name": "Edge One",
                        "snapshot": {
                            "desktop_catalog": {
                                "apps": [
                                    {
                                        "id": "weather_skill",
                                        "title": "Weather",
                                        "launchModal": "weather_modal",
                                        "dataSource": {"kind": "y", "path": "data/weather/current"},
                                    }
                                ],
                                "widgets": [
                                    {
                                        "id": "infrastate",
                                        "title": "Infra State",
                                        "dataSource": {"kind": "stream", "receiver": "infrastate.realtime"},
                                    }
                                ],
                                "registry": {
                                    "modals": {
                                        "weather_modal": {
                                            "title": "Weather Settings",
                                            "schema": {
                                                "widgets": [
                                                    {
                                                        "type": "selector",
                                                        "source": "data/weather/cities",
                                                    }
                                                ]
                                            },
                                        }
                                    }
                                },
                                "webio": {
                                    "receivers": {
                                        "infrastate.realtime": {
                                            "mode": "replace",
                                            "initialState": {"status": "idle"},
                                        }
                                    }
                                },
                                "ydoc_defaults": {
                                    "data/weather/current": {"city": "Moscow"},
                                },
                            }
                        },
                    },
                }
            ]

    monkeypatch.setattr(subnet_directory_module, "get_directory", lambda: _Directory())

    runtime = webspace_runtime_module.WebspaceScenarioRuntime()
    decls = runtime._collect_remote_skill_decls()

    assert len(decls) == 1
    assert decls[0]["skill"] == "subnet.member.member-1"
    assert decls[0]["node_id"] == "member-1"
    assert decls[0]["apps"][0]["node_label"] == "Edge One"
    assert decls[0]["apps"][0]["node_compact_label"] == "N2"
    assert decls[0]["apps"][0]["id"] == "node:member-1:weather_skill"
    assert decls[0]["apps"][0]["launchModal"] == "node:member-1:weather_modal"
    assert decls[0]["apps"][0]["dataSource"]["path"] == "data/nodes/member-1/weather/current"
    assert decls[0]["widgets"][0]["node_label"] == "Edge One"
    assert isinstance(decls[0]["widgets"][0]["node_color"], str) and decls[0]["widgets"][0]["node_color"]
    assert decls[0]["widgets"][0]["id"] == "node:member-1:infrastate"
    assert decls[0]["widgets"][0]["dataSource"]["nodeId"] == "member-1"
    assert decls[0]["registry"]["modals"]["node:member-1:weather_modal"]["schema"]["widgets"][0]["source"] == "data/nodes/member-1/weather/cities"
    assert decls[0]["webio"]["receivers"]["infrastate.realtime"]["mode"] == "replace"
    assert "nodeId" not in decls[0]["webio"]["receivers"]["infrastate.realtime"]
    assert decls[0]["ydoc_defaults"]["data/nodes/member-1/weather/current"] == {"city": "Moscow"}


def test_resolve_webspace_preserves_live_remote_entries_during_projection_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        webspace_runtime_module,
        "load_config",
        lambda: SimpleNamespace(role="hub", node_id="hub-1", node_names=["Hub"]),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "node_display_from_config",
        lambda _conf: {
            "node_label": "Hub",
            "node_compact_label": "N0",
            "node_index": 0,
            "node_color": "#4E79A7",
        },
    )

    runtime = webspace_runtime_module.WebspaceScenarioRuntime()
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="desktop",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={"desktop": {"pageSchema": {"id": "desktop"}}},
            scenario_catalog={"apps": [{"id": "hub_app", "title": "Hub App"}], "widgets": []},
            scenario_registry={"modals": ["apps_catalog"], "widgets": []},
            overlay_snapshot={},
            live_state={
                "application": {
                    "modals": {
                        "node:member-1:weather_modal": {
                            "title": "Weather Settings",
                        }
                    }
                },
                "catalog": {
                    "apps": [
                        {"id": "node:member-1:weather_skill", "title": "Weather", "node_id": "member-1"},
                    ],
                    "widgets": [
                        {"id": "node:member-1:infrastate", "title": "Infra State", "node_id": "member-1"},
                    ],
                },
                "registry": {
                    "modals": ["apps_catalog", "node:member-1:weather_modal"],
                    "widgets": [],
                },
                "desktop": {},
                "routing": {},
            },
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    app_ids = [str(item.get("id") or "") for item in resolved.catalog["apps"]]
    widget_ids = [str(item.get("id") or "") for item in resolved.catalog["widgets"]]
    assert "hub_app" in app_ids
    assert "node:member-1:weather_skill" in app_ids
    assert "node:member-1:infrastate" in widget_ids
    assert "node:member-1:weather_modal" in resolved.application["modals"]
    assert "node:member-1:weather_modal" in resolved.registry["modals"]


def _patch_switch_dependencies(monkeypatch, *, state: dict[str, _FakeMap] | None = None) -> dict[str, _FakeMap]:
    fake_state = state or {"ui": _FakeMap(), "registry": _FakeMap(), "data": _FakeMap()}
    fake_ctx = get_ctx()
    rebuilds: list[str] = []
    workflows: list[tuple[str, str]] = []
    sync_listing_calls: list[bool] = []

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG002
        rebuilds.append(webspace_id)
        self._last_rebuild_timings_ms = {
            "collect_inputs": 1.0,
            "resolve": 2.0,
            "apply_structure": 1.25,
            "apply_interactive": 1.5,
            "apply": 3.0,
            "to_registry_entry": 0.5,
            "total": 6.5,
        }
        self._last_apply_summary = {
            "branch_count": 6,
            "changed_branches": 3,
            "unchanged_branches": 3,
            "failed_branches": 0,
            "changed_paths": ["ui.application", "data.catalog", "registry.merged"],
            "defaults_failed": False,
            "phases": {
                "structure": {
                    "branch_count": 2,
                    "changed_branches": 2,
                    "unchanged_branches": 0,
                    "failed_branches": 0,
                    "changed_paths": ["ui.application", "registry.merged"],
                },
                "interactive": {
                    "branch_count": 4,
                    "changed_branches": 1,
                    "unchanged_branches": 3,
                    "failed_branches": 0,
                    "changed_paths": ["data.catalog"],
                },
            },
        }
        return SimpleNamespace(webspace_id=webspace_id)

    async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):
        workflows.append((scenario_id, webspace_id))
        return None

    async def _fake_sync_listing() -> None:
        sync_listing_calls.append(True)

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: True)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: {
            "id": scenario_id,
            "ui": {"application": {"desktop": {"pageSchema": {"id": f"page-{scenario_id}"}}}},
            "registry": {"modals": [f"modal:{space}:{scenario_id}"]},
            "catalog": {"apps": [{"id": f"app:{scenario_id}"}]},
            "data": {"status": {"scenario": scenario_id, "space": space}},
        },
    )
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    fake_state["_meta"] = _FakeMap({"rebuilds": rebuilds, "workflows": workflows, "listing_syncs": sync_listing_calls})
    return fake_state


def test_switch_webspace_scenario_can_persist_home_scenario(monkeypatch) -> None:
    webspace_id = "phase2-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Home",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            set_home=True,
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["_meta"]["rebuilds"] == [webspace_id]
    assert fake_state["_meta"]["workflows"] == [("prompt_engineer_scenario", webspace_id)]
    assert fake_state["_meta"]["listing_syncs"] == [True]
    assert result["ok"] is True
    assert result["set_home"] is True
    assert result["home_scenario"] == "prompt_engineer_scenario"
    assert result["scenario_switch_mode"] == "pointer_only"
    assert isinstance(result["timings_ms"], dict)
    assert "validate_scenario" in result["timings_ms"]
    assert "write_switch_pointer" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]
    assert "wait_rebuild" in result["timings_ms"]
    assert isinstance(result["rebuild_timings_ms"], dict)
    assert "projection_refresh" in result["rebuild_timings_ms"]
    assert "semantic_rebuild" in result["rebuild_timings_ms"]
    assert isinstance(result["semantic_rebuild_timings_ms"], dict)
    assert result["semantic_rebuild_timings_ms"]["resolve"] == 2.0
    assert result["apply_summary"]["changed_branches"] == 3
    assert isinstance(result["phase_timings_ms"], dict)
    assert "time_to_pointer_update" in result["phase_timings_ms"]
    assert "time_to_first_structure" in result["phase_timings_ms"]
    assert "time_to_interactive_focus" in result["phase_timings_ms"]
    assert "time_to_full_hydration" in result["phase_timings_ms"]
    assert result["phase_timings_ms"]["time_to_first_structure"] < result["phase_timings_ms"]["time_to_full_hydration"]
    assert result["phase_timings_ms"]["time_to_interactive_focus"] < result["phase_timings_ms"]["time_to_full_hydration"]


def test_switch_webspace_scenario_auto_persists_home_for_dev_webspace(monkeypatch) -> None:
    webspace_id = "phase2-dev-auto-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["_meta"]["listing_syncs"] == [True]
    assert result["set_home"] is True
    assert result["home_scenario"] == "prompt_engineer_scenario"


def test_switch_webspace_scenario_keeps_home_unchanged_for_regular_workspace(monkeypatch) -> None:
    webspace_id = "phase2-workspace-no-auto-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Workspace",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "web_desktop"
    assert result["set_home"] is False
    assert result["home_scenario"] == "web_desktop"


def test_switch_webspace_scenario_default_pointer_only_can_schedule_background_rebuild(monkeypatch) -> None:
    webspace_id = "phase2-scenario-fast"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Fast",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(
        monkeypatch,
        state={
            "ui": _FakeMap(
                {
                    "current_scenario": "web_desktop",
                    "application": {"desktop": {"pageSchema": {"id": "old-page"}}},
                    "scenarios": {
                        "web_desktop": {"application": {"desktop": {"pageSchema": {"id": "old-cache"}}}}
                    },
                }
            ),
            "registry": _FakeMap(
                {
                    "merged": {"modals": ["old-modal"]},
                    "scenarios": {"web_desktop": {"modals": ["old-cache-modal"]}},
                }
            ),
            "data": _FakeMap(
                {
                    "catalog": {"apps": [{"id": "old-app"}]},
                    "status": {"scenario": "web_desktop"},
                    "scenarios": {"web_desktop": {"catalog": {"apps": [{"id": "old-cache-app"}]}}},
                }
            ),
        },
    )
    scheduled: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, *, scenario_id, scenario_resolution, switch_mode=None, switch_timings_ms=None: scheduled.append(
            (webspace_id, scenario_id, scenario_resolution, switch_mode, isinstance(switch_timings_ms, dict))
        ),
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["ok"] is True
    assert result["background_rebuild"] is True
    assert result["scenario_switch_mode"] == "pointer_only"
    assert scheduled == [(webspace_id, "prompt_engineer_scenario", "explicit", "pointer_only", True)]
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["ui"]["application"]["desktop"]["pageSchema"]["id"] == "old-page"
    assert fake_state["registry"]["merged"]["modals"] == ["old-modal"]
    assert fake_state["data"]["catalog"]["apps"] == [{"id": "old-app"}]
    assert fake_state["data"]["status"] == {"scenario": "web_desktop"}
    assert "prompt_engineer_scenario" not in fake_state["ui"]["scenarios"]
    assert "prompt_engineer_scenario" not in fake_state["registry"]["scenarios"]
    assert "prompt_engineer_scenario" not in fake_state["data"]["scenarios"]
    assert isinstance(result["timings_ms"], dict)
    assert "validate_scenario" in result["timings_ms"]
    assert "write_switch_pointer" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]
    assert "materialize_switch_payload" not in result["timings_ms"]
    assert "schedule_background_rebuild" in result["timings_ms"]
    assert isinstance(result["phase_timings_ms"], dict)
    assert "time_to_accept" in result["phase_timings_ms"]
    assert "time_to_pointer_update" in result["phase_timings_ms"]
    assert "time_to_full_hydration" not in result["phase_timings_ms"]


def test_switch_webspace_scenario_compat_env_is_ignored_and_keeps_pointer_only_contract(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_SWITCH_COMPAT_CACHE_WRITES", "1")

    webspace_id = "phase2-scenario-compat-rollback"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Compat Rollback",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(
        monkeypatch,
        state={
            "ui": _FakeMap(
                {
                    "current_scenario": "web_desktop",
                    "application": {"desktop": {"pageSchema": {"id": "old-page"}}},
                    "scenarios": {
                        "web_desktop": {"application": {"desktop": {"pageSchema": {"id": "old-cache"}}}}
                    },
                }
            ),
            "registry": _FakeMap(
                {
                    "merged": {"modals": ["old-modal"]},
                    "scenarios": {"web_desktop": {"modals": ["old-cache-modal"]}},
                }
            ),
            "data": _FakeMap(
                {
                    "catalog": {"apps": [{"id": "old-app"}]},
                    "status": {"scenario": "web_desktop"},
                    "scenarios": {"web_desktop": {"catalog": {"apps": [{"id": "old-cache-app"}]}}},
                }
            ),
        },
    )
    scheduled: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, *, scenario_id, scenario_resolution, switch_mode=None, switch_timings_ms=None: scheduled.append(
            (webspace_id, scenario_id, scenario_resolution, switch_mode, isinstance(switch_timings_ms, dict))
        ),
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["ok"] is True
    assert result["background_rebuild"] is True
    assert result["scenario_switch_mode"] == "pointer_only"
    assert scheduled == [(webspace_id, "prompt_engineer_scenario", "explicit", "pointer_only", True)]
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["ui"]["application"]["desktop"]["pageSchema"]["id"] == "old-page"
    assert fake_state["registry"]["merged"]["modals"] == ["old-modal"]
    assert fake_state["data"]["catalog"]["apps"] == [{"id": "old-app"}]
    assert fake_state["data"]["status"] == {"scenario": "web_desktop"}
    assert "prompt_engineer_scenario" not in fake_state["ui"]["scenarios"]
    assert "prompt_engineer_scenario" not in fake_state["registry"]["scenarios"]
    assert "prompt_engineer_scenario" not in fake_state["data"]["scenarios"]
    assert isinstance(result["timings_ms"], dict)
    assert "validate_scenario" in result["timings_ms"]
    assert "write_switch_pointer" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]
    assert "materialize_switch_payload" not in result["timings_ms"]
    assert "schedule_background_rebuild" in result["timings_ms"]
    assert isinstance(result["phase_timings_ms"], dict)
    assert "time_to_accept" in result["phase_timings_ms"]
    assert "time_to_full_hydration" not in result["phase_timings_ms"]

def test_switch_webspace_scenario_pointer_first_updates_pointer_without_eager_materialization(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH", "1")

    webspace_id = "phase-pointer-switch"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Pointer Switch",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(
        monkeypatch,
        state={
            "ui": _FakeMap(
                {
                    "current_scenario": "web_desktop",
                    "application": {"desktop": {"pageSchema": {"id": "old-page"}}},
                    "scenarios": {
                        "web_desktop": {"application": {"desktop": {"pageSchema": {"id": "old-cache"}}}}
                    },
                }
            ),
            "registry": _FakeMap(
                {
                    "merged": {"modals": ["old-modal"]},
                    "scenarios": {"web_desktop": {"modals": ["old-cache-modal"]}},
                }
            ),
            "data": _FakeMap(
                {
                    "catalog": {"apps": [{"id": "old-app"}]},
                    "scenarios": {"web_desktop": {"catalog": {"apps": [{"id": "old-cache-app"}]}}},
                }
            ),
        },
    )
    scheduled: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: True)

    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, *, scenario_id, scenario_resolution, switch_mode=None, switch_timings_ms=None: scheduled.append(
            (webspace_id, scenario_id, scenario_resolution, switch_mode, isinstance(switch_timings_ms, dict))
        ),
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["ok"] is True
    assert result["background_rebuild"] is True
    assert result["scenario_switch_mode"] == "pointer_first"
    assert scheduled == [(webspace_id, "prompt_engineer_scenario", "explicit", "pointer_first", True)]
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["ui"]["application"]["desktop"]["pageSchema"]["id"] == "old-page"
    assert "prompt_engineer_scenario" not in fake_state["ui"]["scenarios"]
    assert fake_state["registry"]["merged"]["modals"] == ["old-modal"]
    assert "prompt_engineer_scenario" not in fake_state["registry"]["scenarios"]
    assert fake_state["data"]["catalog"]["apps"] == [{"id": "old-app"}]
    assert "prompt_engineer_scenario" not in fake_state["data"]["scenarios"]
    assert isinstance(result["timings_ms"], dict)
    assert "validate_scenario" in result["timings_ms"]
    assert "write_switch_pointer" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]
    assert "materialize_switch_payload" not in result["timings_ms"]
    assert isinstance(result["phase_timings_ms"], dict)
    assert "time_to_pointer_update" in result["phase_timings_ms"]


def test_switch_webspace_scenario_pointer_first_avoids_eager_scenario_content_load(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH", "1")

    webspace_id = "phase-pointer-no-content-load"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Pointer Switch",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "web_desktop"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: True)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: (_ for _ in ()).throw(AssertionError("should not load scenario content")),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, **kwargs: None,
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["accepted"] is True
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert "validate_scenario" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]


def test_switch_webspace_scenario_pointer_first_preserves_dev_auto_home_policy(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH", "1")

    webspace_id = "phase-pointer-dev-auto-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Pointer Home",
        kind="dev",
        source_mode="dev",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "web_desktop"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    sync_listing_calls: list[bool] = []
    scheduled: list[tuple[str, str]] = []

    async def _fake_sync_listing() -> None:
        sync_listing_calls.append(True)

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: True)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, *, scenario_id, **kwargs: scheduled.append((webspace_id, scenario_id)),
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert sync_listing_calls == [True]
    assert scheduled == [(webspace_id, "prompt_engineer_scenario")]
    assert result["set_home"] is True
    assert result["home_scenario"] == "prompt_engineer_scenario"
    assert result["scenario_switch_mode"] == "pointer_first"


def test_switch_webspace_scenario_compat_env_is_ignored_and_does_not_load_content(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_SWITCH_COMPAT_CACHE_WRITES", "1")

    webspace_id = "phase2-materialize-validate-order"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Materialize Validate",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "web_desktop"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    calls: list[str] = []

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(
        webspace_runtime_module,
        "_scenario_exists_for_switch",
        lambda scenario_id, *, space: (calls.append(f"validate:{space}:{scenario_id}") or True),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: (_ for _ in ()).throw(AssertionError("should not load scenario content")),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "_schedule_scenario_switch_rebuild",
        lambda webspace_id, **kwargs: None,
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["accepted"] is True
    assert "validate:workspace:prompt_engineer_scenario" in calls
    assert result["scenario_switch_mode"] == "pointer_only"
    assert "validate_scenario" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]


def test_switch_webspace_scenario_compat_env_ignored_missing_scenario_fails_without_loading_content(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_SWITCH_COMPAT_CACHE_WRITES", "1")

    webspace_id = "phase2-materialize-missing"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Materialize Missing",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "web_desktop"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: False)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: (_ for _ in ()).throw(AssertionError("should not load missing scenario content")),
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "missing_scenario",
            wait_for_rebuild=False,
        )
    )

    assert result["accepted"] is False
    assert result["error"] == "scenario_not_found"
    assert result["scenario_switch_mode"] == "pointer_only"
    assert "validate_scenario" in result["timings_ms"]
    assert "load_scenario" not in result["timings_ms"]


def test_switch_webspace_scenario_same_current_ready_skips_rebuild_and_only_persists_home(monkeypatch) -> None:
    webspace_id = "phase2-same-current-noop"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Same Current",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    sync_listing_calls: list[bool] = []

    async def _fake_sync_listing() -> None:
        sync_listing_calls.append(True)

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: (_ for _ in ()).throw(AssertionError("should not reload scenario content")),
    )
    monkeypatch.setattr(
        webspace_runtime_module.WebspaceScenarioRuntime,
        "rebuild_webspace_async",
        lambda self, webspace_id: (_ for _ in ()).throw(AssertionError("should not rebuild current scenario")),
    )
    webspace_runtime_module._set_webspace_rebuild_status(
        webspace_id,
        status="ready",
        pending=False,
        scenario_id="prompt_engineer_scenario",
        resolver={"source": "loader:workspace", "legacy_fallback": False, "cache_hit": True},
        apply_summary={
            "branch_count": 6,
            "changed_branches": 0,
            "unchanged_branches": 6,
            "failed_branches": 0,
            "changed_paths": [],
            "defaults_failed": False,
        },
        timings_ms={"projection_refresh": 1.5, "semantic_rebuild": 2.5, "total": 4.0},
        semantic_rebuild_timings_ms={"collect_inputs": 0.5, "resolve": 1.0, "apply": 1.5, "total": 3.0},
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            set_home=True,
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert sync_listing_calls == [True]
    assert result["accepted"] is True
    assert result["switch_skipped"] is True
    assert result["skip_reason"] == "already_current_ready"
    assert result["background_rebuild"] is False
    assert result["apply_summary"]["unchanged_branches"] == 6
    assert result["rebuild_timings_ms"]["total"] == 4.0
    assert "load_scenario" not in result["timings_ms"]
    assert "wait_rebuild" not in result["timings_ms"]


def test_switch_webspace_scenario_same_current_pending_rebuild_is_deduplicated(monkeypatch) -> None:
    webspace_id = "phase2-same-current-pending"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Same Current Pending",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    sync_listing_calls: list[bool] = []

    async def _fake_sync_listing() -> None:
        sync_listing_calls.append(True)

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: (_ for _ in ()).throw(AssertionError("should not reload scenario content")),
    )
    monkeypatch.setattr(
        webspace_runtime_module.WebspaceScenarioRuntime,
        "rebuild_webspace_async",
        lambda self, webspace_id: (_ for _ in ()).throw(AssertionError("should not rebuild while pending")),
    )
    webspace_runtime_module._set_webspace_rebuild_status(
        webspace_id,
        status="running",
        pending=True,
        background=True,
        scenario_id="prompt_engineer_scenario",
        action="scenario_switch_rebuild",
        resolver={"source": "loader:workspace", "legacy_fallback": False, "cache_hit": False},
        apply_summary={
            "branch_count": 6,
            "changed_branches": 1,
            "unchanged_branches": 5,
            "failed_branches": 0,
            "changed_paths": ["ui.application"],
            "defaults_failed": False,
        },
        phase_timings_ms={"time_to_accept": 3.0, "time_to_full_hydration": 12.0},
    )

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            set_home=True,
            wait_for_rebuild=False,
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert sync_listing_calls == [True]
    assert result["accepted"] is True
    assert result["switch_skipped"] is True
    assert result["skip_reason"] == "already_pending_rebuild"
    assert result["background_rebuild"] is True
    assert result["apply_summary"]["changed_branches"] == 1
    assert result["phase_timings_ms"]["time_to_full_hydration"] == 12.0
    assert "load_scenario" not in result["timings_ms"]
    assert "wait_rebuild" not in result["timings_ms"]


def test_background_scenario_switch_rebuild_superseded_request_keeps_newer_status(monkeypatch) -> None:
    webspace_id = "phase2-background-supersede"
    events: dict[str, asyncio.Event] = {}

    async def _fake_complete(
        webspace_id: str,
        *,
        scenario_id: str,
        scenario_resolution: str | None,
        request_id: str | None = None,
        switch_mode: str | None = None,
        switch_timings_ms=None,
    ) -> dict[str, object]:
        gate = events.setdefault(scenario_id, asyncio.Event())
        await gate.wait()
        webspace_runtime_module._set_webspace_rebuild_status_if_current(
            webspace_id,
            request_id,
            status="ready",
            pending=False,
            background=True,
            scenario_id=scenario_id,
            switch_mode=switch_mode,
            finished_at=time.time(),
            phase_timings_ms={"time_to_full_hydration": 5.0},
        )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "request_id": request_id,
            "switch_mode": switch_mode,
            "timings_ms": {"projection_refresh": 1.0, "semantic_rebuild": 2.0, "total": 3.0},
            "switch_timings_ms": switch_timings_ms,
            "semantic_rebuild_timings_ms": {"collect_inputs": 0.5, "resolve": 1.0, "apply": 1.5, "total": 3.5},
            "phase_timings_ms": {"time_to_full_hydration": 5.0},
        }

    monkeypatch.setattr(webspace_runtime_module, "_complete_scenario_switch_rebuild", _fake_complete)
    webspace_runtime_module._SCENARIO_SWITCH_REBUILD_TASKS.clear()
    webspace_runtime_module._WEBSPACE_REBUILD_STATUS.clear()

    async def _run() -> dict[str, object]:
        webspace_runtime_module._schedule_scenario_switch_rebuild(
            webspace_id,
            scenario_id="scenario_a",
            scenario_resolution="explicit",
            switch_mode="pointer_first",
            switch_timings_ms={"total": 1.0},
        )
        await asyncio.sleep(0)
        first = webspace_runtime_module.describe_webspace_rebuild_state(webspace_id)
        assert first["scenario_id"] == "scenario_a"
        first_request_id = first["request_id"]

        webspace_runtime_module._schedule_scenario_switch_rebuild(
            webspace_id,
            scenario_id="scenario_b",
            scenario_resolution="explicit",
            switch_mode="pointer_first",
            switch_timings_ms={"total": 2.0},
        )
        await asyncio.sleep(0)
        second = webspace_runtime_module.describe_webspace_rebuild_state(webspace_id)
        assert second["scenario_id"] == "scenario_b"
        assert second["request_id"] != first_request_id

        events["scenario_b"].set()
        task = webspace_runtime_module._SCENARIO_SWITCH_REBUILD_TASKS[webspace_id]
        await task
        return webspace_runtime_module.describe_webspace_rebuild_state(webspace_id)

    final = asyncio.run(_run())

    assert final["status"] == "ready"
    assert final["scenario_id"] == "scenario_b"
    assert final["switch_mode"] == "pointer_first"
    assert isinstance(final["phase_timings_ms"], dict)
    assert "time_to_full_hydration" in final["phase_timings_ms"]


def test_scenario_switch_rebuild_can_defer_listing_sync(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_WEBSPACE_SCENARIO_SWITCH_INLINE_LISTING_SYNC", "0")
    monkeypatch.setenv("ADAOS_WEBSPACE_REBUILD_REFRESH_LIVE_ROOM", "0")
    sync_calls: list[bool] = []

    async def _fake_sync_listing() -> None:
        sync_calls.append(True)

    async def _fake_refresh(
        ctx,  # noqa: ARG001
        webspace_id: str,  # noqa: ARG001
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
        }

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG001
        self._last_rebuild_timings_ms = {"collect_inputs": 1.0, "resolve": 1.0, "apply": 1.0, "total": 3.0}
        self._last_apply_summary = {"changed_branches": 1, "unchanged_branches": 0}
        self._last_rebuild_ydoc_timings_ms = {"total": 3.0}
        return SimpleNamespace(scenario_id="prompt_engineer_scenario", apps=[], widgets=[])

    async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):  # noqa: ARG002
        return None

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(reset_ystore_for_webspace=lambda _webspace_id: None),
    )

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            "phase2-deferred-listing",
            action="scenario_switch_rebuild",
            scenario_id="prompt_engineer_scenario",
            scenario_resolution="explicit",
            source_of_truth="scenario_switch",
            reseed_from_scenario=False,
        )
    )

    assert result["accepted"] is True
    assert sync_calls == []
    assert "fresh_doc_sync_listing" not in result["timings_ms"]
    assert result["timings_ms"]["fresh_doc_sync_listing_deferred"] == 0.0


def test_deferred_webspace_listing_sync_coalesces(monkeypatch) -> None:
    sync_calls: list[str] = []

    async def _fake_sync_listing() -> None:
        sync_calls.append("sync")
        await asyncio.sleep(0)

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    webspace_runtime_module._WEBSPACE_LISTING_SYNC_TASK = None

    async def _run() -> tuple[dict[str, object], dict[str, object]]:
        first = webspace_runtime_module._schedule_webspace_listing_sync(reason="test")
        second = webspace_runtime_module._schedule_webspace_listing_sync(reason="test")
        task = webspace_runtime_module._WEBSPACE_LISTING_SYNC_TASK
        assert task is not None
        await task
        return first, second

    try:
        first, second = asyncio.run(_run())
    finally:
        webspace_runtime_module._WEBSPACE_LISTING_SYNC_TASK = None

    assert sync_calls == ["sync"]
    assert first["coalesced"] is False
    assert second["coalesced"] is True


def test_phase3_stale_rebuild_request_does_not_apply_effective_branches() -> None:
    webspace_id = "phase3-stale-apply-guard"
    webspace_runtime_module._WEBSPACE_REBUILD_STATUS.clear()
    webspace_runtime_module._set_webspace_rebuild_status(
        webspace_id,
        status="running",
        pending=True,
        background=True,
        request_id="req-new",
        action="scenario_switch_rebuild",
        scenario_id="web_desktop",
    )

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace())
    fake_state = {
        "ui": _CountingMap(),
        "registry": _CountingMap(),
        "data": _CountingMap(),
    }
    fake_doc = _FakeDoc(fake_state)
    resolved = webspace_runtime_module.WebspaceResolverOutputs(
        webspace_id=webspace_id,
        scenario_id="prompt_engineer_scenario",
        source_mode="workspace",
        application={"desktop": {"pageSchema": {"id": "prompt"}}},
        catalog={"apps": [{"id": "prompt"}], "widgets": []},
        registry={"modals": [], "widgets": []},
        installed={"apps": ["prompt"], "widgets": []},
        desktop={"installed": {"apps": ["prompt"], "widgets": []}, "pageSchema": {"id": "prompt"}},
        routing={"routes": {}},
        skill_decls=[],
    )
    inputs = webspace_runtime_module.WebspaceResolverInputs(
        webspace_id=webspace_id,
        scenario_id="prompt_engineer_scenario",
        source_mode="workspace",
        compatibility_cache_presence={
            "scenario_ui_application": False,
            "scenario_registry_entry": False,
            "scenario_catalog": False,
        },
    )

    with pytest.raises(webspace_runtime_module._StaleRebuildRequestError):
        runtime._apply_resolved_state_in_doc(
            fake_doc,
            webspace_id,
            resolved,
            inputs=inputs,
            expected_request_id="req-old",
        )

    assert fake_state["ui"] == {}
    assert fake_state["data"] == {}
    assert fake_state["registry"] == {}
    assert fake_state["ui"].set_count == 0
    assert fake_state["data"].set_count == 0
    assert fake_state["registry"].set_count == 0


def test_rebuild_webspace_async_prefers_live_room_ydoc_session(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_state = {
        "ui": _FakeMap(),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    class _CapturedAsyncDoc:
        async def __aenter__(self) -> _FakeDoc:
            return _FakeDoc(fake_state)

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _fake_async_get_ydoc(
        webspace_id: str,
        *,
        read_only: bool = False,
        prefer_live_room: bool = False,
        timings=None,
        timing_prefix: str = "",
    ):
        captured["webspace_id"] = webspace_id
        captured["read_only"] = read_only
        captured["prefer_live_room"] = prefer_live_room
        captured["timings_is_dict"] = isinstance(timings, dict)
        captured["timing_prefix"] = timing_prefix
        return _CapturedAsyncDoc()

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", _fake_async_get_ydoc)
    monkeypatch.setattr(
        webspace_runtime_module.WebspaceScenarioRuntime,
        "_rebuild_in_doc",
        lambda self, ydoc, webspace_id, expected_request_id=None: {
            "webspace_id": webspace_id,
            "expected_request_id": expected_request_id,
            "doc": ydoc,
        },
    )

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(ctx=SimpleNamespace())
    result = asyncio.run(runtime.rebuild_webspace_async("default", request_id="req-live-room"))

    assert result["webspace_id"] == "default"
    assert result["expected_request_id"] == "req-live-room"
    assert captured == {
        "webspace_id": "default",
        "read_only": False,
        "prefer_live_room": True,
        "timings_is_dict": True,
        "timing_prefix": "",
    }


def test_go_home_webspace_uses_manifest_home_scenario(monkeypatch) -> None:
    webspace_id = "phase2-go-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool = False,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, *, space: True)

    result = asyncio.run(webspace_runtime_module.go_home_webspace(webspace_id))

    assert captured == [(webspace_id, "prompt_engineer_scenario", False)]
    assert result["scenario_id"] == "prompt_engineer_scenario"
    assert result["action"] == "go_home"
    assert result["source_of_truth"] == "manifest_home_scenario"
    assert result["scenario_resolution"] == "manifest_home"


def test_go_home_webspace_preflight_falls_back_to_web_desktop_when_home_missing(monkeypatch) -> None:
    webspace_id = "phase2-go-home-fallback"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Go Home Fallback",
        kind="workspace",
        source_mode="workspace",
        home_scenario="infrascope",
    )

    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool = False,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    def _scenario_exists(scenario_id: str, *, space: str) -> bool:  # noqa: ARG001
        return scenario_id == "web_desktop"

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", _scenario_exists)

    result = asyncio.run(webspace_runtime_module.go_home_webspace(webspace_id))

    assert captured == [(webspace_id, "web_desktop", False)]
    assert result["scenario_id"] == "web_desktop"
    assert result["scenario_resolution"] == "manifest_home_fallback"
    assert result["validation"]["requested_scenario_id"] == "infrascope"
    assert result["validation"]["resolved_scenario_id"] == "web_desktop"
    assert result["validation"]["fallback_applied"] is True


def test_phase3_resolve_rebuild_target_prefers_current_before_manifest_home(monkeypatch) -> None:
    webspace_id = "phase3-resolve-current-first"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Resolve Current First",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    state, scenario_id, scenario_resolution = asyncio.run(
        webspace_runtime_module._resolve_rebuild_scenario_target(webspace_id, None)
    )

    assert state.webspace_id == webspace_id
    assert scenario_id == "prompt_engineer_scenario"
    assert scenario_resolution == "current_scenario"


def test_phase3_reload_target_preserves_manifest_home_before_current(monkeypatch) -> None:
    webspace_id = "phase3-resolve-home-first"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Resolve Home First",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    state, scenario_id, scenario_resolution = asyncio.run(
        webspace_runtime_module._resolve_reload_scenario_target(webspace_id, None)
    )

    assert state.webspace_id == webspace_id
    assert scenario_id == "web_desktop"
    assert scenario_resolution == "manifest_home"


def test_sync_webspace_listing_skips_unchanged_payload(monkeypatch) -> None:
    webspace_id = "phase2-listing-noop"
    ensure_workspace(webspace_id)

    listing = [{"id": webspace_id, "title": "Phase 2 Listing"}]
    data_map = _CountingMap({"webspaces": {"items": listing}})
    fake_state = {
        "ui": _FakeMap(),
        "registry": _FakeMap(),
        "data": data_map,
    }

    monkeypatch.setattr(
        webspace_runtime_module.workspace_index,
        "list_workspaces",
        lambda: [SimpleNamespace(workspace_id=webspace_id)],
    )
    monkeypatch.setattr(webspace_runtime_module, "_webspace_listing", lambda: listing)
    monkeypatch.setattr(
        webspace_runtime_module,
        "async_get_ydoc",
        lambda _webspace_id: _FakeAsyncDoc(fake_state),
    )

    asyncio.run(webspace_runtime_module._sync_webspace_listing())

    assert data_map.set_count == 0


def test_webspace_listing_includes_local_node_metadata(monkeypatch) -> None:
    webspace_id = "phase2-listing-node-meta"
    ensure_workspace(webspace_id)

    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "member-01")
    monkeypatch.setattr(webspace_runtime_module, "_local_node_label", lambda: "Edge Member")
    monkeypatch.setattr(
        webspace_runtime_module,
        "_local_node_display",
        lambda: {
            "node_label": "Edge Member",
            "node_compact_label": "N1",
            "node_index": 1,
            "node_color": "#F28E2B",
        },
    )
    monkeypatch.setattr(webspace_runtime_module, "_try_read_live_current_scenario", lambda _webspace_id: "web_desktop")

    listing = webspace_runtime_module._webspace_listing()
    item = next(entry for entry in listing if entry["id"] == webspace_id)

    assert item["node_id"] == "member-01"
    assert item["node_label"] == "Edge Member"


def test_webspace_service_set_home_scenario_updates_manifest(monkeypatch) -> None:
    webspace_id = "phase2-set-home-service"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Service Home",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    info = asyncio.run(webspace_runtime_module.WebspaceService().set_home_scenario(webspace_id, "prompt_engineer_scenario"))

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert info is not None
    assert info.home_scenario == "prompt_engineer_scenario"


def test_webspace_service_update_metadata_updates_title_and_home(monkeypatch) -> None:
    webspace_id = "phase2-update-metadata"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Workspace Before",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    info = asyncio.run(
        webspace_runtime_module.WebspaceService().update_metadata(
            webspace_id,
            title="Workspace After",
            home_scenario="prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.title == "Workspace After"
    assert row.home_scenario == "prompt_engineer_scenario"
    assert info is not None
    assert info.title == "Workspace After"
    assert info.home_scenario == "prompt_engineer_scenario"


def test_ensure_dev_webspace_for_scenario_reuses_existing_dev_space() -> None:
    webspace_id = "phase2-dev-existing"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt IDE",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    result = asyncio.run(webspace_runtime_module.ensure_dev_webspace_for_scenario("prompt_engineer_scenario"))

    assert result["ok"] is True
    assert result["created"] is False
    assert result["webspace_id"] == webspace_id
    assert result["home_scenario"] == "prompt_engineer_scenario"


def test_ensure_dev_webspace_for_scenario_creates_missing_dev_space(monkeypatch) -> None:
    async def _fake_seed(webspace_id: str, scenario_id: str, *, dev=None) -> None:  # noqa: ARG001
        return None

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    result = asyncio.run(webspace_runtime_module.ensure_dev_webspace_for_scenario("phase2_fresh_scenario"))

    row = get_workspace(str(result["webspace_id"]))
    assert row is not None
    assert row.is_dev is True
    assert row.home_scenario == "phase2_fresh_scenario"
    assert result["created"] is True
    assert result["kind"] == "dev"
    assert result["source_mode"] == "dev"


def test_desktop_scenario_set_forwards_set_home_flag(monkeypatch) -> None:
    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool = False,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    asyncio.run(
        webspace_runtime_module._on_desktop_scenario_set(
            {"webspace_id": "phase2-forward", "scenario_id": "prompt_engineer_scenario", "set_home": True}
        )
    )

    assert captured == [("phase2-forward", "prompt_engineer_scenario", True, False)]


def test_desktop_scenario_set_preserves_explicit_false(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool | None = None,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    asyncio.run(
        webspace_runtime_module._on_desktop_scenario_set(
            {"webspace_id": "phase2-forward", "scenario_id": "prompt_engineer_scenario", "set_home": False}
        )
    )

    assert captured == [("phase2-forward", "prompt_engineer_scenario", False, False)]


def test_desktop_scenario_set_forwards_explicit_wait_for_rebuild(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None, bool]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool | None = None,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    asyncio.run(
        webspace_runtime_module._on_desktop_scenario_set(
            {
                "webspace_id": "phase2-forward",
                "scenario_id": "prompt_engineer_scenario",
                "wait_for_rebuild": True,
            }
        )
    )

    assert captured == [("phase2-forward", "prompt_engineer_scenario", None, True)]


def test_webspace_go_home_event_forwards_explicit_wait_for_rebuild(monkeypatch) -> None:
    captured: list[tuple[str, bool]] = []

    async def _fake_go_home(
        webspace_id: str,
        *,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, wait_for_rebuild))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "go_home_webspace", _fake_go_home)

    asyncio.run(
        webspace_runtime_module._on_webspace_go_home(
            {
                "webspace_id": "phase2-home",
                "wait_for_rebuild": True,
            }
        )
    )

    assert captured == [("phase2-home", True)]


def test_reload_preview_webspaces_for_scenario_project(monkeypatch) -> None:
    scenario_id = "prompt_engineer_scenario"
    preview_a = "dev-prompt-a"
    preview_b = "dev-prompt-b"
    ensure_workspace(preview_a)
    ensure_workspace(preview_b)
    set_workspace_manifest(
        preview_a,
        display_name="DEV: Prompt A",
        kind="dev",
        source_mode="dev",
        home_scenario=scenario_id,
    )
    set_workspace_manifest(
        preview_b,
        display_name="DEV: Prompt B",
        kind="dev",
        source_mode="dev",
        home_scenario="other_scenario",
    )

    captured: list[tuple[str, str, str]] = []

    async def _fake_reload(webspace_id: str, *, scenario_id: str | None = None, action: str = "reload") -> dict[str, object]:
        captured.append((webspace_id, str(scenario_id), action))
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "action": action}

    monkeypatch.setattr(webspace_runtime_module, "reload_webspace_from_scenario", _fake_reload)

    result = asyncio.run(
        webspace_runtime_module.reload_preview_webspaces_for_project(
            "scenario",
            scenario_id,
            reason="project_meta_updated",
        )
    )

    assert captured == [(preview_a, scenario_id, "reload")]
    assert result["accepted"] is True
    assert result["reloaded_webspaces"] == [preview_a]


def test_reload_preview_webspaces_for_skill_dependency(monkeypatch) -> None:
    preview = "dev-scenario-preview"
    ensure_workspace(preview)
    set_workspace_manifest(
        preview,
        display_name="DEV: Scenario Preview",
        kind="dev",
        source_mode="dev",
        home_scenario="demo_scenario",
    )

    async def _fake_reload(webspace_id: str, *, scenario_id: str | None = None, action: str = "reload") -> dict[str, object]:
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "action": action}

    monkeypatch.setattr(webspace_runtime_module, "reload_webspace_from_scenario", _fake_reload)
    monkeypatch.setattr(
        webspace_runtime_module.scenarios_loader,
        "read_manifest",
        lambda scenario_id, *, space="workspace": {"depends": ["weather_skill", "skill_alpha"]},
    )

    result = asyncio.run(
        webspace_runtime_module.reload_preview_webspaces_for_project(
            "skill",
            "skill_alpha",
            reason="git_updated",
        )
    )

    assert result["accepted"] is True
    assert result["reloaded_webspaces"] == [preview]


def test_scenarios_synced_routes_through_semantic_rebuild_helper(monkeypatch) -> None:
    captured: list[tuple[str, str | None, str, str]] = []

    async def _fake_rebuild(
        webspace_id: str,
        *,
        action: str = "rebuild",
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
        source_of_truth: str = "current_runtime",
        reseed_from_scenario: bool = False,
        event_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert reseed_from_scenario is False
        assert event_payload is None
        captured.append((webspace_id, scenario_id, action, source_of_truth))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "rebuild_webspace_from_sources", _fake_rebuild)

    asyncio.run(
        webspace_runtime_module._on_scenarios_synced(
            {"webspace_id": "phase3-bootstrap", "scenario_id": "web_desktop"}
        )
    )

    assert captured == [("phase3-bootstrap", "web_desktop", "scenario_projection_sync", "scenario_projection")]


def test_phase4_collect_resolver_inputs_does_not_refresh_projection_registry(monkeypatch) -> None:
    projection_calls: list[str] = []

    class _Projections:
        def load_from_scenario(self, scenario_id: str) -> int:
            projection_calls.append(scenario_id)
            return 1

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace(projections=_Projections()))
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap({"current_scenario": "web_desktop", "scenarios": {"web_desktop": {"application": {}}}}),
            "data": _FakeMap({"scenarios": {"web_desktop": {"catalog": {}}}}),
            "registry": _FakeMap({"scenarios": {"web_desktop": {}}}),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, "phase4-collect")

    assert inputs.scenario_id == "web_desktop"
    assert projection_calls == []


def test_phase_pointer_collect_resolver_inputs_prefers_loader_payload_over_legacy_yjs(monkeypatch) -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])
    monkeypatch.setattr(
        webspace_runtime_module.scenarios_loader,
        "read_content",
        lambda scenario_id, space="workspace": {
            "id": scenario_id,
            "ui": {"application": {"desktop": {"pageSchema": {"id": f"loader-page:{space}:{scenario_id}"}}}},
            "catalog": {"apps": [{"id": f"loader-app:{space}:{scenario_id}"}]},
            "registry": {"modals": [f"loader-modal:{space}:{scenario_id}"]},
        },
    )

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap(
                {
                    "current_scenario": "prompt_engineer_scenario",
                    "scenarios": {
                        "prompt_engineer_scenario": {
                            "application": {"desktop": {"pageSchema": {"id": "legacy-page"}}}
                        }
                    },
                }
            ),
            "data": _FakeMap(
                {
                    "scenarios": {
                        "prompt_engineer_scenario": {
                            "catalog": {"apps": [{"id": "legacy-app"}]}
                        }
                    }
                }
            ),
            "registry": _FakeMap(
                {
                    "scenarios": {
                        "prompt_engineer_scenario": {"modals": ["legacy-modal"]}
                    }
                }
            ),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, "phase-pointer-loader")

    assert inputs.scenario_application["desktop"]["pageSchema"]["id"] == "loader-page:workspace:prompt_engineer_scenario"
    assert inputs.scenario_catalog["apps"] == [{"id": "loader-app:workspace:prompt_engineer_scenario"}]
    assert inputs.scenario_registry["modals"] == ["loader-modal:workspace:prompt_engineer_scenario"]
    assert inputs.scenario_source == "loader:workspace"
    assert inputs.legacy_scenario_fallback is False
    assert inputs.metadata["scenario_source"] == "loader:workspace"


def test_phase_pointer_collect_resolver_inputs_falls_back_to_legacy_yjs_when_loader_missing(monkeypatch) -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])
    monkeypatch.setattr(webspace_runtime_module.scenarios_loader, "read_content", lambda scenario_id, space="workspace": {})

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap(
                {
                    "current_scenario": "prompt_engineer_scenario",
                    "scenarios": {
                        "node-1": {
                            "prompt_engineer_scenario": {
                                "application": {"desktop": {"pageSchema": {"id": "legacy-page"}}}
                            }
                        }
                    },
                }
            ),
            "data": _FakeMap(
                {
                    "scenarios": {
                        "node-1": {
                            "prompt_engineer_scenario": {
                                "catalog": {"apps": [{"id": "legacy-app"}]}
                            }
                        }
                    }
                }
            ),
            "registry": _FakeMap(
                {
                    "scenarios": {
                        "node-1": {
                            "prompt_engineer_scenario": {"modals": ["legacy-modal"]}
                        }
                    }
                }
            ),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, "phase-pointer-legacy")

    assert inputs.scenario_application["desktop"]["pageSchema"]["id"] == "legacy-page"
    assert inputs.scenario_catalog["apps"] == [{"id": "legacy-app"}]
    assert inputs.scenario_registry["modals"] == ["legacy-modal"]
    assert inputs.scenario_source == "legacy_yjs"
    assert inputs.legacy_scenario_fallback is True
    assert inputs.metadata["legacy_scenario_fallback"] is True


def test_phase_pointer_collect_resolver_inputs_reads_node_scoped_legacy_yjs_when_loader_missing(monkeypatch) -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub")
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])
    monkeypatch.setattr(webspace_runtime_module.scenarios_loader, "read_content", lambda scenario_id, space="workspace": {})

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap(
                {
                    "current_scenario": "prompt_engineer_scenario",
                    "scenarios": {
                        "hub": {
                            "prompt_engineer_scenario": {
                                "application": {"desktop": {"pageSchema": {"id": "legacy-node-page"}}}
                            }
                        }
                    },
                }
            ),
            "data": _FakeMap(
                {
                    "scenarios": {
                        "hub": {
                            "prompt_engineer_scenario": {
                                "catalog": {"apps": [{"id": "legacy-node-app"}]}
                            }
                        }
                    }
                }
            ),
            "registry": _FakeMap(
                {
                    "scenarios": {
                        "hub": {
                            "prompt_engineer_scenario": {"modals": ["legacy-node-modal"]}
                        }
                    }
                }
            ),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, "phase-pointer-legacy-node-scoped")

    assert inputs.scenario_application["desktop"]["pageSchema"]["id"] == "legacy-node-page"
    assert inputs.scenario_catalog["apps"] == [{"id": "legacy-node-app"}]
    assert inputs.scenario_registry["modals"] == ["legacy-node-modal"]
    assert inputs.scenario_source == "legacy_yjs"
    assert inputs.legacy_scenario_fallback is True


def test_phase5_collect_resolver_inputs_prefers_persistent_overlay(monkeypatch) -> None:
    webspace_id = "phase5-overlay-collect"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Overlay Collect",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )
    set_workspace_installed_overlay(
        webspace_id,
        {"apps": ["overlay-app"], "widgets": ["overlay-widget"]},
    )
    set_workspace_pinned_widgets_overlay(
        webspace_id,
        [{"id": "infra-status", "type": "visual.metricTile"}],
    )
    set_workspace_topbar_overlay(
        webspace_id,
        [{"id": "home", "label": "Home"}],
    )
    set_workspace_page_schema_overlay(
        webspace_id,
        {"id": "desktop", "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]}, "widgets": []},
    )

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap({"current_scenario": "web_desktop", "scenarios": {"web_desktop": {"application": {}}}}),
            "data": _FakeMap(
                {
                    "installed": {"apps": ["ydoc-app"], "widgets": ["ydoc-widget"]},
                    "scenarios": {"web_desktop": {"catalog": {}}},
                }
            ),
            "registry": _FakeMap({"scenarios": {"web_desktop": {}}}),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, webspace_id)

    assert inputs.overlay_snapshot["installed"] == {
        "apps": ["overlay-app"],
        "widgets": ["overlay-widget"],
    }
    assert inputs.overlay_snapshot["pinnedWidgets"] == [
        {"id": "infra-status", "type": "visual.metricTile"}
    ]
    assert inputs.overlay_snapshot["source"] == "workspace_manifest_overlay"


def test_phase5_resolver_prefers_pinned_widgets_from_overlay_over_scenario_defaults() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase5-pinned-overlay",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={
                "desktop": {
                    "topbar": [],
                    "pinnedWidgets": [{"id": "scenario-pin", "type": "visual.metricTile"}],
                }
            },
            scenario_catalog={"apps": [], "widgets": [{"id": "overlay-pin", "type": "visual.metricTile"}]},
            scenario_registry={},
            overlay_snapshot={
                "installed": {"apps": [], "widgets": []},
                "pinnedWidgets": [{"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}],
            },
            live_state={"desktop": {}, "routing": {}},
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    assert resolved.application["desktop"]["pinnedWidgets"] == [
        {"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}
    ]
    assert resolved.desktop["pinnedWidgets"] == [
        {"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}
    ]


def test_phase5_resolver_prefers_scenario_page_schema_and_topbar_over_overlay() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase5-layout-overlay",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={
                "desktop": {
                    "topbar": [{"id": "scenario-home", "label": "Home"}],
                    "pageSchema": {
                        "id": "desktop",
                        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [{"id": "scenario-widget", "type": "desktop.widgets", "area": "main"}],
                    },
                }
            },
            scenario_catalog={"apps": [], "widgets": []},
            scenario_registry={},
            overlay_snapshot={"installed": {"apps": [], "widgets": []}},
            live_state={"desktop": {}, "routing": {}},
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    assert resolved.application["desktop"]["topbar"] == [{"id": "scenario-home", "label": "Home"}]
    assert resolved.application["desktop"]["pageSchema"]["id"] == "desktop"
    assert resolved.desktop["topbar"] == [{"id": "scenario-home", "label": "Home"}]
    assert resolved.desktop["pageSchema"]["widgets"][0]["id"] == "scenario-widget"


def test_phase4_semantic_rebuild_refreshes_projection_rules_before_runtime_rebuild(monkeypatch) -> None:
    order: list[str] = []

    async def _fake_refresh(
        ctx,
        webspace_id: str,
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:  # noqa: ARG001
        order.append("refresh")
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "space": "workspace",
            "rules_loaded": 1,
        }

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG002
        order.append("rebuild")
        self._last_rebuild_timings_ms = {
            "collect_inputs": 1.25,
            "resolve": 2.5,
            "apply": 3.75,
            "to_registry_entry": 0.5,
            "total": 8.0,
        }
        self._last_apply_summary = {
            "branch_count": 6,
            "changed_branches": 2,
            "unchanged_branches": 4,
            "failed_branches": 0,
            "changed_paths": ["ui.application", "registry.merged"],
            "defaults_failed": False,
        }
        return SimpleNamespace(scenario_id="web_desktop", apps=[], widgets=[])

    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: get_ctx())
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            "phase4-ordered-rebuild",
            action="rebuild",
            scenario_id="web_desktop",
            source_of_truth="scenario_projection",
        )
    )

    assert order == ["refresh", "rebuild"]
    assert result["accepted"] is True
    assert result["projection_refresh"]["rules_loaded"] == 1
    assert isinstance(result["timings_ms"], dict)
    assert "projection_refresh" in result["timings_ms"]
    assert "semantic_rebuild" in result["timings_ms"]
    assert isinstance(result["semantic_rebuild_timings_ms"], dict)
    assert result["semantic_rebuild_timings_ms"]["apply"] == 3.75
    assert result["apply_summary"]["changed_branches"] == 2


def test_phase4_projection_refresh_uses_dev_space_for_dev_webspace(monkeypatch) -> None:
    webspace_id = "phase4-dev-refresh"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    captured: list[tuple[str, str]] = []

    class _Projections:
        def load_from_scenario(self, scenario_id: str, *, space: str = "workspace") -> int:
            captured.append((scenario_id, space))
            return 2

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    result = asyncio.run(
        webspace_runtime_module._refresh_projection_rules_for_rebuild(
            SimpleNamespace(projections=_Projections()),
            webspace_id,
        )
    )

    assert captured == [("prompt_engineer_scenario", "dev")]
    assert result["space"] == "dev"
    assert result["rules_loaded"] == 2


def test_phase4_rebuild_from_sources_succeeds_without_materialized_yjs_scenario_payload(monkeypatch) -> None:
    webspace_runtime_module._RESOLVED_WEBSPACE_CACHE.clear()
    webspace_id = "phase4-loader-rebuild"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Loader Rebuild",
        kind="workspace",
        source_mode="workspace",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    async def _fake_refresh(
        ctx,
        webspace_id: str,
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:  # noqa: ARG001
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "space": "workspace",
            "rules_loaded": 0,
        }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", lambda self, mode="mixed": [])
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_list_desktop_scenarios", lambda self, space: [])
    monkeypatch.setattr(
        webspace_runtime_module.scenarios_loader,
        "read_content",
        lambda scenario_id, *, space="workspace": {
            "id": scenario_id,
            "ui": {"application": {"desktop": {"pageSchema": {"id": "loader-page"}}}},
            "registry": {"modals": ["loader-modal"], "widgets": []},
            "catalog": {"apps": [{"id": "loader-app", "title": "Loader App"}], "widgets": []},
            "data": {"routing": {"routes": {"home": "/loader"}}},
        }
        if scenario_id == "prompt_engineer_scenario" and space == "workspace"
        else {},
    )

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            webspace_id,
            action="rebuild",
            source_of_truth="scenario_projection",
        )
    )

    assert result["accepted"] is True
    assert result["resolver"]["source"] == "loader:workspace"
    assert result["resolver"]["legacy_fallback"] is False
    assert fake_state["ui"]["application"]["desktop"]["pageSchema"]["id"] == "loader-page"
    assert fake_state["data"]["catalog"]["apps"][0]["id"] == "loader-app"
    assert "scenarios" not in fake_state["ui"]
    assert "scenarios" not in fake_state["data"]
    assert "scenarios" not in fake_state["registry"]


def test_phase3_resolver_outputs_are_explicit_and_reusable() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase3-explicit-resolver",
            scenario_id="prompt_engineer_scenario",
            source_mode="dev",
            scenario_application={
                "id": "prompt-root",
                "modals": {"scenario_modal": {"title": "Scenario"}},
                "desktop": {
                    "pageSchema": {
                        "widgets": [{"id": "desktop-widgets", "type": "desktop.widgets", "area": "main"}]
                    }
                },
            },
            scenario_catalog={
                "apps": [{"id": "scenario-app", "title": "Scenario App"}],
                "widgets": [{"id": "scenario-widget", "title": "Scenario Widget"}],
            },
            scenario_registry={"modals": ["scenario_modal"], "widgets": ["scenario_widget"]},
            overlay_snapshot={"installed": {"apps": ["scenario-app"], "widgets": []}},
            live_state={"desktop": {"installed": {}}, "routing": {}},
            skill_decls=[
                {
                    "skill": "prompt_skill",
                    "space": "dev",
                    "apps": [{"id": "skill-app", "title": "Skill App"}],
                    "widgets": [{"id": "skill-widget", "title": "Skill Widget"}],
                    "registry": {
                        "modals": {"skill_modal": {"title": "Skill Modal"}},
                        "widgets": ["skill_widget"],
                    },
                    "contributions": [
                        {
                            "extensionPoint": "desktop.apps",
                            "type": "app",
                            "id": "skill-app",
                            "autoInstall": True,
                        }
                    ],
                    "ydoc_defaults": {"data/prompt": {"status": "idle"}},
                }
            ],
            desktop_scenarios=[("other_scenario", "Other Scenario")],
        )
    )

    assert resolved.scenario_id == "prompt_engineer_scenario"
    assert [item["id"] for item in resolved.catalog["apps"]] == [
        "scenario-app",
        "scenario:other_scenario",
        "skill-app",
    ]
    assert [item["node_label"] for item in resolved.catalog["apps"]] == [
        "Node 0",
        "Node 0",
        "Node 0",
    ]
    assert [item["id"] for item in resolved.catalog["widgets"]] == ["scenario-widget", "skill-widget"]
    assert [item["node_label"] for item in resolved.catalog["widgets"]] == ["Node 0", "Node 0"]
    assert resolved.registry["modals"] == [
        "scenario_modal",
        "skill_modal",
        "apps_catalog",
        "widgets_catalog",
        "scenario_switcher",
    ]
    assert resolved.registry["widgets"] == ["scenario_widget", "skill_widget"]
    assert resolved.installed["apps"] == ["scenario-app", "scenario:other_scenario", "skill-app"]
    assert resolved.application["modals"]["scenario_modal"]["title"] == "Scenario"
    assert resolved.application["modals"]["skill_modal"]["title"] == "Skill Modal"
    assert resolved.application["modals"]["apps_catalog"]["load"]["focus"] == "off_focus"
    assert resolved.application["modals"]["apps_catalog"]["schema"]["load"]["data"] == "deferred"
    assert resolved.application["modals"]["widgets_catalog"]["schema"]["widgets"][0]["load"]["offFocusReadyState"] == "hydrating"
    assert resolved.desktop["installed"]["apps"] == ["scenario-app", "scenario:other_scenario", "skill-app"]
    assert resolved.routing["routes"] == {}


def test_phase5_resolver_cache_reuses_same_inputs_without_leaking_mutations() -> None:
    webspace_runtime_module._RESOLVED_WEBSPACE_CACHE.clear()
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    inputs = webspace_runtime_module.WebspaceResolverInputs(
        webspace_id="phase5-resolver-cache",
        scenario_id="prompt_engineer_scenario",
        source_mode="workspace",
        scenario_application={"desktop": {"pageSchema": {"id": "cached-page"}}},
        scenario_catalog={"apps": [{"id": "cached-app", "title": "Cached App"}], "widgets": []},
        scenario_registry={"modals": [], "widgets": []},
        overlay_snapshot={"installed": {"apps": [], "widgets": []}},
        live_state={"desktop": {"installed": {}}, "routing": {}},
        skill_decls=[],
        desktop_scenarios=[],
        scenario_source="loader:workspace",
        legacy_scenario_fallback=False,
    )

    first = runtime.resolve_webspace(inputs)
    first_debug = dict(runtime._last_resolver_debug or {})
    first.catalog["apps"].append({"id": "mutated-app"})

    second = runtime.resolve_webspace(inputs)
    second_debug = dict(runtime._last_resolver_debug or {})

    assert first_debug["cache_hit"] is False
    assert second_debug["cache_hit"] is True
    assert second_debug["source"] == "loader:workspace"
    assert second_debug["legacy_fallback"] is False
    assert set(second_debug["cache_keys"].keys()) >= {"scenario", "skills", "overlay"}
    assert [item["id"] for item in second.catalog["apps"]] == ["cached-app"]


def test_phase5_apply_summary_reports_changed_and_unchanged_top_level_branches() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    fake_state = {
        "ui": _CountingMap(),
        "registry": _CountingMap(),
        "data": _CountingMap(),
    }
    resolved = webspace_runtime_module.WebspaceResolverOutputs(
        webspace_id="phase5-apply-summary",
        scenario_id="prompt_engineer_scenario",
        source_mode="workspace",
        application={"desktop": {"pageSchema": {"id": "apply-page"}}},
        catalog={"apps": [{"id": "apply-app", "title": "Apply App"}], "widgets": []},
        registry={"modals": ["apply-modal"], "widgets": []},
        installed={"apps": ["apply-app"], "widgets": []},
        desktop={"installed": {"apps": ["apply-app"], "widgets": []}},
        routing={"routes": {"home": "/apply"}},
        skill_decls=[],
    )
    fake_doc = _FakeDoc(fake_state)

    runtime._apply_resolved_state_in_doc(fake_doc, "phase5-apply-summary", resolved)
    first_summary = dict(runtime._last_apply_summary or {})

    runtime._apply_resolved_state_in_doc(fake_doc, "phase5-apply-summary", resolved)
    second_summary = dict(runtime._last_apply_summary or {})

    assert first_summary["changed_branches"] == 7
    assert first_summary["unchanged_branches"] == 0
    assert first_summary["changed_paths"] == [
        "ui.application",
        "registry.merged",
        "data.catalog",
        "data.installed",
        "data.desktop",
        "data.webio",
        "data.routing",
    ]
    assert first_summary["phases"]["structure"]["changed_paths"] == [
        "ui.application",
        "registry.merged",
    ]
    assert first_summary["phases"]["interactive"]["changed_paths"] == [
        "data.catalog",
        "data.installed",
        "data.desktop",
        "data.webio",
        "data.routing",
    ]
    assert second_summary["changed_branches"] == 0
    assert second_summary["unchanged_branches"] == 7
    assert second_summary["failed_branches"] == 0
    assert second_summary["transaction_total"] == 2
    assert second_summary["changed_paths"] == []
    assert second_summary["phases"]["structure"]["unchanged_branches"] == 2
    assert second_summary["phases"]["interactive"]["unchanged_branches"] == 5
    assert runtime._last_apply_phase_timings_ms is not None
    assert "apply_structure" in runtime._last_apply_phase_timings_ms
    assert "apply_interactive" in runtime._last_apply_phase_timings_ms
    assert fake_state["ui"].set_count == 1
    assert fake_state["data"].set_count == 5
    assert fake_state["registry"].set_count == 2
    assert fake_doc.transaction_count == 4
    assert "runtime_meta" in fake_state["registry"]


def test_phase5_derive_phase_timings_uses_semantic_phase_breakdown() -> None:
    phase_timings = webspace_runtime_module._derive_phase_timings(
        switch_timings_ms={
            "describe_state_before": 0.5,
            "resolve_manifest_policy": 0.5,
            "validate_scenario": 1.0,
            "write_switch_pointer": 1.5,
            "total": 4.0,
        },
        rebuild_timings_ms={
            "projection_refresh": 2.0,
            "workflow_sync": 1.0,
            "event_emit": 1.0,
            "total": 10.0,
        },
        semantic_rebuild_timings_ms={
            "collect_inputs": 1.0,
            "resolve": 1.0,
            "apply_structure": 1.0,
            "apply_interactive": 2.0,
            "apply": 3.0,
            "to_registry_entry": 0.5,
            "total": 6.0,
        },
        switch_mode="pointer_only",
    )

    assert phase_timings is not None
    assert phase_timings["time_to_pointer_update"] == 3.5
    assert phase_timings["time_to_first_structure"] == 9.0
    assert phase_timings["time_to_interactive_focus"] == 11.0
    assert phase_timings["time_to_full_hydration"] == 12.0


def test_phase5_resolver_omits_catalog_modals_without_desktop_library_capability() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase5-no-library",
            scenario_id="prompt_engineer_scenario",
            source_mode="workspace",
            scenario_application={"id": "prompt-root", "modals": {"scenario_modal": {"title": "Scenario"}}},
            scenario_catalog={"apps": [{"id": "scenario-app", "title": "Scenario App"}]},
            scenario_registry={"modals": ["scenario_modal"], "widgets": []},
            overlay_snapshot={"installed": {"apps": [], "widgets": []}},
            live_state={"desktop": {"installed": {}}, "routing": {}},
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    assert resolved.registry["modals"] == ["scenario_modal", "scenario_switcher"]
    assert "apps_catalog" not in (resolved.application.get("modals") or {})
    assert "widgets_catalog" not in (resolved.application.get("modals") or {})


def test_skill_activated_event_can_defer_webspace_rebuild(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def _fake_rebuild(webspace_id: str, *, action: str = "rebuild", source_of_truth: str = "workspace", **kwargs):  # noqa: ARG001
        calls.append((webspace_id, action, source_of_truth))
        return None

    monkeypatch.setattr(webspace_runtime_module, "rebuild_webspace_from_sources", _fake_rebuild)

    asyncio.run(
        webspace_runtime_module._on_skill_activated(
            {
                "skill_name": "weather_skill",
                "webspace_id": "default",
                "defer_webspace_rebuild": True,
            }
        )
    )

    assert calls == []


def test_phase4_rebuild_status_exposes_legacy_resolver_fallback(monkeypatch) -> None:
    webspace_runtime_module._RESOLVED_WEBSPACE_CACHE.clear()
    webspace_id = "phase4-legacy-fallback"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Legacy Fallback",
        kind="workspace",
        source_mode="workspace",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap(
            {
                "current_scenario": "prompt_engineer_scenario",
                "scenarios": {
                    "hub": {
                        "prompt_engineer_scenario": {"application": {"desktop": {"pageSchema": {"id": "legacy-page"}}}}
                    }
                },
            }
        ),
        "registry": _FakeMap(
            {
                "scenarios": {
                    "hub": {
                        "prompt_engineer_scenario": {"modals": ["legacy-modal"], "widgets": []}
                    }
                }
            }
        ),
        "data": _FakeMap(
            {
                "scenarios": {
                    "hub": {
                        "prompt_engineer_scenario": {"catalog": {"apps": [{"id": "legacy-app"}], "widgets": []}}
                    }
                }
            }
        ),
    }

    async def _fake_refresh(
        ctx,
        webspace_id: str,
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:  # noqa: ARG001
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "space": "workspace",
            "rules_loaded": 0,
        }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "hub")
    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_collect_skill_decls", lambda self, mode="mixed": [])
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "_list_desktop_scenarios", lambda self, space: [])
    monkeypatch.setattr(
        webspace_runtime_module.scenarios_loader,
        "read_content",
        lambda scenario_id, *, space="workspace": {},
    )

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            webspace_id,
            action="rebuild",
            source_of_truth="scenario_projection",
        )
    )
    status = webspace_runtime_module.describe_webspace_rebuild_state(webspace_id)

    assert result["accepted"] is True
    assert result["resolver"]["source"] == "legacy_yjs"
    assert result["resolver"]["legacy_fallback"] is True
    assert status["resolver"]["source"] == "legacy_yjs"
    assert status["resolver"]["legacy_fallback"] is True


def test_restore_webspace_from_snapshot_reconciles_runtime(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap({"current_scenario": "restored_prompt_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    rebuilds: list[str] = []
    workflows: list[tuple[str, str]] = []
    emitted: list[tuple[str, dict[str, object], str]] = []

    class _Bus:
        def publish(self, _event) -> None:
            return None

    fake_ctx = SimpleNamespace(bus=_Bus())

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG002
        rebuilds.append(webspace_id)
        return SimpleNamespace(scenario_id="restored_prompt_scenario", apps=[{"id": "app-1"}], widgets=[])

    async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):
        workflows.append((scenario_id, webspace_id))
        return None

    async def _fake_restore_ystore(_webspace_id: str) -> dict[str, object]:
        return {"ok": True, "accepted": True, "snapshot_path": "state/ystores/default.snapshot"}

    async def _fake_reset_live_room(_webspace_id: str, close_reason: str = "webspace_restore") -> dict[str, object]:
        return {"accepted": True, "close_reason": close_reason}

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
    monkeypatch.setattr(
        webspace_runtime_module,
        "emit",
        lambda bus, topic, payload, source: emitted.append((topic, dict(payload), source)),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway",
        types.SimpleNamespace(reset_live_webspace_room=_fake_reset_live_room),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(restore_ystore_for_webspace=_fake_restore_ystore),
    )

    result = asyncio.run(webspace_runtime_module.restore_webspace_from_snapshot("phase3-restore"))

    assert rebuilds == ["phase3-restore"]
    assert workflows == [("restored_prompt_scenario", "phase3-restore")]
    assert emitted == [
        (
            "desktop.webspace.restored",
            {
                "webspace_id": "phase3-restore",
                "action": "restore",
                "scenario_id": "restored_prompt_scenario",
                "snapshot_path": "state/ystores/default.snapshot",
            },
            "scenario.webspace_runtime",
        )
    ]
    assert result["accepted"] is True
    assert result["scenario_id"] == "restored_prompt_scenario"
    assert result["scenario_resolution"] == "current_scenario"
    assert result["source_of_truth"] == "snapshot"
    assert result["projection_refresh"]["scenario_id"] == "restored_prompt_scenario"
    assert result["projection_refresh"]["scenario_resolution"] == "current_scenario"


def test_phase3_reload_and_reset_rebuild_sync_workflow_for_target_scenario(monkeypatch) -> None:
    class _Bus:
        def publish(self, _event) -> None:
            return None

    for action in ("reload", "reset"):
        fake_state = {
            "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
            "registry": _FakeMap(),
            "data": _FakeMap(),
        }
        rebuilds: list[str] = []
        workflows: list[tuple[str, str]] = []
        emitted: list[tuple[str, dict[str, object], str]] = []
        fake_ctx = SimpleNamespace(bus=_Bus())

        async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG002
            rebuilds.append(webspace_id)
            self._last_rebuild_timings_ms = {
                "collect_inputs": 1.0,
                "resolve": 2.0,
                "apply": 3.0,
                "to_registry_entry": 0.5,
                "total": 6.5,
            }
            self._last_apply_summary = {
                "branch_count": 6,
                "changed_branches": 2,
                "unchanged_branches": 4,
                "failed_branches": 0,
                "changed_paths": ["ui.application", "registry.merged"],
                "defaults_failed": False,
            }
            return SimpleNamespace(scenario_id="prompt_engineer_scenario", apps=[{"id": "app-1"}], widgets=[])

        async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):
            workflows.append((scenario_id, webspace_id))
            return None

        async def _fake_refresh(ctx, webspace_id: str, *, scenario_id: str | None = None, scenario_resolution: str | None = None):
            return {
                "attempted": True,
                "scenario_id": scenario_id,
                "scenario_resolution": scenario_resolution,
                "space": "workspace",
                "rules_loaded": 1,
                "source": "scenario_manifest",
            }

        monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
        monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
        monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
        monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
        monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
        monkeypatch.setattr(
            webspace_runtime_module,
            "emit",
            lambda bus, topic, payload, source: emitted.append((topic, dict(payload), source)),
        )

        result = asyncio.run(
            webspace_runtime_module.rebuild_webspace_from_sources(
                f"phase3-{action}-workflow-sync",
                action=action,
                scenario_id="prompt_engineer_scenario",
                scenario_resolution="explicit",
                source_of_truth="scenario",
            )
        )

        assert rebuilds == [f"phase3-{action}-workflow-sync"]
        assert workflows == [("prompt_engineer_scenario", f"phase3-{action}-workflow-sync")]
        assert emitted == [
            (
                "desktop.webspace.reloaded",
                {
                    "webspace_id": f"phase3-{action}-workflow-sync",
                    "action": action,
                    "scenario_id": "prompt_engineer_scenario",
                },
                "scenario.webspace_runtime",
            )
        ]
        assert result["accepted"] is True
        assert result["scenario_resolution"] == "explicit"
        assert result["projection_refresh"]["scenario_resolution"] == "explicit"
        assert "workflow_sync" in result["timings_ms"]


def test_phase3_reload_reuses_live_runtime_without_reset(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    fake_ctx = SimpleNamespace(bus=SimpleNamespace(publish=lambda _event: None))
    project_calls: list[tuple[str, str, bool]] = []
    seed_calls: list[tuple[str, str, bool]] = []
    reset_calls: list[tuple[str, str]] = []
    rebuilds: list[str] = []
    listing_syncs: list[str] = []

    async def _fake_project(
        webspace_id: str,
        scenario_id: str,
        *,
        dev: bool | None = None,  # noqa: ARG001
        emit_event: bool = True,
    ) -> None:
        project_calls.append((webspace_id, scenario_id, emit_event))

    async def _fake_seed(
        webspace_id: str,
        scenario_id: str,
        *,
        dev: bool | None = None,  # noqa: ARG001
        emit_event: bool = True,
    ) -> None:
        seed_calls.append((webspace_id, scenario_id, emit_event))

    async def _fake_refresh(
        ctx,  # noqa: ARG001
        webspace_id: str,
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "space": "workspace",
            "rules_loaded": 1,
        }

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG002
        rebuilds.append(webspace_id)
        self._last_rebuild_timings_ms = {"total": 1.0}
        self._last_apply_summary = {"changed_branches": 1, "unchanged_branches": 0}
        return SimpleNamespace(scenario_id="prompt_engineer_scenario", apps=[], widgets=[])

    async def _fake_listing() -> None:
        listing_syncs.append("default")

    async def _fake_reset_live_room(_webspace_id: str, close_reason: str = "webspace_reset") -> dict[str, object]:
        reset_calls.append(("room", close_reason))
        return {"accepted": True}

    def _fake_reset_ystore(_webspace_id: str) -> None:
        reset_calls.append(("ystore", "reset"))

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(webspace_runtime_module, "_project_webspace_from_scenario", _fake_project)
    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario_with_options", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_listing)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway",
        types.SimpleNamespace(reset_live_webspace_room=_fake_reset_live_room),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(reset_ystore_for_webspace=_fake_reset_ystore),
    )

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            "phase3-soft-reload",
            action="reload",
            scenario_id="prompt_engineer_scenario",
            scenario_resolution="explicit",
            source_of_truth="scenario",
            reseed_from_scenario=True,
        )
    )

    assert project_calls == [("phase3-soft-reload", "prompt_engineer_scenario", False)]
    assert seed_calls == []
    assert reset_calls == []
    assert rebuilds == ["phase3-soft-reload"]
    assert listing_syncs == ["default"]
    assert result["accepted"] is True
    assert "project_scenario_payload" in result["timings_ms"]
    assert "reset_runtime_state" not in result["timings_ms"]


def test_phase3_reset_keeps_hard_runtime_reset(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    fake_ctx = SimpleNamespace(bus=SimpleNamespace(publish=lambda _event: None))
    project_calls: list[tuple[str, str, bool]] = []
    seed_calls: list[tuple[str, str, bool]] = []
    reset_calls: list[tuple[str, str]] = []

    async def _fake_project(
        webspace_id: str,
        scenario_id: str,
        *,
        dev: bool | None = None,  # noqa: ARG001
        emit_event: bool = True,
    ) -> None:
        project_calls.append((webspace_id, scenario_id, emit_event))

    async def _fake_seed(
        webspace_id: str,
        scenario_id: str,
        *,
        dev: bool | None = None,  # noqa: ARG001
        emit_event: bool = True,
    ) -> None:
        seed_calls.append((webspace_id, scenario_id, emit_event))

    async def _fake_refresh(
        ctx,  # noqa: ARG001
        webspace_id: str,
        *,
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
    ) -> dict[str, object]:
        return {
            "attempted": True,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "space": "workspace",
            "rules_loaded": 1,
        }

    async def _fake_rebuild(self, webspace_id: str, **kwargs):  # noqa: ARG001, ARG002
        self._last_rebuild_timings_ms = {"total": 1.0}
        self._last_apply_summary = {"changed_branches": 1, "unchanged_branches": 0}
        return SimpleNamespace(scenario_id="prompt_engineer_scenario", apps=[], widgets=[])

    async def _fake_listing() -> None:
        return None

    async def _fake_reset_live_room(_webspace_id: str, close_reason: str = "webspace_reset") -> dict[str, object]:
        reset_calls.append(("room", close_reason))
        return {"accepted": True}

    def _fake_reset_ystore(_webspace_id: str) -> None:
        reset_calls.append(("ystore", "reset"))

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(webspace_runtime_module, "_project_webspace_from_scenario", _fake_project)
    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario_with_options", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_listing)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway",
        types.SimpleNamespace(reset_live_webspace_room=_fake_reset_live_room),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(reset_ystore_for_webspace=_fake_reset_ystore),
    )

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            "phase3-hard-reset",
            action="reset",
            scenario_id="prompt_engineer_scenario",
            scenario_resolution="explicit",
            source_of_truth="scenario",
            reseed_from_scenario=True,
        )
    )

    assert project_calls == []
    assert seed_calls == [("phase3-hard-reset", "prompt_engineer_scenario", False)]
    assert reset_calls == [("room", "webspace_reset"), ("ystore", "reset")]
    assert result["accepted"] is True
    assert "reset_runtime_state" in result["timings_ms"]
    assert "seed_from_scenario" in result["timings_ms"]
