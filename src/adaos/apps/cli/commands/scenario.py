from __future__ import annotations
import typer
from typing import Optional
from pathlib import Path

from adaos.apps.bootstrap import get_ctx
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.scenario.manager import ScenarioManager

scenario_app = typer.Typer(help="Управление сценариями (монорепо, реестр в БД)")


def _mgr() -> ScenarioManager:
    ctx = get_ctx()
    repo = GitScenarioRepository(paths=ctx.paths, git=ctx.git, url=ctx.settings.scenarios_monorepo_url, branch=ctx.settings.scenarios_monorepo_branch)
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


@scenario_app.command("list")
def list_scenarios(show_fs: bool = typer.Option(False, "--fs", help="Показать фактическое наличие на диске")):
    mgr = _mgr()
    rows = mgr.list_installed()
    if not rows:
        typer.echo("Установленных сценариев нет (реестр пуст).")
    else:
        for r in rows:
            typer.echo(f"{r.name:32} pin={r.pin or '-'}  updated={int(r.last_updated or 0)}")
    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(f"⚠ На диске отсутствуют (есть в реестре): {', '.join(sorted(missing))}")
        if extra:
            typer.echo(f"⚠ На диске лишние (нет в реестре): {', '.join(sorted(extra))}")


@scenario_app.command("sync")
def sync():
    mgr = _mgr()
    mgr.sync()
    typer.echo("Синхронизация сценариев завершена.")


@scenario_app.command("install")
def install(name: str = typer.Argument(..., help="Имя сценария (подпапка монорепо)"), pin: Optional[str] = typer.Option(None, "--pin")):
    mgr = _mgr()
    meta = mgr.install(name, pin=pin)
    typer.echo(f"Установлен сценарий: {meta.id.value} v{meta.version} @ {meta.path}")


@scenario_app.command("remove")
def remove(name: str):
    mgr = _mgr()
    mgr.remove(name)
    typer.echo(f"Удалён сценарий: {name}")
