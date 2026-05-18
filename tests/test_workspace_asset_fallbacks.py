from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import types

from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
import adaos.services.skill.manager as skill_manager_module

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.services.scenario import webspace_runtime as webspace_runtime_module
from adaos.services.scenario.webspace_runtime import WebspaceScenarioRuntime


class _PathsStub:
    def __init__(self, *, base_dir: Path, repo_root: Path) -> None:
        self._base_dir = base_dir
        self._repo_root = repo_root

    def scenarios_dir(self) -> Path:
        return self._base_dir / "workspace" / "scenarios"

    def dev_scenarios_dir(self) -> Path:
        return self._base_dir / "dev" / "scenarios"

    def skills_dir(self) -> Path:
        return self._base_dir / "workspace" / "skills"

    def skills_workspace_dir(self) -> Path:
        return self.skills_dir()

    def dev_skills_dir(self) -> Path:
        return self._base_dir / "dev" / "skills"

    def repo_root(self) -> Path:
        return self._repo_root


def test_scenario_loader_falls_back_to_repo_workspace(monkeypatch, tmp_path: Path) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_scenario = repo_root / ".adaos" / "workspace" / "scenarios" / "prompt_engineer_scenario"
    repo_scenario.mkdir(parents=True, exist_ok=True)
    (repo_scenario / "scenario.yaml").write_text(
        'id: prompt_engineer_scenario\nversion: "0.1.0"\ntype: desktop\ntitle: Prompt IDE\n',
        encoding="utf-8",
    )
    (repo_scenario / "scenario.json").write_text(
        json.dumps({"id": "prompt_engineer_scenario", "ui": {"application": {"desktop": {"pageSchema": {"id": "prompt"}}}}}),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    monkeypatch.setattr(scenarios_loader, "get_ctx", lambda: fake_ctx)
    scenarios_loader.invalidate_cache(scenario_id="prompt_engineer_scenario", space="workspace")

    manifest = scenarios_loader.read_manifest("prompt_engineer_scenario")
    content = scenarios_loader.read_content("prompt_engineer_scenario")

    assert manifest["title"] == "Prompt IDE"
    assert content["id"] == "prompt_engineer_scenario"
    assert content["ui"]["application"]["desktop"]["pageSchema"]["id"] == "prompt"


def test_scenario_loader_cache_refreshes_when_repo_workspace_files_change(monkeypatch, tmp_path: Path) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_scenario = repo_root / ".adaos" / "workspace" / "scenarios" / "prompt_engineer_scenario"
    repo_scenario.mkdir(parents=True, exist_ok=True)
    manifest_path = repo_scenario / "scenario.yaml"
    content_path = repo_scenario / "scenario.json"
    manifest_path.write_text(
        'id: prompt_engineer_scenario\nversion: "0.1.0"\ntype: desktop\ntitle: Prompt IDE A\n',
        encoding="utf-8",
    )
    content_path.write_text(
        json.dumps({"id": "prompt_engineer_scenario", "ui": {"application": {"desktop": {"pageSchema": {"id": "prompt-a"}}}}}),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    monkeypatch.setattr(scenarios_loader, "get_ctx", lambda: fake_ctx)
    scenarios_loader.invalidate_cache(scenario_id="prompt_engineer_scenario", space="workspace")

    first_manifest = scenarios_loader.read_manifest("prompt_engineer_scenario")
    first_content = scenarios_loader.read_content("prompt_engineer_scenario")

    time.sleep(0.01)
    manifest_path.write_text(
        'id: prompt_engineer_scenario\nversion: "0.1.0"\ntype: desktop\ntitle: Prompt IDE B\n',
        encoding="utf-8",
    )
    content_path.write_text(
        json.dumps({"id": "prompt_engineer_scenario", "ui": {"application": {"desktop": {"pageSchema": {"id": "prompt-b"}}}}}),
        encoding="utf-8",
    )

    second_manifest = scenarios_loader.read_manifest("prompt_engineer_scenario")
    second_content = scenarios_loader.read_content("prompt_engineer_scenario")

    assert first_manifest["title"] == "Prompt IDE A"
    assert first_content["ui"]["application"]["desktop"]["pageSchema"]["id"] == "prompt-a"
    assert second_manifest["title"] == "Prompt IDE B"
    assert second_content["ui"]["application"]["desktop"]["pageSchema"]["id"] == "prompt-b"


def test_webspace_runtime_load_webui_falls_back_to_repo_workspace(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "prompt_engineer_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)
    (repo_skill / "webui.json").write_text(
        json.dumps(
            {
                "apps": [
                    {
                        "id": "scenario:prompt_engineer_scenario",
                        "title": "Prompt IDE",
                        "scenario_id": "prompt_engineer_scenario",
                    }
                ],
                "registry": {"modals": {"prompt_ide_modal": {"title": "Prompt IDE"}}},
            }
        ),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    runtime = WebspaceScenarioRuntime(fake_ctx)
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")

    payload = runtime._load_webui("prompt_engineer_skill", "default")

    assert payload["apps"][0]["scenario_id"] == "prompt_engineer_scenario"
    assert payload["node_id"] == "node-1"
    assert "prompt_ide_modal" in payload["registry"]["modals"]


def test_webspace_runtime_load_webui_reads_shared_ui_owner_from_skill_manifest(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "demo_metrics_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)
    (repo_skill / "skill.yaml").write_text(
        "name: demo_metrics_skill\nwebui_owner: shared\n",
        encoding="utf-8",
    )
    (repo_skill / "webui.json").write_text(
        json.dumps(
            {
                "ydoc_defaults": {
                    "data/demo_metrics/table": {"items": [{"id": "cpu"}]},
                }
            }
        ),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    runtime = WebspaceScenarioRuntime(fake_ctx)
    monkeypatch.setattr(webspace_runtime_module, "_local_node_id", lambda: "node-1")

    payload = runtime._load_webui("demo_metrics_skill", "default")

    assert payload["ui_owner"] == "shared"
    assert payload["ydoc_defaults"]["data/demo_metrics/table"] == {"items": [{"id": "cpu"}]}


def test_webspace_runtime_load_webui_cache_refreshes_when_file_changes(tmp_path: Path) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "prompt_engineer_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)
    webui_path = repo_skill / "webui.json"
    webui_path.write_text(
        json.dumps(
            {
                "apps": [{"id": "scenario:prompt_a", "title": "Prompt A", "scenario_id": "prompt_a"}],
                "registry": {"modals": {"prompt_modal_a": {"title": "Prompt A"}}},
            }
        ),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    runtime = WebspaceScenarioRuntime(fake_ctx)

    first_payload = runtime._load_webui("prompt_engineer_skill", "default")

    time.sleep(0.01)
    webui_path.write_text(
        json.dumps(
            {
                "apps": [{"id": "scenario:prompt_b", "title": "Prompt B", "scenario_id": "prompt_b"}],
                "registry": {"modals": {"prompt_modal_b": {"title": "Prompt B"}}},
            }
        ),
        encoding="utf-8",
    )

    second_payload = runtime._load_webui("prompt_engineer_skill", "default")

    assert first_payload["apps"][0]["scenario_id"] == "prompt_a"
    assert "prompt_modal_a" in first_payload["registry"]["modals"]
    assert second_payload["apps"][0]["scenario_id"] == "prompt_b"
    assert "prompt_modal_b" in second_payload["registry"]["modals"]


def test_webspace_runtime_load_webui_normalizes_staged_load_hints(tmp_path: Path) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "prompt_engineer_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)
    (repo_skill / "webui.json").write_text(
        json.dumps(
            {
                "apps": [
                    {
                        "id": "prompt_ide",
                        "title": "Prompt IDE",
                        "load": {"structure": "visible", "data": "interaction", "focus": "primary"},
                    }
                ],
                "widgets": [
                    {
                        "id": "chat_widget",
                        "type": "ui.chat",
                        "load": {"structure": "visible", "scheduler": "critical_path"},
                    }
                ],
                "registry": {
                    "modals": {
                        "prompt_modal": {
                            "title": "Prompt Modal",
                            "load": {
                                "structure": "interaction",
                                "data": "deferred",
                                "focus": "off_focus",
                                "offFocusReadyState": "hydrating",
                            },
                            "schema": {
                                "id": "prompt_modal",
                                "load": {"structure": "interaction", "data": "deferred", "focus": "off_focus"},
                                "layout": {"type": "single", "areas": [{"id": "main"}]},
                                "widgets": [
                                    {
                                        "id": "prompt_list",
                                        "type": "collection.grid",
                                        "area": "main",
                                        "load": {"structure": "visible", "data": "deferred", "focus": "off_focus"},
                                    }
                                ],
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root))
    runtime = WebspaceScenarioRuntime(fake_ctx)

    payload = runtime._load_webui("prompt_engineer_skill", "default")

    assert payload["apps"][0]["load"] == {"structure": "visible", "data": "interaction", "focus": "primary"}
    assert payload["widgets"][0]["load"] == {"structure": "visible"}
    modal = payload["registry"]["modals"]["prompt_modal"]
    assert modal["load"]["offFocusReadyState"] == "hydrating"
    assert modal["schema"]["load"]["focus"] == "off_focus"
    assert modal["schema"]["widgets"][0]["load"]["data"] == "deferred"


def test_webspace_runtime_switch_content_falls_back_to_builtin_web_desktop(monkeypatch) -> None:
    monkeypatch.setattr(webspace_runtime_module.scenarios_loader, "read_content", lambda _scenario_id, space="workspace": {})

    payload = webspace_runtime_module._load_scenario_switch_content("web_desktop", space="workspace")

    assert payload["id"] == "web_desktop"
    assert payload["ui"]["application"]["desktop"]["pageSchema"]["id"] == "desktop"
    assert isinstance(payload["catalog"], dict)


def test_skill_manager_runtime_update_falls_back_to_repo_workspace(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"

    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "infrastate_skill"
    (repo_skill / "handlers").mkdir(parents=True, exist_ok=True)
    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "repo-workspace-handler"\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrastate_skill\nversion: '0.1.0'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )

    skills_root = runtime_base / "workspace" / "skills"
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="infrastate_skill")
    env.prepare_version("0.1.0")
    slot = env.build_slot_paths("0.1.0", env.read_active_slot("0.1.0"))
    runtime_skill = slot.src_dir / "skills" / "infrastate_skill"
    (runtime_skill / "handlers").mkdir(parents=True, exist_ok=True)
    (runtime_skill / "handlers" / "main.py").write_text(
        'MARKER = "stale-runtime-handler"\n',
        encoding="utf-8",
    )
    slot.resolved_manifest.write_text("{}", encoding="utf-8")

    fake_ctx = SimpleNamespace(
        paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root),
        caps=SimpleNamespace(),
        bus=None,
        settings=SimpleNamespace(),
    )
    monkeypatch.setattr(skill_manager_module, "get_ctx", lambda: fake_ctx)

    manager = skill_manager_module.SkillManager(
        git=SimpleNamespace(),
        paths=fake_ctx.paths,
        caps=fake_ctx.caps,
        settings=fake_ctx.settings,
        registry=None,
        repo=None,
        bus=None,
    )

    result = manager.runtime_update("infrastate_skill", space="workspace")

    assert result["ok"] is True
    assert result["source"] == "repo_workspace"
    assert result["source_path"].endswith(".adaos\\workspace\\skills\\infrastate_skill") or result["source_path"].endswith(
        ".adaos/workspace/skills/infrastate_skill"
    )
    assert "repo-workspace-handler" in (runtime_skill / "handlers" / "main.py").read_text(encoding="utf-8")


def test_skill_manager_activate_runtime_prepares_repo_workspace_when_missing(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"

    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "infrascope_skill"
    (repo_skill / "handlers").mkdir(parents=True, exist_ok=True)
    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "repo-workspace-handler"\n'
        'def handle(payload=None):\n'
        '    return payload or {}\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrascope_skill\nversion: '0.3.0'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(
        paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root),
        caps=SimpleNamespace(),
        bus=None,
        settings=SimpleNamespace(
            default_wall_time_sec=30.0,
            default_max_rss_mb=None,
            default_cpu_time_sec=None,
        ),
    )
    monkeypatch.setattr(skill_manager_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)

    manager = skill_manager_module.SkillManager(
        git=SimpleNamespace(),
        paths=fake_ctx.paths,
        caps=fake_ctx.caps,
        settings=fake_ctx.settings,
        registry=None,
        repo=None,
        bus=None,
    )

    slot_name = manager.activate_runtime("infrascope_skill")
    status = manager.runtime_status("infrascope_skill")
    env = SkillRuntimeEnvironment(skills_root=runtime_base / "workspace" / "skills", skill_name="infrascope_skill")
    runtime_handler = env.ensure_current_link("0.3.0") / "src" / "skills" / "infrascope_skill" / "handlers" / "main.py"

    assert slot_name in {"A", "B"}
    assert status["version"] == "0.3.0"
    assert status["ready"] is True
    assert Path(status["resolved_manifest"]).exists()
    assert "repo-workspace-handler" in runtime_handler.read_text(encoding="utf-8")


def test_skill_manager_activate_runtime_refreshes_when_workspace_version_changed(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"

    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "infrastate_skill"
    (repo_skill / "handlers").mkdir(parents=True, exist_ok=True)
    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "runtime-v1"\n'
        'def handle(payload=None):\n'
        '    return payload or {}\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrastate_skill\nversion: '1.0.0'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(
        paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root),
        caps=SimpleNamespace(),
        bus=None,
        settings=SimpleNamespace(
            default_wall_time_sec=30.0,
            default_max_rss_mb=None,
            default_cpu_time_sec=None,
        ),
    )
    monkeypatch.setattr(skill_manager_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)

    manager = skill_manager_module.SkillManager(
        git=SimpleNamespace(),
        paths=fake_ctx.paths,
        caps=fake_ctx.caps,
        settings=fake_ctx.settings,
        registry=None,
        repo=None,
        bus=None,
    )

    first_slot = manager.activate_runtime("infrastate_skill")
    assert first_slot in {"A", "B"}
    assert manager.runtime_status("infrastate_skill")["version"] == "1.0.0"

    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "runtime-v2"\n'
        'def handle(payload=None):\n'
        '    return payload or {}\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrastate_skill\nversion: '1.1.0'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )

    second_slot = manager.activate_runtime("infrastate_skill")
    status = manager.runtime_status("infrastate_skill")
    env = SkillRuntimeEnvironment(skills_root=runtime_base / "workspace" / "skills", skill_name="infrastate_skill")
    runtime_handler = env.ensure_current_link("1.1.0") / "src" / "skills" / "infrastate_skill" / "handlers" / "main.py"

    assert second_slot in {"A", "B"}
    assert status["version"] == "1.1.0"
    assert "runtime-v2" in runtime_handler.read_text(encoding="utf-8")

    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "runtime-v3"\n'
        'def handle(payload=None):\n'
        '    return payload or {}\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrastate_skill\nversion: '1.1.1'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )
    env.active_version_marker().write_text("1.1.1", encoding="utf-8")

    third_slot = manager.activate_runtime("infrastate_skill")
    status = manager.runtime_status("infrastate_skill")
    runtime_handler = env.ensure_current_link("1.1.1") / "src" / "skills" / "infrastate_skill" / "handlers" / "main.py"

    assert third_slot in {"A", "B"}
    assert status["version"] == "1.1.1"
    assert "runtime-v3" in runtime_handler.read_text(encoding="utf-8")


def test_skills_loader_imports_repo_workspace_handler_when_workspace_missing(tmp_path: Path, monkeypatch) -> None:
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"

    repo_skill = repo_root / ".adaos" / "workspace" / "skills" / "infrastate_skill"
    (repo_skill / "handlers").mkdir(parents=True, exist_ok=True)
    (repo_skill / "handlers" / "main.py").write_text(
        'MARKER = "repo-workspace-handler"\n',
        encoding="utf-8",
    )
    (repo_skill / "skill.yaml").write_text(
        "name: infrastate_skill\nversion: '0.1.0'\nentry: handlers/main.py\n",
        encoding="utf-8",
    )

    fake_ctx = SimpleNamespace(
        paths=_PathsStub(base_dir=runtime_base, repo_root=repo_root),
        caps=SimpleNamespace(),
        bus=None,
        settings=SimpleNamespace(),
    )
    monkeypatch.setattr("adaos.services.skills_loader_importlib.get_ctx", lambda: fake_ctx)

    loaded: list[Path] = []
    loader = ImportlibSkillsLoader()
    monkeypatch.setattr(loader, "_sync_runtime_from_repo_workspace_if_missing", lambda _root: None)
    monkeypatch.setattr(loader, "_sync_runtime_from_workspace_if_debug", lambda _root: None)
    monkeypatch.setattr(loader, "_load_skill_data_projections", lambda _handler, _loaded: None)
    monkeypatch.setattr(loader, "_load_handler", lambda handler: loaded.append(handler))

    import asyncio

    asyncio.run(loader.import_all_handlers(fake_ctx.paths.skills_dir()))

    assert loaded == [repo_skill / "handlers" / "main.py"]


def test_webspace_reload_emits_reloaded_event_after_rebuild(monkeypatch) -> None:
    import asyncio

    emitted: list[tuple[str, dict[str, object], str]] = []

    class _Bus:
        def publish(self, _event) -> None:
            return None

    fake_ctx = SimpleNamespace(bus=_Bus())

    async def _fake_seed(_webspace_id: str, _scenario_id: str, *, dev: bool | None = None) -> None:
        return None

    async def _fake_sync_listing() -> None:
        return None

    async def _fake_rebuild(self, webspace_id: str):
        assert webspace_id == "default"
        return SimpleNamespace()

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(
        webspace_runtime_module,
        "emit",
        lambda bus, topic, payload, source: emitted.append((topic, dict(payload), source)),
    )

    monkeypatch.setitem(sys.modules, "adaos.services.yjs.gateway", types.SimpleNamespace(y_server=SimpleNamespace(rooms={})))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(reset_ystore_for_webspace=lambda _webspace_id: None),
    )

    asyncio.run(webspace_runtime_module._on_webspace_reload({"webspace_id": "default", "scenario_id": "web_desktop"}))

    assert emitted == [
        (
            "desktop.webspace.reloaded",
            {"webspace_id": "default", "scenario_id": "web_desktop", "action": "reload"},
            "scenario.webspace_runtime",
        )
    ]
