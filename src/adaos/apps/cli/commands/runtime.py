# src/adaos/apps/cli/commands/runtime.py
import asyncio
import typer
from rich import print
from pathlib import Path
import sys
from adaos.apps.bootstrap import get_ctx
from adaos.domain import ProcessSpec

app = typer.Typer()


@app.command("logs")
def show_logs():
    """Показать логи Runtime"""
    log_file = Path("runtime/runtime.log")
    if log_file.exists():
        print(f"[bold cyan]{log_file.read_text(encoding='utf-8')}[/bold cyan]")
    else:
        print("[yellow]Логов пока нет.[/yellow]")


@app.command("restart")
def restart_runtime():
    """Перезапустить Runtime (заглушка для MVP)"""
    print("[cyan]Runtime перезапущен (MVP-заглушка)[/cyan]")


@app.command("start-cmd")
def start_cmd(*cmd: str):
    """Запустить внешнюю команду как процесс AdaOS (пример: adaos runtime start-cmd python -c "print('ok')")"""
    if not cmd:
        raise typer.BadParameter("Укажите команду")
    ctx = get_ctx()
    handle = asyncio.run(ctx.proc.start(ProcessSpec(name="user-cmd", cmd=list(cmd))))
    typer.echo(f"started: {handle}")


@app.command("start-demo")
def start_demo():
    """Запустить демо-корутину на 2 секунды"""

    async def demo():
        import asyncio

        await asyncio.sleep(2)

    ctx = get_ctx()
    handle = asyncio.run(ctx.proc.start(ProcessSpec(name="demo", entrypoint=demo)))
    typer.echo(f"started: {handle}")


@app.command("stop")
def stop(handle: str, timeout: float = 3.0):
    ctx = get_ctx()
    asyncio.run(ctx.proc.stop(handle, timeout_s=timeout))
    typer.echo("stopped")


@app.command("status")
def status(handle: str):
    ctx = get_ctx()
    s = asyncio.run(ctx.proc.status(handle))
    typer.echo(s)
