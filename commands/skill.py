import typer
from rich import print
from pathlib import Path
from llm_client import generate_test_yaml, generate_skill
from test_runner import TestRunner
from process_llm_output import process_llm_output
from git_utils import commit_skill_changes, rollback_last_commit
from db import list_skills, get_skill_versions, add_skill_version

app = typer.Typer()


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
        print("[yellow]Нет установленных навыков.[/yellow]")
        return
    for s in skills:
        print(f"- [bold]{s['name']}[/bold] (активная версия: {s['active_version']})")


@app.command("versions")
def skill_versions(skill_name: str):
    """Список версий навыка"""
    versions = get_skill_versions(skill_name)
    for v in versions:
        print(f"- версия: [bold]{v['version']}[/bold], статус: {v['status']}")


@app.command("rollback")
def rollback_skill():
    """Откат последнего изменения"""
    rollback_last_commit()
    print("[yellow]Откат произведён.[/yellow]")
