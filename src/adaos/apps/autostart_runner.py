from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
import tracemalloc as _tracemalloc
from http import HTTPStatus
from pathlib import Path
from string import Formatter
from typing import Any, Callable
from urllib.parse import quote

import requests
import uvicorn

from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import (
    _advertise_base,
    _cleanup_pidfile,
    _pidfile_path,
    _resolve_bind,
    _stop_previous_server,
    _uvicorn_loop_mode,
    _write_pidfile,
)
from adaos.services.agent_context import get_ctx
from adaos.services.core_update import (
    clear_plan,
    execute_pending_update,
    finalize_runtime_boot_status,
    read_plan,
    read_status,
    resolved_root_promotion_requirement,
    rollback_installed_skill_runtimes,
    write_status,
)
from adaos.services.core_slots import (
    active_slot,
    active_slot_manifest,
    rollback_to_previous_slot,
    slot_dir,
    slot_status,
    write_slot_manifest,
)
from adaos.services.node_config import load_config, save_config
from adaos.services.runtime_refresh import rebuild_webspace_projection_sync
from adaos.services.runtime_memory_profile import (
    RuntimeMemoryProfileSession,
    finish_active_runtime_memory_profile,
    register_active_runtime_memory_profile,
)
from adaos.services.runtime_paths import current_base_dir, current_logs_dir
from adaos.services.root.client import RootHttpClient
from adaos.services.root.core_update_sync import build_core_update_report
_SKIP_PENDING_UPDATE_ENV = "ADAOS_SKIP_PENDING_CORE_UPDATE"
_LOG = logging.getLogger("adaos.autostart")
tracemalloc = _tracemalloc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS API via autostart wrapper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--token", default=None)
    return parser.parse_args()


def _resolved_token(raw_token: str | None = None) -> str | None:
    token = str(raw_token or os.getenv("ADAOS_TOKEN") or "").strip()
    if not token:
        try:
            conf = load_config()
        except Exception:
            conf = None
        token = str(getattr(conf, "token", "") or "").strip() if conf is not None else ""
    return token or None


