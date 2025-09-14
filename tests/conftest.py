# tests/conftest.py
from __future__ import annotations
import inspect, json, sys, subprocess, datetime as dt
import os, asyncio, inspect, types
from pathlib import Path
import sys, os, subprocess
import pytest

from adaos.services.agent_context import AgentContext, set_ctx, clear_ctx
from adaos.services.settings import Settings
from adaos.services.eventbus import LocalEventBus
from adaos.adapters.db.sqlite_store import SQLite, SQLiteKV
from adaos.domain.types import ProcessSpec
from adaos.services.logging import setup_logging, attach_event_logger  # важное: создаёт файл-лог

_MIN_PY = tuple(map(int, os.getenv("ADAOS_MIN_PY", "3.11").split(".")))


def _versions_list():
    try:
        return subprocess.check_output(["py", "-0p"], text=True, stderr=subprocess.STDOUT)
    except Exception:
        return "(py launcher not available)"


# ---- Paths provider для тестов ----
class TestPaths:
    def __init__(self, base: Path):
        self._base = Path(base).resolve()
        self._skills = self._base / "skills"
        self._scenarios = self._base / "scenarios"
        self._state = self._base / "state"
        self._cache = self._base / "cache"
        self._logs = self._base / "logs"
        self._package = Path(__file__).resolve().parents[1]

    def base_dir(self) -> str:
        return str(self._base)

    def skills_dir(self) -> str:
        return str(self._skills)

    def scenarios_dir(self) -> str:
        return str(self._scenarios)

    def state_dir(self) -> str:
        return str(self._state)

    def cache_dir(self) -> str:
        return str(self._cache)

    def logs_dir(self) -> str:
        return str(self._logs)

    def package_dir(self) -> str:
        return str(self._package)


# ---- минимальный реалистичный процесс-менеджер для тестов ----
class TestProc:
    async def start(self, spec: ProcessSpec):
        if spec.cmd:
            proc = await asyncio.create_subprocess_exec(*spec.cmd)
            handle = types.SimpleNamespace(kind="proc", proc=proc, name=spec.name)

            async def stop():
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass

            async def wait():
                try:
                    await proc.wait()
                except Exception:
                    pass

            handle.stop = stop
            handle.wait = wait
            return handle

        if spec.entrypoint is not None:
            if inspect.iscoroutinefunction(spec.entrypoint):
                task = asyncio.create_task(spec.entrypoint())
            else:

                async def _run_sync():
                    spec.entrypoint()  # type: ignore[misc]

                task = asyncio.create_task(_run_sync())

            handle = types.SimpleNamespace(kind="task", task=task, name=spec.name)

            async def stop():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

            async def wait():
                try:
                    await task
                except Exception:
                    pass

            handle.stop = stop
            handle.wait = wait
            return handle

        raise ValueError("ProcessSpec must have either cmd or entrypoint")

    async def status(self, handle) -> str:
        if getattr(handle, "kind", None) == "proc":
            proc = handle.proc
            rc = proc.returncode
            if rc is None:
                return "running"
            return "stopped" if rc == 0 else "error"
        if getattr(handle, "kind", None) == "task":
            task = handle.task
            if not task.done():
                return "running"
            if task.cancelled():
                return "stopped"
            return "error" if task.exception() else "stopped"
        return "error"


# ---------- фикстура CLI-приложения ----------
@pytest.fixture
def cli_app():
    from adaos.apps.cli.app import app

    return app


# ---------- отдельная фикстура tmp_base_dir (нужна тестам CLI CRUD) ----------
@pytest.fixture
def tmp_base_dir(tmp_path, monkeypatch) -> Path:
    base_dir = tmp_path / "base"
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    monkeypatch.setenv("ADAOS_TESTING", "1")
    return base_dir


# ---------- автofixture: поднимаем AgentContext для каждого теста ----------
@pytest.fixture(autouse=True)
def _autocontext(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    monkeypatch.setenv("ADAOS_SANDBOX_DISABLED", "1")
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    monkeypatch.setenv("ADAOS_TESTING", "1")

    # Settings (без make_paths; только base_dir/profile)
    settings = Settings.from_sources().with_overrides(base_dir=str(base_dir), profile="test")

    # Paths + каталоги
    paths = TestPaths(base_dir)
    for p in (paths.skills_dir(), paths.scenarios_dir(), paths.state_dir(), paths.cache_dir(), paths.logs_dir()):
        Path(p).mkdir(parents=True, exist_ok=True)

    # Реальные порты
    bus = LocalEventBus()
    sql = SQLite(paths)  # БД в {state_dir}/adaos.db
    kv = SQLiteKV(sql)
    proc = TestProc()  # важно для runtime-тестов

    # Остальные — простые no-op объекты
    class Noop: ...

    ctx_kwargs = dict(
        settings=settings,
        paths=paths,
        bus=bus,
        sql=sql,
        kv=kv,
        proc=proc,
        caps=Noop(),
        devices=Noop(),
        secrets=Noop(),
        net=Noop(),
        updates=Noop(),
        git=Noop(),
        fs=Noop(),
        sandbox=Noop(),
    )

    # Создать контекст (фильтр по сигнатуре — на случай эволюции)
    sig = inspect.signature(AgentContext)
    ctx = AgentContext(**{k: v for k, v in ctx_kwargs.items() if k in sig.parameters})

    # 1) Регистрируем контекст
    set_ctx(ctx)
    # 2) Настраиваем логирование (создаёт {logs}/adaos.log)
    logger = setup_logging(paths)
    # 3) Подшиваем логгер событий к шине
    attach_event_logger(bus, logger)

    try:
        yield ctx
    finally:
        clear_ctx()


def pytest_sessionstart(session):
    if sys.version_info < _MIN_PY:
        from _pytest.outcomes import Exit

        msg = [
            f"AdaOS tests require Python >= {'.'.join(map(str,_MIN_PY))}.",
            f"Current: {sys.executable} ({sys.version.split()[0]}).",
            "",
            "Fix options:",
            "  1) Activate your project venv and re-run: pytest",
            "  2) Or use CLI runner with current venv:",
            "     adaos tests run --use-current --no-sandbox",
            "  3) Or point to another interpreter:",
            "     adaos tests run --python 3.11-64",
        ]
        if os.name == "nt":
            msg += ["", "py -0p:\n" + _versions_list()]
        raise Exit("\n".join(msg), returncode=2)


import asyncio
import pytest


@pytest.fixture
def event_loop():
    """Локальный event loop на тест (совместимо без pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()
