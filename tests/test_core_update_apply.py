from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path


def test_checkout_target_version_ignores_non_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(mod, "_run", lambda cmd, cwd=None: calls.append(list(cmd)))

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="2026.3.1")
    assert calls == []


def test_checkout_target_version_checks_out_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="a" * 12)
    assert calls == [["git", "checkout", "aaaaaaaaaaaa"]]


def test_checkout_target_version_fetches_then_retries(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    state = {"attempt": 0}

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "checkout"] and state["attempt"] == 0:
            state["attempt"] += 1
            raise RuntimeError("missing commit in shallow clone")

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="b" * 12)
    assert calls == [
        ["git", "checkout", "bbbbbbbbbbbb"],
        ["git", "fetch", "--depth", "50", "origin", "main"],
        ["git", "checkout", "bbbbbbbbbbbb"],
    ]


def test_repair_moved_venv_rewrites_script_paths(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    original_venv = tmp_path / "tmp-build" / "B" / "venv"
    final_venv = tmp_path / "slots" / "B" / "venv"
    scripts_dir_name = "Scripts" if mod.os.name == "nt" else "bin"
    python_name = "python.exe" if mod.os.name == "nt" else "python"
    scripts = final_venv / scripts_dir_name
    scripts.mkdir(parents=True, exist_ok=True)
    old_python = original_venv / scripts_dir_name / python_name
    pip_script = scripts / "pip"
    activate_script = scripts / "activate"
    pip_script.write_text(f"#!{old_python}\nprint('ok')\n", encoding="utf-8")
    activate_script.write_text(f"VIRTUAL_ENV=\"{original_venv}\"\n", encoding="utf-8")

    result = mod._repair_moved_venv(final_venv, original_venv_dir=original_venv)

    assert result["ok"] is True
    assert str(pip_script) in result["repaired_files"]
    assert str(activate_script) in result["repaired_files"]
    assert pip_script.read_text(encoding="utf-8").startswith(f"#!{final_venv / scripts_dir_name / python_name}")
    assert str(final_venv) in activate_script.read_text(encoding="utf-8")


def test_repair_moved_venv_preserves_windows_crlf_in_pyvenv_cfg(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    original_venv = tmp_path / "tmp-build" / "B" / "venv"
    final_venv = tmp_path / "slots" / "B" / "venv"
    final_venv.mkdir(parents=True, exist_ok=True)
    pyvenv_cfg = final_venv / "pyvenv.cfg"
    pyvenv_cfg.write_bytes(
        (
            f"home = {original_venv}\r\n"
            "include-system-site-packages = false\r\n"
            "version = 3.11.15\r\n"
        ).encode("utf-8")
    )

    result = mod._repair_moved_venv(final_venv, original_venv_dir=original_venv)

    assert result["ok"] is True
    assert str(pyvenv_cfg) in result["repaired_files"]
    raw = pyvenv_cfg.read_bytes()
    assert str(final_venv).encode("utf-8") in raw
    assert b"\r\r\n" not in raw
    assert raw.count(b"\r\n") == 3


def test_replace_slot_dir_refuses_nested_move_when_cleanup_leaves_destination(
    monkeypatch, tmp_path: Path
) -> None:
    import adaos.apps.core_update_apply as mod

    prepared_slot = tmp_path / "tmp-build" / "A"
    slot_dir = tmp_path / "slots" / "A"
    prepared_slot.mkdir(parents=True, exist_ok=True)
    slot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod.shutil, "rmtree", lambda *_args, **_kwargs: None)

    try:
        mod._replace_slot_dir(prepared_slot, slot_dir)
        assert False, "expected RuntimeError when destination survives cleanup"
    except RuntimeError as exc:
        assert "refusing nested move" in str(exc)


def test_replace_slot_dir_removes_existing_partial_slot_dir(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    prepared_slot = tmp_path / "tmp-build" / "A"
    slot_dir = tmp_path / "slots" / "A"
    (prepared_slot / "repo").mkdir(parents=True, exist_ok=True)
    (prepared_slot / "repo" / "manifest.txt").write_text("fresh", encoding="utf-8")
    (slot_dir / "repo" / ".git").mkdir(parents=True, exist_ok=True)
    stale_file = slot_dir / "repo" / ".git" / "pack.idx"
    stale_file.write_text("stale", encoding="utf-8")

    mod._replace_slot_dir(prepared_slot, slot_dir)

    assert slot_dir.exists() is True
    assert (slot_dir / "repo" / "manifest.txt").read_text(encoding="utf-8") == "fresh"
    assert stale_file.exists() is False


def test_cleanup_stale_temp_slot_dirs_removes_only_old_temp_dirs(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    slots_root = tmp_path / "slots"
    slots_root.mkdir(parents=True, exist_ok=True)
    keep_regular = slots_root / "A"
    keep_regular.mkdir()
    stale = slots_root / "adaos-core-a-stale123"
    recent = slots_root / "adaos-core-b-recent123"
    stale.mkdir()
    recent.mkdir()
    old_now = 1_700_000_000.0
    os.utime(stale, (old_now - 900.0, old_now - 900.0))
    os.utime(recent, (old_now - 60.0, old_now - 60.0))

    result = mod._cleanup_stale_temp_slot_dirs(
        slots_root,
        min_age_seconds=300.0,
        now=old_now,
    )

    assert result["ok"] is True
    assert result["removed_total"] == 1
    assert str(stale) in result["removed_paths"]
    assert stale.exists() is False
    assert recent.exists() is True
    assert keep_regular.exists() is True
    assert result["skipped_recent_total"] == 1
    assert str(recent) in result["skipped_recent_paths"]


def test_migrate_installed_skill_runtimes_uses_target_python(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}
    repo_root = tmp_path / "repo"
    migrate_script = repo_root / "src" / "adaos" / "apps" / "skill_runtime_migrate.py"
    migrate_script.parent.mkdir(parents=True, exist_ok=True)
    migrate_script.write_text("print('ok')\n", encoding="utf-8")

    def _fake_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "skills": []}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    payload = mod._migrate_installed_skill_runtimes(
        tmp_path / "venv" / "bin" / "python",
        repo_root=repo_root,
        base_dir="/tmp/adaos-base",
        shared_dotenv_path="/tmp/adaos.env",
        run_tests=True,
    )

    assert payload["ok"] is True
    assert captured["cmd"] == [
        str(tmp_path / "venv" / "bin" / "python"),
        str(migrate_script),
        "--json",
    ]
    assert captured["cwd"] == str(repo_root)
    assert captured["env"]["ADAOS_BASE_DIR"] == "/tmp/adaos-base"
    assert captured["env"]["ADAOS_SHARED_DOTENV_PATH"] == "/tmp/adaos.env"
    assert captured["env"]["ADAOS_SLOT_REPO_ROOT"] == str(repo_root)
    assert captured["env"]["PYTHONPATH"].split(mod.os.pathsep)[0] == str(repo_root / "src")


def test_migrate_installed_skill_runtimes_can_skip_tests(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}
    repo_root = tmp_path / "repo"
    migrate_script = repo_root / "src" / "adaos" / "apps" / "skill_runtime_migrate.py"
    migrate_script.parent.mkdir(parents=True, exist_ok=True)
    migrate_script.write_text("print('ok')\n", encoding="utf-8")

    def _fake_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "skills": []}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    mod._migrate_installed_skill_runtimes(
        tmp_path / "venv" / "bin" / "python",
        repo_root=repo_root,
        run_tests=False,
    )

    assert captured["cmd"] == [
        str(tmp_path / "venv" / "bin" / "python"),
        str(migrate_script),
        "--json",
        "--skip-tests",
    ]
    assert captured["cwd"] == str(repo_root)
    assert captured["env"]["PYTHONPATH"].split(mod.os.pathsep)[0] == str(repo_root / "src")


def test_migrate_installed_skill_runtimes_reports_missing_script_in_prepared_repo(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    repo_root = tmp_path / "repo"
    apps_dir = repo_root / "src" / "adaos" / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / "autostart_runner.py").write_text("print('ok')\n", encoding="utf-8")

    payload = mod._migrate_installed_skill_runtimes(
        tmp_path / "venv" / "bin" / "python",
        repo_root=repo_root,
        run_tests=True,
    )

    assert payload["ok"] is True
    assert payload["skipped"] is True
    assert payload["unsupported"] is True
    assert payload["reason"] == "missing_skill_runtime_migration_entrypoint"
    assert payload["apps_dir_exists"] is True
    assert "autostart_runner.py" in payload["visible_files"]
    assert payload["deferred"] is False
    assert payload["skills"] == []


def test_validate_prepared_slot_imports_checks_installed_package_without_pythonpath(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}

    def _fake_run(cmd, *, capture_output=None, text=None, timeout=None, env=None):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"ok": True, "modules": ["adaos.services.runtime_refresh"]}),
            stderr="",
        )

    monkeypatch.setenv("PYTHONPATH", "/tmp/source-overlay")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    payload = mod._validate_prepared_slot_imports(tmp_path / "venv" / "bin" / "python")

    assert payload["ok"] is True
    assert captured["cmd"][1] == "-c"
    assert "PYTHONPATH" not in captured["env"]


