import typer
from db import init_db

app = typer.Typer()


@app.command("init")
def init():
    """Создать базу данных"""
    print("init")
    init_db()
    print("[green]База данных инициализирована[/green]")


def list_versions(skill_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT active_version FROM skills WHERE name = ?", (skill_name,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else None
