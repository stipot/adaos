from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

if "nats" not in sys.modules:
    sys.modules["nats"] = types.ModuleType("nats")
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.api import node_api as node_api_module
from adaos.apps.cli.commands import node as node_cli_module
from adaos.services.scenario import webspace_runtime as webspace_runtime_module
from adaos.services.status_card_registry import clear_status_card_registry, status_card_registry_snapshot


async def _awaitable(value):
    return value


class _FakeMap(dict):
    pass


class _FakeDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    def get_map(self, name: str) -> _FakeMap:
        return self._state.setdefault(name, _FakeMap())


class _FakeAsyncDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    async def __aenter__(self) -> _FakeDoc:
        return _FakeDoc(self._state)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def test_node_yjs_webspace_endpoint_coerces_legacy_default_to_desktop(monkeypatch) -> None:
    captured: list[str] = []

    async def _fake_desktop_snapshot(webspace_id: str):
        captured.append(webspace_id)
        return SimpleNamespace(to_dict=lambda: {"installed": {"apps": [], "widgets": []}})

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module.WebDesktopService,
        "get_snapshot_async",
        lambda self, webspace_id: _fake_desktop_snapshot(webspace_id),
    )
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_desktop_state("default"))

    assert captured == ["desktop"]
    assert result["webspace_id"] == "desktop"
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_yjs_switch_scenario_endpoint_forwards_set_home(monkeypatch) -> None:
    captured: list[tuple[str, str, bool]] = []
    published: list[tuple[str, str, str | None, bool, str | None]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool = False,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"role": kwargs.get("role"), "webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "scheduled",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "prompt_engineer_scenario",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "_publish_yjs_control_event",
        lambda action, webspace_id, result, scenario_id=None: published.append(
            (action, webspace_id, scenario_id, bool(result.get("switch_skipped")), str(result.get("skip_reason") or "").strip() or None)
        ),
    )

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(scenario_id="prompt_engineer_scenario", set_home=True),
        )
    )

    assert captured == [("phase2-node", "prompt_engineer_scenario", True, False)]
    assert result["ok"] is True
    assert result["runtime"]["webspace_id"] == "phase2-node"
    assert result["rebuild"]["status"] == "scheduled"
    assert published == [("scenario", "phase2-node", "prompt_engineer_scenario", False, None)]


def test_node_yjs_switch_scenario_endpoint_preserves_implicit_set_home(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool | None = None,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(scenario_id="prompt_engineer_scenario"),
        )
    )

    assert captured == [("phase2-node", "prompt_engineer_scenario", None, False)]
    assert result["ok"] is True
    assert result["set_home"] is None


def test_node_yjs_switch_scenario_endpoint_can_wait_for_rebuild(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None, bool]] = []

    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool | None = None,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home, wait_for_rebuild))
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "background_rebuild": not wait_for_rebuild,
        }

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "ready",
            "pending": False,
            "background": False,
            "action": "scenario_switch_rebuild",
            "scenario_id": "prompt_engineer_scenario",
        },
    )

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(
                scenario_id="prompt_engineer_scenario",
                wait_for_rebuild=True,
            ),
        )
    )

    assert captured == [("phase2-node", "prompt_engineer_scenario", None, True)]
    assert result["background_rebuild"] is False
    assert result["rebuild"]["status"] == "ready"


def test_node_yjs_switch_scenario_endpoint_propagates_skip_metadata(monkeypatch) -> None:
    async def _fake_switch(
        webspace_id: str,
        scenario_id: str,
        *,
        set_home: bool | None = None,
        wait_for_rebuild: bool = True,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "set_home": set_home,
            "background_rebuild": True,
            "switch_skipped": True,
            "skip_reason": "already_pending_rebuild",
        }

    published: list[dict[str, object]] = []

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "prompt_engineer_scenario",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "_publish_yjs_control_event",
        lambda action, webspace_id, result, scenario_id=None: published.append(
            {
                "action": action,
                "webspace_id": webspace_id,
                "scenario_id": scenario_id,
                "switch_skipped": bool(result.get("switch_skipped")),
                "skip_reason": result.get("skip_reason"),
            }
        ),
    )

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(scenario_id="prompt_engineer_scenario"),
        )
    )

    assert result["switch_skipped"] is True
    assert result["skip_reason"] == "already_pending_rebuild"
    assert result["rebuild"]["status"] == "running"
    assert published == [
        {
            "action": "scenario",
            "webspace_id": "phase2-node",
            "scenario_id": "prompt_engineer_scenario",
            "switch_skipped": True,
            "skip_reason": "already_pending_rebuild",
        }
    ]


def test_node_yjs_go_home_endpoint_uses_helper(monkeypatch) -> None:
    captured: list[str] = []
    published: list[tuple[str, str, str | None]] = []

    async def _fake_go_home(webspace_id: str, *, wait_for_rebuild: bool = True) -> dict[str, object]:
        captured.append((webspace_id, wait_for_rebuild))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": "prompt_engineer_scenario"}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "go_home_webspace", _fake_go_home)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "scheduled",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "prompt_engineer_scenario",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "_publish_yjs_control_event",
        lambda action, webspace_id, result, scenario_id=None: published.append((action, webspace_id, scenario_id)),
    )

    result = asyncio.run(node_api_module.node_yjs_go_home("phase2-home"))

    assert captured == [("phase2-home", False)]
    assert result["scenario_id"] == "prompt_engineer_scenario"
    assert result["runtime"]["webspace_id"] == "phase2-home"
    assert result["rebuild"]["status"] == "scheduled"
    assert published == [("go_home", "phase2-home", "prompt_engineer_scenario")]


def test_node_yjs_go_home_endpoint_can_wait_for_rebuild(monkeypatch) -> None:
    captured: list[tuple[str, bool]] = []

    async def _fake_go_home(webspace_id: str, *, wait_for_rebuild: bool = True) -> dict[str, object]:
        captured.append((webspace_id, wait_for_rebuild))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": "web_desktop"}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "go_home_webspace", _fake_go_home)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "ready",
            "pending": False,
            "background": False,
            "action": "scenario_switch_rebuild",
            "scenario_id": "web_desktop",
        },
    )

    result = asyncio.run(
        node_api_module.node_yjs_go_home(
            "phase2-home",
            node_api_module.WebspaceYjsActionRequest(wait_for_rebuild=True),
        )
    )

    assert captured == [("phase2-home", True)]
    assert result["scenario_id"] == "web_desktop"
    assert result["rebuild"]["status"] == "ready"


def test_node_yjs_set_home_current_publishes_correct_action(monkeypatch) -> None:
    published: list[tuple[str, str, str | None]] = []
    async def _fake_set_current(webspace_id: str) -> dict[str, object]:
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": "web_desktop"}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "set_current_webspace_home",
        _fake_set_current,
    )
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(
        node_api_module,
        "_publish_yjs_control_event",
        lambda action, webspace_id, result, scenario_id=None: published.append((action, webspace_id, scenario_id)),
    )

    result = asyncio.run(node_api_module.node_yjs_set_home_current("phase2-home"))

    assert result["ok"] is True
    assert published == [("set_home_current", "phase2-home", "web_desktop")]


def test_node_yjs_toggle_install_endpoint_uses_desktop_service(monkeypatch) -> None:
    captured: list[tuple[str, str, str]] = []

    class _Installed:
        def to_dict(self) -> dict[str, list[str]]:
            return {"apps": ["scenario:prompt_engineer_scenario"], "widgets": ["weather"]}

    class _DesktopService:
        def toggle_install_with_live_room(self, item_type: str, item_id: str, webspace_id: str | None = None) -> None:
            captured.append((item_type, item_id, str(webspace_id or "")))

        async def get_installed_async(self, webspace_id: str | None = None) -> _Installed:
            assert webspace_id == "desktop"
            return _Installed()

        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return SimpleNamespace(
                to_dict=lambda: {
                    "installed": {"apps": ["scenario:prompt_engineer_scenario"], "widgets": ["weather"]},
                    "pinnedWidgets": [],
                }
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_toggle_install(
            "default",
            node_api_module.WebspaceToggleInstallRequest(type="widget", id="weather"),
        )
    )

    assert captured == [("widget", "weather", "desktop")]
    assert result["ok"] is True
    assert result["installed"]["widgets"] == ["weather"]
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_infrastate_snapshot_endpoint_runs_skill_tool(monkeypatch) -> None:
    clear_status_card_registry()
    captured: list[tuple[str, str, dict[str, object]]] = []

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            captured.append((skill_name, tool_name, dict(payload)))
            return {"summary": {"label": "Core update", "value": "idle"}}

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "get_ctx", lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=None, caps=None, settings=None))
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(node_api_module.node_infrastate_snapshot("default"))

    assert captured == [("infrastate_skill", "get_snapshot", {"webspace_id": "desktop", "project": False})]
    assert result["ok"] is True
    assert result["snapshot"]["summary"]["value"] == "idle"
    assert result["status_cards"]["card_total"] == 5
    assert result["status_cards"]["cards"][0]["owner"] == "skill:infrastate_skill"
    registry = status_card_registry_snapshot(webspace_id="desktop")
    assert registry["card_total"] == 5


def test_node_infrastate_snapshot_endpoint_returns_fallback_on_tool_error(monkeypatch) -> None:
    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            raise RuntimeError(f"boom:{skill_name}:{tool_name}:{payload.get('webspace_id')}")

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1"))
    monkeypatch.setattr(node_api_module, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready", "reason": "ok"})
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"assessment": {"state": "nominal"}, "webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(node_api_module, "get_ctx", lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=None, caps=None, settings=None))
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(node_api_module.node_infrastate_snapshot("default"))

    assert result["ok"] is True
    assert result["degraded"] is True
    assert "RuntimeError" in str(result["error"])
    assert result["snapshot"]["fallback"] is True
    assert result["snapshot"]["summary"]["label"] == "Infra State"


def test_node_infrastate_action_endpoint_publishes_event_and_returns_snapshot(monkeypatch) -> None:
    published: list[object] = []
    wait_calls: list[float] = []
    captured: list[tuple[str, str, dict[str, object]]] = []

    class _FakeBus:
        def publish(self, event) -> None:
            published.append(event)

        async def wait_for_idle(self, timeout: float = 0.0) -> bool:
            wait_calls.append(timeout)
            return True

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            captured.append((skill_name, tool_name, dict(payload)))
            return {
                "summary": {"label": "Infra State", "value": "ready"},
                "last_refresh_ts": 123.0,
                "ui_state": {
                    "last_action": "select_node",
                    "last_result": {
                        "ok": True,
                        "operation_id": "op-node-select",
                    },
                },
            }

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "get_ctx",
        lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=_FakeBus(), caps=None, settings=None),
    )
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(
        node_api_module.node_infrastate_action(
            node_api_module.InfrastateActionRequest(
                id="select_node",
                webspace_id="default",
                node_id="member-1",
            )
        )
    )

    assert len(published) == 1
    assert getattr(published[0], "type", "") == "infrastate.action"
    assert getattr(published[0], "payload", {})["node_id"] == "member-1"
    assert wait_calls == [2.5]
    assert captured == [("infrastate_skill", "get_snapshot", {"webspace_id": "desktop", "project": False})]
    assert result["ok"] is True
    assert result["action"] == "select_node"
    assert result["operation_id"] == "op-node-select"
    assert result["result"]["operation_id"] == "op-node-select"
    assert result["snapshot"]["summary"]["value"] == "ready"


