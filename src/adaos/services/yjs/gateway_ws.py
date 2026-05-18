from __future__ import annotations

"""
Yjs websocket gateway implementation (service layer).
"""

import asyncio
from collections import deque
import gc
import hashlib
import inspect
import json
import time
import logging
import threading
import os
from typing import TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from typing import Awaitable, Callable

from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

try:
    from ypy_websocket.websocket import Websocket as YWebsocket
    from ypy_websocket.websocket_server import WebsocketServer
    from ypy_websocket.yroom import YRoom
    from ypy_websocket.yutils import create_update_message
except ImportError as exc:  # pragma: no cover - import guard for dev envs
    raise RuntimeError("ypy_websocket is required for AdaOS realtime collaboration. " "Install dependencies via `pip install -e .[dev]` or `pip install ypy-websocket`.") from exc

from adaos.services.workspaces import ensure_workspace, get_workspace
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.observers import attach_room_observers, forget_room_observers
from adaos.services.yjs.store import evict_ystore_for_webspace, get_ystore_for_webspace, ystore_write_metadata_sync
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.update_origin import consume_backend_room_update
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.scheduler import get_scheduler
from adaos.domain import Event as DomainEvent
from adaos.services.agent_context import get_ctx as get_agent_ctx

router = APIRouter()
_log = logging.getLogger("adaos.events_ws")
_ylog = logging.getLogger("adaos.yjs.gateway")
_TRANSPORT_LOCK = threading.RLock()
_ACTIVE_YWS_LOCK = threading.RLock()
_YWS_STORM_LOCK = threading.RLock()
_TRANSPORT_STATE: dict[str, dict[str, Any]] = {
    "ws": {
        "active_connections": 0,
        "open_total": 0,
        "close_total": 0,
        "last_open_at": 0.0,
        "last_close_at": 0.0,
    },
    "yws": {
        "active_connections": 0,
        "open_total": 0,
        "close_total": 0,
        "last_open_at": 0.0,
        "last_close_at": 0.0,
    },
}
_ACTIVE_YWS_CONNECTIONS: dict[str, list[WebSocket]] = {}
_ACTIVE_YWS_CLIENTS: dict[str, dict[str, int]] = {}
_YWS_OPEN_HISTORY: deque[float] = deque(maxlen=512)
_YWS_CLIENT_OPEN_HISTORY: dict[str, deque[float]] = {}
_YWS_GUARD_QUARANTINE_UNTIL: dict[str, float] = {}
_YWS_GUARD_LAST_LOG_AT: dict[str, float] = {}
_YWS_GUARD_LAST_NOTIFY_AT: dict[str, float] = {}
_YWS_GUARD_INCIDENTS: dict[str, dict[str, float]] = {}
_YWS_GUARD_DIAG: dict[str, Any] = {
    "reject_total": 0,
    "last_reject_at": 0.0,
    "last_reject_reason": "",
    "last_reject_webspace_id": "",
    "last_reject_dev_id": "",
}
_YROOM_LIFECYCLE_LOCK = threading.RLock()
_YROOM_LIFECYCLE: dict[str, dict[str, Any]] = {}
_WS_EVENT_SUBSCRIPTIONS_LOCK = threading.RLock()
_WS_EVENT_SUBSCRIBERS: dict[int, dict[str, Any]] = {}
_WS_EVENT_FORWARDER_INSTALLED = False
_COMMAND_TRACE_LOCK = threading.RLock()
_COMMAND_TRACE_HISTORY: deque[dict[str, Any]] = deque(maxlen=128)
_COMMAND_TRACE_STATS: dict[str, int] = {
    "reload_total": 0,
    "reload_duplicate_total": 0,
    "reset_total": 0,
    "reset_duplicate_total": 0,
}
_COMMAND_TRACE_SEQ = 0
_IDLE_ROOM_RESET_TASKS: dict[str, asyncio.Task[None]] = {}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _coerce_gateway_webspace_id(value: Any) -> str:
    raw = str(value or "").strip()
    default_id = default_webspace_id()
    # Older browser builds persisted "default"; route them to the runtime default.
    if not raw or raw == "default":
        return default_id
    return raw


def _clean_browser_metadata_value(value: Any, *, max_len: int = 256) -> str | None:
    token = str(value or "").strip()
    if not token:
        return None
    return token[:max_len]


def _browser_session_metadata(params: Dict[str, str]) -> dict[str, str]:
    raw: dict[str, Any] = {
        "browser_family": params.get("browser_family") or params.get("browserFamily") or params.get("browser"),
        "os_name": params.get("os_name") or params.get("osName") or params.get("os") or params.get("platform"),
        "form_factor": params.get("form_factor") or params.get("formFactor") or params.get("form"),
        "user_agent": params.get("user_agent") or params.get("userAgent") or params.get("ua"),
    }
    out: dict[str, str] = {}
    for key, value in raw.items():
        cleaned = _clean_browser_metadata_value(value, max_len=512 if key == "user_agent" else 96)
        if cleaned:
            out[key] = cleaned
    return out


def _browser_auth_response_payload(
    *,
    dev_id: str,
    webspace_id: str,
    allowed: bool,
    reason: str | None,
) -> dict[str, Any]:
    reason_token = str(reason or "").strip().lower() or None
    payload: dict[str, Any] = {
        "ok": True,
        "kind": "browser",
        "device_id": str(dev_id or "").strip(),
        "webspace_id": _coerce_gateway_webspace_id(webspace_id),
        "allowed": bool(allowed),
        "reason": reason_token,
        "next": "continue" if allowed else "login",
        "terminal": not bool(allowed),
    }
    if reason_token:
        payload["connection_state"] = reason_token
    return payload


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


_IDLE_ROOM_EVICT_SEC = _env_float("ADAOS_YJS_IDLE_ROOM_EVICT_SEC", 60.0, minimum=0.0)
_YROOM_DIAG_ENABLED = _env_flag("ADAOS_YJS_ROOM_DIAG_ENABLED", True)
_YWS_ROOM_READY_TIMEOUT_S = _env_float("ADAOS_YWS_ROOM_READY_TIMEOUT_S", 12.0, minimum=0.0)
_YWS_ROOM_READY_MAX_S = _env_float("ADAOS_YWS_ROOM_READY_MAX_S", 45.0, minimum=0.0)
_YWS_ROOM_READY_POLL_S = _env_float("ADAOS_YWS_ROOM_READY_POLL_S", 1.0, minimum=0.25)
_YWS_ROOM_BOOTSTRAP_STEP_TIMEOUT_S = _env_float("ADAOS_YWS_ROOM_BOOTSTRAP_STEP_TIMEOUT_S", 20.0, minimum=0.0)
_YWS_ROOM_STALE_RECOVERY_TIMEOUT_S = _env_float("ADAOS_YWS_ROOM_STALE_RECOVERY_TIMEOUT_S", 3.0, minimum=0.25)
_YWS_FIRST_MESSAGE_TIMEOUT_S = _env_float("ADAOS_YWS_FIRST_MESSAGE_TIMEOUT_S", 12.0, minimum=0.0)
_YWS_MAX_ACTIVE_PER_WEBSPACE = _env_int("ADAOS_YWS_MAX_ACTIVE_PER_WEBSPACE", 6, minimum=1)
_YWS_MAX_ACTIVE_PER_CLIENT = _env_int("ADAOS_YWS_MAX_ACTIVE_PER_CLIENT", 1, minimum=1)
_YWS_GUARD_RECENT_OPEN_10S = _env_int("ADAOS_YWS_GUARD_RECENT_OPEN_10S", 8, minimum=1)
_YWS_GUARD_CLIENT_OPEN_15S = _env_int("ADAOS_YWS_GUARD_CLIENT_OPEN_15S", 4, minimum=1)
_YWS_GUARD_COOLDOWN_S = _env_float("ADAOS_YWS_GUARD_COOLDOWN_S", 300.0, minimum=0.0)
_YWS_GUARD_MAX_COOLDOWN_S = _env_float("ADAOS_YWS_GUARD_MAX_COOLDOWN_S", 1800.0, minimum=0.0)
_YWS_GUARD_ESCALATION_WINDOW_S = _env_float("ADAOS_YWS_GUARD_ESCALATION_WINDOW_S", 3600.0, minimum=1.0)
_YWS_GUARD_NOTIFY_INTERVAL_S = _env_float("ADAOS_YWS_GUARD_NOTIFY_INTERVAL_S", 30.0, minimum=1.0)
_YROOM_DIAG_LOG_INTERVAL_SEC = _env_float("ADAOS_YJS_ROOM_DIAG_LOG_INTERVAL_SEC", 5.0, minimum=0.0)
_YROOM_DIAG_BUFFER_WARN = _env_int("ADAOS_YJS_ROOM_DIAG_BUFFER_WARN", 32, minimum=1)
_YROOM_DIAG_PENDING_WARN = _env_int("ADAOS_YJS_ROOM_DIAG_PENDING_WARN", 32, minimum=1)
_YROOM_DIAG_UPDATE_WARN_BYTES = _env_int("ADAOS_YJS_ROOM_DIAG_UPDATE_WARN_BYTES", 256 * 1024, minimum=1)
_YROOM_INBOUND_GUARD_BLOCK_BYTES = _env_int("ADAOS_YJS_ROOM_INBOUND_GUARD_BLOCK_BYTES", 4 * 1024 * 1024, minimum=1)
_YROOM_INBOUND_GUARD_RESET_COOLDOWN_SEC = _env_float("ADAOS_YJS_ROOM_INBOUND_GUARD_RESET_COOLDOWN_SEC", 5.0, minimum=0.0)
_YROOM_DIAG_INCLUDE_YSTORE = _env_flag("ADAOS_YJS_ROOM_DIAG_INCLUDE_YSTORE", False)
_YROOM_EFFECTIVE_GUARD_FULL_CHECK_INTERVAL_SEC = _env_float("ADAOS_YJS_EFFECTIVE_GUARD_FULL_CHECK_INTERVAL_SEC", 120.0, minimum=0.0)
_YROOM_EFFECTIVE_GUARD_FULL_CHECK_BYTES = _env_int("ADAOS_YJS_EFFECTIVE_GUARD_FULL_CHECK_BYTES", 64 * 1024 * 1024, minimum=1)
_YROOM_EFFECTIVE_GUARD_MIN_CHECK_INTERVAL_SEC = _env_float("ADAOS_YJS_EFFECTIVE_GUARD_MIN_CHECK_INTERVAL_SEC", 1.0, minimum=0.0)
_YROOM_EFFECTIVE_GUARD_TOP_LEVEL_CHECKS = _env_flag("ADAOS_YJS_EFFECTIVE_GUARD_TOP_LEVEL_CHECKS", True)
_YROOM_EFFECTIVE_GUARD_SNAPSHOT_HASHES = _env_flag("ADAOS_YJS_EFFECTIVE_GUARD_SNAPSHOT_HASHES", False)
_YROOM_EFFECTIVE_GUARD_SNAPSHOT_DETAILS = _env_flag("ADAOS_YJS_EFFECTIVE_GUARD_SNAPSHOT_DETAILS", False)
_YROOM_EFFECTIVE_GUARD_STRICT_FULL_CHECKS = _env_flag("ADAOS_YJS_EFFECTIVE_GUARD_STRICT_FULL_CHECKS", False)
_YROOM_EFFECTIVE_GUARD_REPAIR_INITIAL_UPDATES = _env_int(
    "ADAOS_YJS_EFFECTIVE_GUARD_REPAIR_INITIAL_UPDATES",
    8,
    minimum=0,
)
_YROOM_EFFECTIVE_GUARD_REPAIR_COOLDOWN_SEC = _env_float(
    "ADAOS_YJS_EFFECTIVE_GUARD_REPAIR_COOLDOWN_SEC",
    0.25,
    minimum=0.0,
)
_YROOM_AUTHORITATIVE_SELECTOR_LEASE_SEC = _env_float(
    "ADAOS_YJS_AUTHORITATIVE_SELECTOR_LEASE_SEC",
    30.0,
    minimum=0.0,
)
_EMPTY_Y_UPDATE = b"\x00\x00"
_YROOM_INBOUND_GUARD_RESET_AT: dict[str, float] = {}


