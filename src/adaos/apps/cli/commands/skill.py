# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations
import typer
import json
from typing import Optional
import os, traceback
from pathlib import Path

from adaos.apps.bootstrap import get_ctx
from adaos.adapters.skills.mono_repo import MonoSkillRepository
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteSkillRegistry
from adaos.sdk.skills import (
    push as push_skill,
    pull as pull_skill,
    install as install_skill,
    uninstall as uninstall_skill,
    install_all as install_all_skills,
    create as create_skill,
)


app = typer.Typer(help="Управление навыками (монорепозиторий, реестр в БД)")


def _run_safe(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = MonoSkillRepository(paths=ctx.paths, git=ctx.git, url=ctx.settings.skills_monorepo_url, branch=ctx.settings.skills_monorepo_branch)
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=getattr(ctx, "bus", None), caps=ctx.caps)


@_run_safe
@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Вывести JSON"),
    show_fs: bool = typer.Option(False, "--fs", help="Показать сверку с файловой системой"),
):
    """
    Список установленных навыков из реестра.
    JSON-формат: {"skills": [{"name": "...", "version": "..."}, ...]}
    """
    mgr = _mgr()
    rows = mgr.list_installed()  # SkillRecord[]

    if json_output:
        payload = {
            "skills": [
                {
                    "name": r.name,
                    # тестам важен только name, но version полезно оставить
                    "version": getattr(r, "active_version", None) or "unknown",
                }
                for r in rows
                # оставляем только действительно установленные (если поле есть)
                if bool(getattr(r, "installed", True))
            ]
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        typer.echo("Установленных навыков нет (реестр пуст).")
    else:
        for r in rows:
            if not bool(getattr(r, "installed", True)):
                continue
            av = getattr(r, "active_version", None) or "unknown"
            typer.echo(f"- {r.name} (version: {av})")

    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows if bool(getattr(r, "installed", True))}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(f"⚠ На диске отсутствуют (есть в реестре): {', '.join(sorted(missing))}")
        if extra:
            typer.echo(f"⚠ На диске лишние (нет в реестре): {', '.join(sorted(extra))}")


@_run_safe
@app.command("sync")
def sync():
    """Применяет sparse-set к набору из реестра и делает pull."""
    mgr = _mgr()
    mgr.sync()
    typer.echo("Синхронизация завершена.")


@_run_safe
@app.command("uninstall")
def uninstall(name: str):
    mgr = _mgr()
    mgr.uninstall(name)
    typer.echo(f"Удалён: {name}")


@_run_safe
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


@_run_safe
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


@_run_safe
@app.command("create")
def cmd_create(name: str, template: str = typer.Option("demo_skill", "--template", "-t")):
    p = create_skill(name, template=template)
    typer.echo(f"Created: {p}")


@_run_safe
@app.command("install")
def cmd_install(name: str):
    msg = install_skill(name)
    typer.echo(msg)