def test_node_infrastate_action_marketplace_install_returns_fast_operation_ack(monkeypatch) -> None:
    published: list[object] = []
    wait_calls: list[float] = []
    submitted: list[dict[str, object]] = []

    class _FakeBus:
        def publish(self, event) -> None:
            published.append(event)

        async def wait_for_idle(self, timeout: float = 0.0) -> bool:
            wait_calls.append(timeout)
            return True

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "get_ctx",
        lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=_FakeBus(), caps=None, settings=None),
    )

    def _submit_install_operation(**kwargs):
        submitted.append(dict(kwargs))
        return {
            "operation_id": "op-scenario-install",
            "target_kind": kwargs["target_kind"],
            "target_id": kwargs["target_id"],
            "status": "accepted",
        }

    monkeypatch.setattr(node_api_module, "submit_install_operation", _submit_install_operation)

    result = asyncio.run(
        node_api_module.node_infrastate_action(
            node_api_module.InfrastateActionRequest(
                id="marketplace_install",
                webspace_id="default",
                value={
                    "kind": "scenario",
                    "id": "prompt_engineer_scenario",
                },
            )
        )
    )

    assert published == []
    assert wait_calls == []
    assert len(submitted) == 1
    assert submitted[0]["target_kind"] == "scenario"
    assert submitted[0]["target_id"] == "prompt_engineer_scenario"
    assert submitted[0]["webspace_id"] == "desktop"
    assert submitted[0]["initiator"] == {"kind": "api.node", "id": "marketplace_install"}
    assert submitted[0]["ctx"] is not None
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["action"] == "marketplace_install"
    assert result["operation_id"] == "op-scenario-install"
    assert result["result"]["operation"]["target_id"] == "prompt_engineer_scenario"
    assert result["snapshot"] == {}


def test_node_infra_access_action_endpoint_runs_skill_tools(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "get_ctx",
        lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=None, caps=None, settings=None),
    )

    class _FakeSkillManager:
        def __init__(self, *args, **kwargs) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]):
            self.calls.append((skill_name, tool_name, dict(payload)))
            if tool_name == "issue_codex_connection":
                return {
                    "ok": True,
                    "session_id": "sess-1",
                    "access_token": "secret",
                }
            if tool_name in {"get_snapshot", "refresh_snapshot"}:
                return {
                    "ok": True,
                    "summary": {"label": "Active MCP credentials"},
                }
            return {"ok": True}

    manager = _FakeSkillManager()
    monkeypatch.setattr(node_api_module, "SkillManager", lambda *args, **kwargs: manager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)

    async def _fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(
        node_api_module.node_infra_access_action(
            node_api_module.InfraAccessActionRequest(
                id="issue_codex_session",
                webspace_id="default",
                capability_profile="ProfileOpsRead",
                ttl_seconds=600,
            )
        )
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["action"] == "issue_codex_session"
    assert result["result"]["session_id"] == "sess-1"
    assert result["snapshot"]["summary"]["label"] == "Active MCP credentials"
    assert manager.calls == [
        (
            "infra_access_skill",
            "issue_codex_connection",
            {
                "webspace_id": "desktop",
                "target_id": None,
                "capability_profile": "ProfileOpsRead",
                "ttl_seconds": 600,
            },
        ),
        (
            "infra_access_skill",
            "get_snapshot",
            {
                "webspace_id": "desktop",
                "target_id": None,
            },
        ),
    ]


def test_node_yjs_set_home_requires_scenario_id(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))

    result = asyncio.run(node_api_module.node_yjs_set_home("phase2-home", node_api_module.WebspaceYjsActionRequest()))

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["error"] == "scenario_id_required"


def test_node_yjs_ensure_dev_endpoint_forwards_requested_id_and_title(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    async def _fake_ensure_dev(scenario_id: str, *, requested_id: str | None = None, title: str | None = None) -> dict[str, object]:
        captured.append(
            {
                "scenario_id": scenario_id,
                "requested_id": requested_id,
                "title": title,
            }
        )
        return {
            "ok": True,
            "accepted": True,
            "created": False,
            "webspace_id": requested_id or "dev_prompt_engineer_scenario",
            "scenario_id": scenario_id,
            "home_scenario": scenario_id,
        }

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "ensure_dev_webspace_for_scenario", _fake_ensure_dev)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_ensure_dev(
            node_api_module.WebspaceYjsActionRequest(
                scenario_id="prompt_engineer_scenario",
                requested_id="dev_prompt",
                title="Prompt IDE",
            )
        )
    )

    assert captured == [
        {
            "scenario_id": "prompt_engineer_scenario",
            "requested_id": "dev_prompt",
            "title": "Prompt IDE",
        }
    ]
    assert result["ok"] is True
    assert result["runtime"]["webspace_id"] == "dev_prompt"


def test_node_yjs_webspaces_endpoint_returns_manifest_listing(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "WebspaceService",
        lambda: SimpleNamespace(
            list=lambda mode="mixed": [
                SimpleNamespace(
                    id="default",
                    title="Desktop",
                    created_at=123.0,
                    kind="workspace",
                    home_scenario="web_desktop",
                    source_mode="workspace",
                ),
                SimpleNamespace(
                    id="dev_prompt",
                    title="Prompt IDE",
                    created_at=456.0,
                    kind="dev",
                    home_scenario="prompt_engineer_scenario",
                    source_mode="dev",
                ),
            ]
        ),
    )

    result = asyncio.run(node_api_module.node_yjs_webspaces())

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["items"][0]["id"] == "default"
    assert result["items"][1]["kind"] == "dev"


def test_node_yjs_create_webspace_endpoint_uses_service(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _Svc:
        async def create(self, requested_id, title, *, scenario_id="web_desktop", scenario_ref=None, dev=False):
            captured.append(
                {
                    "requested_id": requested_id,
                    "title": title,
                    "scenario_id": scenario_id,
                    "scenario_ref": scenario_ref,
                    "dev": dev,
                }
            )
            return SimpleNamespace(
                id="preview-space",
                title=title or "Preview Space",
                created_at=123.0,
                kind="dev" if dev else "workspace",
                home_scenario=scenario_id,
                source_mode="dev" if dev else "workspace",
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebspaceService", _Svc)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_create_webspace(
            node_api_module.WebspaceCreateRequest(
                id="preview-space",
                title="Preview Space",
                scenario_id="prompt_engineer_scenario",
                dev=True,
            )
        )
    )

    assert captured == [
        {
            "requested_id": "preview-space",
            "title": "Preview Space",
            "scenario_id": "prompt_engineer_scenario",
            "scenario_ref": None,
            "dev": True,
        }
    ]
    assert result["ok"] is True
    assert result["webspace"]["id"] == "preview-space"
    assert result["runtime"]["webspace_id"] == "preview-space"


def test_node_yjs_update_webspace_endpoint_uses_service(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _Svc:
        async def update_metadata(self, webspace_id, *, title=None, home_scenario=None):
            captured.append(
                {
                    "webspace_id": webspace_id,
                    "title": title,
                    "home_scenario": home_scenario,
                }
            )
            return SimpleNamespace(
                id=webspace_id,
                title=title or "Desktop",
                created_at=123.0,
                kind="workspace",
                home_scenario=home_scenario or "web_desktop",
                source_mode="workspace",
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebspaceService", _Svc)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_update_webspace(
            "default",
            node_api_module.WebspaceUpdateRequest(
                title="Desktop",
                home_scenario="prompt_engineer_scenario",
            ),
        )
    )

    assert captured == [
        {
            "webspace_id": "desktop",
            "title": "Desktop",
            "home_scenario": "prompt_engineer_scenario",
        }
    ]
    assert result["ok"] is True
    assert result["webspace"]["home_scenario"] == "prompt_engineer_scenario"
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_yjs_webspace_state_endpoint_returns_operational_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_operational_state",
        lambda webspace_id: _awaitable(
            SimpleNamespace(
                to_dict=lambda: {
                    "webspace_id": webspace_id,
                    "kind": "dev",
                    "source_mode": "dev",
                    "home_scenario": "prompt_engineer_scenario",
                    "current_scenario": "prompt_engineer_runtime",
                    "current_matches_home": False,
                }
            )
        ),
    )
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_validation_state",
        lambda webspace_id: _awaitable(
            {
                "webspace_id": webspace_id,
                "source_mode": "dev",
                "stored_home_scenario": "prompt_engineer_scenario",
                "home_scenario": "prompt_engineer_scenario",
                "current_scenario": "prompt_engineer_runtime",
                "stored_home_scenario_exists": True,
                "home_scenario_exists": True,
                "current_scenario_exists": True,
                "degraded": False,
                "validation_reason": None,
                "recommended_action": None,
            }
        ),
    )
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_overlay_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "source": "workspace_manifest_overlay",
            "has_overlay": True,
            "has_installed": True,
            "has_pinned_widgets": True,
            "has_topbar": False,
            "has_page_schema": False,
            "installed": {"apps": ["scenario:prompt_engineer_runtime"], "widgets": []},
            "pinned_widgets": [{"id": "infra-status", "type": "visual.metricTile"}],
            "topbar": [],
            "page_schema": {},
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_projection_state",
        lambda webspace_id: _awaitable(
            {
                "webspace_id": webspace_id,
                "target_scenario": "prompt_engineer_runtime",
                "target_space": "dev",
                "active_scenario": "prompt_engineer_runtime",
                "active_space": "dev",
                "active_matches_target": True,
                "base_rule_count": 2,
                "scenario_rule_count": 1,
            }
        ),
    )
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "ready",
            "pending": False,
            "background": False,
            "action": "scenario_switch_rebuild",
            "scenario_id": "prompt_engineer_runtime",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "_describe_yjs_materialization",
        lambda webspace_id, rebuild_state=None: _awaitable(
            {
                "ready": True,
                "webspace_id": webspace_id,
                "current_scenario": "prompt_engineer_runtime",
                "has_desktop_page_schema": True,
                "has_catalog_apps": True,
                "has_catalog_widgets": True,
                "catalog_counts": {"apps": 3, "widgets": 2},
                "compatibility_caches": {
                    "client_fallback_readable": True,
                    "present_count": 3,
                    "required_count": 3,
                    "complete": True,
                    "switch_writes_enabled": False,
                    "legacy_fallback_active": False,
                    "runtime_removal_ready": True,
                    "runtime_removal_blockers": [],
                },
            }
        ),
    )
    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "dev_prompt"
            return SimpleNamespace(
                to_dict=lambda: {
                    "installed": {"apps": ["scenario:prompt_engineer_runtime"], "widgets": []},
                    "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
                    "topbar": [{"id": "home", "label": "Home"}],
                    "pageSchema": {
                        "id": "desktop-custom",
                        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [],
                    },
                }
            )

    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_webspace_state("dev_prompt"))

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["webspace"]["webspace_id"] == "dev_prompt"
    assert result["validation"]["current_scenario_exists"] is True
    assert result["overlay"]["has_pinned_widgets"] is True
    assert result["overlay"]["pinned_widgets"][0]["id"] == "infra-status"
    assert result["overlay"]["topbar"] == []
    assert result["desktop"]["pinnedWidgets"][0]["id"] == "infra-status"
    assert result["desktop"]["pageSchema"]["id"] == "desktop-custom"
    assert result["webspace"]["source_mode"] == "dev"
    assert result["projection"]["active_scenario"] == "prompt_engineer_runtime"
    assert result["rebuild"]["status"] == "ready"
    assert result["materialization"]["ready"] is True
    assert result["materialization"]["catalog_counts"]["apps"] == 3
    assert result["runtime"]["webspace_id"] == "dev_prompt"


