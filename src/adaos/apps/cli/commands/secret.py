from __future__ import annotations
import typer, json, sys
from adaos.apps.bootstrap import get_ctx

app = typer.Typer(help="Управление секретами")


def _svc():
    ctx = get_ctx()
    if not hasattr(ctx, "secrets") or not hasattr(ctx.secrets, "put"):
        raise typer.BadParameter("Secrets backend не инициализирован. Проверь bootstrap: ctx.secrets должен быть SecretsService.")
    return ctx.secrets


def _redact(v: str) -> str:
    if v is None:
        return "-"
    if len(v) <= 8:
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 6) + v[-4:]


@app.command("set")
def cmd_set(key: str, value: str, scope: str = typer.Option("profile", "--scope", help="profile|global")):
    _svc().put(key, value, scope=scope)  # значений в stdout не печатаем
    typer.echo(f"Saved: {key} [{scope}]")


@app.command("get")
def cmd_get(key: str, show: bool = typer.Option(False, "--show", help="Показать значение"), scope: str = typer.Option("profile", "--scope")):
    v = _svc().get(key, scope=scope)
    if v is None:
        typer.echo("Not found")
        raise typer.Exit(1)
    typer.echo(v if show else _redact(v))


@app.command("list")
def cmd_list(scope: str = typer.Option("profile", "--scope")):
    items = _svc().list(scope=scope)
    for it in items:
        typer.echo(f"- {it['key']} ({json.dumps(it.get('meta') or {})})")


@app.command("delete")
def cmd_delete(key: str, scope: str = typer.Option("profile", "--scope")):
    _svc().delete(key, scope=scope)
    typer.echo(f"Deleted: {key} [{scope}]")


@app.command("export")
def cmd_export(scope: str = typer.Option("profile", "--scope"), show: bool = typer.Option(False, "--show")):
    data = _svc().export_items(scope=scope)
    if not show:
        # по умолчанию редактируем значения
        for d in data:
            if "value" in d and d["value"] is not None:
                d["value"] = _redact(d["value"])
    typer.echo(json.dumps({"items": data}, ensure_ascii=False, indent=2))


@app.command("import")
def cmd_import(file: str, scope: str = typer.Option("profile", "--scope")):
    if file == "-":
        payload = json.loads(sys.stdin.read())
    else:
        import json, pathlib

        payload = json.loads(pathlib.Path(file).read_text(encoding="utf-8"))
    n = _svc().import_items(payload.get("items", []), scope=scope)
    typer.echo(f"Imported: {n}")
