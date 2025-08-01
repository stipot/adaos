import os
import typer
from rich import print
import importlib.util
from pathlib import Path
from git import Repo
import yaml, json
from adaos.llm.llm_client import generate_test_yaml, generate_skill
from adaos.core.test_runner import TestRunner
from adaos.llm.process_llm_output import process_llm_output
from adaos.utils.git_utils import commit_skill_changes, rollback_last_commit
from adaos.db.db import list_skills, get_skill_versions, add_skill_version, list_versions
from adaos.i18n.translator import _
from adaos.sdk.context import PACKAGE_DIR, BASE_DIR, SKILLS_DIR, TEMPLATES_DIR, MONOREPO_URL, get_current_skill_path, set_current_skill, current_skill_name, current_skill_path
from adaos.sdk.skill_service import (
    create_skill,
    push_skill,
    pull_skill,
    update_skill,
    install_skill,
    uninstall_skill,
    install_skill_dependencies,
)

app = typer.Typer(help=_("cli.help"))


@app.command("create")
def create_command(skill_name: str, template: str = typer.Option("basic", "--template", "-t", help=_("cli.template.help"))):
    """Создать новый навык из шаблона"""
    typer.echo(create_skill(skill_name, template))


@app.command("push")
def push_command(skill_name: str, message: str = typer.Option(_("skill.push_message"), "--message", "-m", help=_("cli.commit_message.help"))):
    """Отправить изменения навыка в monorepo"""
    set_current_skill(skill_name)
    typer.echo(push_skill(message))


@app.command("pull")
def pull_command(skill_name: str):
    """Загрузить навык из monorepo"""
    typer.echo(pull_skill(skill_name))


@app.command("update")
def update_command(skill_name: str):
    """Обновить навык из monorepo"""
    set_current_skill(skill_name)
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
def list_installed_skills_cmd():
    """Список установленных навыков"""
    skills = list_skills()
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
def install_command(skill_name: str):
    """Установить навык из monorepo"""
    typer.echo(install_skill(skill_name))


@app.command("uninstall")
def uninstall_command(skill_name: str):
    """Удалить навык у пользователя"""
    typer.echo(uninstall_skill(skill_name))


@app.command("prep")
def prep_command(skill_name: str):
    """Запуск стадии подготовки (discover) для навыка"""
    set_current_skill(skill_name)
    skill_path = Path(SKILLS_DIR) / skill_name

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
def run_skill(skill_name: str, intent: str, entities: str = "{}"):
    set_current_skill(skill_name)

    # Устанавливаем зависимости перед запуском
    install_skill_dependencies(current_skill_path)

    # Загружаем handler
    spec = importlib.util.spec_from_file_location("handler", current_skill_path / "handlers" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.handle(intent, json.loads(entities), current_skill_path)