def test_node_yjs_webspace_rebuild_state_endpoint_returns_lightweight_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "infrascope",
            "request_id": "req-bench-1",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "yjs_sync_runtime_snapshot",
        lambda **kwargs: {"role": kwargs.get("role"), "webspace_id": kwargs.get("webspace_id")},
    )

    result = asyncio.run(node_api_module.node_yjs_webspace_rebuild_state("default"))

    assert result == {
        "ok": True,
        "accepted": True,
        "webspace_id": "desktop",
        "rebuild": {
            "webspace_id": "desktop",
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "infrascope",
            "request_id": "req-bench-1",
        },
    }


def test_node_yjs_webspace_materialization_state_endpoint_returns_lightweight_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "infrascope",
            "request_id": "req-bench-1",
        },
    )
    monkeypatch.setattr(
        node_api_module,
        "_describe_yjs_materialization",
        lambda webspace_id, rebuild_state=None: _awaitable(
            {
                "ready": False,
                "webspace_id": webspace_id,
                "current_scenario": "infrascope",
                "readiness_state": "interactive",
                "missing_branches": ["data.desktop"],
            }
        ),
    )
    monkeypatch.setattr(
        node_api_module,
        "yjs_sync_runtime_snapshot",
        lambda **kwargs: {"role": kwargs.get("role"), "webspace_id": kwargs.get("webspace_id")},
    )

    result = asyncio.run(node_api_module.node_yjs_webspace_materialization_state("default"))

    assert result == {
        "ok": True,
        "accepted": True,
        "webspace_id": "desktop",
        "materialization": {
            "ready": False,
            "webspace_id": "desktop",
            "current_scenario": "infrascope",
            "readiness_state": "interactive",
            "missing_branches": ["data.desktop"],
        },
        "rebuild": {
            "webspace_id": "desktop",
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "infrascope",
            "request_id": "req-bench-1",
        },
    }


def test_node_yjs_webspace_rebuild_state_endpoint_includes_cached_materialization(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {
            "webspace_id": webspace_id,
            "status": "running",
            "pending": True,
            "background": True,
            "action": "scenario_switch_rebuild",
            "scenario_id": "infrascope",
            "request_id": "req-bench-2",
            "materialization": {
                "ready": False,
                "webspace_id": webspace_id,
                "current_scenario": "infrascope",
                "readiness_state": "pending_structure",
                "missing_branches": ["ui.application"],
                "snapshot_source": "rebuild:running",
                "stale": True,
            },
        },
    )

    result = asyncio.run(node_api_module.node_yjs_webspace_rebuild_state("default"))

    assert result["rebuild"]["materialization"]["readiness_state"] == "pending_structure"
    assert result["rebuild"]["materialization"]["snapshot_source"] == "rebuild:running"


def test_describe_yjs_materialization_prefers_cached_rebuild_snapshot_while_pending(monkeypatch) -> None:
    result = asyncio.run(
        node_api_module._describe_yjs_materialization(
            "desktop",
            rebuild_state={
                "webspace_id": "desktop",
                "status": "running",
                "pending": True,
                "materialization": {
                    "ready": False,
                    "webspace_id": "desktop",
                    "current_scenario": "infrascope",
                    "readiness_state": "hydrating",
                    "missing_branches": ["data.catalog.apps"],
                    "snapshot_source": "semantic_rebuild:structure",
                    "observed_at": 123.0,
                    "stale": False,
                },
            },
        )
    )

    assert result["readiness_state"] == "hydrating"
    assert result["snapshot_source"] == "semantic_rebuild:structure"


