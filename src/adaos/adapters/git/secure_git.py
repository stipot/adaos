from __future__ import annotations
from pathlib import Path
from typing import Optional, Sequence
from adaos.ports.git import GitClient
from adaos.ports import Net
from .cli_git import _run_git  # используем вспомогательную команду


class SecureGitClient(GitClient):
    """
    Обёртка над любым GitClient с проверкой NetPolicy.
    - ensure_repo(url) — проверяем URL по allow-листу
    - pull(dir) — проверяем origin.url перед pull
    - sparse_* — не генерируют сетевых запросов; пропускаем без проверок
    """

    def __init__(self, base: GitClient, net: Net):
        self.base = base
        self.net = net

    def _origin_url(self, dir: str) -> Optional[str]:
        d = Path(dir)
        if not (d / ".git").exists():
            return None
        try:
            return _run_git(["config", "--get", "remote.origin.url"], cwd=dir).strip()
        except Exception:
            return None

    def ensure_repo(self, dir: str, url: str, branch: Optional[str] = None) -> None:
        self.net.require_url(url)
        self.base.ensure_repo(dir, url, branch)

    def pull(self, dir: str) -> None:
        url = self._origin_url(dir)
        if url:
            self.net.require_url(url)
        self.base.pull(dir)

    def current_commit(self, dir: str) -> str:
        return self.base.current_commit(dir)

    # sparse passthrough
    def sparse_init(self, dir: str, cone: bool = True) -> None:
        self.base.sparse_init(dir, cone)

    def sparse_set(self, dir: str, paths: Sequence[str], no_cone: bool = True) -> None:
        self.base.sparse_set(dir, paths, no_cone)

    def sparse_add(self, dir: str, path: str) -> None:
        self.base.sparse_add(dir, path)

    def changed_files(self, dir: str, subpath: Optional[str] = None) -> list[str]:
        return self.base.changed_files(dir, subpath)

    def commit_subpath(self, dir: str, subpath: str, message: str, author_name: str, author_email: str, signoff: bool = False) -> str:
        # локальная операция — сетевых проверок не нужно
        return self.base.commit_subpath(dir, subpath, message, author_name, author_email, signoff)

    def push(self, dir: str, remote: str = "origin", branch: Optional[str] = None) -> None:
        url = self._origin_url(dir)
        if url:
            self.net.require_url(url)
        self.base.push(dir, remote=remote, branch=branch)
