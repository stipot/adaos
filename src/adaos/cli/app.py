import os
import shutil
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
from adaos.i18n.translator import _
import typer
from pathlib import Path
from adaos.utils import setup_env

BASE_DIR = Path(os.getenv("BASE_DIR", str(Path.home())) + "/.adaos")
from adaos.cli.commands import db, skill, test, runtime, llm

app = typer.Typer(help=_("cli.help"))


def ensure_environment(ctx: typer.Context):
    """Проверяем, инициализировано ли окружение"""
    if ctx.invoked_subcommand == "reset":
        return  # пропускаем auto-setup для reset
    if not BASE_DIR.exists():
        typer.echo(_("cli.no_env_creating"))
        setup_env.prepare_environment()


@app.callback()
def auto_setup(ctx: typer.Context):
    """Вызывается перед любыми подкомандами"""
    ensure_environment(ctx)


@app.command()
def reset():
    """Сброс окружения AdaOS"""
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
        typer.echo(_("cli.env_deleted"))
    else:
        typer.echo(_("cli.no_env"))


# Подкоманды
app.add_typer(db.app, name="db", help=_("cli.help_db"))
app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
app.add_typer(test.app, name="test", help=_("cli.help_test"))
app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))

if __name__ == "__main__":
    app()
