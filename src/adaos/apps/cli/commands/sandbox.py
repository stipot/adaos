"""
adaos sandbox run "python -c 'while True: pass'" --cpu 1 --wall 10
adaos sandbox run "python -c 'import time; time.sleep(2)'" --wall 1
"""

from __future__ import annotations
import shlex
from typing import List

import typer
from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.ports.sandbox import ExecLimits

app = typer.Typer(help="Песочница процессов (диагностика)")


def _parse_kv(items: List[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in items:
        if "=" not in s:
            raise typer.BadParameter(f"Invalid --env '{s}', use KEY=VAL")
        k, v = s.split("=", 1)
        out[k] = v
    return out


@app.command("profiles")
def profiles():
    """Показать доступные профили песочницы и их лимиты."""
    from adaos.services.sandbox.profiles import DEFAULT_PROFILES

    for name, lim in DEFAULT_PROFILES.items():
        typer.echo(f"{name:8} wall={lim.wall_time_sec}  cpu={lim.cpu_time_sec}  rss={lim.max_rss_mb}MB")


@app.command("run")
def run(
    cmd: str = typer.Argument(..., help="Команда (в кавычках)"),
    cwd: str = typer.Option(None, "--cwd", help="Рабочая директория (должна быть внутри BASE_DIR)"),
    # профили (перекрываются явными лимитами)
    profile: str = typer.Option("default", "--profile", "-p", help="Профиль лимитов: default|prep|handler|tool"),
    # явные лимиты (любое из них перекроет профиль)
    wall: float | None = typer.Option(None, "--wall", help="Таймаут выполнения, сек."),
    cpu: float | None = typer.Option(None, "--cpu", help="Лимит суммарного CPU-времени, сек."),
    rss: int | None = typer.Option(None, "--rss", help="Лимит RSS, МБ"),
    # окружение
    inherit_env: bool = typer.Option(False, "--inherit-env/--no-inherit-env", help="Наследовать безопасную часть системного окружения"),
    env: List[str] = typer.Option([], "--env", help="Доп. переменные окружения KEY=VAL (можно многократно)"),
):
    """
    Запустить команду в sandbox с профилем лимитов/явными лимитами и безопасным окружением.
    """
    ctx = get_ctx()

    # если задан хотя бы один лимит — используем явные лимиты, иначе профиль
    limits = None
    if any(x is not None for x in (wall, cpu, rss)):
        limits = ExecLimits(wall_time_sec=wall, cpu_time_sec=cpu, max_rss_mb=rss)

    res = ctx.sandbox.run(
        shlex.split(cmd),
        cwd=cwd,
        profile=profile,
        limits=limits,
        inherit_env=inherit_env,
        extra_env=_parse_kv(env),
    )
    typer.echo(f"exit={res.exit_code} timed_out={res.timed_out} reason={res.killed_reason}" f"\n--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}")
