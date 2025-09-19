# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations

import json
import os
import traceback

import typer

from adaos.sdk.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.runtime import (
    SkillPrepError,
    SkillPrepMissingFunctionError,
    SkillPrepScriptNotFoundError,
    SkillRuntimeError,
    run_skill_handler_sync,
    run_skill_prep,
)
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
    repo = ctx.skills_repo
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
            # TODO автоматически установить из репозитория
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


@app.command("run")
def run(
    skill: str = typer.Argument(..., help="Имя навыка (директория в skills_root)"),
    topic: str = typer.Option("nlp.intent.weather.get", "--topic", "-t", help="Топик/интент"),
    payload: str = typer.Option("{}", "--payload", "-p", help='JSON-пейлоад, например: \'{"city":"Berlin"}\''),
):
    """
    Запустить навык локально из каталога skills_root, определяемого через get_ctx().
    Пример:
    adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'
    """
    # парсим payload
    try:
        payload_obj = json.loads(payload) if payload else {}
        if not isinstance(payload_obj, dict):
            raise ValueError("payload должен быть JSON-объектом")
    except Exception as e:
        raise typer.BadParameter(f"Некорректный --payload: {e}")

    try:
        result = run_skill_handler_sync(skill, topic, payload_obj)
    except SkillRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"OK: {result!r}")


@app.command("prep")
def prep_command(skill_name: str):
    """Запуск стадии подготовки (discover) для навыка"""
    try:
        result = run_skill_prep(skill_name)
    except SkillPrepScriptNotFoundError:
        print(f"[red]{_('skill.prep.not_found', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)
    except SkillPrepMissingFunctionError:
        print(f"[red]{_('skill.prep.missing_func', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)
    except SkillPrepError as exc:
        print(f"[red]{_('skill.prep.failed', reason=str(exc))}[/red]")
        raise typer.Exit(code=1)

    if result.get("status") == "ok":
        print(f"[green]{_('skill.prep.success', skill_name=skill_name)}[/green]")
    else:
        reason = result.get("reason", "unknown")
        print(f"[red]{_('skill.prep.failed', reason=reason)}[/red]")
