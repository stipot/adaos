from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import traceback
import tracemalloc
from pathlib import Path
from typing import Any

from adaos.services.supervisor_memory import (
    MemoryArtifactRef,
    read_memory_session_summary,
    supervisor_memory_session_artifacts_dir,
    write_memory_session_summary,
)


_LOG = logging.getLogger("adaos.runtime.profile")


def _runtime_profile_tracemalloc_frames(mode: str) -> int:
    if mode == "trace_profile":
        return 25
    if mode == "sampled_profile":
        return 10
    return 0


def _traceback_stats_payload(snapshot, *, limit: int = 25) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for stat in snapshot.statistics("traceback")[: max(1, int(limit or 1))]:
        items.append(
            {
                "traceback": [str(frame) for frame in stat.traceback],
                "size_bytes": int(stat.size),
                "count": int(stat.count),
            }
        )
    return items


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_finalize_debug_artifact(session_id: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    session_name = str(session_id or "").strip()
    if not session_name:
        return None
    artifacts_dir = supervisor_memory_session_artifacts_dir(session_name)
    path = (artifacts_dir / "runtime-profile-finalize-debug.json").resolve()
    _write_json_file(path, payload)
    return MemoryArtifactRef(
        artifact_id=f"{session_name}-finalize-debug",
        kind="runtime_profile_finalize_debug",
        path=str(path),
        content_type="application/json",
        size_bytes=path.stat().st_size if path.exists() else None,
        created_at=time.time(),
    ).to_dict()


class RuntimeMemoryProfileSession:
    def __init__(
        self,
        *,
        profile_mode: str,
        session_id: str | None,
        profile_trigger: str | None = None,
    ) -> None:
        self.profile_mode = str(profile_mode or "normal").strip().lower() or "normal"
        self.session_id = str(session_id or "").strip() or None
        self.profile_trigger = str(profile_trigger or "").strip() or None
        self.started_at: float | None = None
        self.started = False
        self._start_snapshot = None
        self._lock = threading.RLock()
        self._finished = False

    def start(self) -> None:
        with self._lock:
            if self.profile_mode == "normal" or not self.session_id or self.started:
                _LOG.info(
                    "runtime memory profile start skipped session_id=%s profile_mode=%s started=%s",
                    self.session_id,
                    self.profile_mode,
                    self.started,
                )
                return
            frames = _runtime_profile_tracemalloc_frames(self.profile_mode)
            if frames <= 0:
                _LOG.warning(
                    "runtime memory profile start skipped because frame depth is non-positive session_id=%s profile_mode=%s",
                    self.session_id,
                    self.profile_mode,
                )
                return
            _LOG.info(
                "runtime memory profile start session_id=%s profile_mode=%s frames=%s trigger=%s",
                self.session_id,
                self.profile_mode,
                frames,
                self.profile_trigger,
            )
            tracemalloc.start(frames)
            self.started_at = time.time()
            self.started = True
            self._finished = False
            self._start_snapshot = tracemalloc.take_snapshot()
            self._write_start_snapshot()
            atexit.register(self.finish)

    def _update_session_summary(
        self,
        *,
        artifact_refs: list[dict[str, Any]] | None = None,
        top_growth_sites: list[dict[str, Any]] | None = None,
        finished: bool = False,
    ) -> None:
        if not self.session_id:
            return
        summary = read_memory_session_summary(self.session_id) or {"session_id": self.session_id}
        if self.started_at is not None:
            summary["started_at"] = summary.get("started_at") or self.started_at
        summary["session_state"] = "finished" if finished else "running"
        if finished:
            summary["finished_at"] = time.time()
        if top_growth_sites is not None:
            summary["top_growth_sites"] = top_growth_sites
        if artifact_refs:
            existing = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
            summary["artifact_refs"] = [*existing, *artifact_refs]
        write_memory_session_summary(self.session_id, summary)

    def _write_start_snapshot(self) -> None:
        if not self.session_id or not tracemalloc.is_tracing():
            _LOG.warning(
                "runtime memory profile start snapshot skipped session_id=%s tracing=%s",
                self.session_id,
                tracemalloc.is_tracing(),
            )
            return
        snapshot = self._start_snapshot or tracemalloc.take_snapshot()
        artifacts_dir = supervisor_memory_session_artifacts_dir(self.session_id)
        path = (artifacts_dir / "tracemalloc-start.json").resolve()
        top_stats = snapshot.statistics("lineno")[:25]
        payload = {
            "session_id": self.session_id,
            "profile_mode": self.profile_mode,
            "trigger": self.profile_trigger,
            "started_at": self.started_at,
            "top_allocations": [
                {
                    "traceback": str(stat.traceback),
                    "size_bytes": int(stat.size),
                    "count": int(stat.count),
                }
                for stat in top_stats
            ],
        }
        _write_json_file(path, payload)
        extra_refs: list[dict[str, Any]] = []
        if self.profile_mode == "trace_profile":
            trace_path = (artifacts_dir / "tracemalloc-trace-start.json").resolve()
            _write_json_file(
                trace_path,
                {
                    "session_id": self.session_id,
                    "profile_mode": self.profile_mode,
                    "started_at": self.started_at,
                    "trace_frames": _runtime_profile_tracemalloc_frames(self.profile_mode),
                    "top_tracebacks": _traceback_stats_payload(snapshot),
                },
            )
            extra_refs.append(
                MemoryArtifactRef(
                    artifact_id=f"{self.session_id}-trace-start",
                    kind="tracemalloc_trace_start",
                    path=str(trace_path),
                    content_type="application/json",
                    size_bytes=trace_path.stat().st_size if trace_path.exists() else None,
                    created_at=self.started_at,
                ).to_dict()
            )
        self._update_session_summary(
            artifact_refs=[
                MemoryArtifactRef(
                    artifact_id=f"{self.session_id}-start",
                    kind="tracemalloc_start_snapshot",
                    path=str(path),
                    content_type="application/json",
                    size_bytes=path.stat().st_size if path.exists() else None,
                    created_at=self.started_at,
                ).to_dict(),
                *extra_refs,
            ]
        )
        _LOG.info(
            "runtime memory profile wrote start snapshot session_id=%s profile_mode=%s artifact_path=%s",
            self.session_id,
            self.profile_mode,
            path,
        )

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            if not self.started or not self.session_id or not tracemalloc.is_tracing():
                _LOG.warning(
                    "runtime memory profile finish skipped session_id=%s started=%s finished=%s tracing=%s",
                    self.session_id,
                    self.started,
                    self._finished,
                    tracemalloc.is_tracing(),
                )
                return
            _LOG.info(
                "runtime memory profile finish begin session_id=%s profile_mode=%s",
                self.session_id,
                self.profile_mode,
            )
            finished_at = time.time()
            snapshot = tracemalloc.take_snapshot()
            artifacts_dir = supervisor_memory_session_artifacts_dir(self.session_id)
            snapshot_path = (artifacts_dir / "tracemalloc-final.json").resolve()
            top_stats = snapshot.statistics("lineno")[:25]
            diff_path = (artifacts_dir / "tracemalloc-top-growth.json").resolve()
            baseline_snapshot = self._start_snapshot or snapshot
            diff_stats = snapshot.compare_to(baseline_snapshot, "lineno")[:25]
            snapshot_payload = {
                "session_id": self.session_id,
                "profile_mode": self.profile_mode,
                "finished_at": finished_at,
                "top_allocations": [
                    {
                        "traceback": str(stat.traceback),
                        "size_bytes": int(stat.size),
                        "count": int(stat.count),
                    }
                    for stat in top_stats
                ],
            }
            growth_sites = [
                {
                    "traceback": str(stat.traceback),
                    "size_diff_bytes": int(stat.size_diff),
                    "count_diff": int(stat.count_diff),
                    "size_bytes": int(stat.size),
                    "count": int(stat.count),
                }
                for stat in diff_stats
            ]
            _write_json_file(snapshot_path, snapshot_payload)
            _write_json_file(
                diff_path,
                {
                    "session_id": self.session_id,
                    "profile_mode": self.profile_mode,
                    "finished_at": finished_at,
                    "top_growth_sites": growth_sites,
                },
            )
            extra_refs: list[dict[str, Any]] = []
            if self.profile_mode == "trace_profile":
                trace_path = (artifacts_dir / "tracemalloc-trace-final.json").resolve()
                _write_json_file(
                    trace_path,
                    {
                        "session_id": self.session_id,
                        "profile_mode": self.profile_mode,
                        "finished_at": finished_at,
                        "trace_frames": _runtime_profile_tracemalloc_frames(self.profile_mode),
                        "top_tracebacks": _traceback_stats_payload(snapshot),
                    },
                )
                extra_refs.append(
                    MemoryArtifactRef(
                        artifact_id=f"{self.session_id}-trace-final",
                        kind="tracemalloc_trace_final",
                        path=str(trace_path),
                        content_type="application/json",
                        size_bytes=trace_path.stat().st_size if trace_path.exists() else None,
                        created_at=finished_at,
                    ).to_dict()
                )
            self._update_session_summary(
                artifact_refs=[
                    MemoryArtifactRef(
                        artifact_id=f"{self.session_id}-final",
                        kind="tracemalloc_final_snapshot",
                        path=str(snapshot_path),
                        content_type="application/json",
                        size_bytes=snapshot_path.stat().st_size if snapshot_path.exists() else None,
                        created_at=finished_at,
                    ).to_dict(),
                    MemoryArtifactRef(
                        artifact_id=f"{self.session_id}-growth",
                        kind="tracemalloc_top_growth",
                        path=str(diff_path),
                        content_type="application/json",
                        size_bytes=diff_path.stat().st_size if diff_path.exists() else None,
                        created_at=finished_at,
                    ).to_dict(),
                    *extra_refs,
                ],
                top_growth_sites=growth_sites,
                finished=True,
            )
            _LOG.info(
                "runtime memory profile finish wrote artifacts session_id=%s final_path=%s growth_path=%s growth_sites=%s",
                self.session_id,
                snapshot_path,
                diff_path,
                len(growth_sites),
            )
            tracemalloc.stop()
            self.started = False
            self._start_snapshot = None
            self._finished = True
            _LOG.info(
                "runtime memory profile finish complete session_id=%s profile_mode=%s",
                self.session_id,
                self.profile_mode,
            )


_ACTIVE_RUNTIME_MEMORY_PROFILE: RuntimeMemoryProfileSession | None = None
_ACTIVE_RUNTIME_MEMORY_PROFILE_LOCK = threading.RLock()


def register_active_runtime_memory_profile(session: RuntimeMemoryProfileSession | None) -> None:
    global _ACTIVE_RUNTIME_MEMORY_PROFILE
    with _ACTIVE_RUNTIME_MEMORY_PROFILE_LOCK:
        _ACTIVE_RUNTIME_MEMORY_PROFILE = session


def finish_active_runtime_memory_profile() -> dict[str, Any]:
    with _ACTIVE_RUNTIME_MEMORY_PROFILE_LOCK:
        session = _ACTIVE_RUNTIME_MEMORY_PROFILE
    if session is None:
        _LOG.warning("finish_active_runtime_memory_profile found no active session")
        return {"ok": False, "found": False, "reason": "no_active_session"}
    session_id = getattr(session, "session_id", None)
    result = {
        "ok": False,
        "found": True,
        "session_id": session_id,
        "profile_mode": getattr(session, "profile_mode", None),
        "started": bool(getattr(session, "started", False)),
        "finished": bool(getattr(session, "_finished", False)),
    }
    try:
        _LOG.info(
            "finish_active_runtime_memory_profile invoking finish session_id=%s profile_mode=%s started=%s finished=%s",
            result.get("session_id"),
            result.get("profile_mode"),
            result.get("started"),
            result.get("finished"),
        )
        session.finish()
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        _LOG.warning(
            "finish_active_runtime_memory_profile failed session_id=%s profile_mode=%s error_type=%s error=%s",
            result.get("session_id"),
            result.get("profile_mode"),
            result["error_type"],
            result["error"],
        )
        artifact_ref = _write_finalize_debug_artifact(session_id, result)
        if artifact_ref:
            summary = read_memory_session_summary(str(session_id)) or {"session_id": str(session_id)}
            existing = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
            summary["artifact_refs"] = [*existing, artifact_ref]
            write_memory_session_summary(str(session_id), summary)
        return result
    result["ok"] = True
    result["finished"] = bool(getattr(session, "_finished", False))
    _LOG.info(
        "finish_active_runtime_memory_profile completed session_id=%s profile_mode=%s finished=%s",
        result.get("session_id"),
        result.get("profile_mode"),
        result.get("finished"),
    )
    artifact_ref = _write_finalize_debug_artifact(session_id, result)
    if artifact_ref:
        summary = read_memory_session_summary(str(session_id)) or {"session_id": str(session_id)}
        existing = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
        summary["artifact_refs"] = [*existing, artifact_ref]
        write_memory_session_summary(str(session_id), summary)
    return result
