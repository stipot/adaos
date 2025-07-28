import os
from git import Repo
import yaml
from db import add_or_update_skill, update_skill_version, get_skill, list_skills
import shutil

SKILLS_DIR = "skills"
TEMPLATES_DIR = "runtime/skills_templates"
MONOREPO_URL = os.getenv("SKILLS_REPO_URL")


def _skill_subdir(skill_name: str) -> str:
    return f"skills/{skill_name}"


def _ensure_repo() -> Repo:
    """Клонируем monorepo если он не локален, включаем sparse-checkout."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    git_dir = os.path.join(SKILLS_DIR, ".git")

    if not os.path.exists(git_dir):
        print(f"[cyan]Клонируем monorepo {MONOREPO_URL}[/cyan]")
        repo = Repo.clone_from(MONOREPO_URL, SKILLS_DIR)
        repo.git.sparse_checkout("init", "--cone")
        return repo

    return Repo(SKILLS_DIR)


def create_skill(skill_name: str, template_name: str = "basic") -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)
    skill_path = os.path.join(SKILLS_DIR, skill_subdir)

    if os.path.exists(skill_path):
        return f"[red]Навык {skill_name} уже существует в monorepo[/red]"

    template_path = os.path.join(TEMPLATES_DIR, template_name)
    if not os.path.exists(template_path):
        return f"[red]Шаблон {template_name} не найден[/red]"

    print(f"[cyan]Создаём навык {skill_name} из шаблона {template_name}[/cyan]")
    shutil.copytree(template_path, skill_path)

    repo.git.sparse_checkout("set", skill_subdir)
    repo.git.add(skill_subdir)
    repo.index.commit(f"Создан новый навык {skill_name} из шаблона {template_name}")
    repo.remotes.origin.push()

    # Обновляем БД
    yaml_path = os.path.join(skill_path, "skill.yaml")
    version = "1.0"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "1.0")
    add_or_update_skill(skill_name, version, MONOREPO_URL)

    return f"[green]Навык {skill_name} создан и добавлен в monorepo[/green]"


def push_skill(skill_name: str, message: str = "Обновление навыка") -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)
    skill_path = os.path.join(SKILLS_DIR, skill_subdir)

    if not os.path.exists(skill_path):
        return f"[red]Навык {skill_name} не найден в monorepo[/red]"

    repo.git.sparse_checkout("set", skill_subdir)
    repo.git.add(skill_subdir)

    if repo.is_dirty():
        repo.index.commit(message)
        repo.remotes.origin.push()
        return f"[green]{skill_name} отправлен в monorepo[/green]"
    else:
        return f"[yellow]Нет изменений для отправки[/yellow]"


def pull_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)

    repo.git.sparse_checkout("set", skill_subdir)
    repo.remotes.origin.pull()

    yaml_path = os.path.join(SKILLS_DIR, skill_subdir, "skill.yaml")
    version = "unknown"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    add_or_update_skill(skill_name, version, MONOREPO_URL)
    return f"[green]{skill_name} загружен (v{version})[/green]"


def update_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)

    repo.git.sparse_checkout("set", skill_subdir)
    repo.remotes.origin.pull()

    yaml_path = os.path.join(SKILLS_DIR, skill_subdir, "skill.yaml")
    version = "unknown"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    update_skill_version(skill_name, version)
    return f"[green]{skill_name} обновлён (v{version})[/green]"
