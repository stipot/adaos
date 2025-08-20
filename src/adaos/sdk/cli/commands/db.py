# src\adaos\sdk\cli\commands\db.py
import typer
from adaos.agent.db.sqlite import init_db

app = typer.Typer()


@app.command("init")
def init():
    """Создать базу данных"""
    print("init")
    init_db()
    print("[green]База данных инициализирована[/green]")
