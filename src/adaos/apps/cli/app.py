# src/adaos/apps/cli/app.py
from __future__ import annotations

import os, traceback
import sys
import shutil
import json
from pathlib import Path
from typing import Optional
import typer
try:
    from dotenv import load_dotenv, find_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

    def find_dotenv(*args, **kwargs):
        return ""
from adaos.services.runtime_dotenv import apply_runtime_dotenv_overrides


def _repo_venv_python() -> str | None:
    try:
        for base in Path(__file__).resolve().parents:
            candidates = [base / ".venv" / "Scripts" / "python.exe", base / ".venv" / "bin" / "python"]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
    except Exception:
        pass
    return None


def _active_slot_manifest_payload() -> tuple[str | None, dict[str, str], str | None]:
    try:
        base_dir = Path(os.getenv("ADAOS_BASE_DIR") or (Path.home() / ".adaos")).expanduser().resolve()
        active_path = base_dir / "state" / "core_slots" / "active"
        if not active_path.exists():
            return None, {}, None
        active_slot = active_path.read_text(encoding="utf-8").strip().upper()
        if active_slot not in {"A", "B"}:
            return None, {}, None
        manifest_path = base_dir / "state" / "core_slots" / "slots" / active_slot / "manifest.json"
        if not manifest_path.exists():
            return None, {}, None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None, {}, None
        slot_dir = manifest_path.parent.resolve()
        venv_dir = str(manifest.get("venv_dir") or "").strip()
        repo_dir = str(manifest.get("repo_dir") or manifest.get("cwd") or "").strip()
        env_map = manifest.get("env") if isinstance(manifest.get("env"), dict) else {}
        merged_env = {str(key): str(value) for key, value in env_map.items()}
        merged_env.setdefault("ADAOS_ACTIVE_CORE_SLOT", active_slot)
        merged_env.setdefault("ADAOS_ACTIVE_CORE_SLOT_DIR", str(slot_dir))
        if repo_dir:
            merged_env.setdefault("ADAOS_SLOT_REPO_ROOT", repo_dir)
            src_dir = Path(repo_dir) / "src"
            if src_dir.exists():
                existing_pythonpath = str(os.getenv("PYTHONPATH") or "").strip()
                merged_env["PYTHONPATH"] = (
                    f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(src_dir)
                )
        merged_env.setdefault("ADAOS_BASE_DIR", str(base_dir))
        if not venv_dir:
            return None, merged_env, repo_dir or None
        venv_path = Path(venv_dir)
        candidates = [venv_path / "Scripts" / "python.exe", venv_path / "bin" / "python"]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate), merged_env, repo_dir or None
    except Exception:
        pass
    return None, {}, None


def _preferred_cli_python() -> str:
    repo_python = _repo_venv_python()
    if repo_python:
        return repo_python

    argv0 = str(sys.argv[0] or "").strip()
    if argv0:
        try:
            resolved_argv0 = shutil.which(argv0) or argv0
            script_dir = Path(resolved_argv0).resolve().parent
            candidates = [script_dir / "python.exe", script_dir / "python"]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
        except Exception:
            pass
    return sys.executable


def _same_executable_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except Exception:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _should_reexec_windows_wrapper(argv0: str | None = None) -> bool:
    if os.name != "nt":
        return False
    if os.getenv("ADAOS_CLI_REEXECED") == "1":
        return False
    entry = Path(argv0 or sys.argv[0] or "").name.lower()
    return entry == "adaos.exe"


def _should_reexec_repo_venv() -> bool:
    if os.getenv("ADAOS_CLI_REEXECED") == "1":
        return False
    if os.getenv("ADAOS_CLI_SLOT_BOUND") == "1":
        return False
    if os.getenv("ADAOS_DISABLE_PREFERRED_PYTHON_REEXEC") == "1":
        return False
    preferred = _repo_venv_python()
    if not preferred:
        return False
    return not _same_executable_path(preferred, sys.executable)


