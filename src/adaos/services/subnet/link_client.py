from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Callable

import requests
import websockets  # type: ignore
import y_py as Y

from adaos.apps.cli.active_control import resolve_control_token
from adaos.build_info import BUILD_INFO
from adaos.domain import Event as DomainEvent
from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.core_update_policy import core_update_reactions_disabled_reason
from adaos.services.node_config import load_config, normalize_node_names, set_node_names as persist_node_names
from adaos.services.node_runtime_state import save_node_runtime_state
from adaos.services.node_runtime_state import load_member_hub_token
from adaos.services.capacity import get_local_capacity
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.skill.manager import SkillManager
from adaos.services.yjs.doc import apply_update_to_live_room
from adaos.services.yjs.store import add_ystore_write_listener, get_ystore_for_webspace, suppress_ystore_write_notifications

_log = logging.getLogger("adaos.subnet.client")


def _resolve_member_hub_token(conf) -> str:
    token = load_member_hub_token()
    if token:
        return token
    return str(getattr(conf, "token", "") or "dev-local-token").strip() or "dev-local-token"


def _to_ws_url(http_base: str, path: str) -> str:
    u = urllib.parse.urlparse(str(http_base or "").strip())
    if u.scheme in ("http", "https"):
        scheme = "wss" if u.scheme == "https" else "ws"
        netloc = u.netloc
        base_path = u.path
    else:
        # tolerate bare host:port or host
        scheme = "ws"
        netloc = u.path
        base_path = ""
    full_path = (base_path.rstrip("/") + "/" + path.lstrip("/")).rstrip("/")
    return urllib.parse.urlunparse((scheme, netloc, full_path, "", "", ""))


def _member_link_transition_snapshot() -> dict[str, Any]:
    update_status = read_core_update_status() or {}
    lifecycle = runtime_lifecycle_snapshot()
    status = update_status if isinstance(update_status, dict) else {}
    runtime = lifecycle if isinstance(lifecycle, dict) else {}
    state = str(status.get("state") or "").strip().lower()
    phase = str(status.get("phase") or "").strip().lower()
    node_state = str(runtime.get("node_state") or "").strip().lower()
    lifecycle_reason = str(runtime.get("reason") or "").strip().lower()
    draining = bool(runtime.get("draining"))
    transition_state = "ready"
    reason = "none"
    if state in {"preparing", "countdown", "draining", "stopping", "applying"}:
        transition_state = "paused_for_update"
        reason = state
    elif state == "restarting" or phase in {"launch", "root_promoted"}:
        transition_state = "restarting"
        reason = state or phase or "restarting"
    elif state == "validated" and phase == "root_promotion_pending":
        transition_state = "waiting_restart"
        reason = "root_promotion_pending"
    elif draining or node_state in {"stopping", "stopped", "restarting"}:
        transition_state = "waiting_restart"
        reason = lifecycle_reason or node_state or "draining"
    return {
        "transition_state": transition_state,
        "reason": reason,
        "update_state": state or None,
        "update_phase": phase or None,
    }