def test_strip_repo_vcs_metadata_removes_git_dir(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    repo_dir = tmp_path / "repo"
    git_dir = repo_dir / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text("[remote \"origin\"]\nurl = /tmp/source\n", encoding="utf-8")

    mod._strip_repo_vcs_metadata(repo_dir)

    assert not git_dir.exists()


def test_clone_local_repo_copy_mode_skips_git_metadata(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    source_repo = tmp_path / "source"
    checkout_dir = tmp_path / "checkout"
    (source_repo / ".git").mkdir(parents=True, exist_ok=True)
    (source_repo / "src").mkdir(parents=True, exist_ok=True)
    (source_repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    mod._clone_local_repo(source_repo, target_rev="rev2026", target_version="1.2.3", checkout_dir=checkout_dir)

    assert (checkout_dir / "src" / "app.py").exists()
    assert not (checkout_dir / ".git").exists()


def test_clone_local_repo_copy_mode_when_worktree_dirty_and_target_unpinned(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    source_repo = tmp_path / "source"
    checkout_dir = tmp_path / "checkout"
    (source_repo / ".git").mkdir(parents=True, exist_ok=True)
    (source_repo / "src").mkdir(parents=True, exist_ok=True)
    (source_repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    def _fake_run(*_args, **_kwargs):
        raise AssertionError("git clone path should not run for dirty unpinned worktree")

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(mod, "_git_worktree_has_changes", lambda _path: True)
    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._clone_local_repo(source_repo, target_rev="", target_version="1.2.3", checkout_dir=checkout_dir)

    assert (checkout_dir / "src" / "app.py").exists()
    assert not (checkout_dir / ".git").exists()


def test_validate_checkout_target_version_rejects_mismatched_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    monkeypatch.setattr(mod, "_git_text", lambda *_args: "d7d79d5d08eb12446a4f7bf6069246368df6d4d0")

    try:
        mod._validate_checkout_target_version(
            tmp_path,
            target_version="f7d14e92e38bb6b37f9068c2ee894de61710b92e",
            source_label="local source repo",
        )
        assert False, "expected RuntimeError on git commit mismatch"
    except RuntimeError as exc:
        text = str(exc)
        assert "local source repo" in text
        assert "d7d79d5d08eb12446a4f7bf6069246368df6d4d0" in text
        assert "f7d14e92e38bb6b37f9068c2ee894de61710b92e" in text


def test_prepare_checkout_repo_falls_back_to_remote_when_local_source_misses_target_sha(
    monkeypatch, tmp_path: Path
) -> None:
    import adaos.apps.core_update_apply as mod

    target_version = "f7d14e92e38bb6b37f9068c2ee894de61710b92e"
    source_repo = tmp_path / "source"
    checkout_dir = tmp_path / "checkout"
    (source_repo / ".git").mkdir(parents=True, exist_ok=True)

    clone_calls: list[str] = []
    validate_calls: list[str] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    def _fake_clone_local(_source_repo_root, _target_rev, _target_version, target_dir):
        clone_calls.append("local")
        target_dir.mkdir(parents=True, exist_ok=True)

    def _fake_clone_repo(_repo_url, _target_rev, _target_version, target_dir):
        clone_calls.append("remote")
        target_dir.mkdir(parents=True, exist_ok=True)

    def _fake_validate(_repo_dir, *, target_version: str, source_label: str):
        validate_calls.append(source_label)
        if source_label == "local source repo":
            raise RuntimeError(f"resolved to d7d79d5 instead of {target_version}")

    monkeypatch.setattr(mod, "_clone_local_repo", _fake_clone_local)
    monkeypatch.setattr(mod, "_clone_repo", _fake_clone_repo)
    monkeypatch.setattr(mod, "_validate_checkout_target_version", _fake_validate)

    source_kind = mod._prepare_checkout_repo(
        checkout_dir=checkout_dir,
        source_repo_dir=source_repo,
        repo_url="https://github.com/stipot-com/adaos.git",
        target_rev="rev2026",
        target_version=target_version,
    )

    assert source_kind == "remote_git_clone"
    assert clone_calls == ["local", "remote"]
    assert validate_calls == ["local source repo", "remote repo clone"]


def test_prepare_slot_preserves_explicit_empty_repo_url(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}

    def _fake_prepare_checkout_repo(**kwargs):
        captured.update(kwargs)
        checkout_dir = Path(kwargs["checkout_dir"])
        apps_dir = checkout_dir / "src" / "adaos" / "apps"
        apps_dir.mkdir(parents=True, exist_ok=True)
        (apps_dir / "__init__.py").write_text("", encoding="utf-8")
        return "local_source_tree"

    def _fake_run(_cmd, *, cwd=None):
        if cwd is None:
            return

    monkeypatch.setattr(mod, "_prepare_checkout_repo", _fake_prepare_checkout_repo)
    monkeypatch.setattr(mod, "_run", _fake_run)
    monkeypatch.setattr(mod, "_strip_repo_vcs_metadata", lambda _repo_dir: None)
    monkeypatch.setattr(mod, "_replace_slot_dir", lambda prepared_slot, slot_dir: shutil.move(str(prepared_slot), str(slot_dir)))
    monkeypatch.setattr(mod, "_repair_moved_venv", lambda _venv_dir, original_venv_dir=None: {"ok": True, "repaired_files": []})
    monkeypatch.setattr(mod, "_validate_prepared_slot_imports", lambda _python_bin: {"ok": True, "modules": []})
    monkeypatch.setattr(mod, "_migrate_installed_skill_runtimes", lambda *args, **kwargs: {"ok": True, "skills": []})
    monkeypatch.setattr(mod, "_git_text", lambda *_args: "value")
    monkeypatch.setattr(mod, "_detect_bootstrap_promotion_requirement", lambda *_args, **_kwargs: {"required": False, "changed_paths": []})
    cleanup_calls: list[tuple[str, float]] = []

    def _fake_cleanup(slots_root: Path, *, min_age_seconds: float = 300.0, now=None):
        cleanup_calls.append((str(slots_root), float(min_age_seconds)))
        return {"ok": True, "removed_total": 0}

    monkeypatch.setattr(mod, "_cleanup_stale_temp_slot_dirs", _fake_cleanup)

    slot_dir = tmp_path / "slots" / "A"
    manifest = mod.prepare_slot(
        slot="A",
        slot_dir_path=str(slot_dir),
        base_dir=str(tmp_path / "base"),
        repo_root=str(tmp_path / "repo-root"),
        source_repo_root=str(tmp_path / "source"),
        repo_url="",
        migrate_skill_runtimes=False,
    )

    assert manifest["slot"] == "A"
    assert captured["repo_url"] == ""
    assert cleanup_calls == [(str(slot_dir.parent.resolve()), 300.0)]


def test_detect_bootstrap_promotion_requirement_reports_changed_paths(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    current_root = tmp_path / "root"
    candidate = tmp_path / "candidate"
    for base in (current_root, candidate):
        (base / "src" / "adaos" / "apps").mkdir(parents=True, exist_ok=True)
        (base / "src" / "adaos" / "services").mkdir(parents=True, exist_ok=True)
    (current_root / "src" / "adaos" / "apps" / "supervisor.py").write_text("old\n", encoding="utf-8")
    (candidate / "src" / "adaos" / "apps" / "supervisor.py").write_text("new\n", encoding="utf-8")

    payload = mod._detect_bootstrap_promotion_requirement(candidate, current_root)

    assert payload["required"] is True
    assert "src/adaos/apps/supervisor.py" in payload["changed_paths"]


def test_bootstrap_critical_paths_are_shared_with_core_update_service() -> None:
    import adaos.apps.core_update_apply as apply_mod
    import adaos.services.core_update as core_mod
    from adaos.services.bootstrap_update import BOOTSTRAP_CRITICAL_PATHS

    assert apply_mod.BOOTSTRAP_CRITICAL_PATHS is BOOTSTRAP_CRITICAL_PATHS
    assert core_mod.BOOTSTRAP_CRITICAL_PATHS is BOOTSTRAP_CRITICAL_PATHS


def _top_level_adaos_import_paths(module_path: str) -> set[str]:
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    path = repo_root / module_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_paths: set[str] = set()
    for node in tree.body:
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = [node.module or ""]
        for module in modules:
            if not module.startswith("adaos."):
                continue
            module_root = src_root / Path(*module.split("."))
            candidates = (module_root.with_suffix(".py"), module_root / "__init__.py")
            for candidate in candidates:
                if candidate.exists():
                    imported_paths.add(candidate.relative_to(repo_root).as_posix())
                    break
    return imported_paths


def test_bootstrap_critical_paths_cover_supervisor_and_sidecar_import_surface() -> None:
    from adaos.services.bootstrap_update import BOOTSTRAP_CRITICAL_PATHS, SIDECAR_CONTROLLED_PATHS

    critical = set(BOOTSTRAP_CRITICAL_PATHS)
    supervisor_paths = {
        "src/adaos/apps/supervisor.py",
        *_top_level_adaos_import_paths("src/adaos/apps/supervisor.py"),
    }
    sidecar_paths = {
        "src/adaos/services/realtime_sidecar.py",
        *_top_level_adaos_import_paths("src/adaos/services/realtime_sidecar.py"),
    }

    assert not sorted((supervisor_paths | sidecar_paths) - critical)
    assert not sorted(sidecar_paths - set(SIDECAR_CONTROLLED_PATHS))


def test_bootstrap_critical_paths_include_runtime_projection_helpers() -> None:
    from adaos.services.bootstrap_update import BOOTSTRAP_CRITICAL_PATHS

    critical = set(BOOTSTRAP_CRITICAL_PATHS)

    assert "src/adaos/services/core_update_policy.py" in critical
    assert "src/adaos/services/runtime_refresh.py" in critical
    assert "src/adaos/services/node_display.py" in critical
    assert "src/adaos/services/node_runtime_state.py" in critical
    assert "src/adaos/services/scenario/webspace_runtime.py" in critical
    assert "src/adaos/services/subnet/link_client.py" in critical
    assert "src/adaos/services/subnet/link_manager.py" in critical

