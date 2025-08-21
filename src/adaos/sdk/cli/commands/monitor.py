import typer, json, time
from pathlib import Path
from adaos.sdk.context import get_base_dir

app = typer.Typer(help="Monitoring tools")


@app.command("events")
def monitor_events(
    topic: str = typer.Option(None, "--topic", "-t", help="Фильтр по префиксу топика"),
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Следить за логом"),
):
    logf = Path(get_base_dir()) / "logs" / "events.log"
    if not logf.exists():
        typer.echo("No events yet.")
        raise typer.Exit(0)

    with logf.open("r", encoding="utf-8") as f:
        # сначала распечатаем конец файла (последние ~200 строк)
        lines = f.readlines()[-200:]
        for line in lines:
            try:
                evt = json.loads(line)
                if topic and not (evt.get("topic", "").startswith(topic)):
                    continue
                typer.echo(line.rstrip())
            except Exception:
                continue

        if not follow:
            raise typer.Exit(0)

        # затем tail -f
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                evt = json.loads(line)
                if topic and not (evt.get("topic", "").startswith(topic)):
                    continue
                typer.echo(line.rstrip())
            except Exception:
                pass