def _skip_pending_update_requested() -> bool:
    raw = str(os.getenv(_SKIP_PENDING_UPDATE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _runtime_profile_mode() -> str:
    mode = str(os.getenv("ADAOS_SUPERVISOR_PROFILE_MODE") or "normal").strip().lower()
    return mode if mode in {"normal", "sampled_profile", "trace_profile"} else "normal"


def _runtime_profile_session_id() -> str | None:
    token = str(os.getenv("ADAOS_SUPERVISOR_PROFILE_SESSION_ID") or "").strip()
    return token or None


def _runtime_profile_trigger() -> str | None:
    token = str(os.getenv("ADAOS_SUPERVISOR_PROFILE_TRIGGER") or "").strip()
    return token or None


class _RuntimeMemoryProfileSession(RuntimeMemoryProfileSession):
    def __init__(self) -> None:
        super().__init__(
            profile_mode=_runtime_profile_mode(),
            session_id=_runtime_profile_session_id(),
            profile_trigger=_runtime_profile_trigger(),
        )


def _install_runtime_profile_signal_handlers(
    runtime_memory_profile: RuntimeMemoryProfileSession,
) -> Callable[[], None]:
    previous_handlers: dict[int, Any] = {}
    registered: list[int] = []

    def _handler(signum: int, _frame: Any) -> None:
        finish_result = finish_active_runtime_memory_profile()
        if not finish_result.get("ok"):
            _LOG.warning("runtime memory profile finalize on signal did not complete result=%s", finish_result)
        raise SystemExit(128 + int(signum))

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            previous_handlers[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _handler)
            registered.append(int(sig))
        except Exception:
            continue

    def _restore() -> None:
        for sig in registered:
            previous = previous_handlers.get(sig, signal.SIG_DFL)
            with contextlib.suppress(Exception):
                signal.signal(sig, previous)

    return _restore


def _format_slot_value(template: str, values: dict[str, str]) -> str:
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    payload = dict(values)
    for field in fields:
        payload.setdefault(field, "")
    return template.format(**payload)


def _slot_launch_spec(manifest: dict[str, object], *, host: str, port: int, token: str | None) -> tuple[list[str] | None, str | None]:
    slot = str(manifest.get("slot") or "").strip().upper()
    try:
        base_dir_text = str(current_base_dir())
    except Exception:
        base_dir_text = str(os.getenv("ADAOS_BASE_DIR") or "")
    values = {
        "host": str(host),
        "port": str(port),
        "token": str(token or ""),
        "slot": slot,
        "slot_dir": str(slot_dir(slot)) if slot else "",
        "base_dir": base_dir_text,
        "python": os.sys.executable,
    }
    argv_raw = manifest.get("argv")
    if isinstance(argv_raw, list):
        argv = [_format_slot_value(str(item), values) for item in argv_raw if str(item).strip()]
        if argv:
            return argv, None
    command = str(manifest.get("command") or "").strip()
    if command:
        return None, _format_slot_value(command, values)
    return None, None


def _upload_update_report(status: dict[str, object], conf) -> None:
    try:
        base_url = str(getattr(getattr(conf, "root_settings", None), "base_url", None) or "").rstrip("/")
        cert_path = conf.hub_cert_path()
        key_path = conf.hub_key_path()
        ca_path = conf.ca_cert_path()
        if not base_url or not cert_path.exists() or not key_path.exists():
            return
        verify: str | bool = str(ca_path) if ca_path.exists() else True
        client = RootHttpClient(base_url=base_url, verify=verify, cert=(str(cert_path), str(key_path)))
        payload = build_core_update_report(conf)
        payload["status"] = status
        payload["reported_at"] = time.time()
        client.hub_core_update_report(payload=payload)
    except Exception:
        return


def _update_validation_timeout_sec() -> float:
    try:
        return max(1.0, float(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_TIMEOUT_SEC") or "45"))
    except Exception:
        return 45.0


def _update_validation_strict() -> bool:
    raw = str(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _update_validation_runtime_guards_enabled() -> bool:
    raw = os.getenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _reconcile_post_root_promotion_restart(current: dict[str, Any]) -> dict[str, Any] | None:
    state = str(current.get("state") or "").strip().lower()
    phase = str(current.get("phase") or "").strip().lower()
    if state != "succeeded" or phase != "root_promoted":
        return None
    payload = dict(current)
    payload["message"] = "root promotion restart in progress; awaiting runtime boot validation under updated supervisor"
    payload["candidate_prewarm_state"] = None
    payload["candidate_prewarm_message"] = None
    payload["candidate_prewarm_ready_at"] = None
    payload["updated_at"] = time.time()
    return payload


def _reconcile_interrupted_update_transition(current: dict[str, Any], *, plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(plan, dict) and plan:
        return None
    state = str(current.get("state") or "").strip().lower()
    phase = str(current.get("phase") or "").strip().lower()
    if state not in {"restarting", "applying"}:
        return None
    if state == "restarting" and phase in {"launch", "validate", "root_promoted", "root_promotion_pending"}:
        return None
    payload = dict(current)
    payload["state"] = "failed"
    payload["phase"] = phase or ("apply" if state == "applying" else "launch")
    payload["message"] = "core update transition was interrupted before validation commit"
    payload["interrupted_transition_state"] = state
    payload["interrupted_transition_phase"] = phase or None
    payload["updated_at"] = time.time()
    payload["finished_at"] = time.time()
    return payload


def _resume_post_apply_update_transition(current: dict[str, Any], *, plan: dict[str, Any] | None) -> bool:
    if not isinstance(plan, dict) or not plan:
        return False
    state = str(current.get("state") or "").strip().lower()
    phase = str(current.get("phase") or "").strip().lower()
    if state != "restarting" or phase not in {"launch", "validate", "root_promoted", "root_promotion_pending"}:
        return False
    plan_action = str(plan.get("action") or "update").strip().lower()
    if plan_action != "update":
        return False
    plan_target_slot = str(plan.get("target_slot") or "").strip().upper() or None
    status_target_slot = str(current.get("target_slot") or "").strip().upper() or None
    if plan_target_slot and status_target_slot and plan_target_slot != status_target_slot:
        return False
    return True


def _update_validation_webspace_id() -> str:
    value = str(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_WEBSPACE_ID") or "default").strip()
    return value or "default"


def _post_commit_quarantine_summary(report: dict[str, Any]) -> str:
    if not isinstance(report, dict):
        return ""
    items: list[str] = []
    for item in report.get("skills") or []:
        if not isinstance(item, dict) or not bool(item.get("deactivated")):
            continue
        deactivation = item.get("deactivation") if isinstance(item.get("deactivation"), dict) else {}
        if not bool(deactivation.get("committed_core_switch")):
            continue
        skill_label = str(item.get("skill") or "skill")
        failure_kind = str(item.get("failure_kind") or deactivation.get("failure_kind") or "").strip()
        failed_stage = str(item.get("failed_stage") or deactivation.get("failed_stage") or "failed").strip() or "failed"
        label = f"{skill_label}:{failure_kind + '/' if failure_kind else ''}{failed_stage}"
        items.append(label)
    if not items:
        return ""
    return ", ".join(items[:3]) + (f" (+{len(items) - 3})" if len(items) > 3 else "")


def _validation_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = str(token)
    return headers


def _required_update_checks(base_url: str) -> list[tuple[str, bool]]:
    if not _update_validation_strict():
        return [(f"{base_url}/api/ping", False)]
    return [
        (f"{base_url}/api/ping", False),
        (f"{base_url}/api/status", True),
        (f"{base_url}/api/admin/update/status", True),
    ]


def _validate_sidecar_runtime_payload(payload: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    enabled = bool(runtime.get("enabled"))
    status = str(runtime.get("status") or "").strip().lower() or ("enabled" if enabled else "disabled")
    listener_running = bool(process.get("listener_running") or str(runtime.get("local_listener_state") or "").strip().lower() == "ready")
    transport_ready = bool(runtime.get("transport_ready"))
    details = {
        "enabled": enabled,
        "status": status,
        "listener_running": listener_running,
        "transport_ready": transport_ready,
        "control_ready": runtime.get("control_ready"),
    }
    if not enabled:
        return True, None, details
    if not listener_running:
        return False, "sidecar runtime is enabled but local listener is not running", details
    if not transport_ready:
        return False, "sidecar runtime listener is up but hub-root transport is not ready", details
    return True, None, details


def _validate_yjs_runtime_payload(
    payload: dict[str, Any],
    *,
    expected_webspace_id: str | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    transport = runtime.get("transport") if isinstance(runtime.get("transport"), dict) else {}
    available = bool(runtime.get("available"))
    assessment_state = str(assessment.get("state") or "").strip().lower()
    selected_webspace_id = str(runtime.get("selected_webspace_id") or "").strip() or None
    server_ready = transport.get("server_ready")
    details = {
        "available": available,
        "assessment_state": assessment_state or None,
        "assessment_reason": assessment.get("reason"),
        "selected_webspace_id": selected_webspace_id,
        "server_ready": bool(server_ready) if isinstance(server_ready, bool) else None,
        "server_requested": bool(transport.get("server_requested")),
        "server_task_running": bool(transport.get("server_task_running")),
        "server_error": transport.get("server_error"),
    }
    if not available:
        reason = str(assessment.get("reason") or "Yjs runtime is unavailable").strip() or "Yjs runtime is unavailable"
        return False, reason, details
    if assessment_state == "unavailable":
        reason = str(assessment.get("reason") or "Yjs runtime is unavailable").strip() or "Yjs runtime is unavailable"
        return False, reason, details
    if isinstance(server_ready, bool) and not server_ready:
        reason = str(transport.get("server_error") or assessment.get("reason") or "Yjs websocket server is not ready").strip()
        return False, reason or "Yjs websocket server is not ready", details
    if expected_webspace_id and selected_webspace_id and selected_webspace_id != expected_webspace_id:
        return (
            False,
            f"Yjs runtime selected_webspace_id={selected_webspace_id}, expected {expected_webspace_id}",
            details,
        )
    return True, None, details


def _runtime_update_checks(base_url: str) -> list[tuple[str, bool, str, Any]]:
    if not _update_validation_runtime_guards_enabled():
        return []
    webspace_id = _update_validation_webspace_id()
    return [
        (
            f"{base_url}/api/node/sidecar/status",
            True,
            "sidecar_runtime",
            _validate_sidecar_runtime_payload,
        ),
        (
            f"{base_url}/api/node/yjs/webspaces/{quote(webspace_id, safe='')}/runtime",
            True,
            "yjs_runtime",
            lambda payload: _validate_yjs_runtime_payload(payload, expected_webspace_id=webspace_id),
        ),
    ]


def _tail_text(path: Path, *, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:].strip()


def _validation_log_paths(slot: str | None) -> tuple[Path, Path]:
    logs_dir = current_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    suffix = str(slot or "unknown").strip().upper() or "UNKNOWN"
    return (
        (logs_dir / f"autostart-slot-{suffix}.out.log").resolve(),
        (logs_dir / f"autostart-slot-{suffix}.err.log").resolve(),
    )


def _run_post_commit_skill_checks() -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "adaos.apps.skill_runtime_migrate",
            "--json",
            "--post-commit",
            "--deactivate-on-failure",
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"post-commit skill checks failed rc={completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[-4000:]}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"post-commit skill checks returned invalid JSON: {(completed.stdout or '')[-4000:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("post-commit skill checks returned non-object payload")
    return payload


def _slot_python_executable(slot: str | None) -> Path:
    slot_name = str(slot or "").strip().upper()
    base = Path(slot_dir(slot_name))
    candidates = [
        base / "venv" / "bin" / "python",
        base / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _run_slot_cli_smoke_check(
    slot: str | None,
    *,
    manifest: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    slot_name = str(slot or "").strip().upper()
    manifest_payload = manifest if isinstance(manifest, dict) else {}
    effective_env = dict(env or os.environ)
    effective_cwd = cwd
    if effective_cwd is None:
        cwd_raw = str(manifest_payload.get("cwd") or "").strip()
        if cwd_raw:
            effective_cwd = Path(cwd_raw).expanduser().resolve()
        else:
            effective_cwd = (Path(slot_dir(slot_name)) / "repo").resolve()
    python_executable = _slot_python_executable(slot_name)
    command = [
        str(python_executable),
        "-c",
        "from adaos.apps.cli.app import app as _app; print('cli_import_ok')",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=effective_env,
        cwd=str(effective_cwd) if effective_cwd else None,
    )
    result = {
        "ok": completed.returncode == 0,
        "slot": slot_name,
        "python": str(python_executable),
        "cwd": str(effective_cwd) if effective_cwd else "",
        "returncode": int(completed.returncode),
        "stdout": (completed.stdout or "").strip()[-1000:],
        "stderr": (completed.stderr or "").strip()[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(
            f"slot {slot_name} CLI smoke check failed rc={completed.returncode}: "
            f"{result['stderr'] or result['stdout'] or 'no output'}"
        )
    return result


def _run_prepared_restart_skill_migration(slot: str, manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    slot_name = str(slot or "").strip().upper()
    if not slot_name:
        raise RuntimeError("prepared restart is missing target slot")
    current_migration = manifest.get("skill_runtime_migration") if isinstance(manifest.get("skill_runtime_migration"), dict) else {}
    if current_migration and not bool(current_migration.get("deferred")):
        return dict(current_migration), dict(manifest)

    env = dict(os.environ)
    manifest_env = manifest.get("env")
    if isinstance(manifest_env, dict):
        for key, value in manifest_env.items():
            env[str(key)] = str(value)
    env["ADAOS_ACTIVE_CORE_SLOT"] = slot_name
    env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = str(slot_dir(slot_name))
    cwd_raw = str(manifest.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else None
    completed = subprocess.run(
        [sys.executable, "-m", "adaos.apps.skill_runtime_migrate", "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"deferred skill runtime migration failed rc={completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[-4000:]}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(
            f"deferred skill runtime migration returned invalid JSON: {(completed.stdout or '')[-4000:]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("deferred skill runtime migration returned non-object payload")
    safe_for_core_update = bool(payload.get("safe_for_core_update"))
    if not bool(payload.get("ok")) and not safe_for_core_update:
        raise RuntimeError(f"deferred skill runtime migration failed: {json.dumps(payload, ensure_ascii=False)}")
    updated_manifest = dict(manifest)
    updated_manifest["skill_runtime_migration"] = payload
    write_slot_manifest(slot_name, updated_manifest)
    return payload, updated_manifest


def _defer_prepared_restart_skill_migration_on_pressure() -> bool:
    raw = (
        str(os.getenv("ADAOS_CORE_UPDATE_DEFER_SKILL_MIGRATION_ON_PRESSURE") or "1")
        .strip()
        .lower()
    )
    return raw not in {"0", "false", "no", "off"}


def _prepared_restart_skill_migration_defer_reason(
    *,
    plan: dict[str, Any],
    status: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> str | None:
    if not _defer_prepared_restart_skill_migration_on_pressure():
        return None
    current_migration = (
        manifest.get("skill_runtime_migration")
        if isinstance(manifest.get("skill_runtime_migration"), dict)
        else {}
    )
    if current_migration and not bool(current_migration.get("deferred")):
        return None

    status_payload = status if isinstance(status, dict) else {}
    candidate_state = str(
        plan.get("candidate_prewarm_state")
        or status_payload.get("candidate_prewarm_state")
        or ""
    ).strip().lower()
    pressure_text = " ".join(
        str(item or "")
        for item in [
            plan.get("candidate_prewarm_message"),
            plan.get("warm_switch_reason"),
            status_payload.get("candidate_prewarm_message"),
            status_payload.get("warm_switch_reason"),
        ]
    ).lower()
    if candidate_state == "skipped" and "insufficient memory" in pressure_text:
        return "pressure_stop_and_switch"
    return None


def _defer_prepared_restart_skill_migration(
    slot: str,
    manifest: dict[str, Any],
    *,
    plan: dict[str, Any],
    status: dict[str, Any] | None,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    slot_name = str(slot or "").strip().upper()
    if not slot_name:
        raise RuntimeError("prepared restart is missing target slot")
    status_payload = status if isinstance(status, dict) else {}
    payload: dict[str, Any] = {
        "ok": True,
        "deferred": True,
        "safe_for_core_update": True,
        "reason": reason,
        "defer_reason": reason,
        "phase": "prepared_restart.launch",
        "message": (
            "skill runtime migration deferred so the prepared runtime can restore "
            "the control plane under pressure"
        ),
        "target_slot": slot_name,
        "candidate_prewarm_state": str(
            plan.get("candidate_prewarm_state")
            or status_payload.get("candidate_prewarm_state")
            or ""
        ).strip() or None,
        "candidate_prewarm_message": str(
            plan.get("candidate_prewarm_message")
            or status_payload.get("candidate_prewarm_message")
            or ""
        ).strip() or None,
        "deferred_at": time.time(),
        "total": 0,
        "failed_total": 0,
        "rollback_total": 0,
        "deactivated_total": 0,
        "skills": [],
    }
    updated_manifest = dict(manifest)
    updated_manifest["skill_runtime_migration"] = payload
    write_slot_manifest(slot_name, updated_manifest)
    return payload, updated_manifest


def _probe_update_runtime(
    *,
    host: str,
    port: int,
    token: str | None,
    timeout_sec: float,
    expected_slot: str | None = None,
    proc: subprocess.Popen | None = None,
) -> tuple[bool, dict[str, Any]]:
    base_url = f"http://{host}:{int(port)}"
    timeout_sec = max(1.0, float(timeout_sec))
    deadline = time.time() + timeout_sec
    last_error = "runtime validation did not start"
    headers = _validation_headers(token)
    strict = _update_validation_strict()
    attempts = 0
    last_attempt: dict[str, Any] = {}
    while time.time() < deadline:
        if proc is not None:
            try:
                rc = proc.poll()
            except Exception:
                rc = None
            if rc is not None:
                last_error = f"slot process exited before becoming ready (rc={int(rc)})"
                return False, {
                    "ok": False,
                    "summary": last_error,
                    "base_url": base_url,
                    "timeout_sec": timeout_sec,
                    "attempts": attempts,
                    "token_present": bool(token),
                    "strict": strict,
                    "runtime_guards": bool(_runtime_update_checks(base_url)),
                    "last_attempt": last_attempt,
                    "proc_returncode": int(rc),
                }
        attempts += 1
        attempt: dict[str, Any] = {
            "ts": time.time(),
            "checks": [],
            "warnings": [],
        }
        for url, need_token in _required_update_checks(base_url):
            check: dict[str, Any] = {
                "url": url,
                "requires_token": bool(need_token),
            }
            try:
                response = requests.get(
                    url,
                    headers=headers if need_token else {"Accept": "application/json"},
                    timeout=1.5,
                )
                check["status_code"] = int(response.status_code)
                if response.status_code != HTTPStatus.OK:
                    last_error = f"{url} returned {response.status_code}"
                    check["ok"] = False
                    check["error"] = last_error
                    attempt["checks"].append(check)
                    break
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("ok") is False:
                    last_error = f"{url} returned invalid payload"
                    check["ok"] = False
                    check["error"] = last_error
                    attempt["checks"].append(check)
                    break
                if expected_slot and url.endswith("/api/admin/update/status"):
                    slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
                    active_manifest = payload.get("active_manifest") if isinstance(payload.get("active_manifest"), dict) else {}
                    active_slot_name = str(slots.get("active_slot") or active_manifest.get("slot") or "").strip().upper()
                    check["active_slot"] = active_slot_name
                    if active_slot_name and active_slot_name != str(expected_slot).strip().upper():
                        last_error = f"{url} returned active_slot={active_slot_name}, expected {str(expected_slot).strip().upper()}"
                        check["ok"] = False
                        check["error"] = last_error
                        attempt["checks"].append(check)
                        break
                check["ok"] = True
                check["payload_keys"] = sorted(str(key) for key in payload.keys())
                attempt["checks"].append(check)
            except Exception as exc:
                last_error = f"{url} probe failed: {exc}"
                check["ok"] = False
                check["error"] = str(exc)
                attempt["checks"].append(check)
                break
        else:
            if not strict:
                for url, need_token in [
                    (f"{base_url}/api/status", True),
                    (f"{base_url}/api/admin/update/status", True),
                ]:
                    check = {
                        "url": url,
                        "requires_token": bool(need_token),
                        "advisory": True,
                    }
                    try:
                        response = requests.get(
                            url,
                            headers=headers if need_token else {"Accept": "application/json"},
                            timeout=1.5,
                        )
                        check["status_code"] = int(response.status_code)
                        if response.status_code != HTTPStatus.OK:
                            check["ok"] = False
                            check["error"] = f"{url} returned {response.status_code}"
                            attempt["warnings"].append(dict(check))
                            attempt["checks"].append(check)
                            continue
                        payload = response.json()
                        if not isinstance(payload, dict) or payload.get("ok") is False:
                            check["ok"] = False
                            check["error"] = f"{url} returned invalid payload"
                            attempt["warnings"].append(dict(check))
                            attempt["checks"].append(check)
                            continue
                        if expected_slot and url.endswith("/api/admin/update/status"):
                            slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
                            active_manifest = payload.get("active_manifest") if isinstance(payload.get("active_manifest"), dict) else {}
                            active_slot_name = str(slots.get("active_slot") or active_manifest.get("slot") or "").strip().upper()
                            check["active_slot"] = active_slot_name
                            if active_slot_name and active_slot_name != str(expected_slot).strip().upper():
                                last_error = f"{url} returned active_slot={active_slot_name}, expected {str(expected_slot).strip().upper()}"
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                break
                        check["ok"] = True
                        check["payload_keys"] = sorted(str(key) for key in payload.keys())
                        attempt["checks"].append(check)
                    except Exception as exc:
                        check["ok"] = False
                        check["error"] = str(exc)
                        attempt["warnings"].append(dict(check))
                        attempt["checks"].append(check)
                else:
                    runtime_checks = _runtime_update_checks(base_url)
                    runtime_failed = False
                    for runtime_url, runtime_need_token, runtime_check_id, runtime_validator in runtime_checks:
                        check = {
                            "url": runtime_url,
                            "requires_token": bool(runtime_need_token),
                            "check_id": runtime_check_id,
                        }
                        try:
                            response = requests.get(
                                runtime_url,
                                headers=headers if runtime_need_token else {"Accept": "application/json"},
                                timeout=1.5,
                            )
                            check["status_code"] = int(response.status_code)
                            if response.status_code != HTTPStatus.OK:
                                last_error = f"{runtime_url} returned {response.status_code}"
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            payload = response.json()
                            if not isinstance(payload, dict) or payload.get("ok") is False:
                                last_error = f"{runtime_url} returned invalid payload"
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            valid, error_text, details = runtime_validator(payload)
                            check["ok"] = bool(valid)
                            check["payload_keys"] = sorted(str(key) for key in payload.keys())
                            if details:
                                check["details"] = details
                            if not valid:
                                last_error = str(error_text or f"{runtime_url} failed runtime validation")
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            attempt["checks"].append(check)
                        except Exception as exc:
                            last_error = f"{runtime_url} probe failed: {exc}"
                            check["ok"] = False
                            check["error"] = str(exc)
                            attempt["checks"].append(check)
                            runtime_failed = True
                            break
                    if runtime_failed:
                        last_attempt = attempt
                        time.sleep(0.5)
                        continue
                    return True, {
                        "ok": True,
                        "summary": "ok" if not attempt["warnings"] else "ok_with_warnings",
                        "base_url": base_url,
                        "timeout_sec": timeout_sec,
                        "attempts": attempts,
                        "token_present": bool(token),
                        "strict": strict,
                        "runtime_guards": bool(runtime_checks),
                        "last_attempt": attempt,
                    }
                last_attempt = attempt
                time.sleep(0.5)
                continue
            return True, {
                "ok": True,
                "summary": "ok",
                "base_url": base_url,
                "timeout_sec": timeout_sec,
                "attempts": attempts,
                "token_present": bool(token),
                "strict": strict,
                "runtime_guards": bool(_runtime_update_checks(base_url)),
                "last_attempt": attempt,
            }
        last_attempt = attempt
        time.sleep(0.5)
    return False, {
        "ok": False,
        "summary": last_error,
        "base_url": base_url,
        "timeout_sec": timeout_sec,
        "attempts": attempts,
        "token_present": bool(token),
        "strict": strict,
        "runtime_guards": bool(_runtime_update_checks(base_url)),
        "last_attempt": last_attempt,
    }


def _launch_active_slot_if_needed(args: argparse.Namespace, *, host: str, port: int, validate: bool = False) -> None:
    slot = active_slot()
    if not slot:
        return
    if str(os.getenv("ADAOS_ACTIVE_CORE_SLOT") or "").strip().upper() == slot:
        return
    manifest = active_slot_manifest()
    if not isinstance(manifest, dict):
        return
    resolved_token = _resolved_token(args.token)
    argv, command = _slot_launch_spec(manifest, host=host, port=port, token=resolved_token)
    if not argv and not command:
        write_status(
            {
                "state": "failed",
                "phase": "launch",
                "message": f"active slot {slot} manifest has no launch command",
                "slot_status": slot_status(),
            }
        )
        raise SystemExit(2)
    env = dict(os.environ)
    manifest_env = manifest.get("env")
    if isinstance(manifest_env, dict):
        for key, value in manifest_env.items():
            env[str(key)] = str(value)
    env["ADAOS_ACTIVE_CORE_SLOT"] = slot
    env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = str(slot_dir(slot))
    if resolved_token:
        env["ADAOS_TOKEN"] = str(resolved_token)
    env[_SKIP_PENDING_UPDATE_ENV] = "1"
    cwd_raw = str(manifest.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else None
    if validate:
        started_at = time.time()
        stdout_path, stderr_path = _validation_log_paths(slot)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("w", encoding="utf-8", buffering=1) as stdout_fh, stderr_path.open("w", encoding="utf-8", buffering=1) as stderr_fh:
            proc = subprocess.Popen(
                argv or command or [],
                shell=bool(command),
                env=env,
                cwd=str(cwd) if cwd else None,
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
            ok, details = _probe_update_runtime(
                host=host,
                port=port,
                token=resolved_token,
                timeout_sec=_update_validation_timeout_sec(),
                expected_slot=slot,
                proc=proc,
            )
            if ok:
                post_commit_skill_checks: dict[str, Any] = {}
                cli_smoke_check: dict[str, Any] = {}
                post_commit_note = ""
                post_commit_webspace_refresh: dict[str, Any] = {}
                try:
                    post_commit_skill_checks = _run_post_commit_skill_checks()
                except Exception as exc:
                    post_commit_skill_checks = {
                        "ok": False,
                        "failed_total": 0,
                        "deactivated_total": 0,
                        "error": str(exc),
                    }
                    post_commit_note = " | post-commit skill checks failed to execute"
                else:
                    failed_total = int(post_commit_skill_checks.get("failed_total") or 0)
                    deactivated_total = int(post_commit_skill_checks.get("deactivated_total") or 0)
                    if failed_total or deactivated_total:
                        quarantine_summary = _post_commit_quarantine_summary(post_commit_skill_checks)
                        post_commit_note = (
                            f" | skills degraded after commit"
                            f" failed={failed_total}"
                            f" deactivated={deactivated_total}"
                        )
                        if quarantine_summary:
                            post_commit_note += f" quarantine={quarantine_summary}"
                try:
                    cli_smoke_check = _run_slot_cli_smoke_check(
                        slot,
                        manifest=manifest,
                        env=env,
                        cwd=cwd,
                    )
                except Exception as exc:
                    ok = False
                    details = {
                        "ok": False,
                        "summary": f"post-core-update CLI smoke check failed: {exc}",
                        "cli_smoke_check": {
                            "ok": False,
                            "error": str(exc),
                            "slot": slot,
                        },
                    }
                try:
                    if ok:
                        post_commit_webspace_refresh = rebuild_webspace_projection_sync(
                            webspace_id=_update_validation_webspace_id(),
                            action="core_update_post_boot_sync",
                            source_of_truth="scenario_projection",
                        )
                except Exception as exc:
                    ok = False
                    details = {
                        "ok": False,
                        "summary": f"post-core-update webspace refresh failed: {exc}",
                        "post_commit_webspace_refresh": {
                            "ok": False,
                            "error": str(exc),
                            "webspace_id": _update_validation_webspace_id(),
                        },
                    }
                if ok:
                    root_promotion_required, bootstrap_update = resolved_root_promotion_requirement(manifest)
                    status_state = "validated" if root_promotion_required else "succeeded"
                    status_phase = "root_promotion_pending" if root_promotion_required else "validate"
                    status_message = (
                        f"slot {slot} passed post-switch validation; root promotion pending{post_commit_note}"
                        if root_promotion_required
                        else f"slot {slot} passed post-switch validation{post_commit_note}"
                    )
                    write_status(
                        {
                            "state": status_state,
                            "phase": status_phase,
                            "message": status_message,
                            "target_slot": slot,
                            "manifest": manifest,
                            "validated_at": time.time(),
                            "skill_post_commit_checks": post_commit_skill_checks,
                            "cli_smoke_check": cli_smoke_check,
                            "post_commit_webspace_refresh": post_commit_webspace_refresh,
                            "root_promotion_required": root_promotion_required,
                            "bootstrap_update": bootstrap_update,
                            "validation_logs": {
                                "stdout_path": str(stdout_path),
                                "stderr_path": str(stderr_path),
                            },
                        }
                    )
                    clear_plan()
                    raise SystemExit(int(proc.wait()))
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except Exception:
                proc.kill()
        validation_stdout = _tail_text(stdout_path)
        validation_stderr = _tail_text(stderr_path)
        restored = rollback_to_previous_slot()
        skill_runtime_rollback = rollback_installed_skill_runtimes() if restored else {}
        payload: dict[str, Any] = {
            "state": "failed",
            "phase": "validate",
            "message": f"slot {slot} failed post-switch validation",
            "validation_error": details,
            "validation_error_summary": str(details.get("summary") or "validation failed") if isinstance(details, dict) else str(details or "validation failed"),
            "validation_stdout": validation_stdout,
            "validation_stderr": validation_stderr,
            "validation_logs": {
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
            "target_slot": slot,
            "manifest": manifest,
            "started_at": started_at,
            "finished_at": time.time(),
            "restored_slot": restored or "",
        }
        if restored:
            payload["rollback"] = {"ok": True, "slot": restored}
        if skill_runtime_rollback:
            payload["skill_runtime_rollback"] = skill_runtime_rollback
            if not bool(skill_runtime_rollback.get("ok")):
                payload["message"] += " | some skill runtime rollbacks failed"
        write_status(payload)
        clear_plan()
        raise SystemExit(1)
    completed = subprocess.run(argv or command or [], shell=bool(command), env=env, cwd=str(cwd) if cwd else None)
    raise SystemExit(int(completed.returncode))


def main() -> None:
    args = _parse_args()
    token = _resolved_token(args.token)
    phase = "init"
    pidfile: Path | None = None
    try:
        phase = "init_ctx"
        init_ctx()
        phase = "read_plan"
        skip_pending_update = _skip_pending_update_requested()
        plan = None if skip_pending_update else read_plan()
        pending_update_succeeded = False
        prepared_restart_boot = False
        current_status = read_status()
        conf = None
        try:
            conf = load_config()
        except Exception:
            conf = None
        if plan:
            plan_state = str(plan.get("state") or "").strip().lower()
            plan_action = str(plan.get("action") or "update").strip().lower()
            if plan_state == "prepared_restart" and plan_action == "update":
                phase = "prepared_restart"
                target_slot = str(plan.get("target_slot") or active_slot() or "").strip().upper()
                manifest = active_slot_manifest()
                manifest = dict(manifest) if isinstance(manifest, dict) else {}
                try:
                    migration_defer_reason = _prepared_restart_skill_migration_defer_reason(
                        plan=plan,
                        status=current_status,
                        manifest=manifest,
                    )
                    if migration_defer_reason:
                        skill_runtime_migration, manifest = _defer_prepared_restart_skill_migration(
                            target_slot,
                            manifest,
                            plan=plan,
                            status=current_status,
                            reason=migration_defer_reason,
                        )
                    else:
                        skill_runtime_migration, manifest = _run_prepared_restart_skill_migration(target_slot, manifest)
                except Exception as exc:
                    clear_plan()
                    restored = rollback_to_previous_slot()
                    skill_runtime_rollback = rollback_installed_skill_runtimes() if restored else {}
                    payload: dict[str, Any] = {
                        "state": "failed",
                        "phase": "apply",
                        "message": (
                            f"prepared slot {target_slot} failed deferred skill runtime migration"
                            if target_slot
                            else "prepared slot failed deferred skill runtime migration"
                        ),
                        "target_slot": target_slot,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "manifest": manifest,
                        "started_at": float(plan.get("prepared_at") or plan.get("created_at") or time.time()),
                        "finished_at": time.time(),
                        "restored_slot": restored or "",
                    }
                    if restored:
                        payload["rollback"] = {"ok": True, "slot": restored}
                    if skill_runtime_rollback:
                        payload["skill_runtime_rollback"] = skill_runtime_rollback
                        if not bool(skill_runtime_rollback.get("ok")):
                            payload["message"] += " | some skill runtime rollbacks failed"
                    write_status(payload)
                    raise SystemExit(1) from exc
                prepared_restart_boot = True
                payload = {
                    "state": "restarting",
                    "phase": "launch",
                    "message": (
                        f"prepared core update activated; launching slot {target_slot}"
                        if target_slot
                        else "prepared core update activated; launching runtime"
                    ),
                    "target_slot": target_slot,
                    "plan": plan,
                    "started_at": float(plan.get("prepared_at") or plan.get("created_at") or time.time()),
                    "finished_at": time.time(),
                    "manifest": manifest,
                    "skill_runtime_migration": skill_runtime_migration,
                }
                write_status(payload)
            else:
                if _resume_post_apply_update_transition(current_status, plan=plan):
                    pending_update_succeeded = True
                else:
                    phase = "execute_pending_update"
                    result = execute_pending_update(plan)
                    if conf is not None:
                        _upload_update_report(result, conf)
                    if str(result.get("state") or "") != "succeeded":
                        clear_plan()
                        raise SystemExit(int(result.get("returncode") or 1) or 1)
                    pending_update_succeeded = True
                    target_slot = str(result.get("target_slot") or active_slot() or "").strip().upper()
                    payload = {
                        "state": "restarting",
                        "phase": "launch",
                        "message": (
                            f"core update applied; restarting into slot {target_slot}"
                            if target_slot
                            else "core update applied; restarting runtime"
                        ),
                        "target_slot": target_slot,
                        "plan": plan,
                        "started_at": float(result.get("started_at") or time.time()),
                        "finished_at": time.time(),
                    }
                    manifest = result.get("manifest")
                    if isinstance(manifest, dict) and manifest:
                        payload["manifest"] = manifest
                    write_status(payload)
        if not prepared_restart_boot and not pending_update_succeeded:
            phase = "boot"
            current_status = read_status()
            reconciled = _reconcile_post_root_promotion_restart(current_status)
            if reconciled is not None:
                write_status(reconciled)
            else:
                interrupted = _reconcile_interrupted_update_transition(current_status, plan=plan)
                if interrupted is not None:
                    write_status(interrupted)
                else:
                    finalized = finalize_runtime_boot_status()
                    if finalized is None:
                        write_status({"state": "idle", "message": "autostart runner boot", "updated_at": time.time()})

        phase = "resolve_bind"
        host, port = _resolve_bind(conf, args.host, args.port)
        advertised_base = _advertise_base(host, port)
        phase = "stop_previous_server"
        _stop_previous_server(host, port)
        phase = "write_pidfile"
        pidfile = _pidfile_path(host, port)
        _write_pidfile(pidfile, host=host, port=port, advertised_base=advertised_base)
        atexit.register(_cleanup_pidfile, pidfile)

        if conf is not None and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            phase = "update_hub_url"
            try:
                if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                    conf.hub_url = advertised_base
                    save_config(conf)
            except Exception:
                pass

        if token:
            os.environ["ADAOS_TOKEN"] = str(token)
        os.environ["ADAOS_SELF_BASE_URL"] = advertised_base
        os.environ["ADAOS_AUTOSTART_MODE"] = "1"

        phase = "launch_active_slot"
        _launch_active_slot_if_needed(args, host=host, port=port, validate=pending_update_succeeded)

        from adaos.apps.api.server import app as server_app

        runtime_memory_profile = RuntimeMemoryProfileSession(
            profile_mode=_runtime_profile_mode(),
            session_id=_runtime_profile_session_id(),
            profile_trigger=_runtime_profile_trigger(),
        )
        runtime_memory_profile.start()
        register_active_runtime_memory_profile(runtime_memory_profile)
        restore_profile_signal_handlers = _install_runtime_profile_signal_handlers(runtime_memory_profile)

        phase = "uvicorn.run"
        try:
            uvicorn.run(
                server_app,
                host=host,
                port=int(port),
                loop=_uvicorn_loop_mode(),
                reload=False,
                workers=1,
                access_log=False,
            )
        finally:
            restore_profile_signal_handlers()
            finish_result = finish_active_runtime_memory_profile()
            if not finish_result.get("ok"):
                _LOG.warning("runtime memory profile finalize on runner shutdown did not complete result=%s", finish_result)
            register_active_runtime_memory_profile(None)
            if pidfile is not None:
                _cleanup_pidfile(pidfile)
    except SystemExit:
        raise
    except Exception as exc:
        try:
            write_status(
                {
                    "state": "failed",
                    "phase": phase,
                    "message": f"autostart runner failed during {phase}",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=20),
                    "updated_at": time.time(),
                }
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
