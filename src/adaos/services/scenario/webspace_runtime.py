from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from collections import OrderedDict
from collections.abc import Iterable
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import traceback

import y_py as Y
import yaml

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.capacity import get_local_capacity
from adaos.services.node_config import load_config
from adaos.services.node_display import node_display_from_config, node_display_from_directory_node
from adaos.services.yjs.doc import (
    get_ydoc,
    async_get_ydoc,
    async_read_ydoc,
    mutate_live_room,
    try_read_live_map_value,
)
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.runtime_environment import runtime_environment_payload
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspaces import index as workspace_index
from adaos.services.yjs.store import get_ystore_for_webspace, ystore_write_metadata, ystore_write_metadata_sync
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.seed import SEED
from adaos.services.eventbus import emit
from adaos.sdk.core.decorators import subscribe
from .node_data_scope import local_unscoped_data_path, node_scope_data_path
from .workflow_runtime import ScenarioWorkflowRuntime

_log = logging.getLogger("adaos.scenario.webspace_runtime")
_WS_ID_RE = re.compile(r"[^a-zA-Z0-9-_]+")
_SCENARIO_SWITCH_REBUILD_TASKS: dict[str, asyncio.Task[Any]] = {}
_WEBSPACE_REBUILD_STATUS: dict[str, Dict[str, Any]] = {}
_WEBSPACE_RECOVERY_COMMAND_CACHE: dict[str, Dict[str, Any]] = {}
_WEBSPACE_RECOVERY_COMMAND_CACHE_LIMIT = 256
_SCENARIO_SYNC_REBUILD_AT: dict[str, float] = {}
_SCENARIO_SYNC_REBUILD_CACHE_LIMIT = 512
_SKILL_RUNTIME_REBUILD_TASKS: dict[str, asyncio.Task[Any]] = {}
_SKILL_RUNTIME_REBUILD_PENDING: dict[str, Dict[str, Any]] = {}
_SKILL_RUNTIME_REBUILD_STATS: dict[str, Dict[str, Any]] = {}
_WEBSPACE_LISTING_SYNC_TASK: asyncio.Task[Any] | None = None
_WEBUI_DECL_CACHE: dict[str, tuple[tuple[str, int, int], Dict[str, Any]]] = {}
_MEMBER_SNAPSHOT_REBUILD_AT: dict[str, float] = {}
_MEMBER_SNAPSHOT_REBUILD_TASKS: dict[str, asyncio.Task[Any]] = {}
_MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS: dict[str, asyncio.Task[Any]] = {}
_MEMBER_SNAPSHOT_REBUILD_DIRTY: dict[str, Dict[str, Any]] = {}
_MEMBER_SNAPSHOT_REBUILD_STATS: dict[str, Dict[str, Any]] = {}
_RESOLVED_WEBSPACE_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_RESOLVED_WEBSPACE_CACHE_LIMIT = 64
_DESKTOP_SCENARIOS_CACHE_TTL_S = 30.0
_DESKTOP_SCENARIOS_CACHE: dict[str, tuple[float, tuple[tuple[str, int, int], ...], list[Tuple[str, str]]]] = {}
_LOCAL_NODE_DISPLAY_CACHE_TTL_S = 2.0
_LOCAL_NODE_DISPLAY_CACHE: tuple[float, dict[str, Any]] = (0.0, {})
_EFFECTIVE_BRANCH_PATHS = (
    "ui.application",
    "data.catalog",
    "data.installed",
    "data.desktop",
    "data.webio",
    "data.routing",
    "registry.merged",
    "runtime.environment",
)


def _member_snapshot_rebuild_min_interval_s() -> float:
    raw = str(os.getenv("ADAOS_MEMBER_SNAPSHOT_REBUILD_MIN_INTERVAL_S", "") or "").strip()
    if raw:
        try:
            return max(0.0, min(float(raw), 60.0))
        except Exception:
            pass
    return 5.0


def _scenario_sync_rebuild_min_interval_s() -> float:
    raw = str(os.getenv("ADAOS_SCENARIO_SYNC_REBUILD_MIN_INTERVAL_S", "") or "").strip()
    if raw:
        try:
            return max(0.0, min(float(raw), 60.0))
        except Exception:
            pass
    return 10.0


def _skill_runtime_rebuild_debounce_s() -> float:
    raw = str(os.getenv("ADAOS_SKILL_RUNTIME_REBUILD_DEBOUNCE_S", "") or "").strip()
    if raw:
        try:
            return max(0.0, min(float(raw), 30.0))
        except Exception:
            pass
    return 1.5


def _skill_runtime_rebuild_stats(webspace_id: str) -> Dict[str, Any]:
    key = str(webspace_id or "").strip() or default_webspace_id()
    stats = _SKILL_RUNTIME_REBUILD_STATS.get(key)
    if stats is None:
        stats = {
            "requested_total": 0,
            "scheduled_total": 0,
            "coalesced_total": 0,
            "completed_total": 0,
            "failed_total": 0,
        }
        _SKILL_RUNTIME_REBUILD_STATS[key] = stats
    return stats


def _merge_skill_runtime_rebuild_request(
    *,
    webspace_id: str,
    action: str,
    source_of_truth: str,
    reason: str,
) -> Dict[str, Any]:
    key = str(webspace_id or "").strip() or default_webspace_id()
    pending = _SKILL_RUNTIME_REBUILD_PENDING.get(key)
    if pending is None:
        pending = {
            "webspace_id": key,
            "actions": [],
            "reasons": [],
            "source_of_truth": str(source_of_truth or "").strip() or "skill_runtime",
            "requested_at": time.time(),
            "updated_at": time.time(),
            "request_count": 0,
        }
        _SKILL_RUNTIME_REBUILD_PENDING[key] = pending
    action_token = str(action or "").strip() or "skill_runtime_sync"
    reason_token = str(reason or "").strip() or action_token
    pending["request_count"] = int(pending.get("request_count") or 0) + 1
    pending["updated_at"] = time.time()
    if action_token not in list(pending.get("actions") or []):
        pending.setdefault("actions", []).append(action_token)
    if reason_token not in list(pending.get("reasons") or []):
        pending.setdefault("reasons", []).append(reason_token)
    return pending


def _coalesced_skill_runtime_action(actions: list[str]) -> str:
    normalized = [str(item or "").strip() for item in actions if str(item or "").strip()]
    unique = list(dict.fromkeys(normalized))
    if len(unique) == 1:
        return unique[0]
    if any(item in unique for item in {"skill_rollback_sync", "skill_uninstall_sync"}):
        return "skill_runtime_sync"
    if any(item == "skill_update_sync" for item in unique):
        return "skill_update_sync"
    if any(item == "skill_activation_sync" for item in unique):
        return "skill_activation_sync"
    return "skill_runtime_sync"


def schedule_skill_runtime_rebuild(
    *,
    webspace_id: str | None = None,
    action: str = "skill_runtime_sync",
    source_of_truth: str = "skill_runtime",
    reason: str = "",
) -> Dict[str, Any]:
    key = str(webspace_id or "").strip() or default_webspace_id()
    stats = _skill_runtime_rebuild_stats(key)
    stats["requested_total"] = int(stats.get("requested_total") or 0) + 1
    pending = _merge_skill_runtime_rebuild_request(
        webspace_id=key,
        action=action,
        source_of_truth=source_of_truth,
        reason=reason or action,
    )
    existing = _SKILL_RUNTIME_REBUILD_TASKS.get(key)
    if existing is not None and not existing.done():
        stats["coalesced_total"] = int(stats.get("coalesced_total") or 0) + 1
        return {
            "scheduled": True,
            "mode": "coalesced",
            "webspace_id": key,
            "action": action,
            "source_of_truth": source_of_truth,
            "pending_count": int(pending.get("request_count") or 0),
            "task": existing.get_name(),
        }
    return _start_skill_runtime_rebuild_task(key, stats=stats, pending=pending)


def _start_skill_runtime_rebuild_task(
    webspace_id: str,
    *,
    stats: Dict[str, Any] | None = None,
    pending: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    key = str(webspace_id or "").strip() or default_webspace_id()
    stats = stats or _skill_runtime_rebuild_stats(key)
    pending = pending or _SKILL_RUNTIME_REBUILD_PENDING.get(key) or {}
    stats["scheduled_total"] = int(stats.get("scheduled_total") or 0) + 1
    task = asyncio.create_task(
        _run_skill_runtime_rebuild_coalesced(key),
        name=f"skill-runtime-rebuild:{key}",
    )
    _SKILL_RUNTIME_REBUILD_TASKS[key] = task
    return {
        "scheduled": True,
        "mode": "coalesced",
        "webspace_id": key,
        "action": _coalesced_skill_runtime_action(list(pending.get("actions") or [])),
        "source_of_truth": str(pending.get("source_of_truth") or "skill_runtime"),
        "pending_count": int(pending.get("request_count") or 0),
        "task": task.get_name(),
    }


async def _run_skill_runtime_rebuild_coalesced(webspace_id: str) -> None:
    key = str(webspace_id or "").strip() or default_webspace_id()
    stats = _skill_runtime_rebuild_stats(key)
    try:
        while True:
            delay_s = _skill_runtime_rebuild_debounce_s()
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            pending = _SKILL_RUNTIME_REBUILD_PENDING.pop(key, None)
            if not pending:
                return
            actions = list(pending.get("actions") or [])
            reasons = list(pending.get("reasons") or [])
            action = _coalesced_skill_runtime_action(actions)
            source_of_truth = str(pending.get("source_of_truth") or "skill_runtime").strip() or "skill_runtime"
            request_count = int(pending.get("request_count") or 0)
            _log.info(
                "running coalesced skill runtime rebuild webspace=%s action=%s requests=%s actions=%s reasons=%s",
                key,
                action,
                request_count,
                ",".join(str(item) for item in actions) or "-",
                ",".join(str(item) for item in reasons[:8]) or "-",
            )
            try:
                await rebuild_webspace_from_sources(
                    key,
                    action=action,
                    source_of_truth=source_of_truth,
                )
                stats["completed_total"] = int(stats.get("completed_total") or 0) + 1
            except Exception:
                stats["failed_total"] = int(stats.get("failed_total") or 0) + 1
                _log.warning(
                    "coalesced skill runtime rebuild failed webspace=%s action=%s",
                    key,
                    action,
                    exc_info=True,
                )
            if key not in _SKILL_RUNTIME_REBUILD_PENDING:
                return
    finally:
        current = asyncio.current_task()
        if _SKILL_RUNTIME_REBUILD_TASKS.get(key) is current:
            _SKILL_RUNTIME_REBUILD_TASKS.pop(key, None)
        if key in _SKILL_RUNTIME_REBUILD_PENDING and key not in _SKILL_RUNTIME_REBUILD_TASKS:
            _start_skill_runtime_rebuild_task(key)


def _should_skip_bootstrap_scenario_sync_rebuild(
    evt: Mapping[str, Any],
    *,
    webspace_id: str,
    scenario_id: str | None,
) -> bool:
    # Event source is not always preserved by the subscription adapter. Guard
    # all repeated scenario sync events for the same webspace/scenario, so a
    # noisy publisher cannot rebuild the desktop in a tight loop. Distinct
    # scenario ids still pass through immediately.
    interval_s = _scenario_sync_rebuild_min_interval_s()
    if interval_s <= 0:
        return False
    scenario_token = str(scenario_id or "").strip() or "-"
    key = f"bootstrap\0{webspace_id}\0{scenario_token}"
    now = time.monotonic()
    last_at = float(_SCENARIO_SYNC_REBUILD_AT.get(key) or 0.0)
    _SCENARIO_SYNC_REBUILD_AT[key] = now
    if len(_SCENARIO_SYNC_REBUILD_AT) > _SCENARIO_SYNC_REBUILD_CACHE_LIMIT:
        for old_key, _ in sorted(_SCENARIO_SYNC_REBUILD_AT.items(), key=lambda item: item[1])[
            : max(1, len(_SCENARIO_SYNC_REBUILD_AT) - _SCENARIO_SYNC_REBUILD_CACHE_LIMIT)
        ]:
            _SCENARIO_SYNC_REBUILD_AT.pop(old_key, None)
    if last_at > 0.0 and now - last_at < interval_s:
        _log.debug(
            "deduplicated bootstrap scenario sync rebuild webspace=%s scenario=%s age_s=%.3f min_interval_s=%.3f",
            webspace_id,
            scenario_token,
            now - last_at,
            interval_s,
        )
        return True
    return False


def _member_snapshot_rebuild_request_id(*, webspace_id: str, node_id: str) -> str:
    webspace_token = str(webspace_id or "").strip() or default_webspace_id()
    node_token = str(node_id or "").strip() or "member"
    return f"member-snapshot-rebuild:{webspace_token}:{node_token}:{time.time_ns()}"


def _member_snapshot_rebuild_stats(task_key: str) -> Dict[str, Any]:
    stats = _MEMBER_SNAPSHOT_REBUILD_STATS.get(task_key)
    if stats is None:
        stats = {
            "requested_total": 0,
            "scheduled_total": 0,
            "coalesced_running_total": 0,
            "coalesced_interval_total": 0,
            "rerun_total": 0,
            "delayed_total": 0,
            "completed_total": 0,
            "last_reason": "",
            "last_requested_at": 0.0,
            "last_scheduled_at": 0.0,
            "last_completed_at": 0.0,
            "last_request_id": "",
            "current_request_id": "",
            "last_completed_request_id": "",
            "last_delayed_request_id": "",
        }
        _MEMBER_SNAPSHOT_REBUILD_STATS[task_key] = stats
    return stats


def member_snapshot_rebuild_runtime_snapshot(*, limit: int = 25) -> Dict[str, Any]:
    items: list[Dict[str, Any]] = []
    now_ts = time.time()
    for task_key, raw_stats in list(_MEMBER_SNAPSHOT_REBUILD_STATS.items()):
        if not isinstance(raw_stats, dict):
            continue
        node_id, _, webspace_id = str(task_key or "").partition("\0")
        dirty = dict(_MEMBER_SNAPSHOT_REBUILD_DIRTY.get(task_key) or {})
        task = _MEMBER_SNAPSHOT_REBUILD_TASKS.get(task_key)
        delayed = _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS.get(task_key)
        last_requested_at = float(raw_stats.get("last_requested_at") or 0.0)
        last_completed_at = float(raw_stats.get("last_completed_at") or 0.0)
        items.append(
            {
                "task_key": task_key,
                "node_id": node_id or "member",
                "webspace_id": webspace_id or default_webspace_id(),
                "requested_total": int(raw_stats.get("requested_total") or 0),
                "scheduled_total": int(raw_stats.get("scheduled_total") or 0),
                "rerun_total": int(raw_stats.get("rerun_total") or 0),
                "completed_total": int(raw_stats.get("completed_total") or 0),
                "coalesced_running_total": int(raw_stats.get("coalesced_running_total") or 0),
                "coalesced_interval_total": int(raw_stats.get("coalesced_interval_total") or 0),
                "delayed_total": int(raw_stats.get("delayed_total") or 0),
                "last_reason": str(raw_stats.get("last_reason") or "").strip() or None,
                "last_request_id": str(raw_stats.get("last_request_id") or "").strip() or None,
                "current_request_id": str(raw_stats.get("current_request_id") or "").strip() or None,
                "last_completed_request_id": str(raw_stats.get("last_completed_request_id") or "").strip() or None,
                "last_delayed_request_id": str(raw_stats.get("last_delayed_request_id") or "").strip() or None,
                "last_requested_at": last_requested_at or None,
                "last_requested_age_s": round(max(0.0, now_ts - last_requested_at), 3) if last_requested_at > 0.0 else None,
                "last_completed_at": last_completed_at or None,
                "last_completed_age_s": round(max(0.0, now_ts - last_completed_at), 3) if last_completed_at > 0.0 else None,
                "active": bool(task is not None and not task.done()),
                "delayed_active": bool(delayed is not None and not delayed.done()),
                "dirty_pending": bool(dirty),
                "dirty_reason": str(dirty.get("last_reason") or "").strip() or None,
                "dirty_mode": str(dirty.get("last_mode") or "").strip() or None,
                "dirty_count": int(dirty.get("count") or 0),
                "dirty_requested_at": dirty.get("last_requested_at"),
                "dirty_request_id": str(dirty.get("last_request_id") or "").strip() or None,
                "correlation_id": (
                    str(raw_stats.get("current_request_id") or "").strip()
                    or str(dirty.get("last_request_id") or "").strip()
                    or str(raw_stats.get("last_request_id") or "").strip()
                    or None
                ),
            }
        )
    items.sort(
        key=lambda item: (
            0 if bool(item.get("active")) else 1,
            0 if bool(item.get("dirty_pending")) else 1,
            -int(item.get("requested_total") or 0),
            -float(item.get("last_requested_at") or 0.0),
            str(item.get("webspace_id") or ""),
        )
    )
    capped = items[: max(1, int(limit or 1))]
    return {
        "tracked_key_total": len(items),
        "active_total": sum(1 for item in items if bool(item.get("active"))),
        "delayed_total": sum(1 for item in items if bool(item.get("delayed_active"))),
        "dirty_total": sum(1 for item in items if bool(item.get("dirty_pending"))),
        "items": capped,
    }


def _member_snapshot_rebuild_reason(evt: Any, payload: Any) -> str:
    event_type = str(
        getattr(evt, "type", "")
        or (payload.get("type") if isinstance(payload, Mapping) else "")
        or "subnet.member.snapshot.changed"
    ).strip()
    source = str(
        getattr(evt, "source", "")
        or (payload.get("source") if isinstance(payload, Mapping) else "")
        or ""
    ).strip()
    if source:
        return f"{event_type}:{source}"
    return event_type


def _mark_member_snapshot_rebuild_dirty(*, task_key: str, reason: str, mode: str, request_id: str | None = None) -> None:
    dirty = _MEMBER_SNAPSHOT_REBUILD_DIRTY.get(task_key)
    if dirty is None:
        dirty = {
            "count": 0,
            "last_reason": "",
            "last_mode": "",
            "last_requested_at": 0.0,
            "last_request_id": "",
        }
    dirty["count"] = int(dirty.get("count") or 0) + 1
    dirty["last_reason"] = str(reason or "").strip() or "subnet.member.snapshot.changed"
    dirty["last_mode"] = str(mode or "").strip() or "coalesced"
    dirty["last_requested_at"] = time.time()
    dirty["last_request_id"] = str(request_id or "").strip() or str(dirty.get("last_request_id") or "").strip()
    _MEMBER_SNAPSHOT_REBUILD_DIRTY[task_key] = dirty
    stats = _member_snapshot_rebuild_stats(task_key)
    if mode == "task_running":
        stats["coalesced_running_total"] = int(stats.get("coalesced_running_total") or 0) + 1
    else:
        stats["coalesced_interval_total"] = int(stats.get("coalesced_interval_total") or 0) + 1
    stats["last_reason"] = dirty["last_reason"]
    stats["last_requested_at"] = dirty["last_requested_at"]
    stats["last_request_id"] = str(dirty.get("last_request_id") or "").strip()


def _schedule_member_snapshot_rebuild_delayed(
    *,
    webspace_id: str,
    node_id: str,
    delay_s: float,
    reason: str,
    request_id: str | None = None,
) -> None:
    task_key = f"{str(node_id or '').strip()}\0{str(webspace_id or '').strip()}"
    existing = _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS.get(task_key)
    if existing is not None and not existing.done():
        return
    delayed_request_id = str(request_id or "").strip() or _member_snapshot_rebuild_request_id(
        webspace_id=webspace_id,
        node_id=node_id,
    )
    stats = _member_snapshot_rebuild_stats(task_key)
    stats["last_delayed_request_id"] = delayed_request_id

    async def _runner() -> None:
        try:
            await asyncio.sleep(max(0.0, float(delay_s)))
            if task_key not in _MEMBER_SNAPSHOT_REBUILD_DIRTY:
                return
            current = _MEMBER_SNAPSHOT_REBUILD_TASKS.get(task_key)
            if current is not None and not current.done():
                return
            _MEMBER_SNAPSHOT_REBUILD_AT[task_key] = time.monotonic()
            stats = _member_snapshot_rebuild_stats(task_key)
            stats["delayed_total"] = int(stats.get("delayed_total") or 0) + 1
            delayed_reason = str((_MEMBER_SNAPSHOT_REBUILD_DIRTY.get(task_key) or {}).get("last_reason") or reason or "").strip() or "subnet.member.snapshot.changed"
            _schedule_member_snapshot_rebuild(
                webspace_id=webspace_id,
                node_id=node_id,
                reason=f"{delayed_reason}:delayed",
                request_id=str((_MEMBER_SNAPSHOT_REBUILD_DIRTY.get(task_key) or {}).get("last_request_id") or delayed_request_id),
            )
        finally:
            current_delayed = _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS.get(task_key)
            if current_delayed is task:
                _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS.pop(task_key, None)

    task = asyncio.create_task(
        _runner(),
        name=f"member-snapshot-rebuild-delayed:{webspace_id}:{node_id}",
    )
    _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS[task_key] = task


def _webspace_runtime_async_write_meta(*, root_names: list[str], source: str):
    return ystore_write_metadata(
        root_names=root_names,
        source=source,
        owner="core:webspace_runtime",
        channel="core.webspace_runtime.async",
    )


def _webspace_runtime_sync_write_meta(*, root_names: list[str], source: str):
    return ystore_write_metadata_sync(
        root_names=root_names,
        source=source,
        owner="core:webspace_runtime",
        channel="core.webspace_runtime.sync",
    )
_WHOLE_BRANCH_REPLACE_PATHS = frozenset(_EFFECTIVE_BRANCH_PATHS)
_RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY = "effective_branch_fingerprints"
_WEBUI_LOAD_PHASES = frozenset({"eager", "visible", "interaction", "deferred"})
_WEBUI_LOAD_FOCUS = frozenset({"primary", "supporting", "off_focus", "background"})
_WEBUI_READINESS_STATES = frozenset({"pending_structure", "first_paint", "interactive", "hydrating", "ready", "degraded"})
_DEFERRED_OFF_FOCUS_LOAD = {
    "structure": "interaction",
    "data": "deferred",
    "focus": "off_focus",
    "offFocusReadyState": "hydrating",
}


def _reload_dedupe_window_s() -> float:
    raw = str(os.getenv("ADAOS_WEBSPACE_RECOVERY_DEDUPE_WINDOW_S") or "").strip()
    if not raw:
        return 1.5
    try:
        value = float(raw)
    except Exception:
        return 1.5
    if value < 0.0:
        return 0.0
    if value > 30.0:
        return 30.0
    return value


def _reload_pending_stale_after_s() -> float:
    raw = str(os.getenv("ADAOS_WEBSPACE_RECOVERY_PENDING_STALE_AFTER_S") or "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except Exception:
        return 10.0
    if value < 0.0:
        return 0.0
    if value > 300.0:
        return 300.0
    return value


def _reload_command_dedupe_ttl_s() -> float:
    raw = str(os.getenv("ADAOS_WEBSPACE_RECOVERY_COMMAND_DEDUPE_TTL_S") or "").strip()
    if not raw:
        return 300.0
    try:
        value = float(raw)
    except Exception:
        return 300.0
    if value < 0.0:
        return 0.0
    if value > 3600.0:
        return 3600.0
    return value


def _recovery_command_cache_key(
    *,
    webspace_id: str,
    action: str,
    scenario_id: str | None,
    cmd_id: str,
) -> str:
    raw = {
        "webspace_id": str(webspace_id or "").strip() or "default",
        "action": str(action or "").strip() or "reload",
        "scenario_id": str(scenario_id or "").strip() or None,
        "cmd_id": str(cmd_id or "").strip(),
    }
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:16]


def _claim_recovery_command_once(
    *,
    webspace_id: str,
    action: str,
    scenario_id: str | None,
    cmd_id: str | None,
    fingerprint: str,
) -> tuple[bool, dict[str, Any] | None]:
    cmd_id = str(cmd_id or "").strip()
    ttl_s = _reload_command_dedupe_ttl_s()
    if not cmd_id or ttl_s <= 0.0:
        return True, None

    now = time.time()
    expired = [
        key
        for key, entry in _WEBSPACE_RECOVERY_COMMAND_CACHE.items()
        if now - float(entry.get("ts") or 0.0) > ttl_s
    ]
    for key in expired:
        _WEBSPACE_RECOVERY_COMMAND_CACHE.pop(key, None)
    while len(_WEBSPACE_RECOVERY_COMMAND_CACHE) >= _WEBSPACE_RECOVERY_COMMAND_CACHE_LIMIT:
        oldest_key = min(
            _WEBSPACE_RECOVERY_COMMAND_CACHE,
            key=lambda item: float(_WEBSPACE_RECOVERY_COMMAND_CACHE[item].get("ts") or 0.0),
        )
        _WEBSPACE_RECOVERY_COMMAND_CACHE.pop(oldest_key, None)

    key = _recovery_command_cache_key(
        webspace_id=webspace_id,
        action=action,
        scenario_id=scenario_id,
        cmd_id=cmd_id,
    )
    existing = _WEBSPACE_RECOVERY_COMMAND_CACHE.get(key)
    if existing:
        duplicate = dict(existing)
        duplicate["age_s"] = round(max(0.0, now - float(existing.get("ts") or now)), 3)
        duplicate["ttl_s"] = ttl_s
        duplicate["cmd_id"] = cmd_id
        duplicate["cache_key"] = key
        return False, duplicate

    _WEBSPACE_RECOVERY_COMMAND_CACHE[key] = {
        "ts": now,
        "webspace_id": str(webspace_id or "").strip() or "default",
        "action": str(action or "").strip() or "reload",
        "scenario_id": str(scenario_id or "").strip() or None,
        "fingerprint": str(fingerprint or "").strip(),
        "cmd_id": cmd_id,
        "cache_key": key,
    }
    return True, None


def _project_scenario_timeout_s() -> float:
    raw = str(os.getenv("ADAOS_WEBSPACE_PROJECT_SCENARIO_TIMEOUT_S") or "").strip()
    if not raw:
        return 6.0
    try:
        value = float(raw)
    except Exception:
        return 6.0
    if value < 0.0:
        return 0.0
    if value > 120.0:
        return 120.0
    return value


def _normalize_optional_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def _local_node_id() -> str:
    try:
        conf = load_config()
        node_id = str(getattr(conf, "node_id", "") or "").strip()
        if node_id:
            return node_id
        nested = str(getattr(getattr(conf, "node_settings", None), "id", "") or "").strip()
        if nested:
            return nested
    except Exception:
        pass
    return "hub"


def _local_node_label() -> str:
    try:
        conf = load_config()
        return str(node_display_from_config(conf).get("node_label") or "").strip() or _local_node_id()
    except Exception:
        return _local_node_id()


def _local_node_display() -> dict[str, Any]:
    cached_at, cached = _LOCAL_NODE_DISPLAY_CACHE
    now = time.monotonic()
    if cached and (now - cached_at) <= _LOCAL_NODE_DISPLAY_CACHE_TTL_S:
        return dict(cached)
    try:
        display = node_display_from_config(load_config())
    except Exception:
        display = {
            "node_label": _local_node_label(),
            "node_compact_label": "N0",
            "node_index": 0,
            "node_color": "",
        }
    globals()["_LOCAL_NODE_DISPLAY_CACHE"] = (now, dict(display))
    return dict(display)


_HOME_SCENARIO_REF_UNSET = object()


@dataclass(slots=True)
class WebUIRegistryEntry:
    """
    Effective UI model snapshot for a single webspace after merging:

      - scenario-projected catalog/registry,
      - skill contributions from webui.json,
      - auto-installed items and current desktop overlay state.
    """

    scenario_id: str
    apps: List[Dict[str, Any]] = field(default_factory=list)
    widgets: List[Dict[str, Any]] = field(default_factory=list)
    registry_modals: List[str] = field(default_factory=list)
    registry_widgets: List[str] = field(default_factory=list)
    installed: Dict[str, List[str]] = field(default_factory=lambda: {"apps": [], "widgets": []})


@dataclass(slots=True)
class WebspaceInfo:
    """
    Lightweight snapshot of a webspace entry used by higher-level services
    and SDK helpers. ``is_dev`` is derived from the display name and can be
    used to filter workspace vs dev spaces.
    """

    id: str
    title: str
    created_at: int
    kind: str = "workspace"
    home_scenario: str = "web_desktop"
    home_scenario_ref: dict[str, Any] | None = None
    source_mode: str = "workspace"
    node_id: str = "hub"
    node_label: str = "hub"
    node_compact_label: str | None = None
    node_index: int | None = None
    node_color: str | None = None
    is_dev: bool = False
    current_scenario: str | None = None
    stored_home_scenario_exists: bool | None = None
    home_scenario_exists: bool = True
    current_scenario_exists: bool | None = None
    degraded: bool = False
    validation_reason: str | None = None
    recommended_action: str | None = None


@dataclass(slots=True)
class WebspaceOperationalState:
    """
    Lightweight operational view of a webspace that combines persistent
    manifest metadata with the current live scenario selection from Yjs.

    ``stored_home_scenario`` preserves whether the manifest explicitly stores
    a home scenario. This matters for legacy spaces, where reload/reset should
    still be able to fall back to ``ui.current_scenario`` instead of forcing
    ``web_desktop`` semantics too early.
    """

    webspace_id: str
    title: str
    kind: str
    source_mode: str
    is_dev: bool
    stored_home_scenario: str | None
    effective_home_scenario: str
    home_scenario_ref: dict[str, Any] | None
    current_scenario: str | None
    stored_home_scenario_exists: bool | None = None
    home_scenario_exists: bool = True
    current_scenario_exists: bool | None = None
    degraded: bool = False
    validation_reason: str | None = None
    recommended_action: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "webspace_id": self.webspace_id,
            "title": self.title,
            "kind": self.kind,
            "source_mode": self.source_mode,
            "is_dev": self.is_dev,
            "stored_home_scenario": self.stored_home_scenario,
            "home_scenario": self.effective_home_scenario,
            "home_scenario_ref": self.home_scenario_ref,
            "current_scenario": self.current_scenario,
            "stored_home_scenario_exists": self.stored_home_scenario_exists,
            "home_scenario_exists": self.home_scenario_exists,
            "current_scenario_exists": self.current_scenario_exists,
            "degraded": self.degraded,
            "validation_reason": self.validation_reason,
            "recommended_action": self.recommended_action,
            "current_matches_home": bool(self.current_scenario) and self.current_scenario == self.effective_home_scenario,
        }


@dataclass(slots=True)
class WebspaceResolverInputs:
    """
    Explicit resolver inputs for the current light-weight Phase 3 contract.

    `overlay_snapshot` is sourced from persistent webspace metadata and
    represents canonical desktop customization state for the current MVP
    Phase 5 boundary.
    """

    webspace_id: str
    scenario_id: str
    source_mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    scenario_application: Dict[str, Any] = field(default_factory=dict)
    scenario_catalog: Dict[str, Any] = field(default_factory=dict)
    scenario_registry: Dict[str, Any] = field(default_factory=dict)
    overlay_snapshot: Dict[str, Any] = field(default_factory=dict)
    live_state: Dict[str, Any] = field(default_factory=dict)
    compatibility_cache_presence: Dict[str, bool] = field(default_factory=dict)
    skill_decls: List[Dict[str, Any]] = field(default_factory=list)
    desktop_scenarios: List[Tuple[str, str]] = field(default_factory=list)
    scenario_source: str = "legacy_yjs"
    legacy_scenario_fallback: bool = False


