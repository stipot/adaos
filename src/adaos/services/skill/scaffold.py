# src/adaos/services/skill/scaffold.py
from __future__ import annotations

import re, os
import shutil
from pathlib import Path
from typing import Optional

from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit

# git/FS helpers
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

_name_re = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _safe_subdir(root: Path, name: str) -> Path:
    """Защита от path traversal: target всегда внутри root."""
    p = (root / name).resolve()
    root = root.resolve()
    try:
        p.relative_to(root)
    except Exception:
        raise ValueError("unsafe path traversal")
    return p


def _resolve_template_dir(template: str) -> Path:
    """
    Находит директорию шаблона навыка:
    1) как пакетный ресурс (adaos.skills_templates/<template>)
    2) рядом с исходниками (…/src/adaos/skills_templates/<template>)
    3) на случай запуска из корня репо (cwd/src/adaos/skills_templates/<template>)
    """
    # 1) пакетный ресурс
    try:
        import importlib.resources as ir
        import adaos.skills_templates as st_pkg

        res = ir.files(st_pkg) / template
        with ir.as_file(res) as fp:
            p = Path(fp)
            if p.exists():
                return p
    except Exception:
        pass

    # 2) отталкиваемся от текущего файла
    here = Path(__file__).resolve()
    candidates = []
    # …/services/skill/scaffold.py -> …/src/adaos/skills_templates/<template>
    if len(here.parents) >= 3:
        candidates.append(here.parents[2] / "skills_templates" / template)
    if len(here.parents) >= 4:
        candidates.append(here.parents[3] / "src" / "adaos" / "skills_templates" / template)
    # 3) корень репозитория (если запускаем из него)
    candidates.append(Path.cwd() / "src" / "adaos" / "skills_templates" / template)

    for c in candidates:
        if c and c.exists():
            return c

    raise FileNotFoundError(f"template '{template}' not found; tried package resource and: " + ", ".join(str(c) for c in candidates if c))


def create(
    name: str,
    template: str = "demo_skill",
    *,
    register: bool = True,
    push: bool = False,
    version: str = "0.1.0",
) -> Path:
    """
    Создаёт новый навык из локального шаблона и (опционально) регистрирует его в БД.
    Если push=True — коммитит поддерево навыка в монорепо и пушит.
    """
    if not _name_re.match(name):
        raise ValueError("invalid skill name")

    ctx = get_ctx()
    skills_root = ctx.paths.skills_dir()
    target = _safe_subdir(skills_root, name)

    if target.exists():
        raise FileExistsError(f"skill '{name}' already exists at {target}")

    # найдём директорию шаблона устойчиво
    tpl_root = _resolve_template_dir(template)

    # скопируем шаблон
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tpl_root, target)  # читаемо и атомарно: падает, если target уже есть

    # гарантируем meta
    meta = target / "skill.yaml"
    if not meta.exists():
        meta.write_text(f"name: {name}\nversion: {version}\n", encoding="utf-8")

    # регистрация в локальном реестре (идемпотентно)
    if register:
        try:
            reg = SqliteSkillRegistry(sql=ctx.sql)
            if not reg.get(name):
                reg.register(name, pin=None, active_version=None, repo_url=None)
        except Exception:
            # тест/ранняя стадия — не валим создание навыка из-за реестра
            pass

    emit(ctx.bus, "skill.created", {"name": name, "template": template}, "skill.scaffold")

    # 2) Если есть репозиторий и это не тесты — включим новый subpath в sparse-checkout
    repo_ready = (skills_root / ".git").exists()
    is_testing = os.getenv("ADAOS_TESTING") == "1"
    if repo_ready and not is_testing:
        try:
            # собираем целевой набор путей из реестра (и точно включаем текущий)
            names = [r.name for r in reg.list()]
            if name not in names:
                names.append(name)

            # пересобираем sparse-набор и подтягиваем индекс
            ctx.git.sparse_init(str(skills_root), cone=False)
            ctx.git.sparse_set(str(skills_root), names, no_cone=True)
            # pull не обязателен для локального коммита, но вреда не будет:
            # ctx.git.pull(str(skills_root))
        except Exception as e:
            emit(ctx.bus, "skill.warn", {"name": name, "warn": f"sparse_set_failed: {e!r}"}, "skill.scaffold")

    # 3) Коммитим только подпапку навыка (если просили)
    if push and repo_ready and not is_testing:
        msg = f"feat(skill): init {name} from template {template}"
        sha = ctx.git.commit_subpath(
            str(skills_root),
            subpath=name,
            message=msg,
            author_name=ctx.settings.git_author_name,
            author_email=ctx.settings.git_author_email,
            signoff=False,
        )
        if sha != "nothing-to-commit" and push:
            ctx.git.push(str(skills_root))
        emit(ctx.bus, "skill.pushed", {"name": name, "sha": sha}, "skill.scaffold")

    return target


# совместимость со старым API
def create_skill(name: str, template: str = "demo_skill", *, register: bool = True, push: bool = False) -> Path:
    return create(name, template, register=register, push=push)