def test_describe_yjs_materialization_reports_ready_readiness_and_no_missing_branches(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap(
            {
                "current_scenario": "prompt_engineer_scenario",
                "application": {
                    "desktop": {
                        "pageSchema": {"id": "desktop", "widgets": [{"id": "main-widget"}]},
                        "topbar": [{"id": "home"}],
                    },
                    "modals": {
                        "apps_catalog": {"title": "Apps"},
                        "widgets_catalog": {"title": "Widgets"},
                    },
                },
                "scenarios": {
                    "hub-1": {
                        "prompt_engineer_scenario": {
                            "application": {
                                "desktop": {
                                    "pageSchema": {"id": "node-desktop"},
                                },
                            },
                        },
                    }
                },
            }
        ),
        "registry": _FakeMap(
            {
                "scenarios": {
                    "hub-1": {
                        "prompt_engineer_scenario": {
                            "modals": ["node-modal"],
                        }
                    }
                }
            }
        ),
        "data": _FakeMap(
            {
                "catalog": {
                    "apps": [{"id": "prompt_ide"}],
                    "widgets": [{"id": "weather"}],
                },
                "scenarios": {
                    "hub-1": {
                        "prompt_engineer_scenario": {
                            "catalog": {
                                "apps": [{"id": "node-app"}],
                            }
                        }
                    }
                },
            }
        ),
    }

    monkeypatch.setattr(node_api_module, "async_read_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(node_api_module, "_local_node_id", lambda: "hub-1")

    result = asyncio.run(node_api_module._describe_yjs_materialization("default"))

    assert result["ready"] is True
    assert result["readiness_state"] == "ready"
    assert result["missing_branches"] == []
    assert result["compatibility_caches"]["complete"] is True
    assert result["compatibility_caches"]["client_fallback_readable"] is True
    assert result["compatibility_caches"]["runtime_removal_ready"] is True


def test_describe_yjs_materialization_reports_hydrating_readiness_and_missing_branches(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap(
            {
                "current_scenario": "prompt_engineer_scenario",
                "application": {
                    "desktop": {
                        "pageSchema": {"id": "desktop", "widgets": [{"id": "main-widget"}]},
                    },
                },
            }
        ),
        "data": _FakeMap(
            {
                "catalog": {
                    "widgets": [{"id": "weather"}],
                }
            }
        ),
    }

    monkeypatch.setattr(node_api_module, "async_read_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    result = asyncio.run(node_api_module._describe_yjs_materialization("default"))

    assert result["ready"] is False
    assert result["readiness_state"] == "hydrating"
    assert "data.catalog.apps" in result["missing_branches"]
    assert "ui.application.modals.apps_catalog" in result["missing_branches"]
    assert result["compatibility_caches"]["runtime_removal_ready"] is False
    assert "effective_materialization_not_ready" in result["compatibility_caches"]["runtime_removal_blockers"]


def test_describe_compatibility_caches_reports_runtime_removal_ready_when_runtime_is_clean(monkeypatch) -> None:
    result = node_api_module._describe_compatibility_caches(
        current_scenario="prompt_engineer_scenario",
        has_scenario_ui_application=True,
        has_scenario_registry_entry=True,
        has_scenario_catalog=True,
        effective_ready=True,
        rebuild_state={"resolver": {"legacy_fallback": False}},
    )

    assert result["present_count"] == 3
    assert result["required_count"] == 3
    assert result["complete"] is True
    assert result["client_fallback_readable"] is True
    assert result["runtime_removal_ready"] is True
    assert result["runtime_removal_blockers"] == []


def test_describe_compatibility_caches_reports_runtime_removal_blockers(monkeypatch) -> None:
    result = node_api_module._describe_compatibility_caches(
        current_scenario="prompt_engineer_scenario",
        has_scenario_ui_application=False,
        has_scenario_registry_entry=True,
        has_scenario_catalog=False,
        effective_ready=False,
        rebuild_state={"resolver": {"legacy_fallback": True}},
    )

    assert result["client_fallback_readable"] is False
    assert result["switch_writes_enabled"] is False
    assert result["legacy_fallback_active"] is True
    assert result["runtime_removal_ready"] is False
    assert set(result["runtime_removal_blockers"]) == {
        "effective_materialization_not_ready",
        "resolver_legacy_fallback_active",
    }


def test_describe_webspace_operational_state_prefers_live_room_fast_path(monkeypatch) -> None:
    row = SimpleNamespace(
        title="Desktop",
        effective_kind="workspace",
        effective_source_mode="workspace",
        is_dev=False,
        home_scenario="web_desktop",
        effective_home_scenario="web_desktop",
    )

    monkeypatch.setattr(webspace_runtime_module.workspace_index, "get_workspace", lambda webspace_id: row)
    monkeypatch.setattr(webspace_runtime_module.workspace_index, "ensure_workspace", lambda webspace_id: row)
    monkeypatch.setattr(
        webspace_runtime_module,
        "try_read_live_map_value",
        lambda webspace_id, map_name, key: (True, "infrascope"),
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "async_read_ydoc",
        lambda _webspace_id: (_ for _ in ()).throw(AssertionError("async_read_ydoc should not run when live room is readable")),
    )

    result = asyncio.run(webspace_runtime_module.describe_webspace_operational_state("default"))

    assert result.webspace_id == "default"
    assert result.current_scenario == "infrascope"
    assert result.effective_home_scenario == "web_desktop"


def test_switch_webspace_scenario_uses_live_room_pointer_fast_path(monkeypatch) -> None:
    row = SimpleNamespace(
        effective_kind="workspace",
        effective_source_mode="workspace",
        effective_home_scenario="web_desktop",
        is_dev=False,
    )
    scheduled: list[dict[str, object]] = []
    live_mutations: list[str] = []
    describe_calls = {"count": 0}

    async def _fake_operational_state(_webspace_id: str):
        return SimpleNamespace(
            current_scenario="web_desktop",
            effective_home_scenario="web_desktop",
        )

    def _fake_describe_rebuild(_webspace_id: str) -> dict[str, object]:
        describe_calls["count"] += 1
        if describe_calls["count"] == 1:
            return {"status": "idle", "pending": False, "scenario_id": "web_desktop"}
        return {
            "request_id": "req-live-1",
            "status": "scheduled",
            "pending": True,
            "background": True,
            "scenario_id": "infrascope",
        }

    monkeypatch.setattr(webspace_runtime_module, "describe_webspace_operational_state", _fake_operational_state)
    monkeypatch.setattr(webspace_runtime_module.workspace_index, "get_workspace", lambda webspace_id: row)
    monkeypatch.setattr(webspace_runtime_module.workspace_index, "ensure_workspace", lambda webspace_id: row)
    monkeypatch.setattr(webspace_runtime_module, "_scenario_switch_mode", lambda: "pointer_only")
    monkeypatch.setattr(webspace_runtime_module, "_scenario_exists_for_switch", lambda scenario_id, space="workspace": True)
    monkeypatch.setattr(
        webspace_runtime_module,
        "mutate_live_room",
        lambda webspace_id, mutator, **_kwargs: (live_mutations.append(str(webspace_id)), True)[1],
    )
    monkeypatch.setattr(
        webspace_runtime_module,
        "async_get_ydoc",
        lambda _webspace_id: (_ for _ in ()).throw(AssertionError("async_get_ydoc should not run on live-room pointer fast path")),
    )
    monkeypatch.setattr(webspace_runtime_module, "_schedule_scenario_switch_rebuild", lambda *args, **kwargs: scheduled.append(dict(kwargs)))
    monkeypatch.setattr(webspace_runtime_module, "describe_webspace_rebuild_state", _fake_describe_rebuild)
    monkeypatch.setattr(webspace_runtime_module, "_set_webspace_rebuild_status_if_current", lambda *args, **kwargs: None)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            "default",
            "infrascope",
            wait_for_rebuild=False,
        )
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["background_rebuild"] is True
    assert result["scenario_switch_mode"] == "pointer_only"
    assert live_mutations == ["default"]
    assert len(scheduled) == 1
    assert scheduled[0]["scenario_id"] == "infrascope"
    assert scheduled[0]["scenario_resolution"] == "explicit"
    assert scheduled[0]["switch_mode"] == "pointer_only"
    assert isinstance(scheduled[0]["switch_timings_ms"], dict)
    assert "write_switch_pointer" in result["timings_ms"]
    assert "open_doc" not in result["timings_ms"]


def test_node_yjs_desktop_state_endpoint_returns_snapshot(monkeypatch) -> None:
    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return SimpleNamespace(
                to_dict=lambda: {
                    "installed": {"apps": ["scenario:web_desktop"], "widgets": ["weather"]},
                    "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
                    "topbar": [{"id": "home", "label": "Home"}],
                    "pageSchema": {
                        "id": "desktop",
                        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [],
                    },
                }
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_desktop_state("default"))

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["desktop"]["installed"]["widgets"] == ["weather"]
    assert result["desktop"]["pinnedWidgets"][0]["id"] == "infra-status"
    assert result["desktop"]["topbar"][0]["id"] == "home"
    assert result["desktop"]["pageSchema"]["id"] == "desktop"
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_yjs_catalog_state_endpoint_returns_items_and_materialization(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "_describe_yjs_materialization",
        lambda webspace_id, rebuild_state=None: _awaitable(
            {
                "ready": False,
                "webspace_id": webspace_id,
                "current_scenario": "web_desktop",
                "has_desktop_page_schema": False,
                "has_catalog_apps": False,
                "has_catalog_widgets": True,
                "catalog_counts": {"apps": 0, "widgets": 2},
                "compatibility_caches": {
                    "client_fallback_readable": False,
                    "present_count": 1,
                    "required_count": 3,
                    "complete": False,
                    "switch_writes_enabled": False,
                    "legacy_fallback_active": False,
                    "runtime_removal_ready": False,
                    "runtime_removal_blockers": ["effective_materialization_not_ready"],
                },
            }
        ),
    )
    monkeypatch.setattr(
        node_api_module,
        "_read_live_catalog_items",
        lambda webspace_id, kind: _awaitable(
            [
                {
                    "id": "weather",
                    "title": "Weather",
                    "node_id": "member-1",
                    "node_label": "Node 1",
                    "node_compact_label": "N1",
                    "node_color": "#F28E2B",
                    "node_index": 1,
                    "node_local_id": "weather",
                }
            ]
            if kind == "widgets"
            else []
        ),
    )
    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return SimpleNamespace(
                installed=SimpleNamespace(apps=[], widgets=["weather"]),
                pinned_widgets=[{"id": "weather", "type": "visual.metricTile"}],
            )

    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_catalog_state("default", "widgets"))

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["kind"] == "widgets"
    assert result["items"][0]["id"] == "weather"
    assert result["items"][0]["installType"] == "widget"
    assert result["items"][0]["installed"] is True
    assert result["items"][0]["pinned"] is True
    assert result["items"][0]["node_id"] == "member-1"
    assert result["items"][0]["node_label"] == "Node 1"
    assert result["items"][0]["node_compact_label"] == "N1"
    assert result["items"][0]["node_index"] == 1
    assert result["items"][0]["node_local_id"] == "weather"
    assert result["materialization"]["ready"] is False
    assert result["rebuild"]["webspace_id"] == "desktop"
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_yjs_set_pinned_widgets_endpoint_uses_desktop_service(monkeypatch) -> None:
    captured: list[tuple[list[dict[str, object]], str]] = []

    class _DesktopService:
        def set_pinned_widgets_with_live_room(self, pinned_widgets: list[dict[str, object]], webspace_id: str | None = None) -> None:
            captured.append((list(pinned_widgets), str(webspace_id or "")))

        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return SimpleNamespace(
                to_dict=lambda: {
                    "installed": {"apps": [], "widgets": ["weather"]},
                    "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
                    "topbar": [{"id": "home", "label": "Home"}],
                    "pageSchema": {
                        "id": "desktop",
                        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [],
                    },
                }
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_set_pinned_widgets(
            "default",
            node_api_module.WebspacePinnedWidgetsRequest(
                pinnedWidgets=[{"id": "infra-status", "type": "visual.metricTile"}]
            ),
        )
    )

    assert captured == [
        (
            [{"id": "infra-status", "type": "visual.metricTile"}],
            "desktop",
        )
    ]
    assert result["ok"] is True
    assert result["desktop"]["pinnedWidgets"][0]["id"] == "infra-status"
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_yjs_update_desktop_endpoint_uses_snapshot_update(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return node_api_module.WebDesktopSnapshot(
                installed=node_api_module.WebDesktopInstalled(apps=["scenario:web_desktop"], widgets=["weather"]),
                pinned_widgets=[{"id": "infra-status", "type": "visual.metricTile"}],
                topbar=[{"id": "home", "label": "Home"}],
                page_schema={"id": "desktop", "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]}, "widgets": []},
                icon_order=["scenario:web_desktop"],
                widget_order=["weather"],
            )

        def set_snapshot_with_live_room(self, snapshot, webspace_id: str | None = None) -> None:
            captured.append(
                {
                    "webspace_id": str(webspace_id or ""),
                    "installed": snapshot.installed.to_dict(),
                    "pinnedWidgets": list(snapshot.pinned_widgets),
                    "topbar": list(snapshot.topbar),
                    "pageSchema": dict(snapshot.page_schema),
                    "iconOrder": list(snapshot.icon_order),
                    "widgetOrder": list(snapshot.widget_order),
                }
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_update_desktop(
            "default",
            node_api_module.WebspaceDesktopUpdateRequest(
                topbar=[{"id": "overlay-home", "label": "Overlay Home"}],
                pageSchema={
                    "id": "desktop-custom",
                    "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                    "widgets": [{"id": "desktop-widgets", "type": "desktop.widgets", "area": "main"}],
                },
                iconOrder=["scenario:web_desktop"],
                widgetOrder=["infra-status"],
            ),
        )
    )

    assert captured == [
        {
            "webspace_id": "desktop",
            "installed": {"apps": ["scenario:web_desktop"], "widgets": ["weather"]},
            "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
            "topbar": [{"id": "overlay-home", "label": "Overlay Home"}],
            "pageSchema": {
                "id": "desktop-custom",
                "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                "widgets": [{"id": "desktop-widgets", "type": "desktop.widgets", "area": "main"}],
            },
            "iconOrder": ["scenario:web_desktop"],
            "widgetOrder": ["infra-status"],
        }
    ]
    assert result["ok"] is True
    assert result["runtime"]["webspace_id"] == "desktop"


def test_node_cli_yjs_control_action_includes_set_home(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "webspace_id": "phase2-home", "scenario_id": "prompt_engineer_scenario"})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_control_action(
        action="scenario",
        webspace="phase2-home",
        scenario_id="prompt_engineer_scenario",
        set_home=True,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces/phase2-home/scenario",
            "body": {"scenario_id": "prompt_engineer_scenario", "set_home": True},
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_scenario_command_omits_set_home_when_flag_is_absent(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    monkeypatch.setattr(
        node_cli_module,
        "_node_yjs_control_action",
        lambda **kwargs: captured.append(dict(kwargs)),
    )

    node_cli_module.node_yjs_scenario(
        webspace="phase2-home",
        scenario_id="prompt_engineer_scenario",
        set_home=False,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "action": "scenario",
            "webspace": "phase2-home",
            "scenario_id": "prompt_engineer_scenario",
            "set_home": None,
            "control": "http://127.0.0.1:8080",
            "json_output": True,
        }
    ]


def test_node_cli_control_action_prints_timings(monkeypatch) -> None:
    echoed: list[str] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            200,
            {
                "ok": True,
                "accepted": True,
                "webspace_id": "phase2-home",
                "scenario_id": "prompt_engineer_scenario",
                "scenario_switch_mode": "pointer_first",
                "switch_skipped": True,
                "skip_reason": "already_pending_rebuild",
                "background_rebuild": True,
                "timings_ms": {"load_scenario": 1.25, "total": 4.5},
                "rebuild_timings_ms": {"projection_refresh": 2.0, "total": 6.0},
                "phase_timings_ms": {"time_to_accept": 4.5, "time_to_full_hydration": 10.5},
                "apply_summary": {
                    "branch_count": 6,
                    "changed_branches": 2,
                    "unchanged_branches": 4,
                    "failed_branches": 0,
                    "changed_paths": ["ui.application", "registry.merged"],
                },
                "rebuild": {
                    "status": "running",
                    "pending": True,
                    "background": True,
                    "action": "scenario_switch_rebuild",
                    "scenario_id": "prompt_engineer_scenario",
                    "resolver": {
                        "source": "loader:workspace",
                        "legacy_fallback": False,
                        "cache_hit": True,
                    },
                    "apply_summary": {
                        "branch_count": 6,
                        "changed_branches": 1,
                        "unchanged_branches": 5,
                        "failed_branches": 0,
                        "changed_paths": ["ui.application"],
                    },
                },
            },
        ),
    )
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_control_action(
        action="scenario",
        webspace="phase2-home",
        scenario_id="prompt_engineer_scenario",
        set_home=None,
        control="http://127.0.0.1:8080",
        json_output=False,
    )

    assert any("switch: mode=pointer_first skipped=yes background=yes reason=already_pending_rebuild" in line for line in echoed)
    assert any("rebuild: status=running pending=yes background=yes action=scenario_switch_rebuild scenario=prompt_engineer_scenario" in line for line in echoed)
    assert any("timings_ms: load_scenario=1.250 total=4.500" in line for line in echoed)
    assert any("rebuild_timings_ms: projection_refresh=2.000 total=6.000" in line for line in echoed)
    assert any("phase_timings_ms: time_to_accept=4.500 time_to_full_hydration=10.500" in line for line in echoed)
    assert any("apply: changed=2/6 unchanged=4 failed=0 paths=ui.application,registry.merged" in line for line in echoed)


def test_node_cli_rebuild_summary_prints_resolver_debug(monkeypatch) -> None:
    echoed: list[str] = []
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._print_rebuild_summary(
        {
            "rebuild": {
                "status": "ready",
                "pending": False,
                "background": True,
                "action": "scenario_switch_rebuild",
                "scenario_id": "prompt_engineer_scenario",
                "resolver": {
                    "source": "legacy_yjs",
                    "legacy_fallback": True,
                    "cache_hit": False,
                },
                "apply_summary": {
                    "branch_count": 6,
                    "changed_branches": 0,
                    "unchanged_branches": 6,
                    "failed_branches": 0,
                    "changed_paths": [],
                },
            }
        }
    )

    assert any("resolver: source=legacy_yjs legacy_fallback=yes cache_hit=no" in line for line in echoed)
    assert any("apply: changed=0/6 unchanged=6 failed=0" in line for line in echoed)


