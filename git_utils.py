import os
from pathlib import Path
from git import Repo
from dotenv import load_dotenv, find_dotenv

print(os.getenv("OPENAI_API_KEY"))

GIT_USER = os.getenv("GIT_USER", "adaos")
GIT_EMAIL = os.getenv("GIT_EMAIL", "adaos@local")

SKILLS_PATH = Path("runtime/skills")
REPO_PATH = Path("skills_repo")


def init_git_repo():
    """Инициализация репозитория Git для навыков с настройкой user/email"""
    if not REPO_PATH.exists():
        REPO_PATH.mkdir(parents=True, exist_ok=True)
        repo = Repo.init(REPO_PATH)

        # Создаём папку skills внутри репозитория
        skills_dir = REPO_PATH / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Настраиваем user.name и user.email
        with repo.config_writer() as config:
            config.set_value("user", "name", GIT_USER)
            config.set_value("user", "email", GIT_EMAIL)

        print(f"[GIT] Репозиторий навыков инициализирован. user={GIT_USER}, email={GIT_EMAIL}")
    else:
        repo = Repo(REPO_PATH)

        # Проверяем, есть ли user/email в конфиге
        with repo.config_writer() as config:
            if not config.has_section("user") or not config.has_option("user", "name"):
                config.set_value("user", "name", GIT_USER)
            if not config.has_section("user") or not config.has_option("user", "email"):
                config.set_value("user", "email", GIT_EMAIL)

    return repo


def commit_skill_changes(skill_name: str, message: str):
    """Коммит изменений навыка"""
    repo = Repo(REPO_PATH)
    skills_dir = REPO_PATH / "skills" / skill_name.lower()

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
    repo = Repo(REPO_PATH)
    if not repo.head.is_valid():
        print("[GIT] Нет коммитов для отката")
        return
    last_commit = repo.head.commit
    repo.git.reset("--hard", "HEAD~1")
    print(f"[GIT] Откат на коммит {last_commit.hexsha[:7]}")
