from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Mapping
import uuid

from adaos.services.runtime_paths import current_state_dir


MEMORY_CONTRACT_VERSION = "1"
MEMORY_OPERATION_CONTRACT_VERSION = "1"
DEFAULT_PROFILER_ADAPTER = "tracemalloc"
JSONL_TAIL_CHUNK_BYTES = 64 * 1024
JSONL_TAIL_MAX_BYTES = 4 * 1024 * 1024
JSONL_TAIL_BYTES_PER_LINE = 2048
MEMORY_TELEMETRY_MAX_BYTES_DEFAULT = 16 * 1024 * 1024
MEMORY_TELEMETRY_COMPACT_KEEP_LINES_DEFAULT = 5000
IMPLEMENTED_PROFILE_MODES = ("normal", "sampled_profile", "trace_profile")
PLANNED_PROFILE_MODES = ("normal", "sampled_profile", "trace_profile")
IMPLEMENTED_PROFILER_ADAPTERS = ("tracemalloc",)
PLANNED_PROFILER_ADAPTERS = ("tracemalloc", "memray")
IMPLEMENTED_PROFILE_CONTROL_MODE = "phase2_supervisor_restart"
IMPLEMENTED_PROFILE_CONTROL_ACTIONS = ("profile_start", "profile_stop", "publish_request")
PROFILE_LAUNCH_ENV_KEYS = (
    "ADAOS_SUPERVISOR_PROFILE_MODE",
    "ADAOS_SUPERVISOR_PROFILE_SESSION_ID",
    "ADAOS_SUPERVISOR_PROFILE_TRIGGER",
)
TOP_LEVEL_OPERATION_EVENTS = (
    "slot_started",
    "slot_promoted",
    "skill_loaded",
    "skill_activated",
    "skill_unloaded",
    "scenario_started",
    "workspace_opened",
    "model_session_started",
    "tool_invoked",
    "core_update_prepare",
    "core_update_apply",
    "core_update_activate",
)


def _string(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _positive_int(value: Any) -> int | None:
    item = _int(value)
    return item if item is not None and item > 0 else None


def _string_tuple(values: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return default
    items: list[str] = []
    for value in values:
        item = _optional_string(value)
        if item:
            items.append(item)
    return tuple(items) or default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _read_jsonl_tail_lines(path: Path, *, limit: int = 50) -> list[str]:
    normalized_limit = max(1, int(limit or 1))
    max_bytes = max(
        JSONL_TAIL_CHUNK_BYTES,
        min(JSONL_TAIL_MAX_BYTES, normalized_limit * JSONL_TAIL_BYTES_PER_LINE),
    )
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            read_total = 0
            while position > 0 and newline_count <= normalized_limit and read_total < max_bytes:
                read_size = min(JSONL_TAIL_CHUNK_BYTES, position, max_bytes - read_total)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
                read_total += read_size
    except Exception:
        return []
    data = b"".join(reversed(chunks))
    if position > 0:
        first_newline = data.find(b"\n")
        if first_newline < 0:
            return []
        data = data[first_newline + 1 :]
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-normalized_limit:]]


