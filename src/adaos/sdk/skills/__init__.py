from __future__ import annotations
from pathlib import Path
from typing import Optional, List

from adaos.apps.bootstrap import get_ctx
from adaos.services.skill.manager import SkillManager

from adaos.adapters.skills.mono_repo import MonoSkillRepository
from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = MonoSkillRepository(
        paths=ctx.paths,
        git=ctx.git,
        url=ctx.settings.skills_monorepo_url,
        branch=ctx.settings.skills_monorepo_branch,
    )
    reg = SqliteSkillRegistry(sql=ctx.sql)
    return SkillManager(
        git=ctx.git,
        paths=ctx.paths,
        caps=ctx.caps,
        settings=ctx.settings,
        registry=reg,
        repo=repo,
        bus=ctx.bus,
    )


# ---------------------------- utils ----------------------------


def _resolve_template_dir(template: str) -> Path:
    """
    Находит директорию шаблона навыка по нескольким стратегиям, чтобы
    код работал и в dev-репозитории, и из установленного пакета, и в песочнице.
    """
    # 1) Попытка через importlib.resources (если шаблоны входят в пакет)
    try:
        import importlib.resources as ir
        import adaos.skills_templates as st_pkg  # пакет должен существовать

        p = ir.files(st_pkg) / template
        # as_file нужен для zip/namespace случаев; вернёт настоящий Path
        with ir.as_file(p) as fp:
            fp = Path(fp)
            if fp.exists():
                return fp
    except Exception:
        pass

    # 2) Относительно расположения текущего файла (site-packages или src)
    here = Path(__file__).resolve()
    candidates = [
        # src/adaos/sdk/skills/__init__.py -> вверх до src/adaos/skills_templates/<template>
        here.parents[3] / "src" / "adaos" / "skills_templates" / template if len(here.parents) >= 4 else None,
        # установленный пакет рядом с модулем: adaos/skills_templates/<template>
        here.parents[2] / "skills_templates" / template if len(here.parents) >= 3 else None,
        # 3) На случай запуска из корня репозитория
        Path.cwd() / "src" / "adaos" / "skills_templates" / template,
    ]
    for c in [c for c in candidates if c]:
        if c.exists():
            return c

    raise FileNotFoundError(f"Template not found: {template} " f"(tried package resource and {', '.join(str(c) for c in candidates if c)})")


# ---------------------------- API ----------------------------


def install(name: str) -> str:
    return _mgr().install(name)


def uninstall(name: str) -> str:
    return _mgr().uninstall(name)


def pull(name: str) -> str:
    return _mgr().pull(name)


def push(name: str, message: str, signoff: bool = False) -> str:
    return _mgr().push(name, message, signoff=signoff)


def list_installed() -> List[str]:
    m = _mgr()
    return [r.name for r in m.list_installed() if getattr(r, "installed", True)]


def create(name: str, template: str = "demo_skill") -> str:
    # если существует сервисный scaffolder — пусть он рулит
    try:
        from adaos.services.skill.scaffold import create as _create

        return str(_create(name, template=template))
    except Exception:
        ctx = get_ctx()
        src = _resolve_template_dir(template)
        dst = Path(ctx.paths.skills_dir()) / name
        from shutil import copytree

        copytree(src, dst, dirs_exist_ok=True)
        return str(dst)


def install_all(limit: Optional[int] = None) -> List[str]:
    m = _mgr()
    try:
        m.repo.ensure()
    except Exception:
        pass

    names: List[str] = []
    # предпочитаем repo.list() -> объекты с .name
    if hasattr(m.repo, "list"):
        try:
            for it in m.repo.list() or []:
                names.append(getattr(it, "name", it))
        except Exception:
            pass

    if not names:
        for getter in ("available", "available_names", "list_available", "list_names"):
            if hasattr(m.repo, getter):
                try:
                    val = getattr(m.repo, getter)() or []
                    for it in val:
                        names.append(getattr(it, "name", it))
                    if names:
                        break
                except Exception:
                    pass

    if not names:
        names = ["weather_skill"]

    if limit and limit > 0:
        names = names[:limit]

    ok: List[str] = []
    for n in names:
        try:
            m.install(n)
            ok.append(n)
        except Exception:
            continue
    return ok
