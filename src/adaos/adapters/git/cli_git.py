# src/adaos/adapters/git/cli_git.py  (расширение)
from __future__ import annotations
import subprocess, os
from pathlib import Path
from typing import Optional, Final, Sequence
from adaos.ports.git import GitClient


class GitError(RuntimeError): ...


def _run_git(args: list[str], cwd: Optional[str] = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def _append_exclude(dir: str, lines: list[str]) -> None:
    from pathlib import Path

    p = Path(dir) / ".git" / "info" / "exclude"
    existing = set()
    if p.exists():
        existing = set(p.read_text(encoding="utf-8").splitlines())
    merged = existing.union(lines)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(merged)) + "\n", encoding="utf-8")


class CliGitClient(GitClient):
    def __init__(self, depth: int = 1) -> None:
        self._depth: Final[int] = depth

    def ensure_repo(self, dir: str, url: str, branch: Optional[str] = None) -> None:
        d = Path(dir)
        d.mkdir(parents=True, exist_ok=True)
        git_dir = d / ".git"
        if git_dir.exists():
            if branch:
                _run_git(["fetch", "origin", branch, f"--depth={self._depth}"], cwd=str(d))
                _run_git(["checkout", branch], cwd=str(d))
                _run_git(["reset", "--hard", f"origin/{branch}"], cwd=str(d))
            else:
                _run_git(["pull", "--ff-only"], cwd=str(d))
        else:
            args = ["clone", url, str(d)]
            if self._depth > 0:
                args += [f"--depth={self._depth}"]
            if branch:
                args += ["--branch", branch]
            _run_git(args, cwd=None)
            # сразу включим sparse cone по умолчанию (можно переинициализировать на no-cone)
            try:
                _run_git(["sparse-checkout", "init", "--cone"], cwd=str(d))
            except GitError:
                pass
        _append_exclude(
            dir,
            [
                "*.pyc",
                "__pycache__/",
                ".venv/",
                "state/",
                "cache/",
                "logs/",
            ],
        )

    def pull(self, dir: str) -> None:
        _run_git(["pull", "--ff-only"], cwd=dir)

    def current_commit(self, dir: str) -> str:
        return _run_git(["rev-parse", "HEAD"], cwd=dir)

    # --- sparse ---
    def sparse_init(self, dir: str, cone: bool = True) -> None:
        args = ["sparse-checkout", "init"]
        if cone:
            args.append("--cone")
        _run_git(args, cwd=dir)

    def sparse_set(self, dir: str, paths: Sequence[str], no_cone: bool = True) -> None:
        args = ["sparse-checkout", "set"]
        if no_cone:
            args.append("--no-cone")
        _run_git([*args, *paths], cwd=dir)

    def sparse_add(self, dir: str, path: str) -> None:
        try:
            _run_git(["sparse-checkout", "add", path], cwd=dir)
        except GitError:
            # fallback: перечитать и расширить вручную (как в твоей логике)
            info = Path(dir) / ".git" / "info"
            sp = info / "sparse-checkout"
            lines = sp.read_text(encoding="utf-8").splitlines() if sp.exists() else []
            if path not in lines:
                info.mkdir(parents=True, exist_ok=True)
                lines.append(path)
                sp.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def changed_files(self, dir: str, subpath: Optional[str] = None) -> list[str]:
        # untracked (-o) + modified (-m), исключая игнор по .gitignore
        args = ["ls-files", "-m", "-o", "--exclude-standard"]
        if subpath:
            args += ["--", subpath]
        out = _run_git(args, cwd=dir)
        files = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return files

    def _current_branch(self, dir: str) -> str:
        out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dir).strip()
        return out or "main"

    def commit_subpath(self, dir: str, subpath: str, message: str, author_name: str, author_email: str, signoff: bool = False) -> str:
        # stage только подпуть
        _run_git(["add", "--", subpath], cwd=dir)
        # пустой ли индекс?
        status = _run_git(["diff", "--cached", "--name-only"], cwd=dir)
        if not status.strip():
            return "nothing-to-commit"
        # автор в -c для изоляции от глобальных конфигов
        args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", message]
        if signoff:
            args.append("--signoff")
        _run_git(args, cwd=dir)
        return _run_git(["rev-parse", "HEAD"], cwd=dir).strip()

    def push(self, dir: str, remote: str = "origin", branch: Optional[str] = None) -> None:
        branch = branch or self._current_branch(dir)
        _run_git(["push", remote, branch], cwd=dir)
