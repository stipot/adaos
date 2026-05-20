# src\adaos\services\bootstrap.py
from __future__ import annotations

import asyncio
import base64
import hashlib
import json as _json
import logging
import math
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Sequence
from urllib.parse import urlparse

import nats as _nats

from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.domain import Event
from adaos.ports.heartbeat import HeartbeatPort
from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.ports.subnet_registry import SubnetRegistryPort
from adaos.sdk.core.decorators import register_subscriptions
from adaos.sdk.data import bus
from adaos.services import yjs as _y_store  # ensure YStore subscriptions are registered
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.chat_io import telemetry as tm
from adaos.services.chat_io.interfaces import ChatOutputEvent, ChatOutputMessage
from adaos.services.chat_io.nlu_bridge import register_chat_nlu_bridge  # chat->NLU bridge
from adaos.services.eventbus import LocalEventBus
from adaos.services.io_bus.http_fallback import HttpFallbackBus
from adaos.services.io_bus.local_bus import LocalIoBus
from adaos.services.nats_config import (
    PUBLIC_NATS_WS_API,
    PUBLIC_NATS_WS_DEDICATED,
    normalize_nats_ws_url,
    nats_url_uses_websocket,
    order_nats_ws_candidates,
    public_nats_ws_api,
    public_nats_tcp_candidates,
    public_nats_ws_candidates,
)
from adaos.services.reliability import (
    ReadinessStatus,
    configure_hub_root_transport_strategy,
    hub_root_protocol_class_policy,
    hub_root_protocol_traffic_class,
    hub_root_transport_strategy_snapshot,
    mark_root_control_down,
    note_root_control_reconnect,
    mark_root_control_up,
    mark_route_degraded,
    mark_route_ready,
    note_route_incident,
    observe_route_e2e,
    observe_hub_root_integration_outbox,
    observe_hub_root_protocol_publish,
    observe_hub_root_protocol_subscription,
    observe_hub_root_route_flow,
    observe_hub_root_route_runtime,
    record_hub_root_transport_event,
    set_integration_readiness,
)
from adaos.services.realtime_sidecar import (
    probe_realtime_sidecar_ready,
    realtime_sidecar_diag_path,
    realtime_sidecar_enabled,
    realtime_sidecar_host,
    realtime_sidecar_log_path,
    realtime_sidecar_local_url,
    realtime_sidecar_port,
    realtime_sidecar_route_tunnel_ws_bases,
    resolve_realtime_remote_candidates,
)
from adaos.services.node_config import NodeConfig, generate_provisional_subnet_id, load_config, set_role as cfg_set_role
from adaos.services.node_runtime_state import (
    load_member_hub_token,
    load_nats_runtime_config,
    migrate_legacy_nats_runtime_config,
    save_nats_runtime_config,
)
from adaos.services.nats_errors import (
    install_transient_nats_log_filter,
    is_transient_nats_error,
    nats_error_summary,
)
from adaos.services.hub_root_outbox_store import load_outbox_items, outbox_store_path, save_outbox_items
from adaos.services.root.control_lifecycle_sync import report_hub_control_lifecycle_state
from adaos.services.root.core_update_sync import reconcile_hub_core_update
from adaos.services.runtime_identity import (
    runtime_connect_name,
    runtime_identity_snapshot,
    runtime_instance_id,
    runtime_transition_role,
)
from adaos.services.scheduler import start_scheduler, stop_scheduler
from adaos.services.scenario import (
    webspace_runtime as _scenario_ws_runtime,  # ensure core scenario subscriptions
)
from adaos.services.scenario import workflow_runtime as _scenario_workflow_runtime  # ensure scenario workflow subscriptions
from adaos.services import weather as _weather_services  # ensure weather observers
from adaos.services import nlu as _nlu_services  # ensure NLU dispatcher subscriptions
from adaos.services import named_entity_projection as _named_entity_projection  # ensure named-entity projection subscriptions
from adaos.services.skill import runtime_shutdown_runtime as _runtime_shutdown_runtime  # ensure skill shutdown subscriptions
from adaos.services.skill import service_supervisor_runtime as _service_supervisor_runtime  # ensure service supervisor subscriptions
from adaos.services.skill.service_supervisor import get_service_supervisor
from adaos.services.zone_hosts import canonical_zone_id, zone_public_base_url
from adaos.services.subnet_alias import save_subnet_alias
from adaos.integrations.telegram.sender import TelegramSender


def _bounded_interval_seconds(raw: Any, *, default: float, minimum: float) -> float:
    try:
        interval_s = float(raw)
    except Exception:
        interval_s = float(default)
    if not math.isfinite(interval_s):
        interval_s = float(default)
    if interval_s < float(minimum):
        interval_s = float(minimum)
    return float(interval_s)


