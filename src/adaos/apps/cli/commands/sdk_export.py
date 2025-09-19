# src/adaos/apps/cli/commands/sdk_export.py
import json, pathlib, typer
from adaos.sdk.core.exporter import export as sdk_export
from adaos.services.agent_context import get_ctx

app = typer.Typer(help="SDK export utilities")


def _dump(data, fmt: str, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    elif fmt == "jsonl":
        # ожидается level=mini
        items = data["items"] if isinstance(data, dict) else data
        with path.open("w", encoding="utf-8") as f:
            for row in items:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    elif fmt == "yaml":
        import yaml  # pyyaml

        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    else:
        raise typer.BadParameter("format must be json|jsonl|yaml")


@app.command("export")
def export_cmd(
    level: str = typer.Option("std", "--level", help="mini|std|rich"),
    fmt: str = typer.Option("json", "--format", help="json|jsonl|yaml"),
    out: str = typer.Option("std.json", "--out"),
):
    """
    Сгенерировать один артефакт в /sdk/descriptions/<out>.
    """
    target_dir = get_ctx().paths.package_dir / "sdk" / "descriptions"
    data = sdk_export(level=level)
    path = target_dir / pathlib.Path(out)
    _dump(data, fmt, path)
    typer.echo(f"written: {path}")


@app.command("export-all")
def export_all_cmd():
    """
    Сгенерировать полный набор:
      /sdk/descriptions/mini.jsonl
      /sdk/descriptions/std.json
      /sdk/descriptions/rich.yaml
    """
    base = get_ctx().paths.package_dir / "sdk" / "descriptions"
    # mini
    _dump(sdk_export(level="mini"), "jsonl", base / "mini.jsonl")
    # std
    _dump(sdk_export(level="std"), "json", base / "std.json")
    # rich
    _dump(sdk_export(level="rich"), "yaml", base / "rich.yaml")
    typer.echo(f"written: {base / 'mini.jsonl'}")
    typer.echo(f"written: {base / 'std.json'}")
    typer.echo(f"written: {base / 'rich.yaml'}")


@app.command("check")
def check_cmd(reference: str = typer.Option("sdk/descriptions/std.json", "--ref"), level: str = typer.Option("std", "--level")):
    """
    Проверить дрейф контракта: сравнить текущую генерацию с файлом --ref.
    """
    import json, hashlib

    cur = sdk_export(level=level)
    rpath = get_ctx().paths.package_dir / reference
    if not rpath.exists():
        typer.echo(f"[warn] reference file not found: {rpath}")
        raise typer.Exit(code=0)
    ref = json.loads(rpath.read_text(encoding="utf-8"))

    def h(x):
        return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    if h(cur) != h(ref):
        typer.echo("[error] sdk manifest drift detected")
        raise typer.Exit(code=2)
    typer.echo("ok")
