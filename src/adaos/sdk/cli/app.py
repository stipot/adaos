import os
import shutil
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
from adaos.sdk.skills.i18n import _
import typer
from pathlib import Path
from adaos.sdk.utils.setup_env import prepare_environment
from adaos.sdk.context import BASE_DIR

from adaos.sdk.cli.commands import db, skill, runtime, llm, ovos, tests, tts, rhasspy, native
from adaos.sdk.cli.commands import tests as tests_cmd

app = typer.Typer(help=_("cli.help"))


def ensure_environment(ctx: typer.Context):
    """Проверяем, инициализировано ли окружение"""
    if ctx.invoked_subcommand == "reset":
        return  # пропускаем auto-setup для reset
    if not Path(BASE_DIR).exists():
        typer.echo(_("cli.no_env_creating"))
        prepare_environment()


@app.callback()
def auto_setup(ctx: typer.Context):
    """Вызывается перед любыми подкомандами"""
    ensure_environment(ctx)


@app.command()
def reset():
    """Сброс окружения AdaOS"""
    if Path(BASE_DIR).exists():
        shutil.rmtree(Path(BASE_DIR))
        typer.echo(_("cli.env_deleted"))
    else:
        typer.echo(_("cli.no_env"))


# Подкоманды
app.add_typer(db.app, name="db", help=_("cli.help_db"))
app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
app.add_typer(tests_cmd.app, name="tests", help=_("cli.help_test"))
app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))
app.add_typer(ovos.app, name="ovos")
app.add_typer(tts.app, name="tts")
app.add_typer(rhasspy.app, name="rhasspy")
app.add_typer(native.app, name="")

if __name__ == "__main__":
    app()
