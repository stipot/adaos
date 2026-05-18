from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.domain import Event
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.io_web.toast import WebToastService
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.services.skill.manager import SkillManager
from adaos.services.yjs.doc import async_get_ydoc, get_ydoc
from adaos.services.yjs.store import ystore_write_metadata, ystore_write_metadata_sync
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.operations")
_MANAGER_LOCK = threading.RLock()
_MANAGERS: dict[str, "OperationManager"] = {}
_ACTIVE_STATUSES = {"accepted", "queued", "running", "waiting_input"}


def _operations_async_write_meta():
    return ystore_write_metadata(
        root_names=["runtime"],
        source="operations.manager",
        owner="core:operations",
        channel="core.operations.async",
    )


def _operations_sync_write_meta():
    return ystore_write_metadata_sync(
        root_names=["runtime"],
        source="operations.manager",
        owner="core:operations",
        channel="core.operations.sync",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _timeout_env_seconds(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    if value <= 0:
        return float(default)
    return float(value)


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _use_subprocess_install() -> bool:
    if os.getenv("ADAOS_TESTING") == "1":
        return False
    return _env_flag("ADAOS_OPERATIONS_INSTALL_SUBPROCESS", True)


def _install_subprocess_argv(*, target_kind: str, target_id: str) -> list[str]:
    argv = [sys.executable, "-m", "adaos"]
    if target_kind == "skill":
        return [*argv, "skill", "install", target_id, "--local", "--silent"]
    return [*argv, "scenario", "install", target_id]


def _install_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ADAOS_DISABLE_PREFERRED_PYTHON_REEXEC"] = "1"
    env["ADAOS_CLI_REEXECED"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _tail_text(data: bytes | str | None, *, limit: int = 4000) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = str(data)
    trimmed = text.strip()
    if len(trimmed) <= limit:
        return trimmed
    return trimmed[-limit:]


async def _best_effort_rebuild_webspace(
    *,
    webspace_id: str,
    action: str,
    source_of_truth: str,
    scenario_id: str | None = None,
) -> None:
    try:
        await rebuild_webspace_from_sources(
            webspace_id,
            action=action,
            scenario_id=scenario_id,
            source_of_truth=source_of_truth,
        )
    except Exception:
        _log.warning(
            "operation webspace rebuild failed webspace=%s action=%s scenario=%s",
            webspace_id,
            action,
            scenario_id or "-",
            exc_info=True,
        )


async def _terminate_subprocess(proc: asyncio.subprocess.Process, *, grace_s: float = 5.0) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        _log.debug("failed to terminate install subprocess", exc_info=True)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        _log.debug("failed to kill install subprocess", exc_info=True)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except Exception:
        _log.debug("install subprocess did not exit after kill", exc_info=True)


@dataclass(slots=True)
class OperationState:
    operation_id: str
    kind: str
    target_kind: str
    target_id: str
    status: str
    progress: int | None = None
    message: str = ""
    current_step: str = ""
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    initiator: dict[str, Any] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)
    can_cancel: bool = False
    webspace_id: str = field(default_factory=default_webspace_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OperationNotification:
    id: str
    level: str
    message: str
    operation_id: str
    target_kind: str
    target_id: str
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperationHandle:
    def __init__(self, manager: "OperationManager", operation_id: str):
        self._manager = manager
        self.operation_id = operation_id

    def update(
        self,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        current_step: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        return self._manager.update_operation(
            self.operation_id,
            status=status,
            progress=progress,
            message=message,
            current_step=current_step,
            result=result,
            error=error,
            finished=finished,
        )

    def running(self, *, progress: int | None = None, message: str = "", current_step: str = "") -> dict[str, Any]:
        return self.update(status="running", progress=progress, message=message, current_step=current_step)

    def succeeded(self, *, result: dict[str, Any] | None = None, message: str = "") -> dict[str, Any]:
        return self.update(status="succeeded", progress=100, message=message, result=result or {}, finished=True)

    def failed(self, *, error: dict[str, Any] | None = None, message: str = "") -> dict[str, Any]:
        payload = dict(error or {})
        if message and "message" not in payload:
            payload["message"] = message
        return self.update(status="failed", message=message, error=payload, finished=True)


class OperationManager:
    def __init__(self, ctx: AgentContext, *, max_finished: int = 20, max_notifications: int = 40) -> None:
        self.ctx = ctx
        self.max_finished = max(1, int(max_finished))
        self.max_notifications = max(1, int(max_notifications))
        self._lock = threading.RLock()
        self._sequence = 0
        self._operations_by_webspace: dict[str, dict[str, OperationState]] = {}
        self._order_by_webspace: dict[str, list[str]] = {}
        self._notifications_by_webspace: dict[str, list[OperationNotification]] = {}
        self._tasks_by_operation_id: dict[str, asyncio.Task[Any]] = {}

    def create_operation(
        self,
        *,
        kind: str,
        target_kind: str,
        target_id: str,
        webspace_id: str | None = None,
        initiator: dict[str, Any] | None = None,
        scope: list[str] | None = None,
        message: str = "",
    ) -> OperationState:
        ws = str(webspace_id or "").strip() or default_webspace_id()
        with self._lock:
            self._sequence += 1
            op_id = f"op_{int(time.time() * 1000)}_{self._sequence}"
            item = OperationState(
                operation_id=op_id,
                kind=str(kind or "").strip(),
                target_kind=str(target_kind or "").strip(),
                target_id=str(target_id or "").strip(),
                status="accepted",
                message=str(message or "").strip(),
                initiator=dict(initiator or {}),
                scope=list(scope or []),
                webspace_id=ws,
            )
            ops = self._operations_by_webspace.setdefault(ws, {})
            order = self._order_by_webspace.setdefault(ws, [])
            ops[op_id] = item
            order.append(op_id)
            self._trim_finished_locked(ws)
        self._after_change(item, event_type="operations.changed")
        return item

    def update_operation(
        self,
        operation_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        current_step: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        item = self._get_operation(operation_id)
        with self._lock:
            if status:
                item.status = str(status)
            if progress is not None:
                item.progress = max(0, min(100, int(progress)))
            if message is not None:
                item.message = str(message)
            if current_step is not None:
                item.current_step = str(current_step)
            if result is not None:
                item.result = _json_clone(result)
            if error is not None:
                item.error = _json_clone(error)
            item.updated_at = _now_iso()
            if finished or item.status not in _ACTIVE_STATUSES:
                item.finished_at = item.finished_at or _now_iso()
            self._trim_finished_locked(item.webspace_id)
        self._after_change(item, event_type="operations.changed")
        return item.to_dict()

    def launch(self, operation_id: str, worker: Any) -> dict[str, Any]:
        item = self._get_operation(operation_id)
        handle = OperationHandle(self, operation_id)

        async def _runner() -> None:
            try:
                await worker(handle)
            except Exception as exc:
                _log.warning("operation failed op=%s kind=%s", item.operation_id, item.kind, exc_info=True)
                handle.failed(
                    message=str(exc),
                    error={"type": type(exc).__name__, "message": str(exc)},
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_runner())
        else:
            task = loop.create_task(_runner(), name=f"operation-{item.operation_id}")
            with self._lock:
                self._tasks_by_operation_id[item.operation_id] = task

            def _finalize(done: asyncio.Task[Any]) -> None:
                with self._lock:
                    self._tasks_by_operation_id.pop(item.operation_id, None)
                try:
                    current = self._get_operation(item.operation_id)
                except KeyError:
                    return
                if current.status not in _ACTIVE_STATUSES:
                    return
                if done.cancelled():
                    handle.failed(
                        message="operation cancelled",
                        error={"type": "CancelledError", "message": "operation cancelled"},
                    )
                    return
                try:
                    exc = done.exception()
                except asyncio.CancelledError:
                    handle.failed(
                        message="operation cancelled",
                        error={"type": "CancelledError", "message": "operation cancelled"},
                    )
                    return
                if exc is not None:
                    handle.failed(
                        message=str(exc),
                        error={"type": type(exc).__name__, "message": str(exc)},
                    )
                    return
                handle.failed(
                    message="operation ended without a terminal status",
                    error={"type": "OperationAbandoned", "message": "operation ended without a terminal status"},
                )

            task.add_done_callback(_finalize)
        return item.to_dict()

    def snapshot(self, *, webspace_id: str | None = None) -> dict[str, Any]:
        ws = str(webspace_id or "").strip() or default_webspace_id()
        with self._lock:
            ops = self._operations_by_webspace.get(ws, {})
            order = list(self._order_by_webspace.get(ws, []))
            by_id = {op_id: ops[op_id].to_dict() for op_id in order if op_id in ops}
            active = [op_id for op_id in order if op_id in ops and ops[op_id].status in _ACTIVE_STATUSES]
            notifications = [item.to_dict() for item in self._notifications_by_webspace.get(ws, [])]
        active_items = [by_id[op_id] for op_id in active if op_id in by_id]
        return {
            "by_id": by_id,
            "order": order,
            "active": active,
            "active_items": active_items,
            "notifications": notifications,
        }

    def _get_operation(self, operation_id: str) -> OperationState:
        with self._lock:
            for ops in self._operations_by_webspace.values():
                if operation_id in ops:
                    return ops[operation_id]
        raise KeyError(operation_id)

    def _trim_finished_locked(self, webspace_id: str) -> None:
        ops = self._operations_by_webspace.get(webspace_id, {})
        order = self._order_by_webspace.get(webspace_id, [])
        finished = [op_id for op_id in order if op_id in ops and ops[op_id].status not in _ACTIVE_STATUSES]
        if len(finished) <= self.max_finished:
            return
        remove = finished[: len(finished) - self.max_finished]
        for op_id in remove:
            ops.pop(op_id, None)
        self._order_by_webspace[webspace_id] = [op_id for op_id in order if op_id not in remove]

    def _after_change(self, item: OperationState, *, event_type: str) -> None:
        if item.status == "succeeded":
            self._record_notification(item, level="success", message=f"{item.target_kind} {item.target_id} completed")
        elif item.status == "failed":
            message = str((item.error or {}).get("message") or item.message or f"{item.target_kind} {item.target_id} failed")
            self._record_notification(item, level="error", message=message)
        self._publish_event(item, event_type=event_type)
        self._project(item.webspace_id)

    def _record_notification(self, item: OperationState, *, level: str, message: str) -> None:
        notification = OperationNotification(
            id=f"notif:{item.operation_id}:{level}",
            level=level,
            message=message,
            operation_id=item.operation_id,
            target_kind=item.target_kind,
            target_id=item.target_id,
        )
        with self._lock:
            bucket = self._notifications_by_webspace.setdefault(item.webspace_id, [])
            bucket.append(notification)
            if len(bucket) > self.max_notifications:
                self._notifications_by_webspace[item.webspace_id] = bucket[-self.max_notifications :]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(
                WebToastService(self.ctx).push(
                    notification.message,
                    level="success" if level == "success" else "danger",
                    source="operations",
                    code=item.operation_id,
                    webspace_id=item.webspace_id,
                )
            )
        else:
            loop.create_task(
                WebToastService(self.ctx).push(
                    notification.message,
                    level="success" if level == "success" else "danger",
                    source="operations",
                    code=item.operation_id,
                    webspace_id=item.webspace_id,
                ),
                name=f"operation-toast-{item.operation_id}",
            )

    def _publish_event(self, item: OperationState, *, event_type: str) -> None:
        try:
            self.ctx.bus.publish(
                Event(
                    type=event_type,
                    payload={
                        "operation_id": item.operation_id,
                        "kind": item.kind,
                        "target_kind": item.target_kind,
                        "target_id": item.target_id,
                        "status": item.status,
                        "webspace_id": item.webspace_id,
                    },
                    source="operations",
                    ts=time.time(),
                )
            )
        except Exception:
            _log.debug("failed to publish operation event op=%s", item.operation_id, exc_info=True)

    def _project(self, webspace_id: str) -> None:
        snapshot = self.snapshot(webspace_id=webspace_id)

        async def _write_async() -> None:
            async with _operations_async_write_meta():
                async with async_get_ydoc(webspace_id) as ydoc:
                    runtime_map = ydoc.get_map("runtime")
                    with ydoc.begin_transaction() as txn:
                        runtime_map.set(
                            txn,
                            "operations",
                            _json_clone({"by_id": snapshot["by_id"], "order": snapshot["order"], "active": snapshot["active"]}),
                        )
                        runtime_map.set(txn, "notifications", _json_clone(snapshot["notifications"]))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with _operations_sync_write_meta():
                with get_ydoc(webspace_id) as ydoc:
                    runtime_map = ydoc.get_map("runtime")
                    with ydoc.begin_transaction() as txn:
                        runtime_map.set(
                            txn,
                            "operations",
                            _json_clone({"by_id": snapshot["by_id"], "order": snapshot["order"], "active": snapshot["active"]}),
                        )
                        runtime_map.set(txn, "notifications", _json_clone(snapshot["notifications"]))
        else:
            loop.create_task(_write_async(), name=f"operation-project-{webspace_id}")


def get_operation_manager(ctx: AgentContext | None = None) -> OperationManager:
    runtime = ctx or get_ctx()
    try:
        key = str(runtime.paths.base_dir())
    except Exception:
        key = "default"
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = OperationManager(runtime)
            _MANAGERS[key] = manager
        return manager


def _operation_scope(*, target_kind: str, target_id: str) -> list[str]:
    normalized_kind = str(target_kind or "").strip()
    normalized_id = str(target_id or "").strip()
    return [
        "global",
        f"{normalized_kind}.install",
        f"{normalized_kind}:{normalized_id}",
    ]


def submit_install_operation(
    *,
    target_kind: str,
    target_id: str,
    webspace_id: str | None = None,
    initiator: dict[str, Any] | None = None,
    ctx: AgentContext | None = None,
) -> dict[str, Any]:
    runtime = ctx or get_ctx()
    manager = get_operation_manager(runtime)
    ws = str(webspace_id or "").strip() or default_webspace_id()
    operation = manager.create_operation(
        kind=f"{target_kind}.install",
        target_kind=target_kind,
        target_id=target_id,
        webspace_id=ws,
        initiator=initiator or {"kind": "user", "id": "local"},
        scope=_operation_scope(target_kind=target_kind, target_id=target_id),
        message=f"Accepted {target_kind} install",
    )

    async def _worker(handle: OperationHandle) -> None:
        if _use_subprocess_install():
            timeout_name = (
                "ADAOS_SKILL_INSTALL_PROCESS_TIMEOUT_S"
                if target_kind == "skill"
                else "ADAOS_SCENARIO_INSTALL_PROCESS_TIMEOUT_S"
            )
            timeout_default_s = 300.0 if target_kind == "skill" else 240.0
            argv = _install_subprocess_argv(target_kind=target_kind, target_id=target_id)
            handle.running(progress=10, message="Spawning isolated installer", current_step="install.subprocess.spawn")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_install_subprocess_env(),
            )
            handle.update(progress=35, message="Installing in isolated process", current_step="install.subprocess.run")
            timeout_s = _timeout_env_seconds(timeout_name, timeout_default_s)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                await _terminate_subprocess(proc)
                handle.failed(
                    message=f"{target_kind} install timed out after {timeout_s:.0f}s",
                    error={
                        "type": "InstallSubprocessTimeout",
                        "message": f"{target_kind} install timed out after {timeout_s:.0f}s",
                        "argv": argv,
                    },
                )
                return
            rc = proc.returncode if proc.returncode is not None else -1
            if rc != 0:
                stdout_tail = _tail_text(stdout)
                stderr_tail = _tail_text(stderr)
                detail = stderr_tail or stdout_tail or f"installer exited with code {rc}"
                handle.failed(
                    message=detail,
                    error={
                        "type": "InstallSubprocessFailed",
                        "message": detail,
                        "returncode": rc,
                        "argv": argv,
                        "stdout_tail": stdout_tail,
                        "stderr_tail": stderr_tail,
                    },
                )
                return
            handle.succeeded(
                result={
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "webspace_id": ws,
                    "mode": "subprocess",
                },
                message=f"Installed {target_kind} {target_id}",
            )
            return

        async def _best_effort_phase(
            *,
            timeout_env: str,
            timeout_default_s: float,
            message: str,
            current_step: str,
            progress: int,
            func: Any,
        ) -> list[str]:
            warnings: list[str] = []
            handle.update(progress=progress, message=message, current_step=current_step)
            timeout_s = _timeout_env_seconds(timeout_env, timeout_default_s)
            try:
                await asyncio.wait_for(asyncio.to_thread(func), timeout=timeout_s)
            except asyncio.TimeoutError:
                warning = f"{current_step} timed out after {timeout_s:.0f}s"
                warnings.append(warning)
                _log.warning(
                    "operation phase timeout op=%s kind=%s target=%s step=%s timeout_s=%s",
                    handle.operation_id,
                    target_kind,
                    target_id,
                    current_step,
                    timeout_s,
                )
            except Exception as exc:
                warning = f"{current_step} warning: {type(exc).__name__}: {exc}"
                warnings.append(warning)
                _log.warning(
                    "operation phase warning op=%s kind=%s target=%s step=%s",
                    handle.operation_id,
                    target_kind,
                    target_id,
                    current_step,
                    exc_info=True,
                )
            return warnings

        handle.running(progress=5, message="Preparing install", current_step="prepare")
        if target_kind == "skill":
            mgr = SkillManager(
                repo=runtime.skills_repo,
                registry=SqliteSkillRegistry(runtime.sql),
                git=runtime.git,
                paths=runtime.paths,
                bus=getattr(runtime, "bus", None),
                caps=runtime.caps,
                settings=runtime.settings,
            )
            try:
                handle.update(progress=15, message="Syncing skills workspace", current_step="workspace.sync")
                await asyncio.to_thread(mgr.sync)
            except Exception as exc:
                _log.debug("skill sync before install failed id=%s", target_id, exc_info=True)
                handle.update(message=f"Continuing after sync warning: {exc}", current_step="workspace.sync")
            handle.update(progress=45, message="Installing skill", current_step="skill.install")
            result = await asyncio.to_thread(
                partial(
                    mgr.install,
                    target_id,
                    pin=None,
                    validate=True,
                    strict=True,
                    probe_tools=False,
                )
            )
            if isinstance(result, tuple):
                meta, report = result
            else:
                meta, report = result, None
            handle.update(progress=70, message="Preparing runtime", current_step="skill.prepare_runtime")
            prep = await asyncio.to_thread(partial(mgr.prepare_runtime, target_id, run_tests=False))
            handle.update(progress=88, message="Activating skill", current_step="skill.activate")
            active_slot = await asyncio.to_thread(
                partial(
                    mgr.activate_for_space,
                    target_id,
                    version=getattr(prep, "version", None),
                    slot=getattr(prep, "slot", None),
                    space="default",
                    webspace_id=ws,
                )
            )
            handle.update(progress=95, message="Refreshing webspace", current_step="webspace.rebuild")
            await _best_effort_rebuild_webspace(
                webspace_id=ws,
                action="skill_install_sync",
                source_of_truth="skill_runtime",
            )
            payload = {
                "target_kind": "skill",
                "target_id": target_id,
                "version": getattr(meta, "version", None),
                "path": str(getattr(meta, "path", "")),
                "runtime_version": getattr(prep, "version", None),
                "slot": active_slot,
                "prepared_slot": getattr(prep, "slot", None),
                "webspace_id": ws,
            }
            if report is not None:
                payload["report"] = report.to_dict() if hasattr(report, "to_dict") else repr(report)
            handle.succeeded(result=payload, message=f"Installed and activated skill {target_id}")
            return

        mgr = ScenarioManager(
            repo=runtime.scenarios_repo,
            registry=SqliteScenarioRegistry(runtime.sql),
            git=runtime.git,
            paths=runtime.paths,
            bus=getattr(runtime, "bus", None),
            caps=runtime.caps,
        )
        warnings: list[str] = []
        handle.update(progress=15, message="Syncing scenarios workspace", current_step="workspace.sync")
        await asyncio.to_thread(mgr.sync)
        handle.update(progress=45, message="Installing scenario", current_step="scenario.install")
        meta = await asyncio.to_thread(partial(mgr.install, target_id, pin=None))
        dependency_bootstrap: dict[str, Any] | None = None
        handle.update(
            progress=65,
            message="Activating scenario dependencies",
            current_step="scenario.bootstrap_dependencies",
        )
        dep_timeout_s = _timeout_env_seconds("ADAOS_SCENARIO_DEP_BOOTSTRAP_TIMEOUT_S", 60.0)
        try:
            raw_dependency_bootstrap = await asyncio.wait_for(
                asyncio.to_thread(partial(mgr.bootstrap_dependencies, target_id, webspace_id=ws)),
                timeout=dep_timeout_s,
            )
            if isinstance(raw_dependency_bootstrap, dict):
                dependency_bootstrap = dict(raw_dependency_bootstrap)
            else:
                raw_last_result = getattr(mgr, "last_dependency_bootstrap_result", None)
                dependency_bootstrap = dict(raw_last_result) if isinstance(raw_last_result, dict) else None
        except asyncio.TimeoutError:
            warning = f"scenario.bootstrap_dependencies timed out after {dep_timeout_s:.0f}s"
            warnings.append(warning)
            dependency_bootstrap = {
                "ok": False,
                "scenario_id": target_id,
                "webspace_id": ws,
                "required": [],
                "items": [],
                "succeeded": [],
                "failed": [],
                "error": warning,
            }
            _log.warning(
                "operation phase timeout op=%s kind=%s target=%s step=scenario.bootstrap_dependencies timeout_s=%s",
                handle.operation_id,
                target_kind,
                target_id,
                dep_timeout_s,
            )
        except Exception as exc:
            warning = f"scenario.bootstrap_dependencies warning: {type(exc).__name__}: {exc}"
            warnings.append(warning)
            dependency_bootstrap = {
                "ok": False,
                "scenario_id": target_id,
                "webspace_id": ws,
                "required": [],
                "items": [],
                "succeeded": [],
                "failed": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
            _log.warning(
                "operation phase warning op=%s kind=%s target=%s step=scenario.bootstrap_dependencies",
                handle.operation_id,
                target_kind,
                target_id,
                exc_info=True,
            )
        if isinstance(dependency_bootstrap, dict) and not bool(dependency_bootstrap.get("ok", True)):
            warning = str(dependency_bootstrap.get("error") or "scenario dependency bootstrap reported failures")
            if warning not in warnings:
                warnings.append(warning)
        warnings.extend(
            await _best_effort_phase(
                timeout_env="ADAOS_SCENARIO_SYNC_TIMEOUT_S",
                timeout_default_s=20.0,
                message="Syncing scenario into webspace",
                current_step="scenario.sync_yjs",
                progress=80,
                func=partial(mgr.sync_to_yjs, target_id, webspace_id=ws, emit_event=False),
            )
        )
        handle.update(progress=95, message="Refreshing webspace", current_step="webspace.rebuild")
        try:
            await asyncio.wait_for(
                _best_effort_rebuild_webspace(
                    webspace_id=ws,
                    action="scenario_install_sync",
                    scenario_id=target_id,
                    source_of_truth="scenario_projection",
                ),
                timeout=_timeout_env_seconds("ADAOS_SCENARIO_REBUILD_TIMEOUT_S", 45.0),
            )
        except asyncio.TimeoutError:
            warnings.append("webspace.rebuild timed out")
            _log.warning(
                "operation phase timeout op=%s kind=%s target=%s step=webspace.rebuild",
                handle.operation_id,
                target_kind,
                target_id,
            )
        payload = {
            "target_kind": "scenario",
            "target_id": target_id,
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
            "webspace_id": ws,
        }
        if dependency_bootstrap is not None:
            payload["dependency_bootstrap"] = dependency_bootstrap
        if warnings:
            payload["warnings"] = warnings
        handle.succeeded(
            result=payload,
            message=f"Installed scenario {target_id}" + (" with warnings" if warnings else ""),
        )

    manager.launch(operation.operation_id, _worker)
    return operation.to_dict()
