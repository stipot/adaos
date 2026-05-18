# src/adaos/apps/api/server.py
# NOTE: CLI (`adaos ...`) loads `.env`, but direct `uvicorn adaos.apps.api.server:app` does not.
# Many subsystems (notably NATS-over-WS tuning) rely on env vars, so best-effort load `.env` here too.
import os
from pathlib import Path
import logging
import time


def _parse_dotenv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not k:
            continue
        data[k] = v.strip().strip('"').strip("'")
    return data


def _search_dotenv_in_parents(start: Path, *, name: str = ".env") -> Path | None:
    try:
        start = start.expanduser().resolve()
    except Exception:
        return None
    for base in (start, *start.parents):
        cand = base / name
        try:
            if cand.exists():
                return cand
        except Exception:
            continue
    return None


def _resolve_dotenv_path() -> Path | None:
    raw = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    if raw:
        try:
            p = Path(raw).expanduser().resolve()
            if p.exists():
                return p
        except Exception:
            pass

    # Core-slot runtime sets cwd to the slot repo dir; search parents (covers `~/adaos/.adaos/state/core_slots/...` -> `~/adaos/.env`).
    try:
        cwd = Path.cwd()
    except Exception:
        cwd = None
    if cwd is not None:
        found = _search_dotenv_in_parents(cwd)
        if found is not None:
            return found

    # Best-effort: search relative to code location as well.
    try:
        here = Path(__file__).resolve()
        found = _search_dotenv_in_parents(here.parent)
        if found is not None:
            return found
    except Exception:
        pass
    return None


def _maybe_load_dotenv() -> None:
    path = _resolve_dotenv_path()
    if path is None:
        return
    try:  # pragma: no cover
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(str(path), override=False)
        return
    except Exception:
        pass
    # Fallback when python-dotenv is absent: still honor values from `.env` without overwriting existing vars.
    for k, v in _parse_dotenv(path).items():
        os.environ.setdefault(str(k), str(v))


