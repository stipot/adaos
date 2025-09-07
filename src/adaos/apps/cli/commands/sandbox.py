from __future__ import annotations
import typer, shlex
from adaos.apps.bootstrap import get_ctx
from adaos.ports.sandbox import ExecLimits

app = typer.Typer(help="Песочница процессов (диагностика)")


@app.command("run")
def run(
    cmd: str = typer.Argument(..., help="Команда (в кавычках)"),
    cwd: str = typer.Option(None, "--cwd", help="Рабочая директория (должна быть внутри BASE_DIR)"),
    wall: float = typer.Option(30.0, "--wall", help="Таймаут, сек."),
    cpu: float = typer.Option(None, "--cpu", help="Лимит CPU-времени, сек."),
    rss: int = typer.Option(None, "--rss", help="Лимит памяти, МБ"),
):
    ctx = get_ctx()
    limits = ExecLimits(wall_time_sec=wall, cpu_time_sec=cpu, max_rss_mb=rss)
    res = ctx.sandbox.run(shlex.split(cmd), cwd=cwd, limits=limits)
    typer.echo(f"exit={res.exit_code} timed_out={res.timed_out} reason={res.killed_reason}\n--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}")
