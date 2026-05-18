# tests/test_git_pull_upstream.py
import subprocess

import pytest

from adaos.adapters.git.cli_git import CliGitClient, GitError


def _run(cmd: list[str], cwd, *, env=None) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr or proc.stdout}")
    return (proc.stdout or "").strip()


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _git_available(), reason="git is not available")
def test_pull_works_without_upstream(tmp_path):
    """
    Reproduce the common case where a repo is initialized in-place (non-empty dest),
    so `git checkout -B main origin/main` does not configure upstream. Our adapter
    should still be able to pull via `origin <branch>`.
    """
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    dest = tmp_path / "dest"

    _run(["git", "init", "--bare", str(remote)], cwd=tmp_path)
    _run(["git", "clone", str(remote), str(seed)], cwd=tmp_path)

    env = dict(**__import__("os").environ)
    env.setdefault("GIT_AUTHOR_NAME", "adaos")
    env.setdefault("GIT_AUTHOR_EMAIL", "adaos@example.local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

    (seed / "readme.txt").write_text("v1\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=seed, env=env)
    _run(["git", "commit", "-m", "init"], cwd=seed, env=env)
    _run(["git", "branch", "-M", "main"], cwd=seed, env=env)
    _run(["git", "push", "-u", "origin", "main"], cwd=seed, env=env)

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "not-empty.txt").write_text("trigger init+fetch path\n", encoding="utf-8")

    git = CliGitClient(depth=0)
    git.ensure_repo(dest, str(remote), branch="main")
    before = git.current_commit(dest)

    (seed / "readme.txt").write_text("v2\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=seed, env=env)
    _run(["git", "commit", "-m", "update"], cwd=seed, env=env)
    _run(["git", "push"], cwd=seed, env=env)

    # Should not raise GitError about missing upstream.
    git.pull(dest)
    after = git.current_commit(dest)

    assert before != after


@pytest.mark.skipif(not _git_available(), reason="git is not available")
def test_pull_reports_divergence_hint(tmp_path):
    git = CliGitClient(depth=0)
    remote = tmp_path / "remote.git"
    a = tmp_path / "a"
    b = tmp_path / "b"

    _run(["git", "init", "--bare", str(remote)], cwd=tmp_path)
    _run(["git", "clone", str(remote), str(a)], cwd=tmp_path)
    _run(["git", "clone", str(remote), str(b)], cwd=tmp_path)

    env = dict(**__import__("os").environ)
    env.setdefault("GIT_AUTHOR_NAME", "adaos")
    env.setdefault("GIT_AUTHOR_EMAIL", "adaos@example.local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

    (a / "x.txt").write_text("1\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=a, env=env)
    _run(["git", "commit", "-m", "c1"], cwd=a, env=env)
    _run(["git", "branch", "-M", "main"], cwd=a, env=env)
    _run(["git", "push", "-u", "origin", "main"], cwd=a, env=env)

    # Ensure b tracks origin/main (cloning an empty bare repo may leave it on master).
    _run(["git", "fetch", "origin", "main"], cwd=b, env=env)
    _run(["git", "checkout", "-B", "main", "origin/main"], cwd=b, env=env)
    _run(["git", "branch", "--set-upstream-to=origin/main", "main"], cwd=b, env=env)

    # Diverge b: commit locally without pushing, then advance remote.
    (b / "x.txt").write_text("local\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=b, env=env)
    _run(["git", "commit", "-m", "local"], cwd=b, env=env)

    (a / "x.txt").write_text("remote\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=a, env=env)
    _run(["git", "commit", "-m", "remote"], cwd=a, env=env)
    _run(["git", "push"], cwd=a, env=env)

    with pytest.raises(GitError) as ei:
        git.pull(b)
    assert "Non fast-forward pull detected." in str(ei.value)


@pytest.mark.skipif(not _git_available(), reason="git is not available")
def test_push_aborts_rebase_conflict_and_leaves_worktree_clean(tmp_path):
    git = CliGitClient(depth=0)
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / ".adaos" / "workspace"

    _run(["git", "init", "--bare", str(remote)], cwd=tmp_path)
    _run(["git", "clone", str(remote), str(seed)], cwd=tmp_path)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", str(remote), str(workspace)], cwd=tmp_path)

    env = dict(**__import__("os").environ)
    env.setdefault("GIT_AUTHOR_NAME", "adaos")
    env.setdefault("GIT_AUTHOR_EMAIL", "adaos@example.local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

    (seed / "registry.json").write_text('{"version":"base"}\n', encoding="utf-8")
    _run(["git", "add", "."], cwd=seed, env=env)
    _run(["git", "commit", "-m", "init"], cwd=seed, env=env)
    _run(["git", "branch", "-M", "main"], cwd=seed, env=env)
    _run(["git", "push", "-u", "origin", "main"], cwd=seed, env=env)

    _run(["git", "fetch", "origin", "main"], cwd=workspace, env=env)
    _run(["git", "checkout", "-B", "main", "origin/main"], cwd=workspace, env=env)
    _run(["git", "branch", "--set-upstream-to=origin/main", "main"], cwd=workspace, env=env)

    (workspace / "registry.json").write_text('{"version":"local"}\n', encoding="utf-8")
    _run(["git", "add", "."], cwd=workspace, env=env)
    _run(["git", "commit", "-m", "local"], cwd=workspace, env=env)

    (seed / "registry.json").write_text('{"version":"remote"}\n', encoding="utf-8")
    _run(["git", "add", "."], cwd=seed, env=env)
    _run(["git", "commit", "-m", "remote"], cwd=seed, env=env)
    _run(["git", "push"], cwd=seed, env=env)

    with pytest.raises(GitError) as ei:
        git.push(workspace, branch="main")

    assert "workspace is back at the local commit" in str(ei.value)
    assert _run(["git", "status", "--porcelain"], cwd=workspace, env=env) == ""
    assert not (workspace / ".git" / "rebase-merge").exists()
    assert not (workspace / ".git" / "rebase-apply").exists()
    assert (workspace / "registry.json").read_text(encoding="utf-8") == '{"version":"local"}\n'
