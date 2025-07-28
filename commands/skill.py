import os
import typer
from rich import print
from pathlib import Path
from llm_client import generate_test_yaml, generate_skill
from test_runner import TestRunner
from process_llm_output import process_llm_output
from git_utils import commit_skill_changes, rollback_last_commit
from db import list_skills, get_skill_versions, add_skill_version, list_versions
from git import Repo
import yaml
from .skill_service import create_skill, push_skill, pull_skill, update_skill

app = typer.Typer(help="Управление навыками в monorepo")


@app.command("create")
def create_command(skill_name: str, template: str = typer.Option("basic", "--template", "-t", help="Шаблон навыка")):
    """Создать новый навык из шаблона"""
    typer.echo(create_skill(skill_name, template))


@app.command("push")
def push_command(skill_name: str, message: str = typer.Option("Обновление навыка", "--message", "-m", help="Комментарий к коммиту")):
    """Отправить изменения навыка в monorepo"""
    typer.echo(push_skill(skill_name, message))


@app.command("pull")
def pull_command(skill_name: str):
    """Загрузить навык из monorepo"""
    typer.echo(pull_skill(skill_name))


@app.command("update")
def update_command(skill_name: str):
    """Обновить навык из monorepo"""
    typer.echo(update_skill(skill_name))


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
def list_installed_skills():
    """Список установленных навыков"""
    skills = list_skills()
    if not skills:
        print("[yellow]Нет установленных навыков[/yellow]")
        return
    for s in skills:
        print(f"- {s['name']} (активная версия: {s['active_version']})")


@app.command("versions")
def versions(skill_name: str):
    """Вывод версий заданного навыка"""
    version = list_versions(skill_name)
    if version:
        print(f"[green]{skill_name}[/green] — активная версия: [yellow]{version}[/yellow]")
    else:
        print(f"[red]Навык '{skill_name}' не найден[/red]")


@app.command("rollback")
def rollback_skill():
    """Откат последнего изменения"""
    rollback_last_commit()
    print("[yellow]Откат произведён.[/yellow]")
