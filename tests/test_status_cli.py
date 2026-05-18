from pathlib import Path
import sys
import types

from typer.testing import CliRunner

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.cli.commands import scenario as scenario_cmd
from adaos.apps.cli.commands import skill as skill_cmd


def _fake_path_status(path: str):
    class _Status:
        def __init__(self, target: str):
            self.path = target
            self.exists = True
            self.dirty = False
            self.base_ref = "HEAD"
            self.changed_vs_base = False
            self.ahead_count = 0
            self.behind_count = 0
            self.local_last_commit = None
            self.base_last_commit = None
            self.error = None

    return _Status(path)


def test_skill_status_falls_back_to_workspace_when_registry_empty(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "weather_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    runtime_root = tmp_base_dir / "workspace" / "skills" / ".runtime" / "weather_skill" / "1.0.0"
    runtime_root.mkdir(parents=True, exist_ok=True)
    ((tmp_base_dir / "workspace" / "skills" / ".runtime" / "weather_skill") / "current_version").write_text("1.0.0", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/weather_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            return {"version": "1.0.0", "active_slot": "A", "ready": False}

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "weather_skill: v1.0.0 slot=A" in result.stdout


def test_skill_status_marks_workspace_draft_without_runtime_error(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "infra_access_skill"
    skill_root.mkdir(parents=True, exist_ok=True)

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/infra_access_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise AssertionError("runtime_status should not be called for workspace draft skills")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infra_access_skill: vn/a slot=n/a [draft]" in result.stdout
    assert "runtime-error" not in result.stdout


def test_skill_status_uses_workspace_registry_for_pushed_uninstalled_skill(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "infra_access_skill"
    skill_root.mkdir(parents=True, exist_ok=True)

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/infra_access_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "infra_access_skill", "version": "0.4.0"}],
    )

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise AssertionError("runtime_status should not be called for uninstalled workspace skill")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infra_access_skill: v0.4.0 slot=n/a" in result.stdout
    assert "[draft]" not in result.stdout
    assert "runtime-error" not in result.stdout


def test_skill_status_includes_repo_workspace_fallback_skills(tmp_base_dir, monkeypatch):
    repo_skill = tmp_base_dir / "repo" / ".adaos" / "workspace" / "skills" / "infrastate_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

        def repo_root(self):
            return tmp_base_dir / "repo"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status(".adaos/workspace/skills/infrastate_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise AssertionError("runtime_status should not be called for repo workspace draft skills")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infrastate_skill: vn/a slot=n/a [draft]" in result.stdout


def test_skill_status_marks_runtime_missing_without_runtime_error(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "infra_access_skill"
    skill_root.mkdir(parents=True, exist_ok=True)

    class _Row:
        name = "infra_access_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/infra_access_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise RuntimeError("no versions installed")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infra_access_skill: vn/a slot=n/a [" in result.stdout
    assert "runtime-error" not in result.stdout


def test_skill_status_prefers_workspace_version_over_runtime_version(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '1.1.0'\n", encoding="utf-8")

    class _Row:
        name = "demo_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/demo_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "1.1.0"}],
    )

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            return {"version": "1.0.0", "active_slot": "A", "ready": False}

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "demo_skill: v1.1.0 slot=A" in result.stdout
    assert "runtime-behind" in result.stdout


def test_skill_list_prefers_workspace_version_over_runtime_version(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '1.1.0'\n", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    class _Row:
        name = "demo_skill"
        installed = True
        active_version = "1.0.0"

    class _Mgr:
        @staticmethod
        def list_installed():
            return [_Row()]

    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "1.1.0"}],
    )

    result = CliRunner().invoke(skill_cmd.app, ["list", "--local", "--json"])

    assert result.exit_code == 0
    assert '"name": "demo_skill"' in result.stdout
    assert '"version": "1.1.0"' in result.stdout


def test_skill_list_shows_dirty_flag_and_json_flags(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '1.1.0'\n", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    class _Row:
        name = "demo_skill"
        installed = True
        active_version = "1.0.0"

    class _Mgr:
        @staticmethod
        def list_installed():
            return [_Row()]

    def _dirty_status(**kwargs):
        status = _fake_path_status("skills/demo_skill")
        status.dirty = True
        return status

    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(skill_cmd, "compute_path_status", _dirty_status)
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "1.1.0"}],
    )

    text_result = CliRunner().invoke(skill_cmd.app, ["list", "--local"])
    assert text_result.exit_code == 0
    assert "runtime-behind" in text_result.stdout
    assert "git-dirty" in text_result.stdout

    json_result = CliRunner().invoke(skill_cmd.app, ["list", "--local", "--json"])
    assert json_result.exit_code == 0
    assert '"flags": ["runtime-behind", "git-dirty"]' in json_result.stdout


def test_skill_status_does_not_flag_v_prefixed_equivalent_runtime(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '0.75.6'\n", encoding="utf-8")

    class _Row:
        name = "demo_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            return {"version": "v0.75.6", "active_slot": "A", "ready": True}

    monkeypatch.setattr(skill_cmd, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/demo_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "0.75.6"}],
    )

    result = CliRunner().invoke(skill_cmd.app, ["status", "demo_skill"])

    assert result.exit_code == 0
    assert "runtime status:" not in result.stdout


