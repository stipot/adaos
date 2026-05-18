from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from string import Formatter
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency in some environments
    psutil = None  # type: ignore

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from adaos.apps.api.auth import require_token
from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import _advertise_base, _uvicorn_loop_mode
from adaos.services.agent_context import get_ctx
from adaos.services.bootstrap_update import SIDECAR_CONTROLLED_PATHS
from adaos.services.core_slots import (
    activate_slot,
    active_slot,
    active_slot_manifest,
    choose_inactive_slot,
    read_slot_manifest,
    rollback_to_previous_slot,
    slot_status as core_slot_status,
    validate_slot_structure,
)
from adaos.services.core_update import clear_plan as clear_core_update_plan
from adaos.services.core_update import finalize_runtime_boot_status
from adaos.services.core_update import prepare_pending_update
from adaos.services.core_update import promote_root_from_slot
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_plan as read_core_update_plan
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.core_update import resolved_root_promotion_requirement
from adaos.services.core_update import rollback_installed_skill_runtimes
from adaos.services.core_update import write_plan as write_core_update_plan
from adaos.services.core_update import write_status as write_core_update_status
from adaos.services.core_update_policy import core_update_reactions_disabled_reason
from adaos.services.node_config import load_config
from adaos.services.realtime_sidecar import (
    probe_realtime_sidecar_ready,
    realtime_sidecar_enabled,
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
    start_realtime_sidecar_subprocess,
    stop_realtime_sidecar_subprocess,
)
from adaos.services.root.memory_profile_sync import (
    memory_profile_artifact_published_ref,
    memory_profile_artifact_source_api_path,
    report_hub_memory_profile,
)
from adaos.services.runtime_paths import current_base_dir, current_repo_root
from adaos.services.supervisor_memory import (
    DEFAULT_PROFILER_ADAPTER,
    IMPLEMENTED_PROFILE_CONTROL_ACTIONS,
    IMPLEMENTED_PROFILE_CONTROL_MODE,
    MEMORY_OPERATION_CONTRACT_VERSION,
    PROFILE_LAUNCH_ENV_KEYS,
    TOP_LEVEL_OPERATION_EVENTS,
    append_memory_telemetry_sample,
    append_memory_session_operation,
    ensure_memory_store,
    read_memory_telemetry_tail,
    read_memory_runtime_state,
    read_memory_session_operations,
    read_memory_session_index,
    read_memory_session_summary,
    supervisor_memory_runtime_state_path,
    supervisor_memory_session_artifacts_dir,
    supervisor_memory_session_operations_path,
    supervisor_memory_sessions_index_path,
    supervisor_memory_telemetry_path,
    write_memory_session_index,
    write_memory_session_summary,
    write_memory_runtime_state,
)


_SKIP_PENDING_UPDATE_ENV = "ADAOS_SKIP_PENDING_CORE_UPDATE"
_LOG = logging.getLogger("adaos.supervisor")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS supervisor")
    parser.add_argument("--host", default="127.0.0.1", help="Managed runtime host")
    parser.add_argument("--port", type=int, default=8777, help="Managed runtime port")
    parser.add_argument("--token", default=None)
    return parser.parse_known_args()[0]


def _resolved_token(raw_token: str | None = None) -> str | None:
    token = str(raw_token or os.getenv("ADAOS_TOKEN") or "").strip()
    if token:
        return token
    try:
        return str(get_ctx().config.token or "").strip() or None
    except Exception:
        return None