def _should_forward_node_status_to_members(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    meta = payload.get("_meta")
    if not isinstance(meta, dict):
        return True
    return not bool(meta.get("subnet_origin_node_id"))


def _node_status_emit_fingerprint(payload: object) -> tuple[Any, ...]:
    if not isinstance(payload, dict):
        return ("invalid",)
    node_names = payload.get("node_names")
    if isinstance(node_names, list):
        normalized_node_names = tuple(str(item or "").strip() for item in node_names if str(item or "").strip())
    else:
        normalized_node_names = ()
    connected_to_subnet = payload.get("connected_to_subnet")
    if connected_to_subnet is None:
        connected_to_subnet = payload.get("connected_to_hub")
    connected_to_hub = payload.get("connected_to_hub")
    if connected_to_hub is None:
        connected_to_hub = connected_to_subnet
    return (
        str(payload.get("node_id") or "").strip(),
        str(payload.get("subnet_id") or "").strip(),
        str(payload.get("role") or "").strip(),
        normalized_node_names,
        str(payload.get("primary_node_name") or "").strip(),
        bool(payload.get("ready")),
        str(payload.get("node_state") or "").strip(),
        bool(payload.get("draining")),
        str(payload.get("route_mode") or "").strip(),
        connected_to_subnet,
        connected_to_hub,
        str(payload.get("trigger") or "").strip(),
    )


def _should_emit_node_status(
    *,
    payload: object,
    now: float,
    last_emitted_at: float,
    last_fingerprint: tuple[Any, ...] | None,
    dedupe_window_s: float = 1.0,
) -> tuple[bool, tuple[Any, ...]]:
    fingerprint = _node_status_emit_fingerprint(payload)
    if (
        last_fingerprint is not None
        and fingerprint == last_fingerprint
        and (now - float(last_emitted_at or 0.0)) < float(dedupe_window_s)
    ):
        return False, fingerprint
    return True, fingerprint


def _env_truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("", "0", "false", "off", "no")


def _hub_channel_console_trace_enabled() -> bool:
    return _env_truthy(os.getenv("HUB_CHANNEL_CONSOLE_TRACE"), default=False)


def _hub_channel_console_allow_rl(key: str, msg: str) -> bool:
    if _hub_channel_console_trace_enabled():
        return True
    text = str(msg or "")
    detail_prefixes = (
        "nats.ws_diag",
        "nats.ws_eof",
        "nats.env",
        "nats.transport",
        "nats.ws_hb",
        "nats.ws_tag",
        "nats.keepalive",
        "nats.connect_try",
        "nats.try",
        "root.snap",
        "root.snap_fail",
        "nats.sidecar_route",
        "nats.sidecar_unready",
        "hub-route.probe_resend",
        "hub-route.probe_resend_cfg",
    )
    if any(str(key or "").startswith(prefix) for prefix in detail_prefixes):
        return False
    if "[hub-io] nats ws diag:" in text:
        return False
    return True


def _is_local_http_base(url: str) -> bool:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        return host in ("127.0.0.1", "localhost")
    except Exception:
        return False


def _hub_route_prefers_supervisor_public_status(path_norm: str, method: str) -> bool:
    return method in ("GET", "HEAD") and path_norm in {
        "/api/supervisor/public/update-status",
        "/api/supervisor/public/memory-status",
    }


def _dev_without_supervisor() -> bool:
    env_type = str(os.getenv("ENV_TYPE") or "").strip().lower()
    if env_type != "dev":
        return False
    return str(os.getenv("ADAOS_SUPERVISOR_ENABLED") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def _dev_api_serve_core_update_sync_disabled() -> bool:
    try:
        from adaos.services.core_update_policy import core_update_reactions_disabled_reason

        return core_update_reactions_disabled_reason() is not None
    except Exception:
        launch_mode = str(os.getenv("ADAOS_RUNTIME_LAUNCH_MODE") or "").strip().lower()
        if launch_mode != "api_serve":
            return False
        return str(os.getenv("ADAOS_API_SERVE_ALLOW_CORE_UPDATE") or "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }


def _supervisor_local_bases() -> list[str]:
    if _dev_without_supervisor():
        return []
    bases: list[str] = []
    explicit = (
        os.getenv("ADAOS_SUPERVISOR_URL")
        or os.getenv("ADAOS_SUPERVISOR_BASE")
        or ""
    ).strip()
    if explicit and _is_local_http_base(explicit):
        bases.append(explicit.rstrip("/"))
    supervisor_port = str(os.getenv("ADAOS_SUPERVISOR_PORT") or "").strip() or "8776"
    bases.append(f"http://127.0.0.1:{supervisor_port}")
    bases.append(f"http://localhost:{supervisor_port}")
    seen: set[str] = set()
    return [b for b in bases if (b not in seen and not seen.add(b))]


_ROUTE_LOCAL_BASE_LOCK = threading.RLock()
_ROUTE_LOCAL_BASE_CACHE: dict[str, Any] = {
    "value": None,
    "expires_at": 0.0,
}
_ROUTE_LOCAL_BASE_DIAG: dict[str, Any] = {
    "local_base_discovery_total": 0,
    "local_base_cache_hit_total": 0,
    "local_base_error_total": 0,
    "local_base_runtime_port_shortcut_total": 0,
    "local_base_last_source": "",
    "local_base_last_value": "",
    "local_base_last_latency_ms": None,
    "local_base_last_error": "",
    "local_base_last_error_at": 0.0,
    "local_base_last_discovered_at": 0.0,
}


def _route_local_base_cache_ttl_s() -> float:
    raw = str(os.getenv("HUB_ROUTE_LOCAL_BASE_CACHE_TTL_S") or "").strip()
    if not raw:
        return 5.0
    try:
        value = float(raw)
    except Exception:
        return 5.0
    if value < 0.0:
        return 0.0
    if value > 60.0:
        return 60.0
    return value


def _runtime_port_local_http_base() -> str | None:
    runtime_port = str(os.getenv("ADAOS_RUNTIME_PORT") or "").strip()
    if runtime_port.isdigit():
        return f"http://127.0.0.1:{runtime_port}"
    return None


def _runtime_port_probe_candidates() -> list[str]:
    bases: list[str] = []
    runtime_port_base = _runtime_port_local_http_base()
    if runtime_port_base:
        bases.append(runtime_port_base)
    bases.extend(
        [
            "http://127.0.0.1:8778",
            "http://localhost:8778",
            "http://127.0.0.1:8777",
            "http://localhost:8777",
        ]
    )
    seen: set[str] = set()
    return [b for b in bases if (b not in seen and not seen.add(b))]


def _route_state_dir_from_ctx(ctx: Any | None) -> Path | None:
    try:
        paths = getattr(ctx, "paths", None)
        raw = getattr(paths, "state_dir", None)
        value = raw() if callable(raw) else raw
        if value:
            return Path(value).expanduser().resolve()
    except Exception:
        return None
    return None


def _route_state_dir_fallback() -> Path | None:
    try:
        base_dir = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
        if base_dir:
            return Path(base_dir).expanduser().resolve() / "state"
        return Path(os.path.expanduser("~")).expanduser().resolve() / ".adaos" / "state"
    except Exception:
        return None


def _active_runtime_state_local_http_bases(ctx: Any | None = None) -> list[str]:
    """Return supervisor-advertised local runtime bases without network probing.

    Route handlers run on the runtime event loop and must not synchronously probe
    localhost on the hot path. Supervisor state and node_runtime.json are useful
    fallbacks during early bootstrap and slot transitions, but they are persisted
    files and can briefly lag behind the process that is handling this message.
    """
    state_dir = _route_state_dir_from_ctx(ctx) or _route_state_dir_fallback()
    if state_dir is None:
        return []

    paths = [
        state_dir / "supervisor" / "runtime.json",
        state_dir / "node_runtime.json",
    ]
    bases: list[str] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = _json.load(fh)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        for key in ("runtime_url", "hub_url"):
            value = str(payload.get(key) or "").strip().rstrip("/")
            if value and _is_local_http_base(value):
                bases.append(value)

        try:
            port_raw = payload.get("runtime_port")
            port = int(port_raw) if port_raw is not None else 0
            host = str(payload.get("runtime_host") or "127.0.0.1").strip() or "127.0.0.1"
            if port > 0 and host in ("127.0.0.1", "localhost"):
                bases.append(f"http://127.0.0.1:{port}")
        except Exception:
            pass

    seen: set[str] = set()
    return [b for b in bases if (b not in seen and not seen.add(b))]


def _append_local_http_base(bases: list[str], value: str | None) -> None:
    base = str(value or "").strip().rstrip("/")
    if base and _is_local_http_base(base):
        bases.append(base)


def _hub_route_local_http_timeout(path: str) -> tuple[float, float]:
    path_norm = "/" + str(path or "").split("?", 1)[0].lstrip("/")
    if path_norm in ("/api/node/status", "/api/ping", "/healthz"):
        return (0.5, 1.2)
    if path_norm == "/api/tools/call":
        # Root allows tools/call to take up to 60s. Keep the local hop below
        # that ceiling, but do not make member-link tools fail under normal
        # cross-node latency.
        return (1.5, 55.0)
    return (1.5, 2.5)


def _hub_route_should_retry_http_upstream_error(
    *, method: str, path: str, error_kind: str
) -> bool:
    path_norm = "/" + str(path or "").split("?", 1)[0].lstrip("/")
    method_norm = str(method or "").strip().upper()
    kind = str(error_kind or "").strip()
    if kind == "ReadTimeout" and (
        method_norm not in {"GET", "HEAD"} or path_norm == "/api/tools/call"
    ):
        return False
    return True


def _probe_runtime_http_base(sess: Any, *, base: str, timeout_s: float) -> bool:
    try:
        response = sess.get(
            str(base).rstrip("/") + "/api/ping",
            headers={"Accept": "application/json"},
            timeout=max(0.1, float(timeout_s)),
        )
        if int(response.status_code) != 200:
            return False
        payload = response.json()
        if not isinstance(payload, dict):
            return False
        if not bool(payload.get("ok")):
            return False
        return str(payload.get("service") or "").strip() == "adaos-runtime"
    except Exception:
        return False


def _observe_route_local_base_diag(**details: Any) -> None:
    with _ROUTE_LOCAL_BASE_LOCK:
        _ROUTE_LOCAL_BASE_DIAG.update(details)
        snapshot = dict(_ROUTE_LOCAL_BASE_DIAG)
    try:
        observe_hub_root_route_runtime(**snapshot)
    except Exception:
        pass


def _note_route_local_base_shortcut(*, source: str, value: str | None) -> None:
    now = time.time()
    with _ROUTE_LOCAL_BASE_LOCK:
        _ROUTE_LOCAL_BASE_DIAG["local_base_runtime_port_shortcut_total"] = int(
            _ROUTE_LOCAL_BASE_DIAG.get("local_base_runtime_port_shortcut_total") or 0
        ) + 1
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_source"] = str(source or "").strip() or "runtime_port_env"
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_value"] = str(value or "").strip()
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_discovered_at"] = now
        snapshot = dict(_ROUTE_LOCAL_BASE_DIAG)
    try:
        observe_hub_root_route_runtime(**snapshot)
    except Exception:
        pass


def _discover_active_runtime_local_base(
    *, timeout_s: float = 0.6, allow_network_probe: bool = False
) -> str | None:
    try:
        import requests  # type: ignore
    except Exception:
        return None

    now = time.time()
    ttl_s = _route_local_base_cache_ttl_s()
    with _ROUTE_LOCAL_BASE_LOCK:
        cached_value = str(_ROUTE_LOCAL_BASE_CACHE.get("value") or "").strip() or None
        cached_expires_at = float(_ROUTE_LOCAL_BASE_CACHE.get("expires_at") or 0.0)
        if cached_value and cached_expires_at > now:
            _ROUTE_LOCAL_BASE_DIAG["local_base_cache_hit_total"] = int(
                _ROUTE_LOCAL_BASE_DIAG.get("local_base_cache_hit_total") or 0
            ) + 1
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_source"] = "cache"
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_value"] = cached_value
            snapshot = dict(_ROUTE_LOCAL_BASE_DIAG)
            try:
                observe_hub_root_route_runtime(**snapshot)
            except Exception:
                pass
            return cached_value

        # Route handling runs on the event loop. When we have no cached local base,
        # skip synchronous network probing in the hot path and fall back to static
        # localhost candidates instead of blocking the loop on connect timeouts.
        if not allow_network_probe:
            _ROUTE_LOCAL_BASE_DIAG["local_base_cache_miss_total"] = int(
                _ROUTE_LOCAL_BASE_DIAG.get("local_base_cache_miss_total") or 0
            ) + 1
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_source"] = "cache_miss_no_probe"
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_value"] = ""
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_error"] = "network_probe_skipped"
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_error_at"] = now
            snapshot = dict(_ROUTE_LOCAL_BASE_DIAG)
            try:
                observe_hub_root_route_runtime(**snapshot)
            except Exception:
                pass
            return None

    started = time.monotonic()
    result: str | None = None
    result_source = ""
    last_error = ""
    sess = requests.Session()
    try:
        try:
            sess.trust_env = False
        except Exception:
            pass

        for supervisor_base in _supervisor_local_bases():
            try:
                response = sess.get(
                    supervisor_base + "/api/supervisor/public/update-status",
                    headers={"Accept": "application/json"},
                    timeout=max(0.1, float(timeout_s)),
                )
                if int(response.status_code) != 200:
                    last_error = f"status:{response.status_code}"
                    continue
                payload = response.json()
                runtime = payload.get("runtime") if isinstance(payload, dict) else {}
                runtime_url = str((runtime or {}).get("runtime_url") or "").strip().rstrip("/")
                if runtime_url and _is_local_http_base(runtime_url):
                    result = runtime_url
                    result_source = "supervisor_public_status"
                    break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if not result:
            probe_timeout_s = max(0.1, min(float(timeout_s), 0.35))
            for runtime_base in _runtime_port_probe_candidates():
                if _probe_runtime_http_base(sess, base=runtime_base, timeout_s=probe_timeout_s):
                    result = runtime_base.rstrip("/")
                    result_source = "runtime_port_probe"
                    last_error = ""
                    break
    finally:
        try:
            sess.close()
        except Exception:
            pass

    latency_ms = round((time.monotonic() - started) * 1000.0, 3)
    with _ROUTE_LOCAL_BASE_LOCK:
        _ROUTE_LOCAL_BASE_DIAG["local_base_discovery_total"] = int(
            _ROUTE_LOCAL_BASE_DIAG.get("local_base_discovery_total") or 0
        ) + 1
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_latency_ms"] = latency_ms
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_source"] = (
            str(result_source or "").strip()
            if result
            else "supervisor_public_status_failed"
        )
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_value"] = str(result or "").strip()
        _ROUTE_LOCAL_BASE_DIAG["local_base_last_discovered_at"] = time.time()
        if result:
            _ROUTE_LOCAL_BASE_CACHE["value"] = result
            _ROUTE_LOCAL_BASE_CACHE["expires_at"] = time.time() + max(0.0, ttl_s)
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_error"] = ""
        else:
            _ROUTE_LOCAL_BASE_DIAG["local_base_error_total"] = int(
                _ROUTE_LOCAL_BASE_DIAG.get("local_base_error_total") or 0
            ) + 1
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_error"] = last_error
            _ROUTE_LOCAL_BASE_DIAG["local_base_last_error_at"] = time.time()
            _ROUTE_LOCAL_BASE_CACHE["value"] = None
            _ROUTE_LOCAL_BASE_CACHE["expires_at"] = 0.0
        snapshot = dict(_ROUTE_LOCAL_BASE_DIAG)
    try:
        observe_hub_root_route_runtime(**snapshot)
    except Exception:
        pass
    return result


def _build_hub_route_http_bases(
    *, path_norm: str, method: str, cfg: Any | None, ctx: Any | None = None
) -> list[str]:
    bases: list[str] = []
    env_base = (
        os.getenv("ADAOS_SELF_BASE_URL")
        or os.getenv("ADAOS_BASE")
        or os.getenv("ADAOS_API_BASE")
        or ""
    ).strip()
    cfg_base = str(getattr(cfg, "hub_url", None) or "").strip()
    runtime_port = str(os.getenv("ADAOS_RUNTIME_PORT") or "").strip()
    runtime_port_base = _runtime_port_local_http_base()
    state_bases = _active_runtime_state_local_http_bases(ctx)

    if _hub_route_prefers_supervisor_public_status(path_norm, method):
        bases.extend(_supervisor_local_bases())

    if runtime_port_base:
        _note_route_local_base_shortcut(source="runtime_port_env", value=runtime_port_base)
        bases.append(runtime_port_base)
    if runtime_port.isdigit():
        bases.append(f"http://127.0.0.1:{runtime_port}")
    _append_local_http_base(bases, env_base)
    _append_local_http_base(bases, cfg_base)
    bases.extend(state_bases)

    if not runtime_port_base and not state_bases:
        active_runtime_base = _discover_active_runtime_local_base()
        if active_runtime_base:
            _append_local_http_base(bases, active_runtime_base)

    # Keep runtime ports as fallback even for the browser-safe supervisor status path.
    bases.extend(["http://127.0.0.1:8778", "http://127.0.0.1:8777"])

    seen_bases: set[str] = set()
    return [b for b in bases if (b not in seen_bases and not seen_bases.add(b))]


def _http_base_to_ws_base(base: str) -> str:
    value = str(base or "").strip().rstrip("/")
    if value.startswith("https://"):
        return "wss://" + value[len("https://"):]
    if value.startswith("http://"):
        return "ws://" + value[len("http://"):]
    return value


def _build_hub_route_ws_bases(
    *, cfg: Any | None, path: str | None = None, ctx: Any | None = None
) -> list[str]:
    bases: list[str] = []
    role = str(getattr(cfg, "role", None) or "").strip().lower() or None
    bases.extend(realtime_sidecar_route_tunnel_ws_bases(path=path, role=role))
    env_base = str(os.getenv("ADAOS_SELF_BASE_URL") or "").strip()
    cfg_base = str(getattr(cfg, "hub_url", None) or "").strip()
    runtime_port_base = _runtime_port_local_http_base()
    state_bases = _active_runtime_state_local_http_bases(ctx)

    if runtime_port_base:
        _note_route_local_base_shortcut(source="runtime_port_env", value=runtime_port_base)
        bases.append(_http_base_to_ws_base(runtime_port_base))
    if env_base and _is_local_http_base(env_base):
        bases.append(_http_base_to_ws_base(env_base))
    if cfg_base and _is_local_http_base(cfg_base):
        bases.append(_http_base_to_ws_base(cfg_base))
    for state_base in state_bases:
        bases.append(_http_base_to_ws_base(state_base))

    if not runtime_port_base and not state_bases:
        active_runtime_base = _discover_active_runtime_local_base()
        if active_runtime_base:
            bases.append(_http_base_to_ws_base(active_runtime_base))

    bases.extend(["ws://127.0.0.1:8778", "ws://127.0.0.1:8777"])

    seen_bases: set[str] = set()
    return [b for b in bases if (b not in seen_bases and not seen_bases.add(b))]


def _hub_root_transport_kind(server: str | None) -> str | None:
    text = str(server or "").strip().lower()
    if not text:
        return None
    if text.startswith(("ws://", "wss://")):
        return "ws"
    if text.startswith(("nats://", "tls://")):
        return "tcp"
    if text.startswith(("http://", "https://")):
        return "sidecar"
    return None


def _hub_nats_prefer_dedicated() -> str:
    raw = os.getenv("HUB_NATS_PREFER_DEDICATED")
    text = str(raw or "").strip()
    if text:
        return text
    return "0"


def _normalize_hub_nats_ws_url(value: str | None) -> str | None:
    normalized = normalize_nats_ws_url(value, fallback=None)
    if _hub_nats_prefer_dedicated() == "1":
        return normalized
    if normalized == PUBLIC_NATS_WS_DEDICATED:
        return public_nats_ws_api()
    return normalized


def _hub_public_ws_candidates(base_url: str | None) -> list[str]:
    prefer_dedicated = _hub_nats_prefer_dedicated()
    normalized_base = _normalize_hub_nats_ws_url(base_url)

    candidates: list[str] = []
    if normalized_base and nats_url_uses_websocket(normalized_base):
        candidates.append(normalized_base)
    for item in public_nats_ws_candidates(
        prefer_dedicated=prefer_dedicated,
        allow_dedicated_fallback=prefer_dedicated == "1",
    ):
        if item not in candidates:
            candidates.append(item)
    return candidates


def _hub_public_tcp_candidates(base_url: str | None) -> list[str]:
    prefer_dedicated = _hub_nats_prefer_dedicated()
    candidates: list[str] = []
    base = str(base_url or "").strip()
    if base:
        candidates.append(base)
    for item in public_nats_tcp_candidates(
        prefer_dedicated=prefer_dedicated,
        allow_dedicated_fallback=prefer_dedicated == "1",
    ):
        if item not in candidates:
            candidates.append(item)
    return candidates


def _hub_route_force_close_no_upstream_s() -> float:
    raw = os.getenv("HUB_ROUTE_FORCE_CLOSE_NO_UPSTREAM_S")
    if raw is None:
        return 1.5
    try:
        value = float(str(raw).strip() or "0")
    except Exception:
        value = 0.0
    if value <= 0.0:
        return 0.0
    if value < 0.25:
        value = 0.25
    if value > 30.0:
        value = 30.0
    return value


def _runtime_candidate_mode() -> bool:
    return runtime_transition_role() == "candidate"


def _hub_root_candidate_passive_mode() -> bool:
    return _runtime_candidate_mode()


def _nats_url_needs_public_ws_refresh(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw or nats_url_uses_websocket(raw):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if (parsed.scheme or "").lower() != "nats":
        return False
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    return host == "api.inimatic.com" or host.endswith(".inimatic.com")


def _build_realtime_sidecar_fallback_candidates(
    candidates: Sequence[str | None],
    *,
    local_candidate: str,
) -> list[str]:
    allow_tcp_fallback = _env_truthy(os.getenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK"), default=False)
    fallback_candidates: list[str] = []
    for item in candidates:
        try:
            candidate_text = str(item or "").strip()
        except Exception:
            continue
        if not candidate_text or candidate_text == local_candidate:
            continue
        if candidate_text.startswith("ws"):
            continue
        if not allow_tcp_fallback:
            continue
        if candidate_text not in fallback_candidates:
            fallback_candidates.append(candidate_text)
    return fallback_candidates


def _resolve_nats_log_server(
    *,
    server: str | None = None,
    current_attempt: str | None = None,
    connected_server: str | None = None,
) -> str | None:
    for value in (server, current_attempt, connected_server):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _hub_id_from_nats_user(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("hub_") and len(raw) > 4:
        return raw[4:]
    return None


def _canonical_hub_nats_identity(
    *,
    local_hub_id: str | None,
    nats_user: str | None,
    response_hub_id: str | None = None,
) -> tuple[str | None, str | None]:
    resolved_hub_id = (
        str(response_hub_id or "").strip()
        or _hub_id_from_nats_user(nats_user)
        or str(local_hub_id or "").strip()
        or None
    )
    if resolved_hub_id:
        return resolved_hub_id, f"hub_{resolved_hub_id}"
    resolved_user = str(nats_user or "").strip() or None
    return None, resolved_user


class BootstrapService:
    def __init__(
        self,
        ctx: AgentContext,
        *,
        heartbeat: HeartbeatPort,
        skills_loader: SkillsLoaderPort,
        subnet_registry: SubnetRegistryPort,
    ) -> None:
        self.ctx = ctx
        self.heartbeat = heartbeat
        self.skills_loader = skills_loader
        self.subnet_registry = subnet_registry
        self._boot_tasks: List[asyncio.Task] = []
        self._boot_lock = asyncio.Lock()
        self._boot_done = asyncio.Event()
        self._boot_done.set()
        self._boot_in_progress = False
        self._ready = asyncio.Event()
        self._booted = False
        self._app: Any = None
        self._io_bus: Any = None
        self._log = logging.getLogger("adaos.hub-io")
        # Current hub-root NATS client (when connected). Used for forced reconnects without full process restart.
        self._hub_root_nc: Any = None
        # Best-effort route relay reset hook installed by the hub-route runtime once subscriptions are live.
        self._hub_root_route_reset: Any = None

    def _find_live_boot_task(self, task_name: str) -> asyncio.Task | None:
        live_tasks: list[asyncio.Task] = []
        found: asyncio.Task | None = None
        for task in self._boot_tasks:
            if task.done():
                continue
            live_tasks.append(task)
            if found is None and task.get_name() == task_name:
                found = task
        if len(live_tasks) != len(self._boot_tasks):
            self._boot_tasks = live_tasks
        return found

    def _start_boot_task_once(self, task_name: str, coro_factory: Callable[[], Awaitable[Any]]) -> asyncio.Task:
        existing = self._find_live_boot_task(task_name)
        if existing is not None:
            return existing
        task = asyncio.create_task(coro_factory(), name=task_name)
        self._boot_tasks.append(task)
        return task

    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def _reset_hub_root_route_runtime(
        self,
        *,
        reason: str,
        notify_browser: bool,
    ) -> dict[str, Any]:
        cb = getattr(self, "_hub_root_route_reset", None)
        if not callable(cb):
            return {
                "ok": False,
                "reason": str(reason or "").strip() or "route_reset",
                "notify_browser": bool(notify_browser),
                "skipped": "route_reset_unavailable",
            }
        try:
            timeout_s = float(os.getenv("HUB_ROUTE_RESET_TIMEOUT_S", "2.5") or "2.5")
        except Exception:
            timeout_s = 2.5
        if timeout_s < 0.2:
            timeout_s = 0.2
        try:
            result = cb(
                reason=str(reason or "").strip() or "route_reset",
                notify_browser=bool(notify_browser),
            )
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=timeout_s)
            if isinstance(result, dict):
                return result
            return {
                "ok": True,
                "reason": str(reason or "").strip() or "route_reset",
                "notify_browser": bool(notify_browser),
                "result": result,
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "reason": str(reason or "").strip() or "route_reset",
                "notify_browser": bool(notify_browser),
                "error": "TimeoutError: hub route reset timed out",
            }
        except Exception as exc:
            return {
                "ok": False,
                "reason": str(reason or "").strip() or "route_reset",
                "notify_browser": bool(notify_browser),
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def request_hub_root_reconnect(
        self,
        *,
        transport: str | None = None,
        url_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Force hub-root transport reconnect.

        This is a debugging/ops hook: update env-like overrides and proactively close the current
        NATS connection so the supervisor reconnects using new settings.
        """
        tr = str(transport or "").strip().lower() or None
        override = str(url_override or "").strip() or None
        close_diag: dict[str, Any] = {"attempted": False, "timeout": False, "forced_ws_close": False}

        def _safe_strategy() -> dict[str, Any]:
            try:
                return hub_root_transport_strategy_snapshot()
            except Exception:
                return {}

        try:
            if tr is not None:
                os.environ["HUB_NATS_TRANSPORT"] = tr
            if override is not None:
                os.environ["HUB_NATS_URL_OVERRIDE"] = override
            elif url_override is not None:
                # Explicit empty override clears it.
                os.environ.pop("HUB_NATS_URL_OVERRIDE", None)
            try:
                strategy_update: dict[str, Any] = {}
                if transport is not None:
                    strategy_update["requested_transport"] = tr
                if url_override is not None:
                    strategy_update["url_override"] = override
                if strategy_update:
                    configure_hub_root_transport_strategy(**strategy_update)
                record_hub_root_transport_event(
                    "reconnect_requested",
                    transport=tr,
                    server=override,
                    summary="manual hub-root reconnect requested",
                    details={"requested_transport": tr, "url_override": override},
                )
            except Exception:
                pass
            try:
                close_diag["route_reset"] = await self._reset_hub_root_route_runtime(
                    reason="manual_reconnect",
                    notify_browser=True,
                )
            except Exception:
                pass
            # Trigger reconnect by closing the active connection if present.
            nc = getattr(self, "_hub_root_nc", None)
            if nc is not None:
                try:
                    close = getattr(nc, "close", None)
                    if callable(close):
                        close_diag["attempted"] = True
                        try:
                            close_timeout_s = float(os.getenv("HUB_ROOT_RECONNECT_CLOSE_TIMEOUT_S", "1.5") or "1.5")
                        except Exception:
                            close_timeout_s = 1.5
                        if close_timeout_s < 0.2:
                            close_timeout_s = 0.2

                        # NOTE: asyncio.wait_for() can itself hang if the close coroutine ignores cancellation.
                        # Use asyncio.wait() with timeout to ensure the HTTP request returns promptly.
                        try:
                            task = asyncio.create_task(close())
                            _done, pending = await asyncio.wait({task}, timeout=close_timeout_s)
                            if pending:
                                close_diag["timeout"] = True
                                try:
                                    task.cancel()
                                except Exception:
                                    pass
                                # Best-effort: force-close websocket transport internals if present to avoid a stuck close().
                                try:
                                    tr_obj = getattr(nc, "_transport", None)
                                    ws = getattr(tr_obj, "_ws", None) if tr_obj else None
                                    close_task = getattr(tr_obj, "_close_task", None) if tr_obj else None
                                    client = getattr(tr_obj, "_client", None) if tr_obj else None
                                    try:
                                        if ws is not None:
                                            t = asyncio.create_task(ws.close())
                                            await asyncio.wait({t}, timeout=0.5)
                                            if not t.done():
                                                try:
                                                    t.cancel()
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                                    try:
                                        if close_task is not None and hasattr(close_task, "done") and not close_task.done():
                                            close_task.set_result(None)
                                    except Exception:
                                        pass
                                    try:
                                        if client is not None:
                                            t = asyncio.create_task(client.close())
                                            await asyncio.wait({t}, timeout=0.5)
                                            if not t.done():
                                                try:
                                                    t.cancel()
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                                    close_diag["forced_ws_close"] = True
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
            return {
                "ok": True,
                "requested": {"transport": tr, "url_override": override},
                "strategy": _safe_strategy(),
                "close": close_diag,
            }
        except Exception as exc:
            return {
                "ok": False,
                "requested": {"transport": tr, "url_override": override},
                "strategy": _safe_strategy(),
                "close": close_diag,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _member_hub_transition_snapshot(self) -> dict[str, Any]:
        try:
            from adaos.services.core_update import read_status as _read_core_update_status
        except Exception:
            _read_core_update_status = None
        try:
            from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot as _runtime_lifecycle_snapshot
        except Exception:
            _runtime_lifecycle_snapshot = None

        update_status = _read_core_update_status() if callable(_read_core_update_status) else {}
        lifecycle = _runtime_lifecycle_snapshot() if callable(_runtime_lifecycle_snapshot) else {}
        status = update_status if isinstance(update_status, dict) else {}
        runtime = lifecycle if isinstance(lifecycle, dict) else {}
        state = str(status.get("state") or "").strip().lower()
        phase = str(status.get("phase") or "").strip().lower()
        node_state = str(runtime.get("node_state") or "").strip().lower()
        lifecycle_reason = str(runtime.get("reason") or "").strip().lower()
        draining = bool(runtime.get("draining"))

        transition_state = "ready"
        reason = "none"
        recovery_blocked = False
        if state in {"preparing", "countdown", "draining", "stopping", "applying"}:
            transition_state = "paused_for_update"
            reason = state
            recovery_blocked = True
        elif state == "restarting" or phase in {"launch", "root_promoted"}:
            transition_state = "restarting"
            reason = state or phase or "restarting"
            recovery_blocked = True
        elif state == "validated" and phase == "root_promotion_pending":
            transition_state = "waiting_restart"
            reason = "root_promotion_pending"
            recovery_blocked = True
        elif draining or node_state in {"stopping", "stopped", "restarting"}:
            transition_state = "waiting_restart"
            reason = lifecycle_reason or node_state or "draining"
            recovery_blocked = True
        return {
            "transition_state": transition_state,
            "reason": reason,
            "recovery_blocked": recovery_blocked,
            "update_state": state or None,
            "update_phase": phase or None,
            "node_state": node_state or None,
            "draining": draining,
        }

    async def request_member_hub_reconnect(self, *, force: bool = False) -> dict[str, Any]:
        conf = load_config(ctx=self.ctx)
        transition = self._member_hub_transition_snapshot()
        if str(getattr(conf, "role", "") or "").strip().lower() != "member":
            return {
                "ok": False,
                "accepted": False,
                "error": "role_not_member",
                "role": str(getattr(conf, "role", "") or "").strip().lower() or None,
                "transition": transition,
            }
        hub_url = str(getattr(conf, "hub_url", "") or "").strip()
        if not hub_url:
            return {
                "ok": False,
                "accepted": False,
                "error": "hub_url_missing",
                "transition": transition,
            }
        member_hub_token = str(load_member_hub_token() or getattr(conf, "token", "") or "").strip()
        if not member_hub_token:
            return {
                "ok": False,
                "accepted": False,
                "error": "member_hub_token_missing",
                "transition": transition,
            }
        if transition.get("recovery_blocked") and not force:
            return {
                "ok": True,
                "accepted": False,
                "transition": transition,
                "reason": "transition_in_progress",
            }

        from adaos.services.subnet.link_client import get_member_link_client

        existing = self._find_live_boot_task("adaos-heartbeat")
        if existing is not None:
            existing.cancel()
            try:
                await existing
            except asyncio.CancelledError:
                pass
            except BaseException:
                pass
        try:
            await get_member_link_client().stop()
        except Exception:
            pass

        heartbeat_task = await self._member_register_and_heartbeat(conf)
        if heartbeat_task is not None:
            self._boot_tasks.append(heartbeat_task)
        try:
            await get_member_link_client().start()
        except Exception as exc:
            return {
                "ok": False,
                "accepted": False,
                "error": f"{type(exc).__name__}: {exc}",
                "transition": transition,
            }
        return {
            "ok": True,
            "accepted": True,
            "transition": transition,
            "role": "member",
            "hub_url": hub_url,
            "started": {
                "heartbeat": heartbeat_task is not None,
                "link_client": True,
            },
        }

    async def request_hub_root_route_reset(
        self,
        *,
        reason: str,
        notify_browser: bool = True,
    ) -> dict[str, Any]:
        return await self._reset_hub_root_route_runtime(
            reason=str(reason or "").strip() or "route_reset",
            notify_browser=bool(notify_browser),
        )

    def _prepare_environment(self) -> None:
        """
        Гарантированная подготовка окружения:
          - создаёт каталоги (skills, scenarios, state, cache, logs)
          - инициализирует схему БД (skills/scenarios)
          - при наличии URL монорепо — клонирует репозитории без установки
        """
        ctx = self.ctx

        # каталоги (учитываем, что в paths могут быть callables)
        def _resolve(x):
            return x() if callable(x) else x

        skills_root = Path(_resolve(getattr(ctx.paths, "skills_dir", "")))
        scenarios_root = Path(_resolve(getattr(ctx.paths, "scenarios_dir", "")))
        state_root = Path(_resolve(getattr(ctx.paths, "state_dir", "")))
        cache_root = Path(_resolve(getattr(ctx.paths, "cache_dir", "")))
        logs_root = Path(_resolve(getattr(ctx.paths, "logs_dir", "")))

        for p in (skills_root, scenarios_root, state_root, cache_root, logs_root):
            if p:
                p.mkdir(parents=True, exist_ok=True)

        # схема БД (единая функция, не через побочный эффект конкретного реестра)
        ensure_schema(ctx.sql)

        # в тестах — не трогаем удалённые репозитории/сеть
        if os.getenv("ADAOS_TESTING") == "1":
            return

        # Default routing rules for RouterService (stdout + telegram broadcast).
        # This file is a runtime config (often ignored by git) but must exist for
        # system notifications (subnet.started/stopped, greet_on_boot, etc).
        try:
            base_dir = getattr(ctx.paths, "base_dir", None)
            base_dir = base_dir() if callable(base_dir) else base_dir
            if base_dir:
                rules_path = Path(base_dir) / "route_rules.yaml"
                if not rules_path.exists():
                    rules_path.write_text(
                        "rules:\n"
                        "  - priority: 60\n"
                        "    match: {}\n"
                        "    target: {node_id: this, kind: io_type, io_type: stdout}\n"
                        "  - priority: 50\n"
                        "    match: {}\n"
                        "    target: {node_id: this, kind: io_type, io_type: telegram}\n",
                        encoding="utf-8",
                    )
        except Exception:
            pass

        # монорепо навыков
        try:
            if ctx.settings.skills_monorepo_url and not (skills_root / ".git").exists():
                GitSkillRepository(
                    paths=ctx.paths,
                    git=ctx.git,
                    monorepo_url=getattr(ctx.settings, "skills_monorepo_url", None),
                    monorepo_branch=getattr(ctx.settings, "skills_monorepo_branch", None),
                ).ensure()
        except Exception:
            # не блокируем бут при сбое ensure; логирование можно добавить позже
            pass

        # монорепо сценариев (поддержим оба возможных конструктора)
        try:
            if ctx.settings.scenarios_monorepo_url and not (scenarios_root / ".git").exists():
                GitScenarioRepository(
                    paths=ctx.paths,
                    git=ctx.git,
                    url=getattr(ctx.settings, "scenarios_monorepo_url", None),
                    branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
                ).ensure()

        except Exception:
            pass

    async def _member_register_and_heartbeat(self, conf: NodeConfig) -> Optional[asyncio.Task]:
        hub_url = str(conf.hub_url or "").strip()
        if not hub_url:
            await bus.emit("net.subnet.register.error", {"status": "hub_url_missing"}, source="lifecycle", actor="system")
            return None
        member_hub_token = str(load_member_hub_token() or conf.token or "").strip()
        try:
            ok = await self.heartbeat.register(
                hub_url,
                member_hub_token,
                node_id=conf.node_id,
                subnet_id=conf.subnet_id,
                hostname=socket.gethostname(),
                roles=["member"],
            )
        except Exception as exc:
            await bus.emit("net.subnet.register.error", {"error": str(exc)}, source="lifecycle", actor="system")
            return None
        if not ok:
            await bus.emit("net.subnet.register.error", {"status": "non-200"}, source="lifecycle", actor="system")
            return None
        await bus.emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

        async def loop() -> None:
            backoff = 1
            while True:
                try:
                    ok_hb = await self.heartbeat.heartbeat(
                        hub_url,
                        member_hub_token,
                        node_id=conf.node_id,
                        base_url=str(os.getenv("ADAOS_SELF_BASE_URL") or "").strip() or None,
                    )
                    if ok_hb:
                        backoff = 1
                    else:
                        await bus.emit("net.subnet.heartbeat.warn", {"status": "non-200"}, source="lifecycle", actor="system")
                        backoff = min(backoff * 2, 30)
                except Exception as e:
                    await bus.emit("net.subnet.heartbeat.error", {"error": str(e)}, source="lifecycle", actor="system")
                    backoff = min(backoff * 2, 30)
                await asyncio.sleep(backoff if backoff > 1 else 5)

        return asyncio.create_task(loop(), name="adaos-heartbeat")

    async def run_boot_sequence(self, app: Any) -> None:
        while True:
            async with self._boot_lock:
                if self._booted:
                    return
                if not self._boot_in_progress:
                    self._boot_in_progress = True
                    self._boot_done.clear()
                    break
                boot_done = self._boot_done
            await boot_done.wait()
        try:
            await self._run_boot_sequence_impl(app)
        finally:
            async with self._boot_lock:
                self._boot_in_progress = False
                self._boot_done.set()

    async def _run_boot_sequence_impl(self, app: Any) -> None:
        if self._booted:
            return
        self._app = app
        # Unified deep-trace switch for WS/NATS/route debugging.
        try:
            if os.getenv("HUB_TRACE", "0") == "1":
                for k in (
                    "HUB_NATS_TRACE",
                    "HUB_NATS_VERBOSE",
                    "HUB_NATS_WS_TRACE",
                    "HUB_NATS_WIRETAP",
                    "HUB_NATS_WS_PATCH_AIOHTTP",
                    "HUB_ROUTE_TRACE",
                    "HUB_ROUTE_FRAME_VERBOSE",
                    "HUB_ROUTE_TX_VERBOSE",
                    "HUB_ROUTE_DIAG",
                    "HUB_WS_TRACE",
                    "HUB_ROOT_LOG_SNAPSHOT",
                    "HUB_ROOT_LOG_SNAPSHOT_EXTRACT_PRINT",
                ):
                    os.environ.setdefault(k, "1")
                os.environ.setdefault("HUB_NATS_WIRETAP_MAX_BYTES", "200")
                os.environ.setdefault("HUB_ROOT_LOG_SNAPSHOT_LINES", "2000")
                os.environ.setdefault("ADAOS_LOOP_LAG_MONITOR", "1")
                try:
                    print("[hub-io] HUB_TRACE=1 -> enabling deep WS/NATS/route tracing")
                except Exception:
                    pass
        except Exception:
            pass
        conf = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
        _control_lifecycle_await_watch_enabled = _env_truthy(
            os.getenv("ADAOS_CONTROL_LIFECYCLE_AWAIT_WATCH"),
            default=_env_truthy(os.getenv("HUB_TRACE"), default=False),
        )

        async def _report_control_lifecycle(trigger: str) -> None:
            try:
                if getattr(conf, "role", None) != "hub":
                    return
                done_box: dict[str, Any] = {"thread_done_at": None, "dumped": False}
                main_tid = threading.get_ident()

                def _is_idle_event_loop_stack(stack_text: str) -> bool:
                    try:
                        st = stack_text.replace("\\", "/")
                        return (
                            "asyncio/base_events.py" in st
                            and "in _run_once" in st
                            and (
                                ("selectors.py" in st and "select.select(" in st)
                                or ("asyncio/windows_events.py" in st and "_overlapped.GetQueuedCompletionStatus" in st)
                            )
                        )
                    except Exception:
                        return False

                def _run_report() -> Any:
                    try:
                        return report_hub_control_lifecycle_state(conf)
                    finally:
                        done_box["thread_done_at"] = time.monotonic()

                def _watch_resume() -> None:
                    while True:
                        time.sleep(0.25)
                        finished_at = done_box.get("thread_done_at")
                        if finished_at is None:
                            continue
                        if done_box.get("dumped"):
                            return
                        lag_s = time.monotonic() - float(finished_at)
                        if lag_s < 1.0:
                            continue
                        done_box["dumped"] = True
                        try:
                            fr = sys._current_frames().get(main_tid)  # type: ignore[attr-defined]
                            if fr is None:
                                self._log.warning(
                                    "control lifecycle await resume delayed lag_s=%.3f main_frame=missing trigger=%s",
                                    lag_s,
                                    trigger,
                                )
                                return
                            st = "".join(traceback.format_stack(fr, limit=40))
                            if _is_idle_event_loop_stack(st):
                                self._log.debug(
                                    "control lifecycle await resume delayed but main loop idle lag_s=%.3f trigger=%s",
                                    lag_s,
                                    trigger,
                                )
                                return
                            self._log.warning(
                                "control lifecycle await resume delayed lag_s=%.3f trigger=%s stack=\n%s",
                                lag_s,
                                trigger,
                                st.rstrip(),
                            )
                        except Exception:
                            return

                # The await-resume watcher is diagnostic-only. Starting a fresh
                # thread from the event loop on every control heartbeat can add
                # avoidable Windows jitter, so keep it opt-in outside HUB_TRACE.
                if _control_lifecycle_await_watch_enabled:
                    watcher = threading.Thread(
                        target=_watch_resume,
                        name="adaos-control-lifecycle-await-watch",
                        daemon=True,
                    )
                    watcher.start()
                await asyncio.to_thread(_run_report)
            except Exception as exc:
                # This is best-effort telemetry to Root; never break hub boot/loop on failures.
                # Include the error string in the structured log so JSON log collectors still show it.
                self._log.debug(
                    "control lifecycle report failed trigger=%s error=%s",
                    trigger,
                    str(exc),
                    exc_info=True,
                )

        _control_lifecycle_heartbeat_s = _bounded_interval_seconds(
            os.getenv("HUB_CONTROL_LIFECYCLE_HEARTBEAT_S", "15") or "15",
            default=15.0,
            minimum=5.0,
        )

        async def _control_lifecycle_heartbeat() -> None:
            if getattr(conf, "role", None) != "hub":
                return
            while True:
                await asyncio.sleep(_control_lifecycle_heartbeat_s)
                await _report_control_lifecycle("heartbeat")

        try:
            from adaos.services.system_model.service import (
                current_node_status_push_payload as _current_node_status_push_payload,
                node_status_push_heartbeat_s as _node_status_push_heartbeat_s,
            )
        except Exception:
            _current_node_status_push_payload = None
            _node_status_push_heartbeat_s = None

        _last_node_status_emit_at = 0.0
        _last_node_status_fingerprint: tuple[Any, ...] | None = None
        _suppressed_duplicate_node_status_total = 0

        async def _emit_node_status(trigger: str) -> None:
            nonlocal _last_node_status_emit_at, _last_node_status_fingerprint, _suppressed_duplicate_node_status_total
            try:
                if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
                    return
                if not callable(_current_node_status_push_payload):
                    return
                payload = _current_node_status_push_payload()
                payload["trigger"] = str(trigger or "").strip() or "runtime"
                now = time.time()
                should_emit, fingerprint = _should_emit_node_status(
                    payload=payload,
                    now=now,
                    last_emitted_at=_last_node_status_emit_at,
                    last_fingerprint=_last_node_status_fingerprint,
                )
                if not should_emit:
                    _suppressed_duplicate_node_status_total += 1
                    if _suppressed_duplicate_node_status_total in {1, 10} or (
                        _suppressed_duplicate_node_status_total % 100 == 0
                    ):
                        self._log.warning(
                            "suppressed duplicate node.status trigger=%s total=%s",
                            payload["trigger"],
                            _suppressed_duplicate_node_status_total,
                        )
                    return
                _last_node_status_emit_at = now
                _last_node_status_fingerprint = fingerprint
                await bus.emit(
                    "node.status",
                    payload,
                    source="lifecycle",
                    actor="system",
                )
            except Exception:
                self._log.debug("failed to emit node.status trigger=%s", trigger, exc_info=True)

        _node_status_push_heartbeat_interval_s = _bounded_interval_seconds(
            _node_status_push_heartbeat_s() if callable(_node_status_push_heartbeat_s) else 5.0,
            default=5.0,
            minimum=2.0,
        )

        async def _node_status_push_heartbeat() -> None:
            if getattr(conf, "role", None) != "hub":
                return
            while True:
                await asyncio.sleep(_node_status_push_heartbeat_interval_s)
                await _emit_node_status("heartbeat")

        self._prepare_environment()
        # local adapter over LocalEventBus
        core_bus = self.ctx.bus if isinstance(self.ctx.bus, LocalEventBus) else LocalEventBus()
        io_bus: Any = LocalIoBus(core=core_bus)
        await io_bus.connect()
        print("[bootstrap] IO bus: LocalEventBus")
        self._io_bus = io_bus
        # Attach chat IO -> NLU bridge (e.g. Telegram text -> nlp.intent.detect.request)
        try:
            register_chat_nlu_bridge(core_bus)
        except Exception:
            self._log.warning("failed to register chat_io NLU bridge", exc_info=True)
        # expose in app.state
        try:
            setattr(app.state, "bus", io_bus)
        except Exception:
            pass
        await bus.emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
        await self.skills_loader.import_all_handlers(self.ctx.paths.skills_dir())
        # Start service-type skills (external processes).
        if _runtime_candidate_mode():
            self._log.info("skipping service skill startup for candidate runtime prewarm")
        else:
            try:
                await get_service_supervisor().start_all()
            except Exception:
                self._log.warning("failed to start service skills", exc_info=True)
        await register_subscriptions()
        if str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            try:
                from adaos.services.subnet.link_manager import get_hub_link_manager as _get_hub_link_manager

                def _forward_core_update_status_to_members(ev: Event) -> None:
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type="core.update.status",
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror core.update.status to members", exc_info=True)

                def _forward_supervisor_update_status_raw_to_members(ev: Event) -> None:
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type="supervisor.update.status.raw",
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror supervisor.update.status.raw to members", exc_info=True)

                def _forward_node_status_to_members(ev: Event) -> None:
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    if not _should_forward_node_status_to_members(payload):
                        return
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type="node.status",
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror node.status to members", exc_info=True)

                def _forward_desktop_reload_to_members(ev: Event) -> None:
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type=str(ev.type or "desktop.webspace.reload"),
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror desktop reload event=%s to members", str(ev.type or ""), exc_info=True)

                def _forward_webio_stream_control_to_members(ev: Event) -> None:
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type=str(ev.type or ""),
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror webio stream control event=%s to members", str(ev.type or ""), exc_info=True)

                def _forward_targeted_event_to_members(ev: Event) -> None:
                    event_type = str(ev.type or "").strip()
                    if not event_type or event_type.startswith("desktop."):
                        return
                    if event_type in {"webio.stream.snapshot.requested", "webio.stream.subscription.changed"}:
                        return
                    payload = ev.payload if isinstance(ev.payload, dict) else {}
                    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
                    if bool(meta.get("subnet_origin_node_id")) or bool(meta.get("subnet_hub_mirrored")):
                        return
                    target_node_id = str(
                        payload.get("target_node_id")
                        or payload.get("node_target_id")
                        or meta.get("target_node_id")
                        or meta.get("node_target_id")
                        or ""
                    ).strip()
                    if not target_node_id or target_node_id == str(getattr(conf, "node_id", "") or "").strip():
                        return
                    try:
                        asyncio.get_running_loop().create_task(
                            _get_hub_link_manager().broadcast_event(
                                event_type=event_type,
                                payload=payload,
                                source=str(ev.source or "hub"),
                            )
                        )
                    except Exception:
                        self._log.debug("failed to mirror node-targeted event=%s to members", event_type, exc_info=True)

                core_bus.subscribe("core.update.status", _forward_core_update_status_to_members)
                core_bus.subscribe("supervisor.update.status.raw", _forward_supervisor_update_status_raw_to_members)
                core_bus.subscribe("node.status", _forward_node_status_to_members)
                core_bus.subscribe("desktop.webspace.reload", _forward_desktop_reload_to_members)
                core_bus.subscribe("desktop.webspace.reloaded", _forward_desktop_reload_to_members)
                core_bus.subscribe("desktop.webspace.reset", _forward_desktop_reload_to_members)
                core_bus.subscribe("webio.stream.snapshot.requested", _forward_webio_stream_control_to_members)
                core_bus.subscribe("webio.stream.subscription.changed", _forward_webio_stream_control_to_members)
                core_bus.subscribe("*", _forward_targeted_event_to_members)
            except Exception:
                self._log.debug(
                    "failed to install member status forwarders",
                    exc_info=True,
                )
        try:
            from adaos.services.core_update import (
                finalize_runtime_boot_status as _finalize_runtime_boot_status,
                read_public_update_status as _read_public_update_status,
                read_status as _read_core_update_status,
            )

            await bus.emit(
                "core.update.status",
                _read_core_update_status(),
                source="lifecycle",
                actor="system",
            )
            await bus.emit(
                "supervisor.update.status.raw",
                _read_public_update_status(),
                source="lifecycle",
                actor="system",
            )
        except Exception:
            _finalize_runtime_boot_status = None
            self._log.debug("failed to emit initial core.update.status", exc_info=True)
        await _emit_node_status("boot")
        await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")
        # Start in-process scheduler after the bus is ready.
        try:
            await start_scheduler()
        except Exception:
            self._log.warning("failed to start scheduler", exc_info=True)

        # Optional: monitor asyncio event loop lag to catch blocking handlers (which can manifest as
        # WebSocket stalls/timeouts and cascading disconnects).
        try:
            if os.getenv("ADAOS_LOOP_LAG_MONITOR", "0") == "1":
                try:
                    interval_s = float(os.getenv("ADAOS_LOOP_LAG_INTERVAL_S", "0.5") or "0.5")
                except Exception:
                    interval_s = 0.5
                if interval_s < 0.05:
                    interval_s = 0.05
                try:
                    # Keep normal runs readable: sub-second drift is useful for
                    # targeted diagnostics, but too noisy under browser attach.
                    warn_ms = float(os.getenv("ADAOS_LOOP_LAG_WARN_MS", "1000") or "1000")
                except Exception:
                    warn_ms = 1000.0
                try:
                    dump_ms = float(os.getenv("ADAOS_LOOP_LAG_DUMP_MS", "2000") or "2000")
                except Exception:
                    dump_ms = 2000.0
                try:
                    dump_top = int(os.getenv("ADAOS_LOOP_LAG_DUMP_TOP", "10") or "10")
                except Exception:
                    dump_top = 10
                if dump_top < 1:
                    dump_top = 1
                if dump_top > 50:
                    dump_top = 50

                async def _loop_lag_monitor() -> None:
                    # Measure *per-interval* overshoot (do not accumulate drift), so we can distinguish
                    # a single stall from a slow-but-steady loop.
                    last_tick = time.monotonic()
                    last_log = 0.0
                    last_dump = 0.0
                    while True:
                        await asyncio.sleep(interval_s)
                        now = time.monotonic()
                        drift_s = (now - last_tick) - interval_s
                        last_tick = now
                        if drift_s < 0:
                            drift_s = 0.0
                        drift_ms = drift_s * 1000.0
                        if drift_ms >= warn_ms:
                            try:
                                # Local rate-limit (do not depend on hub-io _rl_log).
                                if now - last_log >= 1.0:
                                    last_log = now
                                    msg = (
                                        f"[diag] event loop lag {drift_ms:.0f}ms (interval={interval_s:.2f}s warn={warn_ms:.0f}ms dump={dump_ms:.0f}ms)"
                                    )
                                    print(msg)
                                    diag_log.warning(
                                        "event loop lag drift_ms=%.0f interval_s=%.2f warn_ms=%.0f dump_ms=%.0f",
                                        drift_ms,
                                        interval_s,
                                        warn_ms,
                                        dump_ms,
                                    )
                            except Exception:
                                pass
                        if drift_ms >= dump_ms and (now - last_dump) >= max(5.0, interval_s):
                            last_dump = now
                            try:
                                tasks = list(asyncio.all_tasks())
                                # Keep deterministic ordering for repeated dumps.
                                tasks.sort(key=lambda t: (0 if t is asyncio.current_task() else 1, t.get_name()))
                                lines: list[str] = []
                                for t in tasks[:dump_top]:
                                    try:
                                        frames = t.get_stack(limit=1)
                                        top = frames[-1] if frames else None
                                        loc = None
                                        if top is not None:
                                            try:
                                                loc = f"{top.f_code.co_filename}:{top.f_lineno}"
                                            except Exception:
                                                loc = None
                                        lines.append(f"- task={t.get_name()} done={t.done()} cancelled={t.cancelled()} at={loc}")
                                    except Exception:
                                        continue
                                if lines:
                                    dump = "\n".join(lines)
                                    print("[diag] loop lag dump:\n" + dump)
                                    diag_log.warning("event loop lag dump\n%s", dump)
                            except Exception:
                                pass

                self._start_boot_task_once("adaos-loop-lag-monitor", _loop_lag_monitor)
        except Exception:
            pass

        # Optional: hang watchdog (thread-based) to capture the main thread stack during prolonged
        # event loop stalls. This catches cases where asyncio tasks show "await" positions only.
        try:
            hang_watchdog_default = "1" if os.getenv("ADAOS_LOOP_LAG_MONITOR", "0") == "1" else "0"
            if os.getenv("ADAOS_LOOP_HANG_WATCHDOG", hang_watchdog_default) == "1":
                try:
                    import threading as _threading
                    import sys as _sys
                    import traceback as _traceback
                except Exception:
                    _threading = None  # type: ignore[assignment]
                    _sys = None  # type: ignore[assignment]
                    _traceback = None  # type: ignore[assignment]
                if _threading and _sys and _traceback:
                    try:
                        hang_ms = float(
                            os.getenv("ADAOS_LOOP_HANG_MS")
                            or os.getenv("ADAOS_LOOP_LAG_DUMP_MS")
                            or "3000"
                        )
                    except Exception:
                        hang_ms = 3000.0
                    try:
                        every_s = float(os.getenv("ADAOS_LOOP_HANG_EVERY_S", "10") or "10")
                    except Exception:
                        every_s = 10.0
                    try:
                        stack_limit = int(os.getenv("ADAOS_LOOP_HANG_STACK", "40") or "40")
                    except Exception:
                        stack_limit = 40
                    if stack_limit < 5:
                        stack_limit = 5
                    if stack_limit > 200:
                        stack_limit = 200
                    if hang_ms < 200:
                        hang_ms = 200.0
                    if every_s < 1:
                        every_s = 1.0

                    main_tid = _threading.get_ident()
                    last_tick_box = {"t": time.monotonic()}

                    async def _tick() -> None:
                        while True:
                            last_tick_box["t"] = time.monotonic()
                            await asyncio.sleep(0.2)

                    self._start_boot_task_once("adaos-loop-tick", _tick)

                    def _is_idle_event_loop_wait(stack_text: str) -> bool:
                        try:
                            st = stack_text.replace("\\", "/")
                            if "asyncio/base_events.py" not in st or "in _run_once" not in st:
                                return False
                            if "selectors.py" in st and "select.select(" in st:
                                return True
                            if "asyncio/windows_events.py" in st and "_overlapped.GetQueuedCompletionStatus" in st:
                                return True
                        except Exception:
                            return False
                        return False

                    def _watch() -> None:
                        last_dump = 0.0
                        while True:
                            time.sleep(0.25)
                            now = time.monotonic()
                            dt_ms = (now - float(last_tick_box.get("t", now))) * 1000.0
                            if dt_ms < hang_ms:
                                continue
                            if now - last_dump < every_s:
                                continue
                            last_dump = now
                            try:
                                fr = _sys._current_frames().get(main_tid)  # type: ignore[attr-defined]
                                if fr is None:
                                    print(f"[diag] event loop hang {dt_ms:.0f}ms (no frame)")
                                    diag_log.warning("event loop hang dt_ms=%.0f frame=none", dt_ms)
                                    continue
                                st = "".join(_traceback.format_stack(fr, limit=stack_limit))
                                if _is_idle_event_loop_wait(st):
                                    diag_log.debug("event loop hang suppressed idle wait dt_ms=%.0f", dt_ms)
                                    continue
                                print(f"[diag] event loop hang {dt_ms:.0f}ms stack:\n{st.rstrip()}")
                                diag_log.warning("event loop hang dt_ms=%.0f stack:\n%s", dt_ms, st.rstrip())
                            except Exception:
                                continue

                    t = _threading.Thread(target=_watch, name="adaos-loop-hang-watchdog", daemon=True)
                    t.start()
        except Exception:
            pass
        diag_log = logging.getLogger("adaos.diagnostics")
        startup_log = logging.getLogger("adaos.startup")
        startup_stage_logs_enabled = str(os.getenv("ADAOS_STARTUP_STAGE_LOGS") or "").strip().lower() in {"1", "true", "yes", "on"}

        def _startup_stage_mark(stage: str, *, started: float | None = None, failed: Exception | None = None) -> float:
            now = time.perf_counter()
            if started is None:
                if startup_stage_logs_enabled:
                    startup_log.info("startup stage start stage=%s", stage)
                return now
            duration = now - started
            if failed is None:
                if startup_stage_logs_enabled:
                    startup_log.info("startup stage done stage=%s duration_s=%.3f", stage, duration)
            else:
                startup_log.warning(
                    "startup stage failed stage=%s duration_s=%.3f error=%s",
                    stage,
                    duration,
                    type(failed).__name__,
                )
            return now

        try:
            from adaos.services.agent_context import get_ctx as _get_ctx
            from adaos.services.workspace_sync import reconcile_workspace_db_to_materialized as _reconcile_workspace_db_to_materialized

            _reconcile_started = _startup_stage_mark("bootstrap_reconcile_workspace_registry")
            _reconcile_workspace_db_to_materialized(_get_ctx())
            _startup_stage_mark("bootstrap_reconcile_workspace_registry", started=_reconcile_started)
        except Exception:
            self._log.debug("failed to reconcile workspace sqlite registry on boot", exc_info=True)
        if conf.role == "hub":
            _hub_ready_started = _startup_stage_mark("bootstrap_emit_net_subnet_hub_ready")
            await bus.emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
            _startup_stage_mark("bootstrap_emit_net_subnet_hub_ready", started=_hub_ready_started)

            async def lease_monitor() -> None:
                while True:
                    for info in self.subnet_registry.mark_down_if_expired():
                        await bus.emit("net.subnet.node.down", {"node_id": getattr(info, "node_id", None)}, source="lifecycle", actor="system")
                    await asyncio.sleep(5)

            self._start_boot_task_once("adaos-lease-monitor", lease_monitor)
            self._ready.set()
            self._booted = True
            _sys_ready_started = _startup_stage_mark("bootstrap_emit_sys_ready")
            await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
            _startup_stage_mark("bootstrap_emit_sys_ready", started=_sys_ready_started)
            _node_status_started = _startup_stage_mark("bootstrap_emit_node_status")
            await _emit_node_status("sys.ready")
            _startup_stage_mark("bootstrap_emit_node_status", started=_node_status_started)
            try:
                if callable(_finalize_runtime_boot_status):
                    _finalize_runtime_boot_status()
            except Exception:
                self._log.debug("failed to finalize core.update.status after sys.ready", exc_info=True)
            _control_started = _startup_stage_mark("bootstrap_report_control_lifecycle")
            await _report_control_lifecycle("sys.ready")
            _startup_stage_mark("bootstrap_report_control_lifecycle", started=_control_started)
            self._start_boot_task_once("adaos-control-lifecycle-heartbeat", _control_lifecycle_heartbeat)
            self._start_boot_task_once("adaos-node-status-push-heartbeat", _node_status_push_heartbeat)
        else:
            task = await self._member_register_and_heartbeat(conf)
            if task:
                self._boot_tasks.append(task)
                self._ready.set()
                self._booted = True
                _sys_ready_started = _startup_stage_mark("bootstrap_emit_sys_ready")
                await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
                _startup_stage_mark("bootstrap_emit_sys_ready", started=_sys_ready_started)
                _node_status_started = _startup_stage_mark("bootstrap_emit_node_status")
                await _emit_node_status("sys.ready")
                _startup_stage_mark("bootstrap_emit_node_status", started=_node_status_started)
                try:
                    if callable(_finalize_runtime_boot_status):
                        _finalize_runtime_boot_status()
                except Exception:
                    self._log.debug("failed to finalize core.update.status after sys.ready", exc_info=True)

        # After IO bus is ready, wire outbound subscriber for Telegram if NATS/local
        _post_ready_started = _startup_stage_mark("bootstrap_post_ready_tail")
        try:
            if hasattr(self._io_bus, "subscribe_output"):
                _subscribe_output_started = _startup_stage_mark("bootstrap_subscribe_output")

                # Subscribe to all bot ids ("tg.output.*") and use the single configured TG_BOT_TOKEN.
                sender = TelegramSender("any-bot")

                async def _handler(subject: str, data: bytes) -> None:
                    try:
                        payload = _json.loads(data.decode("utf-8"))
                        # payload may already match ChatOutputEvent schema
                        messages = [ChatOutputMessage(**m) for m in payload.get("messages", [])]
                        out = ChatOutputEvent(target=payload.get("target", {}), messages=messages, options=payload.get("options"))
                        await sender.send(out)
                        for m in messages:
                            tm.record_event("outbound_total", {"type": m.type})
                    except Exception as e:
                        # On error, emit DLQ if possible
                        try:
                            dlq_env = {"error": str(e), "subject": subject, "data": payload if "payload" in locals() else None}
                            if hasattr(self._io_bus, "publish_dlq"):
                                await self._io_bus.publish_dlq("output", dlq_env)
                        except Exception:
                            pass

                await self._io_bus.subscribe_output("*", _handler)
                _startup_stage_mark("bootstrap_subscribe_output", started=_subscribe_output_started)
        except Exception:
            pass

        # Inbound bridge from root NATS -> local event bus (tg.input.<hub_id>)
        try:
            _bridge_setup_started = _startup_stage_mark("bootstrap_inbound_bridge_setup")
            # Hot-reload friendly: read persisted runtime NATS config on every connect attempt.
            hub_id = load_config(ctx=self.ctx).subnet_id
            if hub_id:
                try:
                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                        print(f"[hub-io] nats init: hub_id={hub_id}")
                except Exception:
                    pass

                # Track connectivity state to log/emit only on transitions
                reported_down = False
                nats_last_log_at: dict[str, float] = {}
                nats_last_ok_at: float | None = None
                # Track flaky NATS WS endpoints and temporarily avoid them after short transient drops.
                nats_server_quarantine_until: dict[str, float] = {}
                nats_last_server: str | None = None
                nats_attempt_server: str | None = None

                def _rl_log(key: str, msg: str, *, every_s: float = 5.0) -> None:
                    """
                    Rate-limited console log helper for noisy NATS diagnostics.
                    Uses monotonic time to avoid being affected by clock changes.
                    """
                    try:
                        if not _hub_channel_console_allow_rl(key, msg):
                            return
                        now = time.monotonic()
                        last = nats_last_log_at.get(key, 0.0)
                        if now - last < every_s:
                            return
                        nats_last_log_at[key] = now
                        print(msg)
                    except Exception:
                        return

                def _read_node_nats() -> tuple[str | None, str | None, str | None]:
                    try:
                        node_nats = load_nats_runtime_config()
                        if not node_nats:
                            node_nats = migrate_legacy_nats_runtime_config()
                        if not isinstance(node_nats, dict) or not node_nats:
                            return None, None, None
                        # Allow explicit override for experiments (e.g. switching from WS to TCP).
                        override = str(os.getenv("HUB_NATS_URL_OVERRIDE", "") or "").strip() or None
                        raw_nurl = str(node_nats.get("ws_url") or "").strip() or None
                        if override:
                            raw_nurl = override
                        requested_transport = str(os.getenv("HUB_NATS_TRANSPORT", "") or "").strip().lower()
                        if requested_transport in {"ws", "websocket", "websockets"} and raw_nurl and not nats_url_uses_websocket(raw_nurl):
                            ws_candidates = _hub_public_ws_candidates(raw_nurl)
                            raw_nurl = next(
                                (str(item).strip() for item in ws_candidates if nats_url_uses_websocket(item)),
                                public_nats_ws_api(),
                            )
                        nurl = _normalize_hub_nats_ws_url(raw_nurl)
                        nuser = str(node_nats.get("user") or "") or None
                        npass = str(node_nats.get("pass") or "") or None
                        if nurl and raw_nurl and nurl != raw_nurl:
                            save_nats_runtime_config(ws_url=nurl, user=nuser, password=npass)
                        elif (
                            requested_transport in {"ws", "websocket", "websockets"}
                            and nurl
                            and raw_nurl
                            and nurl != str(node_nats.get("ws_url") or "").strip()
                        ):
                            save_nats_runtime_config(ws_url=nurl, user=nuser, password=npass)
                        return nurl, nuser, npass
                    except Exception:
                        return None, None, None

                last_token_fetch = 0.0

                async def _fetch_nats_credentials() -> bool:
                    nonlocal hub_id
                    nonlocal last_token_fetch
                    # rate-limit attempts to avoid spamming root
                    now = time.monotonic()
                    if now - last_token_fetch < 30.0:
                        return False
                    last_token_fetch = now
                    debug = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                    try:
                        from adaos.services.root.client import RootHttpClient
                        from adaos.services.node_config import load_config
                        from adaos.services.node_config import _expand_path as _expand_path
                    except Exception:
                        return False

                    try:
                        cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                    except Exception:
                        cfg = None

                    # Prefer zonal backend for hub-root runtime credentials.
                    # `root.base_url` remains the central control-plane URL, but the
                    # hub NATS session for stage 1 must be issued by the selected zone.
                    zone_id = str(
                        os.getenv("ADAOS_ZONE_ID")
                        or getattr(cfg, "zone_id", None)
                        or ""
                    ).strip().lower()
                    if zone_id:
                        base_url = zone_public_base_url(zone_id)
                    else:
                        # Fallback for legacy configs without explicit zone.
                        base_url = (
                            getattr(self.ctx.settings, "api_base", None)
                            or getattr(getattr(cfg, "root_settings", None), "base_url", None)
                            or "https://api.inimatic.com"
                        )
                    try:
                        ca = _expand_path(getattr(getattr(cfg, "root_settings", None), "ca_cert", None), "keys/ca.cert")
                        cert = _expand_path(getattr(getattr(getattr(cfg, "subnet_settings", None), "hub", None), "cert", None), "keys/hub_cert.pem")
                        key = _expand_path(getattr(getattr(getattr(cfg, "subnet_settings", None), "hub", None), "key", None), "keys/hub_private.pem")
                    except Exception:
                        ca = None
                        cert = None
                        key = None

                    verify: Any = True
                    # By default keep system CA verification (important for public HTTPS like api.inimatic.com).
                    # If you need to pin CA explicitly, set ADAOS_ROOT_VERIFY_CA=1.
                    if os.getenv("ADAOS_ROOT_VERIFY_CA", "0") == "1" and ca is not None:
                        try:
                            if ca.exists():
                                verify = str(ca)
                        except Exception:
                            pass
                    cert_tuple = None
                    if cert is not None and key is not None:
                        try:
                            if cert.exists() and key.exists():
                                cert_tuple = (str(cert), str(key))
                        except Exception:
                            cert_tuple = None

                    client = RootHttpClient(base_url=str(base_url), verify=verify, cert=cert_tuple)
                    if not client.cert:
                        if debug:
                            try:
                                import logging as _logging

                                _logging.getLogger("adaos.hub_io").warning(
                                    "nats.mtls_missing",
                                    extra={
                                        "extra": {
                                            "base_url": str(base_url),
                                            "verify": str(verify),
                                            "ca_path": str(ca) if ca is not None else None,
                                            "cert_path": str(cert) if cert is not None else None,
                                            "key_path": str(key) if key is not None else None,
                                            "have_ca": bool(ca and ca.exists()),
                                            "have_cert": bool(cert and cert.exists()),
                                            "have_key": bool(key and key.exists()),
                                        }
                                    },
                                )
                            except Exception:
                                pass
                        return False

                    def _do_request() -> dict[str, Any] | None:
                        try:
                            identity = runtime_identity_snapshot()
                            data = client.request(
                                "POST",
                                "/v1/hub/nats/token",
                                json={
                                    "runtime_instance_id": str(identity.get("runtime_instance_id") or ""),
                                    "transition_role": str(identity.get("transition_role") or "active"),
                                    "active_slot": str(os.getenv("ADAOS_ACTIVE_CORE_SLOT") or ""),
                                    "runtime_host": str(os.getenv("ADAOS_RUNTIME_HOST") or ""),
                                    "runtime_port": str(os.getenv("ADAOS_RUNTIME_PORT") or ""),
                                },
                            )
                            return dict(data) if isinstance(data, dict) else None
                        except Exception as e:
                            if debug:
                                try:
                                    import logging as _logging

                                    _logging.getLogger("adaos.hub_io").warning(
                                        "nats.token_request_failed",
                                        extra={
                                            "extra": {
                                                "base_url": str(base_url),
                                                "verify": str(verify),
                                                "error": str(e),
                                                "error_type": type(e).__name__,
                                            }
                                        },
                                    )
                                except Exception:
                                    pass
                            return None

                    data = await asyncio.to_thread(_do_request)
                    if not isinstance(data, dict):
                        return False
                    token = data.get("hub_nats_token")
                    nats_user = data.get("nats_user")
                    response_hub_id = data.get("hub_id")
                    nats_ws_url = _normalize_hub_nats_ws_url(data.get("nats_ws_url"))
                    if not token or not nats_user or not nats_ws_url:
                        if debug:
                            try:
                                import logging as _logging

                                _logging.getLogger("adaos.hub_io").warning(
                                    "nats.token_response_incomplete",
                                    extra={"extra": {"data": data}},
                                )
                            except Exception:
                                pass
                        return False

                    try:
                        resolved_hub_id, resolved_nats_user = _canonical_hub_nats_identity(
                            local_hub_id=getattr(cfg, "subnet_id", None),
                            nats_user=str(nats_user),
                            response_hub_id=str(response_hub_id or "").strip() or None,
                        )
                        # Experimental switch: allow running hub-root over native NATS TCP.
                        # WARNING: public `nats://` is not encrypted. Use only for controlled testing
                        # unless you have TLS-enabled NATS endpoints.
                        transport = str(os.getenv("HUB_NATS_TRANSPORT", "") or "").strip().lower()
                        if transport in {"tcp", "nats"}:
                            selected_url = str(_hub_public_tcp_candidates(None)[0])
                        else:
                            selected_url = str(nats_ws_url)
                        save_nats_runtime_config(
                            ws_url=selected_url,
                            user=str(resolved_nats_user or nats_user),
                            password=str(token),
                        )
                        if resolved_hub_id:
                            hub_id = str(resolved_hub_id)
                        return True
                    except Exception:
                        return False

                # Correlate hub-side NATS WS sessions with root-side ws-nats-proxy logs + optionally snapshot root logs.
                ws_connect_tag: str | None = None
                established_ws_tag: str | None = None
                last_root_snapshot_at: float | None = None
                last_ws_transport: str | None = None

                async def _nats_bridge() -> None:
                    nonlocal hub_id
                    nonlocal reported_down
                    nonlocal nats_last_ok_at
                    nonlocal nats_attempt_server
                    nonlocal nats_last_server
                    nonlocal last_ws_transport
                    backoff = 1.0
                    trace = os.getenv("HUB_NATS_TRACE", "0") == "1"
                    runtime_identity = runtime_identity_snapshot()
                    runtime_role = str(runtime_identity.get("transition_role") or "active")
                    runtime_instance = str(runtime_identity.get("runtime_instance_id") or "")
                    candidate_passive_mode = _hub_root_candidate_passive_mode()
                    if trace or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                        try:
                            import asyncio as _asyncio

                            policy = _asyncio.get_event_loop_policy()
                            try:
                                loop = _asyncio.get_running_loop()
                            except RuntimeError:
                                loop = None
                                _rl_log(
                                    "loop.info",
                                    f"[hub-io] asyncio loop policy={type(policy).__name__} loop={type(loop).__name__ if loop else None} role={runtime_role} instance={runtime_instance}",
                                    every_s=3600.0,
                                )
                            if loop is not None and os.name == "nt" and "Selector" in type(loop).__name__:
                                _rl_log(
                                    "loop.warn",
                                    "[hub-io] Windows Selector event loop detected; NATS-over-WS may stall on PUB load. Prefer default Proactor loop and only set ADAOS_WIN_SELECTOR_LOOP=1 for targeted diagnostics.",
                                    every_s=3600.0,
                                )
                        except Exception:
                            pass
                    raw_keepalive_task: asyncio.Task | None = None
                    try:
                        realtime_enabled = realtime_sidecar_enabled(
                            role=str(getattr(self.ctx.config, "role", "") or "").strip().lower()
                        )
                    except Exception:
                        realtime_enabled = False
                    try:
                        realtime_remote_candidates = resolve_realtime_remote_candidates() if realtime_enabled else []
                    except Exception:
                        realtime_remote_candidates = []
                    # Best-effort outbox for telegram replies when NATS is flapping.
                    try:
                        if not hasattr(self, "_tg_output_pending"):
                            setattr(self, "_tg_output_pending", load_outbox_items("telegram"))
                        setattr(self, "_tg_output_persist_path", outbox_store_path("telegram"))
                    except Exception:
                        try:
                            setattr(self, "_tg_output_pending", deque())
                        except Exception:
                            pass
                    if realtime_enabled and realtime_remote_candidates:
                        last_ws_transport = "sidecar"
                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                            _rl_log(
                                "nats.ws_transport",
                                f"[hub-io] nats ws transport: sidecar (internal WS client disabled, local={realtime_sidecar_local_url()})",
                                every_s=3600.0,
                            )
                    else:
                        # NATS WS transport: use `websockets` (avoid aiohttp WS flaps under PUB load).
                        try:
                            from adaos.services.nats_ws_transport import install_nats_ws_transport_patch

                            ws_transport = install_nats_ws_transport_patch(verbose=False)
                            last_ws_transport = ws_transport
                            if (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace) and ws_transport:
                                _rl_log(
                                    "nats.ws_transport",
                                    f"[hub-io] nats ws transport: {ws_transport}",
                                    every_s=3600.0,
                                )
                        except Exception as _patch_e:
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                _rl_log(
                                    "nats.ws_transport_patch_err",
                                    f"[hub-io] nats ws transport patch error: {type(_patch_e).__name__}: {_patch_e}",
                                    every_s=5.0,
                                )

                    def _explain_connect_error(err: Exception) -> str:
                        try:
                            msg = str(err) or ""
                            low = msg.lower()
                            if isinstance(err, TypeError) and "argument of type 'int' is not iterable" in low:
                                return "root nats authentication error: WS closed after CONNECT; verify persisted runtime NATS credentials"
                        except Exception:
                            pass
                        # fallback – include class and message
                        try:
                            return f"{type(err).__name__}: {str(err)}"
                        except Exception:
                            return type(err).__name__

                    def _looks_like_auth_failure(err: Exception) -> bool:
                        """
                        Heuristic: when root-side NATS WS proxy closes after CONNECT because of invalid credentials,
                        nats-py can surface confusing exceptions (historically observed in this project).
                        Treat these as auth-ish failures and trigger credential refresh.
                        """
                        try:
                            msg = str(err) or ""
                            low = msg.lower()
                            if isinstance(err, TypeError) and "argument of type 'int' is not iterable" in low:
                                return True
                            if "authentication timeout" in low:
                                return True
                            if "authorization violation" in low:
                                return True
                            if "auth" in low:
                                return True
                            if type(err).__name__ == "UnexpectedEOF" or "unexpected eof" in low:
                                return True
                        except Exception:
                            return False
                        return False

                    while True:
                        try:
                            cfg_now = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                            current_hub_id = str(getattr(cfg_now, "subnet_id", "") or "").strip()
                            if current_hub_id:
                                hub_id = current_hub_id
                        except Exception:
                            pass
                        runtime_identity = runtime_identity_snapshot()
                        runtime_role = str(runtime_identity.get("transition_role") or "active")
                        runtime_instance = str(runtime_identity.get("runtime_instance_id") or "")
                        candidate_passive_mode = _hub_root_candidate_passive_mode()
                        try:
                            nats_attempt_server = None
                            nurl, nuser, npass = _read_node_nats()
                            requested_transport = str(os.getenv("HUB_NATS_TRANSPORT", "") or "").strip().lower()
                            if not nurl or not nuser or not npass:
                                fetched = await _fetch_nats_credentials()
                                if fetched:
                                    # re-read persisted runtime NATS state on next loop
                                    await asyncio.sleep(0.1)
                                    continue
                                # Wait for `adaos dev telegram` to provision credentials.
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                    print("[hub-io] NATS disabled: missing persisted runtime nats.ws_url/user/pass")
                                await asyncio.sleep(2.0)
                                continue
                            if requested_transport in {"ws", "websocket", "websockets"} and not nats_url_uses_websocket(nurl):
                                fetched = await _fetch_nats_credentials()
                                if fetched:
                                    await asyncio.sleep(0.1)
                                    continue
                            if nats_url_uses_websocket(nurl) or (
                                realtime_enabled and _nats_url_needs_public_ws_refresh(nurl)
                            ):
                                fetched = await _fetch_nats_credentials()
                                if fetched:
                                    await asyncio.sleep(0.1)
                                    continue

                            user = nuser
                            pw = npass
                            pw_mask = (pw[:3] + "***" + pw[-2:]) if pw and len(pw) > 6 else ("***" if pw else None)
                            # Build candidates without mixing WS and TCP schemes to avoid client errors.
                            candidates: List[str] = []

                            def _dedup_push(url: str) -> None:
                                if not url:
                                    return
                                s = str(url).strip()
                                if not s:
                                    return
                                # For NATS WS clients, it's safer to always have an explicit WS path.
                                # In our deployment NATS WS is mounted at `/nats` (not `/`).
                                if s.startswith("ws://") or s.startswith("wss://"):
                                    ws_default_path = os.getenv("NATS_WS_DEFAULT_PATH", "/nats") or "/nats"
                                    if not ws_default_path.startswith("/"):
                                        ws_default_path = "/" + ws_default_path
                                    try:
                                        from urllib.parse import urlparse, urlunparse

                                        pr0 = urlparse(s)
                                        # Keep an explicit "/" WS mount intact: some deployments terminate WS on "/".
                                        # Only inject the default mount when the path is missing entirely.
                                        if not pr0.path:
                                            pr0 = pr0._replace(path=ws_default_path)
                                            s = urlunparse(pr0)
                                    except Exception:
                                        if s.endswith("://") or s.endswith("://localhost") or s.endswith("://127.0.0.1"):
                                            s = s.rstrip("/") + ws_default_path
                                if s not in candidates:
                                    candidates.append(s)

                            base = (nurl or "").rstrip("/")

                            try:
                                from urllib.parse import urlparse, urlunparse

                                pr = urlparse(base) if base else None
                                scheme = (pr.scheme if pr else "").lower()
                                # If base is http(s), normalize to ws(s)
                                if scheme in ("http", "https"):
                                    base = "ws" + base[4:]
                                    pr = urlparse(base)
                                    scheme = pr.scheme.lower()
                                # Default to WS mode when uncertain or when base points to cluster alias
                                is_ws_mode = (not base) or scheme.startswith("ws")
                                if not is_ws_mode and scheme == "nats":
                                    host = (pr.hostname or "").lower()
                                    # Avoid using internal docker alias from host-based hub
                                    if host in ("nats", "localhost", "127.0.0.1"):
                                        is_ws_mode = True

                                if is_ws_mode:
                                    # Prefer WS endpoints only.
                                    # IMPORTANT: Keep this conservative — probing extra mounts/hosts has caused
                                    # "Authentication Timeout" hangs when we accidentally hit non-NATS WS endpoints.
                                    # The dedicated public hostname is opt-in only. In this environment it has
                                    # been closing long-lived hub WS sessions shortly after the first client ping.
                                    for item in _hub_public_ws_candidates(base):
                                        _dedup_push(item)
                                    # Allow explicit WS alternates via env (comma-separated)
                                    extra = os.getenv("NATS_WS_URL_ALT")
                                    if extra:
                                        for it in [x.strip() for x in extra.split(",") if x.strip()]:
                                            if it.startswith("ws"):
                                                _dedup_push(it)
                                else:
                                    # TCP mode: prefer nats:// endpoints.
                                    if base:
                                        _dedup_push(base)
                                    else:
                                        for item in _hub_public_tcp_candidates(base):
                                            _dedup_push(item)
                                    # Optional TCP alternates via env (comma-separated)
                                    extra = os.getenv("NATS_TCP_URL_ALT")
                                    if extra:
                                        for it in [x.strip() for x in extra.split(",") if x.strip()]:
                                            if it.startswith("nats://") or it.startswith("tls://"):
                                                _dedup_push(it)
                            except Exception:
                                # Fallback: if base present, use it only; otherwise default to the api ingress.
                                if base:
                                    _dedup_push(base)
                                else:
                                    _dedup_push(public_nats_ws_api())
                            try:
                                now_m = time.monotonic()
                                available = [s for s in candidates if now_m >= float(nats_server_quarantine_until.get(str(s), 0.0))]
                                if available:
                                    candidates = available
                            except Exception:
                                pass

                            # Prefer the api-domain ingress by default. The dedicated hostname remains opt-in via
                            # `HUB_NATS_PREFER_DEDICATED=1` for environments where it is known to be healthier.
                            try:
                                pref_ded = _hub_nats_prefer_dedicated()
                                if candidates and str(candidates[0]).startswith(("ws://", "wss://")):
                                    candidates = order_nats_ws_candidates(
                                        candidates,
                                        explicit_url=base,
                                        prefer_dedicated=pref_ded,
                                    )
                            except Exception:
                                pass
                            remote_candidates: list[str] = []
                            try:
                                if realtime_enabled:
                                    remote_candidates = resolve_realtime_remote_candidates()
                                    if remote_candidates:
                                        original_candidates = list(candidates)
                                        local_candidate = realtime_sidecar_local_url()
                                        local_ready = await probe_realtime_sidecar_ready(
                                            host=realtime_sidecar_host(),
                                            port=realtime_sidecar_port(),
                                            timeout_s=1.5,
                                        )
                                        fallback_candidates = _build_realtime_sidecar_fallback_candidates(
                                            original_candidates,
                                            local_candidate=local_candidate,
                                        )
                                        if local_ready:
                                            candidates = [local_candidate, *fallback_candidates]
                                            try:
                                                now_m = time.monotonic()
                                                available = [
                                                    s
                                                    for s in candidates
                                                    if now_m >= float(nats_server_quarantine_until.get(str(s), 0.0))
                                                ]
                                                if available:
                                                    candidates = available
                                            except Exception:
                                                pass
                                            _rl_log(
                                                "nats.sidecar_route",
                                                f"[hub-io] nats realtime sidecar local={local_candidate} remote={remote_candidates}"
                                                + (f" fallback={fallback_candidates}" if fallback_candidates else ""),
                                                every_s=60.0,
                                            )
                                        else:
                                            candidates = list(fallback_candidates)
                                            _rl_log(
                                                "nats.sidecar_unready",
                                                f"[hub-io] nats realtime sidecar not ready local={local_candidate}; "
                                                f"falling back to {fallback_candidates}",
                                                every_s=15.0,
                                            )
                            except Exception:
                                pass

                            try:
                                configure_hub_root_transport_strategy(
                                    requested_transport=str(os.getenv("HUB_NATS_TRANSPORT", "") or "").strip().lower() or None,
                                    selected_server=nats_last_server or nats_attempt_server or (candidates[0] if candidates else None),
                                    url_override=str(os.getenv("HUB_NATS_URL_OVERRIDE", "") or "").strip() or None,
                                    candidates=list(candidates),
                                    failover_policy={
                                        "sidecar_enabled": bool(realtime_enabled),
                                        "sidecar_remote_candidates": list(remote_candidates),
                                        "allow_tcp_fallback": _env_truthy(
                                            os.getenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK"),
                                            default=False,
                                        ),
                                        "ws_impl_auto_fallback": _env_truthy(
                                            os.getenv("HUB_NATS_WS_AUTO_FALLBACK"),
                                            default=False,
                                        ),
                                    },
                                    hypothesis={
                                        "selector_loop": bool(os.name == "nt" and os.getenv("ADAOS_WIN_SELECTOR_LOOP", "0") == "1"),
                                        "ws_impl": str(os.getenv("HUB_NATS_WS_IMPL", "") or "").strip() or None,
                                        "raw_keepalive": _env_truthy(os.getenv("HUB_NATS_RAW_KEEPALIVE"), default=False),
                                        "rx_timeout_s": str(os.getenv("HUB_NATS_RX_TIMEOUT_S", "") or "").strip() or None,
                                    },
                                )
                            except Exception:
                                pass

                            hub_nats_verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                            hub_nats_quiet = os.getenv("HUB_NATS_QUIET", "1") == "1"
                            if hub_nats_verbose or not hub_nats_quiet:
                                print(f"[hub-io] Connecting NATS candidates={candidates} user={user} pass={pw_mask}")

                            def _emit_down(kind: str, err: Exception | None) -> None:
                                nonlocal reported_down
                                if not reported_down:
                                    et = type(err).__name__ if err else kind
                                    log_server = _resolve_nats_log_server(
                                        current_attempt=nats_attempt_server,
                                        connected_server=nats_last_server,
                                    )
                                    try:
                                        record_hub_root_transport_event(
                                            "down" if kind in {"disconnected", "eof"} else kind,
                                            transport=_hub_root_transport_kind(log_server),
                                            server=log_server,
                                            summary=f"hub-root transport down ({kind})",
                                            error=str(err) if err else None,
                                            details={"kind": kind},
                                        )
                                    except Exception:
                                        pass
                                    # Produce a richer one-time diagnostics line to aid debugging WS/TLS/DNS issues
                                    if hub_nats_verbose or not hub_nats_quiet:
                                        try:
                                            if os.getenv("SILENCE_NATS_EOF", "0") == "1" and kind == "disconnected":
                                                # Suppress idle disconnect chatter in dev
                                                pass
                                            else:
                                                details = ""
                                                if err is not None:
                                                    msg = str(err) or repr(err)
                                                    # Extract handshake info if present
                                                    status = getattr(err, "status", None)
                                                    url = getattr(err, "url", None) or getattr(getattr(err, "request_info", None), "real_url", None)
                                                    if status:
                                                        details += f" status={status}"
                                                    if url:
                                                        details += f" url={url}"
                                                    # Include a short class:message tail
                                                    details = (details + f" msg={msg}").strip()
                                            if not (os.getenv("SILENCE_NATS_EOF", "0") == "1" and kind == "disconnected"):
                                                print(f"[hub-io] nats server unreachable ({et}){(': ' + details) if details else ''}")
                                        except Exception:
                                            pass
                                    try:
                                        self.ctx.bus.publish(
                                            Event(type="subnet.nats.down", payload={"kind": kind, "error": str(err) if err else None, "ts": time.time()}, source="io.nats")
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        mark_root_control_down(
                                            summary=f"hub-root control session down ({kind})",
                                            details={
                                                "kind": kind,
                                                "error": str(err) if err else None,
                                                "server": log_server,
                                            },
                                        )
                                        mark_route_degraded(
                                            summary="hub route relay degraded because root control is down",
                                            details={"cause": kind},
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        asyncio.create_task(
                                            self._reset_hub_root_route_runtime(
                                                reason=f"nats_{kind}",
                                                notify_browser=False,
                                            )
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        asyncio.create_task(_report_control_lifecycle(f"subnet.nats.down:{kind}"))
                                    except Exception:
                                        pass
                                    reported_down = True

                            def _emit_up() -> None:
                                nonlocal reported_down
                                if reported_down:
                                    log_server = _resolve_nats_log_server(
                                        current_attempt=nats_attempt_server,
                                        connected_server=nats_last_server,
                                    )
                                    if hub_nats_verbose or not hub_nats_quiet:
                                        try:
                                            print("[hub-io] nats connection restored")
                                        except Exception:
                                            pass
                                    try:
                                        self.ctx.bus.publish(Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats"))
                                    except Exception:
                                        pass
                                    try:
                                        record_hub_root_transport_event(
                                            "connected",
                                            transport=_hub_root_transport_kind(log_server),
                                            server=log_server,
                                            summary="hub-root control session established",
                                            details={"ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None},
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        mark_root_control_up(
                                            summary="hub-root control session established",
                                            details={
                                                "server": log_server,
                                                "ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                            },
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        asyncio.create_task(
                                            self._reset_hub_root_route_runtime(
                                                reason="nats_reconnected",
                                                notify_browser=True,
                                            )
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        asyncio.create_task(_report_control_lifecycle("subnet.nats.up"))
                                    except Exception:
                                        pass
                                    reported_down = False

                            def _ws_state(nc_for_diag: Any) -> tuple[Any, Any, Any, Any]:
                                try:
                                    tr = getattr(nc_for_diag, "_transport", None)
                                    ws = getattr(tr, "_ws", None) if tr is not None else None
                                    ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                    ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                    ws_close_reason = getattr(ws, "close_reason", None) if ws is not None else None
                                    ws_exc = None
                                    try:
                                        exf = getattr(ws, "exception", None)
                                        if callable(exf):
                                            ws_exc = exf()
                                    except Exception:
                                        ws_exc = None
                                    return ws_closed, ws_close_code, ws_close_reason, ws_exc
                                except Exception:
                                    return None, None, None, None

                            def _env_is_sensitive(name: str) -> bool:
                                try:
                                    n = (name or "").upper()
                                except Exception:
                                    return False
                                return any(x in n for x in ("PASS", "PASSWORD", "TOKEN", "SECRET", "KEY", "JWT", "AUTH"))

                            def _env_snapshot(keys: list[str]) -> str:
                                parts: list[str] = []
                                for k in keys:
                                    try:
                                        v = os.getenv(k)
                                    except Exception:
                                        v = None
                                    if v is None:
                                        parts.append(f"{k}=<unset>")
                                        continue
                                    vv = str(v)
                                    if _env_is_sensitive(k):
                                        if not vv:
                                            parts.append(f"{k}=<empty>")
                                        else:
                                            parts.append(f"{k}=<set:{len(vv)}>")
                                    else:
                                        # Avoid huge env values in logs.
                                        if len(vv) > 200:
                                            vv = vv[:200] + "…"
                                        parts.append(f"{k}={vv}")
                                return " ".join(parts)

                            diag_file_state: dict[str, float | None] = {"last_at": None}

                            def _nats_task_snapshot(task: Any, *, stack_limit: int = 6) -> dict[str, Any] | None:
                                if not isinstance(task, asyncio.Task):
                                    return None
                                snap: dict[str, Any] = {
                                    "done": bool(task.done()),
                                    "cancelled": bool(task.cancelled()),
                                }
                                try:
                                    exc = task.exception() if task.done() and not task.cancelled() else None
                                    snap["exc"] = f"{type(exc).__name__}: {exc}" if exc is not None else None
                                except Exception as exc:
                                    snap["exc"] = f"{type(exc).__name__}: {exc}"
                                frames: list[str] = []
                                try:
                                    for frame in task.get_stack(limit=max(1, int(stack_limit))):
                                        try:
                                            frames.append(
                                                f"{Path(frame.f_code.co_filename).name}:{int(frame.f_lineno)}:{frame.f_code.co_name}"
                                            )
                                        except Exception:
                                            continue
                                except Exception as exc:
                                    frames = [f"{type(exc).__name__}: {exc}"]
                                snap["stack"] = frames
                                return snap

                            def _write_nats_ws_diag_file(
                                nc_for_diag: Any,
                                *,
                                server: Any | None = None,
                                source: str | None = None,
                                task_name: str | None = None,
                                err: Exception | None = None,
                                force: bool = False,
                            ) -> None:
                                raw_path = str(os.getenv("HUB_NATS_WS_DIAG_FILE", "") or "").strip()
                                if not raw_path:
                                    return
                                try:
                                    every_s = float(os.getenv("HUB_NATS_WS_DIAG_EVERY_S", "2") or "2")
                                except Exception:
                                    every_s = 2.0
                                if every_s <= 0.0:
                                    every_s = 2.0
                                now_mono = time.monotonic()
                                last_at = diag_file_state.get("last_at")
                                if (
                                    not force
                                    and source == "periodic"
                                    and isinstance(last_at, (int, float))
                                    and (now_mono - float(last_at)) < max(0.5, every_s)
                                ):
                                    return
                                diag_file_state["last_at"] = now_mono
                                try:
                                    stack_limit = int(os.getenv("HUB_NATS_WS_DIAG_STACK_LIMIT", "6") or "6")
                                except Exception:
                                    stack_limit = 6
                                try:
                                    loop = asyncio.get_running_loop()
                                except RuntimeError:
                                    loop = None
                                try:
                                    policy = asyncio.get_event_loop_policy()
                                except Exception:
                                    policy = None
                                tr = getattr(nc_for_diag, "_transport", None)
                                ws = getattr(tr, "_ws", None) if tr is not None else None

                                def _ago(attr: str) -> float | None:
                                    try:
                                        value = getattr(tr, attr, None) if tr is not None else None
                                        if isinstance(value, (int, float)):
                                            return round(now_mono - float(value), 3)
                                    except Exception:
                                        return None
                                    return None

                                connected_attr = getattr(nc_for_diag, "is_connected", None)
                                closed_attr = getattr(nc_for_diag, "is_closed", None)
                                connect_url = server if server is not None else nats_last_server
                                snapshot: dict[str, Any] = {
                                    "ts": round(time.time(), 3),
                                    "source": source,
                                    "task_name": task_name,
                                    "server": connect_url,
                                    "connect_url": connect_url,
                                    "conn_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                    "loop_policy": type(policy).__name__ if policy is not None else None,
                                    "loop": type(loop).__name__ if loop is not None else None,
                                    "nc_connected": connected_attr() if callable(connected_attr) else bool(connected_attr),
                                    "nc_closed": closed_attr() if callable(closed_attr) else bool(closed_attr),
                                    "transport": type(tr).__name__ if tr is not None else None,
                                    "ws_url": getattr(tr, "_adaos_ws_url", None) if tr is not None else None,
                                    "ws_tag": getattr(tr, "_adaos_ws_tag", None) if tr is not None else None,
                                    "ws_proto": getattr(tr, "_adaos_ws_proto", None) if tr is not None else None,
                                    "ws_closed": getattr(ws, "closed", None) if ws is not None else None,
                                    "ws_close_code": getattr(ws, "close_code", None) if ws is not None else None,
                                    "last_rx_ago_s": _ago("_adaos_last_rx_at"),
                                    "last_tx_ago_s": _ago("_adaos_last_tx_at"),
                                    "last_ping_rx_ago_s": _ago("_adaos_last_ping_rx_at"),
                                    "last_pong_tx_ago_s": _ago("_adaos_last_pong_tx_at"),
                                    "last_ws_ping_tx_ago_s": _ago("_adaos_last_ws_ping_tx_at"),
                                    "ka_pings_rx": getattr(tr, "_adaos_pings_rx", None) if tr is not None else None,
                                    "ka_pongs_tx": getattr(tr, "_adaos_pongs_tx", None) if tr is not None else None,
                                    "ws_pings_tx": getattr(tr, "_adaos_ws_pings_tx", None) if tr is not None else None,
                                    "ws_data_ping_s": getattr(tr, "_adaos_ws_data_ping", None) if tr is not None else None,
                                    "data_pings_tx": getattr(tr, "_adaos_data_pings_tx", None) if tr is not None else None,
                                    "last_data_ping_tx_ago_s": _ago("_adaos_last_data_ping_tx_at"),
                                    "last_tx_kind": getattr(tr, "_adaos_last_tx_kind", None) if tr is not None else None,
                                    "last_tx_subj": getattr(tr, "_adaos_last_tx_subj", None) if tr is not None else None,
                                    "io_loop_ago_s": _ago("_adaos_io_loop_at"),
                                    "io_sending": getattr(tr, "_io_sending", None) if tr is not None else None,
                                    "current_send_kind": getattr(tr, "_adaos_current_send_kind", None) if tr is not None else None,
                                    "current_send_subj": getattr(tr, "_adaos_current_send_subj", None) if tr is not None else None,
                                    "current_send_len": getattr(tr, "_adaos_current_send_len", None) if tr is not None else None,
                                    "current_send_ago_s": _ago("_adaos_current_send_started_at"),
                                    "last_send_kind": getattr(tr, "_adaos_last_send_kind", None) if tr is not None else None,
                                    "last_send_subj": getattr(tr, "_adaos_last_send_subj", None) if tr is not None else None,
                                    "last_send_len": getattr(tr, "_adaos_last_send_len", None) if tr is not None else None,
                                    "last_send_done_ago_s": _ago("_adaos_last_send_done_at"),
                                    "last_send_duration_s": getattr(tr, "_adaos_last_send_duration_s", None) if tr is not None else None,
                                    "send_count": getattr(tr, "_adaos_send_count", None) if tr is not None else None,
                                    "send_fail_count": getattr(tr, "_adaos_send_fail_count", None) if tr is not None else None,
                                    "last_send_error": (
                                        f"{type(getattr(tr, '_adaos_last_send_error', None)).__name__}: {getattr(tr, '_adaos_last_send_error', None)}"
                                        if tr is not None and getattr(tr, "_adaos_last_send_error", None) is not None
                                        else None
                                    ),
                                    "last_send_error_ago_s": _ago("_adaos_last_send_error_at"),
                                    "transport_pending_hi_q": (
                                        getattr(getattr(tr, "_pending_hi", None), "qsize", lambda: None)()
                                        if tr is not None and getattr(tr, "_pending_hi", None) is not None
                                        else None
                                    ),
                                    "transport_pending_q": (
                                        getattr(getattr(tr, "_pending", None), "qsize", lambda: None)()
                                        if tr is not None and getattr(tr, "_pending", None) is not None
                                        else None
                                    ),
                                    "pending_data_size": getattr(nc_for_diag, "_pending_data_size", None),
                                    "io_task": _nats_task_snapshot(getattr(tr, "_io_task", None), stack_limit=stack_limit)
                                    if tr is not None
                                    else None,
                                    "data_ping_task": _nats_task_snapshot(
                                        getattr(tr, "_data_ping_task", None), stack_limit=stack_limit
                                    )
                                    if tr is not None
                                    else None,
                                    "reading_task": _nats_task_snapshot(getattr(nc_for_diag, "_reading_task", None), stack_limit=stack_limit),
                                    "flusher_task": _nats_task_snapshot(getattr(nc_for_diag, "_flusher_task", None), stack_limit=stack_limit),
                                    "ping_interval_task": _nats_task_snapshot(getattr(nc_for_diag, "_ping_interval_task", None), stack_limit=stack_limit),
                                    "err": f"{type(err).__name__}: {err}" if err is not None else None,
                                }
                                try:
                                    path = Path(raw_path)
                                    if not path.is_absolute():
                                        path = Path.cwd() / path
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    with path.open("a", encoding="utf-8") as fh:
                                        fh.write(_json.dumps(snapshot, ensure_ascii=False) + "\n")
                                except Exception:
                                    pass

                            def _log_nats_ws_diag(
                                nc_for_diag: Any,
                                *,
                                server: Any | None = None,
                                rate_key: str = "nats.ws_diag",
                                every_s: float = 1.0,
                                source: str | None = None,
                                task_name: str | None = None,
                                err: Exception | None = None,
                            ) -> tuple[Any, Any, Any, Any]:
                                ws_closed, ws_close_code, ws_close_reason, ws_exc = _ws_state(nc_for_diag)
                                try:
                                    tr = getattr(nc_for_diag, "_transport", None)
                                    ws = getattr(tr, "_ws", None) if tr is not None else None
                                    last_rx_at = getattr(tr, "_adaos_last_rx_at", None)
                                    last_rx_ago_s = None
                                    try:
                                        if isinstance(last_rx_at, (int, float)):
                                            last_rx_ago_s = round(time.monotonic() - float(last_rx_at), 3)
                                    except Exception:
                                        last_rx_ago_s = None
                                    last_tx_ago_s = None
                                    try:
                                        last_tx_at = getattr(tr, "_adaos_last_tx_at", None)
                                        if isinstance(last_tx_at, (int, float)):
                                            last_tx_ago_s = round(time.monotonic() - float(last_tx_at), 3)
                                    except Exception:
                                        last_tx_ago_s = None
                                    tx_connect_ago_s = None
                                    try:
                                        tx_connect_at = getattr(tr, "_adaos_tx_connect_at", None) if tr is not None else None
                                        if isinstance(tx_connect_at, (int, float)):
                                            tx_connect_ago_s = round(time.monotonic() - float(tx_connect_at), 3)
                                    except Exception:
                                        tx_connect_ago_s = None
                                    rx_info_ago_s = None
                                    try:
                                        rx_info_at = getattr(tr, "_adaos_rx_info_at", None) if tr is not None else None
                                        if isinstance(rx_info_at, (int, float)):
                                            rx_info_ago_s = round(time.monotonic() - float(rx_info_at), 3)
                                    except Exception:
                                        rx_info_ago_s = None
                                    max_payload = None
                                    try:
                                        max_payload = getattr(tr, "_adaos_nats_max_payload", None) if tr is not None else None
                                    except Exception:
                                        max_payload = None
                                    pending_data_size = getattr(nc_for_diag, "_pending_data_size", None)
                                    pings_outstanding = getattr(nc_for_diag, "_pings_outstanding", None)
                                    pongs_q = None
                                    try:
                                        pongs = getattr(nc_for_diag, "_pongs", None)
                                        if isinstance(pongs, list):
                                            pongs_q = len(pongs)
                                    except Exception:
                                        pongs_q = None
                                    tr_pending_q = None
                                    try:
                                        q = getattr(tr, "_pending", None) if tr is not None else None
                                        if q is not None:
                                            tr_pending_q = q.qsize()
                                    except Exception:
                                        tr_pending_q = None
                                    tr_pending_hi_q = None
                                    try:
                                        q_hi = getattr(tr, "_pending_hi", None) if tr is not None else None
                                        if q_hi is not None and callable(getattr(q_hi, "qsize", None)):
                                            tr_pending_hi_q = q_hi.qsize()
                                    except Exception:
                                        tr_pending_hi_q = None
                                    send_lock_locked = None
                                    try:
                                        lk = getattr(tr, "_send_lock", None) if tr is not None else None
                                        if lk is not None and callable(getattr(lk, "locked", None)):
                                            send_lock_locked = bool(lk.locked())
                                    except Exception:
                                        send_lock_locked = None
                                    ka_pings_rx = None
                                    ka_last_ping_rx_ago_s = None
                                    try:
                                        ka_pings_rx = getattr(tr, "_adaos_pings_rx", None) if tr is not None else None
                                        ka_last_ping_rx_at = getattr(tr, "_adaos_last_ping_rx_at", None) if tr is not None else None
                                        if isinstance(ka_last_ping_rx_at, (int, float)):
                                            ka_last_ping_rx_ago_s = round(time.monotonic() - float(ka_last_ping_rx_at), 3)
                                    except Exception:
                                        ka_pings_rx = ka_pings_rx or None
                                        ka_last_ping_rx_ago_s = ka_last_ping_rx_ago_s or None
                                    ka_pongs_tx = None
                                    ka_last_pong_tx_ago_s = None
                                    ka_last_pong_wait_ms = None
                                    ka_last_pong_send_ms = None
                                    try:
                                        ka_pongs_tx = getattr(tr, "_adaos_pongs_tx", None) if tr is not None else None
                                        ka_last_pong_tx_at = getattr(tr, "_adaos_last_pong_tx_at", None) if tr is not None else None
                                        if isinstance(ka_last_pong_tx_at, (int, float)):
                                            ka_last_pong_tx_ago_s = round(time.monotonic() - float(ka_last_pong_tx_at), 3)
                                        w_s = getattr(tr, "_adaos_last_pong_tx_wait_s", None) if tr is not None else None
                                        if isinstance(w_s, (int, float)):
                                            ka_last_pong_wait_ms = round(float(w_s) * 1000.0, 3)
                                        s_s = getattr(tr, "_adaos_last_pong_tx_send_s", None) if tr is not None else None
                                        if isinstance(s_s, (int, float)):
                                            ka_last_pong_send_ms = round(float(s_s) * 1000.0, 3)
                                    except Exception:
                                        ka_pongs_tx = ka_pongs_tx or None
                                        ka_last_pong_tx_ago_s = ka_last_pong_tx_ago_s or None
                                        ka_last_pong_wait_ms = ka_last_pong_wait_ms or None
                                        ka_last_pong_send_ms = ka_last_pong_send_ms or None
                                    ws_tag = None
                                    try:
                                        ws_tag = getattr(tr, "_adaos_ws_tag", None) if tr is not None else None
                                    except Exception:
                                        ws_tag = None
                                    if not ws_tag:
                                        try:
                                            ws_tag = ws_connect_tag if isinstance(ws_connect_tag, str) else None
                                        except Exception:
                                            ws_tag = None
                                    ws_hb = None
                                    try:
                                        ws_hb = getattr(tr, "_adaos_ws_heartbeat", None) if tr is not None else None
                                    except Exception:
                                        ws_hb = None
                                    ws_hb_mode = None
                                    try:
                                        ws_hb_mode = getattr(tr, "_adaos_ws_heartbeat_mode", None) if tr is not None else None
                                    except Exception:
                                        ws_hb_mode = None
                                    ws_data_hb = None
                                    try:
                                        ws_data_hb = getattr(tr, "_adaos_ws_data_heartbeat", None) if tr is not None else None
                                    except Exception:
                                        ws_data_hb = None
                                    ws_data_ping = None
                                    data_pings_tx = None
                                    data_last_ping_tx_ago_s = None
                                    try:
                                        ws_data_ping = getattr(tr, "_adaos_ws_data_ping", None) if tr is not None else None
                                        data_pings_tx = getattr(tr, "_adaos_data_pings_tx", None) if tr is not None else None
                                        data_last_ping_tx_at = getattr(tr, "_adaos_last_data_ping_tx_at", None) if tr is not None else None
                                        if isinstance(data_last_ping_tx_at, (int, float)):
                                            data_last_ping_tx_ago_s = round(time.monotonic() - float(data_last_ping_tx_at), 3)
                                    except Exception:
                                        ws_data_ping = ws_data_ping or None
                                        data_pings_tx = data_pings_tx or None
                                        data_last_ping_tx_ago_s = data_last_ping_tx_ago_s or None
                                    ws_recv_timeout = None
                                    try:
                                        ws_recv_timeout = getattr(tr, "_adaos_ws_recv_timeout", None) if tr is not None else None
                                    except Exception:
                                        ws_recv_timeout = None
                                    ws_url = None
                                    try:
                                        ws_url = getattr(tr, "_adaos_ws_url", None) if tr is not None else None
                                    except Exception:
                                        ws_url = None
                                    ws_proto = None
                                    try:
                                        ws_proto = getattr(tr, "_adaos_ws_proto", None) if tr is not None else None
                                    except Exception:
                                        ws_proto = None
                                    if not ws_proto:
                                        try:
                                            ws_proto = getattr(ws, "protocol", None) if ws is not None else None
                                        except Exception:
                                            ws_proto = None
                                    if not ws_proto:
                                        try:
                                            ws_proto = getattr(ws, "_response", None).headers.get("Sec-WebSocket-Protocol") if ws is not None and getattr(ws, "_response", None) is not None else None
                                        except Exception:
                                            ws_proto = None
                                    last_tx_kind = None
                                    last_tx_subj = None
                                    last_tx_len = None
                                    try:
                                        last_tx_kind = getattr(tr, "_adaos_last_tx_kind", None) if tr is not None else None
                                        last_tx_subj = getattr(tr, "_adaos_last_tx_subj", None) if tr is not None else None
                                        last_tx_len = getattr(tr, "_adaos_last_tx_len", None) if tr is not None else None
                                    except Exception:
                                        last_tx_kind = last_tx_kind or None
                                        last_tx_subj = last_tx_subj or None
                                        last_tx_len = last_tx_len or None
                                    last_recv_err = None
                                    last_recv_err_ago_s = None
                                    try:
                                        last_recv_err = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
                                        last_recv_err_at = getattr(tr, "_adaos_last_recv_error_at", None) if tr is not None else None
                                        if isinstance(last_recv_err_at, (int, float)):
                                            last_recv_err_ago_s = round(time.monotonic() - float(last_recv_err_at), 3)
                                    except Exception:
                                        last_recv_err = last_recv_err or None
                                        last_recv_err_ago_s = last_recv_err_ago_s or None
                                    ws_pings_tx = None
                                    ws_last_ping_tx_ago_s = None
                                    ws_last_ping_wait_ms = None
                                    ws_last_ping_send_ms = None
                                    try:
                                        ws_pings_tx = getattr(tr, "_adaos_ws_pings_tx", None) if tr is not None else None
                                        ws_last_ping_tx_at = getattr(tr, "_adaos_last_ws_ping_tx_at", None) if tr is not None else None
                                        if isinstance(ws_last_ping_tx_at, (int, float)):
                                            ws_last_ping_tx_ago_s = round(time.monotonic() - float(ws_last_ping_tx_at), 3)
                                        ws_ping_wait_s = getattr(tr, "_adaos_last_ws_ping_tx_wait_s", None) if tr is not None else None
                                        if isinstance(ws_ping_wait_s, (int, float)):
                                            ws_last_ping_wait_ms = round(float(ws_ping_wait_s) * 1000.0, 3)
                                        ws_ping_send_s = getattr(tr, "_adaos_last_ws_ping_tx_send_s", None) if tr is not None else None
                                        if isinstance(ws_ping_send_s, (int, float)):
                                            ws_last_ping_send_ms = round(float(ws_ping_send_s) * 1000.0, 3)
                                    except Exception:
                                        ws_pings_tx = ws_pings_tx or None
                                        ws_last_ping_tx_ago_s = ws_last_ping_tx_ago_s or None
                                        ws_last_ping_wait_ms = ws_last_ping_wait_ms or None
                                        ws_last_ping_send_ms = ws_last_ping_send_ms or None
                                    io_loop_ago_s = None
                                    current_send_kind = None
                                    current_send_subj = None
                                    current_send_len = None
                                    current_send_ago_s = None
                                    last_send_kind = None
                                    last_send_subj = None
                                    last_send_len = None
                                    last_send_done_ago_s = None
                                    last_send_duration_ms = None
                                    send_count = None
                                    send_fail_count = None
                                    last_send_err = None
                                    last_send_err_ago_s = None
                                    try:
                                        io_loop_at = getattr(tr, "_adaos_io_loop_at", None) if tr is not None else None
                                        if isinstance(io_loop_at, (int, float)):
                                            io_loop_ago_s = round(time.monotonic() - float(io_loop_at), 3)
                                        current_send_kind = getattr(tr, "_adaos_current_send_kind", None) if tr is not None else None
                                        current_send_subj = getattr(tr, "_adaos_current_send_subj", None) if tr is not None else None
                                        current_send_len = getattr(tr, "_adaos_current_send_len", None) if tr is not None else None
                                        current_send_at = (
                                            getattr(tr, "_adaos_current_send_started_at", None) if tr is not None else None
                                        )
                                        if isinstance(current_send_at, (int, float)):
                                            current_send_ago_s = round(time.monotonic() - float(current_send_at), 3)
                                        last_send_kind = getattr(tr, "_adaos_last_send_kind", None) if tr is not None else None
                                        last_send_subj = getattr(tr, "_adaos_last_send_subj", None) if tr is not None else None
                                        last_send_len = getattr(tr, "_adaos_last_send_len", None) if tr is not None else None
                                        last_send_done_at = (
                                            getattr(tr, "_adaos_last_send_done_at", None) if tr is not None else None
                                        )
                                        if isinstance(last_send_done_at, (int, float)):
                                            last_send_done_ago_s = round(time.monotonic() - float(last_send_done_at), 3)
                                        last_send_duration_s = (
                                            getattr(tr, "_adaos_last_send_duration_s", None) if tr is not None else None
                                        )
                                        if isinstance(last_send_duration_s, (int, float)):
                                            last_send_duration_ms = round(float(last_send_duration_s) * 1000.0, 3)
                                        send_count = getattr(tr, "_adaos_send_count", None) if tr is not None else None
                                        send_fail_count = getattr(tr, "_adaos_send_fail_count", None) if tr is not None else None
                                        last_send_err = getattr(tr, "_adaos_last_send_error", None) if tr is not None else None
                                        last_send_err_at = (
                                            getattr(tr, "_adaos_last_send_error_at", None) if tr is not None else None
                                        )
                                        if isinstance(last_send_err_at, (int, float)):
                                            last_send_err_ago_s = round(time.monotonic() - float(last_send_err_at), 3)
                                    except Exception:
                                        pass
                                    server0 = _resolve_nats_log_server(
                                        server=server,
                                        current_attempt=nats_attempt_server,
                                        connected_server=nats_last_server,
                                    )
                                    extra_parts: list[str] = []
                                    if source:
                                        extra_parts.append(f"source={source}")
                                    if task_name:
                                        extra_parts.append(f"task={task_name}")
                                    if err is not None:
                                        extra_parts.append(f"err={type(err).__name__}: {err}")
                                    extra_suffix = (" " + " ".join(extra_parts)) if extra_parts else ""
                                    _rl_log(
                                        rate_key,
                                        f"[hub-io] nats ws diag: tag={ws_tag} server={server0} ws_hb_s={ws_hb} ws_hb_mode={ws_hb_mode} ws_data_hb_s={ws_data_hb} ws_data_ping_s={ws_data_ping} data_pings_tx={data_pings_tx} data_last_ping_tx_ago_s={data_last_ping_tx_ago_s} ws_recv_timeout_s={ws_recv_timeout} ws_url={ws_url} closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc} last_rx_ago_s={last_rx_ago_s} last_tx_ago_s={last_tx_ago_s} io_loop_ago_s={io_loop_ago_s} tx_connect_ago_s={tx_connect_ago_s} rx_info_ago_s={rx_info_ago_s} max_payload={max_payload} pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q} transport_pending_hi_q={tr_pending_hi_q} transport_pending_q={tr_pending_q} send_lock={send_lock_locked} current_send_kind={current_send_kind} current_send_subj={current_send_subj} current_send_len={current_send_len} current_send_ago_s={current_send_ago_s} last_send_kind={last_send_kind} last_send_subj={last_send_subj} last_send_len={last_send_len} last_send_done_ago_s={last_send_done_ago_s} last_send_duration_ms={last_send_duration_ms} send_count={send_count} send_fail_count={send_fail_count} last_send_err={type(last_send_err).__name__ if last_send_err is not None else None} last_send_err_ago_s={last_send_err_ago_s} ka_pings_rx={ka_pings_rx} ka_last_ping_rx_ago_s={ka_last_ping_rx_ago_s} ka_pongs_tx={ka_pongs_tx} ka_last_pong_tx_ago_s={ka_last_pong_tx_ago_s} ka_last_pong_wait_ms={ka_last_pong_wait_ms} ka_last_pong_send_ms={ka_last_pong_send_ms} ws_pings_tx={ws_pings_tx} ws_last_ping_tx_ago_s={ws_last_ping_tx_ago_s} ws_last_ping_wait_ms={ws_last_ping_wait_ms} ws_last_ping_send_ms={ws_last_ping_send_ms} ws_proto={ws_proto} last_tx_kind={last_tx_kind} last_tx_subj={last_tx_subj} last_tx_len={last_tx_len} last_recv_err={type(last_recv_err).__name__ if last_recv_err is not None else None} last_recv_err_ago_s={last_recv_err_ago_s}{extra_suffix}",
                                        every_s=every_s,
                                    )
                                except Exception:
                                    pass
                                return ws_closed, ws_close_code, ws_close_reason, ws_exc

                            async def _on_error_cb(e: Exception, *, nc_for_diag: Any | None = None) -> None:
                                # Best-effort; keep quiet unless explicitly verbose or useful
                                is_transient = is_transient_nats_error(e)
                                is_eof = type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1" and is_eof:
                                    return
                                # Emit extra transport diagnostics to correlate client-side errors with root-side logs.
                                if nc_for_diag is not None and (is_eof or os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1"):
                                    try:
                                        ws_closed, ws_close_code, ws_close_reason, ws_exc = _log_nats_ws_diag(
                                            nc_for_diag,
                                            server=_resolve_nats_log_server(
                                                current_attempt=nats_attempt_server,
                                                connected_server=nats_last_server,
                                            ),
                                            rate_key="nats.ws_diag",
                                            every_s=1.0,
                                            source="error_cb",
                                            err=e,
                                        )
                                        _rl_log(
                                            "nats.ws_eof",
                                            f"[hub-io] nats ws eof: closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc}",
                                            every_s=1.0,
                                        )
                                        await asyncio.to_thread(
                                            _write_nats_ws_diag_file,
                                            nc_for_diag,
                                            server=_resolve_nats_log_server(
                                                current_attempt=nats_attempt_server,
                                                connected_server=nats_last_server,
                                            ),
                                            source="error_cb",
                                            err=e,
                                            force=True,
                                        )
                                    except Exception:
                                        pass
                                # Capture the effective env knobs around NATS-over-WS on errors to make log sharing actionable.
                                try:
                                    _env = _env_snapshot(
                                        [
                                            "HUB_NATS_PING_INTERVAL_S",
                                            "HUB_NATS_MAX_OUTSTANDING_PINGS",
                                            "HUB_NATS_DISABLE_PING_INTERVAL_TASK",
                                            "HUB_NATS_RX_TIMEOUT_S",
                                            "HUB_NATS_WS_IMPL",
                                            "HUB_NATS_WS_MAX_MSG_SIZE",
                                            "HUB_NATS_WS_MAX_QUEUE",
                                            "HUB_NATS_WS_HEARTBEAT_S",
                                            "HUB_NATS_WS_DATA_HEARTBEAT_S",
                                            "HUB_NATS_WS_PROXY",
                                            "HUB_NATS_WS_TRACE",
                                            "HUB_NATS_WS_PATCH_AIOHTTP",
                                            "HUB_NATS_WIRETAP",
                                            "HUB_NATS_WIRETAP_MAX_BYTES",
                                            "HUB_NATS_WIRETAP_EVERY_N",
                                            "HUB_NATS_WIRETAP_SKIP",
                                            "HUB_NATS_TCP_KEEPALIVE",
                                            "HUB_NATS_TCP_KEEPALIVE_S",
                                            "HUB_NATS_TCP_KEEPALIVE_INTERVAL_S",
                                            "HUB_NATS_TCP_KEEPALIVE_PROBES",
                                            "HUB_NATS_RAW_KEEPALIVE",
                                            "HUB_NATS_RAW_KEEPALIVE_S",
                                            "HUB_NATS_CONNECT_TAG_QUERY",
                                            "HUB_TRACE",
                                            "WS_NATS_PROXY_WS_PING",
                                            "WS_NATS_PROXY_TERMINATE_CLIENT_PING",
                                            "WS_NATS_PROXY_KEEPALIVE_REQUIRE_HANDSHAKE",
                                            "WS_NATS_PROXY_WIRETAP",
                                        ]
                                    )
                                    if _env:
                                        _rl_log("nats.env", f"[hub-io] nats env: {_env}", every_s=30.0)
                                except Exception:
                                    pass
                                try:
                                    if type(e).__name__ == "SlowConsumerError":
                                        try:
                                            sub_sc = getattr(e, "sub", None)
                                            q_sc = getattr(sub_sc, "_pending_queue", None) if sub_sc is not None else None
                                            qsize_sc = q_sc.qsize() if q_sc is not None and callable(getattr(q_sc, "qsize", None)) else None
                                        except Exception:
                                            qsize_sc = None
                                        try:
                                            pending_size_sc = getattr(sub_sc, "_pending_size", None) if sub_sc is not None else None
                                        except Exception:
                                            pending_size_sc = None
                                        try:
                                            subject_sc = getattr(e, "subject", None)
                                        except Exception:
                                            subject_sc = None
                                        try:
                                            sid_sc = getattr(e, "sid", None)
                                        except Exception:
                                            sid_sc = None
                                        try:
                                            self._log.warning(
                                                "nats slow consumer hub_id=%s server=%s subject=%s sid=%s qsize=%s pending_size=%s",
                                                hub_id,
                                                nats_last_server,
                                                subject_sc,
                                                sid_sc,
                                                qsize_sc,
                                                pending_size_sc,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            _rl_log(
                                                "nats.slow_consumer",
                                                f"[hub-io] nats slow consumer subject={subject_sc} sid={sid_sc} qsize={qsize_sc} pending_size={pending_size_sc}",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                    error_summary = nats_error_summary(e)
                                    log_method = self._log.info if is_transient else self._log.warning
                                    log_method(
                                        "nats error_cb hub_id=%s server=%s transient=%s type=%s err=%s",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                        is_transient,
                                        type(e).__name__,
                                        error_summary,
                                    )
                                except Exception:
                                    pass
                                try:
                                    verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                                    quiet = os.getenv("HUB_NATS_QUIET", "1") == "1"
                                    if quiet and not verbose and not is_eof:
                                        return
                                    if type(e).__name__ == "WSServerHandshakeError" and not verbose:
                                        print("[hub-io] nats error_cb: WSServerHandshakeError (check nats.ws_url path: '/' vs '/nats')")
                                        return
                                    if verbose:
                                        print(f"[hub-io] nats error_cb: {type(e).__name__}: {e!s}")
                                    else:
                                        print(f"[hub-io] nats error_cb: {type(e).__name__}")
                                except Exception:
                                    pass

                            async def _on_disconnected() -> None:
                                try:
                                    self._log.warning(
                                        "nats disconnected hub_id=%s server=%s",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                    )
                                except Exception:
                                    pass
                                _emit_down("disconnected", None)

                            async def _on_reconnected() -> None:
                                try:
                                    self._log.info(
                                        "nats reconnected hub_id=%s server=%s",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                    )
                                except Exception:
                                    pass
                                # Suppress restored chatter in dev if silenced
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1":
                                    try:
                                        self.ctx.bus.publish(Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats"))
                                    except Exception:
                                        pass
                                else:
                                    _emit_up()

                            # Coerce types to what nats-py expects
                            # For WS proxy auth, always identify as the canonical hub id regardless of any human-friendly alias
                            try:
                                is_ws_candidates = any(isinstance(s, str) and s.startswith("ws") for s in candidates)
                            except Exception:
                                is_ws_candidates = False
                            if is_ws_candidates or realtime_enabled:
                                resolved_hub_id, resolved_nats_user = _canonical_hub_nats_identity(
                                    local_hub_id=hub_id,
                                    nats_user=nuser,
                                )
                                if resolved_hub_id:
                                    hub_id = resolved_hub_id
                                if resolved_nats_user:
                                    user = resolved_nats_user
                            hub_id_str = hub_id if isinstance(hub_id, str) else str(hub_id)
                            user_str = user if (user is None or isinstance(user, str)) else str(user)
                            pw_str = pw if (pw is None or isinstance(pw, str)) else str(pw)
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                try:
                                    print(
                                        f"[hub-io] nats connect opts: name={runtime_connect_name(prefix=f'hub-{hub_id_str!s}')} "
                                        f"user={type(user_str).__name__} pass={type(pw_str).__name__} "
                                        f"role={runtime_role} instance={runtime_instance} servers={candidates}"
                                    )
                                except Exception:
                                    pass

                            try:
                                install_transient_nats_log_filter()
                            except Exception:
                                pass

                            # NOTE: Connect to candidates sequentially. Some endpoints can hang the WS handshake
                            # (leading to "Authentication Timeout") while others work; trying one-by-one keeps
                            # failures isolated and helps cleanup transports.
                            async def _try_connect(server: str) -> Any:
                                # `nats` package does not expose Client at top-level; use nats.aio.client.Client.
                                nc_local = _nats.aio.client.Client()
                                async def _on_error_cb_local(e: Exception) -> None:
                                    await _on_error_cb(
                                        e,
                                        nc_for_diag=nc_local,
                                    )
                                try:
                                    # New correlation id for this connect attempt (sent as WS header).
                                    try:
                                        nonlocal ws_connect_tag
                                        ws_connect_tag = f"{hub_id_str}-{uuid.uuid4().hex[:10]}"
                                    except Exception:
                                        ws_connect_tag = None
                                    connect_server = str(server)
                                    # Some transports do not reliably propagate custom WS headers.
                                    # Optionally attach the correlation id as a query param to help root-side
                                    # logs correlate abnormal closes (1006/EOF) to hub attempts.
                                    try:
                                        if (
                                            connect_server.startswith("ws")
                                            and os.getenv("HUB_NATS_CONNECT_TAG_QUERY", "0") == "1"
                                            and isinstance(ws_connect_tag, str)
                                            and ws_connect_tag
                                        ):
                                            from urllib.parse import urlparse as _urlparse, urlunparse as _urlunparse, parse_qsl as _parse_qsl, urlencode as _urlencode
                                            u = _urlparse(connect_server)
                                            q = dict(_parse_qsl(u.query, keep_blank_values=True))
                                            q.setdefault("adaos_conn", ws_connect_tag)
                                            connect_server = _urlunparse(u._replace(query=_urlencode(q)))
                                    except Exception:
                                        connect_server = str(server)
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            _rl_log(
                                                "nats.connect_try",
                                                f"[hub-io] NATS connect try server={connect_server} tag={ws_connect_tag}",
                                                every_s=1.0,
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        configure_hub_root_transport_strategy(
                                            effective_transport=_hub_root_transport_kind(connect_server),
                                            selected_server=connect_server,
                                            current_ws_tag=ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                            hypothesis={
                                                "selector_loop": bool(os.name == "nt" and os.getenv("ADAOS_WIN_SELECTOR_LOOP", "0") == "1"),
                                                "ws_impl": str(os.getenv("HUB_NATS_WS_IMPL", "") or "").strip() or None,
                                                "raw_keepalive": _env_truthy(os.getenv("HUB_NATS_RAW_KEEPALIVE"), default=False),
                                            },
                                        )
                                        record_hub_root_transport_event(
                                            "attempt",
                                            transport=_hub_root_transport_kind(connect_server),
                                            server=connect_server,
                                            summary="hub-root connect attempt started",
                                            details={"ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None},
                                        )
                                    except Exception:
                                        pass
                                    # Keepalive:
                                    # - Root's ws-nats-proxy sends NATS `PING\r\n` frames to the hub, but those
                                    #   only keep the WS tunnel alive if the hub actually replies with `PONG\r\n`.
                                    # - Some reverse proxies / LBs will still cut long-lived WS connections if the
                                    #   client stays silent (observed as ~1000s / close 1006 + ECONNRESET on root).
                                    #
                                    # Therefore, for WS transports default to a small hub->root ping interval to
                                    # guarantee outbound traffic even when the hub is otherwise idle.
                                    # NOTE: Some NATS-over-WS proxies (observed on inimatic ws-nats-proxy) can
                                    # flap with close 1006/UnexpectedEOF when the client sends periodic NATS PINGs.
                                    # Root/proxy already sends server PINGs, so the hub still generates outbound
                                    # traffic by replying with PONGs even if the client ping interval is conservative.
                                    try:
                                        # Defaults:
                                        # - WS: keep the client ping interval conservative (root/proxy already produces traffic).
                                        # - TCP: use a small ping interval so we can detect half-open links faster and avoid
                                        #   long stalls on Windows (often observed as WinError 121 in the reader task).
                                        is_ws = bool(connect_server.startswith("ws"))
                                        if is_ws:
                                            ping_interval_default = "3600"
                                        else:
                                            try:
                                                is_windows = (os.name == "nt")
                                            except Exception:
                                                is_windows = False
                                            ping_interval_default = "15" if is_windows else "60"
                                        ping_interval = int(
                                            os.getenv("HUB_NATS_PING_INTERVAL_S", ping_interval_default)
                                            or ping_interval_default
                                        )
                                        # nats-py always starts the ping task; 0 would create a busy-loop.
                                        if ping_interval <= 0:
                                            ping_interval = int(ping_interval_default)
                                    except Exception:
                                        ping_interval = 3600
                                    try:
                                        max_out_default = "10" if is_ws else "2"
                                        max_outstanding_pings = int(os.getenv("HUB_NATS_MAX_OUTSTANDING_PINGS", max_out_default) or max_out_default)
                                    except Exception:
                                        max_outstanding_pings = 10 if is_ws else 2
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            _rl_log(
                                                "nats.keepalive",
                                                f"[hub-io] nats keepalive ping_interval={ping_interval}s max_outstanding_pings={max_outstanding_pings}",
                                                every_s=60.0,
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        configure_hub_root_transport_strategy(
                                            effective_transport=_hub_root_transport_kind(connect_server),
                                            selected_server=connect_server,
                                            current_ws_tag=ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                            hypothesis={
                                                "selector_loop": bool(os.name == "nt" and os.getenv("ADAOS_WIN_SELECTOR_LOOP", "0") == "1"),
                                                "ws_impl": str(os.getenv("HUB_NATS_WS_IMPL", "") or "").strip() or None,
                                                "raw_keepalive": _env_truthy(os.getenv("HUB_NATS_RAW_KEEPALIVE"), default=False),
                                                "ping_interval_s": ping_interval,
                                                "max_outstanding_pings": max_outstanding_pings,
                                            },
                                        )
                                    except Exception:
                                        pass
                                    await asyncio.wait_for(
                                        nc_local.connect(
                                            servers=[connect_server],
                                            user=user_str,
                                            password=pw_str,
                                            name=runtime_connect_name(prefix=f"hub-{hub_id_str}"),
                                            ws_connection_headers=(
                                                {
                                                    "X-AdaOS-Nats-Conn": [ws_connect_tag],
                                                    "X-AdaOS-Runtime-Instance": [runtime_instance],
                                                    "X-AdaOS-Runtime-Role": [runtime_role],
                                                }
                                                if connect_server.startswith("ws") and isinstance(ws_connect_tag, str) and ws_connect_tag
                                                else None
                                            ),
                                            allow_reconnect=False,
                                            # Be tolerant to intermittent WS proxy hiccups: missed PONGs should not
                                            # tear down the whole hub IO bridge too aggressively.
                                            ping_interval=ping_interval,
                                            max_outstanding_pings=max_outstanding_pings,
                                            connect_timeout=5.0,
                                            error_cb=_on_error_cb_local,
                                            disconnected_cb=_on_disconnected,
                                            reconnected_cb=_on_reconnected,
                                        ),
                                        timeout=7.0,
                                    )
                                    try:
                                        tr = getattr(nc_local, "_transport", None)
                                        if tr is not None:
                                            try:
                                                setattr(tr, "_adaos_nc", nc_local)
                                            except Exception:
                                                pass
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            hb = getattr(tr, "_adaos_ws_heartbeat", None) if tr else None
                                            hb_mode = getattr(tr, "_adaos_ws_heartbeat_mode", None) if tr else None
                                            if hb is not None:
                                                _rl_log(
                                                    "nats.ws_hb",
                                                    f"[hub-io] nats ws heartbeat: {hb!s}s mode={hb_mode}",
                                                    every_s=60.0,
                                                )
                                            if isinstance(ws_connect_tag, str) and ws_connect_tag:
                                                _rl_log("nats.ws_tag", f"[hub-io] nats ws tag: {ws_connect_tag}", every_s=1.0)
                                            _rl_log(
                                                "nats.transport",
                                                f"[hub-io] nats transport kind: {type(tr).__name__ if tr is not None else None}",
                                                every_s=60.0,
                                            )
                                    except Exception:
                                        pass
                                    # Optionally disable periodic client PINGs on WS transports.
                                    # Some proxies respond poorly to client-initiated PINGs and can force-close (1006/EOF).
                                    # Default: disable for WS; can be re-enabled with HUB_NATS_DISABLE_PING_INTERVAL_TASK=0.
                                    try:
                                        if connect_server.startswith("ws"):
                                            disable_env = os.getenv("HUB_NATS_DISABLE_PING_INTERVAL_TASK", "1")
                                            disable_ping_task = str(disable_env or "").strip() != "0"
                                            if disable_ping_task:
                                                pt = getattr(nc_local, "_ping_interval_task", None)
                                                if isinstance(pt, asyncio.Task):
                                                    try:
                                                        if not pt.done():
                                                            pt.cancel()
                                                    except Exception:
                                                        pass
                                                    # Important: our own bridge watchdog treats core task termination as fatal.
                                                    # When we intentionally disable the ping task, clear the reference so the
                                                    # watchdog doesn't restart the whole bridge on a cancelled task.
                                                    try:
                                                        setattr(nc_local, "_ping_interval_task", None)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        setattr(nc_local, "_adaos_ping_interval_task_disabled", True)
                                                    except Exception:
                                                        pass
                                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                                        _rl_log(
                                                            "nats.ping_task_off",
                                                            "[hub-io] nats ping interval task disabled for WS transport",
                                                            every_s=60.0,
                                                        )
                                    except Exception:
                                        pass

                                    return nc_local
                                except Exception as e:
                                    # Extra diagnostics for flaky WS/NATS drops (e.g. UnexpectedEOF without close frame).
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            tr = getattr(nc_local, "_transport", None)
                                            ws = getattr(tr, "_ws", None) if tr else None
                                            ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                            ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                            ws_exc = None
                                            try:
                                                exf = getattr(ws, "exception", None)
                                                if callable(exf):
                                                    ws_exc = exf()
                                            except Exception:
                                                ws_exc = None
                                            _rl_log(
                                                "nats.ws_diag",
                                                f"[hub-io] nats ws diag: tag={ws_connect_tag} server={locals().get('connect_server', None)} err={type(e).__name__} closed={ws_closed} close_code={ws_close_code} ws_exc={ws_exc}",
                                                every_s=2.0,
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        record_hub_root_transport_event(
                                            "connect_failed",
                                            transport=_hub_root_transport_kind(locals().get("connect_server", None)),
                                            server=locals().get("connect_server", None),
                                            summary="hub-root connect attempt failed",
                                            error=str(e),
                                            details={"ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None},
                                        )
                                    except Exception:
                                        pass

                                    # Best-effort token refresh on auth-ish failures.
                                    try:
                                        if _looks_like_auth_failure(e):
                                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                try:
                                                    print(
                                                        f"[hub-io] NATS auth failure suspected; refreshing credentials (err={type(e).__name__}: {e})"
                                                    )
                                                except Exception:
                                                    pass
                                            await _fetch_nats_credentials()
                                    except Exception:
                                        pass
                                    # Best-effort cleanup of partially created WS transport
                                    try:
                                        await nc_local.close()
                                    except Exception:
                                        pass
                                    # Ensure WS transport is fully torn down if connect() was cancelled/timed out.
                                    try:
                                        tr = getattr(nc_local, "_transport", None)
                                        if tr:
                                            ws = getattr(tr, "_ws", None)
                                            client = getattr(tr, "_client", None)
                                            try:
                                                if ws is not None:
                                                    await ws.close()
                                            except Exception:
                                                pass
                                            try:
                                                if client is not None:
                                                    await client.close()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    raise e

                            last_exc: Exception | None = None
                            nc = None
                            connected_server: str | None = None
                            for srv in [str(s) for s in candidates]:
                                try:
                                    nats_attempt_server = srv
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                        print(f"[hub-io] NATS connect try server={srv}")
                                    elif trace:
                                        _rl_log("nats.try", f"[hub-io] nats connect try server={srv}", every_s=1.0)
                                    nc = await _try_connect(srv)
                                    last_exc = None
                                    connected_server = srv
                                    break
                                except Exception as e:
                                    last_exc = e
                                    if trace:
                                        _rl_log("nats.try_fail", f"[hub-io] nats connect failed server={srv} err={type(e).__name__}", every_s=1.0)
                                    continue
                            if nc is None:
                                raise last_exc or RuntimeError("nats connect failed (no candidates)")
                            try:
                                nats_last_server = connected_server
                                nats_attempt_server = None
                            except Exception:
                                pass
                            try:
                                # Expose for external forced reconnect requests (debug/ops).
                                self._hub_root_nc = nc
                            except Exception:
                                pass

                            # Keepalive: periodically send a tiny NATS protocol frame from hub->root.
                            #
                            # Root's WS proxy already sends NATS `PING` frames to the hub, but the main purpose of that
                            # is to elicit outbound traffic hub->root (`PONG`) to keep some NAT/firewall mappings alive.
                            # In practice, hubs sometimes end up mostly silent and the WS gets closed abnormally (1006),
                            # then hub sees `UnexpectedEOF`. To reduce dependency on nats-py's internal ping futures and
                            # ensure regular outbound traffic, optionally send raw `PING` via `_send_command`+`_flush_pending`.
                            #
                            # This avoids using `flush()` and avoids creating `_pongs` futures which can later explode
                            # with `InvalidStateError` on late/cancelled PONGs.
                            try:
                                raw_keepalive_env = os.getenv("HUB_NATS_RAW_KEEPALIVE", "")
                                # Default OFF: this uses nats-py internals (`_send_command`/`_flush_pending`) from a
                                # separate task and can introduce hard-to-debug races. Root already sends NATS PINGs to
                                # elicit hub->root traffic (PONG), and nats-py also has its own ping interval.
                                raw_keepalive_enabled = raw_keepalive_env.strip() == "1"
                            except Exception:
                                raw_keepalive_enabled = False
                            if raw_keepalive_enabled:
                                try:
                                    raw_keepalive_s = float(os.getenv("HUB_NATS_RAW_KEEPALIVE_S", "15") or "15")
                                except Exception:
                                    raw_keepalive_s = 15.0
                                if raw_keepalive_s < 5.0:
                                    raw_keepalive_s = 5.0

                                async def _raw_keepalive_loop() -> None:
                                    ping_cmd = b"PING\r\n"
                                    sent = 0
                                    while True:
                                        await asyncio.sleep(raw_keepalive_s)
                                        try:
                                            is_closed_attr = getattr(nc, "is_closed", None)
                                            is_closed = is_closed_attr() if callable(is_closed_attr) else bool(is_closed_attr)
                                            if is_closed:
                                                return
                                        except Exception:
                                            pass
                                        try:
                                            sc = getattr(nc, "_send_command", None)
                                            fp = getattr(nc, "_flush_pending", None)
                                            if callable(sc) and callable(fp):
                                                await sc(ping_cmd)
                                                # Ensure the frame actually hits the wire; otherwise some proxies/LBs
                                                # may still consider the connection idle and close it (1006/EOF).
                                                try:
                                                    await fp(force_flush=True)
                                                except TypeError:
                                                    try:
                                                        await fp(True)
                                                    except TypeError:
                                                        await fp()
                                            else:
                                                # Fallback: if internals changed, use public flush() to force outbound IO.
                                                flush = getattr(nc, "flush", None)
                                                if callable(flush):
                                                    try:
                                                        await flush(timeout=1.0)
                                                    except Exception:
                                                        pass
                                            sent += 1
                                            try:
                                                # Log early pings too: if we disconnect before reaching 10,
                                                # it is still useful to know whether we managed to send keepalives.
                                                if sent <= 3 and (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace):
                                                    _rl_log(
                                                        "nats.raw_keepalive_first",
                                                        f"[hub-io] nats raw keepalive sent={sent} every_s={raw_keepalive_s:.1f}",
                                                        every_s=0.5,
                                                    )
                                                if (sent % 10) == 0 and (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace):
                                                    _rl_log(
                                                        "nats.raw_keepalive",
                                                        f"[hub-io] nats raw keepalive sent={sent} every_s={raw_keepalive_s:.1f}",
                                                        every_s=5.0,
                                                    )
                                            except Exception:
                                                pass
                                        except Exception as e:
                                            try:
                                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                                    _rl_log(
                                                        "nats.raw_keepalive_err",
                                                        f"[hub-io] nats raw keepalive failed err={type(e).__name__}: {e}",
                                                        every_s=1.0,
                                                    )
                                            except Exception:
                                                pass
                                            # Keepalive is best-effort; connection supervisor will handle reconnects.
                                            pass

                                try:
                                    raw_keepalive_task = asyncio.create_task(_raw_keepalive_loop(), name="adaos-nats-raw-keepalive")
                                except Exception:
                                    raw_keepalive_task = None

                            # Track subscriptions explicitly. When the connection closes (or this task is cancelled),
                            # unsubscribing helps nats-py cancel internal `_wait_for_msgs()` tasks and avoids
                            # "Task was destroyed but it is pending!" warnings on reconnect/shutdown.
                            subs: list[Any] = []
                            sub_workers: list[asyncio.Task] = []
                            _route_dispatch_trace = (
                                os.getenv("HUB_ROUTE_DISPATCH_TRACE", "0") == "1"
                                or os.getenv("HUB_ROUTE_TRACE", "0") == "1"
                                or os.getenv("HUB_TRACE", "0") == "1"
                            )

                            def _route_dispatch_log(msg0: str) -> None:
                                if not _route_dispatch_trace:
                                    return
                                try:
                                    print(msg0)
                                except Exception:
                                    pass

                            def _sub_qsize(sub0: Any) -> int | None:
                                try:
                                    q0 = getattr(sub0, "_pending_queue", None)
                                    if q0 is None:
                                        return None
                                    qsize = getattr(q0, "qsize", None)
                                    if callable(qsize):
                                        return int(qsize())
                                except Exception:
                                    return None
                                return None

                            def _sub_pending_bytes(sub0: Any) -> int | None:
                                try:
                                    pending_bytes = getattr(sub0, "pending_bytes", None)
                                    if isinstance(pending_bytes, int):
                                        return int(pending_bytes)
                                    if callable(pending_bytes):
                                        return int(pending_bytes())
                                except Exception:
                                    return None
                                return None

                            async def _sub(subject: str, *, cb: Any):
                                traffic_class = hub_root_protocol_traffic_class(subject)
                                policy = hub_root_protocol_class_policy(traffic_class)
                                sub = await nc.subscribe(
                                    subject,
                                    pending_msgs_limit=int(policy.get("pending_msgs_limit") or 1),
                                    pending_bytes_limit=int(policy.get("pending_bytes_limit") or 1024),
                                )
                                # `nats-py` queues SUB locally and may not push it to the server until
                                # some later flush / publish. That breaks Root->Hub routing because
                                # `route.to_hub.*` must be active before the first proxied request arrives.
                                fp = getattr(nc, "_flush_pending", None)
                                if callable(fp):
                                    try:
                                        await asyncio.wait_for(fp(force_flush=True), timeout=2.0)
                                    except TypeError:
                                        try:
                                            await asyncio.wait_for(fp(True), timeout=2.0)
                                        except TypeError:
                                            await asyncio.wait_for(fp(), timeout=2.0)
                                else:
                                    await nc.flush(timeout=2.0)
                                subs.append(sub)
                                try:
                                    observe_hub_root_protocol_subscription(
                                        subject,
                                        traffic_class=traffic_class,
                                        pending_msgs_limit=int(policy.get("pending_msgs_limit") or 0),
                                        pending_bytes_limit=int(policy.get("pending_bytes_limit") or 0),
                                        qsize=_sub_qsize(sub),
                                        pending_bytes=_sub_pending_bytes(sub),
                                    )
                                except Exception:
                                    pass

                                async def _runner() -> None:
                                    try:
                                        pending_queue = getattr(sub, "_pending_queue", None)
                                        while True:
                                            if pending_queue is None:
                                                raise RuntimeError("subscription pending queue missing")
                                            msg = await pending_queue.get()
                                            try:
                                                msg_subject = ""
                                                msg_bytes = None
                                                started = None
                                                if _route_dispatch_trace and (
                                                    subject == "route.to_hub.*"
                                                    or subject.startswith("route.to_hub.")
                                                    or subject.startswith("route.to_browser.")
                                                ):
                                                    try:
                                                        msg_subject = str(getattr(msg, "subject", "") or "")
                                                    except Exception:
                                                        msg_subject = ""
                                                    try:
                                                        raw0 = bytes(getattr(msg, "data", b"") or b"")
                                                        msg_bytes = len(raw0)
                                                    except Exception:
                                                        msg_bytes = None
                                                    started = time.monotonic()
                                                    _route_dispatch_log(
                                                        f"[hub-route:dispatch] start sub={subject} msg={msg_subject} qsize={_sub_qsize(sub)} bytes={msg_bytes}"
                                                    )
                                                await cb(msg)
                                                try:
                                                    observe_hub_root_protocol_subscription(
                                                        subject,
                                                        traffic_class=traffic_class,
                                                        qsize=_sub_qsize(sub),
                                                        pending_bytes=_sub_pending_bytes(sub),
                                                        dispatched=True,
                                                        message_bytes=msg_bytes,
                                                    )
                                                except Exception:
                                                    pass
                                                if started is not None:
                                                    took_ms = (time.monotonic() - started) * 1000.0
                                                    _route_dispatch_log(
                                                        f"[hub-route:dispatch] done sub={subject} msg={msg_subject} qsize={_sub_qsize(sub)} took_ms={took_ms:.1f}"
                                                    )
                                            except asyncio.CancelledError:
                                                raise
                                            except Exception as e:
                                                try:
                                                    observe_hub_root_protocol_subscription(
                                                        subject,
                                                        traffic_class=traffic_class,
                                                        qsize=_sub_qsize(sub),
                                                        pending_bytes=_sub_pending_bytes(sub),
                                                        handler_error=f"{type(e).__name__}: {e}",
                                                    )
                                                except Exception:
                                                    pass
                                                try:
                                                    self._log.warning(
                                                        "nats subscription handler failed subject=%s type=%s err=%s",
                                                        subject,
                                                        type(e).__name__,
                                                        e,
                                                    )
                                                except Exception:
                                                    pass
                                            finally:
                                                try:
                                                    pending_queue.task_done()
                                                except Exception:
                                                    pass
                                                try:
                                                    data0 = getattr(msg, "data", b"")
                                                    if hasattr(data0, "__len__"):
                                                        sub._pending_size -= len(data0)
                                                except Exception:
                                                    pass
                                    except asyncio.CancelledError:
                                        return
                                    except Exception as e:
                                        try:
                                            observe_hub_root_protocol_subscription(
                                                subject,
                                                traffic_class=traffic_class,
                                                qsize=_sub_qsize(sub),
                                                pending_bytes=_sub_pending_bytes(sub),
                                                handler_error=f"worker_stopped:{type(e).__name__}: {e}",
                                                worker_done=True,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            self._log.warning(
                                                "nats subscription worker stopped subject=%s type=%s err=%s",
                                                subject,
                                                type(e).__name__,
                                                e,
                                            )
                                        except Exception:
                                            pass
                                    finally:
                                        try:
                                            observe_hub_root_protocol_subscription(
                                                subject,
                                                traffic_class=traffic_class,
                                                qsize=_sub_qsize(sub),
                                                pending_bytes=_sub_pending_bytes(sub),
                                                worker_done=True,
                                            )
                                        except Exception:
                                            pass

                                task = asyncio.create_task(_runner(), name=f"adaos-nats-sub-{subject}")
                                sub_workers.append(task)
                                return sub

                            # Outbound bridge: local bus -> root NATS.
                            # This lets skills/router publish `tg.output.<bot>.chat.<chat_id>` and have
                            # the backend deliver it to Telegram, without requiring TG_BOT_TOKEN on the hub.
                            try:
                                setattr(self, "_tg_output_nats_nc", nc)
                            except Exception:
                                pass

                            def _report_tg_outbox(
                                *,
                                drained: int = 0,
                                dropped: int = 0,
                                publish_ok: int = 0,
                                publish_fail: int = 0,
                                operation_key: str | None = None,
                                last_error: str | None = None,
                            ) -> None:
                                try:
                                    q0 = getattr(self, "_tg_output_pending", None)
                                    size0 = len(q0) if q0 is not None else 0
                                except Exception:
                                    size0 = 0
                                try:
                                    persist_path0 = getattr(self, "_tg_output_persist_path", None)
                                    persist_path0 = str(persist_path0) if persist_path0 else ""
                                except Exception:
                                    persist_path0 = ""
                                try:
                                    max_outbox0 = int(os.getenv("HUB_TG_OUTBOX_MAX", "200") or "200")
                                except Exception:
                                    max_outbox0 = 200
                                try:
                                    observe_hub_root_integration_outbox(
                                        "telegram",
                                        size=size0,
                                        max_size=max_outbox0,
                                        durable_store=True,
                                        persist_path=persist_path0,
                                        persisted_size=size0,
                                        drained=drained,
                                        dropped=dropped,
                                        publish_ok=publish_ok,
                                        publish_fail=publish_fail,
                                        connected=bool(getattr(self, "_tg_output_nats_nc", None)),
                                        operation_key=operation_key,
                                        last_error=last_error,
                                    )
                                except Exception:
                                    pass

                            def _persist_tg_outbox() -> None:
                                try:
                                    q0 = getattr(self, "_tg_output_pending", None)
                                    if q0 is None:
                                        return
                                    save_outbox_items("telegram", q0)
                                except Exception:
                                    try:
                                        _report_tg_outbox(last_error="persist_failed")
                                    except Exception:
                                        pass

                            def _tg_subject_protocol(subj0: str, payload0: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
                                try:
                                    payload_dict = dict(payload0 or {}) if isinstance(payload0, dict) else {}
                                except Exception:
                                    payload_dict = {}
                                existing = payload_dict.get("_protocol")
                                if isinstance(existing, dict) and str(existing.get("operation_key") or "").strip():
                                    return payload_dict, existing
                                parts = str(subj0 or "").split(".")
                                bot_id = ""
                                chat_id = ""
                                if len(parts) >= 5 and parts[0] == "tg" and parts[1] == "output":
                                    bot_id = str(parts[2] or "").strip()
                                    if str(parts[3] or "").strip() == "chat":
                                        chat_id = ".".join(parts[4:]).strip()
                                target = payload_dict.get("target") if isinstance(payload_dict.get("target"), dict) else {}
                                bot_id = str(target.get("bot_id") or bot_id or "main-bot").strip() or "main-bot"
                                chat_id = str(target.get("chat_id") or chat_id).strip()
                                hub_ref = str(target.get("hub_id") or hub_id or "").strip() or "unknown_hub"
                                normalized = dict(payload_dict)
                                normalized.pop("_protocol", None)
                                try:
                                    raw = _json.dumps(
                                        {"subject": subj0, "payload": normalized},
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                except Exception:
                                    raw = _json.dumps({"subject": subj0, "repr": repr(normalized)}, ensure_ascii=False)
                                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                                protocol = {
                                    "flow_id": "hub_root.integration.telegram",
                                    "message_type": "command",
                                    "delivery_class": "must_not_lose",
                                    "stream_id": f"hub-integration:telegram:{hub_ref}:{bot_id}:{chat_id or 'unknown_chat'}",
                                    "message_id": f"tgmsg:{digest[:24]}",
                                    "operation_key": f"tgop:{hub_ref}:{bot_id}:{chat_id or 'unknown_chat'}:{digest[:24]}",
                                    "authority_epoch": f"hub:{hub_ref}",
                                    "issued_at": time.time(),
                                    "ttl_ms": 600_000,
                                }
                                payload_dict["_protocol"] = protocol
                                return payload_dict, protocol

                            def _split_tg_outbox_item(item: Any) -> tuple[str, bytes, dict[str, Any] | None]:
                                if isinstance(item, tuple):
                                    if len(item) >= 3:
                                        subj0 = str(item[0] or "")
                                        data0 = bytes(item[1] or b"")
                                        meta0 = item[2] if isinstance(item[2], dict) else None
                                        return subj0, data0, meta0
                                    if len(item) == 2:
                                        return str(item[0] or ""), bytes(item[1] or b""), None
                                return "", b"", None

                            # Drain outbox (replay replies produced while NATS was down/flapping).
                            try:
                                q = getattr(self, "_tg_output_pending", None)
                                if q:
                                    drained = 0
                                    max_drain = 200
                                    try:
                                        max_drain = int(os.getenv("HUB_TG_OUTBOX_DRAIN_MAX", "200") or "200")
                                    except Exception:
                                        max_drain = 200
                                    while q and (max_drain <= 0 or drained < max_drain):
                                        try:
                                            subj0, data0, meta0 = _split_tg_outbox_item(q[0])
                                        except Exception:
                                            break
                                        try:
                                            await nc.publish(str(subj0), bytes(data0))
                                            fp = getattr(nc, "_flush_pending", None)
                                            if callable(fp):
                                                await fp(force_flush=True)
                                            try:
                                                q.popleft()
                                            except Exception:
                                                pass
                                            _persist_tg_outbox()
                                            drained += 1
                                            try:
                                                observe_hub_root_protocol_publish(
                                                    str(subj0),
                                                    ok=True,
                                                    traffic_class="integration",
                                                    payload_bytes=len(bytes(data0)),
                                                )
                                            except Exception:
                                                pass
                                            try:
                                                _report_tg_outbox(
                                                    drained=1,
                                                    operation_key=str((meta0 or {}).get("operation_key") or "").strip() or None,
                                                )
                                            except Exception:
                                                pass
                                        except Exception:
                                            try:
                                                observe_hub_root_protocol_publish(
                                                    str(subj0),
                                                    ok=False,
                                                    traffic_class="integration",
                                                    payload_bytes=len(bytes(data0)),
                                                    error="drain_failed",
                                                )
                                            except Exception:
                                                pass
                                            break
                                    if drained and (hub_nats_verbose or trace):
                                        _rl_log("nats.outbox", f"[hub-io] tg outbox drained={drained}", every_s=1.0)
                                    if not drained:
                                        _report_tg_outbox()
                                else:
                                    _report_tg_outbox()
                            except Exception:
                                try:
                                    _report_tg_outbox(last_error="drain_failed")
                                except Exception:
                                    pass

                            try:
                                if candidate_passive_mode:
                                    pass
                                elif not bool(getattr(self, "_tg_output_bridge_hooked", False)):

                                    def _on_local_output(ev: Event) -> None:
                                        try:
                                            subj = ev.type
                                            if not isinstance(subj, str) or not subj.startswith("tg.output."):
                                                return
                                            try:
                                                payload_dict, protocol_meta = _tg_subject_protocol(subj, ev.payload or {})
                                                data = _json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
                                            except Exception:
                                                protocol_meta = None
                                                data = b"{}"
                                            max_outbox = 200
                                            try:
                                                max_outbox = int(os.getenv("HUB_TG_OUTBOX_MAX", "200") or "200")
                                            except Exception:
                                                max_outbox = 200

                                            def _queue() -> None:
                                                dropped = 0
                                                last_op = str((protocol_meta or {}).get("operation_key") or "").strip() or None
                                                try:
                                                    q = getattr(self, "_tg_output_pending", None)
                                                    if q is None:
                                                        q = deque()
                                                        setattr(self, "_tg_output_pending", q)
                                                    while max_outbox > 0 and len(q) >= max_outbox:
                                                        dropped_item = q.popleft()
                                                        _, _, dropped_meta = _split_tg_outbox_item(dropped_item)
                                                        dropped_op = str((dropped_meta or {}).get("operation_key") or "").strip() or None
                                                        if dropped_op and not last_op:
                                                            last_op = dropped_op
                                                        dropped += 1
                                                    q.append((subj, data, protocol_meta))
                                                    _persist_tg_outbox()
                                                except Exception:
                                                    return
                                                try:
                                                    _report_tg_outbox(dropped=dropped, operation_key=last_op)
                                                except Exception:
                                                    pass

                                            nc2 = getattr(self, "_tg_output_nats_nc", None)
                                            if not nc2:
                                                _queue()
                                                return

                                            async def _publish_or_queue() -> None:
                                                try:
                                                    await nc2.publish(subj, data)
                                                    fp = getattr(nc2, "_flush_pending", None)
                                                    if callable(fp):
                                                        await fp(force_flush=True)
                                                    try:
                                                        observe_hub_root_protocol_publish(
                                                            subj,
                                                            ok=True,
                                                            traffic_class="integration",
                                                            payload_bytes=len(data),
                                                        )
                                                    except Exception:
                                                        pass
                                                    try:
                                                        _report_tg_outbox(
                                                            publish_ok=1,
                                                            operation_key=str((protocol_meta or {}).get("operation_key") or "").strip() or None,
                                                        )
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    try:
                                                        observe_hub_root_protocol_publish(
                                                            subj,
                                                            ok=False,
                                                            traffic_class="integration",
                                                            payload_bytes=len(data),
                                                            error="publish_failed",
                                                        )
                                                    except Exception:
                                                        pass
                                                    try:
                                                        _report_tg_outbox(
                                                            publish_fail=1,
                                                            operation_key=str((protocol_meta or {}).get("operation_key") or "").strip() or None,
                                                            last_error="publish_failed",
                                                        )
                                                    except Exception:
                                                        pass
                                                    _queue()

                                            try:
                                                loop = asyncio.get_running_loop()
                                                loop.create_task(_publish_or_queue())
                                            except RuntimeError:
                                                _queue()
                                        except Exception:
                                            return

                                    # Prefix subscription on LocalEventBus works as "starts with".
                                    core_bus.subscribe("tg.output.", _on_local_output)
                                    setattr(self, "_tg_output_bridge_hooked", True)
                            except Exception:
                                pass
                            subj = f"tg.input.{hub_id}"
                            subj_legacy = f"io.tg.in.{hub_id}.text"
                            if candidate_passive_mode:
                                if hub_nats_verbose or not hub_nats_quiet:
                                    print(
                                        f"[hub-io] NATS candidate runtime connected passively hub_id={hub_id} "
                                        f"instance={runtime_instance} role={runtime_role}"
                                    )
                            elif hub_nats_verbose or not hub_nats_quiet:
                                print(f"[hub-io] NATS subscribe {subj} and legacy {subj_legacy}")
                            else:
                                # In quiet mode we still want a single signal that we are connected, because
                                # troubleshooting "TG stops responding" depends on correlating with NATS flaps.
                                _rl_log(
                                    "nats.connected",
                                    f"[hub-io] nats connected ({connected_server or 'unknown'})",
                                    every_s=2.0,
                                )
                            try:
                                self._log.info(
                                    "nats bridge connected server=%s hub_id=%s role=%s instance=%s passive=%s",
                                    connected_server or "unknown",
                                    hub_id,
                                    runtime_role,
                                    runtime_instance,
                                    candidate_passive_mode,
                                )
                            except Exception:
                                pass
                            try:
                                if (
                                    isinstance(established_ws_tag, str)
                                    and established_ws_tag
                                    and isinstance(ws_connect_tag, str)
                                    and ws_connect_tag
                                    and ws_connect_tag != established_ws_tag
                                ):
                                    reconnect_payload = {
                                        "ts": time.time(),
                                        "server": connected_server or "unknown",
                                        "previous_ws_tag": established_ws_tag,
                                        "ws_tag": ws_connect_tag,
                                    }
                                    try:
                                        note_root_control_reconnect(
                                            summary="hub-root websocket session tag changed after reconnect",
                                            details=reconnect_payload,
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        record_hub_root_transport_event(
                                            "reconnected",
                                            transport=_hub_root_transport_kind(connected_server),
                                            server=connected_server,
                                            summary="hub-root transport websocket tag changed after reconnect",
                                            details=reconnect_payload,
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        self.ctx.bus.publish(
                                            Event(type="subnet.nats.reconnect", payload=reconnect_payload, source="io.nats")
                                        )
                                    except Exception:
                                        pass
                                established_ws_tag = ws_connect_tag if isinstance(ws_connect_tag, str) and ws_connect_tag else established_ws_tag
                            except Exception:
                                pass
                            try:
                                configure_hub_root_transport_strategy(
                                    effective_transport=_hub_root_transport_kind(connected_server),
                                    selected_server=connected_server,
                                    current_ws_tag=ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                )
                                record_hub_root_transport_event(
                                    "connected",
                                    transport=_hub_root_transport_kind(connected_server),
                                    server=connected_server,
                                    summary="hub-root control session established",
                                    details={
                                        "phase": "initial_connect",
                                        "ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                    },
                                )
                            except Exception:
                                pass
                            try:
                                mark_root_control_up(
                                    summary="hub-root control session established",
                                    details={
                                        "server": connected_server or "unknown",
                                        "phase": "initial_connect",
                                        "ws_tag": ws_connect_tag if isinstance(ws_connect_tag, str) else None,
                                    },
                                )
                            except Exception:
                                pass
                            try:
                                asyncio.create_task(_report_control_lifecycle("nats.initial_connect"))
                            except Exception:
                                pass
                            # First successful connect after failures
                            _emit_up()
                            try:
                                conf_local = getattr(self.ctx, "config", None)
                                if (
                                    getattr(conf_local, "role", None) == "hub"
                                    and bool(getattr(conf_local, "core_update_enabled", True))
                                    and not candidate_passive_mode
                                    and not _dev_api_serve_core_update_sync_disabled()
                                ):
                                    async def _reconcile_core_release_after_connect() -> None:
                                        try:
                                            result = await asyncio.to_thread(reconcile_hub_core_update, conf_local)
                                            if isinstance(result, dict) and result.get("ok"):
                                                release = result.get("release") if isinstance(result.get("release"), dict) else {}
                                                set_integration_readiness(
                                                    "github",
                                                    status=ReadinessStatus.READY,
                                                    summary="core update release probe succeeded through root",
                                                    details={
                                                        "needs_update": bool(result.get("needs_update")),
                                                        "branch": str(release.get("branch") or result.get("branch") or ""),
                                                        "head_sha": str(release.get("head_sha") or ""),
                                                    },
                                                )
                                            else:
                                                set_integration_readiness(
                                                    "github",
                                                    status=ReadinessStatus.DEGRADED,
                                                    summary="core update release probe returned an unexpected response",
                                                    details={"result_type": type(result).__name__},
                                                )
                                            if isinstance(result, dict) and result.get("needs_update"):
                                                try:
                                                    self._log.info(
                                                        "core update reconcile scheduled hub_id=%s branch=%s release=%s",
                                                        hub_id,
                                                        result.get("branch") or "",
                                                        ((result.get("release") or {}) if isinstance(result.get("release"), dict) else {}).get("head_short_sha")
                                                        or ((result.get("release") or {}) if isinstance(result.get("release"), dict) else {}).get("head_sha")
                                                        or "",
                                                    )
                                                except Exception:
                                                    pass
                                        except Exception:
                                            try:
                                                set_integration_readiness(
                                                    "github",
                                                    status=ReadinessStatus.DEGRADED,
                                                    summary="core update reconcile failed",
                                                    details={"error": traceback.format_exc(limit=1).strip()},
                                                )
                                            except Exception:
                                                pass
                                            try:
                                                self._log.warning("core update reconcile failed hub_id=%s", hub_id, exc_info=True)
                                            except Exception:
                                                pass
                                    loop.create_task(_reconcile_core_release_after_connect())
                            except Exception:
                                pass
                            nats_last_ok_at = time.monotonic()
                            # Baseline for RX watchdog (updated by patched WebSocketTransport.readline()).
                            try:
                                tr = getattr(nc, "_transport", None)
                                if tr is not None and not hasattr(tr, "_adaos_last_rx_at"):
                                    setattr(tr, "_adaos_last_rx_at", time.monotonic())
                            except Exception:
                                pass

                            # Control channel: hub alias updates from backend
                            try:
                                ctl_alias = f"hub.control.{hub_id}.alias"

                                async def _ctl_alias_cb(msg):
                                    try:
                                        data = _json.loads(msg.data.decode("utf-8"))
                                    except Exception:
                                        data = {}
                                    alias = (data or {}).get("alias")
                                    if isinstance(alias, str) and alias:
                                        try:
                                            save_subnet_alias(alias, subnet_id=hub_id)
                                            try:
                                                self.ctx.bus.publish(Event(type="subnet.alias.changed", payload={"alias": alias, "subnet_id": hub_id}, source="io.nats"))
                                            except Exception:
                                                pass
                                            print(f"[hub-io] alias set via NATS: {alias}")
                                        except Exception:
                                            pass

                                await _sub(ctl_alias, cb=_ctl_alias_cb)
                                if hub_nats_verbose or not hub_nats_quiet:
                                    print(f"[hub-io] NATS subscribe control {ctl_alias}")
                            except Exception:
                                pass
                            break
                        except Exception as e:
                            # Optionally print per-attempt diagnostics when verbose
                            try:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                    emsg = _explain_connect_error(e)
                                    print(f"[hub-io] NATS connect failed: {emsg}")
                                    try:
                                        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                                        print(tb.rstrip())
                                    except Exception:
                                        pass
                                else:
                                    if not (
                                        os.getenv("SILENCE_NATS_EOF", "0") == "1"
                                        and (type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower())
                                    ):
                                        # Minimal single-line failure for non-EOF issues
                                        print(f"[hub-io] NATS connect failed: {_explain_connect_error(e)}")
                            except Exception:
                                pass
                            # One-time down message and bus event while offline
                            try:
                                _emit_down("connect_error", e)
                            except Exception:
                                pass
                            # On failure, keep retrying with backoff; candidates are rebuilt each attempt.
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2.0, 30.0)

                    async def cb(msg):
                        msg_subject = str(getattr(msg, "subject", "") or subj)
                        if trace:
                            try:
                                _rl_log("nats.msg", f"[hub-io] nats recv subject={msg_subject} bytes={len(getattr(msg, 'data', b'') or b'')}", every_s=0.2)
                            except Exception:
                                pass
                        try:
                            data = _json.loads(msg.data.decode("utf-8"))
                        except Exception:
                            data = {}
                        try:
                            # Media fetch: if event includes telegram media, download to local cache and annotate path
                            p = (data or {}).get("payload") or {}
                            typ = p.get("type") or (data.get("type") if isinstance(data.get("type"), str) else None)
                            bot_id = p.get("bot_id") or data.get("bot_id") or ""
                            file_id = p.get("file_id") if isinstance(p, dict) else None
                            if not file_id and isinstance(p, dict):
                                file_id = p.get("payload", {}).get("file_id") if isinstance(p.get("payload"), dict) else None
                            media_path = None
                            if isinstance(typ, str) and file_id and bot_id and typ in ("photo", "document", "audio", "voice"):
                                base = self.ctx.settings.api_base.rstrip("/")
                                token = os.getenv("ADAOS_TOKEN", "")
                                url = f"{base}/internal/tg/file?bot_id={bot_id}&file_id={file_id}"
                                cache_dir = self.ctx.paths.cache_dir()
                                cache_dir.mkdir(parents=True, exist_ok=True)
                                import urllib.request as _ureq
                                import uuid as _uuid
                                import mimetypes as _mtypes
                                # `urllib` is blocking; run the download and file write in a worker thread so it
                                # doesn't stall the hub event loop (and therefore NATS keepalives).
                                def _download() -> str:
                                    req = _ureq.Request(url, headers={"X-AdaOS-Token": token})
                                    with _ureq.urlopen(req, timeout=20) as resp:
                                        # Prefer filename from header; fallback to Content-Disposition; then use type
                                        fname = resp.headers.get("X-File-Name") or ""
                                        if not fname:
                                            cd = resp.headers.get("Content-Disposition") or ""
                                            try:
                                                import cgi as _cgi

                                                _val, _params = _cgi.parse_header(cd)
                                                fname = _params.get("filename") or ""
                                            except Exception:
                                                fname = ""
                                        if fname:
                                            import os as _os

                                            fname = _os.path.basename(fname)
                                        else:
                                            # fallback to type-based extension
                                            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                                            ext = _mtypes.guess_extension(ctype) or ""
                                            fname = f"tg_{_uuid.uuid4().hex}{ext}"
                                        dest = cache_dir / fname
                                        with open(dest, "wb") as out:
                                            out.write(resp.read())
                                    return str(dest)

                                media_path = await asyncio.to_thread(_download)
                                # annotate
                                if isinstance(p, dict):
                                    if isinstance(p.get("payload"), dict):
                                        p["payload"]["file_path"] = media_path
                                    else:
                                        p["file_path"] = media_path
                                data["payload"] = p
                        except Exception:
                            pass
                        try:
                            self.ctx.bus.publish(Event(type=msg_subject, payload=data, source="io.nats", ts=time.time()))
                        except Exception:
                            pass

                    if candidate_passive_mode:
                        try:
                            self._log.info(
                                "nats candidate runtime stays passive on root subjects hub_id=%s instance=%s",
                                hub_id,
                                runtime_instance,
                            )
                        except Exception:
                            pass
                    else:
                        await _sub(subj, cb=cb)
                        try:
                            self._log.info("nats bridge subscribed subject=%s", subj)
                        except Exception:
                            pass

                    # Browser<->Hub routing over NATS (root proxy fallback).
                    # Root publishes `route.to_hub.<key>` where key is "<hub_id>--<conn_id|http--req_id>" (no dots).
                    # Hub responds on `route.to_browser.<same-key>`.
                    try:
                        if candidate_passive_mode:
                            raise RuntimeError("candidate runtime keeps root route relay passive until cutover")
                        # Optional dependency: if `websockets` is missing, keep HTTP proxy working
                        # and gracefully deny WS tunnel opens.
                        websockets_mod = None
                        try:
                            import websockets as _websockets  # type: ignore

                            websockets_mod = _websockets
                        except Exception:
                            websockets_mod = None

                        tunnels: dict[str, dict[str, Any]] = {}
                        tunnel_tasks: dict[str, asyncio.Task] = {}
                        media_relay_sessions: dict[str, dict[str, Any]] = {}
                        pending_chunks: dict[str, dict[str, Any]] = {}
                        pending_tunnel_events: dict[str, list[dict[str, Any]]] = {}
                        pending_tunnel_meta: dict[str, dict[str, Any]] = {}
                        pending_tunnel_close_tasks: dict[str, asyncio.Task] = {}
                        # Map route key -> reply subject so we can support both legacy v1 and v2 subjects.
                        # v1:  route.to_browser.<key>
                        # v2:  route.v2.to_browser.<hubId>.<key>
                        reply_subjects: dict[str, str] = {}
                        MAX_CHUNK_RAW = 300_000
                        try:
                            MAX_PENDING_TUNNEL_EVENTS = max(
                                8,
                                int(os.getenv("HUB_ROUTE_PENDING_EVENTS_MAX", "128") or "128"),
                            )
                        except Exception:
                            MAX_PENDING_TUNNEL_EVENTS = 128

                        _route_verbose = os.getenv("HUB_ROUTE_VERBOSE", "0") == "1"
                        _route_diag = _route_verbose or os.getenv("HUB_ROUTE_DIAG", "0") == "1"
                        # Tx logs are extremely noisy (one line per request / response). Keep them separately gated.
                        _route_tx_verbose = os.getenv("HUB_ROUTE_CONSOLE_TX_VERBOSE", "0") == "1"
                        # Trace is an opt-in "everything we know" log for debugging WS routing breaks.
                        _route_trace = os.getenv("HUB_ROUTE_TRACE", "0") == "1"
                        _route_http_trace = (
                            _route_trace
                            or os.getenv("HUB_ROUTE_HTTP_TRACE", "0") == "1"
                            or os.getenv("HUB_TRACE", "0") == "1"
                        )
                        # Frame logs are extremely noisy; keep them explicitly gated.
                        _route_frame_verbose = (
                            os.getenv("HUB_ROUTE_FRAME_VERBOSE", "0") == "1"
                            or os.getenv("ROUTE_PROXY_FRAME_VERBOSE", "0") == "1"
                        )
                        _route_no_upstream_close_after_s = _hub_route_force_close_no_upstream_s()

                        try:
                            route_run_id = uuid.uuid4().hex[:6]
                        except Exception:
                            route_run_id = "route"
                        route_sub = None
                        route_sub_v2 = None
                        route_reset_total = 0
                        # In WS-proxied NATS setups, route replies can sit in local buffers and root times out
                        # waiting for `route.to_browser.*`. Keep fast drain enabled by default.
                        _route_force_flush = os.getenv("HUB_ROUTE_FORCE_FLUSH", "1") == "1"
                        try:
                            _route_send_timeout_s = float(os.getenv("HUB_ROUTE_SEND_TIMEOUT_S", "2.0") or "2.0")
                        except Exception:
                            _route_send_timeout_s = 2.0
                        try:
                            _route_upstream_ws_send_timeout_s = float(
                                os.getenv("HUB_ROUTE_UPSTREAM_WS_SEND_TIMEOUT_S", "2.0") or "2.0"
                            )
                        except Exception:
                            _route_upstream_ws_send_timeout_s = 2.0
                        try:
                            _route_flush_timeout_s = float(os.getenv("HUB_ROUTE_FLUSH_TIMEOUT_S", "1.0") or "1.0")
                        except Exception:
                            _route_flush_timeout_s = 1.0
                        try:
                            _route_publish_slow_warn_s = float(
                                os.getenv("HUB_ROUTE_PUBLISH_SLOW_WARN_S", "0.250") or "0.250"
                            )
                        except Exception:
                            _route_publish_slow_warn_s = 0.250
                        if _route_publish_slow_warn_s < 0.01:
                            _route_publish_slow_warn_s = 0.01
                        try:
                            _route_starvation_warn_s = float(
                                os.getenv("HUB_ROUTE_STARVATION_WARN_S", "1.000") or "1.000"
                            )
                        except Exception:
                            _route_starvation_warn_s = 1.0
                        if _route_starvation_warn_s < 0.05:
                            _route_starvation_warn_s = 0.05
                        try:
                            _route_pending_data_warn_bytes = int(
                                os.getenv("HUB_ROUTE_PENDING_DATA_WARN_BYTES", str(2 * 1024 * 1024)) or str(2 * 1024 * 1024)
                            )
                        except Exception:
                            _route_pending_data_warn_bytes = 2 * 1024 * 1024
                        if _route_pending_data_warn_bytes < 0:
                            _route_pending_data_warn_bytes = 0
                        try:
                            _route_guard_pending_data_bytes = int(
                                os.getenv(
                                    "HUB_ROUTE_GUARD_PENDING_DATA_BYTES",
                                    str(max(_route_pending_data_warn_bytes, 4 * 1024 * 1024)),
                                )
                                or str(max(_route_pending_data_warn_bytes, 4 * 1024 * 1024))
                            )
                        except Exception:
                            _route_guard_pending_data_bytes = max(_route_pending_data_warn_bytes, 4 * 1024 * 1024)
                        if _route_guard_pending_data_bytes < 0:
                            _route_guard_pending_data_bytes = 0
                        try:
                            _route_guard_oldest_age_s = float(
                                os.getenv(
                                    "HUB_ROUTE_GUARD_OLDEST_AGE_S",
                                    str(max(_route_starvation_warn_s, 1.5)),
                                )
                                or str(max(_route_starvation_warn_s, 1.5))
                            )
                        except Exception:
                            _route_guard_oldest_age_s = max(_route_starvation_warn_s, 1.5)
                        if _route_guard_oldest_age_s < 0.05:
                            _route_guard_oldest_age_s = 0.05

                        # Optional probe mitigation: resend inline probe replies after short delays.
                        # Useful when NATS-over-WS intermittently drops a single PUB frame and Root times out.
                        _route_probe_resend_delays_s: list[float] = []
                        try:
                            raw_delays = str(os.getenv("HUB_ROUTE_PROBE_RESEND_S", "") or "").strip()
                            if raw_delays:
                                seen_delays: set[float] = set()
                                for it in raw_delays.split(","):
                                    it = it.strip()
                                    if not it:
                                        continue
                                    try:
                                        d = float(it)
                                    except Exception:
                                        continue
                                    if d <= 0:
                                        continue
                                    # Avoid runaway schedules.
                                    if d > 10.0:
                                        d = 10.0
                                    if d in seen_delays:
                                        continue
                                    seen_delays.add(d)
                                    _route_probe_resend_delays_s.append(d)
                        except Exception:
                            _route_probe_resend_delays_s = []
                        if _route_probe_resend_delays_s and (_route_verbose or _route_tx_verbose):
                            try:
                                _rl_log(
                                    "hub-route.probe_resend_cfg",
                                    f"[hub-route] probe resend delays_s={_route_probe_resend_delays_s}",
                                    every_s=60.0,
                                )
                            except Exception:
                                pass

                        route_diag_state: dict[str, Any] = {
                            "open_request_total": 0,
                            "http_request_total": 0,
                            "last_open_path": "",
                            "last_open_query_has_token": False,
                            "last_open_base_total": 0,
                            "last_http_path": "",
                            "last_http_method": "",
                            "pending_oldest_age_s": 0.0,
                            "pending_oldest_key_tag": "",
                            "pending_starved_total": 0,
                            "last_pending_key_tag": "",
                            "last_nc_pending_data_size": 0,
                            "reply_publish_slow_total": 0,
                            "reply_flush_slow_total": 0,
                            "reply_publish_fail_total": 0,
                            "last_publish_slow_key_tag": "",
                            "last_publish_slow_ms": 0.0,
                            "last_flush_slow_key_tag": "",
                            "last_flush_slow_ms": 0.0,
                            "guardrail_active": False,
                            "guardrail_reason": "",
                            "guardrail_age_s": 0.0,
                            "guardrail_activation_total": 0,
                            "guardrail_clear_total": 0,
                            "dispatch_queue_size": 0,
                            "dispatch_queue_max": 0,
                            "dispatch_enqueued_total": 0,
                            "dispatch_handled_total": 0,
                            "dispatch_drop_total": 0,
                            "dispatch_slow_total": 0,
                            "last_dispatch_ms": 0.0,
                            "last_dispatch_slow_ms": 0.0,
                            "last_dispatch_key_tag": "",
                        }

                        def _route_refresh_starvation_state() -> None:
                            try:
                                oldest_age_s = 0.0
                                oldest_key_tag = ""
                                now = time.monotonic()
                                for key0, st0 in pending_tunnel_meta.items():
                                    if not isinstance(st0, dict):
                                        continue
                                    first_at0 = float(st0.get("first_at") or 0.0)
                                    if first_at0 <= 0:
                                        continue
                                    age0 = max(0.0, now - first_at0)
                                    if age0 > oldest_age_s:
                                        oldest_age_s = age0
                                        oldest_key_tag = _key_tag(str(key0))
                                route_diag_state["pending_oldest_age_s"] = round(oldest_age_s, 3)
                                route_diag_state["pending_oldest_key_tag"] = oldest_key_tag
                            except Exception:
                                pass
                            try:
                                route_diag_state["last_nc_pending_data_size"] = int(
                                    getattr(nc, "_pending_data_size", 0) or 0
                                )
                            except Exception:
                                pass
                            try:
                                _route_refresh_guardrail_state()
                            except Exception:
                                pass

                        def _route_refresh_guardrail_state() -> None:
                            try:
                                pending_oldest_age_s = float(route_diag_state.get("pending_oldest_age_s") or 0.0)
                            except Exception:
                                pending_oldest_age_s = 0.0
                            try:
                                pending_data_size = int(route_diag_state.get("last_nc_pending_data_size") or 0)
                            except Exception:
                                pending_data_size = 0
                            active = False
                            reason = ""
                            if _route_guard_pending_data_bytes > 0 and pending_data_size >= _route_guard_pending_data_bytes:
                                active = True
                                reason = "pending_data"
                            elif _route_guard_oldest_age_s > 0.0 and pending_oldest_age_s >= _route_guard_oldest_age_s:
                                active = True
                                reason = "pending_age"
                            was_active = bool(route_diag_state.get("guardrail_active"))
                            previous_reason = str(route_diag_state.get("guardrail_reason") or "")
                            changed = False
                            now_ts = time.time()
                            if active:
                                if not was_active:
                                    route_diag_state["guardrail_activation_total"] = int(
                                        route_diag_state.get("guardrail_activation_total") or 0
                                    ) + 1
                                    route_diag_state["guardrail_since_at"] = now_ts
                                    changed = True
                                elif previous_reason != reason:
                                    route_diag_state["guardrail_since_at"] = now_ts
                                    changed = True
                                route_diag_state["guardrail_active"] = True
                                route_diag_state["guardrail_reason"] = reason
                                since_at = float(route_diag_state.get("guardrail_since_at") or 0.0)
                                route_diag_state["guardrail_age_s"] = (
                                    round(max(0.0, now_ts - since_at), 3) if since_at > 0.0 else 0.0
                                )
                            else:
                                if was_active:
                                    route_diag_state["guardrail_clear_total"] = int(
                                        route_diag_state.get("guardrail_clear_total") or 0
                                    ) + 1
                                    changed = True
                                route_diag_state["guardrail_active"] = False
                                route_diag_state["guardrail_reason"] = ""
                                route_diag_state["guardrail_since_at"] = 0.0
                                route_diag_state["guardrail_age_s"] = 0.0
                            if changed:
                                _rl_log(
                                    "hub-route.guardrail_state",
                                    (
                                        f"[hub-route] guardrail active={active} reason={reason or 'healthy'} "
                                        f"pending_oldest_age_s={pending_oldest_age_s:.3f} "
                                        f"pending_data_size={pending_data_size} "
                                        f"activations={route_diag_state.get('guardrail_activation_total')} "
                                        f"clears={route_diag_state.get('guardrail_clear_total')}"
                                    ),
                                    every_s=0.25,
                                )

                        def _route_note_starvation(
                            reason: str,
                            *,
                            key: str | None = None,
                            extra: str | None = None,
                        ) -> None:
                            try:
                                msg = (
                                    f"[hub-route] starvation reason={reason} "
                                    f"key={_key_tag(key or '')} "
                                    f"pending_oldest_age_s={route_diag_state.get('pending_oldest_age_s')} "
                                    f"pending_oldest_key={route_diag_state.get('pending_oldest_key_tag')} "
                                    f"pending_data_size={route_diag_state.get('last_nc_pending_data_size')}"
                                )
                                if extra:
                                    msg += f" {extra}"
                                _rl_log(f"hub-route.starvation.{reason}", msg, every_s=1.0)
                            except Exception:
                                pass

                        def _update_route_protocol_runtime(**details: Any) -> None:
                            try:
                                _route_refresh_starvation_state()
                                pending_events = 0
                                for items0 in pending_tunnel_events.values():
                                    try:
                                        pending_events += len(items0 or [])
                                    except Exception:
                                        continue
                                active_reader_tasks = 0
                                for task0 in tunnel_tasks.values():
                                    try:
                                        if task0 and not task0.done():
                                            active_reader_tasks += 1
                                    except Exception:
                                        continue
                                observe_hub_root_route_runtime(
                                    active_tunnels=len(tunnels),
                                    active_reader_tasks=active_reader_tasks,
                                    pending_tunnels=len(pending_tunnel_events),
                                    pending_events=pending_events,
                                    pending_chunks=len(pending_chunks),
                                    max_pending_events=MAX_PENDING_TUNNEL_EVENTS,
                                    no_upstream_close_after_s=_route_no_upstream_close_after_s,
                                    legacy_v1_enabled=bool(route_sub is not None),
                                    v2_enabled=bool(route_sub_v2 is not None),
                                    **route_diag_state,
                                    **details,
                                )
                            except Exception:
                                pass

                        def _route_log(msg: str) -> None:
                            if not _hub_channel_console_trace_enabled():
                                return
                            try:
                                print(f"[hub-route:{route_run_id}] {msg}")
                            except Exception:
                                pass

                        def _key_tag(key: str) -> str:
                            try:
                                if not isinstance(key, str):
                                    return "?"
                                return key[-8:] if len(key) > 12 else key
                            except Exception:
                                return "?"

                        def _route_lifecycle_log(
                            phase: str,
                            key: str,
                            *,
                            subject: str | None = None,
                            payload: dict[str, Any] | None = None,
                            extra: str | None = None,
                        ) -> None:
                            try:
                                p0 = payload or {}
                                t0 = str(p0.get("t") or "")
                                should_log = False
                                if t0 in ("http", "http_resp", "open", "open_ack", "close"):
                                    should_log = bool(_route_http_trace or _route_trace)
                                elif t0 in ("frame", "chunk"):
                                    should_log = bool(_route_frame_verbose)
                                elif _route_trace:
                                    should_log = True
                                if not should_log:
                                    return
                                subj0 = str(subject or "").strip()
                                msg = (
                                    f"[hub-route] lifecycle phase={phase} key={_key_tag(key)} "
                                    f"t={t0 or '?'}"
                                )
                                if subj0:
                                    msg += f" subj={subj0}"
                                summary = _route_payload_summary(p0)
                                if summary:
                                    msg += f" {summary}"
                                if extra:
                                    msg += f" {extra}"
                                _route_log(msg)
                            except Exception:
                                pass

                        def _route_payload_summary(payload: dict[str, Any] | None) -> str:
                            try:
                                p0 = payload or {}
                                t0 = str(p0.get("t") or "")
                                if t0 == "http":
                                    m0 = str(p0.get("method") or "GET").upper()
                                    pth0 = str(p0.get("path") or "")
                                    return f"t=http method={m0} path={pth0}"
                                if t0 == "http_resp":
                                    status0 = p0.get("status")
                                    err0 = p0.get("err")
                                    truncated0 = p0.get("truncated")
                                    body0 = p0.get("body_b64")
                                    body_len0 = len(body0) if isinstance(body0, str) else None
                                    return f"t=http_resp status={status0} truncated={truncated0} body_b64_len={body_len0} err={err0}"
                                if t0 == "open":
                                    pth0 = str(p0.get("path") or "")
                                    q0 = str(p0.get("query") or "")
                                    return (
                                        f"t=open path={pth0} query_len={len(q0)} "
                                        f"token={_query_has_token(q0)} dev={_query_param(q0, 'dev')} ws={_query_param(q0, 'ws')}"
                                    )
                                if t0 in ("frame", "chunk"):
                                    kind0 = p0.get("kind")
                                    size0 = None
                                    data0 = p0.get("data") or p0.get("data_b64")
                                    try:
                                        size0 = len(data0) if data0 is not None else None
                                    except Exception:
                                        size0 = None
                                    if t0 == "chunk":
                                        return (
                                            f"t=chunk kind={kind0} idx={p0.get('idx')} total={p0.get('total')} size={size0}"
                                        )
                                    return f"t=frame kind={kind0} size={size0}"
                                if t0 == "close":
                                    return f"t=close err={p0.get('err')}"
                                return f"t={t0}"
                            except Exception:
                                return "t=?"

                        route_key_prefixes: set[str] = set()
                        try:
                            if hub_id:
                                route_key_prefixes.add(f"{hub_id}--")
                            cfg0 = getattr(self.ctx, "config", None)
                            cfg_hub_id = str(getattr(cfg0, "subnet_id", "") or "").strip() if cfg0 is not None else ""
                            if cfg_hub_id:
                                route_key_prefixes.add(f"{cfg_hub_id}--")
                            extra_prefixes = str(os.getenv("HUB_ROUTE_KEY_PREFIXES", "") or "")
                            for item in extra_prefixes.split(","):
                                item = item.strip()
                                if not item:
                                    continue
                                route_key_prefixes.add(item if item.endswith("--") else f"{item}--")
                        except Exception:
                            route_key_prefixes = {f"{hub_id}--"} if hub_id else set()
                        try:
                            route_diag_state["accepted_key_prefixes"] = sorted(route_key_prefixes)
                        except Exception:
                            pass

                        def _query_has_token(query: str) -> bool:
                            if not isinstance(query, str) or not query:
                                return False
                            try:
                                from urllib.parse import parse_qs

                                raw = query[1:] if query.startswith("?") else query
                                q = parse_qs(raw, keep_blank_values=True)
                                return "token" in q
                            except Exception:
                                return "token=" in query

                        def _query_param(query: str, key: str) -> str | None:
                            if not isinstance(query, str) or not query or not key:
                                return None
                            try:
                                from urllib.parse import parse_qs

                                raw = query[1:] if query.startswith("?") else query
                                q = parse_qs(raw, keep_blank_values=True)
                                vals = q.get(key)
                                if isinstance(vals, list) and vals:
                                    v0 = str(vals[0]).strip()
                                    return v0 or None
                                if isinstance(vals, str):
                                    v0 = str(vals).strip()
                                    return v0 or None
                                return None
                            except Exception:
                                return None

                        def _route_payload_bytes(payload: dict[str, Any] | None) -> int | None:
                            try:
                                p0 = payload or {}
                                t0 = str(p0.get("t") or "")
                                kind0 = str(p0.get("kind") or "")
                                if t0 in ("frame", "chunk"):
                                    if kind0 == "bin":
                                        b64 = p0.get("data_b64")
                                        if isinstance(b64, str) and b64:
                                            return len(base64.b64decode(b64.encode("ascii")))
                                    data0 = p0.get("data")
                                    if isinstance(data0, str):
                                        return len(data0.encode("utf-8"))
                                    return None
                                return len(_json.dumps(p0, ensure_ascii=False).encode("utf-8"))
                            except Exception:
                                return None

                        def _route_observe_flow(
                            flow: str,
                            event: str,
                            *,
                            direction: str | None = None,
                            payload: dict[str, Any] | None = None,
                            payload_bytes: int | None = None,
                            error: str | None = None,
                            pending: bool = False,
                        ) -> None:
                            try:
                                size = payload_bytes if payload_bytes is not None else _route_payload_bytes(payload)
                                observe_hub_root_route_flow(
                                    flow,
                                    event,
                                    direction=direction,
                                    payload_bytes=size,
                                    error=error,
                                    pending=pending,
                                )
                            except Exception:
                                pass

                        def _drop_pending_chunks_for_key(key: str) -> None:
                            try:
                                for pid in [pid for pid, st in list(pending_chunks.items()) if st.get("key") == key]:
                                    pending_chunks.pop(pid, None)
                            except Exception:
                                pass

                        def _mark_pending(key: str) -> None:
                            try:
                                st = pending_tunnel_meta.get(key)
                                now = time.monotonic()
                                if st is None:
                                    pending_tunnel_meta[key] = {"first_at": now, "last_at": now, "count": 1}
                                else:
                                    st["last_at"] = now
                                    st["count"] = int(st.get("count") or 0) + 1
                                task = pending_tunnel_close_tasks.get(key)
                                if (
                                    _route_no_upstream_close_after_s > 0
                                    and (task is None or task.done())
                                ):
                                    pending_tunnel_close_tasks[key] = asyncio.create_task(
                                        _pending_tunnel_force_close_task(key),
                                        name=f"hub-route-pending-close-{_key_tag(key)}",
                                    )
                                _route_refresh_starvation_state()
                                age_now = float(route_diag_state.get("pending_oldest_age_s") or 0.0)
                                route_diag_state["last_pending_key_tag"] = _key_tag(key)
                                if age_now >= _route_starvation_warn_s:
                                    route_diag_state["pending_starved_total"] = int(
                                        route_diag_state.get("pending_starved_total") or 0
                                    ) + 1
                                    _route_note_starvation(
                                        "pending_age",
                                        key=key,
                                        extra=f"age_s={age_now:.3f} threshold_s={_route_starvation_warn_s:.3f}",
                                    )
                            except Exception:
                                pass

                        def _cancel_pending_tunnel_close(key: str) -> None:
                            try:
                                task = pending_tunnel_close_tasks.get(key)
                                if not task:
                                    return
                                if task is asyncio.current_task():
                                    pending_tunnel_close_tasks.pop(key, None)
                                    return
                                pending_tunnel_close_tasks.pop(key, None)
                                task.cancel()
                            except Exception:
                                pass

                        def _clear_pending_tunnel_state(key: str, *, drop_events: bool) -> None:
                            try:
                                _cancel_pending_tunnel_close(key)
                            except Exception:
                                pass
                            try:
                                pending_tunnel_meta.pop(key, None)
                            except Exception:
                                pass
                            try:
                                reply_subjects.pop(key, None)
                            except Exception:
                                pass
                            if drop_events:
                                try:
                                    pending_tunnel_events.pop(key, None)
                                except Exception:
                                    pass
                            try:
                                _update_route_protocol_runtime()
                            except Exception:
                                pass

                        async def _reset_route_runtime(*, reason: str, notify_browser: bool) -> dict[str, Any]:
                            nonlocal route_reset_total
                            reason0 = str(reason or "").strip() or "route_reset"
                            notify0 = bool(notify_browser)
                            closed_tunnels = 0
                            dropped_pending = 0
                            notified_browser = 0
                            keys: list[str] = []
                            try:
                                keys = list(
                                    dict.fromkeys(
                                        [
                                            *[str(k) for k in tunnels.keys()],
                                            *[str(k) for k in pending_tunnel_events.keys()],
                                            *[str(k) for k in reply_subjects.keys()],
                                            *[str(k) for k in media_relay_sessions.keys()],
                                            *[
                                                str(st.get("key"))
                                                for st in pending_chunks.values()
                                                if isinstance(st, dict) and st.get("key")
                                            ],
                                        ]
                                    )
                                )
                            except Exception:
                                keys = []
                            for key in keys:
                                rec = tunnels.pop(key, None)
                                ws = rec.get("ws") if isinstance(rec, dict) else None
                                task = tunnel_tasks.pop(key, None)
                                try:
                                    if task:
                                        task.cancel()
                                except Exception:
                                    pass
                                try:
                                    dropped_pending += len(pending_tunnel_events.get(key) or [])
                                except Exception:
                                    pass
                                if notify0 and str(reply_subjects.get(key) or "").strip():
                                    try:
                                        await asyncio.wait_for(
                                            _route_reply(key, {"t": "close", "err": reason0}),
                                            timeout=0.5,
                                        )
                                        notified_browser += 1
                                    except Exception:
                                        pass
                                try:
                                    _drop_pending_chunks_for_key(key)
                                except Exception:
                                    pass
                                try:
                                    _cleanup_media_relay_session(key, remove_temp=True)
                                except Exception:
                                    pass
                                try:
                                    _clear_pending_tunnel_state(key, drop_events=True)
                                except Exception:
                                    pass
                                if ws:
                                    try:
                                        await asyncio.wait_for(ws.close(), timeout=0.5)
                                    except Exception:
                                        pass
                                    closed_tunnels += 1
                            route_reset_total += 1
                            try:
                                _route_observe_flow("control", "runtime_reset", error=reason0)
                            except Exception:
                                pass
                            try:
                                _update_route_protocol_runtime(
                                    last_reset_at=time.time(),
                                    last_reset_reason=reason0,
                                    last_reset_closed_tunnels=closed_tunnels,
                                    last_reset_dropped_pending=dropped_pending,
                                    last_reset_notified_browser=notified_browser,
                                    reset_total=route_reset_total,
                                )
                            except Exception:
                                pass
                            try:
                                note_route_incident(
                                    status="runtime_reset",
                                    summary="hub route relay runtime reset",
                                    details={
                                        "reason": reason0,
                                        "closed_tunnels": closed_tunnels,
                                        "dropped_pending": dropped_pending,
                                        "notified_browser": notified_browser,
                                    },
                                )
                            except Exception:
                                pass
                            if _route_trace or _route_verbose:
                                try:
                                    _route_log(
                                        f"[hub-route] runtime reset reason={reason0} closed={closed_tunnels} "
                                        f"pending={dropped_pending} notified={notified_browser}"
                                    )
                                except Exception:
                                    pass
                            return {
                                "ok": True,
                                "reason": reason0,
                                "notify_browser": notify0,
                                "closed_tunnels": closed_tunnels,
                                "dropped_pending": dropped_pending,
                                "notified_browser": notified_browser,
                                "reset_total": route_reset_total,
                            }

                        async def _maybe_force_close_no_upstream(key: str) -> None:
                            if _route_no_upstream_close_after_s <= 0:
                                return
                            try:
                                st = pending_tunnel_meta.get(key)
                                if not st:
                                    return
                                first_at = float(st.get("first_at") or 0.0)
                                if first_at <= 0:
                                    return
                                age = time.monotonic() - first_at
                                if age < _route_no_upstream_close_after_s:
                                    return
                                rec = tunnels.get(key)
                                ws = rec.get("ws") if isinstance(rec, dict) else None
                                if ws:
                                    _clear_pending_tunnel_state(key, drop_events=False)
                                    return
                                # Ask root to close this tunnel so it re-opens with an "open" handshake.
                                try:
                                    await _route_reply(key, {"t": "close", "err": "no_upstream"})
                                finally:
                                    _clear_pending_tunnel_state(key, drop_events=True)
                                try:
                                    note_route_incident(
                                        status="forced_close_no_upstream",
                                        summary="hub route forced close due to missing upstream",
                                        details={
                                            "key_tag": _key_tag(key),
                                            "age_s": round(float(age), 3),
                                        },
                                    )
                                except Exception:
                                    pass
                                _route_observe_flow(
                                    "control",
                                    "forced_close_no_upstream",
                                    error="no_upstream",
                                )
                                try:
                                    _update_route_protocol_runtime(last_force_close_at=time.time())
                                except Exception:
                                    pass
                                if _route_trace:
                                    _route_log(
                                        f"[hub-route] forced close key={_key_tag(key)} age_s={age:.2f} reason=no_upstream"
                                    )
                            except Exception:
                                pass

                        async def _pending_tunnel_force_close_task(key: str) -> None:
                            try:
                                await asyncio.sleep(_route_no_upstream_close_after_s)
                                await _maybe_force_close_no_upstream(key)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
                            finally:
                                try:
                                    task = pending_tunnel_close_tasks.get(key)
                                    if task is asyncio.current_task():
                                        pending_tunnel_close_tasks.pop(key, None)
                                except Exception:
                                    pass

                        def _route_nc_diag() -> str:
                            try:
                                tr = getattr(nc, "_transport", None)
                                ws = getattr(tr, "_ws", None) if tr is not None else None
                                ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                ws_close_reason = getattr(ws, "close_reason", None) if ws is not None else None
                                ws_exc = None
                                try:
                                    exf = getattr(ws, "exception", None)
                                    if callable(exf):
                                        ws_exc = exf()
                                except Exception:
                                    ws_exc = None
                                ws_proto = None
                                try:
                                    ws_proto = getattr(ws, "protocol", None) if ws is not None else None
                                except Exception:
                                    ws_proto = None
                                try:
                                    if not ws_proto and ws is not None and getattr(ws, "_response", None) is not None:
                                        ws_proto = ws._response.headers.get("Sec-WebSocket-Protocol")  # type: ignore[attr-defined]
                                except Exception:
                                    ws_proto = ws_proto or None

                                last_rx_ago_s = None
                                last_tx_ago_s = None
                                try:
                                    last_rx_at = getattr(tr, "_adaos_last_rx_at", None) if tr is not None else None
                                    last_tx_at = getattr(tr, "_adaos_last_tx_at", None) if tr is not None else None
                                    if isinstance(last_rx_at, (int, float)):
                                        last_rx_ago_s = round(time.monotonic() - float(last_rx_at), 3)
                                    if isinstance(last_tx_at, (int, float)):
                                        last_tx_ago_s = round(time.monotonic() - float(last_tx_at), 3)
                                except Exception:
                                    last_rx_ago_s = last_rx_ago_s or None
                                    last_tx_ago_s = last_tx_ago_s or None
                                last_recv_err = None
                                last_recv_err_ago_s = None
                                try:
                                    last_recv_err = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
                                    last_recv_err_at = getattr(tr, "_adaos_last_recv_error_at", None) if tr is not None else None
                                    if isinstance(last_recv_err_at, (int, float)):
                                        last_recv_err_ago_s = round(time.monotonic() - float(last_recv_err_at), 3)
                                except Exception:
                                    last_recv_err = last_recv_err or None
                                    last_recv_err_ago_s = last_recv_err_ago_s or None

                                pending_data_size = getattr(nc, "_pending_data_size", None)
                                pings_outstanding = getattr(nc, "_pings_outstanding", None)
                                pongs_q = None
                                try:
                                    pongs = getattr(nc, "_pongs", None)
                                    if isinstance(pongs, list):
                                        pongs_q = len(pongs)
                                except Exception:
                                    pongs_q = None
                                return (
                                    f"ws_closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} "
                                    f"ws_exc={ws_exc} ws_proto={ws_proto} "
                                    f"last_rx_ago_s={last_rx_ago_s} last_tx_ago_s={last_tx_ago_s} "
                                    f"last_recv_err={type(last_recv_err).__name__ if last_recv_err is not None else None} "
                                    f"last_recv_err_ago_s={last_recv_err_ago_s} "
                                    f"pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q}"
                                )
                            except Exception:
                                return ""

                        async def _route_reply(key: str, payload: dict[str, Any]) -> None:
                            reply_subject = ""
                            try:
                                reply_subject = str(reply_subjects.get(key) or "")
                            except Exception:
                                reply_subject = ""
                            if not reply_subject:
                                # Prefer v2 subjects by default; legacy v1 is opt-in and explicitly recorded in reply_subjects.
                                reply_subject = f"route.v2.to_browser.{hub_id}.{key}"
                            reply_started = time.monotonic()
                            t0 = None
                            try:
                                t0 = (payload or {}).get("t")
                            except Exception:
                                t0 = None
                            _route_lifecycle_log("reply.start", key, subject=reply_subject, payload=payload)
                            publish_elapsed_s = 0.0
                            try:
                                try:
                                    publish_step_started = time.monotonic()
                                    await asyncio.wait_for(
                                        nc.publish(
                                            reply_subject,
                                            _json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                        ),
                                        timeout=max(0.1, float(_route_send_timeout_s)),
                                    )
                                    publish_elapsed_s = max(0.0, time.monotonic() - publish_step_started)
                                except asyncio.TimeoutError:
                                    raise RuntimeError("publish timeout")
                                if publish_elapsed_s >= _route_publish_slow_warn_s:
                                    route_diag_state["reply_publish_slow_total"] = int(
                                        route_diag_state.get("reply_publish_slow_total") or 0
                                    ) + 1
                                    route_diag_state["last_publish_slow_key_tag"] = _key_tag(key)
                                    route_diag_state["last_publish_slow_ms"] = round(publish_elapsed_s * 1000.0, 1)
                                    _route_refresh_starvation_state()
                                    _route_note_starvation(
                                        "publish_slow",
                                        key=key,
                                        extra=(
                                            f"publish_ms={publish_elapsed_s * 1000.0:.1f} "
                                            f"threshold_ms={_route_publish_slow_warn_s * 1000.0:.1f}"
                                        ),
                                    )
                                try:
                                    observe_hub_root_protocol_publish(
                                        reply_subject,
                                        ok=True,
                                        traffic_class="route",
                                        payload_bytes=len(_json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                                        latency_ms=(time.monotonic() - reply_started) * 1000.0,
                                    )
                                except Exception:
                                    pass
                                if t0 in ("frame", "chunk"):
                                    _route_observe_flow(
                                        "frame",
                                        f"browser_{t0}",
                                        direction="to_browser",
                                        payload=payload,
                                    )
                                elif t0 in ("http_resp", "close", "open_ack"):
                                    _route_observe_flow(
                                        "control",
                                        f"browser_{t0}",
                                        direction="to_browser",
                                        payload=payload,
                                    )
                                took_ms = (time.monotonic() - reply_started) * 1000.0
                                _route_lifecycle_log(
                                    "reply.published",
                                    key,
                                    subject=reply_subject,
                                    payload=payload,
                                    extra=f"took_ms={took_ms:.1f}",
                                )
                                # Ensure the reply is actually flushed quickly; otherwise Root may time out
                                # waiting on `route.to_browser.<key>` (especially over websocket-proxied NATS).
                                t = (payload or {}).get("t")
                                if _route_trace:
                                    try:
                                        if t in ("close", "http_resp") or (_route_frame_verbose and t in ("frame", "chunk")):
                                            status = (payload or {}).get("status")
                                            kind = (payload or {}).get("kind")
                                            size = None
                                            if t == "frame":
                                                data = (payload or {}).get("data") or (payload or {}).get("data_b64")
                                                try:
                                                    size = len(data) if data is not None else None
                                                except Exception:
                                                    size = None
                                            if t == "chunk":
                                                data = (payload or {}).get("data") or (payload or {}).get("data_b64")
                                                try:
                                                    size = len(data) if data is not None else None
                                                except Exception:
                                                    size = None
                                            _route_log(
                                                f"[hub-route] tx t={t} key={_key_tag(key)} status={status} kind={kind} size={size}"
                                            )
                                    except Exception:
                                        pass
                                if _route_force_flush and t in ("http_resp", "close", "open_ack"):
                                    # Fast-drain pending bytes without relying on NATS PING/PONG.
                                    # This avoids `flush()` (which can time out when PONGs are flaky behind WS proxies).
                                    try:
                                        tout = max(0.1, float(_route_flush_timeout_s))
                                    except Exception:
                                        tout = 1.0
                                    flush_err = None
                                    flush_started = time.monotonic()
                                    fp = getattr(nc, "_flush_pending", None)
                                    if callable(fp):
                                        try:
                                            try:
                                                await asyncio.wait_for(fp(force_flush=True), timeout=tout)
                                            except TypeError:
                                                try:
                                                    await asyncio.wait_for(fp(True), timeout=tout)
                                                except TypeError:
                                                    await asyncio.wait_for(fp(), timeout=tout)
                                        except Exception as e:
                                            flush_err = e
                                    else:
                                        # Fallback: old clients might not have `_flush_pending`.
                                        try:
                                            await nc.flush(timeout=tout)
                                        except Exception as e:
                                            flush_err = e
                                    flush_took_s = time.monotonic() - flush_started
                                    if flush_err is not None:
                                        try:
                                            _rl_log(
                                                "hub-route.flush_fail",
                                                f"[hub-route] flush failed t={t} key={key}: {type(flush_err).__name__}: {flush_err} {_route_nc_diag()}",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                    elif flush_took_s >= max(0.5, float(tout) * 0.9):
                                        route_diag_state["reply_flush_slow_total"] = int(
                                            route_diag_state.get("reply_flush_slow_total") or 0
                                        ) + 1
                                        route_diag_state["last_flush_slow_key_tag"] = _key_tag(key)
                                        route_diag_state["last_flush_slow_ms"] = round(flush_took_s * 1000.0, 1)
                                        _route_refresh_starvation_state()
                                        _route_note_starvation(
                                            "flush_slow",
                                            key=key,
                                            extra=f"flush_ms={flush_took_s * 1000.0:.1f} timeout_ms={tout * 1000.0:.1f}",
                                        )
                                    if (
                                        _route_pending_data_warn_bytes > 0
                                        and int(route_diag_state.get("last_nc_pending_data_size") or 0)
                                        >= _route_pending_data_warn_bytes
                                    ):
                                        _route_note_starvation(
                                            "pending_data",
                                            key=key,
                                            extra=f"threshold_bytes={_route_pending_data_warn_bytes}",
                                        )
                                    if _route_tx_verbose:
                                        try:
                                            print(f"[hub-route] tx {t} key={key}")
                                        except Exception:
                                            pass
                                    elif t in ("http_resp", "close", "open_ack"):
                                        _route_lifecycle_log(
                                            "reply.flushed",
                                            key,
                                            subject=reply_subject,
                                            payload=payload,
                                            extra=f"flush_ms={flush_took_s * 1000.0:.1f}",
                                        )
                                try:
                                    _update_route_protocol_runtime()
                                except Exception:
                                    pass
                            except Exception as e:
                                try:
                                    observe_hub_root_protocol_publish(
                                        reply_subject,
                                        ok=False,
                                        traffic_class="route",
                                        payload_bytes=len(_json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                                        error=f"{type(e).__name__}: {e}",
                                    )
                                except Exception:
                                    pass
                                if t0 in ("frame", "chunk"):
                                    _route_observe_flow(
                                        "frame",
                                        f"{t0}_publish_fail",
                                        payload=payload,
                                        error=str(e),
                                    )
                                elif t0 in ("http_resp", "close", "open_ack"):
                                    _route_observe_flow(
                                        "control",
                                        f"{t0}_publish_fail",
                                        payload=payload,
                                        error=str(e),
                                    )
                                route_diag_state["reply_publish_fail_total"] = int(
                                    route_diag_state.get("reply_publish_fail_total") or 0
                                ) + 1
                                try:
                                    _update_route_protocol_runtime(last_publish_fail_at=time.time())
                                except Exception:
                                    pass
                                # Do not silently drop probe replies: Root will time out and surface `hub_unreachable`.
                                if t0 in ("http_resp", "close") or _route_verbose:
                                    try:
                                        _rl_log(
                                            "hub-route.publish_fail",
                                            f"[hub-route] publish to_browser failed t={t0} key={key}: {type(e).__name__}: {e} {_route_nc_diag()}",
                                            every_s=1.0,
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        note_route_incident(
                                            status="publish_fail",
                                            summary="hub route reply publish failed",
                                            details={
                                                "t": t0,
                                                "key_tag": _key_tag(key),
                                                "reply_subject": reply_subject,
                                                "err_type": type(e).__name__,
                                                "err": str(e),
                                            },
                                        )
                                    except Exception:
                                        pass
                                _route_lifecycle_log(
                                    "reply.fail",
                                    key,
                                    subject=reply_subject,
                                    payload=payload,
                                    extra=f"err={type(e).__name__}: {e} {_route_nc_diag()}",
                                )

                        def _cleanup_media_relay_session(key: str, *, remove_temp: bool) -> None:
                            session = media_relay_sessions.pop(key, None)
                            if not isinstance(session, dict):
                                return
                            handle = session.get("handle")
                            try:
                                if handle:
                                    handle.close()
                            except Exception:
                                pass
                            if remove_temp:
                                tmp_path = session.get("tmp_path")
                                try:
                                    if tmp_path is not None:
                                        Path(tmp_path).unlink(missing_ok=True)
                                except Exception:
                                    pass

                        async def _route_media_reply_json(
                            key: str,
                            *,
                            status: int,
                            payload: dict[str, Any],
                        ) -> None:
                            raw = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
                            await _route_reply(
                                key,
                                {
                                    "t": "media_http_meta",
                                    "status": int(status),
                                    "headers": {
                                        "content-type": "application/json",
                                        "content-length": str(len(raw)),
                                    },
                                },
                            )
                            idx0 = 0
                            for off in range(0, len(raw), 256 * 1024):
                                part = raw[off : off + (256 * 1024)]
                                await _route_reply(
                                    key,
                                    {
                                        "t": "media_http_chunk",
                                        "idx": idx0,
                                        "data_b64": base64.b64encode(bytes(part)).decode("ascii"),
                                    },
                                )
                                idx0 += 1
                            await _route_reply(
                                key,
                                {
                                    "t": "media_http_end",
                                    "total_bytes": len(raw),
                                    "truncated": False,
                                },
                            )

                        def _parse_media_range(range_header: str | None, size_bytes: int) -> tuple[int, int] | None:
                            raw = str(range_header or "").strip()
                            if not raw.lower().startswith("bytes="):
                                return None
                            spec = raw[6:].strip()
                            if not spec or "," in spec:
                                return None
                            start_s, _sep, end_s = spec.partition("-")
                            if not _sep:
                                return None
                            try:
                                if start_s and end_s:
                                    start = int(start_s)
                                    end = int(end_s)
                                elif start_s:
                                    start = int(start_s)
                                    end = size_bytes - 1
                                elif end_s:
                                    suffix_len = int(end_s)
                                    if suffix_len <= 0:
                                        return None
                                    start = max(0, size_bytes - suffix_len)
                                    end = size_bytes - 1
                                else:
                                    return None
                            except Exception:
                                return None
                            if start < 0 or end < start or start >= size_bytes:
                                return None
                            end = min(end, size_bytes - 1)
                            return (start, end)

                        async def _route_media_reply_file(
                            key: str,
                            *,
                            target: Path,
                            method: str,
                            request_headers: dict[str, Any] | None,
                        ) -> None:
                            from adaos.services.media_library import ROOT_MEDIA_RELAY_CHUNK_BYTES, guess_media_type

                            stat = target.stat()
                            total_size = int(stat.st_size)
                            headers_in = request_headers if isinstance(request_headers, dict) else {}
                            range_header = str(headers_in.get("range") or headers_in.get("Range") or "").strip()
                            range_spec = _parse_media_range(range_header or None, total_size)
                            if range_header and range_spec is None:
                                await _route_reply(
                                    key,
                                    {
                                        "t": "media_http_meta",
                                        "status": 416,
                                        "headers": {
                                            "content-range": f"bytes */{total_size}",
                                            "content-length": "0",
                                        },
                                    },
                                )
                                await _route_reply(key, {"t": "media_http_end", "total_bytes": 0, "truncated": False})
                                return

                            start = 0
                            end = total_size - 1
                            status = 200
                            if range_spec is not None:
                                start, end = range_spec
                                status = 206
                            length = max(0, end - start + 1)
                            headers = {
                                "content-type": guess_media_type(target.name),
                                "content-length": str(length),
                                "accept-ranges": "bytes",
                                "content-disposition": f'inline; filename="{target.name}"',
                            }
                            if status == 206:
                                headers["content-range"] = f"bytes {start}-{end}/{total_size}"
                            await _route_reply(
                                key,
                                {
                                    "t": "media_http_meta",
                                    "status": status,
                                    "headers": headers,
                                },
                            )
                            if str(method or "").upper() == "HEAD" or length <= 0:
                                await _route_reply(key, {"t": "media_http_end", "total_bytes": 0, "truncated": False})
                                return

                            sent = 0
                            with target.open("rb") as handle:
                                handle.seek(start)
                                idx0 = 0
                                remaining = length
                                while remaining > 0:
                                    blob = handle.read(min(int(ROOT_MEDIA_RELAY_CHUNK_BYTES), remaining))
                                    if not blob:
                                        break
                                    await _route_reply(
                                        key,
                                        {
                                            "t": "media_http_chunk",
                                            "idx": idx0,
                                            "data_b64": base64.b64encode(blob).decode("ascii"),
                                        },
                                    )
                                    idx0 += 1
                                    sent += len(blob)
                                    remaining -= len(blob)
                            await _route_reply(
                                key,
                                {
                                    "t": "media_http_end",
                                    "total_bytes": sent,
                                    "truncated": False,
                                },
                            )

                        try:
                            self._hub_root_route_reset = _reset_route_runtime
                        except Exception:
                            pass

                        def _hub_key_match(key: str) -> bool:
                            try:
                                if not isinstance(key, str) or not key:
                                    return False
                                prefixes = route_key_prefixes or ({f"{hub_id}--"} if hub_id else set())
                                return any(key.startswith(prefix) for prefix in prefixes if prefix)
                            except Exception:
                                return False

                        async def _tunnel_reader(key: str, ws) -> None:
                            try:
                                async for msg in ws:
                                    if _route_frame_verbose:
                                        try:
                                            if isinstance(msg, (bytes, bytearray)):
                                                _route_log(f"[hub-route] rx upstream frame key={_key_tag(key)} kind=bin size={len(msg)}")
                                            else:
                                                _route_log(
                                                    f"[hub-route] rx upstream frame key={_key_tag(key)} kind=text size={len(str(msg))}"
                                                )
                                        except Exception:
                                            pass
                                    if isinstance(msg, (bytes, bytearray)):
                                        raw = bytes(msg)
                                        if len(raw) > MAX_CHUNK_RAW:
                                            cid = f"c_{uuid.uuid4().hex}"
                                            total = (len(raw) + MAX_CHUNK_RAW - 1) // MAX_CHUNK_RAW
                                            for idx in range(total):
                                                chunk = raw[idx * MAX_CHUNK_RAW : (idx + 1) * MAX_CHUNK_RAW]
                                                await _route_reply(
                                                    key,
                                                    {
                                                        "t": "chunk",
                                                        "id": cid,
                                                        "kind": "bin",
                                                        "idx": idx,
                                                        "total": total,
                                                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                                                    },
                                                )
                                        else:
                                            await _route_reply(
                                                key,
                                                {
                                                    "t": "frame",
                                                    "kind": "bin",
                                                    "data_b64": base64.b64encode(raw).decode("ascii"),
                                                },
                                            )
                                    else:
                                        text = str(msg)
                                        if len(text) > MAX_CHUNK_RAW:
                                            cid = f"c_{uuid.uuid4().hex}"
                                            parts = [text[i : i + MAX_CHUNK_RAW] for i in range(0, len(text), MAX_CHUNK_RAW)]
                                            for idx, part in enumerate(parts):
                                                await _route_reply(
                                                    key,
                                                    {"t": "chunk", "id": cid, "kind": "text", "idx": idx, "total": len(parts), "data": part},
                                                )
                                        else:
                                            await _route_reply(key, {"t": "frame", "kind": "text", "data": text})
                            except Exception as e:
                                if _route_trace:
                                    try:
                                        _route_log(
                                            f"[hub-route] upstream reader error key={_key_tag(key)} err={type(e).__name__}: {e}"
                                        )
                                    except Exception:
                                        pass
                            finally:
                                if _route_trace:
                                    try:
                                        code = getattr(ws, "close_code", None)
                                        reason = getattr(ws, "close_reason", None)
                                        exc = None
                                        try:
                                            exf = getattr(ws, "exception", None)
                                            if callable(exf):
                                                exc = exf()
                                        except Exception:
                                            exc = None
                                        _route_log(
                                            f"[hub-route] upstream closed key={_key_tag(key)} code={code} reason={reason} exc={exc}"
                                        )
                                    except Exception:
                                        pass
                                _route_observe_flow("control", "upstream_closed")
                                try:
                                    await _route_reply(key, {"t": "close"})
                                except Exception:
                                    pass
                                tunnels.pop(key, None)
                                t = tunnel_tasks.pop(key, None)
                                try:
                                    if t:
                                        t.cancel()
                                except Exception:
                                    pass
                                try:
                                    _drop_pending_chunks_for_key(key)
                                except Exception:
                                    pass
                                try:
                                    _clear_pending_tunnel_state(key, drop_events=True)
                                except Exception:
                                    pass
                                try:
                                    await ws.close()
                                except Exception:
                                    pass
                                try:
                                    _update_route_protocol_runtime()
                                except Exception:
                                    pass

                        def _queue_pending_tunnel_event(key: str, payload: dict[str, Any]) -> None:
                            try:
                                items = pending_tunnel_events.get(key)
                                if items is None:
                                    items = []
                                    pending_tunnel_events[key] = items
                                if len(items) >= MAX_PENDING_TUNNEL_EVENTS:
                                    items.pop(0)
                                items.append(dict(payload))
                            except Exception:
                                pass
                            try:
                                _update_route_protocol_runtime()
                            except Exception:
                                pass

                        async def _send_tunnel_event(key: str, ws, payload: dict[str, Any]) -> None:
                            kind = (payload or {}).get("t")
                            if kind == "frame":
                                frame_kind = (payload or {}).get("kind")
                                if frame_kind == "bin":
                                    b64 = (payload or {}).get("data_b64")
                                    if isinstance(b64, str) and b64:
                                        raw = base64.b64decode(b64.encode("ascii"))
                                        await asyncio.wait_for(
                                            ws.send(raw),
                                            timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                        )
                                        _route_observe_flow(
                                            "frame",
                                            "frame_upstream_sent",
                                            direction="to_upstream",
                                            payload_bytes=len(raw),
                                        )
                                else:
                                    txt = (payload or {}).get("data")
                                    if isinstance(txt, str):
                                        await asyncio.wait_for(
                                            ws.send(txt),
                                            timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                        )
                                        _route_observe_flow(
                                            "frame",
                                            "frame_upstream_sent",
                                            direction="to_upstream",
                                            payload_bytes=len(txt.encode("utf-8")),
                                        )
                                return

                            if kind != "chunk":
                                return

                            cid = (payload or {}).get("id")
                            idx = int((payload or {}).get("idx") or 0)
                            total = int((payload or {}).get("total") or 0)
                            frame_kind = "text" if (payload or {}).get("kind") == "text" else "bin"
                            if not isinstance(cid, str) or not cid or total <= 0 or idx < 0 or idx >= total:
                                return
                            st = pending_chunks.get(cid)
                            if not st:
                                st = {"key": key, "kind": frame_kind, "total": total, "parts": [None] * total}
                                pending_chunks[cid] = st
                            if st.get("key") != key or st.get("kind") != frame_kind or int(st.get("total") or 0) != total:
                                return
                            parts = st.get("parts")
                            if not isinstance(parts, list) or len(parts) != total:
                                st["parts"] = [None] * total
                                parts = st["parts"]
                            if frame_kind == "bin":
                                b64 = (payload or {}).get("data_b64")
                                if not isinstance(b64, str):
                                    return
                                parts[idx] = base64.b64decode(b64.encode("ascii"))
                            else:
                                txt = (payload or {}).get("data")
                                if not isinstance(txt, str):
                                    return
                                parts[idx] = txt
                            if any(p is None for p in parts):
                                return
                            pending_chunks.pop(cid, None)
                            if frame_kind == "bin":
                                blob = b"".join([p for p in parts if isinstance(p, (bytes, bytearray))])
                                await asyncio.wait_for(
                                    ws.send(blob),
                                    timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                )
                                _route_observe_flow(
                                    "frame",
                                    "chunk_upstream_sent",
                                    direction="to_upstream",
                                    payload_bytes=len(blob),
                                )
                            else:
                                text_blob = "".join([p for p in parts if isinstance(p, str)])
                                await asyncio.wait_for(
                                    ws.send(text_blob),
                                    timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                )
                                _route_observe_flow(
                                    "frame",
                                    "chunk_upstream_sent",
                                    direction="to_upstream",
                                    payload_bytes=len(text_blob.encode("utf-8")),
                                )

                        async def _route_handle_msg(msg) -> None:
                            key = ""
                            subject = ""
                            is_http_key = False
                            route_t = "?"
                            route_outcome = "start"
                            route_started = time.monotonic()
                            http_method = ""
                            http_path = ""
                            http_kind = ""
                            try:
                                subject = str(getattr(msg, "subject", "") or "")
                                # Legacy v1: route.to_hub.<key>
                                # v2: route.v2.to_hub.<hubId>.<key>
                                parts = subject.split(".")
                                if subject.startswith("route.v2.to_hub."):
                                    if len(parts) < 5:
                                        route_outcome = "drop_bad_subject"
                                        if _route_diag:
                                            try:
                                                _rl_log(
                                                    "hub-route.drop_subject",
                                                    f"[hub-route] drop: bad subject={subject!s}",
                                                    every_s=2.0,
                                                )
                                            except Exception:
                                                pass
                                        return
                                    subj_hub_id = str(parts[3] or "")
                                    if subj_hub_id and subj_hub_id != hub_id:
                                        route_outcome = "drop_hub_mismatch"
                                        if _route_diag:
                                            try:
                                                _rl_log(
                                                    "hub-route.drop_hub",
                                                    f"[hub-route] drop: hub mismatch subject={subject!s} hub={subj_hub_id!s} local={hub_id!s}",
                                                    every_s=2.0,
                                                )
                                            except Exception:
                                                pass
                                        return
                                    key = str(parts[4] or "")
                                    if key:
                                        try:
                                            reply_subjects[key] = f"route.v2.to_browser.{hub_id}.{key}"
                                        except Exception:
                                            pass
                                else:
                                    # route.to_hub.<key>
                                    if len(parts) < 3:
                                        route_outcome = "drop_bad_subject"
                                        if _route_diag:
                                            try:
                                                _rl_log(
                                                    "hub-route.drop_subject",
                                                    f"[hub-route] drop: bad subject={subject!s}",
                                                    every_s=2.0,
                                                )
                                            except Exception:
                                                pass
                                        return
                                    key = str(parts[2] or "")
                                    if key:
                                        try:
                                            reply_subjects[key] = f"route.to_browser.{key}"
                                        except Exception:
                                            pass

                                if not key:
                                    route_outcome = "drop_bad_subject"
                                    return
                                is_http_key = isinstance(key, str) and "--http--" in key
                                if not _hub_key_match(key):
                                    route_outcome = "drop_key_mismatch"
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_key",
                                                f"[hub-route] drop: key mismatch subject={subject!s} key={key!s} expected_prefixes={sorted(route_key_prefixes) or [f'{hub_id}--']}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    return

                                try:
                                    raw = bytes(getattr(msg, "data", b"") or b"")
                                except Exception:
                                    raw = b""
                                try:
                                    data = _json.loads(raw.decode("utf-8"))
                                except Exception as e:
                                    route_outcome = "drop_invalid_json"
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_json",
                                                f"[hub-route] drop: invalid json key={key} bytes={len(raw)} err={type(e).__name__}: {e}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    # Avoid systematic `hub_unreachable` timeouts for HTTP keys.
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "invalid_json"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                if not isinstance(data, dict):
                                    route_outcome = "drop_invalid_payload"
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_payload",
                                                f"[hub-route] drop: unexpected payload type key={key} type={type(data).__name__}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "invalid_payload"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                t = (data or {}).get("t")
                                route_t = str(t or "?")
                                if not isinstance(t, str) or not t:
                                    route_outcome = "drop_missing_t"
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_missing_t",
                                                f"[hub-route] drop: missing t key={key}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "missing_t"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                if t == "http":
                                    route_diag_state["http_request_total"] = int(route_diag_state.get("http_request_total") or 0) + 1
                                    route_diag_state["last_http_path"] = str((data or {}).get("path") or "")
                                    route_diag_state["last_http_method"] = str((data or {}).get("method") or "GET").upper()
                                    _update_route_protocol_runtime()
                                    try:
                                        http_method = str((data or {}).get("method") or "GET").upper()
                                        http_path = str((data or {}).get("path") or "")
                                        http_kind = (
                                            "probe"
                                            if http_path in ("/api/node/status", "/api/ping", "/healthz")
                                            else "app"
                                        )
                                        observe_route_e2e(
                                            details={
                                                f"last_http_{http_kind}_rx_at": time.time(),
                                                "last_http_rx_path": http_path,
                                                "last_http_rx_method": http_method,
                                                "last_http_rx_key_tag": _key_tag(key),
                                            }
                                        )
                                    except Exception:
                                        pass
                                if _route_http_trace and (is_http_key or t in ("open", "close")):
                                    _route_lifecycle_log(
                                        "request.rx",
                                        key,
                                        subject=subject,
                                        payload=data,
                                        extra=f"bytes={len(raw)}",
                                    )
                                if _route_verbose or _route_trace:
                                    try:
                                        if t == "http":
                                            _m = http_method or "GET"
                                            _p = http_path or ""
                                            if _p not in ("/api/node/status", "/api/ping", "/healthz"):
                                                _route_log(f"[hub-route] rx http key={_key_tag(key)} {_m} {_p}")
                                            else:
                                                try:
                                                    _rl_log(
                                                        "hub-route.rx_http_probe",
                                                        f"[hub-route] rx http probe key={key} {_m} {_p}",
                                                        every_s=5.0,
                                                    )
                                                except Exception:
                                                    pass
                                        elif t == "open":
                                            _p = str((data or {}).get("path") or "")
                                            if _p not in ("/api/node/status", "/api/ping"):
                                                _route_log(f"[hub-route] rx open key={_key_tag(key)} path={_p}")
                                        elif t == "close":
                                            _route_log(f"[hub-route] rx close key={_key_tag(key)}")
                                        else:
                                            # Frames are extremely noisy; enable explicitly when debugging.
                                            if t == "frame" and not _route_frame_verbose:
                                                pass
                                            else:
                                                _route_log(f"[hub-route] rx t={t} key={_key_tag(key)}")
                                    except Exception:
                                        pass

                                    if _route_trace:
                                        try:
                                            if t == "open":
                                                _p = str((data or {}).get("path") or "")
                                                _q = str((data or {}).get("query") or "")
                                                _dev = _query_param(_q, "dev")
                                                _wsq = _query_param(_q, "ws")
                                                _route_log(
                                                    f"[hub-route] open req key={_key_tag(key)} path={_p} query_len={len(_q)} token={_query_has_token(_q)} dev={_dev} ws={_wsq}"
                                                )
                                            elif t == "frame":
                                                _kind = (data or {}).get("kind")
                                                _size = None
                                                _body = (data or {}).get("data") or (data or {}).get("data_b64")
                                                try:
                                                    _size = len(_body) if _body is not None else None
                                                except Exception:
                                                    _size = None
                                                if _route_frame_verbose:
                                                    _route_log(
                                                        f"[hub-route] frame req key={_key_tag(key)} kind={_kind} size={_size}"
                                                    )
                                            elif t == "chunk":
                                                if _route_frame_verbose:
                                                    _route_log(
                                                        f"[hub-route] chunk req key={_key_tag(key)} idx={(data or {}).get('idx')} total={(data or {}).get('total')}"
                                                    )
                                            elif t == "close":
                                                _route_log(f"[hub-route] close req key={_key_tag(key)}")
                                        except Exception:
                                            pass

                                if t == "open":
                                    route_outcome = "open"
                                    _route_observe_flow("control", "open_request", payload=data)
                                    # Open a local WS to the hub server and start pumping frames.
                                    if websockets_mod is None:
                                        route_outcome = "open_no_websockets"
                                        _clear_pending_tunnel_state(key, drop_events=True)
                                        if _route_trace:
                                            _route_log(f"[hub-route] open upstream failed key={_key_tag(key)} err=websockets_unavailable")
                                        _route_observe_flow(
                                            "control",
                                            "open_connect_fail",
                                            payload=data,
                                            error="websockets_unavailable",
                                        )
                                        await _route_reply(key, {"t": "close", "err": "websockets_unavailable"})
                                        return
                                    path = str((data or {}).get("path") or "/ws")
                                    query = str((data or {}).get("query") or "")
                                    route_diag_state["open_request_total"] = int(route_diag_state.get("open_request_total") or 0) + 1
                                    route_diag_state["last_open_path"] = path
                                    route_diag_state["last_open_query_has_token"] = bool(_query_has_token(query))
                                    # Local hub server is always reachable inside the hub machine/container.
                                    try:
                                        from adaos.services.node_config import load_config

                                        cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                        ws_bases = _build_hub_route_ws_bases(cfg=cfg, path=path, ctx=self.ctx)
                                        token_local = getattr(cfg, "token", None) or os.getenv("ADAOS_TOKEN", "") or None
                                    except Exception:
                                        ws_bases = _build_hub_route_ws_bases(cfg=None, path=path, ctx=self.ctx)
                                        token_local = os.getenv("ADAOS_TOKEN", "") or None
                                    route_diag_state["last_open_base_total"] = len(list(ws_bases or []))
                                    _update_route_protocol_runtime()
                                    # Translate root-proxy JWT token into local hub token for upstream hub WS auth.
                                    # Local hub expects `token=<X-AdaOS-Token>`; forwarding the session JWT makes the
                                    # hub close immediately and the browser retries endlessly.
                                    try:
                                        from urllib.parse import parse_qs, urlencode

                                        if query.startswith("?"):
                                            q = parse_qs(query[1:], keep_blank_values=True)
                                        else:
                                            q = parse_qs(query, keep_blank_values=True)
                                        if token_local:
                                            q["token"] = [str(token_local)]
                                        else:
                                            # If we don't have a local token, do not forward the root session JWT.
                                            q.pop("token", None)
                                        query = "?" + urlencode(q, doseq=True) if q else ""
                                    except Exception:
                                        pass
                                    # Ensure we don't leak multiple opens for same key.
                                    try:
                                        old = tunnels.get(key)
                                        if old and old.get("ws"):
                                            try:
                                                await old["ws"].close()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    try:
                                        # Yjs sync frames can exceed 1 MiB; do not enforce a small client-side cap.
                                        try:
                                            ws_connect_timeout_s = float(
                                                os.getenv("HUB_ROUTE_UPSTREAM_WS_CONNECT_TIMEOUT_S", "2.5") or "2.5"
                                            )
                                        except Exception:
                                            ws_connect_timeout_s = 2.5
                                        if ws_connect_timeout_s < 0.1:
                                            ws_connect_timeout_s = 0.1
                                        if _route_trace:
                                            _route_log(
                                                f"[hub-route] upstream.connect start key={_key_tag(key)} timeout_s={ws_connect_timeout_s}"
                                            )
                                        ws = None
                                        last_exc = None
                                        for base_ws in ws_bases:
                                            url = f"{base_ws}{path}{query}"
                                            if _route_verbose or _route_trace:
                                                try:
                                                    _route_log(f"[hub-route] open upstream url={url}")
                                                except Exception:
                                                    pass
                                            t0 = time.monotonic()
                                            try:
                                                ws = await asyncio.wait_for(
                                                    websockets_mod.connect(url, max_size=None),
                                                    timeout=ws_connect_timeout_s,
                                                )
                                                if _route_trace:
                                                    took = time.monotonic() - t0
                                                    proto = getattr(ws, "subprotocol", None) or getattr(ws, "protocol", None)
                                                    remote = getattr(ws, "remote_address", None)
                                                    _route_log(
                                                        f"[hub-route] upstream.connect ok key={_key_tag(key)} took_s={took:.3f} proto={proto} remote={remote}"
                                                    )
                                                break
                                            except Exception as exc:
                                                last_exc = exc
                                                if _route_trace:
                                                    try:
                                                        _route_log(
                                                            f"[hub-route] upstream.connect retry key={_key_tag(key)} url={url} err={type(exc).__name__}: {exc}"
                                                        )
                                                    except Exception:
                                                        pass
                                        if ws is None:
                                            raise last_exc or RuntimeError("hub route websocket upstream failed")
                                    except Exception as e:
                                        route_outcome = f"open_connect_fail:{type(e).__name__}"
                                        _clear_pending_tunnel_state(key, drop_events=True)
                                        if _route_trace:
                                            _route_log(
                                                f"[hub-route] upstream.connect fail key={_key_tag(key)} err={type(e).__name__}: {e}"
                                            )
                                        _route_observe_flow(
                                            "control",
                                            "open_connect_fail",
                                            payload=data,
                                            error=str(e),
                                        )
                                        await _route_reply(key, {"t": "close", "err": str(e)})
                                        return
                                    route_outcome = "open_connected"
                                    tunnels[key] = {"ws": ws, "url": url}
                                    _clear_pending_tunnel_state(key, drop_events=False)
                                    tunnel_tasks[key] = asyncio.create_task(_tunnel_reader(key, ws), name=f"hub-route-{key}")
                                    try:
                                        await _route_reply(key, {"t": "open_ack"})
                                    except Exception:
                                        pass
                                    pending = pending_tunnel_events.pop(key, None) or []
                                    for pending_payload in pending:
                                        try:
                                            await _send_tunnel_event(key, ws, pending_payload)
                                        except Exception as e:
                                            if _route_verbose or _route_trace:
                                                try:
                                                    _route_log(
                                                        f"[hub-route] flush pending failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                    )
                                                except Exception:
                                                    pass
                                            break
                                    try:
                                        _update_route_protocol_runtime()
                                    except Exception:
                                        pass
                                    _route_observe_flow("control", "open_ready", payload=data)
                                    route_outcome = "open_ready"
                                    return

                                if t == "close":
                                    route_outcome = "close_local"
                                    _route_observe_flow("control", "close_local", payload=data)
                                    rec = tunnels.pop(key, None)
                                    task = tunnel_tasks.pop(key, None)
                                    _clear_pending_tunnel_state(key, drop_events=True)
                                    try:
                                        if task:
                                            task.cancel()
                                    except Exception:
                                        pass
                                    try:
                                        _update_route_protocol_runtime()
                                    except Exception:
                                        pass
                                    try:
                                        if rec and rec.get("ws"):
                                            await rec["ws"].close()
                                    except Exception:
                                        pass
                                    if _route_trace:
                                        _route_log(f"[hub-route] upstream close req key={_key_tag(key)}")
                                    return

                                if t == "frame":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        route_outcome = "frame_no_upstream"
                                        _route_observe_flow(
                                            "frame",
                                            "frame_no_upstream",
                                            payload=data,
                                            error="no_upstream",
                                            pending=True,
                                        )
                                        _queue_pending_tunnel_event(key, data)
                                        _mark_pending(key)
                                        try:
                                            st = pending_tunnel_meta.get(key) or {}
                                            count = int(st.get("count") or 0)
                                            if count <= 1:
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = round(time.monotonic() - first_at, 3) if first_at > 0 else None
                                                note_route_incident(
                                                    status="no_upstream",
                                                    summary="hub route frame arrived while upstream is not connected",
                                                    details={"key_tag": _key_tag(key), "age_s": age_s, "t": "frame"},
                                                )
                                        except Exception:
                                            pass
                                        try:
                                            _update_route_protocol_runtime(last_no_upstream_at=time.time())
                                        except Exception:
                                            pass
                                        await _maybe_force_close_no_upstream(key)
                                        if _route_trace:
                                            try:
                                                st = pending_tunnel_meta.get(key) or {}
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = time.monotonic() - first_at if first_at > 0 else None
                                                count = st.get("count")
                                            except Exception:
                                                age_s = None
                                                count = None
                                            _route_log(
                                                f"[hub-route] queue frame key={_key_tag(key)} reason=no_upstream age_s={age_s} count={count}"
                                            )
                                        return
                                    try:
                                        await _send_tunnel_event(key, ws, data)
                                        route_outcome = "frame_sent"
                                    except Exception as e:
                                        route_outcome = f"frame_send_fail:{type(e).__name__}"
                                        _route_observe_flow(
                                            "frame",
                                            "frame_send_fail",
                                            payload=data,
                                            error=str(e),
                                        )
                                        if _route_verbose or _route_trace:
                                            try:
                                                _route_log(
                                                    f"[hub-route] ws.send(frame) failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                )
                                            except Exception:
                                                pass
                                    return
                                
                                if t == "chunk":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        route_outcome = "chunk_no_upstream"
                                        _route_observe_flow(
                                            "frame",
                                            "chunk_no_upstream",
                                            payload=data,
                                            error="no_upstream",
                                            pending=True,
                                        )
                                        _queue_pending_tunnel_event(key, data)
                                        _mark_pending(key)
                                        try:
                                            st = pending_tunnel_meta.get(key) or {}
                                            count = int(st.get("count") or 0)
                                            if count <= 1:
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = round(time.monotonic() - first_at, 3) if first_at > 0 else None
                                                note_route_incident(
                                                    status="no_upstream",
                                                    summary="hub route chunk arrived while upstream is not connected",
                                                    details={"key_tag": _key_tag(key), "age_s": age_s, "t": "chunk"},
                                                )
                                        except Exception:
                                            pass
                                        try:
                                            _update_route_protocol_runtime(last_no_upstream_at=time.time())
                                        except Exception:
                                            pass
                                        await _maybe_force_close_no_upstream(key)
                                        if _route_trace:
                                            try:
                                                st = pending_tunnel_meta.get(key) or {}
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = time.monotonic() - first_at if first_at > 0 else None
                                                count = st.get("count")
                                            except Exception:
                                                age_s = None
                                                count = None
                                            _route_log(
                                                f"[hub-route] queue chunk key={_key_tag(key)} reason=no_upstream age_s={age_s} count={count}"
                                            )
                                        return
                                    try:
                                        await _send_tunnel_event(key, ws, data)
                                        route_outcome = "chunk_sent"
                                    except Exception as e:
                                        route_outcome = f"chunk_send_fail:{type(e).__name__}"
                                        _route_observe_flow(
                                            "frame",
                                            "chunk_send_fail",
                                            payload=data,
                                            error=str(e),
                                        )
                                        if _route_verbose or _route_trace:
                                            try:
                                                _route_log(
                                                    f"[hub-route] ws.send(chunked) failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                )
                                            except Exception:
                                                pass
                                    return

                                if t == "media_http_open":
                                    route_outcome = "media_http_open"
                                    try:
                                        from urllib.parse import unquote

                                        from adaos.services.media_library import (
                                            ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
                                            ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
                                            guess_media_type,
                                            list_media_files,
                                            media_capabilities,
                                            media_file_path,
                                            media_runtime_snapshot,
                                            media_snapshot,
                                        )

                                        method = str((data or {}).get("method") or "GET").upper()
                                        path = str((data or {}).get("path") or "/media/files")
                                        path_norm = (path.rstrip("/") or "/") if isinstance(path, str) else "/"
                                        headers = (data or {}).get("headers") if isinstance((data or {}).get("headers"), dict) else {}
                                        content_length = int((data or {}).get("content_length") or 0)

                                        if method in ("GET", "HEAD") and path_norm == "/media/files":
                                            payload0 = media_snapshot()
                                            payload0["proxy_limits"] = {
                                                "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
                                                "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
                                            }
                                            await _route_media_reply_json(key, status=200, payload=payload0)
                                            route_outcome = "media_files_replied"
                                            return

                                        if method in ("GET", "HEAD") and path_norm == "/media/runtime":
                                            runtime0 = media_runtime_snapshot()
                                            runtime0["ok"] = True
                                            runtime0["proxy_limits"] = {
                                                "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
                                                "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
                                            }
                                            runtime0["capabilities"] = media_capabilities()
                                            runtime0["files"] = {
                                                "items": list_media_files(),
                                            }
                                            await _route_media_reply_json(key, status=200, payload=runtime0)
                                            route_outcome = "media_runtime_replied"
                                            return

                                        if method in ("GET", "HEAD") and path_norm.startswith("/media/files/content/"):
                                            filename = unquote(path_norm[len("/media/files/content/"):])
                                            try:
                                                target = media_file_path(filename)
                                            except ValueError as exc:
                                                await _route_media_reply_json(
                                                    key,
                                                    status=400,
                                                    payload={"ok": False, "detail": str(exc)},
                                                )
                                                route_outcome = "media_content_bad_request"
                                                return
                                            if not target.exists() or not target.is_file():
                                                await _route_media_reply_json(
                                                    key,
                                                    status=404,
                                                    payload={"ok": False, "detail": "media_file_not_found"},
                                                )
                                                route_outcome = "media_content_missing"
                                                return
                                            await _route_media_reply_file(
                                                key,
                                                target=target,
                                                method=method,
                                                request_headers=headers,
                                            )
                                            route_outcome = "media_content_replied"
                                            return

                                        if method == "DELETE" and path_norm.startswith("/media/files/"):
                                            filename = unquote(path_norm[len("/media/files/"):])
                                            try:
                                                target = media_file_path(filename)
                                            except ValueError as exc:
                                                await _route_media_reply_json(
                                                    key,
                                                    status=400,
                                                    payload={"ok": False, "detail": str(exc)},
                                                )
                                                route_outcome = "media_delete_bad_request"
                                                return
                                            existed = target.exists()
                                            if existed:
                                                target.unlink()
                                            await _route_media_reply_json(
                                                key,
                                                status=200,
                                                payload={
                                                    "ok": True,
                                                    "filename": target.name,
                                                    "deleted": existed,
                                                    "items": list_media_files(),
                                                },
                                            )
                                            route_outcome = "media_delete_replied"
                                            return

                                        if method == "PUT" and path_norm.startswith("/media/files/"):
                                            filename = unquote(path_norm[len("/media/files/"):])
                                            try:
                                                target = media_file_path(filename)
                                            except ValueError as exc:
                                                await _route_media_reply_json(
                                                    key,
                                                    status=400,
                                                    payload={"ok": False, "detail": str(exc)},
                                                )
                                                route_outcome = "media_upload_bad_request"
                                                return
                                            if content_length > int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES):
                                                await _route_media_reply_json(
                                                    key,
                                                    status=413,
                                                    payload={
                                                        "ok": False,
                                                        "detail": "media_upload_too_large",
                                                        "max_upload_bytes": int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES),
                                                    },
                                                )
                                                route_outcome = "media_upload_too_large"
                                                return
                                            tmp_path = target.with_name(
                                                f"{target.name}.relay-{os.getpid()}-{int(time.time() * 1000)}.part"
                                            )
                                            handle = tmp_path.open("wb")
                                            media_relay_sessions[key] = {
                                                "mode": "upload",
                                                "target": target,
                                                "tmp_path": tmp_path,
                                                "handle": handle,
                                                "size_bytes": 0,
                                                "replaced": target.exists(),
                                                "mime_type": guess_media_type(target.name),
                                                "max_upload_bytes": int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES),
                                            }
                                            route_outcome = "media_upload_open"
                                            return

                                        await _route_media_reply_json(
                                            key,
                                            status=404,
                                            payload={"ok": False, "detail": "media_route_not_found"},
                                        )
                                        route_outcome = "media_not_found"
                                        return
                                    except Exception as e:
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "media_http_error",
                                                "status": 502,
                                                "error": "media_route_open_failed",
                                                "detail": str(e),
                                            },
                                        )
                                        route_outcome = f"media_http_open_fail:{type(e).__name__}"
                                        return

                                if t == "media_http_req_chunk":
                                    session = media_relay_sessions.get(key)
                                    if not isinstance(session, dict) or str(session.get("mode") or "") != "upload":
                                        route_outcome = "media_chunk_without_session"
                                        return
                                    try:
                                        b64 = (data or {}).get("data_b64")
                                        if not isinstance(b64, str) or not b64:
                                            route_outcome = "media_chunk_empty"
                                            return
                                        blob = base64.b64decode(b64.encode("ascii"))
                                        size_bytes = int(session.get("size_bytes") or 0) + len(blob)
                                        if size_bytes > int(session.get("max_upload_bytes") or 0):
                                            await _route_media_reply_json(
                                                key,
                                                status=413,
                                                payload={
                                                    "ok": False,
                                                    "detail": "media_upload_too_large",
                                                    "max_upload_bytes": int(session.get("max_upload_bytes") or 0),
                                                },
                                            )
                                            _cleanup_media_relay_session(key, remove_temp=True)
                                            route_outcome = "media_chunk_too_large"
                                            return
                                        handle = session.get("handle")
                                        if not handle:
                                            route_outcome = "media_chunk_no_handle"
                                            return
                                        handle.write(blob)
                                        session["size_bytes"] = size_bytes
                                        route_outcome = "media_chunk_written"
                                    except Exception as e:
                                        _cleanup_media_relay_session(key, remove_temp=True)
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "media_http_error",
                                                "status": 502,
                                                "error": "media_upload_write_failed",
                                                "detail": str(e),
                                            },
                                        )
                                        route_outcome = f"media_chunk_fail:{type(e).__name__}"
                                    return

                                if t == "media_http_req_end":
                                    session = media_relay_sessions.get(key)
                                    if not isinstance(session, dict) or str(session.get("mode") or "") != "upload":
                                        route_outcome = "media_end_without_session"
                                        return
                                    try:
                                        handle = session.get("handle")
                                        if handle:
                                            handle.close()
                                        target = Path(session.get("target"))
                                        tmp_path = Path(session.get("tmp_path"))
                                        tmp_path.replace(target)
                                        _cleanup_media_relay_session(key, remove_temp=False)
                                        await _route_media_reply_json(
                                            key,
                                            status=200,
                                            payload={
                                                "ok": True,
                                                "filename": target.name,
                                                "size_bytes": int(session.get("size_bytes") or 0),
                                                "mime_type": str(session.get("mime_type") or ""),
                                                "replaced": bool(session.get("replaced")),
                                            },
                                        )
                                        route_outcome = "media_upload_done"
                                    except Exception as e:
                                        _cleanup_media_relay_session(key, remove_temp=True)
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "media_http_error",
                                                "status": 502,
                                                "error": "media_upload_finalize_failed",
                                                "detail": str(e),
                                            },
                                        )
                                        route_outcome = f"media_end_fail:{type(e).__name__}"
                                    return

                                if t == "media_http_abort":
                                    _cleanup_media_relay_session(key, remove_temp=True)
                                    route_outcome = "media_http_abort"
                                    return

                                if t == "http":
                                    route_outcome = "http"
                                    method = str((data or {}).get("method") or "GET").upper()
                                    path = str((data or {}).get("path") or "/api/ping")
                                    # Be tolerant: root might send trailing slashes.
                                    path_norm = (path.rstrip("/") or "/") if isinstance(path, str) else "/"
                                    search = str((data or {}).get("search") or "")
                                    headers = (data or {}).get("headers") or {}
                                    body_b64 = (data or {}).get("body_b64")

                                    # Root continuously probes `/api/node/status` (and `/api/ping`) with a short timeout
                                    # to decide whether the hub is reachable. When the hub is under load (YJS/WebRTC
                                    # init) the local HTTP stack may respond slowly, and root will surface
                                    # `hub_unreachable` / `yjs_sync_timeout`.
                                    #
                                    # Return these probe endpoints inline (no local HTTP) so the browser can log in
                                    # even when the hub API is busy.
                                    try:
                                        if method in ("GET", "HEAD") and path_norm in ("/api/node/status", "/api/ping", "/healthz"):
                                            if path_norm == "/api/node/status":
                                                try:
                                                    cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                                except Exception:
                                                    cfg = load_config(ctx=self.ctx)
                                                payload0 = {
                                                    "node_id": str(getattr(cfg, "node_id", "") or ""),
                                                    "subnet_id": str(getattr(cfg, "subnet_id", "") or ""),
                                                    "role": str(getattr(cfg, "role", "") or ""),
                                                    "ready": bool(is_ready()),
                                                }
                                            else:
                                                payload0 = {"ok": True, "ts": time.time()}
                                            raw = _json.dumps(payload0, ensure_ascii=False).encode("utf-8")
                                            resp = {
                                                "t": "http_resp",
                                                "status": 200,
                                                "headers": {"content-type": "application/json"},
                                                "body_b64": base64.b64encode(raw).decode("ascii"),
                                                "truncated": False,
                                            }
                                            try:
                                                await _route_reply(key, resp)
                                                route_outcome = "http_inline_probe_replied"
                                            except Exception:
                                                pass
                                            if _route_probe_resend_delays_s:
                                                for delay_s in _route_probe_resend_delays_s:
                                                    async def _resend(delay_s: float = float(delay_s)) -> None:
                                                        try:
                                                            await asyncio.sleep(max(0.0, delay_s))
                                                            await _route_reply(key, resp)
                                                            if _route_tx_verbose or _route_verbose:
                                                                try:
                                                                    _rl_log(
                                                                        "hub-route.probe_resend",
                                                                        f"[hub-route] http probe resend delay_s={delay_s} key={key}",
                                                                        every_s=1.0,
                                                                    )
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            return

                                                    try:
                                                        asyncio.create_task(
                                                            _resend(),
                                                            name=f"hub-route-probe-resend-{key[-8:]}-{int(delay_s * 1000)}",
                                                        )
                                                    except Exception:
                                                        pass
                                            try:
                                                if _route_diag:
                                                    _rl_log(
                                                        "hub-route.inline_probe",
                                                        f"[hub-route] http inline ok path={path_norm} key={key}",
                                                        every_s=5.0,
                                                    )
                                            except Exception:
                                                pass
                                            return
                                    except Exception:
                                        pass

                                    def _do_http() -> dict[str, Any]:
                                        try:
                                            if _hub_route_prefers_supervisor_public_status(path_norm, method) and _dev_without_supervisor():
                                                from adaos.services.core_update import (
                                                    read_public_update_status as _read_public_update_status,
                                                )

                                                payload = _read_public_update_status()
                                                raw = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
                                                return {
                                                    "t": "http_resp",
                                                    "status": 200,
                                                    "headers": {"content-type": "application/json; charset=utf-8"},
                                                    "body_b64": base64.b64encode(raw).decode("ascii"),
                                                    "truncated": False,
                                                }
                                            import requests  # type: ignore

                                            try:
                                                from adaos.services.node_config import load_config

                                                cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                                # IMPORTANT: Route-proxy HTTP requests must target the local hub instance,
                                                # not the public Root proxy URL that might be stored in node.yaml as hub_url.
                                                bases = _build_hub_route_http_bases(
                                                    path_norm=path_norm,
                                                    method=method,
                                                    cfg=cfg,
                                                    ctx=self.ctx,
                                                )
                                                token_local = getattr(cfg, "token", None) or os.getenv("ADAOS_TOKEN", "") or None
                                            except Exception:
                                                bases = _build_hub_route_http_bases(
                                                    path_norm=path_norm,
                                                    method=method,
                                                    cfg=None,
                                                    ctx=self.ctx,
                                                )
                                                token_local = os.getenv("ADAOS_TOKEN", "") or None

                                            # Add optional target/core port fallback for local setups.
                                            try:
                                                from urllib.parse import urlparse

                                                u0 = urlparse(bases[0])
                                                h0 = u0.hostname or "127.0.0.1"
                                                p0 = u0.port
                                                scheme0 = u0.scheme or "http"
                                                alt_port_raw = os.getenv("ADAOS_TARGET_PORT") or os.getenv("ADAOS_CORE_PORT") or ""
                                                alt_port = int(alt_port_raw) if alt_port_raw.strip() else 8788
                                                if (p0 in (None, 8777)) and alt_port and alt_port != p0:
                                                    bases.append(f"{scheme0}://{h0}:{alt_port}")
                                            except Exception:
                                                pass

                                            url = f"{bases[0]}{path}{search}"
                                            if _route_verbose and path not in ("/api/node/status", "/api/ping"):
                                                try:
                                                    _route_log(f"[hub-route] http upstream url={url}")
                                                except Exception:
                                                    pass
                                            body = None
                                            if isinstance(body_b64, str) and body_b64:
                                                try:
                                                    body = base64.b64decode(body_b64.encode("ascii"))
                                                except Exception:
                                                    body = None
                                            # Minimal header allowlist.
                                            h2: dict[str, str] = {}
                                            if token_local:
                                                h2["X-AdaOS-Token"] = str(token_local)
                                            if isinstance(headers, dict):
                                                ct = headers.get("content-type") or headers.get("Content-Type")
                                                if isinstance(ct, str) and ct:
                                                    h2["Content-Type"] = ct
                                            # Do not inherit HTTP(S)_PROXY environment from the host/container:
                                            # local hub calls must stay local, otherwise they can hang on a proxy.
                                            def _do_http_upstream() -> dict[str, Any]:
                                                sess = requests.Session()
                                                try:
                                                    try:
                                                        sess.trust_env = False
                                                    except Exception:
                                                        pass
                                                    last_exc: Exception | None = None
                                                    resp = None
                                                    for base in bases:
                                                        url_try = f"{base}{path}{search}"
                                                        try:
                                                            # Root times out fairly quickly while waiting for
                                                            # route.to_browser.* replies. Keep local proxy attempts
                                                            # short and, critically, run them off the event loop
                                                            # thread because the local hub HTTP server lives in this
                                                            # same process.
                                                            timeout = _hub_route_local_http_timeout(path)
                                                            resp = sess.request(method, url_try, data=body, headers=h2, timeout=timeout)
                                                            last_exc = None
                                                            break
                                                        except Exception as e:
                                                            last_exc = e
                                                            if _route_verbose:
                                                                try:
                                                                    print(
                                                                        f"[hub-route] http upstream failed url={url_try}: {type(e).__name__}: {e}"
                                                                    )
                                                                except Exception:
                                                                    pass
                                                            if not _hub_route_should_retry_http_upstream_error(
                                                                method=method,
                                                                path=path,
                                                                error_kind=type(e).__name__,
                                                            ):
                                                                break
                                                    if resp is None:
                                                        raise last_exc or RuntimeError("http upstream failed")
                                                    raw = resp.content or b""
                                                    limit = 2 * 1024 * 1024
                                                    truncated = len(raw) > limit
                                                    if truncated:
                                                        raw = raw[:limit]
                                                    out_headers: dict[str, str] = {}
                                                    try:
                                                        cth = resp.headers.get("content-type")
                                                        if cth:
                                                            out_headers["content-type"] = cth
                                                    except Exception:
                                                        pass
                                                    return {
                                                        "t": "http_resp",
                                                        "status": int(resp.status_code),
                                                        "headers": out_headers,
                                                        "body_b64": base64.b64encode(raw).decode("ascii"),
                                                        "truncated": truncated,
                                                    }
                                                finally:
                                                    try:
                                                        sess.close()
                                                    except Exception:
                                                        pass

                                            return _do_http_upstream()
                                        except Exception as e:
                                            return {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "err": str(e)}

                                    resp = await asyncio.to_thread(_do_http)
                                    route_outcome = f"http_local_done:{resp.get('status')}"
                                    if _route_http_trace:
                                        try:
                                            _route_log(
                                                f"[hub-route] http.local.done key={_key_tag(key)} status={resp.get('status')} err={resp.get('err')} truncated={resp.get('truncated')}"
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        await _route_reply(key, resp)
                                        route_outcome = f"http_replied:{resp.get('status')}"
                                    except Exception:
                                        pass
                                    return
                                # Unknown route message type: for HTTP keys, reply with an error so Root does not time out.
                                try:
                                    route_outcome = f"unsupported_t:{t}"
                                    if is_http_key:
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "http_resp",
                                                "status": 502,
                                                "headers": {},
                                                "body_b64": "",
                                                "truncated": False,
                                                "err": f"unsupported_t:{t}",
                                            },
                                        )
                                except Exception:
                                    pass
                                return
                            except Exception as e:
                                route_outcome = f"handler_failed:{type(e).__name__}"
                                if _route_verbose:
                                    try:
                                        _route_log(f"[hub-route] handler failed key={key}: {type(e).__name__}: {e}")
                                    except Exception:
                                        pass
                                # Avoid pure timeouts for HTTP keys; surface an error response instead.
                                try:
                                    if is_http_key and key:
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "http_resp",
                                                "status": 502,
                                                "headers": {},
                                                "body_b64": "",
                                                "truncated": False,
                                                "err": f"handler_failed:{type(e).__name__}",
                                            },
                                        )
                                except Exception:
                                    pass
                            finally:
                                took_ms = (time.monotonic() - route_started) * 1000.0
                                if http_path:
                                    try:
                                        observe_route_e2e(
                                            details={
                                                f"last_http_{http_kind or 'app'}_reply_at": time.time(),
                                                "last_http_reply_path": http_path,
                                                "last_http_reply_method": http_method or "",
                                                "last_http_reply_took_ms": round(took_ms, 1),
                                                "last_http_reply_outcome": route_outcome,
                                                "last_http_reply_key_tag": _key_tag(key),
                                            }
                                        )
                                    except Exception:
                                        pass

                                    # Detect "late replies" relative to the Root route proxy timeouts.
                                    # This is an end-to-end signal: Root likely already timed out waiting.
                                    try:
                                        expected_timeout_ms = 15000
                                        if http_path in ("/api/node/status", "/api/ping", "/healthz"):
                                            expected_timeout_ms = 6500
                                        elif http_path == "/api/tools/call":
                                            expected_timeout_ms = 60000
                                        # Give a small buffer to avoid false positives around the edge.
                                        if (
                                            http_kind == "app"
                                            and expected_timeout_ms > 0
                                            and took_ms >= float(expected_timeout_ms) * 0.98
                                        ):
                                            note_route_incident(
                                                status="late_reply",
                                                summary="hub route reply exceeded root proxy timeout",
                                                details={
                                                    "path": http_path,
                                                    "method": http_method or "",
                                                    "took_ms": round(took_ms, 1),
                                                    "expected_timeout_ms": int(expected_timeout_ms),
                                                    "key_tag": _key_tag(key),
                                                    "outcome": route_outcome,
                                                },
                                            )
                                            observe_route_e2e(
                                                details={
                                                    "last_http_app_late_reply_at": time.time(),
                                                    "last_http_app_late_reply_details": {
                                                        "path": http_path,
                                                        "method": http_method or "",
                                                        "took_ms": round(took_ms, 1),
                                                        "expected_timeout_ms": int(expected_timeout_ms),
                                                        "key_tag": _key_tag(key),
                                                        "outcome": route_outcome,
                                                    },
                                                }
                                            )
                                    except Exception:
                                        pass

                                if _route_http_trace and key:
                                    try:
                                        _route_log(
                                            f"[hub-route] cb.done key={_key_tag(key)} subj={subject} t={route_t} outcome={route_outcome} took_ms={took_ms:.1f}"
                                        )
                                    except Exception:
                                        pass
                                return

                        class _QueuedRouteMsg:
                            __slots__ = ("subject", "data")

                            def __init__(self, subject: str, data: bytes) -> None:
                                self.subject = subject
                                self.data = data

                        try:
                            route_handler_queue_max = int(os.getenv("HUB_ROUTE_HANDLER_QUEUE_MAX", "4096") or "4096")
                        except Exception:
                            route_handler_queue_max = 4096
                        if route_handler_queue_max < 128:
                            route_handler_queue_max = 128
                        route_handler_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=route_handler_queue_max)
                        route_diag_state["dispatch_queue_max"] = int(route_handler_queue_max)

                        def _route_key_from_subject(subject: str) -> str:
                            try:
                                parts = subject.split(".")
                                if subject.startswith("route.v2.to_hub.") and len(parts) >= 5:
                                    return str(parts[4] or "")
                                if subject.startswith("route.to_hub.") and len(parts) >= 3:
                                    return str(parts[2] or "")
                            except Exception:
                                pass
                            return ""

                        async def _route_handler_worker() -> None:
                            while True:
                                subject, raw = await route_handler_queue.get()
                                started0 = time.monotonic()
                                key0 = _route_key_from_subject(subject)
                                try:
                                    route_diag_state["dispatch_queue_size"] = int(route_handler_queue.qsize())
                                    route_diag_state["last_dispatch_key_tag"] = _key_tag(key0) if key0 else ""
                                    await _route_handle_msg(_QueuedRouteMsg(subject, raw))
                                    route_diag_state["dispatch_handled_total"] = int(route_diag_state.get("dispatch_handled_total") or 0) + 1
                                except asyncio.CancelledError:
                                    raise
                                except Exception as e:
                                    try:
                                        self._log.warning(
                                            "hub route handler failed subject=%s type=%s err=%s",
                                            subject,
                                            type(e).__name__,
                                            e,
                                        )
                                    except Exception:
                                        pass
                                finally:
                                    try:
                                        took_ms = (time.monotonic() - started0) * 1000.0
                                        route_diag_state["last_dispatch_ms"] = round(took_ms, 1)
                                        if took_ms >= 250.0:
                                            route_diag_state["dispatch_slow_total"] = int(route_diag_state.get("dispatch_slow_total") or 0) + 1
                                            route_diag_state["last_dispatch_slow_ms"] = round(took_ms, 1)
                                    except Exception:
                                        pass
                                    try:
                                        route_handler_queue.task_done()
                                    except Exception:
                                        pass
                                    try:
                                        route_diag_state["dispatch_queue_size"] = int(route_handler_queue.qsize())
                                    except Exception:
                                        pass

                        async def _route_cb(msg) -> None:
                            try:
                                subject = str(getattr(msg, "subject", "") or "")
                            except Exception:
                                subject = ""
                            try:
                                raw = bytes(getattr(msg, "data", b"") or b"")
                            except Exception:
                                raw = b""
                            try:
                                route_handler_queue.put_nowait((subject, raw))
                                route_diag_state["dispatch_enqueued_total"] = int(route_diag_state.get("dispatch_enqueued_total") or 0) + 1
                                route_diag_state["dispatch_queue_size"] = int(route_handler_queue.qsize())
                            except asyncio.QueueFull:
                                key0 = _route_key_from_subject(subject)
                                route_diag_state["dispatch_drop_total"] = int(route_diag_state.get("dispatch_drop_total") or 0) + 1
                                route_diag_state["dispatch_queue_size"] = int(route_handler_queue.qsize())
                                route_diag_state["last_dispatch_key_tag"] = _key_tag(key0) if key0 else ""
                                try:
                                    _rl_log(
                                        "hub-route.dispatch_queue_full",
                                        (
                                            "[hub-route] dispatch queue full "
                                            f"qsize={route_handler_queue.qsize()} max={route_handler_queue_max} key={_key_tag(key0)}"
                                        ),
                                        every_s=1.0,
                                    )
                                except Exception:
                                    pass
                                try:
                                    note_route_incident(
                                        status="dispatch_queue_full",
                                        summary="hub route handler queue is full; dropping inbound route frame",
                                        details={
                                            "key_tag": _key_tag(key0),
                                            "queue_size": int(route_handler_queue.qsize()),
                                            "queue_max": int(route_handler_queue_max),
                                        },
                                    )
                                except Exception:
                                    pass

                        try:
                            route_handler_task = asyncio.create_task(
                                _route_handler_worker(),
                                name="adaos-hub-route-handler",
                            )
                            sub_workers.append(route_handler_task)
                        except Exception:
                            pass

                        try:
                            # Legacy v1 subject. Disabled by default because it cannot be isolated by hub id,
                            # so it allows cross-hub route traffic and can cause hard-to-debug flaps.
                            if os.getenv("HUB_ROUTE_V1", "0") == "1":
                                route_sub = await _sub("route.to_hub.*", cb=_route_cb)
                        except Exception:
                            route_sub = None
                        try:
                            # v2: route.v2.to_hub.<hubId>.<key>
                            route_sub_v2 = await _sub(f"route.v2.to_hub.{hub_id}.*", cb=_route_cb)
                        except Exception:
                            route_sub_v2 = None
                        if hub_nats_verbose or not hub_nats_quiet:
                            if route_sub is not None:
                                print("[hub-io] NATS subscribe route.to_hub.* (hub route proxy, legacy v1)")
                            print(f"[hub-io] NATS subscribe route.v2.to_hub.{hub_id}.* (hub route proxy)")
                        try:
                            if route_sub is not None:
                                self._log.info("nats bridge subscribed subject=route.to_hub.* (legacy v1)")
                            self._log.info("nats bridge subscribed subject=route.v2.to_hub.%s.*", hub_id)
                        except Exception:
                            pass
                        try:
                            mark_route_ready(
                                summary="hub route relay subscription installed",
                                details={
                                    "subjects": [
                                        f"route.v2.to_hub.{hub_id}.*",
                                        *(
                                            ["route.to_hub.*"]
                                            if route_sub is not None
                                            else []
                                        ),
                                    ]
                                },
                            )
                        except Exception:
                            pass
                        try:
                            _update_route_protocol_runtime()
                        except Exception:
                            pass
                    except Exception as e:
                        # Do not fail the whole IO stack: this is an optional fallback used only when
                        # browser connects through Root (api.inimatic.com) and needs a NATS tunnel.
                        if candidate_passive_mode and str(e) == "candidate runtime keeps root route relay passive until cutover":
                            try:
                                self._log.info(
                                    "nats route relay kept passive for candidate runtime hub_id=%s instance=%s",
                                    hub_id,
                                    runtime_instance,
                                )
                            except Exception:
                                pass
                            e = None
                        try:
                            current_route_reset = getattr(self, "_hub_root_route_reset", None)
                            if current_route_reset is _reset_route_runtime:
                                setattr(self, "_hub_root_route_reset", None)
                        except Exception:
                            pass
                        if e is not None:
                            try:
                                if os.getenv("HUB_ROUTE_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                    print(f"[hub-io] NATS route proxy init failed: {type(e).__name__}: {e}")
                                    try:
                                        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                                        print(tb.rstrip())
                                    except Exception:
                                        pass
                                else:
                                    print(f"[hub-io] NATS route proxy disabled: {type(e).__name__}: {e}")
                            except Exception:
                                pass
                            try:
                                mark_route_degraded(
                                    summary=f"hub route relay initialization failed ({type(e).__name__})",
                                    details={"error": str(e)},
                                )
                            except Exception:
                                pass

                    # Optional compatibility: also listen to additional hub aliases if explicitly configured
                    if not candidate_passive_mode:
                        try:
                            aliases_env = os.getenv("HUB_INPUT_ALIASES", "")
                            aliases: List[str] = [a.strip() for a in aliases_env.split(",") if a.strip()]
                            seen = set([hub_id])
                            for aid in aliases:
                                if aid in seen:
                                    continue
                                seen.add(aid)
                                alt = f"tg.input.{aid}"
                                if hub_nats_verbose or not hub_nats_quiet:
                                    print(f"[hub-io] NATS subscribe (alias) {alt}")
                                await _sub(alt, cb=cb)
                                try:
                                    self._log.info("nats bridge subscribed subject=%s", alt)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # legacy text bridge -> wrap into minimal envelope and publish to same tg.input subject
                    async def cb_legacy(msg):
                        try:
                            data = _json.loads(msg.data.decode("utf-8"))
                        except Exception:
                            data = {}
                        # transform into minimal io.input envelope compatible with downstream
                        try:
                            text = (data or {}).get("text") or ""
                            chat_id = str((data or {}).get("chat_id") or "")
                            tg_msg_id = (data or {}).get("tg_msg_id") or 0
                            env = {
                                "event_id": str(uuid.uuid4()).replace("-", ""),
                                "kind": "io.input",
                                "ts": datetime.utcnow().isoformat() + "Z",
                                "dedup_key": f"legacy:{chat_id}:{tg_msg_id}",
                                "payload": {
                                    "type": "text",
                                    "source": "telegram",
                                    "bot_id": "",
                                    "hub_id": hub_id,
                                    "chat_id": chat_id,
                                    "user_id": chat_id,
                                    "update_id": str(tg_msg_id),
                                    "payload": {"text": text, "meta": {"msg_id": tg_msg_id}},
                                },
                                "meta": {"hub_id": hub_id},
                            }
                        except Exception:
                            env = data
                        try:
                            self.ctx.bus.publish(Event(type=subj, payload=env, source="io.nats", ts=time.time()))
                        except Exception:
                            pass

                    from datetime import datetime

                    # Legacy classic path subscription only when explicitly enabled
                    if not candidate_passive_mode:
                        try:
                            if os.getenv("HUB_LISTEN_LEGACY", "0") == "1":
                                await _sub(subj_legacy, cb=cb_legacy)
                                aliases_env = os.getenv("HUB_INPUT_ALIASES", "")
                                aliases: List[str] = [a.strip() for a in aliases_env.split(",") if a.strip()]
                                seen = set([hub_id])
                                for aid in aliases:
                                    if aid in seen:
                                        continue
                                    seen.add(aid)
                                    alt_legacy = f"io.tg.in.{aid}.text"
                                    if hub_nats_verbose or not hub_nats_quiet:
                                        print(f"[hub-io] NATS subscribe (alias legacy) {alt_legacy}")
                                    await _sub(alt_legacy, cb=cb_legacy)
                                    try:
                                        self._log.info("nats bridge subscribed subject=%s", alt_legacy)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    # keep task alive
                    try:
                        last_watchdog_tick_at = time.monotonic()
                        while True:
                            await asyncio.sleep(1.0)
                            now = time.monotonic()
                            tick_gap = now - last_watchdog_tick_at
                            last_watchdog_tick_at = now
                            try:
                                await asyncio.to_thread(
                                    _write_nats_ws_diag_file,
                                    nc,
                                    server=nats_last_server,
                                    source="periodic",
                                )
                            except Exception:
                                pass
                            skip_rx_watchdog = tick_gap > 5.0
                            if skip_rx_watchdog:
                                # If the event loop was stalled (e.g. a long sync handler), don't treat lack of RX
                                # during that window as a dead connection; refresh the baseline instead.
                                try:
                                    tr = getattr(nc, "_transport", None)
                                    if tr is not None:
                                        setattr(tr, "_adaos_last_rx_at", now)
                                except Exception:
                                    pass
                            # Watchdog: nats-py can silently lose its internal loops on unexpected WS/control frames
                            # (or other exceptions), leaving the socket open but the client effectively dead.
                            # If any core task terminates unexpectedly, restart the bridge.
                            try:
                                for _tname in ("_reading_task", "_flusher_task", "_ping_interval_task"):
                                    if _tname == "_ping_interval_task" and bool(getattr(nc, "_adaos_ping_interval_task_disabled", False)):
                                        continue
                                    _t = getattr(nc, _tname, None)
                                    if isinstance(_t, asyncio.Task) and _t.done():
                                        _exc = None
                                        try:
                                            _exc = _t.exception()
                                        except asyncio.CancelledError:
                                            _exc = None
                                        # If the core task stopped without an exception, surface the last_error
                                        # so the supervisor can classify transient EOFs and quarantine the server.
                                        try:
                                            if _exc is None:
                                                _le = getattr(nc, "last_error", None)
                                                if isinstance(_le, Exception):
                                                    _exc = _le
                                        except Exception:
                                            pass
                                        try:
                                            if _exc is None:
                                                tr = getattr(nc, "_transport", None)
                                                _le = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
                                                if isinstance(_le, Exception):
                                                    _exc = _le
                                        except Exception:
                                            pass
                                        # If task ended without exception, still restart - it should live forever.
                                        _msg = (
                                            f"[hub-io] nats watchdog: task={_tname} terminated exc={type(_exc).__name__}: {_exc}"
                                            if _exc
                                            else f"[hub-io] nats watchdog: task={_tname} terminated"
                                        )
                                        try:
                                            self._log.warning(_msg)
                                        except Exception:
                                            pass
                                        _rl_log("nats.watchdog", _msg, every_s=1.0)
                                        try:
                                            _log_nats_ws_diag(
                                                nc,
                                                server=nats_last_server,
                                                rate_key="nats.ws_diag.watchdog",
                                                every_s=1.0,
                                                source="watchdog",
                                                task_name=_tname,
                                                err=_exc if isinstance(_exc, Exception) else None,
                                            )
                                        except Exception:
                                            pass
                                        # This failure mode can happen without the NATS client's disconnected_cb firing
                                        # (for example, when `_reading_task` dies first). Emit a one-time DOWN signal so
                                        # readiness/stability reflect the incident immediately.
                                        try:
                                            _emit_down(kind=f"watchdog.{_tname}", err=_exc if isinstance(_exc, Exception) else None)
                                        except Exception:
                                            pass
                                        if _exc is not None:
                                            raise RuntimeError(_msg) from _exc
                                        raise RuntimeError(_msg)
                            except RuntimeError:
                                raise
                            except Exception:
                                pass
                            # RX watchdog: if we stop receiving WS frames (including keepalives) for too long,
                            # treat the connection as dead even if `nc.is_closed()` is still False.
                            try:
                                if skip_rx_watchdog:
                                    raise StopIteration()
                                tr = getattr(nc, "_transport", None)
                                last_rx = getattr(tr, "_adaos_last_rx_at", None) if tr is not None else None
                                if isinstance(last_rx, (int, float)):
                                    try:
                                        rx_timeout_s = float(os.getenv("HUB_NATS_RX_TIMEOUT_S", "90") or "90")
                                    except Exception:
                                        rx_timeout_s = 90.0
                                    if rx_timeout_s >= 10.0 and (time.monotonic() - float(last_rx)) > rx_timeout_s:
                                        _idle = time.monotonic() - float(last_rx)
                                        _msg = f"[hub-io] nats watchdog: no RX for {_idle:.1f}s (timeout={rx_timeout_s:.1f}s)"
                                        _rl_log("nats.watchdog", _msg, every_s=1.0)
                                        raise RuntimeError(_msg)
                            except StopIteration:
                                pass
                            except RuntimeError:
                                raise
                            except Exception:
                                pass

                            is_closed_attr = getattr(nc, "is_closed", None)
                            is_closed = is_closed_attr() if callable(is_closed_attr) else bool(is_closed_attr)
                            if is_closed:
                                # Extra WS diagnostics (close code/reason) for debugging UnexpectedEOF.
                                try:
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                        tr = getattr(nc, "_transport", None)
                                        ws = getattr(tr, "_ws", None) if tr else None
                                        ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                        ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                        ws_exc = None
                                        try:
                                            exf = getattr(ws, "exception", None)
                                            if callable(exf):
                                                ws_exc = exf()
                                        except Exception:
                                            ws_exc = None
                                        _rl_log(
                                            "nats.ws_state",
                                            f"[hub-io] nats ws state: tag={getattr(tr, '_adaos_ws_tag', None) if tr is not None else None} server={nats_last_server} ws_url={getattr(tr, '_adaos_ws_url', None) if tr is not None else None} closed={ws_closed} close_code={ws_close_code} ws_exc={ws_exc}",
                                            every_s=1.0,
                                        )
                                except Exception:
                                    pass
                                last_err = getattr(nc, "last_error", None)
                                details = f"{type(last_err).__name__}: {last_err}" if last_err else ""
                                try:
                                    self._log.warning(
                                        "nats bridge closed server=%s hub_id=%s details=%s",
                                        nats_last_server,
                                        hub_id,
                                        details,
                                    )
                                except Exception:
                                    pass
                                raise RuntimeError(f"nats connection closed{(': ' + details) if details else ''}")
                    finally:
                        try:
                            self._log.info("nats bridge finalizing hub_id=%s server=%s", hub_id, nats_last_server)
                        except Exception:
                            pass
                        def _keep_pending_task(task: asyncio.Task | None) -> None:
                            # asyncio keeps only weak refs to tasks; if we drop our references before a
                            # canceled task finishes, Python can emit "Task was destroyed but it is pending!".
                            try:
                                if not isinstance(task, asyncio.Task) or task.done():
                                    return
                            except Exception:
                                return
                            try:
                                alive = getattr(self, "_nats_pending_cleanup_tasks", None)
                                if alive is None:
                                    alive = set()
                                    setattr(self, "_nats_pending_cleanup_tasks", alive)
                                alive.add(task)

                                def _drop(done: asyncio.Task) -> None:
                                    try:
                                        alive.discard(done)
                                    except Exception:
                                        pass

                                task.add_done_callback(_drop)
                            except Exception:
                                pass
                        try:
                            if raw_keepalive_task is not None:
                                try:
                                    raw_keepalive_task.cancel()
                                except Exception:
                                    pass
                                try:
                                    _keep_pending_task(raw_keepalive_task)
                                except Exception:
                                    pass
                                try:
                                    await asyncio.wait_for(asyncio.gather(raw_keepalive_task, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
                                raw_keepalive_task = None
                        except Exception:
                            pass
                        try:
                            if getattr(self, "_tg_output_nats_nc", None) is nc:
                                setattr(self, "_tg_output_nats_nc", None)
                        except Exception:
                            pass
                        try:
                            if getattr(self, "_hub_root_nc", None) is nc:
                                setattr(self, "_hub_root_nc", None)
                        except Exception:
                            pass
                        async def _force_close_ws_transport() -> None:
                            # WebSocket transports can leave client resources unclosed
                            # if the websocket is already None (close() becomes a no-op and wait_closed() hangs).
                            try:
                                tr = getattr(nc, "_transport", None)
                                if not tr:
                                    return

                                ws = getattr(tr, "_ws", None)
                                close_task = getattr(tr, "_close_task", None)
                                client = getattr(tr, "_client", None)

                                try:
                                    if ws is not None:
                                        await ws.close()
                                except Exception:
                                    pass

                                # Unblock wait_closed() if it would otherwise await an unresolved Future.
                                try:
                                    if close_task is not None and hasattr(close_task, "done") and not close_task.done():
                                        close_task.set_result(None)
                                except Exception:
                                    pass

                                try:
                                    if client is not None:
                                        await client.close()
                                except Exception:
                                    pass

                                try:
                                    setattr(tr, "_ws", None)
                                    setattr(tr, "_client", None)
                                except Exception:
                                    pass
                            except Exception:
                                pass

                        # On shutdown/cancel, close any live proxy tunnels and unsubscribe.
                        try:
                            current_route_reset = getattr(self, "_hub_root_route_reset", None)
                            if current_route_reset is _reset_route_runtime:
                                setattr(self, "_hub_root_route_reset", None)
                        except Exception:
                            pass
                        try:
                            for k, rec in list(tunnels.items()):
                                try:
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if ws:
                                        await ws.close()
                                except Exception:
                                    pass
                                tunnels.pop(k, None)
                        except Exception:
                            pass
                        try:
                            for k, tsk in list(tunnel_tasks.items()):
                                try:
                                    tsk.cancel()
                                except Exception:
                                    pass
                                tunnel_tasks.pop(k, None)
                        except Exception:
                            pass
                        try:
                            for task in list(sub_workers):
                                try:
                                    task.cancel()
                                except Exception:
                                    pass
                                try:
                                    _keep_pending_task(task if isinstance(task, asyncio.Task) else None)
                                except Exception:
                                    pass
                            if sub_workers:
                                try:
                                    await asyncio.wait_for(asyncio.gather(*sub_workers, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            # Unsubscribe all subscriptions explicitly to ensure nats-py cancels
                            # internal subscription tasks before the next reconnect attempt.
                            for sub in list(subs):
                                try:
                                    unsub = sub.unsubscribe()
                                    if asyncio.iscoroutine(unsub):
                                        await unsub
                                except Exception:
                                    pass

                            # Ensure internal subscription tasks are stopped even if the connection is already closed.
                            for sub in list(subs):
                                try:
                                    stop = getattr(sub, "_stop_processing", None)
                                    if callable(stop):
                                        stop()
                                except Exception:
                                    pass

                            # Await/cancel internal subscription tasks, if present.
                            wait_tasks: list[asyncio.Task] = []
                            for sub in list(subs):
                                t = getattr(sub, "_wait_for_msgs_task", None)
                                if isinstance(t, asyncio.Task) and not t.done():
                                    try:
                                        t.cancel()
                                    except Exception:
                                        pass
                                    try:
                                        _keep_pending_task(t)
                                    except Exception:
                                        pass
                                    wait_tasks.append(t)
                            if wait_tasks:
                                try:
                                    await asyncio.wait_for(asyncio.gather(*wait_tasks, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(nc.drain(), timeout=2.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(nc.close(), timeout=2.0)
                        except Exception:
                            pass
                        await _force_close_ws_transport()
                        # Give canceled subscription tasks a chance to finish to avoid
                        # "Task was destroyed but it is pending!" warnings.
                        try:
                            await asyncio.sleep(0)
                        except Exception:
                            pass

                async def _maybe_snapshot_root_logs(
                    *,
                    trace: bool,
                    force: bool = False,
                    tag_override: str | None = None,
                    server_override: str | None = None,
                ) -> None:
                    try:
                        if os.getenv("HUB_ROOT_LOG_SNAPSHOT", "0") != "1":
                            return
                        now = time.monotonic()
                        try:
                            snap_every_s = float(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EVERY_S", "60") or "60")
                        except Exception:
                            snap_every_s = 60.0
                        if snap_every_s < 5.0:
                            snap_every_s = 5.0

                        nonlocal last_root_snapshot_at
                        if (not force) and last_root_snapshot_at is not None and (now - last_root_snapshot_at) < snap_every_s:
                            return
                        last_root_snapshot_at = now

                        base = None
                        try:
                            from urllib.parse import urlparse as _urlparse

                            u = _urlparse(str(server_override or nats_last_server or ""))
                            host = (u.hostname or "").strip()
                            if host:
                                # Dev endpoints (like /v1/dev/log_tail) live on the API host, not the NATS host.
                                # If we connected to `nats.<domain>`, try `api.<domain>` for snapshots.
                                if host.startswith("nats.") and host.count(".") >= 2:
                                    host = "api." + host.split(".", 1)[1]
                                base = ("https://" if str(u.scheme).startswith("wss") else "http://") + host
                        except Exception:
                            base = None
                        if not base:
                            return

                        files = os.getenv("HUB_ROOT_LOG_SNAPSHOT_FILES", "reverse-proxy.log,nats.log,backend-b.log") or ""
                        want = [x.strip() for x in files.split(",") if x.strip()]
                        if not want:
                            return
                        try:
                            snapshot_lines = int(os.getenv("HUB_ROOT_LOG_SNAPSHOT_LINES", "250") or "250")
                        except Exception:
                            snapshot_lines = 250
                        if snapshot_lines < 50:
                            snapshot_lines = 50

                        out_dir = Path(".adaos") / "root_log_snapshots"
                        out_dir.mkdir(parents=True, exist_ok=True)

                        def _fetch_one(fname: str) -> tuple[str, str]:
                            import urllib.parse as _up
                            import urllib.request as _ureq

                            qs = _up.urlencode({"file": fname, "lines": str(snapshot_lines)})
                            url = f"{base}/v1/dev/log_tail?{qs}"
                            hdrs = {}
                            try:
                                # Root dev endpoints are protected by X-Root-Token.
                                tok = (os.getenv("HUB_ROOT_LOG_SNAPSHOT_ROOT_TOKEN", "") or "").strip()
                                if not tok:
                                    tok = (os.getenv("ROOT_TOKEN", "") or "").strip()
                                if not tok:
                                    tok = (os.getenv("ADAOS_ROOT_OWNER_TOKEN", "") or "").strip()
                                if not tok:
                                    # Back-compat: previously this env existed and users sometimes set
                                    # `Bearer <token>`; accept and normalize it.
                                    tok = (os.getenv("HUB_ROOT_LOG_SNAPSHOT_AUTH", "") or "").strip()
                                if tok.lower().startswith("bearer "):
                                    tok = tok.split(" ", 1)[1].strip()
                                if tok:
                                    hdrs["X-Root-Token"] = tok
                            except Exception:
                                pass
                            req = _ureq.Request(url, headers=hdrs)
                            with _ureq.urlopen(req, timeout=10) as resp:
                                body = resp.read().decode("utf-8", errors="replace")
                            return url, body

                        def _extract_tag_lines(body: str, tag: str) -> str:
                            try:
                                if not tag:
                                    return ""
                                import json as _json
                                import re as _re

                                obj = _json.loads(body)
                                lines0 = obj.get("lines", [])
                                if not isinstance(lines0, list):
                                    return ""
                                tag_s = str(tag)
                                hub_prefix = tag_s.rsplit("-", 1)[0] if "-" in tag_s else tag_s
                                tag_hits = [str(s) for s in lines0 if isinstance(s, str) and tag_s in s]
                                conn_ids: set[str] = set()
                                for line0 in tag_hits:
                                    try:
                                        for m0 in _re.finditer(r'"conn":"([^"]+)"', line0):
                                            conn_ids.add(str(m0.group(1)))
                                    except Exception:
                                        continue
                                route_prefixes = (
                                    f"route.to_browser.{hub_prefix}--",
                                    f"route.to_hub.{hub_prefix}--",
                                    # v2 subjects include hubId as a separate token: route.v2.to_browser.<hubId>.<key>
                                    f"route.v2.to_browser.{hub_prefix}.{hub_prefix}--",
                                    f"route.v2.to_hub.{hub_prefix}.{hub_prefix}--",
                                )
                                include_extra = str(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_EXTRA", "0") or "0").strip() == "1"
                                extra_keywords = (
                                    "http proxy failed",
                                    "ws tunnel:",
                                    "nats http route",
                                    "nats keepalive pong missing",
                                    "nats route chunk (client->proxy)",
                                    "nats route upstream write",
                                    "conn close",
                                    "upstream close",
                                    "upstream error",
                                    "ws close 1006 diag",
                                    "ws socket data after keepalive",
                                    "ws socket readable after keepalive",
                                    "ws socket pause",
                                    "ws socket resume",
                                    "ws socket end",
                                    "ws socket close",
                                    "ws socket error",
                                    "ws error",
                                    "ws upstream closed",
                                    "closing superseded hub ws-nats connection",
                                )
                                hits: list[str] = []
                                for item in lines0:
                                    if not isinstance(item, str):
                                        continue
                                    line = str(item)
                                    include = tag_s in line
                                    if not include and conn_ids:
                                        try:
                                            include = any(cid and cid in line for cid in conn_ids)
                                        except Exception:
                                            include = False
                                    if not include:
                                        try:
                                            include = any(pref in line for pref in route_prefixes)
                                        except Exception:
                                            include = False
                                    if include_extra and (not include):
                                        try:
                                            include = any(kw in line for kw in extra_keywords)
                                        except Exception:
                                            include = False
                                    if include:
                                        hits.append(line)
                                # Keep this file small and focused.
                                return "\n".join(hits[-1000:])
                            except Exception:
                                return ""

                        try:
                            if isinstance(tag_override, str) and tag_override.strip():
                                tag0 = tag_override.strip()
                            else:
                                tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else ""
                        except Exception:
                            tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else ""
                        ts = time.strftime("%Y%m%d_%H%M%SZ", time.gmtime())
                        for fname in want:
                            try:
                                url, body = await asyncio.to_thread(_fetch_one, fname)
                                fn = out_dir / f"{ts}__{(tag0 or 'no_tag')}__{fname.replace('/', '_')}"
                                fn.write_text(body, encoding="utf-8", errors="replace")
                                try:
                                    ex = _extract_tag_lines(body, tag0)
                                    if ex:
                                        fn2 = out_dir / f"{ts}__{(tag0 or 'no_tag')}__{fname.replace('/', '_')}__extract.log"
                                        fn2.write_text(ex, encoding="utf-8", errors="replace")
                                        try:
                                            if os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_PRINT", "0") == "1":
                                                try:
                                                    tail_n = int(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_TAIL", "40") or "40")
                                                except Exception:
                                                    tail_n = 40
                                                if tail_n < 1:
                                                    tail_n = 1
                                                tail_lines = ex.splitlines()
                                                tail = "\n".join(tail_lines[-tail_n:]) if tail_lines else ""
                                                if tail:
                                                    # Include best-effort recency hint: extracted tails can be old if
                                                    # the upstream service has been quiet (e.g. only a few errors in nats.log).
                                                    try:
                                                        from datetime import datetime, timezone

                                                        newest_ts = None
                                                        for raw in reversed(tail_lines):
                                                            try:
                                                                token = (str(raw).strip().split(" ", 1)[0] or "").strip()
                                                                if not token:
                                                                    continue
                                                                if token.endswith("Z"):
                                                                    token = token[:-1] + "+00:00"
                                                                dt = datetime.fromisoformat(token)
                                                                if dt.tzinfo is None:
                                                                    dt = dt.replace(tzinfo=timezone.utc)
                                                                newest_ts = dt.timestamp()
                                                                break
                                                            except Exception:
                                                                continue
                                                        age_s = None
                                                        if isinstance(newest_ts, (int, float)) and newest_ts > 0:
                                                            age_s = round(max(0.0, time.time() - float(newest_ts)), 3)
                                                    except Exception:
                                                        newest_ts = None
                                                        age_s = None
                                                    print(
                                                        f"[hub-io] root log extract tail file={fn2} lines={len(tail_lines)}"
                                                        + (f" newest_age_s={age_s}" if age_s is not None else "")
                                                    )
                                                    try:
                                                        extract_log = logging.getLogger("adaos.hub-io.root-log-extract")
                                                        interesting_lines: list[str] = []
                                                        interesting_markers = (
                                                            "http route: timeout",
                                                            "http proxy failed",
                                                            "ws route: open ack fallback elapsed",
                                                            "ws route: open ack received",
                                                            "closing superseded hub ws-nats connection",
                                                            "hub ws-nats auth ok",
                                                            "upstream close",
                                                            "nats route downstream send failed",
                                                            "nats route upstream write missing",
                                                        )
                                                        for tail_line in tail_lines:
                                                            text = str(tail_line)
                                                            if any(marker in text for marker in interesting_markers):
                                                                interesting_lines.append(text)
                                                        interesting_lines = interesting_lines[-12:]
                                                        extract_log.warning(
                                                            "root log extract tail file=%s lines=%s newest_age_s=%s interesting_lines=%s",
                                                            fn2,
                                                            len(tail_lines),
                                                            age_s,
                                                            len(interesting_lines),
                                                        )
                                                        for tail_line in interesting_lines:
                                                            extract_log.warning(
                                                                "root log extract line file=%s line=%s",
                                                                fn2,
                                                                tail_line,
                                                            )
                                                        if (
                                                            os.getenv("HUB_ROOT_LOG_EXTRACT_VERBOSE", "0") == "1"
                                                            or trace0
                                                        ):
                                                            for tail_line in tail_lines:
                                                                extract_log.debug(
                                                                    "root log extract raw file=%s line=%s",
                                                                    fn2,
                                                                    str(tail_line),
                                                                )
                                                    except Exception:
                                                        pass
                                                    if (
                                                        os.getenv("HUB_ROOT_LOG_EXTRACT_VERBOSE", "0") == "1"
                                                        or trace0
                                                    ):
                                                        print(tail)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                    _rl_log("root.snap", f"[hub-io] saved root log snapshot {fn} (from {url})", every_s=1.0)
                            except Exception as _se:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                    _rl_log("root.snap_fail", f"[hub-io] root log snapshot failed file={fname} err={type(_se).__name__}: {_se}", every_s=1.0)
                    except Exception:
                        return

                # Supervisor wrapper: never crash on unhandled errors; restart with backoff
                async def _nats_bridge_supervisor() -> None:
                    delay = 1.0
                    while True:
                        started_at = time.monotonic()
                        trace0 = os.getenv("HUB_NATS_TRACE", "0") == "1"
                        try:
                            _rl_log("nats.supervisor.start", "[hub-io] nats supervisor: start bridge", every_s=5.0)
                            await _nats_bridge()
                            await asyncio.sleep(3600)
                        except asyncio.CancelledError:
                            try:
                                self._log.info(
                                    "nats supervisor cancelled hub_id=%s server=%s",
                                    hub_id,
                                    _resolve_nats_log_server(
                                        current_attempt=nats_attempt_server,
                                        connected_server=nats_last_server,
                                    ),
                                )
                            except Exception:
                                pass
                            return
                        except Exception as e:
                            ran_for_s = time.monotonic() - started_at
                            is_transient = is_transient_nats_error(e)
                            error_summary = nats_error_summary(e)
                            try:
                                if is_transient:
                                    self._log.warning(
                                        "nats transient disconnect hub_id=%s server=%s type=%s err=%s ran_for_s=%.1f",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                        type(e).__name__,
                                        error_summary,
                                        ran_for_s,
                                    )
                                else:
                                    self._log.warning(
                                        "nats supervisor error hub_id=%s server=%s type=%s err=%s",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                        type(e).__name__,
                                        error_summary,
                                    )
                            except Exception:
                                pass
                            try:
                                if not is_transient:
                                    self._log.warning(
                                        "nats encountered error hub_id=%s server=%s type=%s err=%s",
                                        hub_id,
                                        _resolve_nats_log_server(
                                            current_attempt=nats_attempt_server,
                                            connected_server=nats_last_server,
                                        ),
                                        type(e).__name__,
                                        error_summary,
                                    )
                            except Exception:
                                pass
                            try:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or not is_transient:
                                    print(f"[hub-io] nats: encountered error: {error_summary}")
                            except Exception:
                                pass
                            try:
                                local_sidecar_url = realtime_sidecar_local_url()
                                error_server = _resolve_nats_log_server(
                                    current_attempt=nats_attempt_server,
                                    connected_server=nats_last_server,
                                )
                                using_sidecar = bool(
                                    isinstance(error_server, str)
                                    and isinstance(local_sidecar_url, str)
                                    and str(error_server).strip() == str(local_sidecar_url).strip()
                                )
                            except Exception:
                                using_sidecar = False
                            try:
                                if using_sidecar:
                                    async def _print_sidecar_tail() -> None:
                                        def _tail(path: Path, lines: int) -> tuple[Path, list[str]]:
                                            try:
                                                data = path.read_text(encoding="utf-8", errors="replace").splitlines()
                                            except Exception:
                                                data = []
                                            return path, data[-lines:]

                                        try:
                                            log_path, log_tail = await asyncio.to_thread(_tail, realtime_sidecar_log_path(), 40)
                                            if log_tail:
                                                print(f"[hub-io] adaos-realtime log tail file={log_path} lines={len(log_tail)}")
                                                try:
                                                    sidecar_log = logging.getLogger("adaos.hub-io.sidecar")
                                                    sidecar_log.warning(
                                                        "adaos-realtime log tail file=%s lines=%s",
                                                        log_path,
                                                        len(log_tail),
                                                    )
                                                    for line in log_tail:
                                                        sidecar_log.warning(
                                                            "adaos-realtime log line file=%s line=%s",
                                                            log_path,
                                                            line,
                                                        )
                                                except Exception:
                                                    pass
                                                print("\n".join(log_tail))
                                        except Exception:
                                            pass
                                        try:
                                            diag_path, diag_tail = await asyncio.to_thread(_tail, realtime_sidecar_diag_path(), 10)
                                            if diag_tail:
                                                print(f"[hub-io] adaos-realtime diag tail file={diag_path} lines={len(diag_tail)}")
                                                try:
                                                    sidecar_log = logging.getLogger("adaos.hub-io.sidecar")
                                                    sidecar_log.warning(
                                                        "adaos-realtime diag tail file=%s lines=%s",
                                                        diag_path,
                                                        len(diag_tail),
                                                    )
                                                    for line in diag_tail:
                                                        sidecar_log.warning(
                                                            "adaos-realtime diag line file=%s line=%s",
                                                            diag_path,
                                                            line,
                                                        )
                                                except Exception:
                                                    pass
                                                print("\n".join(diag_tail))
                                        except Exception:
                                            pass

                                    asyncio.create_task(_print_sidecar_tail(), name="adaos-realtime-log-tail")
                            except Exception:
                                pass
                            # Optional delayed snapshot: root-side logs (ECONNRESET/conn close) can be emitted
                            # slightly after the hub notices EOF. A second tail a few seconds later often captures it.
                            try:
                                # `HUB_ROOT_LOG_SNAPSHOT_AFTER_ERR_S` accepts a comma list of delays in seconds.
                                # Set it to empty to disable follow-up snapshots entirely.
                                after_env = os.getenv("HUB_ROOT_LOG_SNAPSHOT_AFTER_ERR_S")
                                if after_env is None:
                                    after_env = "0,3"
                            except Exception:
                                after_env = "0,3"
                            delays: list[float] = []
                            try:
                                if str(after_env or "").strip():
                                    for part in str(after_env).split(","):
                                        p = str(part).strip()
                                        if not p:
                                            continue
                                        try:
                                            v = float(p)
                                        except Exception:
                                            continue
                                        if v >= 0:
                                            delays.append(v)
                                else:
                                    delays = []
                            except Exception:
                                delays = []
                            # Schedule snapshots in the background so reconnect is not delayed by HTTP tailing.
                            try:
                                if delays and os.getenv("HUB_ROOT_LOG_SNAPSHOT", "0") == "1":
                                    tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else None
                                    srv0 = _resolve_nats_log_server(
                                        current_attempt=nats_attempt_server,
                                        connected_server=nats_last_server,
                                    )

                                    async def _snap_later(delay_s: float) -> None:
                                        try:
                                            if delay_s > 0:
                                                await asyncio.sleep(min(30.0, max(0.1, float(delay_s))))
                                        except Exception:
                                            pass
                                        try:
                                            await _maybe_snapshot_root_logs(
                                                trace=trace0,
                                                force=True,
                                                tag_override=tag0,
                                                server_override=srv0,
                                            )
                                        except Exception:
                                            pass

                                    for after_s in delays[:8]:
                                        try:
                                            asyncio.create_task(_snap_later(float(after_s)), name="adaos-root-log-snapshot")
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            # No blocking snapshots here: supervisor keeps retrying promptly.
                            try:
                                delays = []
                            except Exception:
                                pass

                            try:
                                auto_env = os.getenv("HUB_NATS_WS_AUTO_FALLBACK")
                                if auto_env is None:
                                    auto_fallback = False
                                else:
                                    auto_fallback = str(auto_env).strip().lower() not in ("0", "false", "off", "no")
                                if (
                                    auto_fallback
                                    and os.name == "nt"
                                    and (last_ws_transport or "").lower() == "websockets"
                                    and is_transient
                                ):
                                    if os.getenv("HUB_NATS_WS_IMPL", "").lower() != "aiohttp":
                                        os.environ["HUB_NATS_WS_IMPL"] = "aiohttp"
                                        try:
                                            self._log.warning(
                                                "nats ws auto-fallback: switching to aiohttp transport after %s",
                                                type(e).__name__,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            print("[hub-io] nats ws auto-fallback -> aiohttp (HUB_NATS_WS_AUTO_FALLBACK=1)")
                                        except Exception:
                                            pass
                                        try:
                                            configure_hub_root_transport_strategy(
                                                hypothesis={
                                                    "selector_loop": bool(os.name == "nt" and os.getenv("ADAOS_WIN_SELECTOR_LOOP", "0") == "1"),
                                                    "ws_impl": "aiohttp",
                                                    "raw_keepalive": _env_truthy(os.getenv("HUB_NATS_RAW_KEEPALIVE"), default=False),
                                                }
                                            )
                                            record_hub_root_transport_event(
                                                "auto_fallback",
                                                transport="ws",
                                                server=nats_last_server,
                                                summary="hub-root WS client implementation switched to aiohttp after transient failure",
                                                error=str(e),
                                            )
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                            try:
                                q_min_uptime_s = float(os.getenv("HUB_NATS_QUARANTINE_MIN_UPTIME_S", "90") or "90")
                            except Exception:
                                q_min_uptime_s = 90.0
                            try:
                                q_for_s = float(os.getenv("HUB_NATS_QUARANTINE_S", "300") or "300")
                            except Exception:
                                q_for_s = 300.0
                            try:
                                if is_transient and ran_for_s < q_min_uptime_s and isinstance(nats_last_server, str) and nats_last_server:
                                    q_seconds = max(30.0, q_for_s)
                                    nats_server_quarantine_until[nats_last_server] = time.monotonic() + q_seconds
                                    _rl_log(
                                        "nats.supervisor.quarantine",
                                        f"[hub-io] nats supervisor: quarantine server={nats_last_server} for {q_seconds:.0f}s (ran_for={ran_for_s:.1f}s)",
                                        every_s=1.0,
                                    )
                            except Exception:
                                pass

                            if ran_for_s >= 10.0 or is_transient:
                                delay = 0.5
                            try:
                                ok_ago = None
                                if nats_last_ok_at is not None:
                                    ok_ago = time.monotonic() - nats_last_ok_at
                                _rl_log(
                                    "nats.supervisor.retry",
                                    f"[hub-io] nats supervisor: retry in {delay:.1f}s (ran_for={ran_for_s:.1f}s ok_ago={ok_ago:.1f}s transient={is_transient})",
                                    every_s=1.0,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(delay)
                            if ran_for_s < 10.0 and not is_transient:
                                delay = min(delay * 2.0, 30.0)
                            else:
                                delay = min(max(delay, 0.5), 2.0)

                # TODO restore nats WS subscription
                self._start_boot_task_once("adaos-nats-io-bridge", _nats_bridge_supervisor)
        except Exception:
            try:
                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("ADAOS_CLI_DEBUG", "0") == "1":
                    print("[hub-io] nats init failed")
                    try:
                        tb = "".join(traceback.format_exception(*__import__("sys").exc_info()))
                        print(tb.rstrip())
                    except Exception:
                        pass
            except Exception:
                pass

    async def shutdown(self) -> None:
        await bus.emit("sys.stopping", {}, source="lifecycle", actor="system")
        try:
            conf = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
            if getattr(conf, "role", None) == "hub":
                await asyncio.to_thread(report_hub_control_lifecycle_state, conf)
        except Exception:
            self._log.debug("control lifecycle report failed trigger=sys.stopping", exc_info=True)
        try:
            await get_service_supervisor().shutdown()
        except Exception as e:
            self._log.debug("service supervisor shutdown failed", exc_info=True)
            pass
        try:
            await stop_scheduler()
        except Exception:
            pass
        for t in list(self._boot_tasks):
            try:
                t.cancel()
            except Exception:
                pass
        if self._boot_tasks:
            await asyncio.gather(*self._boot_tasks, return_exceptions=True)
            self._boot_tasks.clear()
        self._boot_in_progress = False
        self._boot_done.set()
        self._booted = False
        self._ready.clear()
        await bus.emit("sys.stopped", {}, source="lifecycle", actor="system")

    async def switch_role(self, app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
        prev = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
        await self.shutdown()
        if prev.role == "member" and role.lower().strip() == "hub" and prev.hub_url:
            try:
                await self.heartbeat.deregister(prev.hub_url, prev.token or "", node_id=prev.node_id)
            except Exception:
                pass
            subnet_id = subnet_id or generate_provisional_subnet_id()
        conf = cfg_set_role(role, hub_url=hub_url, subnet_id=subnet_id, ctx=self.ctx)
        if str(role or "").strip().lower() == "hub":
            self._last_role_switch_root_init = self._ensure_hub_root_bootstrap_for_role_switch(conf)
            if not bool(self._last_role_switch_root_init.get("ok")):
                raise RuntimeError(str(self._last_role_switch_root_init.get("error") or "hub Root bootstrap failed"))
            conf = load_config(ctx=self.ctx)
        else:
            self._last_role_switch_root_init = {"attempted": False, "reason": "role_not_hub"}
        await self.run_boot_sequence(app or self._app)
        return conf

    def _ensure_hub_root_bootstrap_for_role_switch(self, conf: NodeConfig) -> dict[str, Any]:
        """
        Ensure a switched hub has Root-issued mTLS material before booting.

        `node role switch --role hub` can be called from a member runtime. In that
        path the config role changes immediately, but the hub still needs
        Root-issued hub cert/key/CA before the hub-root NATS tunnel can obtain
        credentials and become visible to routed browsers.
        """
        if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
            return {"attempted": False, "reason": "role_not_hub"}
        if str(os.getenv("ADAOS_ROLE_SWITCH_HUB_ROOT_INIT", "1") or "1").strip().lower() in {"0", "false", "no", "off"}:
            return {"attempted": False, "ok": True, "reason": "disabled_by_env"}
        try:
            from adaos.services.root.service import RootDeveloperService

            result = RootDeveloperService().init(preferred_subnet_id=str(getattr(conf, "subnet_id", "") or "").strip() or None)
            return {
                "attempted": True,
                "ok": True,
                "subnet_id": result.subnet_id,
                "reused": bool(result.reused),
                "hub_key_path": str(result.hub_key_path),
                "hub_cert_path": str(result.hub_cert_path),
                "ca_cert_path": str(result.ca_cert_path) if result.ca_cert_path else None,
                "workspace_path": str(result.workspace_path),
            }
        except Exception as exc:
            return {
                "attempted": True,
                "ok": False,
                "subnet_id": str(getattr(conf, "subnet_id", "") or "") or None,
                "error": f"{type(exc).__name__}: {exc}",
            }


# --- модульные фасады (синглтон) ---
from adaos.services.heartbeat_requests import RequestsHeartbeat
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
from adaos.services.subnet_registry_mem import get_subnet_registry

_SERVICE: BootstrapService | None = None


def _svc() -> BootstrapService:
    global _SERVICE
    if _SERVICE is None:
        ctx = get_ctx()
        _SERVICE = BootstrapService(ctx, heartbeat=RequestsHeartbeat(), skills_loader=ImportlibSkillsLoader(), subnet_registry=get_subnet_registry())
    return _SERVICE


def is_ready() -> bool:
    return _svc().is_ready()


async def request_hub_root_reconnect(*, transport: str | None = None, url_override: str | None = None) -> dict[str, Any]:
    return await _svc().request_hub_root_reconnect(transport=transport, url_override=url_override)


async def request_member_hub_reconnect(*, force: bool = False) -> dict[str, Any]:
    return await _svc().request_member_hub_reconnect(force=bool(force))


async def request_hub_root_route_reset(*, reason: str, notify_browser: bool = True) -> dict[str, Any]:
    return await _svc().request_hub_root_route_reset(
        reason=str(reason or "").strip() or "route_reset",
        notify_browser=bool(notify_browser),
    )


async def run_boot_sequence(app: Any) -> None:
    await _svc().run_boot_sequence(app)


async def shutdown() -> None:
    await _svc().shutdown()


async def switch_role(app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    return await _svc().switch_role(app, role, hub_url=hub_url, subnet_id=subnet_id)
