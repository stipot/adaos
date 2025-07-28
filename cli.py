from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
import typer
import os

from commands import db, skill, test, runtime

app = typer.Typer(help="AdaOS CLI – управление навыками, тестами и Runtime")

# Подкоманды
app.add_typer(db.app, name="db", help="Подготовка БД")
app.add_typer(skill.app, name="skill", help="Работа с навыками")
app.add_typer(test.app, name="test", help="Работа с тестами")
app.add_typer(runtime.app, name="runtime", help="Управление Runtime")

if __name__ == "__main__":
    app()