def _shorten_webspace_id(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if raw else "default"


def _reserve_inbound_guard_reset(webspace_id: str, now_mono: float) -> bool:
    key = _coerce_gateway_webspace_id(webspace_id)
    with _YROOM_LIFECYCLE_LOCK:
        previous = float(_YROOM_INBOUND_GUARD_RESET_AT.get(key) or 0.0)
        if previous > 0.0 and now_mono - previous < _YROOM_INBOUND_GUARD_RESET_COOLDOWN_SEC:
            return False
        _YROOM_INBOUND_GUARD_RESET_AT[key] = now_mono
        return True


def _is_empty_y_update(update: bytes | bytearray | memoryview | None) -> bool:
    return bytes(update or b"") == _EMPTY_Y_UPDATE


def _is_websocket_accept_race(exc: BaseException) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return (
        "websocket.accept" in text
        and "websocket.close" in text
    ) or "close message has been sent" in text


def _is_websocket_receive_disconnect_race(exc: BaseException) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return (
        "websocket is not connected" in text
        or "need to call \"accept\" first" in text
        or "disconnect message has been received" in text
        or "close message has been sent" in text
    )


async def _stop_ystore_maybe_async(ystore: Any) -> None:
    try:
        result = ystore.stop()
    except Exception:
        return
    if inspect.isawaitable(result):
        try:
            await result
        except Exception:
            return


def _seconds_ago(value: Any, now: float) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    if stamp <= 0.0:
        return None
    return round(max(0.0, now - stamp), 3)


def _memory_stream_statistics(stream: Any) -> dict[str, Any]:
    stats = getattr(stream, "statistics", None)
    if not callable(stats):
        return {}
    try:
        snapshot = stats()
    except Exception:
        return {}
    return {
        "current_buffer_used": int(getattr(snapshot, "current_buffer_used", 0) or 0),
        "max_buffer_size": int(getattr(snapshot, "max_buffer_size", 0) or 0),
        "open_send_streams": int(getattr(snapshot, "open_send_streams", 0) or 0),
        "open_receive_streams": int(getattr(snapshot, "open_receive_streams", 0) or 0),
        "tasks_waiting_send": int(getattr(snapshot, "tasks_waiting_send", 0) or 0),
        "tasks_waiting_receive": int(getattr(snapshot, "tasks_waiting_receive", 0) or 0),
    }


_YROOM_PRESSURE_STATE: dict[str, dict[str, Any]] = {}
_AUTHORITATIVE_SCENARIO_LEASES: dict[str, dict[str, Any]] = {}


def note_authoritative_current_scenario(webspace_id: str, scenario_id: str, *, reason: str = "scenario_switch") -> None:
    key = _coerce_gateway_webspace_id(webspace_id)
    scenario = str(scenario_id or "").strip()
    if not key or not scenario or _YROOM_AUTHORITATIVE_SELECTOR_LEASE_SEC <= 0.0:
        return
    _AUTHORITATIVE_SCENARIO_LEASES[key] = {
        "scenario_id": scenario,
        "reason": str(reason or "").strip() or "scenario_switch",
        "expires_mono": time.monotonic() + float(_YROOM_AUTHORITATIVE_SELECTOR_LEASE_SEC),
        "updated_at": time.time(),
    }


def _authoritative_current_scenario(webspace_id: str) -> str | None:
    key = _coerce_gateway_webspace_id(webspace_id)
    lease = dict(_AUTHORITATIVE_SCENARIO_LEASES.get(key) or {})
    scenario = str(lease.get("scenario_id") or "").strip()
    expires_mono = float(lease.get("expires_mono") or 0.0)
    if not scenario or expires_mono <= 0.0:
        _AUTHORITATIVE_SCENARIO_LEASES.pop(key, None)
        return None
    if time.monotonic() > expires_mono:
        _AUTHORITATIVE_SCENARIO_LEASES.pop(key, None)
        return None
    return scenario


def yjs_pressure_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    now = time.monotonic()
    if webspace_id is None:
        active = 0
        rooms: list[dict[str, Any]] = []
        for key, raw in list(_YROOM_PRESSURE_STATE.items()):
            item = dict(raw or {})
            if bool(item.get("active")):
                active += 1
            since_at = float(item.get("since_mono") or 0.0)
            item["age_s"] = round(max(0.0, now - since_at), 3) if bool(item.get("active")) and since_at > 0.0 else 0.0
            item["webspace_id"] = str(item.get("webspace_id") or key or "default").strip() or "default"
            rooms.append(item)
        rooms.sort(key=lambda item: (0 if bool(item.get("active")) else 1, -float(item.get("age_s") or 0.0), str(item.get("webspace_id") or "")))
        return {
            "active_room_total": active,
            "room_total": len(rooms),
            "rooms": rooms,
        }
    key = _coerce_gateway_webspace_id(webspace_id)
    raw = dict(_YROOM_PRESSURE_STATE.get(key) or {})
    if not raw:
        return {
            "webspace_id": key,
            "active": False,
            "reason": "",
            "age_s": 0.0,
            "pending_send_tasks": 0,
            "pending_store_tasks": 0,
            "buffer_used": 0,
            "waiting_send": 0,
            "waiting_receive": 0,
            "update_bytes": 0,
            "message_bytes": 0,
        }
    since_at = float(raw.get("since_mono") or 0.0)
    raw["age_s"] = round(max(0.0, now - since_at), 3) if bool(raw.get("active")) and since_at > 0.0 else 0.0
    raw["webspace_id"] = key
    return raw


class DiagnosticYRoom(YRoom):
    """
    Thin YRoom wrapper that logs pressure signals without changing semantics.

    The goal is to surface whether memory growth comes from queued Y updates
    and fanout tasks, not to alter delivery or persistence behavior yet.
    """

    def __init__(self, ready: bool = True, ystore: Any | None = None, log: logging.Logger | None = None):
        super().__init__(ready=ready, ystore=ystore, log=log)
        self._diag_pending_send_tasks = 0
        self._diag_pending_store_tasks = 0
        self._diag_peak_buffer_used = 0
        self._diag_peak_pending_send_tasks = 0
        self._diag_peak_pending_store_tasks = 0
        self._diag_update_total = 0
        self._diag_update_bytes_total = 0
        self._diag_empty_update_skip_total = 0
        self._diag_empty_update_skip_bytes = 0
        self._diag_backend_persist_skip_total = 0
        self._diag_backend_persist_skip_bytes = 0
        self._diag_destructive_update_block_total = 0
        self._diag_destructive_update_block_bytes = 0
        self._diag_inbound_guard_block_total = 0
        self._diag_inbound_guard_block_bytes = 0
        self._diag_inbound_guard_last_bytes = 0
        self._diag_inbound_guard_last_block_bytes = int(_YROOM_INBOUND_GUARD_BLOCK_BYTES)
        self._diag_inbound_guard_last_at = 0.0
        self._diag_inbound_guard_last_reset_reserved = False
        self._diag_effective_repair_total = 0
        self._diag_effective_repair_bytes = 0
        self._diag_effective_branch_snapshot: dict[str, Any] = {"ready": False, "error": "not_observed"}
        self._diag_effective_last_full_check_mono = time.monotonic()
        self._diag_effective_last_repair_mono = 0.0
        self._diag_last_log_mono = 0.0
        self._diag_pressure_active = False
        self._diag_pressure_reason = ""
        self._diag_pressure_since_mono = 0.0
        self._diag_pressure_activation_total = 0
        self._diag_pressure_clear_total = 0

    def _diag_room_id(self) -> str:
        return str(getattr(self, "_webspace_id", "") or "default").strip() or "default"

    def _diag_ystore_snapshot(self) -> dict[str, Any]:
        ystore = getattr(self, "ystore", None)
        runtime_snapshot = getattr(ystore, "runtime_snapshot", None)
        if callable(runtime_snapshot):
            try:
                raw = runtime_snapshot()
                if isinstance(raw, dict):
                    return {
                        "update_log_entries": int(raw.get("update_log_entries") or 0),
                        "update_log_bytes": int(raw.get("update_log_bytes") or 0),
                        "replay_window_bytes": int(raw.get("replay_window_bytes") or 0),
                        "last_update_bytes": int(raw.get("last_update_bytes") or 0),
                    }
            except Exception:
                return {}
        return {}

    def _diag_snapshot(self, *, include_ystore: bool = False) -> dict[str, Any]:
        send_stats = _memory_stream_statistics(getattr(self, "_update_send_stream", None))
        recv_stats = _memory_stream_statistics(getattr(self, "_update_receive_stream", None))
        now_mono = time.monotonic()
        pressure_age_s = (
            round(max(0.0, now_mono - float(self._diag_pressure_since_mono or 0.0)), 3)
            if self._diag_pressure_active and float(self._diag_pressure_since_mono or 0.0) > 0.0
            else 0.0
        )
        return {
            "webspace_id": self._diag_room_id(),
            "client_total": len(getattr(self, "clients", []) or []),
            "send_stream": send_stats,
            "receive_stream": recv_stats,
            "pending_send_tasks": int(self._diag_pending_send_tasks),
            "pending_store_tasks": int(self._diag_pending_store_tasks),
            "update_total": int(self._diag_update_total),
            "update_bytes_total": int(self._diag_update_bytes_total),
            "empty_update_skip_total": int(self._diag_empty_update_skip_total),
            "empty_update_skip_bytes": int(self._diag_empty_update_skip_bytes),
            "backend_persist_skip_total": int(self._diag_backend_persist_skip_total),
            "backend_persist_skip_bytes": int(self._diag_backend_persist_skip_bytes),
            "destructive_update_block_total": int(self._diag_destructive_update_block_total),
            "destructive_update_block_bytes": int(self._diag_destructive_update_block_bytes),
            "inbound_guard_block_total": int(self._diag_inbound_guard_block_total),
            "inbound_guard_block_bytes": int(self._diag_inbound_guard_block_bytes),
            "inbound_guard_last_bytes": int(self._diag_inbound_guard_last_bytes),
            "inbound_guard_last_block_bytes": int(self._diag_inbound_guard_last_block_bytes),
            "inbound_guard_last_at": float(self._diag_inbound_guard_last_at or 0.0),
            "inbound_guard_last_ago_s": _seconds_ago(
                self._diag_inbound_guard_last_at or None,
                time.time(),
            ),
            "inbound_guard_last_reset_reserved": bool(self._diag_inbound_guard_last_reset_reserved),
            "effective_repair_total": int(self._diag_effective_repair_total),
            "effective_repair_bytes": int(self._diag_effective_repair_bytes),
            "peak_buffer_used": int(self._diag_peak_buffer_used),
            "peak_pending_send_tasks": int(self._diag_peak_pending_send_tasks),
            "peak_pending_store_tasks": int(self._diag_peak_pending_store_tasks),
            "pressure_active": bool(self._diag_pressure_active),
            "pressure_reason": str(self._diag_pressure_reason or ""),
            "pressure_age_s": pressure_age_s,
            "pressure_activation_total": int(self._diag_pressure_activation_total),
            "pressure_clear_total": int(self._diag_pressure_clear_total),
            "ystore": self._diag_ystore_snapshot() if include_ystore else {},
        }

    def _diag_update_pressure_state(
        self,
        *,
        reason: str,
        active: bool,
        snapshot: dict[str, Any],
        buffer_used: int,
        waiting_send: int,
        waiting_receive: int,
        pending_send: int,
        pending_store: int,
        update_bytes: int,
        message_bytes: int,
    ) -> None:
        now_mono = time.monotonic()
        previous_active = bool(self._diag_pressure_active)
        previous_reason = str(self._diag_pressure_reason or "")
        transition = False
        if active:
            if not previous_active:
                self._diag_pressure_activation_total += 1
                self._diag_pressure_since_mono = now_mono
                transition = True
            elif previous_reason != reason:
                transition = True
            self._diag_pressure_active = True
            self._diag_pressure_reason = str(reason or "").strip() or "pressure"
        else:
            if previous_active:
                self._diag_pressure_clear_total += 1
                transition = True
            self._diag_pressure_active = False
            self._diag_pressure_reason = ""
            self._diag_pressure_since_mono = 0.0
        age_s = (
            round(max(0.0, now_mono - float(self._diag_pressure_since_mono or 0.0)), 3)
            if self._diag_pressure_active and float(self._diag_pressure_since_mono or 0.0) > 0.0
            else 0.0
        )
        _YROOM_PRESSURE_STATE[self._diag_room_id()] = {
            "webspace_id": self._diag_room_id(),
            "active": bool(self._diag_pressure_active),
            "reason": str(self._diag_pressure_reason or ""),
            "since_mono": float(self._diag_pressure_since_mono or 0.0),
            "age_s": age_s,
            "pending_send_tasks": int(pending_send),
            "pending_store_tasks": int(pending_store),
            "buffer_used": int(buffer_used),
            "waiting_send": int(waiting_send),
            "waiting_receive": int(waiting_receive),
            "update_bytes": int(update_bytes or 0),
            "message_bytes": int(message_bytes or 0),
            "peak_buffer_used": int(self._diag_peak_buffer_used),
            "peak_pending_send_tasks": int(self._diag_peak_pending_send_tasks),
            "peak_pending_store_tasks": int(self._diag_peak_pending_store_tasks),
            "pressure_activation_total": int(self._diag_pressure_activation_total),
            "pressure_clear_total": int(self._diag_pressure_clear_total),
            "update_total": int(snapshot.get("update_total") or 0),
            "update_bytes_total": int(snapshot.get("update_bytes_total") or 0),
        }
        if transition:
            self.log.warning(
                "yroom pressure state webspace=%s active=%s reason=%s age_s=%s "
                "send_buffer=%s waiting_send=%s waiting_receive=%s pending_send=%s pending_store=%s "
                "activations=%s clears=%s",
                self._diag_room_id(),
                bool(self._diag_pressure_active),
                str(self._diag_pressure_reason or "healthy"),
                age_s,
                int(buffer_used),
                int(waiting_send),
                int(waiting_receive),
                int(pending_send),
                int(pending_store),
                int(self._diag_pressure_activation_total),
                int(self._diag_pressure_clear_total),
            )

    def _diag_log_pressure(
        self,
        reason: str,
        *,
        force: bool = False,
        update_bytes: int | None = None,
        message_bytes: int | None = None,
    ) -> None:
        if not _YROOM_DIAG_ENABLED:
            return
        snapshot = self._diag_snapshot()
        send_stream = snapshot.get("send_stream") if isinstance(snapshot.get("send_stream"), dict) else {}
        receive_stream = snapshot.get("receive_stream") if isinstance(snapshot.get("receive_stream"), dict) else {}
        ystore = snapshot.get("ystore") if isinstance(snapshot.get("ystore"), dict) else {}
        buffer_used = int(send_stream.get("current_buffer_used") or 0)
        waiting_send = int(send_stream.get("tasks_waiting_send") or 0)
        waiting_receive = int(send_stream.get("tasks_waiting_receive") or 0)
        pending_send = int(snapshot.get("pending_send_tasks") or 0)
        pending_store = int(snapshot.get("pending_store_tasks") or 0)
        pressure = (
            buffer_used >= _YROOM_DIAG_BUFFER_WARN
            or waiting_send >= _YROOM_DIAG_PENDING_WARN
            or pending_send >= _YROOM_DIAG_PENDING_WARN
            or pending_store >= _YROOM_DIAG_PENDING_WARN
            or int(update_bytes or 0) >= _YROOM_DIAG_UPDATE_WARN_BYTES
            or int(message_bytes or 0) >= _YROOM_DIAG_UPDATE_WARN_BYTES
        )
        peak = False
        if buffer_used > self._diag_peak_buffer_used:
            self._diag_peak_buffer_used = buffer_used
            peak = True
        if pending_send > self._diag_peak_pending_send_tasks:
            self._diag_peak_pending_send_tasks = pending_send
            peak = True
        if pending_store > self._diag_peak_pending_store_tasks:
            self._diag_peak_pending_store_tasks = pending_store
            peak = True
        now_mono = time.monotonic()
        self._diag_update_pressure_state(
            reason=reason,
            active=pressure,
            snapshot=snapshot,
            buffer_used=buffer_used,
            waiting_send=waiting_send,
            waiting_receive=waiting_receive,
            pending_send=pending_send,
            pending_store=pending_store,
            update_bytes=int(update_bytes or 0),
            message_bytes=int(message_bytes or 0),
        )
        if not force and not pressure and not peak:
            return
        if not force and not peak and now_mono - self._diag_last_log_mono < _YROOM_DIAG_LOG_INTERVAL_SEC:
            return
        self._diag_last_log_mono = now_mono
        if _YROOM_DIAG_INCLUDE_YSTORE:
            ystore = self._diag_ystore_snapshot()
        self.log.warning(
            "yroom pressure webspace=%s reason=%s clients=%s update_bytes=%s message_bytes=%s "
            "send_buffer=%s/%s waiting_send=%s waiting_receive=%s pending_send=%s pending_store=%s "
            "update_total=%s update_bytes_total=%s ystore_entries=%s ystore_bytes=%s replay_bytes=%s",
            snapshot.get("webspace_id"),
            str(reason or "").strip() or "unknown",
            int(snapshot.get("client_total") or 0),
            int(update_bytes or 0),
            int(message_bytes or 0),
            buffer_used,
            int(send_stream.get("max_buffer_size") or 0),
            waiting_send,
            waiting_receive,
            pending_send,
            pending_store,
            int(snapshot.get("update_total") or 0),
            int(snapshot.get("update_bytes_total") or 0),
            int(ystore.get("update_log_entries") or 0),
            int(ystore.get("update_log_bytes") or 0),
            int(ystore.get("replay_window_bytes") or 0),
        )

    async def _tracked_client_send(self, client: Any, message: bytes, update_bytes: int) -> None:
        self._diag_pending_send_tasks += 1
        try:
            self._diag_log_pressure(
                "client.send.scheduled",
                update_bytes=update_bytes,
                message_bytes=len(message),
            )
            await client.send(message)
        finally:
            self._diag_pending_send_tasks = max(0, int(self._diag_pending_send_tasks) - 1)

    async def _tracked_ystore_write(self, update: bytes) -> None:
        ystore = getattr(self, "ystore", None)
        if ystore is None:
            return
        if _is_empty_y_update(update):
            self._diag_empty_update_skip_total += 1
            self._diag_empty_update_skip_bytes += len(update or b"")
            return
        self._diag_pending_store_tasks += 1
        try:
            persisted = consume_backend_room_update(self._diag_room_id(), update)
            if persisted is not None:
                update_len = len(update or b"")
                if bool(persisted.get("already_persisted", True)):
                    self._diag_backend_persist_skip_total += 1
                    self._diag_backend_persist_skip_bytes += update_len
                    self.log.debug(
                        "Skipping duplicate backend-origin YStore write for webspace=%s bytes=%s source=%s owner=%s",
                        self._diag_room_id(),
                        update_len,
                        persisted.get("source"),
                        persisted.get("owner"),
                    )
                    return
                root_names = persisted.get("root_names")
                if not isinstance(root_names, (list, tuple)):
                    root_names = []
                source = str(persisted.get("source") or "yjs.gateway_ws.backend_live_room")
                owner = str(persisted.get("owner") or "").strip() or "gateway_ws"
                channel = str(persisted.get("channel") or "core.yjs.gateway.live_room.persist")
                self._diag_log_pressure("ystore.write.backend_live_room", update_bytes=update_len)
                async with ystore_write_metadata(
                    root_names=[
                        str(item or "").strip()
                        for item in list(root_names or ())
                        if str(item or "").strip()
                    ],
                    source=source,
                    owner=owner,
                    channel=channel,
                    governed=bool(persisted.get("governed", False)),
                ):
                    await ystore.write(update)
                return
            self._diag_log_pressure("ystore.write.scheduled", update_bytes=len(update))
            async with ystore_write_metadata(
                source="yjs.gateway_ws",
                owner="gateway_ws",
                channel="core.yjs.gateway.live_room.persist",
            ):
                await ystore.write(update)
        finally:
            self._diag_pending_store_tasks = max(0, int(self._diag_pending_store_tasks) - 1)

    async def _repair_effective_branches_after_destructive_update(
        self,
        *,
        destructive_update_bytes: int,
        snapshot: dict[str, Any],
    ) -> bytes:
        self._diag_destructive_update_block_total += 1
        self._diag_destructive_update_block_bytes += int(destructive_update_bytes or 0)
        self.log.warning(
            "blocked destructive YRoom update webspace=%s bytes=%s blocks=%s snapshot=%s",
            self._diag_room_id(),
            int(destructive_update_bytes or 0),
            int(self._diag_destructive_update_block_total),
            json.dumps(snapshot, ensure_ascii=True, sort_keys=True)[:1000],
        )
        repair_update = await _repair_room_effective_branches(
            self._diag_room_id(),
            getattr(self, "ystore", None),
            self,
            reason="destructive_client_update",
        )
        if repair_update:
            self._diag_effective_repair_total += 1
            self._diag_effective_repair_bytes += len(repair_update)
            try:
                self._diag_effective_branch_snapshot = _room_effective_branch_snapshot(self.ydoc)
            except Exception as exc:
                self._diag_effective_branch_snapshot = {
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return repair_update

    async def _repair_effective_branches_after_client_update(
        self,
        *,
        update_bytes: int,
        reason: str,
    ) -> bytes:
        repair_update = await _repair_room_effective_branches(
            self._diag_room_id(),
            getattr(self, "ystore", None),
            self,
            reason=reason,
        )
        if repair_update:
            self._diag_effective_repair_total += 1
            self._diag_effective_repair_bytes += len(repair_update)
            try:
                self._diag_effective_branch_snapshot = _room_effective_branch_snapshot(self.ydoc)
            except Exception as exc:
                self._diag_effective_branch_snapshot = {
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.log.warning(
                "repaired YRoom effective branches after client update webspace=%s reason=%s update_bytes=%s repair_bytes=%s repairs=%s",
                self._diag_room_id(),
                reason,
                int(update_bytes or 0),
                len(repair_update),
                int(self._diag_effective_repair_total),
            )
        return repair_update

    async def _broadcast_updates(self):
        if self.ystore is not None and not self.ystore.started.is_set():
            self._task_group.start_soon(self.ystore.start)

        async with self._update_receive_stream:
            async for update in self._update_receive_stream:
                if self._task_group.cancel_scope.cancel_called:
                    return
                update_len = len(update or b"")
                self._diag_update_total += 1
                self._diag_update_bytes_total += update_len
                if _is_empty_y_update(update):
                    self._diag_empty_update_skip_total += 1
                    self._diag_empty_update_skip_bytes += update_len
                    continue
                self._diag_log_pressure("broadcast.update.received", update_bytes=update_len)
                if update_len >= _YROOM_INBOUND_GUARD_BLOCK_BYTES:
                    webspace_id = self._diag_room_id()
                    reset_reserved = _reserve_inbound_guard_reset(webspace_id, time.monotonic())
                    self._diag_inbound_guard_block_total += 1
                    self._diag_inbound_guard_block_bytes += update_len
                    self._diag_inbound_guard_last_bytes = update_len
                    self._diag_inbound_guard_last_block_bytes = int(_YROOM_INBOUND_GUARD_BLOCK_BYTES)
                    self._diag_inbound_guard_last_at = time.time()
                    self._diag_inbound_guard_last_reset_reserved = bool(reset_reserved)
                    self.log.warning(
                        "blocked oversized inbound YWS update webspace=%s update_bytes=%s block_bytes=%s reset_reserved=%s reason=inbound_yws_update_payload_blocked",
                        webspace_id,
                        update_len,
                        _YROOM_INBOUND_GUARD_BLOCK_BYTES,
                        reset_reserved,
                    )
                    if reset_reserved:
                        asyncio.create_task(
                            reset_live_webspace_room(
                                webspace_id,
                                close_reason="inbound_yws_update_payload_blocked",
                                persist_ystore_snapshot=False,
                                reset_route_runtime=True,
                            )
                        )
                    continue
                previous_effective_ready = bool(
                    isinstance(self._diag_effective_branch_snapshot, dict)
                    and self._diag_effective_branch_snapshot.get("ready")
                )
                effective_ready = previous_effective_ready
                effective_snapshot: dict[str, Any] = {"ready": effective_ready}
                try:
                    now_mono = time.monotonic()
                    check_age = now_mono - float(self._diag_effective_last_full_check_mono or 0.0)
                    min_check_elapsed = (
                        _YROOM_EFFECTIVE_GUARD_MIN_CHECK_INTERVAL_SEC <= 0.0
                        or check_age >= _YROOM_EFFECTIVE_GUARD_MIN_CHECK_INTERVAL_SEC
                    )
                    full_check_due = min_check_elapsed and (
                        update_len >= _YROOM_EFFECTIVE_GUARD_FULL_CHECK_BYTES
                        or (
                            _YROOM_EFFECTIVE_GUARD_FULL_CHECK_INTERVAL_SEC > 0.0
                            and check_age >= _YROOM_EFFECTIVE_GUARD_FULL_CHECK_INTERVAL_SEC
                        )
                    )
                    force_initial_check = not previous_effective_ready and self._diag_update_total <= 1
                    checked_effective = False
                    if previous_effective_ready and not full_check_due:
                        if _YROOM_EFFECTIVE_GUARD_TOP_LEVEL_CHECKS:
                            effective_ready = _room_effective_top_level_ready(self.ydoc)
                            effective_snapshot = {
                                "ready": effective_ready,
                                "mode": "top_level_hot",
                            }
                            checked_effective = True
                        else:
                            effective_ready = True
                            effective_snapshot = {"ready": True, "mode": "cached"}
                    elif full_check_due or force_initial_check:
                        self._diag_effective_last_full_check_mono = now_mono
                        checked_effective = True
                        if previous_effective_ready and _YROOM_EFFECTIVE_GUARD_STRICT_FULL_CHECKS:
                            effective_snapshot = _room_effective_branch_snapshot(self.ydoc)
                            effective_ready = bool(effective_snapshot.get("ready"))
                        else:
                            effective_ready = _room_effective_top_level_ready(self.ydoc)
                            effective_snapshot = {
                                "ready": effective_ready,
                                "mode": "top_level_periodic" if previous_effective_ready else "top_level",
                            }
                    else:
                        effective_snapshot = {"ready": effective_ready, "mode": "cached_missing"}
                    if checked_effective and not effective_ready:
                        self._diag_effective_last_full_check_mono = now_mono
                        effective_snapshot = {"ready": False, "mode": "top_level_missing"}
                    elif not previous_effective_ready:
                        effective_snapshot = {"ready": effective_ready, "mode": "top_level"}
                    self._diag_effective_branch_snapshot = effective_snapshot
                except Exception as exc:
                    effective_snapshot = {
                        "ready": previous_effective_ready,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    effective_ready = previous_effective_ready
                initial_repair_due = bool(
                    _YROOM_EFFECTIVE_GUARD_REPAIR_INITIAL_UPDATES > 0
                    and self._diag_update_total <= _YROOM_EFFECTIVE_GUARD_REPAIR_INITIAL_UPDATES
                )
                repair_cooldown_due = bool(
                    _YROOM_EFFECTIVE_GUARD_REPAIR_COOLDOWN_SEC <= 0.0
                    or time.monotonic() - float(self._diag_effective_last_repair_mono or 0.0)
                    >= _YROOM_EFFECTIVE_GUARD_REPAIR_COOLDOWN_SEC
                )
                if effective_ready and initial_repair_due and repair_cooldown_due:
                    self._diag_effective_last_repair_mono = time.monotonic()
                    repair_update = await self._repair_effective_branches_after_client_update(
                        update_bytes=update_len,
                        reason="initial_client_update_reconcile",
                    )
                    if repair_update:
                        repair_message = create_update_message(repair_update)
                        for client in self.clients:
                            self.log.debug("Sending Y repair update to client with endpoint: %s", client.path)
                            self._task_group.start_soon(
                                self._tracked_client_send,
                                client,
                                repair_message,
                                len(repair_update),
                            )
                        continue
                if previous_effective_ready and not effective_ready:
                    repair_update = await self._repair_effective_branches_after_destructive_update(
                        destructive_update_bytes=update_len,
                        snapshot=effective_snapshot,
                    )
                    if repair_update:
                        repair_message = create_update_message(repair_update)
                        for client in self.clients:
                            self.log.debug("Sending Y repair update to client with endpoint: %s", client.path)
                            self._task_group.start_soon(
                                self._tracked_client_send,
                                client,
                                repair_message,
                                len(repair_update),
                            )
                    continue
                for client in self.clients:
                    self.log.debug("Sending Y update to client with endpoint: %s", client.path)
                    message = create_update_message(update)
                    self._task_group.start_soon(self._tracked_client_send, client, message, update_len)
                if self.ystore:
                    self.log.debug("Writing Y update to YStore")
                    self._task_group.start_soon(self._tracked_ystore_write, update)


def _command_payload_fingerprint(kind: str, payload: Any) -> str:
    raw = dict(payload or {}) if isinstance(payload, dict) else {}
    raw.pop("_meta", None)
    try:
        encoded = json.dumps(
            {
                "kind": str(kind or "").strip(),
                "payload": raw,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except Exception:
        encoded = f"{kind}:{sorted(raw.items())}".encode("utf-8", errors="replace")
    return hashlib.sha1(encoded).hexdigest()[:12]


def _record_command_trace(
    *,
    kind: str,
    cmd_id: str | None,
    payload: dict[str, Any] | None,
    device_id: str | None,
    webspace_id: str | None,
    client_label: str | None,
) -> dict[str, Any]:
    global _COMMAND_TRACE_SEQ

    now = time.time()
    normalized_kind = str(kind or "").strip() or "-"
    effective_payload = dict(payload or {})
    effective_webspace = str(
        effective_payload.get("webspace_id")
        or effective_payload.get("workspace_id")
        or webspace_id
        or "default"
    ).strip() or "default"
    fingerprint = _command_payload_fingerprint(normalized_kind, effective_payload)
    scenario_id = str(effective_payload.get("scenario_id") or "").strip() or None
    recreate_room = bool(effective_payload.get("recreate_room"))
    duplicate_recent = False
    duplicate_delta_ms: float | None = None
    duplicate_count_10s = 0

    with _COMMAND_TRACE_LOCK:
        for previous in reversed(_COMMAND_TRACE_HISTORY):
            if str(previous.get("kind") or "") != normalized_kind:
                continue
            if str(previous.get("webspace_id") or "") != effective_webspace:
                continue
            if str(previous.get("fingerprint") or "") != fingerprint:
                continue
            previous_ts = float(previous.get("ts") or 0.0)
            if previous_ts <= 0.0:
                continue
            delta_s = now - previous_ts
            if delta_s <= 10.0:
                duplicate_count_10s += 1
            if not duplicate_recent and delta_s <= 10.0:
                duplicate_recent = True
                duplicate_delta_ms = round(delta_s * 1000.0, 3)

        _COMMAND_TRACE_SEQ += 1
        record = {
            "seq": int(_COMMAND_TRACE_SEQ),
            "ts": now,
            "kind": normalized_kind,
            "cmd_id": str(cmd_id or "").strip() or None,
            "device_id": str(device_id or "").strip() or None,
            "webspace_id": effective_webspace,
            "client": str(client_label or "").strip() or None,
            "scenario_id": scenario_id,
            "recreate_room": recreate_room,
            "fingerprint": fingerprint,
            "duplicate_recent": duplicate_recent,
            "duplicate_delta_ms": duplicate_delta_ms,
            "duplicate_count_10s": duplicate_count_10s,
        }
        _COMMAND_TRACE_HISTORY.append(record)
        if normalized_kind == "desktop.webspace.reload":
            _COMMAND_TRACE_STATS["reload_total"] = int(_COMMAND_TRACE_STATS.get("reload_total") or 0) + 1
            if duplicate_recent:
                _COMMAND_TRACE_STATS["reload_duplicate_total"] = int(_COMMAND_TRACE_STATS.get("reload_duplicate_total") or 0) + 1
        elif normalized_kind == "desktop.webspace.reset":
            _COMMAND_TRACE_STATS["reset_total"] = int(_COMMAND_TRACE_STATS.get("reset_total") or 0) + 1
            if duplicate_recent:
                _COMMAND_TRACE_STATS["reset_duplicate_total"] = int(_COMMAND_TRACE_STATS.get("reset_duplicate_total") or 0) + 1
    return record


def _command_trace_snapshot(now: float) -> dict[str, Any]:
    with _COMMAND_TRACE_LOCK:
        history = list(_COMMAND_TRACE_HISTORY)
        stats = dict(_COMMAND_TRACE_STATS)
    recent_reload_60s = 0
    recent_reset_60s = 0
    last_reload: dict[str, Any] | None = None
    last_reset: dict[str, Any] | None = None
    recent_items: list[dict[str, Any]] = []
    for record in reversed(history):
        ts = float(record.get("ts") or 0.0)
        age_s = round(max(0.0, now - ts), 3) if ts > 0.0 else None
        entry = {
            "seq": int(record.get("seq") or 0),
            "kind": str(record.get("kind") or ""),
            "cmd_id": record.get("cmd_id"),
            "device_id": record.get("device_id"),
            "webspace_id": record.get("webspace_id"),
            "client": record.get("client"),
            "scenario_id": record.get("scenario_id"),
            "recreate_room": bool(record.get("recreate_room")),
            "fingerprint": record.get("fingerprint"),
            "duplicate_recent": bool(record.get("duplicate_recent")),
            "duplicate_delta_ms": record.get("duplicate_delta_ms"),
            "duplicate_count_10s": int(record.get("duplicate_count_10s") or 0),
            "age_s": age_s,
        }
        if entry["kind"] == "desktop.webspace.reload":
            if age_s is not None and age_s <= 60.0:
                recent_reload_60s += 1
            if last_reload is None:
                last_reload = dict(entry)
        elif entry["kind"] == "desktop.webspace.reset":
            if age_s is not None and age_s <= 60.0:
                recent_reset_60s += 1
            if last_reset is None:
                last_reset = dict(entry)
        if len(recent_items) < 8:
            recent_items.append(entry)
    return {
        "reload_total": int(stats.get("reload_total") or 0),
        "reload_duplicate_total": int(stats.get("reload_duplicate_total") or 0),
        "reload_recent_60s": int(recent_reload_60s),
        "reset_total": int(stats.get("reset_total") or 0),
        "reset_duplicate_total": int(stats.get("reset_duplicate_total") or 0),
        "reset_recent_60s": int(recent_reset_60s),
        "last_reload": last_reload or {},
        "last_reset": last_reset or {},
        "recent": recent_items,
    }


def _mark_room_created(webspace_id: str, room: Any) -> None:
    key = str(webspace_id or "").strip() or "default"
    ydoc = getattr(room, "ydoc", None)
    now = time.time()
    with _YROOM_LIFECYCLE_LOCK:
        entry = _YROOM_LIFECYCLE.setdefault(key, {})
        entry["generation"] = int(entry.get("generation") or 0) + 1
        entry["create_total"] = int(entry.get("create_total") or 0) + 1
        entry["last_created_at"] = now
        entry["last_room_object_id"] = id(room)
        entry["last_ydoc_object_id"] = id(ydoc) if ydoc is not None else None


def _mark_room_open(
    webspace_id: str,
    room: Any,
    *,
    created: bool,
    open_total_ms: float | None = None,
    seed_result: dict[str, Any] | None = None,
) -> None:
    key = str(webspace_id or "").strip() or "default"
    now = time.time()
    lifecycle = dict(seed_result or {})
    with _YROOM_LIFECYCLE_LOCK:
        entry = _YROOM_LIFECYCLE.setdefault(key, {})
        entry["open_total"] = int(entry.get("open_total") or 0) + 1
        if created:
            entry["cold_open_total"] = int(entry.get("cold_open_total") or 0) + 1
            if bool(lifecycle.get("used_provided_ydoc")):
                entry["single_pass_bootstrap_total"] = int(entry.get("single_pass_bootstrap_total") or 0) + 1
        else:
            entry["reuse_total"] = int(entry.get("reuse_total") or 0) + 1
        entry["last_open_at"] = now
        entry["last_open_mode"] = "cold_open" if created else "room_reuse"
        entry["last_open_total_ms"] = round(float(open_total_ms), 3) if open_total_ms is not None else None
        entry["last_open_apply_updates_ms"] = (
            round(float(lifecycle.get("apply_updates_ms") or 0.0), 3)
            if created and lifecycle
            else None
        )
        entry["last_open_bootstrap_total_ms"] = (
            round(float(lifecycle.get("total_ms") or 0.0), 3)
            if created and lifecycle
            else None
        )
        entry["last_open_bootstrap_mode"] = (
            str(lifecycle.get("mode") or "").strip() or None
            if created and lifecycle
            else None
        )
        entry["last_open_bootstrap_persisted_via"] = (
            str(lifecycle.get("persisted_via") or "").strip() or None
            if created and lifecycle
            else None
        )
        entry["last_open_bootstrap_single_pass"] = bool(lifecycle.get("used_provided_ydoc")) if created and lifecycle else False


def _mark_room_reset(
    webspace_id: str,
    *,
    close_reason: str,
    room: Any | None,
    room_dropped: bool,
    closed_connections: int,
    closed_webrtc_peers: int,
) -> None:
    key = str(webspace_id or "").strip() or "default"
    ydoc = getattr(room, "ydoc", None) if room is not None else None
    now = time.time()
    with _YROOM_LIFECYCLE_LOCK:
        entry = _YROOM_LIFECYCLE.setdefault(key, {})
        entry["reset_total"] = int(entry.get("reset_total") or 0) + 1
        entry["last_reset_at"] = now
        entry["last_reset_reason"] = str(close_reason or "").strip() or "webspace_reload"
        entry["last_reset_closed_connections"] = int(closed_connections or 0)
        entry["last_reset_closed_webrtc_peers"] = int(closed_webrtc_peers or 0)
        entry["last_reset_room_dropped"] = bool(room_dropped)
        if room is not None:
            entry["last_reset_room_object_id"] = id(room)
        if ydoc is not None:
            entry["last_reset_ydoc_object_id"] = id(ydoc)
        if room_dropped:
            entry["drop_total"] = int(entry.get("drop_total") or 0) + 1
            entry["last_dropped_at"] = now


def _room_debug_snapshot(webspace_id: str, room: Any | None, now: float) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    with _YROOM_LIFECYCLE_LOCK:
        meta = dict(_YROOM_LIFECYCLE.get(key) or {})

    ydoc = getattr(room, "ydoc", None) if room is not None else None
    ystore = getattr(room, "ystore", None) if room is not None else None
    clients = getattr(room, "clients", None) if room is not None else None
    send_stream_stats = _memory_stream_statistics(getattr(room, "_update_send_stream", None) if room is not None else None)
    recv_stream_stats = _memory_stream_statistics(getattr(room, "_update_receive_stream", None) if room is not None else None)
    started_event = getattr(room, "_started", None) if room is not None else None
    task_group = getattr(room, "_task_group", None) if room is not None else None
    ystore_runtime = {}
    if ystore is not None:
        runtime_snapshot = getattr(ystore, "runtime_snapshot", None)
        if callable(runtime_snapshot):
            try:
                raw = runtime_snapshot(now_ts=now)
            except Exception:
                raw = {}
            if isinstance(raw, dict):
                ystore_runtime = {
                    "update_log_entries": int(raw.get("update_log_entries") or 0),
                    "update_log_bytes": int(raw.get("update_log_bytes") or 0),
                    "replay_window_bytes": int(raw.get("replay_window_bytes") or 0),
                    "last_update_bytes": int(raw.get("last_update_bytes") or 0),
                }
    room_diagnostic = {}
    diagnostic_snapshot = getattr(room, "_diag_snapshot", None) if room is not None else None
    if callable(diagnostic_snapshot):
        try:
            raw_diag = diagnostic_snapshot()
        except Exception:
            raw_diag = {}
        if isinstance(raw_diag, dict):
            send_stream = dict(raw_diag.get("send_stream") or {}) if isinstance(raw_diag.get("send_stream"), dict) else {}
            receive_stream = dict(raw_diag.get("receive_stream") or {}) if isinstance(raw_diag.get("receive_stream"), dict) else {}
            diag_ystore = dict(raw_diag.get("ystore") or {}) if isinstance(raw_diag.get("ystore"), dict) else {}
            room_diagnostic = {
                "pending_send_tasks": int(raw_diag.get("pending_send_tasks") or 0),
                "pending_store_tasks": int(raw_diag.get("pending_store_tasks") or 0),
                "update_total": int(raw_diag.get("update_total") or 0),
                "update_bytes_total": int(raw_diag.get("update_bytes_total") or 0),
                "destructive_update_block_total": int(raw_diag.get("destructive_update_block_total") or 0),
                "destructive_update_block_bytes": int(raw_diag.get("destructive_update_block_bytes") or 0),
                "inbound_guard_block_total": int(raw_diag.get("inbound_guard_block_total") or 0),
                "inbound_guard_block_bytes": int(raw_diag.get("inbound_guard_block_bytes") or 0),
                "inbound_guard_last_bytes": int(raw_diag.get("inbound_guard_last_bytes") or 0),
                "inbound_guard_last_block_bytes": int(raw_diag.get("inbound_guard_last_block_bytes") or 0),
                "inbound_guard_last_at": raw_diag.get("inbound_guard_last_at") or None,
                "inbound_guard_last_ago_s": raw_diag.get("inbound_guard_last_ago_s"),
                "inbound_guard_last_reset_reserved": bool(raw_diag.get("inbound_guard_last_reset_reserved")),
                "effective_repair_total": int(raw_diag.get("effective_repair_total") or 0),
                "effective_repair_bytes": int(raw_diag.get("effective_repair_bytes") or 0),
                "send_stream": {
                    "current_buffer_used": int(send_stream.get("current_buffer_used") or 0),
                    "max_buffer_size": int(send_stream.get("max_buffer_size") or 0),
                    "tasks_waiting_send": int(send_stream.get("tasks_waiting_send") or 0),
                    "tasks_waiting_receive": int(send_stream.get("tasks_waiting_receive") or 0),
                },
                "receive_stream": {
                    "current_buffer_used": int(receive_stream.get("current_buffer_used") or 0),
                    "max_buffer_size": int(receive_stream.get("max_buffer_size") or 0),
                    "tasks_waiting_send": int(receive_stream.get("tasks_waiting_send") or 0),
                    "tasks_waiting_receive": int(receive_stream.get("tasks_waiting_receive") or 0),
                },
                "ystore": {
                    "update_log_entries": int(diag_ystore.get("update_log_entries") or 0),
                    "update_log_bytes": int(diag_ystore.get("update_log_bytes") or 0),
                    "replay_window_bytes": int(diag_ystore.get("replay_window_bytes") or 0),
                    "last_update_bytes": int(diag_ystore.get("last_update_bytes") or 0),
                },
            }

    return {
        "webspace_id": key,
        "active": bool(room is not None),
        "generation": int(meta.get("generation") or 0),
        "create_total": int(meta.get("create_total") or 0),
        "reset_total": int(meta.get("reset_total") or 0),
        "drop_total": int(meta.get("drop_total") or 0),
        "last_created_at": meta.get("last_created_at"),
        "last_created_ago_s": _seconds_ago(meta.get("last_created_at"), now),
        "last_open_at": meta.get("last_open_at"),
        "last_open_ago_s": _seconds_ago(meta.get("last_open_at"), now),
        "last_reset_at": meta.get("last_reset_at"),
        "last_reset_ago_s": _seconds_ago(meta.get("last_reset_at"), now),
        "last_dropped_at": meta.get("last_dropped_at"),
        "last_dropped_ago_s": _seconds_ago(meta.get("last_dropped_at"), now),
        "open_total": int(meta.get("open_total") or 0),
        "cold_open_total": int(meta.get("cold_open_total") or 0),
        "reuse_total": int(meta.get("reuse_total") or 0),
        "single_pass_bootstrap_total": int(meta.get("single_pass_bootstrap_total") or 0),
        "last_open_mode": str(meta.get("last_open_mode") or "").strip() or None,
        "last_open_total_ms": meta.get("last_open_total_ms"),
        "last_open_apply_updates_ms": meta.get("last_open_apply_updates_ms"),
        "last_open_bootstrap_total_ms": meta.get("last_open_bootstrap_total_ms"),
        "last_open_bootstrap_mode": str(meta.get("last_open_bootstrap_mode") or "").strip() or None,
        "last_open_bootstrap_persisted_via": str(meta.get("last_open_bootstrap_persisted_via") or "").strip() or None,
        "last_open_bootstrap_single_pass": bool(meta.get("last_open_bootstrap_single_pass")),
        "last_reset_reason": str(meta.get("last_reset_reason") or "").strip() or None,
        "last_reset_closed_connections": int(meta.get("last_reset_closed_connections") or 0),
        "last_reset_closed_webrtc_peers": int(meta.get("last_reset_closed_webrtc_peers") or 0),
        "last_reset_room_dropped": bool(meta.get("last_reset_room_dropped")),
        "room_object_id": id(room) if room is not None else meta.get("last_room_object_id"),
        "ydoc_object_id": id(ydoc) if ydoc is not None else meta.get("last_ydoc_object_id"),
        "client_total": len(clients) if isinstance(clients, list) else 0,
        "ready": bool(getattr(room, "_ready", False)) if room is not None else False,
        "started": bool(getattr(started_event, "is_set", lambda: False)()) if started_event is not None else False,
        "task_group_active": bool(task_group is not None),
        "ystore_attached": bool(ystore is not None),
        "effective_branches": (
            getattr(room, "_diag_effective_branch_snapshot", None)
            if isinstance(getattr(room, "_diag_effective_branch_snapshot", None), dict)
            else {"ready": False, "error": "not_observed"}
        ),
        "ystore_runtime": ystore_runtime,
        "diagnostic": room_diagnostic,
        "update_send_stream": send_stream_stats,
        "update_receive_stream": recv_stream_stats,
    }


def _room_debug_snapshot_all(now: float) -> tuple[dict[str, Any], dict[str, int]]:
    room_keys = set()
    try:
        room_keys.update(str(key) for key in getattr(y_server, "rooms", {}).keys())
    except Exception:
        pass
    with _YROOM_LIFECYCLE_LOCK:
        room_keys.update(str(key) for key in _YROOM_LIFECYCLE.keys())

    room_details: dict[str, Any] = {}
    aggregated = {
        "active_room_total": 0,
        "room_create_total": 0,
        "room_reset_total": 0,
        "room_drop_total": 0,
        "room_generation_max": 0,
        "room_open_total": 0,
        "room_cold_open_total": 0,
        "room_reuse_total": 0,
        "room_single_pass_bootstrap_total": 0,
        "update_stream_buffer_used_total": 0,
        "update_stream_waiting_send_total": 0,
        "update_stream_waiting_receive_total": 0,
        "inbound_guard_block_total": 0,
        "inbound_guard_block_bytes": 0,
    }
    for key in sorted(room_keys):
        room = getattr(y_server, "rooms", {}).get(key)
        snapshot = _room_debug_snapshot(key, room, now)
        room_details[key] = snapshot
        aggregated["active_room_total"] += 1 if snapshot.get("active") else 0
        aggregated["room_create_total"] += int(snapshot.get("create_total") or 0)
        aggregated["room_reset_total"] += int(snapshot.get("reset_total") or 0)
        aggregated["room_drop_total"] += int(snapshot.get("drop_total") or 0)
        aggregated["room_open_total"] += int(snapshot.get("open_total") or 0)
        aggregated["room_cold_open_total"] += int(snapshot.get("cold_open_total") or 0)
        aggregated["room_reuse_total"] += int(snapshot.get("reuse_total") or 0)
        aggregated["room_single_pass_bootstrap_total"] += int(snapshot.get("single_pass_bootstrap_total") or 0)
        aggregated["room_generation_max"] = max(
            aggregated["room_generation_max"],
            int(snapshot.get("generation") or 0),
        )
        send_stream = snapshot.get("update_send_stream") if isinstance(snapshot.get("update_send_stream"), dict) else {}
        diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), dict) else {}
        aggregated["inbound_guard_block_total"] += int(diagnostic.get("inbound_guard_block_total") or 0)
        aggregated["inbound_guard_block_bytes"] += int(diagnostic.get("inbound_guard_block_bytes") or 0)
        aggregated["update_stream_buffer_used_total"] += int(send_stream.get("current_buffer_used") or 0)
        aggregated["update_stream_waiting_send_total"] += int(send_stream.get("tasks_waiting_send") or 0)
        aggregated["update_stream_waiting_receive_total"] += int(send_stream.get("tasks_waiting_receive") or 0)
    return room_details, aggregated


async def _close_room_stream_maybe(stream: Any) -> bool:
    if stream is None:
        return False
    closed = False
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
            closed = True
        except Exception:
            closed = False
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        try:
            result = aclose()
            if inspect.isawaitable(result):
                await result
            closed = True
        except Exception:
            pass
    return closed


async def _release_room_refs(webspace_id: str, room: Any) -> bool:
    released = False
    ydoc = getattr(room, "ydoc", None)
    if ydoc is not None:
        try:
            forget_room_observers(webspace_id, ydoc)
        except Exception:
            pass
    for attr in ("_update_send_stream", "_update_receive_stream"):
        try:
            stream = getattr(room, attr, None)
        except Exception:
            stream = None
        try:
            released = await _close_room_stream_maybe(stream) or released
        except Exception:
            pass

    clients = getattr(room, "clients", None)
    if isinstance(clients, list):
        try:
            clients.clear()
            released = True
        except Exception:
            pass

    for attr in (
        "awareness",
        "_on_message",
        "_started",
        "_exit_stack",
        "_task_group",
        "ydoc",
        "ystore",
        "_loop",
        "_thread_id",
        "ready",
        "log",
    ):
        if not hasattr(room, attr):
            continue
        try:
            setattr(room, attr, None)
            released = True
        except Exception:
            continue
    return released


async def _delete_ystore_backup_job(webspace_id: str) -> bool:
    try:
        sched = get_scheduler()
        await sched.delete(f"ystores.backup.{str(webspace_id or '').strip() or 'default'}")
        return True
    except Exception:
        _ylog.debug("failed to delete YStore backup job webspace=%s", webspace_id, exc_info=True)
        return False


def _cancel_idle_room_reset(webspace_id: str) -> bool:
    key = str(webspace_id or "").strip() or "default"
    task = _IDLE_ROOM_RESET_TASKS.pop(key, None)
    if task is None:
        return False
    current = asyncio.current_task()
    if task is not current and not task.done():
        task.cancel()
    return True


def _trim_allocator_after_yjs_room_reset() -> bool:
    if not _env_flag("ADAOS_YJS_ROOM_RESET_MALLOC_TRIM", True):
        return False
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if not callable(trim):
            return False
        return bool(trim(0))
    except Exception:
        return False


def _active_webrtc_peer_total_for_webspace(webspace_id: str) -> int:
    key = str(webspace_id or "").strip() or "default"
    try:
        from adaos.services.webrtc.peer import webrtc_peer_snapshot

        snapshot = webrtc_peer_snapshot()
    except Exception:
        return 0
    peers = snapshot.get("peers") if isinstance(snapshot, dict) else None
    if not isinstance(peers, list):
        return 0
    return sum(
        1
        for peer in peers
        if isinstance(peer, dict)
        and str(peer.get("webspace_id") or "").strip() == key
    )


def _active_yws_connection_total_for_webspace(webspace_id: str) -> int:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        return len(_ACTIVE_YWS_CONNECTIONS.get(key) or [])


def _webspace_has_live_transports(webspace_id: str) -> bool:
    key = str(webspace_id or "").strip() or "default"
    if _active_yws_connection_total_for_webspace(key) > 0:
        return True
    return _active_webrtc_peer_total_for_webspace(key) > 0


def _schedule_idle_room_reset(webspace_id: str, *, reason: str = "idle_room_eviction") -> bool:
    key = str(webspace_id or "").strip() or "default"
    if _IDLE_ROOM_EVICT_SEC <= 0.0:
        return False
    if key not in getattr(y_server, "rooms", {}):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    _cancel_idle_room_reset(key)

    async def _runner() -> None:
        try:
            await asyncio.sleep(_IDLE_ROOM_EVICT_SEC)
            if _webspace_has_live_transports(key):
                if _active_yws_connection_total_for_webspace(key) <= 0:
                    _schedule_idle_room_reset(key, reason=reason)
                return
            await reset_live_webspace_room(
                key,
                close_reason=reason,
                reset_route_runtime=False,
                prewarm_after_reset=False,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            _ylog.warning(
                "idle room eviction failed webspace=%s reason=%s",
                key,
                reason,
                exc_info=True,
            )
        finally:
            current = asyncio.current_task()
            if _IDLE_ROOM_RESET_TASKS.get(key) is current:
                _IDLE_ROOM_RESET_TASKS.pop(key, None)

    _IDLE_ROOM_RESET_TASKS[key] = asyncio.create_task(
        _runner(),
        name=f"adaos-yjs-idle-room-reset-{key}",
    )
    return True


async def _accept_websocket(websocket: WebSocket, *, channel: str) -> bool:
    try:
        await websocket.accept()
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as exc:
        if _is_websocket_accept_race(exc):
            _ylog.info(
                "%s websocket accept skipped because handshake was already closed client=%s",
                channel,
                _ws_client_str(websocket),
            )
            return False
        raise


def _transport_mark_open(name: str) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _TRANSPORT_LOCK:
        entry = _TRANSPORT_STATE.setdefault(
            key,
            {
                "active_connections": 0,
                "open_total": 0,
                "close_total": 0,
                "last_open_at": 0.0,
                "last_close_at": 0.0,
            },
        )
        entry["active_connections"] = int(entry.get("active_connections") or 0) + 1
        entry["open_total"] = int(entry.get("open_total") or 0) + 1
        entry["last_open_at"] = now


def _transport_mark_close(name: str) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _TRANSPORT_LOCK:
        entry = _TRANSPORT_STATE.setdefault(
            key,
            {
                "active_connections": 0,
                "open_total": 0,
                "close_total": 0,
                "last_open_at": 0.0,
                "last_close_at": 0.0,
            },
        )
        active = int(entry.get("active_connections") or 0) - 1
        entry["active_connections"] = max(0, active)
        entry["close_total"] = int(entry.get("close_total") or 0) + 1
        entry["last_close_at"] = now


def _publish_runtime_event(topic: str, payload: dict[str, Any] | None = None, *, source: str = "yjs.gateway") -> None:
    try:
        ctx = get_agent_ctx()
        ctx.bus.publish(DomainEvent(type=topic, payload=dict(payload or {}), source=source, ts=time.time()))
    except Exception:
        _log.debug("failed to publish runtime event topic=%s", topic, exc_info=True)


def _normalize_ws_event_topics(raw_topics: Any) -> set[str]:
    if not isinstance(raw_topics, list):
        return set()
    return {
        topic
        for topic in (str(raw or "").strip() for raw in raw_topics)
        if topic
    }


def _ws_event_topic_matches(subscription: str, event_type: str) -> bool:
    topic = str(subscription or "").strip()
    event = str(event_type or "").strip()
    if not topic or not event:
        return False
    if topic in {"*", ""}:
        return True
    if topic.endswith("*"):
        return event.startswith(topic[:-1])
    return event == topic


def _build_ws_event_message(
    event_type: str,
    payload: Any,
    *,
    source: str = "events_ws",
    ts: float | None = None,
) -> dict[str, Any]:
    return {
        "ch": "events",
        "t": "evt",
        "kind": str(event_type or "").strip(),
        "payload": payload if isinstance(payload, dict) else {"value": payload},
        "source": str(source or "events_ws").strip() or "events_ws",
        "ts": float(ts or time.time()),
    }


async def _send_ws_event_message(websocket: WebSocket, message: dict[str, Any]) -> None:
    try:
        await websocket.send_text(json.dumps(message))
    except (WebSocketDisconnect, RuntimeError):
        _unregister_ws_event_subscriptions(websocket)
        raise


def _iter_initial_ws_event_messages(topics: set[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if any(_ws_event_topic_matches(topic, "node.status") for topic in topics):
        try:
            from adaos.services.bootstrap import load_config as _load_config
            from adaos.services.system_model.service import (
                current_node_status_push_payload as _current_node_status_push_payload,
            )

            conf = _load_config()
            if str(getattr(conf, "role", "") or "").strip().lower() == "hub":
                messages.append(
                    _build_ws_event_message(
                        "node.status",
                        _current_node_status_push_payload(),
                        source="node.status",
                    )
                )
        except Exception:
            _ylog.debug("failed to snapshot node.status for ws subscriber", exc_info=True)
    if any(_ws_event_topic_matches(topic, "core.update.status") for topic in topics):
        try:
            from adaos.services.core_update import read_status as _read_core_update_status

            messages.append(
                _build_ws_event_message(
                    "core.update.status",
                    _read_core_update_status() or {},
                    source="core.update.status",
                )
            )
        except Exception:
            _ylog.debug("failed to snapshot core.update.status for ws subscriber", exc_info=True)
    if any(_ws_event_topic_matches(topic, "supervisor.update.status.raw") for topic in topics):
        try:
            from adaos.services.core_update import read_public_update_status as _read_public_update_status

            messages.append(
                _build_ws_event_message(
                    "supervisor.update.status.raw",
                    _read_public_update_status(),
                    source="supervisor.update.status.raw",
                )
            )
        except Exception:
            _ylog.debug("failed to snapshot supervisor.update.status.raw for ws subscriber", exc_info=True)
    return messages


def _request_webio_stream_snapshots(topics: set[str], *, transport: str) -> None:
    for topic in topics:
        token = str(topic or "").strip()
        prefix = "webio.stream."
        if not token.startswith(prefix):
            continue
        suffix = token[len(prefix):]
        parts = [str(part or "").strip() for part in suffix.split(".") if str(part or "").strip()]
        if len(parts) < 2:
            continue
        node_id = None
        if parts[0] == "nodes":
            if len(parts) < 3:
                continue
            webspace_id = _coerce_gateway_webspace_id(None)
            node_id = parts[1]
            receiver_parts = parts[2:]
        else:
            webspace_id = _coerce_gateway_webspace_id(parts[0])
            receiver_parts = parts[1:]
        if len(receiver_parts) >= 3 and receiver_parts[0] == "nodes":
            node_id = receiver_parts[1]
            receiver_parts = receiver_parts[2:]
        receiver = ".".join(receiver_parts).strip()
        if not webspace_id or not receiver:
            continue
        try:
            ctx = get_agent_ctx()
            payload = {
                "topic": token,
                "webspace_id": webspace_id,
                "receiver": receiver,
                "transport": str(transport or "ws"),
            }
            if node_id:
                payload["node_id"] = node_id
                payload["target_node_id"] = node_id
                payload["_meta"] = {"webspace_id": webspace_id, "target_node_id": node_id}
            ctx.bus.publish(
                DomainEvent(
                    type="webio.stream.snapshot.requested",
                    payload=payload,
                    source="events_ws",
                    ts=time.time(),
                )
            )
        except Exception:
            _ylog.debug("failed to request webio stream snapshot topic=%s", token, exc_info=True)


def _publish_webio_stream_subscription_change(
    topics: set[str],
    *,
    action: str,
    transport: str,
    connection_id: str | None = None,
) -> None:
    for topic in topics:
        token = str(topic or "").strip()
        prefix = "webio.stream."
        if not token.startswith(prefix):
            continue
        suffix = token[len(prefix):]
        parts = [str(part or "").strip() for part in suffix.split(".") if str(part or "").strip()]
        if len(parts) < 2:
            continue
        node_id = None
        if parts[0] == "nodes":
            if len(parts) < 3:
                continue
            webspace_id = _coerce_gateway_webspace_id(None)
            node_id = parts[1]
            receiver_parts = parts[2:]
        else:
            webspace_id = _coerce_gateway_webspace_id(parts[0])
            receiver_parts = parts[1:]
        if len(receiver_parts) >= 3 and receiver_parts[0] == "nodes":
            node_id = receiver_parts[1]
            receiver_parts = receiver_parts[2:]
        receiver = ".".join(receiver_parts).strip()
        if not webspace_id or not receiver:
            continue
        try:
            ctx = get_agent_ctx()
            payload = {
                "topic": token,
                "webspace_id": webspace_id,
                "receiver": receiver,
                "transport": str(transport or "ws"),
                "action": str(action or "").strip() or "subscribed",
            }
            if connection_id:
                payload["connection_id"] = str(connection_id)
                payload["subscription_id"] = f"{transport}:{connection_id}:{token}"
            if node_id:
                payload["node_id"] = node_id
                payload["target_node_id"] = node_id
                payload["_meta"] = {"webspace_id": webspace_id, "target_node_id": node_id}
            ctx.bus.publish(
                DomainEvent(
                    type="webio.stream.subscription.changed",
                    payload=payload,
                    source="events_ws",
                    ts=time.time(),
                )
            )
        except Exception:
            _ylog.debug("failed to publish webio stream subscription change topic=%s", token, exc_info=True)


async def _send_initial_ws_event_messages(websocket: WebSocket, topics: set[str]) -> None:
    for message in _iter_initial_ws_event_messages(topics):
        try:
            await _send_ws_event_message(websocket, message)
        except (WebSocketDisconnect, RuntimeError):
            return


def _ensure_ws_event_forwarder() -> None:
    global _WS_EVENT_FORWARDER_INSTALLED
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        if _WS_EVENT_FORWARDER_INSTALLED:
            return
        ctx = get_agent_ctx()
        ctx.bus.subscribe("*", _forward_ws_bus_event)
        _WS_EVENT_FORWARDER_INSTALLED = True


def _register_ws_event_subscriptions(
    websocket: WebSocket,
    loop: asyncio.AbstractEventLoop,
    raw_topics: Any,
) -> set[str]:
    topics = _normalize_ws_event_topics(raw_topics)
    if not topics:
        return set()
    _ensure_ws_event_forwarder()
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        entry = _WS_EVENT_SUBSCRIBERS.setdefault(
            id(websocket),
            {
                "websocket": websocket,
                "loop": loop,
                "topics": set(),
            },
        )
        entry["loop"] = loop
        tracked = entry.setdefault("topics", set())
        added = set(topics) - set(tracked)
        tracked.update(topics)
    if added:
        _publish_webio_stream_subscription_change(
            added,
            action="subscribed",
            transport="ws",
            connection_id=str(id(websocket)),
        )
    return added


def _unregister_ws_event_subscriptions(websocket: WebSocket) -> None:
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        entry = _WS_EVENT_SUBSCRIBERS.pop(id(websocket), None)
    topics = set(entry.get("topics") or []) if isinstance(entry, dict) else set()
    if topics:
        _publish_webio_stream_subscription_change(
            topics,
            action="unsubscribed",
            transport="ws",
            connection_id=str(id(websocket)),
        )


def _unregister_ws_event_subscription_topics(websocket: WebSocket, raw_topics: Any) -> set[str]:
    topics = _normalize_ws_event_topics(raw_topics)
    if not topics:
        return set()
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        entry = _WS_EVENT_SUBSCRIBERS.get(id(websocket))
        if not isinstance(entry, dict):
            return set()
        tracked = entry.setdefault("topics", set())
        removed = set(topics) & set(tracked)
        tracked.difference_update(removed)
        if not tracked:
            _WS_EVENT_SUBSCRIBERS.pop(id(websocket), None)
    if removed:
        _publish_webio_stream_subscription_change(
            removed,
            action="unsubscribed",
            transport="ws",
            connection_id=str(id(websocket)),
        )
    return removed


def _forward_ws_bus_event(ev: DomainEvent) -> None:
    event_type = str(getattr(ev, "type", "") or "").strip()
    if not event_type:
        return
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        subscribers = [
            dict(entry)
            for entry in _WS_EVENT_SUBSCRIBERS.values()
            if any(_ws_event_topic_matches(topic, event_type) for topic in entry.get("topics", set()))
        ]
    if not subscribers:
        return
    message = _build_ws_event_message(
        event_type,
        getattr(ev, "payload", {}) or {},
        source=str(getattr(ev, "source", "") or "events_ws"),
        ts=float(getattr(ev, "ts", 0.0) or time.time()),
    )
    for entry in subscribers:
        websocket = entry.get("websocket")
        loop = entry.get("loop")
        if websocket is None or not isinstance(loop, asyncio.AbstractEventLoop):
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                _send_ws_event_message(websocket, message),
                loop,
            )
        except Exception:
            _unregister_ws_event_subscriptions(websocket)


def _track_yws_connection(webspace_id: str, websocket: WebSocket, *, device_id: str | None = None) -> None:
    key = str(webspace_id or "").strip() or "default"
    device_key = str(device_id or "").strip() or "unknown"
    _cancel_idle_room_reset(key)
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.setdefault(key, [])
        if websocket not in items:
            items.append(websocket)
        clients = _ACTIVE_YWS_CLIENTS.setdefault(key, {})
        clients[device_key] = int(clients.get(device_key) or 0) + 1


def _websocket_device_id(websocket: WebSocket) -> str:
    try:
        params = getattr(websocket, "query_params", {}) or {}
        return str(params.get("dev") or "unknown").strip() or "unknown"
    except Exception:
        return "unknown"


def _active_yws_connection_total_for_client(webspace_id: str, dev_id: str) -> int:
    key = str(webspace_id or "").strip() or "default"
    device_key = str(dev_id or "").strip() or "unknown"
    with _ACTIVE_YWS_LOCK:
        clients = _ACTIVE_YWS_CLIENTS.get(key)
        if isinstance(clients, dict):
            return max(0, int(clients.get(device_key) or 0))
        return sum(
            1
            for websocket in list(_ACTIVE_YWS_CONNECTIONS.get(key) or [])
            if _websocket_device_id(websocket) == device_key
        )


def _active_yws_connection_total_for_device(dev_id: str) -> int:
    device_key = str(dev_id or "").strip() or "unknown"
    if not device_key or device_key == "unknown":
        return 0
    total = 0
    with _ACTIVE_YWS_LOCK:
        for clients in _ACTIVE_YWS_CLIENTS.values():
            if isinstance(clients, dict):
                total += max(0, int(clients.get(device_key) or 0))
    return total


def _should_mark_yws_browser_session_offline(dev_id: str) -> bool:
    return _active_yws_connection_total_for_device(dev_id) <= 0


def _active_yws_client_rows() -> list[dict[str, Any]]:
    with _ACTIVE_YWS_LOCK:
        clients = {
            webspace_id: dict(device_counts)
            for webspace_id, device_counts in _ACTIVE_YWS_CLIENTS.items()
            if isinstance(device_counts, dict)
        }
    rows: list[dict[str, Any]] = []
    for webspace_id, device_counts in clients.items():
        for device_id, count in sorted(device_counts.items()):
            rows.append(
                {
                    "webspace_id": str(webspace_id or "").strip() or "default",
                    "dev_id": str(device_id or "").strip() or "unknown",
                    "session_count": max(0, int(count or 0)),
                }
            )
    rows.sort(key=lambda item: (-int(item.get("session_count") or 0), str(item.get("dev_id") or "")))
    return rows


async def _close_existing_yws_client_connections(webspace_id: str, dev_id: str) -> int:
    key = str(webspace_id or "").strip() or "default"
    device_key = str(dev_id or "").strip() or "unknown"
    if not device_key or device_key == "unknown":
        return 0
    with _ACTIVE_YWS_LOCK:
        sockets = [
            websocket
            for websocket in list(_ACTIVE_YWS_CONNECTIONS.get(key) or [])
            if _websocket_device_id(websocket) == device_key
        ]
    if len(sockets) < _YWS_MAX_ACTIVE_PER_CLIENT:
        return 0
    closed = 0
    for websocket in sockets:
        try:
            await websocket.close(code=1012, reason="replaced_by_new_yws_session")
            closed += 1
        except Exception:
            pass
    if closed:
        _YWS_GUARD_DIAG["last_replaced_at"] = time.time()
        _YWS_GUARD_DIAG["last_replaced_webspace_id"] = key
        _YWS_GUARD_DIAG["last_replaced_dev_id"] = device_key
        _YWS_GUARD_DIAG["replaced_total"] = int(_YWS_GUARD_DIAG.get("replaced_total") or 0) + closed
        _ylog.warning(
            "yws guard replaced stale client sessions webspace=%s dev=%s closed=%s max_active_per_client=%s",
            key,
            device_key,
            closed,
            _YWS_MAX_ACTIVE_PER_CLIENT,
        )
        await asyncio.sleep(0)
    return closed


def _record_yws_open(webspace_id: str, dev_id: str) -> None:
    now = time.time()
    key = f"{str(webspace_id or '').strip() or 'default'}::{str(dev_id or '').strip() or 'unknown'}"
    with _YWS_STORM_LOCK:
        _YWS_OPEN_HISTORY.append(now)
        items = _YWS_CLIENT_OPEN_HISTORY.setdefault(key, deque(maxlen=64))
        items.append(now)
        cutoff = now - 60.0
        stale_keys: list[str] = []
        for client_key, queue in _YWS_CLIENT_OPEN_HISTORY.items():
            while queue and queue[0] < cutoff:
                queue.popleft()
            if not queue:
                stale_keys.append(client_key)
        for client_key in stale_keys:
            _YWS_CLIENT_OPEN_HISTORY.pop(client_key, None)
        recent_15s = sum(1 for ts in items if ts >= now - 15.0)
    if recent_15s >= 8:
        _ylog.warning(
            "yws reconnect storm detected webspace=%s dev=%s opens_15s=%s",
            str(webspace_id or "").strip() or "default",
            str(dev_id or "").strip() or "unknown",
            recent_15s,
        )


def _yws_guard_quarantine_key(webspace_id: str, dev_id: str | None = None) -> str:
    webspace_key = str(webspace_id or "").strip() or "default"
    dev_key = str(dev_id or "").strip() or "*"
    return f"{webspace_key}::{dev_key}"


def _set_yws_guard_quarantine_locked(key: str, now: float) -> tuple[float, float, int]:
    incident = _YWS_GUARD_INCIDENTS.get(key) or {}
    last_at = float(incident.get("last_at") or 0.0)
    count = int(incident.get("count") or 0)
    if last_at <= 0.0 or now - last_at > _YWS_GUARD_ESCALATION_WINDOW_S:
        count = 0
    count += 1
    base_ttl = max(0.0, float(_YWS_GUARD_COOLDOWN_S))
    max_ttl = max(base_ttl, float(_YWS_GUARD_MAX_COOLDOWN_S))
    ttl = min(max_ttl, base_ttl * float(2 ** max(0, count - 1))) if base_ttl > 0.0 else 0.0
    until = now + ttl
    _YWS_GUARD_INCIDENTS[key] = {
        "count": float(count),
        "last_at": now,
        "last_ttl_s": ttl,
        "until": until,
    }
    _YWS_GUARD_QUARANTINE_UNTIL[key] = until
    _YWS_GUARD_DIAG["last_quarantine_ttl_s"] = ttl
    _YWS_GUARD_DIAG["last_quarantine_incident_count"] = count
    return until, ttl, count


def _yws_guard_log(
    *,
    webspace_id: str,
    dev_id: str,
    reason: str,
    active_total: int,
    recent_10s: int,
    client_15s: int,
    cooldown_s: float | None = None,
    incident_count: int | None = None,
) -> None:
    now = time.time()
    log_key = f"{webspace_id}:{dev_id}:{reason}"
    with _YWS_STORM_LOCK:
        last = float(_YWS_GUARD_LAST_LOG_AT.get(log_key) or 0.0)
        if now - last < 5.0:
            return
        _YWS_GUARD_LAST_LOG_AT[log_key] = now
    _ylog.warning(
        "yws guard rejected connection webspace=%s dev=%s reason=%s active=%s recent_open_10s=%s client_open_15s=%s cooldown_s=%.1f incident=%s",
        webspace_id,
        dev_id,
        reason,
        active_total,
        recent_10s,
        client_15s,
        float(cooldown_s if cooldown_s is not None else _YWS_GUARD_COOLDOWN_S),
        incident_count,
    )


def _yws_guard_should_notify(*, webspace_id: str, dev_id: str, reason: str) -> bool:
    now = time.time()
    notify_key = f"{str(webspace_id or '').strip() or 'default'}:{str(dev_id or '').strip() or 'unknown'}:{str(reason or '').strip() or 'guard'}"
    with _YWS_STORM_LOCK:
        last = float(_YWS_GUARD_LAST_NOTIFY_AT.get(notify_key) or 0.0)
        if now - last < _YWS_GUARD_NOTIFY_INTERVAL_S:
            return False
        _YWS_GUARD_LAST_NOTIFY_AT[notify_key] = now
    return True


def _yws_guard_reject_reason(webspace_id: str, dev_id: str) -> tuple[str, dict[str, Any]]:
    webspace_key = str(webspace_id or "").strip() or "default"
    dev_key = str(dev_id or "").strip() or "unknown"
    now = time.time()
    active_total = _active_yws_connection_total_for_webspace(webspace_key)
    reason = ""
    recent_10s = 0
    client_15s = 0
    quarantine_until = 0.0
    quarantine_ttl_s: float | None = None
    quarantine_incident_count: int | None = None
    with _YWS_STORM_LOCK:
        cutoff_60 = now - 60.0
        while _YWS_OPEN_HISTORY and _YWS_OPEN_HISTORY[0] < cutoff_60:
            _YWS_OPEN_HISTORY.popleft()
        stale_keys: list[str] = []
        for client_key, queue in _YWS_CLIENT_OPEN_HISTORY.items():
            while queue and queue[0] < cutoff_60:
                queue.popleft()
            if not queue:
                stale_keys.append(client_key)
        for client_key in stale_keys:
            _YWS_CLIENT_OPEN_HISTORY.pop(client_key, None)
        for key0 in list(_YWS_GUARD_QUARANTINE_UNTIL.keys()):
            if float(_YWS_GUARD_QUARANTINE_UNTIL.get(key0) or 0.0) <= now:
                _YWS_GUARD_QUARANTINE_UNTIL.pop(key0, None)
        recent_10s = sum(1 for ts in _YWS_OPEN_HISTORY if ts >= now - 10.0)
        client_key = _yws_guard_quarantine_key(webspace_key, dev_key)
        client_queue = _YWS_CLIENT_OPEN_HISTORY.get(client_key) or deque()
        client_15s = sum(1 for ts in client_queue if ts >= now - 15.0)
        quarantine_until = max(
            float(_YWS_GUARD_QUARANTINE_UNTIL.get(client_key) or 0.0),
            float(_YWS_GUARD_QUARANTINE_UNTIL.get(_yws_guard_quarantine_key(webspace_key)) or 0.0),
        )
        if quarantine_until > now:
            reason = "quarantined"
        elif active_total >= _YWS_MAX_ACTIVE_PER_WEBSPACE:
            reason = "active_limit"
        elif client_15s >= _YWS_GUARD_CLIENT_OPEN_15S:
            reason = "client_reconnect_storm"
            quarantine_until, quarantine_ttl_s, quarantine_incident_count = _set_yws_guard_quarantine_locked(
                client_key,
                now,
            )
        elif recent_10s >= _YWS_GUARD_RECENT_OPEN_10S:
            reason = "webspace_reconnect_storm"
            quarantine_until, quarantine_ttl_s, quarantine_incident_count = _set_yws_guard_quarantine_locked(
                _yws_guard_quarantine_key(webspace_key),
                now,
            )
        if reason:
            _YWS_GUARD_DIAG["reject_total"] = int(_YWS_GUARD_DIAG.get("reject_total") or 0) + 1
            _YWS_GUARD_DIAG["last_reject_at"] = now
            _YWS_GUARD_DIAG["last_reject_reason"] = reason
            _YWS_GUARD_DIAG["last_reject_webspace_id"] = webspace_key
            _YWS_GUARD_DIAG["last_reject_dev_id"] = dev_key
            if quarantine_ttl_s is not None:
                _YWS_GUARD_DIAG["last_reject_quarantine_ttl_s"] = quarantine_ttl_s
            if quarantine_incident_count is not None:
                _YWS_GUARD_DIAG["last_reject_incident_count"] = quarantine_incident_count
    diag = {
        "active_total": active_total,
        "recent_open_10s": recent_10s,
        "client_open_15s": client_15s,
        "quarantine_until": quarantine_until,
        "quarantine_ttl_s": quarantine_ttl_s,
        "quarantine_incident_count": quarantine_incident_count,
    }
    if reason:
        _yws_guard_log(
            webspace_id=webspace_key,
            dev_id=dev_key,
            reason=reason,
            active_total=active_total,
            recent_10s=recent_10s,
            client_15s=client_15s,
            cooldown_s=quarantine_ttl_s,
            incident_count=quarantine_incident_count,
        )
    return reason, diag


def _yws_storm_snapshot(now: float) -> dict[str, Any]:
    active_clients = _active_yws_client_rows()
    with _YWS_STORM_LOCK:
        recent_10s = sum(1 for ts in _YWS_OPEN_HISTORY if ts >= now - 10.0)
        recent_60s = sum(1 for ts in _YWS_OPEN_HISTORY if ts >= now - 60.0)
        quarantined_total = sum(
            1 for until in _YWS_GUARD_QUARANTINE_UNTIL.values() if float(until or 0.0) > now
        )
        incident_total = len(_YWS_GUARD_INCIDENTS)
        guard_diag = dict(_YWS_GUARD_DIAG)
        hot_clients: list[dict[str, Any]] = []
        for key, queue in _YWS_CLIENT_OPEN_HISTORY.items():
            recent_15s = sum(1 for ts in queue if ts >= now - 15.0)
            if recent_15s <= 0:
                continue
            webspace_id, _, dev_id = key.partition("::")
            hot_clients.append(
                {
                    "webspace_id": webspace_id or "default",
                    "dev_id": dev_id or "unknown",
                    "open_15s": recent_15s,
                }
            )
    hot_clients.sort(key=lambda item: (-int(item.get("open_15s") or 0), str(item.get("dev_id") or "")))
    return {
        "recent_open_10s": recent_10s,
        "recent_open_60s": recent_60s,
        "storm_detected": recent_10s >= 8,
        "hot_clients": hot_clients[:3],
        "active_clients": active_clients[:8],
        "guard": {
            "max_active_per_webspace": _YWS_MAX_ACTIVE_PER_WEBSPACE,
            "max_active_per_client": _YWS_MAX_ACTIVE_PER_CLIENT,
            "recent_open_10s_limit": _YWS_GUARD_RECENT_OPEN_10S,
            "client_open_15s_limit": _YWS_GUARD_CLIENT_OPEN_15S,
            "cooldown_s": _YWS_GUARD_COOLDOWN_S,
            "max_cooldown_s": _YWS_GUARD_MAX_COOLDOWN_S,
            "escalation_window_s": _YWS_GUARD_ESCALATION_WINDOW_S,
            "notify_interval_s": _YWS_GUARD_NOTIFY_INTERVAL_S,
            "quarantined_total": quarantined_total,
            "incident_total": incident_total,
            **guard_diag,
        },
    }


def _untrack_yws_connection(webspace_id: str, websocket: WebSocket) -> None:
    key = str(webspace_id or "").strip() or "default"
    remaining_connections = 0
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.get(key)
        if not items:
            device_key = None
        else:
            try:
                items.remove(websocket)
            except ValueError:
                pass
            remaining_connections = len(items)
        if not items:
            _ACTIVE_YWS_CONNECTIONS.pop(key, None)
        device_key = _websocket_device_id(websocket)
        clients = _ACTIVE_YWS_CLIENTS.get(key)
        if clients:
            remaining = int(clients.get(device_key) or 0) - 1
            if remaining > 0:
                clients[device_key] = remaining
            else:
                clients.pop(device_key, None)
            if not clients:
                _ACTIVE_YWS_CLIENTS.pop(key, None)
    if remaining_connections <= 0:
        room = getattr(y_server, "rooms", {}).get(key)
        if room is not None:
            diag_logger = getattr(room, "_diag_log_pressure", None)
            if callable(diag_logger):
                try:
                    diag_logger("last_client_detached", force=True)
                except Exception:
                    pass
    if remaining_connections <= 0:
        _schedule_idle_room_reset(key)


def active_browser_session_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _ACTIVE_YWS_LOCK:
        clients = {
            webspace_id: dict(device_counts)
            for webspace_id, device_counts in _ACTIVE_YWS_CLIENTS.items()
            if isinstance(device_counts, dict)
        }
    peers: list[dict[str, Any]] = []
    for webspace_id, device_counts in clients.items():
        for device_id, session_count in sorted(device_counts.items()):
            token = str(device_id or "").strip()
            if not token:
                continue
            peers.append(
                {
                    "device_id": token,
                    "webspace_id": str(webspace_id or "").strip() or "default",
                    "connection_state": "connected",
                    "yjs_channel_state": "open",
                    "session_count": int(session_count or 0),
                    "source": "yws_gateway",
                }
            )
    return {
        "peer_total": len(peers),
        "peers": peers,
        "updated_at": now,
    }


async def close_webspace_yws_connections(
    webspace_id: str,
    *,
    code: int = 1012,
    reason: str = "webspace_reload",
) -> int:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        sockets = list(_ACTIVE_YWS_CONNECTIONS.get(key) or [])
    closed = 0
    close_reason = str(reason or "webspace_reload")[:120]
    for websocket in sockets:
        try:
            await websocket.close(code=code, reason=close_reason)
            closed += 1
        except Exception:
            pass
    if closed:
        await asyncio.sleep(0)
    return closed


async def close_webspace_webrtc_peers(
    webspace_id: str,
    *,
    reason: str = "webspace_reload",
) -> int:
    try:
        from adaos.services.webrtc.peer import close_peers_for_webspace
    except Exception:
        return 0
    try:
        return int(await close_peers_for_webspace(webspace_id, reason=reason) or 0)
    except Exception:
        _ylog.debug(
            "failed to close webrtc peers for webspace=%s reason=%s",
            webspace_id,
            reason,
            exc_info=True,
        )
        return 0


async def reset_hub_route_runtime(
    *,
    reason: str = "webspace_reload",
    notify_browser: bool = True,
) -> dict[str, Any]:
    try:
        from adaos.services.bootstrap import request_hub_root_route_reset
    except Exception:
        return {
            "ok": False,
            "reason": str(reason or "").strip() or "route_reset",
            "notify_browser": bool(notify_browser),
            "skipped": "route_reset_unavailable",
        }
    try:
        result = await request_hub_root_route_reset(
            reason=str(reason or "").strip() or "route_reset",
            notify_browser=bool(notify_browser),
        )
    except Exception as exc:
        _ylog.debug(
            "failed to reset hub route runtime reason=%s",
            reason,
            exc_info=True,
        )
        return {
            "ok": False,
            "reason": str(reason or "").strip() or "route_reset",
            "notify_browser": bool(notify_browser),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return dict(result) if isinstance(result, dict) else {"ok": True, "result": result}


async def reset_live_webspace_room(
    webspace_id: str,
    *,
    close_reason: str = "webspace_reload",
    persist_ystore_snapshot: bool = True,
    reset_route_runtime: bool = True,
    prewarm_after_reset: bool | None = None,
) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    _cancel_idle_room_reset(key)
    if reset_route_runtime:
        route_reset = await reset_hub_route_runtime(
            reason=f"yjs:{close_reason}",
            notify_browser=True,
        )
    else:
        route_reset = {
            "ok": True,
            "reason": f"yjs:{close_reason}",
            "notify_browser": False,
            "skipped": "route_reset_disabled",
        }
    closed_webrtc_peers = await close_webspace_webrtc_peers(
        key,
        reason=close_reason,
    )
    closed_connections = await close_webspace_yws_connections(
        key,
        code=1012,
        reason=close_reason,
    )
    if closed_connections or closed_webrtc_peers or bool(route_reset.get("closed_tunnels")):
        # Let the active serve() coroutines observe disconnect and run cleanup before
        # a new room is created for the same webspace.
        await asyncio.sleep(0.15)

    room = y_server.rooms.pop(key, None)
    if room is not None:
        diag_logger = getattr(room, "_diag_log_pressure", None)
        if callable(diag_logger):
            try:
                diag_logger(f"room_reset:{close_reason}", force=True)
            except Exception:
                pass
    _mark_room_reset(
        key,
        close_reason=close_reason,
        room=room,
        room_dropped=room is not None,
        closed_connections=closed_connections,
        closed_webrtc_peers=closed_webrtc_peers,
    )
    _room_locks.pop(key, None)
    room_stopped = False
    ystore_stopped = False
    ystore_evicted = False
    ystore_snapshot_persisted = False
    scheduler_job_deleted = False
    runtime_compaction_requested = False
    room_refs_released = False
    gc_collected = 0
    malloc_trimmed = False
    room_prewarmed = False
    room_prewarm_error = ""

    scheduler_job_deleted = await _delete_ystore_backup_job(key)

    if room is not None:
        stop_room = getattr(room, "stop", None)
        if callable(stop_room):
            try:
                result = stop_room()
                if inspect.isawaitable(result):
                    await result
                room_stopped = True
            except Exception:
                room_stopped = False
        ystore = getattr(room, "ystore", None)
        if ystore is not None:
            try:
                await _stop_ystore_maybe_async(ystore)
                ystore_stopped = True
            except Exception:
                ystore_stopped = False
            try:
                eviction = await evict_ystore_for_webspace(
                    key,
                    store=ystore,
                    persist_snapshot=bool(persist_ystore_snapshot),
                    compact_runtime=True,
                    backup_kind=f"room_reset:{close_reason}",
                )
            except Exception:
                eviction = {
                    "ok": False,
                    "persisted": False,
                    "backup_skipped": False,
                    "ystore_found": False,
                }
                _ylog.warning(
                    "failed to evict YStore for webspace=%s close_reason=%s",
                    key,
                    close_reason,
                    exc_info=True,
                )
            ystore_evicted = bool(eviction.get("ystore_found"))
            ystore_snapshot_persisted = bool(eviction.get("persisted"))
            runtime_compaction_requested = bool(
                ystore_snapshot_persisted or eviction.get("backup_skipped")
            )
        room_refs_released = await _release_room_refs(key, room)
        if room_refs_released:
            try:
                gc_collected = int(gc.collect() or 0)
            except Exception:
                gc_collected = 0
    else:
        try:
            eviction = await evict_ystore_for_webspace(
                key,
                persist_snapshot=bool(persist_ystore_snapshot),
                compact_runtime=True,
                backup_kind=f"room_reset:{close_reason}",
            )
        except Exception:
            eviction = {
                "ok": False,
                "persisted": False,
                "backup_skipped": False,
                "ystore_found": False,
            }
            _ylog.warning(
                "failed to evict detached YStore for webspace=%s close_reason=%s",
                key,
                close_reason,
                exc_info=True,
            )
        ystore_evicted = bool(eviction.get("ystore_found"))
        ystore_snapshot_persisted = bool(eviction.get("persisted"))
        ystore_stopped = ystore_evicted
        runtime_compaction_requested = bool(
            ystore_snapshot_persisted or eviction.get("backup_skipped")
        )
        try:
            gc_collected = int(gc.collect() or 0)
        except Exception:
            gc_collected = 0

    malloc_trimmed = _trim_allocator_after_yjs_room_reset()

    should_prewarm_after_reset = (
        str(os.getenv("ADAOS_YJS_PREWARM_ROOM_AFTER_RESET", "1") or "1").strip().lower()
        not in {"0", "false", "no", "off"}
        if prewarm_after_reset is None
        else bool(prewarm_after_reset)
    )
    if should_prewarm_after_reset:
        try:
            await y_server.get_room(key)
            room_prewarmed = True
        except Exception as exc:
            room_prewarmed = False
            room_prewarm_error = f"{type(exc).__name__}: {exc}"
            _ylog.debug(
                "failed to prewarm YRoom after reset webspace=%s reason=%s",
                key,
                close_reason,
                exc_info=True,
            )

    return {
        "webspace_id": key,
        "route_reset": route_reset,
        "closed_webrtc_peers": closed_webrtc_peers,
        "closed_connections": closed_connections,
        "room_dropped": room is not None,
        "persist_ystore_snapshot": bool(persist_ystore_snapshot),
        "reset_route_runtime": bool(reset_route_runtime),
        "room_stopped": room_stopped,
        "ystore_stopped": ystore_stopped,
        "ystore_evicted": ystore_evicted,
        "ystore_snapshot_persisted": ystore_snapshot_persisted,
        "scheduler_job_deleted": scheduler_job_deleted,
        "runtime_compaction_requested": runtime_compaction_requested,
        "room_refs_released": room_refs_released,
        "gc_collected": gc_collected,
        "malloc_trimmed": malloc_trimmed,
        "prewarm_after_reset": should_prewarm_after_reset,
        "room_prewarmed": room_prewarmed,
        "room_prewarm_error": room_prewarm_error,
    }


def _y_server_runtime_snapshot() -> dict[str, Any]:
    task = _y_server_task
    requested = bool(_y_server_started)
    started_handle = getattr(y_server, "started", None)
    started_event = bool(getattr(started_handle, "is_set", lambda: False)())
    task_running = bool(task is not None and not task.done())
    task_done = bool(task is not None and task.done())
    task_cancelled = bool(task is not None and task.cancelled())
    rooms = getattr(y_server, "rooms", None)
    room_total = len(rooms) if isinstance(rooms, dict) else 0
    room_effective_branches: dict[str, Any] = {}
    if isinstance(rooms, dict):
        for room_name, room in list(rooms.items()):
            room_key = str(room_name or "")
            try:
                clients = getattr(room, "clients", None)
                client_total = len(clients) if hasattr(clients, "__len__") else None
            except Exception:
                client_total = None
            cached_branches = getattr(room, "_diag_effective_branch_snapshot", None)
            room_effective_branches[room_key] = {
                "client_total": client_total,
                "branches": cached_branches if isinstance(cached_branches, dict) else {"ready": False, "error": "not_observed"},
            }
    error: str | None = None
    if task_done and not task_cancelled:
        try:
            exc = task.exception()
        except Exception as exc:  # pragma: no cover - defensive runtime snapshot
            error = f"{type(exc).__name__}: {exc}"
        else:
            if exc is not None:
                error = f"{type(exc).__name__}: {exc}"
    ready = bool(requested and started_event and task_running and not error)
    return {
        "requested": requested,
        "started_event": started_event,
        "task_running": task_running,
        "task_done": task_done,
        "task_cancelled": task_cancelled,
        "room_total": room_total,
        "room_effective_branches": room_effective_branches,
        "ready": ready,
        "error": error,
    }


def _gateway_lifecycle_manager() -> str:
    token = str(os.getenv("ADAOS_SUPERVISOR_ENABLED", "0") or "").strip().lower()
    return "supervisor" if token in {"1", "true", "yes", "on"} else "runtime"


def _gateway_transport_ownership_snapshot() -> dict[str, dict[str, Any]]:
    lifecycle_manager = _gateway_lifecycle_manager()
    try:
        from adaos.services import realtime_sidecar as _realtime_sidecar_mod

        route_contract = _realtime_sidecar_mod.realtime_sidecar_route_tunnel_contract()
    except Exception:
        route_contract = {}
    ws_contract = route_contract.get("ws") if isinstance(route_contract.get("ws"), dict) else {}
    yws_contract = route_contract.get("yws") if isinstance(route_contract.get("yws"), dict) else {}
    return {
        "ws": {
            "current_owner": ws_contract.get("current_owner") or "runtime",
            "lifecycle_manager": ws_contract.get("lifecycle_manager") or lifecycle_manager,
            "planned_owner": ws_contract.get("planned_owner") or "sidecar",
            "migration_phase": ws_contract.get("migration_phase") or "phase_2_route_tunnel_ownership",
            "logical_channels": list(
                ws_contract.get("logical_channels")
                or [
                    "hub_member.command",
                    "hub_member.event",
                    "hub_member.presence",
                ]
            ),
            "current_support": ws_contract.get("current_support") or "planned",
            "delegation_mode": ws_contract.get("delegation_mode") or "not_implemented",
            "listener_ready": bool(ws_contract.get("listener_ready")),
            "handoff_ready": bool(ws_contract.get("handoff_ready")),
            "handoff_blockers": list(
                ws_contract.get("blockers")
                or [
                    "browser route websocket still terminates in the runtime FastAPI app",
                ]
            ),
        },
        "yws": {
            "current_owner": yws_contract.get("current_owner") or "runtime",
            "lifecycle_manager": yws_contract.get("lifecycle_manager") or lifecycle_manager,
            "planned_owner": yws_contract.get("planned_owner") or "sidecar",
            "migration_phase": yws_contract.get("migration_phase") or "phase_2_route_tunnel_ownership",
            "logical_channels": list(
                yws_contract.get("logical_channels")
                or [
                    "hub_member.sync",
                ]
            ),
            "current_support": yws_contract.get("current_support") or "planned",
            "delegation_mode": yws_contract.get("delegation_mode") or "not_implemented",
            "listener_ready": bool(yws_contract.get("listener_ready")),
            "handoff_ready": bool(yws_contract.get("handoff_ready")),
            "handoff_blockers": list(
                yws_contract.get("blockers")
                or [
                    "Yjs websocket/session ownership still lives in the runtime gateway",
                ]
            ),
        },
    }


def gateway_transport_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _TRANSPORT_LOCK:
        state = json.loads(json.dumps(_TRANSPORT_STATE))
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        last_open_at = entry.get("last_open_at")
        last_close_at = entry.get("last_close_at")
        entry["last_open_ago_s"] = (
            round(max(0.0, now - float(last_open_at)), 3)
            if isinstance(last_open_at, (int, float)) and float(last_open_at) > 0.0
            else None
        )
        entry["last_close_ago_s"] = (
            round(max(0.0, now - float(last_close_at)), 3)
            if isinstance(last_close_at, (int, float)) and float(last_close_at) > 0.0
            else None
        )
    yws_state = state.get("yws") if isinstance(state.get("yws"), dict) else None
    if yws_state is not None:
        yws_state.update(_yws_storm_snapshot(now))
    room_details, room_aggregates = _room_debug_snapshot_all(now)
    if yws_state is not None:
        yws_state.update(room_aggregates)
    return {
        "transports": state,
        "servers": {
            "yws": _y_server_runtime_snapshot(),
        },
        "rooms": room_details,
        "commands": _command_trace_snapshot(now),
        "ownership": _gateway_transport_ownership_snapshot(),
        "updated_at": now,
    }


def _ws_trace_enabled() -> bool:
    return os.getenv("HUB_WS_TRACE", "0") == "1"


def _ws_client_str(websocket: WebSocket) -> str:
    try:
        client = getattr(websocket, "client", None)
        if client and getattr(client, "host", None) is not None:
            return f"{client.host}:{client.port}"
    except Exception:
        pass
    try:
        scope = getattr(websocket, "scope", None) or {}
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and len(client) >= 2:
            return f"{client[0]}:{client[1]}"
    except Exception:
        pass
    return "unknown"


class WorkspaceWebsocketServer(WebsocketServer):
    """
    WebsocketServer that binds each room to a webspace-backed SQLiteYStore.

    We use the websocket path as the webspace id (e.g. "default").
    """

    async def get_room(self, name: str) -> YRoom:  # type: ignore[override]
        webspace_id = name or "default"
        room_open_started = time.perf_counter()
        created_room = False
        seed_result: dict[str, Any] | None = None

        _cancel_idle_room_reset(webspace_id)

        def _space_mode(ws_id: str) -> str:
            try:
                row = get_workspace(ws_id)
                if not row:
                    return "workspace"
                return row.effective_source_mode
            except Exception:
                return "workspace"

        # Double-checked locking to prevent concurrent room creation.
        # Without this, multiple concurrent get_room() calls can both pass
        # the `if name not in self.rooms` check and create duplicate rooms,
        # causing the second room to overwrite the first and orphan clients.
        if name not in self.rooms:
            lock = _room_locks.setdefault(webspace_id, asyncio.Lock())
            async with lock:
                async def _await_bootstrap_step(label: str, awaitable: Any) -> Any:
                    timeout_s = max(float(_YWS_ROOM_BOOTSTRAP_STEP_TIMEOUT_S), 0.0)
                    if timeout_s <= 0.0:
                        return await awaitable
                    try:
                        return await asyncio.wait_for(awaitable, timeout=timeout_s)
                    except asyncio.TimeoutError:
                        _ylog.warning(
                            "yws room bootstrap step timeout webspace=%s step=%s timeout_s=%.3f",
                            webspace_id,
                            label,
                            timeout_s,
                        )
                        raise

                # Second check after acquiring lock - another coroutine may
                # have already created the room while we were waiting.
                if name not in self.rooms:
                    _ylog.info("creating YRoom for webspace=%s", webspace_id)
                    room: DiagnosticYRoom | None = None
                    ystore = None
                    try:
                        ensure_workspace(webspace_id)
                        ystore = get_ystore_for_webspace(webspace_id)
                        row = get_workspace(webspace_id)
                        space = _space_mode(webspace_id)
                        room = DiagnosticYRoom(ready=self.rooms_ready, ystore=ystore, log=self.log)
                        room._webspace_id = webspace_id
                        room._thread_id = threading.get_ident()
                        room._loop = asyncio.get_running_loop()
                        # Ensure periodic in-memory snapshotting for this webspace.
                        try:
                            sched = get_scheduler()
                            await _await_bootstrap_step(
                                "schedule_backup",
                                sched.ensure_every(
                                    name=f"ystores.backup.{webspace_id}",
                                    interval=6000.0,
                                    topic="sys.ystore.backup",
                                    payload={"webspace_id": webspace_id},
                                ),
                            )
                        except Exception:
                            _ylog.warning("failed to register YStore backup job for webspace=%s", webspace_id, exc_info=True)
                        created_room = True
                        seed_result = await _await_bootstrap_step(
                            "seed_from_scenario",
                            ensure_webspace_seeded_from_scenario(
                                ystore,
                                webspace_id=webspace_id,
                                default_scenario_id=(row.effective_home_scenario if row and row.home_scenario else "web_desktop"),
                                space=space,
                                ydoc=room.ydoc,
                            ),
                        )
                        await _await_bootstrap_step(
                            "effective_materialized",
                            _ensure_room_effective_materialized(
                                webspace_id,
                                ystore,
                                room,
                                seed_result=seed_result,
                            ),
                        )
                        await _await_bootstrap_step(
                            "finalize_rebuild_status",
                            _finalize_room_bootstrap_rebuild_status(
                                webspace_id,
                                seed_result=seed_result,
                            ),
                        )
                        self.rooms[name] = room
                        _mark_room_created(webspace_id, room)
                    except BaseException:
                        self.rooms.pop(name, None)
                        _room_locks.pop(webspace_id, None)
                        if ystore is not None:
                            try:
                                await asyncio.wait_for(
                                    evict_ystore_for_webspace(
                                        webspace_id,
                                        store=ystore,
                                        persist_snapshot=False,
                                        compact_runtime=False,
                                        backup_kind="room_bootstrap_failed",
                                    ),
                                    timeout=max(float(_YWS_ROOM_STALE_RECOVERY_TIMEOUT_S), 0.25),
                                )
                            except Exception:
                                _ylog.warning("failed to evict YStore after room bootstrap failure webspace=%s", webspace_id, exc_info=True)
                        raise
        room = self.rooms[name]
        if not _room_effective_top_level_ready(getattr(room, "ydoc", None)):
            repair_update = await _repair_room_effective_branches(
                webspace_id,
                getattr(room, "ystore", None),
                room,
                reason="room_open_missing_effective_branches",
            )
            if repair_update:
                try:
                    room._diag_effective_repair_total += 1
                    room._diag_effective_repair_bytes += len(repair_update)
                    room._diag_effective_branch_snapshot = _room_effective_branch_snapshot(room.ydoc)
                except Exception:
                    pass
                self.log.warning(
                    "repaired missing YRoom effective branches before open webspace=%s repair_bytes=%s",
                    webspace_id,
                    len(repair_update),
                )
        room._webspace_id = webspace_id
        room._thread_id = getattr(room, "_thread_id", threading.get_ident())
        room._loop = getattr(room, "_loop", asyncio.get_running_loop())
        try:
            attach_room_observers(webspace_id, room.ydoc)
        except Exception:
            _ylog.warning("attach_room_observers failed for webspace=%s", webspace_id, exc_info=True)
        try:
            await self.start_room(room)
        except RuntimeError as exc:
            if "YRoom already running" not in str(exc):
                raise
            _ylog.warning(
                "YRoom start skipped because room is already running webspace=%s",
                webspace_id,
            )
        _mark_room_open(
            webspace_id,
            room,
            created=created_room,
            open_total_ms=(time.perf_counter() - room_open_started) * 1000.0,
            seed_result=seed_result,
        )
        if _ylog.isEnabledFor(logging.DEBUG):
            try:
                ui_map = room.ydoc.get_map("ui")
                data_map = room.ydoc.get_map("data")
                room._diag_effective_branch_snapshot = {
                    "ready": _room_effective_top_level_ready(room.ydoc),
                    "mode": "top_level_debug",
                }
                _ylog.debug(
                    "YRoom ready webspace=%s ui keys=%s data keys=%s",
                    webspace_id,
                    list(ui_map.keys()),
                    list(data_map.keys()),
                )
            except Exception:
                _ylog.warning("failed to inspect YDoc for webspace=%s", webspace_id, exc_info=True)
        return room


y_server = WorkspaceWebsocketServer(auto_clean_rooms=False)
_y_server_started = False
_y_server_task: asyncio.Task[None] | None = None
_room_locks: dict[str, asyncio.Lock] = {}


def _task_exception_summary(task: asyncio.Task[Any] | None) -> str | None:
    if task is None or not task.done() or task.cancelled():
        return None
    try:
        exc = task.exception()
    except BaseException as exc:  # pragma: no cover - defensive diagnostics
        return f"{type(exc).__name__}: {exc}"
    if exc is None:
        return None
    return f"{type(exc).__name__}: {exc}"


def _on_y_server_task_done(task: asyncio.Task[None]) -> None:
    summary = _task_exception_summary(task)
    if summary:
        _ylog.error("Yjs websocket server background task stopped unexpectedly: %s", summary)


def _recreate_y_server_after_failure(reason: str) -> None:
    global y_server, _y_server_started, _y_server_task
    old_server = y_server
    try:
        for room in list(getattr(old_server, "rooms", {}).values()):
            try:
                stop_room = getattr(room, "stop", None)
                if callable(stop_room):
                    stop_room()
            except Exception:
                pass
    except Exception:
        pass
    try:
        stop_server = getattr(old_server, "stop", None)
        if callable(stop_server):
            stop_server()
    except Exception:
        pass
    y_server = WorkspaceWebsocketServer(auto_clean_rooms=False)
    _room_locks.clear()
    _y_server_started = False
    _y_server_task = None
    _ylog.warning("Yjs websocket server runtime recreated after failure reason=%s", reason)


def _room_effective_branches_ready(ydoc: Any) -> bool:
    try:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        application = ui_map.get("application")
        if not isinstance(application, dict) or not application:
            return False
        desktop = application.get("desktop")
        modals = application.get("modals")
        if not isinstance(desktop, dict) or not desktop:
            return False
        if not isinstance(modals, dict) or "apps_catalog" not in modals or "widgets_catalog" not in modals:
            return False
        catalog = data_map.get("catalog")
        if not isinstance(catalog, dict):
            return False
        if not isinstance(catalog.get("apps"), list) or not isinstance(catalog.get("widgets"), list):
            return False
        if not isinstance(data_map.get("installed"), dict):
            return False
        if not isinstance(data_map.get("desktop"), dict):
            return False
        return True
    except Exception:
        return False


def _ymap_contains_key(y_map: Any, key: str) -> bool:
    try:
        return str(key or "") in y_map
    except Exception:
        try:
            return y_map.get(key) is not None
        except Exception:
            return False


def _room_effective_top_level_ready(ydoc: Any) -> bool:
    """
    Cheap hot-path invariant check for the shared desktop document.

    The full effective snapshot intentionally materializes several large Yjs
    branches and is too expensive to run for every update. This top-level check
    catches the destructive class that removes required roots/branches while
    keeping ordinary Yjs fanout cheap.
    """
    if not _YROOM_EFFECTIVE_GUARD_TOP_LEVEL_CHECKS:
        return True
    try:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        ydoc.get_map("registry")
        return (
            _ymap_contains_key(ui_map, "application")
            and _ymap_contains_key(data_map, "catalog")
            and _ymap_contains_key(data_map, "installed")
            and _ymap_contains_key(data_map, "desktop")
        )
    except Exception:
        return False


def _branch_collection_count(value: Any) -> int:
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 0


def _branch_json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "<max_depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _branch_json_safe(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))[:80]
        }
    if isinstance(value, (list, tuple)):
        return [_branch_json_safe(item, depth=depth + 1) for item in list(value)[:80]]
    return repr(value)[:200]


def _branch_hash(value: Any) -> str | None:
    if value is None:
        return None
    try:
        payload = json.dumps(
            _branch_json_safe(value),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:
        payload = repr(value)
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]


def _room_effective_branch_snapshot(ydoc: Any) -> dict[str, Any]:
    if ydoc is None:
        return {"ready": False, "error": "missing_ydoc"}
    if not _YROOM_EFFECTIVE_GUARD_SNAPSHOT_DETAILS:
        return {
            "ready": _room_effective_top_level_ready(ydoc),
            "mode": "top_level_snapshot",
            "details": "disabled",
        }
    try:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")
        ui_keys = [str(key) for key in list(ui_map.keys())[:40]]
        data_keys = [str(key) for key in list(data_map.keys())[:80]]
        registry_keys = [str(key) for key in list(registry_map.keys())[:40]]
        application = ui_map.get("application")
        application_desktop = application.get("desktop") if isinstance(application, dict) else None
        modals = application.get("modals") if isinstance(application, dict) else None
        catalog = data_map.get("catalog")
        installed = data_map.get("installed")
        desktop = data_map.get("desktop")
        snapshot = {
            "ready": _room_effective_branches_ready(ydoc),
            "ui_keys": ui_keys,
            "data_keys": data_keys,
            "registry_keys": registry_keys,
            "has_application": isinstance(application, dict) and bool(application),
            "has_application_desktop": isinstance(application_desktop, dict) and bool(application_desktop),
            "modal_count": _branch_collection_count(modals),
            "has_apps_catalog_modal": isinstance(modals, dict) and "apps_catalog" in modals,
            "has_widgets_catalog_modal": isinstance(modals, dict) and "widgets_catalog" in modals,
            "catalog_app_count": _branch_collection_count(catalog.get("apps") if isinstance(catalog, dict) else None),
            "catalog_widget_count": _branch_collection_count(catalog.get("widgets") if isinstance(catalog, dict) else None),
            "installed_key_count": _branch_collection_count(installed),
            "installed_app_count": _branch_collection_count(installed.get("apps") if isinstance(installed, dict) else None),
            "installed_widget_count": _branch_collection_count(installed.get("widgets") if isinstance(installed, dict) else None),
            "desktop_key_count": _branch_collection_count(desktop),
            "desktop_widget_count": _branch_collection_count(desktop.get("widgets") if isinstance(desktop, dict) else None),
        }
        if _YROOM_EFFECTIVE_GUARD_SNAPSHOT_HASHES:
            snapshot.update(
                {
                    "application_hash": _branch_hash(application),
                    "catalog_hash": _branch_hash(catalog),
                    "installed_hash": _branch_hash(installed),
                    "desktop_hash": _branch_hash(desktop),
                }
            )
        return snapshot
    except Exception as exc:
        return {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def refresh_live_webspace_effective_branches(
    webspace_id: str,
    *,
    reason: str = "live_room_refresh",
) -> dict[str, Any]:
    """Refresh effective scenario branches without tearing down live transports.

    A scenario materialization refresh should update the active YDoc contents,
    not close the browser's YWS/WebRTC datachannel path. Hard room resets remain
    available for explicit recovery paths.
    """

    key = str(webspace_id or "").strip() or "default"
    room_created = False
    room = y_server.rooms.get(key)
    if room is None:
        try:
            room = await y_server.get_room(key)
            room_created = True
        except Exception as exc:
            _ylog.warning(
                "failed to refresh live Yjs room effective branches webspace=%s reason=%s",
                key,
                reason,
                exc_info=True,
            )
            return {
                "ok": False,
                "webspace_id": key,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
                "room_present": False,
                "room_created": False,
                "room_dropped": False,
                "closed_connections": 0,
                "closed_webrtc_peers": 0,
                "reset_route_runtime": False,
            }

    update = await _repair_room_effective_branches(
        key,
        getattr(room, "ystore", None),
        room,
        reason=reason,
    )
    update_size = len(update or b"")
    _ylog.info(
        "refreshed live Yjs room effective branches webspace=%s reason=%s room_created=%s update_bytes=%s",
        key,
        reason,
        room_created,
        update_size,
    )
    return {
        "ok": True,
        "webspace_id": key,
        "reason": reason,
        "room_present": True,
        "room_created": room_created,
        "room_dropped": False,
        "room_repaired": update_size > 0,
        "repair_bytes": update_size,
        "closed_connections": 0,
        "closed_webrtc_peers": 0,
        "reset_route_runtime": False,
    }


async def _repair_room_effective_branches(
    webspace_id: str,
    ystore: Any,
    room: Any,
    *,
    reason: str,
) -> bytes:
    ydoc = getattr(room, "ydoc", None)
    if ydoc is None:
        return b""
    try:
        import y_py as Y  # pylint: disable=import-outside-toplevel
        from adaos.services.scenario.webspace_runtime import WebspaceScenarioRuntime  # pylint: disable=import-outside-toplevel

        before = Y.encode_state_vector(ydoc)
        runtime = WebspaceScenarioRuntime()
        with ystore_write_metadata_sync(
            root_names=["ui", "data", "registry"],
            source=f"yjs.gateway_ws.{reason}",
            owner="core:yjs_gateway",
            channel="core.yjs.gateway.repair",
            governed=True,
        ):
            authoritative_scenario = _authoritative_current_scenario(webspace_id)
            if authoritative_scenario:
                ui_map = ydoc.get_map("ui")
                with ydoc.begin_transaction() as txn:
                    ui_map.set(txn, "current_scenario", authoritative_scenario)
            runtime._rebuild_in_doc(ydoc, webspace_id)  # noqa: SLF001 - invariant repair needs in-doc materialization
        if not _room_effective_branches_ready(ydoc):
            _ylog.warning(
                "YRoom effective branch repair did not restore required branches webspace=%s reason=%s snapshot=%s",
                webspace_id,
                reason,
                json.dumps(_room_effective_branch_snapshot(ydoc), ensure_ascii=True, sort_keys=True)[:1000],
            )
            return b""
        update = Y.encode_state_as_update(ydoc, before)  # type: ignore[arg-type]
        if update and ystore is not None:
            async with ystore_write_metadata(
                root_names=["ui", "data", "registry"],
                source=f"yjs.gateway_ws.{reason}",
                owner="core:yjs_gateway",
                channel="core.yjs.gateway.repair",
                governed=True,
            ):
                await ystore.write_update(update, update_kind="diff", notify=False)
        _ylog.warning(
            "YRoom effective branches repaired webspace=%s reason=%s bytes=%s persisted=%s",
            webspace_id,
            reason,
            len(update or b""),
            bool(update and ystore is not None),
        )
        return bytes(update or b"")
    except Exception as exc:
        _ylog.warning(
            "YRoom effective branch repair failed webspace=%s reason=%s: %s",
            webspace_id,
            reason,
            exc,
            exc_info=True,
        )
        return b""


async def _ensure_room_effective_materialized(
    webspace_id: str,
    ystore: Any,
    room: Any,
    *,
    seed_result: dict[str, Any] | None = None,
) -> bool:
    """
    Ensure cold YRoom opens with effective desktop branches already present.

    ``ensure_webspace_seeded_from_scenario`` may legitimately reuse projected
    scenario branches and emit an async semantic rebuild nudge. That is too late
    for the first browser sync: the initial Yjs state can reach the client
    before ``ui.application``/``data.catalog`` are materialized. For room
    bootstrap we run the semantic materializer in the room YDoc synchronously
    and persist just the resulting diff before exposing the room.
    """
    ydoc = getattr(room, "ydoc", None)
    authoritative_scenario = _authoritative_current_scenario(webspace_id)
    if ydoc is None:
        return False
    if _room_effective_branches_ready(ydoc) and not authoritative_scenario:
        return False

    try:
        import y_py as Y  # pylint: disable=import-outside-toplevel
        from adaos.services.scenario.webspace_runtime import WebspaceScenarioRuntime  # pylint: disable=import-outside-toplevel

        before = Y.encode_state_vector(ydoc)
        runtime = WebspaceScenarioRuntime()
        with ystore_write_metadata_sync(
            root_names=["ui", "data", "registry"],
            source="yjs.gateway_ws.room_bootstrap",
            owner="core:yjs_gateway",
            channel="core.yjs.gateway.bootstrap",
            governed=True,
        ):
            if authoritative_scenario:
                ui_map = ydoc.get_map("ui")
                with ydoc.begin_transaction() as txn:
                    ui_map.set(txn, "current_scenario", authoritative_scenario)
            runtime._rebuild_in_doc(ydoc, webspace_id)  # noqa: SLF001 - room bootstrap needs in-doc materialization

        if not _room_effective_branches_ready(ydoc):
            if seed_result is not None:
                seed_result["room_effective_materialized"] = False
                seed_result["room_effective_materialize_error"] = "effective_branches_still_missing"
            _ylog.warning(
                "YRoom effective materialization left required branches missing webspace=%s",
                webspace_id,
            )
            return False

        update = Y.encode_state_as_update(ydoc, before)  # type: ignore[arg-type]
        persisted = False
        if update:
            async with ystore_write_metadata(
                root_names=["ui", "data", "registry"],
                source="yjs.gateway_ws.room_bootstrap",
                owner="core:yjs_gateway",
                channel="core.yjs.gateway.bootstrap",
                governed=True,
            ):
                persisted = bool(await ystore.write_update(update, update_kind="diff", notify=False))
        if seed_result is not None:
            seed_result["room_effective_materialized"] = True
            seed_result["room_effective_materialized_persisted"] = bool(persisted)
            seed_result["room_effective_materialized_bytes"] = len(update or b"")
        _ylog.info(
            "YRoom effective branches materialized before open webspace=%s persisted=%s bytes=%d",
            webspace_id,
            bool(persisted),
            len(update or b""),
        )
        return True
    except Exception as exc:
        if seed_result is not None:
            seed_result["room_effective_materialized"] = False
            seed_result["room_effective_materialize_error"] = f"{type(exc).__name__}: {exc}"
        _ylog.warning(
            "YRoom effective materialization failed before open webspace=%s: %s",
            webspace_id,
            exc,
            exc_info=True,
        )
        return False


async def _finalize_room_bootstrap_rebuild_status(
    webspace_id: str,
    *,
    seed_result: dict[str, Any] | None = None,
) -> None:
    """
    Publish a semantic rebuild status for rooms restored from a disk snapshot.

    A cold YRoom can already contain all effective branches because the durable
    snapshot is healthy. In that path no semantic rebuild event is emitted, so
    in-memory diagnostics may still report ``materialization_not_ready`` after a
    process restart. Run the public rebuild primitive once before the room is
    exposed so readiness reflects the actual YDoc state.
    """
    try:
        from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources  # pylint: disable=import-outside-toplevel

        result = await rebuild_webspace_from_sources(
            webspace_id,
            action="room_bootstrap",
            source_of_truth="room_bootstrap",
        )
        if seed_result is not None:
            seed_result["room_bootstrap_rebuild_status"] = (
                "ready" if bool(result.get("ok")) and bool(result.get("accepted")) else "not_accepted"
            )
            seed_result["room_bootstrap_rebuild_error"] = str(result.get("error") or "") or None
    except Exception as exc:
        if seed_result is not None:
            seed_result["room_bootstrap_rebuild_status"] = "failed"
            seed_result["room_bootstrap_rebuild_error"] = f"{type(exc).__name__}: {exc}"
        _ylog.warning(
            "YRoom bootstrap rebuild status finalization failed webspace=%s: %s",
            webspace_id,
            exc,
            exc_info=True,
        )


async def start_y_server() -> None:
    """
    Ensure the shared Y websocket server background task is running.
    """
    global _y_server_started, _y_server_task
    if _y_server_started:
        task = _y_server_task
        if task is not None and task.done():
            _recreate_y_server_after_failure(_task_exception_summary(task) or "task_done")
        else:
            return
    _y_server_started = True

    async def _runner() -> None:
        await y_server.start()

    _y_server_task = asyncio.create_task(_runner(), name="adaos-yjs-websocket-server")
    _y_server_task.add_done_callback(_on_y_server_task_done)
    await y_server.started.wait()


async def stop_y_server() -> None:
    """
    Stop the shared Y websocket server background task.

    Without an explicit stop, the anyio task group inside ypy-websocket can
    keep the process alive after FastAPI/uvicorn shutdown.
    """
    global _y_server_started, _y_server_task
    if not _y_server_started:
        return
    for webspace_id in list(_IDLE_ROOM_RESET_TASKS.keys()):
        _cancel_idle_room_reset(webspace_id)
    for webspace_id in list(getattr(y_server, "rooms", {}).keys()):
        try:
            await reset_live_webspace_room(str(webspace_id), close_reason="y_server_shutdown")
        except Exception:
            _ylog.debug("failed to reset room during y_server shutdown webspace=%s", webspace_id, exc_info=True)
    try:
        y_server.stop()
    except Exception:
        pass
    task = _y_server_task
    _y_server_task = None
    _y_server_started = False
    if task is None:
        return
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # shutdown path: ignore
        pass


async def ensure_webspace_ready(webspace_id: str, scenario_id: str | None = None) -> None:
    ensure_workspace(webspace_id)
    ystore = get_ystore_for_webspace(webspace_id)
    row = get_workspace(webspace_id)
    space = row.effective_source_mode if row else "workspace"
    base_scenario = str(scenario_id or "").strip()
    if not base_scenario and row and row.home_scenario:
        base_scenario = row.effective_home_scenario
    if not base_scenario:
        base_scenario = "web_desktop"

    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=base_scenario,
            space=space,
        )
    finally:
        try:
            await _stop_ystore_maybe_async(ystore)
        except Exception:
            pass


class FastAPIWebsocketAdapter:
    """
    Adapt FastAPI's WebSocket to the minimal protocol expected by ypy-websocket.
    """

    def __init__(self, ws: WebSocket, path: str):
        self._ws = ws
        self._path = path
        self._first_message_timeout_s = _YWS_FIRST_MESSAGE_TIMEOUT_S
        self._first_message_received = False

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration()

    async def send(self, message: bytes) -> None:
        try:
            await self._ws.send_bytes(message)
        except (WebSocketDisconnect, RuntimeError):
            # Client is already gone; ypy-websocket treats send failures inside
            # its room task group as fatal unless the adapter absorbs them.
            return
        except Exception:
            _ylog.debug("yws send ignored after client disconnect path=%s", self._path, exc_info=True)
            return

    async def recv(self) -> bytes:
        while True:
            try:
                if not self._first_message_received and self._first_message_timeout_s > 0:
                    # Healthy Yjs clients should send their first sync frame immediately.
                    # If the proxy path wedges before that point we would otherwise leak a
                    # runtime session until the process restarts.
                    msg = await asyncio.wait_for(self._ws.receive(), timeout=self._first_message_timeout_s)
                else:
                    msg = await self._ws.receive()
            except asyncio.TimeoutError as exc:
                raise RuntimeError("websocket first message timeout") from exc
            msg_type = msg.get("type")
            if msg_type == "websocket.receive":
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    if data:
                        self._first_message_received = True
                        return data
                    continue
                if msg.get("text") is not None:
                    data = msg["text"].encode("utf-8")
                    if data:
                        self._first_message_received = True
                        return data
                    continue
                continue
            if msg_type == "websocket.disconnect":
                raise RuntimeError("websocket disconnected")
            raise RuntimeError(f"unexpected websocket event: {msg_type}")


async def _update_device_presence(webspace_id: str, device_id: str) -> None:
    """
    Project basic device presence into the Yjs doc under devices/<device_id>.
    """
    room = await y_server.get_room(webspace_id)
    ydoc = room.ydoc
    now_ms = int(time.time() * 1000)

    with ystore_write_metadata_sync(
        root_names=["devices"],
        source="yjs.gateway_ws",
        owner="core:yjs_gateway",
        channel="core.yjs.gateway.sync",
    ):
        with ydoc.begin_transaction() as txn:
            devices = ydoc.get_map("devices")
            current = devices.get(device_id)
            node = dict(current or {}) if isinstance(current, dict) else {}

            meta = dict(node.get("meta") or {})
            if "created_at" not in meta:
                meta["created_at"] = now_ms
            meta["kind"] = "browser"

            presence = dict(node.get("presence") or {})
            presence["online"] = True
            presence.setdefault("since", now_ms)
            presence["lastSeen"] = now_ms

            node["meta"] = meta
            node["presence"] = presence

            devices.set(txn, device_id, node)


async def _recover_stale_yws_room_bootstrap(webspace: str, dev_id: str, *, waited_s: float, reason: str) -> None:
    """
    Break a stale room bootstrap so reconnect loops do not keep piling onto the
    same locked YWS room creation path.

    This is deliberately scoped to runtime objects only. The persisted snapshot
    remains the source of truth; the next browser connection can create a fresh
    room and replay from disk.
    """
    _ylog.warning(
        "recovering stale yws room bootstrap webspace=%s dev=%s waited_s=%.3f reason=%s",
        webspace,
        dev_id,
        waited_s,
        reason,
    )
    _room_locks.pop(webspace, None)
    room = getattr(y_server, "rooms", {}).pop(webspace, None)
    try:
        _mark_room_reset(
            webspace,
            close_reason=reason,
            room=room,
            room_dropped=room is not None,
            closed_connections=0,
            closed_webrtc_peers=0,
        )
    except Exception:
        _ylog.debug("failed to mark stale yws room bootstrap reset webspace=%s", webspace, exc_info=True)
    try:
        await asyncio.wait_for(
            evict_ystore_for_webspace(
                webspace,
                store=getattr(room, "ystore", None) if room is not None else None,
                persist_snapshot=False,
                compact_runtime=False,
                backup_kind=reason,
            ),
            timeout=max(float(_YWS_ROOM_STALE_RECOVERY_TIMEOUT_S), 0.25),
        )
    except Exception:
        _ylog.warning("failed to evict YStore during stale yws room bootstrap recovery webspace=%s", webspace, exc_info=True)


async def _acquire_yws_room(webspace_id: str, dev_id: str) -> YRoom:
    """
    Resolve YJS room with bounded waiting and cache fallback.

    We keep waiting long enough for legitimate warm bootstrap but avoid hard
    12-second reconnect loops from every connection when startup has stalled.
    """
    webspace = _shorten_webspace_id(webspace_id)
    timeout_s = max(float(_YWS_ROOM_READY_TIMEOUT_S), 0.0)
    max_wait_s = max(float(_YWS_ROOM_READY_MAX_S), 0.0)
    poll_s = max(float(_YWS_ROOM_READY_POLL_S), 0.25)

    wait_task: asyncio.Task[YRoom] = asyncio.create_task(y_server.get_room(webspace))
    started = time.perf_counter()
    attempts = 0

    if max_wait_s <= 0.0:
        max_wait_s = timeout_s if timeout_s > 0.0 else 0.0

    try:
        while True:
            attempts += 1
            if timeout_s <= 0.0:
                return await wait_task

            remaining_for_wait = max(max_wait_s - (time.perf_counter() - started), 0.0)
            if remaining_for_wait <= 0.0:
                raise asyncio.TimeoutError("room wait timeout exceeded")

            try:
                return await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=min(timeout_s, remaining_for_wait),
                )
            except asyncio.TimeoutError:
                pass

            elapsed = time.perf_counter() - started
            room = getattr(y_server, "rooms", {}).get(webspace)
            if room is not None:
                _ylog.info(
                    "yws room cache hit after timeout webspace=%s dev=%s attempt=%s waited_s=%.3f",
                    webspace,
                    dev_id,
                    attempts,
                    elapsed,
                )
                if not wait_task.done():
                    _ylog.debug("yws room cache hit but bootstrap task still running webspace=%s dev=%s", webspace, dev_id)
                return room

            if remaining_for_wait <= 0.0:
                raise asyncio.TimeoutError("room wait timeout exceeded")

            _ylog.warning(
                "yws room ready timeout webspace=%s dev=%s timeout_s=%.3f waited_s=%.3f",
                webspace,
                dev_id,
                timeout_s,
                elapsed,
            )
            await asyncio.sleep(min(poll_s, remaining_for_wait))
    except asyncio.TimeoutError:
        room = getattr(y_server, "rooms", {}).get(webspace)
        if room is not None:
            _ylog.info(
                "yws room cache hit at final timeout webspace=%s dev=%s waited_s=%.3f",
                webspace,
                dev_id,
                time.perf_counter() - started,
            )
            return room
        if wait_task.done():
            try:
                room = wait_task.result()
            except Exception:
                raise
            return room
        wait_task.cancel()
        try:
            await wait_task
        except asyncio.CancelledError:
            pass
        except Exception:
            _ylog.warning(
                "yws room bootstrap task failed after timeout webspace=%s dev=%s",
                webspace,
                dev_id,
                exc_info=True,
            )
        room = getattr(y_server, "rooms", {}).get(webspace)
        if room is not None:
            _ylog.info(
                "yws room cache hit after cancelling timeout task webspace=%s dev=%s waited_s=%.3f",
                webspace,
                dev_id,
                time.perf_counter() - started,
            )
            return room
        lock = _room_locks.get(webspace)
        if lock is not None and not lock.locked():
            _room_locks.pop(webspace, None)
        elif lock is not None:
            waited_s = time.perf_counter() - started
            _ylog.warning(
                "yws room lock remains locked after bootstrap timeout webspace=%s dev=%s waited_s=%.3f",
                webspace,
                dev_id,
                waited_s,
            )
            await _recover_stale_yws_room_bootstrap(
                webspace,
                dev_id,
                waited_s=waited_s,
                reason="room_bootstrap_timeout",
            )
        raise


async def _yws_impl(websocket: WebSocket, room: str | None) -> None:
    """
    Internal Yjs sync handler used by both /yws and /yws/<room> routes.

    Dev policy:
      - if a room segment is present in the path, it is treated as webspace_id;
      - otherwise, fallback to ?ws=<webspace_id> query param;
      - default is "default".
    """
    params: Dict[str, str] = dict(websocket.query_params)
    webspace_id = _coerce_gateway_webspace_id(room or params.get("ws"))
    dev_id = params.get("dev") or "unknown"
    browser_metadata = _browser_session_metadata(params)

    if _ws_trace_enabled():
        try:
            token_present = "token" in params
            _ylog.info(
                "yws trace open client=%s webspace=%s dev=%s token=%s",
                _ws_client_str(websocket),
                webspace_id,
                dev_id,
                token_present,
            )
        except Exception:
            pass
    try:
        from adaos.services.access_links import authorize_link, touch_browser_session

        allowed, reason = authorize_link("browser", dev_id)
        if not allowed:
            reason_token = str(reason or "denied").strip().lower() or "denied"
            try:
                touch_browser_session(
                    dev_id,
                    webspace_id=webspace_id,
                    connection_state=reason_token,
                    online=False,
                    **browser_metadata,
                )
            except Exception:
                pass
            # Accept before closing so browsers receive a real close event with
            # a policy reason. Closing before accept is exposed as an opaque 403
            # in Chrome/WebView, which lets y-websocket keep reconnecting.
            if await _accept_websocket(websocket, channel="yws.auth_denied"):
                try:
                    await websocket.close(code=1008, reason=f"device_{reason_token}")
                except Exception:
                    pass
            return
    except Exception:
        _ylog.debug("browser access policy check failed webspace=%s dev=%s", webspace_id, dev_id, exc_info=True)
    if not await _accept_websocket(websocket, channel="yws"):
        return
    replaced_existing = await _close_existing_yws_client_connections(webspace_id, dev_id)
    if replaced_existing:
        deadline = time.monotonic() + 1.0
        while (
            _active_yws_connection_total_for_client(webspace_id, dev_id) >= _YWS_MAX_ACTIVE_PER_CLIENT
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
    guard_reason, guard_diag = _yws_guard_reject_reason(webspace_id, dev_id)
    if guard_reason:
        state_token = f"yws_guard_{guard_reason}"
        if _yws_guard_should_notify(webspace_id=webspace_id, dev_id=dev_id, reason=guard_reason):
            try:
                from adaos.services.access_links import touch_browser_session

                touch_browser_session(
                    dev_id,
                    webspace_id=webspace_id,
                    connection_state=state_token,
                    online=False,
                    **browser_metadata,
                )
            except Exception:
                _ylog.debug("browser access registry guard update failed webspace=%s dev=%s", webspace_id, dev_id, exc_info=True)
            _publish_runtime_event(
                "browser.session.changed",
                {
                    "device_id": dev_id,
                    "webspace_id": webspace_id,
                    "connection_state": state_token,
                    "yjs_channel_state": "rejected",
                    "reason": guard_reason,
                    "active_yws": guard_diag.get("active_total"),
                    "recent_open_10s": guard_diag.get("recent_open_10s"),
                    "client_open_15s": guard_diag.get("client_open_15s"),
                    "source": "yws.gateway.guard",
                },
            )
        try:
            await websocket.close(code=1013, reason=state_token[:120])
        except Exception:
            pass
        return
    _ylog.info("yws connection open webspace=%s dev=%s", webspace_id, dev_id)
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=webspace_id)
    try:
        room_ref = await _acquire_yws_room(webspace_id, dev_id)
    except asyncio.TimeoutError:
        _ylog.warning(
            "yws room ready timeout webspace=%s dev=%s timeout_s=%.3f max_wait_s=%.3f",
            webspace_id,
            dev_id,
            _YWS_ROOM_READY_TIMEOUT_S,
            _YWS_ROOM_READY_MAX_S,
        )
        try:
            await websocket.close(code=1013, reason="room_ready_timeout")
        except Exception:
            pass
        return
    _record_yws_open(webspace_id, dev_id)
    _track_yws_connection(webspace_id, websocket, device_id=dev_id)
    _transport_mark_open("yws")
    try:
        from adaos.services.access_links import touch_browser_session

        touch_browser_session(
            dev_id,
            webspace_id=webspace_id,
            connection_state="connected",
            online=True,
            **browser_metadata,
        )
    except Exception:
        _ylog.debug("browser access registry open update failed webspace=%s dev=%s", webspace_id, dev_id, exc_info=True)
    _publish_runtime_event(
        "browser.session.changed",
        {
            "device_id": dev_id,
            "webspace_id": webspace_id,
            "connection_state": "connected",
            "yjs_channel_state": "open",
            "source": "yws.gateway",
        },
    )
    try:
        await room_ref.serve(adapter)
    except RuntimeError:
        return
    except Exception:
        _ylog.debug(
            "yws room serve ended with error webspace=%s dev=%s",
            webspace_id,
            dev_id,
            exc_info=True,
        )
        return
    finally:
        _untrack_yws_connection(webspace_id, websocket)
        _transport_mark_close("yws")
        mark_offline = _should_mark_yws_browser_session_offline(dev_id)
        if mark_offline:
            try:
                from adaos.services.access_links import touch_browser_session

                touch_browser_session(
                    dev_id,
                    webspace_id=webspace_id,
                    connection_state="closed",
                    online=False,
                    **browser_metadata,
                )
            except Exception:
                _ylog.debug("browser access registry close update failed webspace=%s dev=%s", webspace_id, dev_id, exc_info=True)
            _publish_runtime_event(
                "browser.session.changed",
                {
                    "device_id": dev_id,
                    "webspace_id": webspace_id,
                    "connection_state": "closed",
                    "yjs_channel_state": "closed",
                    "source": "yws.gateway",
                },
            )
        else:
            _ylog.debug(
                "yws connection closed but browser session remains active webspace=%s dev=%s active_sessions=%s",
                webspace_id,
                dev_id,
                _active_yws_connection_total_for_device(dev_id),
            )
        _ylog.info("yws connection closed webspace=%s dev=%s", webspace_id, dev_id)
        if _ws_trace_enabled():
            try:
                code = getattr(websocket, "close_code", None)
                _ylog.info(
                    "yws trace closed client=%s webspace=%s dev=%s code=%s",
                    _ws_client_str(websocket),
                    webspace_id,
                    dev_id,
                    code,
                )
            except Exception:
                pass


@router.websocket("/yws")
async def yws(websocket: WebSocket):
    """
    Binary Yjs sync endpoint backed by ypy-websocket.

    Frontend connects via y-websocket with:
      ws://host:port/yws/<webspace_id>?dev=<device_id>
    """
    await _yws_impl(websocket, room=None)


@router.websocket("/yws/{room:path}")
async def yws_room(websocket: WebSocket, room: str):
    """
    Route compatible with y-websocket default URL pattern:
      ws://host:port/yws/<webspace_id>?dev=<device_id>
    """
    await _yws_impl(websocket, room=room)


@router.get("/api/browser/session/authorize")
async def browser_session_authorize(
    dev: str | None = None,
    ws: str | None = None,
    browser_family: str | None = None,
    os_name: str | None = None,
    form_factor: str | None = None,
    user_agent: str | None = None,
):
    """
    Lightweight browser-device preflight for clients before opening /yws.

    WebSocket close reasons can be hidden by browsers/proxies when the server
    rejects before accept. This JSON endpoint gives the shell a stable,
    product-level state so revoked/expired endpoints can enter login instead
    of running a noisy reconnect loop.
    """
    dev_id = str(dev or "").strip() or "unknown"
    webspace_id = _coerce_gateway_webspace_id(ws)
    metadata = _browser_session_metadata(
        {
            "browser_family": browser_family or "",
            "os_name": os_name or "",
            "form_factor": form_factor or "",
            "user_agent": user_agent or "",
        }
    )
    try:
        from adaos.services.access_links import authorize_link, touch_browser_session

        allowed, reason = authorize_link("browser", dev_id)
        if not allowed:
            try:
                touch_browser_session(
                    dev_id,
                    webspace_id=webspace_id,
                    connection_state=reason or "denied",
                    online=False,
                    **metadata,
                )
            except Exception:
                pass
        return _browser_auth_response_payload(
            dev_id=dev_id,
            webspace_id=webspace_id,
            allowed=allowed,
            reason=reason,
        )
    except Exception:
        _ylog.debug(
            "browser session authorize policy check failed webspace=%s dev=%s",
            webspace_id,
            dev_id,
            exc_info=True,
        )
        # Match /yws behavior: policy storage failures must not lock users out.
        return _browser_auth_response_payload(
            dev_id=dev_id,
            webspace_id=webspace_id,
            allowed=True,
            reason=None,
        )


def _make_publish_bus(
    device_id_ref: Callable[[], str | None],
    webspace_id_ref: Callable[[], str],
) -> Callable[[str, Dict[str, Any] | None], None]:
    """Create a ``_publish_bus`` closure bound to mutable connection state."""

    def _publish_bus(topic: str, extra: Dict[str, Any] | None = None) -> None:
        data = dict(extra or {})
        effective_ws = str(data.get("webspace_id") or webspace_id_ref())
        if not data.get("webspace_id"):
            data["webspace_id"] = effective_ws
        meta = dict(data.get("_meta") or {})
        meta.setdefault("webspace_id", effective_ws)
        target_node_id = str(
            data.get("target_node_id")
            or data.get("node_target_id")
            or meta.get("target_node_id")
            or meta.get("node_target_id")
            or data.get("node_id")
            or ""
        ).strip()
        if target_node_id:
            data.setdefault("target_node_id", target_node_id)
            meta.setdefault("target_node_id", target_node_id)
        did = device_id_ref()
        if did:
            meta.setdefault("device_id", did)
        data["_meta"] = meta
        try:
            ctx = get_agent_ctx()
            ev = DomainEvent(type=topic, payload=data, source="events_ws", ts=time.time())
            ctx.bus.publish(ev)
        except Exception:
            _log.warning("failed to publish %s", topic, exc_info=True)

    return _publish_bus


async def process_events_command(
    kind: str,
    cmd_id: str,
    payload: dict[str, Any],
    device_id: str,
    webspace_id: str,
    send_response: Callable[[dict[str, Any]], Awaitable[None]],
    client_label: str | None = None,
) -> str | None:
    """
    Process a single events-channel command and send ack via *send_response*.

    Returns the **new** ``webspace_id`` when the command changed it (e.g.
    ``device.register``, ``desktop.webspace.use``), or ``None`` if unchanged.

    This function is shared between the ``/ws`` WebSocket endpoint and the
    WebRTC events DataChannel so that both transports execute the same logic.
    """

    _publish_bus = _make_publish_bus(lambda: device_id, lambda: webspace_id)

    async def _ack(ok: bool = True, *, data: dict[str, Any] | None = None, error: str | None = None) -> None:
        msg: dict[str, Any] = {"ch": "events", "t": "ack", "id": cmd_id, "ok": ok}
        if data is not None:
            msg["data"] = data
        if error is not None:
            msg["error"] = error
        await send_response(msg)

    if kind == "device.register":
        new_device = payload.get("device_id") or "dev-unknown"
        requested_webspace = payload.get("webspace_id") or payload.get("id")
        new_webspace = _coerce_gateway_webspace_id(requested_webspace)

        captured_device = new_device
        captured_ws = new_webspace

        async def _post_register() -> dict[str, Any]:
            try:
                guard_reason, guard_diag = _yws_guard_reject_reason(captured_ws, captured_device)
                if guard_reason:
                    _log.warning(
                        "device.register skipped Yjs post steps due yws guard webspace=%s device=%s reason=%s active=%s recent_open_10s=%s client_open_15s=%s",
                        captured_ws,
                        captured_device,
                        guard_reason,
                        guard_diag.get("active_total"),
                        guard_diag.get("recent_open_10s"),
                        guard_diag.get("client_open_15s"),
                    )
                    return {
                        "yjs_post_skipped": True,
                        "yjs_guard_reason": guard_reason,
                    }
                await start_y_server()
                await _update_device_presence(captured_ws, captured_device)
                # Sync webspace listing directly to the live room's YDoc.
                # This ensures the frontend sees data.webspaces immediately.
                try:
                    from adaos.services.scenario.webspace_runtime import _webspace_listing

                    room = y_server.rooms.get(captured_ws)
                    if room:
                        listing = _webspace_listing()
                        with ystore_write_metadata_sync(
                            root_names=["data"],
                            source="yjs.gateway_ws",
                            owner="core:yjs_gateway",
                            channel="core.yjs.gateway.sync",
                        ):
                            with room.ydoc.begin_transaction() as txn:
                                data_map = room.ydoc.get_map("data")
                                data_map.set(txn, "webspaces", {"items": listing})
                        _log.debug("wrote webspaces listing to room webspace=%s items=%d", captured_ws, len(listing))
                except Exception:
                    _log.debug("webspace listing sync failed", exc_info=True)
                _log.debug("device.register post steps ok webspace=%s device=%s", captured_ws, captured_device)
                return {"yjs_post_skipped": False}
            except Exception:
                _log.warning("device.register post steps failed webspace=%s device=%s", captured_ws, captured_device, exc_info=True)
                return {"yjs_post_failed": True}

        try:
            # Ensure room is created and seeded BEFORE sending ack.
            # This prevents race condition where frontend connects Yjs provider
            # before room is ready, causing empty webspaces on first connection.
            post_result = await _post_register()
            event_payload = {
                "device_id": captured_device,
                "webspace_id": captured_ws,
                "kind": "browser",
            }
            if post_result.get("yjs_post_skipped"):
                event_payload["yjs_post_skipped"] = True
                event_payload["yjs_guard_reason"] = str(post_result.get("yjs_guard_reason") or "")
            _publish_bus(
                "device.registered",
                event_payload,
            )
            ack_data = {"webspace_id": new_webspace}
            if post_result.get("yjs_post_skipped"):
                ack_data["yjs_post_skipped"] = True
                ack_data["yjs_guard_reason"] = str(post_result.get("yjs_guard_reason") or "")
            await _ack(data=ack_data)
        except Exception:
            # Best-effort: still send ack even if post-register fails
            await _ack(data={"webspace_id": new_webspace})
        return new_webspace

    if kind == "desktop.toggleInstall":
        _publish_bus("desktop.toggleInstall", {"type": payload.get("type"), "id": payload.get("id"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "desktop.webspace.create":
        _publish_bus("desktop.webspace.create", {"id": payload.get("id"), "title": payload.get("title"), "scenario_id": payload.get("scenario_id"), "dev": payload.get("dev")})
        await _ack()
        return None

    if kind == "desktop.webspace.rename":
        _publish_bus("desktop.webspace.rename", {"id": payload.get("id"), "title": payload.get("title")})
        await _ack()
        return None

    if kind == "desktop.webspace.update":
        _publish_bus(
            "desktop.webspace.update",
            {
                "id": payload.get("id") or payload.get("webspace_id"),
                "title": payload.get("title"),
                "home_scenario": payload.get("home_scenario") or payload.get("scenario_id"),
            },
        )
        await _ack()
        return None

    if kind == "desktop.webspace.delete":
        _publish_bus("desktop.webspace.delete", {"id": payload.get("id")})
        await _ack()
        return None

    if kind == "desktop.webspace.refresh":
        _publish_bus("desktop.webspace.refresh", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.go_home":
        _publish_bus("desktop.webspace.go_home", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.set_home":
        target = (payload or {}).get("scenario_id")
        if not target:
            await _ack(False, error="scenario_id required")
        else:
            _publish_bus("desktop.webspace.set_home", payload)
            await _ack()
        return None

    if kind == "desktop.webspace.ensure_dev":
        target = str((payload or {}).get("scenario_id") or "").strip()
        if not target:
            await _ack(False, error="scenario_id required")
            return None
        try:
            from adaos.services.scenario.webspace_runtime import ensure_dev_webspace_for_scenario

            result = await ensure_dev_webspace_for_scenario(
                target,
                requested_id=str((payload or {}).get("id") or (payload or {}).get("requested_id") or "").strip() or None,
                title=str((payload or {}).get("title") or "").strip() or None,
            )
            ensured_webspace_id = str(result.get("webspace_id") or "").strip() or None
            if ensured_webspace_id:
                await ensure_webspace_ready(
                    ensured_webspace_id,
                    scenario_id=str(result.get("home_scenario") or target).strip() or target,
                )
            await _ack(data=result)
        except ValueError as exc:
            await _ack(False, error=str(exc) or "scenario_id required")
        except Exception:
            _log.warning("desktop.webspace.ensure_dev failed scenario=%s", target, exc_info=True)
            await _ack(False, error="dev_webspace_unavailable")
        return None

    if kind == "desktop.webspace.use":
        target = payload.get("id") or payload.get("webspace_id")
        if not target:
            await _ack(False, error="webspace_id required")
            return None
        new_webspace = str(target)
        try:
            await ensure_webspace_ready(new_webspace, scenario_id=payload.get("scenario_id"))
            await _update_device_presence(new_webspace, device_id or "dev-unknown")
            _publish_bus("desktop.webspace.refresh", {"webspace_id": new_webspace})
            await _ack(data={"webspace_id": new_webspace})
            return new_webspace
        except Exception:
            await _ack(False, error="webspace_unavailable")
            return None

    if kind == "skill.event.publish":
        event_type = str((payload or {}).get("event_type") or (payload or {}).get("type") or "").strip()
        if not event_type:
            await _ack(False, error="event_type required")
            return None
        raw_event_payload = (payload or {}).get("payload")
        if isinstance(raw_event_payload, dict):
            event_payload = dict(raw_event_payload)
        elif raw_event_payload is None:
            event_payload = {}
        else:
            event_payload = {"value": raw_event_payload}
        for key in ("webspace_id", "workspace_id", "node_id", "target_node_id"):
            value = (payload or {}).get(key)
            if value is not None and not event_payload.get(key):
                event_payload[key] = value
        meta = dict(event_payload.get("_meta") or {})
        top_meta = (payload or {}).get("_meta")
        if isinstance(top_meta, dict):
            for key, value in top_meta.items():
                meta.setdefault(key, value)
        if meta:
            event_payload["_meta"] = meta
        _publish_bus(event_type, event_payload)
        await _ack(data={"event_type": event_type})
        return None

    if kind == "weather.city_changed":
        event_payload = dict(payload or {})
        event_payload["city"] = payload.get("city")
        event_payload["webspace_id"] = payload.get("webspace_id")
        _publish_bus("weather.city_changed", event_payload)
        await _ack()
        return None

    if kind == "demo_metrics.host_action":
        event_payload = dict(payload or {})
        event_payload["webspace_id"] = payload.get("webspace_id")
        _publish_bus("demo_metrics.host_action", event_payload)
        await _ack()
        return None

    if kind == "voice.chat.open":
        event_payload = dict(payload or {})
        event_payload["webspace_id"] = payload.get("webspace_id")
        _publish_bus("voice.chat.open", event_payload)
        await _ack()
        return None

    if kind == "voice.chat.user":
        event_payload = dict(payload or {})
        event_payload["text"] = payload.get("text")
        event_payload["webspace_id"] = payload.get("webspace_id")
        _publish_bus("voice.chat.user", event_payload)
        await _ack()
        return None

    if kind == "desktop.webspace.reload":
        payload = dict(payload or {})
        trace = _record_command_trace(
            kind=kind,
            cmd_id=cmd_id,
            payload=payload,
            device_id=device_id,
            webspace_id=webspace_id,
            client_label=client_label,
        )
        meta = dict(payload.get("_meta") or {})
        meta.setdefault("cmd_id", str(cmd_id or "").strip() or None)
        meta.setdefault("gateway_client", str(client_label or "").strip() or None)
        meta.setdefault("gateway_command_seq", int(trace.get("seq") or 0))
        meta.setdefault("gateway_command_fingerprint", str(trace.get("fingerprint") or ""))
        payload["_meta"] = meta
        _ylog.warning(
            "desktop.webspace.reload ingress cmd=%s seq=%s webspace=%s device=%s client=%s scenario=%s recreate_room=%s dup_recent=%s dup10s=%s fp=%s",
            cmd_id or "-",
            trace.get("seq") or 0,
            trace.get("webspace_id") or webspace_id,
            device_id or "-",
            client_label or "-",
            trace.get("scenario_id") or "-",
            "yes" if trace.get("recreate_room") else "no",
            "yes" if trace.get("duplicate_recent") else "no",
            trace.get("duplicate_count_10s") or 0,
            trace.get("fingerprint") or "-",
        )
        if bool(trace.get("duplicate_recent")):
            _ylog.warning(
                "desktop.webspace.reload duplicate suppressed webspace=%s cmd_id=%s seq=%s fp=%s dup10s=%s",
                webspace_id,
                cmd_id or "-",
                trace.get("seq") or 0,
                trace.get("fingerprint") or "-",
                trace.get("duplicate_count_10s") or 0,
            )
            await _ack(
                data={
                    "duplicate": True,
                    "suppressed": True,
                    "gateway_command_seq": int(trace.get("seq") or 0),
                    "gateway_command_fingerprint": str(trace.get("fingerprint") or ""),
                }
            )
            return None
        _publish_bus("desktop.webspace.reload", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.reset":
        payload = dict(payload or {})
        trace = _record_command_trace(
            kind=kind,
            cmd_id=cmd_id,
            payload=payload,
            device_id=device_id,
            webspace_id=webspace_id,
            client_label=client_label,
        )
        meta = dict(payload.get("_meta") or {})
        meta.setdefault("cmd_id", str(cmd_id or "").strip() or None)
        meta.setdefault("gateway_client", str(client_label or "").strip() or None)
        meta.setdefault("gateway_command_seq", int(trace.get("seq") or 0))
        meta.setdefault("gateway_command_fingerprint", str(trace.get("fingerprint") or ""))
        payload["_meta"] = meta
        _ylog.warning(
            "desktop.webspace.reset ingress cmd=%s seq=%s webspace=%s device=%s client=%s scenario=%s dup_recent=%s dup10s=%s fp=%s",
            cmd_id or "-",
            trace.get("seq") or 0,
            trace.get("webspace_id") or webspace_id,
            device_id or "-",
            client_label or "-",
            trace.get("scenario_id") or "-",
            "yes" if trace.get("duplicate_recent") else "no",
            trace.get("duplicate_count_10s") or 0,
            trace.get("fingerprint") or "-",
        )
        _publish_bus("desktop.webspace.reset", payload)
        await _ack()
        return None

    if kind == "desktop.scenario.set":
        payload = dict(payload or {})
        target = payload.get("scenario_id")
        if not target:
            await _ack(False, error="scenario_id required")
        else:
            try:
                from adaos.services.scenario.webspace_runtime import switch_webspace_scenario

                target_webspace = str(
                    payload.get("webspace_id")
                    or payload.get("workspace_id")
                    or webspace_id
                    or "default"
                ).strip() or "default"
                if "set_home" in payload:
                    set_home = bool(payload.get("set_home"))
                elif "persist_home" in payload:
                    set_home = bool(payload.get("persist_home"))
                else:
                    set_home = None
                wait_for_rebuild = (
                    bool(payload.get("wait_for_rebuild"))
                    if "wait_for_rebuild" in payload
                    else True
                )
                result = await switch_webspace_scenario(
                    target_webspace,
                    str(target),
                    set_home=set_home,
                    wait_for_rebuild=wait_for_rebuild,
                )
                await _ack(bool(result.get("accepted", result.get("ok", True))), data=result)
            except Exception as exc:
                _log.warning(
                    "desktop.scenario.set direct switch failed webspace=%s scenario=%s",
                    webspace_id,
                    target,
                    exc_info=True,
                )
                await _ack(False, error=f"{type(exc).__name__}: {exc}")
        return None

    if kind == "skills.update":
        # Trigger a best-effort skill source refresh (git pull / monorepo sparse pull)
        # and acknowledge with updated version if available.
        try:
            from adaos.services.agent_context import get_ctx as _get_ctx
            from adaos.services.skill.update import SkillUpdateService

            ctx = _get_ctx()
            skill_name = str(payload.get("name") or payload.get("skill") or "").strip()
            dry_run = bool(payload.get("dry_run", False))
            if not skill_name:
                await _ack(False, error="name required")
                return None
            result = SkillUpdateService(ctx).request_update(skill_name, dry_run=dry_run)
            _publish_bus("skills.updated", {"name": skill_name, "version": result.version, "updated": result.updated})
            await _ack(True, data={"name": skill_name, "updated": result.updated, "version": result.version})
        except FileNotFoundError:
            await _ack(False, error="skill_not_installed")
        except PermissionError as exc:
            await _ack(False, error=str(exc) or "fs_readonly")
        except Exception as exc:
            await _ack(False, error=str(exc) or "update_failed")
        return None

    if kind == "nlp.teacher.candidate.apply":
        _publish_bus("nlp.teacher.candidate.apply", {"candidate_id": payload.get("candidate_id"), "target": payload.get("target"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "nlp.teacher.revision.apply":
        _publish_bus(
            "nlp.teacher.revision.apply",
            {
                "revision_id": payload.get("revision_id"),
                "intent": payload.get("intent"),
                "examples": payload.get("examples"),
                "slots": payload.get("slots"),
                "webspace_id": payload.get("webspace_id"),
            },
        )
        await _ack()
        return None

    if kind == "nlp.teacher.regex_rule.apply":
        _publish_bus(
            "nlp.teacher.regex_rule.apply",
            {
                "candidate_id": payload.get("candidate_id"),
                "intent": payload.get("intent"),
                "pattern": payload.get("pattern"),
                "target": payload.get("target"),
                "webspace_id": payload.get("webspace_id"),
            },
        )
        await _ack()
        return None

    if kind == "scenario.workflow.action":
        _publish_bus("scenario.workflow.action", payload)
        await _ack()
        return None

    if kind == "scenario.workflow.set_state":
        _publish_bus("scenario.workflow.set_state", payload)
        await _ack()
        return None

    # Default behaviour for declarative host actions: publish unknown command
    # kinds to the local bus so skills can subscribe to their own UI events.
    if isinstance(kind, str) and kind.strip():
        _publish_bus(kind, payload)
    await _ack()
    return None


@router.websocket("/ws")
async def events_ws(websocket: WebSocket):
    """
    JSON events websocket.

    Implements device.register, desktop/voice/scenario commands, and WebRTC
    signaling (``rtc.offer``, ``rtc.ice``).
    """
    if not await _accept_websocket(websocket, channel="events"):
        return
    _transport_mark_open("ws")
    if _ws_trace_enabled():
        try:
            params: Dict[str, str] = dict(websocket.query_params)
            token_present = "token" in params
            _log.info(
                "ws trace open client=%s token=%s params=%s",
                _ws_client_str(websocket),
                token_present,
                ",".join(sorted(params.keys())) if params else "",
            )
        except Exception:
            pass

    device_id: str | None = None
    webspace_id = _coerce_gateway_webspace_id(None)
    ws_loop = asyncio.get_running_loop()

    async def _ws_send(msg: dict[str, Any]) -> None:
        try:
            await websocket.send_text(json.dumps(msg))
        except (WebSocketDisconnect, RuntimeError):
            # Connection closed - silently return
            return

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except RuntimeError as exc:
                if _is_websocket_receive_disconnect_race(exc):
                    if _ws_trace_enabled():
                        _log.info(
                            "ws receive skipped because connection is already closed client=%s reason=%s",
                            _ws_client_str(websocket),
                            str(exc),
                        )
                    break
                raise

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if msg.get("type") == "subscribe":
                added = _register_ws_event_subscriptions(
                    websocket,
                    ws_loop,
                    msg.get("topics"),
                )
                if added:
                    await _send_initial_ws_event_messages(websocket, added)
                    _request_webio_stream_snapshots(added, transport="ws")
                continue

            if msg.get("type") == "unsubscribe":
                _unregister_ws_event_subscription_topics(websocket, msg.get("topics"))
                continue

            ch = msg.get("ch")
            t = msg.get("t")
            if ch != "events" or t != "cmd":
                continue

            cmd_id = msg.get("id")
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            # -- WebRTC signaling (rtc.offer / rtc.ice) -----------------------
            if kind == "rtc.offer":
                try:
                    from adaos.services.webrtc.peer import handle_rtc_offer

                    async def _send_ice_via_ws(candidate: dict[str, Any]) -> None:
                        try:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "ch": "events",
                                        "t": "evt",
                                        "kind": "rtc.ice",
                                        "payload": {"candidate": candidate},
                                    }
                                )
                            )
                        except (WebSocketDisconnect, RuntimeError):
                            # Connection closed - silently return
                            return

                    answer = await handle_rtc_offer(
                        offer_sdp=payload.get("sdp", ""),
                        offer_type=payload.get("type", "offer"),
                        device_id=device_id or "unknown",
                        webspace_id=webspace_id,
                        send_ice_cb=_send_ice_via_ws,
                    )
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": True, "data": answer})
                except Exception as e:
                    _log.error(f"rtc.offer failed: {e!r}", exc_info=True)
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": f"rtc_offer_failed: {e}"})
                continue

            if kind == "rtc.ice":
                try:
                    from adaos.services.webrtc.peer import handle_remote_ice

                    await handle_remote_ice(device_id or "unknown", payload.get("candidate"))
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": True})
                except Exception as e:
                    _log.error(f"rtc.ice failed: {e!r}", exc_info=True)
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": f"rtc_ice_failed: {e}"})
                continue

            # -- Standard commands via extracted dispatcher --------------------
            new_ws = await process_events_command(
                kind=kind,
                cmd_id=cmd_id,
                payload=payload,
                device_id=device_id or "dev-unknown",
                webspace_id=webspace_id,
                client_label=_ws_client_str(websocket),
                send_response=_ws_send,
            )
            # Update connection-scoped state when a command changed it.
            if new_ws is not None:
                webspace_id = new_ws
            if kind == "device.register":
                device_id = payload.get("device_id") or "dev-unknown"
    finally:
        _transport_mark_close("ws")
        _unregister_ws_event_subscriptions(websocket)
        _ = device_id
        if _ws_trace_enabled():
            try:
                code = getattr(websocket, "close_code", None)
                _log.info(
                    "ws trace closed client=%s device=%s webspace=%s code=%s",
                    _ws_client_str(websocket),
                    device_id,
                    webspace_id,
                    code,
                )
            except Exception:
                pass
