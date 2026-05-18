from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

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

from adaos.services.operations.manager import OperationManager
import adaos.services.operations.manager as operations_manager


class _FakeMap(dict):
    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, default)

    def set(self, txn, key, value):
        self[key] = value


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeYDoc:
    def __init__(self):
        self._maps = {"runtime": _FakeMap()}

    def get_map(self, name: str):
        return self._maps.setdefault(name, _FakeMap())

    def begin_transaction(self):
        return _FakeTxn()


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event) -> None:
        self.events.append(event)


class _FakePaths:
    def base_dir(self):
        return "test-base-dir"


class _FakeToastService:
    pushed: list[dict[str, object]] = []

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def push(self, message: str, **kwargs):
        self.pushed.append({"message": message, **kwargs})


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        bus=_FakeBus(),
        paths=_FakePaths(),
        skills_repo=object(),
        sql=object(),
        git=object(),
        caps=object(),
        settings=object(),
        scenarios_repo=object(),
    )


def test_operation_manager_projects_active_operations_to_yjs(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)

    manager = OperationManager(_make_ctx())
    operation = manager.create_operation(
        kind="skill.install",
        target_kind="skill",
        target_id="demo_skill",
        webspace_id="default",
        scope=["global", "skill.install", "skill:demo_skill"],
        message="Accepted skill install",
    )

    manager.update_operation(
        operation.operation_id,
        status="running",
        progress=25,
        message="Installing",
        current_step="skill.install",
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["active"]
    current = next(item for item in snapshot["active_items"] if item["target_id"] == "demo_skill")
    assert current["target_id"] == "demo_skill"
    assert current["status"] == "running"

    runtime_map = docs["default"].get_map("runtime")
    operations = runtime_map.get("operations")
    assert isinstance(operations, dict)
    assert current["operation_id"] in (operations.get("by_id") or {})


def test_operation_manager_records_notifications_on_completion(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    _FakeToastService.pushed = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)

    manager = OperationManager(_make_ctx())
    operation = manager.create_operation(
        kind="scenario.install",
        target_kind="scenario",
        target_id="welcome",
        webspace_id="default",
        scope=["global", "scenario.install", "scenario:welcome"],
    )
    manager.update_operation(
        operation.operation_id,
        status="succeeded",
        progress=100,
        message="Installed scenario welcome",
        result={"target_id": "welcome"},
        finished=True,
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["notifications"]
    assert snapshot["notifications"][-1]["operation_id"] == operation.operation_id
    assert _FakeToastService.pushed[-1]["message"] == "scenario welcome completed"

    runtime_map = docs["default"].get_map("runtime")
    assert isinstance(runtime_map.get("notifications"), list)


def test_submit_skill_install_operation_prepares_and_activates_runtime(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    calls: list[str] = []
    rebuilds: list[tuple[str, str, str, str | None]] = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    class _FakeSkillManager:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def sync(self) -> None:
            calls.append("sync")

        def install(self, name: str, **kwargs):
            calls.append(f"install:{name}")
            return SimpleNamespace(version="1.2.3", path=f"/skills/{name}")

        def prepare_runtime(self, name: str, run_tests: bool = False):
            calls.append(f"prepare_runtime:{name}:{int(run_tests)}")
            return SimpleNamespace(version="1.2.3", slot="B")

        def activate_for_space(self, name: str, *, version: str | None = None, slot: str | None = None, space: str = "default", webspace_id: str = "default"):
            calls.append(f"activate_for_space:{name}:{version}:{slot}:{space}:{webspace_id}")
            return slot or "B"

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)
    monkeypatch.setattr(operations_manager, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(operations_manager, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(operations_manager, "_MANAGERS", {})
    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))
    monkeypatch.setattr(operations_manager, "rebuild_webspace_from_sources", _rebuild)

    ctx = _make_ctx()
    result = operations_manager.submit_install_operation(
        target_kind="skill",
        target_id="demo_skill",
        webspace_id="default",
        ctx=ctx,
    )

    assert result["target_id"] == "demo_skill"
    assert "sync" in calls
    assert "install:demo_skill" in calls
    assert "prepare_runtime:demo_skill:0" in calls
    assert "activate_for_space:demo_skill:1.2.3:B:default:default" in calls
    assert rebuilds == [("default", "skill_install_sync", "skill_runtime", None)]


def test_submit_scenario_install_operation_rebuilds_target_webspace(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    calls: list[str] = []
    rebuilds: list[tuple[str, str, str, str | None]] = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    class _FakeScenarioManager:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def sync(self) -> None:
            calls.append("sync")

        def install(self, name: str, *, pin: str | None = None):
            calls.append(f"install:{name}:{pin}")
            return SimpleNamespace(version="0.1.0", path=f"/scenarios/{name}")

        def list_present(self):
            calls.append("list_present")
            return [
                SimpleNamespace(
                    id=SimpleNamespace(value="demo_scene"),
                    name="demo_scene",
                    version="0.2.0",
                    path="/scenarios/demo_scene",
                )
            ]

        def bootstrap_dependencies(self, name: str, *, webspace_id: str | None = None):
            calls.append(f"bootstrap_dependencies:{name}:{webspace_id}")
            return {
                "ok": True,
                "scenario_id": name,
                "webspace_id": webspace_id,
                "required": ["demo_skill"],
                "items": [{"name": "demo_skill", "ok": True, "version": "1.2.3", "slot": "B"}],
                "succeeded": ["demo_skill"],
                "failed": [],
            }

        def sync_to_yjs(self, name: str, *, webspace_id: str | None = None, emit_event: bool = True):
            calls.append(f"sync_to_yjs:{name}:{webspace_id}:{int(bool(emit_event))}")
            return SimpleNamespace(version="0.1.0", path=f"/scenarios/{name}")

    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)
    monkeypatch.setattr(operations_manager, "ScenarioManager", _FakeScenarioManager)
    monkeypatch.setattr(operations_manager, "SqliteScenarioRegistry", lambda sql: object())
    monkeypatch.setattr(operations_manager, "_MANAGERS", {})
    monkeypatch.setattr(operations_manager, "rebuild_webspace_from_sources", _rebuild)

    ctx = _make_ctx()
    result = operations_manager.submit_install_operation(
        target_kind="scenario",
        target_id="demo_scene",
        webspace_id="default",
        ctx=ctx,
    )

    assert result["target_id"] == "demo_scene"
    assert "sync" in calls
    assert "install:demo_scene:None" in calls
    assert "bootstrap_dependencies:demo_scene:default" in calls
    assert "sync_to_yjs:demo_scene:default:0" in calls
    assert result["result"]["dependency_bootstrap"]["ok"] is True
    assert result["result"]["dependency_bootstrap"]["succeeded"] == ["demo_skill"]
    assert result["result"]["dependency_bootstrap"]["items"][0]["version"] == "1.2.3"
    assert rebuilds == [("default", "scenario_install_sync", "scenario_projection", "demo_scene")]


def test_submit_scenario_update_operation_reports_dependency_bootstrap(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    calls: list[str] = []
    rebuilds: list[tuple[str, str, str, str | None]] = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    class _FakeScenarioManager:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def sync(self) -> None:
            calls.append("sync")

        def list_present(self):
            calls.append("list_present")
            return [
                SimpleNamespace(
                    id=SimpleNamespace(value="demo_scene"),
                    name="demo_scene",
                    version="0.2.0",
                    path="/scenarios/demo_scene",
                )
            ]

        def bootstrap_dependencies(self, name: str, *, webspace_id: str | None = None):
            calls.append(f"bootstrap_dependencies:{name}:{webspace_id}")
            return {
                "ok": True,
                "scenario_id": name,
                "webspace_id": webspace_id,
                "required": ["demo_skill"],
                "items": [{"name": "demo_skill", "ok": True, "version": "1.2.4", "slot": "A"}],
                "succeeded": ["demo_skill"],
                "failed": [],
            }

        def sync_to_yjs(self, name: str, *, webspace_id: str | None = None, emit_event: bool = True):
            calls.append(f"sync_to_yjs:{name}:{webspace_id}:{int(bool(emit_event))}")
            return SimpleNamespace(version="0.2.0", path=f"/scenarios/{name}")

    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)
    monkeypatch.setattr(operations_manager, "ScenarioManager", _FakeScenarioManager)
    monkeypatch.setattr(operations_manager, "SqliteScenarioRegistry", lambda sql: object())
    monkeypatch.setattr(operations_manager, "_MANAGERS", {})
    monkeypatch.setattr(operations_manager, "rebuild_webspace_from_sources", _rebuild)

    ctx = _make_ctx()
    result = operations_manager.submit_update_operation(
        target_kind="scenario",
        target_id="demo_scene",
        webspace_id="default",
        ctx=ctx,
    )

    assert result["kind"] == "scenario.update"
    assert result["target_id"] == "demo_scene"
    assert "sync" in calls
    assert "list_present" in calls
    assert "bootstrap_dependencies:demo_scene:default" in calls
    assert "sync_to_yjs:demo_scene:default:0" in calls
    assert result["result"]["action"] == "update"
    assert result["result"]["version"] == "0.2.0"
    assert result["result"]["dependency_bootstrap"]["ok"] is True
    assert result["result"]["dependency_bootstrap"]["items"][0]["version"] == "1.2.4"
    assert rebuilds == [("default", "scenario_update_sync", "scenario_projection", "demo_scene")]


def test_submit_install_operation_uses_isolated_subprocess_when_enabled(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    spawned: list[dict[str, object]] = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"installed", b"")

        async def wait(self):
            return self.returncode

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        spawned.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        return _FakeProc()

    monkeypatch.setenv("ADAOS_TESTING", "0")
    monkeypatch.setenv("ADAOS_OPERATIONS_INSTALL_SUBPROCESS", "1")
    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)
    monkeypatch.setattr(operations_manager.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(operations_manager, "_MANAGERS", {})

    ctx = _make_ctx()
    result = operations_manager.submit_install_operation(
        target_kind="scenario",
        target_id="demo_scene",
        webspace_id="default",
        ctx=ctx,
    )

    assert result["target_id"] == "demo_scene"
    assert result["status"] == "succeeded"
    assert len(spawned) == 1
    assert spawned[0]["argv"][:4] == [sys.executable, "-m", "adaos", "scenario"]
    assert spawned[0]["argv"][4:] == ["install", "demo_scene"]
    assert spawned[0]["env"]["ADAOS_DISABLE_PREFERRED_PYTHON_REEXEC"] == "1"
