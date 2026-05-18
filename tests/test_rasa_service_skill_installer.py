from pathlib import Path
import types

import yaml


def test_ensure_rasa_service_skill_installed_creates_skill_tree():
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.rasa_skill_installer import ensure_rasa_service_skill_installed
    from adaos.services.skill.runtime_env import SkillRuntimeEnvironment

    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    target = skills_root / "rasa_nlu_service_skill"
    assert not target.exists()

    installed = ensure_rasa_service_skill_installed()

    assert installed is not None
    assert installed == target
    assert (target / "skill.yaml").exists()
    assert (target / "requirements.in").exists()
    assert (target / "handlers" / "main.py").exists()
    manifest = yaml.safe_load((target / "skill.yaml").read_text(encoding="utf-8"))
    assert manifest["dependencies"][0] == "--no-deps"
    assert any("rasa-port" in item or "adaos-rasa-nlu" in item for item in manifest["dependencies"])

    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="rasa_nlu_service_skill")
    version = env.resolve_active_version()
    assert version
    slot = env.read_active_slot(version)
    slot_skill = env.build_slot_paths(version, slot).src_dir / "skills" / "rasa_nlu_service_skill"
    assert (slot_skill / "skill.yaml").exists()
    assert (slot_skill / ".adaos-managed.json").exists()


def test_ensure_rasa_service_skill_installed_refreshes_managed_files():
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.rasa_skill_installer import ensure_rasa_service_skill_installed

    ctx = get_ctx()
    target = Path(ctx.paths.skills_dir()) / "rasa_nlu_service_skill"
    (target / "handlers").mkdir(parents=True)
    (target / "skill.yaml").write_text("name: stale\n", encoding="utf-8")
    (target / "handlers" / "main.py").write_text("stale handler\n", encoding="utf-8")
    (target / "custom.txt").write_text("keep me\n", encoding="utf-8")

    installed = ensure_rasa_service_skill_installed()

    assert installed == target
    assert "Local Rasa NLU-only service" in (target / "skill.yaml").read_text(encoding="utf-8")
    assert "AdaOSRasaNLU" in (target / "handlers" / "main.py").read_text(encoding="utf-8")
    assert (target / "requirements.in").exists()
    assert (target / "custom.txt").read_text(encoding="utf-8").strip() == "keep me"


def test_ensure_rasa_service_skill_installed_respects_disabled_flag(monkeypatch):
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu import rasa_skill_installer as installer

    monkeypatch.setenv("ADAOS_NLU_RASA", "0")
    monkeypatch.setattr(
        installer,
        "_ensure_rasa_port_submodule_checkout",
        lambda ctx: (_ for _ in ()).throw(AssertionError("disabled Rasa must not touch rasa-port")),
    )
    ctx = get_ctx()
    target = Path(ctx.paths.skills_dir()) / "rasa_nlu_service_skill"

    assert installer.ensure_rasa_service_skill_installed() is None
    assert not target.exists()


