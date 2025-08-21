# src\adaos\sdk\cli\commands\skill.py
import typer
from rich import print
import importlib.util
from pathlib import Path
from git import Repo
import yaml, json
import asyncio
import importlib.util
from adaos.sdk.llm.llm_client import generate_test_yaml, generate_skill
from adaos.agent.core.test_runner import TestRunner
from adaos.sdk.llm.process_llm_output import process_llm_output
from adaos.sdk.utils.git_utils import commit_skill_changes, rollback_last_commit
from adaos.agent.db.sqlite import list_skills, get_skill_versions, add_skill_version, list_versions
from adaos.sdk.skills.i18n import _
from adaos.sdk.context import set_current_skill, get_current_skill
from adaos.sdk.skill_service import (
    create_skill,
    push_skill,
    pull_skill,
    update_skill,
    install_skill,
    uninstall_skill,
    install_skill_dependencies,
    install_all_skills,
)
from adaos.sdk.bus import emit
from adaos.agent.core.event_bus import BUS
from dataclasses import asdict
from adaos.sdk.decorators import register_subscriptions
from adaos.sdk.skill_validator import validate_skill

app = typer.Typer(help=_("cli.help"))


@app.command("create")
def create_command(skill_name: str, template: str = typer.Option("basic", "--template", "-t", help=_("cli.template.help"))):
    """Создать новый навык из шаблона"""
    typer.echo(create_skill(skill_name, template))