@dataclass(slots=True)
class WebspaceResolverOutputs:
    """
    Materialized effective UI state computed from resolver inputs.

    These values are still written to the existing Yjs compatibility paths,
    but the merge result itself is now an explicit architectural layer.
    """

    webspace_id: str
    scenario_id: str
    source_mode: str
    application: Dict[str, Any] = field(default_factory=dict)
    catalog: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: {"apps": [], "widgets": []})
    registry: Dict[str, List[str]] = field(default_factory=lambda: {"modals": [], "widgets": []})
    installed: Dict[str, List[str]] = field(default_factory=lambda: {"apps": [], "widgets": []})
    desktop: Dict[str, Any] = field(default_factory=dict)
    webio: Dict[str, Any] = field(default_factory=dict)
    routing: Dict[str, Any] = field(default_factory=dict)
    skill_decls: List[Dict[str, Any]] = field(default_factory=list)

    def to_registry_entry(self) -> "WebUIRegistryEntry":
        return WebUIRegistryEntry(
            scenario_id=self.scenario_id,
            apps=[dict(it) for it in (self.catalog.get("apps") or []) if isinstance(it, Mapping)],
            widgets=[dict(it) for it in (self.catalog.get("widgets") or []) if isinstance(it, Mapping)],
            registry_modals=list(self.registry.get("modals") or []),
            registry_widgets=list(self.registry.get("widgets") or []),
            installed={
                "apps": list(self.installed.get("apps") or []),
                "widgets": list(self.installed.get("widgets") or []),
            },
        )


def _mark_entry(entry: Dict[str, Any], *, source: str, dev: bool) -> Dict[str, Any]:
    """
    Attach provenance / dev flag to a catalog entry without overwriting its
    semantic "source" (which may already contain a YDoc path like "y:data/...").
    """
    data = dict(entry)
    # Always keep provenance separate from semantic `source` paths used by
    # widget renderers (e.g. metric tiles reading y:data/...).
    data["origin"] = source
    data["dev"] = dev
    return data


def _mark_modal_def(entry: Any, *, source: str, skill: str, dev: bool) -> Dict[str, Any]:
    data = dict(entry) if isinstance(entry, Mapping) else {}
    skill_token = str(skill or "").strip()
    if source:
        data.setdefault("origin", source)
    if skill_token:
        data.setdefault("originSkill", skill_token)
    data.setdefault("dev", dev)
    meta = dict(data.get("_adaos") or {}) if isinstance(data.get("_adaos"), Mapping) else {}
    if source:
        meta.setdefault("origin", source)
    if skill_token:
        meta.setdefault("originSkill", skill_token)
    if meta:
        data["_adaos"] = meta
    return data


def _apply_node_display_to_entry(entry: Dict[str, Any], display: Mapping[str, Any] | None, *, node_id: str | None = None) -> Dict[str, Any]:
    data = dict(entry)
    resolved_node_id = str(node_id or data.get("node_id") or "").strip()
    if resolved_node_id and not str(data.get("node_id") or "").strip():
        data["node_id"] = resolved_node_id
    if not isinstance(display, Mapping):
        return data
    node_label = str(display.get("node_label") or "").strip()
    if node_label and not str(data.get("node_label") or "").strip():
        data["node_label"] = node_label
    compact_label = str(display.get("node_compact_label") or "").strip()
    if compact_label and not str(data.get("node_compact_label") or "").strip():
        data["node_compact_label"] = compact_label
    node_color = str(display.get("node_color") or "").strip()
    if node_color and not str(data.get("node_color") or "").strip():
        data["node_color"] = node_color
    node_index = display.get("node_index")
    if node_index is not None and data.get("node_index") is None:
        data["node_index"] = node_index
    return data


def _is_node_owned_skill(skill_name: str) -> bool:
    token = str(skill_name or "").strip()
    return bool(token) and token != "web_desktop_skill"


def _decl_is_node_owned(decl: Mapping[str, Any] | None) -> bool:
    if not isinstance(decl, Mapping):
        return False
    owner = str(decl.get("ui_owner") or "").strip().lower()
    if owner == "shared":
        return False
    if owner == "node":
        return True
    return _is_node_owned_skill(str(decl.get("skill") or ""))


def _scope_node_data_source(data_source: Any, *, node_id: str) -> Any:
    if not isinstance(data_source, Mapping):
        return data_source
    scoped = _clone_json_like(data_source)
    if not isinstance(scoped, dict):
        return data_source
    kind = str(scoped.get("kind") or "").strip().lower()
    if kind == "stream":
        if node_id and not str(scoped.get("nodeId") or scoped.get("node_id") or "").strip():
            scoped["nodeId"] = node_id
    if kind == "y" and scoped.get("path") is not None:
        scoped["path"] = node_scope_data_path(str(scoped.get("path") or ""), node_id)
    if kind == "api":
        for key in ("params", "body"):
            value = scoped.get(key)
            if isinstance(value, dict) and node_id and not str(value.get("node_id") or value.get("target_node_id") or "").strip():
                value["node_id"] = node_id
                value.setdefault("target_node_id", node_id)
    return scoped


def _apply_node_context_to_ui(
    value: Any,
    display: Mapping[str, Any] | None,
    *,
    node_id: str,
    modal_id_map: Mapping[str, str] | None = None,
) -> Any:
    if not node_id:
        return _clone_json_like(value)
    if isinstance(value, list):
        return [
            _apply_node_context_to_ui(item, display, node_id=node_id, modal_id_map=modal_id_map)
            for item in value
        ]
    if not isinstance(value, Mapping):
        return _clone_json_like(value)

    data: Dict[str, Any] = {
        str(key): _apply_node_context_to_ui(item, display, node_id=node_id, modal_id_map=modal_id_map)
        for key, item in value.items()
    }
    if data.get("id") or data.get("type") or data.get("dataSource") or data.get("actions") or data.get("source"):
        data = _apply_node_display_to_entry(data, display, node_id=node_id)
    if isinstance(data.get("dataSource"), Mapping):
        data["dataSource"] = _scope_node_data_source(data.get("dataSource"), node_id=node_id)
    if isinstance(data.get("source"), str):
        data["source"] = node_scope_data_path(str(data.get("source") or ""), node_id)
    if isinstance(data.get("launchModal"), str):
        modal_id = str(data.get("launchModal") or "").strip()
        if modal_id_map and modal_id in modal_id_map:
            data["launchModal"] = modal_id_map[modal_id]
        elif modal_id:
            data["launchModal"] = _node_scoped_catalog_id(node_id, modal_id)
    return data


def _node_scoped_modal_ids(registry: Mapping[str, Any], *, node_id: str) -> Dict[str, str]:
    if not node_id:
        return {}
    modals = registry.get("modals") if isinstance(registry.get("modals"), Mapping) else {}
    if not isinstance(modals, Mapping):
        return {}
    out: Dict[str, str] = {}
    for key in modals.keys():
        token = str(key or "").strip()
        if token:
            scoped = _node_scoped_catalog_id(node_id, token)
            out[token] = scoped
            stripped = _strip_node_scoped_catalog_prefix(token)
            if stripped:
                out[stripped] = scoped
    return out


def _local_catalog_decl_entries(decls: List[Dict[str, Any]]) -> dict[str, Any]:
    try:
        conf = load_config()
        display = node_display_from_config(conf)
    except Exception:
        display = {
            "node_label": _local_node_label(),
            "node_compact_label": "N0",
            "node_color": "",
            "node_index": 0,
        }
    node_id = _local_node_id()
    apps: List[Dict[str, Any]] = []
    widgets: List[Dict[str, Any]] = []
    registry_modals: Dict[str, Any] = {}
    registry_widgets: Dict[str, Any] = {}
    webio_receivers: Dict[str, Any] = {}
    ydoc_defaults: Dict[str, Any] = {}
    for decl in decls:
        skill_name = str(decl.get("skill") or "").strip()
        source = f"skill:{skill_name}" if skill_name else "skill:unknown"
        dev_flag = str(decl.get("space") or "default").strip().lower() == "dev"
        node_owned = _decl_is_node_owned(decl)
        reg = decl.get("registry") if isinstance(decl.get("registry"), Mapping) else {}
        modal_id_map = _node_scoped_modal_ids(reg, node_id=node_id) if node_owned else {}
        for app in decl.get("apps") or []:
            if not isinstance(app, dict):
                continue
            entry = _mark_entry(app, source=source, dev=dev_flag)
            if node_owned:
                entry = _apply_node_context_to_ui(entry, display, node_id=node_id, modal_id_map=modal_id_map)
            apps.append(_apply_node_display_to_entry(entry, display, node_id=node_id))
        for widget in decl.get("widgets") or []:
            if not isinstance(widget, dict):
                continue
            entry = _mark_entry(widget, source=source, dev=dev_flag)
            if node_owned:
                entry = _apply_node_context_to_ui(entry, display, node_id=node_id, modal_id_map=modal_id_map)
            widgets.append(_apply_node_display_to_entry(entry, display, node_id=node_id))
        mod_spec = reg.get("modals") if isinstance(reg, Mapping) else {}
        if isinstance(mod_spec, Mapping):
            for key, value in mod_spec.items():
                token = str(key or "").strip()
                if not token:
                    continue
                scoped_token = modal_id_map.get(token, token)
                if scoped_token in registry_modals:
                    continue
                modal_def = (
                    _apply_node_context_to_ui(value, display, node_id=node_id, modal_id_map=modal_id_map)
                    if node_owned
                    else _clone_json_like(value)
                )
                registry_modals[scoped_token] = _mark_modal_def(
                    modal_def,
                    source=source,
                    skill=skill_name,
                    dev=dev_flag,
                )
        wid_spec = reg.get("widgets") if isinstance(reg, Mapping) else {}
        if isinstance(wid_spec, Mapping):
            for key, value in wid_spec.items():
                token = str(key or "").strip()
                if not token or token in registry_widgets:
                    continue
                registry_widgets[token] = (
                    _apply_node_context_to_ui(value, display, node_id=node_id, modal_id_map=modal_id_map)
                    if node_owned
                    else _clone_json_like(value)
                )
        webio = decl.get("webio") if isinstance(decl.get("webio"), Mapping) else {}
        receivers = webio.get("receivers") if isinstance(webio.get("receivers"), Mapping) else {}
        for key, value in receivers.items():
            token = str(key or "").strip()
            if token and token not in webio_receivers:
                webio_receivers[token] = _normalize_webio_receiver(value)
        raw_defaults = decl.get("ydoc_defaults") if isinstance(decl.get("ydoc_defaults"), Mapping) else {}
        for path, value in raw_defaults.items():
            token = str(path or "").strip()
            if not token:
                continue
            scoped_path = node_scope_data_path(token, node_id) if node_owned else token
            ydoc_defaults.setdefault(scoped_path, _clone_json_like(value))
    return {
        "captured_at": time.time(),
        "apps": _merge_by_id(apps),
        "widgets": _merge_by_id(widgets),
        "registry": {
            "modals": registry_modals,
            "widgets": registry_widgets,
        },
        "webio": {"receivers": webio_receivers},
        "ydoc_defaults": ydoc_defaults,
    }


def build_local_desktop_catalog_snapshot(*, mode: str = "workspace", include_remote: bool = True) -> dict[str, Any]:
    runtime = WebspaceScenarioRuntime()
    snapshot = _local_catalog_decl_entries(runtime._collect_skill_decls(mode=mode, include_remote=include_remote))
    return _overlay_current_ydoc_defaults(snapshot, webspace_id=default_webspace_id())


async def build_local_desktop_catalog_snapshot_async(*, mode: str = "workspace", include_remote: bool = True) -> dict[str, Any]:
    runtime = WebspaceScenarioRuntime()
    snapshot = _local_catalog_decl_entries(runtime._collect_skill_decls(mode=mode, include_remote=include_remote))
    return await _overlay_current_ydoc_defaults_async(snapshot, webspace_id=default_webspace_id())


_YDOC_PATH_MISSING = object()


def _read_current_ydoc_path_value(ydoc: Any, path: str) -> Any:
    segments = [str(seg or "").strip() for seg in str(path or "").split("/") if str(seg or "").strip()]
    if len(segments) < 2:
        return _YDOC_PATH_MISSING
    current: Any = ydoc.get_map(segments[0])
    for seg in segments[1:]:
        getter = getattr(current, "get", None)
        if not callable(getter):
            return _YDOC_PATH_MISSING
        current = getter(seg)
        if current is None:
            return _YDOC_PATH_MISSING
    return _clone_json_like(current)


def _read_current_ydoc_path_value_with_local_node_fallback(ydoc: Any, path: str) -> Any:
    current = _read_current_ydoc_path_value(ydoc, path)
    if current is not _YDOC_PATH_MISSING:
        return current
    local_path = local_unscoped_data_path(path, _local_node_id())
    if not local_path or local_path == path:
        return _YDOC_PATH_MISSING
    return _read_current_ydoc_path_value(ydoc, local_path)


def _resolve_live_room_ydoc(webspace_id: str) -> Any | None:
    try:
        from adaos.services.yjs.gateway import y_server  # pylint: disable=import-outside-toplevel
    except Exception:
        return None
    room = getattr(y_server, "rooms", {}).get(str(webspace_id or "").strip())
    ydoc = getattr(room, "ydoc", None)
    return ydoc if ydoc is not None else None


def _sync_ydoc_read_allowed() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _overlay_current_ydoc_defaults(snapshot: dict[str, Any], *, webspace_id: str) -> dict[str, Any]:
    defaults = snapshot.get("ydoc_defaults") if isinstance(snapshot.get("ydoc_defaults"), dict) else {}
    if not defaults:
        return snapshot
    live_ydoc = _resolve_live_room_ydoc(webspace_id)
    pending_paths = list(defaults.keys())
    if live_ydoc is not None:
        remaining: list[str] = []
        for path in pending_paths:
            live_value = _read_current_ydoc_path_value_with_local_node_fallback(live_ydoc, str(path or ""))
            if live_value is _YDOC_PATH_MISSING:
                remaining.append(str(path))
                continue
            defaults[str(path)] = live_value
        pending_paths = remaining
    if not pending_paths or not _sync_ydoc_read_allowed():
        snapshot["ydoc_defaults"] = defaults
        return snapshot
    try:
        with get_ydoc(webspace_id) as ydoc:
            for path in pending_paths:
                live_value = _read_current_ydoc_path_value_with_local_node_fallback(ydoc, str(path or ""))
                if live_value is _YDOC_PATH_MISSING:
                    continue
                defaults[str(path)] = live_value
    except Exception:
        return snapshot
    snapshot["ydoc_defaults"] = defaults
    return snapshot


async def _overlay_current_ydoc_defaults_async(snapshot: dict[str, Any], *, webspace_id: str) -> dict[str, Any]:
    defaults = snapshot.get("ydoc_defaults") if isinstance(snapshot.get("ydoc_defaults"), dict) else {}
    if not defaults:
        return snapshot
    live_ydoc = _resolve_live_room_ydoc(webspace_id)
    pending_paths = list(defaults.keys())
    if live_ydoc is not None:
        remaining: list[str] = []
        for path in pending_paths:
            live_value = _read_current_ydoc_path_value_with_local_node_fallback(live_ydoc, str(path or ""))
            if live_value is _YDOC_PATH_MISSING:
                remaining.append(str(path))
                continue
            defaults[str(path)] = live_value
        pending_paths = remaining
    if not pending_paths:
        snapshot["ydoc_defaults"] = defaults
        return snapshot
    try:
        async with async_read_ydoc(webspace_id) as ydoc:
            for path in pending_paths:
                live_value = _read_current_ydoc_path_value_with_local_node_fallback(ydoc, str(path or ""))
                if live_value is _YDOC_PATH_MISSING:
                    continue
                defaults[str(path)] = live_value
    except Exception:
        snapshot["ydoc_defaults"] = defaults
        return snapshot
    snapshot["ydoc_defaults"] = defaults
    return snapshot


def _merge_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        merged.append(item)
    return merged


def _strip_node_scoped_catalog_prefix(item_id: str) -> str:
    item_token = str(item_id or "").strip()
    while item_token.startswith("node:"):
        _prefix, _node_id, remainder = item_token.split(":", 2) if item_token.count(":") >= 2 else ("", "", "")
        if not remainder:
            break
        item_token = remainder.strip()
    return item_token


def _node_scoped_catalog_id(node_id: str, item_id: str) -> str:
    node_token = str(node_id or "").strip()
    item_token = _strip_node_scoped_catalog_prefix(item_id)
    if not node_token or not item_token:
        return item_token
    return f"node:{node_token}:{item_token}"


def _node_scoped_entry_node_id(item_id: Any) -> str | None:
    token = str(item_id or "").strip()
    if not token.startswith("node:"):
        return None
    parts = token.split(":", 2)
    if len(parts) != 3:
        return None
    node_id = str(parts[1] or "").strip()
    return node_id or None


def _node_scoped_data_path_node_id(path: Any) -> str | None:
    raw = str(path or "").strip()
    if raw.startswith("y:"):
        raw = raw[2:]
    parts = [part for part in raw.split("/") if part]
    if len(parts) < 3 or parts[0] != "data" or parts[1] != "nodes":
        return None
    node_id = str(parts[2] or "").strip()
    return node_id or None


def _catalog_entry_is_foreign_relay(entry: Mapping[str, Any], *, node_id: str) -> bool:
    entry_node_id = _node_scoped_entry_node_id(entry.get("id"))
    if entry_node_id and entry_node_id != str(node_id or "").strip():
        return True
    source = str(entry.get("origin") or entry.get("source") or "").strip()
    if source.startswith("skill:subnet.member."):
        source_node_id = source.removeprefix("skill:subnet.member.").strip()
        if source_node_id and source_node_id != str(node_id or "").strip():
            return True
    return False