_maybe_load_dotenv()
try:  # pragma: no cover
    # Keep old behavior for compatibility, but `_maybe_load_dotenv()` above already resolved the desired `.env`.
    from dotenv import find_dotenv, load_dotenv  # type: ignore

    load_dotenv((os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip() or find_dotenv(), override=False)
except Exception:
    pass


_startup_log = logging.getLogger("adaos.startup")
_runtime_log = logging.getLogger("adaos.runtime")


def _startup_stage_logs_enabled() -> bool:
    return str(os.getenv("ADAOS_STARTUP_STAGE_LOGS") or "").strip().lower() in {"1", "true", "yes", "on"}


class _StartupTimer:
    def __init__(self, stage: str):
        self.stage = stage
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        if _startup_stage_logs_enabled():
            _startup_log.info("startup stage start stage=%s", self.stage)
        return self

    def __exit__(self, exc_type, exc, tb):
        duration = time.perf_counter() - self.started
        if exc is None:
            if _startup_stage_logs_enabled():
                _startup_log.info("startup stage done stage=%s duration_s=%.3f", self.stage, duration)
        else:
            _startup_log.warning(
                "startup stage failed stage=%s duration_s=%.3f error=%s",
                self.stage,
                duration,
                type(exc).__name__,
            )
        return False

try:  # pragma: no cover
    from adaos.services.runtime_dotenv import apply_runtime_dotenv_overrides

    apply_runtime_dotenv_overrides(dotenv_path=(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip() or None)
except Exception:
    pass

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from pydantic import BaseModel, Field
import asyncio
import json
import logging
import platform, time
import signal
import sys
from typing import Any
from urllib.parse import urlparse

def _maybe_set_windows_selector_loop() -> None:
    if os.name != "nt":
        return
    raw = os.getenv("ADAOS_WIN_SELECTOR_LOOP")
    enabled = False
    if raw is not None:
        val = str(raw).strip().lower()
        if val in ("1", "true", "on", "yes"):
            enabled = True
        elif val in ("0", "false", "off", "no"):
            enabled = False
    # Selector loop is retained only as an opt-in diagnostic mode. Normal
    # Windows runtime should stay on the Proactor-capable asyncio default.
    if not enabled:
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if os.getenv("HUB_NATS_TRACE", "0") == "1" or os.getenv("ADAOS_CLI_DEBUG", "0") == "1":
            print("[AdaOS] Windows selector event loop policy enabled (api.server, ADAOS_WIN_SELECTOR_LOOP=1)", file=sys.stderr)
    except Exception:
        pass


_maybe_set_windows_selector_loop()

from adaos.apps.api.auth import require_token
from adaos.build_info import BUILD_INFO
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter

from adaos.apps.bootstrap import init_ctx
from adaos.services.bootstrap import run_boot_sequence, shutdown, is_ready, request_hub_root_reconnect
from adaos.services.observe import start_observer, stop_observer
from adaos.services.agent_context import get_ctx
from adaos.services.router import RouterService
from adaos.services.realtime_sidecar import (
    realtime_sidecar_enabled,
    start_realtime_sidecar_subprocess,
    stop_realtime_sidecar_subprocess,
)
from adaos.services.reliability import ReadinessStatus, set_integration_readiness
from adaos.services.skill.service_supervisor import get_service_supervisor
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.agent_context import get_ctx as _get_ctx
from adaos.services.io_console import print_text
from adaos.services.capacity import install_io_in_capacity, get_local_capacity
from adaos.services.core_update import clear_plan as clear_core_update_plan
from adaos.services.core_update import finalize_runtime_boot_status as finalize_core_update_boot_status
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_plan as read_core_update_plan
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.core_update import write_plan as write_core_update_plan
from adaos.services.core_update import write_status as write_core_update_status
from adaos.services.core_update_policy import core_update_reactions_disabled_reason
from adaos.services.core_slots import active_slot, active_slot_manifest, slot_status as core_slot_status
from adaos.services.node_config import save_config
from adaos.services.runtime_identity import runtime_identity_snapshot, runtime_transition_role
from adaos.services.runtime_lifecycle import (
    is_draining,
    request_drain,
    reset_runtime_lifecycle,
    runtime_lifecycle_snapshot,
)
from adaos.services.runtime_memory_profile import finish_active_runtime_memory_profile
from adaos.services.root_mcp.logs import aggregate_subnet_logs, list_local_logs, normalize_log_category
from adaos.services.supervisor_memory import supervisor_memory_session_artifacts_dir
from adaos.services.subnet_alias import display_subnet_alias, load_subnet_alias, save_subnet_alias
from adaos.domain import Event as DomainEvent

init_ctx()

_DEFAULT_SHUTDOWN_DRAIN_SEC = 5.0
_DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC = 0.2
_DEFAULT_UPDATE_COUNTDOWN_SEC = 60.0


def _write_runtime_profile_shutdown_debug(payload: dict[str, Any]) -> None:
    session_id = str(os.getenv("ADAOS_SUPERVISOR_PROFILE_SESSION_ID") or "").strip()
    if not session_id:
        return
    try:
        path = (supervisor_memory_session_artifacts_dir(session_id) / "runtime-admin-shutdown-debug.json").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        _runtime_log.warning("failed to persist runtime admin shutdown debug payload", exc_info=True)


def _runtime_identity_public_payload() -> dict[str, Any]:
    identity = runtime_identity_snapshot()
    slot_name = ""
    try:
        slot_name = str(active_slot() or "").strip().upper()
    except Exception:
        slot_name = ""
    runtime_port = None
    try:
        runtime_port_value = int(str(os.getenv("ADAOS_RUNTIME_PORT") or "").strip() or "0")
        if runtime_port_value > 0:
            runtime_port = runtime_port_value
    except Exception:
        runtime_port = None
    transition_role = str(identity.get("transition_role") or runtime_transition_role() or "active").strip().lower() or "active"
    return {
        "runtime_instance_id": str(identity.get("runtime_instance_id") or "").strip() or None,
        "transition_role": transition_role,
        "slot": slot_name or None,
        "runtime_port": runtime_port,
        "admin_mutation_allowed": transition_role != "candidate",
    }


def _compact_public_update_status(status: dict[str, Any] | None) -> dict[str, Any]:
    source = status if isinstance(status, dict) else {}
    return {
        "action": str(source.get("action") or "").strip().lower() or None,
        "state": str(source.get("state") or "").strip().lower() or "unknown",
        "phase": str(source.get("phase") or "").strip().lower() or "",
        "message": str(source.get("message") or "").strip(),
        "target_rev": str(source.get("target_rev") or "").strip(),
        "target_version": str(source.get("target_version") or "").strip(),
        "planned_reason": str(source.get("planned_reason") or "").strip() or None,
        "min_update_period_sec": source.get("min_update_period_sec"),
        "scheduled_for": source.get("scheduled_for"),
        "subsequent_transition": bool(source.get("subsequent_transition")),
        "subsequent_transition_requested_at": source.get("subsequent_transition_requested_at"),
        "candidate_prewarm_state": str(source.get("candidate_prewarm_state") or "").strip() or None,
        "candidate_prewarm_message": str(source.get("candidate_prewarm_message") or "").strip() or None,
        "candidate_prewarm_ready_at": source.get("candidate_prewarm_ready_at"),
        "restart_mode": str(source.get("restart_mode") or "").strip() or None,
        "restart_requested_at": source.get("restart_requested_at"),
        "updated_at": source.get("updated_at"),
    }


def _supervisor_manages_sidecar() -> bool:
    raw = str(os.getenv("ADAOS_SUPERVISOR_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _ensure_runtime_admin_mutation_allowed(action: str) -> None:
    info = _runtime_identity_public_payload()
    if str(info.get("transition_role") or "active").strip().lower() != "candidate":
        return
    raise HTTPException(
        status_code=409,
        detail={
            "ok": False,
            "error": "candidate_runtime_is_passive",
            "message": (
                f"candidate runtime rejects {str(action or 'mutation').strip() or 'mutation'} "
                "until supervisor cutover completes"
            ),
            "runtime": info,
        },
    )


async def _wait_bus_idle(timeout: float) -> bool:
    try:
        waiter = getattr(_get_ctx().bus, "wait_for_idle", None)
        if callable(waiter):
            return bool(await waiter(timeout=max(0.0, float(timeout))))
    except Exception:
        logging.getLogger("adaos.eventbus").debug("wait_for_idle failed", exc_info=True)
    return True


async def _emit_shutdown_event(event_type: str, payload: dict[str, Any], *, drain_timeout: float) -> bool:
    try:
        _get_ctx().bus.publish(
            DomainEvent(
                type=event_type,
                payload=payload,
                source="api",
                ts=time.time(),
            )
        )
    except Exception:
        logging.getLogger("adaos.router").warning("failed to publish shutdown event %s", event_type, exc_info=True)
        return False
    return await _wait_bus_idle(drain_timeout)


async def _request_process_shutdown(delay_sec: float = _DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC) -> None:
    await asyncio.sleep(max(0.0, float(delay_sec)))
    signal.raise_signal(signal.SIGINT)


async def _run_core_update_shutdown(app: FastAPI, *, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> None:
    conf = get_ctx().config
    request_drain(reason=reason)
    app.state.shutdown_requested = True
    app.state.shutdown_reason = reason
    app.state.shutdown_drain_timeout = float(drain_timeout_sec)
    await _emit_shutdown_event(
        "subnet.stopping",
        {
            "subnet_id": conf.subnet_id,
            "reason": reason,
        },
        drain_timeout=drain_timeout_sec,
    )
    app.state.shutdown_stopping_emitted = True
    asyncio.create_task(_request_process_shutdown(signal_delay_sec), name="core-update-shutdown")


async def _core_update_countdown_worker(
    app: FastAPI,
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
        await _run_core_update_shutdown(
            app,
            reason=reason,
            drain_timeout_sec=drain_timeout_sec,
            signal_delay_sec=signal_delay_sec,
        )
    except asyncio.CancelledError:
        write_core_update_status(
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
        raise


def _api_state_dir() -> Path:
    raw = get_ctx().paths.state_dir()
    path = raw() if callable(raw) else raw
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _restart_marker_path_from_base(base_url: str | None) -> Path | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = str(parsed.hostname or "").strip()
    port = int(parsed.port or 0)
    if not host or port <= 0:
        return None
    safe_host = host.replace(":", "_").replace("/", "_").replace("\\", "_")
    root = _api_state_dir() / "api"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"restart-{safe_host}-{port}.json"


def _consume_restart_marker(base_url: str | None) -> dict[str, Any] | None:
    path = _restart_marker_path_from_base(base_url)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    if not isinstance(data, dict):
        return None
    try:
        expires_at = float(data.get("expires_at") or 0.0)
    except Exception:
        expires_at = 0.0
    if expires_at and time.time() > expires_at:
        return None
    return data


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) инициализируем AgentContext (публикуется через set_ctx внутри bootstrap_app)

    # 2) только теперь импортируем то, что может косвенно дернуть контекст
    from adaos.apps.api import tool_bridge, subnet_api, observe_api, node_api, scenarios, root_endpoints, skills, stt_api, nlu_teacher_api, join_api
    from adaos.apps.api import io_webhooks
    from adaos.services.yjs.gateway import router as y_router, start_y_server, stop_y_server
    from adaos.services.subnet.link_ws import router as subnet_link_router
    from adaos.services.subnet.runtime import start_subnet_p2p, stop_subnet_p2p

    # 3) монтируем роутеры после bootstrap
    app.include_router(tool_bridge.router, prefix="/api")
    app.include_router(subnet_api.router, prefix="/api")
    app.include_router(nlu_teacher_api.router, prefix="/api")
    app.include_router(node_api.router, prefix="/api/node")
    app.include_router(join_api.router, prefix="/api")
    app.include_router(observe_api.router, prefix="/api/observe")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(stt_api.router, prefix="/api")
    app.include_router(root_endpoints.router)
    # Chat IO webhooks (mounted without /api prefix to keep exact paths)
    app.include_router(io_webhooks.router)
    # Yjs / events gateways (Stage A1)
    app.include_router(y_router)
    # Subnet P2P member link (member->hub)
    app.include_router(subnet_link_router)

    # 3.5) сохранить ссылки на контекст/шину в state для внешних компонентов
    try:
        app.state.ctx = _get_ctx()
        app.state.bus = app.state.ctx.bus
        app.state.shutdown_requested = False
        app.state.shutdown_reason = "signal"
        app.state.shutdown_drain_timeout = _DEFAULT_SHUTDOWN_DRAIN_SEC
        app.state.shutdown_stopping_emitted = False
        reset_runtime_lifecycle()
        app.state.core_update_task = None
        app.state.restart_marker = _consume_restart_marker(os.getenv("ADAOS_SELF_BASE_URL"))
        app.state.realtime_sidecar_proc = None
    except Exception:
        pass

    # 3.6) стартуем RouterService с локальной шиной
    router_service = RouterService(eventbus=app.state.bus, base_dir=app.state.ctx.paths.base_dir())
    app.state.router_service = router_service
    # Periodic liveness staler (hub only)
    staler_task = None

    # 4) поднимаем наблюдатель и выполняем boot-последовательность
    await start_observer()
    # Start Yjs websocket server background task
    try:
        await start_y_server()
    except Exception:
        logging.getLogger("adaos.yjs.gateway").warning("failed to start Yjs websocket server", exc_info=True)
    # Start router early so ui.notify/ui.say from boot sequence are routed.
    try:
        await router_service.start()
    except Exception:
        pass
    try:
        conf = get_ctx().config
        advertised_base = str(os.getenv("ADAOS_SELF_BASE_URL") or "").strip()
        if advertised_base and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                conf.hub_url = advertised_base
                save_config(conf)
    except Exception:
        pass
    try:
        conf = get_ctx().config
        role = str(getattr(conf, "role", "") or "").strip().lower()
        if not _supervisor_manages_sidecar() and realtime_sidecar_enabled(role=role):
            app.state.realtime_sidecar_proc = await start_realtime_sidecar_subprocess(role=role)
    except Exception:
        logging.getLogger("adaos.realtime").warning("failed to start adaos-realtime sidecar", exc_info=True)
    with _StartupTimer("run_boot_sequence"):
        await run_boot_sequence(app)
    # Keep the local capacity projection in sync with optional native deps
    # (vosk/pyttsx3), so other components can see IO availability without importing native libs.
    try:
        from adaos.services.capacity import refresh_native_io_capacity

        with _StartupTimer("refresh_native_io_capacity"):
            await asyncio.to_thread(refresh_native_io_capacity)
    except Exception:
        pass
    try:
        with _StartupTimer("start_subnet_p2p"):
            await start_subnet_p2p(app)
    except Exception:
        pass
    # hub: seed self node into directory (base_url + capacity)
    try:
        with _StartupTimer("seed_subnet_directory"):
            def _seed_subnet_directory() -> None:
                conf = get_ctx().config
                from adaos.services.registry.subnet_directory import get_directory

                directory = get_directory()
                base_url = os.environ.get("ADAOS_SELF_BASE_URL")
                node_item = {
                    "node_id": conf.node_id,
                    "subnet_id": conf.subnet_id,
                    "hostname": platform.node(),
                    "roles": [conf.role],
                    "base_url": base_url,
                    "capacity": get_local_capacity(),
                }
                directory.on_register(node_item)

            await asyncio.to_thread(_seed_subnet_directory)
    except Exception:
        pass

    # 4.5) Hub-only: detect Telegram binding on Root for this subnet and expose IO telegram in capacity.
    tg_enabled = False
    try:
        conf = get_ctx().config
        if conf.role == "hub" and conf.subnet_id:
            ctx = _get_ctx()
            api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
            import requests as _requests

            link_url = f"{api_base.rstrip('/')}/io/tg/pair/link"
            def _safe_get() -> tuple[int, dict[str, Any] | None, str]:
                # requests is sync; never run it on the asyncio event loop thread.
                # Also ignore environment proxy vars (HTTP_PROXY/HTTPS_PROXY), which can otherwise
                # cause long stalls in urllib3 proxy tunneling on some Windows setups.
                sess = _requests.Session()
                try:
                    try:
                        sess.trust_env = False
                    except Exception:
                        pass
                    resp = sess.get(link_url, params={"hub_id": conf.subnet_id}, timeout=(1.5, 1.5))
                    try:
                        js = resp.json() if resp.status_code == 200 else None
                    except Exception:
                        js = None
                    try:
                        txt = (resp.text or "")[:300]
                    except Exception:
                        txt = ""
                    return int(resp.status_code or 0), js if isinstance(js, dict) else None, txt
                finally:
                    try:
                        sess.close()
                    except Exception:
                        pass

            try:
                with _StartupTimer("telegram_binding_probe"):
                    status, js, body_txt = await asyncio.to_thread(_safe_get)
            except Exception as e:
                status = 0
                js = None
                body_txt = f"{type(e).__name__}: {e}"
                logging.getLogger("adaos.io.telegram").warning(
                    "telegram binding probe request failed",
                    extra={"hub_id": conf.subnet_id, "url": link_url, "error_type": type(e).__name__, "error": str(e)},
                )
            link_ok = False
            try:
                link_ok = status == 200 and (js or {}).get("ok")
            except Exception:
                link_ok = False
            if not link_ok:
                try:
                    set_integration_readiness(
                        "telegram",
                        status=ReadinessStatus.DEGRADED,
                        summary="telegram binding not found or root link probe failed",
                        details={"hub_id": conf.subnet_id, "url": link_url, "status": status, "body": body_txt},
                    )
                except Exception:
                    pass
                try:
                    logging.getLogger("adaos.io.telegram").warning(
                        "telegram binding not found or unreachable",
                        extra={"hub_id": conf.subnet_id, "url": link_url, "status": status, "body": body_txt},
                    )
                except Exception:
                    pass
            if link_ok:
                # install telegram IO into capacity and refresh directory snapshot for this node
                install_io_in_capacity("telegram", ["text", "lang:ru", "lang:en"], priority=60)
                try:
                    from adaos.services.registry.subnet_directory import get_directory as _get_dir

                    cap = get_local_capacity()
                    _get_dir().repo.replace_io_capacity(conf.node_id, cap.get("io") or [])
                except Exception:
                    pass
                # Send greeting via Root
                startup_notice_key = "subnet.restarted" if getattr(app.state, "restart_marker", None) else "subnet.started"
                try:
                    from adaos.sdk.data.i18n import _ as _t

                    text = _t(startup_notice_key)
                except Exception:
                    text = startup_notice_key
                alias = display_subnet_alias(
                    load_subnet_alias(subnet_id=conf.subnet_id) or getattr(get_ctx().settings, "default_hub", None),
                    conf.subnet_id,
                )
                try:
                    prefixed_text = f"[{alias}]: {text}" if alias else text
                    send_url = f"{api_base.rstrip('/')}/io/tg/send"

                    def _safe_post() -> tuple[int, str]:
                        sess = _requests.Session()
                        try:
                            try:
                                sess.trust_env = False
                            except Exception:
                                pass
                            resp = sess.post(send_url, json={"hub_id": conf.subnet_id, "text": prefixed_text}, timeout=(1.5, 1.5))
                            try:
                                txt = (resp.text or "")[:300]
                            except Exception:
                                txt = ""
                            return int(resp.status_code or 0), txt
                        finally:
                            try:
                                sess.close()
                            except Exception:
                                pass

                    with _StartupTimer("telegram_startup_send"):
                        st2, body2 = await asyncio.to_thread(_safe_post)
                    if st2 not in (200, 201, 202):
                        try:
                            set_integration_readiness(
                                "telegram",
                                status=ReadinessStatus.DEGRADED,
                                summary="telegram binding exists, but startup send failed",
                                details={"hub_id": conf.subnet_id, "url": send_url, "status": st2, "body": body2},
                            )
                        except Exception:
                            pass
                        logging.getLogger("adaos.router").warning(
                            "telegram broadcast (%s) failed",
                            startup_notice_key,
                            extra={"hub_id": conf.subnet_id, "status": st2, "body": body2},
                        )
                    else:
                        try:
                            set_integration_readiness(
                                "telegram",
                                status=ReadinessStatus.READY,
                                summary="telegram binding and startup send validated",
                                details={"hub_id": conf.subnet_id, "url": send_url, "status": st2},
                            )
                        except Exception:
                            pass
                except Exception:
                    try:
                        set_integration_readiness(
                            "telegram",
                            status=ReadinessStatus.DEGRADED,
                            summary="telegram binding exists, but startup send raised an exception",
                            details={"hub_id": conf.subnet_id, "url": send_url},
                        )
                    except Exception:
                        pass
                    logging.getLogger("adaos.router").warning(
                        "telegram broadcast (%s) exception", startup_notice_key, exc_info=True
                    )
                tg_enabled = True
    except Exception:
        try:
            if tg_enabled:
                set_integration_readiness(
                    "telegram",
                    status=ReadinessStatus.DEGRADED,
                    summary="telegram startup probe aborted by an unexpected exception",
                    details={},
                )
        except Exception:
            pass
        pass
    # Start directory staler on hub to mark nodes offline after TTL
    try:
        conf = get_ctx().config
        if conf.role == "hub":
            import asyncio as _asyncio

            async def _staler():
                directory = get_directory()

                def _tick_directory() -> None:
                    try:
                        directory.on_heartbeat(conf.node_id, None)
                    except Exception:
                        pass
                    try:
                        directory.mark_stale_if_expired(45.0)
                    except Exception:
                        pass

                while True:
                    await _asyncio.to_thread(_tick_directory)
                    await _asyncio.sleep(5.0)

            staler_task = _asyncio.create_task(_staler(), name="subnet-directory-staler")
        else:
            # member: periodically fetch snapshot from hub and ingest locally
            import asyncio as _asyncio
            import requests as _requests

            async def _pull_snapshot():
                directory = get_directory()
                while True:
                    try:
                        if conf.hub_url:
                            url = f"{conf.hub_url.rstrip('/')}/api/subnet/nodes"
                            r = await _asyncio.to_thread(
                                _requests.get,
                                url,
                                headers={"X-AdaOS-Token": conf.token or "dev-local-token"},
                                timeout=3.0,
                            )
                            if r.status_code == 200:
                                payload = r.json() or {}
                                directory.ingest_snapshot(payload.get("nodes") or [])
                    except Exception:
                        pass
                    await _asyncio.sleep(10.0)

            staler_task = _asyncio.create_task(_pull_snapshot(), name="subnet-directory-snapshot-puller")
    except Exception:
        pass

    try:
        yield
    finally:
        try:
            conf = get_ctx().config
            if not getattr(app.state, "shutdown_stopping_emitted", False):
                await _emit_shutdown_event(
                    "subnet.stopping",
                    {
                        "subnet_id": conf.subnet_id,
                        "reason": getattr(app.state, "shutdown_reason", "signal"),
                    },
                    drain_timeout=float(getattr(app.state, "shutdown_drain_timeout", _DEFAULT_SHUTDOWN_DRAIN_SEC)),
                )
                app.state.shutdown_stopping_emitted = True
        except Exception:
            pass
        await stop_observer()
        # Stop ypy-websocket background server so it does not keep the process alive.
        try:
            await stop_y_server()
        except Exception:
            pass
        # On graceful shutdown, notify Telegram and UI if enabled
        try:
            if tg_enabled and str(getattr(app.state, "shutdown_reason", "signal") or "signal") != "cli.restart":
                conf = get_ctx().config
                ctx = _get_ctx()
                api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
                try:
                    from adaos.sdk.data.i18n import _ as _t

                    text = _t("subnet.stopped")
                except Exception:
                    text = "subnet.stopped"
                import requests as _requests

                alias = display_subnet_alias(
                    load_subnet_alias(subnet_id=conf.subnet_id) or getattr(get_ctx().settings, "default_hub", None),
                    conf.subnet_id,
                )
                prefixed_text = f"[{alias}]: {text}" if alias else text
                # Try routed notify first if router is running.
                routed = False
                try:
                    if getattr(router_service, "_started", False):
                        ctx.bus.publish(
                            DomainEvent(
                                type="ui.notify",
                                payload={"text": prefixed_text},
                                source="api",
                                ts=time.time(),
                            )
                        )
                        routed = True
                except Exception:
                    routed = False
                if not routed:
                    r3 = _requests.post(
                        f"{api_base.rstrip('/')}/io/tg/send",
                        json={"hub_id": conf.subnet_id, "text": prefixed_text},
                        timeout=2.5,
                    )
                    if r3.status_code not in (200, 201, 202):
                        logging.getLogger("adaos.router").warning(
                            "telegram broadcast (subnet.stopped) failed",
                            extra={"hub_id": conf.subnet_id, "status": r3.status_code},
                        )
        except Exception:
            pass
        try:
            conf = get_ctx().config
            await _emit_shutdown_event(
                "subnet.stopped",
                {
                    "subnet_id": conf.subnet_id,
                    "reason": getattr(app.state, "shutdown_reason", "signal"),
                },
                drain_timeout=float(getattr(app.state, "shutdown_drain_timeout", _DEFAULT_SHUTDOWN_DRAIN_SEC)),
            )
        except Exception:
            pass
        try:
            await router_service.stop()
        except Exception:
            pass
        try:
            if staler_task:
                staler_task.cancel()
        except Exception:
            pass
        try:
            await stop_subnet_p2p(app)
        except asyncio.CancelledError:
            # Expected during shutdown; don't fail lifespan teardown.
            pass
        except Exception:
            pass
        if not _supervisor_manages_sidecar():
            try:
                await stop_realtime_sidecar_subprocess(getattr(app.state, "realtime_sidecar_proc", None))
            except Exception:
                logging.getLogger("adaos.realtime").warning("failed to stop adaos-realtime sidecar", exc_info=True)
        await shutdown()


# пересоздаём приложение с lifespan
_CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_CORS_ALLOW_HEADERS = ["*"]


app = FastAPI(title="AdaOS API", lifespan=lifespan, version=BUILD_INFO.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # from local web app and routed browser shells
    allow_methods=_CORS_ALLOW_METHODS,
    allow_headers=_CORS_ALLOW_HEADERS,
    allow_credentials=False,  # токен идёт в заголовке, куки не нужны
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


@app.get("/api/ping")
async def ping():
    return {
        "ok": True,
        "ts": time.time(),
        "service": "adaos-runtime",
        "runtime": _runtime_identity_public_payload(),
    }


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str | None = Field(default=None, description="Опционально: имя/идентификатор голоса")


class SayResponse(BaseModel):
    ok: bool
    duration_ms: int


def _make_tts():
    mode = get_tts_backend()
    if mode == "rhasspy":
        return RhasspyTTSAdapter()
    return NativeTTS()


class SetAliasRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=64)
    hub_id: str | None = Field(default=None, description="Optional hub/subnet id; ignored on hub, for logging only.")
    webspace_id: str | None = Field(default=None, max_length=128)


class ShutdownRequest(BaseModel):
    reason: str = Field(default="cli.stop", min_length=1, max_length=128)
    drain_timeout_sec: float = Field(default=_DEFAULT_SHUTDOWN_DRAIN_SEC, ge=0.0, le=30.0)
    signal_delay_sec: float = Field(default=_DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC, ge=0.0, le=5.0)


class ShutdownResponse(BaseModel):
    ok: bool
    accepted: bool
    reason: str
    drain_timeout_sec: float


class DrainRequest(BaseModel):
    reason: str = Field(default="admin.drain", min_length=1, max_length=128)
    drain_timeout_sec: float = Field(default=_DEFAULT_SHUTDOWN_DRAIN_SEC, ge=0.0, le=30.0)


class DrainResponse(BaseModel):
    ok: bool
    accepted: bool
    node_state: str
    reason: str
    drain_timeout_sec: float


class CoreUpdateStartRequest(BaseModel):
    target_rev: str = Field(default="", max_length=128)
    target_version: str = Field(default="", max_length=128)
    reason: str = Field(default="core.update", min_length=1, max_length=128)
    countdown_sec: float = Field(default=_DEFAULT_UPDATE_COUNTDOWN_SEC, ge=0.0, le=3600.0)
    drain_timeout_sec: float = Field(default=_DEFAULT_SHUTDOWN_DRAIN_SEC, ge=0.0, le=30.0)
    signal_delay_sec: float = Field(default=_DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC, ge=0.0, le=5.0)


class CoreUpdateCancelRequest(BaseModel):
    reason: str = Field(default="user.cancelled", min_length=1, max_length=128)


class CoreUpdateRollbackRequest(BaseModel):
    reason: str = Field(default="core.rollback", min_length=1, max_length=128)
    countdown_sec: float = Field(default=0.0, ge=0.0, le=3600.0)
    drain_timeout_sec: float = Field(default=_DEFAULT_SHUTDOWN_DRAIN_SEC, ge=0.0, le=30.0)
    signal_delay_sec: float = Field(default=_DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC, ge=0.0, le=5.0)


class RuntimePromoteActiveRequest(BaseModel):
    reason: str = Field(default="supervisor.fast_cutover", min_length=1, max_length=128)
    reconnect_hub_root: bool = Field(default=True)


@app.post("/api/subnet/alias")
async def set_alias(body: SetAliasRequest, token=Depends(require_token)):
    try:
        conf = get_ctx().config
        save_subnet_alias(body.alias, subnet_id=conf.subnet_id)
        event_payload = {
            "alias": body.alias,
            "subnet_id": conf.subnet_id,
            "webspace_id": body.webspace_id,
        }
        projection_refreshed = False
        try:
            from adaos.services import named_entity_projection

            webspace_ids: list[str] = []
            requested_webspace_id = str(body.webspace_id or "").strip()
            if requested_webspace_id:
                webspace_ids.append(requested_webspace_id)
            default_webspace_id = named_entity_projection.default_webspace_id()
            if default_webspace_id not in webspace_ids:
                webspace_ids.append(default_webspace_id)
            for webspace_id in webspace_ids:
                await asyncio.wait_for(
                    named_entity_projection.project_named_entity_registry(webspace_id=webspace_id),
                    timeout=2.5,
                )
            projection_refreshed = True
        except Exception:
            projection_refreshed = False
        # broadcast over local event bus
        try:
            from adaos.domain import Event as _Ev

            get_ctx().bus.publish(
                _Ev(
                    type="subnet.alias.changed",
                    payload=event_payload,
                    source="api",
                )
            )
        except Exception:
            pass
        return {"ok": True, "alias": body.alias, "projection_refreshed": projection_refreshed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/shutdown", response_model=ShutdownResponse, dependencies=[Depends(require_token)])
async def admin_shutdown(body: ShutdownRequest, background: BackgroundTasks):
    conf = get_ctx().config
    request_drain(reason=body.reason)
    if getattr(app.state, "shutdown_requested", False):
        return ShutdownResponse(
            ok=True,
            accepted=False,
            reason=str(getattr(app.state, "shutdown_reason", body.reason)),
            drain_timeout_sec=float(getattr(app.state, "shutdown_drain_timeout", body.drain_timeout_sec)),
        )

    app.state.shutdown_requested = True
    app.state.shutdown_reason = body.reason
    app.state.shutdown_drain_timeout = float(body.drain_timeout_sec)
    profile_mode = str(os.getenv("ADAOS_SUPERVISOR_PROFILE_MODE") or "normal").strip().lower()
    shutdown_debug_payload: dict[str, Any] = {
        "entered_at": time.time(),
        "reason": body.reason,
        "drain_timeout_sec": float(body.drain_timeout_sec),
        "signal_delay_sec": float(body.signal_delay_sec),
        "profile_mode": profile_mode,
        "session_id": str(os.getenv("ADAOS_SUPERVISOR_PROFILE_SESSION_ID") or "").strip() or None,
        "finish_result": None,
    }
    _runtime_log.info(
        "admin shutdown entered reason=%s profile_mode=%s session_id=%s drain_timeout_sec=%s signal_delay_sec=%s",
        body.reason,
        profile_mode,
        shutdown_debug_payload.get("session_id"),
        body.drain_timeout_sec,
        body.signal_delay_sec,
    )
    _write_runtime_profile_shutdown_debug(shutdown_debug_payload)
    if profile_mode != "normal":
        finish_result = finish_active_runtime_memory_profile()
        shutdown_debug_payload["finish_result"] = finish_result
        shutdown_debug_payload["finished_at"] = time.time()
        _write_runtime_profile_shutdown_debug(shutdown_debug_payload)
        _runtime_log.info(
            "admin shutdown finish result session_id=%s profile_mode=%s result=%s",
            shutdown_debug_payload.get("session_id"),
            profile_mode,
            finish_result,
        )
        if finish_result.get("ok"):
            _runtime_log.info(
                "runtime memory profile finalized during admin shutdown session_id=%s profile_mode=%s finished=%s",
                finish_result.get("session_id"),
                finish_result.get("profile_mode"),
                finish_result.get("finished"),
            )
        else:
            _runtime_log.warning(
                "runtime memory profile finalize during admin shutdown did not complete result=%s",
                finish_result,
            )
    stopping_payload = {
        "subnet_id": conf.subnet_id,
        "reason": body.reason,
    }
    await _emit_shutdown_event(
        "subnet.stopping",
        stopping_payload,
        drain_timeout=body.drain_timeout_sec,
    )
    app.state.shutdown_stopping_emitted = True
    background.add_task(_request_process_shutdown, body.signal_delay_sec)
    return ShutdownResponse(
        ok=True,
        accepted=True,
        reason=body.reason,
        drain_timeout_sec=body.drain_timeout_sec,
    )


@app.post("/api/admin/drain", response_model=DrainResponse, dependencies=[Depends(require_token)])
async def admin_drain(body: DrainRequest):
    conf = get_ctx().config
    was_draining = is_draining()
    lifecycle = request_drain(reason=body.reason)
    await _emit_shutdown_event(
        "subnet.draining",
        {
            "subnet_id": conf.subnet_id,
            "reason": body.reason,
        },
        drain_timeout=body.drain_timeout_sec,
    )
    return DrainResponse(
        ok=True,
        accepted=not was_draining,
        node_state=lifecycle.node_state,
        reason=lifecycle.reason,
        drain_timeout_sec=body.drain_timeout_sec,
    )


@app.get("/api/admin/lifecycle", dependencies=[Depends(require_token)])
async def admin_lifecycle():
    return {"ok": True, "lifecycle": runtime_lifecycle_snapshot(), "runtime": _runtime_identity_public_payload()}


@app.get("/api/admin/root_mcp/logs/{category}", dependencies=[Depends(require_token)])
async def admin_root_mcp_logs(
    category: str,
    limit: int = 5,
    lines: int = 200,
    contains: str | None = None,
    skill: str | None = None,
    file: str | None = None,
    scope: str | None = None,
    include_hub: bool = True,
):
    try:
        category_token = normalize_log_category(category)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown log category: {category}") from exc

    conf = get_ctx().config
    requested_scope = str(scope or "").strip().lower() or "root_local"
    if requested_scope == "subnet_active":
        subnet_id = str(getattr(conf, "subnet_id", None) or "").strip()
        if not subnet_id:
            raise HTTPException(status_code=400, detail="subnet_active logs require configured subnet_id")
        logs = await aggregate_subnet_logs(
            category=category_token,
            subnet_id=subnet_id,
            limit=limit,
            lines=lines,
            contains=contains,
            skill=skill,
            file=file,
            include_hub=include_hub,
        )
        return {"ok": True, "logs": logs}

    return {
        "ok": True,
        "logs": list_local_logs(
            category=category_token,
            limit=limit,
            lines=lines,
            contains=contains,
            skill=skill,
            file=file,
            source_mode="node_local_logs_dir",
        ),
    }


@app.post("/api/admin/update/start", dependencies=[Depends(require_token)])
async def admin_update_start(body: CoreUpdateStartRequest):
    _ensure_runtime_admin_mutation_allowed("update.start")
    disabled_reason = core_update_reactions_disabled_reason()
    if disabled_reason:
        return {
            "ok": True,
            "accepted": False,
            "skipped": True,
            "reason": disabled_reason,
            "status": read_core_update_status(),
        }
    existing = getattr(app.state, "core_update_task", None)
    if existing is not None and not existing.done():
        return {"ok": True, "accepted": False, "status": read_core_update_status()}
    if getattr(app.state, "shutdown_requested", False):
        return {"ok": True, "accepted": False, "status": read_core_update_status()}

    clear_core_update_plan()
    write_core_update_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": str(body.target_rev or ""),
            "target_version": str(body.target_version or ""),
            "reason": body.reason,
            "countdown_sec": float(body.countdown_sec),
            "drain_timeout_sec": float(body.drain_timeout_sec),
            "signal_delay_sec": float(body.signal_delay_sec),
            "started_at": time.time(),
            "scheduled_for": time.time() + float(body.countdown_sec),
        }
    )
    task = asyncio.create_task(
        _core_update_countdown_worker(
            app,
            action="update",
            target_rev=str(body.target_rev or ""),
            target_version=str(body.target_version or ""),
            reason=body.reason,
            countdown_sec=float(body.countdown_sec),
            drain_timeout_sec=float(body.drain_timeout_sec),
            signal_delay_sec=float(body.signal_delay_sec),
        ),
        name="core-update-countdown",
    )
    app.state.core_update_task = task
    return {"ok": True, "accepted": True, "status": read_core_update_status()}


@app.post("/api/admin/update/cancel", dependencies=[Depends(require_token)])
async def admin_update_cancel(body: CoreUpdateCancelRequest):
    _ensure_runtime_admin_mutation_allowed("update.cancel")
    task = getattr(app.state, "core_update_task", None)
    clear_core_update_plan()
    if task is None or task.done():
        status = write_core_update_status(
            {
                "state": "cancelled",
                "phase": "countdown",
                "message": "no pending countdown task",
                "reason": body.reason,
            }
        )
        return {"ok": True, "accepted": False, "status": status}

    task.cancel()
    app.state.core_update_task = None
    status = write_core_update_status(
        {
            "state": "cancelled",
            "phase": "countdown",
            "action": str((read_core_update_status() or {}).get("action") or "update"),
            "message": "core update cancelled by request",
            "reason": body.reason,
            "drain_timeout_sec": float((read_core_update_status() or {}).get("drain_timeout_sec") or _DEFAULT_SHUTDOWN_DRAIN_SEC),
            "signal_delay_sec": float((read_core_update_status() or {}).get("signal_delay_sec") or _DEFAULT_SHUTDOWN_SIGNAL_DELAY_SEC),
        }
    )
    return {"ok": True, "accepted": True, "status": status}


@app.post("/api/admin/update/rollback", dependencies=[Depends(require_token)])
async def admin_update_rollback(body: CoreUpdateRollbackRequest):
    _ensure_runtime_admin_mutation_allowed("update.rollback")
    disabled_reason = core_update_reactions_disabled_reason()
    if disabled_reason:
        return {
            "ok": True,
            "accepted": False,
            "skipped": True,
            "reason": disabled_reason,
            "status": read_core_update_status(),
        }
    existing = getattr(app.state, "core_update_task", None)
    if existing is not None and not existing.done():
        return {"ok": True, "accepted": False, "status": read_core_update_status()}
    if getattr(app.state, "shutdown_requested", False):
        return {"ok": True, "accepted": False, "status": read_core_update_status()}

    clear_core_update_plan()
    write_core_update_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "rollback",
            "reason": body.reason,
            "countdown_sec": float(body.countdown_sec),
            "drain_timeout_sec": float(body.drain_timeout_sec),
            "signal_delay_sec": float(body.signal_delay_sec),
            "started_at": time.time(),
            "scheduled_for": time.time() + float(body.countdown_sec),
        }
    )
    task = asyncio.create_task(
        _core_update_countdown_worker(
            app,
            action="rollback",
            target_rev="",
            target_version="",
            reason=body.reason,
            countdown_sec=float(body.countdown_sec),
            drain_timeout_sec=float(body.drain_timeout_sec),
            signal_delay_sec=float(body.signal_delay_sec),
        ),
        name="core-update-rollback-countdown",
    )
    app.state.core_update_task = task
    return {"ok": True, "accepted": True, "status": read_core_update_status()}


@app.post("/api/admin/runtime/promote-active", dependencies=[Depends(require_token)])
async def admin_runtime_promote_active(body: RuntimePromoteActiveRequest):
    info = _runtime_identity_public_payload()
    current_role = str(info.get("transition_role") or "active").strip().lower() or "active"
    if current_role == "active":
        return {
            "ok": True,
            "accepted": False,
            "message": "runtime already active",
            "reason": body.reason,
            "runtime": _runtime_identity_public_payload(),
            "reconnect": None,
        }

    os.environ["ADAOS_RUNTIME_TRANSITION_ROLE"] = "active"
    try:
        await get_service_supervisor().start_all()
    except Exception:
        logging.getLogger("adaos.runtime").warning(
            "failed to start service skills after candidate promotion",
            exc_info=True,
        )
    reconnect_result: dict[str, Any] | None = None
    if bool(body.reconnect_hub_root):
        try:
            reconnect_result = await request_hub_root_reconnect()
        except Exception as exc:
            reconnect_result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    return {
        "ok": True,
        "accepted": True,
        "message": "candidate runtime promoted to active",
        "reason": body.reason,
        "runtime": _runtime_identity_public_payload(),
        "reconnect": reconnect_result,
    }


@app.get("/api/admin/update/status", dependencies=[Depends(require_token)])
async def admin_update_status():
    try:
        finalized = finalize_core_update_boot_status()
    except Exception:
        finalized = None
    return {
        "ok": True,
        "status": finalized if isinstance(finalized, dict) else read_core_update_status(),
        "last_result": read_core_update_last_result(),
        "plan": read_core_update_plan(),
        "slots": core_slot_status(),
        "active_manifest": active_slot_manifest(),
        "runtime": _runtime_identity_public_payload(),
    }


@app.get("/api/supervisor/public/update-status")
async def supervisor_public_update_status():
    try:
        finalized = finalize_core_update_boot_status()
    except Exception:
        finalized = None
    status_payload = finalized if isinstance(finalized, dict) else read_core_update_status()
    runtime_payload = _runtime_identity_public_payload()
    runtime_payload["runtime_state"] = "ready" if is_ready() else "starting"
    return {
        "ok": True,
        "status": _compact_public_update_status(status_payload),
        "runtime": runtime_payload,
        "_served_by": "runtime_public_update_status",
    }


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status():
    return {
        "ok": True,
        "time": time.time(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "adaos": {
            "version": BUILD_INFO.version,
            "build_date": BUILD_INFO.build_date,
        },
        "lifecycle": runtime_lifecycle_snapshot(),
        "runtime": _runtime_identity_public_payload(),
    }


@app.get("/api/services", dependencies=[Depends(require_token)])
async def list_services(check_health: bool = False) -> dict:
    supervisor = get_service_supervisor()
    names = supervisor.list()
    return {
        "ok": True,
        "services": [supervisor.status(name, check_health=check_health) for name in names],
    }


@app.get("/api/services/{name}", dependencies=[Depends(require_token)])
async def get_service_status(name: str, check_health: bool = False) -> dict:
    supervisor = get_service_supervisor()
    status = supervisor.status(name, check_health=check_health)
    if not status:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True, "service": status}


@app.post("/api/services/{name}/start", dependencies=[Depends(require_token)])
async def start_service(name: str) -> dict:
    supervisor = get_service_supervisor()
    try:
        await supervisor.start(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True}


@app.post("/api/services/{name}/stop", dependencies=[Depends(require_token)])
async def stop_service(name: str) -> dict:
    supervisor = get_service_supervisor()
    # stop is idempotent; 404 only if not configured at all
    if not supervisor.status(name):
        raise HTTPException(status_code=404, detail="service not found")
    await supervisor.stop(name)
    return {"ok": True}


@app.post("/api/services/{name}/restart", dependencies=[Depends(require_token)])
async def restart_service(name: str) -> dict:
    supervisor = get_service_supervisor()
    try:
        await supervisor.restart(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True}


class ServiceIssueRequest(BaseModel):
    type: str
    message: str
    details: dict | None = None


@app.get("/api/services/{name}/issues", dependencies=[Depends(require_token)])
async def get_service_issues(name: str) -> dict:
    supervisor = get_service_supervisor()
    try:
        issues = supervisor.issues(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True, "issues": issues}


@app.post("/api/services/{name}/issue", dependencies=[Depends(require_token)])
async def inject_service_issue(name: str, body: ServiceIssueRequest) -> dict:
    supervisor = get_service_supervisor()
    try:
        await supervisor.inject_issue(name, issue_type=body.type, message=body.message, details=body.details or {})
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True}


class ServiceSelfHealRequest(BaseModel):
    reason: str
    issue: dict | None = None


@app.post("/api/services/{name}/self-heal", dependencies=[Depends(require_token)])
async def service_self_heal(name: str, body: ServiceSelfHealRequest) -> dict:
    supervisor = get_service_supervisor()
    try:
        result = await supervisor.self_heal(name, reason=body.reason, issue=body.issue)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True, "result": result}


@app.get("/api/services/{name}/doctor/requests", dependencies=[Depends(require_token)])
async def get_service_doctor_requests(name: str) -> dict:
    supervisor = get_service_supervisor()
    try:
        items = supervisor.doctor_requests(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True, "requests": items}


class ServiceDoctorRequest(BaseModel):
    reason: str
    issue: dict | None = None


@app.post("/api/services/{name}/doctor/request", dependencies=[Depends(require_token)])
async def request_service_doctor(name: str, body: ServiceDoctorRequest) -> dict:
    supervisor = get_service_supervisor()
    try:
        result = await supervisor.request_doctor(name, reason=body.reason, issue=body.issue)
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True, "request": result}


