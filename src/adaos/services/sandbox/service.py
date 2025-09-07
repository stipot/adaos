from __future__ import annotations
import os, time, shlex
from typing import Mapping, Optional, Sequence
from adaos.ports.sandbox import Sandbox, ExecLimits, ExecResult
from adaos.ports import Capabilities, EventBus
from adaos.services.sandbox.profiles import DEFAULT_PROFILES
from adaos.services.eventbus import emit

_POSIX = os.name == "posix"

_BASE_ENV_ALLOW = {
    # posix
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMP",
    "TEMP",
    "TMPDIR",
    # windows
    "Path",
    "SystemRoot",
    "USERNAME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
}
_PREFIX_ALLOW = ("ADAOS_", "PYTHON")  # разрешённые префиксы при inherit_env


def _inherit_env_filtered(env: Mapping[str, str] | None, inherit: bool) -> dict[str, str]:
    if not inherit:
        return {}
    out: dict[str, str] = {}
    src = os.environ if env is None else env
    for k, v in src.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k in _BASE_ENV_ALLOW or any(k.startswith(p) for p in _PREFIX_ALLOW):
            out[k] = v
    return out


class SandboxService(Sandbox):
    """
    Совместим с интерфейсом Sandbox (есть .run), но добавляет:
    - capabilities ("proc.run")
    - профили лимитов (profile="prep"/"handler"/"tool"/"default")
    - безопасное наследование окружения
    - события в шину: sandbox.start / .killed / .end
    """

    def __init__(self, *, runner: Sandbox, caps: Capabilities, bus: EventBus, profiles: dict[str, ExecLimits] | None = None):
        self.runner = runner
        self.caps = caps
        self.bus = bus
        self.profiles = dict(DEFAULT_PROFILES)
        if profiles:
            self.profiles.update(profiles)

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        limits: Optional[ExecLimits] = None,
        stdin: Optional[bytes] = None,
        text: bool = True,
        profile: Optional[str] = None,
        inherit_env: bool = False,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> ExecResult:
        self.caps.require("core", "proc.run")

        # лимиты: приоритет — явные limits > профиль > default
        use_limits = limits or self.profiles.get(profile or "default") or DEFAULT_PROFILES["default"]

        # окружение
        base = _inherit_env_filtered(None, inherit_env)
        if env:
            base.update({k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)})
        if extra_env:
            base.update({k: v for k, v in extra_env.items() if isinstance(k, str) and isinstance(v, str)})

        started_at = time.time()
        emit(
            self.bus,
            "sandbox.start",
            {
                "cmd": list(cmd),
                "cwd": cwd,
                "profile": profile or "default",
                "limits": {"wall": use_limits.wall_time_sec, "cpu": use_limits.cpu_time_sec, "rss": use_limits.max_rss_mb},
            },
            actor="sandbox.service",
        )

        res = self.runner.run(cmd, cwd=cwd, env=base, limits=use_limits, stdin=stdin, text=text)
        duration = time.time() - started_at

        if res.timed_out:
            emit(self.bus, "sandbox.killed", {"cmd": list(cmd), "cwd": cwd, "reason": res.killed_reason, "duration": duration}, actor="sandbox.service")

        emit(self.bus, "sandbox.end", {"cmd": list(cmd), "cwd": cwd, "exit": res.exit_code, "timed_out": res.timed_out, "duration": duration}, actor="sandbox.service")

        return res