def test_rasa_port_dependency_initializes_declared_submodule(monkeypatch, tmp_path):
    from adaos.services.nlu import rasa_skill_installer as installer

    repo = tmp_path / "repo"
    submodule = repo / "src" / "adaos" / "integrations" / "rasa-port"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitmodules").write_text(
        """
[submodule "src/adaos/integrations/rasa-port"]
    path = src/adaos/integrations/rasa-port
    url = https://github.com/stipot-com/rasa-port.git
""".strip(),
        encoding="utf-8",
    )
    package_dir = repo / "src" / "adaos"
    package_dir.mkdir(parents=True)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "status" in cmd:
            return types.SimpleNamespace(
                returncode=0,
                stdout="-7352542dcb3b484d7787ec70447d9240e83ce092 src/adaos/integrations/rasa-port\n",
                stderr="",
            )
        (submodule / "adaos_rasa_nlu").mkdir(parents=True)
        (submodule / "rasa").mkdir()
        (submodule / "pyproject.toml").write_text("[project]\nname = 'adaos-rasa-nlu'\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "git" if name == "git" else None)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    ctx = types.SimpleNamespace(paths=types.SimpleNamespace(package_dir=package_dir, repo_root=lambda: repo))

    deps = installer._rasa_port_dependency_args(ctx)

    assert calls
    cmd = next(call[0] for call in calls if "update" in call[0])
    assert cmd[:4] == ["git", "-C", str(repo), "submodule"]
    assert cmd[-1] == "src/adaos/integrations/rasa-port"
    assert deps[:2] == ["--no-deps", "-e"]
    assert deps[2].startswith("file:")
    assert "rasa-port" in deps[2]


def test_rasa_port_dependency_falls_back_to_git_requirement(monkeypatch):
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu import rasa_skill_installer as installer

    monkeypatch.setattr(installer, "_ensure_rasa_port_submodule_checkout", lambda ctx: None)
    deps = installer._rasa_port_dependency_args(get_ctx())

    assert deps == [
        "--no-deps",
        "adaos-rasa-nlu @ git+https://github.com/stipot-com/rasa-port.git@main",
    ]


def test_interpreter_cli_rasa_service_url_bootstraps_template_without_starting(monkeypatch):
    import importlib
    import sys
    import types

    from adaos.services.agent_context import get_ctx

    ctx = get_ctx()
    target = Path(ctx.paths.skills_dir()) / "rasa_nlu_service_skill"
    assert not target.exists()

    bootstrap_stub = types.ModuleType("adaos.apps.bootstrap")
    bootstrap_stub.get_ctx = get_ctx
    monkeypatch.setitem(sys.modules, "adaos.apps.bootstrap", bootstrap_stub)
    interpreter_cli = importlib.import_module("adaos.apps.cli.commands.interpreter")

    assert interpreter_cli._rasa_service_url(start=False) == "http://127.0.0.1:18092"
    assert (target / "skill.yaml").exists()


def test_interpreter_cli_rasa_service_url_reuses_healthy_service(monkeypatch):
    import importlib
    import sys
    import types

    from adaos.services.agent_context import get_ctx

    bootstrap_stub = types.ModuleType("adaos.apps.bootstrap")
    bootstrap_stub.get_ctx = get_ctx
    monkeypatch.setitem(sys.modules, "adaos.apps.bootstrap", bootstrap_stub)
    interpreter_cli = importlib.import_module("adaos.apps.cli.commands.interpreter")

    monkeypatch.setattr(
        interpreter_cli,
        "_http_get_json",
        lambda url, *, timeout_ms=1000: {"ok": True} if url.endswith("/health") else None,
    )

    class Supervisor:
        async def refresh_discovered(self, force=False):
            raise AssertionError("healthy Rasa service should be reused")

        async def start(self, name):
            raise AssertionError("healthy Rasa service should not be restarted")

        def resolve_base_url(self, name):
            return None

    monkeypatch.setattr(interpreter_cli, "get_service_supervisor", lambda: Supervisor())

    assert interpreter_cli._rasa_service_url(start=True) == "http://127.0.0.1:18092"


def test_interpreter_cli_rasa_train_records_successful_service_training(monkeypatch, tmp_path):
    import importlib
    import sys
    import types

    from adaos.services.agent_context import get_ctx

    bootstrap_stub = types.ModuleType("adaos.apps.bootstrap")
    bootstrap_stub.get_ctx = get_ctx
    monkeypatch.setitem(sys.modules, "adaos.apps.bootstrap", bootstrap_stub)
    interpreter_cli = importlib.import_module("adaos.apps.cli.commands.interpreter")

    records = []

    class Workspace:
        def build_rasa_project(self):
            return tmp_path / "rasa_project"

        def record_training(self, *, note=None, extra=None):
            records.append({"note": note, "extra": extra})
            return {"trained_at": "now"}

    monkeypatch.setattr(interpreter_cli, "_workspace", lambda: Workspace())
    monkeypatch.setattr(interpreter_cli, "sync_from_scenarios_and_skills", lambda ctx: None)
    monkeypatch.setattr(interpreter_cli, "_rasa_service_url", lambda: "http://127.0.0.1:18092")
    monkeypatch.setattr(
        interpreter_cli,
        "_http_post_json",
        lambda url, payload, *, timeout_ms: {"ok": True, "model_path": str(tmp_path / "interpreter_latest.tar.gz")},
    )

    interpreter_cli.train(note="smoke", dry_run=False, engine="rasa")

    assert records == [
        {
            "note": "smoke",
            "extra": {
                "engine": "rasa_service",
                "model_path": str(tmp_path / "interpreter_latest.tar.gz"),
            },
        }
    ]


def test_setup_install_prepares_rasa_before_post_install_training(monkeypatch, tmp_path):
    from adaos.apps.cli.commands import setup

    calls = []

    async def _train_once(*, reason="manual", note=None):
        calls.append(("train", reason, note))
        return {"ok": True, "response": {"model_path": str(tmp_path / "model.tar.gz")}}

    monkeypatch.setattr(
        setup,
        "ensure_rasa_service_skill_installed",
        lambda: calls.append(("ensure",)) or (tmp_path / "rasa_nlu_service_skill"),
    )
    monkeypatch.setattr(setup, "train_rasa_nlu_once", _train_once)

    installed = {"warnings": []}

    setup._bootstrap_rasa_nlu_after_install(installed, enabled=True, train=True)

    assert calls == [("ensure",), ("train", "post-install", "rasa-post-install")]
    assert installed["nlu"]["rasa"]["ok"] is True
    assert installed["warnings"] == []
