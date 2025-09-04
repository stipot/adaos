# src/adaos/sdk/utils/git_utils.py
import os
from pathlib import Path
from typing import Union
from git import Repo
from dotenv import load_dotenv, find_dotenv
from adaos.sdk.context import SKILLS_DIR, MONOREPO_URL

GIT_USER = os.getenv("GIT_USER", "adaos")
GIT_EMAIL = os.getenv("GIT_EMAIL", "adaos@local")

# -------------------- существующие функции для skills (как было) --------------------


def clone_git_repo(dir: Path = SKILLS_DIR, git_url: str = MONOREPO_URL) -> Repo:
    os.makedirs(Path(dir), exist_ok=True)
    git_dir = os.path.join(Path(dir), ".git")
    if not os.path.exists(git_dir):
        print(f"[cyan]The repo is cloned: {git_url}[/cyan]")
        repo = Repo.clone_from(git_url, dir)
        repo.git.config("index.version", "2")
        repo.git.sparse_checkout("init", "--cone")
        return repo
    repo = Repo(Path(dir))
    try:
        current_index_ver = repo.git.config("index.version")
    except:
        current_index_ver = "3"
    if current_index_ver != "2":
        print(f"[yellow]The repo index has been rebuilt[/yellow]")
        repo.git.config("index.version", "2")
        if repo.head.is_valid():
            repo.git.reset("--mixed")
    return repo


def init_git_repo() -> Repo:
    """Инициализация с поддержкой sparse checkout"""
    repo_url = "https://github.com/stipot/adaoskills.git"
    skills_dir = Path(SKILLS_DIR)
    if (skills_dir / ".git").exists():
        return Repo(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(skills_dir)
    with repo.config_writer() as config:
        config.set_value("core", "sparseCheckout", "true")
        config.set_value("pull", "rebase", "false")
    sparse_file = skills_dir / ".git" / "info" / "sparse-checkout"
    sparse_file.parent.mkdir(parents=True, exist_ok=True)
    sparse_file.write_text("/*\n!/*/\n")  # Блокируем все папки по умолчанию
    origin = repo.create_remote("origin", repo_url)
    origin.fetch()
    return repo


def commit_skill_changes(skill_name: str, message: str):
    """Коммит изменений навыка"""
    repo = Repo(Path(SKILLS_DIR))
    skills_dir = Path(SKILLS_DIR) / "skills" / skill_name.lower()
    if not skills_dir.exists():
        print(f"[GIT] Навык {skill_name} не найден в репозитории")
        return
    repo.git.add(str(skills_dir))
    repo.index.commit(message)
    tag_name = f"{skill_name}_v{len(list(repo.tags)) + 1}"
    repo.create_tag(tag_name)
    print(f"[GIT] Коммит {message} (тег: {tag_name})")


def rollback_last_commit():
    """Откат последнего коммита"""
    repo = Repo(Path(SKILLS_DIR))
    if not repo.head.is_valid():
        print("[GIT] Нет коммитов для отката")
        return
    last_commit = repo.head.commit
    repo.git.reset("--hard", "HEAD~1")
    print(f"[GIT] Откат на коммит {last_commit.hexsha[:7]}")


def _ensure_repo(dir: Path, git_url: str) -> Repo:
    os.makedirs(dir, exist_ok=True)
    git_dir = os.path.join(dir, ".git")
    if not os.path.exists(git_dir):
        repo = clone_git_repo(dir, git_url)
    else:
        repo = Repo(dir)
    return repo


# -------------------- НОВОЕ: универсальные утилиты git для сценариев и пр. --------------------


def init_repo(target_dir: Union[str, Path], repo_url: str) -> Repo:
    """
    Инициализирует git-репозиторий в target_dir:
    - если пусто — clone repo_url
    - включает sparse-checkout (cone)
    - настраивает user.name/email (если не проставлены)
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if (target_dir / ".git").exists():
        repo = Repo(target_dir)
    else:
        repo = Repo.clone_from(repo_url, target_dir)
        try:
            repo.git.config("index.version", "2")
            repo.git.sparse_checkout("init", "--cone")
        except Exception:
            # совместимость со старыми git: просто продолжаем
            pass

    # user identity (как у навыков)
    try:
        repo.config_reader().get_value("user", "email")
        repo.config_reader().get_value("user", "name")
    except Exception:
        with repo.config_writer() as cw:
            cw.set_value("user", "name", os.environ.get("GIT_AUTHOR_NAME", "AdaOS Bot"))
            cw.set_value("user", "email", os.environ.get("GIT_AUTHOR_EMAIL", "bot@example.com"))

    return repo


def sparse_add(repo: Repo, subpath: str) -> None:
    """
    Добавляет путь в sparse-checkout.
    """
    subpath = subpath.strip("/")

    try:
        # новый git: команда sparse-checkout add
        repo.git.sparse_checkout("add", subpath)
    except Exception:
        # fallback: правим файл sparse-checkout руками
        sp = Path(repo.git_dir) / "info" / "sparse-checkout"
        lines = sp.read_text(encoding="utf-8").splitlines() if sp.exists() else []
        if subpath not in lines:
            lines.append(subpath)
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clone_repo(repo_url: str, dest_parent: Union[str, Path]) -> str:
    dest_parent = Path(dest_parent)
    dest_parent.mkdir(parents=True, exist_ok=True)
    name = repo_url.rstrip("/").split("/")[-1]
    name = name[:-4] if name.endswith(".git") else name
    dest = dest_parent / name
    if dest.exists() and (dest / ".git").exists():
        return str(dest)
    Repo.clone_from(repo_url, dest)
    return str(dest)


def checkout_ref(repo_path: Union[str, Path], ref: str) -> None:
    repo = Repo(Path(repo_path))
    repo.remotes.origin.fetch(prune=True)
    try:
        repo.git.checkout(ref)
    except Exception:
        try:
            repo.git.checkout(f"origin/{ref}")
        except Exception:
            repo.git.checkout(ref)  # коммит/хеш


def current_commit(repo_path: Union[str, Path]) -> str:
    repo = Repo(Path(repo_path))
    return repo.head.commit.hexsha


def pull_repo(repo_path: Union[str, Path]) -> None:
    repo = Repo(Path(repo_path))
    repo.remotes.origin.pull(rebase=False)


# - --------------
def _sync_sparse_checkout(repo: Repo, el_list, installed=[]):
    installed = [s["name"] for s in el_list if s.get("installed", 1)] + installed
    repo.git.sparse_checkout("set", *installed)


def _ensure_in_sparse(repo: Repo, path: str):
    """
    Гарантирует, что указанный путь попал в sparse-checkout.
    Без этого git add <path> падает с advice.updateSparsePath.
    """
    try:
        # инициализируем режим no-cone, если уже инициализирован — git сам проигнорирует
        repo.git.sparse_checkout("init", "--no-cone")
    except Exception:
        pass
    try:
        repo.git.sparse_checkout("add", path)
    except Exception:
        # старые git без 'add' могут требовать set — подхватим через _sync_sparse_checkout
        # но сначала расширим текущий список
        _sync_sparse_checkout(repo, installed=[path])
