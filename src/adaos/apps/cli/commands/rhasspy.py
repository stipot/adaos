# src/adaos/sdk/cli/commands/rhasspy.py
from __future__ import annotations

import typer
from typing import Optional

from adaos.integrations.rhasspy.runner import (
    start_rhasspy,
    stop_rhasspy,
    status_rhasspy,
    wait_until_ready,
    DEFAULT_CONTAINER,
    DEFAULT_IMAGE,
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    DEFAULT_DATA_DIR,
)

app = typer.Typer(help="Управление локальным Rhasspy (через Docker)")


@app.command("start")
def start(
    port: int = typer.Option(DEFAULT_PORT, "--port"),
    profile: str = typer.Option(DEFAULT_PROFILE, "--profile"),
    data_dir: str = typer.Option(DEFAULT_DATA_DIR, "--data-dir"),
    image: str = typer.Option(DEFAULT_IMAGE, "--image"),
    container: str = typer.Option(DEFAULT_CONTAINER, "--name"),
):
    ok = start_rhasspy(container_name=container, image=image, port=port, profile=profile, data_dir=data_dir)
    if not ok:
        raise typer.Exit(1)

    typer.echo("Ждём готовности HTTP API ...")
    ready = wait_until_ready(port=port, timeout=25)
    if ready:
        typer.echo(f"Rhasspy готов: http://127.0.0.1:{port}")
    else:
        typer.echo("Rhasspy пока не ответил на /api/health — проверь логи контейнера: docker logs -f " + container)


@app.command("stop")
def stop(container: str = typer.Option(DEFAULT_CONTAINER, "--name")):
    stop_rhasspy(container_name=container)


@app.command("status")
def status(base_url: str = typer.Option(None, "--url"), port: int = typer.Option(DEFAULT_PORT, "--port"), container: str = typer.Option(DEFAULT_CONTAINER, "--name")):
    info = status_rhasspy(base_url=base_url, port=port, container_name=container)
    typer.echo(info)
