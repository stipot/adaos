# src\adaos\apps\cli\commands\api.py
import typer
import uvicorn
import os
from adaos.apps.cli.i18n import _

app = typer.Typer(help="HTTP API для AdaOS")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Для разработки"),
    token: str = typer.Option(None, "--token", help="X-AdaOS-Token; иначе возьмем из ADAOS_TOKEN"),
):
    """Запустить HTTP API (FastAPI)."""
    if token:
        os.environ["ADAOS_TOKEN"] = token
    # точка входа FastAPI
    uvicorn.run("adaos.apps.api.server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
