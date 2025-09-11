import typer, json, time, sys, requests
from pathlib import Path
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit
from adaos.apps.cli.i18n import _

app = typer.Typer(help="Monitoring tools")


@app.command("events")
def monitor_events(
    topic: str = typer.Option(None, "--topic", "-t", help="Фильтр по префиксу топика"),
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Следить за логом"),
):
    logf = get_ctx().paths.base / "logs" / "events.log"
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


@app.command("sse")
def monitor_sse(
    url: str = typer.Argument(..., help="URL до /api/observe/stream"),
    topic: str = typer.Option(None, "--topic", "-t", help="topic_prefix фильтр"),
    node_id: str = typer.Option(None, "--node", help="node_id фильтр"),
    token: str = typer.Option(None, "--token", help="X-AdaOS-Token; если нужен"),
):
    params = {}
    if topic:
        params["topic_prefix"] = topic
    if node_id:
        params["node_id"] = node_id
    headers = {}
    if token:
        headers["X-AdaOS-Token"] = token
    backoff = 1
    while True:
        try:
            with requests.get(url, params=params, headers=headers, stream=True, timeout=None) as r:
                if r.status_code != 200:
                    typer.echo(f"HTTP {r.status_code}: {r.text}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 1
                for line in r.iter_lines(chunk_size=1):
                    if not line:
                        continue
                    if line.startswith(b"data: "):
                        try:
                            evt = json.loads(line[6:].decode("utf-8", "ignore"))
                            typer.echo(json.dumps(evt, ensure_ascii=False))
                        except Exception:
                            typer.echo(line.decode("utf-8", "ignore"))
        except KeyboardInterrupt:
            raise typer.Exit(0)
        except Exception as e:
            typer.echo(f"[reconnect] {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


@app.command("ping")
def ping():
    ctx = get_ctx()
    emit(ctx.bus, "cli.ping", {"ok": True}, "cli")
    typer.echo("event cli.ping sent")
