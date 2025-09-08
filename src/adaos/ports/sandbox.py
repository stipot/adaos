from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Mapping, Sequence, Optional


@dataclass
class ExecLimits:
    wall_time_sec: Optional[float] = 30.0  # общий таймаут
    cpu_time_sec: Optional[float] = None  # суммарное CPU-время
    max_rss_mb: Optional[int] = None  # лимит RSS (МБ)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    killed_reason: Optional[str] = None


class Sandbox(Protocol):
    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        limits: Optional[ExecLimits] = None,
        stdin: Optional[bytes] = None,
        text: bool = True,
    ) -> ExecResult: ...
