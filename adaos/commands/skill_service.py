import os
import shutil
import yaml
from pathlib import Path
from git import Repo
from adaos.i18n.translator import _
from adaos.db import add_or_update_skill, update_skill_version, list_skills, set_installed_flag

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # adaos/
BASE_DIR = os.getenv("BASE_DIR", str(Path.home())) + "/.adaos"
SKILLS_DIR = BASE_DIR + "/skills"
TEMPLATES_DIR = str(PACKAGE_DIR / "runtime" / "skills_templates")
MONOREPO_URL = os.getenv("SKILLS_REPO_URL")


def _skill_subdir(skill_name: str) -> str:
    return skill_name


def _ensure_repo() -> Repo:
    os.makedirs(SKILLS_DIR, exist_ok=True)
    git_dir = os.path.join(SKILLS_DIR, ".git")

    if not os.path.exists(git_dir):
        print(f"[cyan]{_('repo.clone')} {MONOREPO_URL}[/cyan]")
        repo = Repo.clone_from(MONOREPO_URL, SKILLS_DIR)
        repo.git.config("index.version", "2")
        repo.git.sparse_checkout("init", "--cone")
        return repo

    repo = Repo(SKILLS_DIR)

    try:
        current_index_ver = repo.git.config("index.version")
    except:
        current_index_ver = "3"

    if current_index_ver != "2":
        print(f"[yellow]{_('repo.rebuild_index')}[/yellow]")
        repo.git.config("index.version", "2")
        if repo.head.is_valid():
            repo.git.reset("--mixed")

    return repo


def _sync_sparse_checkout(repo: Repo):
    """Пересобираем sparse-checkout из всех установленных навыков"""
    installed = [s["name"] for s in list_skills() if s.get("installed", 1)]
    if installed:
        repo.git.sparse_checkout("set", *installed)
    else:
        repo.git.sparse_checkout("disable")


def create_skill(skill_name: str, template_name: str = "basic") -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)
    skill_path = os.path.join(SKILLS_DIR, skill_subdir)

    # Проверяем, не существует ли уже папка с таким навыком
    if os.path.exists(skill_path):
        return f"[red]{_('skill.exists', skill_name=skill_name)}[/red]"

    # Проверяем, существует ли шаблон
    template_path = os.path.join(TEMPLATES_DIR, template_name)
    if not os.path.exists(template_path):
        return f"[red]{_('template.not_found', template_name=template_name)}[/red]"

    # Копируем шаблон в папку навыка
    print(f"[cyan]{_('skill.create', skill_name=skill_name, template_name=template_name)}[/cyan]")
    shutil.copytree(template_path, skill_path)

    # ВАЖНО: добавляем новый навык в sparse-checkout
    repo.git.sparse_checkout("add", skill_subdir)

    # Добавляем файлы в индекс
    repo.git.add(skill_subdir)

    # Если есть изменения – коммитим
    if repo.is_dirty():
        repo.git.commit("-m", _("skill.commit_message", skill_name=skill_name, template_name=template_name))
        repo.remotes.origin.push()
    else:
        print(f"[yellow]{_('skill.no_changes')}[/yellow]")

    # Обновляем БД версией из skill.yaml
    yaml_path = os.path.join(skill_path, "skill.yaml")
    version = "1.0"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "1.0")

    add_or_update_skill(skill_name, version, MONOREPO_URL, installed=1)
    return f"[green]{_('skill.created', skill_name=skill_name)}[/green]"


def push_skill(skill_name: str, message: str = None) -> str:
    repo = _ensure_repo()
    skill_subdir = _skill_subdir(skill_name)
    skill_path = os.path.join(SKILLS_DIR, skill_subdir)

    if not os.path.exists(skill_path):
        return f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]"

    _sync_sparse_checkout(repo)
    repo.git.add(skill_subdir)

    if repo.is_dirty():
        repo.git.commit("-m", message or _("skill.push_message"))
        repo.remotes.origin.push()
        return f"[green]{_('skill.pushed',skill_name=skill_name)}[/green]"
    else:
        return f"[yellow]{_('skill.no_changes_push')}[/yellow]"


def pull_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    set_installed_flag(skill_name, installed=1)
    _sync_sparse_checkout(repo)
    repo.remotes.origin.pull()

    yaml_path = os.path.join(SKILLS_DIR, skill_name, "skill.yaml")
    version = "unknown"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    add_or_update_skill(skill_name, version, MONOREPO_URL, installed=1)
    return f"[green]{_('skill.pulled', skill_name=skill_name, version=version)}[/green]"


def update_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    _sync_sparse_checkout(repo)
    repo.remotes.origin.pull()

    yaml_path = os.path.join(SKILLS_DIR, skill_name, "skill.yaml")
    version = "unknown"
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    update_skill_version(skill_name, version)
    return f"[green]{_('skill.updated', skill_name=skill_name, version=version)}[/green]"


def install_skill(skill_name: str) -> str:
    """Устанавливает навык из monorepo (фактически pull + флаг installed=1)"""
    return pull_skill(skill_name)


def uninstall_skill(skill_name: str) -> str:
    """Удаляет навык у пользователя (ставим installed=0 и пересобираем sparse-checkout)"""
    repo = _ensure_repo()
    set_installed_flag(skill_name, installed=0)
    _sync_sparse_checkout(repo)
    return f"[green]{_('skill.uninstalled', skill_name=skill_name)}[/green]"
