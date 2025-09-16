# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations
import typer
import json
import asyncio
import importlib.util
from adaos.sdk.bus import emit
from typing import Optional
import os, traceback
from pathlib import Path
from adaos.sdk.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteSkillRegistry
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.sdk.context import set_current_skill, get_current_skill
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


_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")


def _resolve_skill_dir(skill_name: str) -> Path:
    """
    Ищем директорию навыка по имени в каталоге skills_root из контекста.
    1) <skills_root>/<skill_name>
    2) Fallback: поиск по подкаталогам с наличием одного из манифестов.
    """
    # TODO логику перенести на уровень сервиса
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())  # ожидается, что в контексте настроено
    direct = skills_root / skill_name
    if direct.is_dir():
        return direct

    # fallback-поиск: skills_root/**/(skill.yaml|manifest.yaml|adaos.skill.yaml)
    matches = []
    for p in skills_root.rglob("*"):
        if p.is_file() and p.name in _MANIFEST_NAMES and p.parent.name == skill_name:
            matches.append(p.parent)

    if not matches:
        raise typer.BadParameter(f"Skill '{skill_name}' not найден в {skills_root}. " f"Ожидал {skills_root / skill_name} или подкаталог с манифестом.")
    if len(matches) > 1:
        # неоднозначность — просим уточнить
        found = "\n - " + "\n - ".join(str(m) for m in matches)
        raise typer.BadParameter(f"Найдено несколько директорий с именем '{skill_name}':{found}\n" f"Уточните путь или переименуйте дубликаты.")
    return matches[0]


def _import_handler(handler_file: Path):
    spec = importlib.util.spec_from_file_location("skill_handler", handler_file)
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"Не удалось импортировать {handler_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "handle"):
        raise typer.BadParameter("Файл не содержит функцию handle(topic, payload)")
    return module.handle


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
    # TODO логику перенести на уровень сервиса
    # 1) находим папку навыка через get_ctx()
    skill_dir = _resolve_skill_dir(skill)
    handler_path = skill_dir / "handlers" / "main.py"
    if not handler_path.is_file():
        raise typer.BadParameter(f"Не найден обработчик: {handler_path}")

    # 2) импортируем handle(topic, payload)
    handle_fn = _import_handler(handler_path)

    # 3) парсим payload
    try:
        payload_obj = json.loads(payload) if payload else {}
        if not isinstance(payload_obj, dict):
            raise ValueError("payload должен быть JSON-объектом")
    except Exception as e:
        raise typer.BadParameter(f"Некорректный --payload: {e}")

    # 4) вызываем
    async def main():
        res = handle_fn(topic, payload_obj)
        if asyncio.iscoroutine(res):
            res = await res
        typer.echo(f"OK: {res!r}")

    asyncio.run(main())


@app.command("prep")
def prep_command(skill_name: str):
    """Запуск стадии подготовки (discover) для навыка"""
    # TODO логику перенести на уровень сервиса
    set_current_skill(skill_name)
    ctx = get_ctx()
    skill_path = ctx.paths.skills_dir() / skill_name

    prep_script = skill_path / "prep" / "prepare.py"
    if not prep_script.exists():
        print(f"[red]{_('skill.prep.not_found', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)

    # Динамически импортируем prepare.py
    spec = importlib.util.spec_from_file_location("prepare", prep_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if hasattr(module, "run_prep"):
        result = module.run_prep(skill_path)
        if result["status"] == "ok":
            print(f"[green]{_('skill.prep.success', skill_name=skill_name)}[/green]")
        else:
            print(f"[red]{_('skill.prep.failed', reason=result['reason'])}[/red]")
    else:
        print(f"[red]{_('skill.prep.missing_func', skill_name=skill_name)}[/red]")