def _should_reexec_active_slot_venv() -> bool:
    reexec_reason = str(os.getenv("ADAOS_CLI_REEXEC_REASON") or "").strip()
    if os.getenv("ADAOS_CLI_REEXECED") == "1" and reexec_reason != "adaos.exe wrapper":
        return False
    if os.getenv("ADAOS_DISABLE_ACTIVE_SLOT_PYTHON_REEXEC") == "1":
        return False
    preferred, _, _ = _active_slot_manifest_payload()
    if not preferred:
        return False
    return not _same_executable_path(preferred, sys.executable)


def _reexec_preferred_python(reason: str, *, python: str | None = None, extra_env: dict[str, str] | None = None) -> None:
    os.environ["ADAOS_CLI_REEXECED"] = "1"
    os.environ["ADAOS_CLI_REEXEC_REASON"] = str(reason)
    python = python or _preferred_cli_python()
    if extra_env:
        for key, value in extra_env.items():
            os.environ[str(key)] = str(value)
    try:
        print(f"[AdaOS] re-exec CLI via {python} ({reason})", file=sys.stderr)
    except Exception:
        pass
    os.execl(python, python, "-m", "adaos", *sys.argv[1:])


def _maybe_reexec_windows_wrapper():
    if not _should_reexec_windows_wrapper():
        return
    _reexec_preferred_python("adaos.exe wrapper")


def _maybe_reexec_repo_venv():
    if not _should_reexec_repo_venv():
        return
    _reexec_preferred_python("repo .venv")


def _maybe_reexec_active_slot_venv():
    if not _should_reexec_active_slot_venv():
        return
    preferred, extra_env, repo_dir = _active_slot_manifest_payload()
    if not preferred:
        return
    if repo_dir:
        try:
            os.chdir(repo_dir)
        except Exception:
            pass
    _reexec_preferred_python("active slot .venv", python=preferred, extra_env=extra_env)


def _apply_active_slot_manifest_environment_if_current() -> bool:
    if os.getenv("ADAOS_DISABLE_ACTIVE_SLOT_ENV_APPLY") == "1":
        return False
    preferred, extra_env, repo_dir = _active_slot_manifest_payload()
    if not preferred or not _same_executable_path(preferred, sys.executable):
        return False
    for key, value in extra_env.items():
        os.environ[str(key)] = str(value)
    os.environ["ADAOS_CLI_SLOT_BOUND"] = "1"
    if repo_dir and os.getenv("ADAOS_DISABLE_ACTIVE_SLOT_CHDIR") != "1":
        try:
            os.chdir(repo_dir)
        except Exception:
            pass
    return True


_GLOBAL_OPTION_VALUE_FLAGS = {"--base-dir", "--profile"}
_GLOBAL_OPTION_BOOL_FLAGS = {"--reload", "--help", "-h"}
_STATE_CHANGING_ROOT_COMMANDS = {"install", "reset", "switch", "update"}
_STATE_CHANGING_COMMANDS = {
    ("autostart", "disable"),
    ("autostart", "enable"),
    ("autostart", "restart"),
    ("autostart", "smoke-update"),
    ("autostart", "update-cancel"),
    ("autostart", "update-complete"),
    ("autostart", "update-defer"),
    ("autostart", "update-promote-root"),
    ("autostart", "update-restore-root"),
    ("autostart", "update-rollback"),
    ("autostart", "update-start"),
    ("node", "join"),
    ("node", "member-refresh"),
    ("node", "member-update"),
    ("node", "role"),
    ("scenario", "create"),
    ("scenario", "install"),
    ("scenario", "push"),
    ("scenario", "sync"),
    ("scenario", "uninstall"),
    ("skill", "activate"),
    ("skill", "create"),
    ("skill", "gc"),
    ("skill", "install"),
    ("skill", "migrate"),
    ("skill", "push"),
    ("skill", "reconcile-fs-to-db"),
    ("skill", "rollback"),
    ("skill", "scaffold"),
    ("skill", "sync"),
    ("skill", "uninstall"),
}
_STATE_CHANGING_TRIPLE_COMMANDS = {
    ("node", "yjs", "backup"),
    ("node", "yjs", "benchmark-scenario"),
    ("node", "yjs", "create"),
    ("node", "yjs", "ensure-dev"),
    ("node", "yjs", "go-home"),
    ("node", "yjs", "reload"),
    ("node", "yjs", "reset"),
    ("node", "yjs", "restore"),
    ("node", "yjs", "scenario"),
    ("node", "yjs", "set-home"),
    ("node", "yjs", "set-home-current"),
    ("node", "yjs", "update"),
}