def _supervisor_host() -> str:
    return str(os.getenv("ADAOS_SUPERVISOR_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _supervisor_port() -> int:
    try:
        return int(str(os.getenv("ADAOS_SUPERVISOR_PORT") or "8776").strip() or "8776")
    except Exception:
        return 8776


def _supervisor_base_url() -> str:
    return f"http://{_supervisor_host()}:{_supervisor_port()}"


def _supervisor_state_dir() -> Path:
    path = (current_base_dir() / "state" / "supervisor").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _supervisor_runtime_state_path() -> Path:
    return (_supervisor_state_dir() / "runtime.json").resolve()


def _supervisor_hub_root_watchdog_log_path() -> Path:
    return (_supervisor_state_dir() / "hub_root_watchdog.jsonl").resolve()


def _supervisor_member_hub_watchdog_log_path() -> Path:
    return (_supervisor_state_dir() / "member_hub_watchdog.jsonl").resolve()


def _supervisor_update_attempt_path() -> Path:
    return (_supervisor_state_dir() / "update_attempt.json").resolve()


def _memory_profiler_adapter() -> str:
    token = str(os.getenv("ADAOS_SUPERVISOR_MEMORY_PROFILER") or "").strip().lower()
    return token or DEFAULT_PROFILER_ADAPTER


def _memory_telemetry_interval_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_SEC") or "15").strip()))
    except Exception:
        return 15.0


def _memory_telemetry_window_sec() -> float:
    try:
        return max(60.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_WINDOW_SEC") or "180").strip()))
    except Exception:
        return 180.0


def _memory_suspicion_growth_threshold_bytes() -> int:
    default_value = 1024 * 1024 * 1024
    total_memory = _total_memory_bytes()
    if total_memory and total_memory > 0:
        default_value = min(
            1024 * 1024 * 1024,
            max(256 * 1024 * 1024, int(float(total_memory) * 0.20)),
        )
    try:
        return max(
            32 * 1024 * 1024,
            int(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_GROWTH_BYTES") or str(default_value)).strip()),
        )
    except Exception:
        return default_value


def _memory_suspicion_slope_threshold_bytes_per_min() -> float:
    try:
        return max(
            float(8 * 1024 * 1024),
            float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_SLOPE_BYTES_PER_MIN") or str(128 * 1024 * 1024)).strip()),
        )
    except Exception:
        return float(128 * 1024 * 1024)


def _memory_auto_profile_cooldown_sec() -> float:
    try:
        return max(60.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_PROFILE_COOLDOWN_SEC") or "86400").strip()))
    except Exception:
        return 86400.0


def _memory_policy_profile_restarts_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_MEMORY_POLICY_PROFILE_RESTARTS")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _memory_auto_profile_min_uptime_sec() -> float:
    try:
        return max(
            0.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC") or "300").strip()),
        )
    except Exception:
        return 300.0


def _memory_auto_profile_browser_live_ttl_sec() -> float:
    try:
        return max(
            5.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_BROWSER_LIVE_TTL_SEC") or "45").strip()),
        )
    except Exception:
        return 45.0


def _memory_auto_profile_circuit_window_sec() -> float:
    try:
        return max(300.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_PROFILE_CIRCUIT_WINDOW_SEC") or "1800").strip()))
    except Exception:
        return 1800.0


def _memory_auto_profile_circuit_limit() -> int:
    try:
        return max(1, int(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_PROFILE_CIRCUIT_LIMIT") or "3").strip()))
    except Exception:
        return 3


def _available_memory_bytes() -> int | None:
    if psutil is None:
        return None
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return None
    try:
        return int(vm.available)
    except Exception:
        return None


def _total_memory_bytes() -> int | None:
    if psutil is None:
        return None
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return None
    try:
        return int(vm.total)
    except Exception:
        return None


def _memory_critical_available_percent_threshold() -> float:
    try:
        return max(
            1.0,
            min(25.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_AVAILABLE_PERCENT") or "5").strip())),
        )
    except Exception:
        return 5.0


def _memory_critical_available_bytes_threshold() -> int:
    try:
        return max(
            64 * 1024 * 1024,
            int(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_AVAILABLE_BYTES") or str(256 * 1024 * 1024)).strip()),
        )
    except Exception:
        return 256 * 1024 * 1024


def _memory_critical_duration_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_DURATION_SEC") or "20").strip()))
    except Exception:
        return 20.0


def _memory_critical_restart_cooldown_sec() -> float:
    try:
        return max(30.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_RESTART_COOLDOWN_SEC") or "120").strip()))
    except Exception:
        return 120.0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_jsonl_tail(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _local_update_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": read_core_update_status(),
        "last_result": read_core_update_last_result(),
        "plan": read_core_update_plan(),
        "slots": core_slot_status(),
        "active_manifest": active_slot_manifest(),
        "_local_fallback": True,
    }


def _update_attempt_timeout_sec() -> float:
    try:
        return max(10.0, float(str(os.getenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC") or "180").strip()))
    except Exception:
        return 180.0


def _min_update_period_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC") or "300").strip()))
    except Exception:
        return 300.0


def _live_media_guard_defer_sec() -> float:
    try:
        return max(30.0, float(str(os.getenv("ADAOS_SUPERVISOR_LIVE_MEDIA_DEFER_SEC") or "300").strip()))
    except Exception:
        return 300.0


def _auto_update_complete_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_AUTO_UPDATE_COMPLETE")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _root_restart_delay_sec() -> float:
    try:
        return max(0.1, float(str(os.getenv("ADAOS_SUPERVISOR_ROOT_RESTART_DELAY_SEC") or "0.25").strip()))
    except Exception:
        return 0.25


def _autostart_self_restart_supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    raw = os.getenv("ADAOS_AUTOSTART_MANAGED")
    if raw is not None and str(raw).strip():
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return bool(str(os.getenv("INVOCATION_ID") or "").strip())


def _slot_runtime_ports(primary_port: int) -> dict[str, int]:
    fallback_a = int(primary_port)
    fallback_b = fallback_a + 1
    try:
        slot_a = int(str(os.getenv("ADAOS_SUPERVISOR_SLOT_A_PORT") or fallback_a).strip() or fallback_a)
    except Exception:
        slot_a = fallback_a
    try:
        slot_b = int(str(os.getenv("ADAOS_SUPERVISOR_SLOT_B_PORT") or fallback_b).strip() or fallback_b)
    except Exception:
        slot_b = fallback_b
    if slot_a <= 0:
        slot_a = fallback_a
    if slot_b <= 0:
        slot_b = fallback_b
    return {"A": slot_a, "B": slot_b}


def _slot_runtime_port(slot: str | None, primary_port: int) -> int:
    slot_name = str(slot or "").strip().upper()
    return int(_slot_runtime_ports(primary_port).get(slot_name, int(primary_port)))


def _warm_switch_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _warm_switch_min_available_bytes() -> int:
    try:
        return max(0, int(float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_MIN_AVAILABLE_MB") or "256").strip()) * 1024 * 1024))
    except Exception:
        return 256 * 1024 * 1024


def _warm_switch_min_candidate_bytes() -> int:
    try:
        return max(0, int(float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_MIN_CANDIDATE_MB") or "192").strip()) * 1024 * 1024))
    except Exception:
        return 192 * 1024 * 1024


def _warm_switch_rss_multiplier() -> float:
    try:
        return max(1.0, float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_RSS_MULTIPLIER") or "1.15").strip()))
    except Exception:
        return 1.15


def _warm_switch_candidate_ready_timeout_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_CANDIDATE_READY_TIMEOUT_SEC") or "12").strip()))
    except Exception:
        return 12.0


def _sidecar_code_change_debounce_sec() -> float:
    try:
        return max(0.5, float(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_CODE_DEBOUNCE_SEC") or "3").strip()))
    except Exception:
        return 3.0


def _sidecar_restart_window_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_RESTART_WINDOW_SEC") or "60").strip()))
    except Exception:
        return 60.0


def _sidecar_restart_limit() -> int:
    try:
        return max(2, int(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_RESTART_LIMIT") or "4").strip()))
    except Exception:
        return 4


def _sidecar_restart_base_backoff_sec() -> float:
    try:
        return max(1.0, float(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_RESTART_BASE_BACKOFF_SEC") or "2").strip()))
    except Exception:
        return 2.0


def _sidecar_restart_max_backoff_sec() -> float:
    try:
        return max(2.0, float(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_RESTART_MAX_BACKOFF_SEC") or "30").strip()))
    except Exception:
        return 30.0


def _sidecar_restart_circuit_open_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_SIDECAR_RESTART_CIRCUIT_OPEN_SEC") or "90").strip()))
    except Exception:
        return 90.0


def _hub_root_watchdog_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_HUB_ROOT_WATCHDOG")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _hub_root_watchdog_cooldown_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_HUB_ROOT_RECONNECT_COOLDOWN_SEC") or "30").strip()))
    except Exception:
        return 30.0


def _hub_root_watchdog_reset_degraded_route_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_HUB_ROOT_ROUTE_DEGRADED_RESET")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _hub_root_watchdog_verify_timeout_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_HUB_ROOT_VERIFY_TIMEOUT_SEC") or "15").strip()))
    except Exception:
        return 15.0


def _hub_root_watchdog_verify_interval_sec() -> float:
    try:
        return max(0.25, float(str(os.getenv("ADAOS_SUPERVISOR_HUB_ROOT_VERIFY_INTERVAL_SEC") or "1").strip()))
    except Exception:
        return 1.0


def _member_hub_watchdog_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_MEMBER_HUB_WATCHDOG")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _member_hub_watchdog_cooldown_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMBER_HUB_RECONNECT_COOLDOWN_SEC") or "20").strip()))
    except Exception:
        return 20.0


def _member_hub_watchdog_verify_timeout_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_MEMBER_HUB_VERIFY_TIMEOUT_SEC") or "10").strip()))
    except Exception:
        return 10.0


def _member_hub_watchdog_verify_interval_sec() -> float:
    try:
        return max(0.25, float(str(os.getenv("ADAOS_SUPERVISOR_MEMBER_HUB_VERIFY_INTERVAL_SEC") or "1").strip()))
    except Exception:
        return 1.0


def _terminal_update_states() -> set[str]:
    return {"failed", "validated", "succeeded", "rolled_back", "expired", "cancelled", "idle"}


UPDATE_ATTEMPT_CONTRACT_VERSION = "1"


def _new_runtime_instance_id(*, slot: str | None, transition_role: str) -> str:
    slot_token = str(slot or "x").strip().lower() or "x"
    role_token = str(transition_role or "active").strip().lower() or "active"
    return f"rt-{slot_token}-{role_token[:1]}-{uuid.uuid4().hex[:8]}"


def _normalize_update_attempt(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    source = dict(payload)
    state = str(source.get("state") or "").strip().lower()
    action = str(source.get("action") or "").strip().lower() or None
    normalized = {
        "contract_version": str(source.get("contract_version") or UPDATE_ATTEMPT_CONTRACT_VERSION),
        "authority": str(source.get("authority") or "supervisor"),
        "state": state or None,
        "action": action,
        "requested_at": _epoch(source.get("requested_at")) or None,
        "transitioned_at": _epoch(source.get("transitioned_at")) or None,
        "scheduled_for": _epoch(source.get("scheduled_for")) or None,
        "updated_at": _epoch(source.get("updated_at")) or None,
        "completed_at": _epoch(source.get("completed_at")) or None,
        "countdown_sec": _epoch(source.get("countdown_sec")) or None,
        "drain_timeout_sec": _epoch(source.get("drain_timeout_sec")) or None,
        "signal_delay_sec": _epoch(source.get("signal_delay_sec")) or None,
        "target_rev": str(source.get("target_rev") or "").strip() or None,
        "target_version": str(source.get("target_version") or "").strip() or None,
        "reason": str(source.get("reason") or "").strip() or None,
        "planned_reason": str(source.get("planned_reason") or "").strip() or None,
        "completion_reason": str(source.get("completion_reason") or "").strip() or None,
        "accepted": bool(source.get("accepted")),
        "awaiting_restart": bool(source.get("awaiting_restart")),
        "restart_required": bool(source.get("restart_required")),
        "restart_mode": str(source.get("restart_mode") or "").strip() or None,
        "restart_requested_at": _epoch(source.get("restart_requested_at")) or None,
        "min_update_period_sec": _epoch(source.get("min_update_period_sec")) or None,
        "subsequent_transition": bool(source.get("subsequent_transition")),
        "subsequent_transition_requested_at": _epoch(source.get("subsequent_transition_requested_at")) or None,
        "candidate_prewarm_state": str(source.get("candidate_prewarm_state") or "").strip() or None,
        "candidate_prewarm_message": str(source.get("candidate_prewarm_message") or "").strip() or None,
        "candidate_prewarm_ready_at": _epoch(source.get("candidate_prewarm_ready_at")) or None,
        "subsequent_transition_request": dict(source.get("subsequent_transition_request") or {})
        if isinstance(source.get("subsequent_transition_request"), dict)
        else None,
        "last_status": dict(source.get("last_status") or {}) if isinstance(source.get("last_status"), dict) else {},
    }
    if normalized["updated_at"] is None:
        normalized["updated_at"] = time.time()
    return normalized


def _read_update_attempt() -> dict[str, Any] | None:
    payload = _read_json(_supervisor_update_attempt_path())
    return _normalize_update_attempt(payload if isinstance(payload, dict) else None)


def _write_update_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    merged = _normalize_update_attempt(payload)
    if not isinstance(merged, dict):
        raise ValueError("update attempt payload must be a dict")
    _write_json(_supervisor_update_attempt_path(), merged)
    return merged


def _epoch(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _status_updated_at(payload: dict[str, Any]) -> float:
    for key in ("updated_at", "validated_at", "finished_at", "started_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return value
    return 0.0


def _attempt_transition_at(payload: dict[str, Any]) -> float:
    for key in ("transitioned_at", "scheduled_for", "requested_at", "updated_at", "created_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return value
    return 0.0


def _is_terminal_update_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("state") or "").strip().lower() in _terminal_update_states()


def _is_root_restart_pending_attempt(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("state") or "").strip().lower() == "awaiting_root_restart"


def _is_root_restart_completed_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("phase") or "").strip().lower()
    return state == "succeeded" and phase == "validate" and float(payload.get("root_restart_completed_at") or 0.0) > 0.0


def _is_root_promotion_pending_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("phase") or "").strip().lower()
    return state == "validated" and phase == "root_promotion_pending"


def _is_root_restart_pending_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("phase") or "").strip().lower()
    return state == "succeeded" and phase == "root_promoted"


def _is_transition_in_progress(status: dict[str, Any] | None, attempt: dict[str, Any] | None) -> bool:
    status_map = status if isinstance(status, dict) else {}
    attempt_map = attempt if isinstance(attempt, dict) else {}
    state = str(status_map.get("state") or "").strip().lower()
    phase = str(status_map.get("phase") or "").strip().lower()
    attempt_state = str(attempt_map.get("state") or "").strip().lower()
    if attempt_state in {"active", "awaiting_root_restart"}:
        return True
    if state in {"preparing", "countdown", "draining", "stopping", "restarting", "applying"}:
        return True
    if state == "validated" and phase == "root_promotion_pending":
        return True
    if state == "succeeded" and phase == "root_promoted":
        return True
    return False


def _transition_request_payload(
    *,
    action: str,
    target_rev: str,
    target_version: str,
    reason: str,
    countdown_sec: float,
    drain_timeout_sec: float,
    signal_delay_sec: float,
    requested_at: float | None = None,
) -> dict[str, Any]:
    return {
        "action": str(action or "update"),
        "target_rev": str(target_rev or ""),
        "target_version": str(target_version or ""),
        "reason": str(reason or ""),
        "countdown_sec": float(countdown_sec),
        "drain_timeout_sec": float(drain_timeout_sec),
        "signal_delay_sec": float(signal_delay_sec),
        "requested_at": float(requested_at or time.time()),
    }


def _request_from_attempt(attempt: dict[str, Any] | None) -> dict[str, Any]:
    data = attempt if isinstance(attempt, dict) else {}
    return _transition_request_payload(
        action=str(data.get("action") or "update"),
        target_rev=str(data.get("target_rev") or ""),
        target_version=str(data.get("target_version") or ""),
        reason=str(data.get("reason") or ""),
        countdown_sec=float(data.get("countdown_sec") or 0.0),
        drain_timeout_sec=float(data.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(data.get("signal_delay_sec") or 0.25),
        requested_at=_epoch(data.get("requested_at")) or time.time(),
    )


def _subsequent_transition_request(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    data = attempt if isinstance(attempt, dict) else {}
    queued = data.get("subsequent_transition_request")
    return dict(queued) if isinstance(queued, dict) and queued else None


def _last_update_completion_at(status: dict[str, Any] | None, attempt: dict[str, Any] | None) -> float:
    attempt_map = attempt if isinstance(attempt, dict) else {}
    if str(attempt_map.get("action") or "").strip().lower() == "update":
        completed_at = _epoch(attempt_map.get("completed_at"))
        if completed_at > 0.0:
            return completed_at
        updated_at = _epoch(attempt_map.get("updated_at"))
        if updated_at > 0.0 and str(attempt_map.get("state") or "").strip().lower() in {"completed", "failed", "cancelled"}:
            return updated_at
    status_map = status if isinstance(status, dict) else {}
    if str(status_map.get("action") or "").strip().lower() != "update":
        return 0.0
    if not _is_terminal_update_status(status_map):
        return 0.0
    return max(
        _epoch(status_map.get("root_restart_completed_at")),
        _epoch(status_map.get("finished_at")),
        _status_updated_at(status_map),
    )


def _build_attempt_payload(*, action: str, request: dict[str, Any], status: dict[str, Any] | None, accepted: bool) -> dict[str, Any]:
    now = time.time()
    current_status = dict(status or {})
    countdown_sec = float(request.get("countdown_sec") or current_status.get("countdown_sec") or 0.0)
    scheduled_for = float(current_status.get("scheduled_for") or (now + countdown_sec))
    return {
        "state": "active" if accepted else "rejected",
        "action": str(action or current_status.get("action") or "update"),
        "requested_at": now,
        "transitioned_at": scheduled_for if accepted else now,
        "countdown_sec": countdown_sec,
        "drain_timeout_sec": float(request.get("drain_timeout_sec") or current_status.get("drain_timeout_sec") or 0.0),
        "signal_delay_sec": float(request.get("signal_delay_sec") or current_status.get("signal_delay_sec") or 0.0),
        "target_rev": str(request.get("target_rev") or current_status.get("target_rev") or ""),
        "target_version": str(request.get("target_version") or current_status.get("target_version") or ""),
        "reason": str(request.get("reason") or current_status.get("reason") or ""),
        "accepted": bool(accepted),
        "scheduled_for": scheduled_for if accepted else None,
        "min_update_period_sec": float(request.get("min_update_period_sec") or current_status.get("min_update_period_sec") or 0.0),
        "candidate_prewarm_state": str(
            request.get("candidate_prewarm_state") or current_status.get("candidate_prewarm_state") or ""
        ).strip()
        or None,
        "candidate_prewarm_message": str(
            request.get("candidate_prewarm_message") or current_status.get("candidate_prewarm_message") or ""
        ).strip()
        or None,
        "last_status": current_status,
        "updated_at": now,
    }


def _complete_update_attempt(*, state: str, status: dict[str, Any] | None, reason: str | None = None) -> dict[str, Any]:
    now = time.time()
    current = _read_update_attempt() or {}
    payload = dict(current)
    payload["state"] = str(state or "completed")
    payload["completed_at"] = now
    payload["updated_at"] = now
    payload["awaiting_restart"] = False
    payload["restart_required"] = False
    payload["candidate_prewarm_state"] = None
    payload["candidate_prewarm_message"] = None
    payload["candidate_prewarm_ready_at"] = None
    if reason:
        payload["completion_reason"] = str(reason)
    if isinstance(status, dict):
        payload["last_status"] = dict(status)
    return _write_update_attempt(payload)


def _fail_root_restart_attempt(
    *,
    status: dict[str, Any],
    attempt: dict[str, Any],
    timeout_sec: float,
    now: float,
) -> dict[str, Any]:
    failed_status = write_core_update_status(
        {
            "state": "failed",
            "phase": "root_restart_timeout",
            "action": str(status.get("action") or attempt.get("action") or "update"),
            "target_rev": str(status.get("target_rev") or attempt.get("target_rev") or ""),
            "target_version": str(status.get("target_version") or attempt.get("target_version") or ""),
            "reason": str(status.get("reason") or attempt.get("reason") or "supervisor.root_restart_timeout"),
            "message": "supervisor timed out waiting for autostart service restart after root promotion",
            "supervisor_timeout_sec": timeout_sec,
            "supervisor_timeout_at": now,
            "supervisor_previous_status": dict(status),
        }
    )
    return _complete_update_attempt(
        state="failed",
        status=failed_status,
        reason="root restart timeout",
    )


def _reconcile_update_status(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    attempt = _read_update_attempt()
    if not isinstance(attempt, dict):
        return payload

    payload["attempt"] = dict(attempt)
    now = time.time()
    timeout_sec = _update_attempt_timeout_sec()
    status_age = max(0.0, now - _status_updated_at(status)) if _status_updated_at(status) > 0.0 else 0.0
    transition_age = max(0.0, now - _attempt_transition_at(attempt)) if _attempt_transition_at(attempt) > 0.0 else 0.0
    if _is_root_restart_pending_attempt(attempt):
        if _is_root_restart_completed_status(status):
            payload["attempt"] = _complete_update_attempt(
                state="completed",
                status=status,
                reason="root restart completed",
            )
        elif max(status_age, transition_age) >= timeout_sec:
            # If a new runtime is already serving status, let it finalize a stale
            # root-promoted marker before declaring the update failed.
            finalized_status = finalize_runtime_boot_status()
            if _is_root_restart_completed_status(finalized_status):
                payload["status"] = finalized_status
                payload["attempt"] = _complete_update_attempt(
                    state="completed",
                    status=finalized_status,
                    reason="root restart completed",
                )
                payload["_served_by"] = "supervisor_timeout_finalize"
                return payload
            failed_attempt = _fail_root_restart_attempt(
                status=status,
                attempt=attempt,
                timeout_sec=timeout_sec,
                now=now,
            )
            payload["status"] = read_core_update_status()
            payload["attempt"] = failed_attempt
            payload["_served_by"] = "supervisor_timeout_recovery"
        return payload

    if str(attempt.get("state") or "").strip().lower() != "active":
        return payload

    if _is_terminal_update_status(status):
        payload["attempt"] = _complete_update_attempt(state="completed", status=status, reason="terminal core update status")
        return payload

    if max(status_age, transition_age) < timeout_sec:
        return payload

    action = str(status.get("action") or attempt.get("action") or "update")
    failed_payload: dict[str, Any] = {
        "state": "failed",
        "phase": str(status.get("phase") or "restart_timeout"),
        "action": action,
        "target_rev": str(status.get("target_rev") or attempt.get("target_rev") or ""),
        "target_version": str(status.get("target_version") or attempt.get("target_version") or ""),
        "reason": str(status.get("reason") or attempt.get("reason") or "supervisor.timeout"),
        "message": f"supervisor timed out waiting for runtime to finish {status.get('state') or 'update transition'}",
        "supervisor_timeout_sec": timeout_sec,
        "supervisor_timeout_at": now,
        "supervisor_previous_status": status,
    }
    if action == "update":
        restored = rollback_to_previous_slot()
        skill_runtime_rollback = rollback_installed_skill_runtimes() if restored else {}
        if restored:
            failed_payload["restored_slot"] = restored
            failed_payload["rollback"] = {"ok": True, "slot": restored}
            failed_payload["message"] += f"; rolled back to slot {restored}"
        if skill_runtime_rollback:
            failed_payload["skill_runtime_rollback"] = skill_runtime_rollback
            if restored and not bool(skill_runtime_rollback.get("ok")):
                failed_payload["message"] += " | some skill runtime rollbacks failed"
    failed_status = write_core_update_status(failed_payload)
    with contextlib.suppress(Exception):
        clear_core_update_plan()
    payload["status"] = failed_status
    payload["attempt"] = _complete_update_attempt(state="failed", status=failed_status, reason="restart/apply timeout")
    payload["_served_by"] = "supervisor_timeout_recovery"
    return payload


def _public_update_status_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    status = source.get("status") if isinstance(source.get("status"), dict) else {}
    runtime = source.get("runtime") if isinstance(source.get("runtime"), dict) else {}
    attempt = source.get("attempt") if isinstance(source.get("attempt"), dict) else {}
    sidecar = runtime.get("sidecar") if isinstance(runtime.get("sidecar"), dict) else {}
    sidecar_process = sidecar.get("process") if isinstance(sidecar.get("process"), dict) else {}
    sidecar_health = sidecar.get("health") if isinstance(sidecar.get("health"), dict) else {}
    sidecar_code = sidecar.get("code") if isinstance(sidecar.get("code"), dict) else {}
    sidecar_policy = sidecar.get("restart_policy") if isinstance(sidecar.get("restart_policy"), dict) else {}
    sidecar_sync = sidecar.get("sync") if isinstance(sidecar.get("sync"), dict) else {}
    public_status = {
        "action": str(status.get("action") or "").strip().lower() or None,
        "state": str(status.get("state") or "").strip().lower() or "unknown",
        "phase": str(status.get("phase") or "").strip().lower() or "",
        "message": str(status.get("message") or "").strip(),
        "target_rev": str(status.get("target_rev") or "").strip(),
        "target_version": str(status.get("target_version") or "").strip(),
        "planned_reason": str(status.get("planned_reason") or "").strip() or None,
        "min_update_period_sec": status.get("min_update_period_sec"),
        "scheduled_for": status.get("scheduled_for"),
        "subsequent_transition": bool(status.get("subsequent_transition")),
        "subsequent_transition_requested_at": status.get("subsequent_transition_requested_at"),
        "candidate_prewarm_state": str(status.get("candidate_prewarm_state") or "").strip() or None,
        "candidate_prewarm_message": str(status.get("candidate_prewarm_message") or "").strip() or None,
        "candidate_prewarm_ready_at": status.get("candidate_prewarm_ready_at"),
        "restart_mode": str(status.get("restart_mode") or "").strip() or None,
        "restart_requested_at": status.get("restart_requested_at"),
        "updated_at": status.get("updated_at"),
    }
    return {
        "ok": True,
        "status": public_status,
        "attempt": {
            "contract_version": str(attempt.get("contract_version") or UPDATE_ATTEMPT_CONTRACT_VERSION),
            "authority": str(attempt.get("authority") or "supervisor"),
            "action": str(attempt.get("action") or "").strip().lower() or None,
            "state": str(attempt.get("state") or "").strip().lower() or None,
            "awaiting_restart": bool(attempt.get("awaiting_restart")),
            "planned_reason": str(attempt.get("planned_reason") or "").strip() or None,
            "scheduled_for": attempt.get("scheduled_for"),
            "subsequent_transition": bool(attempt.get("subsequent_transition")),
            "subsequent_transition_requested_at": attempt.get("subsequent_transition_requested_at"),
            "candidate_prewarm_state": str(attempt.get("candidate_prewarm_state") or "").strip() or None,
            "candidate_prewarm_message": str(attempt.get("candidate_prewarm_message") or "").strip() or None,
            "restart_mode": str(attempt.get("restart_mode") or "").strip() or None,
            "restart_requested_at": attempt.get("restart_requested_at"),
            "updated_at": attempt.get("updated_at"),
        },
        "runtime": {
            "active_slot": str(runtime.get("active_slot") or "").strip() or None,
            "runtime_state": str(runtime.get("runtime_state") or "").strip() or None,
            "runtime_url": str(runtime.get("runtime_url") or "").strip() or None,
            "runtime_port": runtime.get("runtime_port"),
            "runtime_instance_id": str(runtime.get("runtime_instance_id") or "").strip() or None,
            "transition_role": str(runtime.get("transition_role") or "").strip() or None,
            "listener_running": bool(runtime.get("listener_running")),
            "runtime_api_ready": bool(runtime.get("runtime_api_ready")),
            "candidate_slot": str(runtime.get("candidate_slot") or "").strip() or None,
            "candidate_runtime_url": str(runtime.get("candidate_runtime_url") or "").strip() or None,
            "candidate_runtime_port": runtime.get("candidate_runtime_port"),
            "candidate_runtime_instance_id": str(runtime.get("candidate_runtime_instance_id") or "").strip() or None,
            "candidate_transition_role": str(runtime.get("candidate_transition_role") or "").strip() or None,
            "candidate_listener_running": bool(runtime.get("candidate_listener_running")),
            "candidate_runtime_api_ready": bool(runtime.get("candidate_runtime_api_ready")),
            "candidate_runtime_state": str(runtime.get("candidate_runtime_state") or "").strip() or None,
            "transition_mode": str(runtime.get("transition_mode") or "").strip() or None,
            "warm_switch_supported": runtime.get("warm_switch_supported"),
            "warm_switch_allowed": runtime.get("warm_switch_allowed"),
            "warm_switch_reason": str(runtime.get("warm_switch_reason") or "").strip() or None,
            "slot_ports": runtime.get("slot_ports") if isinstance(runtime.get("slot_ports"), dict) else {},
            "root_promotion_required": bool(runtime.get("root_promotion_required")),
            "sidecar": {
                "enabled": bool(sidecar.get("enabled")),
                "role": str(sidecar.get("role") or "").strip() or None,
                "launch_cwd": str(sidecar.get("launch_cwd") or "").strip() or None,
                "last_start_reason": str(sidecar.get("last_start_reason") or "").strip() or None,
                "last_restart_reason": str(sidecar.get("last_restart_reason") or "").strip() or None,
                "listener_running": bool(sidecar_process.get("listener_running")),
                "listener_pid": sidecar_process.get("listener_pid"),
                "managed_pid": sidecar_process.get("managed_pid"),
                "adopted_listener": bool(sidecar_process.get("adopted_listener")),
                "last_probe_ok": sidecar_health.get("last_probe_ok"),
                "last_probe_error": str(sidecar_health.get("last_probe_error") or "").strip() or None,
                "consecutive_failures": sidecar_health.get("consecutive_failures"),
                "code_changed": bool(
                    sidecar_code.get("fingerprint")
                    and sidecar_code.get("active_fingerprint")
                    and str(sidecar_code.get("fingerprint")) != str(sidecar_code.get("active_fingerprint"))
                ),
                "active_fingerprint": str(sidecar_code.get("active_fingerprint") or "").strip() or None,
                "pending_fingerprint": str(sidecar_policy.get("pending_code_fingerprint") or "").strip() or None,
                "restart_backoff_remaining_s": sidecar_policy.get("restart_backoff_remaining_s"),
                "circuit_open_remaining_s": sidecar_policy.get("circuit_open_remaining_s"),
                "sync_source_mode": str(sidecar_code.get("sync_source_mode") or "").strip() or None,
                "sync_source_slot": str(sidecar_code.get("sync_source_slot") or "").strip() or None,
                "last_sync_at": sidecar_sync.get("last_sync_at"),
                "last_sync_source_slot": str(sidecar_sync.get("last_sync_source_slot") or "").strip() or None,
                "last_sync_reason": str(sidecar_sync.get("last_sync_reason") or "").strip() or None,
                "last_sync_changed_paths": list(sidecar_sync.get("last_sync_changed_paths") or []),
                "sync_changed": bool(sidecar_sync.get("last_sync_changed_paths")),
            },
        },
        "_served_by": str(source.get("_served_by") or "").strip() or "unknown",
    }


def _listener_running(host: str, port: int, *, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((str(host or "127.0.0.1"), int(port)), timeout=max(0.05, float(timeout))):
            return True
    except Exception:
        return False


def _runtime_api_ready(base_url: str, *, token: str | None, timeout: float = 0.75) -> bool:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = token
    try:
        response = requests.get(f"{base_url}/api/ping", headers=headers, timeout=max(0.1, float(timeout)))
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return False
    return bool(isinstance(payload, dict) and payload.get("ok") is True)


def _runtime_listener_restart_timeout_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_RUNTIME_LISTENER_TIMEOUT_SEC") or "45").strip()))
    except Exception:
        return 45.0


def _runtime_listener_startup_grace_sec() -> float:
    try:
        return max(
            _runtime_listener_restart_timeout_sec(),
            float(str(os.getenv("ADAOS_SUPERVISOR_RUNTIME_STARTUP_GRACE_SEC") or "90").strip()),
        )
    except Exception:
        return max(_runtime_listener_restart_timeout_sec(), 90.0)


def _runtime_api_restart_timeout_sec() -> float:
    try:
        return max(5.0, float(str(os.getenv("ADAOS_SUPERVISOR_RUNTIME_API_TIMEOUT_SEC") or "60").strip()))
    except Exception:
        return 60.0


def _runtime_shutdown_request_timeout(*, drain_timeout_sec: float, signal_delay_sec: float) -> float:
    return max(5.0, float(drain_timeout_sec) + float(signal_delay_sec) + 2.0)


def _runtime_profile_graceful_shutdown_timeout_sec(profile_mode: str) -> tuple[float, float, float, float]:
    normalized = str(profile_mode or "normal").strip().lower()
    if normalized == "normal":
        return 5.0, 0.25, 8.0, 5.0
    try:
        drain_timeout = max(
            5.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_PROFILE_DRAIN_TIMEOUT_SEC") or "20").strip()),
        )
    except Exception:
        drain_timeout = 20.0
    try:
        signal_delay = max(
            0.25,
            float(str(os.getenv("ADAOS_SUPERVISOR_PROFILE_SIGNAL_DELAY_SEC") or "1").strip()),
        )
    except Exception:
        signal_delay = 1.0
    try:
        graceful_wait = max(
            8.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_PROFILE_GRACEFUL_WAIT_SEC") or "25").strip()),
        )
    except Exception:
        graceful_wait = 25.0
    try:
        terminate_wait = max(
            5.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_PROFILE_TERMINATE_WAIT_SEC") or "10").strip()),
        )
    except Exception:
        terminate_wait = 10.0
    return drain_timeout, signal_delay, graceful_wait, terminate_wait


def _runtime_profile_finalize_wait_sec() -> float:
    try:
        return max(
            2.0,
            float(str(os.getenv("ADAOS_SUPERVISOR_PROFILE_FINALIZE_WAIT_SEC") or "8").strip()),
        )
    except Exception:
        return 8.0


def _memory_profile_max_runtime_sec(profile_mode: str) -> float:
    normalized = str(profile_mode or "normal").strip().lower()
    if normalized == "sampled_profile":
        raw = os.getenv("ADAOS_SUPERVISOR_SAMPLED_PROFILE_MAX_RUNTIME_SEC")
        default = "40"
    elif normalized == "trace_profile":
        raw = os.getenv("ADAOS_SUPERVISOR_TRACE_PROFILE_MAX_RUNTIME_SEC")
        default = "75"
    else:
        return 0.0
    try:
        return max(5.0, float(str(raw or default).strip()))
    except Exception:
        return float(default)


def _proc_details(proc: subprocess.Popen[Any] | None, *, cwd_hint: str | None = None) -> dict[str, Any]:
    managed_pid = None
    managed_alive = False
    managed_cmdline: list[str] = []
    managed_executable = None
    managed_cwd = None
    if proc is None:
        return {
            "managed_pid": None,
            "managed_alive": False,
            "managed_cmdline": [],
            "managed_executable": None,
            "managed_cwd": None,
        }
    try:
        managed_pid = int(proc.pid or 0) or None
        managed_alive = proc.poll() is None
        raw_args = proc.args if isinstance(proc.args, (list, tuple)) else [str(proc.args or "")]
        managed_cmdline = [str(item) for item in raw_args if str(item or "").strip()]
        managed_executable = managed_cmdline[0] if managed_cmdline else None
        managed_cwd = str(cwd_hint or getattr(proc, "cwd", None) or "").strip() or None
    except Exception:
        managed_pid = None
        managed_alive = False
        managed_cmdline = []
        managed_executable = None
        managed_cwd = None
    return {
        "managed_pid": managed_pid,
        "managed_alive": managed_alive,
        "managed_cmdline": managed_cmdline,
        "managed_executable": managed_executable,
        "managed_cwd": managed_cwd,
    }


def _process_family_rss_bytes(pid: int | None) -> tuple[int | None, int | None]:
    if not pid or psutil is None:
        return None, None
    try:
        root = psutil.Process(int(pid))
    except Exception:
        return None, None
    try:
        root_rss = int(root.memory_info().rss)
    except Exception:
        root_rss = None
    family_rss = int(root_rss or 0)
    try:
        children = list(root.children(recursive=True))
    except Exception:
        children = []
    for child in children:
        try:
            family_rss += int(child.memory_info().rss)
        except Exception:
            continue
    return root_rss, family_rss if family_rss > 0 else root_rss


def _positive_int_or_none(value: Any) -> int | None:
    try:
        item = int(value)
    except Exception:
        return None
    return item if item > 0 else None


def _format_slot_value(template: str, values: dict[str, str]) -> str:
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    payload = dict(values)
    for field in fields:
        payload.setdefault(field, "")
    return template.format(**payload)


class SupervisorManager:
    def __init__(self, *, runtime_host: str, runtime_port: int, token: str | None) -> None:
        self.runtime_host = str(runtime_host or "127.0.0.1").strip() or "127.0.0.1"
        self.runtime_port = int(runtime_port)
        self.token = str(token or "").strip() or None
        ensure_memory_store()
        self._proc: subprocess.Popen[Any] | None = None
        self._candidate_proc: subprocess.Popen[Any] | None = None
        self._sidecar_proc: subprocess.Popen[Any] | None = None
        self._desired_running = True
        self._stopping = False
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[Any] | None = None
        self._restart_count = 0
        self._last_start_at: float | None = None
        self._last_exit_at: float | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._runtime_unhealthy_since: float | None = None
        self._runtime_unhealthy_kind: str | None = None
        self._hub_root_watchdog_last_reconnect_at: float | None = None
        self._hub_root_watchdog_last_state: str | None = None
        self._hub_root_watchdog_last_reason: str | None = None
        self._hub_root_watchdog_reconnect_total = 0
        self._hub_root_watchdog_last_result: dict[str, Any] | None = None
        self._member_hub_watchdog_last_reconnect_at: float | None = None
        self._member_hub_watchdog_last_state: str | None = None
        self._member_hub_watchdog_last_reason: str | None = None
        self._member_hub_watchdog_reconnect_total = 0
        self._member_hub_watchdog_last_result: dict[str, Any] | None = None
        self._update_task: asyncio.Task[Any] | None = None
        self._update_task_cancel_mode: str | None = None
        self._managed_runtime_instance_id: str | None = None
        self._managed_transition_role: str | None = None
        self._managed_runtime_cwd: str | None = None
        self._managed_start_reason: str | None = None
        self._last_stop_reason: str | None = None
        self._candidate_slot: str | None = None
        self._candidate_runtime_instance_id: str | None = None
        self._candidate_transition_role: str | None = None
        self._candidate_runtime_cwd: str | None = None
        self._candidate_start_reason: str | None = None
        self._candidate_last_stop_reason: str | None = None
        self._service_restart_pending = False
        self._service_restart_thread: threading.Thread | None = None
        self._memory_profiler_adapter = _memory_profiler_adapter()
        self._memory_profile_mode = "normal"
        self._memory_requested_profile_mode: str | None = None
        self._memory_publish_request_session_id: str | None = None
        self._memory_profile_current_trigger_source: str | None = None
        self._memory_suspicion_state = "idle"
        self._memory_suspicion_reason: str | None = None
        self._memory_suspicion_since: float | None = None
        self._memory_active_session_id: str | None = None
        self._memory_last_session_id: str | None = None
        self._memory_baseline_family_rss_bytes: int | None = None
        self._memory_last_growth_bytes: int | None = None
        self._memory_last_growth_bytes_per_min: float | None = None
        self._memory_last_available_bytes: int | None = None
        self._memory_last_available_percent: float | None = None
        self._memory_last_telemetry_at: float | None = None
        self._memory_auto_profile_last_block_reason: str | None = None
        self._memory_auto_profile_last_block_at: float | None = None
        self._memory_critical_since: float | None = None
        self._memory_critical_reason: str | None = None
        self._memory_critical_restart_last_at: float | None = None
        self._sidecar_launch_cwd: str | None = None
        self._sidecar_last_start_reason: str | None = None
        self._sidecar_last_restart_reason: str | None = None
        self._sidecar_last_probe_at: float | None = None
        self._sidecar_last_probe_ok: bool | None = None
        self._sidecar_last_probe_error: str | None = None
        self._sidecar_consecutive_probe_failures = 0
        self._sidecar_code_fingerprint: str | None = None
        self._sidecar_code_fingerprint_updated_at: float | None = None
        self._sidecar_code_change_pending_fingerprint: str | None = None
        self._sidecar_code_change_pending_since: float | None = None
        self._sidecar_restart_history: deque[float] = deque(maxlen=32)
        self._sidecar_restart_backoff_until: float | None = None
        self._sidecar_circuit_open_until: float | None = None
        self._sidecar_last_restart_at: float | None = None
        self._sidecar_last_sync_at: float | None = None
        self._sidecar_last_sync_source_slot: str | None = None
        self._sidecar_last_sync_reason: str | None = None
        self._sidecar_last_sync_changed_paths: list[str] = []

    def _sidecar_repo_root(self) -> Path | None:
        def _normalize_candidate(raw: Any) -> Path | None:
            try:
                text = str(raw or "").strip()
                if not text:
                    return None
                path = Path(text).expanduser().resolve()
            except Exception:
                return None
            return path if path.exists() else None

        def _looks_like_python_install_root(path: Path) -> bool:
            parts = tuple(part.lower() for part in path.parts)
            if "site-packages" in parts or "dist-packages" in parts:
                return True
            for idx, part in enumerate(parts):
                if part == "venv" and idx + 2 < len(parts):
                    if parts[idx + 1] == "lib" and parts[idx + 2].startswith("python"):
                        return True
            return False

        def _looks_like_project_root(path: Path) -> bool:
            try:
                if (path / ".git").exists() or (path / "pyproject.toml").exists():
                    return True
                src_root = path / "src" / "adaos"
                return src_root.exists()
            except Exception:
                return False

        def _shared_dotenv_repo_root() -> Path | None:
            raw = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
            if not raw:
                return None
            try:
                dotenv_path = Path(raw).expanduser().resolve()
            except Exception:
                return None
            if not dotenv_path.exists():
                return None
            candidate = dotenv_path.parent
            return candidate if _looks_like_project_root(candidate) else None

        shared_dotenv_root = _shared_dotenv_repo_root()
        if shared_dotenv_root is not None:
            return shared_dotenv_root
        try:
            ctx = get_ctx()
            repo_root = ctx.paths.repo_root()
            raw = repo_root() if callable(repo_root) else repo_root
            candidate = _normalize_candidate(raw)
            if candidate is not None and not _looks_like_python_install_root(candidate):
                return candidate
        except Exception:
            pass
        candidate = current_repo_root()
        if candidate is not None and not _looks_like_python_install_root(candidate):
            return candidate
        try:
            cwd = Path.cwd().resolve()
        except Exception:
            cwd = None
        if cwd is not None:
            for base in (cwd, *cwd.parents):
                if _looks_like_project_root(base):
                    return base
        return shared_dotenv_root

    def _sidecar_active_slot_repo_root(self) -> Path | None:
        manifest = active_slot_manifest()
        raw = str((manifest or {}).get("repo_dir") or "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.exists():
                return path
        current_slot = str(active_slot() or "").strip().upper() or None
        if not current_slot:
            return None
        slot_payload = core_slot_status().get("slots", {}).get(current_slot, {}) if isinstance(core_slot_status().get("slots"), dict) else {}
        structure = slot_payload.get("structure") if isinstance(slot_payload.get("structure"), dict) else {}
        raw = str(structure.get("repo_dir") or "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.exists():
                return path
        return None

    def _sidecar_controlled_relpaths(self) -> list[Path]:
        return [Path(rel_path) for rel_path in SIDECAR_CONTROLLED_PATHS]

    def _sidecar_validated_slot_source(self) -> dict[str, Any]:
        slot_repo = self._sidecar_active_slot_repo_root()
        status = read_core_update_status()
        state = str((status or {}).get("state") or "").strip().lower()
        phase = str((status or {}).get("phase") or "").strip().lower()
        current_slot = str(active_slot() or "").strip().upper() or None
        target_slot = str((status or {}).get("target_slot") or "").strip().upper() or None
        slot_validated = bool(
            current_slot
            and slot_repo is not None
            and (
                (state == "validated" and phase == "root_promotion_pending")
                or (state == "succeeded" and phase in {"validate", "root_promoted"})
            )
            and (not target_slot or target_slot == current_slot)
        )
        if slot_validated:
            return {
                "mode": "validated_active_slot_source",
                "reason": "active slot runtime validated; sidecar may sync controlled files from validated slot",
                "repo_root": slot_repo,
                "slot": current_slot,
            }
        return {
            "mode": "unavailable",
            "reason": "validated active slot source is unavailable",
            "repo_root": None,
            "slot": current_slot,
        }

    def _sidecar_tracked_paths(self) -> list[Path]:
        repo_root = self._sidecar_repo_root()
        if repo_root is None:
            return []
        candidates = [repo_root / rel_path for rel_path in self._sidecar_controlled_relpaths()]
        return [path.resolve() for path in candidates if path.exists()]

    def _sidecar_code_state(self) -> dict[str, Any]:
        repo_root = self._sidecar_repo_root()
        sync_source = self._sidecar_validated_slot_source()
        tracked_paths = self._sidecar_tracked_paths()
        digest = hashlib.sha256()
        tracked_text: list[str] = []
        for path in tracked_paths:
            try:
                stat = path.stat()
                tracked_text.append(str(path))
                digest.update(str(path).encode("utf-8", errors="ignore"))
                digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
                digest.update(str(int(stat.st_size)).encode("ascii"))
            except Exception:
                continue
        fingerprint = digest.hexdigest() if tracked_text else None
        return {
            "mode": "root_project",
            "reason": "sidecar always executes from root project code",
            "repo_root": str(repo_root) if repo_root is not None else None,
            "launch_cwd": self._sidecar_launch_cwd,
            "fingerprint": fingerprint,
            "updated_at": time.time() if fingerprint else None,
            "tracked_paths": tracked_text,
            "sync_source_mode": str(sync_source.get("mode") or "").strip() or None,
            "sync_source_reason": str(sync_source.get("reason") or "").strip() or None,
            "sync_source_repo_root": str(sync_source.get("repo_root")) if isinstance(sync_source.get("repo_root"), Path) else None,
            "sync_source_slot": str(sync_source.get("slot") or "").strip() or None,
        }

    def _sync_sidecar_controlled_files_from_validated_slot(self) -> dict[str, Any]:
        source = self._sidecar_validated_slot_source()
        source_root = source.get("repo_root")
        source_root = source_root if isinstance(source_root, Path) else None
        root_repo = self._sidecar_repo_root()
        if source_root is None or root_repo is None:
            return {"ok": True, "changed": False, "reason": str(source.get("reason") or "source_unavailable")}
        changed_paths: list[str] = []
        for rel_path in self._sidecar_controlled_relpaths():
            src = (source_root / rel_path).resolve()
            dst = (root_repo / rel_path).resolve()
            if not src.exists():
                continue
            try:
                src_bytes = src.read_bytes()
            except Exception:
                continue
            try:
                dst_bytes = dst.read_bytes() if dst.exists() else None
            except Exception:
                dst_bytes = None
            if dst_bytes == src_bytes:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src_bytes)
            changed_paths.append(str(rel_path).replace("\\", "/"))
        if changed_paths:
            self._sidecar_last_sync_at = time.time()
            self._sidecar_last_sync_source_slot = str(source.get("slot") or "").strip() or None
            self._sidecar_last_sync_reason = "validated_slot_sync"
            self._sidecar_last_sync_changed_paths = changed_paths
            return {
                "ok": True,
                "changed": True,
                "reason": "validated_slot_sync",
                "slot": self._sidecar_last_sync_source_slot,
                "changed_paths": changed_paths,
            }
        return {"ok": True, "changed": False, "reason": "up_to_date"}

    def _sidecar_restart_policy_state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "code_change_debounce_sec": _sidecar_code_change_debounce_sec(),
            "restart_window_sec": _sidecar_restart_window_sec(),
            "restart_limit": _sidecar_restart_limit(),
            "base_backoff_sec": _sidecar_restart_base_backoff_sec(),
            "max_backoff_sec": _sidecar_restart_max_backoff_sec(),
            "circuit_open_sec": _sidecar_restart_circuit_open_sec(),
            "pending_code_fingerprint": self._sidecar_code_change_pending_fingerprint,
            "pending_code_since": self._sidecar_code_change_pending_since,
            "pending_code_age_s": (
                max(0.0, now - float(self._sidecar_code_change_pending_since))
                if self._sidecar_code_change_pending_since
                else None
            ),
            "restart_backoff_until": self._sidecar_restart_backoff_until,
            "restart_backoff_remaining_s": (
                max(0.0, float(self._sidecar_restart_backoff_until) - now)
                if self._sidecar_restart_backoff_until and self._sidecar_restart_backoff_until > now
                else 0.0
            ),
            "circuit_open_until": self._sidecar_circuit_open_until,
            "circuit_open_remaining_s": (
                max(0.0, float(self._sidecar_circuit_open_until) - now)
                if self._sidecar_circuit_open_until and self._sidecar_circuit_open_until > now
                else 0.0
            ),
            "recent_restart_total": len(self._sidecar_restart_history),
            "last_restart_at": self._sidecar_last_restart_at,
        }

    def _record_sidecar_restart_attempt(self, *, reason: str) -> None:
        del reason
        now = time.time()
        window_sec = _sidecar_restart_window_sec()
        while self._sidecar_restart_history and now - self._sidecar_restart_history[0] > window_sec:
            self._sidecar_restart_history.popleft()
        self._sidecar_restart_history.append(now)
        self._sidecar_last_restart_at = now
        recent_total = len(self._sidecar_restart_history)
        if recent_total >= _sidecar_restart_limit():
            self._sidecar_circuit_open_until = now + _sidecar_restart_circuit_open_sec()
            self._sidecar_restart_backoff_until = self._sidecar_circuit_open_until
            return
        exponent = max(0, recent_total - 2)
        backoff_sec = min(_sidecar_restart_max_backoff_sec(), _sidecar_restart_base_backoff_sec() * (2 ** exponent))
        self._sidecar_restart_backoff_until = now + backoff_sec if recent_total > 1 else None
        self._sidecar_circuit_open_until = None

    def _sidecar_restart_allowed(self) -> tuple[bool, str | None]:
        now = time.time()
        if self._sidecar_circuit_open_until and self._sidecar_circuit_open_until > now:
            return False, "supervisor.sidecar.circuit_open"
        if self._sidecar_restart_backoff_until and self._sidecar_restart_backoff_until > now:
            return False, "supervisor.sidecar.backoff"
        return True, None

    async def _probe_sidecar_health(self, *, force: bool = False) -> bool | None:
        snapshot = realtime_sidecar_listener_snapshot(self._sidecar_proc, role=self._sidecar_role())
        if not bool(snapshot.get("listener_running")):
            self._sidecar_last_probe_at = time.time()
            self._sidecar_last_probe_ok = False
            self._sidecar_last_probe_error = "listener_not_running"
            self._sidecar_consecutive_probe_failures += 1
            return False
        now = time.time()
        if (
            not force
            and self._sidecar_last_probe_at is not None
            and now - self._sidecar_last_probe_at < 5.0
        ):
            return self._sidecar_last_probe_ok
        try:
            ready = await probe_realtime_sidecar_ready(
                host=str(snapshot.get("host") or "127.0.0.1"),
                port=int(snapshot.get("port") or 0),
                timeout_s=1.5,
            )
        except Exception as exc:
            ready = False
            self._sidecar_last_probe_error = f"{type(exc).__name__}: {exc}"
        else:
            self._sidecar_last_probe_error = None if ready else "probe_not_ready"
        self._sidecar_last_probe_at = now
        self._sidecar_last_probe_ok = bool(ready)
        if ready:
            self._sidecar_consecutive_probe_failures = 0
        else:
            self._sidecar_consecutive_probe_failures += 1
        return ready

    def _desired_memory_profile_mode(self) -> str:
        mode = str(self._memory_requested_profile_mode or "").strip().lower() or "normal"
        return mode if mode in {"normal", "sampled_profile", "trace_profile"} else "normal"

    def _managed_runtime_uptime_sec(self, *, now: float | None = None) -> float | None:
        if self._last_start_at is None:
            return None
        current_time = time.time() if now is None else float(now)
        return max(0.0, current_time - float(self._last_start_at))

    def _active_memory_profile_trigger_source(self) -> str:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id:
            return ""
        summary = read_memory_session_summary(session_id) or {}
        return str(summary.get("trigger_source") or "").strip().lower()

    def _memory_session_index_items(self) -> list[dict[str, Any]]:
        index = read_memory_session_index()
        items = index.get("sessions") if isinstance(index.get("sessions"), list) else []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _memory_session_telemetry_window(self, session: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
        runtime_instance_id = str(session.get("runtime_instance_id") or "").strip() or None
        started_at = float(session.get("started_at") or session.get("requested_at") or 0.0)
        finished_at = float(session.get("finished_at") or time.time())
        items = read_memory_telemetry_tail(limit=5000)
        window: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sampled_at = float(item.get("sampled_at") or 0.0)
            if sampled_at and sampled_at < started_at:
                continue
            if finished_at and sampled_at and sampled_at > finished_at:
                continue
            if runtime_instance_id and str(item.get("runtime_instance_id") or "").strip() not in {"", runtime_instance_id}:
                continue
            window.append(item)
        return window[-max(1, int(limit or 1)) :]

    def _memory_profile_request_timeout_sec(self) -> float:
        try:
            timeout_sec = float(
                os.getenv("ADAOS_SUPERVISOR_MEMORY_PROFILE_REQUEST_TIMEOUT_S", "90") or "90"
            )
        except Exception:
            timeout_sec = 90.0
        return max(5.0, float(timeout_sec))

    def _capture_memory_profile_local_incident_artifact(
        self,
        session_id: str,
        *,
        reason: str,
        stage: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        token = str(session_id or "").strip()
        if not token:
            return None
        summary = read_memory_session_summary(token)
        if not isinstance(summary, dict):
            return None
        stage_token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(stage or "incident"))
        stage_token = stage_token.strip("-_") or "incident"
        artifact_id = f"local-incident-{stage_token}"
        now = time.time()
        operations_tail = read_memory_session_operations(token, limit=200)
        telemetry_tail = self._memory_session_telemetry_window(summary, limit=200)
        yjs_pressure: dict[str, Any] = {}
        try:
            from adaos.services.yjs.gateway_ws import yjs_pressure_snapshot

            snapshot = yjs_pressure_snapshot()
            yjs_pressure = dict(snapshot) if isinstance(snapshot, dict) else {}
        except Exception:
            yjs_pressure = {}
        route_diagnostics: dict[str, Any] = {}
        try:
            from adaos.services.reliability import channel_diagnostics_snapshot

            snapshot = channel_diagnostics_snapshot()
            route_diagnostics = (
                dict(snapshot.get("route") or {})
                if isinstance(snapshot, dict) and isinstance(snapshot.get("route"), dict)
                else {}
            )
        except Exception:
            route_diagnostics = {}
        member_snapshot_rebuild: dict[str, Any] = {}
        try:
            from adaos.services.scenario.webspace_runtime import member_snapshot_rebuild_runtime_snapshot

            snapshot = member_snapshot_rebuild_runtime_snapshot(limit=25)
            member_snapshot_rebuild = dict(snapshot) if isinstance(snapshot, dict) else {}
        except Exception:
            member_snapshot_rebuild = {}
        eventbus_backlog: dict[str, Any] = {}
        try:
            snapshot = self._ctx.bus.backlog_snapshot() if hasattr(self._ctx.bus, "backlog_snapshot") else {}
            eventbus_backlog = dict(snapshot) if isinstance(snapshot, dict) else {}
        except Exception:
            eventbus_backlog = {}
        incident_summary: dict[str, Any] = {}
        try:
            from adaos.services.hmg_incident_summary import build_hmg_incident_summary

            incident_summary = build_hmg_incident_summary(
                route_diagnostics=route_diagnostics,
                yjs_pressure=yjs_pressure,
                member_snapshot_rebuild=member_snapshot_rebuild,
                eventbus_backlog=eventbus_backlog,
            )
        except Exception:
            incident_summary = {}
        payload = {
            "captured_at": now,
            "reason": str(reason or "").strip() or "memory_profile_failure",
            "stage": stage_token,
            "details": dict(details or {}),
            "session": summary,
            "runtime": self._memory_runtime_state_payload(),
            "telemetry_tail": [dict(item) for item in telemetry_tail[-50:] if isinstance(item, dict)],
            "operations_tail": [dict(item) for item in operations_tail[-50:] if isinstance(item, dict)],
            "yjs_pressure": yjs_pressure,
            "route_diagnostics": route_diagnostics,
            "member_snapshot_rebuild": member_snapshot_rebuild,
            "eventbus_backlog": eventbus_backlog,
            "incident_summary": incident_summary,
        }
        artifact_dir = supervisor_memory_session_artifacts_dir(token)
        path = (artifact_dir / f"{artifact_id}.json").resolve()
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(content)
        artifact_ref = {
            "artifact_id": artifact_id,
            "kind": "local_incident_context",
            "path": str(path),
            "content_type": "application/json",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "created_at": now,
            "published_ref": None,
        }
        refs = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
        summary["artifact_refs"] = [
            dict(item)
            for item in refs
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip() != artifact_id
        ] + [artifact_ref]
        window = summary.get("operation_window") if isinstance(summary.get("operation_window"), dict) else {}
        window = dict(window)
        window["local_incident_artifact_id"] = artifact_id
        window["local_incident_stage"] = stage_token
        window["local_incident_reason"] = str(reason or "").strip() or "memory_profile_failure"
        window["local_incident_captured_at"] = now
        if incident_summary:
            window["local_incident_headline"] = str(incident_summary.get("headline") or "").strip() or None
            window["local_incident_dominant_signal"] = (
                str(incident_summary.get("dominant_signal") or "").strip() or None
            )
        summary["operation_window"] = window
        self._upsert_memory_session_summary(summary)
        return artifact_ref

    def _fail_active_memory_session(
        self,
        *,
        reason: str,
        exit_code: int | None = None,
        stage: str = "profile_runtime",
        details: dict[str, Any] | None = None,
    ) -> None:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id:
            return
        summary = read_memory_session_summary(session_id)
        if not isinstance(summary, dict):
            return
        state = str(summary.get("session_state") or "").strip().lower()
        if state in {"finished", "stopped", "cancelled", "failed"}:
            return
        now = time.time()
        summary["session_state"] = "failed"
        summary["stop_reason"] = reason
        summary["stopped_at"] = now
        summary["finished_at"] = summary.get("finished_at") or now
        operation_window = summary.get("operation_window") if isinstance(summary.get("operation_window"), dict) else {}
        operation_window = dict(operation_window)
        operation_window["failure_reason"] = str(reason or "").strip() or "memory_profile_failure"
        operation_window["failure_stage"] = str(stage or "profile_runtime").strip() or "profile_runtime"
        operation_window["failure_at"] = now
        if details:
            operation_window["failure_details"] = dict(details)
        if exit_code is not None:
            operation_window["exit_code"] = int(exit_code)
        summary["operation_window"] = operation_window
        updated = self._upsert_memory_session_summary(summary)
        artifact_ref = None
        try:
            artifact_ref = self._capture_memory_profile_local_incident_artifact(
                session_id,
                reason=reason,
                stage=stage,
                details={
                    **(dict(details or {})),
                    "exit_code": int(exit_code) if exit_code is not None else None,
                },
            )
        except Exception:
            _LOG.warning(
                "failed to capture local memory incident artifact session_id=%s stage=%s",
                session_id,
                stage,
                exc_info=True,
            )
        self._append_memory_operation(
            session_id=session_id,
            event="tool_invoked",
            profile_mode=str(updated.get("profile_mode") or self._memory_profile_mode),
            details={
                "action": "profile_failed",
                "reason": reason,
                "stage": str(stage or "profile_runtime").strip() or "profile_runtime",
                "exit_code": int(exit_code) if exit_code is not None else None,
                "artifact_id": str((artifact_ref or {}).get("artifact_id") or "").strip() or None,
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            },
        )
        self._memory_active_session_id = None
        self._memory_requested_profile_mode = None
        if session_id == str(self._memory_publish_request_session_id or "").strip():
            self._memory_publish_request_session_id = None
        self._memory_profile_mode = "normal"
        self._persist_runtime_state()

    def _persist_memory_session_index_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "contract_version": "1",
            "sessions": items,
            "updated_at": time.time(),
        }
        return write_memory_session_index(payload)

    def _upsert_memory_session_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary = write_memory_session_summary(str(payload.get("session_id") or "session"), payload)
        items = self._memory_session_index_items()
        summary_item = dict(
            {
                "session_id": summary.get("session_id"),
                "slot": summary.get("slot"),
                "profile_mode": summary.get("profile_mode"),
                "session_state": summary.get("session_state"),
                "trigger_source": summary.get("trigger_source"),
                "trigger_reason": summary.get("trigger_reason"),
                "requested_at": summary.get("requested_at"),
                "started_at": summary.get("started_at"),
                "finished_at": summary.get("finished_at"),
                "suspected_leak": bool(summary.get("suspected_leak")),
                "retry_of_session_id": summary.get("retry_of_session_id"),
                "retry_depth": int(summary.get("retry_depth") or 0),
                "published_to_root": bool(summary.get("published_to_root")),
                "publish_state": summary.get("publish_state"),
                "published_ref": summary.get("published_ref"),
            }
        )
        replaced = False
        for index, item in enumerate(items):
            if str(item.get("session_id") or "").strip() == str(summary.get("session_id") or "").strip():
                items[index] = summary_item
                replaced = True
                break
        if not replaced:
            items.append(summary_item)
        self._persist_memory_session_index_items(items)
        self._memory_last_session_id = str(summary.get("session_id") or "").strip() or self._memory_last_session_id
        return summary

    def _append_memory_operation(
        self,
        *,
        session_id: str,
        event: str,
        profile_mode: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operations = read_memory_session_operations(session_id, limit=5000)
        payload = {
            "event_id": f"op-{uuid.uuid4().hex[:10]}",
            "event": event,
            "emitted_at": time.time(),
            "contract_version": MEMORY_OPERATION_CONTRACT_VERSION,
            "session_id": session_id,
            "profile_mode": profile_mode or self._memory_profile_mode,
            "slot": str(active_slot() or "").strip().upper() or None,
            "runtime_instance_id": self._managed_runtime_instance_id,
            "transition_role": self._managed_transition_role,
            "sample_source": "supervisor",
            "sequence": len(operations) + 1,
            "details": dict(details or {}),
        }
        return append_memory_session_operation(session_id, payload)

    def _request_memory_profile_session(
        self,
        *,
        profile_mode: str,
        reason: str,
        trigger_source: str,
        trigger_threshold: str | None = None,
    ) -> dict[str, Any]:
        requested_mode = str(profile_mode or "").strip().lower() or "sampled_profile"
        if requested_mode not in {"sampled_profile", "trace_profile"}:
            raise HTTPException(status_code=400, detail="unsupported profile_mode")
        if _is_transition_in_progress(read_core_update_status(), _read_update_attempt()):
            raise HTTPException(status_code=409, detail="memory profiling intent is blocked during active transition")
        active_session_id = str(self._memory_active_session_id or "").strip()
        if active_session_id:
            active_session = read_memory_session_summary(active_session_id) or {}
            active_state = str(active_session.get("session_state") or "").strip().lower()
            if active_state in {"planned", "requested", "running"}:
                raise HTTPException(status_code=409, detail="a memory profiling session is already active")
        session_id = f"mem-{uuid.uuid4().hex[:8]}"
        now = time.time()
        summary = self._upsert_memory_session_summary(
            {
                "session_id": session_id,
                "slot": str(active_slot() or "").strip().upper() or None,
                "runtime_instance_id": self._managed_runtime_instance_id,
                "transition_role": self._managed_transition_role,
                "profile_mode": requested_mode,
                "session_state": "requested",
                "trigger_source": trigger_source,
                "trigger_reason": str(reason or "supervisor.memory.request"),
                "trigger_threshold": str(trigger_threshold or "").strip() or None,
                "baseline_rss_bytes": self._memory_baseline_family_rss_bytes,
                "peak_rss_bytes": None,
                "rss_growth_bytes": self._memory_last_growth_bytes,
                "requested_at": now,
                "started_at": None,
                "finished_at": None,
                "publish_state": "local_only",
                "suspected_leak": trigger_source == "policy",
                "retry_of_session_id": None,
                "retry_root_session_id": None,
                "retry_depth": 0,
                "operation_window": {
                    "contract_version": MEMORY_OPERATION_CONTRACT_VERSION,
                    "events_path": str(supervisor_memory_session_operations_path(session_id)),
                },
            }
        )
        self._memory_active_session_id = session_id
        self._memory_last_session_id = session_id
        self._memory_requested_profile_mode = requested_mode
        self._append_memory_operation(
            session_id=session_id,
            event="tool_invoked",
            profile_mode=requested_mode,
            details={
                "action": "profile_start",
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                "reason": str(reason or "supervisor.memory.request"),
                "trigger_source": trigger_source,
                "trigger_threshold": str(trigger_threshold or "").strip() or None,
                "note": "Supervisor will apply requested profile mode via controlled runtime restart",
            },
        )
        self._persist_runtime_state()
        return summary

    def _mark_active_memory_session_running(self, *, runtime_instance_id: str | None, transition_role: str) -> None:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id:
            return
        summary = read_memory_session_summary(session_id)
        if not isinstance(summary, dict):
            return
        now = time.time()
        summary["slot"] = str(active_slot() or "").strip().upper() or summary.get("slot")
        summary["runtime_instance_id"] = runtime_instance_id
        summary["transition_role"] = transition_role
        summary["session_state"] = "running"
        summary["started_at"] = summary.get("started_at") or now
        summary["baseline_rss_bytes"] = summary.get("baseline_rss_bytes") or self._memory_baseline_family_rss_bytes
        summary["rss_growth_bytes"] = self._memory_last_growth_bytes
        updated = self._upsert_memory_session_summary(summary)
        self._append_memory_operation(
            session_id=session_id,
            event="slot_started",
            profile_mode=str(updated.get("profile_mode") or self._memory_profile_mode),
            details={
                "action": "profile_mode_applied",
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                "runtime_instance_id": runtime_instance_id,
                "transition_role": transition_role,
            },
        )

    def _update_memory_session_peak(self, family_rss_bytes: int | None) -> None:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id or family_rss_bytes is None:
            return
        summary = read_memory_session_summary(session_id)
        if not isinstance(summary, dict):
            return
        peak = summary.get("peak_rss_bytes")
        if peak is None or int(family_rss_bytes) > int(peak):
            summary["peak_rss_bytes"] = int(family_rss_bytes)
        summary["rss_growth_bytes"] = self._memory_last_growth_bytes
        self._upsert_memory_session_summary(summary)

    def _memory_policy_auto_profile_guard(self, *, now: float) -> tuple[bool, str | None]:
        min_uptime_sec = _memory_auto_profile_min_uptime_sec()
        if min_uptime_sec > 0:
            uptime_sec = self._managed_runtime_uptime_sec(now=now)
            if uptime_sec is None or uptime_sec < min_uptime_sec:
                observed = "unknown" if uptime_sec is None else f"{uptime_sec:.1f}s"
                return False, f"auto_profile_min_uptime:{observed}<{min_uptime_sec:.1f}s"
        cooldown_cutoff = now - _memory_auto_profile_cooldown_sec()
        circuit_cutoff = now - _memory_auto_profile_circuit_window_sec()
        recent_policy_sessions = 0
        for item in reversed(self._memory_session_index_items()):
            if not isinstance(item, dict):
                continue
            if str(item.get("trigger_source") or "").strip().lower() != "policy":
                continue
            requested_at = float(item.get("requested_at") or 0.0)
            if requested_at >= cooldown_cutoff:
                return False, "auto_profile_cooldown"
            if requested_at >= circuit_cutoff:
                recent_policy_sessions += 1
                if recent_policy_sessions >= _memory_auto_profile_circuit_limit():
                    return False, "auto_profile_circuit_open"
        return self._memory_profile_subnet_guard()

    def _memory_live_subnet_state(
        self,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        browser_total = 0
        browser_latest_age: float | None = None
        now = time.time()
        try:
            from adaos.services.access_links import browser_snapshot

            ttl_sec = _memory_auto_profile_browser_live_ttl_sec()
            for entry in browser_snapshot():
                if not isinstance(entry, dict):
                    continue
                last_seen_at = float(entry.get("last_seen_at") or 0.0)
                if last_seen_at <= 0.0:
                    continue
                age = max(0.0, now - last_seen_at)
                state = str(entry.get("connection_state") or "").strip().lower()
                online = bool(entry.get("online"))
                if age <= ttl_sec and (online or state in {"connected", "open", "ready"}):
                    browser_total += 1
                    browser_latest_age = age if browser_latest_age is None else min(browser_latest_age, age)
        except Exception:
            browser_total = 0
            browser_latest_age = None
        runtime_payload = runtime if isinstance(runtime, dict) else self._runtime_reliability_payload(timeout=1.0)
        if browser_total > 0:
            age_text = "-" if browser_latest_age is None else f"{browser_latest_age:.1f}s"
            return True, f"browser_sessions_connected:{browser_total}:last_seen={age_text}"
        if not runtime_payload:
            return False, None
        node = runtime_payload.get("node") if isinstance(runtime_payload.get("node"), dict) else {}
        role = str(node.get("role") or self._managed_transition_role or self._sidecar_role() or "").strip().lower()
        if role == "hub":
            member_state = (
                runtime_payload.get("hub_member_connection_state")
                if isinstance(runtime_payload.get("hub_member_connection_state"), dict)
                else {}
            )
            connected_total = int(member_state.get("connected_total") or 0)
            if connected_total > 0:
                return True, f"subnet_members_connected:{connected_total}"
            return False, None
        if role == "member":
            member_state = (
                runtime_payload.get("hub_member_connection_state")
                if isinstance(runtime_payload.get("hub_member_connection_state"), dict)
                else {}
            )
            hub_state = member_state.get("hub") if isinstance(member_state.get("hub"), dict) else {}
            connected = bool(node.get("connected_to_subnet")) or bool(node.get("connected_to_hub")) or bool(hub_state.get("connected"))
            if connected:
                return True, "member_hub_connected"
        return False, None

    def _memory_profile_subnet_guard(
        self,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        subnet_live, subnet_reason = self._memory_live_subnet_state(runtime=runtime)
        if subnet_live:
            return False, subnet_reason
        return True, None

    def _memory_profile_restart_guard(self, *, desired_mode: str, now: float | None = None) -> tuple[bool, str | None]:
        normalized_mode = str(desired_mode or "").strip().lower()
        if normalized_mode == "normal":
            if (
                self._memory_profile_mode != "normal"
                and str(self._memory_profile_current_trigger_source or "").strip().lower() == "policy"
                and not _memory_policy_profile_restarts_enabled()
            ):
                return False, "policy_profile_restart_disabled"
            if self._memory_profile_mode != "normal" and str(self._memory_profile_current_trigger_source or "").strip().lower() == "policy":
                return self._memory_profile_subnet_guard()
            return True, None
        if self._active_memory_profile_trigger_source() != "policy":
            return True, None
        if not _memory_policy_profile_restarts_enabled():
            return False, "policy_profile_restart_disabled"
        min_uptime_sec = _memory_auto_profile_min_uptime_sec()
        if min_uptime_sec <= 0:
            return self._memory_profile_subnet_guard()
        uptime_sec = self._managed_runtime_uptime_sec(now=now)
        if uptime_sec is None or uptime_sec < min_uptime_sec:
            observed = "unknown" if uptime_sec is None else f"{uptime_sec:.1f}s"
            return False, f"auto_profile_min_uptime:{observed}<{min_uptime_sec:.1f}s"
        return self._memory_profile_subnet_guard()

    def _memory_critical_restart_decision(self, *, now: float | None = None) -> dict[str, Any] | None:
        if self._stopping or not self._desired_running:
            self._memory_critical_since = None
            self._memory_critical_reason = None
            return None
        if self._proc is None or self._proc.poll() is not None:
            self._memory_critical_since = None
            self._memory_critical_reason = None
            return None
        if _is_transition_in_progress(read_core_update_status(), _read_update_attempt()):
            self._memory_critical_since = None
            self._memory_critical_reason = None
            return None
        available_bytes = self._memory_last_available_bytes
        total_bytes = _total_memory_bytes()
        if available_bytes is None or total_bytes is None or total_bytes <= 0:
            self._memory_critical_since = None
            self._memory_critical_reason = None
            return None
        available_percent = (float(available_bytes) / float(total_bytes)) * 100.0
        self._memory_last_available_percent = available_percent
        threshold_percent = _memory_critical_available_percent_threshold()
        threshold_bytes = _memory_critical_available_bytes_threshold()
        reasons: list[str] = []
        if available_percent <= threshold_percent:
            reasons.append(f"available_percent<={threshold_percent:.1f}")
        if int(available_bytes) <= int(threshold_bytes):
            reasons.append(f"available_bytes<={int(threshold_bytes)}")
        if not reasons:
            self._memory_critical_since = None
            self._memory_critical_reason = None
            return None
        current_time = time.time() if now is None else float(now)
        reason = ",".join(reasons)
        if self._memory_critical_reason != reason:
            self._memory_critical_reason = reason
            self._memory_critical_since = current_time
            return None
        critical_since = float(self._memory_critical_since or current_time)
        duration_sec = _memory_critical_duration_sec()
        if (current_time - critical_since) < duration_sec:
            return None
        cooldown_sec = _memory_critical_restart_cooldown_sec()
        last_restart_at = float(self._memory_critical_restart_last_at or 0.0)
        if last_restart_at > 0.0 and (current_time - last_restart_at) < cooldown_sec:
            return None
        subnet_live, subnet_reason = self._memory_live_subnet_state()
        return {
            "reason": "supervisor.memory.critical_pressure",
            "message": (
                "runtime restart requested because free memory stayed below the critical threshold"
                f" for {duration_sec:.0f}s (available={int(available_bytes)}B, total={int(total_bytes)}B)"
            ),
            "available_memory_bytes": int(available_bytes),
            "available_memory_percent": round(available_percent, 3),
            "threshold_percent": float(threshold_percent),
            "threshold_bytes": int(threshold_bytes),
            "critical_for_sec": max(0.0, current_time - critical_since),
            "restart_cooldown_sec": float(cooldown_sec),
            "critical_reason": reason,
            "subnet_live": bool(subnet_live),
            "subnet_reason": subnet_reason,
        }

    def _record_memory_auto_profile_block(self, reason: str | None, *, now: float | None = None) -> None:
        reason_text = str(reason or "").strip()
        if not reason_text:
            return
        self._memory_auto_profile_last_block_reason = reason_text
        self._memory_auto_profile_last_block_at = time.time() if now is None else float(now)

    def _should_finalize_active_memory_profile(self, *, now: float | None = None) -> dict[str, Any] | None:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id:
            return None
        profile_mode = str(self._memory_profile_mode or "").strip().lower()
        max_runtime_sec = _memory_profile_max_runtime_sec(profile_mode)
        if profile_mode == "normal" or max_runtime_sec <= 0:
            return None
        summary = read_memory_session_summary(session_id)
        if not isinstance(summary, dict):
            return None
        state = str(summary.get("session_state") or "").strip().lower()
        if state not in {"running", "requested"}:
            return None
        started_at = float(summary.get("started_at") or summary.get("requested_at") or 0.0)
        if started_at <= 0:
            return None
        current_time = time.time() if now is None else float(now)
        window_started_at = float(summary.get("profile_window_started_at") or 0.0)
        if window_started_at <= 0.0 and started_at > 0.0:
            window_started_at = started_at
        if window_started_at <= 0.0:
            runtime_snapshot = self.status()
            if not bool(runtime_snapshot.get("runtime_api_ready")):
                return None
            window_started_at = current_time
            summary["profile_window_started_at"] = window_started_at
            summary["profile_window_started_reason"] = "runtime_api_ready"
            updated_window = summary.get("operation_window") if isinstance(summary.get("operation_window"), dict) else {}
            updated_window = dict(updated_window)
            updated_window["profile_window_started_at"] = window_started_at
            updated_window["profile_window_started_reason"] = "runtime_api_ready"
            summary["operation_window"] = updated_window
            self._upsert_memory_session_summary(summary)
            self._append_memory_operation(
                session_id=session_id,
                event="slot_started",
                profile_mode=profile_mode,
                details={
                    "action": "profile_window_started",
                    "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                    "reason": "runtime_api_ready",
                    "runtime_instance_id": self._managed_runtime_instance_id,
                    "transition_role": self._managed_transition_role,
                },
            )
            return None
        elapsed_sec = max(0.0, current_time - window_started_at)
        if elapsed_sec < max_runtime_sec:
            return None
        return {
            "session_id": session_id,
            "profile_mode": profile_mode,
            "trigger_source": str(summary.get("trigger_source") or "").strip().lower() or None,
            "elapsed_sec": elapsed_sec,
            "max_runtime_sec": max_runtime_sec,
            "reason": f"supervisor.memory.profile_window_complete.{profile_mode}",
        }

    def _expire_stuck_requested_memory_profile(self, *, now: float | None = None) -> dict[str, Any] | None:
        session_id = str(self._memory_active_session_id or "").strip()
        if not session_id:
            return None
        if str(self._memory_profile_mode or "").strip().lower() != "normal":
            return None
        desired_mode = self._desired_memory_profile_mode()
        if desired_mode == "normal":
            return None
        summary = read_memory_session_summary(session_id)
        if not isinstance(summary, dict):
            return None
        session_state = str(summary.get("session_state") or "").strip().lower()
        if session_state != "requested":
            return None
        current_time = time.time() if now is None else float(now)
        requested_at = float(summary.get("requested_at") or 0.0)
        if requested_at <= 0.0:
            return None
        timeout_sec = self._memory_profile_request_timeout_sec()
        elapsed_sec = max(0.0, current_time - requested_at)
        if elapsed_sec < timeout_sec:
            return None
        details = {
            "desired_profile_mode": desired_mode,
            "current_profile_mode": str(self._memory_profile_mode or "").strip().lower() or "normal",
            "requested_elapsed_sec": round(elapsed_sec, 3),
            "request_timeout_sec": timeout_sec,
            "active_slot": str(active_slot() or "").strip().upper() or None,
            "runtime_instance_id": self._managed_runtime_instance_id,
            "transition_role": self._managed_transition_role,
            "last_block_reason": self._memory_auto_profile_last_block_reason,
            "last_block_at": self._memory_auto_profile_last_block_at,
        }
        self._fail_active_memory_session(
            reason=f"requested_profile_mode_timeout.{desired_mode}",
            stage="profile_apply_timeout",
            details=details,
        )
        return {
            "session_id": session_id,
            "desired_mode": desired_mode,
            "elapsed_sec": elapsed_sec,
            "timeout_sec": timeout_sec,
        }

    async def _maybe_apply_memory_profile_mode(self) -> None:
        desired_mode = self._desired_memory_profile_mode()
        if desired_mode == self._memory_profile_mode:
            return
        if self._stopping or not self._desired_running:
            return
        if self._proc is None or self._proc.poll() is not None:
            return
        if _is_transition_in_progress(read_core_update_status(), _read_update_attempt()):
            return
        allowed, block_reason = self._memory_profile_restart_guard(desired_mode=desired_mode)
        if not allowed:
            self._record_memory_auto_profile_block(block_reason)
            self._persist_runtime_state()
            return
        await self.restart_runtime(reason=f"supervisor.memory.apply_profile_mode.{desired_mode}")

    def _sample_memory_telemetry(self) -> dict[str, Any] | None:
        now = time.time()
        interval_sec = _memory_telemetry_interval_sec()
        if self._memory_last_telemetry_at and now - self._memory_last_telemetry_at < interval_sec:
            return None
        managed = _proc_details(self._proc, cwd_hint=self._managed_runtime_cwd)
        managed_pid = managed.get("managed_pid")
        if not managed_pid:
            return None
        process_rss_bytes, family_rss_bytes = _process_family_rss_bytes(managed_pid)
        if family_rss_bytes is None:
            return None
        self._memory_last_telemetry_at = now
        self._memory_last_available_bytes = _available_memory_bytes()
        total_memory_bytes = _total_memory_bytes()
        self._memory_last_available_percent = (
            ((float(self._memory_last_available_bytes) / float(total_memory_bytes)) * 100.0)
            if self._memory_last_available_bytes is not None and total_memory_bytes not in {None, 0}
            else None
        )
        family_rss_value = int(family_rss_bytes)
        baseline_family_rss = _positive_int_or_none(self._memory_baseline_family_rss_bytes)
        if family_rss_value > 0 and (baseline_family_rss is None or family_rss_value < baseline_family_rss):
            baseline_family_rss = family_rss_value
        self._memory_baseline_family_rss_bytes = baseline_family_rss
        growth_bytes = max(0, family_rss_value - baseline_family_rss) if baseline_family_rss is not None else 0
        tail = read_memory_telemetry_tail(limit=256)
        window_start = now - _memory_telemetry_window_sec()
        window = [item for item in tail if float(item.get("sampled_at") or 0.0) >= window_start]
        first = window[0] if window else None
        slope = 0.0
        if isinstance(first, dict):
            first_family = int(first.get("family_rss_bytes") or family_rss_bytes)
            first_at = float(first.get("sampled_at") or now)
            elapsed_min = max((now - first_at) / 60.0, 1.0 / 60.0)
            slope = max(0.0, (int(family_rss_bytes) - first_family) / elapsed_min)
        suspicion_state = "stable"
        suspicion_reason: str | None = None
        growth_threshold = _memory_suspicion_growth_threshold_bytes()
        slope_threshold = _memory_suspicion_slope_threshold_bytes_per_min()
        if growth_bytes >= growth_threshold and slope >= slope_threshold:
            suspicion_state = "suspected"
            suspicion_reason = "growth_and_slope_threshold"
            if self._memory_suspicion_since is None:
                self._memory_suspicion_since = now
        elif growth_bytes >= growth_threshold:
            suspicion_state = "suspected"
            suspicion_reason = "growth_threshold"
            if self._memory_suspicion_since is None:
                self._memory_suspicion_since = now
        elif slope >= slope_threshold:
            suspicion_state = "watch"
            suspicion_reason = "slope_threshold"
            self._memory_suspicion_since = None
        else:
            self._memory_suspicion_since = None
        self._memory_suspicion_state = suspicion_state
        self._memory_suspicion_reason = suspicion_reason
        self._memory_last_growth_bytes = growth_bytes
        self._memory_last_growth_bytes_per_min = slope
        sample = append_memory_telemetry_sample(
            {
                "sampled_at": now,
                "slot": str(active_slot() or "").strip().upper() or None,
                "runtime_instance_id": self._managed_runtime_instance_id,
                "transition_role": self._managed_transition_role,
                "managed_pid": managed_pid,
                "profile_mode": self._memory_profile_mode,
                "suspicion_state": suspicion_state,
                "process_rss_bytes": process_rss_bytes,
                "family_rss_bytes": family_rss_bytes,
                "available_memory_bytes": self._memory_last_available_bytes,
                "available_memory_percent": self._memory_last_available_percent,
                "baseline_rss_bytes": self._memory_baseline_family_rss_bytes,
                "rss_growth_bytes": growth_bytes,
                "rss_growth_bytes_per_min": slope,
                "sample_source": "supervisor",
            }
        )
        self._update_memory_session_peak(family_rss_bytes)
        if (
            suspicion_state == "suspected"
            and self._desired_memory_profile_mode() == "normal"
            and not str(self._memory_active_session_id or "").strip()
        ):
            auto_allowed, auto_block_reason = self._memory_policy_auto_profile_guard(now=now)
            if auto_allowed:
                if not _memory_policy_profile_restarts_enabled() and self._memory_profile_mode != "sampled_profile":
                    self._record_memory_auto_profile_block("policy_profile_restart_disabled", now=now)
                else:
                    try:
                        self._request_memory_profile_session(
                            profile_mode="sampled_profile",
                            reason=f"memory.{suspicion_reason or 'threshold'}",
                            trigger_source="policy",
                            trigger_threshold=(
                                f"growth>={growth_threshold}; slope>={int(slope_threshold)}"
                            ),
                        )
                    except HTTPException:
                        pass
            else:
                self._record_memory_auto_profile_block(auto_block_reason, now=now)
        self._persist_runtime_state()
        return sample

    def _memory_profile_finalize_observed(self, session_id: str | None) -> bool:
        token = str(session_id or "").strip()
        if not token:
            return False
        summary = read_memory_session_summary(token) or {}
        artifact_refs = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
        artifact_kinds = {str(item.get("kind") or "").strip() for item in artifact_refs if isinstance(item, dict)}
        if "runtime_profile_finalize_debug" in artifact_kinds:
            return True
        if "tracemalloc_final_snapshot" in artifact_kinds or "tracemalloc_top_growth" in artifact_kinds:
            return True
        artifacts_dir = supervisor_memory_session_artifacts_dir(token)
        for name in (
            "runtime-admin-shutdown-debug.json",
            "runtime-profile-finalize-debug.json",
            "tracemalloc-final.json",
            "tracemalloc-top-growth.json",
        ):
            if (artifacts_dir / name).exists():
                return True
        return False

    def _schedule_service_restart(self, *, reason: str) -> dict[str, Any]:
        delay_sec = _root_restart_delay_sec()
        if not _autostart_self_restart_supported():
            return {
                "ok": True,
                "requested": False,
                "mode": "manual",
                "delay_sec": None,
                "reason": "autostart self-restart is unavailable for the current supervisor process",
            }
        if self._service_restart_pending:
            return {
                "ok": True,
                "requested": True,
                "mode": "self_exit",
                "delay_sec": delay_sec,
                "duplicate": True,
            }
        self._service_restart_pending = True
        pid = os.getpid()
        restart_reason = str(reason or "supervisor.update.complete")

        def _worker() -> None:
            try:
                time.sleep(delay_sec)
                _LOG.info(
                    "requesting autostart service self-restart pid=%s delay_sec=%.3f reason=%s",
                    pid,
                    delay_sec,
                    restart_reason,
                )
                os.kill(pid, signal.SIGTERM)
            except Exception:
                self._service_restart_pending = False
                _LOG.warning("failed to request autostart service self-restart", exc_info=True)

        thread = threading.Thread(target=_worker, name="adaos-supervisor-self-restart", daemon=True)
        self._service_restart_thread = thread
        thread.start()
        return {"ok": True, "requested": True, "mode": "self_exit", "delay_sec": delay_sec}

    async def complete_update(self, *, reason: str, auto: bool = False) -> dict[str, Any]:
        status = read_core_update_status()
        attempt = _read_update_attempt() or {}
        runtime = self.status()
        promotion: dict[str, Any] | None = None
        if _is_root_promotion_pending_status(status) or bool(runtime.get("root_promotion_required")):
            promotion = await self.promote_root(reason=reason)
            status = promotion.get("status") if isinstance(promotion.get("status"), dict) else read_core_update_status()
            attempt = _read_update_attempt() or {}
            runtime = self.status()
        if not (_is_root_restart_pending_status(status) or _is_root_restart_pending_attempt(attempt)):
            return {
                "ok": True,
                "accepted": False,
                "noop": True,
                "auto": bool(auto),
                "restart_required": False,
                "status": status,
                "attempt": attempt,
                "runtime": runtime,
                "promotion": promotion,
                "restart": {"ok": True, "requested": False, "mode": "none"},
                "message": "root promotion is not required for the current update state",
                "_served_by": "supervisor",
            }

        restart = self._schedule_service_restart(reason=reason)
        now = time.time()
        status_payload = dict(status)
        status_payload["state"] = "succeeded"
        status_payload["phase"] = "root_promoted"
        status_payload["root_promotion_required"] = False
        status_payload["restart_mode"] = str(restart.get("mode") or "manual")
        status_payload["updated_at"] = now
        if restart.get("requested"):
            status_payload["message"] = "root promotion completed; restarting autostart service to activate updated supervisor"
            status_payload["restart_requested_at"] = now
        else:
            status_payload["message"] = "root promotion completed; autostart service restart is still required"
        status = write_core_update_status(status_payload)

        attempt_payload = dict(attempt)
        attempt_payload["state"] = "awaiting_root_restart"
        attempt_payload["action"] = str(attempt_payload.get("action") or status.get("action") or "update")
        attempt_payload["accepted"] = True
        attempt_payload["awaiting_restart"] = True
        attempt_payload["restart_required"] = True
        attempt_payload["restart_mode"] = str(restart.get("mode") or "manual")
        attempt_payload["requested_at"] = _epoch(attempt_payload.get("requested_at")) or now
        attempt_payload["transitioned_at"] = _epoch(attempt_payload.get("transitioned_at")) or now
        attempt_payload["updated_at"] = now
        attempt_payload["completion_reason"] = ""
        attempt_payload["last_status"] = status
        if restart.get("requested"):
            attempt_payload["restart_requested_at"] = now
        attempt = _write_update_attempt(attempt_payload)
        return {
            "ok": True,
            "accepted": True,
            "auto": bool(auto),
            "restart_required": True,
            "status": status,
            "attempt": attempt,
            "runtime": runtime,
            "promotion": promotion,
            "restart": restart,
            "message": str(status.get("message") or "").strip(),
            "_served_by": "supervisor",
        }

    def _sidecar_role(self) -> str | None:
        try:
            ctx = get_ctx()
        except Exception:
            ctx = None
        if ctx is not None:
            with contextlib.suppress(Exception):
                role = str(getattr(ctx, "config", None).role or "").strip().lower()
                if role:
                    return role
            with contextlib.suppress(Exception):
                conf = load_config(ctx=ctx)
                role = str(getattr(conf, "role", "") or "").strip().lower()
                if role:
                    return role
        try:
            conf = load_config()
            role = str(getattr(conf, "role", "") or "").strip().lower()
            if role:
                return role
        except Exception:
            pass
        return None

    def _sidecar_status_payload(self) -> dict[str, Any]:
        role = self._sidecar_role()
        try:
            process = realtime_sidecar_listener_snapshot(self._sidecar_proc, role=role)
        except TypeError:
            process = realtime_sidecar_listener_snapshot(self._sidecar_proc)
        code_state = self._sidecar_code_state()
        process.update(
            {
                "health": {
                    "last_probe_at": self._sidecar_last_probe_at,
                    "last_probe_ok": self._sidecar_last_probe_ok,
                    "last_probe_error": self._sidecar_last_probe_error,
                    "consecutive_failures": int(self._sidecar_consecutive_probe_failures),
                },
                "code": {
                    **code_state,
                    "active_fingerprint": self._sidecar_code_fingerprint,
                    "active_updated_at": self._sidecar_code_fingerprint_updated_at,
                },
                "launch_cwd": self._sidecar_launch_cwd,
                "last_start_reason": self._sidecar_last_start_reason,
                "last_restart_reason": self._sidecar_last_restart_reason,
                "restart_policy": self._sidecar_restart_policy_state(),
                "sync": {
                    "last_sync_at": self._sidecar_last_sync_at,
                    "last_sync_source_slot": self._sidecar_last_sync_source_slot,
                    "last_sync_reason": self._sidecar_last_sync_reason,
                    "last_sync_changed_paths": list(self._sidecar_last_sync_changed_paths),
                },
            }
        )
        return {
            "enabled": bool(realtime_sidecar_enabled(role=role)),
            "role": role,
            "process": process,
            "code": process["code"],
            "health": process["health"],
            "restart_policy": process["restart_policy"],
            "sync": process["sync"],
            "launch_cwd": self._sidecar_launch_cwd,
            "last_start_reason": self._sidecar_last_start_reason,
            "last_restart_reason": self._sidecar_last_restart_reason,
        }

    def _runtime_request_json(
        self,
        *,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        session = requests.Session()
        try:
            try:
                session.trust_env = False
            except Exception:
                pass
            response = session.request(
                str(method or "GET").upper(),
                self.runtime_base_url + str(path or ""),
                headers=headers,
                json=payload,
                timeout=float(timeout),
            )
            if int(response.status_code or 0) >= 400:
                try:
                    detail: Any = response.json()
                except Exception:
                    detail = (response.text or f"runtime returned HTTP {response.status_code}").strip()[:500]
                if isinstance(detail, dict) and set(detail.keys()) == {"detail"}:
                    detail = detail["detail"]
                raise HTTPException(status_code=int(response.status_code), detail=detail)
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("runtime returned a non-object payload")
            return body
        finally:
            with contextlib.suppress(Exception):
                session.close()

    def _runtime_reliability_payload(self, *, timeout: float = 2.0) -> dict[str, Any]:
        try:
            payload = self._runtime_request_json(path="/api/node/reliability", timeout=timeout)
        except Exception as exc:
            _LOG.debug("supervisor reliability preflight unavailable: %s: %s", type(exc).__name__, exc)
            return {}
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        return dict(runtime) if isinstance(runtime, dict) else {}

    def _transition_continuity_guard_snapshot(self, *, timeout: float = 2.0) -> dict[str, Any]:
        runtime = self._runtime_reliability_payload(timeout=timeout)
        sidecar_runtime = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
        media_runtime = runtime.get("media_runtime") if isinstance(runtime.get("media_runtime"), dict) else {}
        continuity_contract = (
            sidecar_runtime.get("continuity_contract")
            if isinstance(sidecar_runtime.get("continuity_contract"), dict)
            else {}
        )
        update_guard = media_runtime.get("update_guard") if isinstance(media_runtime.get("update_guard"), dict) else {}
        role = str(update_guard.get("role") or self._sidecar_role() or "").strip().lower() or None
        return {
            "role": role,
            "continuity_contract": dict(continuity_contract) if isinstance(continuity_contract, dict) else {},
            "update_guard": dict(update_guard) if isinstance(update_guard, dict) else {},
        }

    @staticmethod
    def _transition_operation_label(operation: str) -> str:
        op = str(operation or "").strip().lower()
        if op == "restart":
            return "runtime restart"
        if op == "rollback":
            return "core rollback"
        return "core update"

    def _transition_continuity_guard_decision(self, *, operation: str) -> dict[str, Any] | None:
        snapshot = self._transition_continuity_guard_snapshot(timeout=2.0)
        role = str(snapshot.get("role") or "").strip().lower()
        continuity_contract = (
            snapshot.get("continuity_contract")
            if isinstance(snapshot.get("continuity_contract"), dict)
            else {}
        )
        update_guard = snapshot.get("update_guard") if isinstance(snapshot.get("update_guard"), dict) else {}
        if role not in {"hub", "member"}:
            return None

        member_policy = str(update_guard.get("member_runtime_update") or "allow").strip().lower() or "allow"
        hub_policy = str(
            continuity_contract.get("hub_runtime_update") or update_guard.get("hub_runtime_update") or "allow"
        ).strip().lower() or "allow"
        current_support = str(
            continuity_contract.get("current_support") or update_guard.get("current_support") or "unknown"
        ).strip().lower() or "unknown"
        required = bool(
            continuity_contract.get("required") or update_guard.get("hub_sidecar_continuity_required")
        )
        operation_label = self._transition_operation_label(operation)

        if role == "member" and member_policy == "defer" and bool(update_guard.get("live_session_present")):
            return {
                "code": "member_live_media_defer",
                "planned_reason": "live_media_guard",
                "message": f"{operation_label} deferred while member owns an active browser media session",
                "retry_after_sec": max(_live_media_guard_defer_sec(), _min_update_period_sec()),
                "live_media_guard": update_guard,
                "continuity_contract": continuity_contract,
            }

        if role == "hub" and required and hub_policy == "preserve_sidecar" and current_support != "ready":
            return {
                "code": "hub_sidecar_continuity_pending",
                "planned_reason": "live_media_guard",
                "message": (
                    f"{operation_label} deferred until independent sidecar continuity is ready "
                    "for the active live media path"
                ),
                "retry_after_sec": max(_live_media_guard_defer_sec(), _min_update_period_sec()),
                "live_media_guard": update_guard,
                "continuity_contract": continuity_contract,
            }

        return None

    def _schedule_continuity_guarded_transition(
        self,
        request: dict[str, Any],
        decision: dict[str, Any],
        *,
        current_status: dict[str, Any] | None = None,
        current_attempt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        retry_after_sec = max(30.0, float(decision.get("retry_after_sec") or _live_media_guard_defer_sec()))
        existing_due_at = _epoch((current_attempt or {}).get("scheduled_for") or (current_status or {}).get("scheduled_for"))
        scheduled_for = max(time.time() + retry_after_sec, existing_due_at)
        extra_payload = {
            "guard_code": str(decision.get("code") or "").strip() or None,
            "live_media_guard": decision.get("live_media_guard"),
            "continuity_contract": decision.get("continuity_contract"),
        }
        return self._schedule_planned_transition(
            request=request,
            scheduled_for=scheduled_for,
            planned_reason=str(decision.get("planned_reason") or "live_media_guard"),
            message=str(decision.get("message") or "transition deferred by live media guard"),
            extra_status=extra_payload,
            extra_attempt=extra_payload,
        )

    def _raise_restart_continuity_block(self, decision: dict[str, Any]) -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(decision.get("message") or "runtime restart blocked by live media guard"),
                "planned_reason": str(decision.get("planned_reason") or "live_media_guard"),
                "guard_code": str(decision.get("code") or "").strip() or None,
                "live_media_guard": decision.get("live_media_guard"),
                "continuity_contract": decision.get("continuity_contract"),
                "retry_after_sec": float(decision.get("retry_after_sec") or _live_media_guard_defer_sec()),
            },
        )

    def _runtime_sidecar_runtime_payload(self) -> dict[str, Any]:
        runtime = self._runtime_reliability_payload(timeout=2.0)
        return dict(runtime.get("sidecar_runtime")) if isinstance(runtime.get("sidecar_runtime"), dict) else {}

    def _hub_root_watchdog_state_payload(self) -> dict[str, Any]:
        log_path = _supervisor_hub_root_watchdog_log_path()
        return {
            "enabled": _hub_root_watchdog_enabled(),
            "last_state": self._hub_root_watchdog_last_state,
            "last_reason": self._hub_root_watchdog_last_reason,
            "last_reconnect_at": self._hub_root_watchdog_last_reconnect_at,
            "reconnect_total": int(self._hub_root_watchdog_reconnect_total),
            "cooldown_sec": _hub_root_watchdog_cooldown_sec(),
            "reset_degraded_route": _hub_root_watchdog_reset_degraded_route_enabled(),
            "verify_timeout_sec": _hub_root_watchdog_verify_timeout_sec(),
            "log_path": str(log_path),
            "recent_events": _read_jsonl_tail(log_path, limit=10),
            "last_result": dict(self._hub_root_watchdog_last_result or {}),
        }

    def _member_hub_watchdog_state_payload(self) -> dict[str, Any]:
        log_path = _supervisor_member_hub_watchdog_log_path()
        return {
            "enabled": _member_hub_watchdog_enabled(),
            "last_state": self._member_hub_watchdog_last_state,
            "last_reason": self._member_hub_watchdog_last_reason,
            "last_reconnect_at": self._member_hub_watchdog_last_reconnect_at,
            "reconnect_total": int(self._member_hub_watchdog_reconnect_total),
            "cooldown_sec": _member_hub_watchdog_cooldown_sec(),
            "verify_timeout_sec": _member_hub_watchdog_verify_timeout_sec(),
            "log_path": str(log_path),
            "recent_events": _read_jsonl_tail(log_path, limit=10),
            "last_result": dict(self._member_hub_watchdog_last_result or {}),
        }

    @staticmethod
    def _required_upstream_link_kind_for_role(role: str | None) -> str:
        role_norm = str(role or "").strip().lower()
        return "member_hub" if role_norm == "member" else "hub_root"

    def _required_upstream_link_state_payload(self, *, role: str | None = None) -> dict[str, Any]:
        role_norm = str(role or self._managed_transition_role or self._sidecar_role() or "").strip().lower() or None
        kind = self._required_upstream_link_kind_for_role(role_norm)
        payload = (
            self._member_hub_watchdog_state_payload()
            if kind == "member_hub"
            else self._hub_root_watchdog_state_payload()
        )
        sidecar_enabled = bool(realtime_sidecar_enabled(role=role_norm))
        state = str(payload.get("last_state") or "").strip().lower() or "unknown"
        paused_states = {"waiting_restart", "restarting", "paused_for_update", "cooldown"}
        ready = state in {"ready", "not_applicable"} or state in paused_states
        desired_state = "connected" if self._desired_running and not self._stopping else "paused"
        current_owner = "runtime"
        planned_owner = "runtime"
        future_owner = None
        continuity_mode = "runtime_bound"
        if kind == "hub_root":
            current_owner = "sidecar" if sidecar_enabled else "runtime"
            planned_owner = current_owner
            continuity_mode = "slot_sticky" if current_owner == "sidecar" else "runtime_bound"
        elif kind == "member_hub":
            current_owner = "runtime"
            planned_owner = "runtime"
            future_owner = "sidecar"
            continuity_mode = "runtime_bound"
        return {
            "kind": kind,
            "role": role_norm,
            "owner": "supervisor",
            "state": state,
            "reason": str(payload.get("last_reason") or "").strip() or None,
            "ready": ready,
            "visible": True,
            "desired_state": desired_state,
            "current_owner": current_owner,
            "planned_owner": planned_owner,
            "future_owner": future_owner,
            "continuity_mode": continuity_mode,
            "sidecar_enabled": sidecar_enabled,
            "reconnect_total": int(payload.get("reconnect_total") or 0),
            "cooldown_sec": float(payload.get("cooldown_sec") or 0.0),
            "verify_timeout_sec": float(payload.get("verify_timeout_sec") or 0.0),
            "served_by": "supervisor",
            "watchdog": dict(payload),
            "blockers": [],
        }

    def _required_upstream_link_snapshot(
        self,
        *,
        runtime: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        payload = (
            runtime.get("required_upstream_link")
            if isinstance(runtime, dict) and isinstance(runtime.get("required_upstream_link"), dict)
            else {}
        )
        if payload:
            return dict(payload)
        return self._required_upstream_link_state_payload(role=role)

    @staticmethod
    def _hub_root_channel_state(runtime: dict[str, Any]) -> dict[str, Any]:
        runtime = runtime if isinstance(runtime, dict) else {}
        readiness_tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
        root_control = readiness_tree.get("root_control") if isinstance(readiness_tree.get("root_control"), dict) else {}
        route = readiness_tree.get("route") if isinstance(readiness_tree.get("route"), dict) else {}
        channel_overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
        hub_root = channel_overview.get("hub_root") if isinstance(channel_overview.get("hub_root"), dict) else {}
        hub_root_browser = (
            channel_overview.get("hub_root_browser")
            if isinstance(channel_overview.get("hub_root_browser"), dict)
            else {}
        )
        strategy = (
            runtime.get("hub_root_transport_strategy")
            if isinstance(runtime.get("hub_root_transport_strategy"), dict)
            else {}
        )
        return {
            "root_control_status": str(root_control.get("status") or "").strip().lower() or None,
            "route_status": str(route.get("status") or "").strip().lower() or None,
            "hub_root_status": str(hub_root.get("effective_status") or "").strip().lower() or None,
            "hub_root_state": str(hub_root.get("effective_state") or "").strip().lower() or None,
            "hub_root_browser_status": str(hub_root_browser.get("effective_status") or "").strip().lower() or None,
            "hub_root_browser_state": str(hub_root_browser.get("effective_state") or "").strip().lower() or None,
            "last_event": str(strategy.get("last_event") or "").strip() or None,
            "last_summary": str(strategy.get("last_summary") or "").strip() or None,
            "selected_server": str(strategy.get("selected_server") or "").strip() or None,
            "effective_transport": str(strategy.get("effective_transport") or "").strip() or None,
        }

    @staticmethod
    def _member_hub_channel_state(runtime: dict[str, Any]) -> dict[str, Any]:
        runtime = runtime if isinstance(runtime, dict) else {}
        readiness_tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
        route = readiness_tree.get("route") if isinstance(readiness_tree.get("route"), dict) else {}
        hub_member = readiness_tree.get("hub_member") if isinstance(readiness_tree.get("hub_member"), dict) else {}
        member_state = (
            runtime.get("hub_member_connection_state")
            if isinstance(runtime.get("hub_member_connection_state"), dict)
            else {}
        )
        hub = member_state.get("hub") if isinstance(member_state.get("hub"), dict) else {}
        return {
            "route_status": str(route.get("status") or "").strip().lower() or None,
            "hub_member_status": str(hub_member.get("status") or "").strip().lower() or None,
            "member_state": str(member_state.get("state") or "").strip().lower() or None,
            "assessment_state": (
                str((member_state.get("assessment") or {}).get("state") or "").strip().lower()
                if isinstance(member_state.get("assessment"), dict)
                else None
            ),
            "assessment_reason": (
                str((member_state.get("assessment") or {}).get("reason") or "").strip() or None
                if isinstance(member_state.get("assessment"), dict)
                else None
            ),
            "connected": bool(hub.get("connected")),
            "transition_state": str(hub.get("transition_state") or "").strip().lower() or None,
            "transition_reason": str(hub.get("transition_reason") or "").strip() or None,
            "hub_url": str(hub.get("hub_url") or "").strip() or None,
            "last_error": str(hub.get("last_error") or "").strip() or None,
            "last_close_reason": str(hub.get("last_close_reason") or "").strip() or None,
        }

    @staticmethod
    def _hub_root_channel_ready(state: dict[str, Any]) -> bool:
        return (
            str(state.get("root_control_status") or "").strip().lower() == "ready"
            and str(state.get("hub_root_status") or "").strip().lower() == "ready"
            and str(state.get("route_status") or "").strip().lower() == "ready"
            and str(state.get("hub_root_browser_status") or "").strip().lower() == "ready"
        )

    @staticmethod
    def _hub_root_channel_down(state: dict[str, Any]) -> bool:
        return any(
            str(state.get(key) or "").strip().lower() == "down"
            for key in ("root_control_status", "hub_root_status", "hub_root_state")
        )

    @staticmethod
    def _hub_root_route_degraded(state: dict[str, Any]) -> bool:
        degraded_states = {"down", "degraded", "unstable", "flapping"}
        return any(
            str(state.get(key) or "").strip().lower() in degraded_states
            for key in ("route_status", "hub_root_browser_status", "hub_root_browser_state")
        )

    def _append_hub_root_watchdog_event(self, payload: dict[str, Any]) -> None:
        event = {
            "ts": time.time(),
            "runtime_url": self.runtime_base_url,
            **payload,
        }
        try:
            _append_jsonl(_supervisor_hub_root_watchdog_log_path(), event)
        except Exception:
            _LOG.debug("failed to append hub-root watchdog event", exc_info=True)

    def _append_member_hub_watchdog_event(self, payload: dict[str, Any]) -> None:
        event = {
            "ts": time.time(),
            "runtime_url": self.runtime_base_url,
            **payload,
        }
        try:
            _append_jsonl(_supervisor_member_hub_watchdog_log_path(), event)
        except Exception:
            _LOG.debug("failed to append member-hub watchdog event", exc_info=True)

    def _hub_root_watchdog_decision(
        self,
        runtime: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not _hub_root_watchdog_enabled():
            self._hub_root_watchdog_last_state = "disabled"
            self._hub_root_watchdog_last_reason = "watchdog disabled"
            return None
        if self._stopping or not self._desired_running:
            return None
        current_time = time.time() if now is None else float(now)
        node = runtime.get("node") if isinstance(runtime.get("node"), dict) else {}
        role = str(node.get("role") or self._sidecar_role() or "").strip().lower()
        if role != "hub":
            self._hub_root_watchdog_last_state = "not_applicable"
            self._hub_root_watchdog_last_reason = f"role={role or '-'}"
            return None

        channel_state = self._hub_root_channel_state(runtime)
        root_status = str(channel_state.get("root_control_status") or "")
        route_status = str(channel_state.get("route_status") or "")
        hub_root_status = str(channel_state.get("hub_root_status") or "")
        hub_root_state = str(channel_state.get("hub_root_state") or "")
        hub_root_browser_status = str(channel_state.get("hub_root_browser_status") or "")
        hub_root_browser_state = str(channel_state.get("hub_root_browser_state") or "")
        required_link = self._required_upstream_link_snapshot(runtime=runtime, role=role)
        sidecar_enabled = bool(required_link.get("sidecar_enabled"))
        transport_owner = str(required_link.get("current_owner") or "").strip().lower() or ("sidecar" if sidecar_enabled else "runtime")
        root_down = self._hub_root_channel_down(channel_state)
        route_degraded = self._hub_root_route_degraded(channel_state)
        route_degraded_reset_enabled = _hub_root_watchdog_reset_degraded_route_enabled()
        action = (
            "sidecar_restart"
            if transport_owner == "sidecar"
            else ("runtime_reconnect" if root_down else "runtime_route_reset")
        )

        if not root_down and not route_degraded:
            state = (
                root_status
                or hub_root_status
                or hub_root_state
                or route_status
                or hub_root_browser_status
                or hub_root_browser_state
                or "unknown"
            )
            self._hub_root_watchdog_last_state = state
            self._hub_root_watchdog_last_reason = "hub-root and browser route are not down"
            return None

        if route_degraded and not root_down and not route_degraded_reset_enabled:
            state = hub_root_browser_status or hub_root_browser_state or route_status or "route_degraded"
            self._hub_root_watchdog_last_state = state
            self._hub_root_watchdog_last_reason = "browser route degraded; preserving active runtime-owned tunnels"
            return None

        cooldown = _hub_root_watchdog_cooldown_sec()
        last_reconnect = self._hub_root_watchdog_last_reconnect_at
        if last_reconnect is not None and (current_time - float(last_reconnect)) < cooldown:
            self._hub_root_watchdog_last_state = "cooldown"
            self._hub_root_watchdog_last_reason = f"hub-root down but reconnect cooldown is active ({cooldown:.0f}s)"
            return None

        reason = (
            f"root_control={root_status or '-'} "
            f"hub_root={hub_root_status or hub_root_state or '-'} "
            f"route={route_status or '-'} "
            f"hub_root_browser={hub_root_browser_status or hub_root_browser_state or '-'}"
        )
        return {
            "reason": "supervisor.hub_root.watchdog_reconnect",
            "message": f"hub-root route watchdog requesting {action} ({reason})",
            "action": action,
            "transport_owner": transport_owner,
            "root_control_status": root_status or None,
            "route_status": route_status or None,
            "hub_root_status": hub_root_status or None,
            "hub_root_state": hub_root_state or None,
            "hub_root_browser_status": hub_root_browser_status or None,
            "hub_root_browser_state": hub_root_browser_state or None,
            "last_event": channel_state.get("last_event"),
            "last_summary": channel_state.get("last_summary"),
            "channel_before": channel_state,
            "required_upstream_link": required_link,
        }

    async def _verify_hub_root_watchdog_recovery(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        timeout = _hub_root_watchdog_verify_timeout_sec() if timeout_sec is None else max(0.0, float(timeout_sec))
        interval = _hub_root_watchdog_verify_interval_sec()
        deadline = time.time() + timeout
        attempts = 0
        last_state: dict[str, Any] = {}
        while True:
            attempts += 1
            runtime = self._runtime_reliability_payload(timeout=1.5)
            last_state = self._hub_root_channel_state(runtime)
            if self._hub_root_channel_ready(last_state):
                return {
                    "ok": True,
                    "state": "ready",
                    "attempts": attempts,
                    "timeout_sec": timeout,
                    "channel": last_state,
                }
            if timeout <= 0.0 or time.time() >= deadline:
                return {
                    "ok": False,
                    "state": "not_ready",
                    "attempts": attempts,
                    "timeout_sec": timeout,
                    "channel": last_state,
                }
            await asyncio.sleep(interval)

    async def _maybe_reconnect_hub_root_from_watchdog(self) -> None:
        if not _hub_root_watchdog_enabled() or self._stopping or not self._desired_running:
            return
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        runtime = self._runtime_reliability_payload(timeout=1.5)
        if not runtime:
            return
        decision = self._hub_root_watchdog_decision(runtime, now=time.time())
        if decision is None:
            return
        self._hub_root_watchdog_last_state = "reconnect_requested"
        self._hub_root_watchdog_last_reason = str(decision.get("message") or decision.get("reason") or "")
        self._hub_root_watchdog_last_reconnect_at = time.time()
        self._hub_root_watchdog_reconnect_total += 1
        action = str(decision.get("action") or "runtime_reconnect")
        try:
            if action == "sidecar_restart":
                result = await self.restart_sidecar(reconnect_hub_root=True)
            elif action == "runtime_route_reset":
                result = self._runtime_request_json(
                    path="/api/node/hub-root/route-reset",
                    method="POST",
                    payload={
                        "reason": "supervisor_route_watchdog",
                        "notify_browser": True,
                    },
                    timeout=5.0,
                )
            else:
                result = self._runtime_request_json(
                    path="/api/node/hub-root/reconnect",
                    method="POST",
                    payload={},
                    timeout=5.0,
                )
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _LOG.warning("hub-root watchdog reconnect request failed: %s: %s", type(exc).__name__, exc)
        verification = await self._verify_hub_root_watchdog_recovery()
        self._hub_root_watchdog_last_result = {
            "requested_at": self._hub_root_watchdog_last_reconnect_at,
            "action": action,
            "decision": decision,
            "result": result,
            "verification": verification,
        }
        self._hub_root_watchdog_last_state = "ready" if bool(verification.get("ok")) else "recovery_failed"
        self._hub_root_watchdog_last_reason = (
            "hub-root channel recovered"
            if bool(verification.get("ok"))
            else "hub-root channel did not recover after watchdog action"
        )
        self._append_hub_root_watchdog_event(
            {
                "event": "recovery_attempt",
                "action": action,
                "transport_owner": decision.get("transport_owner"),
                "decision": decision,
                "result": result,
                "verification": verification,
            }
        )
        self._persist_runtime_state()

    def _member_hub_watchdog_decision(
        self,
        runtime: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not _member_hub_watchdog_enabled():
            self._member_hub_watchdog_last_state = "disabled"
            self._member_hub_watchdog_last_reason = "watchdog disabled"
            return None
        if self._stopping or not self._desired_running:
            return None
        current_time = time.time() if now is None else float(now)
        node = runtime.get("node") if isinstance(runtime.get("node"), dict) else {}
        role = str(node.get("role") or self._sidecar_role() or "").strip().lower()
        if role != "member":
            self._member_hub_watchdog_last_state = "not_applicable"
            self._member_hub_watchdog_last_reason = f"role={role or '-'}"
            return None

        channel_state = self._member_hub_channel_state(runtime)
        required_link = self._required_upstream_link_snapshot(runtime=runtime, role=role)
        transition_state = str(channel_state.get("transition_state") or "").strip().lower()
        if transition_state in {"waiting_restart", "restarting", "paused_for_update"}:
            self._member_hub_watchdog_last_state = transition_state
            self._member_hub_watchdog_last_reason = (
                str(channel_state.get("transition_reason") or "").strip() or transition_state
            )
            return None

        if bool(channel_state.get("connected")):
            self._member_hub_watchdog_last_state = "ready"
            self._member_hub_watchdog_last_reason = "member-hub link is connected"
            return None

        cooldown = _member_hub_watchdog_cooldown_sec()
        last_reconnect = self._member_hub_watchdog_last_reconnect_at
        if last_reconnect is not None and (current_time - float(last_reconnect)) < cooldown:
            self._member_hub_watchdog_last_state = "cooldown"
            self._member_hub_watchdog_last_reason = (
                f"member-hub down but reconnect cooldown is active ({cooldown:.0f}s)"
            )
            return None

        route_status = str(channel_state.get("route_status") or "").strip().lower() or "-"
        member_state = str(channel_state.get("member_state") or "").strip().lower() or "-"
        transport_owner = str(required_link.get("current_owner") or "").strip().lower() or "runtime"
        continuity_mode = str(required_link.get("continuity_mode") or "").strip().lower() or "runtime_bound"
        handoff_state = str(required_link.get("handoff_state") or "").strip().lower() or "unknown"
        handoff_ready = bool(required_link.get("handoff_ready"))
        recovery_policy = (
            dict(required_link.get("recovery_policy"))
            if isinstance(required_link.get("recovery_policy"), dict)
            else {}
        )
        reason = (
            f"route={route_status} "
            f"member_state={member_state} "
            f"assessment={str(channel_state.get('assessment_state') or '-')} "
            f"hub_url={str(channel_state.get('hub_url') or '-')} "
            f"owner={transport_owner} "
            f"handoff={handoff_state} "
            f"continuity={continuity_mode}"
        )
        return {
            "reason": "supervisor.member_hub.watchdog_reconnect",
            "message": f"member-hub watchdog requesting runtime_reconnect ({reason})",
            "action": "runtime_reconnect",
            "transport_owner": transport_owner,
            "continuity_mode": continuity_mode,
            "handoff_state": handoff_state,
            "handoff_ready": handoff_ready,
            "recovery_policy": recovery_policy,
            "route_status": channel_state.get("route_status"),
            "hub_member_status": channel_state.get("hub_member_status"),
            "member_state": channel_state.get("member_state"),
            "assessment_state": channel_state.get("assessment_state"),
            "assessment_reason": channel_state.get("assessment_reason"),
            "transition_state": channel_state.get("transition_state"),
            "transition_reason": channel_state.get("transition_reason"),
            "last_error": channel_state.get("last_error"),
            "last_close_reason": channel_state.get("last_close_reason"),
            "channel_before": channel_state,
            "required_upstream_link": required_link,
        }

    async def _verify_member_hub_watchdog_recovery(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        timeout = _member_hub_watchdog_verify_timeout_sec() if timeout_sec is None else max(0.0, float(timeout_sec))
        interval = _member_hub_watchdog_verify_interval_sec()
        deadline = time.time() + timeout
        attempts = 0
        last_state: dict[str, Any] = {}
        while True:
            attempts += 1
            runtime = self._runtime_reliability_payload(timeout=1.5)
            last_state = self._member_hub_channel_state(runtime)
            if bool(last_state.get("connected")):
                return {
                    "ok": True,
                    "state": "ready",
                    "attempts": attempts,
                    "timeout_sec": timeout,
                    "channel": last_state,
                }
            if timeout <= 0.0 or time.time() >= deadline:
                return {
                    "ok": False,
                    "state": "not_ready",
                    "attempts": attempts,
                    "timeout_sec": timeout,
                    "channel": last_state,
                }
            await asyncio.sleep(interval)

    async def _maybe_reconnect_member_hub_from_watchdog(self) -> None:
        if not _member_hub_watchdog_enabled() or self._stopping or not self._desired_running:
            return
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        runtime = self._runtime_reliability_payload(timeout=1.5)
        if not runtime:
            return
        decision = self._member_hub_watchdog_decision(runtime, now=time.time())
        if decision is None:
            return
        self._member_hub_watchdog_last_state = "reconnect_requested"
        self._member_hub_watchdog_last_reason = str(decision.get("message") or decision.get("reason") or "")
        self._member_hub_watchdog_last_reconnect_at = time.time()
        self._member_hub_watchdog_reconnect_total += 1
        try:
            result = self._runtime_request_json(
                path="/api/node/member-hub/reconnect",
                method="POST",
                payload={},
                timeout=5.0,
            )
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _LOG.warning("member-hub watchdog reconnect request failed: %s: %s", type(exc).__name__, exc)
        verification = await self._verify_member_hub_watchdog_recovery()
        self._member_hub_watchdog_last_result = {
            "requested_at": self._member_hub_watchdog_last_reconnect_at,
            "action": "runtime_reconnect",
            "decision": decision,
            "result": result,
            "verification": verification,
        }
        self._member_hub_watchdog_last_state = "ready" if bool(verification.get("ok")) else "recovery_failed"
        self._member_hub_watchdog_last_reason = (
            "member-hub channel recovered"
            if bool(verification.get("ok"))
            else "member-hub channel did not recover after watchdog action"
        )
        self._append_member_hub_watchdog_event(
            {
                "event": "recovery_attempt",
                "action": "runtime_reconnect",
                "transport_owner": decision.get("transport_owner"),
                "decision": decision,
                "result": result,
                "verification": verification,
            }
        )
        self._persist_runtime_state()

    def _required_upstream_link_decision(
        self,
        runtime: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        node = runtime.get("node") if isinstance(runtime.get("node"), dict) else {}
        role = str(node.get("role") or self._managed_transition_role or self._sidecar_role() or "").strip().lower()
        if role == "member":
            return self._member_hub_watchdog_decision(runtime, now=now)
        return self._hub_root_watchdog_decision(runtime, now=now)

    async def _maybe_maintain_required_upstream_link(self) -> None:
        role = str(self._managed_transition_role or self._sidecar_role() or "").strip().lower()
        if role == "member":
            await self._maybe_reconnect_member_hub_from_watchdog()
        else:
            await self._maybe_reconnect_hub_root_from_watchdog()

    @property
    def active_runtime_port(self) -> int:
        return self.slot_runtime_port(active_slot())

    @property
    def runtime_base_url(self) -> str:
        return self.slot_runtime_base_url(active_slot())

    def slot_runtime_port(self, slot: str | None) -> int:
        return _slot_runtime_port(slot, self.runtime_port)

    def slot_runtime_base_url(self, slot: str | None) -> str:
        return f"http://{self.runtime_host}:{self.slot_runtime_port(slot)}"

    def slot_runtime_urls(self) -> dict[str, str]:
        ports = _slot_runtime_ports(self.runtime_port)
        return {slot_name: f"http://{self.runtime_host}:{port}" for slot_name, port in ports.items()}

    def _candidate_transition_slot(
        self,
        *,
        current_slot: str | None,
        update_status: dict[str, Any] | None,
        update_attempt: dict[str, Any] | None,
    ) -> str | None:
        state = str((update_status or {}).get("state") or "").strip().lower()
        phase = str((update_status or {}).get("phase") or "").strip().lower()
        attempt_state = str((update_attempt or {}).get("state") or "").strip().lower()
        current_slot_name = str(current_slot or "").strip().upper()
        transition_active = state in {
            "planned",
            "preparing",
            "countdown",
            "draining",
            "stopping",
            "restarting",
            "applying",
            "validated",
        } or attempt_state in {"planned", "active"}
        if not transition_active and attempt_state == "awaiting_root_restart":
            transition_active = _subsequent_transition_request(update_attempt) is not None
        if not transition_active and state == "succeeded" and phase == "root_promoted":
            transition_active = False
        if not transition_active:
            return None
        for source in (update_status or {}, update_attempt or {}):
            target_slot = str(source.get("target_slot") or "").strip().upper()
            if target_slot in {"A", "B"} and target_slot != current_slot_name:
                return target_slot
        if transition_active:
            target_slot = choose_inactive_slot()
            if target_slot and target_slot != current_slot_name:
                return target_slot
        return None

    def _warm_switch_state(
        self,
        *,
        current_slot: str | None,
        update_status: dict[str, Any] | None,
        update_attempt: dict[str, Any] | None,
        managed_pid: int | None,
    ) -> dict[str, Any]:
        candidate_slot = self._candidate_transition_slot(
            current_slot=current_slot,
            update_status=update_status,
            update_attempt=update_attempt,
        )
        slot_ports = _slot_runtime_ports(self.runtime_port)
        active_port = self.slot_runtime_port(current_slot)
        candidate_port = self.slot_runtime_port(candidate_slot)
        supported = bool(candidate_slot) and candidate_port != active_port
        enabled = _warm_switch_enabled()
        allowed = False
        reason = "warm switch is disabled"
        available_bytes = None
        estimated_candidate_bytes = None
        reserve_bytes = _warm_switch_min_available_bytes()
        current_rss_bytes = None
        current_family_rss_bytes = None
        if not candidate_slot:
            reason = "no transition candidate slot"
        elif not supported:
            reason = "candidate runtime uses the same port as the active slot"
        elif not enabled:
            reason = "warm switch is disabled"
        elif psutil is None:
            reason = "psutil unavailable; cannot evaluate memory gate"
        else:
            try:
                vm = psutil.virtual_memory()
                available_bytes = int(getattr(vm, "available", 0) or 0)
            except Exception:
                available_bytes = None
            if managed_pid:
                current_rss_bytes, current_family_rss_bytes = _process_family_rss_bytes(managed_pid)
            estimated_candidate_bytes = max(
                _warm_switch_min_candidate_bytes(),
                int(float(current_family_rss_bytes or current_rss_bytes or 0) * _warm_switch_rss_multiplier()),
            )
            if available_bytes is None or available_bytes <= 0:
                reason = "available memory is unknown"
            elif available_bytes < estimated_candidate_bytes + reserve_bytes:
                reason = "insufficient memory for warm switch; using stop-and-switch"
            else:
                allowed = True
                reason = "warm switch admitted"
        transition_mode = None
        if candidate_slot:
            transition_mode = "warm_switch" if supported and enabled and allowed else "stop_and_switch"
        return {
            "candidate_slot": candidate_slot,
            "candidate_runtime_port": candidate_port if candidate_slot else None,
            "candidate_runtime_url": self.slot_runtime_base_url(candidate_slot) if candidate_slot else None,
            "candidate_transition_role": "candidate" if candidate_slot else None,
            "transition_mode": transition_mode,
            "warm_switch_enabled": enabled,
            "warm_switch_supported": supported,
            "warm_switch_allowed": allowed if candidate_slot else None,
            "warm_switch_reason": reason if candidate_slot else None,
            "warm_switch_memory": {
                "available_bytes": available_bytes,
                "current_rss_bytes": current_family_rss_bytes or current_rss_bytes,
                "current_process_rss_bytes": current_rss_bytes,
                "current_family_rss_bytes": current_family_rss_bytes,
                "estimated_candidate_bytes": estimated_candidate_bytes,
                "reserve_bytes": reserve_bytes,
            },
            "slot_ports": slot_ports,
            "slot_urls": self.slot_runtime_urls(),
        }

    def _runtime_env(
        self,
        *,
        slot: str | None,
        slot_dir: str,
        slot_port: int,
        transition_role: str,
        runtime_instance_id: str,
        profile_mode: str = "normal",
        profile_session_id: str | None = None,
        profile_trigger: str | None = None,
        skip_pending_update: bool = False,
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["ADAOS_SUPERVISOR_ENABLED"] = "1"
        env["ADAOS_SUPERVISOR_URL"] = _supervisor_base_url()
        env["ADAOS_SUPERVISOR_HOST"] = _supervisor_host()
        env["ADAOS_SUPERVISOR_PORT"] = str(_supervisor_port())
        env["ADAOS_RUNTIME_INSTANCE_ID"] = str(runtime_instance_id)
        env["ADAOS_RUNTIME_TRANSITION_ROLE"] = str(transition_role or "active")
        env["ADAOS_RUNTIME_HOST"] = self.runtime_host
        env["ADAOS_RUNTIME_PORT"] = str(slot_port)
        if self.token:
            env["ADAOS_TOKEN"] = self.token
        if slot:
            env["ADAOS_ACTIVE_CORE_SLOT"] = slot
            env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = slot_dir
        env["ADAOS_SUPERVISOR_PROFILE_MODE"] = str(profile_mode or "normal")
        if profile_session_id:
            env["ADAOS_SUPERVISOR_PROFILE_SESSION_ID"] = str(profile_session_id)
        else:
            env.pop("ADAOS_SUPERVISOR_PROFILE_SESSION_ID", None)
        if profile_trigger:
            env["ADAOS_SUPERVISOR_PROFILE_TRIGGER"] = str(profile_trigger)
        else:
            env.pop("ADAOS_SUPERVISOR_PROFILE_TRIGGER", None)
        if skip_pending_update:
            env[_SKIP_PENDING_UPDATE_ENV] = "1"
        return env

    def _runtime_launch_spec(
        self,
        *,
        slot: str | None = None,
        transition_role: str = "active",
        runtime_instance_id: str | None = None,
        profile_mode: str | None = None,
        profile_session_id: str | None = None,
        profile_trigger: str | None = None,
        skip_pending_update: bool = False,
    ) -> tuple[list[str] | None, str | None, dict[str, str], str | None, str, str]:
        resolved_slot = str(slot or active_slot() or "").strip().upper() or None
        manifest = read_slot_manifest(resolved_slot) if slot else active_slot_manifest()
        slot_port = self.slot_runtime_port(resolved_slot)
        slot_dir = str(core_slot_status().get("slots", {}).get(resolved_slot or "", {}).get("path") or "")
        resolved_runtime_instance_id = str(
            runtime_instance_id or _new_runtime_instance_id(slot=resolved_slot, transition_role=transition_role)
        )
        requested_session_id = profile_session_id
        requested_mode = str(profile_mode or "").strip().lower() or ""
        resolved_profile_trigger = str(profile_trigger or "").strip() or None
        if not requested_mode:
            requested_session_id = str(self._memory_active_session_id or "").strip() or None
            requested_mode = self._desired_memory_profile_mode()
        if requested_session_id and not resolved_profile_trigger:
            session = read_memory_session_summary(requested_session_id) or {}
            trigger_source = str(session.get("trigger_source") or "").strip() or "operator"
            trigger_reason = str(session.get("trigger_reason") or "").strip() or "supervisor.memory.request"
            resolved_profile_trigger = f"{trigger_source}:{trigger_reason}"
        env = self._runtime_env(
            slot=resolved_slot,
            slot_dir=slot_dir,
            slot_port=slot_port,
            transition_role=transition_role,
            runtime_instance_id=resolved_runtime_instance_id,
            profile_mode=requested_mode,
            profile_session_id=requested_session_id,
            profile_trigger=resolved_profile_trigger,
            skip_pending_update=skip_pending_update,
        )
        if isinstance(manifest, dict):
            manifest_env = manifest.get("env")
            if isinstance(manifest_env, dict):
                for key, value in manifest_env.items():
                    env[str(key)] = str(value)
            if resolved_slot:
                env["ADAOS_ACTIVE_CORE_SLOT"] = resolved_slot
                env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = slot_dir
            values = {
                "host": self.runtime_host,
                "port": str(slot_port),
                "token": str(self.token or ""),
                "slot": str(resolved_slot or ""),
                "slot_dir": slot_dir,
                "base_dir": str(current_base_dir()),
                "python": os.sys.executable,
                "runtime_instance_id": resolved_runtime_instance_id,
                "transition_role": str(transition_role or "active"),
            }
            argv_raw = manifest.get("argv")
            if isinstance(argv_raw, list):
                argv = [_format_slot_value(str(item), values) for item in argv_raw if str(item).strip()]
                if argv:
                    cwd = str(manifest.get("cwd") or "").strip() or None
                    return argv, None, env, cwd, resolved_runtime_instance_id, str(transition_role or "active")
            command = str(manifest.get("command") or "").strip()
            if command:
                cwd = str(manifest.get("cwd") or "").strip() or None
                return None, _format_slot_value(command, values), env, cwd, resolved_runtime_instance_id, str(
                    transition_role or "active"
                )
        return (
            [
                sys.executable,
                "-m",
                "adaos.apps.autostart_runner",
                "--host",
                self.runtime_host,
                "--port",
                str(slot_port),
            ],
            None,
            env,
            None,
            resolved_runtime_instance_id,
            str(transition_role or "active"),
        )

    def _runtime_state_payload(self) -> dict[str, Any]:
        proc = self._proc
        slot_snapshot = core_slot_status()
        current_slot = str(slot_snapshot.get("active_slot") or active_slot() or "").strip().upper() or None
        previous_slot = str(slot_snapshot.get("previous_slot") or "").strip().upper() or None
        active_manifest = active_slot_manifest()
        update_status = read_core_update_status()
        update_attempt = _read_update_attempt()
        root_promotion_required, bootstrap_update = resolved_root_promotion_requirement(active_manifest)
        slot_structure = validate_slot_structure(current_slot) if current_slot else None
        active_runtime_port = self.slot_runtime_port(current_slot)
        active_runtime_url = self.slot_runtime_base_url(current_slot)
        managed = _proc_details(proc, cwd_hint=self._managed_runtime_cwd)
        managed_pid = managed["managed_pid"]
        managed_alive = bool(managed["managed_alive"])
        managed_cmdline = managed["managed_cmdline"]
        managed_executable = managed["managed_executable"]
        managed_cwd = managed["managed_cwd"]
        listener_running = bool(managed_alive) and _listener_running(self.runtime_host, active_runtime_port)
        api_ready = listener_running and _runtime_api_ready(active_runtime_url, token=self.token)
        runtime_state = "stopped"
        if self._stopping:
            runtime_state = "stopping"
        elif managed_alive and api_ready:
            runtime_state = "ready"
        elif managed_alive and listener_running:
            runtime_state = "starting"
        elif managed_alive:
            runtime_state = "spawned"
        expected_executable, expected_cwd, managed_matches_active_slot = self._managed_runtime_slot_expectations(
            manifest=active_manifest,
            managed_executable=managed_executable,
            managed_cwd=managed_cwd,
        )
        warm_switch = self._warm_switch_state(
            current_slot=current_slot,
            update_status=update_status,
            update_attempt=update_attempt,
            managed_pid=managed_pid,
        )
        candidate_slot = str(self._candidate_slot or warm_switch.get("candidate_slot") or "").strip().upper() or None
        candidate_manifest = read_slot_manifest(candidate_slot) if candidate_slot else None
        candidate_runtime_port = self.slot_runtime_port(candidate_slot) if candidate_slot else None
        candidate_runtime_url = self.slot_runtime_base_url(candidate_slot) if candidate_slot else None
        candidate_managed = _proc_details(self._candidate_proc, cwd_hint=self._candidate_runtime_cwd)
        candidate_managed_pid = candidate_managed["managed_pid"]
        candidate_managed_alive = bool(candidate_managed["managed_alive"])
        candidate_managed_cmdline = candidate_managed["managed_cmdline"]
        candidate_managed_executable = candidate_managed["managed_executable"]
        candidate_managed_cwd = candidate_managed["managed_cwd"]
        candidate_listener_running = bool(candidate_managed_alive and candidate_runtime_port) and _listener_running(
            self.runtime_host,
            int(candidate_runtime_port or 0),
        )
        candidate_runtime_api_ready = bool(candidate_listener_running and candidate_runtime_url) and _runtime_api_ready(
            str(candidate_runtime_url),
            token=self.token,
        )
        candidate_runtime_state = None
        if candidate_slot:
            candidate_runtime_state = "stopped"
            if candidate_managed_alive and candidate_runtime_api_ready:
                candidate_runtime_state = "ready"
            elif candidate_managed_alive and candidate_listener_running:
                candidate_runtime_state = "starting"
            elif candidate_managed_alive:
                candidate_runtime_state = "spawned"
        candidate_expected_executable = None
        candidate_expected_cwd = None
        candidate_matches_candidate_slot = None
        if isinstance(candidate_manifest, dict):
            argv = candidate_manifest.get("argv")
            if isinstance(argv, list) and argv:
                candidate_expected_executable = str(argv[0] or "").strip() or None
            candidate_expected_cwd = str(candidate_manifest.get("cwd") or "").strip() or None
        if candidate_slot and (candidate_expected_executable or candidate_expected_cwd):
            candidate_matches_candidate_slot = True
            if (
                candidate_expected_executable
                and str(candidate_managed_executable or "").strip() != candidate_expected_executable
            ):
                candidate_matches_candidate_slot = False
            if candidate_expected_cwd and str(candidate_managed_cwd or "").strip() != candidate_expected_cwd:
                candidate_matches_candidate_slot = False
        return {
            "ok": True,
            "supervisor_pid": os.getpid(),
            "supervisor_url": _supervisor_base_url(),
            "sidecar": self._sidecar_status_payload(),
            "runtime_url": active_runtime_url,
            "runtime_host": self.runtime_host,
            "runtime_port": active_runtime_port,
            "runtime_instance_id": self._managed_runtime_instance_id,
            "transition_role": self._managed_transition_role if self._managed_runtime_instance_id else None,
            "active_slot": current_slot,
            "previous_slot": previous_slot,
            "desired_running": bool(self._desired_running),
            "stopping": bool(self._stopping),
            "managed_pid": managed_pid,
            "managed_alive": managed_alive,
            "listener_running": listener_running,
            "runtime_api_ready": api_ready,
            "runtime_state": runtime_state,
            "managed_cmdline": managed_cmdline,
            "managed_executable": managed_executable,
            "managed_cwd": managed_cwd,
            "managed_start_reason": self._managed_start_reason,
            "hub_root_watchdog": self._hub_root_watchdog_state_payload(),
            "member_hub_watchdog": self._member_hub_watchdog_state_payload(),
            "required_upstream_link": self._required_upstream_link_state_payload(),
            "expected_managed_executable": expected_executable,
            "expected_managed_cwd": expected_cwd,
            "managed_matches_active_slot": managed_matches_active_slot,
            **warm_switch,
            "candidate_slot": candidate_slot,
            "candidate_runtime_url": candidate_runtime_url,
            "candidate_runtime_port": candidate_runtime_port,
            "candidate_runtime_instance_id": self._candidate_runtime_instance_id,
            "candidate_transition_role": (
                self._candidate_transition_role
                if self._candidate_runtime_instance_id
                else str(warm_switch.get("candidate_transition_role") or "").strip() or None
            ),
            "candidate_managed_pid": candidate_managed_pid,
            "candidate_managed_alive": candidate_managed_alive,
            "candidate_listener_running": candidate_listener_running,
            "candidate_runtime_api_ready": candidate_runtime_api_ready,
            "candidate_runtime_state": candidate_runtime_state,
            "candidate_managed_cmdline": candidate_managed_cmdline,
            "candidate_managed_executable": candidate_managed_executable,
            "candidate_managed_cwd": candidate_managed_cwd,
            "candidate_start_reason": self._candidate_start_reason,
            "candidate_expected_managed_executable": candidate_expected_executable,
            "candidate_expected_managed_cwd": candidate_expected_cwd,
            "candidate_matches_candidate_slot": candidate_matches_candidate_slot,
            "active_manifest": active_manifest,
            "root_promotion_required": root_promotion_required,
            "bootstrap_update": bootstrap_update,
            "slot_structure": slot_structure,
            "restart_count": int(self._restart_count),
            "last_start_at": self._last_start_at,
            "last_stop_reason": self._last_stop_reason,
            "candidate_last_stop_reason": self._candidate_last_stop_reason,
            "last_exit_at": self._last_exit_at,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
            "updated_at": time.time(),
        }

    def _managed_runtime_slot_expectations(
        self,
        *,
        manifest: dict[str, Any] | None,
        managed_executable: str | None,
        managed_cwd: str | None,
    ) -> tuple[str | None, str | None, bool | None]:
        expected_executable = None
        expected_cwd = None
        matches_active_slot = None
        if isinstance(manifest, dict):
            argv = manifest.get("argv")
            if isinstance(argv, list) and argv:
                expected_executable = str(argv[0] or "").strip() or None
            expected_cwd = str(manifest.get("cwd") or "").strip() or None
        if expected_executable or expected_cwd:
            matches_active_slot = True
            if expected_executable and str(managed_executable or "").strip() != expected_executable:
                matches_active_slot = False
            if expected_cwd and str(managed_cwd or "").strip() != expected_cwd:
                matches_active_slot = False
        return expected_executable, expected_cwd, matches_active_slot

    def _persist_runtime_state(self) -> None:
        with contextlib.suppress(Exception):
            _write_json(_supervisor_runtime_state_path(), self._runtime_state_payload())
        with contextlib.suppress(Exception):
            write_memory_runtime_state(self._memory_runtime_state_payload())

    def _memory_runtime_state_payload(self) -> dict[str, Any]:
        ensure_memory_store()
        self._memory_baseline_family_rss_bytes = _positive_int_or_none(self._memory_baseline_family_rss_bytes)
        current_slot = str(active_slot() or "").strip().upper() or None
        managed = _proc_details(self._proc, cwd_hint=self._managed_runtime_cwd)
        managed_pid = managed.get("managed_pid")
        process_rss_bytes, family_rss_bytes = _process_family_rss_bytes(managed_pid)
        telemetry_tail = read_memory_telemetry_tail(limit=5000)
        sessions_index = read_memory_session_index()
        session_items = sessions_index.get("sessions") if isinstance(sessions_index.get("sessions"), list) else []
        last_session_id = self._memory_last_session_id
        if not last_session_id and session_items:
            last_item = session_items[-1] if isinstance(session_items[-1], dict) else {}
            last_session_id = str(last_item.get("session_id") or "").strip() or None
        return {
            "contract_version": "1",
            "authority": "supervisor",
            "selected_profiler_adapter": self._memory_profiler_adapter,
            "implemented_profiler_adapters": ["tracemalloc"],
            "planned_profiler_adapters": ["tracemalloc", "memray"],
            "current_profile_mode": self._memory_profile_mode,
            "implemented_profile_modes": ["normal", "sampled_profile", "trace_profile"],
            "planned_profile_modes": ["normal", "sampled_profile", "trace_profile"],
            "profile_control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            "implemented_profile_control_actions": list(IMPLEMENTED_PROFILE_CONTROL_ACTIONS),
            "implemented_profile_launch_env": list(PROFILE_LAUNCH_ENV_KEYS),
            "requested_profile_mode": self._memory_requested_profile_mode,
            "requested_session_id": self._memory_active_session_id,
            "publish_request_session_id": self._memory_publish_request_session_id,
            "suspicion_state": self._memory_suspicion_state,
            "suspicion_reason": self._memory_suspicion_reason,
            "suspicion_since": self._memory_suspicion_since,
            "active_session_id": self._memory_active_session_id,
            "last_session_id": last_session_id,
            "active_slot": current_slot,
            "runtime_instance_id": self._managed_runtime_instance_id,
            "transition_role": self._managed_transition_role,
            "managed_pid": managed_pid,
            "current_process_rss_bytes": process_rss_bytes,
            "current_family_rss_bytes": family_rss_bytes,
            "available_memory_bytes": self._memory_last_available_bytes,
            "available_memory_percent": self._memory_last_available_percent,
            "telemetry_interval_sec": _memory_telemetry_interval_sec(),
            "telemetry_window_sec": _memory_telemetry_window_sec(),
            "telemetry_samples_total": len(telemetry_tail),
            "baseline_family_rss_bytes": self._memory_baseline_family_rss_bytes,
            "rss_growth_bytes": self._memory_last_growth_bytes,
            "rss_growth_bytes_per_min": self._memory_last_growth_bytes_per_min,
            "suspicion_growth_threshold_bytes": _memory_suspicion_growth_threshold_bytes(),
            "suspicion_slope_threshold_bytes_per_min": _memory_suspicion_slope_threshold_bytes_per_min(),
            "policy_profile_restarts_enabled": _memory_policy_profile_restarts_enabled(),
            "auto_profile_min_uptime_sec": _memory_auto_profile_min_uptime_sec(),
            "auto_profile_browser_live_ttl_sec": _memory_auto_profile_browser_live_ttl_sec(),
            "auto_profile_last_block_reason": self._memory_auto_profile_last_block_reason,
            "auto_profile_last_block_at": self._memory_auto_profile_last_block_at,
            "critical_available_percent_threshold": _memory_critical_available_percent_threshold(),
            "critical_available_bytes_threshold": _memory_critical_available_bytes_threshold(),
            "critical_duration_sec": _memory_critical_duration_sec(),
            "critical_restart_cooldown_sec": _memory_critical_restart_cooldown_sec(),
            "critical_state": "critical" if self._memory_critical_since is not None else "normal",
            "critical_reason": self._memory_critical_reason,
            "critical_since": self._memory_critical_since,
            "critical_restart_last_at": self._memory_critical_restart_last_at,
            "telemetry_path": str(supervisor_memory_telemetry_path()),
            "sessions_index_path": str(supervisor_memory_sessions_index_path()),
            "implemented_operation_events": list(TOP_LEVEL_OPERATION_EVENTS),
            "operation_log_contract_version": MEMORY_OPERATION_CONTRACT_VERSION,
            "sessions_total": len(session_items),
            "updated_at": time.time(),
        }

    def _memory_sessions_index_compact(self, *, limit: int = 10) -> dict[str, Any]:
        index = read_memory_session_index()
        items = index.get("sessions") if isinstance(index.get("sessions"), list) else []
        normalized_limit = max(0, min(int(limit or 0), 100))
        if normalized_limit > 0:
            sessions = [dict(item) for item in items[-normalized_limit:] if isinstance(item, dict)]
        else:
            sessions = []
        omitted = max(0, len(items) - len(sessions))
        return {
            "contract_version": str(index.get("contract_version") or "1"),
            "sessions": sessions,
            "total": len(items),
            "returned": len(sessions),
            "omitted": omitted,
            "limit": normalized_limit,
            "compact": True,
            "updated_at": index.get("updated_at"),
        }

    def memory_status(self) -> dict[str, Any]:
        payload = self._memory_runtime_state_payload()
        payload["persisted_state"] = read_memory_runtime_state()
        payload["sessions_index"] = self._memory_sessions_index_compact(limit=10)
        payload["runtime_state_path"] = str(supervisor_memory_runtime_state_path())
        return payload

    def memory_telemetry(self, *, limit: int = 100) -> dict[str, Any]:
        items = read_memory_telemetry_tail(limit=max(1, min(int(limit or 100), 1000)))
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "telemetry_path": str(supervisor_memory_telemetry_path()),
            "runtime": self._memory_runtime_state_payload(),
        }

    def memory_sessions(self, *, limit: int = 100) -> dict[str, Any]:
        index = read_memory_session_index()
        items = index.get("sessions") if isinstance(index.get("sessions"), list) else []
        normalized_limit = max(1, min(int(limit or 100), 1000))
        returned = [dict(item) for item in items[-normalized_limit:] if isinstance(item, dict)]
        return {
            "ok": True,
            "contract_version": str(index.get("contract_version") or "1"),
            "sessions": returned,
            "total": len(items),
            "returned": len(returned),
            "limit": normalized_limit,
            "updated_at": index.get("updated_at"),
        }

    def memory_incidents(self, *, limit: int = 50) -> dict[str, Any]:
        items = self._memory_session_index_items()
        incidents: list[dict[str, Any]] = []
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            state = str(item.get("session_state") or "").strip().lower()
            suspected = bool(item.get("suspected_leak"))
            publish_state = str(item.get("publish_state") or "").strip().lower()
            if state not in {"failed", "finished", "stopped"} and not suspected and publish_state != "publish_requested":
                continue
            incidents.append(dict(item))
            if len(incidents) >= max(1, min(int(limit or 50), 200)):
                break
        return {
            "ok": True,
            "incidents": incidents,
            "total": len(incidents),
            "updated_at": read_memory_session_index().get("updated_at"),
        }

    def memory_session(self, session_id: str) -> dict[str, Any] | None:
        token = str(session_id or "").strip()
        if not token:
            return None
        payload = read_memory_session_summary(token)
        if payload is None:
            return None
        artifacts_dir = supervisor_memory_session_artifacts_dir(token)
        return {
            "ok": True,
            "session": payload,
            "operations": read_memory_session_operations(token, limit=100),
            "operations_path": str(supervisor_memory_session_operations_path(token)),
            "artifacts_dir": str(artifacts_dir),
            "telemetry": self._memory_session_telemetry_window(payload, limit=100),
        }

    def memory_session_artifact(self, session_id: str, artifact_id: str) -> dict[str, Any] | None:
        return self.memory_session_artifact_chunk(session_id, artifact_id, offset=0, max_bytes=256 * 1024)

    def memory_session_artifact_chunk(
        self,
        session_id: str,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int = 256 * 1024,
    ) -> dict[str, Any] | None:
        token = str(session_id or "").strip()
        ref_id = str(artifact_id or "").strip()
        if not token or not ref_id:
            return None
        session = read_memory_session_summary(token)
        if not isinstance(session, dict):
            return None
        refs = session.get("artifact_refs") if isinstance(session.get("artifact_refs"), list) else []
        artifact = next(
            (
                dict(item)
                for item in refs
                if isinstance(item, dict) and str(item.get("artifact_id") or "").strip() == ref_id
            ),
            None,
        )
        if artifact is None:
            return None
        path = Path(str(artifact.get("path") or "").strip()) if artifact.get("path") else None
        payload: dict[str, Any] = {
            "ok": True,
            "session_id": token,
            "artifact": artifact,
        }
        if path and path.exists():
            payload["exists"] = True
            size_bytes = int(path.stat().st_size)
            start = max(0, int(offset or 0))
            chunk_size = max(1, min(int(max_bytes or 256 * 1024), 1024 * 1024))
            if start > size_bytes:
                start = size_bytes
            remaining_bytes = max(0, size_bytes - start)
            read_bytes = min(chunk_size, remaining_bytes)
            content_type = str(artifact.get("content_type") or "").strip().lower()
            payload["transfer"] = {
                "offset": start,
                "requested_max_bytes": chunk_size,
                "size_bytes": size_bytes,
                "chunk_bytes": read_bytes,
                "remaining_bytes": max(0, remaining_bytes - read_bytes),
                "truncated": remaining_bytes > read_bytes,
                "pull_supported": True,
            }
            if content_type == "application/json" and start == 0 and size_bytes <= chunk_size:
                try:
                    payload["content"] = json.loads(path.read_text(encoding="utf-8"))
                    payload["transfer"]["encoding"] = "json"
                except Exception:
                    payload["content"] = None
                    payload["transfer"]["encoding"] = "unavailable"
            else:
                data = b""
                if read_bytes > 0:
                    with path.open("rb") as handle:
                        handle.seek(start)
                        data = handle.read(read_bytes)
                if content_type.startswith("text/"):
                    payload["text"] = data.decode("utf-8", errors="replace")
                    payload["transfer"]["encoding"] = "utf-8"
                else:
                    payload["content_base64"] = base64.b64encode(data).decode("ascii")
                    payload["transfer"]["encoding"] = "base64"
            payload["content"] = payload.get("content")
        else:
            payload["exists"] = False
            payload["content"] = None
            payload["transfer"] = {
                "offset": max(0, int(offset or 0)),
                "requested_max_bytes": max(1, min(int(max_bytes or 256 * 1024), 1024 * 1024)),
                "size_bytes": 0,
                "chunk_bytes": 0,
                "remaining_bytes": 0,
                "truncated": False,
                "pull_supported": False,
                "encoding": "unavailable",
            }
        return payload

    def start_memory_profile(
        self,
        *,
        profile_mode: str,
        reason: str,
        trigger_source: str = "operator",
    ) -> dict[str, Any]:
        summary = self._request_memory_profile_session(
            profile_mode=profile_mode,
            reason=reason,
            trigger_source=trigger_source,
        )
        return {
            "ok": True,
            "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            "session": summary,
            "runtime": self.memory_status(),
        }

    def retry_memory_profile(self, session_id: str, *, reason: str) -> dict[str, Any]:
        token = str(session_id or "").strip()
        summary = read_memory_session_summary(token)
        if summary is None:
            raise HTTPException(status_code=404, detail="memory profiling session was not found")
        state = str(summary.get("session_state") or "").strip().lower()
        if state not in {"failed", "cancelled", "stopped", "finished"}:
            raise HTTPException(status_code=409, detail="memory profiling session is not retryable yet")
        trigger_source = str(summary.get("trigger_source") or "operator").strip() or "operator"
        retried = self._request_memory_profile_session(
            profile_mode=str(summary.get("profile_mode") or "sampled_profile"),
            reason=str(reason or "operator.retry"),
            trigger_source=trigger_source,
            trigger_threshold=str(summary.get("trigger_threshold") or "").strip() or None,
        )
        retry_root_session_id = str(summary.get("retry_root_session_id") or token).strip() or token
        retry_depth = max(1, int(summary.get("retry_depth") or 0) + 1)
        retried["retry_of_session_id"] = token
        retried["retry_root_session_id"] = retry_root_session_id
        retried["retry_depth"] = retry_depth
        retried_window = (
            retried.get("operation_window") if isinstance(retried.get("operation_window"), dict) else {}
        )
        retried_window["retry_of_session_id"] = token
        retried_window["retry_root_session_id"] = retry_root_session_id
        retried_window["retry_depth"] = retry_depth
        retried_window["retry_reason"] = str(reason or "operator.retry")
        retried["operation_window"] = retried_window
        retried = self._upsert_memory_session_summary(retried)
        self._append_memory_operation(
            session_id=str(retried.get("session_id") or ""),
            event="tool_invoked",
            profile_mode=str(retried.get("profile_mode") or ""),
            details={
                "action": "profile_retry",
                "retry_of_session_id": token,
                "retry_root_session_id": retry_root_session_id,
                "retry_depth": retry_depth,
                "reason": str(reason or "operator.retry"),
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            },
        )
        return {
            "ok": True,
            "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            "retry_of_session_id": token,
            "session": retried,
            "runtime": self.memory_status(),
        }

    def stop_memory_profile(self, session_id: str, *, reason: str) -> dict[str, Any]:
        token = str(session_id or "").strip()
        summary = read_memory_session_summary(token)
        if summary is None:
            raise HTTPException(status_code=404, detail="memory profiling session was not found")
        now = time.time()
        state = str(summary.get("session_state") or "").strip().lower() or "planned"
        next_state = "cancelled" if state in {"planned", "requested"} else "stopped"
        summary["session_state"] = next_state
        summary["stop_reason"] = str(reason or "operator.stop")
        summary["stopped_at"] = now
        summary["finished_at"] = summary.get("finished_at") or now
        updated = self._upsert_memory_session_summary(summary)
        self._append_memory_operation(
            session_id=token,
            event="tool_invoked",
            profile_mode=str(updated.get("profile_mode") or ""),
            details={
                "action": "profile_stop",
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                "reason": str(reason or "operator.stop"),
            },
        )
        if token == str(self._memory_active_session_id or "").strip():
            self._memory_active_session_id = None
            self._memory_requested_profile_mode = None
        self._persist_runtime_state()
        return {
            "ok": True,
            "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            "session": updated,
            "runtime": self.memory_status(),
        }

    def _publish_memory_profile_to_root(
        self,
        *,
        summary: dict[str, Any],
        reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        token = str(summary.get("session_id") or "").strip()
        operations = read_memory_session_operations(token, limit=200)
        telemetry = self._memory_session_telemetry_window(summary, limit=200)
        try:
            conf = load_config()
        except Exception as exc:
            return (
                {
                    "ok": False,
                    "state": "publish_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "reason": "root_config_unavailable",
                },
                None,
            )
        try:
            result = report_hub_memory_profile(
                conf,
                session_summary=summary,
                operations=operations,
                telemetry=telemetry,
            )
        except Exception as exc:
            return (
                {
                    "ok": False,
                    "state": "publish_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "reason": str(reason or "operator.publish"),
                },
                None,
            )
        if not isinstance(result, dict):
            return (
                {
                    "ok": False,
                    "state": "publish_failed",
                    "error": "root client is unavailable",
                    "reason": str(reason or "operator.publish"),
                },
                None,
            )
        protocol_meta = result.get("_protocol") if isinstance(result.get("_protocol"), dict) else {}
        published_ref = (
            str(result.get("published_ref") or "").strip()
            or str(protocol_meta.get("message_id") or "").strip()
            or f"root://hub-memory-profile/{token}"
        )
        return (
            {
                "ok": True,
                "state": "published",
                "reason": str(reason or "operator.publish"),
                "reported_at": result.get("reported_at"),
                "published_ref": published_ref,
                "duplicate": bool(result.get("duplicate")),
                "message_id": protocol_meta.get("message_id") or result.get("message_id"),
                "cursor": protocol_meta.get("cursor"),
            },
            result,
        )

    def publish_memory_profile(self, session_id: str, *, reason: str) -> dict[str, Any]:
        token = str(session_id or "").strip()
        summary = read_memory_session_summary(token)
        if summary is None:
            raise HTTPException(status_code=404, detail="memory profiling session was not found")
        now = time.time()
        summary["publish_state"] = "publish_requested"
        summary["publish_requested_at"] = now
        self._memory_publish_request_session_id = token
        updated = self._upsert_memory_session_summary(summary)
        self._append_memory_operation(
            session_id=token,
            event="tool_invoked",
            profile_mode=str(updated.get("profile_mode") or ""),
            details={
                "action": "publish_request",
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                "reason": str(reason or "operator.publish"),
            },
        )
        publish_result, raw_result = self._publish_memory_profile_to_root(summary=updated, reason=reason)
        updated["publish_result"] = publish_result
        updated["published_to_root"] = bool(publish_result.get("ok"))
        updated["publish_state"] = str(publish_result.get("state") or "publish_failed")
        updated["published_ref"] = publish_result.get("published_ref")
        if bool(publish_result.get("ok")):
            artifact_refs = updated.get("artifact_refs") if isinstance(updated.get("artifact_refs"), list) else []
            published_artifacts: list[dict[str, Any]] = []
            for item in artifact_refs:
                if not isinstance(item, dict):
                    continue
                artifact_id = str(item.get("artifact_id") or "").strip()
                published_artifacts.append(
                    {
                        **item,
                        "published_ref": memory_profile_artifact_published_ref(
                            session_id=token,
                            artifact_id=artifact_id,
                        ) if artifact_id else item.get("published_ref"),
                        "fetch_strategy": (
                            "inline_content"
                            if bool(item.get("remote_available")) or str(item.get("publish_status") or "").strip() == "inline_available"
                            else "local_control_pull"
                        ),
                        "source_api_path": (
                            memory_profile_artifact_source_api_path(
                                session_id=token,
                                artifact_id=artifact_id,
                            )
                            if artifact_id
                            else item.get("source_api_path")
                        ),
                    }
                )
            updated["artifact_refs"] = published_artifacts
        updated_window = updated.get("operation_window") if isinstance(updated.get("operation_window"), dict) else {}
        updated_window["publish_result"] = publish_result
        updated["operation_window"] = updated_window
        updated = self._upsert_memory_session_summary(updated)
        self._append_memory_operation(
            session_id=token,
            event="tool_invoked",
            profile_mode=str(updated.get("profile_mode") or ""),
            details={
                "action": "publish_complete" if bool(publish_result.get("ok")) else "publish_failed",
                "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
                "reason": str(reason or "operator.publish"),
                "publish_state": updated.get("publish_state"),
                "published_ref": updated.get("published_ref"),
                "error": publish_result.get("error"),
            },
        )
        self._persist_runtime_state()
        return {
            "ok": True,
            "control_mode": IMPLEMENTED_PROFILE_CONTROL_MODE,
            "session": updated,
            "publish_result": publish_result,
            "root_result": raw_result,
            "runtime": self.memory_status(),
        }

    def _runtime_self_heal_decision(self, *, now: float | None = None) -> dict[str, Any] | None:
        proc = self._proc
        if proc is None or proc.poll() is not None or self._stopping or not self._desired_running:
            self._runtime_unhealthy_since = None
            self._runtime_unhealthy_kind = None
            return None
        update_status = read_core_update_status()
        update_state = str(update_status.get("state") or "").strip().lower()
        update_phase = str(update_status.get("phase") or "").strip().lower()
        current_slot = str(active_slot() or "").strip().upper() or None
        active_manifest = active_slot_manifest()
        managed = _proc_details(proc, cwd_hint=self._managed_runtime_cwd)
        managed_executable = str(managed.get("managed_executable") or "").strip() or None
        managed_cwd = str(managed.get("managed_cwd") or "").strip() or None
        expected_executable, expected_cwd, managed_matches_active_slot = self._managed_runtime_slot_expectations(
            manifest=active_manifest,
            managed_executable=managed_executable,
            managed_cwd=managed_cwd,
        )
        if managed_matches_active_slot is False:
            self._runtime_unhealthy_since = None
            self._runtime_unhealthy_kind = None
            mismatch_detail = expected_executable or expected_cwd or current_slot or "active slot"
            return {
                "reason": "supervisor.runtime.slot_mismatch",
                "message": (
                    f"active runtime process does not match the active slot {current_slot or '-'}"
                    f"; expected {mismatch_detail} and will be restarted"
                ),
                "active_slot": current_slot,
                "managed_executable": managed_executable,
                "managed_cwd": managed_cwd,
                "expected_managed_executable": expected_executable,
                "expected_managed_cwd": expected_cwd,
            }
        if update_state == "applying" and update_phase == "apply":
            # During core_update_apply the runner intentionally has no listener yet.
            # Let supervisor timeout/recovery handle a stalled apply instead of
            # repeatedly restarting the process mid-apply every listener timeout.
            #
            # Keep slot-mismatch recovery above this guard so a stale applying/apply
            # status cannot pin the supervisor to an outdated runtime after the
            # active slot marker has already moved on.
            self._runtime_unhealthy_since = None
            self._runtime_unhealthy_kind = None
            return None
        runtime_port = self.slot_runtime_port(current_slot)
        runtime_url = self.slot_runtime_base_url(current_slot)
        listener_running = _listener_running(self.runtime_host, runtime_port)
        api_ready = bool(listener_running and _runtime_api_ready(runtime_url, token=self.token))
        if listener_running and api_ready:
            self._runtime_unhealthy_since = None
            self._runtime_unhealthy_kind = None
            return None

        unhealthy_kind = "api_unready" if listener_running else "listener_lost"
        current_time = time.time() if now is None else float(now)
        if self._runtime_unhealthy_kind != unhealthy_kind:
            self._runtime_unhealthy_kind = unhealthy_kind
            self._runtime_unhealthy_since = current_time
            return None

        unhealthy_since = float(self._runtime_unhealthy_since or current_time)
        if self._last_start_at is not None:
            unhealthy_since = max(unhealthy_since, float(self._last_start_at))
        if unhealthy_kind == "listener_lost" and self._last_start_at is not None:
            runtime_age = max(0.0, current_time - float(self._last_start_at))
            if runtime_age < _runtime_listener_startup_grace_sec():
                return None
        timeout_sec = (
            _runtime_api_restart_timeout_sec()
            if unhealthy_kind == "api_unready"
            else _runtime_listener_restart_timeout_sec()
        )
        if (current_time - unhealthy_since) < timeout_sec:
            return None

        target = runtime_url if unhealthy_kind == "api_unready" else f"http://{self.runtime_host}:{runtime_port}"
        return {
            "reason": f"supervisor.runtime.{unhealthy_kind}",
            "message": (
                f"active runtime stayed {unhealthy_kind.replace('_', ' ')} for {timeout_sec:.0f}s"
                f" at {target}; restarting"
            ),
            "runtime_port": runtime_port,
            "runtime_url": runtime_url,
            "listener_running": listener_running,
            "runtime_api_ready": api_ready,
            "timeout_sec": timeout_sec,
        }

    def _local_supervisor_update_status_payload(self) -> dict[str, Any]:
        payload = _local_update_payload()
        payload["runtime"] = self.status()
        payload["_served_by"] = "supervisor_fallback"
        return _reconcile_update_status(payload)

    async def _spawn_runtime_locked(self, *, reason: str = "supervisor.start") -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        profile_mode = self._desired_memory_profile_mode()
        profile_session_id = str(self._memory_active_session_id or "").strip() or None
        profile_trigger_source = self._active_memory_profile_trigger_source() if profile_mode != "normal" else ""
        allowed, block_reason = self._memory_profile_restart_guard(desired_mode=profile_mode)
        if not allowed:
            self._record_memory_auto_profile_block(block_reason)
            profile_mode = "normal"
            profile_session_id = None
            profile_trigger_source = ""
        argv, command, env, cwd, runtime_instance_id, transition_role = self._runtime_launch_spec(
            profile_mode=profile_mode,
            profile_session_id=profile_session_id,
        )
        proc = subprocess.Popen(
            argv or command or [],
            shell=bool(command),
            cwd=cwd or os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=(os.name != "nt"),
            creationflags=(int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0),
        )
        self._proc = proc
        self._managed_runtime_instance_id = runtime_instance_id
        self._managed_transition_role = transition_role
        self._managed_runtime_cwd = str(cwd or os.getcwd())
        self._managed_start_reason = str(reason or "supervisor.start")
        self._memory_profile_mode = profile_mode
        self._memory_profile_current_trigger_source = str(profile_trigger_source or "").strip().lower() or None
        self._last_start_at = time.time()
        self._last_error = None
        self._runtime_unhealthy_since = None
        self._runtime_unhealthy_kind = None
        if profile_mode != "normal":
            self._mark_active_memory_session_running(runtime_instance_id=runtime_instance_id, transition_role=transition_role)
        self._persist_runtime_state()

    async def _spawn_sidecar_locked(self, *, reason: str = "supervisor.sidecar.start") -> None:
        proc = self._sidecar_proc
        if proc is not None and proc.poll() is None:
            return
        code_state = self._sidecar_code_state()
        repo_root = str(self._sidecar_repo_root() or "").strip() or None
        self._sidecar_proc = await start_realtime_sidecar_subprocess(
            role=self._sidecar_role(),
            repo_root=repo_root,
        )
        self._sidecar_launch_cwd = str(code_state.get("repo_root") or code_state.get("launch_cwd") or "") or None
        self._sidecar_code_fingerprint = str(code_state.get("fingerprint") or "").strip() or None
        self._sidecar_code_fingerprint_updated_at = time.time() if self._sidecar_code_fingerprint else None
        self._sidecar_code_change_pending_fingerprint = None
        self._sidecar_code_change_pending_since = None
        self._sidecar_last_start_reason = str(reason or "supervisor.sidecar.start")
        self._sidecar_last_probe_at = None
        self._sidecar_last_probe_ok = None
        self._sidecar_last_probe_error = None
        self._sidecar_consecutive_probe_failures = 0
        self._record_sidecar_restart_attempt(reason=reason)
        self._persist_runtime_state()

    async def _spawn_candidate_runtime_locked(self, *, slot: str, reason: str = "supervisor.candidate.start") -> None:
        resolved_slot = str(slot or "").strip().upper()
        if not resolved_slot:
            raise RuntimeError("candidate slot is required")
        if resolved_slot == str(active_slot() or "").strip().upper():
            raise RuntimeError("candidate slot must differ from the active slot")
        existing = self._candidate_proc
        if (
            existing is not None
            and existing.poll() is None
            and str(self._candidate_slot or "").strip().upper() == resolved_slot
        ):
            return
        if existing is not None and existing.poll() is None:
            await self._terminate_candidate_proc_locked(graceful=True, reason="supervisor.candidate.replace")
        argv, command, env, cwd, runtime_instance_id, transition_role = self._runtime_launch_spec(
            slot=resolved_slot,
            transition_role="candidate",
            profile_mode="normal",
            profile_session_id=None,
            profile_trigger=None,
            skip_pending_update=True,
        )
        proc = subprocess.Popen(
            argv or command or [],
            shell=bool(command),
            cwd=cwd or os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=(os.name != "nt"),
            creationflags=(int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0),
        )
        self._candidate_proc = proc
        self._candidate_slot = resolved_slot
        self._candidate_runtime_instance_id = runtime_instance_id
        self._candidate_transition_role = transition_role
        self._candidate_runtime_cwd = str(cwd or os.getcwd())
        self._candidate_start_reason = str(reason or "supervisor.candidate.start")
        self._persist_runtime_state()

    async def ensure_started(self, *, reason: str = "supervisor.start") -> None:
        async with self._lock:
            self._stopping = False
            self._desired_running = True
            await self._spawn_runtime_locked(reason=reason)

    async def ensure_sidecar_started(self) -> dict[str, Any]:
        async with self._lock:
            await self._spawn_sidecar_locked()
            self._persist_runtime_state()
            return self._sidecar_status_payload()

    async def _terminate_proc_locked(
        self,
        *,
        proc: subprocess.Popen[Any] | None = None,
        base_url: str | None = None,
        graceful: bool,
        reason: str,
    ) -> None:
        proc = proc or self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            return
        profile_mode = self._memory_profile_mode if proc is self._proc else "normal"
        profile_session_id = str(self._memory_active_session_id or "").strip() if proc is self._proc else ""
        drain_timeout_sec, signal_delay_sec, graceful_wait_sec, terminate_wait_sec = _runtime_profile_graceful_shutdown_timeout_sec(
            profile_mode
        )
        if graceful:
            shutdown_requested = False
            shutdown_url = str(base_url or self.runtime_base_url) + "/api/admin/shutdown"
            shutdown_status_code: int | None = None
            shutdown_error: str | None = None
            try:
                headers = {"Content-Type": "application/json"}
                if self.token:
                    headers["X-AdaOS-Token"] = self.token
                _LOG.info(
                    "supervisor requesting runtime shutdown reason=%s url=%s profile_mode=%s session_id=%s pid=%s",
                    reason,
                    shutdown_url,
                    profile_mode,
                    profile_session_id or None,
                    getattr(proc, "pid", None),
                )
                response = requests.post(
                    shutdown_url,
                    headers=headers,
                    json={
                        "reason": reason,
                        "drain_timeout_sec": float(drain_timeout_sec),
                        "signal_delay_sec": float(signal_delay_sec),
                    },
                    timeout=_runtime_shutdown_request_timeout(
                        drain_timeout_sec=drain_timeout_sec,
                        signal_delay_sec=signal_delay_sec,
                    ),
                )
                shutdown_status_code = int(response.status_code)
                response_tail = (response.text or "").strip()[-400:]
                _LOG.info(
                    "supervisor runtime shutdown response reason=%s url=%s status_code=%s body_tail=%s",
                    reason,
                    shutdown_url,
                    shutdown_status_code,
                    response_tail,
                )
                shutdown_requested = True
            except Exception as exc:
                shutdown_error = f"{type(exc).__name__}: {exc}"
                _LOG.warning(
                    "supervisor runtime shutdown request failed reason=%s url=%s profile_mode=%s session_id=%s error=%s",
                    reason,
                    shutdown_url,
                    profile_mode,
                    profile_session_id or None,
                    shutdown_error,
                )
            finalize_wait_deadline = time.time() + float(_runtime_profile_finalize_wait_sec())
            if shutdown_requested and profile_mode != "normal" and profile_session_id:
                _LOG.info(
                    "supervisor waiting for runtime profile finalize marker session_id=%s timeout_sec=%.2f",
                    profile_session_id,
                    _runtime_profile_finalize_wait_sec(),
                )
                finalize_checks = max(1, int(float(_runtime_profile_finalize_wait_sec()) / 0.1) + 2)
                while time.time() < finalize_wait_deadline and finalize_checks > 0:
                    finalize_checks -= 1
                    if proc.poll() is not None:
                        return
                    if self._memory_profile_finalize_observed(profile_session_id):
                        _LOG.info(
                            "supervisor observed runtime profile finalize marker session_id=%s",
                            profile_session_id,
                        )
                        break
                    await asyncio.sleep(0.1)
                else:
                    _LOG.warning(
                        "supervisor did not observe runtime profile finalize marker before timeout session_id=%s shutdown_status_code=%s shutdown_error=%s",
                        profile_session_id,
                        shutdown_status_code,
                        shutdown_error,
                    )
            deadline = time.time() + float(graceful_wait_sec)
            graceful_checks = max(1, int(float(graceful_wait_sec) / 0.2) + 2)
            while time.time() < deadline and graceful_checks > 0:
                graceful_checks -= 1
                if proc.poll() is not None:
                    return
                await asyncio.sleep(0.2)
        with contextlib.suppress(Exception):
            proc.terminate()
        deadline = time.time() + float(terminate_wait_sec)
        terminate_checks = max(1, int(float(terminate_wait_sec) / 0.1) + 2)
        while time.time() < deadline and terminate_checks > 0:
            terminate_checks -= 1
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.1)
        with contextlib.suppress(Exception):
            proc.kill()

    async def _terminate_candidate_proc_locked(self, *, graceful: bool, reason: str) -> None:
        candidate_slot = str(self._candidate_slot or "").strip().upper() or None
        candidate_base_url = self.slot_runtime_base_url(candidate_slot) if candidate_slot else None
        await self._terminate_proc_locked(
            proc=self._candidate_proc,
            base_url=candidate_base_url,
            graceful=graceful,
            reason=reason,
        )
        self._candidate_last_stop_reason = str(reason or "supervisor.candidate.stop")
        self._candidate_proc = None
        self._candidate_slot = None
        self._candidate_runtime_instance_id = None
        self._candidate_transition_role = None
        self._candidate_runtime_cwd = None
        self._persist_runtime_state()

    async def restart_runtime(self, *, reason: str = "supervisor.restart") -> dict[str, Any]:
        decision = self._transition_continuity_guard_decision(operation="restart")
        if decision is not None:
            self._raise_restart_continuity_block(decision)
        async with self._lock:
            self._desired_running = True
            await self._terminate_proc_locked(graceful=True, reason=reason)
            self._last_stop_reason = str(reason or "supervisor.restart")
            await self._spawn_runtime_locked(reason=reason)
            self._restart_count += 1
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def stop(self, *, reason: str = "supervisor.stop") -> None:
        async with self._lock:
            self._desired_running = False
            self._stopping = True
            await self._terminate_proc_locked(graceful=True, reason=reason)
            self._last_stop_reason = str(reason or "supervisor.stop")
            await self._terminate_candidate_proc_locked(graceful=True, reason=f"{reason}.candidate")
            self._persist_runtime_state()

    async def stop_sidecar(self, *, reason: str = "supervisor.sidecar.stop") -> dict[str, Any]:
        async with self._lock:
            await stop_realtime_sidecar_subprocess(self._sidecar_proc)
            self._sidecar_proc = None
            self._sidecar_last_restart_reason = str(reason or "supervisor.sidecar.stop")
            self._persist_runtime_state()
            return self._sidecar_status_payload()

    def sidecar_status(self) -> dict[str, Any]:
        payload = self._sidecar_status_payload()
        return {
            "ok": True,
            "runtime": self._runtime_sidecar_runtime_payload(),
            "process": payload.get("process"),
        }

    async def restart_sidecar(self, *, reconnect_hub_root: bool = False) -> dict[str, Any]:
        async with self._lock:
            try:
                new_proc, restart_result = await restart_realtime_sidecar_subprocess(
                    proc=self._sidecar_proc,
                    role=self._sidecar_role(),
                    repo_root=str(self._sidecar_repo_root() or "").strip() or None,
                )
            except TypeError:
                new_proc, restart_result = await restart_realtime_sidecar_subprocess(
                    proc=self._sidecar_proc,
                    role=self._sidecar_role(),
                )
            self._sidecar_proc = new_proc
            code_state = self._sidecar_code_state()
            self._sidecar_launch_cwd = str(code_state.get("repo_root") or code_state.get("launch_cwd") or "") or None
            self._sidecar_code_fingerprint = str(code_state.get("fingerprint") or "").strip() or None
            self._sidecar_code_fingerprint_updated_at = time.time() if self._sidecar_code_fingerprint else None
            self._sidecar_code_change_pending_fingerprint = None
            self._sidecar_code_change_pending_since = None
            self._sidecar_last_start_reason = "supervisor.sidecar.restart"
            self._sidecar_last_restart_reason = str(restart_result.get("reason") or "restarted")
            self._sidecar_last_probe_at = None
            self._sidecar_last_probe_ok = None
            self._sidecar_last_probe_error = None
            self._sidecar_consecutive_probe_failures = 0
            self._record_sidecar_restart_attempt(reason=self._sidecar_last_restart_reason)
            self._persist_runtime_state()
        reconnect_result: dict[str, Any] | None = None
        if reconnect_hub_root and str(self._sidecar_role() or "").strip().lower() == "hub":
            try:
                reconnect_result = self._runtime_request_json(
                    path="/api/node/hub-root/reconnect",
                    method="POST",
                    payload={},
                    timeout=5.0,
                )
            except Exception as exc:
                reconnect_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "restart": restart_result,
            "reconnect": reconnect_result,
            "runtime": self._runtime_sidecar_runtime_payload(),
            "process": self._sidecar_status_payload().get("process"),
        }

    async def start_candidate_runtime(
        self,
        *,
        slot: str | None = None,
        reason: str = "supervisor.candidate.start",
    ) -> dict[str, Any]:
        resolved_slot = str(slot or choose_inactive_slot() or "").strip().upper()
        if resolved_slot not in {"A", "B"}:
            raise HTTPException(status_code=409, detail="candidate slot is unavailable")
        current_slot = str(active_slot() or "").strip().upper()
        if resolved_slot == current_slot:
            raise HTTPException(status_code=409, detail="candidate slot must differ from the active slot")
        if self.slot_runtime_port(resolved_slot) == self.slot_runtime_port(current_slot):
            raise HTTPException(status_code=409, detail="candidate slot uses the same runtime port as the active slot")
        structure = validate_slot_structure(resolved_slot)
        if not bool(structure.get("ok")):
            raise HTTPException(status_code=409, detail=f"candidate slot {resolved_slot} is not launchable")
        async with self._lock:
            await self._spawn_candidate_runtime_locked(slot=resolved_slot, reason=reason)
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def stop_candidate_runtime(self, *, reason: str = "supervisor.candidate.stop") -> dict[str, Any]:
        async with self._lock:
            await self._terminate_candidate_proc_locked(graceful=True, reason=reason)
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def _candidate_prewarm(self, *, target_slot: str | None) -> dict[str, Any]:
        resolved_target = str(target_slot or "").strip().upper()
        if not resolved_target:
            return {
                "attempted": False,
                "state": "skipped",
                "message": "candidate prewarm skipped: target slot is unavailable",
            }

        runtime_snapshot = self.status()
        candidate_slot = str(runtime_snapshot.get("candidate_slot") or "").strip().upper()
        transition_mode = str(runtime_snapshot.get("transition_mode") or "").strip().lower()
        warm_switch_allowed = bool(runtime_snapshot.get("warm_switch_allowed"))
        warm_switch_reason = str(runtime_snapshot.get("warm_switch_reason") or "").strip()
        if candidate_slot != resolved_target or transition_mode != "warm_switch" or not warm_switch_allowed:
            return {
                "attempted": False,
                "state": "skipped",
                "message": warm_switch_reason or "candidate prewarm skipped: warm switch is not admitted",
                "runtime": runtime_snapshot,
            }

        await self.start_candidate_runtime(slot=resolved_target, reason="supervisor.candidate.prewarm")
        timeout_sec = _warm_switch_candidate_ready_timeout_sec()
        deadline = time.time() + timeout_sec
        snapshot = self.status()
        while timeout_sec > 0.0 and time.time() < deadline:
            snapshot = self.status()
            if str(snapshot.get("candidate_slot") or "").strip().upper() != resolved_target:
                break
            if bool(snapshot.get("candidate_runtime_api_ready")):
                return {
                    "attempted": True,
                    "state": "ready",
                    "message": (
                        f"passive candidate runtime is ready on {snapshot.get('candidate_runtime_url')}"
                    ),
                    "ready_at": time.time(),
                    "runtime": snapshot,
                }
            await asyncio.sleep(0.25)

        snapshot = self.status()
        candidate_alive = bool(snapshot.get("candidate_managed_alive"))
        candidate_ready = bool(snapshot.get("candidate_runtime_api_ready"))
        candidate_url = str(snapshot.get("candidate_runtime_url") or "").strip()
        if candidate_ready:
            return {
                "attempted": True,
                "state": "ready",
                "message": f"passive candidate runtime is ready on {candidate_url}",
                "ready_at": time.time(),
                "runtime": snapshot,
            }
        if candidate_alive:
            return {
                "attempted": True,
                "state": "starting",
                "message": (
                    f"passive candidate runtime is still warming on {candidate_url or resolved_target}"
                ),
                "runtime": snapshot,
            }
        return {
            "attempted": True,
            "state": "failed",
            "message": "candidate prewarm failed before the runtime became ready",
            "runtime": snapshot,
        }

    async def _cleanup_candidate_runtime(self, *, reason: str, slot: str | None = None) -> dict[str, Any]:
        resolved_slot = str(slot or "").strip().upper() or None
        async with self._lock:
            current_slot = str(self._candidate_slot or "").strip().upper() or None
            if self._candidate_proc is None or (resolved_slot and current_slot != resolved_slot):
                self._persist_runtime_state()
                return {
                    "ok": True,
                    "stopped": False,
                    "slot": current_slot,
                }
            await self._terminate_candidate_proc_locked(graceful=True, reason=reason)
            self._persist_runtime_state()
            return {
                "ok": True,
                "stopped": True,
                "slot": current_slot,
            }

    async def _promote_candidate_runtime(self, *, slot: str, reason: str) -> dict[str, Any]:
        resolved_slot = str(slot or "").strip().upper()
        current_candidate_slot = str(self._candidate_slot or "").strip().upper()
        candidate_proc = self._candidate_proc
        if resolved_slot not in {"A", "B"}:
            raise RuntimeError("candidate slot is unavailable for fast cutover")
        if current_candidate_slot != resolved_slot:
            raise RuntimeError("candidate runtime slot does not match the prepared target slot")
        if candidate_proc is None or candidate_proc.poll() is not None:
            raise RuntimeError("candidate runtime is not running for fast cutover")

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        candidate_base_url = self.slot_runtime_base_url(resolved_slot)
        response = requests.post(
            candidate_base_url + "/api/admin/runtime/promote-active",
            headers=headers,
            json={"reason": reason, "reconnect_hub_root": True},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("candidate promotion returned a non-object payload")
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        promoted_role = str(runtime.get("transition_role") or "").strip().lower()
        if promoted_role != "active":
            raise RuntimeError("candidate runtime did not report active role after promotion")
        promoted_instance_id = str(runtime.get("runtime_instance_id") or self._candidate_runtime_instance_id or "").strip() or None
        async with self._lock:
            proc = self._candidate_proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError("candidate runtime exited before supervisor adopted it")
            self._proc = proc
            self._managed_runtime_instance_id = promoted_instance_id
            self._managed_transition_role = "active"
            self._managed_runtime_cwd = self._candidate_runtime_cwd
            self._candidate_proc = None
            self._candidate_slot = None
            self._candidate_runtime_instance_id = None
            self._candidate_transition_role = None
            self._candidate_runtime_cwd = None
            self._last_start_at = time.time()
            self._last_error = None
            self._restart_count += 1
            self._persist_runtime_state()
        return payload

    async def monitor_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            sidecar_proc = self._sidecar_proc
            if sidecar_proc is not None and sidecar_proc.poll() is not None:
                self._sidecar_last_restart_reason = "supervisor.sidecar.exited"
                self._sidecar_proc = None
                self._persist_runtime_state()
            if realtime_sidecar_enabled(role=self._sidecar_role()) and not self._stopping:
                sync_result = self._sync_sidecar_controlled_files_from_validated_slot()
                if bool(sync_result.get("changed")):
                    self._persist_runtime_state()
                sidecar_snapshot = realtime_sidecar_listener_snapshot(self._sidecar_proc, role=self._sidecar_role())
                code_state = self._sidecar_code_state()
                current_fingerprint = str(code_state.get("fingerprint") or "").strip() or None
                code_changed = bool(
                    current_fingerprint
                    and self._sidecar_code_fingerprint
                    and current_fingerprint != self._sidecar_code_fingerprint
                )
                code_change_ready = False
                if code_changed:
                    if current_fingerprint != self._sidecar_code_change_pending_fingerprint:
                        self._sidecar_code_change_pending_fingerprint = current_fingerprint
                        self._sidecar_code_change_pending_since = time.time()
                    elif self._sidecar_code_change_pending_since is not None:
                        code_change_ready = (
                            time.time() - float(self._sidecar_code_change_pending_since)
                            >= _sidecar_code_change_debounce_sec()
                        )
                else:
                    self._sidecar_code_change_pending_fingerprint = None
                    self._sidecar_code_change_pending_since = None
                sidecar_ready = await self._probe_sidecar_health()
                should_restart_sidecar = False
                restart_reason = None
                if bool(sync_result.get("changed")):
                    should_restart_sidecar = True
                    restart_reason = "supervisor.sidecar.validated_slot_sync"
                elif self._sidecar_proc is None and not bool(sidecar_snapshot.get("listener_running")):
                    should_restart_sidecar = True
                    restart_reason = "supervisor.sidecar.missing"
                elif code_changed and code_change_ready:
                    should_restart_sidecar = True
                    restart_reason = "supervisor.sidecar.code_changed"
                elif sidecar_ready is False and self._sidecar_consecutive_probe_failures >= 2:
                    should_restart_sidecar = True
                    restart_reason = "supervisor.sidecar.unhealthy"
                if should_restart_sidecar:
                    allowed, blocked_reason = self._sidecar_restart_allowed()
                    if not allowed:
                        self._sidecar_last_restart_reason = blocked_reason
                        self._persist_runtime_state()
                        await self._maybe_resume_or_continue_transition()
                        candidate_proc = self._candidate_proc
                        if candidate_proc is not None:
                            candidate_rc = candidate_proc.poll()
                            if candidate_rc is not None:
                                self._candidate_last_stop_reason = self._candidate_last_stop_reason or "supervisor.candidate.exited"
                                self._candidate_proc = None
                                self._candidate_slot = None
                                self._candidate_runtime_instance_id = None
                                self._candidate_transition_role = None
                                self._candidate_runtime_cwd = None
                                self._persist_runtime_state()
                        proc = self._proc
                        if proc is None:
                            self._runtime_unhealthy_since = None
                            self._runtime_unhealthy_kind = None
                            if self._desired_running and not self._stopping:
                                async with self._lock:
                                    if self._proc is None and self._desired_running and not self._stopping:
                                        await self._spawn_runtime_locked(reason="supervisor.monitor.ensure_running")
                            continue
                    try:
                        async with self._lock:
                            if self._stopping:
                                pass
                            elif self._sidecar_proc is None and restart_reason == "supervisor.sidecar.missing":
                                self._sidecar_last_restart_reason = restart_reason
                                await self._spawn_sidecar_locked(reason=restart_reason)
                            else:
                                self._sidecar_last_restart_reason = str(restart_reason or "supervisor.sidecar.restart")
                                new_proc, restart_result = await restart_realtime_sidecar_subprocess(
                                    proc=self._sidecar_proc,
                                    role=self._sidecar_role(),
                                    repo_root=str(self._sidecar_repo_root() or "").strip() or None,
                                )
                                self._sidecar_proc = new_proc
                                self._sidecar_launch_cwd = str(code_state.get("repo_root") or self._sidecar_launch_cwd or "") or None
                                self._sidecar_code_fingerprint = current_fingerprint
                                self._sidecar_code_fingerprint_updated_at = time.time() if current_fingerprint else None
                                self._sidecar_last_start_reason = str(restart_reason or "supervisor.sidecar.restart")
                                self._sidecar_last_restart_reason = str(restart_reason or restart_result.get("reason") or "restarted")
                                self._sidecar_last_probe_at = None
                                self._sidecar_last_probe_ok = None
                                self._sidecar_last_probe_error = None
                                self._sidecar_consecutive_probe_failures = 0
                                self._record_sidecar_restart_attempt(reason=self._sidecar_last_restart_reason)
                                self._persist_runtime_state()
                    except Exception:
                        _LOG.warning("failed to restart adaos-realtime sidecar", exc_info=True)
            await self._maybe_resume_or_continue_transition()
            candidate_proc = self._candidate_proc
            if candidate_proc is not None:
                candidate_rc = candidate_proc.poll()
                if candidate_rc is not None:
                    self._candidate_last_stop_reason = self._candidate_last_stop_reason or "supervisor.candidate.exited"
                    self._candidate_proc = None
                    self._candidate_slot = None
                    self._candidate_runtime_instance_id = None
                    self._candidate_transition_role = None
                    self._candidate_runtime_cwd = None
                    self._persist_runtime_state()
            proc = self._proc
            if proc is None:
                self._runtime_unhealthy_since = None
                self._runtime_unhealthy_kind = None
                if self._desired_running and not self._stopping:
                    async with self._lock:
                        if self._proc is None and self._desired_running and not self._stopping:
                            await self._spawn_runtime_locked(reason="supervisor.monitor.ensure_running")
                continue
            rc = proc.poll()
            if rc is None:
                with contextlib.suppress(Exception):
                    self._sample_memory_telemetry()
                critical_memory_decision = self._memory_critical_restart_decision()
                if critical_memory_decision is not None:
                    self._last_error = str(
                        critical_memory_decision.get("message") or "runtime restart requested due to critical memory pressure"
                    )
                    self._memory_critical_restart_last_at = time.time()
                    self._persist_runtime_state()
                    try:
                        await self.restart_runtime(
                            reason=str(critical_memory_decision.get("reason") or "supervisor.memory.critical_pressure")
                        )
                    except Exception:
                        _LOG.warning("failed to self-heal critical memory pressure", exc_info=True)
                    continue
                try:
                    await self._maybe_apply_memory_profile_mode()
                except Exception as exc:
                    _LOG.warning("failed to apply requested memory profile mode", exc_info=True)
                    if str(self._memory_active_session_id or "").strip() and self._desired_memory_profile_mode() != "normal":
                        self._fail_active_memory_session(
                            reason=f"requested_profile_mode_apply_error.{self._desired_memory_profile_mode()}",
                            stage="profile_apply_error",
                            details={
                                "error": f"{type(exc).__name__}: {exc}",
                                "active_slot": str(active_slot() or "").strip().upper() or None,
                                "runtime_instance_id": self._managed_runtime_instance_id,
                                "transition_role": self._managed_transition_role,
                            },
                        )
                try:
                    self._expire_stuck_requested_memory_profile()
                except Exception:
                    _LOG.warning("failed to expire stuck requested memory profile session", exc_info=True)
                finalize_profile = self._should_finalize_active_memory_profile()
                if finalize_profile is not None:
                    try:
                        finalize_trigger_source = str(finalize_profile.get("trigger_source") or "").strip().lower() or None
                        self.stop_memory_profile(
                            str(finalize_profile.get("session_id") or ""),
                            reason=str(finalize_profile.get("reason") or "supervisor.memory.profile_window_complete"),
                        )
                        if finalize_trigger_source:
                            self._memory_profile_current_trigger_source = finalize_trigger_source
                        allowed, block_reason = self._memory_profile_restart_guard(desired_mode="normal")
                        if not allowed:
                            self._record_memory_auto_profile_block(block_reason)
                            self._persist_runtime_state()
                            continue
                        await self.restart_runtime(
                            reason=f"supervisor.memory.complete_profile_mode.{str(finalize_profile.get('profile_mode') or 'profile')}"
                        )
                    except Exception:
                        _LOG.warning("failed to finalize active memory profile session", exc_info=True)
                    continue
                try:
                    await self._maybe_maintain_required_upstream_link()
                except Exception:
                    _LOG.warning("required-upstream-link supervisor watchdog failed", exc_info=True)
                restart_decision = self._runtime_self_heal_decision()
                if restart_decision is not None:
                    self._last_error = str(restart_decision.get("message") or "active runtime became unhealthy")
                    self._runtime_unhealthy_since = None
                    self._runtime_unhealthy_kind = None
                    self._persist_runtime_state()
                    try:
                        await self.restart_runtime(
                            reason=str(restart_decision.get("reason") or "supervisor.runtime.unhealthy")
                        )
                    except Exception:
                        _LOG.warning("failed to self-heal active runtime", exc_info=True)
                    continue
                continue
            self._last_exit_code = int(rc)
            self._last_exit_at = time.time()
            self._last_stop_reason = self._last_stop_reason or "supervisor.runtime.exited"
            if self._memory_profile_mode != "normal" and not self._stopping and self._desired_running:
                self._fail_active_memory_session(
                    reason="runtime_exited_during_profile_mode",
                    exit_code=int(rc),
                )
            self._proc = None
            self._managed_runtime_instance_id = None
            self._managed_transition_role = None
            self._managed_runtime_cwd = None
            self._runtime_unhealthy_since = None
            self._runtime_unhealthy_kind = None
            self._memory_profile_mode = "normal"
            self._memory_profile_current_trigger_source = None
            self._persist_runtime_state()
            if self._stopping or not self._desired_running:
                continue
            async with self._lock:
                if self._proc is None and self._desired_running and not self._stopping:
                    await asyncio.sleep(1.0)
                    await self._spawn_runtime_locked(reason="supervisor.monitor.respawn_after_exit")

    async def start(self) -> None:
        try:
            await self.ensure_sidecar_started()
        except Exception:
            _LOG.warning("failed to start adaos-realtime sidecar", exc_info=True)
        await self.ensure_started(reason="supervisor.start")
        self._monitor_task = asyncio.create_task(self.monitor_forever(), name="adaos-supervisor-monitor")

    async def close(self) -> None:
        self._stopping = True
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(BaseException):
                await self._update_task
            self._update_task = None
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(BaseException):
                await self._monitor_task
        async with self._lock:
            await self._terminate_candidate_proc_locked(graceful=True, reason="supervisor.shutdown.candidate")
        await self.stop(reason="supervisor.shutdown")
        try:
            await self.stop_sidecar(reason="supervisor.shutdown.sidecar")
        except Exception:
            _LOG.warning("failed to stop adaos-realtime sidecar", exc_info=True)

    def status(self) -> dict[str, Any]:
        payload = self._runtime_state_payload()
        payload["persisted_state"] = _read_json(_supervisor_runtime_state_path())
        payload["update_attempt"] = _read_update_attempt()
        payload["update_task_running"] = bool(self._update_task is not None and not self._update_task.done())
        return payload

    def supervisor_update_status(self) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        try:
            response = requests.get(
                self.runtime_base_url + "/api/admin/update/status",
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                payload.setdefault("runtime", self.status())
                payload["_served_by"] = "runtime"
                return _reconcile_update_status(payload)
        except Exception:
            pass
        return self._local_supervisor_update_status_payload()

    def public_update_status(self) -> dict[str, Any]:
        return _public_update_status_payload(self._local_supervisor_update_status_payload())

    def public_memory_status(self) -> dict[str, Any]:
        runtime = self._memory_runtime_state_payload()
        last_session_id = str(runtime.get("last_session_id") or "").strip() or None
        active_session_id = str(runtime.get("active_session_id") or "").strip() or None
        last_session = read_memory_session_summary(last_session_id) if last_session_id else None
        active_session = read_memory_session_summary(active_session_id) if active_session_id else None
        session = active_session if isinstance(active_session, dict) and active_session else last_session
        compact_session = None
        if isinstance(session, dict):
            compact_session = {
                "session_id": str(session.get("session_id") or "").strip() or None,
                "profile_mode": str(session.get("profile_mode") or "").strip() or None,
                "session_state": str(session.get("session_state") or "").strip() or None,
                "trigger_source": str(session.get("trigger_source") or "").strip() or None,
                "trigger_reason": str(session.get("trigger_reason") or "").strip() or None,
                "requested_at": session.get("requested_at"),
                "finished_at": session.get("finished_at"),
                "stopped_at": session.get("stopped_at"),
                "publish_state": str(session.get("publish_state") or "").strip() or None,
                "published_ref": str(session.get("published_ref") or "").strip() or None,
                "failure_reason": str(
                    (
                        session.get("operation_window")
                        if isinstance(session.get("operation_window"), dict)
                        else {}
                    ).get("failure_reason")
                    or ""
                ).strip() or None,
                "failure_stage": str(
                    (
                        session.get("operation_window")
                        if isinstance(session.get("operation_window"), dict)
                        else {}
                    ).get("failure_stage")
                    or ""
                ).strip() or None,
                "local_incident_artifact_id": str(
                    (
                        session.get("operation_window")
                        if isinstance(session.get("operation_window"), dict)
                        else {}
                    ).get("local_incident_artifact_id")
                    or ""
                ).strip() or None,
                "local_incident_headline": str(
                    (
                        session.get("operation_window")
                        if isinstance(session.get("operation_window"), dict)
                        else {}
                    ).get("local_incident_headline")
                    or ""
                ).strip() or None,
                "local_incident_dominant_signal": str(
                    (
                        session.get("operation_window")
                        if isinstance(session.get("operation_window"), dict)
                        else {}
                    ).get("local_incident_dominant_signal")
                    or ""
                ).strip() or None,
                "retry_depth": int(session.get("retry_depth") or 0),
                "suspected_leak": bool(session.get("suspected_leak")),
            }
        return {
            "ok": True,
            "memory": {
                "authority": str(runtime.get("authority") or "supervisor"),
                "profile_control_mode": str(runtime.get("profile_control_mode") or IMPLEMENTED_PROFILE_CONTROL_MODE),
                "current_profile_mode": str(runtime.get("current_profile_mode") or "normal"),
                "requested_profile_mode": str(runtime.get("requested_profile_mode") or "").strip() or None,
                "requested_session_id": str(runtime.get("requested_session_id") or "").strip() or None,
                "active_session_id": active_session_id,
                "last_session_id": last_session_id,
                "publish_request_session_id": str(runtime.get("publish_request_session_id") or "").strip() or None,
                "suspicion_state": str(runtime.get("suspicion_state") or "idle"),
                "suspicion_reason": str(runtime.get("suspicion_reason") or "").strip() or None,
                "baseline_family_rss_bytes": runtime.get("baseline_family_rss_bytes"),
                "rss_growth_bytes": runtime.get("rss_growth_bytes"),
                "rss_growth_bytes_per_min": runtime.get("rss_growth_bytes_per_min"),
                "available_memory_bytes": runtime.get("available_memory_bytes"),
                "available_memory_percent": runtime.get("available_memory_percent"),
                "auto_profile_min_uptime_sec": runtime.get("auto_profile_min_uptime_sec"),
                "auto_profile_last_block_reason": str(runtime.get("auto_profile_last_block_reason") or "").strip()
                or None,
                "auto_profile_last_block_at": runtime.get("auto_profile_last_block_at"),
                "critical_available_percent_threshold": runtime.get("critical_available_percent_threshold"),
                "critical_available_bytes_threshold": runtime.get("critical_available_bytes_threshold"),
                "critical_duration_sec": runtime.get("critical_duration_sec"),
                "critical_restart_cooldown_sec": runtime.get("critical_restart_cooldown_sec"),
                "critical_state": str(runtime.get("critical_state") or "normal"),
                "critical_reason": str(runtime.get("critical_reason") or "").strip() or None,
                "critical_since": runtime.get("critical_since"),
                "critical_restart_last_at": runtime.get("critical_restart_last_at"),
                "selected_profiler_adapter": str(runtime.get("selected_profiler_adapter") or DEFAULT_PROFILER_ADAPTER),
                "sessions_total": int(runtime.get("sessions_total") or 0),
                "last_session": compact_session,
                "updated_at": runtime.get("updated_at"),
            },
            "_served_by": "supervisor",
        }

    async def _request_runtime_shutdown(self, *, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict[str, Any]:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._desired_running = True
                self._persist_runtime_state()
                return {"ok": True, "accepted": False, "reason": "runtime not running"}
            try:
                headers = {"Content-Type": "application/json"}
                if self.token:
                    headers["X-AdaOS-Token"] = self.token
                response = requests.post(
                    self.runtime_base_url + "/api/admin/shutdown",
                    headers=headers,
                    json={
                        "reason": reason,
                        "drain_timeout_sec": float(drain_timeout_sec),
                        "signal_delay_sec": float(signal_delay_sec),
                    },
                    timeout=_runtime_shutdown_request_timeout(
                        drain_timeout_sec=drain_timeout_sec,
                        signal_delay_sec=signal_delay_sec,
                    ),
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
            except Exception as exc:
                self._last_error = f"shutdown request failed: {type(exc).__name__}: {exc}"
                self._persist_runtime_state()
                raise HTTPException(status_code=503, detail=f"runtime shutdown API unavailable: {type(exc).__name__}: {exc}") from exc

    async def _ensure_runtime_stopped_for_update(
        self,
        *,
        drain_timeout_sec: float,
        signal_delay_sec: float,
        reason: str,
    ) -> dict[str, Any]:
        graceful_deadline = time.time() + max(3.0, float(drain_timeout_sec) + float(signal_delay_sec) + 3.0)
        while time.time() < graceful_deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return {"ok": True, "forced": False, "reason": reason}
            await asyncio.sleep(0.2)

        forced = False
        async with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                forced = True
                with contextlib.suppress(Exception):
                    proc.terminate()
                kill_deadline = time.time() + 5.0
                while time.time() < kill_deadline:
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    final_deadline = time.time() + 5.0
                    while time.time() < final_deadline:
                        if proc.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                if proc.poll() is None:
                    raise RuntimeError(f"runtime process did not exit after forced stop: {reason}")
                self._last_error = f"forced runtime stop after shutdown timeout: {reason}"
                self._persist_runtime_state()
        return {"ok": True, "forced": forced, "reason": reason}

    def _begin_countdown_transition(self, request: dict[str, Any], *, countdown_sec: float | None = None) -> dict[str, Any]:
        countdown_value = max(0.0, float(request.get("countdown_sec") if countdown_sec is None else countdown_sec))
        status = write_core_update_status(
            {
                "state": "countdown",
                "phase": "countdown",
                "action": str(request.get("action") or "update"),
                "target_rev": str(request.get("target_rev") or ""),
                "target_version": str(request.get("target_version") or ""),
                "reason": str(request.get("reason") or ""),
                "countdown_sec": countdown_value,
                "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
                "started_at": time.time(),
                "scheduled_for": time.time() + countdown_value,
            }
        )
        request_payload = dict(request)
        request_payload["countdown_sec"] = countdown_value
        _write_update_attempt(
            _build_attempt_payload(
                action=str(request_payload.get("action") or "update"),
                request=request_payload,
                status=status,
                accepted=True,
            )
        )
        self._update_task_cancel_mode = None
        self._update_task = asyncio.create_task(
            self._countdown_update_worker(
                action=str(request_payload.get("action") or "update"),
                target_rev=str(request_payload.get("target_rev") or ""),
                target_version=str(request_payload.get("target_version") or ""),
                reason=str(request_payload.get("reason") or ""),
                countdown_sec=countdown_value,
                drain_timeout_sec=float(request_payload.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(request_payload.get("signal_delay_sec") or 0.25),
            ),
            name=f"adaos-supervisor-core-update-{request_payload.get('action') or 'update'}",
        )
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    def _begin_prepare_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        started_at = time.time()
        status = write_core_update_status(
            {
                "state": "preparing",
                "phase": "prepare",
                "action": str(request.get("action") or "update"),
                "target_rev": str(request.get("target_rev") or ""),
                "target_version": str(request.get("target_version") or ""),
                "reason": str(request.get("reason") or ""),
                "countdown_sec": float(request.get("countdown_sec") or 0.0),
                "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
                "started_at": started_at,
                "message": "preparing inactive slot before restart",
            }
        )
        attempt_payload = dict(request)
        attempt_payload.update(
            {
                "state": "active",
                "accepted": True,
                "requested_at": _epoch(request.get("requested_at")) or started_at,
                "prepare_started_at": started_at,
                "last_status": status,
                "updated_at": started_at,
            }
        )
        _write_update_attempt(attempt_payload)
        self._update_task_cancel_mode = None
        self._update_task = asyncio.create_task(
            self._prepare_and_countdown_update_worker(
                action=str(request.get("action") or "update"),
                target_rev=str(request.get("target_rev") or ""),
                target_version=str(request.get("target_version") or ""),
                reason=str(request.get("reason") or ""),
                countdown_sec=float(request.get("countdown_sec") or 0.0),
                drain_timeout_sec=float(request.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(request.get("signal_delay_sec") or 0.25),
            ),
            name=f"adaos-supervisor-core-update-prepare-{request.get('action') or 'update'}",
        )
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    def _schedule_planned_transition(
        self,
        request: dict[str, Any],
        *,
        scheduled_for: float,
        planned_reason: str,
        message: str,
        extra_status: dict[str, Any] | None = None,
        extra_attempt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        due_at = max(time.time(), float(scheduled_for))
        status_payload = {
            "state": "planned",
            "phase": "scheduled",
            "action": str(request.get("action") or "update"),
            "target_rev": str(request.get("target_rev") or ""),
            "target_version": str(request.get("target_version") or ""),
            "reason": str(request.get("reason") or ""),
            "countdown_sec": float(request.get("countdown_sec") or 0.0),
            "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
            "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
            "min_update_period_sec": _min_update_period_sec(),
            "planned_reason": planned_reason,
            "scheduled_for": due_at,
            "message": message,
        }
        if isinstance(extra_status, dict):
            status_payload.update(extra_status)
        status = write_core_update_status(status_payload)
        payload = dict(request)
        payload.update(
            {
                "state": "planned",
                "accepted": True,
                "scheduled_for": due_at,
                "planned_reason": planned_reason,
                "min_update_period_sec": _min_update_period_sec(),
                "last_status": status,
                "updated_at": time.time(),
            }
        )
        if isinstance(extra_attempt, dict):
            payload.update(extra_attempt)
        _write_update_attempt(payload)
        return {"ok": True, "accepted": True, "planned": True, "status": status, "_served_by": "supervisor"}

    def _queue_subsequent_transition(
        self,
        *,
        request: dict[str, Any],
        current_status: dict[str, Any] | None,
        current_attempt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = _epoch(request.get("requested_at")) or time.time()
        queued = dict(request)
        attempt = dict(current_attempt or {})
        if not attempt:
            attempt = {
                "state": "active",
                "action": str((current_status or {}).get("action") or request.get("action") or "update"),
                "requested_at": now,
                "updated_at": now,
                "last_status": dict(current_status or {}),
            }
        previous = _subsequent_transition_request(attempt)
        if previous:
            queued["first_requested_at"] = _epoch(previous.get("first_requested_at")) or _epoch(previous.get("requested_at")) or now
        attempt["subsequent_transition"] = True
        attempt["subsequent_transition_requested_at"] = now
        attempt["subsequent_transition_request"] = queued
        attempt["updated_at"] = now
        _write_update_attempt(attempt)

        status_payload = dict(current_status or read_core_update_status() or {})
        status_payload["subsequent_transition"] = True
        status_payload["subsequent_transition_requested_at"] = now
        status_payload["subsequent_transition_action"] = str(queued.get("action") or "update")
        status_payload["subsequent_transition_target_rev"] = str(queued.get("target_rev") or "")
        status_payload["subsequent_transition_target_version"] = str(queued.get("target_version") or "")
        status_payload["updated_at"] = time.time()
        status = write_core_update_status(status_payload)
        return {
            "ok": True,
            "accepted": True,
            "deferred": True,
            "subsequent_transition": True,
            "status": status,
            "_served_by": "supervisor",
        }

    async def _maybe_resume_or_continue_transition(self) -> None:
        payload = _reconcile_update_status(
            {
                "ok": True,
                "status": read_core_update_status(),
                "runtime": self.status(),
                "_served_by": "supervisor_monitor",
            }
        )
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else _read_update_attempt() or {}
        if self._candidate_proc is not None and not _is_transition_in_progress(status, attempt):
            await self._cleanup_candidate_runtime(reason="supervisor.candidate.idle_cleanup")
            payload = _reconcile_update_status(
                {
                    "ok": True,
                    "status": read_core_update_status(),
                    "runtime": self.status(),
                    "_served_by": "supervisor_monitor",
                }
            )
            status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
            attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else _read_update_attempt() or {}
        if self._update_task is not None and not self._update_task.done():
            return

        attempt_state = str(attempt.get("state") or "").strip().lower()
        now = time.time()
        if attempt_state == "planned":
            scheduled_for = _epoch(attempt.get("scheduled_for") or status.get("scheduled_for"))
            if scheduled_for > 0.0 and scheduled_for <= now:
                request = _request_from_attempt(attempt)
                decision = self._transition_continuity_guard_decision(
                    operation=str(request.get("action") or "update")
                )
                if decision is not None:
                    self._schedule_continuity_guarded_transition(
                        request,
                        decision,
                        current_status=status,
                        current_attempt=attempt,
                    )
                else:
                    self._begin_countdown_transition(request)
            return

        if attempt_state == "active" and str(status.get("state") or "").strip().lower() == "preparing":
            self._begin_prepare_transition(_request_from_attempt(attempt))
            return

        if attempt_state == "active" and str(status.get("state") or "").strip().lower() == "countdown":
            scheduled_for = _epoch(status.get("scheduled_for") or attempt.get("scheduled_for"))
            remaining = max(0.0, scheduled_for - now) if scheduled_for > 0.0 else float(attempt.get("countdown_sec") or 0.0)
            self._begin_countdown_transition(_request_from_attempt(attempt), countdown_sec=remaining)
            return

        if (
            _auto_update_complete_enabled()
            and _autostart_self_restart_supported()
            and not self._service_restart_pending
            and (
                _is_root_promotion_pending_status(status)
                or _is_root_restart_pending_status(status)
                or _is_root_restart_pending_attempt(attempt)
            )
        ):
            await self.complete_update(reason="supervisor.auto_update_complete", auto=True)
            return

        queued = _subsequent_transition_request(attempt)
        if queued and _is_terminal_update_status(status):
            await self._cleanup_candidate_runtime(reason="supervisor.candidate.before_subsequent_transition")
            await self.start_update(
                action=str(queued.get("action") or "update"),
                target_rev=str(queued.get("target_rev") or ""),
                target_version=str(queued.get("target_version") or ""),
                reason=str(queued.get("reason") or "subsequent.transition"),
                countdown_sec=float(queued.get("countdown_sec") or 0.0),
                drain_timeout_sec=float(queued.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(queued.get("signal_delay_sec") or 0.25),
                bypass_min_period=True,
            )

    async def _countdown_update_worker(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
    ) -> None:
        started_at = time.time()
        write_core_update_status(
            {
                "state": "countdown",
                "phase": "countdown",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "countdown_sec": countdown_sec,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
                "started_at": started_at,
                "scheduled_for": started_at + countdown_sec,
            }
        )
        try:
            await asyncio.sleep(max(0.0, float(countdown_sec)))
            plan = {
                "state": "pending_restart",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "created_at": time.time(),
                "expires_at": time.time() + 1800.0,
            }
            write_core_update_plan(plan)
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "message": "countdown completed; pending update written",
                }
            )
            shutdown_request_error: Exception | None = None
            try:
                await self._request_runtime_shutdown(
                    reason=reason,
                    drain_timeout_sec=drain_timeout_sec,
                    signal_delay_sec=signal_delay_sec,
                )
            except Exception as exc:
                shutdown_request_error = exc
            stop_result = await self._ensure_runtime_stopped_for_update(
                drain_timeout_sec=drain_timeout_sec,
                signal_delay_sec=signal_delay_sec,
                reason=reason,
            )
            if shutdown_request_error or bool(stop_result.get("forced")):
                write_core_update_status(
                    {
                        "state": "restarting",
                        "phase": "shutdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "message": (
                            "runtime shutdown API was unavailable; supervisor continued with direct process stop"
                            if shutdown_request_error and bool(stop_result.get("forced"))
                            else "runtime shutdown API response was unavailable; runtime still stopped during grace window"
                            if shutdown_request_error
                            else "runtime shutdown exceeded grace period; supervisor forced process stop"
                        ),
                        "forced_shutdown": bool(stop_result.get("forced")),
                        "shutdown_request_error_type": (
                            type(shutdown_request_error).__name__ if shutdown_request_error is not None else None
                        ),
                        "shutdown_request_error": str(shutdown_request_error) if shutdown_request_error is not None else None,
                    }
                )
        except asyncio.CancelledError:
            clear_core_update_plan()
            cancel_mode = str(self._update_task_cancel_mode or "").strip().lower()
            self._update_task_cancel_mode = None
            if cancel_mode != "rescheduled":
                status = write_core_update_status(
                    {
                        "state": "cancelled",
                        "phase": "countdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "message": "core update cancelled",
                    }
                )
                _complete_update_attempt(state="cancelled", status=status, reason=reason)
            raise
        except Exception as exc:
            clear_core_update_plan()
            status = write_core_update_status(
                {
                    "state": "failed",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "message": "failed to request runtime shutdown for pending core update",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "updated_at": time.time(),
                }
            )
            _complete_update_attempt(
                state="failed",
                status=status,
                reason=f"shutdown request failed: {type(exc).__name__}",
            )
        finally:
            self._update_task_cancel_mode = None
            if self._update_task is not None and self._update_task.done():
                self._update_task = None

    async def _prepare_and_countdown_update_worker(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
    ) -> None:
        cancel_phase = "prepare"
        failure_phase = "prepare"
        target_slot = ""
        manifest: dict[str, Any] | None = None
        candidate_prewarm_state = "skipped"
        candidate_prewarm_message = ""
        candidate_prewarm_ready_at = None
        candidate_launch_state = "skipped"
        candidate_launch_message = ""
        used_candidate_cutover = False
        prepare_result = prepare_pending_update(
            {
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
            }
        )
        try:
            if str(prepare_result.get("state") or "").strip().lower() != "prepared":
                status = write_core_update_status(
                    {
                        **dict(prepare_result),
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                    }
                )
                _complete_update_attempt(
                    state="failed",
                    status=status,
                    reason=str(prepare_result.get("message") or "prepare failed"),
                )
                return

            prepared_plan = prepare_result.get("plan") if isinstance(prepare_result.get("plan"), dict) else {}
            target_slot = str(
                prepare_result.get("target_slot")
                or prepared_plan.get("target_slot")
                or choose_inactive_slot()
                or ""
            ).strip().upper()
            manifest = prepare_result.get("manifest") if isinstance(prepare_result.get("manifest"), dict) else None
            try:
                candidate_prewarm = await self._candidate_prewarm(target_slot=target_slot)
            except Exception as exc:
                candidate_prewarm = {
                    "attempted": True,
                    "state": "failed",
                    "message": f"candidate prewarm failed: {type(exc).__name__}: {exc}",
                }
            candidate_prewarm_state = str(candidate_prewarm.get("state") or "").strip().lower() or "skipped"
            candidate_prewarm_message = str(candidate_prewarm.get("message") or "").strip()
            candidate_prewarm_ready_at = candidate_prewarm.get("ready_at")
            countdown_started_at = time.time()
            status = write_core_update_status(
                {
                    "state": "countdown",
                    "phase": "countdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "countdown_sec": countdown_sec,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "started_at": countdown_started_at,
                    "scheduled_for": countdown_started_at + countdown_sec,
                    "prepared_at": float(prepare_result.get("finished_at") or countdown_started_at),
                    "target_slot": target_slot,
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": (
                        (
                            f"slot {target_slot} prepared; passive candidate ready; countdown started"
                            if candidate_prewarm_state == "ready"
                            else f"slot {target_slot} prepared; passive candidate warming; countdown started"
                            if candidate_prewarm_state == "starting"
                            else f"slot {target_slot} prepared; passive candidate prewarm failed; countdown started"
                            if candidate_prewarm_state == "failed"
                            else f"slot {target_slot} prepared; countdown started"
                        )
                        if target_slot
                        else "inactive slot prepared; countdown started"
                    ),
                    "manifest": manifest,
                }
            )
            _write_update_attempt(
                _build_attempt_payload(
                    action=action,
                    request={
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "countdown_sec": countdown_sec,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message,
                    },
                    status=status,
                    accepted=True,
                )
            )
            cancel_phase = "countdown"
            await asyncio.sleep(max(0.0, float(countdown_sec)))

            plan = {
                "state": "prepared_restart",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "target_slot": target_slot,
                "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                "created_at": time.time(),
                "expires_at": time.time() + 1800.0,
            }
            write_core_update_plan(plan)
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "target_slot": target_slot,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": "countdown completed; prepared restart written",
                    "manifest": manifest,
                }
            )
            failure_phase = "shutdown"
            shutdown_request_error: Exception | None = None
            try:
                await self._request_runtime_shutdown(
                    reason=reason,
                    drain_timeout_sec=drain_timeout_sec,
                    signal_delay_sec=signal_delay_sec,
                )
            except Exception as exc:
                shutdown_request_error = exc
            async with self._lock:
                self._desired_running = False
                self._persist_runtime_state()
            stop_result = await self._ensure_runtime_stopped_for_update(
                drain_timeout_sec=drain_timeout_sec,
                signal_delay_sec=signal_delay_sec,
                reason=reason,
            )
            if shutdown_request_error or bool(stop_result.get("forced")):
                write_core_update_status(
                    {
                        "state": "restarting",
                        "phase": "shutdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "target_slot": target_slot,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message or None,
                        "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                        "message": (
                            "runtime shutdown API was unavailable; supervisor continued with direct process stop"
                            if shutdown_request_error and bool(stop_result.get("forced"))
                            else "runtime shutdown API response was unavailable; runtime still stopped during grace window"
                            if shutdown_request_error
                            else "runtime shutdown exceeded grace period; supervisor forced process stop"
                        ),
                        "forced_shutdown": bool(stop_result.get("forced")),
                        "shutdown_request_error_type": (
                            type(shutdown_request_error).__name__ if shutdown_request_error is not None else None
                        ),
                        "shutdown_request_error": str(shutdown_request_error) if shutdown_request_error is not None else None,
                        "manifest": manifest,
                    }
                )
            activate_slot(target_slot)
            candidate_cleanup: dict[str, Any] | None = None
            candidate_launch_state = candidate_prewarm_state
            candidate_launch_message = candidate_prewarm_message
            if candidate_prewarm_state == "ready":
                try:
                    await self._promote_candidate_runtime(
                        slot=target_slot,
                        reason="supervisor.fast_cutover",
                    )
                    used_candidate_cutover = True
                    candidate_launch_state = "promoted_to_active"
                    candidate_launch_message = (
                        "passive candidate runtime promoted to active via warm-switch cutover"
                    )
                except Exception as exc:
                    candidate_cleanup = await self._cleanup_candidate_runtime(
                        reason="supervisor.candidate.cutover_fallback",
                        slot=target_slot,
                    )
                    candidate_launch_state = "cutover_fallback"
                    candidate_launch_message = (
                        f"warm-switch cutover fallback: {type(exc).__name__}: {exc}"
                    )
            elif candidate_prewarm_state != "skipped":
                candidate_cleanup = await self._cleanup_candidate_runtime(
                    reason="supervisor.candidate.stop_before_active_launch",
                    slot=target_slot,
                )
                if bool((candidate_cleanup or {}).get("stopped")):
                    candidate_launch_state = "stopped_for_launch"
                    candidate_launch_message = "passive candidate runtime stopped before active launch"
            failure_phase = "launch"
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "launch",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "target_slot": target_slot,
                    "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                    "candidate_prewarm_state": candidate_launch_state,
                    "candidate_prewarm_message": candidate_launch_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": (
                        f"prepared slot {target_slot} activated via warm-switch cutover; awaiting validation"
                        if used_candidate_cutover and target_slot
                        else "prepared slot activated via warm-switch cutover; awaiting validation"
                        if used_candidate_cutover
                        else f"prepared slot {target_slot} activated; awaiting runtime launch"
                        if target_slot
                        else "prepared slot activated; awaiting runtime launch"
                    ),
                    "manifest": manifest,
                }
            )
            async with self._lock:
                self._desired_running = True
                self._persist_runtime_state()
        except asyncio.CancelledError:
            clear_core_update_plan()
            await self._cleanup_candidate_runtime(
                reason="supervisor.candidate.cancelled_transition",
                slot=target_slot or None,
            )
            cancel_mode = str(self._update_task_cancel_mode or "").strip().lower()
            self._update_task_cancel_mode = None
            if cancel_mode != "rescheduled":
                status = write_core_update_status(
                    {
                        "state": "cancelled",
                        "phase": cancel_phase,
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message or None,
                        "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                        "message": "core update cancelled",
                    }
                )
                _complete_update_attempt(state="cancelled", status=status, reason=reason)
            raise
        except Exception as exc:
            clear_core_update_plan()
            await self._cleanup_candidate_runtime(
                reason="supervisor.candidate.failed_transition",
                slot=target_slot or None,
            )
            async with self._lock:
                self._desired_running = True
                self._persist_runtime_state()
            status = write_core_update_status(
                {
                    "state": "failed",
                    "phase": failure_phase,
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": "prepared core update transition failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "updated_at": time.time(),
                }
            )
            _complete_update_attempt(
                state="failed",
                status=status,
                reason=f"prepared transition failed: {type(exc).__name__}",
            )
        finally:
            self._update_task_cancel_mode = None
            if self._update_task is not None and self._update_task.done():
                self._update_task = None

    async def start_update(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
        bypass_min_period: bool = False,
    ) -> dict[str, Any]:
        request = _transition_request_payload(
            action=action,
            target_rev=target_rev,
            target_version=target_version,
            reason=reason,
            countdown_sec=countdown_sec,
            drain_timeout_sec=drain_timeout_sec,
            signal_delay_sec=signal_delay_sec,
        )
        disabled_reason = core_update_reactions_disabled_reason()
        if disabled_reason:
            return {
                "ok": True,
                "accepted": False,
                "skipped": True,
                "reason": disabled_reason,
                "status": read_core_update_status(),
                "_served_by": "supervisor",
            }
        current_status = read_core_update_status()
        current_attempt = _read_update_attempt()
        if _is_transition_in_progress(current_status, current_attempt):
            return self._queue_subsequent_transition(
                request=request,
                current_status=current_status,
                current_attempt=current_attempt,
            )

        decision = self._transition_continuity_guard_decision(operation=action)
        if decision is not None:
            return self._schedule_continuity_guarded_transition(
                request,
                decision,
                current_status=current_status,
                current_attempt=current_attempt,
            )

        if str((current_attempt or {}).get("state") or "").strip().lower() == "planned" and action == "update":
            scheduled_for = _epoch((current_attempt or {}).get("scheduled_for") or current_status.get("scheduled_for")) or time.time()
            return self._schedule_planned_transition(
                request=request,
                scheduled_for=scheduled_for,
                planned_reason=str((current_attempt or {}).get("planned_reason") or "minimum_update_period"),
                message="planned core update refreshed while waiting for scheduled window",
            )

        if action == "update" and not bypass_min_period:
            min_period_sec = _min_update_period_sec()
            last_completed_at = _last_update_completion_at(current_status, current_attempt)
            next_allowed_at = last_completed_at + min_period_sec
            if min_period_sec > 0.0 and last_completed_at > 0.0 and next_allowed_at > time.time():
                return self._schedule_planned_transition(
                    request=request,
                    scheduled_for=next_allowed_at,
                    planned_reason="minimum_update_period",
                    message="core update deferred until minimum update interval elapses",
                )

        clear_core_update_plan()
        if action == "update":
            return self._begin_prepare_transition(request)
        return self._begin_countdown_transition(request)

    async def cancel_update(self, *, reason: str) -> dict[str, Any]:
        task = self._update_task
        clear_core_update_plan()
        current_attempt = _read_update_attempt() or {}
        current_status = read_core_update_status()
        if str(current_attempt.get("state") or "").strip().lower() == "planned":
            status = write_core_update_status(
                {
                    "state": "cancelled",
                    "phase": "scheduled",
                    "action": str(current_status.get("action") or current_attempt.get("action") or "update"),
                    "message": "planned core update cancelled by request",
                    "reason": reason,
                }
            )
            _complete_update_attempt(state="cancelled", status=status, reason=reason)
            return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

        if task is None or task.done():
            current_phase = str(current_status.get("phase") or "").strip().lower() or "countdown"
            status = write_core_update_status(
                {
                    "state": "cancelled",
                    "phase": current_phase,
                    "message": "no pending countdown task",
                    "reason": reason,
                }
            )
            _complete_update_attempt(state="cancelled", status=status, reason=reason)
            self._update_task = None
            return {"ok": True, "accepted": False, "status": status, "_served_by": "supervisor"}

        self._update_task_cancel_mode = "cancelled"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._update_task = None
        current_phase = str((read_core_update_status() or {}).get("phase") or "").strip().lower() or "countdown"
        status = write_core_update_status(
            {
                "state": "cancelled",
                "phase": current_phase,
                "action": str((read_core_update_status() or {}).get("action") or "update"),
                "message": "core update cancelled by request",
                "reason": reason,
                "drain_timeout_sec": float((read_core_update_status() or {}).get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float((read_core_update_status() or {}).get("signal_delay_sec") or 0.25),
            }
        )
        _complete_update_attempt(state="cancelled", status=status, reason=reason)
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    async def defer_update(self, *, delay_sec: float, reason: str) -> dict[str, Any]:
        delay_value = max(0.0, float(delay_sec))
        current_attempt = _read_update_attempt() or {}
        current_status = read_core_update_status()
        attempt_state = str(current_attempt.get("state") or "").strip().lower()
        status_state = str(current_status.get("state") or "").strip().lower()
        if attempt_state not in {"planned", "active"} and status_state not in {"planned", "countdown"}:
            raise HTTPException(status_code=409, detail="defer requires a planned update or active countdown")

        if self._update_task is not None and not self._update_task.done():
            self._update_task_cancel_mode = "rescheduled"
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None

        request = _request_from_attempt(current_attempt or current_status)
        scheduled_for = time.time() + delay_value
        return self._schedule_planned_transition(
            request=request,
            scheduled_for=scheduled_for,
            planned_reason="operator_defer",
            message="core update deferred by request",
        )

    async def promote_root(self, *, reason: str) -> dict[str, Any]:
        current_status = read_core_update_status()
        state = str(current_status.get("state") or "").strip().lower()
        phase = str(current_status.get("phase") or "").strip().lower()
        manifest = active_slot_manifest()
        root_promotion_required, bootstrap_update = resolved_root_promotion_requirement(manifest)
        if state not in {"validated", "succeeded"} and phase != "root_promotion_pending" and not root_promotion_required:
            raise HTTPException(status_code=409, detail="root promotion requires a validated slot runtime")
        if not root_promotion_required:
            status = write_core_update_status(
                {
                    "state": "succeeded",
                    "phase": "validate",
                    "message": "no root promotion required for the active slot",
                    "target_slot": str((manifest or {}).get("slot") or active_slot() or ""),
                    "manifest": manifest,
                    "root_promotion_required": False,
                    "bootstrap_update": bootstrap_update,
                    "scheduled_for": None,
                    "candidate_prewarm_state": None,
                    "candidate_prewarm_message": None,
                    "candidate_prewarm_ready_at": None,
                    "finished_at": time.time(),
                }
            )
            _complete_update_attempt(state="completed", status=status, reason=reason)
            return {"ok": True, "accepted": False, "status": status, "_served_by": "supervisor"}
        promotion = promote_root_from_slot(slot=str((manifest or {}).get("slot") or active_slot() or ""))
        status = write_core_update_status(
            {
                "state": "succeeded",
                "phase": "root_promoted",
                "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
                "target_slot": str((manifest or {}).get("slot") or active_slot() or ""),
                "manifest": manifest,
                "root_promotion_required": False,
                "bootstrap_update": bootstrap_update,
                "root_promotion": promotion,
                "promotion_reason": reason,
                "finished_at": time.time(),
            }
        )
        previous_attempt = _read_update_attempt() or {}
        now = time.time()
        awaiting_attempt = dict(previous_attempt)
        awaiting_attempt.update(
            {
                "state": "awaiting_root_restart",
                "action": str(previous_attempt.get("action") or "update"),
                "accepted": True,
                "awaiting_restart": True,
                "restart_required": True,
                "requested_at": _epoch(previous_attempt.get("requested_at")) or now,
                "transitioned_at": now,
                "updated_at": now,
                "completion_reason": "",
                "last_status": status,
            }
        )
        _write_update_attempt(awaiting_attempt)
        return {"ok": True, "accepted": True, "status": status, "root_promotion": promotion, "_served_by": "supervisor"}

    def proxy_update_post(self, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        try:
            response = requests.post(
                self.runtime_base_url + path,
                headers=headers,
                json=body,
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            status = payload.get("status") if isinstance(payload, dict) and isinstance(payload.get("status"), dict) else {}
            accepted = bool(payload.get("accepted", True)) if isinstance(payload, dict) else True
            if path.endswith("/update/start"):
                _write_update_attempt(
                    _build_attempt_payload(action="update", request=body, status=status, accepted=accepted)
                )
            elif path.endswith("/update/rollback"):
                _write_update_attempt(
                    _build_attempt_payload(action="rollback", request=body, status=status, accepted=accepted)
                )
            elif path.endswith("/update/cancel"):
                _complete_update_attempt(state="cancelled", status=status, reason=str(body.get("reason") or "cancelled"))
            if isinstance(payload, dict):
                payload["_served_by"] = "runtime"
                return payload
            return {"ok": True, "response": payload, "_served_by": "runtime"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"runtime admin API unavailable: {type(exc).__name__}: {exc}") from exc


init_ctx()
_CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_CORS_ALLOW_HEADERS = ["*"]


app = FastAPI(title="AdaOS Supervisor")


@app.on_event("startup")
async def _startup() -> None:
    args = _parse_args()
    manager = SupervisorManager(runtime_host=args.host, runtime_port=args.port, token=_resolved_token(args.token))
    app.state.manager = manager
    await manager.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    manager = getattr(app.state, "manager", None)
    if manager is not None:
        await manager.close()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=_CORS_ALLOW_METHODS,
    allow_headers=_CORS_ALLOW_HEADERS,
    allow_credentials=False,
)


@app.middleware("http")
async def private_network_access_middleware(request: Request, call_next):
    origin = str(request.headers.get("origin") or "").strip()
    requested_method = str(request.headers.get("access-control-request-method") or "").strip().upper()
    requested_headers = str(request.headers.get("access-control-request-headers") or "").strip()
    requested_private_network = str(request.headers.get("access-control-request-private-network") or "").strip().lower()
    if (
        origin
        and request.method.upper() == "OPTIONS"
        and requested_method
        and requested_private_network == "true"
    ):
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = requested_method
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        if requested_headers:
            response.headers["Access-Control-Allow-Headers"] = requested_headers
        response.headers["Vary"] = "Origin"
        return response

    response = await call_next(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Vary"] = "Origin"
    return response


def _manager() -> SupervisorManager:
    manager = getattr(app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="supervisor is not initialized")
    return manager


@app.get("/api/ping")
async def ping() -> dict[str, Any]:
    return {"ok": True, "ts": time.time(), "service": "adaos-supervisor"}


@app.get("/api/supervisor/status", dependencies=[Depends(require_token)])
async def supervisor_status() -> dict[str, Any]:
    return _manager().status()


@app.get("/api/supervisor/memory/status", dependencies=[Depends(require_token)])
async def supervisor_memory_status() -> dict[str, Any]:
    return _manager().memory_status()


@app.get("/api/supervisor/memory/telemetry", dependencies=[Depends(require_token)])
async def supervisor_memory_telemetry(limit: int = 100) -> dict[str, Any]:
    return _manager().memory_telemetry(limit=limit)


@app.get("/api/supervisor/public/memory-status")
async def supervisor_public_memory_status() -> dict[str, Any]:
    return _manager().public_memory_status()


@app.get("/api/supervisor/memory/sessions", dependencies=[Depends(require_token)])
async def supervisor_memory_sessions(limit: int = 100) -> dict[str, Any]:
    manager = _manager()
    try:
        return manager.memory_sessions(limit=limit)
    except TypeError:
        return manager.memory_sessions()


@app.get("/api/supervisor/memory/incidents", dependencies=[Depends(require_token)])
async def supervisor_memory_incidents(limit: int = 50) -> dict[str, Any]:
    return _manager().memory_incidents(limit=limit)


@app.get("/api/supervisor/memory/sessions/{session_id}", dependencies=[Depends(require_token)])
async def supervisor_memory_session(session_id: str) -> dict[str, Any]:
    payload = _manager().memory_session(session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="memory profiling session was not found")
    return payload


@app.get("/api/supervisor/memory/sessions/{session_id}/artifacts/{artifact_id}", dependencies=[Depends(require_token)])
async def supervisor_memory_session_artifact(
    session_id: str,
    artifact_id: str,
    offset: int = 0,
    max_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    manager = _manager()
    if hasattr(manager, "memory_session_artifact_chunk"):
        payload = manager.memory_session_artifact_chunk(
            session_id,
            artifact_id,
            offset=offset,
            max_bytes=max_bytes,
        )
    else:
        payload = manager.memory_session_artifact(session_id, artifact_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="memory profiling artifact was not found")
    return payload


@app.post("/api/supervisor/memory/profile/start", dependencies=[Depends(require_token)])
async def supervisor_memory_profile_start(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    return _manager().start_memory_profile(
        profile_mode=str(body.get("profile_mode") or "sampled_profile"),
        reason=str(body.get("reason") or "operator.request"),
        trigger_source=str(body.get("trigger_source") or "operator"),
    )


@app.post("/api/supervisor/memory/profile/{session_id}/stop", dependencies=[Depends(require_token)])
async def supervisor_memory_profile_stop(session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    return _manager().stop_memory_profile(session_id, reason=str(body.get("reason") or "operator.stop"))


@app.post("/api/supervisor/memory/profile/{session_id}/retry", dependencies=[Depends(require_token)])
async def supervisor_memory_profile_retry(session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    return _manager().retry_memory_profile(session_id, reason=str(body.get("reason") or "operator.retry"))


@app.post("/api/supervisor/memory/publish", dependencies=[Depends(require_token)])
async def supervisor_memory_publish(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    return _manager().publish_memory_profile(session_id, reason=str(body.get("reason") or "operator.publish"))


@app.get("/api/supervisor/sidecar/status", dependencies=[Depends(require_token)])
async def supervisor_sidecar_status() -> dict[str, Any]:
    return _manager().sidecar_status()


@app.post("/api/supervisor/runtime/restart", dependencies=[Depends(require_token)])
async def supervisor_runtime_restart(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    status = await _manager().restart_runtime(reason=str(body.get("reason") or "supervisor.restart"))
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/runtime/candidate/start", dependencies=[Depends(require_token)])
async def supervisor_runtime_candidate_start(payload: dict[str, Any]) -> dict[str, Any]:
    status = await _manager().start_candidate_runtime(
        slot=str(payload.get("slot") or "").strip().upper() or None,
        reason=str(payload.get("reason") or "supervisor.candidate.start"),
    )
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/runtime/candidate/stop", dependencies=[Depends(require_token)])
async def supervisor_runtime_candidate_stop(payload: dict[str, Any]) -> dict[str, Any]:
    status = await _manager().stop_candidate_runtime(
        reason=str(payload.get("reason") or "supervisor.candidate.stop")
    )
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/sidecar/restart", dependencies=[Depends(require_token)])
async def supervisor_sidecar_restart(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().restart_sidecar(reconnect_hub_root=bool(payload.get("reconnect_hub_root")))


@app.get("/api/supervisor/update/status", dependencies=[Depends(require_token)])
async def supervisor_update_status() -> dict[str, Any]:
    return _manager().supervisor_update_status()


@app.get("/api/supervisor/public/update-status")
async def supervisor_public_update_status() -> dict[str, Any]:
    return _manager().public_update_status()


@app.post("/api/supervisor/update/start", dependencies=[Depends(require_token)])
async def supervisor_update_start(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().start_update(
        action="update",
        target_rev=str(payload.get("target_rev") or ""),
        target_version=str(payload.get("target_version") or ""),
        reason=str(payload.get("reason") or "core.update"),
        countdown_sec=float(payload.get("countdown_sec") or 60.0),
        drain_timeout_sec=float(payload.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(payload.get("signal_delay_sec") or 0.25),
    )


@app.post("/api/supervisor/update/cancel", dependencies=[Depends(require_token)])
async def supervisor_update_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().cancel_update(reason=str(payload.get("reason") or "user.cancelled"))


@app.post("/api/supervisor/update/defer", dependencies=[Depends(require_token)])
async def supervisor_update_defer(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().defer_update(
        delay_sec=float(payload.get("delay_sec") or payload.get("countdown_sec") or 300.0),
        reason=str(payload.get("reason") or "user.deferred"),
    )


@app.post("/api/supervisor/update/rollback", dependencies=[Depends(require_token)])
async def supervisor_update_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().start_update(
        action="rollback",
        target_rev="",
        target_version="",
        reason=str(payload.get("reason") or "core.rollback"),
        countdown_sec=float(payload.get("countdown_sec") or 0.0),
        drain_timeout_sec=float(payload.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(payload.get("signal_delay_sec") or 0.25),
    )


@app.post("/api/supervisor/update/promote-root", dependencies=[Depends(require_token)])
async def supervisor_update_promote_root(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().promote_root(reason=str(payload.get("reason") or "core.root_promotion"))


@app.post("/api/supervisor/update/complete", dependencies=[Depends(require_token)])
async def supervisor_update_complete(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().complete_update(reason=str(payload.get("reason") or "core.update.complete"))


def main() -> None:
    args = _parse_args()
    if args.token:
        os.environ["ADAOS_TOKEN"] = str(args.token)
    os.environ["ADAOS_SUPERVISOR_ENABLED"] = "1"
    os.environ["ADAOS_SUPERVISOR_URL"] = _supervisor_base_url()
    uvicorn.run(
        app,
        host=_supervisor_host(),
        port=_supervisor_port(),
        loop=_uvicorn_loop_mode(),
        reload=False,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