class MemberLinkClient:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self._task: asyncio.Task | None = None
        self._remove_ystore_listener: Callable[[], None] | None = None
        self._bus_subscribed = False
        self._yjs_enabled = os.getenv("ADAOS_SUBNET_YJS_REPLICATION", "1").strip().lower() not in ("0", "false", "no")
        self._bus_prefixes = self._parse_bus_prefixes(os.getenv("ADAOS_SUBNET_BUS_FORWARD_PREFIXES", "io.out.,ui."))
        self._connected_at = 0.0
        self._last_message_at = 0.0
        self._last_pong_at = 0.0
        self._ws_url = ""
        self._hub_node_id = ""
        self._last_hub_event_type = ""
        self._last_hub_event_at = 0.0
        self._last_hub_core_update: dict[str, Any] = {}
        self._last_follow_key = ""
        self._last_follow_result: dict[str, Any] = {}
        self._last_follow_error = ""
        self._last_follow_at = 0.0
        self._last_control_request: dict[str, Any] = {}
        self._last_control_result: dict[str, Any] = {}
        self._last_control_error = ""
        self._last_control_requested_at = 0.0
        self._last_control_completed_at = 0.0
        self._last_forced_snapshot_at = 0.0
        self._last_yjs_write_snapshot_at = 0.0
        self._yjs_write_seen_total = 0
        self._yjs_write_queued_total = 0
        self._yjs_write_drop_disconnected_total = 0
        self._yjs_write_drop_encode_total = 0
        self._yjs_write_drop_queue_total = 0
        self._yjs_sent_total = 0
        self._yjs_send_failed_total = 0
        self._yjs_received_total = 0
        self._yjs_received_bytes = 0
        self._yjs_snapshot_queued_total = 0
        self._yjs_snapshot_failed_total = 0
        self._yjs_snapshot_bytes = 0
        self._last_yjs_write_at = 0.0
        self._last_yjs_sent_at = 0.0
        self._last_yjs_received_at = 0.0
        self._last_yjs_snapshot_at = 0.0
        self._last_yjs_write_webspace_id = ""
        self._last_yjs_write_bytes = 0
        self._last_yjs_snapshot_webspace_id = ""
        self._last_yjs_snapshot_reason = ""
        self._last_yjs_queue_size = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._last_connect_full_snapshot_at = 0.0
        self._last_connect_yjs_state_at = 0.0
        self._link_session_end_total = 0
        self._last_link_session_end_log_at = 0.0

    @staticmethod
    def _pong_stale_after_s() -> float:
        raw = str(os.getenv("ADAOS_SUBNET_PONG_STALE_AFTER_S") or "").strip()
        try:
            value = float(raw or 35.0)
        except Exception:
            value = 35.0
        return max(15.0, value)

    @staticmethod
    def _parse_bus_prefixes(raw: str | None) -> list[str] | None:
        txt = str(raw or "").strip()
        if not txt:
            return ["io.out.", "ui."]
        if txt in ("*", "all"):
            return None
        parts = [p.strip() for p in txt.split(",") if p.strip()]
        return parts or ["io.out.", "ui."]

    def is_connected(self) -> bool:
        if not self._connected.is_set():
            return False
        last_activity_at = max(
            float(self._last_pong_at or 0.0),
            float(self._last_message_at or 0.0),
            float(self._connected_at or 0.0),
        )
        if last_activity_at <= 0.0:
            return False
        try:
            stale_after_s = self._pong_stale_after_s()
        except Exception:
            stale_after_s = 35.0
        return (time.time() - last_activity_at) <= max(15.0, stale_after_s)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        last_hub_core_update = (
            dict(self._last_hub_core_update)
            if isinstance(self._last_hub_core_update, dict)
            else {}
        )
        transition = _member_link_transition_snapshot()
        return {
            "role": "member",
            "connected": self.is_connected(),
            "ws_url": self._ws_url,
            "hub_node_id": self._hub_node_id,
            "connected_ago_s": round(max(0.0, now - self._connected_at), 3) if self._connected_at else None,
            "last_message_ago_s": round(max(0.0, now - self._last_message_at), 3) if self._last_message_at else None,
            "last_pong_ago_s": round(max(0.0, now - self._last_pong_at), 3) if self._last_pong_at else None,
            "last_hub_event_type": self._last_hub_event_type,
            "last_hub_event_ago_s": round(max(0.0, now - self._last_hub_event_at), 3) if self._last_hub_event_at else None,
            "last_hub_core_update": last_hub_core_update,
            "last_follow_key": self._last_follow_key or None,
            "last_follow_result": dict(self._last_follow_result) if isinstance(self._last_follow_result, dict) else {},
            "last_follow_error": self._last_follow_error or None,
            "last_follow_ago_s": round(max(0.0, now - self._last_follow_at), 3) if self._last_follow_at else None,
            "last_control_request": dict(self._last_control_request) if isinstance(self._last_control_request, dict) else {},
            "last_control_result": dict(self._last_control_result) if isinstance(self._last_control_result, dict) else {},
            "last_control_error": self._last_control_error or None,
            "last_control_request_ago_s": round(max(0.0, now - self._last_control_requested_at), 3) if self._last_control_requested_at else None,
            "last_control_result_ago_s": round(max(0.0, now - self._last_control_completed_at), 3) if self._last_control_completed_at else None,
            "yjs_replication": {
                "enabled": bool(self._yjs_enabled),
                "write_seen_total": int(self._yjs_write_seen_total),
                "write_queued_total": int(self._yjs_write_queued_total),
                "write_drop_disconnected_total": int(self._yjs_write_drop_disconnected_total),
                "write_drop_encode_total": int(self._yjs_write_drop_encode_total),
                "write_drop_queue_total": int(self._yjs_write_drop_queue_total),
                "sent_total": int(self._yjs_sent_total),
                "send_failed_total": int(self._yjs_send_failed_total),
                "received_total": int(self._yjs_received_total),
                "received_bytes": int(self._yjs_received_bytes),
                "snapshot_queued_total": int(self._yjs_snapshot_queued_total),
                "snapshot_failed_total": int(self._yjs_snapshot_failed_total),
                "snapshot_bytes": int(self._yjs_snapshot_bytes),
                "last_write_ago_s": round(max(0.0, now - self._last_yjs_write_at), 3) if self._last_yjs_write_at else None,
                "last_sent_ago_s": round(max(0.0, now - self._last_yjs_sent_at), 3) if self._last_yjs_sent_at else None,
                "last_received_ago_s": round(max(0.0, now - self._last_yjs_received_at), 3) if self._last_yjs_received_at else None,
                "last_snapshot_ago_s": round(max(0.0, now - self._last_yjs_snapshot_at), 3) if self._last_yjs_snapshot_at else None,
                "last_write_webspace_id": self._last_yjs_write_webspace_id or None,
                "last_write_bytes": int(self._last_yjs_write_bytes),
                "last_snapshot_webspace_id": self._last_yjs_snapshot_webspace_id or None,
                "last_snapshot_reason": self._last_yjs_snapshot_reason or None,
                "last_queue_size": int(self._last_yjs_queue_size),
            },
            "transition_state": str(transition.get("transition_state") or "ready"),
            "transition_reason": str(transition.get("reason") or "none"),
            "updated_at": now,
        }

    def _compose_local_node_snapshot(
        self,
        *,
        desktop_catalog: dict[str, Any] | None = None,
        include_capacity: bool = True,
    ) -> dict[str, Any]:
        conf = get_ctx().config
        lifecycle = runtime_lifecycle_snapshot()
        update_status = read_core_update_status() or {}
        transition = _member_link_transition_snapshot()
        last_result = read_core_update_last_result() or {}
        slots = slot_status() or {}
        active_manifest = active_slot_manifest() or {}
        node_names = normalize_node_names(getattr(getattr(conf, "node_settings", None), "node_names", []))
        now = time.time()
        node_state = str(lifecycle.get("node_state") or "ready")
        snapshot = {
            "captured_at": now,
            "node_id": str(getattr(conf, "node_id", "") or ""),
            "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
            "role": str(getattr(conf, "role", "") or ""),
            "node_names": list(node_names),
            "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
            "ready": bool(node_state == "ready" and not bool(lifecycle.get("draining"))),
            "node_state": node_state,
            "reason": str(lifecycle.get("reason") or ""),
            "draining": bool(lifecycle.get("draining")),
            "route_mode": "ws" if self.is_connected() else "none",
            "connected_to_subnet": bool(self.is_connected()),
            "connected_to_hub": bool(self.is_connected()),
            "member_link_transition": transition,
            "build": {
                "version": str(BUILD_INFO.version or ""),
                "build_date": str(BUILD_INFO.build_date or ""),
                "runtime_version": str(active_manifest.get("target_version") or ""),
                "runtime_git_commit": str(active_manifest.get("git_commit") or ""),
                "runtime_git_short_commit": str(active_manifest.get("git_short_commit") or ""),
                "runtime_git_branch": str(active_manifest.get("git_branch") or active_manifest.get("target_rev") or ""),
                "runtime_git_subject": str(active_manifest.get("git_subject") or ""),
            },
            "update_status": {
                "state": str(update_status.get("state") or ""),
                "phase": str(update_status.get("phase") or ""),
                "action": str(update_status.get("action") or ""),
                "message": str(update_status.get("message") or ""),
                "reason": str(update_status.get("reason") or ""),
                "target_rev": str(update_status.get("target_rev") or ""),
                "target_version": str(update_status.get("target_version") or ""),
                "target_slot": str(update_status.get("target_slot") or ""),
                "scheduled_for": update_status.get("scheduled_for"),
                "updated_at": update_status.get("updated_at"),
                "finished_at": update_status.get("finished_at"),
            },
            "last_result": {
                "state": str(last_result.get("state") or ""),
                "phase": str(last_result.get("phase") or ""),
                "message": str(last_result.get("message") or last_result.get("validation_error_summary") or ""),
                "target_slot": str(last_result.get("target_slot") or ""),
                "finished_at": last_result.get("finished_at"),
                "validated_at": last_result.get("validated_at"),
            },
            "slots": {
                "active_slot": str(slots.get("active_slot") or ""),
                "previous_slot": str(slots.get("previous_slot") or ""),
                "active_manifest": {
                    "slot": str(active_manifest.get("slot") or ""),
                    "target_rev": str(active_manifest.get("target_rev") or ""),
                    "target_version": str(active_manifest.get("target_version") or ""),
                    "git_commit": str(active_manifest.get("git_commit") or ""),
                    "git_short_commit": str(active_manifest.get("git_short_commit") or ""),
                    "git_branch": str(active_manifest.get("git_branch") or ""),
                    "git_subject": str(active_manifest.get("git_subject") or ""),
                },
            },
            "hub_control_request": {
                "request": dict(self._last_control_request) if isinstance(self._last_control_request, dict) else {},
                "result": dict(self._last_control_result) if isinstance(self._last_control_result, dict) else {},
                "error": self._last_control_error or "",
                "requested_at": self._last_control_requested_at or None,
                "completed_at": self._last_control_completed_at or None,
            },
        }
        if include_capacity:
            snapshot["capacity"] = get_local_capacity()
        if desktop_catalog is not None:
            snapshot["desktop_catalog"] = desktop_catalog
        return snapshot

    def _local_node_snapshot(self) -> dict[str, Any]:
        try:
            from adaos.services.scenario.webspace_runtime import build_local_desktop_catalog_snapshot

            desktop_catalog = build_local_desktop_catalog_snapshot(mode="workspace", include_remote=False)
        except Exception:
            desktop_catalog = {"apps": [], "widgets": []}
        return self._compose_local_node_snapshot(desktop_catalog=desktop_catalog)

    async def _local_node_snapshot_async(self) -> dict[str, Any]:
        try:
            from adaos.services.scenario.webspace_runtime import build_local_desktop_catalog_snapshot_async

            desktop_catalog = await build_local_desktop_catalog_snapshot_async(mode="workspace", include_remote=False)
        except Exception:
            desktop_catalog = {"apps": [], "widgets": []}
        return self._compose_local_node_snapshot(desktop_catalog=desktop_catalog)

    def _local_node_snapshot_heartbeat(self) -> dict[str, Any]:
        return self._compose_local_node_snapshot(
            desktop_catalog=None,
            include_capacity=False,
        )

    def _queue_node_snapshot_heartbeat(self) -> None:
        try:
            self._out_q.put_nowait(
                {
                    "t": "node.snapshot.heartbeat",
                    "snapshot": self._local_node_snapshot_heartbeat(),
                    "ts": time.time(),
                }
            )
        except Exception:
            return

    def _queue_node_snapshot(self) -> None:
        self._last_forced_snapshot_at = time.time()
        loop = self._loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._ensure_snapshot_task)
                return
            except Exception:
                pass
        try:
            self._out_q.put_nowait(
                {
                    "t": "node.snapshot",
                    "snapshot": self._local_node_snapshot(),
                    "ts": time.time(),
                }
            )
        except Exception:
            return

    @staticmethod
    def _yjs_write_needs_full_node_snapshot(meta: dict[str, Any] | None) -> bool:
        metadata = dict(meta or {})
        source = str(metadata.get("source") or "").strip().lower()
        channel = str(metadata.get("channel") or "").strip().lower()
        # Skill/subnet data projections are already replicated through the
        # lightweight yjs.node_state message below. Forcing a full node snapshot
        # for every such write creates an infrastate/catalog rebuild loop on the
        # hub and can starve the member link under pressure.
        if source in {"projection_service", "async_get_ydoc", "yjs.gateway_ws"}:
            return False
        if source.startswith("projection_service") or channel.startswith("projection."):
            return False
        # Catalog/scenario mutations are structural desktop changes; a bounded
        # full snapshot is still useful so the hub can rebuild app/widget
        # listings without waiting for the periodic snapshot loop.
        structural_tokens = (
            "catalog",
            "desktop_catalog",
            "scenario",
            "webspace_runtime",
            "webui",
            "installed",
        )
        return any(token in source or token in channel for token in structural_tokens)

    def _queue_node_snapshot_from_yjs_write(self, *, webspace_id: str | None, meta: dict[str, Any] | None = None) -> None:
        token = str(webspace_id or "").strip() or "default"
        # Keep desktop/subnet projections warm without turning every Yjs write
        # into a snapshot storm. The shared desktop only needs a quick bounded
        # pulse after the first write in a short burst.
        if token not in {"default", "desktop"}:
            return
        if not self._yjs_write_needs_full_node_snapshot(meta):
            return
        # Source-side suppression: regular Yjs replication already sends the
        # lightweight yjs.node_state frame. Rebuilding and publishing a full
        # node snapshot from the Yjs write callback is too expensive for idle
        # member runtimes and can become a self-sustaining catalog rebuild loop.
        # Keep the old path only as an explicit debug escape hatch.
        raw_enabled = str(os.getenv("ADAOS_SUBNET_FULL_SNAPSHOT_ON_YJS_WRITE") or "").strip().lower()
        if raw_enabled not in {"1", "true", "yes", "on"}:
            return
        now = time.time()
        min_interval = 15.0
        if now - float(self._last_yjs_write_snapshot_at or 0.0) < min_interval:
            return
        self._last_yjs_write_snapshot_at = now
        self._queue_node_snapshot()

    def _ensure_snapshot_task(self) -> None:
        if self._snapshot_task is not None and not self._snapshot_task.done():
            return
        self._snapshot_task = asyncio.create_task(
            self._enqueue_node_snapshot_async(),
            name="subnet-link-node-snapshot",
        )

    async def _enqueue_node_snapshot_async(self) -> None:
        try:
            snapshot = await self._local_node_snapshot_async()
        except Exception:
            try:
                snapshot = self._local_node_snapshot()
            except Exception:
                return
        try:
            self._out_q.put_nowait(
                {
                    "t": "node.snapshot",
                    "snapshot": snapshot,
                    "ts": time.time(),
                }
            )
        except Exception:
            return

    @staticmethod
    def _forced_snapshot_min_interval_s() -> float:
        raw = str(os.getenv("ADAOS_SUBNET_FORCED_SNAPSHOT_MIN_INTERVAL_S") or "").strip()
        try:
            value = float(raw or 5.0)
        except Exception:
            value = 5.0
        return max(1.0, min(60.0, value))

    @staticmethod
    def _connect_full_snapshot_min_interval_s() -> float:
        raw = str(os.getenv("ADAOS_SUBNET_CONNECT_FULL_SNAPSHOT_MIN_INTERVAL_S") or "").strip()
        try:
            value = float(raw or 3600.0)
        except Exception:
            value = 3600.0
        return max(15.0, min(3600.0, value))

    @staticmethod
    def _connect_yjs_state_min_interval_s() -> float:
        raw = str(os.getenv("ADAOS_SUBNET_CONNECT_YJS_STATE_MIN_INTERVAL_S") or "").strip()
        try:
            value = float(raw or 60.0)
        except Exception:
            value = 60.0
        return max(5.0, min(3600.0, value))

    def _request_local_snapshot_sync(self, *, webspace_id: str | None = None, reason: str = "subnet_sync") -> None:
        now = time.time()
        if self._last_forced_snapshot_at and (now - self._last_forced_snapshot_at) < self._forced_snapshot_min_interval_s():
            return
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="infrastate.refresh",
                    payload={
                        "webspace_id": str(webspace_id or "").strip() or None,
                        "reason": str(reason or "subnet_sync"),
                    },
                    source="subnet.link_client",
                    ts=now,
                )
            )
        except Exception:
            pass
        self._queue_node_snapshot()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="subnet-link-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except BaseException:
                pass
        self._task = None
        self._connected.clear()
        self._connected_at = 0.0
        self._loop = None
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass
            except BaseException:
                pass
        self._snapshot_task = None
        try:
            if self._remove_ystore_listener:
                self._remove_ystore_listener()
        except Exception:
            pass

    def _install_ystore_listener(self) -> None:
        if not self._yjs_enabled:
            return
        if self._remove_ystore_listener:
            return

        def _on_write(webspace_id: str, update: bytes, meta: dict[str, Any] | None = None) -> None:
            if not update:
                return
            self._yjs_write_seen_total += 1
            self._last_yjs_write_at = time.time()
            self._last_yjs_write_webspace_id = str(webspace_id or "default")
            self._last_yjs_write_bytes = len(update)
            if not self._connected.is_set():
                self._yjs_write_drop_disconnected_total += 1
                return
            try:
                loop = self._loop or asyncio.get_running_loop()
            except Exception:
                self._yjs_write_drop_queue_total += 1
                return
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self._queue_yjs_node_state(
                            webspace_id=webspace_id or "default",
                            reason="ystore_write",
                        )
                    )
                )
            except Exception:
                self._yjs_write_drop_queue_total += 1
                return
            self._queue_node_snapshot_from_yjs_write(webspace_id=webspace_id, meta=meta if isinstance(meta, dict) else None)

        self._remove_ystore_listener = add_ystore_write_listener(_on_write)

    @staticmethod
    def _yjs_snapshot_webspaces() -> list[str]:
        raw = str(os.getenv("ADAOS_SUBNET_YJS_REPLICATION_WEBSPACES") or "desktop").strip()
        out: list[str] = []
        for item in raw.split(","):
            token = str(item or "").strip()
            if token and token not in out:
                out.append(token)
        return out or ["desktop"]

    async def _queue_yjs_node_state(self, *, webspace_id: str, reason: str) -> None:
        if not self._yjs_enabled:
            return
        ws_id = str(webspace_id or "").strip() or "default"
        try:
            local_node_id = str(get_ctx().config.node_id or "").strip()
        except Exception:
            local_node_id = ""
        if not local_node_id:
            return
        ydoc = Y.YDoc()
        store = get_ystore_for_webspace(ws_id)
        try:
            await store.start()
            await store.apply_updates(ydoc)
            data_map = ydoc.get_map("data")
            data = data_map.to_json() if hasattr(data_map, "to_json") else {}
            if isinstance(data, str):
                data = json.loads(data)
            nodes = data.get("nodes") if isinstance(data, dict) else {}
            node_state = nodes.get(local_node_id) if isinstance(nodes, dict) else None
            if not isinstance(node_state, dict):
                return
            msg = {
                "t": "yjs.node_state",
                "webspace_id": ws_id,
                "node_id": local_node_id,
                "state": node_state,
                "reason": str(reason or "member_link_snapshot"),
                "ts": time.time(),
            }
            self._out_q.put_nowait(msg)
            self._yjs_snapshot_queued_total += 1
            self._yjs_write_queued_total += 1
            self._yjs_snapshot_bytes += len(json.dumps(node_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            self._last_yjs_snapshot_at = time.time()
            self._last_yjs_snapshot_webspace_id = ws_id
            self._last_yjs_snapshot_reason = str(reason or "member_link_snapshot")
            self._last_yjs_queue_size = int(self._out_q.qsize())
        except Exception:
            self._yjs_snapshot_failed_total += 1
            _log.debug("failed to queue member-link Yjs state snapshot webspace=%s", ws_id, exc_info=True)
        finally:
            try:
                store.stop()
            except Exception:
                pass

    def _ensure_bus_subscription(self) -> None:
        if self._bus_subscribed:
            return

        def _on_ev(ev: Any) -> None:
            # Forward only a small subset; expand via env later if needed.
            try:
                if not self._connected.is_set():
                    return
                typ = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
                if not isinstance(typ, str) or not typ:
                    return
                if typ in {
                    "sys.ready",
                    "subnet.stopping",
                    "subnet.stopped",
                    "core.update.status",
                    "node.names.changed",
                    "subnet.nats.up",
                    "subnet.nats.down",
                    "subnet.nats.reconnect",
                }:
                    self._queue_node_snapshot()
                if self._bus_prefixes is not None and not any(typ.startswith(p) for p in self._bus_prefixes):
                    return
                payload = getattr(ev, "payload", None) if hasattr(ev, "payload") else (ev.get("payload") if isinstance(ev, dict) else None)
                payload_dict = payload if isinstance(payload, dict) else {"value": payload}
                meta = payload_dict.get("_meta") if isinstance(payload_dict, dict) else None
                if isinstance(meta, dict) and (
                    bool(meta.get("subnet_hub_mirrored")) or bool(meta.get("subnet_origin_node_id"))
                ):
                    return
                source = getattr(ev, "source", None) if hasattr(ev, "source") else (ev.get("source") if isinstance(ev, dict) else None)
                ts = getattr(ev, "ts", None) if hasattr(ev, "ts") else (ev.get("ts") if isinstance(ev, dict) else None)
                msg = {
                    "t": "bus.emit",
                    "event": {
                        "type": typ,
                        "payload": payload_dict,
                        "source": str(source or "member"),
                        "ts": float(ts or time.time()),
                    },
                }
                self._out_q.put_nowait(msg)
            except Exception:
                return

        try:
            get_ctx().bus.subscribe("*", _on_ev)
            self._bus_subscribed = True
        except Exception:
            pass

    async def _run(self) -> None:
        conf = get_ctx().config
        if conf.role != "member":
            return
        if not conf.hub_url:
            _log.warning("subnet link: hub_url is not set for member")
            return
        self._loop = asyncio.get_running_loop()

        self._install_ystore_listener()
        self._ensure_bus_subscription()

        ws_url = _to_ws_url(conf.hub_url, "/ws/subnet")
        self._ws_url = ws_url
        headers = [("X-AdaOS-Token", _resolve_member_hub_token(conf))]

        backoff = 1.0
        while not self._stop.is_set():
            sender_t: asyncio.Task | None = None
            receiver_t: asyncio.Task | None = None
            ping_t: asyncio.Task | None = None
            snapshot_t: asyncio.Task | None = None
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    max_size=None,
                    ping_interval=5.0,
                    ping_timeout=20.0,
                ) as ws:
                    self._connected.set()
                    self._connected_at = time.time()
                    self._last_message_at = self._connected_at
                    self._last_pong_at = self._connected_at
                    backoff = 1.0

                    hello = {
                        "t": "hello",
                        "node_id": conf.node_id,
                        "subnet_id": conf.subnet_id,
                        "hostname": None,
                        "roles": ["member"],
                        "node_names": normalize_node_names(getattr(getattr(conf, "node_settings", None), "node_names", [])),
                        "base_url": None,
                        "capacity": get_local_capacity(),
                    }
                    await ws.send(json.dumps(hello))
                    try:
                        raw_ack = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        try:
                            ack = json.loads(raw_ack)
                        except Exception:
                            ack = {}
                        if isinstance(ack, dict):
                            self._hub_node_id = str(ack.get("hub_node_id") or "").strip()
                            self._last_message_at = time.time()
                    except Exception:
                        pass
                    try:
                        now = time.time()
                        min_full_interval = self._connect_full_snapshot_min_interval_s()
                        send_full_snapshot = (
                            self._last_connect_full_snapshot_at <= 0.0
                            or (now - self._last_connect_full_snapshot_at) >= min_full_interval
                        )
                        if send_full_snapshot:
                            snapshot = await self._local_node_snapshot_async()
                            self._last_connect_full_snapshot_at = now
                            msg_type = "node.snapshot"
                        else:
                            snapshot = self._local_node_snapshot_heartbeat()
                            msg_type = "node.snapshot.heartbeat"
                        await ws.send(
                            json.dumps(
                                {
                                    "t": msg_type,
                                    "snapshot": snapshot,
                                    "ts": now,
                                }
                            )
                        )
                    except Exception:
                        pass
                    now = time.time()
                    min_yjs_interval = self._connect_yjs_state_min_interval_s()
                    if (
                        self._last_connect_yjs_state_at <= 0.0
                        or (now - self._last_connect_yjs_state_at) >= min_yjs_interval
                    ):
                        self._last_connect_yjs_state_at = now
                        for ws_id in self._yjs_snapshot_webspaces():
                            await self._queue_yjs_node_state(
                                webspace_id=ws_id,
                                reason="member_link_connected",
                            )

                    async def _sender() -> None:
                        while True:
                            msg = await self._out_q.get()
                            try:
                                await ws.send(json.dumps(msg))
                                if isinstance(msg, dict) and msg.get("t") in {"yjs.update", "yjs.node_state"}:
                                    self._yjs_sent_total += 1
                                    self._last_yjs_sent_at = time.time()
                                    self._last_yjs_queue_size = int(self._out_q.qsize())
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if isinstance(msg, dict) and msg.get("t") in {"yjs.update", "yjs.node_state"}:
                                    self._yjs_send_failed_total += 1
                                return

                    async def _receiver() -> None:
                        while True:
                            try:
                                raw = await ws.recv()
                            except asyncio.CancelledError:
                                raise
                            except websockets.exceptions.ConnectionClosedOK:
                                return
                            except websockets.exceptions.ConnectionClosedError:
                                return
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if not isinstance(msg, dict):
                                continue
                            self._last_message_at = time.time()
                            t = msg.get("t")
                            if t == "pong":
                                self._last_pong_at = time.time()
                                continue
                            if t == "yjs.update":
                                if self._yjs_enabled:
                                    await self._on_yjs_update(msg)
                                continue
                            if t == "hub.event":
                                await self._on_hub_event(msg)
                                continue
                            if t == "node.snapshot.request":
                                self._request_local_snapshot_sync(reason=str(msg.get("reason") or "node.snapshot.request"))
                                continue
                            if t == "node.display.assignment":
                                await self._on_node_display_assignment(msg)
                                continue
                            if t == "core.update.request":
                                await self._on_core_update_request(ws, msg)
                                continue
                            if t == "node.names.set":
                                await self._on_node_names_set(msg)
                                continue
                            if t == "rpc.req":
                                await self._on_rpc(ws, msg)
                                continue

                    async def _snapshot_loop() -> None:
                        interval_raw = str(os.getenv("ADAOS_SUBNET_SNAPSHOT_INTERVAL_S") or "").strip()
                        try:
                            interval = max(5.0, min(120.0, float(interval_raw or 20.0)))
                        except Exception:
                            interval = 20.0
                        while True:
                            await asyncio.sleep(interval)
                            self._queue_node_snapshot_heartbeat()

                    sender_t = asyncio.create_task(_sender(), name="subnet-link-sender")
                    receiver_t = asyncio.create_task(_receiver(), name="subnet-link-receiver")
                    ping_t = asyncio.create_task(self._ping_loop(ws), name="subnet-link-ping")
                    snapshot_t = asyncio.create_task(_snapshot_loop(), name="subnet-link-snapshot")
                    tasks = [sender_t, receiver_t, ping_t, snapshot_t]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for p in pending:
                        p.cancel()
                    # Ensure task exceptions are retrieved so shutdown doesn't spam logs.
                    _ = await asyncio.gather(*pending, return_exceptions=True)
                    done_results = await asyncio.gather(*done, return_exceptions=True)
                    done_diag: list[str] = []
                    for task, result in zip(done, done_results):
                        name = task.get_name() if hasattr(task, "get_name") else str(task)
                        if isinstance(result, BaseException):
                            done_diag.append(f"{name}:{type(result).__name__}:{result}")
                        else:
                            done_diag.append(f"{name}:ok")
                    now = time.time()
                    self._link_session_end_total += 1
                    log_fn = _log.debug
                    if now - float(self._last_link_session_end_log_at or 0.0) >= 60.0:
                        self._last_link_session_end_log_at = now
                        log_fn = _log.warning
                    log_fn(
                        "subnet link session ended ws=%s done=%s connected_for_s=%.3f last_message_ago_s=%.3f last_pong_ago_s=%.3f queue=%d",
                        ws_url,
                        ",".join(done_diag) or "-",
                        max(0.0, now - float(self._connected_at or 0.0)),
                        max(0.0, now - float(self._last_message_at or 0.0)) if self._last_message_at else -1.0,
                        max(0.0, now - float(self._last_pong_at or 0.0)) if self._last_pong_at else -1.0,
                        int(self._out_q.qsize()),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.debug("subnet link connect failed ws=%s err=%s", ws_url, exc)
            finally:
                for t in (sender_t, receiver_t, ping_t, snapshot_t):
                    if t and not t.done():
                        t.cancel()
                try:
                    await asyncio.gather(*(t for t in (sender_t, receiver_t, ping_t, snapshot_t) if t), return_exceptions=True)
                except Exception:
                    pass
                self._connected.clear()
                self._connected_at = 0.0

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 15.0)

    async def _ping_loop(self, ws) -> None:
        pong_stale_after_s = self._pong_stale_after_s()
        while True:
            await asyncio.sleep(3.0)
            now = time.time()
            last_activity_at = max(
                float(self._last_pong_at or 0.0),
                float(self._last_message_at or 0.0),
                float(self._connected_at or 0.0),
            )
            if last_activity_at > 0.0 and (now - last_activity_at) > pong_stale_after_s:
                _log.warning(
                    "subnet link activity watchdog expired ws=%s age_s=%.3f threshold_s=%.3f",
                    self._ws_url,
                    now - last_activity_at,
                    pong_stale_after_s,
                )
                return
            try:
                await ws.send(json.dumps({"t": "ping", "ts": time.time()}))
            except Exception:
                return

    async def _on_yjs_update(self, msg: dict[str, Any]) -> None:
        try:
            ws_id = str(msg.get("webspace_id") or "default")
            b64 = str(msg.get("update_b64") or "")
            if not b64:
                return
            upd = base64.b64decode(b64.encode("ascii"), validate=False)
            self._yjs_received_total += 1
            self._yjs_received_bytes += len(upd)
            self._last_yjs_received_at = time.time()
            store = get_ystore_for_webspace(ws_id)
            async with suppress_ystore_write_notifications():
                await store.write(upd)
            apply_update_to_live_room(
                ws_id,
                upd,
                root_names=["data", "ui"],
                source="subnet.link_client",
                owner="core:subnet_link_client",
                channel="core.subnet.link.update",
            )
        except Exception:
            return

    async def _on_rpc(self, ws, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(rid, str) or not rid:
            return
        if method != "tools.call":
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": "unknown_method"}))
            return

        tool = (params or {}).get("tool")
        arguments = (params or {}).get("arguments") or {}
        timeout = (params or {}).get("timeout")
        dev = bool((params or {}).get("dev", False))
        if not isinstance(tool, str) or ":" not in tool:
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": "invalid_tool"}))
            return

        try:
            result = await asyncio.to_thread(self._run_tool, tool, arguments, timeout, dev)
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": True, "result": result}))
        except Exception as exc:
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}))

    @staticmethod
    def _run_tool(tool: str, arguments: dict[str, Any], timeout: Any, dev: bool) -> Any:
        ctx = get_ctx()
        skill_name, public_tool = tool.split(":", 1)
        mgr = SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
            settings=ctx.settings,
        )
        if dev:
            return mgr.run_dev_tool(skill_name, public_tool, arguments or {}, timeout=timeout)
        return mgr.run_tool(skill_name, public_tool, arguments or {}, timeout=timeout)

    async def _on_hub_event(self, msg: dict[str, Any]) -> None:
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}
        source = str(event.get("source") or "hub").strip() or "hub"
        mirrored_payload = dict(payload)
        meta = mirrored_payload.get("_meta")
        if isinstance(meta, dict):
            meta = dict(meta)
        else:
            meta = {}
        meta["subnet_hub_mirrored"] = True
        if self._hub_node_id:
            meta.setdefault("subnet_hub_node_id", self._hub_node_id)
        target_node_id = str(
            mirrored_payload.get("target_node_id")
            or meta.get("target_node_id")
            or meta.get("node_target_id")
            or ""
        ).strip()
        local_node_id = str(getattr(get_ctx().config, "node_id", "") or "").strip()
        if target_node_id and local_node_id and target_node_id != local_node_id:
            return
        mirrored_payload["_meta"] = meta
        self._last_hub_event_type = event_type
        self._last_hub_event_at = time.time()
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type=event_type if event_type != "core.update.status" else "hub.core_update.status",
                    payload=mirrored_payload,
                    source=source,
                    ts=float(event.get("ts") or time.time()),
                )
            )
        except Exception:
            _log.debug("failed to publish mirrored hub event type=%s", event_type, exc_info=True)
        if event_type == "core.update.status":
            self._last_hub_core_update = dict(payload)
            await self._follow_hub_core_update(payload)
        if event_type in {"desktop.webspace.reload", "desktop.webspace.reloaded", "desktop.webspace.reset"}:
            self._request_local_snapshot_sync(
                webspace_id=str(payload.get("webspace_id") or "").strip() or None,
                reason=event_type,
            )

    async def _on_node_display_assignment(self, msg: dict[str, Any]) -> None:
        payload = msg.get("node_display")
        if not isinstance(payload, dict):
            return
        try:
            save_node_runtime_state(
                node_display={
                    "display_index": payload.get("node_index"),
                    "accent_index": payload.get("node_color_index"),
                    "node_label": str(payload.get("node_label") or "").strip(),
                    "node_compact_label": str(payload.get("node_compact_label") or "").strip(),
                    "node_color": str(payload.get("node_color") or "").strip(),
                }
            )
        except Exception:
            _log.debug("failed to persist node display assignment", exc_info=True)

    async def _follow_hub_core_update(self, payload: dict[str, Any]) -> None:
        if str(os.getenv("ADAOS_MEMBER_FOLLOW_HUB_UPDATE", "1")).strip().lower() in {"0", "false", "no", "off"}:
            return
        try:
            conf = getattr(get_ctx(), "config", None) or load_config()
        except Exception:
            conf = None
        if conf is not None and not bool(getattr(conf, "core_update_enabled", True)):
            return
        disabled_reason = core_update_reactions_disabled_reason()
        if disabled_reason:
            _log.info("hub core update follow skipped reason=%s", disabled_reason)
            return
        state = str(payload.get("state") or "").strip().lower()
        action = str(payload.get("action") or "update").strip().lower()
        target_rev = str(payload.get("target_rev") or "").strip()
        target_version = str(payload.get("target_version") or "").strip()
        scheduled_for = payload.get("scheduled_for")
        follow_key = f"{action}:{target_rev}:{target_version}:{scheduled_for}:{state}"
        if follow_key == self._last_follow_key and self._last_follow_at > 0:
            return
        if action not in {"update", "rollback"}:
            return
        if state not in {"countdown", "draining", "stopping", "cancelled"}:
            return
        if action == "update" and state != "cancelled" and not (target_rev or target_version):
            return
        from adaos.services.core_update import read_status as read_core_update_status

        local_status = read_core_update_status()
        local_state = str(local_status.get("state") or "").strip().lower()
        if state == "cancelled":
            if local_state not in {"countdown", "draining", "stopping"}:
                return
            path = "/api/admin/update/cancel"
            body = {"reason": "hub.member_follow.cancel"}
        elif action == "rollback":
            if local_state in {"countdown", "draining", "stopping", "restarting", "applying"}:
                return
            body = {
                "reason": "hub.member_follow.rollback",
                "countdown_sec": self._remaining_countdown_s(scheduled_for, default=12.0),
                "drain_timeout_sec": float(payload.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(payload.get("signal_delay_sec") or 0.25),
            }
            path = "/api/admin/update/rollback"
        else:
            if local_state in {"countdown", "draining", "stopping", "restarting", "applying"}:
                return
            body = {
                "reason": "hub.member_follow.update",
                "target_rev": target_rev,
                "target_version": target_version,
                "countdown_sec": self._remaining_countdown_s(scheduled_for, default=15.0),
                "drain_timeout_sec": float(payload.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(payload.get("signal_delay_sec") or 0.25),
            }
            path = "/api/admin/update/start"
        self._last_follow_key = follow_key
        self._last_follow_at = time.time()
        try:
            result = await asyncio.to_thread(self._post_local_admin, path, body)
            self._last_follow_result = result if isinstance(result, dict) else {"ok": True}
            self._last_follow_error = ""
        except Exception as exc:
            self._last_follow_error = f"{type(exc).__name__}: {exc}"
            self._last_follow_result = {"ok": False, "error": self._last_follow_error}
        self._queue_node_snapshot()

    @staticmethod
    def _remaining_countdown_s(scheduled_for: Any, *, default: float) -> float:
        try:
            value = float(scheduled_for or 0.0)
        except Exception:
            value = 0.0
        if value <= 0.0:
            return default
        remaining = max(5.0, min(120.0, value - time.time()))
        return round(remaining, 3)

    @staticmethod
    def _post_local_admin(path: str, body: dict[str, Any]) -> dict[str, Any]:
        base = MemberLinkClient._resolve_local_control_base()
        # Re-resolve the control token against the selected local control base because
        # the active runtime may be serving with a newer supervisor/env token than the
        # persisted node config still knows about.
        token = str(resolve_control_token(base_url=base) or "dev-local-token")
        headers = {"X-AdaOS-Token": token, "Accept": "application/json"}
        sess = requests.Session()
        try:
            sess.trust_env = False
        except Exception:
            pass
        response = sess.post(base.rstrip("/") + path, headers=headers, json=body, timeout=8.0)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {"ok": True}

    @staticmethod
    def _resolve_local_control_base() -> str:
        candidates: list[str] = []
        env_type = str(os.getenv("ENV_TYPE") or "").strip().lower()
        supervisor_enabled = str(os.getenv("ADAOS_SUPERVISOR_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        allow_supervisor_probe = bool(supervisor_enabled or env_type != "dev")
        supervisor_candidates = []
        if allow_supervisor_probe:
            supervisor_candidates.extend(
                [
                    "http://127.0.0.1:8776",
                    "http://localhost:8776",
                ]
            )
        for raw in (
            os.getenv("ADAOS_SELF_BASE_URL"),
            os.getenv("ADAOS_CONTROL_URL"),
            os.getenv("ADAOS_CONTROL_BASE"),
            "http://127.0.0.1:8777",
            "http://127.0.0.1:8778",
            "http://127.0.0.1:8779",
            "http://localhost:8777",
            "http://localhost:8778",
            "http://localhost:8779",
        ):
            text = str(raw or "").strip().rstrip("/")
            if not text or text in candidates:
                continue
            candidates.append(text)
        sess = requests.Session()
        try:
            sess.trust_env = False
        except Exception:
            pass
        for supervisor_base in supervisor_candidates:
            if not supervisor_base:
                continue
            try:
                resp = sess.get(
                    supervisor_base + "/api/supervisor/public/update-status",
                    headers={"Accept": "application/json"},
                    timeout=0.6,
                )
                if int(resp.status_code) != 200:
                    continue
                payload = resp.json()
                runtime = payload.get("runtime") if isinstance(payload, dict) else {}
                runtime_url = str((runtime or {}).get("runtime_url") or "").strip().rstrip("/")
                if runtime_url and runtime_url not in candidates:
                    candidates.insert(0, runtime_url)
            except Exception:
                continue
        for base in candidates:
            try:
                resp = sess.get(base + "/api/ping", headers={"Accept": "application/json"}, timeout=0.5)
                if int(resp.status_code) != 200:
                    continue
                payload = resp.json()
                runtime = payload.get("runtime") if isinstance(payload, dict) else {}
                transition_role = str((runtime or {}).get("transition_role") or "").strip().lower()
                if transition_role == "candidate":
                    continue
                if isinstance(runtime, dict) and runtime.get("admin_mutation_allowed") is False:
                    continue
                if int(resp.status_code) == 200:
                    return base
            except Exception:
                continue
        return candidates[0] if candidates else "http://127.0.0.1:8777"

    async def _on_core_update_request(self, ws, msg: dict[str, Any]) -> None:
        action = str(msg.get("action") or "").strip().lower()
        if action == "start":
            action = "update"
        request_id = str(msg.get("request_id") or "").strip()
        reason = str(msg.get("reason") or "hub.member_control").strip() or "hub.member_control"
        target_rev = str(msg.get("target_rev") or "").strip()
        target_version = str(msg.get("target_version") or "").strip()
        try:
            countdown_sec = float(msg.get("countdown_sec") or (15.0 if action == "update" else 12.0))
        except Exception:
            countdown_sec = 15.0 if action == "update" else 12.0
        try:
            drain_timeout_sec = float(msg.get("drain_timeout_sec") or 10.0)
        except Exception:
            drain_timeout_sec = 10.0
        try:
            signal_delay_sec = float(msg.get("signal_delay_sec") or 0.25)
        except Exception:
            signal_delay_sec = 0.25
        self._last_control_requested_at = time.time()
        self._last_control_completed_at = 0.0
        self._last_control_error = ""
        self._last_control_request = {
            "request_id": request_id,
            "action": action,
            "reason": reason,
            "target_rev": target_rev,
            "target_version": target_version,
            "countdown_sec": countdown_sec,
            "drain_timeout_sec": drain_timeout_sec,
            "signal_delay_sec": signal_delay_sec,
            "state": "requested",
        }
        if action not in {"update", "cancel", "rollback", "drain"}:
            self._last_control_error = "invalid_action"
            result = {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": "invalid_action",
            }
        else:
            if action == "cancel":
                path = "/api/admin/update/cancel"
                body = {"reason": reason}
            elif action == "rollback":
                path = "/api/admin/update/rollback"
                body = {
                    "reason": reason,
                    "countdown_sec": countdown_sec,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                }
            elif action == "drain":
                path = "/api/admin/drain"
                body = {
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                }
            else:
                path = "/api/admin/update/start"
                body = {
                    "reason": reason,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "countdown_sec": countdown_sec,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                }
            try:
                admin_result = await asyncio.to_thread(self._post_local_admin, path, body)
                result = {
                    "ok": True,
                    "request_id": request_id,
                    "action": action,
                    "response": admin_result if isinstance(admin_result, dict) else {"ok": True},
                }
            except Exception as exc:
                self._last_control_error = f"{type(exc).__name__}: {exc}"
                result = {
                    "ok": False,
                    "request_id": request_id,
                    "action": action,
                    "error": self._last_control_error,
                }
        self._last_control_completed_at = time.time()
        self._last_control_result = dict(result)
        self._last_control_request["state"] = "completed"
        self._last_control_request["ok"] = bool(result.get("ok"))
        if not result.get("ok") and result.get("error"):
            self._last_control_request["error"] = str(result.get("error"))
        self._queue_node_snapshot()
        try:
            await ws.send(json.dumps({"t": "core.update.result", "result": result}))
        except Exception:
            pass

    async def _on_node_names_set(self, msg: dict[str, Any]) -> None:
        node_names = normalize_node_names(msg.get("node_names"))
        conf = persist_node_names(node_names)
        try:
            self._out_q.put_nowait(
                {
                    "t": "node.meta",
                    "node_names": list(getattr(conf, "node_names", []) or []),
                    "ts": time.time(),
                }
            )
        except Exception:
            pass
        self._queue_node_snapshot()
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="node.names.changed",
                    payload={
                        "node_id": str(getattr(conf, "node_id", "") or ""),
                        "node_names": list(getattr(conf, "node_names", []) or []),
                    },
                    source="subnet.member",
                    ts=time.time(),
                )
            )
        except Exception:
            pass


_MEMBER_CLIENT: MemberLinkClient | None = None


def get_member_link_client() -> MemberLinkClient:
    global _MEMBER_CLIENT
    if _MEMBER_CLIENT is None:
        _MEMBER_CLIENT = MemberLinkClient()
    return _MEMBER_CLIENT


def member_link_client_snapshot() -> dict[str, Any]:
    return get_member_link_client().snapshot()