def _cli_command_tokens(argv: list[str] | tuple[str, ...] | None = None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    tokens: list[str] = []
    skip_next = False
    for raw_part in raw:
        part = str(raw_part or "").strip()
        if skip_next:
            skip_next = False
            continue
        if not part:
            continue
        if part in _GLOBAL_OPTION_VALUE_FLAGS:
            skip_next = True
            continue
        if any(part.startswith(flag + "=") for flag in _GLOBAL_OPTION_VALUE_FLAGS):
            continue
        if part in _GLOBAL_OPTION_BOOL_FLAGS:
            continue
        if part.startswith("-"):
            continue
        tokens.append(part)
        if len(tokens) >= 3:
            break
    return tokens


def _is_state_changing_cli_command(argv: list[str] | tuple[str, ...] | None = None) -> bool:
    tokens = _cli_command_tokens(argv)
    if not tokens:
        return False
    root = tokens[0]
    if root == "dev":
        return False
    if root in _STATE_CHANGING_ROOT_COMMANDS:
        return True
    if len(tokens) >= 3 and (tokens[0], tokens[1], tokens[2]) in _STATE_CHANGING_TRIPLE_COMMANDS:
        return True
    if len(tokens) < 2:
        return False
    return (tokens[0], tokens[1]) in _STATE_CHANGING_COMMANDS


def _production_cli_slot_guard_enabled() -> bool:
    if os.getenv("ADAOS_ALLOW_UNSLOTTED_CLI") == "1":
        return False
    if os.getenv("ADAOS_DISABLE_SLOT_CONTEXT_WARNING") == "1":
        return False
    env_type = str(os.getenv("ENV_TYPE") or os.getenv("ADAOS_ENV_TYPE") or "prod").strip().lower()
    return env_type != "dev"


def _slot_shell_required_diagnostic(argv: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
    if not _production_cli_slot_guard_enabled() or not _is_state_changing_cli_command(argv):
        return {}
    preferred, _extra_env, repo_dir = _active_slot_manifest_payload()
    if not preferred:
        return {}
    current_python = str(sys.executable or "")
    python_matches = _same_executable_path(preferred, current_python)
    bound = os.getenv("ADAOS_CLI_SLOT_BOUND") == "1"
    cwd_matches = True
    if repo_dir:
        try:
            cwd_matches = Path.cwd().resolve() == Path(repo_dir).resolve()
        except Exception:
            cwd_matches = False
    if python_matches and bound and cwd_matches:
        return {}
    return {
        "code": "slot_shell_required",
        "command": " ".join(_cli_command_tokens(argv)),
        "expected_python": str(preferred),
        "current_python": current_python,
        "expected_repo": str(repo_dir or ""),
        "current_cwd": str(Path.cwd()),
        "hint": "source tools/slot-shell.sh --cd",
    }


def _warn_if_slot_shell_required(argv: list[str] | tuple[str, ...] | None = None) -> None:
    diagnostic = _slot_shell_required_diagnostic(argv)
    if not diagnostic:
        return
    hint = diagnostic.get("hint") or "source tools/slot-shell.sh --cd"
    try:
        typer.secho(
            "[AdaOS] warning: state-changing production CLI command is not running from the active slot context.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        typer.echo(f"[AdaOS] diagnostic: {diagnostic.get('code')}; run `{hint}` before retrying.", err=True)
    except Exception:
        pass


_maybe_reexec_windows_wrapper()
_maybe_reexec_active_slot_venv()
_apply_active_slot_manifest_environment_if_current()
_maybe_reexec_repo_venv()

load_dotenv(find_dotenv())
apply_runtime_dotenv_overrides()


def _maybe_set_windows_selector_loop() -> None:
    if os.name != "nt":
        return
    raw = os.getenv("ADAOS_WIN_SELECTOR_LOOP")
    enabled = False
    if raw is not None:
        val = str(raw).strip().lower()
        enabled = val in ("1", "true", "on", "yes")
    if not enabled:
        return
    try:
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            msg = "Windows selector event loop policy enabled (ADAOS_WIN_SELECTOR_LOOP=1)"
            print(f"[AdaOS] {msg}", file=sys.stderr)
        except Exception:
            pass
    except Exception:
        pass


_maybe_set_windows_selector_loop()


def _apply_cli_log_noise_defaults() -> None:
    """
    Keep routine CLI output quiet even when the global log level is DEBUG.

    Users can still override this via ADAOS_LOG_HIDE or ADAOS_CLI_DEBUG.
    """
    if str(os.getenv("ADAOS_CLI_DEBUG", "0") or "0").strip() == "1":
        return
    existing = str(os.getenv("ADAOS_LOG_HIDE", "") or "").strip()
    rule = "adaos.eventbus=INFO"
    if not existing:
        os.environ["ADAOS_LOG_HIDE"] = rule
        return
    items = [part.strip() for part in existing.split(",") if part.strip()]
    if any(part.split("=", 1)[0].split(":", 1)[0].strip() == "adaos.eventbus" for part in items):
        return
    os.environ["ADAOS_LOG_HIDE"] = ",".join([*items, rule])

from adaos.sdk.manage.environment import prepare_environment
from adaos.services.settings import Settings
from adaos.apps.bootstrap import init_ctx, reload_ctx
from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.runtime_paths import current_base_dir
from adaos.apps.cli.commands import monitor, skill, runtime, llm, tests as tests_cmd, api, scenario, sdk_export as _sdk_export, repo, dev, node, hub, realtime
from adaos.apps.cli.commands import diag360
from adaos.apps.cli.commands import git as git_cmd
from adaos.apps.cli.commands import interpreter
from adaos.apps.cli.commands import native
from adaos.apps.cli.commands import rhasspy as rhasspy_cmd
from adaos.apps.cli.commands import secret
from adaos.apps.cli.commands import sandbox as sandbox_cmd
from adaos.apps.cli.commands import setup as setup_cmd

app = typer.Typer(help=_("cli.help"))

# -------- helpers --------


def _run_safe(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _read(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().lower()


def _write_env_var(key: str, value: str, dotenv_path: Path | None = None):
    """Примитивно патчим .env (или создаём)."""
    dotenv_path = dotenv_path or Path(find_dotenv() or ".env")
    lines: list[str] = []
    if dotenv_path.exists():
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    dotenv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _restart_self():
    # Перезапускаем текущий процесс CLI (кроссплатформенно)
    if hasattr(sys, "frozen"):
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        os.execl(sys.executable, sys.executable, "-m", "adaos", *sys.argv[1:])


def ensure_environment():
    """Проверяем, инициализировано ли окружение; вызывается после сборки контекста."""
    if os.getenv("ADAOS_TESTING") == "1":
        return  # В CI/юнит-тестах окружение не готовим и ничего не скачиваем
    ctx = get_ctx()
    base_dir = ctx.paths.base_dir()

    # для совместимости со старым кодом, который читает env напрямую
    os.environ["ADAOS_BASE_DIR"] = str(base_dir)
    os.environ["ADAOS_PROFILE"] = ctx.settings.profile

    if not base_dir.exists():
        typer.echo(_("cli.no_env_creating"))
        prepare_environment()


# -------- корневой callback (composition root) --------


@_run_safe
@app.callback()
def main(
    ctx: typer.Context,
    base_dir: Optional[str] = typer.Option(None, "--base-dir", help="Базовый каталог AdaOS (по умолчанию ~/.adaos или из .env/ENV)"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Профиль настроек (по умолчанию 'default' или из .env/ENV)"),
    reload: bool = typer.Option(False, "--reload", help="Пересобрать контекст с новыми настройками"),
):
    """
    Вызывается перед любыми подкомандами: строит (или пересобирает) контекст и гарантирует готовность окружения.
    """
    _apply_cli_log_noise_defaults()

    # 1) читаем базовые настройки (константы/.env/ENV)
    settings = Settings.from_sources()

    # 2) применяем CLI-переопределения только к безопасным полям
    settings = settings.with_overrides(base_dir=base_dir, profile=profile)

    # 3) создать/пересобрать единый контекст процесса
    if reload:
        # важно: использовать имена аргументов base_dir/profile
        reload_ctx(base_dir=base_dir, profile=profile)
    else:
        init_ctx(settings)

    # 4) автоподготовка окружения
    if ctx.invoked_subcommand != "reset":
        ensure_environment()
    _warn_if_slot_shell_required()


# -------- команды обслуживания --------


@app.command("reset")
def reset():
    """Сброс окружения AdaOS (удаляет base_dir)."""
    base_dir = get_ctx().paths.base_dir()
    if base_dir.exists():
        shutil.rmtree(base_dir)
        typer.echo(_("cli.env_deleted"))
    else:
        typer.echo(_("cli.no_env"))


# -------- переключатели профилей интеграций --------

switch_app = typer.Typer(help="Переключение бэкендов / оснасток")


@switch_app.command("tts")
def switch_tts(mode: str = typer.Argument(..., help="native | rhasspy")):
    mode = mode.strip().lower()
    if mode not in {"native", "rhasspy"}:
        raise typer.BadParameter("Allowed: native, rhasspy")
    _write_env_var("ADAOS_TTS", mode)
    typer.echo(f"[AdaOS] ADAOS_TTS set to '{mode}'. Reloading ...")
    _restart_self()


@switch_app.command("stt")
def switch_stt(mode: str = typer.Argument(..., help="vosk | rhasspy | native")):
    mode = mode.strip().lower()
    if mode not in {"vosk", "rhasspy", "native"}:
        raise typer.BadParameter("Allowed: vosk, rhasspy, native")
    _write_env_var("ADAOS_STT", mode)
    typer.echo(f"[AdaOS] ADAOS_STT set to '{mode}'. Reloading ...")
    _restart_self()


@app.command("where")
def where():
    print("base_dir:", current_base_dir())


# -------- подкоманды --------

app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
app.add_typer(tests_cmd.app, name="tests", help=_("cli.help_test"))
app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))
app.add_typer(api.app, name="api")
app.add_typer(realtime.app, name="realtime", help="Realtime sidecar")
app.add_typer(diag360.app, name="360log", help="360log flight-recorder snapshots")
app.add_typer(node.app, name="node", help="Node onboarding and role management")
app.add_typer(hub.app, name="hub", help="Hub operations (join-codes)")
app.add_typer(monitor.app, name="monitor")
app.add_typer(repo.app, name="repo", help=_("cli.repo.help"))
app.add_typer(git_cmd.app, name="git", help="Git availability / archive fallback")
app.add_typer(scenario.app, name="scenario", help=_("cli.help_scenario"))
app.add_typer(setup_cmd.autostart_app, name="autostart", help="OS autostart management")
app.add_typer(switch_app, name="switch", help="Переключение профилей интеграций")
app.add_typer(secret.app, name="secret")
app.add_typer(sandbox_cmd.app, name="sandbox")
app.add_typer(_sdk_export.app, name="sdk")
app.add_typer(interpreter.app, name="interpreter", help="Интерпретатор и обучение")
app.add_typer(dev.app, name="dev", help="Developer operations")

# Root-level setup helpers
app.command("install")(setup_cmd.install)
app.command("update")(setup_cmd.update)

# ---- Фильтрация интеграций по ENV ----
_tts = _read("ADAOS_TTS", "native")
if _tts == "rhasspy":
    app.add_typer(rhasspy_cmd.app, name="rhasspy", help="Rhasspy-integration")
else:
    app.add_typer(native.app, name="", help="Native commands")

if __name__ == "__main__":
    app()