def test_node_cli_apply_summary_prints_phase_breakdown(monkeypatch) -> None:
    echoed: list[str] = []
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._print_apply_summary(
        {
            "apply_summary": {
                "branch_count": 6,
                "changed_branches": 2,
                "unchanged_branches": 4,
                "failed_branches": 0,
                "diff_applied_branches": 1,
                "replaced_branches": 1,
                "changed_paths": ["ui.application", "registry.merged"],
                "phases": {
                    "structure": {
                        "branch_count": 2,
                        "changed_branches": 2,
                        "unchanged_branches": 0,
                        "failed_branches": 0,
                        "diff_applied_branches": 1,
                        "replaced_branches": 1,
                        "changed_paths": ["ui.application", "registry.merged"],
                    },
                    "interactive": {
                        "branch_count": 4,
                        "changed_branches": 0,
                        "unchanged_branches": 4,
                        "failed_branches": 0,
                        "changed_paths": [],
                    },
                },
            }
        }
    )

    assert any("apply: changed=2/6 unchanged=4 failed=0 diff=1 replace=1 paths=ui.application,registry.merged" in line for line in echoed)
    assert any("apply.phase.structure: changed=2/2 unchanged=0 failed=0 diff=1 replace=1 paths=ui.application,registry.merged" in line for line in echoed)
    assert any("apply.phase.interactive: changed=0/4 unchanged=4 failed=0" in line for line in echoed)


def test_node_cli_apply_summary_prints_fingerprint_skip_breakdown(monkeypatch) -> None:
    echoed: list[str] = []
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._print_apply_summary(
        {
            "apply_summary": {
                "branch_count": 6,
                "changed_branches": 0,
                "unchanged_branches": 6,
                "failed_branches": 0,
                "fingerprint_unchanged_branches": 6,
                "phases": {
                    "structure": {
                        "branch_count": 2,
                        "changed_branches": 0,
                        "unchanged_branches": 2,
                        "failed_branches": 0,
                        "fingerprint_unchanged_branches": 2,
                    },
                    "interactive": {
                        "branch_count": 4,
                        "changed_branches": 0,
                        "unchanged_branches": 4,
                        "failed_branches": 0,
                        "fingerprint_unchanged_branches": 4,
                    },
                },
            }
        }
    )

    assert any("apply: changed=0/6 unchanged=6 failed=0 fingerprint_skip=6" in line for line in echoed)
    assert any("apply.phase.structure: changed=0/2 unchanged=2 failed=0 fingerprint_skip=2" in line for line in echoed)
    assert any("apply.phase.interactive: changed=0/4 unchanged=4 failed=0 fingerprint_skip=4" in line for line in echoed)


