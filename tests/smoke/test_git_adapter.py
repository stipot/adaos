# tests/smoke/test_git_adapter.py
import os, tempfile
from adaos.adapters.git.cli_git import CliGitClient, GitError


def test_git_smoke(monkeypatch, tmp_path):
    # пропустить, если git недоступен в окружении
    try:
        import subprocess

        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except Exception:
        return
    # локальный пустой репо
    g = CliGitClient()
    d = tmp_path / "repo"
    # не клонируем (нет URL), просто убедимся, что команда формируется
    try:
        g.current_commit(str(tmp_path))  # даст ошибку (не git-репо) — это нормально
    except GitError:
        pass