def _memory_telemetry_max_bytes() -> int:
    try:
        raw = str(os.getenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_MAX_BYTES") or str(MEMORY_TELEMETRY_MAX_BYTES_DEFAULT)).strip()
        return max(0, int(raw))
    except Exception:
        return MEMORY_TELEMETRY_MAX_BYTES_DEFAULT


def _memory_telemetry_compact_keep_lines() -> int:
    try:
        raw = str(
            os.getenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_COMPACT_KEEP_LINES")
            or str(MEMORY_TELEMETRY_COMPACT_KEEP_LINES_DEFAULT)
        ).strip()
        return max(1, int(raw))
    except Exception:
        return MEMORY_TELEMETRY_COMPACT_KEEP_LINES_DEFAULT


def _compact_memory_telemetry_if_needed(path: Path) -> None:
    max_bytes = _memory_telemetry_max_bytes()
    if max_bytes <= 0:
        return
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        lines = _read_jsonl_tail_lines(path, limit=_memory_telemetry_compact_keep_lines())
        tmp = path.with_suffix(path.suffix + ".tmp")
        text = "\n".join(lines)
        tmp.write_text((text + "\n") if text else "", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return


def supervisor_memory_state_dir() -> Path:
    path = (current_state_dir() / "supervisor" / "memory").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def supervisor_memory_runtime_state_path() -> Path:
    return (supervisor_memory_state_dir() / "runtime.json").resolve()


def supervisor_memory_telemetry_path() -> Path:
    return (supervisor_memory_state_dir() / "telemetry.ndjson").resolve()


def supervisor_memory_sessions_dir() -> Path:
    path = (supervisor_memory_state_dir() / "sessions").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def supervisor_memory_sessions_index_path() -> Path:
    return (supervisor_memory_sessions_dir() / "index.json").resolve()


def supervisor_memory_session_dir(session_id: str) -> Path:
    session_token = _string(session_id, default="unknown")
    path = (supervisor_memory_sessions_dir() / session_token).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def supervisor_memory_session_summary_path(session_id: str) -> Path:
    return (supervisor_memory_session_dir(session_id) / "summary.json").resolve()


def supervisor_memory_session_operations_path(session_id: str) -> Path:
    return (supervisor_memory_session_dir(session_id) / "operations.ndjson").resolve()


def supervisor_memory_session_artifacts_dir(session_id: str) -> Path:
    path = (supervisor_memory_session_dir(session_id) / "artifacts").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(slots=True)
class MemoryArtifactRef:
    artifact_id: str
    kind: str
    path: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    created_at: float | None = None
    published_ref: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemoryArtifactRef":
        source = _mapping(payload)
        return cls(
            artifact_id=_string(source.get("artifact_id"), default="artifact"),
            kind=_string(source.get("kind"), default="unknown"),
            path=_optional_string(source.get("path")),
            content_type=_optional_string(source.get("content_type")),
            size_bytes=_int(source.get("size_bytes")),
            sha256=_optional_string(source.get("sha256")),
            created_at=_float(source.get("created_at")),
            published_ref=_optional_string(source.get("published_ref")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "path": self.path,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "published_ref": self.published_ref,
        }


@dataclass(slots=True)
class MemoryOperationEvent:
    event_id: str
    event: str
    emitted_at: float | None = None
    contract_version: str = MEMORY_OPERATION_CONTRACT_VERSION
    session_id: str | None = None
    profile_mode: str | None = None
    slot: str | None = None
    runtime_instance_id: str | None = None
    transition_role: str | None = None
    sample_source: str = "supervisor"
    sequence: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemoryOperationEvent":
        source = _mapping(payload)
        return cls(
            event_id=_string(source.get("event_id"), default=f"op-{uuid.uuid4().hex[:10]}"),
            event=_string(source.get("event"), default="unknown"),
            emitted_at=_float(source.get("emitted_at")),
            contract_version=_string(
                source.get("contract_version"), default=MEMORY_OPERATION_CONTRACT_VERSION
            ),
            session_id=_optional_string(source.get("session_id")),
            profile_mode=_optional_string(source.get("profile_mode")),
            slot=_optional_string(source.get("slot")),
            runtime_instance_id=_optional_string(source.get("runtime_instance_id")),
            transition_role=_optional_string(source.get("transition_role")),
            sample_source=_string(source.get("sample_source"), default="supervisor"),
            sequence=_int(source.get("sequence")),
            details=_mapping(source.get("details")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event": self.event,
            "emitted_at": self.emitted_at,
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "profile_mode": self.profile_mode,
            "slot": self.slot,
            "runtime_instance_id": self.runtime_instance_id,
            "transition_role": self.transition_role,
            "sample_source": self.sample_source,
            "sequence": self.sequence,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class MemoryTelemetrySample:
    sampled_at: float | None = None
    slot: str | None = None
    runtime_instance_id: str | None = None
    transition_role: str | None = None
    managed_pid: int | None = None
    profile_mode: str = "normal"
    suspicion_state: str = "idle"
    process_rss_bytes: int | None = None
    family_rss_bytes: int | None = None
    available_memory_bytes: int | None = None
    baseline_rss_bytes: int | None = None
    rss_growth_bytes: int | None = None
    rss_growth_bytes_per_min: float | None = None
    sample_source: str = "supervisor"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemoryTelemetrySample":
        source = _mapping(payload)
        return cls(
            sampled_at=_float(source.get("sampled_at")),
            slot=_optional_string(source.get("slot")),
            runtime_instance_id=_optional_string(source.get("runtime_instance_id")),
            transition_role=_optional_string(source.get("transition_role")),
            managed_pid=_int(source.get("managed_pid")),
            profile_mode=_string(source.get("profile_mode"), default="normal"),
            suspicion_state=_string(source.get("suspicion_state"), default="idle"),
            process_rss_bytes=_int(source.get("process_rss_bytes")),
            family_rss_bytes=_int(source.get("family_rss_bytes")),
            available_memory_bytes=_int(source.get("available_memory_bytes")),
            baseline_rss_bytes=_positive_int(source.get("baseline_rss_bytes")),
            rss_growth_bytes=_int(source.get("rss_growth_bytes")),
            rss_growth_bytes_per_min=_float(source.get("rss_growth_bytes_per_min")),
            sample_source=_string(source.get("sample_source"), default="supervisor"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at": self.sampled_at,
            "slot": self.slot,
            "runtime_instance_id": self.runtime_instance_id,
            "transition_role": self.transition_role,
            "managed_pid": self.managed_pid,
            "profile_mode": self.profile_mode,
            "suspicion_state": self.suspicion_state,
            "process_rss_bytes": self.process_rss_bytes,
            "family_rss_bytes": self.family_rss_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "baseline_rss_bytes": self.baseline_rss_bytes,
            "rss_growth_bytes": self.rss_growth_bytes,
            "rss_growth_bytes_per_min": self.rss_growth_bytes_per_min,
            "sample_source": self.sample_source,
        }


@dataclass(slots=True)
class MemorySessionSummary:
    session_id: str
    slot: str | None = None
    runtime_instance_id: str | None = None
    transition_role: str | None = None
    profile_mode: str = "normal"
    session_state: str = "planned"
    trigger_source: str | None = None
    trigger_reason: str | None = None
    trigger_threshold: str | None = None
    baseline_rss_bytes: int | None = None
    peak_rss_bytes: int | None = None
    rss_growth_bytes: int | None = None
    requested_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    stopped_at: float | None = None
    stop_reason: str | None = None
    failure_reason: str | None = None
    failure_stage: str | None = None
    suspected_leak: bool = False
    retry_of_session_id: str | None = None
    retry_root_session_id: str | None = None
    retry_depth: int = 0
    top_growth_sites: list[dict[str, Any]] = field(default_factory=list)
    operation_window: dict[str, Any] = field(default_factory=dict)
    published_to_root: bool = False
    publish_state: str = "local_only"
    publish_requested_at: float | None = None
    published_ref: str | None = None
    publish_result: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[MemoryArtifactRef] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemorySessionSummary":
        source = _mapping(payload)
        sites = source.get("top_growth_sites")
        artifacts = source.get("artifact_refs")
        return cls(
            session_id=_string(source.get("session_id"), default="session"),
            slot=_optional_string(source.get("slot")),
            runtime_instance_id=_optional_string(source.get("runtime_instance_id")),
            transition_role=_optional_string(source.get("transition_role")),
            profile_mode=_string(source.get("profile_mode"), default="normal"),
            session_state=_string(source.get("session_state"), default="planned"),
            trigger_source=_optional_string(source.get("trigger_source")),
            trigger_reason=_optional_string(source.get("trigger_reason")),
            trigger_threshold=_optional_string(source.get("trigger_threshold")),
            baseline_rss_bytes=_positive_int(source.get("baseline_rss_bytes")),
            peak_rss_bytes=_int(source.get("peak_rss_bytes")),
            rss_growth_bytes=_int(source.get("rss_growth_bytes")),
            requested_at=_float(source.get("requested_at")),
            started_at=_float(source.get("started_at")),
            finished_at=_float(source.get("finished_at")),
            stopped_at=_float(source.get("stopped_at")),
            stop_reason=_optional_string(source.get("stop_reason")),
            failure_reason=_optional_string(source.get("failure_reason")),
            failure_stage=_optional_string(source.get("failure_stage")),
            suspected_leak=_bool(source.get("suspected_leak")),
            retry_of_session_id=_optional_string(source.get("retry_of_session_id")),
            retry_root_session_id=_optional_string(source.get("retry_root_session_id")),
            retry_depth=max(0, int(_int(source.get("retry_depth")) or 0)),
            top_growth_sites=list(sites) if isinstance(sites, list) else [],
            operation_window=_mapping(source.get("operation_window")),
            published_to_root=_bool(source.get("published_to_root")),
            publish_state=_string(source.get("publish_state"), default="local_only"),
            publish_requested_at=_float(source.get("publish_requested_at")),
            published_ref=_optional_string(source.get("published_ref")),
            publish_result=_mapping(source.get("publish_result")),
            artifact_refs=(
                [MemoryArtifactRef.from_dict(item) for item in artifacts if isinstance(item, Mapping)]
                if isinstance(artifacts, list)
                else []
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "slot": self.slot,
            "runtime_instance_id": self.runtime_instance_id,
            "transition_role": self.transition_role,
            "profile_mode": self.profile_mode,
            "session_state": self.session_state,
            "trigger_source": self.trigger_source,
            "trigger_reason": self.trigger_reason,
            "trigger_threshold": self.trigger_threshold,
            "baseline_rss_bytes": self.baseline_rss_bytes,
            "peak_rss_bytes": self.peak_rss_bytes,
            "rss_growth_bytes": self.rss_growth_bytes,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "failure_reason": self.failure_reason,
            "failure_stage": self.failure_stage,
            "suspected_leak": self.suspected_leak,
            "retry_of_session_id": self.retry_of_session_id,
            "retry_root_session_id": self.retry_root_session_id,
            "retry_depth": self.retry_depth,
            "top_growth_sites": list(self.top_growth_sites),
            "operation_window": dict(self.operation_window),
            "published_to_root": self.published_to_root,
            "publish_state": self.publish_state,
            "publish_requested_at": self.publish_requested_at,
            "published_ref": self.published_ref,
            "publish_result": dict(self.publish_result),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }

    def to_index_item(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "slot": self.slot,
            "profile_mode": self.profile_mode,
            "session_state": self.session_state,
            "trigger_source": self.trigger_source,
            "trigger_reason": self.trigger_reason,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "suspected_leak": self.suspected_leak,
            "retry_of_session_id": self.retry_of_session_id,
            "retry_depth": self.retry_depth,
            "published_to_root": self.published_to_root,
            "publish_state": self.publish_state,
            "published_ref": self.published_ref,
        }


@dataclass(slots=True)
class MemorySessionIndex:
    contract_version: str = MEMORY_CONTRACT_VERSION
    sessions: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemorySessionIndex":
        source = _mapping(payload)
        sessions = source.get("sessions")
        return cls(
            contract_version=_string(source.get("contract_version"), default=MEMORY_CONTRACT_VERSION),
            sessions=[dict(item) for item in sessions if isinstance(item, Mapping)] if isinstance(sessions, list) else [],
            updated_at=_float(source.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "sessions": [dict(item) for item in self.sessions],
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class MemoryRuntimeState:
    contract_version: str = MEMORY_CONTRACT_VERSION
    authority: str = "supervisor"
    selected_profiler_adapter: str = DEFAULT_PROFILER_ADAPTER
    implemented_profiler_adapters: tuple[str, ...] = IMPLEMENTED_PROFILER_ADAPTERS
    planned_profiler_adapters: tuple[str, ...] = PLANNED_PROFILER_ADAPTERS
    current_profile_mode: str = "normal"
    implemented_profile_modes: tuple[str, ...] = IMPLEMENTED_PROFILE_MODES
    planned_profile_modes: tuple[str, ...] = PLANNED_PROFILE_MODES
    profile_control_mode: str = IMPLEMENTED_PROFILE_CONTROL_MODE
    implemented_profile_control_actions: tuple[str, ...] = IMPLEMENTED_PROFILE_CONTROL_ACTIONS
    implemented_profile_launch_env: tuple[str, ...] = PROFILE_LAUNCH_ENV_KEYS
    requested_profile_mode: str | None = None
    requested_session_id: str | None = None
    publish_request_session_id: str | None = None
    suspicion_state: str = "idle"
    suspicion_reason: str | None = None
    suspicion_since: float | None = None
    active_session_id: str | None = None
    last_session_id: str | None = None
    active_slot: str | None = None
    runtime_instance_id: str | None = None
    transition_role: str | None = None
    managed_pid: int | None = None
    current_process_rss_bytes: int | None = None
    current_family_rss_bytes: int | None = None
    available_memory_bytes: int | None = None
    telemetry_interval_sec: float | None = None
    telemetry_window_sec: float | None = None
    telemetry_samples_total: int = 0
    baseline_family_rss_bytes: int | None = None
    rss_growth_bytes: int | None = None
    rss_growth_bytes_per_min: float | None = None
    suspicion_growth_threshold_bytes: int | None = None
    suspicion_slope_threshold_bytes_per_min: float | None = None
    telemetry_path: str | None = None
    sessions_index_path: str | None = None
    implemented_operation_events: tuple[str, ...] = TOP_LEVEL_OPERATION_EVENTS
    operation_log_contract_version: str = MEMORY_OPERATION_CONTRACT_VERSION
    sessions_total: int = 0
    updated_at: float | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "MemoryRuntimeState":
        source = _mapping(payload)
        return cls(
            contract_version=_string(source.get("contract_version"), default=MEMORY_CONTRACT_VERSION),
            authority=_string(source.get("authority"), default="supervisor"),
            selected_profiler_adapter=_string(source.get("selected_profiler_adapter"), default=DEFAULT_PROFILER_ADAPTER),
            implemented_profiler_adapters=_string_tuple(
                source.get("implemented_profiler_adapters"),
                default=IMPLEMENTED_PROFILER_ADAPTERS,
            ),
            planned_profiler_adapters=_string_tuple(
                source.get("planned_profiler_adapters"),
                default=PLANNED_PROFILER_ADAPTERS,
            ),
            current_profile_mode=_string(source.get("current_profile_mode"), default="normal"),
            implemented_profile_modes=_string_tuple(
                source.get("implemented_profile_modes"),
                default=IMPLEMENTED_PROFILE_MODES,
            ),
            planned_profile_modes=_string_tuple(
                source.get("planned_profile_modes"),
                default=PLANNED_PROFILE_MODES,
            ),
            profile_control_mode=_string(
                source.get("profile_control_mode"), default=IMPLEMENTED_PROFILE_CONTROL_MODE
            ),
            implemented_profile_control_actions=_string_tuple(
                source.get("implemented_profile_control_actions"),
                default=IMPLEMENTED_PROFILE_CONTROL_ACTIONS,
            ),
            implemented_profile_launch_env=_string_tuple(
                source.get("implemented_profile_launch_env"),
                default=PROFILE_LAUNCH_ENV_KEYS,
            ),
            requested_profile_mode=_optional_string(source.get("requested_profile_mode")),
            requested_session_id=_optional_string(source.get("requested_session_id")),
            publish_request_session_id=_optional_string(source.get("publish_request_session_id")),
            suspicion_state=_string(source.get("suspicion_state"), default="idle"),
            suspicion_reason=_optional_string(source.get("suspicion_reason")),
            suspicion_since=_float(source.get("suspicion_since")),
            active_session_id=_optional_string(source.get("active_session_id")),
            last_session_id=_optional_string(source.get("last_session_id")),
            active_slot=_optional_string(source.get("active_slot")),
            runtime_instance_id=_optional_string(source.get("runtime_instance_id")),
            transition_role=_optional_string(source.get("transition_role")),
            managed_pid=_int(source.get("managed_pid")),
            current_process_rss_bytes=_int(source.get("current_process_rss_bytes")),
            current_family_rss_bytes=_int(source.get("current_family_rss_bytes")),
            available_memory_bytes=_int(source.get("available_memory_bytes")),
            telemetry_interval_sec=_float(source.get("telemetry_interval_sec")),
            telemetry_window_sec=_float(source.get("telemetry_window_sec")),
            telemetry_samples_total=max(0, int(_int(source.get("telemetry_samples_total")) or 0)),
            baseline_family_rss_bytes=_positive_int(source.get("baseline_family_rss_bytes")),
            rss_growth_bytes=_int(source.get("rss_growth_bytes")),
            rss_growth_bytes_per_min=_float(source.get("rss_growth_bytes_per_min")),
            suspicion_growth_threshold_bytes=_int(source.get("suspicion_growth_threshold_bytes")),
            suspicion_slope_threshold_bytes_per_min=_float(source.get("suspicion_slope_threshold_bytes_per_min")),
            telemetry_path=_optional_string(source.get("telemetry_path")),
            sessions_index_path=_optional_string(source.get("sessions_index_path")),
            implemented_operation_events=_string_tuple(
                source.get("implemented_operation_events"),
                default=TOP_LEVEL_OPERATION_EVENTS,
            ),
            operation_log_contract_version=_string(
                source.get("operation_log_contract_version"),
                default=MEMORY_OPERATION_CONTRACT_VERSION,
            ),
            sessions_total=max(0, int(_int(source.get("sessions_total")) or 0)),
            updated_at=_float(source.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "authority": self.authority,
            "selected_profiler_adapter": self.selected_profiler_adapter,
            "implemented_profiler_adapters": list(self.implemented_profiler_adapters),
            "planned_profiler_adapters": list(self.planned_profiler_adapters),
            "current_profile_mode": self.current_profile_mode,
            "implemented_profile_modes": list(self.implemented_profile_modes),
            "planned_profile_modes": list(self.planned_profile_modes),
            "profile_control_mode": self.profile_control_mode,
            "implemented_profile_control_actions": list(self.implemented_profile_control_actions),
            "implemented_profile_launch_env": list(self.implemented_profile_launch_env),
            "requested_profile_mode": self.requested_profile_mode,
            "requested_session_id": self.requested_session_id,
            "publish_request_session_id": self.publish_request_session_id,
            "suspicion_state": self.suspicion_state,
            "suspicion_reason": self.suspicion_reason,
            "suspicion_since": self.suspicion_since,
            "active_session_id": self.active_session_id,
            "last_session_id": self.last_session_id,
            "active_slot": self.active_slot,
            "runtime_instance_id": self.runtime_instance_id,
            "transition_role": self.transition_role,
            "managed_pid": self.managed_pid,
            "current_process_rss_bytes": self.current_process_rss_bytes,
            "current_family_rss_bytes": self.current_family_rss_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "telemetry_interval_sec": self.telemetry_interval_sec,
            "telemetry_window_sec": self.telemetry_window_sec,
            "telemetry_samples_total": self.telemetry_samples_total,
            "baseline_family_rss_bytes": self.baseline_family_rss_bytes,
            "rss_growth_bytes": self.rss_growth_bytes,
            "rss_growth_bytes_per_min": self.rss_growth_bytes_per_min,
            "suspicion_growth_threshold_bytes": self.suspicion_growth_threshold_bytes,
            "suspicion_slope_threshold_bytes_per_min": self.suspicion_slope_threshold_bytes_per_min,
            "telemetry_path": self.telemetry_path,
            "sessions_index_path": self.sessions_index_path,
            "implemented_operation_events": list(self.implemented_operation_events),
            "operation_log_contract_version": self.operation_log_contract_version,
            "sessions_total": self.sessions_total,
            "updated_at": self.updated_at,
        }


def read_memory_runtime_state() -> dict[str, Any]:
    return MemoryRuntimeState.from_dict(_read_json(supervisor_memory_runtime_state_path())).to_dict()


def write_memory_runtime_state(payload: MemoryRuntimeState | Mapping[str, Any]) -> dict[str, Any]:
    state = payload if isinstance(payload, MemoryRuntimeState) else MemoryRuntimeState.from_dict(payload)
    return _write_json(supervisor_memory_runtime_state_path(), state.to_dict())


def read_memory_session_index() -> dict[str, Any]:
    return MemorySessionIndex.from_dict(_read_json(supervisor_memory_sessions_index_path())).to_dict()


def write_memory_session_index(payload: MemorySessionIndex | Mapping[str, Any]) -> dict[str, Any]:
    state = payload if isinstance(payload, MemorySessionIndex) else MemorySessionIndex.from_dict(payload)
    return _write_json(supervisor_memory_sessions_index_path(), state.to_dict())


def read_memory_session_summary(session_id: str) -> dict[str, Any] | None:
    path = supervisor_memory_session_summary_path(session_id)
    if not path.exists():
        return None
    return MemorySessionSummary.from_dict(_read_json(path)).to_dict()


def write_memory_session_summary(session_id: str, payload: MemorySessionSummary | Mapping[str, Any]) -> dict[str, Any]:
    state = payload if isinstance(payload, MemorySessionSummary) else MemorySessionSummary.from_dict(payload)
    return _write_json(supervisor_memory_session_summary_path(session_id), state.to_dict())


def append_memory_session_operation(
    session_id: str,
    payload: MemoryOperationEvent | Mapping[str, Any],
) -> dict[str, Any]:
    event = payload if isinstance(payload, MemoryOperationEvent) else MemoryOperationEvent.from_dict(payload)
    path = supervisor_memory_session_operations_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
    return event.to_dict()


def read_memory_session_operations(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    path = supervisor_memory_session_operations_path(session_id)
    if not path.exists():
        return []
    try:
        lines = _read_jsonl_tail_lines(path, limit=limit)
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        items.append(MemoryOperationEvent.from_dict(payload).to_dict())
    return items


def append_memory_telemetry_sample(payload: MemoryTelemetrySample | Mapping[str, Any]) -> dict[str, Any]:
    sample = payload if isinstance(payload, MemoryTelemetrySample) else MemoryTelemetrySample.from_dict(payload)
    path = supervisor_memory_telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _compact_memory_telemetry_if_needed(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
    return sample.to_dict()


def read_memory_telemetry_tail(limit: int = 50) -> list[dict[str, Any]]:
    path = supervisor_memory_telemetry_path()
    if not path.exists():
        return []
    try:
        lines = _read_jsonl_tail_lines(path, limit=limit)
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        items.append(MemoryTelemetrySample.from_dict(payload).to_dict())
    return items


def ensure_memory_store() -> None:
    supervisor_memory_state_dir()
    supervisor_memory_sessions_dir()
    if not supervisor_memory_runtime_state_path().exists():
        write_memory_runtime_state(MemoryRuntimeState())
    if not supervisor_memory_sessions_index_path().exists():
        write_memory_session_index(MemorySessionIndex())


__all__ = [
    "DEFAULT_PROFILER_ADAPTER",
    "IMPLEMENTED_PROFILE_MODES",
    "IMPLEMENTED_PROFILE_CONTROL_ACTIONS",
    "IMPLEMENTED_PROFILE_CONTROL_MODE",
    "IMPLEMENTED_PROFILER_ADAPTERS",
    "MEMORY_CONTRACT_VERSION",
    "MEMORY_OPERATION_CONTRACT_VERSION",
    "MemoryArtifactRef",
    "MemoryOperationEvent",
    "MemoryRuntimeState",
    "MemorySessionIndex",
    "MemorySessionSummary",
    "MemoryTelemetrySample",
    "PLANNED_PROFILE_MODES",
    "PLANNED_PROFILER_ADAPTERS",
    "PROFILE_LAUNCH_ENV_KEYS",
    "TOP_LEVEL_OPERATION_EVENTS",
    "append_memory_session_operation",
    "append_memory_telemetry_sample",
    "ensure_memory_store",
    "read_memory_session_operations",
    "read_memory_telemetry_tail",
    "read_memory_runtime_state",
    "read_memory_session_index",
    "read_memory_session_summary",
    "supervisor_memory_runtime_state_path",
    "supervisor_memory_session_artifacts_dir",
    "supervisor_memory_session_operations_path",
    "supervisor_memory_session_summary_path",
    "supervisor_memory_sessions_index_path",
    "supervisor_memory_state_dir",
    "supervisor_memory_telemetry_path",
    "write_memory_runtime_state",
    "write_memory_session_index",
    "write_memory_session_summary",
]