@app.command("push")
def push_command(skill_name: str, message: str = typer.Option(_("skill.push_message"), "--message", "-m", help=_("cli.commit_message.help"))):
    """Отправить изменения навыка в monorepo"""
    if not set_current_skill(skill_name):
        typer.echo(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")
    else:
        typer.echo(push_skill(skill_name, message))


@app.command("pull")
def pull_command(skill_name: str):
    """Загрузить навык из monorepo"""
    typer.echo(pull_skill(skill_name))


@app.command("update")
def update_command(skill_name: str):
    """Обновить навык из monorepo"""
    if not set_current_skill(skill_name):
        typer.echo(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")
    else:
        typer.echo(update_skill())


@app.command("request")
def request_skill(user_request: str):
    """Создать навык по пользовательскому запросу"""
    print(f"[cyan]Запрос пользователя:[/cyan] {user_request}")
    test_yaml = generate_test_yaml(user_request)
    test_path = Path(f"runtime/tests/test_{hash(user_request)}.yaml")
    test_path.write_text(test_yaml, encoding="utf-8")

    runner = TestRunner()
    if runner.run_test(str(test_path)):
        print("[green]Навык уже существует и прошёл тест.[/green]")
        return

    print("[yellow]Навык не найден. Генерация нового...[/yellow]")
    skill_code = generate_skill(user_request)

    try:
        skill_dir = process_llm_output(skill_code)
        commit_skill_changes(skill_dir.name, f"Создан навык {skill_dir.name}")
        add_skill_version(skill_dir.name, "v1.0", str(skill_dir))
    except Exception as e:
        print(f"[red]Ошибка генерации навыка:[/red] {e}")
        rollback_last_commit()
        raise

    if runner.run_test(str(test_path)):
        print(f"[green]Навык {skill_dir.name} успешно создан и прошёл тест.[/green]")
    else:
        print(f"[red]Навык {skill_dir.name} не прошёл тест. Откат.[/red]")
        rollback_last_commit()


@app.command("list")
def list_installed_skills_cmd(json_output: bool = typer.Option(False, "--json", help="Вывод в JSON")):
    """Список установленных навыков"""
    skills = [s for s in list_skills() if s.get("installed", 1)]
    if json_output:
        payload = {"skills": [{"name": s["name"], "version": s.get("active_version") or "unknown"} for s in skills]}
        print(json.dumps(payload, ensure_ascii=False))
        return

    if not skills:
        print(f"[yellow]{_('skill.list.empty')}[/yellow]")
        return
    for s in skills:
        print(f"- {s['name']} ({_('skill.active_version')}: {s['active_version']})")


@app.command("versions")
def versions_command(skill_name: str):
    """Вывод версий заданного навыка"""
    version = list_versions(skill_name)
    if version:
        print(f"[green]{skill_name}[/green] — {_('skill.active_version')}: [yellow]{version}[/yellow]")
    else:
        print(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")


@app.command("rollback")
def rollback_skill():
    """Откат последнего изменения"""
    rollback_last_commit()
    print("[yellow]Откат произведён.[/yellow]")


@app.command("install")
def install_command(
    skill_name: str = typer.Argument(None),
    all: bool = typer.Option(False, "--all", help=_("skill.install_all.help")),  # «Установить все навыки из монорепо»
    limit: int = typer.Option(None, "--limit", help=_("skill.install_all.limit.help")),  # «Ограничить количество для --all»
):
    """Установить навык из monorepo или все навыки (--all)"""
    if all:
        installed = install_all_skills(limit=limit)
        if installed:
            print(f"[green]{_('skill.installed_many')}[/green] " + ", ".join(installed))
            raise typer.Exit(0)
        else:
            print(f"[yellow]{_('skill.install_all.empty')}[/yellow]")
            raise typer.Exit(1)

    if not skill_name:
        print(f"[red]{_('skill.install.missing_name')}[/red]")
        raise typer.Exit(2)

    typer.echo(install_skill(skill_name))


@app.command("uninstall")
def uninstall_command(skill_name: str):
    """Удалить навык у пользователя"""
    if not set_current_skill(skill_name):
        typer.echo(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")
    else:
        typer.echo(uninstall_skill(skill_name))


@app.command("prep")
def prep_command(skill_name: str):
    """Запуск стадии подготовки (discover) для навыка"""
    if not set_current_skill(skill_name):
        typer.echo(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)
    skill_path = get_current_skill().path

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


@app.command("run")
def run_skill(
    skill_name: str,
    intent: str,
    entities: str = "{}",
    via_event: bool = typer.Option(False, "--event", help="Отправить intent через шину событий"),
    wait_notify: bool = typer.Option(False, "--wait-notify", help="Дождаться первого ui.notify и вывести его"),
    timeout: float = typer.Option(2.0, "--timeout", help="Таймаут ожидания ui.notify, сек."),
):
    if not set_current_skill(skill_name):
        typer.echo(f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]")

    # Устанавливаем зависимости перед запуском
    install_skill_dependencies(get_current_skill().path)

    if via_event:
        # 1) Импортируем handlers, чтобы декоратор @subscribe заполнил реестр
        spec = importlib.util.spec_from_file_location("handler", get_current_skill().path / "handlers" / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 2) Регистрируем подписки в рабочем event loop
        async def _main():
            await register_subscriptions()
            notify_event = asyncio.Event()
            msg_holder = {"text": None}
            unsubscribe = None
            if wait_notify:

                async def tap(evt):
                    msg = evt.payload.get("text")
                    if msg is not None:
                        msg_holder["text"] = msg
                        notify_event.set()

                await BUS.subscribe("ui.notify", tap)
            await emit(f"nlp.intent.{intent}", json.loads(entities), actor="user:local", source="cli")
            if wait_notify:
                try:
                    await asyncio.wait_for(notify_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    print("[yellow]timeout waiting ui.notify[/yellow]")
                else:
                    print(msg_holder["text"])
            else:
                # дать шанс обработчику отработать
                await asyncio.sleep(0.05)

        asyncio.run(_main())
    else:
        # Старый путь: прямой вызов handler
        spec = importlib.util.spec_from_file_location("handler", get_current_skill().path / "handlers" / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.handle(intent, json.loads(entities), get_current_skill().path)


@app.command("validate")
def validate_command(
    skill_name: str = typer.Argument(None),
    strict: bool = typer.Option(False, "--strict", help="install-mode: warnings treated as errors"),
    json_output: bool = typer.Option(False, "--json", help="JSON отчёт"),
    probe_tools: bool = typer.Option(False, "--probe-tools", help="Пробный вызов инструментов (опасно)"),
):
    report = validate_skill(skill_name, install_mode=strict, probe_tools=json_output)
    if json_output:
        payload = {
            "ok": report.ok,
            "issues": [asdict(i) for i in report.issues],
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if not report.issues:
            print("[green]OK[/green]")
        else:
            for i in report.issues:
                lvl = "red" if i.level == "error" else "yellow"
                where = f" [{i.where}]" if i.where else ""
                print(f"[{lvl}]{i.level}[/{lvl}] {i.code}{where}: {i.message}")
    raise typer.Exit(0 if report.ok else 1)