def test_node_cli_benchmark_scenario_restores_baseline_and_prints_summary(monkeypatch) -> None:
    echoed: list[str] = []
    posted: list[str] = []
    polled_paths: list[str] = []
    perf_values = iter(
        [
            0.000,
            0.010,
            0.010,
            0.060,
            1.000,
            1.008,
            1.008,
            1.028,
            2.000,
            2.012,
            2.012,
            2.066,
            3.000,
            3.007,
            3.007,
            3.025,
        ]
    )
    target_posts = [
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "scenario_id": "infrascope",
            "scenario_switch_mode": "pointer_only",
            "switch_skipped": False,
            "phase_timings_ms": {
                "time_to_accept": 10.0,
                "time_to_pointer_update": 4.0,
            },
            "timings_ms": {"write_switch_pointer": 4.0, "total": 10.0},
            "rebuild": {"request_id": "req-target-1", "status": "scheduled", "pending": True, "scenario_id": "infrascope"},
        },
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "scenario_id": "infrascope",
            "scenario_switch_mode": "pointer_only",
            "switch_skipped": False,
            "phase_timings_ms": {
                "time_to_accept": 12.0,
                "time_to_pointer_update": 5.0,
            },
            "timings_ms": {"write_switch_pointer": 5.0, "total": 12.0},
            "rebuild": {"request_id": "req-target-2", "status": "scheduled", "pending": True, "scenario_id": "infrascope"},
        },
    ]
    rebuild_payloads = [
        {
            "ok": True,
            "accepted": True,
            "webspace": {
                "webspace_id": "default",
                "home_scenario": "web_desktop",
                "current_scenario": "infrascope",
            },
            "rebuild": {
                "request_id": "req-target-1",
                "status": "ready",
                "pending": False,
                "background": True,
                "scenario_id": "infrascope",
                "resolver": {"source": "loader:workspace", "cache_hit": False},
                "apply_summary": {"changed_branches": 2, "unchanged_branches": 4},
                "switch_timings_ms": {"write_switch_pointer": 4.0, "total": 10.0},
                "timings_ms": {"resolve_rebuild_target": 6.0, "semantic_rebuild": 50.0, "total": 56.0},
                "semantic_rebuild_timings_ms": {
                    "collect_inputs": 5.0,
                    "resolve": 10.0,
                    "apply_structure": 15.0,
                    "apply_interactive": 10.0,
                    "total": 50.0,
                },
                "ydoc_timings_ms": {
                    "ystore_start": 3.0,
                    "ystore_apply_updates": 7.0,
                    "in_doc_rebuild": 50.0,
                    "ystore_stop": 1.0,
                    "total": 61.0,
                },
                "phase_timings_ms": {
                    "time_to_accept": 10.0,
                    "time_to_pointer_update": 4.0,
                    "time_to_first_structure": 30.0,
                    "time_to_interactive_focus": 40.0,
                    "time_to_full_hydration": 60.0,
                },
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace": {
                "webspace_id": "default",
                "home_scenario": "web_desktop",
                "current_scenario": "web_desktop",
            },
            "rebuild": {
                "request_id": "req-base-1",
                "status": "ready",
                "pending": False,
                "background": True,
                "scenario_id": "web_desktop",
                "switch_timings_ms": {"write_switch_pointer": 3.0, "total": 8.0},
                "timings_ms": {"resolve_rebuild_target": 4.0, "semantic_rebuild": 16.0, "total": 20.0},
                "semantic_rebuild_timings_ms": {"collect_inputs": 2.0, "resolve": 5.0, "apply_structure": 4.0, "total": 16.0},
                "ydoc_timings_ms": {
                    "ystore_start": 2.0,
                    "ystore_apply_updates": 4.0,
                    "in_doc_rebuild": 16.0,
                    "ystore_stop": 1.0,
                    "total": 23.0,
                },
                "phase_timings_ms": {
                    "time_to_accept": 8.0,
                    "time_to_pointer_update": 3.0,
                    "time_to_first_structure": 12.0,
                    "time_to_interactive_focus": 18.0,
                    "time_to_full_hydration": 28.0,
                },
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace": {
                "webspace_id": "default",
                "home_scenario": "web_desktop",
                "current_scenario": "infrascope",
            },
            "rebuild": {
                "request_id": "req-target-2",
                "status": "ready",
                "pending": False,
                "background": True,
                "scenario_id": "infrascope",
                "resolver": {"source": "loader:workspace", "cache_hit": True},
                "apply_summary": {"changed_branches": 1, "unchanged_branches": 5},
                "switch_timings_ms": {"write_switch_pointer": 5.0, "total": 12.0},
                "timings_ms": {"resolve_rebuild_target": 7.0, "semantic_rebuild": 42.0, "total": 49.0},
                "semantic_rebuild_timings_ms": {
                    "collect_inputs": 4.0,
                    "resolve": 8.0,
                    "apply_structure": 12.0,
                    "apply_interactive": 12.0,
                    "total": 42.0,
                },
                "ydoc_timings_ms": {
                    "ystore_start": 4.0,
                    "ystore_apply_updates": 6.0,
                    "in_doc_rebuild": 42.0,
                    "ystore_stop": 1.0,
                    "total": 53.0,
                },
                "phase_timings_ms": {
                    "time_to_accept": 12.0,
                    "time_to_pointer_update": 5.0,
                    "time_to_first_structure": 24.0,
                    "time_to_interactive_focus": 36.0,
                    "time_to_full_hydration": 54.0,
                },
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace": {
                "webspace_id": "default",
                "home_scenario": "web_desktop",
                "current_scenario": "web_desktop",
            },
            "rebuild": {
                "request_id": "req-base-2",
                "status": "ready",
                "pending": False,
                "background": True,
                "scenario_id": "web_desktop",
                "switch_timings_ms": {"write_switch_pointer": 2.0, "total": 7.0},
                "timings_ms": {"resolve_rebuild_target": 3.0, "semantic_rebuild": 15.0, "total": 18.0},
                "semantic_rebuild_timings_ms": {"collect_inputs": 2.0, "resolve": 4.0, "apply_structure": 4.0, "total": 15.0},
                "ydoc_timings_ms": {
                    "ystore_start": 1.0,
                    "ystore_apply_updates": 3.0,
                    "in_doc_rebuild": 15.0,
                    "ystore_stop": 1.0,
                    "total": 20.0,
                },
                "phase_timings_ms": {
                    "time_to_accept": 7.0,
                    "time_to_pointer_update": 2.0,
                    "time_to_first_structure": 10.0,
                    "time_to_interactive_focus": 15.0,
                    "time_to_full_hydration": 25.0,
                },
            },
        },
    ]
    materialization_payloads = [
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "materialization": {
                "ready": False,
                "webspace_id": "default",
                "current_scenario": "infrascope",
                "readiness_state": "interactive",
                "missing_branches": ["data.desktop"],
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "materialization": {
                "ready": True,
                "webspace_id": "default",
                "current_scenario": "web_desktop",
                "readiness_state": "ready",
                "missing_branches": [],
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "materialization": {
                "ready": False,
                "webspace_id": "default",
                "current_scenario": "infrascope",
                "readiness_state": "hydrating",
                "missing_branches": ["data.routing"],
            },
        },
        {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "materialization": {
                "ready": True,
                "webspace_id": "default",
                "current_scenario": "web_desktop",
                "readiness_state": "ready",
                "missing_branches": [],
            },
        },
    ]
    describe_calls = {"value": 0}

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    def _fake_get_json(**kwargs):
        path = str(kwargs.get("path") or "")
        polled_paths.append(path)
        if path == "/api/node/yjs/webspaces/default":
            describe_calls["value"] += 1
            if describe_calls["value"] == 1:
                return (
                    200,
                    {
                        "ok": True,
                        "accepted": True,
                        "webspace": {
                            "webspace_id": "default",
                            "home_scenario": "web_desktop",
                            "current_scenario": "web_desktop",
                        },
                    },
                )
            raise AssertionError(f"unexpected describe poll: {path}")
        if path == "/api/node/yjs/webspaces/default/rebuild?include_runtime=0":
            return 200, rebuild_payloads.pop(0)
        if path == "/api/node/yjs/webspaces/default/materialization?include_runtime=0":
            return 200, materialization_payloads.pop(0)
        raise AssertionError(f"unexpected poll path: {path}")

    monkeypatch.setattr(node_cli_module, "_control_get_json", _fake_get_json)

    def _fake_post_json(**kwargs):
        body = dict(kwargs.get("body") or {})
        scenario = str(body.get("scenario_id") or "")
        posted.append(scenario)
        if scenario == "infrascope":
            payload = target_posts[len([item for item in posted if item == "infrascope"]) - 1]
            return 200, payload
        return 200, {
            "ok": True,
            "accepted": True,
            "webspace_id": "default",
            "scenario_id": scenario,
            "scenario_switch_mode": "pointer_only",
            "phase_timings_ms": {"time_to_accept": 8.0 if len([item for item in posted if item == "web_desktop"]) == 1 else 7.0},
            "rebuild": {
                "request_id": "req-base-1" if len([item for item in posted if item == "web_desktop"]) == 1 else "req-base-2",
                "status": "scheduled",
                "pending": True,
                "scenario_id": scenario,
            },
        }

    monkeypatch.setattr(node_cli_module, "_control_post_json", _fake_post_json)
    monkeypatch.setattr(node_cli_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(node_cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_benchmark_scenario_action(
        webspace="default",
        scenario_id="infrascope",
        baseline_scenario=None,
        iterations=2,
        wait_ready=True,
        ready_timeout_sec=30.0,
        poll_interval_sec=0.01,
        detail=True,
        control="http://127.0.0.1:8080",
        json_output=False,
    )

    assert posted == ["infrascope", "web_desktop", "infrascope", "web_desktop"]
    assert polled_paths[0] == "/api/node/yjs/webspaces/default"
    assert polled_paths[1:] == [
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
    ]
    assert any("yjs benchmark-scenario: webspace=default scenario=infrascope baseline=web_desktop iterations=2" in line for line in echoed)
    assert any(
        "run=1 mode=pointer_only skipped=no cache_hit=no changed=2 fp_skip=0 accept=10.000 ready=60.000 lag=0.000 first=30.000 interactive=40.000 full=60.000 polls=rebuild:1/materialization:1 status=ready"
        in line
        for line in echoed
    )
    assert any(
        "run=2 mode=pointer_only skipped=no cache_hit=yes changed=1 fp_skip=0 accept=12.000 ready=66.000 lag=12.000 first=24.000 interactive=36.000 full=54.000 polls=rebuild:1/materialization:1 status=ready"
        in line
        for line in echoed
    )
    assert any("summary.time_to_accept: avg=11.000 min=10.000 max=12.000" in line for line in echoed)
    assert any("summary.time_to_pointer_update: avg=4.500 min=4.000 max=5.000" in line for line in echoed)
    assert any("summary.time_to_full_hydration: avg=57.000 min=54.000 max=60.000" in line for line in echoed)
    assert any("summary.observed.time_to_accept: avg=11.000 min=10.000 max=12.000" in line for line in echoed)
    assert any("summary.observed.time_to_first_paint: avg=52.000 min=50.000 max=54.000" in line for line in echoed)
    assert any("summary.observed.time_to_interactive: avg=52.000 min=50.000 max=54.000" in line for line in echoed)
    assert any("summary.observed.time_to_ready: avg=63.000 min=60.000 max=66.000" in line for line in echoed)
    assert any("summary.ready_server: avg=57.000 min=54.000 max=60.000" in line for line in echoed)
    assert any("summary.ready_observation_lag: avg=6.000 min=0.000 max=12.000" in line for line in echoed)
    assert any("summary.rebuild_status: ready=2" in line for line in echoed)
    assert any("summary.polls.rebuild: avg=1.000 min=1.000 max=1.000" in line for line in echoed)
    assert any("summary.polls.materialization: avg=1.000 min=1.000 max=1.000" in line for line in echoed)
    assert any("summary.flags: skipped=0/2 cache_hits=1/2 ready_timeouts=0/2" in line for line in echoed)
    assert any("summary.switch_timings_ms:" in line for line in echoed)
    assert any("summary.rebuild_timings_ms:" in line for line in echoed)
    assert any("summary.semantic_rebuild_timings_ms:" in line for line in echoed)
    assert any("summary.ydoc_timings_ms:" in line for line in echoed)
    assert any("summary.ready_alignment_ms:" in line for line in echoed)
    assert any("  ydoc_timings_ms:" in line for line in echoed)


def test_node_cli_benchmark_scenario_tolerates_transient_rebuild_poll_timeout(monkeypatch) -> None:
    echoed: list[str] = []
    posted: list[str] = []
    polled_paths: list[str] = []
    perf_values = iter([0.000, 0.010, 0.010, 0.030, 0.070])
    describe_calls = {"value": 0}
    rebuild_calls = {"value": 0}

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )

    def _fake_get_json(**kwargs):
        path = str(kwargs.get("path") or "")
        polled_paths.append(path)
        if path == "/api/node/yjs/webspaces/default":
            describe_calls["value"] += 1
            assert describe_calls["value"] == 1
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace": {
                        "webspace_id": "default",
                        "home_scenario": "web_desktop",
                        "current_scenario": "web_desktop",
                    },
                },
            )
        if path == "/api/node/yjs/webspaces/default/rebuild?include_runtime=0":
            rebuild_calls["value"] += 1
            if rebuild_calls["value"] == 1:
                return None, {"error": "timeout", "detail": "read timed out"}
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "rebuild": {
                        "request_id": "req-target-1",
                        "status": "ready",
                        "pending": False,
                        "background": True,
                        "scenario_id": "infrascope",
                        "resolver": {"source": "loader:workspace", "cache_hit": False},
                        "apply_summary": {"changed_branches": 2, "unchanged_branches": 4},
                        "switch_timings_ms": {"write_switch_pointer": 4.0, "total": 10.0},
                        "timings_ms": {"resolve_rebuild_target": 8.0, "semantic_rebuild": 52.0, "total": 60.0},
                        "semantic_rebuild_timings_ms": {
                            "collect_inputs": 5.0,
                            "resolve": 10.0,
                            "apply_structure": 15.0,
                            "apply_interactive": 12.0,
                            "total": 52.0,
                        },
                        "phase_timings_ms": {
                            "time_to_accept": 10.0,
                            "time_to_pointer_update": 4.0,
                            "time_to_first_structure": 33.0,
                            "time_to_interactive_focus": 45.0,
                            "time_to_full_hydration": 70.0,
                        },
                    },
                },
            )
        if path == "/api/node/yjs/webspaces/default/materialization?include_runtime=0":
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "desktop",
                    "materialization": {
                        "ready": True,
                        "webspace_id": "default",
                        "current_scenario": "infrascope",
                        "readiness_state": "ready",
                        "missing_branches": [],
                    },
                },
            )
        raise AssertionError(f"unexpected poll path: {path}")

    monkeypatch.setattr(node_cli_module, "_control_get_json", _fake_get_json)
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            posted.append(str((kwargs.get("body") or {}).get("scenario_id") or ""))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "desktop",
                    "scenario_id": "infrascope",
                    "scenario_switch_mode": "pointer_only",
                    "switch_skipped": False,
                    "phase_timings_ms": {
                        "time_to_accept": 10.0,
                        "time_to_pointer_update": 4.0,
                    },
                    "timings_ms": {"write_switch_pointer": 4.0, "total": 10.0},
                    "rebuild": {
                        "request_id": "req-target-1",
                        "status": "scheduled",
                        "pending": True,
                        "scenario_id": "infrascope",
                    },
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(node_cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_benchmark_scenario_action(
        webspace="default",
        scenario_id="infrascope",
        baseline_scenario="infrascope",
        iterations=1,
        wait_ready=True,
        ready_timeout_sec=30.0,
        poll_interval_sec=0.01,
        detail=False,
        control="http://127.0.0.1:8080",
        json_output=False,
    )

    assert posted == ["infrascope"]
    assert polled_paths == [
        "/api/node/yjs/webspaces/default",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
    ]
    assert any(
        "run=1 mode=pointer_only skipped=no cache_hit=no changed=2 fp_skip=0 accept=10.000 ready=60.000 lag=0.000 first=33.000 interactive=45.000 full=70.000 polls=rebuild:2/materialization:1 status=ready"
        in line
        for line in echoed
    )
    assert any("summary.polls.rebuild_transient_failures: avg=1.000 min=1.000 max=1.000" in line for line in echoed)
    assert any("summary.flags: skipped=0/1 cache_hits=0/1 ready_timeouts=0/1" in line for line in echoed)


def test_node_cli_benchmark_scenario_falls_back_from_lightweight_poll_endpoints(monkeypatch) -> None:
    echoed: list[str] = []
    posted: list[str] = []
    polled_paths: list[str] = []
    perf_values = iter([0.000, 0.008, 0.008, 0.040])
    describe_calls = {"value": 0}

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )

    def _fallback_describe_payload() -> tuple[int, dict[str, object]]:
        return (
            200,
            {
                "ok": True,
                "accepted": True,
                "webspace": {
                    "webspace_id": "default",
                    "home_scenario": "web_desktop",
                    "current_scenario": "infrascope",
                },
                "rebuild": {
                    "request_id": "req-target-1",
                    "status": "ready",
                    "pending": False,
                    "background": True,
                    "scenario_id": "infrascope",
                    "resolver": {"source": "loader:workspace", "cache_hit": False},
                    "apply_summary": {"changed_branches": 1, "unchanged_branches": 5},
                    "switch_timings_ms": {"write_switch_pointer": 3.0, "total": 8.0},
                    "timings_ms": {"resolve_rebuild_target": 7.0, "semantic_rebuild": 40.0, "total": 47.0},
                    "semantic_rebuild_timings_ms": {
                        "collect_inputs": 4.0,
                        "resolve": 8.0,
                        "apply_structure": 11.0,
                        "apply_interactive": 10.0,
                        "total": 40.0,
                    },
                    "phase_timings_ms": {
                        "time_to_accept": 8.0,
                        "time_to_pointer_update": 3.0,
                        "time_to_first_structure": 26.0,
                        "time_to_interactive_focus": 36.0,
                        "time_to_full_hydration": 55.0,
                    },
                },
                "materialization": {
                    "ready": True,
                    "webspace_id": "default",
                    "current_scenario": "infrascope",
                    "readiness_state": "ready",
                    "missing_branches": [],
                },
            },
        )

    def _fake_get_json(**kwargs):
        path = str(kwargs.get("path") or "")
        polled_paths.append(path)
        if path == "/api/node/yjs/webspaces/default":
            describe_calls["value"] += 1
            if describe_calls["value"] == 1:
                return (
                    200,
                    {
                        "ok": True,
                        "accepted": True,
                        "webspace": {
                            "webspace_id": "default",
                            "home_scenario": "web_desktop",
                            "current_scenario": "web_desktop",
                        },
                    },
                )
            return _fallback_describe_payload()
        if path == "/api/node/yjs/webspaces/default/rebuild?include_runtime=0":
            return 404, {"detail": "not found"}
        if path == "/api/node/yjs/webspaces/default/materialization?include_runtime=0":
            return 404, {"detail": "not found"}
        raise AssertionError(f"unexpected poll path: {path}")

    monkeypatch.setattr(node_cli_module, "_control_get_json", _fake_get_json)
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            posted.append(str((kwargs.get("body") or {}).get("scenario_id") or ""))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "default",
                    "scenario_id": "infrascope",
                    "scenario_switch_mode": "pointer_only",
                    "switch_skipped": False,
                    "phase_timings_ms": {
                        "time_to_accept": 8.0,
                        "time_to_pointer_update": 3.0,
                    },
                    "timings_ms": {"write_switch_pointer": 3.0, "total": 8.0},
                    "rebuild": {
                        "request_id": "req-target-1",
                        "status": "scheduled",
                        "pending": True,
                        "scenario_id": "infrascope",
                    },
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(node_cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_benchmark_scenario_action(
        webspace="default",
        scenario_id="infrascope",
        baseline_scenario="infrascope",
        iterations=1,
        wait_ready=True,
        ready_timeout_sec=30.0,
        poll_interval_sec=0.01,
        detail=False,
        control="http://127.0.0.1:8080",
        json_output=False,
    )

    assert posted == ["infrascope"]
    assert polled_paths == [
        "/api/node/yjs/webspaces/default",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
        "/api/node/yjs/webspaces/default",
        "/api/node/yjs/webspaces/default/materialization?include_runtime=0",
        "/api/node/yjs/webspaces/default",
    ]
    assert any(
        "run=1 mode=pointer_only skipped=no cache_hit=no changed=1 fp_skip=0 accept=8.000 ready=32.000 lag=0.000 first=26.000 interactive=36.000 full=55.000 polls=rebuild:1/materialization:1 status=ready"
        in line
        for line in echoed
    )
    assert any("summary.polls.rebuild_describe_fallback: avg=1.000 min=1.000 max=1.000" in line for line in echoed)
    assert any("summary.polls.materialization_describe_fallback: avg=1.000 min=1.000 max=1.000" in line for line in echoed)


def test_node_cli_benchmark_scenario_uses_embedded_rebuild_materialization(monkeypatch) -> None:
    echoed: list[str] = []
    posted: list[str] = []
    polled_paths: list[str] = []
    perf_values = iter([0.000, 0.006, 0.006, 0.020])

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )

    def _fake_get_json(**kwargs):
        path = str(kwargs.get("path") or "")
        polled_paths.append(path)
        if path == "/api/node/yjs/webspaces/default":
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace": {
                        "webspace_id": "default",
                        "home_scenario": "web_desktop",
                        "current_scenario": "web_desktop",
                    },
                },
            )
        if path == "/api/node/yjs/webspaces/default/rebuild?include_runtime=0":
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "rebuild": {
                        "request_id": "req-target-embedded",
                        "status": "ready",
                        "pending": False,
                        "background": True,
                        "scenario_id": "infrascope",
                        "resolver": {"source": "loader:workspace", "cache_hit": False},
                        "apply_summary": {"changed_branches": 1, "unchanged_branches": 5},
                        "switch_timings_ms": {"write_switch_pointer": 2.0, "total": 6.0},
                        "timings_ms": {"resolve_rebuild_target": 4.0, "semantic_rebuild": 20.0, "total": 24.0},
                        "semantic_rebuild_timings_ms": {
                            "collect_inputs": 2.0,
                            "resolve": 4.0,
                            "apply_structure": 5.0,
                            "apply_interactive": 5.0,
                            "total": 20.0,
                        },
                        "phase_timings_ms": {
                            "time_to_accept": 6.0,
                            "time_to_pointer_update": 2.0,
                            "time_to_first_structure": 11.0,
                            "time_to_interactive_focus": 16.0,
                            "time_to_full_hydration": 26.0,
                        },
                        "materialization": {
                            "ready": True,
                            "webspace_id": "default",
                            "current_scenario": "infrascope",
                            "readiness_state": "ready",
                            "missing_branches": [],
                            "snapshot_source": "semantic_rebuild:interactive",
                            "observed_at": 123.0,
                            "stale": False,
                        },
                    },
                },
            )
        if path == "/api/node/yjs/webspaces/default/materialization?include_runtime=0":
            raise AssertionError("embedded rebuild materialization should avoid separate materialization poll")
        raise AssertionError(f"unexpected poll path: {path}")

    monkeypatch.setattr(node_cli_module, "_control_get_json", _fake_get_json)
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            posted.append(str((kwargs.get("body") or {}).get("scenario_id") or ""))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "default",
                    "scenario_id": "infrascope",
                    "scenario_switch_mode": "pointer_only",
                    "switch_skipped": False,
                    "phase_timings_ms": {
                        "time_to_accept": 6.0,
                        "time_to_pointer_update": 2.0,
                    },
                    "timings_ms": {"write_switch_pointer": 2.0, "total": 6.0},
                    "rebuild": {
                        "request_id": "req-target-embedded",
                        "status": "scheduled",
                        "pending": True,
                        "scenario_id": "infrascope",
                    },
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(node_cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_benchmark_scenario_action(
        webspace="default",
        scenario_id="infrascope",
        baseline_scenario="infrascope",
        iterations=1,
        wait_ready=True,
        ready_timeout_sec=30.0,
        poll_interval_sec=0.01,
        detail=False,
        control="http://127.0.0.1:8080",
        json_output=False,
    )

    assert posted == ["infrascope"]
    assert polled_paths == [
        "/api/node/yjs/webspaces/default",
        "/api/node/yjs/webspaces/default/rebuild?include_runtime=0",
    ]
    assert any(
        "run=1 mode=pointer_only skipped=no cache_hit=no changed=1 fp_skip=0 accept=6.000 ready=20.000 lag=0.000 first=11.000 interactive=16.000 full=26.000 polls=rebuild:1/materialization:0 status=ready"
        in line
        for line in echoed
    )


def test_benchmark_ready_alignment_falls_back_to_server_timestamps() -> None:
    metrics, source = node_cli_module._benchmark_ready_alignment(
        {
            "rebuild": {
                "status": "ready",
                "finished_at": 100.025,
            },
            "materialization": {
                "ready": True,
                "observed_at": 100.030,
            },
            "observed_timings_ms": {
                "time_to_ready": 40.0,
            },
        },
        request_started_at=100.0,
    )

    assert metrics == {
        "server_ready": 25.0,
        "observation_lag": 15.0,
    }
    assert source == "rebuild_finished_at"


def test_node_cli_benchmark_scenario_falls_back_to_active_runtime_from_supervisor(monkeypatch) -> None:
    echoed: list[str] = []
    posted: list[tuple[str, str]] = []
    perf_values = iter([0.000, 0.006, 0.006, 0.024])

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8777",
            resolve_control_token=lambda explicit=None, base_url=None: explicit or "secret",
        ),
    )

    def _fake_get_json(**kwargs):
        control = str(kwargs.get("control") or "")
        path = str(kwargs.get("path") or "")
        if control == "http://127.0.0.1:8776" and path == "/api/supervisor/public/update-status":
            return (
                200,
                {
                    "ok": True,
                    "runtime": {
                        "runtime_url": "http://127.0.0.1:8778",
                        "candidate_runtime_url": "http://127.0.0.1:8777",
                        "slot_urls": {
                            "A": "http://127.0.0.1:8777",
                            "B": "http://127.0.0.1:8778",
                        },
                    },
                },
            )
        if control == "http://127.0.0.1:8777" and path == "/api/node/yjs/webspaces/default":
            return None, {"error": "timeout", "detail": "read timeout"}
        if control == "http://127.0.0.1:8778" and path == "/api/node/yjs/webspaces/default":
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace": {
                        "webspace_id": "default",
                        "home_scenario": "web_desktop",
                        "current_scenario": "web_desktop",
                    },
                    "runtime": {
                        "transition_role": "active",
                        "admin_mutation_allowed": True,
                    },
                },
            )
        if control == "http://127.0.0.1:8778" and path == "/api/node/yjs/webspaces/default/rebuild?include_runtime=0":
            return (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "rebuild": {
                        "request_id": "req-fallback-1",
                        "status": "ready",
                        "pending": False,
                        "background": True,
                        "scenario_id": "infrascope",
                        "resolver": {"source": "loader:workspace", "cache_hit": False},
                        "apply_summary": {
                            "changed_branches": 1,
                            "unchanged_branches": 5,
                            "fingerprint_unchanged_branches": 2,
                            "diff_applied_branches": 1,
                            "replaced_branches": 0,
                        },
                        "switch_timings_ms": {"write_switch_pointer": 2.0, "total": 6.0},
                        "timings_ms": {"resolve_rebuild_target": 4.0, "semantic_rebuild": 18.0, "total": 22.0},
                        "semantic_rebuild_timings_ms": {
                            "collect_inputs": 2.0,
                            "resolve": 4.0,
                            "apply_structure": 4.0,
                            "apply_interactive": 4.0,
                            "total": 18.0,
                        },
                        "phase_timings_ms": {
                            "time_to_accept": 6.0,
                            "time_to_pointer_update": 2.0,
                            "time_to_first_structure": 10.0,
                            "time_to_interactive_focus": 14.0,
                            "time_to_full_hydration": 24.0,
                        },
                        "materialization": {
                            "ready": True,
                            "webspace_id": "default",
                            "current_scenario": "infrascope",
                            "readiness_state": "ready",
                            "missing_branches": [],
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected request: control={control} path={path}")

    monkeypatch.setattr(node_cli_module, "_control_get_json", _fake_get_json)
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            posted.append((str(kwargs.get("control") or ""), str((kwargs.get("body") or {}).get("scenario_id") or "")))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "default",
                    "scenario_id": "infrascope",
                    "scenario_switch_mode": "pointer_only",
                    "switch_skipped": False,
                    "phase_timings_ms": {
                        "time_to_accept": 6.0,
                        "time_to_pointer_update": 2.0,
                    },
                    "timings_ms": {"write_switch_pointer": 2.0, "total": 6.0},
                    "rebuild": {
                        "request_id": "req-fallback-1",
                        "status": "scheduled",
                        "pending": True,
                        "scenario_id": "infrascope",
                    },
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(node_cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(node_cli_module.typer, "echo", lambda message="": echoed.append(str(message)))

    node_cli_module._node_yjs_benchmark_scenario_action(
        webspace="default",
        scenario_id="infrascope",
        baseline_scenario="infrascope",
        iterations=1,
        wait_ready=True,
        ready_timeout_sec=30.0,
        poll_interval_sec=0.01,
        detail=True,
        control="http://127.0.0.1:8777",
        json_output=False,
    )

    assert posted == [("http://127.0.0.1:8778", "infrascope")]
    assert any(
        "benchmark.control: requested=http://127.0.0.1:8777 selected=http://127.0.0.1:8778 reason=supervisor_runtime_fallback"
        in line
        for line in echoed
    )
    assert any(
        "run=1 mode=pointer_only skipped=no cache_hit=no changed=1 fp_skip=2 diff=1 replace=0 accept=6.000 ready=24.000 lag=0.000 first=10.000 interactive=14.000 full=24.000 polls=rebuild:1/materialization:0 status=ready"
        in line
        for line in echoed
    )
    assert any("summary.fingerprint_unchanged_branches: avg=2.000 min=2.000 max=2.000" in line for line in echoed)
    assert any("summary.diff_applied_branches: avg=1.000 min=1.000 max=1.000" in line for line in echoed)


def test_webspace_runtime_apply_uses_effective_branch_fingerprints_fast_path(monkeypatch) -> None:
    class _Txn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _TrackingMap(dict):
        def __init__(self, initial: dict | None = None, *, forbidden_get_keys: set[str] | None = None) -> None:
            super().__init__(initial or {})
            self.forbidden_get_keys = set(forbidden_get_keys or set())

        def get(self, key, default=None):
            if key in self.forbidden_get_keys:
                raise AssertionError(f"unexpected get for {key}")
            return dict.get(self, key, default)

        def set(self, txn, key, value):
            self[key] = value

    class _TrackingDoc:
        def __init__(self, state: dict[str, _TrackingMap]) -> None:
            self._state = state

        def get_map(self, name: str) -> _TrackingMap:
            return self._state[name]

        def begin_transaction(self):
            return _Txn()

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(ctx=SimpleNamespace())
    monkeypatch.setattr(runtime, "_apply_ydoc_defaults_in_txn", lambda ydoc, txn, skill_decls: None)
    monkeypatch.setattr(
        webspace_runtime_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {"webspace_id": webspace_id, "status": "ready", "pending": False},
    )

    resolved = webspace_runtime_module.WebspaceResolverOutputs(
        webspace_id="default",
        scenario_id="infrascope",
        source_mode="workspace",
        application={
            "desktop": {"topbar": [], "pageSchema": {"widgets": []}, "pinnedWidgets": []},
            "modals": {"apps_catalog": {}, "widgets_catalog": {}},
        },
        catalog={"apps": [], "widgets": []},
        registry={"modals": [], "widgets": []},
        installed={"apps": [], "widgets": []},
        desktop={"installed": {"apps": [], "widgets": []}, "topbar": [], "pageSchema": {"widgets": []}, "pinnedWidgets": []},
        routing={"routes": {}},
        skill_decls=[],
    )
    fingerprints = webspace_runtime_module._resolved_output_branch_fingerprints(resolved)
    ydoc = _TrackingDoc(
        {
            "ui": _TrackingMap(
                {"application": resolved.application},
            ),
            "data": _TrackingMap(
                {
                    "catalog": resolved.catalog,
                    "installed": resolved.installed,
                    "desktop": resolved.desktop,
                    "webio": resolved.webio,
                    "routing": resolved.routing,
                },
            ),
            "registry": _TrackingMap(
                {
                    "merged": resolved.registry,
                    "runtime_meta": {
                        webspace_runtime_module._RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY: dict(fingerprints)
                    },
                },
            ),
            "runtime": _TrackingMap({"environment": webspace_runtime_module.runtime_environment_payload()}),
        }
    )
    inputs = webspace_runtime_module.WebspaceResolverInputs(
        webspace_id="default",
        scenario_id="infrascope",
        source_mode="workspace",
        metadata={},
        scenario_application={},
        scenario_catalog={},
        scenario_registry={},
        overlay_snapshot={},
        live_state={"desktop": {}, "routing": {}},
        compatibility_cache_presence={
            "scenario_ui_application": False,
            "scenario_registry_entry": False,
            "scenario_catalog": False,
        },
        skill_decls=[],
        desktop_scenarios=[],
        scenario_source="loader:workspace",
        legacy_scenario_fallback=False,
    )

    runtime._apply_resolved_state_in_doc(ydoc, "default", resolved, inputs=inputs)

    assert runtime._last_apply_summary == {
        "branch_count": 8,
        "changed_branches": 0,
        "unchanged_branches": 8,
        "failed_branches": 0,
        "changed_paths": [],
        "defaults_failed": False,
        "transaction_total": 2,
        "phases": {
            "structure": {
                "branch_count": 3,
                "changed_branches": 0,
                "unchanged_branches": 3,
                "failed_branches": 0,
                "changed_paths": [],
                "fingerprint_unchanged_branches": 3,
                "fingerprint_unchanged_paths": ["ui.application", "registry.merged", "runtime.environment"],
            },
            "interactive": {
                "branch_count": 5,
                "changed_branches": 0,
                "unchanged_branches": 5,
                "failed_branches": 0,
                "changed_paths": [],
                "fingerprint_unchanged_branches": 5,
                "fingerprint_unchanged_paths": [
                    "data.catalog",
                    "data.installed",
                    "data.desktop",
                    "data.webio",
                    "data.routing",
                ],
            },
        },
        "fingerprint_unchanged_branches": 8,
        "fingerprint_unchanged_paths": [
            "ui.application",
            "registry.merged",
            "runtime.environment",
            "data.catalog",
            "data.installed",
            "data.desktop",
            "data.webio",
            "data.routing",
        ],
    }


def test_webspace_runtime_apply_rewrites_missing_effective_branch_even_when_fingerprint_matches(monkeypatch) -> None:
    class _Txn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _TrackingMap(dict):
        def set(self, txn, key, value):
            self[key] = value

    class _TrackingDoc:
        def __init__(self, state: dict[str, _TrackingMap]) -> None:
            self._state = state

        def get_map(self, name: str) -> _TrackingMap:
            return self._state[name]

        def begin_transaction(self):
            return _Txn()

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(ctx=SimpleNamespace())
    monkeypatch.setattr(runtime, "_apply_ydoc_defaults_in_txn", lambda ydoc, txn, skill_decls: None)
    monkeypatch.setattr(
        webspace_runtime_module,
        "describe_webspace_rebuild_state",
        lambda webspace_id: {"webspace_id": webspace_id, "status": "ready", "pending": False},
    )

    resolved = webspace_runtime_module.WebspaceResolverOutputs(
        webspace_id="default",
        scenario_id="web_desktop",
        source_mode="workspace",
        application={
            "desktop": {"topbar": [], "pageSchema": {"widgets": []}, "pinnedWidgets": []},
            "modals": {"apps_catalog": {}, "widgets_catalog": {}},
        },
        catalog={"apps": [], "widgets": []},
        registry={"modals": [], "widgets": []},
        installed={"apps": [], "widgets": []},
        desktop={"installed": {"apps": [], "widgets": []}, "topbar": [], "pageSchema": {"widgets": []}, "pinnedWidgets": []},
        routing={"routes": {}},
        skill_decls=[],
    )
    fingerprints = webspace_runtime_module._resolved_output_branch_fingerprints(resolved)
    ydoc = _TrackingDoc(
        {
            "ui": _TrackingMap(
                {
                    "application": resolved.application,
                }
            ),
            "data": _TrackingMap(
                {
                    "installed": resolved.installed,
                    "desktop": resolved.desktop,
                    "routing": resolved.routing,
                }
            ),
            "registry": _TrackingMap(
                {
                    "merged": resolved.registry,
                    "runtime_meta": {
                        webspace_runtime_module._RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY: dict(fingerprints)
                    },
                }
            ),
            "runtime": _TrackingMap({"environment": webspace_runtime_module.runtime_environment_payload()}),
        }
    )
    inputs = webspace_runtime_module.WebspaceResolverInputs(
        webspace_id="default",
        scenario_id="web_desktop",
        source_mode="workspace",
        metadata={},
        scenario_application={},
        scenario_catalog={},
        scenario_registry={},
        overlay_snapshot={},
        live_state={"desktop": {}, "routing": {}},
        compatibility_cache_presence={
            "scenario_ui_application": False,
            "scenario_registry_entry": False,
            "scenario_catalog": False,
        },
        skill_decls=[],
        desktop_scenarios=[],
        scenario_source="loader:workspace",
        legacy_scenario_fallback=False,
    )

    runtime._apply_resolved_state_in_doc(ydoc, "default", resolved, inputs=inputs)

    assert ydoc.get_map("data")["catalog"] == {"apps": [], "widgets": []}
    assert runtime._last_apply_summary["changed_paths"] == ["data.catalog", "data.webio"]
    interactive = runtime._last_apply_summary["phases"]["interactive"]
    assert interactive["changed_paths"] == ["data.catalog", "data.webio"]
    assert "data.catalog" not in interactive.get("fingerprint_unchanged_paths", [])


def test_node_cli_ensure_dev_posts_requested_id_and_title(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "created": False, "webspace_id": "dev_prompt", "scenario_id": "prompt_engineer_scenario"})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_ensure_dev_action(
        scenario_id="prompt_engineer_scenario",
        requested_id="dev_prompt",
        title="Prompt IDE",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/dev-webspaces/ensure",
            "body": {
                "scenario_id": "prompt_engineer_scenario",
                "requested_id": "dev_prompt",
                "title": "Prompt IDE",
            },
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_create_posts_metadata(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "webspace": {"id": "preview-space", "home_scenario": "prompt_engineer_scenario"}})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_create_action(
        webspace="preview-space",
        title="Preview Space",
        scenario_id="prompt_engineer_scenario",
        dev=True,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces",
            "body": {
                "id": "preview-space",
                "title": "Preview Space",
                "scenario_id": "prompt_engineer_scenario",
                "dev": True,
            },
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_update_patches_metadata(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_patch_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "webspace": {"id": "default", "home_scenario": "prompt_engineer_scenario"}})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_update_action(
        webspace="default",
        title="Desktop",
        home_scenario="prompt_engineer_scenario",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces/default",
            "body": {
                "title": "Desktop",
                "home_scenario": "prompt_engineer_scenario",
            },
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_describe_reads_webspace_state(monkeypatch) -> None:
    captured: list[str] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_get_json",
        lambda **kwargs: (
            captured.append(kwargs.get("path"))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace": {
                        "webspace_id": "default",
                        "kind": "workspace",
                        "source_mode": "workspace",
                        "home_scenario": "web_desktop",
                        "current_scenario": "web_desktop",
                    },
                    "projection": {
                        "target_scenario": "web_desktop",
                        "target_space": "workspace",
                        "active_scenario": "web_desktop",
                        "active_space": "workspace",
                        "active_matches_target": True,
                        "base_rule_count": 2,
                        "scenario_rule_count": 1,
                    },
                    "runtime": {"assessment": {"state": "nominal"}},
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_describe_action(
        webspace="default",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == ["/api/node/yjs/webspaces/default"]
    assert rendered[-1][1] is True


def test_node_cli_desktop_reads_desktop_state(monkeypatch) -> None:
    captured: list[str] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_get_json",
        lambda **kwargs: (
            captured.append(kwargs.get("path"))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "default",
                    "desktop": {
                        "installed": {"apps": ["scenario:web_desktop"], "widgets": ["weather"]},
                        "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
                    },
                    "runtime": {"assessment": {"state": "nominal"}},
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_desktop_action(
        webspace="default",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == ["/api/node/yjs/webspaces/default/desktop"]
    assert rendered[-1][1] is True


def test_node_cli_materialization_reads_lightweight_state(monkeypatch) -> None:
    captured: list[str] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_get_json",
        lambda **kwargs: (
            captured.append(kwargs.get("path"))
            or (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace_id": "default",
                    "rebuild": {"status": "running", "pending": True},
                    "materialization": {
                        "ready": False,
                        "readiness_state": "interactive",
                        "missing_branches": ["data.desktop"],
                    },
                    "runtime": {"assessment": {"state": "nominal"}},
                },
            )
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_materialization_action(
        webspace="default",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == ["/api/node/yjs/webspaces/default/materialization?include_runtime=1"]
    assert rendered[-1][1] is True


def test_node_cli_materialization_falls_back_to_full_describe_on_404(monkeypatch) -> None:
    captured: list[str] = []
    rendered: list[tuple[object, bool]] = []
    responses = iter(
        [
            (404, {"detail": "not found"}),
            (
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "webspace": {
                        "webspace_id": "default",
                    },
                    "rebuild": {"status": "ready", "pending": False},
                    "materialization": {
                        "ready": True,
                        "readiness_state": "ready",
                        "missing_branches": [],
                    },
                    "runtime": {"assessment": {"state": "nominal"}},
                },
            ),
        ]
    )

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_get_json",
        lambda **kwargs: captured.append(kwargs.get("path")) or next(responses),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_materialization_action(
        webspace="default",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        "/api/node/yjs/webspaces/default/materialization?include_runtime=1",
        "/api/node/yjs/webspaces/default",
    ]
    assert rendered[-1][1] is True
