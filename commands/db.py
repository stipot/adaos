import typer
from db import init_db

app = typer.Typer()


@app.command("init")
def init():
    """Создать базу данных"""
    print("init")
    init_db()
    print("[green]База данных инициализирована[/green]")
