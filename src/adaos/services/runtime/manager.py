# src/adaos/services/runtime/manager.py
from __future__ import annotations

import asyncio
import signal
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from adaos.domain import Event, ProcessSpec
from adaos.ports import EventBus, Process
from adaos.services.eventbus import emit


def _gen_handle() -> str:
    return uuid.uuid4().hex


class ProcState(str, Enum):
    INIT = "init"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(slots=True)
class _Record:
    handle: str
    name: str
    spec: ProcessSpec
    state: ProcState = ProcState.INIT
    restarts: int = 0
    last_start_ts: float = 0.0
    task: Optional[asyncio.Task] = None
    proc: Optional[asyncio.subprocess.Process] = None
    # итог выполнения
    returncode: Optional[int] = None
    error: Optional[str] = None


class AsyncProcessManager(Process):
    """
    Реализация контракта Process:
      - поддерживает либо внешнюю команду (spec.cmd), либо корутину (spec.entrypoint)
      - публикует события в EventBus: proc.starting|running|stopping|stopped|exited|restart|error
      - минимальный anti-crash: backoff + ограничение перезапусков
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        restart_on_crash: bool = True,
        max_restarts: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 5.0,
        crash_window_s: float = 10.0,
    ) -> None:
        self._bus = bus
        self._records: Dict[str, _Record] = {}
        self._lock = asyncio.Lock()
        self._restart_on_crash = restart_on_crash
        self._max_restarts = max_restarts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._crash_window_s = crash_window_s

    # ---------- API ----------

    async def start(self, spec: ProcessSpec) -> str:
        handle = _gen_handle()
        rec = _Record(
            handle=handle,
            name=spec.name,
            spec=spec,
            state=ProcState.STARTING,
            last_start_ts=time.time(),
        )
        self._records[handle] = rec
        # событие до фактического запуска — тест ожидает минимум "starting"
        emit(self._bus, "proc.starting", {"handle": handle, "name": rec.name}, "runtime")
        # запускаем супервизор (он переведёт в RUNNING/ERROR/...)
        rec.task = asyncio.create_task(self._supervise(rec))
        return handle

    async def stop(self, handle: str, timeout_s: float = 5.0) -> None:
        rec = self._records.get(handle)
        if not rec:
            return
        if rec.state in (ProcState.STOPPED, ProcState.ERROR):
            return

        rec.state = ProcState.STOPPING
        emit(self._bus, "proc.stopping", {"handle": handle, "name": rec.name}, "runtime")

        if rec.proc:
            # внешняя команда
            try:
                if hasattr(signal, "SIGTERM"):
                    rec.proc.send_signal(signal.SIGTERM)  # *nix
                else:
                    rec.proc.terminate()  # windows
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(rec.proc.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                try:
                    rec.proc.kill()
                except ProcessLookupError:
                    pass
        elif rec.task:
            rec.task.cancel()
            try:
                await asyncio.wait_for(rec.task, timeout=timeout_s)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                pass

        rec.state = ProcState.STOPPED
        emit(self._bus, "proc.stopped", {"handle": handle, "name": rec.name}, "runtime")

    async def status(self, handle: str) -> str:
        rec = self._records.get(handle)
        return rec.state.value if rec else ProcState.ERROR.value

    # ---------- внутренняя логика ----------

    async def _supervise(self, rec: _Record) -> None:
        restarts = 0
        while True:
            rec.state = ProcState.STARTING
            rec.last_start_ts = time.time()
            emit(self._bus, "proc.starting", {"handle": rec.handle, "name": rec.name}, "runtime")

            # запуск
            try:
                if rec.spec.cmd:
                    rec.proc = await asyncio.create_subprocess_exec(
                        *rec.spec.cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    rec.task = asyncio.create_task(self._wait_subprocess(rec))
                else:
                    assert rec.spec.entrypoint is not None
                    rec.task = asyncio.create_task(self._run_entrypoint(rec))
            except Exception as e:  # ошибка старта
                rec.state = ProcState.ERROR
                rec.error = f"start_error: {e!r}"
                emit(self._bus, "proc.error", {"handle": rec.handle, "name": rec.name, "error": rec.error}, "runtime")
                return

            # пометим как running
            rec.state = ProcState.RUNNING
            emit(self._bus, "proc.running", {"handle": rec.handle, "name": rec.name}, "runtime")

            # ждём завершения текущего запуска
            try:
                assert rec.task is not None
                await rec.task
            finally:
                # task завершилась: либо штатно, либо с ошибкой/kill
                pass

            # если нас попросили остановиться — выходим
            if rec.state in (ProcState.STOPPING, ProcState.STOPPED):
                return

            # произошёл выход (exited) или ошибка — решаем, перезапускать ли
            crashed = (rec.returncode not in (0, None)) or (rec.state == ProcState.ERROR)
            emit(
                self._bus,
                "proc.exited",
                {
                    "handle": rec.handle,
                    "name": rec.name,
                    "returncode": rec.returncode,
                    "error": rec.error,
                },
                "runtime",
            )

            if not (self._restart_on_crash and crashed):
                rec.state = ProcState.STOPPED
                return

            # crash-loop защита
            now = time.time()
            if (now - rec.last_start_ts) > self._crash_window_s:
                # «давно» стартовали — обнулим счётчик
                restarts = 0
            restarts += 1
            rec.restarts = restarts
            if restarts > self._max_restarts:
                rec.state = ProcState.ERROR
                rec.error = f"crash_loop: restarts>{self._max_restarts}"
                emit(
                    self._bus,
                    "proc.error",
                    {
                        "handle": rec.handle,
                        "name": rec.name,
                        "error": rec.error,
                    },
                    "runtime",
                )
                return

            # backoff
            delay = min(self._backoff_base * (2 ** (restarts - 1)), self._backoff_max)
            emit(
                self._bus,
                "proc.restart",
                {
                    "handle": rec.handle,
                    "name": rec.name,
                    "attempt": restarts,
                    "delay": delay,
                },
                "runtime",
            )
            await asyncio.sleep(delay)

    async def _wait_subprocess(self, rec: _Record) -> None:
        assert rec.proc is not None
        # опционально: можно читать stdout/stderr и слать события proc.stdout/stderr
        await rec.proc.wait()
        rec.returncode = rec.proc.returncode
        # если нас не переводили в STOPPING — это незапланированное завершение
        if rec.state == ProcState.RUNNING:
            rec.state = ProcState.ERROR if (rec.returncode or 0) != 0 else ProcState.STOPPED

    async def _run_entrypoint(self, rec: _Record) -> None:
        assert rec.spec.entrypoint is not None
        try:
            await rec.spec.entrypoint()
            rec.returncode = 0
            if rec.state == ProcState.RUNNING:
                rec.state = ProcState.STOPPED
        except asyncio.CancelledError:
            rec.returncode = None
            # состояние выставляет stop()
        except Exception as e:
            rec.error = f"entry_error: {e!r}"
            rec.returncode = -1
            if rec.state == ProcState.RUNNING:
                rec.state = ProcState.ERROR
