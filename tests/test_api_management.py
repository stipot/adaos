from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

from adaos.apps.api import scenarios, skills
from adaos.apps.api.auth import require_token


@dataclass
class _Record:
    name: str
    installed: bool = True
    active_version: Optional[str] = None


@dataclass
class _Meta:
    id: Any
    name: str
    version: str
    path: str


class _FakeSkillManager:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.active_slot = "A"
        self.active_version = "1.0.0"

    def list_installed(self) -> list[_Record]:
        self.calls.append("list_installed")
        return [_Record(name="demo", installed=True, active_version="1.0.0")]

    def list_present(self) -> list[_Meta]:
        self.calls.append("list_present")
        return [_Meta(id=type("Id", (), {"value": "demo"})(), name="demo", version="1.0.0", path="/skills/demo")]

    def sync(self, *, force: bool | None = None) -> None:
        self.calls.append(f"sync:{force}")

    def install(self, name: str, **kwargs: Any):
        self.calls.append(f"install:{name}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="1.0.0", path=f"/skills/{name}")

    def get(self, name: str):
        self.calls.append(f"get:{name}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="1.0.0", path=f"/skills/{name}")

    def runtime_status(self, name: str):
        self.calls.append(f"runtime_status:{name}")
        return {"active_slot": self.active_slot, "version": self.active_version}

    def runtime_update(self, name: str, *, space: str = "workspace"):
        self.calls.append(f"runtime_update:{name}:{space}")
        return {"ok": True, "version": "1.0.0"}

    def prepare_runtime(self, name: str, run_tests: bool = False):
        self.calls.append(f"prepare_runtime:{name}")
        return SimpleNamespace(version="2.0.0", slot="B")

    def activate_for_space(self, name: str, *, version: str | None = None, slot: str | None = None, space: str = "default", webspace_id: str = "default"):
        self.calls.append(f"activate_for_space:{name}:{version}:{slot}:{webspace_id}")
        self.active_version = version or self.active_version
        self.active_slot = slot or self.active_slot
        return slot or "B"

    def uninstall(self, name: str, **kwargs: Any) -> None:
        force = int(bool(kwargs.get("force", False)))
        self.calls.append(f"uninstall:{name}:{force}")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.calls.append(f"push:{name}:{message}:{int(signoff)}")
        return "deadbeef"


class _FakeScenarioManager:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.last_dependency_bootstrap_result: dict[str, Any] | None = None

    def list_installed(self) -> list[_Record]:
        self.calls.append("list_installed")
        return [_Record(name="scene", installed=True, active_version="0.1.0")]

    def list_present(self) -> list[_Meta]:
        self.calls.append("list_present")
        return [_Meta(id=type("Id", (), {"value": "scene"})(), name="scene", version="0.1.0", path="/scenarios/scene")]

    def sync(self) -> None:
        self.calls.append("sync")

    def install(self, name: str, *, pin: str | None = None):
        self.calls.append(f"install:{name}:{pin}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="0.1.0", path=f"/scenarios/{name}")

    def install_with_deps(self, name: str, *, pin: str | None = None, webspace_id: str | None = None):
        self.calls.append(f"install_with_deps:{name}:{pin}:{webspace_id}")
        self.last_dependency_bootstrap_result = {
            "ok": True,
            "scenario_id": name,
            "webspace_id": webspace_id,
            "required": ["demo"],
            "items": [{"name": "demo", "ok": True}],
            "succeeded": ["demo"],
            "failed": [],
        }
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="0.1.0", path=f"/scenarios/{name}")

    def bootstrap_dependencies(self, name: str, *, webspace_id: str | None = None):
        self.calls.append(f"bootstrap_dependencies:{name}:{webspace_id}")
        self.last_dependency_bootstrap_result = {
            "ok": True,
            "scenario_id": name,
            "webspace_id": webspace_id,
            "required": ["demo"],
            "items": [{"name": "demo", "ok": True}],
            "succeeded": ["demo"],
            "failed": [],
        }
        return self.last_dependency_bootstrap_result

    def sync_to_yjs(self, name: str, *, webspace_id: str | None = None, emit_event: bool = True):
        self.calls.append(f"sync_to_yjs:{name}:{webspace_id}:{int(bool(emit_event))}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="0.1.0", path=f"/scenarios/{name}")

    def uninstall(self, name: str) -> None:
        self.calls.append(f"uninstall:{name}")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.calls.append(f"push:{name}:{message}:{int(signoff)}")
        return "cafebabe"


def _make_client(skill_mgr: _FakeSkillManager, scenario_mgr: _FakeScenarioManager) -> TestClient:
    app = FastAPI()
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.dependency_overrides[require_token] = lambda: None
    app.dependency_overrides[skills._get_manager] = lambda: skill_mgr
    app.dependency_overrides[scenarios._get_manager] = lambda: scenario_mgr
    return TestClient(app)


def test_skill_api_exposes_management_routes() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    rebuilds: list[tuple[str, str, str, str | None]] = []
    skills.submit_install_operation = lambda **kwargs: {
        "operation_id": "op-skill-demo",
        "target_id": kwargs["target_id"],
        "target_kind": kwargs["target_kind"],
        "status": "accepted",
    }
    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))
    skills.rebuild_webspace_projection = _rebuild
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/skills/list")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"][0]["name"] == "demo"

    assert client.post("/api/skills/sync").status_code == 200
    assert "sync:None" in skill_mgr.calls

    resp = client.post("/api/skills/install", json={"name": "demo"})
    assert resp.status_code == 200
    assert resp.json()["skill"]["id"] == "demo"
    assert resp.json()["runtime"]["slot"] == "B"
    assert ("desktop", "skill_install_sync", "skill_runtime", None) in rebuilds

    resp = client.post("/api/skills/install", json={"name": "demo", "async_operation": True, "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["operation"]["target_id"] == "demo"

    resp = client.get("/api/skills/demo")
    assert resp.status_code == 200
    assert resp.json()["skill"]["name"] == "demo"

    assert client.post("/api/skills/uninstall", json={"name": "demo", "webspace_id": "desktop"}).status_code == 200
    assert ("desktop", "skill_uninstall_sync", "skill_runtime", None) in rebuilds

    assert client.delete("/api/skills/demo").status_code == 200
    assert ("desktop", "skill_uninstall_sync", "skill_runtime", None) in rebuilds

    resp = client.post("/api/skills/push", json={"name": "demo", "message": "msg"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "deadbeef"

    assert any(call.startswith("install:") for call in skill_mgr.calls)
    assert "prepare_runtime:demo" in skill_mgr.calls
    assert any(call.startswith("activate_for_space:demo:") and call.endswith(":desktop") for call in skill_mgr.calls)
    assert any(call.startswith("push:") for call in skill_mgr.calls)


def test_skill_api_install_reports_runtime_preparation_failure() -> None:
    class _FailingRuntimeSkillManager(_FakeSkillManager):
        def prepare_runtime(self, name: str, run_tests: bool = False):
            raise RuntimeError("missing torch dependency")

    skill_mgr = _FailingRuntimeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.post("/api/skills/install", json={"name": "demo"})

    assert resp.status_code == 409
    assert "runtime preparation failed for demo" in resp.json()["detail"]
    assert "missing torch dependency" in resp.json()["detail"]


def test_skill_api_list_prefers_workspace_version(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    monkeypatch.setattr(skills, "list_workspace_registry_entries", lambda *args, **kwargs: [{"name": "demo", "version": "2.0.0"}])
    monkeypatch.setattr(skills, "_resolve_list_skill_version", lambda **kwargs: "2.0.0")

    resp = client.get("/api/skills/list")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"][0]["name"] == "demo"
    assert payload["items"][0]["version"] == "2.0.0"


def test_scenario_api_matches_service_surface() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    rebuilds: list[tuple[str, str, str, str | None]] = []
    scenarios.submit_install_operation = lambda **kwargs: {
        "operation_id": "op-scenario-scene",
        "target_id": kwargs["target_id"],
        "target_kind": kwargs["target_kind"],
        "status": "accepted",
    }
    scenarios.submit_update_operation = lambda **kwargs: {
        "operation_id": "op-scenario-update",
        "target_id": kwargs["target_id"],
        "target_kind": kwargs["target_kind"],
        "kind": "scenario.update",
        "status": "accepted",
    }
    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))
    scenarios.rebuild_webspace_from_sources = _rebuild
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/scenarios/list?fs=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"][0]["name"] == "scene"
    assert "fs" in data

    assert client.post("/api/scenarios/sync").status_code == 200

    resp = client.post("/api/scenarios/install", json={"name": "scene"})
    assert resp.status_code == 200
    assert resp.json()["scenario"]["id"] == "scene"
    assert resp.json()["dependency_bootstrap"]["ok"] is True
    assert resp.json()["dependency_bootstrap"]["succeeded"] == ["demo"]
    assert ("desktop", "scenario_install_sync", "scenario_projection", "scene") in rebuilds

    resp = client.post("/api/scenarios/install", json={"name": "scene", "async_operation": True, "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["operation"]["target_id"] == "scene"

    resp = client.post("/api/scenarios/update", json={"name": "scene", "webspace_id": "desktop"})
    assert resp.status_code == 200
    assert resp.json()["scenario"]["id"] == "scene"
    assert resp.json()["dependency_bootstrap"]["ok"] is True
    assert resp.json()["dependency_bootstrap"]["succeeded"] == ["demo"]
    assert "bootstrap_dependencies:scene:desktop" in scenario_mgr.calls
    assert "sync_to_yjs:scene:desktop:0" in scenario_mgr.calls
    assert ("desktop", "scenario_update_sync", "scenario_projection", "scene") in rebuilds

    resp = client.post("/api/scenarios/update", json={"name": "scene", "async_operation": True, "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["operation"]["kind"] == "scenario.update"

    assert client.post("/api/scenarios/uninstall", json={"name": "scene", "webspace_id": "desktop"}).status_code == 200
    assert ("desktop", "scenario_uninstall_sync", "scenario_projection", None) in rebuilds

    assert client.delete("/api/scenarios/scene").status_code == 200
    assert ("desktop", "scenario_uninstall_sync", "scenario_projection", None) in rebuilds

    resp = client.post("/api/scenarios/push", json={"name": "scene", "message": "msg", "signoff": True})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "cafebabe"

    assert any(call.startswith("push:") for call in scenario_mgr.calls)


def test_scenario_api_blocks_failed_dependencies_before_projection() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    rebuilds: list[tuple[str, str, str, str | None]] = []
    dep_result = {
        "ok": False,
        "scenario_id": "scene",
        "webspace_id": "desktop",
        "required": ["bad_skill"],
        "items": [{"name": "bad_skill", "ok": False, "error": "prepare failed"}],
        "succeeded": [],
        "failed": ["bad_skill"],
        "error": "RuntimeError: prepare failed",
    }

    def _bootstrap_dependencies(name: str, *, webspace_id: str | None = None):
        scenario_mgr.calls.append(f"bootstrap_dependencies:{name}:{webspace_id}")
        return dict(dep_result)

    def _sync_to_yjs(name: str, *, webspace_id: str | None = None, emit_event: bool = True):
        scenario_mgr.calls.append(f"sync_to_yjs:{name}:{webspace_id}:{int(bool(emit_event))}")

    async def _rebuild(webspace_id: str, *, action: str = "rebuild", scenario_id: str | None = None, source_of_truth: str = "workspace"):
        rebuilds.append((webspace_id, action, source_of_truth, scenario_id))

    scenario_mgr.bootstrap_dependencies = _bootstrap_dependencies  # type: ignore[method-assign]
    scenario_mgr.sync_to_yjs = _sync_to_yjs  # type: ignore[method-assign]
    scenarios.rebuild_webspace_from_sources = _rebuild
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.post("/api/scenarios/update", json={"name": "scene", "webspace_id": "desktop"})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "scenario_dependency_lifecycle_failed"
    assert detail["dependency_bootstrap"]["failed"] == ["bad_skill"]
    assert "bootstrap_dependencies:scene:desktop" in scenario_mgr.calls
    assert not any(call.startswith("sync_to_yjs:") for call in scenario_mgr.calls)
    assert rebuilds == []


def test_skill_installed_status_uses_registry_catalog_version(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    monkeypatch.setattr(skills, "find_workspace_registry_entry", lambda *args, **kwargs: {"version": "2.0.0"})
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/skills/installed-status")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["remote_version"] == "2.0.0"
    assert item["update_available"] is True


def test_skill_update_refreshes_runtime_when_source_version_changed(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            return SimpleNamespace(updated=True, version="2.0.0")

    async def _rebuild(*args, **kwargs):
        return None

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)
    monkeypatch.setattr(skills, "_get_manager", lambda ctx: skill_mgr)
    monkeypatch.setattr(skills, "rebuild_webspace_projection", _rebuild)

    resp = client.post("/api/skills/update", json={"name": "demo", "webspace_id": "default"})
    assert resp.status_code == 200
    payload = resp.json()
    refresh = payload["runtime_refresh"]
    assert payload["updated"] is True
    assert "runtime_update:demo:workspace" in skill_mgr.calls
    assert "prepare_runtime:demo" in skill_mgr.calls
    assert any(call.startswith("activate_for_space:demo:2.0.0:B:default") for call in skill_mgr.calls)
    assert refresh["ok"] is True
    assert refresh["prepared_version"] == "2.0.0"
    assert refresh["prepared_slot"] == "B"
    assert refresh["activated_slot"] == "B"
    assert refresh["failed_stage"] == ""
    assert refresh["failure_reason"] == ""
    assert [stage["stage"] for stage in refresh["lifecycle_stages"]] == [
        "runtime_update",
        "prepare",
        "activate",
        "converge",
    ]


def test_skill_update_fails_when_active_runtime_does_not_converge(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)
    bus_events: list[tuple[str, dict[str, Any], str]] = []
    rebuilds: list[dict[str, Any]] = []

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            return SimpleNamespace(updated=True, version="2.0.0")

    def _activate_without_state_change(
        name: str,
        *,
        version: str | None = None,
        slot: str | None = None,
        space: str = "default",
        webspace_id: str = "default",
    ):
        skill_mgr.calls.append(f"activate_for_space:{name}:{version}:{slot}:{webspace_id}")
        return slot or "B"

    monkeypatch.setattr(skill_mgr, "activate_for_space", _activate_without_state_change)
    monkeypatch.setattr(skills, "SkillUpdateService", _Service)
    monkeypatch.setattr(skills, "_get_manager", lambda ctx: skill_mgr)
    monkeypatch.setattr(skills, "bus_emit", lambda bus, typ, payload, source: bus_events.append((typ, payload, source)))
    monkeypatch.setattr(skills, "_schedule_webspace_rebuild", lambda **kwargs: rebuilds.append(dict(kwargs)))

    resp = client.post("/api/skills/update", json={"name": "demo", "webspace_id": "default"})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "did not converge" in detail["message"]
    assert detail["runtime_refresh"]["failed_stage"] == "converge"
    assert "did not converge" in detail["runtime_refresh"]["failure_reason"]
    assert detail["runtime_refresh"]["prepared_version"] == "2.0.0"
    assert detail["runtime_refresh"]["prepared_slot"] == "B"
    assert "runtime_update:demo:workspace" in skill_mgr.calls
    assert "prepare_runtime:demo" in skill_mgr.calls
    assert bus_events == []
    assert rebuilds == []


def test_skill_update_can_defer_webspace_rebuild_until_batch_finalize(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)
    bus_events: list[tuple[str, dict[str, Any], str]] = []
    rebuilds: list[tuple[str, str, str, str | None]] = []

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            return SimpleNamespace(updated=True, version="2.0.0")

    async def _rebuild(*args, **kwargs):
        rebuilds.append((
            kwargs.get("webspace_id") if "webspace_id" in kwargs else args[0],
            kwargs.get("action", "rebuild"),
            kwargs.get("source_of_truth", "workspace"),
            kwargs.get("scenario_id"),
        ))
        return None

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)
    monkeypatch.setattr(skills, "_get_manager", lambda ctx: skill_mgr)
    monkeypatch.setattr(skills, "rebuild_webspace_projection", _rebuild)
    monkeypatch.setattr(skills, "bus_emit", lambda bus, typ, payload, source: bus_events.append((typ, payload, source)))
    monkeypatch.setattr(skills, "get_ctx", lambda: SimpleNamespace(bus=object()))

    resp = client.post(
        "/api/skills/update",
        json={
            "name": "demo",
            "webspace_id": "default",
            "defer_webspace_rebuild": True,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["updated"] is True
    assert rebuilds == []
    assert any(
        typ == "skills.activated" and payload.get("defer_webspace_rebuild") is True
        for typ, payload, _source in bus_events
    )


def test_skill_runtime_rebuild_webspace_endpoint_rebuilds_once(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)
    rebuilds: list[tuple[str, str, str, str | None]] = []

    async def _rebuild(*args, **kwargs):
        rebuilds.append((
            kwargs.get("webspace_id") if "webspace_id" in kwargs else args[0],
            kwargs.get("action", "rebuild"),
            kwargs.get("source_of_truth", "workspace"),
            kwargs.get("scenario_id"),
        ))
        return None

    monkeypatch.setattr(skills, "rebuild_webspace_projection", _rebuild)

    resp = client.post(
        "/api/skills/runtime/rebuild-webspace",
        json={"webspace_id": "default"},
    )

    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert rebuilds == [("default", "skill_batch_runtime_sync", "skill_runtime", None)]


def test_skill_update_returns_not_found_when_source_skill_missing(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            raise FileNotFoundError(f"skill '{skill_id}' is not installed")

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)

    resp = client.post("/api/skills/update", json={"name": "missing"})
    assert resp.status_code == 404
    assert "missing" in str(resp.json().get("detail") or "")


def test_skill_update_returns_conflict_for_runtime_git_errors(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            raise RuntimeError("workspace has local changes")

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)

    resp = client.post("/api/skills/update", json={"name": "demo"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "workspace has local changes"


def test_skill_update_passes_force_flag_when_requested(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)
    captured: list[tuple[str, bool, bool | None]] = []

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False, force: bool | None = None):
            captured.append((skill_id, dry_run, force))
            return SimpleNamespace(updated=True, version="2.0.0")

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)
    monkeypatch.setattr(skills, "_get_manager", lambda ctx: skill_mgr)
    monkeypatch.setattr(skills, "rebuild_webspace_projection", lambda *args, **kwargs: None)

    resp = client.post("/api/skills/update", json={"name": "demo", "force": True})

    assert resp.status_code == 200
    assert captured == [("demo", False, True)]
