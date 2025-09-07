from __future__ import annotations
import re, shutil
from pathlib import Path
from adaos.apps.bootstrap import get_ctx
from adaos.services.fs.safe_io import ensure_dir  # если удобно, иначе target.parent.mkdir(...)

_name_re = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _safe_subdir(root: Path, name: str) -> Path:
    p = (root / name).resolve()
    root = root.resolve()
    try:
        p.relative_to(root)
    except Exception:
        raise ValueError("unsafe path traversal")
    return p


def create_skill(name: str, template: str = "demo_skill", *, register: bool = True, push: bool = False) -> Path:
    if not _name_re.match(name):
        raise ValueError("invalid skill name")

    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    repo_ready = (skills_root / ".git").exists()

    target = _safe_subdir(skills_root, name)
    if target.exists():
        raise FileExistsError(f"skill '{name}' already exists at {target}")

    # путь: src/adaos/skills_templates/<template>
    tpl_root = Path(__file__).resolve().parents[2] / "skills_templates" / template
    if not tpl_root.exists():
        raise FileNotFoundError(f"template '{template}' not found at {tpl_root}")

    ensure_dir(str(target), ctx.fs)  # или: target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tpl_root, target, dirs_exist_ok=True)

    meta = target / "skill.yaml"
    if not meta.exists():
        meta.write_text(f"name: {name}\nversion: 0.1.0\n", encoding="utf-8")

    if register:
        try:
            from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

            reg = SqliteSkillRegistry(ctx.sql)
            if not reg.get(name):
                reg.register(name, pin=None)
        except Exception:
            # если в тестовом окружении реестр ещё не поднят — не валим create
            pass

    if push:
        if not repo_ready:
            raise RuntimeError("Cannot push: skills repo is not initialized. Run `adaos skill sync` once.")
        from adaos.services.git.safe_commit import sanitize_message, check_no_denied

        changed = ctx.git.changed_files(str(skills_root), subpath=name)
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        sha = ctx.git.commit_subpath(
            str(skills_root),
            subpath=name,
            message=sanitize_message(f"feat(skill): init {name} from template {template}"),
            author_name=ctx.settings.git_author_name,
            author_email=ctx.settings.git_author_email,
            signoff=False,
        )
        if sha != "nothing-to-commit":
            ctx.git.push(str(skills_root))

    return target