def _preserve_live_remote_catalog_entries(
    merged: List[Dict[str, Any]],
    *,
    current_items: Any,
    active_remote_node_ids: set[str],
) -> List[Dict[str, Any]]:
    if not isinstance(current_items, list):
        return merged
    seen_ids = {
        str(item.get("id") or "").strip()
        for item in merged
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    preserved: List[Dict[str, Any]] = list(merged)
    for item in current_items:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen_ids:
            continue
        node_id = _node_scoped_entry_node_id(item_id)
        if not node_id or node_id in active_remote_node_ids:
            continue
        preserved.append(dict(item))
        seen_ids.add(item_id)
    return preserved


def _preserve_live_remote_modals(
    merged_modals_map: Dict[str, Any],
    *,
    current_modals: Any,
    active_remote_node_ids: set[str],
) -> Dict[str, Any]:
    if not isinstance(current_modals, Mapping):
        return merged_modals_map
    preserved = dict(merged_modals_map)
    for key, value in current_modals.items():
        modal_id = str(key or "").strip()
        if not modal_id or modal_id in preserved:
            continue
        node_id = _node_scoped_entry_node_id(modal_id)
        if not node_id or node_id in active_remote_node_ids:
            continue
        preserved[modal_id] = _clone_json_like(value)
    return preserved


def _preserve_live_remote_registry_tokens(
    merged_tokens: List[str],
    *,
    current_tokens: Any,
    active_remote_node_ids: set[str],
) -> List[str]:
    tokens = [str(token or "").strip() for token in merged_tokens if str(token or "").strip()]
    seen = set(tokens)
    if not isinstance(current_tokens, list):
        return tokens
    for value in current_tokens:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        node_id = _node_scoped_entry_node_id(token)
        if not node_id or node_id in active_remote_node_ids:
            continue
        tokens.append(token)
        seen.add(token)
    return tokens


def _scope_remote_catalog_entry_id(entry: Dict[str, Any], *, node_id: str) -> Dict[str, Any]:
    data = dict(entry)
    remote_node_id = str(node_id or "").strip()
    local_item_id = _strip_node_scoped_catalog_prefix(str(data.get("id") or "").strip())
    if not remote_node_id or not local_item_id:
        return data
    canonical_local_id = _strip_node_scoped_catalog_prefix(
        str(data.get("node_local_id") or data.get("remote_id") or local_item_id).strip()
    )
    data["node_local_id"] = canonical_local_id or local_item_id
    data["remote_id"] = canonical_local_id or local_item_id
    data["id"] = _node_scoped_catalog_id(remote_node_id, local_item_id)
    return data


def _merge_registry_lists(base: List[str], extras: List[List[str]]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for value in base:
        token = str(value)
        if token and token not in seen:
            seen.add(token)
            merged.append(token)
    for contrib in extras:
        for token in contrib:
            token = str(token)
            if token and token not in seen:
                seen.add(token)
                merged.append(token)
    return merged


def _filter_installed(installed: Dict[str, List[str]], apps: List[Dict[str, Any]], widgets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    app_ids = {str(item.get("id")) for item in apps if item.get("id")}
    widget_ids = {str(item.get("id")) for item in widgets if item.get("id")}
    current_apps = [a for a in (installed.get("apps") or []) if a in app_ids]
    current_widgets = [w for w in (installed.get("widgets") or []) if w in widget_ids]
    return {"apps": current_apps, "widgets": current_widgets}

def _dedupe_str_list(values: Any) -> List[str]:
    # YJS may return YArray-like values which are iterable but not `list`.
    if isinstance(values, (str, bytes, bytearray)) or isinstance(values, Mapping):
        return []
    if not isinstance(values, Iterable):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        token = v.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """
    Best-effort conversion of YJS map-like values to a plain dict.

    y_py map objects are not guaranteed to implement `collections.abc.Mapping`
    but they often expose `.items()`. Using `isinstance(..., Mapping)` only
    can silently drop persisted state (e.g. installed apps) during scenario
    switches.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:
            return {}
    return {}


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    getter = getattr(value, "get", None)
    if callable(getter):
        try:
            result = getter(key)
            return default if result is None else result
        except Exception:
            return default
    return default


def _coerce_live_branch_subset(value: Any, keys: tuple[str, ...]) -> Dict[str, Any]:
    """
    Read only the live YMap branches the resolver actually needs.

    Full ``dict(y_map.items())`` materialization is expensive on large Yjs
    maps and used to dominate scenario-switch allocation profiles. The
    resolver only needs a few preservation inputs from live state, so keep this
    intentionally shallow and explicit.
    """
    out: Dict[str, Any] = {}
    for key in keys:
        item = _mapping_get(value, key)
        if item is None:
            continue
        if key in {"modals", "installed", "pageSchema", "routes"}:
            out[key] = _coerce_dict(item)
        elif isinstance(item, (list, tuple)):
            out[key] = list(item)
        else:
            out[key] = item
    return out


def _normalize_webui_load_hint(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, str] = {}
    structure = str(value.get("structure") or "").strip()
    if structure in _WEBUI_LOAD_PHASES:
        out["structure"] = structure
    data = str(value.get("data") or "").strip()
    if data in _WEBUI_LOAD_PHASES:
        out["data"] = data
    focus = str(value.get("focus") or "").strip()
    if focus in _WEBUI_LOAD_FOCUS:
        out["focus"] = focus
    off_focus_ready = str(value.get("offFocusReadyState") or "").strip()
    if off_focus_ready in _WEBUI_READINESS_STATES:
        out["offFocusReadyState"] = off_focus_ready
    return out


def _apply_webui_load_hint(node: Any) -> Dict[str, Any]:
    item = _coerce_dict(node)
    if not item:
        return {}
    load = _normalize_webui_load_hint(item.get("load"))
    if load:
        item["load"] = load
    else:
        item.pop("load", None)
    return item


def _normalize_webui_widget_config(node: Any) -> Dict[str, Any]:
    return _apply_webui_load_hint(node)


def _normalize_webui_page_schema(node: Any) -> Dict[str, Any]:
    page = _apply_webui_load_hint(node)
    if not page:
        return {}
    widgets = page.get("widgets")
    if isinstance(widgets, list):
        page["widgets"] = [_normalize_webui_widget_config(widget) for widget in widgets if isinstance(widget, Mapping)]
    return page


def _normalize_webui_modal_def(node: Any) -> Dict[str, Any]:
    modal = _apply_webui_load_hint(node)
    if not modal:
        return {}
    schema = modal.get("schema")
    if isinstance(schema, Mapping):
        modal["schema"] = _normalize_webui_page_schema(schema)
    return modal


def _clone_json_like(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if value is None:
            return None
        if isinstance(value, dict):
            return {str(k): _clone_json_like(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clone_json_like(v) for v in value]
        if isinstance(value, tuple):
            return [_clone_json_like(v) for v in value]
        if isinstance(value, Mapping):
            return {str(k): _clone_json_like(v) for k, v in value.items()}
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(k): _clone_json_like(v) for k, v in items()}
            except Exception:
                return value
        return value


def _json_like_equal(current: Any, next_value: Any) -> bool:
    if current is next_value:
        return True

    current_items = _mapping_items(current)
    next_items = _mapping_items(next_value)
    if current_items is not None or next_items is not None:
        if current_items is None or next_items is None:
            return False
        if len(current_items) != len(next_items):
            return False
        next_lookup = {key: item for key, item in next_items}
        if len(next_lookup) != len(next_items):
            return False
        for key, current_item in current_items:
            if key not in next_lookup:
                return False
            if not _json_like_equal(current_item, next_lookup[key]):
                return False
        return True

    if isinstance(current, (list, tuple)) or isinstance(next_value, (list, tuple)):
        if not isinstance(current, (list, tuple)) or not isinstance(next_value, (list, tuple)):
            return False
        if len(current) != len(next_value):
            return False
        return all(_json_like_equal(left, right) for left, right in zip(current, next_value))

    try:
        return current == next_value
    except Exception:
        return _clone_json_like(current) == _clone_json_like(next_value)


def _merge_nested_json_path(existing: Any, segments: List[str], payload: Any) -> tuple[bool, Any]:
    if not segments:
        if _json_like_equal(existing, payload):
            return False, existing
        return True, _clone_json_like(payload)

    key = str(segments[0] or "")
    if not key:
        return False, _clone_json_like(existing)

    child_existing = None
    items = _mapping_items(existing)
    if items is not None:
        for item_key, item_value in items:
            if item_key == key:
                child_existing = item_value
                break

    changed, merged_child = _merge_nested_json_path(child_existing, segments[1:], payload)
    if not changed:
        return False, existing

    base = _clone_json_like(existing)
    if not isinstance(base, dict):
        base = {}
    merged = dict(base)
    merged[key] = merged_child
    return True, merged


def _nested_json_path_exists(existing: Any, segments: List[str]) -> bool:
    current = existing
    for segment in segments:
        key = str(segment or "")
        if not key:
            return False
        items = _mapping_items(current)
        if items is None:
            return False
        found = False
        for item_key, item_value in items:
            if item_key == key:
                current = item_value
                found = True
                break
        if not found:
            return False
    return True


def _is_y_map_value(value: Any) -> bool:
    y_map_type = getattr(Y, "YMap", None)
    return bool(y_map_type) and isinstance(value, y_map_type)


def _mapping_items(value: Any) -> list[tuple[str, Any]] | None:
    if isinstance(value, Mapping):
        return [(str(key), item) for key, item in value.items() if str(key)]
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return [(str(key), item) for key, item in items() if str(key)]
        except Exception:
            return None
    return None


def _attach_empty_y_map(parent_map: Any, txn: Any, key: str) -> Any | None:
    y_map_type = getattr(Y, "YMap", None)
    if not y_map_type or not _is_y_map_value(parent_map):
        return None
    try:
        parent_map.set(txn, key, y_map_type({}))
        attached = parent_map.get(key)
    except Exception:
        return None
    return attached if _is_y_map_value(attached) else None


def _reconcile_attached_y_map(node: Any, txn: Any, next_value: Any) -> bool:
    next_items = _mapping_items(next_value)
    if next_items is None:
        return False
    changed = False
    next_keys = {key for key, _item in next_items}
    try:
        current_keys = tuple(str(key) for key in node.keys() if str(key))
    except Exception:
        current_keys = ()
    for current_key in current_keys:
        if current_key in next_keys:
            continue
        try:
            node.pop(txn, current_key)
            changed = True
        except Exception:
            continue
    for child_key, raw_child in next_items:
        child_items = _mapping_items(raw_child)
        try:
            current_child = node.get(child_key)
        except Exception:
            current_child = None
        if child_items is not None:
            if _is_y_map_value(current_child):
                if _reconcile_attached_y_map(current_child, txn, raw_child):
                    changed = True
                continue
            if _json_like_equal(current_child, raw_child):
                continue
            attached_child = _attach_empty_y_map(node, txn, child_key)
            if attached_child is None:
                node.set(txn, child_key, _clone_json_like(raw_child))
                changed = True
                continue
            changed = True
            _reconcile_attached_y_map(attached_child, txn, raw_child)
            continue
        if _json_like_equal(current_child, raw_child):
            continue
        node.set(txn, child_key, _clone_json_like(raw_child))
        changed = True
    return changed


def _fingerprint_json_like(value: Any) -> str:
    try:
        normalized = json.dumps(
            _clone_json_like(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except Exception:
        normalized = repr(value)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _resolved_output_branch_fingerprints(resolved: "WebspaceResolverOutputs") -> Dict[str, str]:
    return {
        "ui.application": _fingerprint_json_like(resolved.application),
        "data.catalog": _fingerprint_json_like(resolved.catalog),
        "data.installed": _fingerprint_json_like(resolved.installed),
        "data.desktop": _fingerprint_json_like(resolved.desktop),
        "data.webio": _fingerprint_json_like(resolved.webio),
        "data.routing": _fingerprint_json_like(resolved.routing),
        "registry.merged": _fingerprint_json_like(resolved.registry),
        "runtime.environment": _fingerprint_json_like(runtime_environment_payload()),
    }


def _normalize_webio_receiver(node: Any) -> Dict[str, Any]:
    item = _coerce_dict(node)
    if not item:
        return {}
    out: Dict[str, Any] = {}
    mode = str(item.get("mode") or "").strip().lower()
    if mode in {"replace", "append"}:
        out["mode"] = mode
    collection_key = str(item.get("collectionKey") or "").strip()
    if collection_key:
        out["collectionKey"] = collection_key
    dedupe_by = str(item.get("dedupeBy") or "").strip()
    if dedupe_by:
        out["dedupeBy"] = dedupe_by
    max_items = item.get("maxItems")
    try:
        if max_items is not None and int(max_items) > 0:
            out["maxItems"] = int(max_items)
    except Exception:
        pass
    node_id = str(item.get("nodeId") or item.get("node_id") or "").strip()
    if node_id:
        out["nodeId"] = node_id
    transport = str(item.get("transport") or "").strip().lower()
    if transport in {"auto", "member", "hub"}:
        out["transport"] = transport
    if "initialState" in item:
        out["initialState"] = _clone_json_like(item.get("initialState"))
    return out


def _merge_webio_receivers(skill_decls: List[Dict[str, Any]]) -> Dict[str, Any]:
    receivers: Dict[str, Any] = {}
    for decl in skill_decls:
        skill_name = str(decl.get("skill") or "").strip()
        webio = decl.get("webio") if isinstance(decl.get("webio"), Mapping) else {}
        raw_receivers = webio.get("receivers") if isinstance(webio.get("receivers"), Mapping) else {}
        for key, value in raw_receivers.items():
            receiver_id = str(key or "").strip()
            if not receiver_id or receiver_id in receivers:
                continue
            normalized = _normalize_webio_receiver(value)
            if not normalized:
                continue
            normalized["id"] = receiver_id
            if skill_name:
                normalized["origin"] = f"skill:{skill_name}"
            receivers[receiver_id] = normalized
    return {"receivers": receivers}


def _read_node_scoped_scenario_entry(
    scenarios_root: Any,
    scenario_id: str,
    *,
    node_id: str | None = None,
) -> Dict[str, Any]:
    target_node_id = str(node_id or "").strip() or _local_node_id()

    local_bucket = _mapping_get(scenarios_root or {}, target_node_id) or {}
    local_entry = _coerce_dict(_mapping_get(local_bucket, scenario_id) or {})
    if local_entry:
        return local_entry

    root_items = _mapping_items(scenarios_root or {}) or []
    for _bucket_key, maybe_bucket in root_items:
        entry = _coerce_dict(_mapping_get(maybe_bucket or {}, scenario_id) or {})
        if entry:
            return entry
    return {}


def _read_effective_branch_fingerprints(registry_map: Any) -> Dict[str, str]:
    runtime_meta = _coerce_dict(registry_map.get("runtime_meta") or {})
    stored = _coerce_dict(runtime_meta.get(_RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY) or {})
    fingerprints: Dict[str, str] = {}
    for path in _EFFECTIVE_BRANCH_PATHS:
        token = str(stored.get(path) or "").strip()
        if token:
            fingerprints[path] = token
    return fingerprints


def _write_effective_branch_fingerprints(
    registry_map: Any,
    txn: Any,
    *,
    current: Mapping[str, str],
    updates: Mapping[str, str],
) -> bool:
    runtime_meta = _coerce_dict(registry_map.get("runtime_meta") or {})
    next_runtime_meta = dict(runtime_meta)
    next_fingerprints = _coerce_dict(next_runtime_meta.get(_RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY) or {})
    changed = False
    for path in _EFFECTIVE_BRANCH_PATHS:
        current_value = str(current.get(path) or "").strip()
        next_value = str(updates.get(path) or current_value).strip()
        if not next_value:
            continue
        if str(next_fingerprints.get(path) or "").strip() == next_value:
            continue
        next_fingerprints[path] = next_value
        changed = True
    if not changed:
        return False
    next_runtime_meta[_RUNTIME_META_EFFECTIVE_BRANCH_FINGERPRINTS_KEY] = next_fingerprints
    _set_map_value_if_changed(registry_map, txn, "runtime_meta", next_runtime_meta)
    return True


def _has_effective_branch_value(y_map: Any, key: str) -> bool:
    try:
        return y_map.get(key) is not None
    except Exception:
        return False


def _resolver_cache_keys(inputs: WebspaceResolverInputs) -> Dict[str, str]:
    scenario_snapshot = {
        "scenario_id": inputs.scenario_id,
        "source_mode": inputs.source_mode,
        "scenario_source": inputs.scenario_source,
        "legacy_scenario_fallback": inputs.legacy_scenario_fallback,
        "scenario_application": inputs.scenario_application,
        "scenario_catalog": inputs.scenario_catalog,
        "scenario_registry": inputs.scenario_registry,
    }
    return {
        "scenario": _fingerprint_json_like(scenario_snapshot),
        "skills": _fingerprint_json_like(inputs.skill_decls),
        "overlay": _fingerprint_json_like(inputs.overlay_snapshot),
        "live": _fingerprint_json_like(inputs.live_state),
        "desktop_scenarios": _fingerprint_json_like(inputs.desktop_scenarios),
    }


def _resolver_input_fingerprint(inputs: WebspaceResolverInputs, *, cache_keys: Mapping[str, Any]) -> str:
    snapshot = {
        "webspace_id": inputs.webspace_id,
        "scenario_id": inputs.scenario_id,
        "source_mode": inputs.source_mode,
        "scenario_source": inputs.scenario_source,
        "legacy_scenario_fallback": inputs.legacy_scenario_fallback,
        "metadata": inputs.metadata,
        "cache_keys": dict(cache_keys),
    }
    return _fingerprint_json_like(snapshot)


def _resolved_outputs_to_cache_payload(resolved: WebspaceResolverOutputs) -> Dict[str, Any]:
    return {
        "webspace_id": str(resolved.webspace_id or ""),
        "scenario_id": str(resolved.scenario_id or ""),
        "source_mode": str(resolved.source_mode or ""),
        "application": _clone_json_like(resolved.application),
        "catalog": _clone_json_like(resolved.catalog),
        "registry": _clone_json_like(resolved.registry),
        "installed": _clone_json_like(resolved.installed),
        "desktop": _clone_json_like(resolved.desktop),
        "webio": _clone_json_like(resolved.webio),
        "routing": _clone_json_like(resolved.routing),
        "skill_decls": _clone_json_like(resolved.skill_decls),
    }


def _resolved_outputs_from_cache_payload(payload: Mapping[str, Any]) -> WebspaceResolverOutputs:
    return WebspaceResolverOutputs(
        webspace_id=str(payload.get("webspace_id") or ""),
        scenario_id=str(payload.get("scenario_id") or ""),
        source_mode=str(payload.get("source_mode") or ""),
        application=_coerce_dict(payload.get("application") or {}),
        catalog=_coerce_dict(payload.get("catalog") or {}),
        registry=_coerce_dict(payload.get("registry") or {}),
        installed=_coerce_dict(payload.get("installed") or {}),
        desktop=_coerce_dict(payload.get("desktop") or {}),
        webio=_coerce_dict(payload.get("webio") or {}),
        routing=_coerce_dict(payload.get("routing") or {}),
        skill_decls=[
            dict(item)
            for item in (payload.get("skill_decls") or [])
            if isinstance(item, Mapping)
        ],
    )


def _get_cached_resolved_outputs(fingerprint: str) -> WebspaceResolverOutputs | None:
    token = str(fingerprint or "").strip()
    if not token:
        return None
    cached = _RESOLVED_WEBSPACE_CACHE.get(token)
    if not isinstance(cached, Mapping):
        return None
    _RESOLVED_WEBSPACE_CACHE.move_to_end(token)
    return _resolved_outputs_from_cache_payload(cached)


def _remember_resolved_outputs(fingerprint: str, resolved: WebspaceResolverOutputs) -> None:
    token = str(fingerprint or "").strip()
    if not token:
        return
    _RESOLVED_WEBSPACE_CACHE[token] = _resolved_outputs_to_cache_payload(resolved)
    _RESOLVED_WEBSPACE_CACHE.move_to_end(token)
    while len(_RESOLVED_WEBSPACE_CACHE) > _RESOLVED_WEBSPACE_CACHE_LIMIT:
        _RESOLVED_WEBSPACE_CACHE.popitem(last=False)


def _set_map_value_if_changed(y_map: Any, txn: Any, key: str, value: Any) -> tuple[bool, str]:
    next_items = _mapping_items(value)
    try:
        current = y_map.get(key)
    except Exception:
        current = None
    if next_items is not None:
        if _is_y_map_value(current):
            return _reconcile_attached_y_map(current, txn, value), "diff"
        if _json_like_equal(current, value):
            return False, "diff"
        attached = _attach_empty_y_map(y_map, txn, key)
        if attached is not None:
            _reconcile_attached_y_map(attached, txn, value)
            return True, "diff"
        y_map.set(txn, key, _clone_json_like(value))
        return True, "replace"
    if _json_like_equal(current, value):
        return False, "replace"
    y_map.set(txn, key, _clone_json_like(value))
    return True, "replace"


def _replace_map_value(y_map: Any, txn: Any, key: str, value: Any) -> tuple[bool, str]:
    y_map.set(txn, key, _clone_json_like(value))
    return True, "replace"


def _merge_installed_with_auto(installed: Dict[str, Any], *, auto_apps: set[str], auto_widgets: set[str]) -> Dict[str, List[str]]:
    """
    Merge existing installed apps/widgets with auto-installed ids while
    preserving user choices across scenario switches.

    Important: we do NOT drop ids that are not present in the current catalog,
    because switching scenarios would otherwise lose installed apps/widgets
    that become available again when returning to the previous scenario.
    """
    apps = _dedupe_str_list(installed.get("apps"))
    widgets = _dedupe_str_list(installed.get("widgets"))
    removed_apps = set(_dedupe_str_list(installed.get("removedApps")))
    removed_widgets = set(_dedupe_str_list(installed.get("removedWidgets")))

    for app_id in sorted(auto_apps):
        if app_id not in removed_apps and app_id not in apps:
            apps.append(app_id)
    for widget_id in sorted(auto_widgets):
        if widget_id not in removed_widgets and widget_id not in widgets:
            widgets.append(widget_id)

    return {"apps": apps, "widgets": widgets}


def _normalize_overlay_widget_entries(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        item["id"] = item_id
        if item.get("type") is not None:
            item["type"] = str(item.get("type"))
        out.append(item)
    return out


def _built_in_scenario_content(scenario_id: str) -> Dict[str, Any]:
    if str(scenario_id or "").strip() != "web_desktop":
        return {}
    try:
        app = json.loads(json.dumps(((SEED.get("ui") or {}).get("application") or {})))
        data = json.loads(json.dumps((SEED.get("data") or {})))
    except Exception:
        return {}
    catalog = data.get("catalog") if isinstance(data, dict) else {}
    if not isinstance(catalog, dict):
        catalog = {}
    return {
        "id": "web_desktop",
        "ui": {"application": app if isinstance(app, dict) else {}},
        "registry": {},
        "catalog": catalog,
        "data": data if isinstance(data, dict) else {},
    }


def _load_scenario_switch_content(scenario_id: str, *, space: str) -> Dict[str, Any]:
    content = scenarios_loader.read_content(scenario_id, space=space)
    if isinstance(content, dict) and content:
        return content
    fallback = _built_in_scenario_content(scenario_id)
    if fallback:
        _log.info("desktop.scenario.set: using built-in fallback content for scenario=%s", scenario_id)
        return fallback
    return {}


def _scenario_exists_for_switch(scenario_id: str, *, space: str) -> bool:
    if _built_in_scenario_content(scenario_id):
        return True
    try:
        return bool(scenarios_loader.scenario_exists(scenario_id, space=space))
    except Exception:
        return False


def _scenario_exists_for_source_mode(scenario_id: str | None, *, source_mode: str) -> bool | None:
    token = str(scenario_id or "").strip()
    if not token:
        return None
    return _scenario_exists_for_switch(token, space=_scenario_loader_space(source_mode))


def _build_webspace_validation(
    *,
    source_mode: str,
    stored_home_scenario: str | None,
    effective_home_scenario: str,
    current_scenario: str | None,
) -> dict[str, Any]:
    stored_home_exists = _scenario_exists_for_source_mode(stored_home_scenario, source_mode=source_mode)
    effective_home_exists = bool(_scenario_exists_for_source_mode(effective_home_scenario, source_mode=source_mode))
    current_exists = _scenario_exists_for_source_mode(current_scenario, source_mode=source_mode)

    degraded = False
    reason = None
    recommended_action = None
    if stored_home_scenario and stored_home_exists is False and current_scenario and current_exists is False:
        degraded = True
        reason = "current_and_home_scenario_missing"
        recommended_action = "reload_or_reset"
    elif current_scenario and current_exists is False:
        degraded = True
        reason = "current_scenario_missing"
        recommended_action = "reload_or_reset"
    elif stored_home_scenario and stored_home_exists is False:
        degraded = True
        reason = "home_scenario_missing"
        recommended_action = "go_home_or_set_home"
    elif effective_home_exists is False:
        degraded = True
        reason = "effective_home_scenario_missing"
        recommended_action = "set_home_or_reset"

    return {
        "stored_home_scenario_exists": stored_home_exists,
        "home_scenario_exists": effective_home_exists,
        "current_scenario_exists": current_exists,
        "degraded": degraded,
        "validation_reason": reason,
        "recommended_action": recommended_action,
    }


def _with_webspace_validation(
    *,
    source_mode: str,
    stored_home_scenario: str | None,
    effective_home_scenario: str,
    current_scenario: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload.update(
        _build_webspace_validation(
            source_mode=source_mode,
            stored_home_scenario=stored_home_scenario,
            effective_home_scenario=effective_home_scenario,
            current_scenario=current_scenario,
        )
    )
    return payload


def _preflight_validated_scenario(
    scenario_id: str | None,
    *,
    source_mode: str,
    resolution: str,
) -> tuple[str, str, dict[str, Any]]:
    requested = str(scenario_id or "").strip() or None
    requested_exists = _scenario_exists_for_source_mode(requested, source_mode=source_mode)
    if requested and requested_exists:
        return requested, resolution, {
            "requested_scenario_id": requested,
            "resolved_scenario_id": requested,
            "requested_scenario_exists": True,
            "fallback_applied": False,
            "reason": None,
        }

    fallback = "web_desktop"
    fallback_exists = bool(_scenario_exists_for_source_mode(fallback, source_mode=source_mode))
    if requested and fallback_exists:
        return fallback, f"{resolution}_fallback", {
            "requested_scenario_id": requested,
            "resolved_scenario_id": fallback,
            "requested_scenario_exists": bool(requested_exists),
            "fallback_applied": True,
            "reason": "scenario_missing",
        }

    return str(requested or ""), resolution, {
        "requested_scenario_id": requested,
        "resolved_scenario_id": requested,
        "requested_scenario_exists": bool(requested_exists),
        "fallback_applied": False,
        "reason": "scenario_missing" if requested else "scenario_unresolved",
    }


def _scenario_loader_space(source_mode: str) -> str:
    return "dev" if str(source_mode or "").strip().lower() == "dev" else "workspace"


def _env_flag_enabled(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_flag_default_enabled(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _trim_allocator_after_yjs_rebuild() -> bool:
    if not _env_flag_default_enabled("ADAOS_WEBSPACE_REBUILD_MALLOC_TRIM"):
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


def _pointer_first_scenario_switch_enabled() -> bool:
    return _env_flag_enabled("ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH")


def _preserve_live_state_on_rebuild_enabled() -> bool:
    return _env_flag_enabled("ADAOS_WEBSPACE_REBUILD_PRESERVE_LIVE_STATE")


def _publish_live_room_during_rebuild_enabled() -> bool:
    return _env_flag_enabled("ADAOS_WEBSPACE_REBUILD_LIVE_ROOM_UPDATES")


def _refresh_live_room_after_rebuild_enabled() -> bool:
    return _env_flag_default_enabled("ADAOS_WEBSPACE_REBUILD_REFRESH_LIVE_ROOM")


def _fresh_doc_on_scenario_switch_enabled() -> bool:
    return _env_flag_default_enabled("ADAOS_WEBSPACE_SCENARIO_SWITCH_FRESH_DOC")


def _scenario_switch_inline_listing_sync_enabled() -> bool:
    if os.getenv("ADAOS_TESTING") and os.getenv("ADAOS_WEBSPACE_SCENARIO_SWITCH_INLINE_LISTING_SYNC") is None:
        return False
    return _env_flag_default_enabled("ADAOS_WEBSPACE_SCENARIO_SWITCH_INLINE_LISTING_SYNC")


def _scenario_switch_subprocess_enabled() -> bool:
    if os.getenv("ADAOS_TESTING") and os.getenv("ADAOS_WEBSPACE_SCENARIO_SWITCH_SUBPROCESS") is None:
        return False
    return _env_flag_default_enabled("ADAOS_WEBSPACE_SCENARIO_SWITCH_SUBPROCESS")


def _scenario_switch_subprocess_timeout_s() -> float:
    raw = os.getenv("ADAOS_WEBSPACE_SCENARIO_SWITCH_SUBPROCESS_TIMEOUT_S")
    try:
        value = float(str(raw or "").strip())
    except Exception:
        value = 180.0
    return max(30.0, value)


def _scenario_switch_mode() -> str:
    if _pointer_first_scenario_switch_enabled():
        return "pointer_first"
    return "pointer_only"


def _extract_scenario_sections_from_content(content: Mapping[str, Any] | None) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    payload = _coerce_dict(content or {})
    ui_section = _coerce_dict(_coerce_dict(payload.get("ui") or {}).get("application") or {})
    registry_section = _coerce_dict(payload.get("registry") or {})
    catalog_section = _coerce_dict(payload.get("catalog") or {})
    return ui_section, catalog_section, registry_section


def _read_legacy_materialized_scenario_sections(
    ui_map: Any,
    data_map: Any,
    registry_map: Any,
    scenario_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    scenarios_ui = _mapping_get(ui_map, "scenarios") or {}
    scenario_ui_entry = _read_node_scoped_scenario_entry(scenarios_ui, scenario_id)
    scenario_app_ui = _coerce_dict(scenario_ui_entry.get("application") or {})

    scenarios_data = _mapping_get(data_map, "scenarios") or {}
    scenario_entry = _read_node_scoped_scenario_entry(scenarios_data, scenario_id)
    base_catalog = _coerce_dict(scenario_entry.get("catalog") or {})

    scenario_registry_map = _mapping_get(registry_map, "scenarios") or {}
    registry_entry = _read_node_scoped_scenario_entry(scenario_registry_map, scenario_id)
    return scenario_app_ui, base_catalog, registry_entry


def _resolve_scenario_sections_in_doc(
    ydoc: Any,
    *,
    webspace_id: str,
    scenario_id: str,
    source_mode: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str, bool]:
    ui_map = ydoc.get_map("ui")
    data_map = ydoc.get_map("data")
    registry_map = ydoc.get_map("registry")

    loader_space = _scenario_loader_space(source_mode)
    content = _load_scenario_switch_content(scenario_id, space=loader_space)
    if isinstance(content, Mapping) and content:
        ui_section, catalog_section, registry_section = _extract_scenario_sections_from_content(content)
        return ui_section, catalog_section, registry_section, f"loader:{loader_space}", False

    scenario_app_ui, base_catalog, registry_entry = _read_legacy_materialized_scenario_sections(
        ui_map,
        data_map,
        registry_map,
        scenario_id,
    )
    if scenario_app_ui or base_catalog or registry_entry:
        _log.info(
            "resolver using legacy materialized scenario payload webspace=%s scenario=%s source_mode=%s",
            webspace_id,
            scenario_id,
            source_mode,
        )
        return scenario_app_ui, base_catalog, registry_entry, "legacy_yjs", True

    _log.warning(
        "resolver found no canonical or legacy scenario payload webspace=%s scenario=%s source_mode=%s",
        webspace_id,
        scenario_id,
        source_mode,
    )
    return {}, {}, {}, "missing", True


def _scenario_supports_catalog_controls(
    scenario_id: str,
    scenario_application: Mapping[str, Any] | None,
) -> bool:
    scenario_token = str(scenario_id or "").strip()
    if scenario_token == "web_desktop":
        return True
    app = _coerce_dict(scenario_application or {})
    desktop = _coerce_dict(app.get("desktop") or {})
    page_schema = _coerce_dict(desktop.get("pageSchema") or {})
    widgets = page_schema.get("widgets") or []
    if isinstance(widgets, list):
        for raw in widgets:
            if not isinstance(raw, Mapping):
                continue
            widget_type = str(raw.get("type") or "").strip()
            if widget_type == "desktop.widgets":
                return True
            data_source = _coerce_dict(raw.get("dataSource") or {})
            if (
                widget_type == "collection.grid"
                and str(data_source.get("transform") or "").strip() == "desktop.icons"
            ):
                return True
    topbar = desktop.get("topbar") or []
    if isinstance(topbar, list):
        for raw in topbar:
            if not isinstance(raw, Mapping):
                continue
            action = _coerce_dict(raw.get("action") or {})
            modal_id = str(action.get("openModal") or action.get("modalId") or "").strip()
            if modal_id in {"apps_catalog", "widgets_catalog"}:
                return True
    return False


def _collect_materialization_missing_branches(
    *,
    has_ui_application: bool,
    has_desktop_config: bool,
    has_desktop_page_schema: bool,
    has_apps_catalog_modal: bool,
    has_widgets_catalog_modal: bool,
    has_catalog_apps: bool,
    has_catalog_widgets: bool,
) -> list[str]:
    missing: list[str] = []
    if not has_ui_application:
        missing.append("ui.application")
    if not has_desktop_config:
        missing.append("ui.application.desktop")
    if not has_desktop_page_schema:
        missing.append("ui.application.desktop.pageSchema")
    if not has_apps_catalog_modal:
        missing.append("ui.application.modals.apps_catalog")
    if not has_widgets_catalog_modal:
        missing.append("ui.application.modals.widgets_catalog")
    if not has_catalog_apps:
        missing.append("data.catalog.apps")
    if not has_catalog_widgets:
        missing.append("data.catalog.widgets")
    return missing


def _derive_materialization_readiness_state(
    *,
    ready: bool,
    current_scenario: str | None,
    has_ui_application: bool,
    has_desktop_config: bool,
    has_desktop_page_schema: bool,
    has_apps_catalog_modal: bool,
    has_widgets_catalog_modal: bool,
    has_catalog_apps: bool,
    has_catalog_widgets: bool,
) -> str:
    if ready:
        return "ready"
    if has_desktop_page_schema and has_catalog_apps and has_catalog_widgets:
        return "interactive"
    if has_desktop_page_schema and (
        has_catalog_apps or has_catalog_widgets or has_apps_catalog_modal or has_widgets_catalog_modal
    ):
        return "hydrating"
    if has_desktop_page_schema:
        return "first_paint"
    if current_scenario or has_ui_application or has_desktop_config:
        return "pending_structure"
    return "degraded"


def _collect_compatibility_cache_required_branches(current_scenario: str | None) -> list[str]:
    scenario_id = str(current_scenario or "").strip()
    if not scenario_id:
        return []
    node_id = _local_node_id()
    return [
        f"ui.scenarios.{node_id}.{scenario_id}.application",
        f"registry.scenarios.{node_id}.{scenario_id}",
        f"data.scenarios.{node_id}.{scenario_id}.catalog",
    ]


def _describe_compatibility_caches(
    *,
    current_scenario: str | None,
    has_scenario_ui_application: bool,
    has_scenario_registry_entry: bool,
    has_scenario_catalog: bool,
    effective_ready: bool,
    rebuild_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    required_branches = _collect_compatibility_cache_required_branches(current_scenario)
    present_flags = (
        has_scenario_ui_application,
        has_scenario_registry_entry,
        has_scenario_catalog,
    )
    present_branches = [path for path, present in zip(required_branches, present_flags) if present]
    missing_branches = [path for path, present in zip(required_branches, present_flags) if not present]
    resolver = (
        rebuild_state.get("resolver")
        if isinstance(rebuild_state, Mapping) and isinstance(rebuild_state.get("resolver"), Mapping)
        else {}
    )
    legacy_fallback_active = bool(resolver.get("legacy_fallback"))
    switch_writes_enabled = False
    runtime_removal_blockers: list[str] = []
    if not str(current_scenario or "").strip():
        runtime_removal_blockers.append("current_scenario_missing")
    if not effective_ready:
        runtime_removal_blockers.append("effective_materialization_not_ready")
    if legacy_fallback_active:
        runtime_removal_blockers.append("resolver_legacy_fallback_active")
    return {
        "current_scenario": str(current_scenario or "").strip() or None,
        "required_branches": required_branches,
        "present_branches": present_branches,
        "missing_branches": missing_branches,
        "present_count": len(present_branches),
        "required_count": len(required_branches),
        "present": bool(present_branches),
        "complete": bool(required_branches) and not missing_branches,
        "client_fallback_readable": bool(str(current_scenario or "").strip() and has_scenario_ui_application),
        "switch_writes_enabled": switch_writes_enabled,
        "legacy_fallback_active": legacy_fallback_active,
        "runtime_removal_ready": not runtime_removal_blockers,
        "runtime_removal_blockers": runtime_removal_blockers,
    }


def _copy_materialization_snapshot(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return json.loads(json.dumps(dict(value)))
    except Exception:
        return dict(value)


def _build_materialization_snapshot(
    *,
    webspace_id: str,
    current_scenario: str | None,
    has_ui_application: bool,
    has_desktop_config: bool,
    has_desktop_page_schema: bool,
    has_apps_catalog_modal: bool,
    has_widgets_catalog_modal: bool,
    has_catalog_apps: bool,
    has_catalog_widgets: bool,
    has_scenario_ui_application: bool,
    has_scenario_registry_entry: bool,
    has_scenario_catalog: bool,
    catalog_apps_count: int,
    catalog_widgets_count: int,
    topbar_count: int,
    page_widget_count: int,
    rebuild_state: Mapping[str, Any] | None = None,
    snapshot_source: str,
    stale: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    missing_branches = _collect_materialization_missing_branches(
        has_ui_application=has_ui_application,
        has_desktop_config=has_desktop_config,
        has_desktop_page_schema=has_desktop_page_schema,
        has_apps_catalog_modal=has_apps_catalog_modal,
        has_widgets_catalog_modal=has_widgets_catalog_modal,
        has_catalog_apps=has_catalog_apps,
        has_catalog_widgets=has_catalog_widgets,
    )
    ready = not missing_branches
    readiness_state = _derive_materialization_readiness_state(
        ready=ready,
        current_scenario=current_scenario,
        has_ui_application=has_ui_application,
        has_desktop_config=has_desktop_config,
        has_desktop_page_schema=has_desktop_page_schema,
        has_apps_catalog_modal=has_apps_catalog_modal,
        has_widgets_catalog_modal=has_widgets_catalog_modal,
        has_catalog_apps=has_catalog_apps,
        has_catalog_widgets=has_catalog_widgets,
    )
    compatibility_caches = _describe_compatibility_caches(
        current_scenario=current_scenario,
        has_scenario_ui_application=has_scenario_ui_application,
        has_scenario_registry_entry=has_scenario_registry_entry,
        has_scenario_catalog=has_scenario_catalog,
        effective_ready=ready,
        rebuild_state=rebuild_state,
    )
    snapshot = {
        "ready": ready,
        "readiness_state": readiness_state,
        "missing_branches": missing_branches,
        "compatibility_caches": compatibility_caches,
        "webspace_id": str(webspace_id or "").strip() or "default",
        "current_scenario": str(current_scenario or "").strip() or None,
        "has_ui_application": bool(has_ui_application),
        "has_desktop_config": bool(has_desktop_config),
        "has_desktop_page_schema": bool(has_desktop_page_schema),
        "has_apps_catalog_modal": bool(has_apps_catalog_modal),
        "has_widgets_catalog_modal": bool(has_widgets_catalog_modal),
        "has_catalog_apps": bool(has_catalog_apps),
        "has_catalog_widgets": bool(has_catalog_widgets),
        "catalog_counts": {
            "apps": int(catalog_apps_count or 0),
            "widgets": int(catalog_widgets_count or 0),
        },
        "topbar_count": int(topbar_count or 0),
        "page_widget_count": int(page_widget_count or 0),
        "snapshot_source": str(snapshot_source or "").strip() or "unknown",
        "observed_at": time.time(),
        "stale": bool(stale),
    }
    error_text = str(error or "").strip()
    if error_text:
        snapshot["error"] = error_text
    return snapshot


def _pending_materialization_snapshot(
    webspace_id: str,
    *,
    scenario_id: str | None,
    snapshot_source: str,
    rebuild_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _build_materialization_snapshot(
        webspace_id=webspace_id,
        current_scenario=scenario_id,
        has_ui_application=False,
        has_desktop_config=False,
        has_desktop_page_schema=False,
        has_apps_catalog_modal=False,
        has_widgets_catalog_modal=False,
        has_catalog_apps=False,
        has_catalog_widgets=False,
        has_scenario_ui_application=False,
        has_scenario_registry_entry=False,
        has_scenario_catalog=False,
        catalog_apps_count=0,
        catalog_widgets_count=0,
        topbar_count=0,
        page_widget_count=0,
        rebuild_state=rebuild_state,
        snapshot_source=snapshot_source,
        stale=True,
    )


def _set_webspace_rebuild_status(webspace_id: str, **fields: Any) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
    current.update(fields)
    current["webspace_id"] = target
    current["updated_at"] = time.time()
    _WEBSPACE_REBUILD_STATUS[target] = current
    return dict(current)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _record_timing(timings: Dict[str, float], key: str, started_at: float) -> float:
    value = _elapsed_ms(started_at)
    timings[str(key or "").strip() or "unknown"] = value
    return value


def _copy_timing_map(value: Any) -> Dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out: Dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            out[key] = round(float(raw_value), 3)
        except Exception:
            continue
    return out or None


def _sum_timing_values(timings: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(timings, Mapping):
        return None
    total = 0.0
    seen = False
    for key in keys:
        try:
            value = timings.get(key)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except Exception:
            continue
    if not seen:
        return None
    return round(total, 3)


def _derive_phase_timings(
    *,
    switch_timings_ms: Mapping[str, Any] | None = None,
    rebuild_timings_ms: Mapping[str, Any] | None = None,
    semantic_rebuild_timings_ms: Mapping[str, Any] | None = None,
    switch_mode: str | None = None,
) -> Dict[str, float] | None:
    phase: Dict[str, float] = {}

    switch_total = None
    if isinstance(switch_timings_ms, Mapping):
        try:
            switch_total = float(switch_timings_ms.get("total")) if switch_timings_ms.get("total") is not None else None
        except Exception:
            switch_total = None
    rebuild_total = None
    if isinstance(rebuild_timings_ms, Mapping):
        try:
            rebuild_total = float(rebuild_timings_ms.get("total")) if rebuild_timings_ms.get("total") is not None else None
        except Exception:
            rebuild_total = None

    if switch_total is not None:
        phase["time_to_accept"] = round(switch_total, 3)

    pointer_update = _sum_timing_values(switch_timings_ms, "describe_state_before", "resolve_manifest_policy", "validate_scenario", "write_switch_pointer")
    if pointer_update is not None:
        phase["time_to_pointer_update"] = pointer_update

    rebuild_before_semantic = _sum_timing_values(
        rebuild_timings_ms,
        "resolve_rebuild_target",
        "reseed_pointer",
        "invalidate_loader_cache",
        "reset_runtime_state",
        "seed_from_scenario",
        "sync_listing",
        "projection_refresh",
    )
    semantic_time_to_first_structure = _sum_timing_values(
        semantic_rebuild_timings_ms,
        "collect_inputs",
        "resolve",
        "apply_structure",
    )
    semantic_time_to_interactive = _sum_timing_values(
        semantic_rebuild_timings_ms,
        "collect_inputs",
        "resolve",
        "apply_structure",
        "apply_interactive",
    )
    semantic_total = None
    if isinstance(semantic_rebuild_timings_ms, Mapping):
        try:
            raw_semantic_total = semantic_rebuild_timings_ms.get("total")
            semantic_total = round(float(raw_semantic_total), 3) if raw_semantic_total is not None else None
        except Exception:
            semantic_total = None

    baseline = 0.0
    if switch_total is not None:
        baseline += switch_total
    if rebuild_before_semantic is not None:
        baseline += rebuild_before_semantic

    if semantic_time_to_first_structure is not None:
        phase["time_to_first_structure"] = round(baseline + semantic_time_to_first_structure, 3)
    if semantic_time_to_interactive is not None:
        phase["time_to_interactive_focus"] = round(baseline + semantic_time_to_interactive, 3)
    if "time_to_first_structure" not in phase and switch_total is not None and rebuild_total is not None:
        full_ready = round(switch_total + rebuild_total, 3)
        phase["time_to_first_structure"] = full_ready
        phase["time_to_interactive_focus"] = full_ready

    if semantic_total is not None:
        phase["time_to_full_hydration"] = round(baseline + semantic_total, 3)
    elif switch_total is not None and rebuild_total is not None:
        phase["time_to_full_hydration"] = round(switch_total + rebuild_total, 3)
    elif rebuild_total is not None:
        phase["time_to_full_hydration"] = round(rebuild_total, 3)

    return phase or None


def _finalize_timing_map(timings: Dict[str, float], *, started_at: float) -> Dict[str, float]:
    finalized = dict(timings)
    finalized["total"] = _elapsed_ms(started_at)
    return finalized


def _set_webspace_rebuild_status_if_current(webspace_id: str, request_id: str | None, **fields: Any) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    request_token = str(request_id or "").strip()
    if request_token:
        current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
        current_request = str(current.get("request_id") or "").strip()
        if current_request and current_request != request_token:
            return current
    if request_token and "request_id" not in fields:
        fields["request_id"] = request_token
    return _set_webspace_rebuild_status(target, **fields)


class _StaleRebuildRequestError(RuntimeError):
    def __init__(self, webspace_id: str, expected_request_id: str, current_request_id: str | None) -> None:
        self.webspace_id = str(webspace_id or "").strip()
        self.expected_request_id = str(expected_request_id or "").strip()
        self.current_request_id = str(current_request_id or "").strip() or None
        super().__init__(
            f"stale rebuild request superseded for webspace={self.webspace_id}: "
            f"expected={self.expected_request_id} current={self.current_request_id or '-'}"
        )


def _raise_if_rebuild_request_superseded(webspace_id: str, request_id: str | None) -> None:
    request_token = str(request_id or "").strip()
    if not request_token:
        return
    current_request = str(describe_webspace_rebuild_state(webspace_id).get("request_id") or "").strip()
    if current_request and current_request != request_token:
        raise _StaleRebuildRequestError(webspace_id, request_token, current_request)


def describe_webspace_rebuild_state(webspace_id: str) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
    if not current:
        return {
            "webspace_id": target,
            "status": "idle",
            "pending": False,
            "background": False,
            "updated_at": None,
        }
    return {
        "webspace_id": target,
        "status": str(current.get("status") or "idle"),
        "pending": bool(current.get("pending")),
        "background": bool(current.get("background")),
        "action": str(current.get("action") or "") or None,
        "request_id": str(current.get("request_id") or "") or None,
        "source_of_truth": str(current.get("source_of_truth") or "") or None,
        "scenario_id": str(current.get("scenario_id") or "") or None,
        "scenario_resolution": str(current.get("scenario_resolution") or "") or None,
        "switch_mode": str(current.get("switch_mode") or "") or None,
        "requested_at": current.get("requested_at"),
        "started_at": current.get("started_at"),
        "finished_at": current.get("finished_at"),
        "updated_at": current.get("updated_at"),
        "projection_refresh": dict(current.get("projection_refresh") or {})
        if isinstance(current.get("projection_refresh"), Mapping)
        else None,
        "registry_summary": dict(current.get("registry_summary") or {})
        if isinstance(current.get("registry_summary"), Mapping)
        else None,
        "resolver": dict(current.get("resolver") or {})
        if isinstance(current.get("resolver"), Mapping)
        else None,
        "apply_summary": dict(current.get("apply_summary") or {})
        if isinstance(current.get("apply_summary"), Mapping)
        else None,
        "timings_ms": _copy_timing_map(current.get("timings_ms")),
        "switch_timings_ms": _copy_timing_map(current.get("switch_timings_ms")),
        "semantic_rebuild_timings_ms": _copy_timing_map(current.get("semantic_rebuild_timings_ms")),
        "ydoc_timings_ms": _copy_timing_map(current.get("ydoc_timings_ms")),
        "phase_timings_ms": _copy_timing_map(current.get("phase_timings_ms")),
        "materialization": _copy_materialization_snapshot(current.get("materialization")),
        "recovery_fingerprint": str(current.get("recovery_fingerprint") or "") or None,
        "recovery_duplicate_total": int(current.get("recovery_duplicate_total") or 0),
        "recovery_last_duplicate_at": current.get("recovery_last_duplicate_at"),
        "recovery_last_duplicate_reason": str(current.get("recovery_last_duplicate_reason") or "") or None,
        "recovery_last_duplicate_age_s": current.get("recovery_last_duplicate_age_s"),
        "recovery_last_command_client": str(current.get("recovery_last_command_client") or "") or None,
        "recovery_last_command_id": str(current.get("recovery_last_command_id") or "") or None,
        "recovery_last_command_seq": int(current.get("recovery_last_command_seq") or 0),
        "error": str(current.get("error") or "") or None,
    }


class WebspaceScenarioRuntime:
    """
    Core runtime responsible for computing and applying the effective UI
    (application + catalog + registry + installed) for a given webspace.

    It reads:
      - ui.current_scenario,
      - scenario content from loader-backed canonical sources,
      - legacy Yjs scenario materialization as fallback only,
      - skill webui.json declarations (apps/widgets/registry/contributions),
      - persistent webspace desktop overlay,
    and writes:
      - ui.application,
      - data.catalog,
      - data.installed,
      - data.desktop,
      - data.webio,
      - registry.merged.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()
        # Cached snapshot of desktop scenarios discovered on disk.
        self._desktop_scenarios: Optional[List[Tuple[str, str]]] = None
        self._last_rebuild_timings_ms: Dict[str, float] | None = None
        self._last_rebuild_ydoc_timings_ms: Dict[str, float] | None = None
        self._last_resolver_debug: Dict[str, Any] | None = None
        self._last_apply_summary: Dict[str, Any] | None = None
        self._last_apply_phase_timings_ms: Dict[str, float] | None = None

    # --- scenario helpers -------------------------------------------------

    def _list_desktop_scenarios(self, space: str) -> List[Tuple[str, str]]:
        """
        Discover scenarios with ``type: desktop`` under the workspace
        scenarios directory. Returns a list of ``(scenario_id, title)``
        tuples. The ``web_desktop`` scenario itself is excluded so that it
        does not create a recursive launcher icon.

        ``space`` controls which manifest metadata is preferred:
          - ``workspace`` ¢?" use workspace manifests only,
          - ``dev``       ¢?" prefer dev manifests, fallback to workspace.
        """
        entries: List[Tuple[str, str]] = []
        try:
            root = self.ctx.paths.scenarios_dir()
            now = time.monotonic()
            cache_key = f"{space}:{root}"
            cached = _DESKTOP_SCENARIOS_CACHE.get(cache_key)
            if cached is not None and now - float(cached[0]) <= _DESKTOP_SCENARIOS_CACHE_TTL_S:
                return list(cached[2])
            children = [child for child in root.iterdir() if child.is_dir()]
            stamp = tuple(
                sorted(
                    (
                        str(child),
                        int((child / "scenario.yaml").stat().st_mtime_ns if (child / "scenario.yaml").exists() else 0),
                        int((child / "scenario.json").stat().st_mtime_ns if (child / "scenario.json").exists() else 0),
                    )
                    for child in children
                )
            )
            if cached is not None and cached[1] == stamp:
                _DESKTOP_SCENARIOS_CACHE[cache_key] = (now, stamp, list(cached[2]))
                return list(cached[2])
            for child in children:
                scenario_id = child.name
                if scenario_id == "web_desktop":
                    continue
                if space == "dev":
                    manifest = scenarios_loader.read_manifest(scenario_id, space="dev")
                    if not isinstance(manifest, dict) or not manifest:
                        manifest = scenarios_loader.read_manifest(scenario_id, space="workspace")
                else:
                    manifest = scenarios_loader.read_manifest(scenario_id, space="workspace")
                if not isinstance(manifest, dict) or not manifest:
                    continue
                if manifest.get("type") != "desktop":
                    continue
                title = str(manifest.get("title") or manifest.get("name") or scenario_id)
                entries.append((scenario_id, title))
            _DESKTOP_SCENARIOS_CACHE[cache_key] = (now, stamp, list(entries))
        except Exception:
            _log.debug("failed to list desktop scenarios", exc_info=True)
        return entries

    # --- helpers ---------------------------------------------------------

    def _load_webui(self, skill_name: str, space: str) -> Dict[str, Any]:
        paths = self.ctx.paths
        base = paths.dev_skills_dir() if space == "dev" else paths.skills_dir()
        skill_dir = Path(base) / skill_name
        path = skill_dir / "webui.json"
        manifest_path = skill_dir / "skill.yaml"
        if not path.exists():
            try:
                repo_root_attr = getattr(paths, "repo_root", None)
                repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
                if repo_root:
                    fallback_dir = (
                        Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills" / skill_name
                    )
                    fallback = fallback_dir / "webui.json"
                    if fallback.exists():
                        path = fallback
                        manifest_path = fallback_dir / "skill.yaml"
            except Exception:
                pass
        if not path.exists():
            _WEBUI_DECL_CACHE.pop(str(path), None)
            if _log.isEnabledFor(logging.DEBUG):
                stack = " <- ".join(
                    f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
                    for frame in traceback.extract_stack(limit=8)[:-1]
                )
                _log.debug("webui.json missing for %s (%s) caller=%s", skill_name, space, stack)
            return {}
        cache_key = str(path.resolve())
        try:
            stat = path.stat()
            stamp_parts: list[Any] = [cache_key, int(stat.st_mtime_ns), int(stat.st_size)]
            if manifest_path.exists():
                manifest_stat = manifest_path.stat()
                stamp_parts.extend(
                    [
                        str(manifest_path.resolve()),
                        int(manifest_stat.st_mtime_ns),
                        int(manifest_stat.st_size),
                    ]
                )
            stamp = tuple(stamp_parts)
        except Exception:
            stamp = None
        if stamp is not None:
            cached = _WEBUI_DECL_CACHE.get(cache_key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            # Accept UTF-8 with BOM produced by some Windows/PowerShell editors.
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _log.warning("failed to read webui.json for %s: %s", skill_name, exc)
            if stamp is not None:
                _WEBUI_DECL_CACHE[cache_key] = (stamp, {})
            return {}
        if not isinstance(raw, dict):
            _log.warning("webui.json must be an object for %s", skill_name)
            if stamp is not None:
                _WEBUI_DECL_CACHE[cache_key] = (stamp, {})
            return {}

        catalog = raw.get("catalog") or {}
        apps = raw.get("apps") or catalog.get("apps") or []
        widgets = raw.get("widgets") or catalog.get("widgets") or []
        registry = raw.get("registry") or {}
        reg_modals_raw = registry.get("modals") or {}
        reg_widgets_raw = registry.get("widgets") or {}
        ydoc_defaults = raw.get("ydoc_defaults") or {}
        raw_contrib = raw.get("contributions") or []
        contributions = [c for c in raw_contrib if isinstance(c, dict)]
        webio_raw = raw.get("webio") or {}
        webio_receivers_raw = webio_raw.get("receivers") if isinstance(webio_raw, dict) else {}
        ui_owner = "shared" if skill_name == "web_desktop_skill" else "node"
        try:
            if manifest_path.exists():
                manifest_raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                if isinstance(manifest_raw, dict):
                    owner_token = str(manifest_raw.get("webui_owner") or manifest_raw.get("ui_owner") or "").strip().lower()
                    if owner_token in {"shared", "node"}:
                        ui_owner = owner_token
        except Exception:
            _log.debug("failed to read skill manifest ownership for %s", skill_name, exc_info=True)

        payload = {
            "skill": skill_name,
            "space": space,
            "node_id": _local_node_id(),
            "ui_owner": ui_owner,
            "apps": [_apply_webui_load_hint(it) for it in apps if isinstance(it, dict)],
            "widgets": [_apply_webui_load_hint(it) for it in widgets if isinstance(it, dict)],
            "registry": {
                "modals": (
                    {str(k): _normalize_webui_modal_def(v) for k, v in reg_modals_raw.items()}
                    if isinstance(reg_modals_raw, dict)
                    else [str(x) for x in reg_modals_raw if isinstance(x, (str, int))]
                ),
                "widgets": (
                    {str(k): _apply_webui_load_hint(v) for k, v in reg_widgets_raw.items()}
                    if isinstance(reg_widgets_raw, dict)
                    else [str(x) for x in reg_widgets_raw if isinstance(x, (str, int))]
                ),
            },
            "ydoc_defaults": ydoc_defaults if isinstance(ydoc_defaults, dict) else {},
            "contributions": contributions,
            "webio": {
                "receivers": (
                    {str(k): _normalize_webio_receiver(v) for k, v in webio_receivers_raw.items() if str(k).strip()}
                    if isinstance(webio_receivers_raw, dict)
                    else {}
                ),
            },
        }
        if stamp is not None:
            _WEBUI_DECL_CACHE[cache_key] = (stamp, payload)
        return payload

    def _collect_skill_decls(self, mode: str = "mixed", *, include_remote: bool = True) -> List[Dict[str, Any]]:
        try:
            cap = get_local_capacity()
            skills = cap.get("skills") or []
        except Exception:
            skills = []
        if not isinstance(skills, list):
            skills = []

        decls: List[Dict[str, Any]] = []
        for rec in skills:
            if not isinstance(rec, dict) or not rec.get("active", True):
                continue
            name = rec.get("name") or rec.get("id")
            if not name:
                continue
            skill_name = str(name)

            if mode == "workspace":
                # Workspace mode: always use default webui.json regardless of
                # dev flag so that skills remain visible even when a dev
                # variant exists.
                decl = self._load_webui(skill_name, "default")
                if decl:
                    decls.append(decl)
                continue

            if mode == "dev":
                # Dev mode: include all active skills but prefer dev webui.json
                # when present, falling back to workspace webui.json.
                decl = self._load_webui(skill_name, "dev")
                if not decl:
                    decl = self._load_webui(skill_name, "default")
                if decl:
                    decls.append(decl)
                continue

            # Mixed mode: include both dev and default variants as-is.
            space = "dev" if rec.get("dev") else "default"
            decl = self._load_webui(skill_name, space)
            if decl:
                decls.append(decl)

        # Always ensure desktop skill's own webui.json is loaded so that
        # base desktop modals remain available even if not listed in capacity.
        try:
            desktop_decl = self._load_webui("web_desktop_skill", "default")
        except Exception:
            desktop_decl = {}
        if isinstance(desktop_decl, dict) and desktop_decl:
            decls.append(desktop_decl)

        if include_remote and mode != "dev":
            decls.extend(self._collect_remote_skill_decls())

        return decls

    def _collect_remote_skill_decls(self) -> List[Dict[str, Any]]:
        try:
            conf = load_config()
        except Exception:
            conf = None
        if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
            return []
        try:
            from adaos.services.registry.subnet_directory import get_directory

            nodes = get_directory().list_known_nodes()
        except Exception:
            nodes = []
        local_node_id = _local_node_id()
        decls: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            node_id = str(node.get("node_id") or "").strip()
            if not node_id or node_id == local_node_id:
                continue
            runtime_projection = (
                node.get("runtime_projection")
                if isinstance(node.get("runtime_projection"), Mapping)
                else {}
            )
            snapshot = (
                runtime_projection.get("snapshot")
                if isinstance(runtime_projection.get("snapshot"), Mapping)
                else {}
            )
            catalog = (
                snapshot.get("desktop_catalog")
                if isinstance(snapshot.get("desktop_catalog"), Mapping)
                else {}
            )
            apps = catalog.get("apps") if isinstance(catalog.get("apps"), list) else []
            widgets = catalog.get("widgets") if isinstance(catalog.get("widgets"), list) else []
            registry = catalog.get("registry") if isinstance(catalog.get("registry"), Mapping) else {}
            webio = catalog.get("webio") if isinstance(catalog.get("webio"), Mapping) else {}
            ydoc_defaults = catalog.get("ydoc_defaults") if isinstance(catalog.get("ydoc_defaults"), Mapping) else {}
            if not apps and not widgets and not registry and not webio and not ydoc_defaults:
                continue
            display = node_display_from_directory_node(node)
            modal_id_map = _node_scoped_modal_ids(registry, node_id=node_id)
            decl: Dict[str, Any] = {
                "skill": f"subnet.member.{node_id}",
                "space": "default",
                "node_id": node_id,
                "apps": [],
                "widgets": [],
                "registry": {"modals": {}, "widgets": {}},
                "webio": {"receivers": {}},
                "ydoc_defaults": {},
                "contributions": [],
            }
            mod_spec = registry.get("modals") if isinstance(registry.get("modals"), Mapping) else {}
            if isinstance(mod_spec, Mapping):
                for key, value in mod_spec.items():
                    token = str(key or "").strip()
                    if not token:
                        continue
                    scoped_token = modal_id_map.get(token, _node_scoped_catalog_id(node_id, token))
                    decl["registry"]["modals"][scoped_token] = _mark_modal_def(
                        _apply_node_context_to_ui(
                            value,
                            display,
                            node_id=node_id,
                            modal_id_map=modal_id_map,
                        ),
                        source=f"skill:subnet.member.{node_id}",
                        skill=f"subnet.member.{node_id}",
                        dev=False,
                    )
            wid_spec = registry.get("widgets") if isinstance(registry.get("widgets"), Mapping) else {}
            if isinstance(wid_spec, Mapping):
                for key, value in wid_spec.items():
                    token = str(key or "").strip()
                    if not token:
                        continue
                    scoped_token = _node_scoped_catalog_id(node_id, token)
                    decl["registry"]["widgets"][scoped_token] = _apply_node_context_to_ui(
                        value,
                        display,
                        node_id=node_id,
                        modal_id_map=modal_id_map,
                    )
            webio_receivers = webio.get("receivers") if isinstance(webio.get("receivers"), Mapping) else {}
            if isinstance(webio_receivers, Mapping):
                for key, value in webio_receivers.items():
                    token = str(key or "").strip()
                    if token:
                        decl["webio"]["receivers"][token] = _normalize_webio_receiver(value)
            for path, value in ydoc_defaults.items():
                token = str(path or "").strip()
                if token:
                    scoped_node_id = _node_scoped_data_path_node_id(token)
                    if scoped_node_id and scoped_node_id != node_id:
                        continue
                    decl["ydoc_defaults"][node_scope_data_path(token, node_id)] = _clone_json_like(value)
            for item in apps:
                if not isinstance(item, dict):
                    continue
                if _catalog_entry_is_foreign_relay(item, node_id=node_id):
                    continue
                entry = _scope_remote_catalog_entry_id(
                    _apply_node_context_to_ui(item, display, node_id=node_id, modal_id_map=modal_id_map),
                    node_id=node_id,
                )
                decl["apps"].append(entry)
                app_id = str(entry.get("id") or "").strip()
                if app_id:
                    decl["contributions"].append(
                        {
                            "extensionPoint": "desktop.apps",
                            "type": "app",
                            "id": app_id,
                            "autoInstall": True,
                        }
                    )
            for item in widgets:
                if not isinstance(item, dict):
                    continue
                if _catalog_entry_is_foreign_relay(item, node_id=node_id):
                    continue
                entry = _scope_remote_catalog_entry_id(
                    _apply_node_context_to_ui(item, display, node_id=node_id, modal_id_map=modal_id_map),
                    node_id=node_id,
                )
                decl["widgets"].append(entry)
                widget_id = str(entry.get("id") or "").strip()
                if widget_id:
                    decl["contributions"].append(
                        {
                            "extensionPoint": "desktop.widgets",
                            "type": "widget",
                            "id": widget_id,
                            "autoInstall": True,
                        }
                    )
            if (
                decl["apps"]
                or decl["widgets"]
                or decl["registry"]["modals"]
                or decl["registry"]["widgets"]
                or decl["webio"]["receivers"]
                or decl["ydoc_defaults"]
            ):
                decls.append(decl)
        return decls

    def _apply_ydoc_defaults_in_txn(self, ydoc: Y.YDoc, txn: Any, decls: List[Dict[str, Any]]) -> None:  # type: ignore[override]
        spec: Dict[str, Any] = {}
        for decl in decls:
            raw = decl.get("ydoc_defaults") or {}
            if not isinstance(raw, dict):
                continue
            skill_name = str(decl.get("skill") or "").strip()
            node_id = str(decl.get("node_id") or "").strip()
            for path, default in raw.items():
                if not isinstance(path, str):
                    continue
                if _decl_is_node_owned(decl) and node_id:
                    path = node_scope_data_path(path, node_id)
                # Preserve first writer semantics for conflicting defaults.
                spec.setdefault(path, default)

        current_top_cache: Dict[tuple[str, str], Any] = {}
        missing = object()
        for path, default in spec.items():
            segments = [s for s in path.split("/") if s]
            if len(segments) < 2:
                continue
            root_name, key = segments[0], segments[1]
            root = ydoc.get_map(root_name)
            cache_key = (root_name, key)
            if len(segments) == 2:
                current_top = current_top_cache.get(cache_key, missing)
                if current_top is missing:
                    current_top = root.get(key)
                    current_top_cache[cache_key] = current_top
                if current_top is not None:
                    continue
                try:
                    value = json.loads(json.dumps(default))
                except Exception:
                    value = default
                root.set(txn, key, value)
                current_top_cache[cache_key] = value
                continue
            current_top = current_top_cache.get(cache_key, missing)
            if current_top is missing:
                current_top = root.get(key)
                current_top_cache[cache_key] = current_top
            tail = segments[2:]
            if _nested_json_path_exists(current_top, tail):
                continue
            try:
                value = json.loads(json.dumps(default))
            except Exception:
                value = default
            changed, merged = _merge_nested_json_path(current_top, tail, value)
            if changed:
                root.set(txn, key, merged)
                current_top_cache[cache_key] = merged

    def _collect_resolver_inputs_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebspaceResolverInputs:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")

        scenario_id = str(ui_map.get("current_scenario") or "web_desktop").strip() or "web_desktop"
        scenarios_ui = _mapping_get(ui_map, "scenarios") or {}
        scenario_ui_entry = _read_node_scoped_scenario_entry(scenarios_ui, scenario_id)
        scenario_ui_application = _coerce_dict(scenario_ui_entry.get("application") or {})
        scenario_registry_map = _mapping_get(registry_map, "scenarios") or {}
        scenario_registry_entry = _read_node_scoped_scenario_entry(scenario_registry_map, scenario_id)
        scenario_data_map = _mapping_get(data_map, "scenarios") or {}
        scenario_data_entry = _read_node_scoped_scenario_entry(scenario_data_map, scenario_id)
        scenario_catalog = _coerce_dict(scenario_data_entry.get("catalog") or {})

        mode = "mixed"
        metadata: Dict[str, Any] = {}
        overlay_snapshot: Dict[str, Any] = {}
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                mode = row.effective_source_mode
                metadata = {
                    "title": row.title,
                    "kind": row.effective_kind,
                    "source_mode": row.effective_source_mode,
                    "home_scenario": row.effective_home_scenario,
                    "is_dev": row.is_dev,
                }
                if getattr(row, "has_ui_overlay", False):
                    overlay_snapshot = {
                        "installed": _coerce_dict(getattr(row, "installed_overlay", {}) or {}),
                        "pinnedWidgets": _normalize_overlay_widget_entries(
                            getattr(row, "pinned_widgets_overlay", []) or []
                        ),
                        "topbar": list(getattr(row, "topbar_overlay", []) or []),
                        "pageSchema": _coerce_dict(getattr(row, "page_schema_overlay", {}) or {}),
                        "iconOrder": list(getattr(row, "icon_order_overlay", []) or []),
                        "widgetOrder": list(getattr(row, "widget_order_overlay", []) or []),
                        "hiddenSections": list(getattr(row, "hidden_sections_overlay", []) or []),
                        "source": "workspace_manifest_overlay",
                    }
        except Exception:
            mode = "mixed"
            metadata = {}

        scenario_app_ui, base_catalog, registry_entry, scenario_source, legacy_fallback = _resolve_scenario_sections_in_doc(
            ydoc,
            webspace_id=webspace_id,
            scenario_id=scenario_id,
            source_mode=mode,
        )
        if metadata:
            metadata = dict(metadata)
        metadata["scenario_source"] = scenario_source
        metadata["legacy_scenario_fallback"] = legacy_fallback

        preserve_live_state = _preserve_live_state_on_rebuild_enabled()
        if preserve_live_state:
            live_application = _coerce_live_branch_subset(
                _mapping_get(ui_map, "application") or {},
                ("modals",),
            )
            live_catalog = _coerce_live_branch_subset(
                _mapping_get(data_map, "catalog") or {},
                ("apps", "widgets"),
            )
            live_registry = _coerce_live_branch_subset(
                _mapping_get(registry_map, "merged") or {},
                ("modals", "widgets"),
            )
            live_desktop = _coerce_live_branch_subset(
                _mapping_get(data_map, "desktop") or {},
                ("installed", "topbar", "pageSchema", "pinnedWidgets", "iconOrder", "widgetOrder", "hiddenSections"),
            )
            live_routing = _coerce_live_branch_subset(
                _mapping_get(data_map, "routing") or {},
                ("routes",),
            )
        else:
            live_application = {}
            live_catalog = {}
            live_registry = {}
            live_desktop = {}
            live_routing = {}

        return WebspaceResolverInputs(
            webspace_id=webspace_id,
            scenario_id=str(scenario_id),
            source_mode=mode,
            metadata=metadata,
            scenario_application=scenario_app_ui,
            scenario_catalog=base_catalog,
            scenario_registry=registry_entry,
            overlay_snapshot=overlay_snapshot,
            live_state={
                "application": live_application,
                "catalog": live_catalog,
                "registry": live_registry,
                "desktop": live_desktop,
                "routing": live_routing,
            },
            compatibility_cache_presence={
                "scenario_ui_application": bool(scenario_ui_application),
                "scenario_registry_entry": bool(scenario_registry_entry),
                "scenario_catalog": bool(scenario_catalog),
            },
            skill_decls=self._collect_skill_decls(mode=mode),
            desktop_scenarios=self._list_desktop_scenarios(space=mode),
            scenario_source=scenario_source,
            legacy_scenario_fallback=legacy_fallback,
        )

    def resolve_webspace(self, inputs: WebspaceResolverInputs) -> WebspaceResolverOutputs:
        cache_keys = _resolver_cache_keys(inputs)
        resolver_fingerprint = _resolver_input_fingerprint(inputs, cache_keys=cache_keys)
        resolver_debug = {
            "source": str(inputs.scenario_source or ""),
            "legacy_fallback": bool(inputs.legacy_scenario_fallback),
            "cache_keys": dict(cache_keys),
            "input_fingerprint": resolver_fingerprint,
            "cache_hit": False,
        }
        cached = _get_cached_resolved_outputs(resolver_fingerprint)
        if cached is not None:
            resolver_debug["cache_hit"] = True
            self._last_resolver_debug = resolver_debug
            return cached

        scenario_id = str(inputs.scenario_id or "").strip() or "web_desktop"
        source_mode = str(inputs.source_mode or "").strip() or "mixed"
        scenario_application = _coerce_dict(inputs.scenario_application or {})
        scenario_desktop = _coerce_dict(scenario_application.get("desktop") or {})
        scenario_catalog = _coerce_dict(inputs.scenario_catalog or {})
        scenario_registry = _coerce_dict(inputs.scenario_registry or {})
        scenario_apps = [it for it in (scenario_catalog.get("apps") or []) if isinstance(it, Mapping)]
        scenario_widgets = [it for it in (scenario_catalog.get("widgets") or []) if isinstance(it, Mapping)]
        base_registry_modals = [str(x) for x in (scenario_registry.get("modals") or [])]
        base_registry_widgets = [str(x) for x in (scenario_registry.get("widgets") or [])]

        skill_decls = list(inputs.skill_decls or [])
        skill_apps: List[Dict[str, Any]] = []
        skill_widgets: List[Dict[str, Any]] = []
        skill_registry_modals: List[List[str]] = []
        skill_registry_widgets: List[List[str]] = []
        auto_widget_ids: set[str] = set()
        auto_app_ids: set[str] = set()
        active_remote_node_ids: set[str] = set()
        local_display = node_display_from_config(load_config())

        for decl in skill_decls:
            skill_name = decl.get("skill") or ""
            space = decl.get("space") or "default"
            node_id = str(decl.get("node_id") or "").strip()
            node_owned = _decl_is_node_owned(decl)
            if node_id and str(skill_name or "").strip().startswith("subnet.member."):
                active_remote_node_ids.add(node_id)
            decl_display = {
                "node_label": str(decl.get("node_label") or "").strip(),
                "node_compact_label": str(decl.get("node_compact_label") or "").strip(),
                "node_color": str(decl.get("node_color") or "").strip(),
                "node_index": decl.get("node_index"),
            }
            if not any(decl_display.values()):
                decl_display = local_display
            source = f"skill:{skill_name}"
            dev_flag = space == "dev"
            reg = decl.get("registry") or {}
            modal_id_map = _node_scoped_modal_ids(reg, node_id=node_id) if node_owned else {}
            for app in decl.get("apps") or []:
                if isinstance(app, dict):
                    entry = _mark_entry(app, source=source, dev=dev_flag)
                    if node_owned and node_id:
                        entry = _apply_node_context_to_ui(entry, decl_display, node_id=node_id, modal_id_map=modal_id_map)
                    skill_apps.append(_apply_node_display_to_entry(entry, decl_display, node_id=node_id))
            for widget in decl.get("widgets") or []:
                if isinstance(widget, dict):
                    entry = _mark_entry(widget, source=source, dev=dev_flag)
                    if node_owned and node_id:
                        entry = _apply_node_context_to_ui(entry, decl_display, node_id=node_id, modal_id_map=modal_id_map)
                    skill_widgets.append(_apply_node_display_to_entry(entry, decl_display, node_id=node_id))
            mod_spec = reg.get("modals") or {}
            if isinstance(mod_spec, dict):
                skill_registry_modals.append([modal_id_map.get(str(k), str(k)) for k in mod_spec.keys()])
            else:
                skill_registry_modals.append([str(x) for x in mod_spec])
            wid_spec = reg.get("widgets") or {}
            if isinstance(wid_spec, dict):
                skill_registry_widgets.append([
                    _node_scoped_catalog_id(node_id, str(k)) if node_owned and node_id else str(k)
                    for k in wid_spec.keys()
                ])
            else:
                skill_registry_widgets.append([str(x) for x in wid_spec])
            for contrib in decl.get("contributions") or []:
                if not isinstance(contrib, dict):
                    continue
                ep = str(contrib.get("extensionPoint") or "")
                ctype = str(contrib.get("type") or "")
                cid = str(contrib.get("id") or "")
                auto = bool(contrib.get("autoInstall"))
                if not cid or not auto:
                    continue
                if ep == "desktop.widgets" and ctype == "widget":
                    auto_widget_ids.add(cid)
                if ep == "desktop.apps" and ctype == "app":
                    auto_app_ids.add(cid)

        merged_apps = [
            _apply_node_display_to_entry(
                _mark_entry(it, source=f"scenario:{scenario_id}", dev=False),
                local_display,
                node_id=_local_node_id(),
            )
            for it in scenario_apps
        ]
        merged_widgets = [
            _apply_node_display_to_entry(
                _mark_entry(it, source=f"scenario:{scenario_id}", dev=False),
                local_display,
                node_id=_local_node_id(),
            )
            for it in scenario_widgets
        ]

        extra_apps: List[Dict[str, Any]] = []
        for sid, title in inputs.desktop_scenarios:
            if sid == scenario_id:
                continue
            app_id = f"scenario:{sid}"
            extra_apps.append(
                _apply_node_display_to_entry(
                    _mark_entry(
                        {
                            "id": app_id,
                            "title": title,
                            "icon": "apps-outline",
                            "scenario_id": sid,
                        },
                        source=f"scenario:{sid}",
                        dev=False,
                    ),
                    local_display,
                    node_id=_local_node_id(),
                )
            )
            auto_app_ids.add(app_id)

        merged_apps = _merge_by_id(merged_apps + extra_apps + skill_apps)
        merged_widgets = _merge_by_id(merged_widgets + skill_widgets)
        live_catalog = _coerce_dict((inputs.live_state or {}).get("catalog") or {})
        merged_apps = _preserve_live_remote_catalog_entries(
            merged_apps,
            current_items=live_catalog.get("apps"),
            active_remote_node_ids=active_remote_node_ids,
        )
        merged_widgets = _preserve_live_remote_catalog_entries(
            merged_widgets,
            current_items=live_catalog.get("widgets"),
            active_remote_node_ids=active_remote_node_ids,
        )
        supports_catalog_controls = _scenario_supports_catalog_controls(
            scenario_id,
            scenario_application,
        )
        default_modal_ids = ["scenario_switcher"]
        if supports_catalog_controls:
            default_modal_ids = ["apps_catalog", "widgets_catalog", *default_modal_ids]
        merged_registry = {
            "modals": _merge_registry_lists(
                base_registry_modals,
                skill_registry_modals + [default_modal_ids],
            ),
            "widgets": _merge_registry_lists(base_registry_widgets, skill_registry_widgets),
        }

        installed_current = _coerce_dict((inputs.overlay_snapshot or {}).get("installed") or {})
        overlay_has_pinned_widgets = "pinnedWidgets" in (inputs.overlay_snapshot or {})
        overlay_pinned_widgets = _normalize_overlay_widget_entries((inputs.overlay_snapshot or {}).get("pinnedWidgets"))
        overlay_icon_order = _dedupe_str_list((inputs.overlay_snapshot or {}).get("iconOrder"))
        overlay_widget_order = _dedupe_str_list((inputs.overlay_snapshot or {}).get("widgetOrder"))
        overlay_hidden_sections = _dedupe_str_list((inputs.overlay_snapshot or {}).get("hiddenSections"))
        scenario_pinned_widgets = _normalize_overlay_widget_entries(scenario_desktop.get("pinnedWidgets"))
        scenario_topbar = list(scenario_desktop.get("topbar") or []) if isinstance(scenario_desktop.get("topbar"), list) else []
        scenario_page_schema = _coerce_dict(scenario_desktop.get("pageSchema") or {})
        installed_with_auto = _merge_installed_with_auto(
            installed_current,
            auto_apps=auto_app_ids,
            auto_widgets=auto_widget_ids,
        )

        merged_modals_map: Dict[str, Any] = {}
        base_modals_map = _coerce_dict(scenario_application.get("modals") or {})
        for key, value in base_modals_map.items():
            merged_modals_map[str(key)] = value
        for decl in skill_decls:
            reg = decl.get("registry") or {}
            mod_spec = reg.get("modals") or {}
            if not isinstance(mod_spec, dict):
                continue
            skill_name = str(decl.get("skill") or "").strip()
            node_id = str(decl.get("node_id") or "").strip()
            node_owned = _decl_is_node_owned(decl)
            decl_display = {
                "node_label": str(decl.get("node_label") or "").strip(),
                "node_compact_label": str(decl.get("node_compact_label") or "").strip(),
                "node_color": str(decl.get("node_color") or "").strip(),
                "node_index": decl.get("node_index"),
            }
            if not any(decl_display.values()):
                decl_display = local_display
            modal_id_map = _node_scoped_modal_ids(reg, node_id=node_id) if node_owned else {}
            for key, value in mod_spec.items():
                raw_token = str(key)
                token = modal_id_map.get(raw_token, raw_token)
                if token and token not in merged_modals_map:
                    modal_def = (
                        _apply_node_context_to_ui(value, decl_display, node_id=node_id, modal_id_map=modal_id_map)
                        if node_owned and node_id
                        else value
                    )
                    merged_modals_map[token] = _mark_modal_def(
                        modal_def,
                        source=f"skill:{skill_name}" if skill_name else "skill:unknown",
                        skill=skill_name,
                        dev=str(decl.get("space") or "default").strip().lower() == "dev",
                    )

        if supports_catalog_controls and "apps_catalog" not in merged_modals_map:
            merged_modals_map["apps_catalog"] = {
                "title": "Available Apps",
                "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                "schema": {
                    "id": "apps_catalog",
                    "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "apps-list",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Apps",
                            "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                            "dataSource": {
                                "kind": "y",
                                "path": "data/catalog/apps",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "callHost",
                                    "target": "desktop.toggleInstall",
                                    "params": {
                                        "type": "app",
                                        "id": "$event.id",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }
        if supports_catalog_controls and "widgets_catalog" not in merged_modals_map:
            merged_modals_map["widgets_catalog"] = {
                "title": "Available Widgets",
                "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                "schema": {
                    "id": "widgets_catalog",
                    "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "widgets-list",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Widgets",
                            "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                            "dataSource": {
                                "kind": "y",
                                "path": "data/catalog/widgets",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "callHost",
                                    "target": "desktop.toggleInstall",
                                    "params": {
                                        "type": "widget",
                                        "id": "$event.id",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }

        live_application = _coerce_dict((inputs.live_state or {}).get("application") or {})
        merged_modals_map = _preserve_live_remote_modals(
            merged_modals_map,
            current_modals=live_application.get("modals"),
            active_remote_node_ids=active_remote_node_ids,
        )

        live_registry = _coerce_dict((inputs.live_state or {}).get("registry") or {})
        merged_registry["modals"] = _preserve_live_remote_registry_tokens(
            list(merged_registry.get("modals") or []),
            current_tokens=live_registry.get("modals"),
            active_remote_node_ids=active_remote_node_ids,
        )
        merged_registry["widgets"] = _preserve_live_remote_registry_tokens(
            list(merged_registry.get("widgets") or []),
            current_tokens=live_registry.get("widgets"),
            active_remote_node_ids=active_remote_node_ids,
        )

        app_with_modals: Dict[str, Any] = dict(scenario_application)
        if merged_modals_map:
            app_with_modals["modals"] = merged_modals_map
        desktop_config = _coerce_dict(app_with_modals.get("desktop") or {})
        desktop_config["topbar"] = scenario_topbar
        desktop_config["pageSchema"] = scenario_page_schema
        desktop_config["pinnedWidgets"] = (
            overlay_pinned_widgets if overlay_has_pinned_widgets else scenario_pinned_widgets
        )
        desktop_config["iconOrder"] = list(overlay_icon_order)
        desktop_config["widgetOrder"] = list(overlay_widget_order)
        desktop_config["hiddenSections"] = list(overlay_hidden_sections)
        app_with_modals["desktop"] = desktop_config

        desktop_next = _coerce_dict((inputs.live_state or {}).get("desktop") or {})
        desktop_installed = _coerce_dict(desktop_next.get("installed") or {})
        desktop_installed["apps"] = list(installed_with_auto.get("apps") or [])
        desktop_installed["widgets"] = list(installed_with_auto.get("widgets") or [])
        desktop_next["installed"] = desktop_installed
        desktop_next["topbar"] = list(desktop_config.get("topbar") or [])
        desktop_next["pageSchema"] = _coerce_dict(desktop_config.get("pageSchema") or {})
        desktop_next["pinnedWidgets"] = list(desktop_config.get("pinnedWidgets") or [])
        desktop_next["iconOrder"] = list(desktop_config.get("iconOrder") or [])
        desktop_next["widgetOrder"] = list(desktop_config.get("widgetOrder") or [])
        desktop_next["hiddenSections"] = list(desktop_config.get("hiddenSections") or [])

        webio_dict = _merge_webio_receivers(skill_decls)

        routing_dict = _coerce_dict((inputs.live_state or {}).get("routing") or {})
        routes = routing_dict.get("routes")
        routing_dict = {**routing_dict, "routes": _coerce_dict(routes)}

        resolved = WebspaceResolverOutputs(
            webspace_id=inputs.webspace_id,
            scenario_id=scenario_id,
            source_mode=source_mode,
            application=app_with_modals,
            catalog={
                "apps": [dict(it) for it in merged_apps],
                "widgets": [dict(it) for it in merged_widgets],
            },
            registry={
                "modals": list(merged_registry.get("modals") or []),
                "widgets": list(merged_registry.get("widgets") or []),
            },
            installed={
                "apps": list(installed_with_auto.get("apps") or []),
                "widgets": list(installed_with_auto.get("widgets") or []),
            },
            desktop=desktop_next,
            webio=webio_dict,
            routing=routing_dict,
            skill_decls=skill_decls,
        )
        _remember_resolved_outputs(resolver_fingerprint, resolved)
        self._last_resolver_debug = resolver_debug
        return resolved

    def _apply_resolved_state_in_doc(
        self,
        ydoc: Y.YDoc,
        webspace_id: str,
        resolved: WebspaceResolverOutputs,
        *,
        inputs: WebspaceResolverInputs | None = None,
        expected_request_id: str | None = None,
    ) -> None:
        _raise_if_rebuild_request_superseded(webspace_id, expected_request_id)
        effective_inputs = inputs or WebspaceResolverInputs(
            webspace_id=webspace_id,
            scenario_id=str(resolved.scenario_id or ""),
            source_mode=str(resolved.source_mode or ""),
        )
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")
        runtime_map = ydoc.get_map("runtime")
        runtime_environment = runtime_environment_payload()
        target_paths = _EFFECTIVE_BRANCH_PATHS
        changed_paths: List[str] = []
        diff_applied_paths: List[str] = []
        replaced_paths: List[str] = []
        failed_paths: List[str] = []
        fingerprint_unchanged_paths: List[str] = []
        stale_fingerprint_paths: List[str] = []
        defaults_failed = False
        phase_summaries: Dict[str, Dict[str, Any]] = {}
        phase_timings_ms: Dict[str, float] = {}
        compatibility_presence = dict(effective_inputs.compatibility_cache_presence or {})
        resolved_branch_fingerprints = _resolved_output_branch_fingerprints(resolved)
        persisted_branch_fingerprints = _read_effective_branch_fingerprints(registry_map)
        effective_branch_fingerprints = dict(persisted_branch_fingerprints)
        pending_fingerprint_updates: Dict[str, str] = {}
        transaction_total = 0

        def _update_materialization_snapshot(phase_name: str) -> None:
            application = _coerce_dict(resolved.application or {})
            desktop = _coerce_dict(application.get("desktop") or {})
            modals = _coerce_dict(application.get("modals") or {})
            page_schema = _coerce_dict(desktop.get("pageSchema") or {})
            topbar = desktop.get("topbar") if isinstance(desktop.get("topbar"), list) else []
            page_widgets = page_schema.get("widgets") if isinstance(page_schema.get("widgets"), list) else []
            include_catalog = phase_name != "structure"
            snapshot = _build_materialization_snapshot(
                webspace_id=webspace_id,
                current_scenario=resolved.scenario_id,
                has_ui_application=bool(application),
                has_desktop_config=bool(desktop),
                has_desktop_page_schema=bool(page_schema),
                has_apps_catalog_modal="apps_catalog" in modals,
                has_widgets_catalog_modal="widgets_catalog" in modals,
                has_catalog_apps=include_catalog and isinstance(resolved.catalog.get("apps"), list),
                has_catalog_widgets=include_catalog and isinstance(resolved.catalog.get("widgets"), list),
                has_scenario_ui_application=bool(compatibility_presence.get("scenario_ui_application")),
                has_scenario_registry_entry=bool(compatibility_presence.get("scenario_registry_entry")),
                has_scenario_catalog=bool(compatibility_presence.get("scenario_catalog")),
                catalog_apps_count=len(resolved.catalog.get("apps") or []) if include_catalog else 0,
                catalog_widgets_count=len(resolved.catalog.get("widgets") or []) if include_catalog else 0,
                topbar_count=len(topbar),
                page_widget_count=len(page_widgets),
                rebuild_state=describe_webspace_rebuild_state(webspace_id),
                snapshot_source=f"semantic_rebuild:{phase_name}",
                stale=False,
            )
            current_request_id = str(describe_webspace_rebuild_state(webspace_id).get("request_id") or "").strip() or None
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                current_request_id,
                materialization=snapshot,
            )

        def _apply_branch(
            txn: Any,
            path: str,
            y_map: Any,
            key: str,
            value: Any,
            *,
            fingerprint_updates: Dict[str, str],
            ignore_errors: bool = False,
        ) -> None:
            fingerprint = str(resolved_branch_fingerprints.get(path) or "").strip()
            if (
                fingerprint
                and str(effective_branch_fingerprints.get(path) or "").strip() == fingerprint
            ):
                try:
                    actual_fingerprint = _fingerprint_json_like(y_map.get(key))
                except Exception:
                    actual_fingerprint = ""
                if actual_fingerprint == fingerprint:
                    fingerprint_unchanged_paths.append(path)
                    fingerprint_updates[path] = fingerprint
                    pending_fingerprint_updates[path] = fingerprint
                    return
                stale_fingerprint_paths.append(path)
            try:
                if path in _WHOLE_BRANCH_REPLACE_PATHS:
                    changed, apply_mode = _replace_map_value(y_map, txn, key, value)
                else:
                    changed, apply_mode = _set_map_value_if_changed(y_map, txn, key, value)
            except Exception:
                if not ignore_errors:
                    raise
                failed_paths.append(path)
                return
            if fingerprint:
                effective_branch_fingerprints[path] = fingerprint
                fingerprint_updates[path] = fingerprint
                pending_fingerprint_updates[path] = fingerprint
            if changed:
                changed_paths.append(path)
                if apply_mode == "diff":
                    diff_applied_paths.append(path)
                else:
                    replaced_paths.append(path)

        def _apply_phase(
            name: str,
            branch_specs: tuple[tuple[str, Any, str, Any, bool], ...],
            *,
            apply_defaults: bool = False,
            flush_fingerprints: bool = False,
        ) -> None:
            nonlocal defaults_failed
            nonlocal transaction_total
            _raise_if_rebuild_request_superseded(webspace_id, expected_request_id)
            phase_started = time.perf_counter()
            phase_changed_before = len(changed_paths)
            phase_diff_before = len(diff_applied_paths)
            phase_replaced_before = len(replaced_paths)
            phase_failed_before = len(failed_paths)
            phase_fingerprint_unchanged_before = len(fingerprint_unchanged_paths)
            phase_stale_fingerprint_before = len(stale_fingerprint_paths)
            phase_defaults_failed = False

            with ydoc.begin_transaction() as txn:
                transaction_total += 1
                phase_fingerprint_updates: Dict[str, str] = {}
                if apply_defaults:
                    try:
                        self._apply_ydoc_defaults_in_txn(ydoc, txn, resolved.skill_decls)
                    except Exception:
                        defaults_failed = True
                        phase_defaults_failed = True
                        _log.warning("failed to apply ydoc_defaults for webspace=%s", webspace_id, exc_info=True)

                for path, y_map, key, value, ignore_errors in branch_specs:
                    _apply_branch(
                        txn,
                        path,
                        y_map,
                        key,
                        value,
                        fingerprint_updates=phase_fingerprint_updates,
                        ignore_errors=ignore_errors,
                    )
                if flush_fingerprints and pending_fingerprint_updates:
                    _write_effective_branch_fingerprints(
                        registry_map,
                        txn,
                        current=effective_branch_fingerprints,
                        updates=pending_fingerprint_updates,
                    )

            phase_changed_paths = list(changed_paths[phase_changed_before:])
            phase_diff_paths = list(diff_applied_paths[phase_diff_before:])
            phase_replaced_paths = list(replaced_paths[phase_replaced_before:])
            phase_failed_paths = list(failed_paths[phase_failed_before:])
            phase_fingerprint_unchanged_paths = list(fingerprint_unchanged_paths[phase_fingerprint_unchanged_before:])
            phase_stale_fingerprint_paths = list(stale_fingerprint_paths[phase_stale_fingerprint_before:])
            branch_count = len(branch_specs)
            phase_summary: Dict[str, Any] = {
                "branch_count": branch_count,
                "changed_branches": len(phase_changed_paths),
                "unchanged_branches": branch_count - len(phase_changed_paths) - len(phase_failed_paths),
                "failed_branches": len(phase_failed_paths),
                "changed_paths": phase_changed_paths,
            }
            if phase_diff_paths:
                phase_summary["diff_applied_branches"] = len(phase_diff_paths)
                phase_summary["diff_applied_paths"] = phase_diff_paths
            if phase_replaced_paths:
                phase_summary["replaced_branches"] = len(phase_replaced_paths)
                phase_summary["replaced_paths"] = phase_replaced_paths
            if phase_fingerprint_unchanged_paths:
                phase_summary["fingerprint_unchanged_branches"] = len(phase_fingerprint_unchanged_paths)
                phase_summary["fingerprint_unchanged_paths"] = phase_fingerprint_unchanged_paths
            if phase_stale_fingerprint_paths:
                phase_summary["stale_fingerprint_branches"] = len(phase_stale_fingerprint_paths)
                phase_summary["stale_fingerprint_paths"] = phase_stale_fingerprint_paths
            if phase_failed_paths:
                phase_summary["failed_paths"] = phase_failed_paths
            if phase_defaults_failed:
                phase_summary["defaults_failed"] = True
            phase_summaries[name] = phase_summary
            phase_timings_ms[f"apply_{name}"] = _elapsed_ms(phase_started)
            _update_materialization_snapshot(name)

        _apply_phase(
            "structure",
            (
                ("ui.application", ui_map, "application", resolved.application, False),
                ("registry.merged", registry_map, "merged", resolved.registry, False),
                ("runtime.environment", runtime_map, "environment", runtime_environment, False),
            ),
            apply_defaults=True,
            flush_fingerprints=False,
        )
        _apply_phase(
            "interactive",
            (
                ("data.catalog", data_map, "catalog", resolved.catalog, False),
                ("data.installed", data_map, "installed", resolved.installed, False),
                ("data.desktop", data_map, "desktop", resolved.desktop, True),
                ("data.webio", data_map, "webio", resolved.webio, True),
                ("data.routing", data_map, "routing", resolved.routing, True),
            ),
            flush_fingerprints=True,
        )

        self._last_apply_summary = {
            "branch_count": len(target_paths),
            "changed_branches": len(changed_paths),
            "unchanged_branches": len(target_paths) - len(changed_paths) - len(failed_paths),
            "failed_branches": len(failed_paths),
            "changed_paths": list(changed_paths),
            "defaults_failed": defaults_failed,
            "transaction_total": transaction_total,
            "phases": phase_summaries,
        }
        if diff_applied_paths:
            self._last_apply_summary["diff_applied_branches"] = len(diff_applied_paths)
            self._last_apply_summary["diff_applied_paths"] = list(diff_applied_paths)
        if replaced_paths:
            self._last_apply_summary["replaced_branches"] = len(replaced_paths)
            self._last_apply_summary["replaced_paths"] = list(replaced_paths)
        if fingerprint_unchanged_paths:
            self._last_apply_summary["fingerprint_unchanged_branches"] = len(fingerprint_unchanged_paths)
            self._last_apply_summary["fingerprint_unchanged_paths"] = list(fingerprint_unchanged_paths)
        if stale_fingerprint_paths:
            self._last_apply_summary["stale_fingerprint_branches"] = len(stale_fingerprint_paths)
            self._last_apply_summary["stale_fingerprint_paths"] = list(stale_fingerprint_paths)
        if failed_paths:
            self._last_apply_summary["failed_paths"] = list(failed_paths)
        self._last_apply_phase_timings_ms = phase_timings_ms or None

    def _resolve_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebspaceResolverOutputs:
        return self.resolve_webspace(self._collect_resolver_inputs_in_doc(ydoc, webspace_id))

    def _rebuild_in_doc(
        self,
        ydoc: Y.YDoc,
        webspace_id: str,
        *,
        expected_request_id: str | None = None,
    ) -> WebUIRegistryEntry:
        rebuild_started = time.perf_counter()
        timings: Dict[str, float] = {}
        self._last_resolver_debug = None
        self._last_apply_summary = None
        self._last_apply_phase_timings_ms = None

        stage_started = time.perf_counter()
        inputs = self._collect_resolver_inputs_in_doc(ydoc, webspace_id)
        _record_timing(timings, "collect_inputs", stage_started)

        stage_started = time.perf_counter()
        resolved = self.resolve_webspace(inputs)
        _record_timing(timings, "resolve", stage_started)

        _raise_if_rebuild_request_superseded(webspace_id, expected_request_id)
        stage_started = time.perf_counter()
        self._apply_resolved_state_in_doc(
            ydoc,
            webspace_id,
            resolved,
            inputs=inputs,
            expected_request_id=expected_request_id,
        )
        _record_timing(timings, "apply", stage_started)
        apply_phase_timings = _copy_timing_map(self._last_apply_phase_timings_ms) or {}
        timings.update(apply_phase_timings)

        stage_started = time.perf_counter()
        entry = resolved.to_registry_entry()
        _record_timing(timings, "to_registry_entry", stage_started)
        self._last_rebuild_timings_ms = _finalize_timing_map(timings, started_at=rebuild_started)

        try:
            _log.debug(
                "rebuilt webspace=%s scenario=%s source=%s legacy_fallback=%s cache_hit=%s apply=%d/%d apps=%d widgets=%d timings_ms=%s",
                webspace_id,
                resolved.scenario_id,
                str(inputs.scenario_source or ""),
                bool(inputs.legacy_scenario_fallback),
                bool((self._last_resolver_debug or {}).get("cache_hit")),
                int((self._last_apply_summary or {}).get("changed_branches") or 0),
                int((self._last_apply_summary or {}).get("branch_count") or 0),
                len(entry.apps),
                len(entry.widgets),
                self._last_rebuild_timings_ms,
            )
        except Exception:
            pass

        return entry

    # --- public API ------------------------------------------------------

    def compute_registry_for_webspace(
        self,
        webspace_id: str,
        *,
        request_id: str | None = None,
    ) -> WebUIRegistryEntry:
        """
        Compute and apply the effective UI model for the given webspace.

        This is a synchronous helper that loads the YDoc via get_ydoc(),
        rebuilds ui.application/data.catalog/data.installed/registry.merged
        and returns the resulting registry snapshot.
        """
        with _webspace_runtime_sync_write_meta(
            root_names=["ui", "data", "registry", "runtime"],
            source="webspace_runtime.rebuild_sync",
        ):
            with get_ydoc(webspace_id) as ydoc:
                return self._rebuild_in_doc(ydoc, webspace_id, expected_request_id=request_id)

    async def rebuild_webspace_async(
        self,
        webspace_id: str,
        *,
        request_id: str | None = None,
        publish_live_room: bool = True,
        initial_scenario_id: str | None = None,
    ) -> WebUIRegistryEntry:
        """
        Async counterpart of :meth:`compute_registry_for_webspace` for use
        inside running event loops.
        """
        rebuild_started = time.perf_counter()
        ydoc_timings: Dict[str, float] = {}
        self._last_rebuild_ydoc_timings_ms = None
        try:
            async with _open_rebuild_ydoc_session(
                webspace_id,
                timings=ydoc_timings,
                publish_live_room=publish_live_room,
            ) as ydoc:
                seed_scenario = str(initial_scenario_id or "").strip()
                if seed_scenario:
                    stage_started = time.perf_counter()
                    ui_map = ydoc.get_map("ui")
                    with ydoc.begin_transaction() as txn:
                        _set_map_value_if_changed(ui_map, txn, "current_scenario", seed_scenario)
                    _record_timing(ydoc_timings, "seed_initial_scenario", stage_started)
                stage_started = time.perf_counter()
                entry = self._rebuild_in_doc(
                    ydoc,
                    webspace_id,
                    expected_request_id=request_id,
                )
                _record_timing(ydoc_timings, "in_doc_rebuild", stage_started)
                return entry
        finally:
            self._last_rebuild_ydoc_timings_ms = _finalize_timing_map(ydoc_timings, started_at=rebuild_started)


# --- webspace helpers ---------------------------------------------------


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload") or {}
        if isinstance(data, dict):
            return data
    if isinstance(evt, dict):
        return evt
    return {}


def _webspace_id(payload: Dict[str, Any]) -> str:
    """
    Resolve target webspace id for an event payload.

    Explicit fields on the payload (webspace_id/workspace_id) take
    precedence over metadata injected by the transport (_meta).
    """
    if isinstance(payload, dict):
        direct = payload.get("webspace_id") or payload.get("workspace_id")
        if direct:
            return str(direct)
        meta = payload.get("_meta")
        if isinstance(meta, dict):
            token = meta.get("webspace_id") or meta.get("workspace_id")
            if token:
                return str(token)
    return default_webspace_id()


def _payload_command_trace(payload: Dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    return {
        "cmd_id": str(meta.get("cmd_id") or "").strip() or None,
        "gateway_client": str(meta.get("gateway_client") or "").strip() or None,
        "gateway_command_seq": int(meta.get("gateway_command_seq") or 0),
        "gateway_command_fingerprint": str(meta.get("gateway_command_fingerprint") or "").strip() or None,
        "device_id": str(meta.get("device_id") or "").strip() or None,
        "trace_id": str(meta.get("trace_id") or "").strip() or None,
    }


def _recovery_request_fingerprint(
    *,
    webspace_id: str,
    action: str,
    scenario_id: str | None,
    command_trace: Mapping[str, Any] | None = None,
) -> str:
    trace = command_trace if isinstance(command_trace, Mapping) else {}
    trace_fp = str(trace.get("gateway_command_fingerprint") or "").strip()
    if trace_fp:
        return trace_fp
    raw = {
        "webspace_id": str(webspace_id or "").strip() or "default",
        "action": str(action or "").strip() or "reload",
        "scenario_id": str(scenario_id or "").strip() or None,
    }
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:12]


async def _resolve_rebuild_scenario_target(
    webspace_id: str,
    requested_scenario_id: str | None,
    *,
    prefer_manifest_home_before_current: bool = False,
) -> tuple[WebspaceOperationalState, str, str]:
    """
    Resolve the effective scenario target for backend-owned rebuild flows.

    ``prefer_manifest_home_before_current`` preserves legacy reload/reset
    behaviour where the stored manifest home scenario remains authoritative
    unless the caller explicitly overrides it.
    """
    state = await describe_webspace_operational_state(webspace_id)
    requested = str(requested_scenario_id or "").strip()
    if requested:
        return state, requested, "explicit"

    stored_home = str(state.stored_home_scenario or "").strip() or None
    current = str(state.current_scenario or "").strip() or None
    effective_home = str(state.effective_home_scenario or "").strip() or None

    if prefer_manifest_home_before_current:
        if stored_home:
            return state, stored_home, "manifest_home"
        if current:
            return state, current, "current_scenario"
    else:
        if current:
            return state, current, "current_scenario"
        if effective_home:
            return state, effective_home, "manifest_home"

    return state, "web_desktop", "default"


def _resolve_projection_refresh_space(webspace_id: str) -> str:
    try:
        row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
        return "dev" if str(getattr(row, "effective_source_mode", "") or "").strip().lower() == "dev" else "workspace"
    except Exception:
        return "workspace"


async def _refresh_projection_rules_for_rebuild(
    ctx: AgentContext,
    webspace_id: str,
    *,
    scenario_id: str | None = None,
    scenario_resolution: str | None = None,
) -> dict[str, Any]:
    target_scenario = str(scenario_id or "").strip() or None
    target_resolution = str(scenario_resolution or "").strip() or None
    if not target_scenario or not target_resolution:
        try:
            _state, resolved_scenario, resolved_resolution = await _resolve_rebuild_scenario_target(
                webspace_id,
                target_scenario,
                prefer_manifest_home_before_current=False,
            )
            if not target_scenario:
                target_scenario = resolved_scenario
            if not target_resolution:
                target_resolution = resolved_resolution
        except Exception:
            _log.debug("failed to resolve projection refresh target for webspace=%s", webspace_id, exc_info=True)
            target_scenario = target_scenario or None
            target_resolution = target_resolution or None
    target_space = _resolve_projection_refresh_space(webspace_id)
    if not target_scenario:
        return {
            "attempted": False,
            "scenario_id": None,
            "scenario_resolution": target_resolution,
            "space": target_space,
            "rules_loaded": 0,
            "source": "none",
        }
    try:
        rules_loaded = int(ctx.projections.load_from_scenario(target_scenario, space=target_space) or 0)
        return {
            "attempted": True,
            "scenario_id": target_scenario,
            "scenario_resolution": target_resolution,
            "space": target_space,
            "rules_loaded": rules_loaded,
            "source": "scenario_manifest",
        }
    except Exception as exc:
        try:
            replace_scenario_entries = getattr(ctx.projections, "replace_scenario_entries", None)
            if callable(replace_scenario_entries):
                replace_scenario_entries([], scenario_id=target_scenario, space=target_space)
        except Exception:
            _log.debug("failed to clear stale scenario data_projections for scenario=%s", target_scenario, exc_info=True)
        _log.debug("failed to refresh data_projections for scenario=%s", target_scenario, exc_info=True)
        return {
            "attempted": True,
            "scenario_id": target_scenario,
            "scenario_resolution": target_resolution,
            "space": target_space,
            "rules_loaded": 0,
            "source": "scenario_manifest",
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _slugify_webspace_id(raw: str | None) -> str:
    if not raw:
        return ""
    # Preserve original casing while normalising invalid characters so that
    # webspace ids used in events and YDoc room names stay identical.
    token = _WS_ID_RE.sub("-", str(raw).strip())
    return token.strip("-")


def _allocate_webspace_id(raw: str | None) -> str:
    candidate = _slugify_webspace_id(raw)
    if not candidate:
        candidate = f"space-{secrets.token_hex(2)}"
    base = candidate
    suffix = 1
    while workspace_index.get_workspace(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _is_dev_title(title: Optional[str]) -> bool:
    if not title:
        return False
    return str(title).lstrip().upper().startswith("DEV:")


def _display_name_for_kind(title: Optional[str], *, webspace_id: str, kind: str) -> str:
    raw_title = (title or webspace_id).strip() or webspace_id
    if kind == "dev":
        if _is_dev_title(raw_title):
            return raw_title
        return f"DEV: {raw_title}"
    if _is_dev_title(raw_title):
        return raw_title.lstrip()[4:].lstrip() or webspace_id
    return raw_title


def _webspace_listing() -> List[Dict[str, Any]]:
    rows = workspace_index.list_workspaces()
    local_display = _local_node_display()
    return [
        _with_webspace_validation(
            source_mode=row.effective_source_mode,
            stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
            effective_home_scenario=row.effective_home_scenario,
            current_scenario=_try_read_live_current_scenario(row.workspace_id),
            payload={
                "id": row.workspace_id,
                "title": row.title,
                "created_at": row.created_at,
                "kind": row.effective_kind,
                "home_scenario": row.effective_home_scenario,
                "home_scenario_ref": getattr(row, "home_scenario_ref_overlay", {}) or None,
                "source_mode": row.effective_source_mode,
                "node_id": _local_node_id(),
                "node_label": local_display.get("node_label"),
                "node_compact_label": local_display.get("node_compact_label"),
                "node_index": local_display.get("node_index"),
                "node_color": local_display.get("node_color"),
            },
        )
        for row in rows
    ]


def _webspace_info_from_row(
    row: workspace_index.WebspaceManifest,
    *,
    local_display: Mapping[str, Any] | None = None,
    current_scenario: Any = _HOME_SCENARIO_REF_UNSET,
) -> WebspaceInfo:
    resolved_display = dict(local_display) if isinstance(local_display, Mapping) else _local_node_display()
    if current_scenario is _HOME_SCENARIO_REF_UNSET:
        current_scenario = _try_read_live_current_scenario(row.workspace_id)
    validation = _build_webspace_validation(
        source_mode=row.effective_source_mode,
        stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
        effective_home_scenario=row.effective_home_scenario,
        current_scenario=current_scenario,
    )
    return WebspaceInfo(
        id=row.workspace_id,
        title=row.title,
        created_at=row.created_at,
        kind=row.effective_kind,
        home_scenario=row.effective_home_scenario,
        home_scenario_ref=getattr(row, "home_scenario_ref_overlay", {}) or None,
        source_mode=row.effective_source_mode,
        node_id=_local_node_id(),
        node_label=str(resolved_display.get("node_label") or _local_node_label()),
        node_compact_label=str(resolved_display.get("node_compact_label") or "") or None,
        node_index=resolved_display.get("node_index"),
        node_color=str(resolved_display.get("node_color") or "") or None,
        is_dev=row.is_dev,
        current_scenario=current_scenario,
        stored_home_scenario_exists=validation.get("stored_home_scenario_exists"),
        home_scenario_exists=bool(validation.get("home_scenario_exists")),
        current_scenario_exists=validation.get("current_scenario_exists"),
        degraded=bool(validation.get("degraded")),
        validation_reason=str(validation.get("validation_reason") or "").strip() or None,
        recommended_action=str(validation.get("recommended_action") or "").strip() or None,
    )


async def describe_webspace_operational_state(webspace_id: str) -> WebspaceOperationalState:
    """
    Return the combined manifest + live scenario state for a webspace.

    The helper intentionally keeps both the raw stored ``home_scenario`` and
    the effective fallback value so Phase 2 callers can preserve legacy reload
    behaviour while still exposing stable operational semantics to control
    surfaces.
    """
    target_webspace_id = str(webspace_id or "").strip() or default_webspace_id()
    row = workspace_index.get_workspace(target_webspace_id) or workspace_index.ensure_workspace(target_webspace_id)

    current_scenario: str | None = _try_read_live_current_scenario(target_webspace_id)
    validation = _build_webspace_validation(
        source_mode=row.effective_source_mode,
        stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
        effective_home_scenario=row.effective_home_scenario,
        current_scenario=current_scenario,
    )
    if current_scenario is not None:
        return WebspaceOperationalState(
            webspace_id=target_webspace_id,
            title=row.title,
            kind=row.effective_kind,
            source_mode=row.effective_source_mode,
            is_dev=row.is_dev,
            stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
            effective_home_scenario=row.effective_home_scenario,
            home_scenario_ref=getattr(row, "home_scenario_ref_overlay", {}) or None,
            current_scenario=current_scenario,
            stored_home_scenario_exists=validation.get("stored_home_scenario_exists"),
            home_scenario_exists=bool(validation.get("home_scenario_exists")),
            current_scenario_exists=validation.get("current_scenario_exists"),
            degraded=bool(validation.get("degraded")),
            validation_reason=str(validation.get("validation_reason") or "").strip() or None,
            recommended_action=str(validation.get("recommended_action") or "").strip() or None,
        )

    try:
        async with _open_readonly_operational_ydoc(target_webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            raw_current = ui_map.get("current_scenario")
            if raw_current is not None:
                current_scenario = _normalize_optional_token(raw_current)
    except Exception:
        current_scenario = None

    validation = _build_webspace_validation(
        source_mode=row.effective_source_mode,
        stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
        effective_home_scenario=row.effective_home_scenario,
        current_scenario=current_scenario,
    )
    return WebspaceOperationalState(
        webspace_id=target_webspace_id,
        title=row.title,
        kind=row.effective_kind,
        source_mode=row.effective_source_mode,
        is_dev=row.is_dev,
        stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
        effective_home_scenario=row.effective_home_scenario,
        home_scenario_ref=getattr(row, "home_scenario_ref_overlay", {}) or None,
        current_scenario=current_scenario,
        stored_home_scenario_exists=validation.get("stored_home_scenario_exists"),
        home_scenario_exists=bool(validation.get("home_scenario_exists")),
        current_scenario_exists=validation.get("current_scenario_exists"),
        degraded=bool(validation.get("degraded")),
        validation_reason=str(validation.get("validation_reason") or "").strip() or None,
        recommended_action=str(validation.get("recommended_action") or "").strip() or None,
    )


async def describe_webspace_validation_state(webspace_id: str) -> dict[str, Any]:
    state = await describe_webspace_operational_state(webspace_id)
    return {
        "webspace_id": state.webspace_id,
        "source_mode": state.source_mode,
        "stored_home_scenario": state.stored_home_scenario,
        "home_scenario": state.effective_home_scenario,
        "current_scenario": state.current_scenario,
        "stored_home_scenario_exists": state.stored_home_scenario_exists,
        "home_scenario_exists": state.home_scenario_exists,
        "current_scenario_exists": state.current_scenario_exists,
        "degraded": state.degraded,
        "validation_reason": state.validation_reason,
        "recommended_action": state.recommended_action,
    }


def describe_webspace_overlay_state(webspace_id: str) -> dict[str, Any]:
    target_webspace_id = str(webspace_id or "").strip() or default_webspace_id()
    row = workspace_index.get_workspace(target_webspace_id) or workspace_index.ensure_workspace(target_webspace_id)
    return {
        "webspace_id": target_webspace_id,
        "source": "workspace_manifest_overlay",
        "has_overlay": bool(getattr(row, "has_ui_overlay", False)),
        "has_installed": bool(getattr(row, "has_installed_overlay", False)),
        "has_pinned_widgets": bool(getattr(row, "has_pinned_widgets_overlay", False)),
        "has_topbar": bool(getattr(row, "has_topbar_overlay", False)),
        "has_page_schema": bool(getattr(row, "has_page_schema_overlay", False)),
        "has_icon_order": bool(getattr(row, "has_icon_order_overlay", False)),
        "has_widget_order": bool(getattr(row, "has_widget_order_overlay", False)),
        "desktop": dict(getattr(row, "desktop_overlay", {}) or {}),
        "installed": _coerce_dict(getattr(row, "installed_overlay", {}) or {}),
        "pinned_widgets": _normalize_overlay_widget_entries(getattr(row, "pinned_widgets_overlay", []) or []),
        "topbar": list(getattr(row, "topbar_overlay", []) or []),
        "page_schema": _coerce_dict(getattr(row, "page_schema_overlay", {}) or {}),
        "icon_order": list(getattr(row, "icon_order_overlay", []) or []),
        "widget_order": list(getattr(row, "widget_order_overlay", []) or []),
    }


async def describe_webspace_projection_state(
    webspace_id: str,
    *,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    """
    Return a lightweight snapshot of the projection lifecycle for a webspace.

    This is a read-only control-surface helper: it does not refresh or mutate
    the active registry, it only explains which scenario the current layer is
    targeting and whether that matches the active scenario layer in memory.
    """
    operational = await describe_webspace_operational_state(webspace_id)
    target_scenario = (
        str(scenario_id or "").strip()
        or str(operational.current_scenario or "").strip()
        or str(operational.effective_home_scenario or "").strip()
        or None
    )

    registry = get_ctx().projections
    snapshot: Dict[str, Any] = {}
    try:
        raw = registry.snapshot() if hasattr(registry, "snapshot") else {}
        snapshot = dict(raw) if isinstance(raw, Mapping) else {}
    except Exception:
        snapshot = {}

    active_scenario = str(snapshot.get("active_scenario_id") or "").strip() or None
    active_space = str(snapshot.get("active_space") or "").strip() or "workspace"
    target_space = "dev" if str(operational.source_mode or "").strip().lower() == "dev" else "workspace"
    return {
        "webspace_id": operational.webspace_id,
        "target_scenario": target_scenario,
        "target_space": target_space,
        "active_scenario": active_scenario,
        "active_space": active_space,
        "active_matches_target": bool(target_scenario)
        and active_scenario == target_scenario
        and active_space == target_space,
        "base_rule_count": int(snapshot.get("base_rule_count") or 0),
        "scenario_rule_count": int(snapshot.get("scenario_rule_count") or 0),
        "source": "projection_registry",
    }


async def _resolve_reload_scenario_target(
    webspace_id: str,
    requested_scenario_id: str | None,
) -> tuple[WebspaceOperationalState, str, str]:
    """
    Resolve the scenario source for reload/reset.

    Ordering intentionally preserves Phase 1 / Phase 2 compatibility:

    1. explicit scenario override
    2. explicit stored manifest home_scenario
    3. current live scenario for legacy spaces without stored home
    4. default ``web_desktop``
    """
    return await _resolve_rebuild_scenario_target(
        webspace_id,
        requested_scenario_id,
        prefer_manifest_home_before_current=True,
    )


def _try_read_live_current_scenario(webspace_id: str) -> str | None:
    live_hit, raw_current = try_read_live_map_value(webspace_id, "ui", "current_scenario")
    if not live_hit:
        return None
    return _normalize_optional_token(raw_current)


def _open_readonly_operational_ydoc(webspace_id: str):
    """
    Open a read-only YDoc session for operational/status reads.

    Prefer the modern live-room-aware accessor, but degrade gracefully to the
    legacy helper or a bare async getter while tests and older wrappers still
    patch narrower call signatures during the migration.
    """
    try:
        return async_get_ydoc(
            webspace_id,
            read_only=True,
            prefer_live_room=True,
        )
    except TypeError:
        try:
            return async_get_ydoc(webspace_id)
        except TypeError:
            return async_read_ydoc(webspace_id)


def _open_rebuild_ydoc_session(
    webspace_id: str,
    *,
    timings: dict[str, float] | None = None,
    publish_live_room: bool = True,
):
    """
    Open a writable YDoc session for semantic rebuild.

    Production code prefers the live-room-aware async accessor with timing
    capture, but tests and older shims may still expose a narrower
    `async_get_ydoc(webspace_id)` contract.
    """
    try:
        return async_get_ydoc(
            webspace_id,
            prefer_live_room=bool(publish_live_room),
            publish_live_room=bool(publish_live_room),
            timings=timings,
            load_mark_roots=["ui", "data", "registry", "runtime"],
            governed=True,
            write_source="webspace_runtime.rebuild_async",
            write_owner="core:webspace_runtime",
            write_channel="core.webspace_runtime.async",
        )
    except TypeError:
        try:
            return async_get_ydoc(
                webspace_id,
                prefer_live_room=bool(publish_live_room),
                timings=timings,
            )
        except TypeError:
            return async_get_ydoc(webspace_id)


async def _sync_webspace_listing() -> None:
    listing = _webspace_listing()
    payload = {"items": listing}
    rows = workspace_index.list_workspaces()
    for row in rows:
        async with _webspace_runtime_async_write_meta(
            root_names=["data"],
            source="webspace_runtime.sync_listing",
        ):
            async with async_get_ydoc(row.workspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                with ydoc.begin_transaction() as txn:
                    _set_map_value_if_changed(data_map, txn, "webspaces", payload)


def _schedule_webspace_listing_sync(*, reason: str) -> dict[str, Any]:
    global _WEBSPACE_LISTING_SYNC_TASK  # pylint: disable=global-statement

    current = _WEBSPACE_LISTING_SYNC_TASK
    if current is not None and not current.done():
        return {
            "scheduled": True,
            "coalesced": True,
            "reason": reason,
            "task": current.get_name(),
        }

    async def _runner() -> None:
        started = time.perf_counter()
        try:
            await _sync_webspace_listing()
            _log.info(
                "post-ready webspace listing sync completed reason=%s duration_ms=%.3f",
                reason,
                _elapsed_ms(started),
            )
        except Exception:
            _log.warning("post-ready webspace listing sync failed reason=%s", reason, exc_info=True)
        finally:
            global _WEBSPACE_LISTING_SYNC_TASK  # pylint: disable=global-statement
            if _WEBSPACE_LISTING_SYNC_TASK is task:
                _WEBSPACE_LISTING_SYNC_TASK = None

    task = asyncio.create_task(
        _runner(),
        name=f"webspace-listing-sync:{str(reason or 'background')[:40]}",
    )
    _WEBSPACE_LISTING_SYNC_TASK = task
    return {
        "scheduled": True,
        "coalesced": False,
        "reason": reason,
        "task": task.get_name(),
    }


class WebspaceService:
    """
    Helper for managing webspaces (workspaces) from core services and SDK.

    This service centralises CRUD logic that was previously spread across
    event handlers so that higher-level callers do not need to touch YDoc
    or SQLite details directly.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    def list_ids(self, *, mode: str = "mixed") -> List[str]:
        rows = workspace_index.list_workspaces()
        ids: List[str] = []
        for row in rows:
            kind = row.effective_kind
            if mode == "workspace" and kind != "workspace":
                continue
            if mode == "dev" and kind != "dev":
                continue
            token = str(row.workspace_id or "").strip()
            if token:
                ids.append(token)
        return ids

    def list(self, *, mode: str = "mixed") -> List[WebspaceInfo]:
        """
        List known webspaces.

        mode:
          - \"workspace\" — only non-dev webspaces,
          - \"dev\"       — only dev webspaces,
          - \"mixed\"     — all (default).
        """
        rows = workspace_index.list_workspaces()
        infos: List[WebspaceInfo] = []
        local_display = _local_node_display()
        for row in rows:
            title = row.title
            kind = row.effective_kind
            is_dev = row.is_dev
            if mode == "workspace" and kind != "workspace":
                continue
            if mode == "dev" and kind != "dev":
                continue
            infos.append(_webspace_info_from_row(row, local_display=local_display))
        return infos

    async def _sync_listing(self) -> None:
        await _sync_webspace_listing()

    async def create(
        self,
        requested_id: Optional[str],
        title: Optional[str],
        *,
        scenario_id: str = "web_desktop",
        scenario_ref: Any = None,
        dev: bool = False,
    ) -> WebspaceInfo:
        webspace_id = _allocate_webspace_id(requested_id)
        _log.info("creating webspace %s (requested=%s dev=%s)", webspace_id, requested_id, dev)
        kind = "dev" if dev else "workspace"
        source_mode = "dev" if dev else "workspace"
        workspace_index.ensure_workspace(webspace_id)
        display_name = _display_name_for_kind(title, webspace_id=webspace_id, kind=kind)
        row = workspace_index.set_workspace_manifest(
            webspace_id,
            display_name=display_name,
            kind=kind,
            home_scenario=str(scenario_id or "").strip() or "web_desktop",
            source_mode=source_mode,
        )
        if isinstance(scenario_ref, Mapping):
            row = workspace_index.set_workspace_home_scenario_ref_overlay(webspace_id, dict(scenario_ref))
        await _seed_webspace_from_scenario(webspace_id, scenario_id, dev=dev)
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def rename(self, webspace_id: str, title: str) -> Optional[WebspaceInfo]:
        webspace_id = (webspace_id or "").strip()
        title = (title or "").strip()
        if not webspace_id or not title:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot rename missing webspace %s", webspace_id)
            return None
        display_name = _display_name_for_kind(title, webspace_id=webspace_id, kind=row.effective_kind)
        row = workspace_index.set_workspace_manifest(
            webspace_id,
            display_name=display_name,
            kind=row.effective_kind,
            source_mode=row.effective_source_mode,
        )
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def update_metadata(
        self,
        webspace_id: str,
        *,
        title: str | None = None,
        home_scenario: str | None = None,
        home_scenario_ref: Any = _HOME_SCENARIO_REF_UNSET,
    ) -> Optional[WebspaceInfo]:
        webspace_id = str(webspace_id or "").strip()
        if not webspace_id:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot update missing webspace %s", webspace_id)
            return None

        manifest_kwargs: Dict[str, Any] = {}
        next_title = str(title or "").strip()
        if next_title:
            manifest_kwargs["display_name"] = _display_name_for_kind(
                next_title,
                webspace_id=webspace_id,
                kind=row.effective_kind,
            )

        next_home_scenario = str(home_scenario or "").strip()
        if next_home_scenario:
            manifest_kwargs["home_scenario"] = next_home_scenario

        if not manifest_kwargs and home_scenario_ref is _HOME_SCENARIO_REF_UNSET:
            return _webspace_info_from_row(row)

        updated = row if not manifest_kwargs else workspace_index.set_workspace_manifest(webspace_id, **manifest_kwargs)
        if home_scenario_ref is not _HOME_SCENARIO_REF_UNSET:
            updated = workspace_index.set_workspace_home_scenario_ref_overlay(webspace_id, home_scenario_ref)
        await self._sync_listing()
        return _webspace_info_from_row(updated)

    async def set_home_scenario(
        self,
        webspace_id: str,
        scenario_id: str,
        *,
        home_scenario_ref: Any = _HOME_SCENARIO_REF_UNSET,
    ) -> Optional[WebspaceInfo]:
        webspace_id = (webspace_id or "").strip()
        scenario_id = (scenario_id or "").strip()
        if not webspace_id or not scenario_id:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot set home_scenario for missing webspace %s", webspace_id)
            return None
        row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
        if home_scenario_ref is not _HOME_SCENARIO_REF_UNSET:
            row = workspace_index.set_workspace_home_scenario_ref_overlay(webspace_id, home_scenario_ref)
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def ensure_dev_for_scenario(
        self,
        scenario_id: str,
        *,
        requested_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[WebspaceInfo, bool]:
        scenario_id = str(scenario_id or "").strip()
        requested_id = str(requested_id or "").strip() or None
        title = str(title or "").strip() or None
        if not scenario_id:
            raise ValueError("scenario_id is required")

        existing: Optional[workspace_index.WebspaceManifest] = None
        if requested_id:
            row = workspace_index.get_workspace(requested_id)
            if row and not row.is_dev:
                raise ValueError("requested webspace is not a dev webspace")
            existing = row
        if existing is None:
            for row in workspace_index.list_workspaces():
                if row.is_dev and row.effective_home_scenario == scenario_id:
                    existing = row
                    break

        created = False
        if existing is None:
            preferred_id = requested_id or f"dev-{scenario_id}"
            info = await self.create(
                preferred_id,
                title or scenario_id,
                scenario_id=scenario_id,
                dev=True,
            )
            created = True
        else:
            info = _webspace_info_from_row(existing)

        return info, created

    async def delete(self, webspace_id: str) -> bool:
        webspace_id = (webspace_id or "").strip()
        if not webspace_id or webspace_id == default_webspace_id():
            return False
        _log.info("deleting webspace %s via WebspaceService", webspace_id)
        try:
            workspace_index.delete_workspace(webspace_id)
        except Exception as exc:
            _log.warning("failed to delete webspace %s: %s", webspace_id, exc)
            return False
        try:
            from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
            from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel
 
            try:
                await reset_live_webspace_room(webspace_id, close_reason="webspace_delete")
            except Exception:
                pass
            try:
                reset_ystore_for_webspace(webspace_id)
            except Exception:
                pass
        except Exception:
            _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)
        await self._sync_listing()
        return True

    async def refresh(self) -> None:
        try:
            workspace_index.normalize_workspaces()
        except Exception:
            _log.debug("failed to normalize webspace manifests before refresh", exc_info=True)
        await self._sync_listing()


async def _seed_webspace_from_scenario(webspace_id: str, scenario_id: str, *, dev: Optional[bool] = None) -> None:
    """
    Seed a webspace YDoc from the given scenario package using the standard
    ScenarioManager.sync_to_yjs* projection path, falling back to static
    seeds inside ensure_webspace_seeded_from_scenario when needed.
    """
    await _seed_webspace_from_scenario_with_options(
        webspace_id,
        scenario_id,
        dev=dev,
        emit_event=True,
    )


def _resolve_webspace_source_mode(webspace_id: str, *, dev: Optional[bool] = None) -> str:
    source_mode = "workspace"
    if dev is None:
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                dev = row.is_dev
                source_mode = row.effective_source_mode
            else:
                dev = False
        except Exception:
            dev = False
    elif dev:
        source_mode = "dev"
    return source_mode


def _build_scenario_manager():
    from adaos.adapters.db import SqliteScenarioRegistry  # pylint: disable=import-outside-toplevel
    from adaos.services.scenario.manager import ScenarioManager  # pylint: disable=import-outside-toplevel

    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(
        repo=ctx.scenarios_repo,
        registry=reg,
        git=ctx.git,
        paths=ctx.paths,
        bus=ctx.bus,
        caps=ctx.caps,
    )


async def _project_webspace_from_scenario(
    webspace_id: str,
    scenario_id: str,
    *,
    dev: Optional[bool] = None,
    emit_event: bool = True,
) -> None:
    source_mode = _resolve_webspace_source_mode(webspace_id, dev=dev)
    _log.debug(
        "projecting webspace=%s scenario=%s source_mode=%s emit_event=%s",
        webspace_id,
        scenario_id,
        source_mode,
        emit_event,
    )
    try:
        mgr = _build_scenario_manager()
        timeout_s = _project_scenario_timeout_s()
        projection = mgr.sync_to_yjs_async(
            scenario_id or "web_desktop",
            webspace_id,
            space=source_mode,
            emit_event=emit_event,
        )
        if timeout_s > 0.0:
            await asyncio.wait_for(projection, timeout=timeout_s)
        else:
            await projection
    except asyncio.TimeoutError:
        _log.warning(
            "timed out projecting webspace=%s from scenario=%s timeout_s=%s",
            webspace_id,
            scenario_id,
            _project_scenario_timeout_s(),
        )
    except Exception:
        _log.warning(
            "failed to project webspace=%s from scenario=%s",
            webspace_id,
            scenario_id,
            exc_info=True,
        )


async def _seed_webspace_from_scenario_with_options(
    webspace_id: str,
    scenario_id: str,
    *,
    dev: Optional[bool] = None,
    emit_event: bool = True,
) -> None:
    ystore = get_ystore_for_webspace(webspace_id)
    source_mode = _resolve_webspace_source_mode(webspace_id, dev=dev)
    _log.debug("seeding webspace=%s scenario=%s dev=%s", webspace_id, scenario_id, dev)
    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=scenario_id or "web_desktop",
            space=source_mode,
            emit_event=emit_event,
        )
    except Exception:
        _log.warning("failed to seed webspace=%s from scenario=%s", webspace_id, scenario_id, exc_info=True)


# --- event subscriptions (core-level) -----------------------------------


@subscribe("scenarios.synced")
async def _on_scenarios_synced(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI for a webspace when its scenario has been projected
    into YDoc by ScenarioManager.sync_to_yjs*.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    scenario_id = str(evt.get("scenario_id") or "").strip() or None
    if _should_skip_bootstrap_scenario_sync_rebuild(evt, webspace_id=webspace_id, scenario_id=scenario_id):
        return
    await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_projection_sync",
        scenario_id=scenario_id,
        scenario_resolution="projected_payload",
        source_of_truth="scenario_projection",
    )


@subscribe("skills.activated")
async def _on_skill_activated(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI for the target webspace when a skill is activated.

    For MVP we only rebuild the webspace explicitly referenced in the event
    (or the default webspace), not all workspaces.
    """
    if bool(evt.get("defer_webspace_rebuild")):
        return
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    schedule_skill_runtime_rebuild(
        webspace_id=webspace_id,
        action="skill_activation_sync",
        source_of_truth="skill_runtime",
        reason=str(evt.get("skill_name") or evt.get("name") or "skills.activated"),
    )


@subscribe("skills.updated")
async def _on_skill_updated(evt: Dict[str, Any]) -> None:
    if bool(evt.get("defer_webspace_rebuild")):
        return
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    schedule_skill_runtime_rebuild(
        webspace_id=webspace_id,
        action="skill_update_sync",
        source_of_truth="skill_runtime",
        reason=str(evt.get("name") or evt.get("skill_name") or "skills.updated"),
    )


@subscribe("skills.rolledback")
async def _on_skill_rolled_back(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI when a skill is rolled back so that its catalog
    entries and registry contributions are removed from the target webspace.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    schedule_skill_runtime_rebuild(
        webspace_id=webspace_id,
        action="skill_rollback_sync",
        source_of_truth="skill_runtime",
        reason=str(evt.get("name") or evt.get("skill_name") or "skills.rolledback"),
    )


@subscribe("skill.uninstalled")
async def _on_skill_uninstalled(evt: Dict[str, Any]) -> None:
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    schedule_skill_runtime_rebuild(
        webspace_id=webspace_id,
        action="skill_uninstall_sync",
        source_of_truth="skill_runtime",
        reason=str(evt.get("name") or evt.get("skill_name") or "skill.uninstalled"),
    )


@subscribe("scenario.removed")
async def _on_scenario_removed(evt: Dict[str, Any]) -> None:
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_uninstall_sync",
        source_of_truth="scenario_projection",
    )


@subscribe("subnet.member.snapshot.changed")
async def _on_subnet_member_snapshot_changed(evt: Any) -> None:
    payload = _payload(evt)
    node_id = str(payload.get("node_id") or "").strip() or "member"
    reason = _member_snapshot_rebuild_reason(evt, payload)
    now = time.monotonic()
    interval_s = _member_snapshot_rebuild_min_interval_s()
    try:
        rows = [row for row in workspace_index.list_workspaces() if not bool(getattr(row, "is_dev", False))]
    except Exception:
        rows = []
    targets = [
        str(getattr(row, "workspace_id", "") or "").strip()
        for row in rows
        if str(getattr(row, "workspace_id", "") or "").strip()
    ] or [default_webspace_id()]
    for webspace_id in targets:
        key = f"{node_id}\0{webspace_id}"
        stats = _member_snapshot_rebuild_stats(key)
        request_id = _member_snapshot_rebuild_request_id(webspace_id=webspace_id, node_id=node_id)
        stats["requested_total"] = int(stats.get("requested_total") or 0) + 1
        stats["last_reason"] = reason
        stats["last_requested_at"] = time.time()
        stats["last_request_id"] = request_id
        existing = _MEMBER_SNAPSHOT_REBUILD_TASKS.get(key)
        if existing is not None and not existing.done():
            _mark_member_snapshot_rebuild_dirty(task_key=key, reason=reason, mode="task_running", request_id=request_id)
            continue
        last_at = float(_MEMBER_SNAPSHOT_REBUILD_AT.get(key) or 0.0)
        if interval_s > 0 and last_at > 0 and now - last_at < interval_s:
            _mark_member_snapshot_rebuild_dirty(task_key=key, reason=reason, mode="interval_window", request_id=request_id)
            _schedule_member_snapshot_rebuild_delayed(
                webspace_id=webspace_id,
                node_id=node_id,
                delay_s=max(0.0, interval_s - (now - last_at)),
                reason=reason,
                request_id=request_id,
            )
            continue
        _MEMBER_SNAPSHOT_REBUILD_AT[key] = now
        _schedule_member_snapshot_rebuild(webspace_id=webspace_id, node_id=node_id, reason=reason, request_id=request_id)


def _schedule_member_snapshot_rebuild(
    *,
    webspace_id: str,
    node_id: str,
    reason: str = "subnet.member.snapshot.changed",
    request_id: str | None = None,
) -> None:
    task_key = f"{str(node_id or '').strip()}\0{str(webspace_id or '').strip()}"
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    delayed = _MEMBER_SNAPSHOT_REBUILD_DELAYED_TASKS.pop(task_key, None)
    if delayed is not None and delayed is not current_task and not delayed.done():
        delayed.cancel()
    existing = _MEMBER_SNAPSHOT_REBUILD_TASKS.get(task_key)
    if existing and not existing.done():
        _mark_member_snapshot_rebuild_dirty(task_key=task_key, reason=reason, mode="task_running", request_id=request_id)
        return
    stats = _member_snapshot_rebuild_stats(task_key)
    effective_request_id = str(request_id or "").strip() or _member_snapshot_rebuild_request_id(
        webspace_id=webspace_id,
        node_id=node_id,
    )
    stats["scheduled_total"] = int(stats.get("scheduled_total") or 0) + 1
    stats["last_reason"] = str(reason or "").strip() or str(stats.get("last_reason") or "") or "subnet.member.snapshot.changed"
    stats["last_scheduled_at"] = time.time()
    stats["last_request_id"] = effective_request_id
    stats["current_request_id"] = effective_request_id

    async def _runner() -> None:
        try:
            try:
                await _seed_member_snapshot_ydoc_defaults(webspace_id=webspace_id, node_id=node_id)
            except Exception:
                _log.debug(
                    "failed to seed member snapshot defaults webspace=%s node_id=%s",
                    webspace_id,
                    node_id,
                    exc_info=True,
                )
            _log.info(
                "starting member snapshot rebuild webspace=%s node_id=%s request_id=%s reason=%s requested_total=%s scheduled_total=%s",
                webspace_id,
                node_id,
                effective_request_id,
                str(stats.get("last_reason") or reason or "").strip() or "subnet.member.snapshot.changed",
                int(stats.get("requested_total") or 0),
                int(stats.get("scheduled_total") or 0),
            )
            result = await rebuild_webspace_from_sources(
                webspace_id,
                action="subnet_member_snapshot_sync",
                source_of_truth="member_runtime_snapshot",
                request_id=effective_request_id,
            )
            stats["completed_total"] = int(stats.get("completed_total") or 0) + 1
            stats["last_completed_at"] = time.time()
            stats["last_completed_request_id"] = effective_request_id
            dirty = _MEMBER_SNAPSHOT_REBUILD_DIRTY.get(task_key) or {}
            _log.info(
                "completed member snapshot rebuild webspace=%s node_id=%s request_id=%s accepted=%s error=%s requested_total=%s scheduled_total=%s rerun_total=%s coalesced_running_total=%s coalesced_interval_total=%s delayed_total=%s dirty_pending=%s",
                webspace_id,
                node_id,
                effective_request_id,
                bool(result.get("accepted")),
                str(result.get("error") or "").strip() or None,
                int(stats.get("requested_total") or 0),
                int(stats.get("scheduled_total") or 0),
                int(stats.get("rerun_total") or 0),
                int(stats.get("coalesced_running_total") or 0),
                int(stats.get("coalesced_interval_total") or 0),
                int(stats.get("delayed_total") or 0),
                int(dirty.get("count") or 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "member snapshot rebuild failed webspace=%s node_id=%s",
                webspace_id,
                node_id,
                exc_info=True,
            )
        finally:
            current = _MEMBER_SNAPSHOT_REBUILD_TASKS.get(task_key)
            if current is task:
                _MEMBER_SNAPSHOT_REBUILD_TASKS.pop(task_key, None)
            if str(stats.get("current_request_id") or "").strip() == effective_request_id:
                stats["current_request_id"] = ""
            dirty = _MEMBER_SNAPSHOT_REBUILD_DIRTY.pop(task_key, None)
            if dirty:
                stats["rerun_total"] = int(stats.get("rerun_total") or 0) + 1
                _MEMBER_SNAPSHOT_REBUILD_AT[task_key] = time.monotonic()
                rerun_reason = str(dirty.get("last_reason") or reason or "").strip() or "subnet.member.snapshot.changed"
                _schedule_member_snapshot_rebuild(
                    webspace_id=webspace_id,
                    node_id=node_id,
                    reason=f"{rerun_reason}:coalesced",
                    request_id=str(dirty.get("last_request_id") or "").strip() or None,
                )

    task = asyncio.create_task(
        _runner(),
        name=f"member-snapshot-rebuild:{webspace_id}:{node_id}",
    )
    _MEMBER_SNAPSHOT_REBUILD_TASKS[task_key] = task


async def _seed_member_snapshot_ydoc_defaults(*, webspace_id: str, node_id: str) -> None:
    try:
        from adaos.services.registry.subnet_directory import get_directory

        node = get_directory().get_node(node_id) or {}
    except Exception:
        return
    runtime_projection = node.get("runtime_projection") if isinstance(node.get("runtime_projection"), Mapping) else {}
    snapshot = runtime_projection.get("snapshot") if isinstance(runtime_projection.get("snapshot"), Mapping) else {}
    desktop_catalog = snapshot.get("desktop_catalog") if isinstance(snapshot.get("desktop_catalog"), Mapping) else {}
    defaults = desktop_catalog.get("ydoc_defaults") if isinstance(desktop_catalog.get("ydoc_defaults"), Mapping) else {}
    if not defaults:
        return

    async with _webspace_runtime_async_write_meta(root_names=["data"], source="member_snapshot.seed_defaults"):
        async with async_get_ydoc(webspace_id, prefer_live_room=True) as ydoc:
            with ydoc.begin_transaction() as txn:
                for raw_path, raw_value in defaults.items():
                    path = str(raw_path or "").strip()
                    if not path:
                        continue
                    segments = [segment for segment in path.split("/") if segment]
                    if len(segments) < 2:
                        continue
                    root_name = segments[0]
                    root = ydoc.get_map(root_name)
                    value = _clone_json_like(raw_value)
                    if len(segments) == 2:
                        key = segments[1]
                        current = root.get(key)
                        if _json_like_equal(current, value):
                            continue
                        root.set(txn, key, value)
                        continue
                    top_key = segments[1]
                    current_top = root.get(top_key)
                    changed, merged = _merge_nested_json_path(current_top, segments[2:], value)
                    if changed:
                        root.set(txn, top_key, merged)


@subscribe("desktop.webspace.create")
async def _on_webspace_create(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    _log.debug("desktop.webspace.create payload=%s", payload)
    requested = payload.get("id") or payload.get("webspace_id")
    title = payload.get("title")
    scenario_id = str(payload.get("scenario_id") or "web_desktop")
    scenario_ref = payload.get("scenario_ref") if isinstance(payload.get("scenario_ref"), Mapping) else None
    dev = bool(payload.get("dev"))
    svc = WebspaceService(get_ctx())
    await svc.create(
        str(requested) if requested is not None else None,
        str(title) if title is not None else None,
        scenario_id=scenario_id,
        scenario_ref=scenario_ref,
        dev=dev,
    )


@subscribe("desktop.webspace.rename")
async def _on_webspace_rename(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    title = str(payload.get("title") or "").strip()
    if not webspace_id or not title:
        return
    svc = WebspaceService(get_ctx())
    await svc.rename(webspace_id, title)


@subscribe("desktop.webspace.update")
async def _on_webspace_update(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or payload.get("webspace_id") or "").strip()
    if not webspace_id:
        return
    title = str(payload.get("title") or "").strip() or None
    home_scenario = str(payload.get("home_scenario") or payload.get("scenario_id") or "").strip() or None
    home_scenario_ref = (
        payload.get("home_scenario_ref")
        if "home_scenario_ref" in payload
        else _HOME_SCENARIO_REF_UNSET
    )
    svc = WebspaceService(get_ctx())
    await svc.update_metadata(
        webspace_id,
        title=title,
        home_scenario=home_scenario,
        home_scenario_ref=home_scenario_ref,
    )


@subscribe("desktop.webspace.delete")
async def _on_webspace_delete(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    svc = WebspaceService(get_ctx())
    await svc.delete(webspace_id)


@subscribe("desktop.webspace.refresh")
async def _on_webspace_refresh(evt: Dict[str, Any]) -> None:  # noqa: ARG001
    svc = WebspaceService(get_ctx())
    await svc.refresh()


async def rebuild_webspace_from_sources(
    webspace_id: str,
    *,
    action: str = "rebuild",
    scenario_id: str | None = None,
    scenario_resolution: str | None = None,
    source_of_truth: str = "current_runtime",
    reseed_from_scenario: bool = False,
    event_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Single semantic rebuild primitive for the current runtime.

    Phase 3 keeps the existing storage and frontend contracts intact, but
    routes reload/reset/restore-style operations through one backend-owned
    materialization step so reconcile behaviour is explicit.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    rebuild_started = time.perf_counter()
    timings_ms: Dict[str, float] = {}
    requested_action = str(action or "").strip().lower() or "rebuild"
    target_scenario = str(scenario_id or "").strip() or None
    resolved_scenario_resolution = str(scenario_resolution or "").strip() or None
    status_started_at = time.time()
    if not target_scenario or not resolved_scenario_resolution:
        stage_started = time.perf_counter()
        _state, resolved_target_scenario, resolved_target_resolution = await _resolve_rebuild_scenario_target(
            webspace_id,
            target_scenario,
            prefer_manifest_home_before_current=requested_action in {"reload", "reset"},
        )
        if not target_scenario:
            target_scenario = resolved_target_scenario
        if not resolved_scenario_resolution:
            resolved_scenario_resolution = resolved_target_resolution
        _record_timing(timings_ms, "resolve_rebuild_target", stage_started)

    previous_status = describe_webspace_rebuild_state(webspace_id)
    effective_switch_timings = _copy_timing_map(switch_timings_ms) or _copy_timing_map(previous_status.get("switch_timings_ms"))
    effective_switch_mode = str(switch_mode or previous_status.get("switch_mode") or "").strip() or None
    running_materialization = _pending_materialization_snapshot(
        webspace_id,
        scenario_id=target_scenario,
        snapshot_source="rebuild:running",
        rebuild_state=previous_status,
    )
    _set_webspace_rebuild_status(
        webspace_id,
        status="running",
        pending=True,
        background=bool(previous_status.get("background")),
        request_id=request_id,
        action=requested_action,
        source_of_truth=source_of_truth,
        scenario_id=target_scenario,
        scenario_resolution=resolved_scenario_resolution,
        switch_mode=effective_switch_mode,
        requested_at=previous_status.get("requested_at") or status_started_at,
        started_at=status_started_at,
        finished_at=None,
        error=None,
        projection_refresh=None,
        registry_summary=None,
        resolver=None,
        apply_summary=None,
        timings_ms=None,
        switch_timings_ms=effective_switch_timings,
        semantic_rebuild_timings_ms=None,
        phase_timings_ms=None,
        materialization=running_materialization,
    )

    reset_room_result: dict[str, Any] | None = None
    ystore_reset = False
    fresh_doc_rebuild = False

    if requested_action == "scenario_switch_rebuild" and _fresh_doc_on_scenario_switch_enabled():
        if not target_scenario:
            raise ValueError("scenario_id is required for scenario switch rebuild")

        fresh_doc_rebuild = True
        stage_started = time.perf_counter()
        try:
            from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

            reset_room_result = {
                "ok": True,
                "skipped": "live_transport_preserved",
                "close_reason": "scenario_switch_fresh_doc",
                "reset_route_runtime": False,
                "closed_connections": 0,
                "closed_webrtc_peers": 0,
            }
            try:
                reset_ystore_for_webspace(webspace_id)
                ystore_reset = True
            except Exception:
                pass
        except Exception:
            _log.warning(
                "failed to reset runtime state for fresh scenario switch webspace=%s scenario=%s",
                webspace_id,
                target_scenario,
                exc_info=True,
            )
        _record_timing(timings_ms, "fresh_doc_reset_runtime_state", stage_started)

        if _scenario_switch_inline_listing_sync_enabled():
            stage_started = time.perf_counter()
            await _sync_webspace_listing()
            _record_timing(timings_ms, "fresh_doc_sync_listing", stage_started)
        else:
            timings_ms["fresh_doc_sync_listing_deferred"] = 0.0

    if reseed_from_scenario:
        if not target_scenario:
            raise ValueError("scenario_id is required when reseed_from_scenario is enabled")
        stage_started = time.perf_counter()
        try:
            async with _webspace_runtime_async_write_meta(
                root_names=["ui"],
                source="webspace_runtime.reseed_pointer",
            ):
                async with async_get_ydoc(webspace_id) as ydoc:
                    ui_map = ydoc.get_map("ui")
                    with ydoc.begin_transaction() as txn:
                        ui_map.set(txn, "current_scenario", target_scenario)
        except Exception:
            pass
        _record_timing(timings_ms, "reseed_pointer", stage_started)

        stage_started = time.perf_counter()
        try:
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="workspace")
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="dev")
        except Exception:
            pass
        _record_timing(timings_ms, "invalidate_loader_cache", stage_started)

        if requested_action == "reset":
            stage_started = time.perf_counter()
            try:
                from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
                from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

                try:
                    reset_room_result = await reset_live_webspace_room(
                        webspace_id,
                        close_reason="webspace_reset",
                    )
                except Exception:
                    pass
                try:
                    reset_ystore_for_webspace(webspace_id)
                    ystore_reset = True
                except Exception:
                    pass
            except Exception:
                _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)
            _record_timing(timings_ms, "reset_runtime_state", stage_started)

            stage_started = time.perf_counter()
            await _seed_webspace_from_scenario_with_options(
                webspace_id,
                target_scenario,
                emit_event=False,
            )
            _record_timing(timings_ms, "seed_from_scenario", stage_started)
        else:
            stage_started = time.perf_counter()
            await _project_webspace_from_scenario(
                webspace_id,
                target_scenario,
                emit_event=False,
            )
            _record_timing(timings_ms, "project_scenario_payload", stage_started)

        stage_started = time.perf_counter()
        await _sync_webspace_listing()
        _record_timing(timings_ms, "sync_listing", stage_started)

    ctx = get_ctx()
    stage_started = time.perf_counter()
    projection_refresh = await _refresh_projection_rules_for_rebuild(
        ctx,
        webspace_id,
        scenario_id=target_scenario,
        scenario_resolution=resolved_scenario_resolution,
    )
    _record_timing(timings_ms, "projection_refresh", stage_started)
    runtime = WebspaceScenarioRuntime(ctx)
    publish_live_room = _publish_live_room_during_rebuild_enabled()
    try:
        stage_started = time.perf_counter()
        if str(request_id or "").strip():
            entry = await runtime.rebuild_webspace_async(
                webspace_id,
                request_id=request_id,
                publish_live_room=publish_live_room,
                initial_scenario_id=target_scenario if fresh_doc_rebuild else None,
            )
        else:
            entry = await runtime.rebuild_webspace_async(
                webspace_id,
                publish_live_room=publish_live_room,
                initial_scenario_id=target_scenario if fresh_doc_rebuild else None,
            )
        _record_timing(timings_ms, "semantic_rebuild", stage_started)
        stage_started = time.perf_counter()
        _trim_allocator_after_yjs_rebuild()
        _record_timing(timings_ms, "malloc_trim", stage_started)
    except _StaleRebuildRequestError:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=rebuild_started)
        semantic_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_timings_ms", None))
        ydoc_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_ydoc_timings_ms", None))
        resolver_debug = dict(getattr(runtime, "_last_resolver_debug", None) or {})
        apply_summary = dict(getattr(runtime, "_last_apply_summary", None) or {})
        phase_timings = _derive_phase_timings(
            switch_timings_ms=effective_switch_timings,
            rebuild_timings_ms=finalized_timings,
            semantic_rebuild_timings_ms=semantic_timings,
            switch_mode=effective_switch_mode,
        )
        _set_webspace_rebuild_status_if_current(
            webspace_id,
            request_id,
            status="cancelled",
            pending=False,
            finished_at=time.time(),
            error="stale_rebuild_superseded",
            switch_mode=effective_switch_mode,
            scenario_resolution=resolved_scenario_resolution,
            projection_refresh=projection_refresh,
            resolver=resolver_debug or None,
            apply_summary=apply_summary or None,
            timings_ms=finalized_timings,
            switch_timings_ms=effective_switch_timings,
            semantic_rebuild_timings_ms=semantic_timings,
            ydoc_timings_ms=ydoc_timings,
            phase_timings_ms=phase_timings,
        )
        _log.info(
            "stale semantic rebuild skipped apply webspace=%s action=%s scenario=%s request_id=%s",
            webspace_id,
            requested_action,
            target_scenario,
            request_id,
        )
        return {
            "ok": False,
            "accepted": False,
            "action": requested_action,
            "source_of_truth": source_of_truth,
            "webspace_id": webspace_id,
            "scenario_id": target_scenario,
            "scenario_resolution": resolved_scenario_resolution,
            "request_id": request_id,
            "switch_mode": effective_switch_mode,
            "projection_refresh": projection_refresh,
            "resolver": resolver_debug or None,
            "apply_summary": apply_summary or None,
            "timings_ms": finalized_timings,
            "switch_timings_ms": effective_switch_timings,
            "semantic_rebuild_timings_ms": semantic_timings,
            "ydoc_timings_ms": ydoc_timings,
            "phase_timings_ms": phase_timings,
            "error": "stale_rebuild_superseded",
        }
    except Exception:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=rebuild_started)
        semantic_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_timings_ms", None))
        ydoc_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_ydoc_timings_ms", None))
        resolver_debug = dict(getattr(runtime, "_last_resolver_debug", None) or {})
        apply_summary = dict(getattr(runtime, "_last_apply_summary", None) or {})
        phase_timings = _derive_phase_timings(
            switch_timings_ms=effective_switch_timings,
            rebuild_timings_ms=finalized_timings,
            semantic_rebuild_timings_ms=semantic_timings,
            switch_mode=effective_switch_mode,
        )
        _set_webspace_rebuild_status_if_current(
            webspace_id,
            request_id,
            status="failed",
            pending=False,
            finished_at=time.time(),
            error="webspace_rebuild_failed",
            switch_mode=effective_switch_mode,
            scenario_resolution=resolved_scenario_resolution,
            projection_refresh=projection_refresh,
            resolver=resolver_debug or None,
            apply_summary=apply_summary or None,
            timings_ms=finalized_timings,
            switch_timings_ms=effective_switch_timings,
            semantic_rebuild_timings_ms=semantic_timings,
            ydoc_timings_ms=ydoc_timings,
            phase_timings_ms=phase_timings,
        )
        _log.warning(
            "failed to rebuild webspace from sources webspace=%s action=%s scenario=%s timings_ms=%s semantic_timings_ms=%s",
            webspace_id,
            requested_action,
            target_scenario,
            finalized_timings,
            semantic_timings,
            exc_info=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "action": requested_action,
            "source_of_truth": source_of_truth,
            "webspace_id": webspace_id,
            "scenario_id": target_scenario,
            "scenario_resolution": resolved_scenario_resolution,
            "request_id": request_id,
            "switch_mode": effective_switch_mode,
            "projection_refresh": projection_refresh,
            "resolver": resolver_debug or None,
            "apply_summary": apply_summary or None,
            "timings_ms": finalized_timings,
            "switch_timings_ms": effective_switch_timings,
            "semantic_rebuild_timings_ms": semantic_timings,
            "ydoc_timings_ms": ydoc_timings,
            "phase_timings_ms": phase_timings,
            "error": "webspace_rebuild_failed",
        }

    semantic_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_timings_ms", None))
    ydoc_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_ydoc_timings_ms", None))
    resolver_debug = dict(getattr(runtime, "_last_resolver_debug", None) or {})
    apply_summary = dict(getattr(runtime, "_last_apply_summary", None) or {})
    live_room_refresh_result: dict[str, Any] | None = None

    should_refresh_live_room = (
        not publish_live_room
        and _refresh_live_room_after_rebuild_enabled()
        and requested_action
        in {
            "scenario_switch_rebuild",
            "reload",
            "reset",
            "restore",
        }
    )
    if should_refresh_live_room:
        stage_started = time.perf_counter()
        try:
            if requested_action in {"scenario_switch_rebuild", "reload"}:
                from adaos.services.yjs.gateway import refresh_live_webspace_effective_branches  # pylint: disable=import-outside-toplevel

                live_room_refresh_result = await refresh_live_webspace_effective_branches(
                    webspace_id,
                    reason=f"semantic_rebuild:{requested_action}",
                )
            else:
                from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel

                live_room_refresh_result = await reset_live_webspace_room(
                    webspace_id,
                    close_reason=f"semantic_rebuild:{requested_action}",
                )
        except Exception as exc:
            live_room_refresh_result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            _log.warning(
                "failed to refresh live YRoom after detached semantic rebuild webspace=%s action=%s",
                webspace_id,
                requested_action,
                exc_info=True,
            )
        _record_timing(timings_ms, "live_room_refresh", stage_started)

    if not target_scenario or not resolved_scenario_resolution:
        stage_started = time.perf_counter()
        try:
            state_after, resolved_target_scenario, resolved_target_resolution = await _resolve_rebuild_scenario_target(
                webspace_id,
                target_scenario,
                prefer_manifest_home_before_current=requested_action in {"reload", "reset"},
            )
            if not target_scenario:
                target_scenario = resolved_target_scenario
            if not resolved_scenario_resolution:
                resolved_scenario_resolution = resolved_target_resolution
        except Exception:
            target_scenario = target_scenario or None
            resolved_scenario_resolution = resolved_scenario_resolution or None
        _record_timing(timings_ms, "resolve_active_scenario", stage_started)

    should_sync_workflow = requested_action in {"scenario_switch_rebuild", "restore", "reload", "reset"}
    if target_scenario and should_sync_workflow:
        stage_started = time.perf_counter()
        try:
            wf = ScenarioWorkflowRuntime(ctx)
            await wf.sync_workflow_for_webspace(target_scenario, webspace_id)
        except Exception:
            _log.warning(
                "failed to sync workflow during semantic rebuild webspace=%s scenario=%s action=%s",
                webspace_id,
                target_scenario,
                requested_action,
                exc_info=True,
            )
        _record_timing(timings_ms, "workflow_sync", stage_started)

    event_topic = None
    if requested_action in {"reload", "reset"}:
        event_topic = "desktop.webspace.reloaded"
    elif requested_action == "restore":
        event_topic = "desktop.webspace.restored"
    if event_topic:
        stage_started = time.perf_counter()
        try:
            payload: dict[str, Any] = {
                "webspace_id": webspace_id,
                "action": requested_action,
            }
            if target_scenario:
                payload["scenario_id"] = target_scenario
            if isinstance(event_payload, dict):
                payload.update(event_payload)
            emit(ctx.bus, event_topic, payload, "scenario.webspace_runtime")
        except Exception:
            _log.debug("failed to emit %s for webspace=%s", event_topic, webspace_id, exc_info=True)
        _record_timing(timings_ms, "event_emit", stage_started)

    finalized_timings = _finalize_timing_map(timings_ms, started_at=rebuild_started)
    phase_timings = _derive_phase_timings(
        switch_timings_ms=effective_switch_timings,
        rebuild_timings_ms=finalized_timings,
        semantic_rebuild_timings_ms=semantic_timings,
        switch_mode=effective_switch_mode,
    )
    final_rebuild_state = describe_webspace_rebuild_state(webspace_id)
    final_materialization = _copy_materialization_snapshot(
        final_rebuild_state.get("materialization") if isinstance(final_rebuild_state, Mapping) else None
    )
    result = {
        "ok": True,
        "accepted": True,
        "action": requested_action,
        "source_of_truth": source_of_truth,
        "webspace_id": webspace_id,
        "scenario_id": target_scenario,
        "scenario_resolution": resolved_scenario_resolution,
        "request_id": request_id,
        "switch_mode": effective_switch_mode,
        "projection_refresh": projection_refresh,
        "registry_summary": {
            "scenario_id": str(getattr(entry, "scenario_id", target_scenario) or ""),
            "apps": len(getattr(entry, "apps", []) or []),
            "widgets": len(getattr(entry, "widgets", []) or []),
        },
        "resolver": resolver_debug or None,
        "apply_summary": apply_summary or None,
        "timings_ms": finalized_timings,
        "switch_timings_ms": effective_switch_timings,
        "semantic_rebuild_timings_ms": semantic_timings,
        "ydoc_timings_ms": ydoc_timings,
        "phase_timings_ms": phase_timings,
        "materialization": final_materialization,
        "live_room_publish": bool(publish_live_room),
        "live_room_refresh": live_room_refresh_result,
        "fresh_doc_rebuild": bool(fresh_doc_rebuild),
    }
    if requested_action == "reset" or reset_room_result is not None:
        result["reset_room"] = reset_room_result or {
            "webspace_id": webspace_id,
            "room_dropped": False,
        }
        result["ystore_reset"] = bool(ystore_reset)
    _set_webspace_rebuild_status_if_current(
        webspace_id,
        request_id,
        status="ready",
        pending=False,
        finished_at=time.time(),
        error=None,
        switch_mode=effective_switch_mode,
        scenario_id=target_scenario,
        scenario_resolution=resolved_scenario_resolution,
        projection_refresh=projection_refresh,
        registry_summary=result.get("registry_summary"),
        resolver=resolver_debug or None,
        apply_summary=apply_summary or None,
        timings_ms=finalized_timings,
        switch_timings_ms=effective_switch_timings,
        semantic_rebuild_timings_ms=semantic_timings,
        ydoc_timings_ms=ydoc_timings,
        phase_timings_ms=phase_timings,
        materialization=final_materialization,
    )
    _log.info(
        "semantic rebuild completed webspace=%s action=%s scenario=%s timings_ms=%s semantic_timings_ms=%s",
        webspace_id,
        requested_action,
        target_scenario,
        finalized_timings,
        semantic_timings,
    )
    return result


async def _complete_scenario_switch_rebuild_in_subprocess(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
    request_id: str | None = None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    worker_code = r"""
import asyncio
import json
import os
import sys

os.environ["ADAOS_WEBSPACE_SCENARIO_SWITCH_SUBPROCESS"] = "0"
os.environ.setdefault("ADAOS_WEBSPACE_SCENARIO_SWITCH_FRESH_DOC", "1")
os.environ.setdefault("ADAOS_WEBSPACE_SCENARIO_SWITCH_INLINE_LISTING_SYNC", "0")
os.environ.setdefault("ADAOS_WEBSPACE_REBUILD_LIVE_ROOM_UPDATES", "0")
os.environ.setdefault("ADAOS_WEBSPACE_REBUILD_REFRESH_LIVE_ROOM", "0")

from adaos.apps.bootstrap import init_ctx

init_ctx()

from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.services.yjs.store import get_ystore_for_webspace


async def _main() -> None:
    result = await rebuild_webspace_from_sources(
        sys.argv[1],
        action="scenario_switch_rebuild",
        scenario_id=sys.argv[2],
        scenario_resolution=(sys.argv[3] or None),
        source_of_truth="scenario_switch_worker",
        reseed_from_scenario=False,
        request_id=(sys.argv[4] or None),
        switch_mode=(sys.argv[5] or None),
    )
    ystore_persist = {"attempted": False, "reason": "worker_result_not_accepted"}
    if bool(result.get("accepted")):
        ystore_persist = {"attempted": True}
        try:
            store = get_ystore_for_webspace(sys.argv[1])
            await store.backup_to_disk(
                compact_runtime=True,
                backup_kind="scenario_switch_worker",
            )
            snapshot = store.runtime_snapshot()
            snapshot_size = int(snapshot.get("snapshot_file_size") or 0)
            persisted = bool(snapshot.get("snapshot_file_exists")) and snapshot_size > 0
            ystore_persist = {
                "attempted": True,
                "ok": persisted,
                "snapshot_file_exists": bool(snapshot.get("snapshot_file_exists")),
                "snapshot_file_size": snapshot_size,
                "persisted_up_to_date": bool(snapshot.get("persisted_up_to_date")),
                "update_log_entries": int(snapshot.get("update_log_entries") or 0),
                "last_backup_mode": snapshot.get("last_backup_mode"),
            }
            if not persisted:
                result["ok"] = False
                result["accepted"] = False
                result["error"] = "scenario_switch_worker_persist_failed"
        except Exception as exc:
            ystore_persist = {
                "attempted": True,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            result["ok"] = False
            result["accepted"] = False
            result["error"] = "scenario_switch_worker_persist_failed"
    result["ystore_persist"] = ystore_persist
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


asyncio.run(_main())
"""
    cmd = [
        sys.executable,
        "-c",
        worker_code,
        str(webspace_id or "").strip(),
        str(scenario_id or "").strip(),
        str(scenario_resolution or ""),
        str(request_id or ""),
        str(switch_mode or ""),
    ]
    env = os.environ.copy()
    env["ADAOS_WEBSPACE_SCENARIO_SWITCH_SUBPROCESS"] = "0"
    env.setdefault("ADAOS_WEBSPACE_SCENARIO_SWITCH_FRESH_DOC", "1")
    env.setdefault("ADAOS_WEBSPACE_SCENARIO_SWITCH_INLINE_LISTING_SYNC", "0")
    env.setdefault("ADAOS_WEBSPACE_REBUILD_LIVE_ROOM_UPDATES", "0")
    env.setdefault("ADAOS_WEBSPACE_REBUILD_REFRESH_LIVE_ROOM", "0")
    started = time.perf_counter()

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            env=env,
            text=True,
            capture_output=True,
            timeout=_scenario_switch_subprocess_timeout_s(),
            check=False,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "request_id": request_id,
            "switch_mode": switch_mode,
            "error": "scenario_switch_worker_timeout",
            "worker_elapsed_ms": _elapsed_ms(started),
            "worker_stdout": str(getattr(exc, "stdout", "") or "")[-2000:],
            "worker_stderr": str(getattr(exc, "stderr", "") or "")[-2000:],
        }

    parsed: dict[str, Any] | None = None
    for line in reversed((proc.stdout or "").splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            raw = json.loads(text)
            if isinstance(raw, dict):
                parsed = raw
                break
        except Exception:
            continue
    if proc.returncode != 0 or parsed is None:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "request_id": request_id,
            "switch_mode": switch_mode,
            "error": "scenario_switch_worker_failed",
            "worker_returncode": int(proc.returncode),
            "worker_elapsed_ms": _elapsed_ms(started),
            "worker_stdout": str(proc.stdout or "")[-2000:],
            "worker_stderr": str(proc.stderr or "")[-4000:],
        }

    parsed["scenario_switch_worker"] = True
    parsed["worker_elapsed_ms"] = _elapsed_ms(started)
    parsed["switch_timings_ms"] = _copy_timing_map(switch_timings_ms)

    if bool(parsed.get("accepted")):
        try:
            from adaos.services.yjs.gateway import refresh_live_webspace_effective_branches  # pylint: disable=import-outside-toplevel

            parsed["live_room_refresh"] = await refresh_live_webspace_effective_branches(
                webspace_id,
                reason="scenario_switch_worker_persisted_snapshot",
            )
        except Exception as exc:
            parsed["live_room_refresh"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "reset_route_runtime": False,
            }
            _log.warning(
                "failed to refresh live YRoom after scenario switch worker webspace=%s scenario=%s",
                webspace_id,
                scenario_id,
                exc_info=True,
            )
    else:
        parsed["live_room_refresh"] = {
            "ok": False,
            "skipped": True,
            "reason": "scenario_switch_worker_not_accepted",
        }

    try:
        _trim_allocator_after_yjs_rebuild()
    except Exception:
        pass
    try:
        _set_webspace_rebuild_status(
            webspace_id,
            status="ready" if bool(parsed.get("accepted")) else "failed",
            pending=False,
            background=False,
            request_id=request_id,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch_worker",
            scenario_id=scenario_id,
            scenario_resolution=scenario_resolution,
            switch_mode=str(switch_mode or "") or None,
            requested_at=time.time(),
            started_at=None,
            finished_at=time.time(),
            error=None if bool(parsed.get("accepted")) else str(parsed.get("error") or "scenario_switch_worker_failed"),
            projection_refresh=parsed.get("projection_refresh"),
            registry_summary=parsed.get("registry_summary"),
            resolver=parsed.get("resolver"),
            apply_summary=parsed.get("apply_summary"),
            timings_ms=parsed.get("timings_ms"),
            switch_timings_ms=_copy_timing_map(switch_timings_ms),
            semantic_rebuild_timings_ms=parsed.get("semantic_rebuild_timings_ms"),
            ydoc_timings_ms=parsed.get("ydoc_timings_ms"),
            phase_timings_ms=parsed.get("phase_timings_ms"),
            materialization=_copy_materialization_snapshot(parsed.get("materialization")),
        )
    except Exception:
        _log.debug("failed to publish parent rebuild status for scenario switch worker", exc_info=True)
    if bool(parsed.get("accepted")):
        try:
            parsed["post_ready_listing_sync"] = _schedule_webspace_listing_sync(
                reason="scenario_switch_worker_ready",
            )
        except Exception as exc:
            parsed["post_ready_listing_sync"] = {
                "scheduled": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return parsed


async def _complete_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
    request_id: str | None = None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if _scenario_switch_subprocess_enabled():
        return await _complete_scenario_switch_rebuild_in_subprocess(
            webspace_id,
            scenario_id=scenario_id,
            scenario_resolution=scenario_resolution,
            request_id=request_id,
            switch_mode=switch_mode,
            switch_timings_ms=switch_timings_ms,
        )
    return await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_switch_rebuild",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        source_of_truth="scenario_switch",
        reseed_from_scenario=False,
        request_id=request_id,
        switch_mode=switch_mode,
        switch_timings_ms=switch_timings_ms,
    )


def _schedule_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> None:
    request_id = secrets.token_hex(8)
    initial_phase_timings = _derive_phase_timings(
        switch_timings_ms=switch_timings_ms,
        rebuild_timings_ms=None,
        switch_mode=switch_mode,
    )
    initial_materialization = _pending_materialization_snapshot(
        webspace_id,
        scenario_id=scenario_id,
        snapshot_source="rebuild:scheduled",
    )
    _set_webspace_rebuild_status(
        webspace_id,
        status="scheduled",
        pending=True,
        background=True,
        request_id=request_id,
        action="scenario_switch_rebuild",
        source_of_truth="scenario_switch",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        switch_mode=str(switch_mode or "") or None,
        requested_at=time.time(),
        started_at=None,
        finished_at=None,
        error=None,
        projection_refresh=None,
        registry_summary=None,
        resolver=None,
        apply_summary=None,
        timings_ms=None,
        switch_timings_ms=_copy_timing_map(switch_timings_ms),
        semantic_rebuild_timings_ms=None,
        phase_timings_ms=initial_phase_timings,
        materialization=initial_materialization,
    )
    existing = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
    if existing and not existing.done():
        existing.cancel()

    async def _runner() -> None:
        try:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="running",
                pending=True,
                background=True,
                switch_mode=str(switch_mode or "") or None,
                started_at=time.time(),
                finished_at=None,
                error=None,
                projection_refresh=None,
                registry_summary=None,
                resolver=None,
                apply_summary=None,
                timings_ms=None,
                semantic_rebuild_timings_ms=None,
                materialization=_pending_materialization_snapshot(
                    webspace_id,
                    scenario_id=scenario_id,
                    snapshot_source="rebuild:running",
                ),
            )
            result = await _complete_scenario_switch_rebuild(
                webspace_id,
                scenario_id=scenario_id,
                scenario_resolution=scenario_resolution,
                request_id=request_id,
                switch_mode=switch_mode,
                switch_timings_ms=None,
            )
            if not bool(result.get("accepted")):
                if str(result.get("error") or "").strip() == "stale_rebuild_superseded":
                    return
                _set_webspace_rebuild_status_if_current(
                    webspace_id,
                    request_id,
                    status="failed",
                    pending=False,
                    background=True,
                    finished_at=time.time(),
                    error=str(result.get("error") or "scenario_switch_rebuild_failed"),
                    switch_mode=str(switch_mode or "") or None,
                    projection_refresh=result.get("projection_refresh"),
                    resolver=result.get("resolver"),
                    apply_summary=result.get("apply_summary"),
                    timings_ms=_copy_timing_map(result.get("timings_ms")),
                    switch_timings_ms=_copy_timing_map(result.get("switch_timings_ms") or switch_timings_ms),
                    semantic_rebuild_timings_ms=_copy_timing_map(result.get("semantic_rebuild_timings_ms")),
                    phase_timings_ms=_copy_timing_map(result.get("phase_timings_ms")),
                )
                _log.warning(
                    "background scenario switch rebuild rejected webspace=%s scenario=%s error=%s",
                    webspace_id,
                    scenario_id,
                    result.get("error"),
                )
        except asyncio.CancelledError:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="cancelled",
                pending=False,
                background=True,
                finished_at=time.time(),
                error="cancelled",
            )
            raise
        except Exception:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="failed",
                pending=False,
                background=True,
                finished_at=time.time(),
                error="background_scenario_switch_rebuild_failed",
            )
            _log.warning(
                "background scenario switch rebuild failed webspace=%s scenario=%s",
                webspace_id,
                scenario_id,
                exc_info=True,
            )
        finally:
            current = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
            if current is task:
                _SCENARIO_SWITCH_REBUILD_TASKS.pop(webspace_id, None)

    task = asyncio.create_task(
        _runner(),
        name=f"webspace-scenario-switch:{webspace_id}:{scenario_id}",
    )
    _SCENARIO_SWITCH_REBUILD_TASKS[webspace_id] = task


async def reload_webspace_from_scenario(
    webspace_id: str,
    *,
    scenario_id: str | None = None,
    action: str = "reload",
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Re-seed a single webspace from its current or explicit scenario source and
    rebuild its effective UI/runtime projection.

    This is the explicit operator-facing sync recovery path used by event
    handlers as well as local control API endpoints.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    requested_action = "reset" if str(action or "").strip().lower() == "reset" else "reload"
    state, scenario_id, scenario_resolution = await _resolve_reload_scenario_target(webspace_id, scenario_id)
    scenario_id, scenario_resolution, preflight = _preflight_validated_scenario(
        scenario_id,
        source_mode=state.source_mode,
        resolution=scenario_resolution,
    )
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "action": requested_action,
            "webspace_id": webspace_id,
            "scenario_id": None,
            "scenario_resolution": scenario_resolution,
            "kind": state.kind,
            "source_mode": state.source_mode,
            "home_scenario": state.effective_home_scenario,
            "current_scenario_before": state.current_scenario,
            "validation": preflight,
            "error": "scenario_not_found",
        }

    command_trace = _payload_command_trace(event_payload or {})
    recovery_fingerprint = _recovery_request_fingerprint(
        webspace_id=webspace_id,
        action=requested_action,
        scenario_id=scenario_id,
        command_trace=command_trace,
    )
    claimed_command, duplicate_command = _claim_recovery_command_once(
        webspace_id=webspace_id,
        action=requested_action,
        scenario_id=scenario_id,
        cmd_id=command_trace.get("cmd_id"),
        fingerprint=recovery_fingerprint,
    )
    if not claimed_command:
        duplicate_total = int(describe_webspace_rebuild_state(webspace_id).get("recovery_duplicate_total") or 0) + 1
        _set_webspace_rebuild_status(
            webspace_id,
            recovery_fingerprint=recovery_fingerprint,
            recovery_duplicate_total=duplicate_total,
            recovery_last_duplicate_at=time.time(),
            recovery_last_duplicate_reason="duplicate_recovery_command",
            recovery_last_duplicate_age_s=duplicate_command.get("age_s") if isinstance(duplicate_command, dict) else None,
            recovery_last_command_client=command_trace.get("gateway_client"),
            recovery_last_command_id=command_trace.get("cmd_id"),
            recovery_last_command_seq=int(command_trace.get("gateway_command_seq") or 0),
        )
        _log.warning(
            "deduplicated webspace recovery command webspace=%s action=%s scenario=%s cmd=%s seq=%s client=%s fp=%s age_s=%s ttl_s=%s dup_total=%s",
            webspace_id,
            requested_action,
            scenario_id,
            command_trace.get("cmd_id") or "-",
            command_trace.get("gateway_command_seq") or 0,
            command_trace.get("gateway_client") or "-",
            recovery_fingerprint,
            duplicate_command.get("age_s") if isinstance(duplicate_command, dict) else "-",
            duplicate_command.get("ttl_s") if isinstance(duplicate_command, dict) else "-",
            duplicate_total,
        )
        return {
            "ok": True,
            "accepted": True,
            "deduplicated": True,
            "skip_reason": "duplicate_recovery_command",
            "action": requested_action,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "kind": state.kind,
            "source_mode": state.source_mode,
            "home_scenario": state.effective_home_scenario,
            "current_scenario_before": state.current_scenario,
            "recovery_fingerprint": recovery_fingerprint,
            "recovery_duplicate_total": duplicate_total,
            "duplicate_age_s": duplicate_command.get("age_s") if isinstance(duplicate_command, dict) else None,
            "rebuild": describe_webspace_rebuild_state(webspace_id),
        }

    rebuild_state_before = describe_webspace_rebuild_state(webspace_id)
    duplicate_window_s = _reload_dedupe_window_s()
    previous_action = str(rebuild_state_before.get("action") or "").strip().lower()
    previous_scenario = str(rebuild_state_before.get("scenario_id") or "").strip() or None
    previous_fingerprint = str(rebuild_state_before.get("recovery_fingerprint") or "").strip()
    previous_pending = bool(rebuild_state_before.get("pending"))
    previous_status = str(rebuild_state_before.get("status") or "").strip().lower()
    previous_updated_at = rebuild_state_before.get("updated_at")
    if previous_updated_at is None:
        previous_updated_at = rebuild_state_before.get("finished_at")
    if previous_updated_at is None:
        previous_updated_at = rebuild_state_before.get("started_at")
    previous_age_s: float | None = None
    try:
        if previous_updated_at is not None:
            previous_age_s = round(max(0.0, time.time() - float(previous_updated_at)), 3)
    except Exception:
        previous_age_s = None
    pending_stale_after_s = _reload_pending_stale_after_s()
    previous_pending_stale = bool(
        previous_pending
        and pending_stale_after_s > 0.0
        and previous_age_s is not None
        and previous_age_s >= pending_stale_after_s
    )

    duplicate_reason: str | None = None
    if (
        previous_action == requested_action
        and previous_scenario == scenario_id
        and previous_fingerprint
        and previous_fingerprint == recovery_fingerprint
    ):
        if previous_pending and not previous_pending_stale:
            duplicate_reason = "already_pending_recovery"
        elif (
            duplicate_window_s > 0.0
            and previous_age_s is not None
            and previous_age_s <= duplicate_window_s
            and previous_status in {"running", "ready", "scheduled"}
        ):
            duplicate_reason = "duplicate_recovery_request"

    if previous_pending_stale:
        _log.warning(
            "stale pending webspace recovery will be superseded webspace=%s action=%s scenario=%s prev_status=%s age_s=%s stale_after_s=%s fp=%s",
            webspace_id,
            requested_action,
            scenario_id,
            previous_status or "-",
            previous_age_s if previous_age_s is not None else "-",
            pending_stale_after_s,
            previous_fingerprint or "-",
        )

    if duplicate_reason:
        duplicate_total = int(rebuild_state_before.get("recovery_duplicate_total") or 0) + 1
        duplicate_now = time.time()
        _set_webspace_rebuild_status(
            webspace_id,
            recovery_fingerprint=recovery_fingerprint,
            recovery_duplicate_total=duplicate_total,
            recovery_last_duplicate_at=duplicate_now,
            recovery_last_duplicate_reason=duplicate_reason,
            recovery_last_duplicate_age_s=previous_age_s,
            recovery_last_command_client=command_trace.get("gateway_client"),
            recovery_last_command_id=command_trace.get("cmd_id"),
            recovery_last_command_seq=int(command_trace.get("gateway_command_seq") or 0),
        )
        _log.warning(
            "deduplicated webspace recovery webspace=%s action=%s scenario=%s reason=%s prev_status=%s age_s=%s cmd=%s seq=%s client=%s fp=%s dup_total=%s",
            webspace_id,
            requested_action,
            scenario_id,
            duplicate_reason,
            previous_status or "-",
            previous_age_s if previous_age_s is not None else "-",
            command_trace.get("cmd_id") or "-",
            command_trace.get("gateway_command_seq") or 0,
            command_trace.get("gateway_client") or "-",
            recovery_fingerprint,
            duplicate_total,
        )
        return {
            "ok": True,
            "accepted": True,
            "deduplicated": True,
            "skip_reason": duplicate_reason,
            "action": requested_action,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_resolution": scenario_resolution,
            "kind": state.kind,
            "source_mode": state.source_mode,
            "home_scenario": state.effective_home_scenario,
            "current_scenario_before": state.current_scenario,
            "recovery_fingerprint": recovery_fingerprint,
            "recovery_duplicate_total": duplicate_total,
            "duplicate_age_s": previous_age_s,
            "rebuild": describe_webspace_rebuild_state(webspace_id),
        }

    _set_webspace_rebuild_status(
        webspace_id,
        recovery_fingerprint=recovery_fingerprint,
        recovery_last_command_client=command_trace.get("gateway_client"),
        recovery_last_command_id=command_trace.get("cmd_id"),
        recovery_last_command_seq=int(command_trace.get("gateway_command_seq") or 0),
    )

    verb = "resetting" if requested_action == "reset" else "reloading"
    _log.info(
        "%s webspace %s from scenario %s (resolution=%s kind=%s source_mode=%s current=%s home=%s cmd=%s seq=%s client=%s device=%s trace=%s fp=%s)",
        verb,
        webspace_id,
        scenario_id,
        scenario_resolution,
        state.kind,
        state.source_mode,
        state.current_scenario,
        state.effective_home_scenario,
        command_trace.get("cmd_id") or "-",
        command_trace.get("gateway_command_seq") or 0,
        command_trace.get("gateway_client") or "-",
        command_trace.get("device_id") or "-",
        command_trace.get("trace_id") or "-",
        recovery_fingerprint,
    )

    result = await rebuild_webspace_from_sources(
        webspace_id,
        action=requested_action,
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        source_of_truth="scenario",
        reseed_from_scenario=True,
        event_payload=event_payload,
    )
    result.update(
        {
            "kind": state.kind,
            "source_mode": state.source_mode,
            "home_scenario": state.effective_home_scenario,
            "current_scenario_before": state.current_scenario,
            "validation": preflight,
        }
    )
    return result


async def restore_webspace_from_snapshot(webspace_id: str) -> dict[str, Any]:
    """
    Restore a webspace from its latest persisted YStore snapshot and reconcile
    its materialized effective UI/runtime projection.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
    from adaos.services.yjs.store import restore_ystore_for_webspace  # pylint: disable=import-outside-toplevel

    restore_result = await restore_ystore_for_webspace(webspace_id)
    if not bool(restore_result.get("accepted")):
        return restore_result

    reset_result: dict[str, Any] = {}
    try:
        reset_result = await reset_live_webspace_room(webspace_id, close_reason="webspace_restore")
    except Exception:
        _log.warning("failed to reset live room before restore for webspace=%s", webspace_id, exc_info=True)

    rebuild_result = await rebuild_webspace_from_sources(
        webspace_id,
        action="restore",
        source_of_truth="snapshot",
        reseed_from_scenario=False,
        event_payload={"snapshot_path": str(restore_result.get("snapshot_path") or "")},
    )

    return {
        **restore_result,
        **rebuild_result,
        "action": "restore",
        "source_of_truth": "snapshot",
        "reset_room": reset_result,
    }


async def switch_webspace_scenario(
    webspace_id: str,
    scenario_id: str,
    *,
    set_home: bool | None = None,
    wait_for_rebuild: bool = True,
) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    scenario_id = str(scenario_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    if not scenario_id:
        raise ValueError("scenario_id is required")

    switch_started = time.perf_counter()
    timings_ms: Dict[str, float] = {}
    stage_started = time.perf_counter()
    state_before = await describe_webspace_operational_state(webspace_id)
    _record_timing(timings_ms, "describe_state_before", stage_started)

    stage_started = time.perf_counter()
    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    resolved_set_home = bool(set_home) if set_home is not None else bool(row.is_dev or row.effective_source_mode == "dev")
    _record_timing(timings_ms, "resolve_manifest_policy", stage_started)
    stage_started = time.perf_counter()
    rebuild_state_before = describe_webspace_rebuild_state(webspace_id)
    _record_timing(timings_ms, "describe_rebuild_before", stage_started)

    _log.info(
        "desktop.scenario.set webspace=%s scenario=%s requested_set_home=%s resolved_set_home=%s",
        webspace_id,
        scenario_id,
        set_home,
        resolved_set_home,
    )
    switch_mode = _scenario_switch_mode()
    loader_space = "workspace"
    try:
        if row:
            loader_space = row.effective_source_mode
    except Exception:
        loader_space = "workspace"
    switch_content: Dict[str, Any] | None = None

    def _build_switch_skip_result(*, skip_reason: str, rebuild_state: Mapping[str, Any], background_rebuild: bool) -> dict[str, Any]:
        phase_timings = _copy_timing_map(rebuild_state.get("phase_timings_ms"))
        if not phase_timings:
            phase_timings = _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                rebuild_timings_ms=_copy_timing_map(rebuild_state.get("timings_ms")),
                semantic_rebuild_timings_ms=_copy_timing_map(rebuild_state.get("semantic_rebuild_timings_ms")),
                switch_mode="noop",
            )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "kind": row.effective_kind,
            "source_mode": row.effective_source_mode,
            "current_scenario_before": state_before.current_scenario,
            "home_scenario_before": state_before.effective_home_scenario,
            "home_scenario": row.effective_home_scenario,
            "set_home": resolved_set_home,
            "background_rebuild": background_rebuild,
            "scenario_switch_mode": switch_mode,
            "switch_skipped": True,
            "skip_reason": skip_reason,
            "timings_ms": finalized_timings,
            "rebuild_timings_ms": _copy_timing_map(rebuild_state.get("timings_ms")),
            "semantic_rebuild_timings_ms": _copy_timing_map(rebuild_state.get("semantic_rebuild_timings_ms")),
            "resolver": dict(rebuild_state.get("resolver") or {})
            if isinstance(rebuild_state.get("resolver"), Mapping)
            else None,
            "apply_summary": dict(rebuild_state.get("apply_summary") or {})
            if isinstance(rebuild_state.get("apply_summary"), Mapping)
            else None,
            "phase_timings_ms": phase_timings,
        }

    if (
        str(state_before.current_scenario or "").strip() == scenario_id
        and not bool(rebuild_state_before.get("pending"))
        and str(rebuild_state_before.get("status") or "").strip().lower() == "ready"
        and str(rebuild_state_before.get("scenario_id") or "").strip() == scenario_id
    ):
        if resolved_set_home and row.effective_home_scenario != scenario_id:
            stage_started = time.perf_counter()
            row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
            _record_timing(timings_ms, "persist_home_scenario", stage_started)

            stage_started = time.perf_counter()
            await _sync_webspace_listing()
            _record_timing(timings_ms, "sync_listing", stage_started)

        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _log.info(
            "desktop.scenario.set skipped webspace=%s scenario=%s mode=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            finalized_timings,
        )
        return _build_switch_skip_result(
            skip_reason="already_current_ready",
            rebuild_state=rebuild_state_before,
            background_rebuild=False,
        )

    if (
        str(state_before.current_scenario or "").strip() == scenario_id
        and bool(rebuild_state_before.get("pending"))
        and str(rebuild_state_before.get("scenario_id") or "").strip() == scenario_id
    ):
        if resolved_set_home and row.effective_home_scenario != scenario_id:
            stage_started = time.perf_counter()
            row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
            _record_timing(timings_ms, "persist_home_scenario", stage_started)

            stage_started = time.perf_counter()
            await _sync_webspace_listing()
            _record_timing(timings_ms, "sync_listing", stage_started)

        if wait_for_rebuild:
            existing_task = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
            if existing_task and not existing_task.done():
                stage_started = time.perf_counter()
                try:
                    await asyncio.shield(existing_task)
                except Exception:
                    pass
                _record_timing(timings_ms, "wait_existing_rebuild", stage_started)
                rebuild_state_before = describe_webspace_rebuild_state(webspace_id)

        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _log.info(
            "desktop.scenario.set deduplicated webspace=%s scenario=%s mode=%s pending=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            bool(rebuild_state_before.get("pending")),
            finalized_timings,
        )
        return _build_switch_skip_result(
            skip_reason="already_pending_rebuild",
            rebuild_state=rebuild_state_before,
            background_rebuild=bool(rebuild_state_before.get("pending") or (not wait_for_rebuild and rebuild_state_before.get("background"))),
        )

    stage_started = time.perf_counter()
    scenario_exists = _scenario_exists_for_switch(scenario_id, space=loader_space)
    _record_timing(timings_ms, "validate_scenario", stage_started)
    if not scenario_exists:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            background=not wait_for_rebuild,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch",
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            requested_at=time.time(),
            finished_at=time.time(),
            error="scenario_not_found",
            projection_refresh=None,
            registry_summary=None,
            resolver=None,
            apply_summary=None,
            timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_not_found",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    try:
        stage_started = time.perf_counter()

        def _mutator(doc: Any, txn: Any) -> None:
            ui_map = doc.get_map("ui")
            _set_map_value_if_changed(ui_map, txn, "current_scenario", scenario_id)

        live_applied = mutate_live_room(
            webspace_id,
            _mutator,
            root_names=["ui"],
            source="webspace_runtime.switch_pointer",
            owner="core:webspace_runtime",
            channel="core.webspace_runtime.live_room",
        )
        if live_applied:
            _record_timing(timings_ms, "write_switch_pointer", stage_started)
        else:
            stage_started = time.perf_counter()
            async with _webspace_runtime_async_write_meta(
                root_names=["ui"],
                source="webspace_runtime.switch_pointer",
            ):
                async with async_get_ydoc(webspace_id) as ydoc:
                    _record_timing(timings_ms, "open_doc", stage_started)
                    ui_map = ydoc.get_map("ui")
                    stage_started = time.perf_counter()
                    with ydoc.begin_transaction() as txn:
                        _set_map_value_if_changed(ui_map, txn, "current_scenario", scenario_id)
                    _record_timing(timings_ms, "write_switch_pointer", stage_started)
    except Exception:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            background=not wait_for_rebuild,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch",
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            requested_at=time.time(),
            finished_at=time.time(),
            error="scenario_switch_failed",
            projection_refresh=None,
            registry_summary=None,
            resolver=None,
            apply_summary=None,
            timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        _log.warning(
            "failed to switch scenario for webspace=%s scenario=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            finalized_timings,
            exc_info=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_switch_failed",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    try:
        from adaos.services.yjs.gateway_ws import (  # pylint: disable=import-outside-toplevel
            note_authoritative_current_scenario,
        )

        note_authoritative_current_scenario(
            webspace_id,
            scenario_id,
            reason="scenario_switch",
        )
    except Exception:
        _log.debug("failed to publish authoritative current_scenario lease", exc_info=True)

    stage_started = time.perf_counter()
    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    _record_timing(timings_ms, "refresh_manifest_row", stage_started)
    if resolved_set_home:
        stage_started = time.perf_counter()
        row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
        _record_timing(timings_ms, "persist_home_scenario", stage_started)

        stage_started = time.perf_counter()
        await _sync_webspace_listing()
        _record_timing(timings_ms, "sync_listing", stage_started)

    if not wait_for_rebuild:
        scheduled_switch_timings = _finalize_timing_map(dict(timings_ms), started_at=switch_started)
        stage_started = time.perf_counter()
        _schedule_scenario_switch_rebuild(
            webspace_id,
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            switch_timings_ms=scheduled_switch_timings,
        )
        _record_timing(timings_ms, "schedule_background_rebuild", stage_started)
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        current_status = describe_webspace_rebuild_state(webspace_id)
        _set_webspace_rebuild_status_if_current(
            webspace_id,
            str(current_status.get("request_id") or "").strip() or None,
            switch_timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        _log.info(
            "desktop.scenario.set accepted webspace=%s scenario=%s mode=%s background=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            True,
            finalized_timings,
        )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "kind": row.effective_kind,
            "source_mode": row.effective_source_mode,
            "current_scenario_before": state_before.current_scenario,
            "home_scenario_before": state_before.effective_home_scenario,
            "home_scenario": row.effective_home_scenario,
            "set_home": resolved_set_home,
            "background_rebuild": True,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    stage_started = time.perf_counter()
    rebuild_result = await _complete_scenario_switch_rebuild(
        webspace_id,
        scenario_id=scenario_id,
        scenario_resolution="explicit",
        switch_mode=switch_mode,
        switch_timings_ms=_finalize_timing_map(dict(timings_ms), started_at=switch_started),
    )
    _record_timing(timings_ms, "wait_rebuild", stage_started)
    if not bool(rebuild_result.get("accepted")):
        final_switch_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        rebuild_result["switch_timings_ms"] = final_switch_timings
        rebuild_result["phase_timings_ms"] = _derive_phase_timings(
            switch_timings_ms=final_switch_timings,
            rebuild_timings_ms=rebuild_result.get("timings_ms"),
            semantic_rebuild_timings_ms=rebuild_result.get("semantic_rebuild_timings_ms"),
            switch_mode=switch_mode,
        )
        return rebuild_result

    finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
    phase_timings = _derive_phase_timings(
        switch_timings_ms=finalized_timings,
        rebuild_timings_ms=rebuild_result.get("timings_ms"),
        semantic_rebuild_timings_ms=rebuild_result.get("semantic_rebuild_timings_ms"),
        switch_mode=switch_mode,
    )
    _log.info(
        "desktop.scenario.set completed webspace=%s scenario=%s mode=%s background=%s timings_ms=%s rebuild_timings_ms=%s",
        webspace_id,
        scenario_id,
        switch_mode,
        False,
        finalized_timings,
        rebuild_result.get("timings_ms"),
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_id,
        "scenario_id": scenario_id,
        "kind": row.effective_kind,
        "source_mode": row.effective_source_mode,
        "current_scenario_before": state_before.current_scenario,
        "home_scenario_before": state_before.effective_home_scenario,
        "home_scenario": row.effective_home_scenario,
        "set_home": resolved_set_home,
        "background_rebuild": False,
        "scenario_switch_mode": switch_mode,
        "timings_ms": finalized_timings,
        "rebuild_timings_ms": _copy_timing_map(rebuild_result.get("timings_ms")),
        "semantic_rebuild_timings_ms": _copy_timing_map(rebuild_result.get("semantic_rebuild_timings_ms")),
        "scenario_switch_worker": bool(rebuild_result.get("scenario_switch_worker")),
        "worker_elapsed_ms": rebuild_result.get("worker_elapsed_ms"),
        "ystore_persist": rebuild_result.get("ystore_persist"),
        "live_room_publish": rebuild_result.get("live_room_publish"),
        "live_room_refresh": rebuild_result.get("live_room_refresh"),
        "post_ready_listing_sync": rebuild_result.get("post_ready_listing_sync"),
        "fresh_doc_rebuild": rebuild_result.get("fresh_doc_rebuild"),
        "resolver": dict(rebuild_result.get("resolver") or {})
        if isinstance(rebuild_result.get("resolver"), Mapping)
        else None,
        "apply_summary": dict(rebuild_result.get("apply_summary") or {})
        if isinstance(rebuild_result.get("apply_summary"), Mapping)
        else None,
        "phase_timings_ms": phase_timings,
    }


async def go_home_webspace(webspace_id: str, *, wait_for_rebuild: bool = True) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    state = await describe_webspace_operational_state(webspace_id)
    scenario_id, scenario_resolution, preflight = _preflight_validated_scenario(
        state.effective_home_scenario,
        source_mode=state.source_mode,
        resolution="manifest_home",
    )
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "action": "go_home",
            "source_of_truth": "manifest_home_scenario",
            "webspace_id": webspace_id,
            "scenario_id": None,
            "scenario_resolution": scenario_resolution,
            "validation": preflight,
            "error": "scenario_not_found",
        }
    result = await switch_webspace_scenario(
        webspace_id,
        scenario_id,
        set_home=False,
        wait_for_rebuild=wait_for_rebuild,
    )
    result["action"] = "go_home"
    result["source_of_truth"] = "manifest_home_scenario"
    result["scenario_resolution"] = scenario_resolution
    result["validation"] = preflight
    return result


async def set_current_webspace_home(webspace_id: str) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    state = await describe_webspace_operational_state(webspace_id)
    scenario_id = str(state.current_scenario or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "action": "set_home_current",
            "source_of_truth": "current_scenario",
            "webspace_id": webspace_id,
            "scenario_id": None,
            "current_scenario": None,
            "home_scenario_before": state.effective_home_scenario,
            "error": "current_scenario_unavailable",
        }
    svc = WebspaceService(get_ctx())
    info = await svc.set_home_scenario(webspace_id, scenario_id, home_scenario_ref=None)
    if info is None:
        return {
            "ok": False,
            "accepted": False,
            "action": "set_home_current",
            "source_of_truth": "current_scenario",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "current_scenario": scenario_id,
            "home_scenario_before": state.effective_home_scenario,
            "error": "webspace_not_found",
        }
    return {
        "ok": True,
        "accepted": True,
        "action": "set_home_current",
        "source_of_truth": "current_scenario",
        "webspace_id": info.id,
        "scenario_id": scenario_id,
        "current_scenario": scenario_id,
        "home_scenario_before": state.effective_home_scenario,
        "home_scenario": info.home_scenario,
        "changed": str(state.effective_home_scenario or "").strip() != str(info.home_scenario or "").strip(),
        "kind": info.kind,
        "source_mode": info.source_mode,
    }


async def ensure_dev_webspace_for_scenario(
    scenario_id: str,
    *,
    requested_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    scenario_id = str(scenario_id or "").strip()
    if not scenario_id:
        raise ValueError("scenario_id is required")
    svc = WebspaceService(get_ctx())
    info, created = await svc.ensure_dev_for_scenario(
        scenario_id,
        requested_id=requested_id,
        title=title,
    )
    return {
        "ok": True,
        "accepted": True,
        "created": created,
        "webspace_id": info.id,
        "scenario_id": scenario_id,
        "home_scenario": info.home_scenario,
        "kind": info.kind,
        "source_mode": info.source_mode,
    }


async def reload_preview_webspaces_for_project(
    object_type: str,
    object_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    object_type = str(object_type or "").strip().lower()
    object_id = str(object_id or "").strip()
    if object_type not in {"scenario", "skill"} or not object_id:
        return {
            "ok": False,
            "accepted": False,
            "error": "project_identity_required",
        }

    targets: list[tuple[str, str]] = []
    for row in workspace_index.list_workspaces():
        if not row.is_dev:
            continue
        home_scenario = str(row.effective_home_scenario or "").strip()
        if not home_scenario:
            continue
        if object_type == "scenario":
            if home_scenario == object_id:
                targets.append((row.workspace_id, home_scenario))
            continue
        try:
            manifest = scenarios_loader.read_manifest(home_scenario, space=row.effective_source_mode)
            depends_raw = manifest.get("depends") or []
            depends = {
                str(item).strip()
                for item in depends_raw
                if str(item).strip()
            }
            if object_id in depends:
                targets.append((row.workspace_id, home_scenario))
        except Exception:
            _log.debug(
                "failed to resolve scenario depends for preview webspace=%s home=%s",
                row.workspace_id,
                home_scenario,
                exc_info=True,
            )

    reloaded: list[str] = []
    failed: list[str] = []
    for webspace_id, scenario_id in targets:
        try:
            await reload_webspace_from_scenario(
                webspace_id,
                scenario_id=scenario_id,
                action="reload",
            )
            reloaded.append(webspace_id)
        except Exception:
            failed.append(webspace_id)
            _log.warning(
                "failed to reload preview webspace=%s for %s:%s reason=%s",
                webspace_id,
                object_type,
                object_id,
                reason,
                exc_info=True,
            )

    return {
        "ok": not failed,
        "accepted": bool(targets),
        "object_type": object_type,
        "object_id": object_id,
        "reason": str(reason or "").strip() or None,
        "reloaded_webspaces": reloaded,
        "failed_webspaces": failed,
    }


@subscribe("desktop.webspace.reload")
async def _on_webspace_reload(evt: Dict[str, Any]) -> None:
    """
    Re-seed the current webspace from its scenario, effectively
    rebuilding ui/data/registry for debugging or recovery.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    recreate_room = bool(payload.get("recreate_room"))
    await reload_webspace_from_scenario(
        webspace_id,
        scenario_id=str(payload.get("scenario_id") or "").strip() or None,
        action="reset" if recreate_room else "reload",
        event_payload=payload,
    )


@subscribe("desktop.webspace.reset")
async def _on_webspace_reset(evt: Dict[str, Any]) -> None:
    """
    Hard reset of the current webspace from its scenario.

    Unlike desktop.webspace.reload, this recovery path intentionally resets
    the live room and persisted YStore before reseeding the scenario payload.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await reload_webspace_from_scenario(
        webspace_id,
        scenario_id=str(payload.get("scenario_id") or "").strip() or None,
        action="reset",
        event_payload=payload,
    )


@subscribe("desktop.webspace.go_home")
async def _on_webspace_go_home(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    wait_for_rebuild = bool(payload.get("wait_for_rebuild")) if "wait_for_rebuild" in payload else True
    await go_home_webspace(webspace_id, wait_for_rebuild=wait_for_rebuild)


@subscribe("desktop.webspace.set_home")
async def _on_webspace_set_home(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    scenario_id = str(payload.get("scenario_id") or "").strip()
    if not webspace_id or not scenario_id:
        return
    svc = WebspaceService(get_ctx())
    home_scenario_ref = (
        payload.get("home_scenario_ref")
        if "home_scenario_ref" in payload
        else _HOME_SCENARIO_REF_UNSET
    )
    await svc.set_home_scenario(webspace_id, scenario_id, home_scenario_ref=home_scenario_ref)


@subscribe("desktop.webspace.set_home_current")
async def _on_webspace_set_home_current(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await set_current_webspace_home(webspace_id)


@subscribe("desktop.scenario.set")
async def _on_desktop_scenario_set(evt: Dict[str, Any]) -> None:
    """
    Switch the current desktop scenario for a webspace and re-sync the
    target YDoc from the selected scenario package.

    Payload:
      - scenario_id: id of the desktop scenario (required)
      - webspace_id / workspace_id: optional, defaults to current/default.
    """
    payload = _payload(evt)
    scenario_id = str(payload.get("scenario_id") or "").strip()
    if not scenario_id:
        return
    webspace_id = _webspace_id(payload)
    set_home: bool | None = None
    if "set_home" in payload:
        set_home = bool(payload.get("set_home"))
    elif "persist_home" in payload:
        set_home = bool(payload.get("persist_home"))
    wait_for_rebuild = bool(payload.get("wait_for_rebuild")) if "wait_for_rebuild" in payload else False
    await switch_webspace_scenario(
        webspace_id,
        scenario_id,
        set_home=set_home,
        wait_for_rebuild=wait_for_rebuild,
    )


@subscribe("prompt.project.changed")
async def _on_prompt_project_changed(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    object_type = str(payload.get("object_type") or "").strip().lower()
    object_id = str(payload.get("object_id") or "").strip()
    if object_type not in {"scenario", "skill"} or not object_id:
        return
    await reload_preview_webspaces_for_project(
        object_type,
        object_id,
        reason=str(payload.get("reason") or "").strip() or None,
    )


__all__ = [
    "WebUIRegistryEntry",
    "WebspaceResolverInputs",
    "WebspaceResolverOutputs",
    "WebspaceScenarioRuntime",
    "describe_webspace_operational_state",
    "describe_webspace_validation_state",
    "describe_webspace_overlay_state",
    "describe_webspace_projection_state",
    "describe_webspace_rebuild_state",
    "set_current_webspace_home",
    "rebuild_webspace_from_sources",
]
