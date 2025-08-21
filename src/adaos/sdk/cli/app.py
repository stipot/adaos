# src\adaos\sdk\cli\app.py
from __future__ import annotations
import os, sys, shutil
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
import typer

load_dotenv(find_dotenv())

from adaos.sdk.skills.i18n import _
from pathlib import Path
from adaos.sdk.utils.setup_env import prepare_environment
from adaos.sdk.context import BASE_DIR

# общие подкоманды
from adaos.sdk.cli.commands import db, monitor, skill, runtime, llm, tests as tests_cmd, api

# интеграции
from adaos.sdk.cli.commands import native
from adaos.sdk.cli.commands import ovos as ovos_cmd
from adaos.sdk.cli.commands import rhasspy as rhasspy_cmd

app = typer.Typer(help=_("cli.help"))


def _read(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().lower()


def ensure_environment(ctx: typer.Context):
    """Проверяем, инициализировано ли окружение"""
    if os.getenv("ADAOS_TESTING") == "1":
        return  # В CI/юнит‑тестах окружение не готовим и ничего не скачиваем
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
    p = Path(BASE_DIR)
    if p.exists():
        shutil.rmtree(p)
        typer.echo(_("cli.env_deleted"))
    else:
        typer.echo(_("cli.no_env"))


def _write_env_var(key: str, value: str, dotenv_path: Path | None = None):
    """Примитивно патчим .env (или создаём). Без зависимостей от python-dotenv для записи."""
    dotenv_path = dotenv_path or Path(find_dotenv()) or Path(".env")
    lines: list[str] = []
    if dotenv_path.exists():
        with dotenv_path.open("r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    with dotenv_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _restart_self():
    # Перезапускаем текущий процесс CLI (кроссплатформенно)
    # Предполагаем точку входа `python -m adaos` или консольный скрипт `adaos`.
    if hasattr(sys, "frozen"):
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        os.execl(sys.executable, sys.executable, "-m", "adaos", *sys.argv[1:])


switch_app = typer.Typer(help="Переключение бекендов / оснасток")


@switch_app.command("tts")
def switch_tts(mode: str = typer.Argument(..., help="native | ovos | rhasspy")):
    mode = mode.strip().lower()
    if mode not in {"native", "ovos", "rhasspy"}:
        raise typer.BadParameter("Allowed: native, ovos, rhasspy")
    _write_env_var("ADAOS_TTS", mode)
    typer.echo(f"[AdaOS] ADAOS_TTS set to '{mode}'. Reloading ...")
    _restart_self()


@switch_app.command("stt")
def switch_stt(mode: str = typer.Argument(..., help="vosk | rhasspy | ovos | native")):
    mode = mode.strip().lower()
    if mode not in {"vosk", "rhasspy", "ovos", "native"}:
        raise typer.BadParameter("Allowed: vosk, rhasspy, ovos, native")
    _write_env_var("ADAOS_STT", mode)
    typer.echo(f"[AdaOS] ADAOS_STT set to '{mode}'. Reloading ...")
    _restart_self()


# Подкоманды
app.add_typer(db.app, name="db", help=_("cli.help_db"))
app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
app.add_typer(tests_cmd.app, name="tests", help=_("cli.help_test"))
app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))
app.add_typer(api.app, name="api")
app.add_typer(monitor.app, name="monitor")

# ---- Фильтрация интеграций по ENV ----
_tts = _read("ADAOS_TTS", "native")
if _tts == "ovos":
    app.add_typer(ovos_cmd.app, name="ovos", help="OVOS-интеграция")
elif _tts == "rhasspy":
    app.add_typer(rhasspy_cmd.app, name="rhasspy", help="Rhasspy-интеграция")
else:
    # корневые команды «нативного» профиля
    app.add_typer(native.app, name="", help="Нативные команды (по умолчанию)")

if __name__ == "__main__":
    app()