@app.get("/api/services/{name}/doctor/reports", dependencies=[Depends(require_token)])
async def get_service_doctor_reports(name: str) -> dict:
    """
    Return persisted doctor reports produced by the in-process doctor consumer.

    Reports are stored at: state/services/<skill>/doctor_reports.json
    """
    supervisor = get_service_supervisor()
    status = supervisor.status(name)
    if not status:
        raise HTTPException(status_code=404, detail="service not found")

    # Reuse supervisor state dir logic indirectly via ctx paths.
    ctx = get_ctx()
    state_raw = ctx.paths.state_dir()
    state_dir = Path(state_raw() if callable(state_raw) else state_raw)
    path = state_dir / "services" / name / "doctor_reports.json"
    if not path.exists():
        return {"ok": True, "reports": []}
    try:
        reports = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(reports, list):
            reports = []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to read doctor reports: {exc}") from exc
    return {"ok": True, "reports": reports}


# TODO deprecated use bus instead. No external interface
@app.post("/api/say", response_model=SayResponse, dependencies=[Depends(require_token)])
async def say(payload: SayRequest):
    t0 = time.perf_counter()
    _make_tts().say(payload.text)
    dt = int((time.perf_counter() - t0) * 1000)
    return SayResponse(ok=True, duration_ms=dt)


# --- IO console endpoint for cross-node routing ---
class SayRequestLike(BaseModel):
    text: str
    origin: dict | None = None


# TODO deprecated use bus instead. No external interface
@app.post("/api/io/console/print", dependencies=[Depends(require_token)])
async def io_console_print(payload: SayRequestLike):
    conf = get_ctx().config
    print_text(payload.text, node_id=conf.node_id)
    return {"ok": True}


# --- health endpoints (без авторизации; удобно для оркестраторов/проб) ---
@app.get("/health/live")
async def health_live():
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}


@app.get("/health/ready")
async def health_ready():
    if not is_ready() or is_draining():
        raise HTTPException(status_code=503, detail="not ready")
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}
