import os
from pathlib import Path
from git import Repo
from dotenv import load_dotenv, find_dotenv
from adaos.sdk.context import SKILLS_DIR

GIT_USER = os.getenv("GIT_USER", "adaos")
GIT_EMAIL = os.getenv("GIT_EMAIL", "adaos@local")


def init_git_repo() -> Repo:
    """Инициализация с поддержкой sparse checkout"""
    repo_url = "https://github.com/stipot/adaoskills.git"
    skills_dir = Path(SKILLS_DIR)

    if (skills_dir / ".git").exists():
        return Repo(skills_dir)

    # Создаем пустой репозиторий
    skills_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(skills_dir)

    # Настройка sparse checkout
    with repo.config_writer() as config:
        config.set_value("core", "sparseCheckout", "true")
        config.set_value("pull", "rebase", "false")

    # Инициализация sparse-checkout файла
    sparse_file = skills_dir / ".git" / "info" / "sparse-checkout"
    sparse_file.parent.mkdir(parents=True, exist_ok=True)
    sparse_file.write_text("/*\n!/*/\n")  # Блокируем все папки по умолчанию

    # Добавляем origin
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