def test_skill_list_shows_ahead_flag_for_committed_workspace_diff(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '1.1.0'\n", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    class _Row:
        name = "demo_skill"
        installed = True
        active_version = "1.1.0"

    class _Mgr:
        @staticmethod
        def list_installed():
            return [_Row()]

    def _ahead_status(**kwargs):
        status = _fake_path_status("skills/demo_skill")
        status.base_ref = kwargs.get("base_ref")
        status.changed_vs_base = True
        status.ahead_count = 1
        return status

    seen_base_refs: list[str | None] = []

    def _compute_path_status(**kwargs):
        seen_base_refs.append(kwargs.get("base_ref"))
        return _ahead_status(**kwargs)

    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "origin/main")
    monkeypatch.setattr(skill_cmd, "compute_path_status", _compute_path_status)
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "1.1.0"}],
    )

    text_result = CliRunner().invoke(skill_cmd.app, ["list", "--local"])
    assert text_result.exit_code == 0
    assert "[git-ahead]" in text_result.stdout
    assert seen_base_refs == ["origin/main"]

    json_result = CliRunner().invoke(skill_cmd.app, ["list", "--local", "--json"])
    assert json_result.exit_code == 0
    assert '"flags": ["git-ahead"]' in json_result.stdout


def test_skill_status_reports_path_ahead_divergence(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "demo_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text("id: demo_skill\nversion: '1.1.0'\n", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    class _Row:
        name = "demo_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    def _ahead_status(**kwargs):
        status = _fake_path_status("skills/demo_skill")
        status.base_ref = kwargs.get("base_ref")
        status.changed_vs_base = True
        status.ahead_count = 1
        return status

    monkeypatch.setattr(skill_cmd, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: types.SimpleNamespace(runtime_status=lambda _name: {"version": "1.1.0", "active_slot": "B", "ready": True}))
    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "origin/main")
    monkeypatch.setattr(skill_cmd, "compute_path_status", _ahead_status)
    monkeypatch.setattr(
        skill_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "demo_skill", "version": "1.1.0"}],
    )

    result = CliRunner().invoke(skill_cmd.app, ["status", "demo_skill"])

    assert result.exit_code == 0
    assert "git status: git-ahead" in result.stdout
    assert "git divergence: ahead=1 behind=0" in result.stdout


def test_scenario_status_reports_empty_when_registry_and_workspace_are_empty(tmp_path, monkeypatch):
    class _Paths:
        def workspace_dir(self):
            return tmp_path / "workspace"

        def scenarios_workspace_dir(self):
            return tmp_path / "workspace" / "scenarios"

        def dev_scenarios_dir(self):
            return tmp_path / "scenarios-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(scenario_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(scenario_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    result = CliRunner().invoke(scenario_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "No installed scenarios." in result.stdout


def test_scenario_status_prefers_workspace_version_over_registry_row(tmp_path, monkeypatch):
    scenario_root = tmp_path / "workspace" / "scenarios" / "welcome_scene"
    scenario_root.mkdir(parents=True, exist_ok=True)
    (scenario_root / "scenario.yaml").write_text("id: welcome_scene\nversion: '0.2.0'\n", encoding="utf-8")

    class _Row:
        name = "welcome_scene"
        installed = True
        active_version = "0.1.0"

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    class _Paths:
        def workspace_dir(self):
            return tmp_path / "workspace"

        def scenarios_workspace_dir(self):
            return tmp_path / "workspace" / "scenarios"

        def dev_scenarios_dir(self):
            return tmp_path / "scenarios-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(scenario_cmd, "SqliteScenarioRegistry", _Registry)
    monkeypatch.setattr(scenario_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(scenario_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(scenario_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("scenarios/welcome_scene"))
    monkeypatch.setattr(
        scenario_cmd,
        "list_workspace_registry_entries",
        lambda *args, **kwargs: [{"name": "welcome_scene", "version": "0.2.0"}],
    )

    result = CliRunner().invoke(scenario_cmd.app, ["status", "--remote", "registry", "--ref", "HEAD"])

    assert result.exit_code == 0
    assert "welcome_scene: v0.2.0" in result.stdout


def test_scenario_list_falls_back_to_workspace_when_registry_empty(tmp_path, monkeypatch):
    scenario_root = tmp_path / "workspace" / "scenarios" / "infrascope"
    scenario_root.mkdir(parents=True, exist_ok=True)
    (scenario_root / "scenario.yaml").write_text("id: infrascope\nversion: '0.3.0'\n", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_path / "workspace"

        def scenarios_workspace_dir(self):
            return tmp_path / "workspace" / "scenarios"

        def dev_scenarios_dir(self):
            return tmp_path / "scenarios-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(scenario_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def list_installed():
            return []

        @staticmethod
        def list_present():
            return [types.SimpleNamespace(id=types.SimpleNamespace(value="infrascope"), version="0.3.0")]

    monkeypatch.setattr(scenario_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(scenario_cmd.app, ["list"])

    assert result.exit_code == 0
    assert "infrascope" in result.stdout
