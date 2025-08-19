# src/adaos/sdk/skill_service.py
import os
import sys
import shutil
import yaml
from pathlib import Path
from git import Repo
import subprocess
import importlib.util
from typing import List, Optional
import json

from adaos.sdk.skills.i18n import _
from adaos.sdk.context import _agent, set_current_skill, get_current_skill_path, get_current_skill  # новый AgentContext (глобальный)
from adaos.agent.db.sqlite import (
    add_or_update_skill,
    update_skill_version,
    list_skills,
    set_installed_flag,
)
from adaos.sdk.utils.git_utils import _ensure_repo

CATALOG_FILENAME = "skills.yaml"  # имя файла каталога в корне монорепо


def _read_catalog(repo: Repo) -> List[str]:
    """Подтягивает и читает каталог навыков из монорепо (skills.yaml)."""
    try:
        repo.git.sparse_checkout("init", "--no-cone")
    except Exception:
        pass

    try:
        repo.git.sparse_checkout("set", "--no-cone", "--skip-checks", CATALOG_FILENAME)
    except Exception:
        repo.git.sparse_checkout("set", "--no-cone", CATALOG_FILENAME)

    repo.remotes.origin.pull()

    catalog_path = _agent.skills_dir.parent / CATALOG_FILENAME
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog file '{CATALOG_FILENAME}' not found in monorepo.")

    with open(catalog_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    skills = data.get("skills") or []
    if not isinstance(skills, list):
        raise ValueError(f"'{CATALOG_FILENAME}' must contain list under key 'skills'")
    return [str(s).strip() for s in skills if str(s).strip()]


def install_all_skills(limit: Optional[int] = None) -> List[str]:
    repo = _ensure_repo()
    names = _read_catalog(repo)
    if limit:
        names = names[:limit]

    installed = []
    for name in names:
        try:
            pull_skill(name)
            installed.append(name)
        except Exception as e:
            print(f"[yellow]Skip installing {name}: {e}[/yellow]")

    _sync_sparse_checkout(repo)
    return installed


def _skill_subdir(skill_name: str) -> str:
    return skill_name


def _sync_sparse_checkout(repo: Repo, installed=[]):
    installed = [s["name"] for s in list_skills() if s.get("installed", 1)] + installed
    repo.git.sparse_checkout("set", *installed)


def _ensure_git_identity(repo: Repo):
    try:
        repo.config_reader().get_value("user", "email")
        repo.config_reader().get_value("user", "name")
    except Exception:
        repo.config_writer().set_value("user", "name", os.environ.get("GIT_AUTHOR_NAME", "AdaOS Bot")).release()
        repo.config_writer().set_value("user", "email", os.environ.get("GIT_AUTHOR_EMAIL", "bot@example.com")).release()


def create_skill(skill_name: str, template_name: str = "basic") -> str:
    repo = _ensure_repo()
    _ensure_git_identity(repo)
    skill_subdir = _skill_subdir(skill_name)
    skill_path = _agent.skills_dir / skill_subdir

    if skill_path.exists():
        return f"[red]{_('skill.exists', skill_name=skill_name)}[/red]"

    template_path = _agent.templates_dir / template_name
    if not template_path.exists():
        return f"[red]{_('template.not_found', template_name=template_name)}[/red]"

    print(f"[cyan]{_('skill.create', skill_name=skill_name, template_name=template_name)}[/cyan]")
    shutil.copytree(template_path, skill_path)

    repo.git.sparse_checkout("add", skill_subdir)
    repo.git.add(skill_subdir)

    if repo.is_dirty():
        repo.git.commit("-m", _("skill.commit_message", skill_name=skill_name, template_name=template_name))
        if os.environ.get("ADAOS_TESTING") != "1":
            repo.remotes.origin.push()
        else:
            print("[yellow]TEST MODE: skip push to remote[/yellow]")
    else:
        print(f"[yellow]{_('skill.no_changes')}[/yellow]")

    yaml_path = skill_path / "skill.yaml"
    version = "1.0"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "1.0")

    add_or_update_skill(skill_name, version, _agent.monorepo_url, installed=1)
    set_current_skill(skill_name)
    return f"[green]{_('skill.created', skill_name=skill_name)}[/green]"


def push_skill(skill_name, message: str = None) -> str:
    repo = _ensure_repo()
    _ensure_git_identity(repo)
    _sync_sparse_checkout(repo)
    repo.git.add(skill_name)

    if repo.is_dirty():
        repo.git.commit("-m", message or _("skill.push_message"))
        if os.environ.get("ADAOS_TESTING") != "1":
            repo.remotes.origin.push()
        else:
            print("[yellow]TEST MODE: skip push to remote[/yellow]")

        return f"[green]{_('skill.pushed', skill_name=skill_name)}[/green]"
    else:
        return f"[yellow]{_('skill.no_changes_push')}[/yellow]"


def pull_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    set_installed_flag(skill_name, installed=1)
    _sync_sparse_checkout(repo, installed=[skill_name])
    repo.remotes.origin.pull()

    yaml_path = _agent.skills_dir / skill_name / "skill.yaml"
    version = "unknown"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    add_or_update_skill(skill_name, version, _agent.monorepo_url, installed=1)
    set_current_skill(skill_name)
    return f"[green]{_('skill.pulled', skill_name=skill_name, version=version)}[/green]"


def update_skill() -> str:
    if not get_current_skill():
        raise RuntimeError("No current skill set")
    repo = _ensure_repo()
    _sync_sparse_checkout(repo)
    repo.remotes.origin.pull()

    yaml_path = get_current_skill().path / "skill.yaml"
    version = "unknown"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            version = yaml.safe_load(f).get("version", "unknown")

    update_skill_version(get_current_skill().name, version)
    return f"[green]{_('skill.updated', skill_name=get_current_skill().name, version=version)}[/green]"


def install_skill(skill_name: str) -> str:
    return pull_skill(skill_name)


def uninstall_skill(skill_name: str) -> str:
    repo = _ensure_repo()
    set_installed_flag(skill_name, installed=0)
    _sync_sparse_checkout(repo)
    return f"[green]{_('skill.uninstalled', skill_name=skill_name)}[/green]"


def install_skill_dependencies(skill_path: Path | None = None):
    """
    Устанавливает зависимости навыка БЕЗ импорта его handler'а.
    Порядок источников:
      1) requirements.txt (одна зависимость на строку)
      2) skill.yaml → dependencies: [ ... ]
      3) prep/prep_result.json → resources.python_deps: [ ... ]
    """
    path = Path(skill_path or get_current_skill_path()).resolve()
    deps: list[str] = []
    # 1) requirements.txt
    req = path / "requirements.txt"
    if req.exists():
        deps += [line.strip() for line in req.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    # 2) skill.yaml
    sy = path / "skill.yaml"
    if sy.exists():
        try:
            y = yaml.safe_load(sy.read_text(encoding="utf-8")) or {}
            deps += list(y.get("dependencies") or [])
        except Exception:
            pass
    # 3) prep_result.json
    prep = path / "prep" / "prep_result.json"
    if prep.exists():
        try:
            j = json.loads(prep.read_text(encoding="utf-8"))
            r = (j.get("resources") or {}).get("python_deps") or []
            deps += list(r)
        except Exception:
            pass
    # убрать дубли
    seen = set()
    deps = [d for d in deps if not (d in seen or seen.add(d))]
    for dep in deps:
        # тихий режим: выводим только ошибки
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-q", dep]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[red]pip failed for '{dep}'[/red]")
            if proc.stderr:
                print(proc.stderr.strip())
