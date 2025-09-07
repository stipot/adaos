from __future__ import annotations
import os, sys, time, subprocess, threading
from pathlib import Path
from typing import Mapping, Sequence, Optional, List
from dataclasses import dataclass
import psutil

from adaos.ports.sandbox import Sandbox, ExecLimits, ExecResult

_IS_POSIX = os.name == "posix"


def _kill_tree(proc: psutil.Process) -> None:
    try:
        children = proc.children(recursive=True)
        for c in children:
            try:
                c.kill()
            except Exception:
                pass
        proc.kill()
    except Exception:
        pass


def _collect_output(p: subprocess.Popen) -> tuple[str, str]:
    out, err = p.communicate()
    # p.opened with text=True — строки, иначе bytes
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    return out, err


def _preexec_posix(limits: ExecLimits):
    # вызывается только на POSIX до exec()
    import resource

    if limits.cpu_time_sec is not None:
        soft = hard = int(max(1, limits.cpu_time_sec))
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    # лимит адресного пространства/данных — косвенная защита (не всегда == RSS)
    if limits.max_rss_mb is not None:
        mb = limits.max_rss_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mb, mb))
        except Exception:
            try:
                resource.setrlimit(resource.RLIMIT_DATA, (mb, mb))
            except Exception:
                pass


@dataclass
class _MonState:
    timed_out: bool = False
    killed_reason: Optional[str] = None


class ProcSandbox(Sandbox):
    def __init__(self, *, fs_base: str):
        # допускаем только запуск внутри BASE_DIR (доп. проверка; FSPolicy — отдельно)
        self._base = Path(fs_base).resolve()

    def _check_cwd(self, cwd: Optional[str]) -> None:
        if not cwd:
            return
        p = Path(cwd).resolve()
        try:
            p.relative_to(self._base)
        except Exception:
            raise PermissionError(f"sandbox: cwd outside base_dir: {p}")

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        limits: Optional[ExecLimits] = None,
        stdin: Optional[bytes] = None,
        text: bool = True,
    ) -> ExecResult:
        limits = limits or ExecLimits()
        self._check_cwd(cwd)

        # минимальный env: только безопасный поднабор системных переменных
        safe_env = {}
        if env:
            # пропускаем только строки
            for k, v in env.items():
                if isinstance(k, str) and isinstance(v, str):
                    safe_env[k] = v

        creationflags = 0
        preexec = None
        if _IS_POSIX and (limits.cpu_time_sec or limits.max_rss_mb):
            # на POSIX установим лимиты ядром
            def _pe():
                _preexec_posix(limits)

            preexec = _pe
        elif os.name == "nt":
            # отдельная группа процессов
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=safe_env if env is not None else None,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            preexec_fn=preexec if _IS_POSIX else None,
            creationflags=creationflags,
        )
        if stdin is not None:
            try:
                p.stdin.write(stdin.decode("utf-8") if isinstance(stdin, (bytes, bytearray)) else str(stdin))
            except Exception:
                pass
            finally:
                try:
                    p.stdin.close()
                except Exception:
                    pass

        proc = psutil.Process(p.pid)
        state = _MonState()

        # мониторинг по CPU/RSS
        def _monitor():
            try:
                start = time.time()
                while True:
                    if p.poll() is not None:
                        return
                    now = time.time()
                    # wall-time
                    if limits.wall_time_sec is not None and (now - start) > limits.wall_time_sec:
                        state.timed_out = True
                        state.killed_reason = "wall_time_exceeded"
                        _kill_tree(proc)
                        return
                    # cpu-time (сумма по дереву)
                    if limits.cpu_time_sec is not None:
                        total_cpu = 0.0
                        try:
                            procs = [proc] + proc.children(recursive=True)
                        except Exception:
                            procs = [proc]
                        for pr in procs:
                            try:
                                t = pr.cpu_times()
                                total_cpu += t.user + t.system
                            except Exception:
                                pass
                        if total_cpu > limits.cpu_time_sec:
                            state.timed_out = True
                            state.killed_reason = "cpu_time_exceeded"
                            _kill_tree(proc)
                            return
                    # rss (берём максимум среди потомков)
                    if limits.max_rss_mb is not None:
                        max_rss = 0
                        try:
                            procs = [proc] + proc.children(recursive=True)
                        except Exception:
                            procs = [proc]
                        for pr in procs:
                            try:
                                rss = pr.memory_info().rss
                                if rss > max_rss:
                                    max_rss = rss
                            except Exception:
                                pass
                        if max_rss > limits.max_rss_mb * 1024 * 1024:
                            state.timed_out = True
                            state.killed_reason = "rss_exceeded"
                            _kill_tree(proc)
                            return
                    time.sleep(0.05)
            except Exception:
                # монитор упал: лучше завершить процесс
                try:
                    _kill_tree(proc)
                except Exception:
                    pass
                state.timed_out = True
                state.killed_reason = state.killed_reason or "monitor_error"

        mon = threading.Thread(target=_monitor, daemon=True)
        mon.start()

        out, err = _collect_output(p)
        code = p.returncode if p.returncode is not None else -9
        return ExecResult(
            exit_code=code,
            stdout=out,
            stderr=err,
            timed_out=state.timed_out,
            killed_reason=state.killed_reason,
        )
