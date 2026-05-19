# src/adaos/apps/cli/commands/interpreter.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional
from urllib.request import Request, urlopen

import typer
import yaml

from adaos.apps.bootstrap import get_ctx
from adaos.services.interpreter.workspace import IntentMapping, InterpreterWorkspace
from adaos.services.nlu import neural_service_bridge
from adaos.services.nlu.data_registry import sync_from_scenarios_and_skills
from adaos.services.nlu.rasa_skill_installer import ensure_rasa_service_skill_installed, is_rasa_nlu_enabled
from adaos.services.skill.service_supervisor import get_service_supervisor

app = typer.Typer(help="Интерпретатор: конфигурация, датасеты и обучение.")
intent_app = typer.Typer(help="Работа с интентами интерпретатора.")
dataset_app = typer.Typer(help="Предустановленные датасеты.")
app.add_typer(intent_app, name="intent")
app.add_typer(dataset_app, name="dataset")


def _workspace() -> InterpreterWorkspace:
    ctx = get_ctx()
    return InterpreterWorkspace(ctx)


def _run_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("cannot run blocking async service operation inside an active event loop")


def _rasa_service_base_from_manifest(ctx) -> str:
    skills_root = Path(ctx.paths.skills_dir())
    manifest_path = skills_root / "rasa_nlu_service_skill" / "skill.yaml"
    if not manifest_path.exists():
        raise RuntimeError(f"rasa_nlu_service_skill not found at {manifest_path}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    service = manifest.get("service") or {}
    host = service.get("host") or "127.0.0.1"
    port = int(service.get("port") or 0)
    if port <= 0:
        raise RuntimeError("rasa_nlu_service_skill.service.port is missing")
    return f"http://{host}:{port}"


def _http_get_json(url: str, *, timeout_ms: int = 1_000) -> dict | None:
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _service_health_ok(base_url: str) -> bool:
    payload = _http_get_json(f"{base_url}/health", timeout_ms=1_000)
    return bool(payload and payload.get("ok") is True)


def _rasa_service_url(*, start: bool = True) -> str:
    ctx = get_ctx()
    if not is_rasa_nlu_enabled():
        raise RuntimeError("Rasa NLU is disabled (ADAOS_NLU_RASA=0)")
    if ensure_rasa_service_skill_installed() is None:
        raise RuntimeError("rasa_nlu_service_skill could not be installed")
    base = _rasa_service_base_from_manifest(ctx)
    if not start:
        return base
    if _service_health_ok(base):
        return base

    supervisor = get_service_supervisor()
    async def _start() -> None:
        await supervisor.refresh_discovered(force=True)
        await supervisor.start("rasa_nlu_service_skill")

    _run_blocking(_start())
    resolved_base = supervisor.resolve_base_url("rasa_nlu_service_skill")
    if resolved_base:
        return resolved_base

    return base


def _http_post_json(url: str, payload: dict, *, timeout_ms: int = 600_000) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


@app.command("sync-nlu")
def sync_nlu() -> None:
    """
    Синхронизировать NLU-данные из skill.yaml/skill metadata и scenario.json['nlu']
    в рабочее пространство интерпретатора (config.yaml + datasets).
    """
    ctx = get_ctx()
    summary = sync_from_scenarios_and_skills(ctx)
    typer.echo(
        f"NLU sync: skills_intents={summary['skills_intents']} "
        f"scenario_intents={summary['scenario_intents']}"
    )


@app.command("parse")
def parse(
    text: str = typer.Argument(..., help="Текст, который нужно разобрать интерпретатором."),
) -> None:
    """
    Прогоняет текст через последнюю обученную модель интерпретатора (Rasa)
    и печатает результат распознавания (intent, confidence, slots).
    """
    base = _rasa_service_url()
    result = _http_post_json(f"{base}/parse", {"text": text}, timeout_ms=30_000)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("neural-probe", context_settings={"allow_extra_args": True, "ignore_unknown_options": False})
def neural_probe(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Text to parse through the Neural NLU bridge."),
    webspace_id: str = typer.Option("desktop", "--webspace-id", help="Webspace id for named-entity context."),
    locale: Optional[str] = typer.Option(None, "--locale", help="Request locale."),
    no_record_stats: bool = typer.Option(False, "--no-record-stats", help="Do not update neural usage statistics."),
) -> None:
    """
    Probe Neural NLU through the hub bridge policy: service discovery/start,
    canonicalization payload, confidence gates, and optional usage stats.
    """
    extra = [str(item) for item in getattr(ctx, "args", []) or []]
    if extra:
        if any(part.startswith("-") for part in extra):
            raise typer.BadParameter(f"unexpected extra arguments: {' '.join(extra)}")
        text = " ".join([text, *extra]).strip()
    result = _run_blocking(
        neural_service_bridge.parse_text(
            text,
            webspace_id=webspace_id,
            meta={"probe": "cli.neural", "webspace_id": webspace_id},
            locale=locale,
            record_usage_stats=not no_record_stats,
        )
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("neural-readiness")
def neural_readiness(
    start_service: bool = typer.Option(False, "--start", help="Start the service and include /health readiness."),
    stop_after: bool = typer.Option(False, "--stop-after", help="Stop the service after the readiness check."),
) -> None:
    """
    Inspect Neural NLU artifacts, service discovery, and optional live health.
    """
    result = _run_blocking(
        neural_service_bridge.diagnose_readiness(
            start_service=start_service,
            stop_after=stop_after,
        )
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("status")
def status(show_skills: bool = typer.Option(False, "--show-skills", help="Показать снимок установленных скиллов.")) -> None:
    """Показывает актуальность обучения и количество данных."""
    ws = _workspace()
    info = ws.describe_status()
    typer.echo(f"Интентов: {info['intent_count']}")
    typer.echo(f"Файлов датасета: {len(info['dataset_summary'].get('files', []))}")
    if info["needs_training"]:
        typer.secho("Нужно переобучить модель:", fg=typer.colors.YELLOW)
        for reason in info["reasons"]:
            typer.echo(f"- {reason}")
    else:
        typer.secho(f"Модель актуальна (обучена {info.get('trained_at')})", fg=typer.colors.GREEN)
    if show_skills and info["skills"]:
        typer.echo("Скиллы в снапшоте:")
        for meta in info["skills"]:
            typer.echo(f"  - {meta['id']} v{meta.get('version')}")


@app.command("train")
def train(
    note: Optional[str] = typer.Option(None, "--note", help="Комментарий к запуску."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать статус и выйти."),
    engine: str = typer.Option("rasa", "--engine", help="Движок обучения (rasa/none)."),
) -> None:
    """Запускает обучение и сохраняет артефакт/метаданные."""
    ws = _workspace()
    if dry_run:
        typer.echo(json.dumps(ws.describe_status(), ensure_ascii=False, indent=2))
        return

    if engine == "rasa":
        ctx = get_ctx()
        sync_from_scenarios_and_skills(ctx)
        project_dir = ws.build_rasa_project()
        models_dir = Path(ctx.paths.models_dir()) / "interpreter"
        models_dir.mkdir(parents=True, exist_ok=True)
        base = _rasa_service_url()
        resp = _http_post_json(
            f"{base}/train",
            {"project_dir": str(project_dir), "out_dir": str(models_dir), "fixed_model_name": "interpreter_latest"},
            timeout_ms=600_000,
        )
        if resp.get("ok"):
            ws.record_training(
                note=note or "rasa-service-train",
                extra={"engine": "rasa_service", "model_path": resp.get("model_path")},
            )
        typer.echo(json.dumps(resp, ensure_ascii=False, indent=2))
        return
    else:
        meta = ws.record_training(note=note or "manual")
        typer.secho(f"Состояние зафиксировано: {meta['trained_at']}", fg=typer.colors.GREEN)
    typer.echo("Хэш состояния: " + meta["fingerprint"])


@intent_app.command("list")
def list_intents() -> None:
    ws = _workspace()
    mappings = ws.list_intents()
    if not mappings:
        typer.echo("Интенты не заданы.")
        return
    typer.echo("intent | target | description")
    typer.echo("-" * 60)
    for item in mappings:
        target = "—"
        if item.skill and item.tool:
            target = f"{item.skill}:{item.tool}"
        elif item.skill:
            target = item.skill
        elif item.scenario:
            target = f"scenario:{item.scenario}"
        typer.echo(f"{item.intent} | {target} | {item.description or ''}")


@intent_app.command("add")
def add_intent(
    intent: str = typer.Argument(..., help="Имя интента."),
    skill: Optional[str] = typer.Option(None, "--skill", help="Скилл, который следует вызвать."),
    tool: Optional[str] = typer.Option(None, "--tool", help="Публичный инструмент внутри скилла."),
    scenario: Optional[str] = typer.Option(None, "--scenario", help="Сценарий вместо скилла."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Описание."),
    examples: List[str] = typer.Option(None, "--example", "-e", help="Примеры (можно повторять)."),
) -> None:
    if not (skill or scenario):
        raise typer.BadParameter("Нужно указать --skill или --scenario.")
    mapping = IntentMapping(
        intent=intent.strip(),
        description=description,
        skill=skill,
        tool=tool,
        scenario=scenario,
        examples=examples or [],
    )
    ws = _workspace()
    ws.upsert_intent(mapping)
    typer.secho(f"Интент '{intent}' сохранён.", fg=typer.colors.GREEN)


@intent_app.command("remove")
def remove_intent(intent: str = typer.Argument(..., help="Имя интента.")) -> None:
    ws = _workspace()
    if ws.remove_intent(intent):
        typer.secho(f"Интент '{intent}' удалён.", fg=typer.colors.GREEN)
    else:
        typer.echo(f"Интент '{intent}' не найден.")
        raise typer.Exit(code=1)


@dataset_app.command("list")
def dataset_list() -> None:
    ws = _workspace()
    presets = ws.available_presets()
    if not presets:
        typer.echo("Пакетные датасеты не найдены.")
        return
    typer.echo("Доступные пресеты:")
    for name in presets:
        typer.echo(f"- {name}")


@dataset_app.command("sync")
def dataset_sync(
    preset: str = typer.Option("moodbot", "--preset", help="Имя пресета из пакета."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Перезаписать существующие файлы."),
) -> None:
    ws = _workspace()
    path = ws.sync_dataset_from_repo(preset, overwrite=overwrite)
    typer.echo(f"Датасет '{preset}' синхронизирован: {path}")


@app.command("init-skill")
def init_skill(
    name: str = typer.Argument("interpreter_router", help="Имя скилла-диспетчера."),
    template: str = typer.Option("skill_default", "--template", help="Шаблон скилла."),
) -> None:
    """Создаёт базовый скилл, который можно использовать как диспетчер."""
    from adaos.services.skill import scaffold

    ws = _workspace()
    try:
        skill_path = scaffold.create(name, template=template, register=False, push=False)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Скилл '{name}' создан: {skill_path}", fg=typer.colors.GREEN)
    typer.echo(f"Конфигурация хранится в {ws.config_path}")

