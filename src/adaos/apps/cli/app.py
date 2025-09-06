# src/adaos/apps/cli/app.py
from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv, find_dotenv
import typer

# загружаем .env один раз (для переменных вроде ADAOS_TTS/ADAOS_STT)
load_dotenv(find_dotenv())

from adaos.sdk.skills.i18n import _
from adaos.sdk.utils.setup_env import prepare_environment

# контекст и настройки (PR-2)
from adaos.services.settings import Settings
from adaos.apps.bootstrap import init_ctx, get_ctx, reload_ctx

# общие подкоманды
from adaos.apps.cli.commands import monitor, skill, runtime, llm, tests as tests_cmd, api, scenario

# интеграции
from adaos.apps.cli.commands import native
from adaos.apps.cli.commands import ovos as ovos_cmd
from adaos.apps.cli.commands import rhasspy as rhasspy_cmd

app = typer.Typer(help=_("cli.help"))

# -------- вспомогательные --------


def _read(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().lower()


def _write_env_var(key: str, value: str, dotenv_path: Path | None = None):
    """Примитивно патчим .env (или создаём)."""
    from dotenv import find_dotenv  # локальный импорт, чтобы не тянуть при тестах

    dotenv_path = dotenv_path or Path(find_dotenv() or ".env")
    lines: list[str] = []
    if dotenv_path.exists():
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    dotenv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _restart_self():
    # Перезапускаем текущий процесс CLI (кроссплатформенно)
    if hasattr(sys, "frozen"):
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        os.execl(sys.executable, sys.executable, "-m", "adaos", *sys.argv[1:])


def ensure_environment():
    """Проверяем, инициализировано ли окружение; вызывается после сборки контекста."""
    if os.getenv("ADAOS_TESTING") == "1":
        return  # В CI/юнит-тестах окружение не готовим и ничего не скачиваем

    ctx = get_ctx()
    base_dir = Path(ctx.settings.base_dir)

    # для совместимости со старым кодом, который читает env напрямую
    os.environ["ADAOS_BASE_DIR"] = ctx.settings.base_dir
    os.environ["ADAOS_PROFILE"] = ctx.settings.profile

    if not base_dir.exists():
        typer.echo(_("cli.no_env_creating"))
        prepare_environment()


# -------- корневой callback (composition root) --------


@app.callback()
def main(
    ctx: typer.Context,
    base_dir: Optional[str] = typer.Option(None, "--base-dir", help="Базовый каталог AdaOS (по умолчанию ~/.adaos или из .env/ENV)"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Профиль настроек (по умолчанию 'default' или из .env/ENV)"),
    reload: bool = typer.Option(False, "--reload", help="Пересобрать контекст с новыми настройками"),
):
    """
    Вызывается перед любыми подкомандами: строит (или пересобирает) контекст и гарантирует готовность окружения.
    """
    # 1) собрать Settings из источников с приоритетом CLI > ENV > .env
    settings = Settings.from_sources(
        cli={
            "ADAOS_BASE_DIR": base_dir,
            "ADAOS_PROFILE": profile,
        }
    )

    # 2) создать/пересобрать единый контекст процесса
    if reload:
        reload_ctx(ADAOS_BASE_DIR=base_dir, ADAOS_PROFILE=profile)
    else:
        init_ctx(settings)

    # 3) автоподготовка окружения (без BASE_DIR, используем предзагруженные настройки)
    if ctx.invoked_subcommand != "reset":
        ensure_environment()


# -------- команды обслуживания --------


@app.command("reset")
def reset():
    """Сброс окружения AdaOS (удаляет base_dir)."""
    base_dir = Path(get_ctx().settings.base_dir)
    if base_dir.exists():
        shutil.rmtree(base_dir)
        typer.echo(_("cli.env_deleted"))
    else:
        typer.echo(_("cli.no_env"))


# -------- переключатели профилей интеграций --------

switch_app = typer.Typer(help="Переключение бэкендов / оснасток")


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


# -------- подкоманды --------

app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
app.add_typer(tests_cmd.app, name="tests", help=_("cli.help_test"))
app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))
app.add_typer(api.app, name="api")
app.add_typer(monitor.app, name="monitor")
app.add_typer(scenario.scenario_app, name="scenario")
app.add_typer(switch_app, name="switch", help="Переключение профилей интеграций")

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
