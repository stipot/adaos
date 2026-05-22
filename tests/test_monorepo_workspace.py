from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from adaos.adapters.git import cli_git as cli_git_module
from adaos.adapters.git.cli_git import CliGitClient, _dirty_paths_covered_by_sparse_request
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.skills.git_repo import GitSkillRepository


class _MiniPaths:
    def __init__(self, base: Path) -> None:
        self._base = Path(base)
        self._workspace = self._base / "workspace"
        self._skills = self._workspace / "skills"
        self._scenarios = self._workspace / "scenarios"

    def base_dir(self) -> Path:
        return self._base

    def workspace_dir(self) -> Path:
        return self._workspace

    def skills_dir(self) -> Path:
        return self._skills

    def scenarios_dir(self) -> Path:
        return self._scenarios


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run([
        "git",
        *args,
    ], cwd=str(cwd), check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_monorepo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir()
    _run_git(["init"], cwd=remote)
    _run_git(["config", "user.email", "adaos-tests@example.com"], cwd=remote)
    _run_git(["config", "user.name", "AdaOS Tests"], cwd=remote)

    skills = remote / "skills"
    weather = skills / "weather_skill"
    news = skills / "news_skill"
    alias = skills / "alias_skill"
    scenarios = remote / "scenarios"
    greet = scenarios / "greet_on_boot"
    vision = scenarios / "new_face_vision"
    for path in (weather, news, alias, greet, vision):
        path.mkdir(parents=True, exist_ok=True)

    (weather / "skill.yaml").write_text(
        "id: weather_skill\nname: Weather\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (news / "skill.yaml").write_text(
        "id: news_skill\nname: News\nversion: '2.0.0'\n",
        encoding="utf-8",
    )
    (alias / "skill.yaml").write_text(
        "id: alias_skill_id\nname: Alias Skill\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (greet / "scenario.yaml").write_text(
        "id: greet_on_boot\nname: Greet on boot\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (vision / "scenario.yaml").write_text(
        "id: new_face_vision_scenario\nname: New Face Vision\nversion: '0.2.0'\n",
        encoding="utf-8",
    )
    (remote / "registry.json").write_text(
        (
            "{\n"
            '  "version": 1,\n'
            '  "updated_at": "2026-03-06T00:00:00+00:00",\n'
            '  "skills": [\n'
            '    {"kind": "skill", "id": "weather_skill", "name": "weather_skill", "version": "1.0.0"},\n'
            '    {"kind": "skill", "id": "news_skill", "name": "news_skill", "version": "2.0.0"},\n'
            '    {"kind": "skill", "id": "alias_skill_id", "name": "alias_skill", "version": "1.0.0"}\n'
            '  ],\n'
            '  "scenarios": [\n'
            '    {"kind": "scenario", "id": "greet_on_boot", "name": "greet_on_boot", "version": "1.0.0"},\n'
            '    {"kind": "scenario", "id": "new_face_vision_scenario", "name": "new_face_vision", "version": "0.2.0"}\n'
            '  ]\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    _run_git(["add", "-A"], cwd=remote)
    _run_git(["commit", "-m", "seed workspace"], cwd=remote)
    return remote


def _make_paths(tmp_path: Path) -> _MiniPaths:
    base = tmp_path / ".adaos"
    paths = _MiniPaths(base)
    paths.workspace_dir().mkdir(parents=True, exist_ok=True)
    return paths


def _make_skill_repo(paths: _MiniPaths, remote: Path) -> GitSkillRepository:
    git = CliGitClient(depth=0)
    return GitSkillRepository(paths=paths, git=git, monorepo_url=str(remote))


def _make_scenario_repo(paths: _MiniPaths, remote: Path) -> GitScenarioRepository:
    git = CliGitClient(depth=0)
    return GitScenarioRepository(paths=paths, git=git, url=str(remote))


@pytest.fixture
def monorepo(tmp_path) -> Path:
    return _init_monorepo(tmp_path)


@pytest.fixture
def paths(tmp_path) -> TestPaths:
    return _make_paths(tmp_path)


def _git_status_clean(workspace: Path) -> bool:
    try:
        out = _run_git(["status", "--porcelain"], cwd=workspace)
    except subprocess.CalledProcessError:
        return False
    return out.strip() == ""


def test_skill_reinstall_happy_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    meta1 = repo.install("weather_skill")
    assert meta1.id.value == "weather_skill"
    repo.uninstall("weather_skill")
    assert not (paths.skills_dir() / "weather_skill").exists()

    meta2 = repo.install("weather_skill")
    assert meta2.id.value == "weather_skill"
    assert (paths.skills_dir() / "weather_skill").exists()
    assert (paths.workspace_dir() / "registry.json").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_scenario_reinstall_happy_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_scenario_repo(paths, monorepo)

    meta1 = repo.install("greet_on_boot")
    assert meta1.id.value == "greet_on_boot"
    repo.uninstall("greet_on_boot")
    assert not (paths.scenarios_dir() / "greet_on_boot").exists()

    meta2 = repo.install("greet_on_boot")
    assert meta2.id.value == "greet_on_boot"
    assert (paths.scenarios_dir() / "greet_on_boot").exists()
    assert (paths.workspace_dir() / "registry.json").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_skill_install_resolves_registry_id_to_workspace_name(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    meta = repo.install("alias_skill_id")

    assert meta.id.value == "alias_skill_id"
    assert (paths.skills_dir() / "alias_skill" / "skill.yaml").exists()
    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    contents = sparse_file.read_text(encoding="utf-8")
    assert "skills/alias_skill" in contents
    assert "skills/alias_skill_id" not in contents


def test_scenario_install_resolves_registry_id_to_workspace_name(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_scenario_repo(paths, monorepo)

    meta = repo.install("new_face_vision_scenario")

    assert meta.id.value == "new_face_vision_scenario"
    assert (paths.scenarios_dir() / "new_face_vision" / "scenario.yaml").exists()
    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    contents = sparse_file.read_text(encoding="utf-8")
    assert "scenarios/new_face_vision" in contents
    assert "scenarios/new_face_vision_scenario" not in contents


def test_uninstall_idempotent(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    repo.install("weather_skill")
    repo.uninstall("weather_skill")
    # second uninstall should be a no-op
    repo.uninstall("weather_skill")
    assert _git_status_clean(paths.workspace_dir())


def test_install_missing_remote_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    with pytest.raises(FileNotFoundError) as ei:
        repo.install("missing_skill")
    assert "workspace registry.json" in str(ei.value)
    assert "weather_skill" in str(ei.value)

    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    if sparse_file.exists():
        assert "skills/missing_skill" not in sparse_file.read_text(encoding="utf-8")
    assert not (paths.skills_dir() / "missing_skill").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_sparse_checkout_scope(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    skill_repo = _make_skill_repo(paths, monorepo)
    scenario_repo = _make_scenario_repo(paths, monorepo)

    skill_repo.install("weather_skill")
    scenario_repo.install("greet_on_boot")

    skills_present = {
        child.name for child in paths.skills_dir().iterdir() if child.is_dir()
    }
    assert skills_present == {"weather_skill"}
    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    assert "registry.json" in sparse_file.read_text(encoding="utf-8")
    assert "skills/weather_skill" in sparse_file.read_text(encoding="utf-8")
    assert "skills/news_skill" not in sparse_file.read_text(encoding="utf-8")

    skill_repo.uninstall("weather_skill")
    assert not (paths.skills_dir() / "weather_skill").exists()
    assert "skills/weather_skill" not in sparse_file.read_text(encoding="utf-8")
    assert "registry.json" in sparse_file.read_text(encoding="utf-8")
    # scenario entry should remain in sparse checkout
    assert "scenarios/greet_on_boot" in sparse_file.read_text(encoding="utf-8")
    assert _git_status_clean(paths.workspace_dir())


def test_sparse_set_auto_stashes_dirty_worktree(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    repo.install("weather_skill")
    shutil.rmtree(paths.skills_dir() / "weather_skill")
    assert not _git_status_clean(paths.workspace_dir())

    meta = repo.install("news_skill")
    assert meta.id.value == "news_skill"
    assert (paths.skills_dir() / "news_skill" / "skill.yaml").exists()
    assert _git_status_clean(paths.workspace_dir())

    stashes = _run_git(["stash", "list"], cwd=paths.workspace_dir())
    assert "adaos:auto-stash sparse-checkout" in stashes


def test_sparse_set_preserves_dirty_files_inside_requested_scope(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)
    repo.install("weather_skill")

    skill_yaml = paths.skills_dir() / "weather_skill" / "skill.yaml"
    skill_yaml.write_text(
        skill_yaml.read_text(encoding="utf-8") + "description: local edit\n",
        encoding="utf-8",
    )
    assert not _git_status_clean(paths.workspace_dir())
    dirty = CliGitClient().changed_files(paths.workspace_dir())
    requested = ["registry.json", "skills/weather_skill"]
    assert _dirty_paths_covered_by_sparse_request(dirty, requested)

    CliGitClient().sparse_set(
        paths.workspace_dir(),
        requested,
        no_cone=True,
    )

    assert "description: local edit" in skill_yaml.read_text(encoding="utf-8")
    stashes = _run_git(["stash", "list"], cwd=paths.workspace_dir())
    assert "adaos:auto-stash sparse-checkout" not in stashes
    assert not _git_status_clean(paths.workspace_dir())


def test_sparse_set_restores_staged_deletion_only_workspace_in_prod(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_TYPE", "prod")
    root = tmp_path / ".adaos" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    deletion_storm = "\n".join(f"D  skills/old_skill_{idx}/skill.yaml" for idx in range(25)) + "\n"
    status_responses = iter(
        [
            deletion_storm,
            deletion_storm,
            "",
        ]
    )
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None):
        calls.append(list(args))
        if args[:2] == ["status", "--porcelain"]:
            return next(status_responses)
        if args == ["reset", "--hard", "HEAD"]:
            return "HEAD is now at abc123 seed"
        if args[:2] == ["sparse-checkout", "set"]:
            return ""
        raise AssertionError(f"unexpected git call: {args!r}")

    monkeypatch.setattr(cli_git_module, "_run_git", fake_run_git)

    CliGitClient(depth=0).sparse_set(str(root), ["registry.json", "scenarios/new_face"], no_cone=True)

    assert ["reset", "--hard", "HEAD"] in calls
    assert not any(call[:2] == ["stash", "push"] for call in calls)


def test_sparse_set_removes_stale_blocker_in_non_dev(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_TYPE", "prod")
    root = tmp_path / ".adaos" / "workspace"
    blocker_one = root / "skills" / "news_skill" / "skill.yaml"
    blocker_two = root / "skills" / "news_skill" / "handlers" / "__init__.py"
    for blocker in (blocker_one, blocker_two):
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("local stale file\n", encoding="utf-8")
    attempts = 0

    def fake_run_git(args, cwd=None):
        nonlocal attempts
        if args[:2] == ["status", "--porcelain"]:
            return ""
        if args[:2] == ["sparse-checkout", "set"]:
            attempts += 1
            if attempts == 1:
                raise cli_git_module.GitError(
                    "git sparse-checkout set failed: error: Working tree file "
                    "'skills/news_skill/skill.yaml' would be overwritten by sparse checkout update."
                )
            if attempts == 2:
                raise cli_git_module.GitError(
                    "git sparse-checkout set failed: error: Working tree file "
                    "'skills/news_skill/handlers/__init__.py' would be overwritten by sparse checkout update."
                )
            return ""
        if args and args[0] in {"status", "log"}:
            return ""
        raise AssertionError(f"unexpected git call: {args!r}")

    monkeypatch.setattr(cli_git_module, "_run_git", fake_run_git)

    CliGitClient(depth=0).sparse_set(str(root), ["skills/news_skill"], no_cone=True)

    assert attempts == 3
    assert not blocker_one.exists()
    assert not blocker_two.exists()


def test_sparse_set_limits_stale_blocker_recovery(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_TYPE", "prod")
    monkeypatch.setenv("ADAOS_SPARSE_CHECKOUT_BLOCKER_RETRIES", "1")
    root = tmp_path / ".adaos" / "workspace"
    blocker_one = root / "skills" / "news_skill" / "skill.yaml"
    blocker_two = root / "skills" / "news_skill" / "handlers" / "__init__.py"
    for blocker in (blocker_one, blocker_two):
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("local stale file\n", encoding="utf-8")
    attempts = 0

    def fake_run_git(args, cwd=None):
        nonlocal attempts
        if args[:2] == ["status", "--porcelain"]:
            return ""
        if args[:2] == ["sparse-checkout", "set"]:
            attempts += 1
            path = "skills/news_skill/skill.yaml" if attempts == 1 else "skills/news_skill/handlers/__init__.py"
            raise cli_git_module.GitError(
                "git sparse-checkout set failed: error: Working tree file "
                f"'{path}' would be overwritten by sparse checkout update."
            )
        if args and args[0] in {"status", "log"}:
            return ""
        raise AssertionError(f"unexpected git call: {args!r}")

    monkeypatch.setattr(cli_git_module, "_run_git", fake_run_git)

    with pytest.raises(cli_git_module.GitError) as ei:
        CliGitClient(depth=0).sparse_set(str(root), ["skills/news_skill"], no_cone=True)

    assert "Sparse checkout blocker recovery exceeded 1 file(s)" in str(ei.value)
    assert attempts == 2
    assert not blocker_one.exists()
    assert blocker_two.exists()


def test_sparse_init_detects_workspace_before_dirty_autostash(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_TYPE", "prod")
    root = tmp_path / ".adaos" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None):
        calls.append(list(args))
        if args[:2] == ["status", "--porcelain"]:
            return ""
        if args[:2] == ["sparse-checkout", "init"]:
            return ""
        raise AssertionError(f"unexpected git call: {args!r}")

    monkeypatch.setattr(cli_git_module, "_run_git", fake_run_git)

    CliGitClient(depth=0).sparse_init(str(root), cone=False)

    assert calls == [["status", "--porcelain"], ["sparse-checkout", "init"]]


def test_sparse_checkout_ignores_cli_flags(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)
    repo.install("weather_skill")

    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    sparse_file.write_text("--no-cone\nskills/weather_skill\nregistry.json\n", encoding="utf-8")

    repo.install("news_skill")
    contents = sparse_file.read_text(encoding="utf-8")
    assert "--no-cone" not in contents
