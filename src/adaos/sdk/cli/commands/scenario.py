from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Optional

import typer

from adaos.sdk.scenario_service import (
    list_installed,
    create_scenario,
    install_from_repo,
    update_from_repo,
    install_scenario,
    pull_scenario,
    push_scenario,
    uninstall_scenario,
    read_prototype,
    write_prototype,
    read_impl,
    write_impl,
    read_bindings,
    write_bindings,
)
from adaos.agent.core.scenario_engine.store import (
    load_prototype as _load_proto_model,
    load_impl as _load_impl_model,
    apply_rewrite,
)
from adaos.agent.core.scenario_engine.runtime import (
    run_scenario,
    MANAGER,
    stop_instance,
    stop_by_activity,
)

scenario_app = typer.Typer(no_args_is_help=True, help="DevOps и рантайм-утилиты для сценариев AdaOS")
impl_app = typer.Typer(no_args_is_help=True, help="Операции с имплементацией (rewrite)")
bindings_app = typer.Typer(no_args_is_help=True, help="Операции с биндингами слотов/устройств")


def _echo_json(obj) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_json_arg(value: str):
    p = Path(value)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    try:
        return json.loads(value)
    except Exception as e:
        raise typer.BadParameter(f"Ожидается JSON или путь к .json файлу: {e}")


# ---- DevOps ----


@scenario_app.command("list")
def cmd_list():
    """Список локально установленных сценариев."""
    _echo_json({"items": list_installed()})


@scenario_app.command("create")
def cmd_create(
    sid: str = typer.Argument(..., help="Имя сценария (папка)"),
    template: str = typer.Option("template", "--template", "-t", help="Шаблон из src/adaos/scenario_templates/<name>"),
):
    """Создать сценарий из локального шаблона (копируются все файлы)."""
    p = create_scenario(sid, template=template)
    typer.secho(f"Created: {p}", fg=typer.colors.GREEN)


@scenario_app.command("install-repo")
def cmd_install_repo(
    repo: str = typer.Option(..., "--repo", help="URL монорепозитория сценариев"),
    sid: Optional[str] = typer.Option(None, "--sid", help="Имя сценария (папка в монорепо)"),
    ref: Optional[str] = typer.Option(None, "--ref", help="Ветка/тег/коммит"),
    subpath: Optional[str] = typer.Option(None, "--subpath", help="Не используется в монорепо, только для совместимости"),
):
    """Установить сценарий из репозитория."""
    p = install_from_repo(repo_url=repo, sid=sid, ref=ref, subpath=subpath)
    typer.secho(f"Installed: {p}", fg=typer.colors.GREEN)


@scenario_app.command("update")
def cmd_update(
    sid: str = typer.Argument(...),
    ref: Optional[str] = typer.Option(None, "--ref", help="Переопределить meta.ref (ветка/тег/коммит)"),
):
    """Обновить сценарий из источника (meta.ref или заданный ref)."""
    p = update_from_repo(sid, ref=ref)
    typer.secho(f"Updated: {p}", fg=typer.colors.GREEN)


@scenario_app.command("install")
def cmd_install(sid: str = typer.Argument(...)):
    """Отметить сценарий как установленный (sparse + pull) — алиас pull."""
    typer.echo(install_scenario(sid))


@scenario_app.command("pull")
def cmd_pull(sid: str = typer.Argument(...)):
    """Подтянуть/обновить сценарий (sparse + pull + валидация)."""
    typer.echo(pull_scenario(sid))


@scenario_app.command("push")
def cmd_push(
    sid: str = typer.Argument(...),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Сообщение коммита"),
):
    """Закоммитить и запушить изменения сценария."""
    typer.echo(push_scenario(sid, message=message))


@scenario_app.command("uninstall")
def cmd_uninstall(sid: str = typer.Argument(...)):
    """Снять установку сценария (sparse пересоберётся)."""
    typer.echo(uninstall_scenario(sid))


# ---- Просмотр/редактирование P/I/bindings ----


@scenario_app.command("show")
def cmd_show(sid: str = typer.Argument(...)):
    """Показать прототип сценария (scenario.json)."""
    _echo_json(read_prototype(sid))


@scenario_app.command("effective")
def cmd_effective(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user", help="Пользователь"),
):
    """Показать эффективную модель (P ∪ I rewrite)."""
    p = _load_proto_model(sid)
    imp = _load_impl_model(sid, user)
    eff = apply_rewrite(p, imp)
    _echo_json(eff.model_dump(by_alias=True))


@impl_app.command("get")
def cmd_impl_get(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user"),
):
    """Получить текущую имплементацию сценария."""
    _echo_json(read_impl(sid, user))


@impl_app.command("set")
def cmd_impl_set(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user"),
    data: str = typer.Option(..., "--data", help="JSON или путь к .json"),
):
    """Записать имплементацию сценария (rewrite)."""
    payload = _load_json_arg(data)
    p = write_impl(sid, user, payload)
    typer.secho(f"Saved: {p}", fg=typer.colors.GREEN)


@bindings_app.command("get")
def cmd_bindings_get(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user"),
):
    """Получить биндинги (slots/devices/secrets)."""
    _echo_json(read_bindings(sid, user))


@bindings_app.command("set")
def cmd_bindings_set(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user"),
    data: str = typer.Option(..., "--data", help="JSON или путь к .json"),
):
    """Записать биндинги."""
    payload = _load_json_arg(data)
    p = write_bindings(sid, user, payload)
    typer.secho(f"Saved: {p}", fg=typer.colors.GREEN)


scenario_app.add_typer(impl_app, name="impl")
scenario_app.add_typer(bindings_app, name="bindings")

# ---- Рантайм ----


@scenario_app.command("run")
def cmd_run(
    sid: str = typer.Argument(...),
    user: str = typer.Option(..., "--user"),
    io: Optional[str] = typer.Option(None, "--io", help="JSON override io.settings (объект или путь к .json)"),
):
    """Запустить сценарий (создаст instance)."""
    io_override = _load_json_arg(io) if io else None
    iid = asyncio.run(run_scenario(sid, user, io_override))
    typer.secho(f"Started iid={iid}", fg=typer.colors.GREEN)


@scenario_app.command("instances")
def cmd_instances():
    """Список активных инстансов."""
    _echo_json({"items": MANAGER.list()})


@scenario_app.command("stop")
def cmd_stop(iid: str = typer.Argument(...)):
    """Остановить инстанс по iid."""
    ok = asyncio.run(stop_instance(iid))
    if ok:
        typer.secho("Stopped", fg=typer.colors.YELLOW)
    else:
        typer.secho("Not found", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@scenario_app.command("stop-by-activity")
def cmd_stop_by_activity_cmd(activity: str = typer.Argument(...)):
    """Остановить все инстансы по activityId."""
    asyncio.run(stop_by_activity(activity))
    typer.secho("Stop signal sent", fg=typer.colors.YELLOW)
