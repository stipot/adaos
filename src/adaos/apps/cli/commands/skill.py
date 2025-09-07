from __future__ import annotations
import typer
from typing import Optional
import os
from pathlib import Path

from adaos.apps.bootstrap import get_ctx
from adaos.adapters.skills.mono_repo import MonoSkillRepository
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteSkillRegistry
from adaos.sdk.skill_service import push_skill
from adaos.sdk.skill_service import create_skill as _create_skill

app = typer.Typer(help="Управление навыками (монорепозиторий, реестр в БД)")


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = MonoSkillRepository(paths=ctx.paths, git=ctx.git, url=ctx.settings.skills_monorepo_url, branch=ctx.settings.skills_monorepo_branch)
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


@app.command("list")
def list_skills(show_fs: bool = typer.Option(False, "--fs", help="Показать фактическое наличие на диске")):
    mgr = _mgr()
    rows = mgr.list_installed()
    if not rows:
        typer.echo("Установленных навыков нет (реестр пуст).")
    else:
        for r in rows:
            typer.echo(f"{r.name:32} pin={r.pin or '-'}  installed_at={int(r.installed_at)}")
    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(f"⚠ На диске отсутствуют (есть в реестре): {', '.join(sorted(missing))}")
        if extra:
            typer.echo(f"⚠ На диске лишние (нет в реестре): {', '.join(sorted(extra))}")


@app.command("sync")
def sync():
    """Применяет sparse-set к набору из реестра и делает pull."""
    mgr = _mgr()
    mgr.sync()
    typer.echo("Синхронизация завершена.")


@app.command("install")
def install(name: str = typer.Argument(..., help="Имя навыка (подпапка монорепо)"), pin: Optional[str] = typer.Option(None, "--pin", help="Закрепить на коммите/теге (опц.)")):
    mgr = _mgr()
    meta = mgr.install(name, pin=pin)
    typer.echo(f"Установлен: {meta.id.value} v{meta.version} @ {meta.path}")


@app.command("remove")
def remove(name: str):
    mgr = _mgr()
    mgr.remove(name)
    typer.echo(f"Удалён: {name}")


@app.command("reconcile-fs-to-db")
def reconcile_fs_to_db():
    """Обходит {skills_dir} и проставляет installed=1 для найденных папок (кроме .git).
    Не трогает active_version/repo_url.
    """
    mgr = _mgr()
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    if not root.exists():
        typer.echo("Папка навыков ещё не создана. Сначала выполните: adaos skill sync")
        raise typer.Exit(1)
    found = []
    for name in os.listdir(root):
        if name == ".git":
            continue
        p = root / name
        if p.is_dir():
            mgr.reg.register(name)  # installed=1
            found.append(name)
    typer.echo(f"В реестр добавлено/актуализировано: {', '.join(found) if found else '(ничего)'}")


@app.command("push")
def push_command(
    skill_name: str = typer.Argument(..., help="Имя навыка (подпапка монорепо)"),
    message: str = typer.Option(..., "--message", "-m", help="Сообщение коммита"),
    signoff: bool = typer.Option(False, "--signoff", help="Добавить Signed-off-by"),
):
    """
    Закоммитить изменения ТОЛЬКО внутри подпапки навыка и выполнить git push.
    Защищён политиками: skills.manage + git.write + net.git.
    """
    res = push_skill(skill_name, message, signoff=signoff)
    if res == "nothing-to-push" or res == "nothing-to-commit":
        typer.echo("Nothing to push.")
    else:
        typer.echo(f"Pushed {skill_name}: {res}")


@app.command("create")
def create_command(
    name: str = typer.Argument(..., help="Имя навыка"),
    template: str = typer.Option("demo_skill", "--template", "-t"),
    register: bool = typer.Option(True, "--register/--no-register"),
    push: bool = typer.Option(False, "--push/--no-push"),
):
    p = _create_skill(name, template, register=register, push=push)
    typer.echo(f"Created: {p}")
