import typer
from rich import print
from adaos.test_runner import TestRunner

app = typer.Typer()


@app.command("run")
def run_test(test_file: str):
    """Запуск теста вручную"""
    runner = TestRunner()
    if runner.run_test(test_file):
        print("[green]Тест пройден успешно[/green]")
    else:
        print("[red]Тест не пройден[/red]")
        for log in runner.logs:
            print(f"[red]- {log}[/red]")
