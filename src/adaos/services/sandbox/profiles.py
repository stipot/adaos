from __future__ import annotations
from adaos.ports.sandbox import ExecLimits

DEFAULT_PROFILES: dict[str, ExecLimits] = {
    "default": ExecLimits(wall_time_sec=30.0, cpu_time_sec=None, max_rss_mb=None),
    "prep": ExecLimits(wall_time_sec=60.0, cpu_time_sec=15.0, max_rss_mb=512),
    "handler": ExecLimits(wall_time_sec=5.0, cpu_time_sec=2.0, max_rss_mb=256),
    "tool": ExecLimits(wall_time_sec=15.0, cpu_time_sec=5.0, max_rss_mb=512),
}
