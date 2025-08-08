import typer
from rich import print
from pathlib import Path

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
