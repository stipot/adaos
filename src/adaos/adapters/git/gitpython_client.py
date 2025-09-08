# adapters/git/gitpython_client.py
from pathlib import Path
from typing import Optional, Sequence
from git import Repo


class GitPythonClient:
    def ensure_repo(self, dir: str, url: str, branch: Optional[str] = None) -> None:
        d = Path(dir)
        d.mkdir(parents=True, exist_ok=True)
        if (d / ".git").exists():
            repo = Repo(d)
            repo.remotes.origin.fetch(prune=True)
            if branch:
                repo.git.checkout(branch)
        else:
            repo = Repo.clone_from(url, d)
        try:
            repo.git.config("index.version", "2")
            repo.git.sparse_checkout("init", "--cone")
        except Exception:
            pass

    def pull(self, dir: str) -> None:
        Repo(Path(dir)).remotes.origin.pull(rebase=False)

    def current_commit(self, dir: str) -> str:
        return Repo(Path(dir)).head.commit.hexsha

    def sparse_init(self, dir: str, cone: bool = True) -> None:
        repo = Repo(Path(dir))
        repo.git.sparse_checkout("init", "--cone" if cone else "--no-cone")

    def sparse_set(self, dir: str, paths: Sequence[str], no_cone: bool = True) -> None:
        repo = Repo(Path(dir))
        args = ["set"] + (["--no-cone"] if no_cone else [])
        repo.git.sparse_checkout(*args, *paths)

    def sparse_add(self, dir: str, path: str) -> None:
        repo = Repo(Path(dir))
        try:
            repo.git.sparse_checkout("add", path)
        except Exception:
            # фоллбек: перечитать и расширить список (твоя логика)
            sp = Path(repo.git_dir) / "info" / "sparse-checkout"
            lines = sp.read_text(encoding="utf-8").splitlines() if sp.exists() else []
            if path not in lines:
                lines.append(path)
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text("\n".join(lines) + "\n", encoding="utf-8")
