from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import threading
import time
import queue
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from adaos.services.agent_context import get_ctx
from adaos.services.hub_root_protocol_store import protocol_streams_snapshot
from adaos.services.node_display import node_display_from_config, node_display_payload
from adaos.services.registry.subnet_runtime_projection import (
    subnet_runtime_projection_freshness,
)
from adaos.services.zone_hosts import canonical_zone_id

_log = logging.getLogger("adaos.reliability")


class MessageTaxonomy(str, Enum):
    COMMAND = "command"
    REQUEST = "request"
    RESPONSE = "response"
    STATE_REPORT = "state_report"
    EVENT = "event"
    SYNC_UPDATE = "sync_update"
    PRESENCE = "presence"
    ROUTE_FRAME = "route_frame"
    MEDIA_FRAME = "media_frame"


class DeliveryClass(str, Enum):
    MUST_NOT_LOSE = "must_not_lose"
    NICE_TO_REPLAY = "nice_to_replay"
    DROP_ALLOWED = "drop_allowed"


class ChannelType(str, Enum):
    COMMAND = "command_channel"
    EVENT = "event_channel"
    SYNC = "sync_channel"
    PRESENCE = "presence_channel"
    ROUTE = "route_channel"
    MEDIA = "media_channel"


class Authority(str, Enum):
    ROOT = "root"
    HUB = "hub"
    MEMBER_BROWSER = "member_browser"
    SIDECAR = "sidecar"
    SHARED = "shared"


class ReadinessStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class FlowSpec:
    flow_id: str
    channel_type: ChannelType
    message_types: tuple[MessageTaxonomy, ...]
    delivery_class: DeliveryClass
    authority: Authority
    ordered: bool
    durable: bool
    replayable: bool
    current_paths: tuple[str, ...]
    description: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "channel_type": self.channel_type.value,
            "message_types": [item.value for item in self.message_types],
            "delivery_class": self.delivery_class.value,
            "authority": self.authority.value,
            "ordered": self.ordered,
            "durable": self.durable,
            "replayable": self.replayable,
            "current_paths": list(self.current_paths),
            "description": self.description,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class SemanticChannelSpec:
    channel_id: str
    title: str
    channel_type: ChannelType
    message_types: tuple[MessageTaxonomy, ...]
    authority: Authority
    candidate_paths: tuple[str, ...]
    failover_order: tuple[str, ...]
    freeze_after_switch_s: int
    duplicate_suppression: str
    description: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "channel_type": self.channel_type.value,
            "message_types": [item.value for item in self.message_types],
            "authority": self.authority.value,
            "candidate_paths": list(self.candidate_paths),
            "failover_order": list(self.failover_order),
            "freeze_after_switch_s": int(self.freeze_after_switch_s),
            "duplicate_suppression": self.duplicate_suppression,
            "description": self.description,
            "notes": self.notes,
        }


HUB_ROOT_FLOW_SPECS: tuple[FlowSpec, ...] = (
    FlowSpec(
        flow_id="hub_root.control.lifecycle",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.SHARED,
        ordered=True,
        durable=True,
        replayable=True,
        current_paths=("mtls_http:/hub/nats/token", "nats_ws", "subnet.nats.up/down"),
        description="Hub-root control session establishment and lifecycle state exchange.",
        notes="Critical control-plane flow. Requires explicit idempotency and resume semantics.",
    ),
    FlowSpec(
        flow_id="hub_root.route.control",
        channel_type=ChannelType.ROUTE,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.EVENT),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.ROOT,
        ordered=True,
        durable=False,
        replayable=True,
        current_paths=("nats:route.v2.to_hub.<hubId>.*",),
        description="Route-install and browser<->hub relay control traffic crossing root.",
        notes="Control metadata for route proxy. Must not share a pressure domain with core control acks.",
    ),
    FlowSpec(
        flow_id="hub_root.route.frame",
        channel_type=ChannelType.ROUTE,
        message_types=(MessageTaxonomy.ROUTE_FRAME,),
        delivery_class=DeliveryClass.DROP_ALLOWED,
        authority=Authority.SHARED,
        ordered=True,
        durable=False,
        replayable=False,
        current_paths=("nats:route.v2.to_hub.<hubId>.*", "nats:route.v2.to_browser.<hubId>.*"),
        description="Relay frames for proxied HTTP/WS browser traffic.",
        notes="Wrapped logical flow defines higher-level semantics; route frames themselves are not durable.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.telegram",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.EVENT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.ROOT,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("root_http", "root_nats_bridge"),
        description="Root-backed Telegram actions and related integration state transitions.",
        notes="Retries require stable operation keys to avoid duplicate user-visible sends.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.github_core_update",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.REQUEST, MessageTaxonomy.RESPONSE, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.ROOT,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("root_http:/hub/core_update/*", "root_state"),
        description="Core update coordination and release/report exchange through root.",
        notes="Drives update orchestration and hub report persistence.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.llm",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.REQUEST, MessageTaxonomy.RESPONSE, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.ROOT,
        ordered=False,
        durable=False,
        replayable=True,
        current_paths=("root_http:/v1/llm/models", "root_http:/v1/llm/response"),
        description="Root-backed LLM model discovery and completion requests.",
        notes="Interactive LLM completions depend on root reachability but do not require durable transport replay.",
    ),
    FlowSpec(
        flow_id="hub_member.sync.yjs",
        channel_type=ChannelType.SYNC,
        message_types=(MessageTaxonomy.SYNC_UPDATE,),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.HUB,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("yws", "webrtc_data:yjs", "member_link_ws"),
        description="Yjs sync as a transport-independent sync channel.",
        notes="Backed by snapshot/diff and bounded replay, not by transport-specific semantics alone.",
    ),
    FlowSpec(
        flow_id="hub_member.presence",
        channel_type=ChannelType.PRESENCE,
        message_types=(MessageTaxonomy.PRESENCE,),
        delivery_class=DeliveryClass.DROP_ALLOWED,
        authority=Authority.MEMBER_BROWSER,
        ordered=False,
        durable=False,
        replayable=False,
        current_paths=("ws", "webrtc_data", "root_route_proxy"),
        description="Awareness and ephemeral session hints for member/browser clients.",
        notes="Explicitly ephemeral. Never escalated into a durable control bus.",
    ),
)


HUB_MEMBER_CHANNEL_SPECS: tuple[SemanticChannelSpec, ...] = (
    SemanticChannelSpec(
        channel_id="hub_member.command",
        title="CommandChannel",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.REQUEST, MessageTaxonomy.RESPONSE),
        authority=Authority.HUB,
        candidate_paths=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        failover_order=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        freeze_after_switch_s=10,
        duplicate_suppression="command_id scoped to one active path",
        description="Imperative browser/member commands into hub runtime.",
        notes="WebRTC events is preferred when active. Root relay is an explicit fallback, not a parallel authority path.",
    ),
    SemanticChannelSpec(
        channel_id="hub_member.event",
        title="EventChannel",
        channel_type=ChannelType.EVENT,
        message_types=(MessageTaxonomy.EVENT,),
        authority=Authority.HUB,
        candidate_paths=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        failover_order=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        freeze_after_switch_s=10,
        duplicate_suppression="one active fanout path; duplicate event ids ignored when present",
        description="Hub-to-member/browser event fanout for UI and session events.",
        notes="Shares transport with command channel today, but remains a separate semantic channel.",
    ),
    SemanticChannelSpec(
        channel_id="hub_member.sync",
        title="SyncChannel",
        channel_type=ChannelType.SYNC,
        message_types=(MessageTaxonomy.SYNC_UPDATE,),
        authority=Authority.HUB,
        candidate_paths=("webrtc_data:yjs", "yws", "root_route_proxy", "member_link_ws"),
        failover_order=("webrtc_data:yjs", "yws", "root_route_proxy", "member_link_ws"),
        freeze_after_switch_s=15,
        duplicate_suppression="single active provider per doc; no multipath sync authority",
        description="Transport-independent Yjs sync channel.",
        notes="WebRTC Yjs datachannel is preferred; websocket and root relay remain bounded fallback paths.",
    ),
    SemanticChannelSpec(
        channel_id="hub_member.presence",
        title="PresenceChannel",
        channel_type=ChannelType.PRESENCE,
        message_types=(MessageTaxonomy.PRESENCE,),
        authority=Authority.MEMBER_BROWSER,
        candidate_paths=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        failover_order=("webrtc_data:events", "ws", "root_route_proxy", "member_link_ws"),
        freeze_after_switch_s=5,
        duplicate_suppression="drop allowed; no durable dedupe window",
        description="Ephemeral awareness and session-hint channel.",
        notes="Explicitly best-effort and non-durable, even when carried over a durable transport.",
    ),
    SemanticChannelSpec(
        channel_id="hub_member.route",
        title="RouteChannel",
        channel_type=ChannelType.ROUTE,
        message_types=(MessageTaxonomy.ROUTE_FRAME,),
        authority=Authority.SHARED,
        candidate_paths=("root_route_proxy",),
        failover_order=("root_route_proxy",),
        freeze_after_switch_s=0,
        duplicate_suppression="stream-scoped; one relay authority path",
        description="Relay path for browser traffic when root sits between browser and hub.",
        notes="Only active when browser traffic is explicitly relayed through root route proxy.",
    ),
    SemanticChannelSpec(
        channel_id="hub_member.media",
        title="MediaChannel",
        channel_type=ChannelType.MEDIA,
        message_types=(MessageTaxonomy.MEDIA_FRAME,),
        authority=Authority.SHARED,
        candidate_paths=("member_browser_webrtc_media", "webrtc_media", "root_media_relay"),
        failover_order=("member_browser_webrtc_media", "webrtc_media", "root_media_relay"),
        freeze_after_switch_s=3,
        duplicate_suppression="none; latency-first media semantics",
        description="Latency-sensitive media plane.",
        notes="Phase 6 keeps media explicitly isolated from control/sync hardening; current runtime now supports bounded root relay for file media, direct WebRTC audio/video loopback for live validation, and an explicit member-browser direct path foundation.",
    ),
)


AUTHORITY_BOUNDARIES: dict[str, Any] = {
    "root": {
        "owns": [
            "hub registration and identity validation",
            "hub NATS session issuance",
            "root-backed owner authentication",
            "cross-subnet coordination",
            "root-routed external integrations",
            "release and update coordination across hubs",
        ],
        "does_not_own": [
            "local hub execution state",
            "local skill runtime internals",
            "local Yjs in-memory session ownership",
        ],
    },
    "hub": {
        "owns": [
            "local skill and scenario execution",
            "local event bus",
            "local webspace and Yjs persistence",
            "admitted member/browser session handling",
            "local degraded-mode execution policy",
        ],
        "does_not_own": [
            "minting fresh root-backed trust",
            "claiming root integration delivery without acknowledgement",
            "global cross-subnet truth",
        ],
    },
    "member_browser": {
        "owns": [
            "local ephemeral session state",
            "local cached sync state",
            "local media device state",
        ],
        "does_not_own": [
            "shared durable control state",
            "global routing authority",
            "root-issued trust state",
        ],
    },
    "sidecar": {
        "may_own": [
            "transport lifecycle",
            "socket diagnostics",
            "reconnect loops",
            "local relay io",
        ],
        "must_not_own": [
            "command semantics",
            "idempotency rules",
            "durable cursor semantics",
            "degraded-mode business policy",
        ],
    },
}


@dataclass(slots=True)
class RuntimeSignal:
    status: ReadinessStatus = ReadinessStatus.UNKNOWN
    summary: str = ""
    updated_at: float = 0.0
    observed: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "updated_at": self.updated_at or None,
            "observed": self.observed,
            "details": dict(self.details or {}),
        }


_LOCK = threading.RLock()
_INTEGRATION_NAMES = ("telegram", "github", "llm")
_CHANNEL_NAMES = ("root_control", "route")
_CHANNEL_HISTORY_LIMIT = 128
_TRANSPORT_HISTORY_LIMIT = 64
_UNSET = object()
_ROOT_CONTROL = RuntimeSignal()
_ROUTE = RuntimeSignal()
_INTEGRATIONS: dict[str, RuntimeSignal] = {name: RuntimeSignal() for name in _INTEGRATION_NAMES}
_CHANNEL_HISTORY: dict[str, deque[dict[str, Any]]] = {
    name: deque(maxlen=_CHANNEL_HISTORY_LIMIT) for name in _CHANNEL_NAMES
}
_HUB_ROOT_TRANSPORT_STATE: dict[str, Any] = {
    "requested_transport": None,
    "effective_transport": None,
    "selected_server": None,
    "url_override": None,
    "current_ws_tag": None,
    "last_event": "",
    "last_error": "",
    "last_summary": "",
    "attempt_seq": 0,
    "last_attempt_at": 0.0,
    "last_connected_at": 0.0,
    "last_failure_at": 0.0,
    "candidates": [],
    "failover_policy": {},
    "hypothesis": {},
    "updated_at": 0.0,
}
_HUB_ROOT_TRANSPORT_HISTORY: deque[dict[str, Any]] = deque(maxlen=_TRANSPORT_HISTORY_LIMIT)

_HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES = ("control", "integration", "route", "sync_metadata")
_HUB_ROOT_PROTOCOL_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "control": {
        "priority": "highest",
        "ack_policy": "required",
        "replay": "bounded",
        "idempotency": "strict",
        "drop_policy": "never",
        "worker_budget": 1,
        "pending_msgs_limit": 256,
        "pending_bytes_limit": 8 * 1024 * 1024,
        "stale_authority_after_s": 30,
    },
    "integration": {
        "priority": "medium",
        "ack_policy": "integration_specific",
        "replay": "selected_flows_only",
        "idempotency": "operation_key",
        "drop_policy": "buffer_then_drop_oldest",
        "worker_budget": 1,
        "pending_msgs_limit": 1024,
        "pending_bytes_limit": 16 * 1024 * 1024,
        "stale_authority_after_s": 120,
    },
    "route": {
        "priority": "lower_than_control",
        "ack_policy": "request_reply_only",
        "replay": "session_bounded",
        "idempotency": "session_scoped",
        "drop_policy": "slow_consumer_backpressure",
        "worker_budget": 1,
        "pending_msgs_limit": 4096,
        "pending_bytes_limit": 64 * 1024 * 1024,
        "stale_authority_after_s": 45,
    },
    "sync_metadata": {
        "priority": "below_control",
        "ack_policy": "negotiation_specific",
        "replay": "bounded",
        "idempotency": "cursor_scoped",
        "drop_policy": "drop_oldest_noncritical",
        "worker_budget": 1,
        "pending_msgs_limit": 512,
        "pending_bytes_limit": 8 * 1024 * 1024,
        "stale_authority_after_s": 60,
    },
}


def _protocol_env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def hub_root_protocol_class_policy(traffic_class: str) -> dict[str, Any]:
    key = str(traffic_class or "").strip().lower()
    defaults = _HUB_ROOT_PROTOCOL_CLASS_DEFAULTS.get(key)
    if defaults is None:
        raise ValueError(f"unsupported hub-root traffic class: {traffic_class!r}")
    prefix = f"HUB_PROTOCOL_{key.upper()}"
    return {
        **defaults,
        "traffic_class": key,
        "pending_msgs_limit": _protocol_env_int(
            f"{prefix}_PENDING_MSGS_LIMIT",
            int(defaults.get("pending_msgs_limit") or 0),
            minimum=1,
        ),
        "pending_bytes_limit": _protocol_env_int(
            f"{prefix}_PENDING_BYTES_LIMIT",
            int(defaults.get("pending_bytes_limit") or 0),
            minimum=1024,
        ),
        "stale_authority_after_s": _protocol_env_int(
            f"{prefix}_STALE_AUTHORITY_AFTER_S",
            int(defaults.get("stale_authority_after_s") or 0),
            minimum=1,
        ),
        "worker_budget": _protocol_env_int(
            f"{prefix}_WORKER_BUDGET",
            int(defaults.get("worker_budget") or 1),
            minimum=1,
        ),
    }


def hub_root_protocol_traffic_class(subject: str) -> str:
    subj = str(subject or "").strip().lower()
    if subj.startswith("hub.control."):
        return "control"
    if subj.startswith("route."):
        return "route"
    if subj.startswith("tg.input.") or subj.startswith("tg.output.") or subj.startswith("io.tg.in."):
        return "integration"
    if subj.startswith("sync.") or subj.startswith("cursor.") or subj.startswith("ystate."):
        return "sync_metadata"
    return "integration"


def _new_protocol_traffic_class_state(name: str) -> dict[str, Any]:
    return {
        "traffic_class": name,
        "policy": hub_root_protocol_class_policy(name),
        "active_subscriptions": 0,
        "subjects": [],
        "dispatch_count": 0,
        "publish_ok": 0,
        "publish_fail": 0,
        "handler_errors": 0,
        "pressure_events": 0,
        "last_dispatch_at": 0.0,
        "last_publish_at": 0.0,
        "last_error_at": 0.0,
        "last_error": "",
        "last_qsize": None,
        "max_qsize": 0,
        "last_pending_bytes": None,
        "max_pending_bytes": 0,
        "last_message_bytes": None,
    }


def _new_route_flow_state(name: str) -> dict[str, Any]:
    return {
        "name": str(name or "").strip().lower() or "unknown",
        "event_total": 0,
        "to_upstream_total": 0,
        "to_browser_total": 0,
        "bytes_to_upstream": 0,
        "bytes_to_browser": 0,
        "pending_total": 0,
        "publish_fail_total": 0,
        "send_fail_total": 0,
        "connect_fail_total": 0,
        "forced_close_total": 0,
        "upstream_close_total": 0,
        "last_event": "",
        "last_event_at": 0.0,
        "last_error": "",
        "last_error_at": 0.0,
        "updated_at": 0.0,
    }


def _new_protocol_runtime() -> dict[str, Any]:
    return {
        "traffic_classes": {
            name: _new_protocol_traffic_class_state(name)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "subscriptions": {},
        "route_runtime": {
            "active_tunnels": 0,
            "active_reader_tasks": 0,
            "pending_tunnels": 0,
            "pending_events": 0,
            "pending_chunks": 0,
            "max_pending_events": 0,
            "no_upstream_close_after_s": None,
            "legacy_v1_enabled": False,
            "v2_enabled": False,
            "last_force_close_at": 0.0,
            "last_no_upstream_at": 0.0,
            "last_publish_fail_at": 0.0,
            "last_reset_at": 0.0,
            "last_reset_reason": "",
            "last_reset_closed_tunnels": 0,
            "last_reset_dropped_pending": 0,
            "last_reset_notified_browser": 0,
            "reset_total": 0,
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
            "open_request_total": 0,
            "http_request_total": 0,
            "last_open_path": "",
            "last_open_query_has_token": False,
            "last_open_base_total": 0,
            "last_http_path": "",
            "last_http_method": "",
            "flows": {
                "control": _new_route_flow_state("control"),
                "frame": _new_route_flow_state("frame"),
            },
        },
        "integration_outboxes": {
            "telegram": {
                "name": "telegram",
                "size": 0,
                "max_size": None,
                "durable_store": False,
                "persist_path": "",
                "persisted_size": 0,
                "drained_total": 0,
                "dropped_total": 0,
                "publish_ok": 0,
                "publish_fail": 0,
                "connected": None,
                "idempotency_mode": "operation_key",
                "last_operation_key": "",
                "cache_hit_total": 0,
                "cache_miss_total": 0,
                "conflict_total": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "updated_at": 0.0,
            },
            "llm": {
                "name": "llm",
                "size": 0,
                "max_size": None,
                "durable_store": False,
                "persist_path": "",
                "persisted_size": 0,
                "drained_total": 0,
                "dropped_total": 0,
                "publish_ok": 0,
                "publish_fail": 0,
                "connected": None,
                "idempotency_mode": "request_id",
                "last_operation_key": "",
                "cache_hit_total": 0,
                "cache_miss_total": 0,
                "conflict_total": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "updated_at": 0.0,
            },
        },
        "streams": {},
        "updated_at": 0.0,
    }


_HUB_ROOT_PROTOCOL_RUNTIME: dict[str, Any] = _new_protocol_runtime()
_HUB_MEMBER_CHANNEL_RUNTIME: dict[str, dict[str, Any]] = {}


def _copy_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dedup_texts(items: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, (list, tuple)):
        return result
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _read_last_jsonl_record(path: Path, *, max_bytes: int = 131072) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in reversed(chunk.splitlines()):
        text = str(line or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _hub_root_transport_from_server(server: str | None, *, explicit_transport: str | None = None) -> str | None:
    explicit = str(explicit_transport or "").strip().lower()
    if explicit:
        return explicit
    text = str(server or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
        scheme = str(parsed.scheme or "").strip().lower()
    except Exception:
        scheme = ""
    if scheme in {"ws", "wss"}:
        return "ws"
    if scheme in {"nats", "tls"}:
        return "tcp"
    if scheme in {"http", "https"}:
        return "sidecar"
    return None


def configure_hub_root_transport_strategy(
    *,
    requested_transport: Any = _UNSET,
    effective_transport: Any = _UNSET,
    selected_server: Any = _UNSET,
    url_override: Any = _UNSET,
    current_ws_tag: Any = _UNSET,
    candidates: Any = _UNSET,
    failover_policy: Any = _UNSET,
    hypothesis: Any = _UNSET,
) -> None:
    with _LOCK:
        state = _HUB_ROOT_TRANSPORT_STATE
        if requested_transport is not _UNSET:
            state["requested_transport"] = str(requested_transport or "").strip().lower() or None
        if effective_transport is not _UNSET or selected_server is not _UNSET:
            state["effective_transport"] = _hub_root_transport_from_server(
                selected_server if selected_server is not _UNSET else state.get("selected_server"),
                explicit_transport=effective_transport if effective_transport is not _UNSET else None,
            )
        if selected_server is not _UNSET:
            state["selected_server"] = str(selected_server or "").strip() or None
        if url_override is not _UNSET:
            state["url_override"] = str(url_override or "").strip() or None
        if current_ws_tag is not _UNSET:
            state["current_ws_tag"] = str(current_ws_tag or "").strip() or None
        if candidates is not _UNSET:
            state["candidates"] = _dedup_texts(candidates)
        if failover_policy is not _UNSET:
            state["failover_policy"] = _copy_dict(failover_policy)
        if hypothesis is not _UNSET:
            state["hypothesis"] = _copy_dict(hypothesis)
        state["updated_at"] = time.time()


def record_hub_root_transport_event(
    event: str,
    *,
    transport: str | None = None,
    server: str | None = None,
    summary: str = "",
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    evt = str(event or "").strip().lower() or "event"
    srv = str(server or "").strip() or None
    tr = _hub_root_transport_from_server(srv, explicit_transport=transport)
    err = str(error or "").strip() or None
    ts = time.time()
    record = {
        "ts": ts,
        "event": evt,
        "transport": tr,
        "server": srv,
        "summary": str(summary or ""),
        "error": err,
        "details": dict(details or {}),
    }
    with _LOCK:
        state = _HUB_ROOT_TRANSPORT_STATE
        if tr:
            state["effective_transport"] = tr
        if srv:
            state["selected_server"] = srv
        state["last_event"] = evt
        state["last_summary"] = str(summary or "")
        if err:
            state["last_error"] = err
        if evt in {"attempt", "connect_try", "reconnect_requested"}:
            state["attempt_seq"] = int(state.get("attempt_seq") or 0) + 1
            state["last_attempt_at"] = ts
        if evt in {"connected", "ready", "reconnected"}:
            state["last_connected_at"] = ts
            state["last_error"] = ""
        if evt in {"connect_failed", "down", "disconnected", "watchdog_error", "supervisor_error", "reader_terminated"}:
            state["last_failure_at"] = ts
        state["updated_at"] = ts
        _HUB_ROOT_TRANSPORT_HISTORY.append(record)


def _hub_root_transport_assessment(history: list[dict[str, Any]], *, now_ts: float) -> dict[str, Any]:
    failure_events = {
        "connect_failed",
        "down",
        "disconnected",
        "watchdog_error",
        "supervisor_error",
        "reader_terminated",
    }
    connect_events = {"connected", "ready", "reconnected"}
    threshold_5m = now_ts - 300.0
    threshold_15m = now_ts - 900.0
    failures_5m = 0
    failures_15m = 0
    connects_15m = 0
    attempts_15m = 0
    transports_15m: list[str] = []
    last_event = ""
    last_failure_at: float | None = None
    last_connected_at: float | None = None
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0.0:
            continue
        event = str(item.get("event") or "").strip().lower()
        if ts >= threshold_15m:
            if event in {"attempt", "connect_try", "reconnect_requested"}:
                attempts_15m += 1
            if event in connect_events:
                connects_15m += 1
            if event in failure_events:
                failures_15m += 1
            transport = str(item.get("transport") or "").strip().lower()
            if transport:
                transports_15m.append(transport)
        if ts >= threshold_5m and event in failure_events:
            failures_5m += 1
        if event in connect_events:
            last_connected_at = ts
        if event in failure_events:
            last_failure_at = ts
        last_event = event or last_event

    transport_switches_15m = 0
    prev_transport = ""
    for transport in transports_15m:
        if not transport:
            continue
        if prev_transport and transport != prev_transport:
            transport_switches_15m += 1
        prev_transport = transport

    last_failure_ago_s = _round_age(now_ts, last_failure_at)
    state = "unknown"
    reason = "hub-root transport has not been observed enough yet"
    if last_failure_ago_s is not None and last_failure_ago_s <= 30.0 and (
        last_connected_at is None or (isinstance(last_failure_at, (int, float)) and last_failure_at >= float(last_connected_at or 0.0))
    ):
        state = "down"
        reason = "latest hub-root transport incident is fresh and no newer successful reconnect is recorded"
    elif failures_5m >= 2 or transport_switches_15m >= 2:
        state = "flapping"
        reason = "multiple recent transport failures or transport switches were recorded"
    elif failures_15m >= 1 or attempts_15m > max(1, connects_15m):
        state = "unstable"
        reason = "recent reconnect attempts or failures indicate an unstable hub-root transport"
    elif last_connected_at is not None:
        state = "stable"
        reason = "hub-root transport has a recent successful connect without fresh failures"

    return {
        "state": state,
        "reason": reason,
        "last_event": last_event or None,
        "failures_5m": failures_5m,
        "failures_15m": failures_15m,
        "attempts_15m": attempts_15m,
        "connects_15m": connects_15m,
        "transport_switches_15m": transport_switches_15m,
        "last_failure_at": last_failure_at,
        "last_failure_ago_s": last_failure_ago_s,
        "last_connected_at": last_connected_at,
        "last_connected_ago_s": _round_age(now_ts, last_connected_at),
    }


def hub_root_transport_strategy_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        state = {
            "requested_transport": _HUB_ROOT_TRANSPORT_STATE.get("requested_transport"),
            "effective_transport": _HUB_ROOT_TRANSPORT_STATE.get("effective_transport"),
            "selected_server": _HUB_ROOT_TRANSPORT_STATE.get("selected_server"),
            "url_override": _HUB_ROOT_TRANSPORT_STATE.get("url_override"),
            "current_ws_tag": _HUB_ROOT_TRANSPORT_STATE.get("current_ws_tag"),
            "last_event": _HUB_ROOT_TRANSPORT_STATE.get("last_event"),
            "last_error": _HUB_ROOT_TRANSPORT_STATE.get("last_error"),
            "last_summary": _HUB_ROOT_TRANSPORT_STATE.get("last_summary"),
            "attempt_seq": int(_HUB_ROOT_TRANSPORT_STATE.get("attempt_seq") or 0),
            "last_attempt_at": _HUB_ROOT_TRANSPORT_STATE.get("last_attempt_at"),
            "last_connected_at": _HUB_ROOT_TRANSPORT_STATE.get("last_connected_at"),
            "last_failure_at": _HUB_ROOT_TRANSPORT_STATE.get("last_failure_at"),
            "candidates": list(_HUB_ROOT_TRANSPORT_STATE.get("candidates") or []),
            "failover_policy": _copy_dict(_HUB_ROOT_TRANSPORT_STATE.get("failover_policy")),
            "hypothesis": _copy_dict(_HUB_ROOT_TRANSPORT_STATE.get("hypothesis")),
            "updated_at": _HUB_ROOT_TRANSPORT_STATE.get("updated_at"),
        }
        history = list(_HUB_ROOT_TRANSPORT_HISTORY)
    state["effective_transport"] = _hub_root_transport_from_server(
        state.get("selected_server"),
        explicit_transport=state.get("effective_transport"),
    )
    state["assessment"] = _hub_root_transport_assessment(history, now_ts=now)
    state["updated_ago_s"] = _round_age(now, state.get("updated_at"))
    state["last_attempt_ago_s"] = _round_age(now, state.get("last_attempt_at"))
    state["last_connected_ago_s"] = _round_age(now, state.get("last_connected_at"))
    state["last_failure_ago_s"] = _round_age(now, state.get("last_failure_at"))
    state["recent_events"] = history[-10:]
    return state


def _set_signal(
    signal: RuntimeSignal,
    *,
    status: ReadinessStatus,
    summary: str = "",
    observed: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    signal.status = status
    signal.summary = str(summary or "")
    signal.updated_at = time.time()
    signal.observed = bool(observed)
    signal.details = dict(details or {})


def _record_channel_transition(
    channel: str,
    *,
    previous_status: ReadinessStatus,
    status: ReadinessStatus,
    summary: str,
    details: dict[str, Any] | None,
) -> None:
    if previous_status == status:
        return
    history = _CHANNEL_HISTORY.setdefault(str(channel), deque(maxlen=_CHANNEL_HISTORY_LIMIT))
    history.append(
        {
            "ts": time.time(),
            "previous_status": previous_status.value,
            "status": status.value,
            "summary": str(summary or ""),
            "details": dict(details or {}),
        }
    )


def _record_channel_incident(
    channel: str,
    *,
    status: str,
    summary: str,
    details: dict[str, Any] | None,
    previous_status: str | None = None,
) -> None:
    history = _CHANNEL_HISTORY.setdefault(str(channel), deque(maxlen=_CHANNEL_HISTORY_LIMIT))
    history.append(
        {
            "ts": time.time(),
            "previous_status": str(previous_status or ""),
            "status": str(status or ""),
            "summary": str(summary or ""),
            "details": dict(details or {}),
        }
    )


def _protocol_class_state(traffic_class: str) -> dict[str, Any]:
    key = str(traffic_class or "").strip().lower()
    traffic_classes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("traffic_classes", {})
    state = traffic_classes.get(key)
    if not isinstance(state, dict):
        state = _new_protocol_traffic_class_state(key)
        traffic_classes[key] = state
    state["policy"] = hub_root_protocol_class_policy(key)
    return state


def _protocol_refresh_subjects_locked() -> None:
    subscriptions = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("subscriptions", {})
    traffic_classes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("traffic_classes", {})
    active_by_class: dict[str, list[str]] = {name: [] for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES}
    for subject, entry in subscriptions.items():
        if not isinstance(entry, dict):
            continue
        traffic_class = str(entry.get("traffic_class") or hub_root_protocol_traffic_class(subject))
        if bool(entry.get("active", True)):
            active_by_class.setdefault(traffic_class, []).append(str(subject))
    for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES:
        cls = traffic_classes.setdefault(name, _new_protocol_traffic_class_state(name))
        subjects = sorted(active_by_class.get(name, []))
        cls["policy"] = hub_root_protocol_class_policy(name)
        cls["subjects"] = subjects
        cls["active_subscriptions"] = len(subjects)


def observe_hub_root_protocol_subscription(
    subject: str,
    *,
    traffic_class: str | None = None,
    pending_msgs_limit: int | None = None,
    pending_bytes_limit: int | None = None,
    qsize: int | None = None,
    pending_bytes: int | None = None,
    dispatched: bool = False,
    message_bytes: int | None = None,
    handler_error: str | None = None,
    worker_done: bool | None = None,
) -> None:
    subj = str(subject or "").strip()
    if not subj:
        return
    traffic = str(traffic_class or hub_root_protocol_traffic_class(subj)).strip().lower()
    now = time.time()
    with _LOCK:
        cls = _protocol_class_state(traffic)
        entry = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("subscriptions", {}).setdefault(
            subj,
            {
                "subject": subj,
                "traffic_class": traffic,
                "active": True,
                "dispatch_count": 0,
                "handler_errors": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "last_dispatch_at": 0.0,
                "last_qsize": None,
                "max_qsize": 0,
                "last_pending_bytes": None,
                "max_pending_bytes": 0,
                "pending_msgs_limit": None,
                "pending_bytes_limit": None,
                "worker_done": False,
                "updated_at": 0.0,
                "last_message_bytes": None,
            },
        )
        entry["traffic_class"] = traffic
        entry["active"] = not bool(worker_done)
        if pending_msgs_limit is not None:
            entry["pending_msgs_limit"] = int(pending_msgs_limit)
        elif entry.get("pending_msgs_limit") is None:
            entry["pending_msgs_limit"] = int(cls["policy"].get("pending_msgs_limit") or 0)
        if pending_bytes_limit is not None:
            entry["pending_bytes_limit"] = int(pending_bytes_limit)
        elif entry.get("pending_bytes_limit") is None:
            entry["pending_bytes_limit"] = int(cls["policy"].get("pending_bytes_limit") or 0)
        if qsize is not None:
            q0 = max(0, int(qsize))
            entry["last_qsize"] = q0
            entry["max_qsize"] = max(int(entry.get("max_qsize") or 0), q0)
            cls["last_qsize"] = q0
            cls["max_qsize"] = max(int(cls.get("max_qsize") or 0), q0)
            limit = int(entry.get("pending_msgs_limit") or 0)
            if limit > 0 and q0 >= limit:
                cls["pressure_events"] = int(cls.get("pressure_events") or 0) + 1
        if pending_bytes is not None:
            b0 = max(0, int(pending_bytes))
            entry["last_pending_bytes"] = b0
            entry["max_pending_bytes"] = max(int(entry.get("max_pending_bytes") or 0), b0)
            cls["last_pending_bytes"] = b0
            cls["max_pending_bytes"] = max(int(cls.get("max_pending_bytes") or 0), b0)
        if message_bytes is not None:
            entry["last_message_bytes"] = int(message_bytes)
            cls["last_message_bytes"] = int(message_bytes)
        if dispatched:
            entry["dispatch_count"] = int(entry.get("dispatch_count") or 0) + 1
            entry["last_dispatch_at"] = now
            cls["dispatch_count"] = int(cls.get("dispatch_count") or 0) + 1
            cls["last_dispatch_at"] = now
        if handler_error:
            err = str(handler_error).strip()
            entry["handler_errors"] = int(entry.get("handler_errors") or 0) + 1
            entry["last_error"] = err
            entry["last_error_at"] = now
            cls["handler_errors"] = int(cls.get("handler_errors") or 0) + 1
            cls["last_error"] = err
            cls["last_error_at"] = now
        if worker_done is not None:
            entry["worker_done"] = bool(worker_done)
        entry["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now
        _protocol_refresh_subjects_locked()


def observe_hub_root_protocol_publish(
    subject: str,
    *,
    ok: bool,
    traffic_class: str | None = None,
    payload_bytes: int | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
) -> None:
    subj = str(subject or "").strip()
    if not subj:
        return
    traffic = str(traffic_class or hub_root_protocol_traffic_class(subj)).strip().lower()
    now = time.time()
    with _LOCK:
        cls = _protocol_class_state(traffic)
        if ok:
            cls["publish_ok"] = int(cls.get("publish_ok") or 0) + 1
            cls["last_publish_at"] = now
        else:
            cls["publish_fail"] = int(cls.get("publish_fail") or 0) + 1
            cls["last_error_at"] = now
            cls["last_error"] = str(error or "").strip()
        if payload_bytes is not None:
            cls["last_message_bytes"] = int(payload_bytes)
        if latency_ms is not None:
            cls["last_publish_latency_ms"] = round(float(latency_ms), 3)
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def observe_hub_root_route_runtime(**details: Any) -> None:
    if not details:
        return
    now = time.time()
    with _LOCK:
        route_runtime = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("route_runtime", {})
        for key, value in details.items():
            route_runtime[key] = value
        flows = route_runtime.get("flows")
        if not isinstance(flows, dict):
            route_runtime["flows"] = {
                "control": _new_route_flow_state("control"),
                "frame": _new_route_flow_state("frame"),
            }
        route_runtime["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def observe_hub_root_route_flow(
    flow: str,
    event: str,
    *,
    direction: str | None = None,
    payload_bytes: int | None = None,
    error: str | None = None,
    pending: bool = False,
) -> None:
    key = str(flow or "").strip().lower()
    event_name = str(event or "").strip().lower()
    if key not in {"control", "frame"} or not event_name:
        return
    now = time.time()
    with _LOCK:
        route_runtime = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("route_runtime", {})
        flows = route_runtime.setdefault("flows", {})
        entry = flows.get(key)
        if not isinstance(entry, dict):
            entry = _new_route_flow_state(key)
            flows[key] = entry
        entry["event_total"] = int(entry.get("event_total") or 0) + 1
        entry["last_event"] = event_name
        entry["last_event_at"] = now
        if direction == "to_upstream":
            entry["to_upstream_total"] = int(entry.get("to_upstream_total") or 0) + 1
            if payload_bytes is not None:
                entry["bytes_to_upstream"] = int(entry.get("bytes_to_upstream") or 0) + max(0, int(payload_bytes))
        elif direction == "to_browser":
            entry["to_browser_total"] = int(entry.get("to_browser_total") or 0) + 1
            if payload_bytes is not None:
                entry["bytes_to_browser"] = int(entry.get("bytes_to_browser") or 0) + max(0, int(payload_bytes))
        if pending:
            entry["pending_total"] = int(entry.get("pending_total") or 0) + 1
        if "publish_fail" in event_name:
            entry["publish_fail_total"] = int(entry.get("publish_fail_total") or 0) + 1
        if "send_fail" in event_name:
            entry["send_fail_total"] = int(entry.get("send_fail_total") or 0) + 1
        if "connect_fail" in event_name:
            entry["connect_fail_total"] = int(entry.get("connect_fail_total") or 0) + 1
        if "forced_close" in event_name:
            entry["forced_close_total"] = int(entry.get("forced_close_total") or 0) + 1
        if "upstream_closed" in event_name:
            entry["upstream_close_total"] = int(entry.get("upstream_close_total") or 0) + 1
        if error:
            entry["last_error"] = str(error).strip()
            entry["last_error_at"] = now
        entry["updated_at"] = now
        route_runtime["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def observe_hub_root_integration_outbox(
    name: str,
    *,
    size: int | None = None,
    max_size: int | None = None,
    durable_store: bool | None = None,
    persist_path: str | None = None,
    persisted_size: int | None = None,
    drained: int | None = None,
    dropped: int | None = None,
    publish_ok: int | None = None,
    publish_fail: int | None = None,
    connected: bool | None = None,
    operation_key: str | None = None,
    idempotency_mode: str | None = None,
    cache_hit: int | None = None,
    cache_miss: int | None = None,
    conflict: int | None = None,
    last_error: str | None = None,
) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _LOCK:
        outboxes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("integration_outboxes", {})
        entry = outboxes.setdefault(
            key,
            {
                "name": key,
                "size": 0,
                "max_size": None,
                "durable_store": False,
                "persist_path": "",
                "persisted_size": 0,
                "drained_total": 0,
                "dropped_total": 0,
                "publish_ok": 0,
                "publish_fail": 0,
                "connected": None,
                "idempotency_mode": "operation_key",
                "last_operation_key": "",
                "cache_hit_total": 0,
                "cache_miss_total": 0,
                "conflict_total": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "updated_at": 0.0,
            },
        )
        if size is not None:
            entry["size"] = max(0, int(size))
        if max_size is not None:
            entry["max_size"] = max(0, int(max_size))
        if durable_store is not None:
            entry["durable_store"] = bool(durable_store)
        if persist_path is not None:
            entry["persist_path"] = str(persist_path).strip()
        if persisted_size is not None:
            entry["persisted_size"] = max(0, int(persisted_size))
        if drained is not None:
            entry["drained_total"] = int(entry.get("drained_total") or 0) + max(0, int(drained))
        if dropped is not None:
            entry["dropped_total"] = int(entry.get("dropped_total") or 0) + max(0, int(dropped))
        if publish_ok is not None:
            entry["publish_ok"] = int(entry.get("publish_ok") or 0) + max(0, int(publish_ok))
        if publish_fail is not None:
            entry["publish_fail"] = int(entry.get("publish_fail") or 0) + max(0, int(publish_fail))
        if connected is not None:
            entry["connected"] = bool(connected)
        if operation_key:
            entry["last_operation_key"] = str(operation_key).strip()
        if idempotency_mode:
            entry["idempotency_mode"] = str(idempotency_mode).strip()
        if cache_hit is not None:
            entry["cache_hit_total"] = int(entry.get("cache_hit_total") or 0) + max(0, int(cache_hit))
        if cache_miss is not None:
            entry["cache_miss_total"] = int(entry.get("cache_miss_total") or 0) + max(0, int(cache_miss))
        if conflict is not None:
            entry["conflict_total"] = int(entry.get("conflict_total") or 0) + max(0, int(conflict))
        if last_error:
            entry["last_error"] = str(last_error).strip()
            entry["last_error_at"] = now
        entry["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def reset_reliability_runtime_state() -> None:
    with _LOCK:
        _set_signal(_ROOT_CONTROL, status=ReadinessStatus.UNKNOWN)
        _set_signal(_ROUTE, status=ReadinessStatus.UNKNOWN)
        for name in _INTEGRATION_NAMES:
            _set_signal(_INTEGRATIONS[name], status=ReadinessStatus.UNKNOWN)
        for name in _CHANNEL_NAMES:
            _CHANNEL_HISTORY.setdefault(name, deque(maxlen=_CHANNEL_HISTORY_LIMIT)).clear()
        _HUB_ROOT_TRANSPORT_STATE.update(
            {
                "requested_transport": None,
                "effective_transport": None,
                "selected_server": None,
                "url_override": None,
                "current_ws_tag": None,
                "last_event": "",
                "last_error": "",
                "last_summary": "",
                "attempt_seq": 0,
                "last_attempt_at": 0.0,
                "last_connected_at": 0.0,
                "last_failure_at": 0.0,
                "candidates": [],
                "failover_policy": {},
                "hypothesis": {},
                "updated_at": 0.0,
            }
        )
        _HUB_ROOT_TRANSPORT_HISTORY.clear()
        _HUB_ROOT_PROTOCOL_RUNTIME.clear()
        _HUB_ROOT_PROTOCOL_RUNTIME.update(_new_protocol_runtime())


def mark_root_control_up(*, summary: str = "hub-root control session established", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROOT_CONTROL.status
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "root_control",
            previous_status=previous_status,
            status=ReadinessStatus.READY,
            summary=summary,
            details=details,
        )


def mark_root_control_down(*, summary: str = "hub-root control session unavailable", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROOT_CONTROL.status
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.DOWN,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "root_control",
            previous_status=previous_status,
            status=ReadinessStatus.DOWN,
            summary=summary,
            details=details,
        )
        if _ROUTE.status == ReadinessStatus.READY:
            route_previous_status = _ROUTE.status
            _set_signal(
                _ROUTE,
                status=ReadinessStatus.DEGRADED,
                summary="route path lost authority while root control is down",
                observed=True,
                details={"cause": "root_control_down"},
            )
            _record_channel_transition(
                "route",
                previous_status=route_previous_status,
                status=ReadinessStatus.DEGRADED,
                summary="route path lost authority while root control is down",
                details={"cause": "root_control_down"},
            )


def mark_route_ready(*, summary: str = "hub route relay subscription installed", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROUTE.status
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "route",
            previous_status=previous_status,
            status=ReadinessStatus.READY,
            summary=summary,
            details=details,
        )


def mark_route_degraded(*, summary: str = "hub route relay degraded", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROUTE.status
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.DEGRADED,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "route",
            previous_status=previous_status,
            status=ReadinessStatus.DEGRADED,
            summary=summary,
            details=details,
        )


def note_root_control_reconnect(
    *,
    summary: str = "hub-root transport session was re-established",
    details: dict[str, Any] | None = None,
) -> None:
    with _LOCK:
        _record_channel_incident(
            "root_control",
            previous_status=_ROOT_CONTROL.status.value,
            status="reconnect",
            summary=summary,
            details=details,
        )


def note_route_incident(*, status: str, summary: str, details: dict[str, Any] | None = None) -> None:
    """Record a user-visible incident for the root relay route.

    Example: late reply for an app request, publish errors, repeated timeouts.
    """
    st = str(status or "").strip() or "incident"
    with _LOCK:
        _record_channel_incident(
            "route",
            previous_status=_ROUTE.status.value,
            status=st,
            summary=str(summary or ""),
            details=details,
        )


def observe_route_e2e(*, details: dict[str, Any]) -> None:
    """Update route E2E observations without emitting a readiness transition."""
    if not isinstance(details, dict) or not details:
        return
    with _LOCK:
        try:
            _ROUTE.details.update(dict(details))
        except Exception:
            _ROUTE.details = dict(details)
        _ROUTE.updated_at = time.time()
        _ROUTE.observed = True


def set_integration_readiness(
    name: str,
    *,
    status: ReadinessStatus,
    summary: str = "",
    observed: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("integration name is required")
    with _LOCK:
        signal = _INTEGRATIONS.setdefault(key, RuntimeSignal())
        _set_signal(signal, status=status, summary=summary, observed=observed, details=details)


def runtime_signal_snapshot() -> dict[str, Any]:
    with _LOCK:
        return {
            "root_control": _ROOT_CONTROL.to_dict(),
            "route": _ROUTE.to_dict(),
            "integrations": {name: signal.to_dict() for name, signal in sorted(_INTEGRATIONS.items())},
        }


def effective_channel_view(
    channel_id: str,
    *,
    tree_item: dict[str, Any],
    diag_item: dict[str, Any],
    transport_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(tree_item.get("status") or diag_item.get("status") or ReadinessStatus.UNKNOWN.value)
    stability = diag_item.get("stability") if isinstance(diag_item.get("stability"), dict) else {}
    effective_state = str(stability.get("state") or "unknown")
    assessment = transport_assessment if isinstance(transport_assessment, dict) else {}
    transport_state = str(assessment.get("state") or "").strip().lower()
    if channel_id in {"root_control", "route"} and transport_state in {"down", "unstable", "flapping"}:
        if status == ReadinessStatus.READY.value:
            status = ReadinessStatus.DEGRADED.value
        elif status == ReadinessStatus.UNKNOWN.value and transport_state == "down":
            status = ReadinessStatus.DOWN.value
        if effective_state in {"stable", "unknown"} or transport_state == "down":
            effective_state = transport_state
    return {
        "status": status,
        "state": effective_state,
        "stability": stability,
    }


def _history_count(entries: list[dict[str, Any]], *, within_s: float, now_ts: float, ready: bool | None = None) -> int:
    total = 0
    threshold = now_ts - max(0.0, float(within_s))
    for item in entries:
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts < threshold:
            continue
        status = str(item.get("status") or "")
        if ready is None:
            total += 1
        elif ready and status == ReadinessStatus.READY.value:
            total += 1
        elif ready is False and status != ReadinessStatus.READY.value:
            total += 1
    return total


def _classify_channel_incident(channel: str, entry: dict[str, Any]) -> str | None:
    if not isinstance(entry, dict):
        return None
    status = str(entry.get("status") or "").strip().lower()
    summary = str(entry.get("summary") or "").strip().lower()
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}

    if channel == "root_control":
        if status == "reconnect":
            return "reconnect"
        kind = str(details.get("kind") or "").strip().lower()
        if kind:
            return f"transport_{kind}"
        if status in {ReadinessStatus.DOWN.value, ReadinessStatus.DEGRADED.value}:
            return f"state_{status}"
        if "transport" in summary or "session" in summary:
            return "transport_incident"
        return None

    if channel == "route":
        if status in {
            "late_reply",
            "publish_fail",
            "no_upstream",
            "forced_close_no_upstream",
        }:
            return status
        route_t = str(details.get("t") or "").strip().lower()
        if route_t in {"frame", "chunk", "http", "open", "close"}:
            return f"{route_t}_incident"
        if status in {ReadinessStatus.DOWN.value, ReadinessStatus.DEGRADED.value}:
            cause = str(details.get("cause") or "").strip().lower()
            if cause:
                return f"derived_{cause}"
            return f"state_{status}"
        return status or None

    return status or None


def _incident_class_counts(
    channel: str,
    entries: list[dict[str, Any]],
    *,
    within_s: float,
    now_ts: float,
) -> dict[str, int]:
    threshold = now_ts - max(0.0, float(within_s))
    counts: dict[str, int] = {}
    for item in entries:
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts < threshold:
            continue
        cls = _classify_channel_incident(channel, item)
        if not cls:
            continue
        counts[cls] = int(counts.get(cls) or 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _recent_incident_samples(
    channel: str,
    entries: list[dict[str, Any]],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in entries:
        cls = _classify_channel_incident(channel, item)
        if not cls:
            continue
        samples.append(
            {
                "ts": item.get("ts"),
                "status": item.get("status"),
                "class": cls,
                "summary": item.get("summary"),
                "details": item.get("details") if isinstance(item.get("details"), dict) else {},
            }
        )
    return samples[-max(1, int(limit)) :]


def _last_transition_at(entries: list[dict[str, Any]], *, ready: bool | None = None) -> float | None:
    for item in reversed(entries):
        status = str(item.get("status") or "")
        if ready is None:
            return float(item.get("ts") or 0.0) or None
        if ready and status == ReadinessStatus.READY.value:
            return float(item.get("ts") or 0.0) or None
        if ready is False and status != ReadinessStatus.READY.value:
            return float(item.get("ts") or 0.0) or None
    return None


def _round_age(now_ts: float, ts: float | None) -> float | None:
    if not isinstance(ts, (int, float)) or float(ts) <= 0.0:
        return None
    return round(max(0.0, now_ts - float(ts)), 3)


def _channel_stability_assessment(
    *,
    status: str,
    non_ready_5m: int,
    non_ready_15m: int,
    transitions_5m: int,
) -> dict[str, Any]:
    score = 100
    if status == ReadinessStatus.DOWN.value:
        score -= 45
    elif status == ReadinessStatus.DEGRADED.value:
        score -= 25
    elif status not in {ReadinessStatus.READY.value, ReadinessStatus.UNKNOWN.value}:
        score -= 10

    score -= min(30, non_ready_5m * 15)
    score -= min(15, non_ready_15m * 5)
    score -= min(10, max(0, transitions_5m - 1) * 2)
    score = max(0, min(100, score))

    if status == ReadinessStatus.DOWN.value:
        state = "down"
        reason = "channel is currently down"
    elif non_ready_5m >= 2 or transitions_5m >= 4:
        state = "flapping"
        reason = f"{non_ready_5m} non-ready transitions in the last 5 minutes"
    elif status == ReadinessStatus.DEGRADED.value:
        state = "degraded"
        reason = "channel is currently degraded"
    elif non_ready_5m >= 1:
        state = "unstable"
        reason = f"{non_ready_5m} non-ready incidents in the last 5 minutes"
    elif non_ready_15m >= 3:
        state = "unstable"
        reason = f"{non_ready_15m} non-ready transitions in the last 15 minutes"
    elif status == ReadinessStatus.READY.value:
        state = "stable"
        reason = "channel is ready and no recent flap threshold is exceeded"
    else:
        state = "unknown"
        reason = "channel has not been observed enough yet"

    return {"state": state, "score": score, "reason": reason}


def channel_diagnostics_snapshot() -> dict[str, Any]:
    now_ts = time.time()
    with _LOCK:
        signals = {
            "root_control": _ROOT_CONTROL,
            "route": _ROUTE,
        }
        diagnostics: dict[str, Any] = {}
        for name, signal in signals.items():
            history_entries = list(_CHANNEL_HISTORY.get(name) or [])
            current_status = signal.status.value
            last_ready_at = _last_transition_at(history_entries, ready=True)
            if last_ready_at is None and current_status == ReadinessStatus.READY.value:
                last_ready_at = signal.updated_at or None
            last_non_ready_at = _last_transition_at(history_entries, ready=False)
            if last_non_ready_at is None and current_status not in {
                ReadinessStatus.READY.value,
                ReadinessStatus.UNKNOWN.value,
                ReadinessStatus.NOT_APPLICABLE.value,
            }:
                last_non_ready_at = signal.updated_at or None
            last_transition_at = _last_transition_at(history_entries, ready=None) or signal.updated_at or None
            non_ready_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=False)
            non_ready_15m = _history_count(history_entries, within_s=900.0, now_ts=now_ts, ready=False)
            ready_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=True)
            transitions_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=None)
            incident_classes_5m = _incident_class_counts(name, history_entries, within_s=300.0, now_ts=now_ts)
            incident_classes_15m = _incident_class_counts(name, history_entries, within_s=900.0, now_ts=now_ts)
            recent_incident_samples = _recent_incident_samples(name, history_entries, limit=6)
            last_incident_class = recent_incident_samples[-1]["class"] if recent_incident_samples else None
            stability = _channel_stability_assessment(
                status=current_status,
                non_ready_5m=non_ready_5m,
                non_ready_15m=non_ready_15m,
                transitions_5m=transitions_5m,
            )
            diagnostics[name] = {
                "status": current_status,
                "summary": signal.summary,
                "updated_at": signal.updated_at or None,
                "status_age_s": _round_age(now_ts, signal.updated_at or None),
                "last_transition_at": last_transition_at,
                "last_transition_ago_s": _round_age(now_ts, last_transition_at),
                "last_ready_at": last_ready_at,
                "last_ready_ago_s": _round_age(now_ts, last_ready_at),
                "last_non_ready_at": last_non_ready_at,
                "last_non_ready_ago_s": _round_age(now_ts, last_non_ready_at),
                "recent_non_ready_transitions_5m": non_ready_5m,
                "recent_non_ready_transitions_15m": non_ready_15m,
                "recent_ready_transitions_5m": ready_5m,
                "recent_transitions_5m": transitions_5m,
                "incident_classes_5m": incident_classes_5m,
                "incident_classes_15m": incident_classes_15m,
                "last_incident_class": last_incident_class,
                "total_non_ready_transitions": sum(
                    1 for item in history_entries if str(item.get("status") or "") != ReadinessStatus.READY.value
                ),
                "total_ready_transitions": sum(
                    1 for item in history_entries if str(item.get("status") or "") == ReadinessStatus.READY.value
                ),
                "stable_for_s": _round_age(now_ts, last_ready_at) if current_status == ReadinessStatus.READY.value else None,
                "non_ready_for_s": _round_age(now_ts, last_non_ready_at)
                if current_status not in {ReadinessStatus.READY.value, ReadinessStatus.UNKNOWN.value, ReadinessStatus.NOT_APPLICABLE.value}
                else None,
                "stability": stability,
                "recent_incident_samples": recent_incident_samples,
                "recent_history": history_entries[-8:],
            }
        return diagnostics


def _transport_task_done(record: dict[str, Any], key: str) -> bool:
    task = record.get(key)
    return isinstance(task, dict) and bool(task.get("done"))


def _transport_diag_incident_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not isinstance(record, dict):
        return reasons
    source = str(record.get("source") or "").strip().lower()
    if source and source not in {"periodic"}:
        reasons.append(f"source:{source}")
    if record.get("err"):
        reasons.append("error")
    if record.get("nc_connected") is False or record.get("nc_closed") is True:
        reasons.append("transport_disconnected")
    ws_closed = record.get("ws_closed")
    ws_close_code = record.get("ws_close_code")
    if ws_closed is True or ws_close_code not in {None, "", 1000, "1000"}:
        reasons.append("ws_closed")
    if _transport_task_done(record, "reading_task"):
        reasons.append("reading_task_terminated")
    if _transport_task_done(record, "flusher_task"):
        reasons.append("flusher_task_terminated")
    if _transport_task_done(record, "ping_interval_task"):
        reasons.append("ping_interval_task_terminated")
    # If the reader is gone while the client still claims to be connected,
    # treat this as a stale-but-broken session snapshot.
    if (
        "reading_task_terminated" in reasons
        and record.get("nc_connected") is True
        and "transport_disconnected" not in reasons
    ):
        reasons.append("connected_without_reader")
    return reasons


def assess_transport_diagnostics(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    recent_5m_threshold = now - 300.0
    recent_15m_threshold = now - 900.0
    recent_records_5m = 0
    recent_records_15m = 0
    recent_incidents_5m = 0
    recent_incidents_15m = 0
    recent_hard_incidents_5m = 0
    recent_hard_incidents_15m = 0
    recent_error_records_5m = 0
    recent_error_records_15m = 0
    recent_tags_5m: set[str] = set()
    recent_tags_15m: set[str] = set()
    recent_incident_samples: list[dict[str, Any]] = []
    last_incident_at: float | None = None
    last_incident_reasons: list[str] = []
    last_incident_summary = ""

    hard_incident_markers = {
        "error",
        "transport_disconnected",
        "ws_closed",
        "reading_task_terminated",
        "flusher_task_terminated",
        "ping_interval_task_terminated",
        "connected_without_reader",
        "source:error_cb",
        "source:watchdog",
        "source:eof",
        "source:disconnected",
    }

    for item in records or ():
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0.0 or ts < recent_15m_threshold:
            continue
        recent_records_15m += 1
        if ts >= recent_5m_threshold:
            recent_records_5m += 1

        tag = str(item.get("ws_tag") or item.get("conn_tag") or "").strip()
        if tag:
            recent_tags_15m.add(tag)
            if ts >= recent_5m_threshold:
                recent_tags_5m.add(tag)

        reasons = _transport_diag_incident_reasons(item)
        if not reasons:
            continue

        is_hard = any(marker in hard_incident_markers for marker in reasons)
        recent_incidents_15m += 1
        if ts >= recent_5m_threshold:
            recent_incidents_5m += 1
        if item.get("err"):
            recent_error_records_15m += 1
            if ts >= recent_5m_threshold:
                recent_error_records_5m += 1
        if is_hard:
            recent_hard_incidents_15m += 1
            if ts >= recent_5m_threshold:
                recent_hard_incidents_5m += 1

        recent_incident_samples.append(
            {
                "ts": ts,
                "source": item.get("source"),
                "ws_tag": tag or None,
                "reasons": reasons,
                "err": item.get("err"),
            }
        )
        if last_incident_at is None or ts >= last_incident_at:
            last_incident_at = ts
            last_incident_reasons = list(reasons)
            last_incident_summary = str(item.get("err") or item.get("source") or "").strip()

    recent_tag_changes_5m = max(0, len(recent_tags_5m) - 1)
    recent_tag_changes_15m = max(0, len(recent_tags_15m) - 1)
    last_incident_ago_s = _round_age(now, last_incident_at)
    state = "unknown"
    reason = "no recent transport diagnostics records"

    latest_is_hard = bool(last_incident_reasons) and any(
        marker in hard_incident_markers for marker in last_incident_reasons
    )
    if recent_records_15m > 0:
        state = "stable"
        reason = "recent transport diagnostics show no incident markers"
    if latest_is_hard and isinstance(last_incident_ago_s, (int, float)) and last_incident_ago_s <= 30.0:
        state = "down"
        reason = "latest transport diagnostics show a fresh disconnect/reader failure"
    elif (
        recent_hard_incidents_5m >= 2
        or recent_incidents_5m >= 3
        or recent_tag_changes_5m >= 2
        or recent_hard_incidents_15m >= 3
        or recent_tag_changes_15m >= 3
    ):
        state = "flapping"
        reason = "multiple recent transport incidents or reconnects detected"
    elif (
        recent_hard_incidents_5m >= 1
        or recent_incidents_5m >= 1
        or recent_tag_changes_5m >= 1
        or recent_hard_incidents_15m >= 2
        or recent_tag_changes_15m >= 2
        or recent_incidents_15m >= 2
    ):
        state = "unstable"
        reason = "recent transport incident or reconnect detected"

    return {
        "state": state,
        "reason": reason,
        "recent_records_5m": recent_records_5m,
        "recent_records_15m": recent_records_15m,
        "recent_incidents_5m": recent_incidents_5m,
        "recent_incidents_15m": recent_incidents_15m,
        "recent_hard_incidents_5m": recent_hard_incidents_5m,
        "recent_hard_incidents_15m": recent_hard_incidents_15m,
        "recent_ws_tags_5m": sorted(recent_tags_5m),
        "recent_ws_tags_15m": sorted(recent_tags_15m),
        "recent_tag_changes_5m": recent_tag_changes_5m,
        "recent_tag_changes_15m": recent_tag_changes_15m,
        "recent_error_records_5m": recent_error_records_5m,
        "recent_error_records_15m": recent_error_records_15m,
        "last_incident_at": last_incident_at,
        "last_incident_ago_s": last_incident_ago_s,
        "last_incident_reasons": list(last_incident_reasons),
        "last_incident_summary": last_incident_summary,
        "recent_incident_samples": recent_incident_samples[-6:],
    }


def _node(
    status: ReadinessStatus,
    summary: str,
    *,
    observed: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status.value,
        "summary": summary,
        "observed": observed,
        "details": dict(details or {}),
    }


def _apply_incident_degradation(
    node: dict[str, Any],
    *,
    channel_name: str,
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    current_status = str(node.get("status") or "")
    if current_status != ReadinessStatus.READY.value:
        return node
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    stability = diag.get("stability") if isinstance(diag.get("stability"), dict) else {}
    stability_state = str(stability.get("state") or "")
    if stability_state not in {"unstable", "flapping"}:
        return node
    degraded = dict(node)
    degraded["status"] = ReadinessStatus.DEGRADED.value
    degraded["summary"] = f"{channel_name} is degraded due to recent transport incidents"
    details = dict(node.get("details") or {})
    details.update(
        {
            "derived_from": "channel_incidents",
            "incident_state": stability_state,
            "incident_reason": str(stability.get("reason") or ""),
            "recent_non_ready_transitions_5m": diag.get("recent_non_ready_transitions_5m"),
            "recent_transitions_5m": diag.get("recent_transitions_5m"),
        }
    )
    degraded["details"] = details
    return degraded


def _is_ready(node: dict[str, Any]) -> bool:
    return str(node.get("status") or "") == ReadinessStatus.READY.value


def _derived_integration_node(name: str, root_control: dict[str, Any], observed_signal: dict[str, Any]) -> dict[str, Any]:
    sig_status = str(observed_signal.get("status") or ReadinessStatus.UNKNOWN.value)
    if sig_status != ReadinessStatus.UNKNOWN.value:
        if (
            sig_status == ReadinessStatus.READY.value
            and str(root_control.get("status") or "") != ReadinessStatus.READY.value
        ):
            node = dict(observed_signal)
            node["status"] = ReadinessStatus.DEGRADED.value
            node["summary"] = f"{name} integration probe last succeeded, but root authority is currently unavailable"
            details = dict(observed_signal.get("details") or {})
            details.update(
                {
                    "derived_from": "root_control",
                    "cause": "root_control_not_ready",
                    "last_observed_status": sig_status,
                }
            )
            node["details"] = details
            return node
        return observed_signal
    if str(root_control.get("status") or "") == ReadinessStatus.READY.value:
        return _node(
            ReadinessStatus.DEGRADED,
            f"{name} integration has no dedicated probe yet; derived from root control readiness",
            observed=False,
            details={"derived_from": "root_control"},
        )
    return _node(
        ReadinessStatus.DOWN,
        f"{name} integration is unavailable while root control is not ready",
        observed=False,
        details={"derived_from": "root_control"},
    )


def build_readiness_tree(
    *,
    role: str,
    local_ready: bool,
    node_state: str,
    draining: bool,
    connected_to_hub: bool | None,
    channel_diagnostics: dict[str, Any] | None = None,
    hub_member_channels: dict[str, Any] | None = None,
    hub_member_connection_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = runtime_signal_snapshot()
    diagnostics = channel_diagnostics if isinstance(channel_diagnostics, dict) else channel_diagnostics_snapshot()
    role_norm = str(role or "").strip().lower()
    member_channels = hub_member_channels if isinstance(hub_member_channels, dict) else {}
    member_state = hub_member_connection_state if isinstance(hub_member_connection_state, dict) else {}
    member_assessment = member_state.get("assessment") if isinstance(member_state.get("assessment"), dict) else {}
    member_channels_map = member_channels.get("channels") if isinstance(member_channels.get("channels"), dict) else {}
    member_sync_channel = (
        member_channels_map.get("hub_member.sync")
        if isinstance(member_channels_map.get("hub_member.sync"), dict)
        else {}
    )

    local_core = _node(
        ReadinessStatus.READY if local_ready else ReadinessStatus.DOWN,
        "local runtime is healthy" if local_ready else "local runtime is not ready",
        observed=True,
        details={"node_state": node_state, "draining": bool(draining), "accepting_new_work": not bool(draining)},
    )

    if role_norm == "hub":
        root_control = _apply_incident_degradation(
            signals["root_control"],
            channel_name="hub-root control",
            diagnostics=diagnostics.get("root_control"),
        )
        route_signal = signals["route"]
        route_status = str(route_signal.get("status") or ReadinessStatus.UNKNOWN.value)
        if route_status == ReadinessStatus.UNKNOWN.value:
            if str(root_control.get("status") or "") == ReadinessStatus.READY.value:
                route = _node(
                    ReadinessStatus.DEGRADED,
                    "hub route path not observed yet",
                    observed=False,
                    details={"derived_from": "root_control"},
                )
            else:
                route = _node(
                    ReadinessStatus.DOWN,
                    "hub route path is unavailable while root control is not ready",
                    observed=False,
                    details={"derived_from": "root_control"},
                )
        else:
            route = _apply_incident_degradation(
                route_signal,
                channel_name="root relay route",
                diagnostics=diagnostics.get("route"),
            )
            if (
                str(route.get("status") or "") == ReadinessStatus.READY.value
                and str(root_control.get("status") or "") == ReadinessStatus.DEGRADED.value
            ):
                route = _node(
                    ReadinessStatus.DEGRADED,
                    "root relay route is degraded because hub-root control is degraded by recent incidents",
                    observed=True,
                    details={"derived_from": "root_control_incidents"},
                )

        sync = _node(
            ReadinessStatus.READY if local_ready else ReadinessStatus.DOWN,
            "hub-local sync services are available" if local_ready else "hub-local sync services are unavailable",
            observed=False,
            details={"derived_from": "local_core"},
        )
        integrations = {
            name: _derived_integration_node(name, root_control, sig)
            for name, sig in signals["integrations"].items()
        }
        member_total = int(member_state.get("member_total") or 0)
        member_conn_state = str(member_assessment.get("state") or "").strip().lower()
        member_conn_reason = str(member_assessment.get("reason") or "").strip()
        if member_total <= 0:
            hub_member = _node(
                ReadinessStatus.READY,
                "no members are currently connected",
                observed=False,
                details={"member_total": member_total, "derived_from": "hub_member_connection_state"},
            )
        elif member_conn_state in {"nominal", "transitioning"}:
            hub_member = _node(
                ReadinessStatus.READY,
                member_conn_reason or "hub-member links are healthy",
                observed=True,
                details={"member_total": member_total, "derived_from": "hub_member_connection_state"},
            )
        elif member_conn_state in {"pressure"}:
            hub_member = _node(
                ReadinessStatus.DEGRADED,
                member_conn_reason or "hub-member links are under pressure",
                observed=True,
                details={"member_total": member_total, "derived_from": "hub_member_connection_state"},
            )
        elif member_conn_state in {"degraded", "down"}:
            hub_member = _node(
                ReadinessStatus.DEGRADED if member_conn_state == "degraded" else ReadinessStatus.DOWN,
                member_conn_reason or "hub-member links are unhealthy",
                observed=True,
                details={"member_total": member_total, "derived_from": "hub_member_connection_state"},
            )
        else:
            hub_member = _node(
                ReadinessStatus.UNKNOWN,
                member_conn_reason or "hub-member link state is not observed yet",
                observed=False,
                details={"member_total": member_total, "derived_from": "hub_member_connection_state"},
            )
    else:
        root_control = _node(
            ReadinessStatus.NOT_APPLICABLE,
            "member/browser does not own a direct root control session",
            observed=False,
        )
        route = _node(
            ReadinessStatus.READY if connected_to_hub is True else ReadinessStatus.DOWN if connected_to_hub is False else ReadinessStatus.UNKNOWN,
            "member link to hub is connected"
            if connected_to_hub is True
            else "member link to hub is disconnected"
            if connected_to_hub is False
            else "member link state is unknown",
            observed=True if connected_to_hub is not None else False,
        )
        sync = _node(
            ReadinessStatus.READY if connected_to_hub is True else ReadinessStatus.DOWN if connected_to_hub is False else ReadinessStatus.UNKNOWN,
            "member sync path is available through the active hub link"
            if connected_to_hub is True
            else "member sync path is unavailable because the hub link is down"
            if connected_to_hub is False
            else "member sync path state is unknown",
            observed=False if connected_to_hub is not None else False,
            details={"derived_from": "connected_to_hub"} if connected_to_hub is not None else {},
        )
        integrations = {
            name: _node(
                ReadinessStatus.NOT_APPLICABLE,
                "integration readiness is evaluated on the hub/root side",
                observed=False,
            )
            for name in sorted(signals["integrations"])
        }
        hub_state = str(member_assessment.get("state") or "").strip().lower()
        hub_reason = str(member_assessment.get("reason") or "").strip()
        if hub_state in {"nominal"}:
            hub_member = _node(
                ReadinessStatus.READY,
                hub_reason or "member link to hub is healthy",
                observed=True,
                details={"derived_from": "hub_member_connection_state"},
            )
        elif hub_state in {"pressure", "transitioning"}:
            hub_member = _node(
                ReadinessStatus.DEGRADED,
                hub_reason or "member link to hub is under pressure",
                observed=True,
                details={"derived_from": "hub_member_connection_state"},
            )
        elif hub_state in {"degraded", "down"}:
            hub_member = _node(
                ReadinessStatus.DEGRADED if hub_state == "degraded" else ReadinessStatus.DOWN,
                hub_reason or "member link to hub is unavailable",
                observed=True,
                details={"derived_from": "hub_member_connection_state"},
            )
        else:
            hub_member = _node(
                ReadinessStatus.UNKNOWN,
                hub_reason or "member link state is unknown",
                observed=False,
                details={"derived_from": "hub_member_connection_state"},
            )

    member_sync_status = str(member_sync_channel.get("status") or ReadinessStatus.UNKNOWN.value).strip().lower()
    member_sync_reason = str(member_sync_channel.get("reason") or "").strip()
    member_sync_active_path = str(member_sync_channel.get("active_path") or "").strip()
    if role_norm == "hub" and int(member_state.get("member_total") or 0) <= 0:
        member_sync = _node(
            ReadinessStatus.READY,
            "member sync channels are idle because no members are connected",
            observed=False,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )
    elif member_sync_status == ReadinessStatus.READY.value:
        member_sync = _node(
            ReadinessStatus.READY,
            member_sync_reason or "member sync channel is healthy",
            observed=True,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )
    elif member_sync_status == ReadinessStatus.DEGRADED.value:
        member_sync = _node(
            ReadinessStatus.DEGRADED,
            member_sync_reason or "member sync channel is degraded",
            observed=True,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )
    elif member_sync_status == ReadinessStatus.DOWN.value:
        member_sync = _node(
            ReadinessStatus.DOWN,
            member_sync_reason or "member sync channel is down",
            observed=True,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )
    elif member_sync_status == ReadinessStatus.NOT_APPLICABLE.value:
        member_sync = _node(
            ReadinessStatus.NOT_APPLICABLE,
            member_sync_reason or "member sync channel is not applicable",
            observed=False,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )
    else:
        member_sync = _node(
            ReadinessStatus.UNKNOWN,
            member_sync_reason or "member sync channel state is unknown",
            observed=False,
            details={"active_path": member_sync_active_path, "derived_from": "hub_member_channels"},
        )

    media = _node(
        ReadinessStatus.UNKNOWN,
        "media plane is not part of the first-stage readiness hardening",
        observed=False,
    )

    return {
        "hub_local_core": local_core,
        "root_control": root_control,
        "route": route,
        "sync": sync,
        "hub_member": hub_member,
        "member_sync": member_sync,
        "integration": integrations,
        "media": media,
    }


def _matrix_entry(*, allowed: bool, reason: str, required_ready: list[str]) -> dict[str, Any]:
    return {
        "allowed": bool(allowed),
        "reason": reason,
        "required_ready": list(required_ready),
    }


def build_degraded_matrix(*, role: str, readiness_tree: dict[str, Any]) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    local_core = readiness_tree["hub_local_core"]
    root_control = readiness_tree["root_control"]
    route = readiness_tree["route"]
    hub_member = readiness_tree.get("hub_member") if isinstance(readiness_tree.get("hub_member"), dict) else {}
    member_sync = readiness_tree.get("member_sync") if isinstance(readiness_tree.get("member_sync"), dict) else {}
    integrations = readiness_tree["integration"]

    local_ok = _is_ready(local_core)
    root_ok = _is_ready(root_control)
    route_ok = _is_ready(route)
    hub_member_ok = _is_ready(hub_member)
    member_sync_ok = _is_ready(member_sync)
    tg_ok = _is_ready(integrations.get("telegram", {}))
    gh_ok = _is_ready(integrations.get("github", {}))
    llm_ok = _is_ready(integrations.get("llm", {}))

    base = {
        "execute_local_scenarios": _matrix_entry(
            allowed=local_ok,
            reason="local scenario execution depends only on hub local core readiness",
            required_ready=["hub_local_core"],
        ),
        "existing_local_member_sessions": _matrix_entry(
            allowed=local_ok,
            reason="existing local sessions may continue while the local core remains healthy",
            required_ready=["hub_local_core"],
        ),
    }

    if role_norm == "hub":
        base.update(
            {
                "new_root_backed_member_admission": _matrix_entry(
                    allowed=local_ok and root_ok,
                    reason="new root-backed admissions require fresh root control authority",
                    required_ready=["hub_local_core", "root_control"],
                ),
                "root_routed_browser_proxy": _matrix_entry(
                    allowed=local_ok and root_ok and route_ok,
                    reason="root-routed browser proxy requires local core, root control, and route readiness",
                    required_ready=["hub_local_core", "root_control", "route"],
                ),
                "telegram_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and tg_ok,
                    reason="Telegram completion requires local core, root control, and Telegram integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.telegram"],
                ),
                "github_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and gh_ok,
                    reason="GitHub completion requires local core, root control, and GitHub integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.github"],
                ),
                "llm_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and llm_ok,
                    reason="Root-backed LLM completion requires local core, root control, and LLM integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.llm"],
                ),
                "core_update_coordination_via_root": _matrix_entry(
                    allowed=local_ok and root_ok,
                    reason="Core update coordination depends on local core and root control readiness",
                    required_ready=["hub_local_core", "root_control"],
                ),
                "remote_member_snapshot_projection": _matrix_entry(
                    allowed=local_ok and hub_member_ok,
                    reason="Remote member projection requires local core and healthy hub-member control links",
                    required_ready=["hub_local_core", "hub_member"],
                ),
                "hub_triggered_member_update_follow": _matrix_entry(
                    allowed=local_ok and hub_member_ok,
                    reason="Hub-triggered member rollout requires local core and healthy hub-member control links",
                    required_ready=["hub_local_core", "hub_member"],
                ),
                "member_sync_projection": _matrix_entry(
                    allowed=local_ok and hub_member_ok and member_sync_ok,
                    reason="Member sync projection requires local core, hub-member control, and member sync readiness",
                    required_ready=["hub_local_core", "hub_member", "member_sync"],
                ),
            }
        )
    else:
        base.update(
            {
                "new_root_backed_member_admission": _matrix_entry(
                    allowed=False,
                    reason="member/browser role does not own root-backed admissions",
                    required_ready=[],
                ),
                "root_routed_browser_proxy": _matrix_entry(
                    allowed=local_ok and route_ok,
                    reason="member/browser route availability depends on local core and the current hub path",
                    required_ready=["hub_local_core", "route"],
                ),
                "telegram_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "github_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "llm_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "core_update_coordination_via_root": _matrix_entry(
                    allowed=False,
                    reason="member/browser role does not coordinate core updates via root",
                    required_ready=[],
                ),
                "remote_member_snapshot_projection": _matrix_entry(
                    allowed=False,
                    reason="member/browser role does not project other remote members",
                    required_ready=[],
                ),
                "hub_triggered_member_update_follow": _matrix_entry(
                    allowed=local_ok and hub_member_ok,
                    reason="Following hub-triggered updates requires local core and a healthy hub-member control link",
                    required_ready=["hub_local_core", "hub_member"],
                ),
                "member_sync_projection": _matrix_entry(
                    allowed=local_ok and hub_member_ok and member_sync_ok,
                    reason="Member sync projection requires local core, hub-member control, and member sync readiness",
                    required_ready=["hub_local_core", "hub_member", "member_sync"],
                ),
            }
        )

    return base


def channel_overview_snapshot(
    *,
    readiness_tree: dict[str, Any],
    channel_diagnostics: dict[str, Any],
    transport_strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = transport_strategy if isinstance(transport_strategy, dict) else {}
    assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}

    root_tree = readiness_tree.get("root_control") if isinstance(readiness_tree.get("root_control"), dict) else {}
    root_diag = channel_diagnostics.get("root_control") if isinstance(channel_diagnostics.get("root_control"), dict) else {}
    route_tree = readiness_tree.get("route") if isinstance(readiness_tree.get("route"), dict) else {}
    route_diag = channel_diagnostics.get("route") if isinstance(channel_diagnostics.get("route"), dict) else {}
    sync_tree = readiness_tree.get("sync") if isinstance(readiness_tree.get("sync"), dict) else {}
    sync_diag: dict[str, Any] = {}

    hub_root = effective_channel_view(
        "root_control",
        tree_item=root_tree,
        diag_item=root_diag,
        transport_assessment=assessment,
    )
    hub_root_browser = effective_channel_view(
        "route",
        tree_item=route_tree,
        diag_item=route_diag,
        transport_assessment=assessment,
    )
    browser_hub_sync = effective_channel_view(
        "sync",
        tree_item=sync_tree,
        diag_item=sync_diag,
        transport_assessment={},
    )

    return {
        "hub_root": {
            "channel_id": "root_control",
            "title": "Hub -> Root control",
            "effective_status": hub_root.get("status"),
            "effective_state": hub_root.get("state"),
            "readiness": root_tree,
            "diagnostics": root_diag,
        },
        "hub_root_browser": {
            "channel_id": "route",
            "title": "Hub -> Root -> Browser relay",
            "effective_status": hub_root_browser.get("status"),
            "effective_state": hub_root_browser.get("state"),
            "readiness": route_tree,
            "diagnostics": route_diag,
        },
        "browser_hub_sync": {
            "channel_id": "sync",
            "title": "Browser -> Hub sync",
            "effective_status": browser_hub_sync.get("status"),
            "effective_state": browser_hub_sync.get("state"),
            "readiness": sync_tree,
            "diagnostics": sync_diag,
        },
    }


def hub_member_semantic_channel_model_snapshot() -> dict[str, Any]:
    return {
        "channels": [item.to_dict() for item in HUB_MEMBER_CHANNEL_SPECS],
        "design_rules": {
            "single_active_authority_path": True,
            "freeze_before_preferred_switch": True,
            "transport_names_are_not_semantics": True,
        },
    }


def _new_hub_member_channel_state(spec: SemanticChannelSpec) -> dict[str, Any]:
    return {
        "channel_id": spec.channel_id,
        "active_path": None,
        "preferred_path": None,
        "last_switch_at": 0.0,
        "switch_total": 0,
        "previous_path": None,
    }


def _sidecar_lifecycle_manager() -> str:
    token = str(os.getenv("ADAOS_SUPERVISOR_ENABLED", "0") or "").strip().lower()
    return "supervisor" if token in {"1", "true", "yes", "on"} else "runtime"


def _sidecar_route_tunnel_entry(route_tunnel_contract: dict[str, Any] | None, key: str) -> dict[str, Any]:
    payload = route_tunnel_contract if isinstance(route_tunnel_contract, dict) else {}
    entry = payload.get(str(key or "").strip())
    return dict(entry) if isinstance(entry, dict) else {}


def _sidecar_route_tunnel_state(*, enabled: bool, entry: dict[str, Any] | None) -> str:
    payload = entry if isinstance(entry, dict) else {}
    current_owner = str(payload.get("current_owner") or "").strip().lower()
    planned_owner = str(payload.get("planned_owner") or "").strip().lower()
    current_support = str(payload.get("current_support") or "").strip().lower()
    delegation_mode = str(payload.get("delegation_mode") or "").strip().lower()
    listener_ready = bool(payload.get("listener_ready"))
    handoff_ready = bool(payload.get("handoff_ready"))

    if current_owner == "sidecar":
        if handoff_ready:
            return "ready"
        if listener_ready:
            return "starting"
        return "degraded" if enabled else "disabled"
    if planned_owner == "sidecar":
        if listener_ready or current_support == "proxy_ready" or delegation_mode in {"local_tcp_proxy", "local_ws_proxy"}:
            return "proxy_ready" if listener_ready or current_support == "proxy_ready" else "planned"
        return "disabled" if not enabled or current_support == "disabled" else "planned"
    if current_owner == "runtime":
        if listener_ready or current_support == "proxy_ready" or delegation_mode in {"local_tcp_proxy", "local_ws_proxy"}:
            return "proxy_ready" if listener_ready or current_support == "proxy_ready" else "not_owned"
        return "not_owned"
    return "unknown"


def _sidecar_scope_snapshot(*, enabled: bool, route_tunnel_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    ws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")
    current_boundaries: list[str] = ["hub_root_transport"] if enabled else []
    runtime_fallback_boundaries: list[str] = ["hub_root_transport"] if not enabled else []
    planned_next_boundaries: list[str] = []

    for boundary, entry in (
        ("browser_events_ws", ws_entry),
        ("browser_yjs_ws", yws_entry),
    ):
        current_owner = str(entry.get("current_owner") or "").strip().lower()
        planned_owner = str(entry.get("planned_owner") or "").strip().lower()
        if current_owner == "sidecar":
            current_boundaries.append(boundary)
            continue
        runtime_fallback_boundaries.append(boundary)
        if planned_owner == "sidecar":
            planned_next_boundaries.append(boundary)

    def _uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            key = str(item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result

    return {
        "current_boundaries": _uniq(current_boundaries),
        "runtime_fallback_boundaries": _uniq(runtime_fallback_boundaries),
        "planned_next_boundaries": _uniq(planned_next_boundaries),
        "planned_later_boundaries": [
            "webrtc_signaling",
            "webrtc_media",
            "yjs_session_authority",
        ],
    }


def _media_update_guard_snapshot(*, role: str, runtime: dict[str, Any] | None) -> dict[str, Any]:
    payload = runtime if isinstance(runtime, dict) else {}
    role_norm = str(role or "").strip().lower() or None
    route_intent = payload.get("route_intent") if isinstance(payload.get("route_intent"), dict) else {}
    attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else {}
    member_browser = payload.get("member_browser_direct") if isinstance(payload.get("member_browser_direct"), dict) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}

    connected_browser_session_total = int(
        member_browser.get("connected_browser_session_total")
        or member_browser.get("browser_session_total")
        or 0
    )
    live_connected_peers = int(counts.get("live_connected_peers") or 0)
    live_tracks_total = sum(
        int(counts.get(key) or 0)
        for key in (
            "incoming_audio_tracks",
            "incoming_video_tracks",
            "loopback_audio_tracks",
            "loopback_video_tracks",
        )
    )

    observed_live_topology: str | None = None
    if bool(member_browser.get("ready")) and connected_browser_session_total > 0:
        observed_live_topology = "member_browser_direct"
    elif live_connected_peers > 0 or live_tracks_total > 0:
        observed_live_topology = "hub_webrtc_loopback"

    live_session_present = observed_live_topology is not None
    member_runtime_update = "allow"
    hub_runtime_update = "allow"
    hub_sidecar_continuity_required = False
    current_support = "not_applicable"
    criticality = "idle"
    reason = "no live media session observed"

    if observed_live_topology == "member_browser_direct":
        member_runtime_update = "defer"
        hub_runtime_update = "preserve_sidecar"
        hub_sidecar_continuity_required = True
        current_support = "planned"
        criticality = "member_live_media"
        reason = (
            "member owns the active browser media path; member update should be deferred and "
            "hub restart should preserve an independent sidecar continuity path"
        )
    elif observed_live_topology == "hub_webrtc_loopback":
        hub_runtime_update = "preserve_sidecar"
        hub_sidecar_continuity_required = True
        current_support = "planned"
        criticality = "hub_live_media"
        reason = (
            "hub participates in the active live media path; target behavior is to keep sidecar alive "
            "while the hub runtime restarts"
        )

    return {
        "role": role_norm,
        "live_session_present": live_session_present,
        "observed_live_topology": observed_live_topology,
        "active_route": payload.get("active_route") or route_intent.get("active_route") or attempt.get("active_route"),
        "preferred_route": payload.get("preferred_route") or route_intent.get("preferred_route") or attempt.get("preferred_route"),
        "member_runtime_update": member_runtime_update,
        "hub_runtime_update": hub_runtime_update,
        "hub_sidecar_continuity_required": hub_sidecar_continuity_required,
        "current_support": current_support,
        "criticality": criticality,
        "reason": reason,
    }


def _sidecar_continuity_contract(
    *,
    enabled: bool,
    media_runtime: dict[str, Any] | None,
    route_tunnel_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = media_runtime if isinstance(media_runtime, dict) else {}
    guard = payload.get("update_guard") if isinstance(payload.get("update_guard"), dict) else {}
    required = bool(guard.get("hub_sidecar_continuity_required"))
    member_policy = str(guard.get("member_runtime_update") or "allow")
    hub_policy = str(guard.get("hub_runtime_update") or "allow")
    ws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")
    required_boundaries = ["browser_events_ws", "browser_yjs_ws"] if required else []
    ready_boundaries: list[str] = []
    pending_boundaries: list[str] = []
    blockers: list[str] = []

    for boundary, entry in (
        ("browser_events_ws", ws_entry),
        ("browser_yjs_ws", yws_entry),
    ):
        current_owner = str(entry.get("current_owner") or "").strip().lower()
        handoff_ready = bool(entry.get("handoff_ready"))
        if current_owner == "sidecar" and handoff_ready:
            ready_boundaries.append(boundary)
            continue
        if required:
            pending_boundaries.append(boundary)
            blocker = next(
                (
                    str(item).strip()
                    for item in (entry.get("blockers") or [])
                    if str(item).strip()
                ),
                "",
            )
            if blocker:
                blockers.append(f"{boundary}: {blocker}")

    if not required:
        current_support = "not_applicable"
    elif not enabled:
        current_support = "disabled"
    elif pending_boundaries and ready_boundaries:
        current_support = "partial"
    elif pending_boundaries:
        current_support = "planned"
    else:
        current_support = "ready"
    reason = str(guard.get("reason") or "").strip() or (
        "no live media continuity requirement observed"
        if not required
        else "live media continuity requires sidecar independence from the hub runtime"
    )
    return {
        "required": required,
        "enabled": bool(enabled),
        "member_runtime_update": member_policy,
        "hub_runtime_update": hub_policy,
        "observed_live_topology": guard.get("observed_live_topology"),
        "current_support": current_support,
        "required_boundaries": required_boundaries,
        "ready_boundaries": ready_boundaries,
        "pending_boundaries": pending_boundaries,
        "blockers": blockers,
        "target_behavior": (
            "keep sidecar alive while the hub runtime restarts during live media sessions"
            if required
            else "transport sidecar currently isolates only hub_root transport"
        ),
        "reason": reason,
    }


def _sidecar_progress_snapshot(
    *,
    enabled: bool,
    transport_ready: bool,
    lifecycle_manager: str,
    route_tunnel_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    ws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")

    def _step_blocker(entry: dict[str, Any]) -> str | None:
        return next((str(item).strip() for item in (entry.get("blockers") or []) if str(item).strip()), None)

    def _handoff_step(step_id: str, title: str, entry: dict[str, Any]) -> dict[str, Any]:
        current_owner = str(entry.get("current_owner") or "").strip().lower()
        planned_owner = str(entry.get("planned_owner") or "").strip().lower()
        handoff_ready = bool(entry.get("handoff_ready"))
        listener_ready = bool(entry.get("listener_ready"))
        blocker = _step_blocker(entry)
        if current_owner == "sidecar" and handoff_ready:
            status = "completed"
        elif current_owner == "sidecar" or planned_owner == "sidecar":
            status = "in_progress"
        else:
            status = "planned"
        return {
            "id": step_id,
            "title": title,
            "status": status,
            "active_on_node": current_owner == "sidecar",
            "ready_on_node": current_owner == "sidecar" and handoff_ready,
            "listener_ready": listener_ready,
            "blocker": blocker,
            "delegation_mode": entry.get("delegation_mode"),
            "summary": (
                "handoff is complete"
                if status == "completed"
                else (
                    "sidecar local proxy listener is ready, but public ownership cutover is still pending"
                    if listener_ready
                    else blocker or "ownership handoff is not complete yet"
                )
            ),
        }

    milestones = [
        {
            "id": "hub_root_transport_sidecar",
            "title": "Hub-root transport sidecar",
            "status": "completed",
            "active_on_node": bool(enabled),
            "ready_on_node": bool(transport_ready),
            "summary": "sidecar owns the hub-root transport boundary",
        },
        {
            "id": "supervisor_managed_sidecar",
            "title": "Supervisor-managed sidecar lifecycle",
            "status": "completed",
            "active_on_node": str(lifecycle_manager or "").strip().lower() == "supervisor",
            "ready_on_node": str(lifecycle_manager or "").strip().lower() == "supervisor",
            "summary": "supervisor-managed sidecar lifecycle is implemented",
        },
        _handoff_step("browser_events_ws_handoff", "Browser /ws handoff", ws_entry),
        _handoff_step("browser_yjs_ws_handoff", "Browser /yws handoff", yws_entry),
    ]
    completed = sum(1 for item in milestones if str(item.get("status") or "") == "completed")
    total = len(milestones)
    percent = int((completed * 100) / total) if total > 0 else 0
    current = next((item for item in milestones if str(item.get("status") or "") != "completed"), None)
    next_blocker = str(current.get("blocker") or "").strip() or None if isinstance(current, dict) else None
    return {
        "target": "first_browser_realtime_tunnel",
        "state": "ready" if completed >= total and total > 0 else "in_progress",
        "completed_milestones": completed,
        "milestone_total": total,
        "percent": percent,
        "current_milestone": current.get("id") if isinstance(current, dict) else None,
        "next_blocker": next_blocker,
        "summary": f"{completed}/{total} milestones completed toward first browser realtime sidecar use case",
        "milestones": milestones,
        "future_targets": [
            "live_media_continuity",
            "webrtc_signaling",
            "webrtc_media",
        ],
    }


def _hub_member_transport_evidence_snapshot(
    *,
    role: str,
    route_mode: str | None,
    connected_to_hub: bool | None,
    hub_root_protocol: dict[str, Any],
) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    evidence: dict[str, dict[str, Any]] = {
        "webrtc_data:events": {"available": False, "source": "webrtc.peer"},
        "webrtc_data:yjs": {"available": False, "source": "webrtc.peer"},
        "ws": {"available": False, "source": "gateway_ws"},
        "yws": {"available": False, "source": "gateway_ws"},
        "root_route_proxy": {"available": False, "source": "hub_root.route"},
        "member_link_ws": {
            "available": False,
            "source": "subnet.link_client",
            "route_mode": route_mode,
            "connected_to_subnet": _connected_to_subnet_alias(connected_to_hub),
            "connected_to_hub": connected_to_hub,
        },
        "webrtc_media": {"available": False, "source": "webrtc.peer"},
        "member_browser_webrtc_media": {
            "available": False,
            "source": "router.media_route",
        },
        "root_media_relay": {"available": False, "source": "root.media"},
    }

    if role_norm == "hub":
        try:
            from adaos.services.yjs.gateway_ws import gateway_transport_snapshot

            gateway = gateway_transport_snapshot()
        except Exception:
            gateway = {}
        transports = gateway.get("transports") if isinstance(gateway.get("transports"), dict) else {}
        ownership = gateway.get("ownership") if isinstance(gateway.get("ownership"), dict) else {}
        ws_entry = transports.get("ws") if isinstance(transports.get("ws"), dict) else {}
        yws_entry = transports.get("yws") if isinstance(transports.get("yws"), dict) else {}
        ws_ownership = ownership.get("ws") if isinstance(ownership.get("ws"), dict) else {}
        yws_ownership = ownership.get("yws") if isinstance(ownership.get("yws"), dict) else {}
        evidence["ws"].update(
            {
                "available": int(ws_entry.get("active_connections") or 0) > 0,
                "active_connections": int(ws_entry.get("active_connections") or 0),
                "last_open_ago_s": ws_entry.get("last_open_ago_s"),
                "owner": ws_ownership.get("current_owner") or "runtime",
                "lifecycle_manager": ws_ownership.get("lifecycle_manager"),
                "planned_owner": ws_ownership.get("planned_owner"),
                "migration_phase": ws_ownership.get("migration_phase"),
                "handoff_ready": bool(ws_ownership.get("handoff_ready")),
                "handoff_blockers": list(ws_ownership.get("handoff_blockers") or []),
            }
        )
        evidence["yws"].update(
            {
                "available": int(yws_entry.get("active_connections") or 0) > 0,
                "browser_hub_only": True,
                "runtime_bound": False,
                "active_connections": int(yws_entry.get("active_connections") or 0),
                "last_open_ago_s": yws_entry.get("last_open_ago_s"),
                "recent_open_10s": int(yws_entry.get("recent_open_10s") or 0),
                "storm_detected": bool(yws_entry.get("storm_detected")),
                "owner": yws_ownership.get("current_owner") or "runtime",
                "lifecycle_manager": yws_ownership.get("lifecycle_manager"),
                "planned_owner": yws_ownership.get("planned_owner"),
                "migration_phase": yws_ownership.get("migration_phase"),
                "handoff_ready": bool(yws_ownership.get("handoff_ready")),
                "handoff_blockers": list(yws_ownership.get("handoff_blockers") or []),
            }
        )

        try:
            from adaos.services.webrtc.peer import webrtc_peer_snapshot

            webrtc = webrtc_peer_snapshot()
        except Exception:
            webrtc = {}
        evidence["webrtc_data:events"].update(
            {
                "available": int(webrtc.get("open_events_channels") or 0) > 0,
                "peer_total": int(webrtc.get("peer_total") or 0),
                "open_channels": int(webrtc.get("open_events_channels") or 0),
            }
        )
        evidence["webrtc_data:yjs"].update(
            {
                "available": int(webrtc.get("open_yjs_channels") or 0) > 0,
                "browser_hub_only": True,
                "runtime_bound": False,
                "peer_total": int(webrtc.get("peer_total") or 0),
                "open_channels": int(webrtc.get("open_yjs_channels") or 0),
            }
        )
        try:
            from adaos.services.subnet.link_manager import hub_link_manager_snapshot

            hub_links = hub_link_manager_snapshot()
        except Exception:
            hub_links = {}
        connected_member_total = int(hub_links.get("connected_total") or hub_links.get("member_total") or 0)
        yjs_replication = (
            hub_links.get("yjs_replication")
            if isinstance(hub_links.get("yjs_replication"), dict)
            else {}
        )
        evidence["member_link_ws"].update(
            {
                "available": connected_member_total > 0,
                "source": "subnet.link_manager",
                "connected_total": connected_member_total,
                "member_total": int(hub_links.get("member_total") or connected_member_total),
                "yjs_replication": yjs_replication,
            }
        )
        evidence["webrtc_media"].update(
            {
                "available": (
                    int(webrtc.get("incoming_audio_tracks") or 0) > 0
                    or int(webrtc.get("incoming_video_tracks") or 0) > 0
                    or int(webrtc.get("loopback_audio_tracks") or 0) > 0
                    or int(webrtc.get("loopback_video_tracks") or 0) > 0
                ),
                "peer_total": int(webrtc.get("peer_total") or 0),
                "connected_peers": int(webrtc.get("connected_peers") or 0),
                "incoming_audio_tracks": int(webrtc.get("incoming_audio_tracks") or 0),
                "incoming_video_tracks": int(webrtc.get("incoming_video_tracks") or 0),
                "loopback_audio_tracks": int(webrtc.get("loopback_audio_tracks") or 0),
                "loopback_video_tracks": int(webrtc.get("loopback_video_tracks") or 0),
            }
        )

        route_runtime = hub_root_protocol.get("route_runtime") if isinstance(hub_root_protocol.get("route_runtime"), dict) else {}
        route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
        route_control = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
        route_frame = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
        route_available = (
            int(route_runtime.get("active_tunnels") or 0) > 0
            or int(route_runtime.get("pending_tunnels") or 0) > 0
            or str(route_control.get("state") or "") in {"active", "pressure", "degraded"}
            or str(route_frame.get("state") or "") in {"active", "pressure", "degraded"}
        )
        evidence["root_route_proxy"].update(
            {
                "available": bool(route_available),
                "active_tunnels": int(route_runtime.get("active_tunnels") or 0),
                "pending_tunnels": int(route_runtime.get("pending_tunnels") or 0),
                "control_state": str(route_control.get("state") or ""),
                "frame_state": str(route_frame.get("state") or ""),
            }
        )
        evidence["root_media_relay"].update(
            {
                "available": bool(route_available),
                "active_tunnels": int(route_runtime.get("active_tunnels") or 0),
                "pending_tunnels": int(route_runtime.get("pending_tunnels") or 0),
                "control_state": str(route_control.get("state") or ""),
                "frame_state": str(route_frame.get("state") or ""),
            }
        )
        try:
            from adaos.services.yjs.gateway_ws import active_browser_session_snapshot

            browser_snapshot = active_browser_session_snapshot()
        except Exception:
            browser_snapshot = {}
        browser_peers = (
            browser_snapshot.get("peers")
            if isinstance(browser_snapshot.get("peers"), list)
            else []
        )
        browser_session_total = sum(1 for item in browser_peers if isinstance(item, dict))
        connected_browser_session_total = sum(
            1
            for item in browser_peers
            if isinstance(item, dict)
            and str(item.get("connection_state") or "").strip().lower() == "connected"
        )
        try:
            from adaos.services.media_capability import member_browser_direct_foundation

            member_browser_direct = member_browser_direct_foundation(
                browser_session_total=browser_session_total,
                connected_browser_session_total=connected_browser_session_total,
                admitted=False,
            )
        except Exception:
            member_browser_direct = {
                "possible": False,
                "admitted": False,
                "ready": False,
                "reason": "member_browser_direct_inventory_unavailable",
                "candidate_member_total": 0,
                "candidate_members": [],
                "preferred_member_id": None,
                "preferred_candidate_source": None,
                "browser_session_total": browser_session_total,
                "connected_browser_session_total": connected_browser_session_total,
            }
        evidence["member_browser_webrtc_media"].update(
            {
                "available": bool(member_browser_direct.get("ready")),
                "possible": bool(member_browser_direct.get("possible")),
                "admitted": bool(member_browser_direct.get("admitted")),
                "reason": str(member_browser_direct.get("reason") or ""),
                "candidate_member_total": int(member_browser_direct.get("candidate_member_total") or 0),
                "candidate_members": list(member_browser_direct.get("candidate_members") or []),
                "preferred_member_id": str(member_browser_direct.get("preferred_member_id") or "") or None,
                "preferred_candidate_source": str(member_browser_direct.get("preferred_candidate_source") or "") or None,
                "browser_session_total": int(member_browser_direct.get("browser_session_total") or 0),
                "connected_browser_session_total": int(member_browser_direct.get("connected_browser_session_total") or 0),
            }
        )
    else:
        member_available = bool(connected_to_hub is True or str(route_mode or "").strip().lower() == "ws")
        evidence["member_link_ws"]["available"] = member_available

    return evidence


def _semantic_channel_status(
    *,
    spec: SemanticChannelSpec,
    role_norm: str,
    active_path: str | None,
    preferred_path: str | None,
    freeze_remaining_s: float,
) -> tuple[str, str, str]:
    if spec.channel_id == "hub_member.media":
        if not active_path:
            return (
                "down",
                "unavailable",
                "bounded media relay is not currently active",
            )
        if active_path == "root_media_relay":
            return (
                "ready",
                "bounded_relay",
                "root bounded media relay is the active authority path",
            )
        if active_path == "member_browser_webrtc_media":
            return (
                "ready",
                "member_browser_direct",
                "browser-member direct media path is the active authority path",
            )
        if active_path.startswith("webrtc_media"):
            return ("ready", "direct_media", "direct media path is active")
        return ("ready", "active", f"{active_path} is the active media authority path")
    if role_norm != "hub" and spec.channel_id == "hub_member.route":
        return (
            "not_applicable",
            "not_applicable",
            "route relay semantics are evaluated on the hub/root runtime",
        )
    if not active_path:
        if spec.channel_id == "hub_member.route":
            return ("down", "unavailable", "root route relay is not currently active")
        return ("down", "unavailable", "no candidate path is currently active")
    if freeze_remaining_s > 0.0:
        return (
            "ready",
            "freeze_hold",
            f"holding {active_path} during freeze window before switching to {preferred_path or active_path}",
        )
    if active_path == "root_route_proxy":
        return ("ready", "relay_fallback", "root relay path is the active authority path")
    if active_path == "member_link_ws":
        return ("ready", "member_link", "member link websocket is the active authority path")
    if active_path.startswith("webrtc_data:"):
        return ("ready", "direct_p2p", "direct WebRTC datachannel is the active authority path")
    if active_path in {"ws", "yws"}:
        return ("ready", "direct_ws", "direct websocket is the active authority path")
    if active_path == "member_browser_webrtc_media":
        return ("ready", "member_browser_direct", "browser-member direct media path is active")
    if active_path.startswith("webrtc_media"):
        return ("ready", "direct_media", "direct media path is active")
    return ("ready", "active", f"{active_path} is the active authority path")


def _hub_member_media_route_contract(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    try:
        from adaos.services.router.media_routes import resolve_media_route_intent
    except Exception:
        return {}

    member_browser = (
        evidence.get("member_browser_webrtc_media")
        if isinstance(evidence.get("member_browser_webrtc_media"), dict)
        else {}
    )
    return resolve_media_route_intent(
        need="live_stream",
        direct_local_ready=False,
        root_routed_ready=bool((evidence.get("root_media_relay") or {}).get("available")),
        hub_webrtc_ready=bool((evidence.get("webrtc_media") or {}).get("available")),
        producer_preference="member",
        preferred_member_id=str(member_browser.get("preferred_member_id") or "") or None,
        candidate_member_ids=list(member_browser.get("candidate_members") or []),
        member_browser_direct_possible=bool(member_browser.get("possible")),
        member_browser_direct_admitted=bool(member_browser.get("admitted")),
        member_browser_direct_reason=str(member_browser.get("reason") or ""),
        candidate_member_total=int(member_browser.get("candidate_member_total") or 0),
        browser_session_total=int(member_browser.get("browser_session_total") or 0),
    )


def hub_member_semantic_channels_snapshot(
    *,
    role: str,
    route_mode: str | None,
    connected_to_hub: bool | None,
    hub_root_protocol: dict[str, Any],
    now_ts: float | None = None,
    transport_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    role_norm = str(role or "").strip().lower()
    evidence = (
        transport_evidence
        if isinstance(transport_evidence, dict)
        else _hub_member_transport_evidence_snapshot(
            role=role_norm,
            route_mode=route_mode,
            connected_to_hub=connected_to_hub,
            hub_root_protocol=hub_root_protocol,
        )
    )

    channels: dict[str, dict[str, Any]] = {}
    assessment_state = "nominal"
    assessment_reasons: list[str] = []
    media_route_contract = _hub_member_media_route_contract(evidence)

    with _LOCK:
        for spec in HUB_MEMBER_CHANNEL_SPECS:
            runtime_entry = _HUB_MEMBER_CHANNEL_RUNTIME.setdefault(
                spec.channel_id,
                _new_hub_member_channel_state(spec),
            )
            available_paths = [
                path
                for path in spec.candidate_paths
                if isinstance(evidence.get(path), dict) and bool(evidence.get(path, {}).get("available"))
            ]
            if spec.channel_id == "hub_member.sync":
                # Browser-hub Yjs transports prove that the browser can sync
                # with the hub YRoom. They do not, by themselves, prove that
                # member-runtime YStore writes are bridged into that shared
                # document. Keep them visible as candidate evidence, but do
                # not select them as the hub-member sync authority until a
                # runtime-bound bridge is present.
                available_paths = [
                    path
                    for path in available_paths
                    if not (
                        bool((evidence.get(path) or {}).get("browser_hub_only"))
                        and not bool((evidence.get(path) or {}).get("runtime_bound"))
                    )
                ]
                if "member_link_ws" in available_paths:
                    # The member-link websocket is the only currently
                    # implemented runtime-bound member YStore replication path.
                    # Prefer it over generic route relays until those relays
                    # expose their own Yjs replication counters.
                    available_paths = [
                        "member_link_ws",
                        *[path for path in available_paths if path != "member_link_ws"],
                    ]
            if spec.channel_id == "hub_member.sync" and "member_link_ws" in available_paths:
                preferred_path = "member_link_ws"
            else:
                preferred_path = next((path for path in spec.failover_order if path in available_paths), None)
            current_path = str(runtime_entry.get("active_path") or "").strip() or None
            freeze_remaining_s = 0.0
            active_path = preferred_path
            selection = "preferred"
            last_switch_at = float(runtime_entry.get("last_switch_at") or 0.0)
            if (
                current_path
                and current_path in available_paths
                and preferred_path
                and current_path != preferred_path
                and int(spec.freeze_after_switch_s) > 0
            ):
                elapsed = max(0.0, now - last_switch_at)
                if elapsed < float(spec.freeze_after_switch_s):
                    active_path = current_path
                    freeze_remaining_s = round(float(spec.freeze_after_switch_s) - elapsed, 3)
                    selection = "freeze_hold"
            if active_path != current_path:
                runtime_entry["previous_path"] = current_path
                runtime_entry["active_path"] = active_path
                runtime_entry["preferred_path"] = preferred_path
                runtime_entry["last_switch_at"] = now
                runtime_entry["switch_total"] = int(runtime_entry.get("switch_total") or 0) + 1
                current_path = active_path
            else:
                runtime_entry["preferred_path"] = preferred_path
            status, state, reason = _semantic_channel_status(
                spec=spec,
                role_norm=role_norm,
                active_path=current_path,
                preferred_path=preferred_path,
                freeze_remaining_s=freeze_remaining_s,
            )
            last_switch_ago_s = _round_age(now, runtime_entry.get("last_switch_at"))
            candidate_state = {
                path: {
                    "available": bool((evidence.get(path) or {}).get("available")),
                    **(
                        {
                            key: value
                            for key, value in (evidence.get(path) or {}).items()
                            if key != "available"
                        }
                        if isinstance(evidence.get(path), dict)
                        else {}
                    ),
                }
                for path in spec.candidate_paths
            }
            entry = {
                "channel_id": spec.channel_id,
                "title": spec.title,
                "channel_type": spec.channel_type.value,
                "authority": spec.authority.value,
                "status": status,
                "state": state,
                "reason": reason,
                "candidate_paths": list(spec.candidate_paths),
                "available_paths": available_paths,
                "preferred_path": preferred_path,
                "active_path": current_path,
                "selection": selection,
                "freeze_after_switch_s": int(spec.freeze_after_switch_s),
                "freeze_remaining_s": freeze_remaining_s if freeze_remaining_s > 0.0 else 0.0,
                "last_switch_ago_s": last_switch_ago_s,
                "switch_total": int(runtime_entry.get("switch_total") or 0),
                "duplicate_suppression": spec.duplicate_suppression,
                "candidate_state": candidate_state,
            }
            if spec.channel_id == "hub_member.media":
                monitoring = (
                    media_route_contract.get("monitoring")
                    if isinstance(media_route_contract.get("monitoring"), dict)
                    else {}
                )
                entry.update(
                    {
                        "route_intent": media_route_contract.get("route_intent"),
                        "delivery_topology": media_route_contract.get("delivery_topology"),
                        "producer_authority": media_route_contract.get("producer_authority"),
                        "producer_target": media_route_contract.get("producer_target"),
                        "preferred_member_id": media_route_contract.get("preferred_member_id"),
                        "selection_reason": media_route_contract.get("selection_reason"),
                        "degradation_reason": media_route_contract.get("degradation_reason"),
                        "fallback_chain": list(media_route_contract.get("fallback_chain") or []),
                        "attempt": media_route_contract.get("attempt"),
                        "member_browser_direct": media_route_contract.get("member_browser_direct"),
                        "observed_failure": monitoring.get("observed_failure"),
                    }
                )
            channels[spec.channel_id] = entry

    command_channel = channels.get("hub_member.command") if isinstance(channels.get("hub_member.command"), dict) else {}
    sync_channel = channels.get("hub_member.sync") if isinstance(channels.get("hub_member.sync"), dict) else {}
    if str(command_channel.get("status") or "") != "ready":
        assessment_state = "degraded"
        assessment_reasons.append("command_path_unavailable")
    if str(sync_channel.get("status") or "") != "ready":
        assessment_state = "degraded"
        assessment_reasons.append("sync_path_unavailable")
    if assessment_state == "nominal":
        primary_ids = (
            "hub_member.command",
            "hub_member.event",
            "hub_member.sync",
            "hub_member.presence",
        )
        primary_channels = [
            channels.get(channel_id)
            for channel_id in primary_ids
            if isinstance(channels.get(channel_id), dict)
        ]
        active_paths = {
            str(item.get("active_path") or "")
            for item in primary_channels
            if isinstance(item, dict) and str(item.get("active_path") or "").strip()
        }
        if any(str(item.get("state") or "") == "freeze_hold" for item in primary_channels if isinstance(item, dict)):
            assessment_state = "transitioning"
            assessment_reasons.append("freeze_hold_active")
        elif any(path in {"root_route_proxy", "member_link_ws"} for path in active_paths):
            assessment_state = "fallback"
            assessment_reasons.append("fallback_path_active")
    if not assessment_reasons:
        assessment_reasons.append("single_active_authority_paths")

    return {
        "assessment": {
            "state": assessment_state,
            "reason": "; ".join(assessment_reasons),
        },
        "channels": channels,
        "transport_evidence": evidence,
        "updated_at": now,
    }


def _node_label(node_names: Any, *, fallback: str) -> str:
    if isinstance(node_names, list):
        for item in node_names:
            token = str(item or "").strip()
            if token:
                return token
    return fallback


def _member_device_inventory_map() -> dict[str, dict[str, Any]]:
    try:
        from adaos.services.device_inventory import list_devices
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        items = list_devices(kind="member")
    except Exception:
        return {}
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
        node_id = str(identity.get("node_id") or "").strip()
        if node_id:
            result[node_id] = item
    return result


def _member_inventory_overlay(item: dict[str, Any] | None) -> dict[str, Any]:
    payload = item if isinstance(item, dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    return {
        "device_ref": str(payload.get("ref") or "").strip() or None,
        "policy_present": bool(policy.get("present")),
        "managed_state": str(policy.get("managed_state") or "").strip() or None,
        "display_name": str(policy.get("display_name") or "").strip() or None,
        "effective_name": str(policy.get("effective_name") or "").strip() or None,
        "access_class": str(policy.get("access_class") or "").strip() or None,
        "lifetime_mode": str(policy.get("lifetime_mode") or "").strip() or None,
        "connected_to_subnet": runtime.get("connected_to_subnet"),
    }


def _connection_local_node_display(*, role: str, node_id: str, node_names: list[str]) -> dict[str, Any]:
    conf = SimpleNamespace(
        role=str(role or "").strip().lower(),
        node_id=str(node_id or "").strip(),
        node_names=list(node_names or []),
        primary_node_name="",
    )
    try:
        return node_display_from_config(conf)
    except Exception:
        return node_display_payload(
            role=str(role or "").strip().lower(),
            node_id=str(node_id or "").strip(),
            node_names=list(node_names or []),
        )


def _member_snapshot_state(*, connected: bool, last_snapshot_ago_s: Any) -> str:
    if not connected:
        return "down"
    if last_snapshot_ago_s is None:
        return "pending"
    try:
        age = float(last_snapshot_ago_s)
    except Exception:
        return "pending"
    if age >= 90.0:
        return "stale"
    if age >= 30.0:
        return "aging"
    return "fresh"


def _member_rollout_state(update_state: str, *, snapshot_state: str) -> str:
    state = str(update_state or "").strip().lower()
    if state in {"countdown", "draining", "stopping", "restarting", "applying", "rolling_back", "rollback", "validate", "validating"}:
        return "in_progress"
    if state in {"succeeded", "validated"}:
        return "updated"
    if state in {"rolled_back"}:
        return "rolled_back"
    if state in {"failed"}:
        return "failed"
    if state in {"cancelled"}:
        return "cancelled"
    if snapshot_state in {"pending", "aging", "stale"}:
        return snapshot_state
    return "steady"


def _connected_to_subnet_alias(connected_to_hub: bool | None) -> bool | None:
    return connected_to_hub if isinstance(connected_to_hub, bool) else None


def hub_member_connection_state_snapshot(
    *,
    role: str,
    route_mode: str | None,
    connected_to_hub: bool | None,
    node_id: str,
    node_names: list[str] | None = None,
) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    now = time.time()
    local_names = list(node_names or [])
    local_display = _connection_local_node_display(role=role_norm, node_id=node_id, node_names=local_names)
    if role_norm == "hub":
        try:
            from adaos.services.subnet.link_manager import hub_link_manager_snapshot

            raw = hub_link_manager_snapshot()
        except Exception:
            raw = {"members": [], "member_total": 0, "connected_total": 0, "updated_at": now}
        try:
            from adaos.services.registry.subnet_directory import get_directory

            directory_nodes = get_directory().list_known_nodes()
        except Exception:
            directory_nodes = []
        members = raw.get("members") if isinstance(raw.get("members"), list) else []
        items: list[dict[str, Any]] = []
        known_members: list[dict[str, Any]] = []
        rollout_counts: dict[str, int] = {}
        snapshot_counts: dict[str, int] = {}
        version_counts: dict[str, int] = {}
        connected_ids: set[str] = set()
        directory_by_id: dict[str, dict[str, Any]] = {}
        inventory_by_id = _member_device_inventory_map()
        for node in directory_nodes:
            if not isinstance(node, dict):
                continue
            known_id = str(node.get("node_id") or "").strip()
            if not known_id or known_id == node_id:
                continue
            directory_by_id[known_id] = node
        for index, item in enumerate(members, start=1):
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("node_id") or "").strip()
            if not member_id:
                continue
            connected_ids.add(member_id)
            directory_item = directory_by_id.get(member_id) if isinstance(directory_by_id.get(member_id), dict) else {}
            runtime_projection = (
                directory_item.get("runtime_projection")
                if isinstance(directory_item.get("runtime_projection"), dict)
                else {}
            )
            persisted_snapshot = (
                runtime_projection.get("snapshot")
                if isinstance(runtime_projection.get("snapshot"), dict)
                else {}
            )
            node_snapshot = item.get("node_snapshot") if isinstance(item.get("node_snapshot"), dict) else {}
            if not node_snapshot and persisted_snapshot:
                node_snapshot = dict(persisted_snapshot)
            snapshot_names = node_snapshot.get("node_names") if isinstance(node_snapshot.get("node_names"), list) else []
            member_names = item.get("node_names") if isinstance(item.get("node_names"), list) else []
            member_names = member_names or snapshot_names
            build = node_snapshot.get("build") if isinstance(node_snapshot.get("build"), dict) else {}
            update_status = node_snapshot.get("update_status") if isinstance(node_snapshot.get("update_status"), dict) else {}
            connected = bool(item.get("connected", True))
            online = bool(directory_item.get("online")) if directory_item else connected
            last_seen = float(directory_item.get("last_seen") or 0.0) if directory_item else 0.0
            media_capability: dict[str, Any] = {}
            try:
                from adaos.services.media_capability import (
                    parse_webrtc_media_capacity_entry,
                    select_member_browser_direct_capacity_entry,
                )

                snapshot_capacity = node_snapshot.get("capacity") if isinstance(node_snapshot.get("capacity"), dict) else {}
                directory_capacity = directory_item.get("capacity") if isinstance(directory_item.get("capacity"), dict) else {}
                capability_entry = select_member_browser_direct_capacity_entry(snapshot_capacity or directory_capacity)
                media_capability = (
                    parse_webrtc_media_capacity_entry(capability_entry)
                    if isinstance(capability_entry, dict)
                    else {}
                )
            except Exception:
                media_capability = {}
            snapshot_state = _member_snapshot_state(
                connected=connected,
                last_snapshot_ago_s=item.get("last_snapshot_ago_s"),
            )
            rollout_state = _member_rollout_state(
                str(update_status.get("state") or ""),
                snapshot_state=snapshot_state,
            )
            runtime_ref = str(build.get("runtime_git_short_commit") or build.get("runtime_version") or build.get("version") or "").strip()
            snapshot_counts[snapshot_state] = int(snapshot_counts.get(snapshot_state) or 0) + 1
            rollout_counts[rollout_state] = int(rollout_counts.get(rollout_state) or 0) + 1
            if runtime_ref:
                version_counts[runtime_ref] = int(version_counts.get(runtime_ref) or 0) + 1
            inventory_overlay = _member_inventory_overlay(inventory_by_id.get(member_id))
            label = str(inventory_overlay.get("effective_name") or "").strip() or str(directory_item.get("node_label") or "").strip() or _node_label(
                member_names,
                fallback=f"Node {index}",
            )
            items.append(
                {
                    **item,
                    "node_id": member_id,
                    "node_names": member_names,
                    "node_snapshot": node_snapshot,
                    "label": label,
                    "primary_name": label,
                    "node_label": label,
                    "node_compact_label": directory_item.get("node_compact_label"),
                    "node_index": directory_item.get("node_index"),
                    "node_color": directory_item.get("node_color"),
                    "role": "member",
                    "state": "connected" if connected else "down",
                    "connected": connected,
                    "online": online,
                    "observed_via": "member_link",
                    "last_seen_ago_s": round(max(0.0, now - last_seen), 3) if last_seen > 0.0 else None,
                    "snapshot_state": snapshot_state,
                    "rollout_state": rollout_state,
                    "snapshot_ready": bool(node_snapshot.get("ready")),
                    "snapshot_node_state": str(node_snapshot.get("node_state") or ""),
                    "snapshot_update_state": str(update_status.get("state") or ""),
                    "snapshot_update_phase": str(update_status.get("phase") or ""),
                    "snapshot_runtime_git_short_commit": str(build.get("runtime_git_short_commit") or ""),
                    "snapshot_runtime_version": str(build.get("runtime_version") or build.get("version") or ""),
                    "media_capability": media_capability,
                    "media_capable": bool(media_capability.get("member_browser_direct")),
                    **inventory_overlay,
                }
            )
            known_members.append(items[-1])
        linkless_online_total = 0
        for known_id, node in directory_by_id.items():
            if known_id in connected_ids:
                continue
            roles = node.get("roles") if isinstance(node.get("roles"), list) else []
            if roles and "member" not in [str(item or "").strip().lower() for item in roles]:
                continue
            online = bool(node.get("online"))
            if online:
                linkless_online_total += 1
            last_seen = float(node.get("last_seen") or 0.0)
            runtime_projection = (
                node.get("runtime_projection")
                if isinstance(node.get("runtime_projection"), dict)
                else {}
            )
            node_snapshot = (
                runtime_projection.get("snapshot")
                if isinstance(runtime_projection.get("snapshot"), dict)
                else {}
            )
            build = node_snapshot.get("build") if isinstance(node_snapshot.get("build"), dict) else {}
            update_status = (
                node_snapshot.get("update_status")
                if isinstance(node_snapshot.get("update_status"), dict)
                else {}
            )
            projection_freshness = subnet_runtime_projection_freshness(
                runtime_projection,
                online=online,
                now=now,
            )
            snapshot_state = str(projection_freshness.get("state") or "pending")
            rollout_state = (
                "stale"
                if not online
                else _member_rollout_state(
                    str(update_status.get("state") or ""),
                    snapshot_state=snapshot_state,
                )
            )
            label = str(node.get("node_label") or "").strip() or _node_label(
                list(runtime_projection.get("node_names") or []),
                fallback=f"Node {len(known_members) + 1}",
            )
            media_capability = {}
            capacity_source = (
                node_snapshot.get("capacity")
                if isinstance(node_snapshot.get("capacity"), dict)
                else node.get("capacity")
            )
            try:
                from adaos.services.media_capability import (
                    parse_webrtc_media_capacity_entry,
                    select_member_browser_direct_capacity_entry,
                )

                capability_entry = select_member_browser_direct_capacity_entry(
                    capacity_source if isinstance(capacity_source, dict) else {}
                )
                media_capability = (
                    parse_webrtc_media_capacity_entry(capability_entry)
                    if isinstance(capability_entry, dict)
                    else {}
                )
            except Exception:
                media_capability = {}
            inventory_overlay = _member_inventory_overlay(inventory_by_id.get(known_id))
            label = str(inventory_overlay.get("effective_name") or "").strip() or label
            known_members.append(
                {
                    "node_id": known_id,
                    "hostname": node.get("hostname"),
                    "roles": list(roles or []),
                    "node_names": list(runtime_projection.get("node_names") or []),
                    "node_snapshot": dict(node_snapshot) if isinstance(node_snapshot, dict) else {},
                    "label": label,
                    "primary_name": label,
                    "node_label": label,
                    "node_compact_label": node.get("node_compact_label"),
                    "node_index": node.get("node_index"),
                    "node_color": node.get("node_color"),
                    "role": "member",
                    "state": "heartbeat" if online else "offline",
                    "connected": False,
                    "online": online,
                    "observed_via": "subnet_directory",
                    "last_seen_ago_s": round(max(0.0, now - last_seen), 3) if last_seen > 0.0 else None,
                    "runtime_projection_freshness": projection_freshness,
                    "snapshot_state": snapshot_state,
                    "rollout_state": rollout_state,
                    "snapshot_ready": bool(runtime_projection.get("ready")),
                    "snapshot_node_state": (
                        str(runtime_projection.get("node_state") or "")
                        or str(node.get("node_state") or "")
                    ),
                    "snapshot_update_state": str(update_status.get("state") or ""),
                    "snapshot_update_phase": str(update_status.get("phase") or ""),
                    "snapshot_runtime_git_short_commit": str(build.get("runtime_git_short_commit") or ""),
                    "snapshot_runtime_version": str(build.get("runtime_version") or build.get("version") or ""),
                    "media_capability": media_capability,
                    "media_capable": bool(media_capability.get("member_browser_direct")),
                    **inventory_overlay,
                }
            )
        assessment_state = "idle"
        assessment_reason = "no_members_connected"
        rollout_state = "idle"
        rollout_reason = "no_members_connected"
        if items:
            if rollout_counts.get("failed"):
                rollout_state = "degraded"
                rollout_reason = "member_update_failed"
            elif snapshot_counts.get("stale"):
                rollout_state = "degraded"
                rollout_reason = "member_snapshots_stale"
            elif snapshot_counts.get("pending"):
                rollout_state = "pressure"
                rollout_reason = "member_snapshots_pending"
            elif rollout_counts.get("in_progress"):
                rollout_state = "transitioning"
                rollout_reason = "member_update_in_progress"
            else:
                rollout_state = "nominal"
                rollout_reason = "member_rollout_steady"
            if rollout_state in {"degraded", "pressure"}:
                assessment_state = rollout_state
                assessment_reason = rollout_reason
            elif all(isinstance(item.get("node_snapshot"), dict) and item.get("node_snapshot") for item in items):
                assessment_state = "nominal"
                assessment_reason = "member_links_and_snapshots_connected"
            else:
                assessment_state = "pressure"
                assessment_reason = "member_snapshots_pending"
        elif linkless_online_total > 0:
            assessment_state = "pressure"
            assessment_reason = "known_members_without_links"
        if assessment_state == "nominal" and linkless_online_total > 0:
            assessment_state = "pressure"
            assessment_reason = "some_members_without_links"
        return {
            "role": "hub",
            "local_node": {
                "node_id": node_id,
                "node_names": local_names,
                **local_display,
                "label": str(local_display.get("node_label") or _node_label(local_names, fallback="Node 0")),
                "role": "hub",
            },
            "assessment": {
                "state": assessment_state,
                "reason": assessment_reason,
            },
            "member_total": len(items),
            "connected_total": len(items),
            "known_total": len(known_members),
            "linkless_total": max(0, len(known_members) - len(items)),
            "members": items,
            "known_members": known_members,
            "update_rollout": {
                "state": rollout_state,
                "reason": rollout_reason,
                "snapshot_counts": snapshot_counts,
                "rollout_counts": rollout_counts,
                "version_counts": version_counts,
            },
            "hub_event_total": int(raw.get("hub_event_total") or 0),
            "hub_core_update_broadcast_total": int(raw.get("hub_core_update_broadcast_total") or 0),
            "updated_at": float(raw.get("updated_at") or now),
        }

    try:
        from adaos.services.subnet.link_client import member_link_client_snapshot

        raw = member_link_client_snapshot()
    except Exception:
        raw = {
            "connected": False,
            "last_hub_core_update": {},
            "last_follow_result": {},
            "updated_at": now,
        }
    transition_state = str(raw.get("transition_state") or "").strip().lower()
    state = "connected" if bool(raw.get("connected")) else ("member_link" if str(route_mode or "") == "ws" else "disconnected")
    assessment_state = "nominal" if bool(raw.get("connected")) else "degraded"
    assessment_reason = "linked_to_hub" if bool(raw.get("connected")) else "member_link_down"
    if not bool(raw.get("connected")) and transition_state in {"waiting_restart", "restarting", "paused_for_update"}:
        state = transition_state
        assessment_reason = str(raw.get("transition_reason") or transition_state)
    return {
        "role": "member",
        "local_node": {
            "node_id": node_id,
            "node_names": local_names,
            **local_display,
            "label": str(local_display.get("node_label") or _node_label(local_names, fallback="Node 1")),
            "role": "member",
        },
        "assessment": {
            "state": assessment_state,
            "reason": assessment_reason,
        },
        "route_mode": route_mode,
        "connected_to_subnet": connected_to_hub,
        "connected_to_hub": connected_to_hub,
        "state": state,
        "hub": raw,
        "updated_at": float(raw.get("updated_at") or now),
    }


def hub_root_protocol_model_snapshot() -> dict[str, Any]:
    return {
        "traffic_classes": {
            name: hub_root_protocol_class_policy(name)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "stale_authority_thresholds_s": {
            name: int(hub_root_protocol_class_policy(name).get("stale_authority_after_s") or 0)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "tracked_streams": [
            {
                "flow_id": "hub_root.control.lifecycle",
                "stream_id_pattern": "hub-control:lifecycle:<hub_id>:<runtime_instance_id>",
                "delivery_class": "must_not_lose",
                "message_type": "state_report",
                "ack_required": True,
                "dedupe_scope": "cursor_and_message_id",
                "heartbeat_expected_s": 15,
            },
            {
                "flow_id": "hub_root.integration.github_core_update",
                "stream_id_pattern": "hub-integration:github-core-update:<hub_id>:<runtime_instance_id>",
                "delivery_class": "must_not_lose",
                "message_type": "state_report",
                "ack_required": True,
                "dedupe_scope": "cursor_and_message_id",
            }
        ],
        "tracked_operation_keys": [
            {
                "flow_id": "hub_root.integration.telegram",
                "operation_key_pattern": "tgop:<hub_id>:<bot_id>:<chat_id>:<digest>",
                "delivery_class": "must_not_lose",
                "hub_durable_outbox": True,
                "dedupe_scope": "root_redis_ttl_window",
                "ttl_s": 600,
            }
        ],
        "tracked_request_keys": [
            {
                "flow_id": "hub_root.integration.llm",
                "request_key": "request_id",
                "delivery_class": "nice_to_replay",
                "dedupe_scope": "root_redis_ttl_window",
                "ttl_s": 600,
                "conflict_rule": "request_fingerprint_must_match",
            }
        ],
    }


def _hub_root_hardening_coverage_snapshot(protocol: dict[str, Any]) -> dict[str, Any]:
    model = hub_root_protocol_model_snapshot()
    tracked_streams = {
        str(item.get("flow_id") or ""): item
        for item in (model.get("tracked_streams") or [])
        if isinstance(item, dict) and str(item.get("flow_id") or "").strip()
    }
    tracked_operation_keys = {
        str(item.get("flow_id") or ""): item
        for item in (model.get("tracked_operation_keys") or [])
        if isinstance(item, dict) and str(item.get("flow_id") or "").strip()
    }
    tracked_request_keys = {
        str(item.get("flow_id") or ""): item
        for item in (model.get("tracked_request_keys") or [])
        if isinstance(item, dict) and str(item.get("flow_id") or "").strip()
    }
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
    outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}

    items: list[dict[str, Any]] = []
    covered = 0
    total = 0
    for spec in HUB_ROOT_FLOW_SPECS:
        flow_id = str(spec.flow_id or "")
        if not flow_id.startswith("hub_root."):
            continue
        total += 1
        mechanisms: list[str] = []
        if flow_id in tracked_streams:
            mechanisms.append("cursor_ack_stream")
        if flow_id in tracked_operation_keys:
            mechanisms.append("operation_key")
            if bool(tracked_operation_keys[flow_id].get("hub_durable_outbox")) or bool(tg_outbox.get("durable_store")):
                mechanisms.append("durable_hub_outbox")
        if flow_id in tracked_request_keys:
            mechanisms.append("request_id_cache")
        if flow_id == "hub_root.route.control" and isinstance(route_flows.get("control"), dict):
            mechanisms.append("route_flow_runtime")
        if flow_id == "hub_root.route.frame" and isinstance(route_flows.get("frame"), dict):
            mechanisms.append("route_flow_runtime")

        required: list[str] = []
        if flow_id in {"hub_root.control.lifecycle", "hub_root.integration.github_core_update"}:
            required = ["cursor_ack_stream"]
        elif flow_id == "hub_root.integration.telegram":
            required = ["operation_key", "durable_hub_outbox"]
        elif flow_id == "hub_root.integration.llm":
            required = ["request_id_cache"]
        elif flow_id in {"hub_root.route.control", "hub_root.route.frame"}:
            required = ["route_flow_runtime"]

        covered_flow = all(req in mechanisms for req in required) if required else bool(mechanisms)
        if covered_flow:
            covered += 1
        items.append(
            {
                "flow_id": flow_id,
                "delivery_class": spec.delivery_class.value,
                "required": required,
                "mechanisms": mechanisms,
                "covered": covered_flow,
            }
        )

    state = "complete" if total > 0 and covered >= total else "partial"
    return {
        "state": state,
        "covered_flows": covered,
        "total_flows": total,
        "flows": items,
    }


def _route_flow_state_snapshot(
    flow: dict[str, Any],
    *,
    now_ts: float,
    route_runtime: dict[str, Any],
) -> dict[str, Any]:
    entry = dict(flow or {})
    last_event_ago_s = _round_age(now_ts, entry.get("last_event_at"))
    last_error_ago_s = _round_age(now_ts, entry.get("last_error_at"))
    entry["last_event_ago_s"] = last_event_ago_s
    entry["last_error_ago_s"] = last_error_ago_s
    name = str(entry.get("name") or "unknown")
    pending_events = int(route_runtime.get("pending_events") or 0)
    pending_tunnels = int(route_runtime.get("pending_tunnels") or 0)
    pending_chunks = int(route_runtime.get("pending_chunks") or 0)
    last_no_upstream_ago_s = _round_age(now_ts, route_runtime.get("last_no_upstream_at"))
    last_force_close_ago_s = _round_age(now_ts, route_runtime.get("last_force_close_at"))

    state = "nominal"
    reason = "no_recent_route_pressure"
    if name == "control":
        if (
            isinstance(last_error_ago_s, (int, float))
            and float(last_error_ago_s) <= 30.0
            and str(entry.get("last_error") or "").strip()
        ):
            state = "degraded"
            reason = f"recent_error:{entry.get('last_event') or 'control_error'}"
        elif isinstance(last_force_close_ago_s, (int, float)) and float(last_force_close_ago_s) <= 30.0:
            state = "degraded"
            reason = "forced_close_no_upstream"
        elif pending_tunnels > 0 or (pending_events > 0 and isinstance(last_no_upstream_ago_s, (int, float)) and float(last_no_upstream_ago_s) <= 30.0):
            state = "pressure"
            reason = "pending_upstream_open"
        elif int(route_runtime.get("active_tunnels") or 0) > 0:
            state = "active"
            reason = "route_control_session_active"
    elif name == "frame":
        if (
            isinstance(last_error_ago_s, (int, float))
            and float(last_error_ago_s) <= 20.0
            and str(entry.get("last_error") or "").strip()
        ):
            state = "degraded"
            reason = f"recent_error:{entry.get('last_event') or 'frame_error'}"
        elif pending_events > 0 or pending_chunks > 0:
            state = "pressure"
            reason = "pending_frame_backlog"
        elif isinstance(last_no_upstream_ago_s, (int, float)) and float(last_no_upstream_ago_s) <= 20.0:
            state = "pressure"
            reason = "recent_no_upstream"
        elif isinstance(last_event_ago_s, (int, float)) and float(last_event_ago_s) <= 30.0:
            state = "active"
            reason = "recent_frame_activity"
    entry["state"] = state
    entry["reason"] = reason
    return entry


def _hub_root_protocol_assessment(protocol: dict[str, Any]) -> dict[str, Any]:
    traffic_classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    integration_outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control = traffic_classes.get("control") if isinstance(traffic_classes.get("control"), dict) else {}
    route = traffic_classes.get("route") if isinstance(traffic_classes.get("route"), dict) else {}
    telegram = integration_outboxes.get("telegram") if isinstance(integration_outboxes.get("telegram"), dict) else {}

    reasons: list[str] = []
    state = "nominal"
    if int(control.get("active_subscriptions") or 0) <= 0:
        state = "degraded"
        reasons.append("control_subscription_missing")
    if int(control.get("handler_errors") or 0) > 0:
        state = "degraded"
        reasons.append("control_handler_errors")
    control_qsize = control.get("last_qsize")
    control_limit = ((control.get("policy") or {}) if isinstance(control.get("policy"), dict) else {}).get("pending_msgs_limit")
    if isinstance(control_qsize, int) and isinstance(control_limit, int) and control_limit > 0 and control_qsize >= control_limit:
        state = "degraded"
        reasons.append("control_queue_at_limit")

    route_backlog = int(route_runtime.get("pending_events") or 0)
    route_qsize = route.get("last_qsize")
    route_limit = ((route.get("policy") or {}) if isinstance(route.get("policy"), dict) else {}).get("pending_msgs_limit")
    if route_backlog > 0:
        if state == "nominal":
            state = "pressure"
        reasons.append("route_backlog")
    if isinstance(route_qsize, int) and isinstance(route_limit, int) and route_limit > 0 and route_qsize >= route_limit:
        if state == "nominal":
            state = "pressure"
        reasons.append("route_queue_at_limit")

    route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
    route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
    route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
    if str(route_control_flow.get("state") or "") == "degraded":
        state = "degraded"
        reasons.append("route_control_unhealthy")
    elif str(route_control_flow.get("state") or "") == "pressure":
        if state == "nominal":
            state = "pressure"
        reasons.append("route_control_pressure")
    if str(route_frame_flow.get("state") or "") == "degraded":
        if state == "nominal":
            state = "pressure"
        reasons.append("route_frame_unhealthy")
    elif str(route_frame_flow.get("state") or "") == "pressure":
        if state == "nominal":
            state = "pressure"
        reasons.append("route_frame_pressure")

    telegram_size = int(telegram.get("size") or 0)
    telegram_max = telegram.get("max_size")
    telegram_durable = bool(telegram.get("durable_store"))
    if telegram_size > 0:
        if state == "nominal":
            state = "pressure"
        reasons.append("integration_buffering")
        if not telegram_durable:
            state = "degraded"
            reasons.append("integration_outbox_not_durable")
    if isinstance(telegram_max, int) and telegram_max > 0 and telegram_size >= telegram_max:
        if state == "nominal":
            state = "pressure"
        reasons.append("integration_outbox_full")

    for stream_id, entry in streams.items():
        if not isinstance(entry, dict):
            continue
        pending = entry.get("pending")
        pending_age_s = pending.get("age_s") if isinstance(pending, dict) else None
        traffic = str(entry.get("traffic_class") or "integration").strip().lower()
        cls = traffic_classes.get(traffic) if isinstance(traffic_classes.get(traffic), dict) else {}
        policy = cls.get("policy") if isinstance(cls.get("policy"), dict) else {}
        stale_after_s = int(policy.get("stale_authority_after_s") or 0)
        flow_id = str(entry.get("flow_id") or stream_id).strip()
        ack_total = int(entry.get("ack_total") or 0)
        last_issue_ago_s = entry.get("last_issue_ago_s")
        last_ack_ago_s = entry.get("last_ack_ago_s")
        if isinstance(pending_age_s, (int, float)) and stale_after_s > 0 and float(pending_age_s) >= float(stale_after_s):
            state = "degraded"
            reasons.append(f"pending_ack_stale:{flow_id}")
        elif isinstance(pending_age_s, (int, float)) and float(pending_age_s) > 0.0:
            if state == "nominal":
                state = "pressure"
            reasons.append(f"pending_ack:{flow_id}")
        elif flow_id == "hub_root.control.lifecycle" and stale_after_s > 0:
            if ack_total <= 0 and isinstance(last_issue_ago_s, (int, float)) and float(last_issue_ago_s) >= float(stale_after_s):
                state = "degraded"
                reasons.append(f"ack_missing:{flow_id}")
            elif isinstance(last_ack_ago_s, (int, float)) and float(last_ack_ago_s) >= float(stale_after_s):
                state = "degraded"
                reasons.append(f"stale_authority:{flow_id}")
            elif isinstance(last_ack_ago_s, (int, float)) and float(last_ack_ago_s) >= max(5.0, float(stale_after_s) / 2.0):
                if state == "nominal":
                    state = "pressure"
                reasons.append(f"aging_authority:{flow_id}")

    if not reasons:
        reasons.append("no_active_protocol_pressure")
    return {"state": state, "reason": "; ".join(reasons)}


def _hub_root_control_authority_snapshot(protocol: dict[str, Any]) -> dict[str, Any]:
    traffic_classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control = traffic_classes.get("control") if isinstance(traffic_classes.get("control"), dict) else {}
    policy = control.get("policy") if isinstance(control.get("policy"), dict) else {}
    stale_after_s = int(policy.get("stale_authority_after_s") or 0)
    stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
        ),
        {},
    )
    if not isinstance(stream, dict) or not stream:
        return {
            "state": "missing",
            "reason": "control lifecycle stream is missing",
            "stale_after_s": stale_after_s,
        }

    pending = stream.get("pending") if isinstance(stream.get("pending"), dict) else None
    ack_total = int(stream.get("ack_total") or 0)
    ack_age_s = stream.get("last_ack_ago_s")
    issue_age_s = stream.get("last_issue_ago_s")
    state = "unknown"
    reason = "control lifecycle authority has not reported yet"
    if isinstance(pending, dict):
        state = "pending"
        reason = "control lifecycle report is awaiting ack"
    elif ack_total <= 0:
        if stale_after_s > 0 and isinstance(issue_age_s, (int, float)) and float(issue_age_s) >= float(stale_after_s):
            state = "missing"
            reason = "control lifecycle authority ack is missing"
        else:
            state = "booting"
            reason = "control lifecycle authority is booting"
    elif stale_after_s > 0 and isinstance(ack_age_s, (int, float)) and float(ack_age_s) >= float(stale_after_s):
        state = "stale"
        reason = "control lifecycle authority is stale"
    elif stale_after_s > 0 and isinstance(ack_age_s, (int, float)) and float(ack_age_s) >= max(5.0, float(stale_after_s) / 2.0):
        state = "aging"
        reason = "control lifecycle authority is aging"
    else:
        state = "fresh"
        reason = "control lifecycle authority is fresh"
    return {
        "state": state,
        "reason": reason,
        "stream_id": str(stream.get("stream_id") or ""),
        "stale_after_s": stale_after_s,
        "ack_age_s": ack_age_s,
        "issue_age_s": issue_age_s,
        "last_ack_result": str(stream.get("last_ack_result") or ""),
        "issued_cursor": int(stream.get("last_issued_cursor") or 0),
        "acked_cursor": int(stream.get("last_acked_cursor") or 0),
        "pending": bool(isinstance(pending, dict)),
    }


def hub_root_protocol_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        runtime = {
            "traffic_classes": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("traffic_classes") or {})),
            "subscriptions": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("subscriptions") or {})),
            "route_runtime": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("route_runtime") or {})),
            "integration_outboxes": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("integration_outboxes") or {})),
            "streams": {},
            "updated_at": _HUB_ROOT_PROTOCOL_RUNTIME.get("updated_at"),
        }
    try:
        stream_state = protocol_streams_snapshot(now_ts=now)
        runtime["streams"] = (
            stream_state.get("streams")
            if isinstance(stream_state.get("streams"), dict)
            else {}
        )
        if not runtime.get("updated_at") and stream_state.get("updated_at"):
            runtime["updated_at"] = stream_state.get("updated_at")
    except Exception:
        runtime["streams"] = {}
    traffic_classes = runtime.get("traffic_classes") if isinstance(runtime.get("traffic_classes"), dict) else {}
    for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES:
        cls = traffic_classes.get(name) if isinstance(traffic_classes.get(name), dict) else {}
        if not cls:
            cls = _new_protocol_traffic_class_state(name)
            traffic_classes[name] = cls
        cls["policy"] = hub_root_protocol_class_policy(name)
        cls["last_dispatch_ago_s"] = _round_age(now, cls.get("last_dispatch_at"))
        cls["last_publish_ago_s"] = _round_age(now, cls.get("last_publish_at"))
        cls["last_error_ago_s"] = _round_age(now, cls.get("last_error_at"))
    subscriptions = runtime.get("subscriptions") if isinstance(runtime.get("subscriptions"), dict) else {}
    for entry in subscriptions.values():
        if not isinstance(entry, dict):
            continue
        entry["last_dispatch_ago_s"] = _round_age(now, entry.get("last_dispatch_at"))
        entry["last_error_ago_s"] = _round_age(now, entry.get("last_error_at"))
        entry["updated_ago_s"] = _round_age(now, entry.get("updated_at"))
    route_runtime = runtime.get("route_runtime") if isinstance(runtime.get("route_runtime"), dict) else {}
    route_runtime["updated_ago_s"] = _round_age(now, route_runtime.get("updated_at"))
    route_runtime["last_force_close_ago_s"] = _round_age(now, route_runtime.get("last_force_close_at"))
    route_runtime["last_no_upstream_ago_s"] = _round_age(now, route_runtime.get("last_no_upstream_at"))
    route_runtime["last_publish_fail_ago_s"] = _round_age(now, route_runtime.get("last_publish_fail_at"))
    route_runtime["last_reset_ago_s"] = _round_age(now, route_runtime.get("last_reset_at"))
    route_flows = route_runtime.get("flows")
    if not isinstance(route_flows, dict):
        route_flows = {
            "control": _new_route_flow_state("control"),
            "frame": _new_route_flow_state("frame"),
        }
        route_runtime["flows"] = route_flows
    for flow_name in ("control", "frame"):
        flow_entry = route_flows.get(flow_name)
        if not isinstance(flow_entry, dict):
            flow_entry = _new_route_flow_state(flow_name)
        route_flows[flow_name] = _route_flow_state_snapshot(flow_entry, now_ts=now, route_runtime=route_runtime)
    outboxes = runtime.get("integration_outboxes") if isinstance(runtime.get("integration_outboxes"), dict) else {}
    for entry in outboxes.values():
        if not isinstance(entry, dict):
            continue
        entry["updated_ago_s"] = _round_age(now, entry.get("updated_at"))
        entry["last_error_ago_s"] = _round_age(now, entry.get("last_error_at"))
    streams = runtime.get("streams") if isinstance(runtime.get("streams"), dict) else {}
    pending_acks = 0
    for entry in streams.values():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("pending"), dict):
            pending_acks += 1
    runtime["pending_ack_streams"] = pending_acks
    runtime["updated_ago_s"] = _round_age(now, runtime.get("updated_at"))
    runtime["hardening_coverage"] = _hub_root_hardening_coverage_snapshot(runtime)
    runtime["control_authority"] = _hub_root_control_authority_snapshot(runtime)
    runtime["assessment"] = _hub_root_protocol_assessment(runtime)
    return runtime


def reliability_model_snapshot() -> dict[str, Any]:
    return {
        "message_taxonomy": [item.value for item in MessageTaxonomy],
        "delivery_classes": [item.value for item in DeliveryClass],
        "channel_types": [item.value for item in ChannelType],
        "authorities": [item.value for item in Authority],
        "authority_boundaries": AUTHORITY_BOUNDARIES,
        "flow_inventory": [item.to_dict() for item in HUB_ROOT_FLOW_SPECS],
        "hub_member_channels": hub_member_semantic_channel_model_snapshot(),
        "hub_root_protocol": hub_root_protocol_model_snapshot(),
    }


def sidecar_runtime_snapshot(
    *,
    role: str | None = None,
    readiness_tree: dict[str, Any] | None = None,
    hub_root_protocol: dict[str, Any] | None = None,
    transport_strategy: dict[str, Any] | None = None,
    media_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        _realtime_sidecar_mod = importlib.import_module("adaos.services.realtime_sidecar")
    except Exception:
        return {"enabled": False, "status": "unavailable", "summary": "sidecar runtime module is unavailable"}

    realtime_sidecar_diag_path = _realtime_sidecar_mod.realtime_sidecar_diag_path
    realtime_sidecar_enabled = _realtime_sidecar_mod.realtime_sidecar_enabled
    realtime_sidecar_listener_snapshot = _realtime_sidecar_mod.realtime_sidecar_listener_snapshot
    realtime_sidecar_local_url = _realtime_sidecar_mod.realtime_sidecar_local_url
    route_tunnel_contract_fn = getattr(_realtime_sidecar_mod, "realtime_sidecar_route_tunnel_contract", None)
    enablement_policy_fn = getattr(_realtime_sidecar_mod, "realtime_sidecar_enablement_policy", None)

    enablement: dict[str, Any]
    if callable(enablement_policy_fn):
        try:
            raw_enablement = enablement_policy_fn(role=role)
        except TypeError:
            raw_enablement = enablement_policy_fn()
        enablement = dict(raw_enablement) if isinstance(raw_enablement, dict) else {}
    else:
        enablement = {}
    if not enablement:
        try:
            enabled = bool(realtime_sidecar_enabled(role=role))
        except TypeError:
            enabled = bool(realtime_sidecar_enabled())
        enablement = {
            "role": str(role or "").strip().lower() or None,
            "enabled": enabled,
            "default_enabled": False,
            "explicit": False,
            "source": "legacy_runtime",
            "env_var": None,
            "env_value": None,
            "reason": "legacy sidecar enablement probe",
        }
    else:
        enabled = bool(enablement.get("enabled"))
    diag_path = realtime_sidecar_diag_path()
    record = _read_last_jsonl_record(diag_path)
    now_ts = time.time()
    readiness_tree = readiness_tree if isinstance(readiness_tree, dict) else {}
    hub_root_protocol = hub_root_protocol if isinstance(hub_root_protocol, dict) else {}
    transport_strategy = transport_strategy if isinstance(transport_strategy, dict) else {}

    ownership = {
        "owns": list((AUTHORITY_BOUNDARIES.get("sidecar") or {}).get("may_own") or []),
        "must_not_own": list((AUTHORITY_BOUNDARIES.get("sidecar") or {}).get("must_not_own") or []),
    }
    lifecycle_manager = _sidecar_lifecycle_manager()
    if callable(route_tunnel_contract_fn):
        try:
            route_tunnel_contract = route_tunnel_contract_fn(role=role)
        except TypeError:
            route_tunnel_contract = route_tunnel_contract_fn()
    else:
        route_tunnel_contract = {}

    status = "disabled"
    summary = "realtime sidecar is disabled"
    session_state = "disabled"
    status_reason = str(enablement.get("reason") or "").strip() or summary
    diag_fresh = False
    last_connect_error_class = None
    last_connect_error_message = None
    diag_age_s = None
    local_listener_state = "disabled" if not enabled else "unknown"
    remote_session_state = "disabled" if not enabled else "unknown"
    transport_ready = False
    control_ready = "not_applicable"
    route_ready = "not_owned"
    sync_ready = "not_owned"
    media_ready = "not_owned"
    transport_provenance: dict[str, Any] = {
        "local_url": realtime_sidecar_local_url(),
        "diag_path": str(diag_path),
        "requested_transport": transport_strategy.get("requested_transport"),
        "effective_transport": transport_strategy.get("effective_transport"),
        "selected_server": transport_strategy.get("selected_server"),
        "last_transport_event": transport_strategy.get("last_event"),
    }
    try:
        process_snapshot = realtime_sidecar_listener_snapshot(role=role)
    except TypeError:
        process_snapshot = realtime_sidecar_listener_snapshot()
    if not enablement and isinstance(process_snapshot.get("enablement_policy"), dict):
        enablement = dict(process_snapshot.get("enablement_policy") or {})
    if not route_tunnel_contract and isinstance(process_snapshot.get("route_tunnel_contract"), dict):
        route_tunnel_contract = dict(process_snapshot.get("route_tunnel_contract") or {})
    if isinstance(record, dict) and isinstance(record.get("enablement_policy"), dict):
        enablement = dict(record.get("enablement_policy") or enablement or {})
    if isinstance(record, dict) and isinstance(record.get("route_tunnel_contract"), dict):
        route_tunnel_contract = dict(record.get("route_tunnel_contract") or route_tunnel_contract or {})
    ws_route_contract = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_route_contract = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")
    route_ready = _sidecar_route_tunnel_state(enabled=enabled, entry=ws_route_contract)
    sync_ready = _sidecar_route_tunnel_state(enabled=enabled, entry=yws_route_contract)
    delegations = {
        "hub_root_transport": bool(enabled),
        "route_tunnel_transport": str(ws_route_contract.get("current_owner") or "").strip().lower() == "sidecar",
        "sync_transport": str(yws_route_contract.get("current_owner") or "").strip().lower() == "sidecar",
        "media_transport": False,
    }
    scope = _sidecar_scope_snapshot(enabled=enabled, route_tunnel_contract=route_tunnel_contract)
    continuity_contract = _sidecar_continuity_contract(
        enabled=enabled,
        media_runtime=media_runtime,
        route_tunnel_contract=route_tunnel_contract,
    )
    progress = _sidecar_progress_snapshot(
        enabled=enabled,
        transport_ready=transport_ready,
        lifecycle_manager=lifecycle_manager,
        route_tunnel_contract=route_tunnel_contract,
    )
    if enabled:
        status = "unknown"
        summary = "realtime sidecar is enabled but has no diagnostics yet"
        session_state = "starting"
        if bool(process_snapshot.get("listener_running")) or bool(process_snapshot.get("managed_alive")):
            status_reason = "sidecar process is running but has not emitted diagnostics yet"
        else:
            status_reason = "sidecar is enabled but has not started emitting diagnostics yet"
    if isinstance(record, dict):
        last_error = str(record.get("last_error") or "").strip()
        last_connect_error_message = str(record.get("last_remote_connect_error") or last_error or "").strip() or None
        if last_connect_error_message:
            last_connect_error_class = last_connect_error_message.split(":", 1)[0].strip() or None
        remote_connected_ago_s = record.get("remote_connected_ago_s")
        local_connected_ago_s = record.get("local_connected_ago_s")
        ts = record.get("ts")
        if isinstance(ts, (int, float)):
            diag_age_s = round(max(0.0, now_ts - float(ts)), 3)
        diag_fresh = not isinstance(diag_age_s, (int, float)) or float(diag_age_s) <= 10.0
        local_listener_state = "ready" if diag_fresh else "stale"
        if isinstance(remote_connected_ago_s, (int, float)) and diag_fresh and not last_error:
            remote_session_state = "ready"
        elif isinstance(remote_connected_ago_s, (int, float)) and not diag_fresh:
            remote_session_state = "stale"
        else:
            remote_session_state = "down"
        if last_error:
            status = "degraded"
            summary = f"sidecar reports transport error: {last_error}"
            session_state = "remote_connect_failed"
            status_reason = last_error
        elif not diag_fresh:
            status = "degraded"
            summary = "sidecar diagnostics are stale"
            session_state = "stale_diag"
            status_reason = "sidecar diagnostics are stale"
        elif isinstance(remote_connected_ago_s, (int, float)):
            status = "ready"
            summary = "sidecar remote session is connected"
            session_state = "remote_ready"
            status_reason = "remote session is connected"
        elif isinstance(local_connected_ago_s, (int, float)):
            status = "degraded"
            summary = "sidecar local listener is active but remote session is not connected"
            session_state = "local_only"
            status_reason = "local listener is active but remote session is not connected"
        else:
            status = "unknown" if enabled else "disabled"
            summary = "sidecar diagnostics do not show an active session"
            if int(record.get("remote_connect_fail_total") or 0) > 0 and last_connect_error_message:
                session_state = "remote_connect_failed"
                status_reason = last_connect_error_message
            elif bool(record.get("active_session")) or int(record.get("session_open_total") or 0) > int(record.get("session_close_total") or 0):
                session_state = "remote_connecting"
                status_reason = "sidecar session is opening but no remote readiness has been observed yet"
            else:
                session_state = "starting"
                status_reason = "sidecar diagnostics do not show an active session yet"
        transport_ready = bool(status == "ready")
        control_authority = hub_root_protocol.get("control_authority") if isinstance(hub_root_protocol.get("control_authority"), dict) else {}
        control_authority_state = str(control_authority.get("state") or "").strip().lower()
        if not transport_ready:
            control_ready = "down"
        elif control_authority_state in {"fresh", "aging"}:
            control_ready = "ready"
        elif control_authority_state:
            control_ready = "degraded"
        else:
            control_ready = "unknown"
        transport_provenance.update(
            {
                "session_id": record.get("session_id"),
                "remote_url": record.get("remote_url"),
                "loop_policy": record.get("loop_policy"),
                "loop": record.get("loop"),
                "active_session": bool(record.get("active_session")),
                "local_client_total": int(record.get("local_client_total") or 0),
                "session_open_total": int(record.get("session_open_total") or 0),
                "session_close_total": int(record.get("session_close_total") or 0),
                "remote_connect_total": int(record.get("remote_connect_total") or 0),
                "remote_connect_fail_total": int(record.get("remote_connect_fail_total") or 0),
                "remote_quarantine_total": int(record.get("remote_quarantine_total") or 0),
                "superseded_total": int(record.get("superseded_total") or 0),
                "last_remote_connect_error": record.get("last_remote_connect_error"),
                "last_remote_connect_error_ago_s": record.get("last_remote_connect_error_ago_s"),
                "last_remote_disconnect_ago_s": record.get("last_remote_disconnect_ago_s"),
                "last_connect_error_class": last_connect_error_class,
                "last_connect_error_message": last_connect_error_message,
            }
        )
        return {
            "enabled": enabled,
            "enablement": enablement,
            "phase": "nats_transport_sidecar",
            "transport_owner": "sidecar" if enabled else "runtime",
            "lifecycle_manager": lifecycle_manager,
            "ownership_boundary": "transport_only",
            "ownership": ownership,
            "delegations": delegations,
            "scope": scope,
            "continuity_contract": continuity_contract,
            "progress": progress,
            "route_tunnel_contract": route_tunnel_contract,
            "status": status,
            "summary": summary,
            "session_state": session_state,
            "status_reason": status_reason,
            "local_url": realtime_sidecar_local_url(),
            "diag_path": str(diag_path),
            "diag_age_s": diag_age_s,
            "diag_fresh": diag_fresh,
            "local_listener_state": local_listener_state,
            "remote_session_state": remote_session_state,
            "transport_ready": transport_ready,
            "control_ready": control_ready,
            "route_ready": route_ready,
            "sync_ready": sync_ready,
            "media_ready": media_ready,
            "transport_provenance": transport_provenance,
            "process": process_snapshot,
            "last_diag": record,
        }

    return {
        "enabled": enabled,
        "enablement": enablement,
        "phase": "nats_transport_sidecar",
        "transport_owner": "sidecar" if enabled else "runtime",
        "lifecycle_manager": lifecycle_manager,
        "ownership_boundary": "transport_only",
        "ownership": ownership,
        "delegations": delegations,
        "scope": scope,
        "continuity_contract": continuity_contract,
        "progress": progress,
        "route_tunnel_contract": route_tunnel_contract,
        "status": status,
        "summary": summary,
        "session_state": session_state,
        "status_reason": status_reason,
        "local_url": realtime_sidecar_local_url(),
        "diag_path": str(diag_path),
        "diag_age_s": None,
        "diag_fresh": diag_fresh,
        "local_listener_state": local_listener_state,
        "remote_session_state": remote_session_state,
        "transport_ready": transport_ready,
        "control_ready": control_ready,
        "route_ready": route_ready,
        "sync_ready": sync_ready,
        "media_ready": media_ready,
        "transport_provenance": transport_provenance,
        "process": process_snapshot,
        "last_diag": None,
    }


def yjs_sync_runtime_snapshot(
    *,
    role: str,
    now_ts: float | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    role_norm = str(role or "").strip().lower()
    selected_webspace_id = str(webspace_id or "").strip()
    action_overrides: dict[str, Any] = {}
    channel_contract = _build_yjs_sync_channel_contract()
    ownership_boundaries: dict[str, Any] = {}
    recovery_playbook: dict[str, Any] = {}
    recovery_guidance: dict[str, Any] = {}
    selected_webspace: dict[str, Any] = {}
    webspace_guidance: dict[str, Any] = {}
    if role_norm != "hub":
        return {
            "available": False,
            "scope": "hub_local_only",
            "selected_webspace_id": selected_webspace_id or None,
            "assessment": {
                "state": "not_applicable",
                "reason": "local Yjs store runtime is observed on the hub only",
            },
            "channel_contract": channel_contract,
            "transport": {},
            "ownership_boundaries": ownership_boundaries,
            "action_overrides": action_overrides,
            "recovery_playbook": recovery_playbook,
            "recovery_guidance": recovery_guidance,
            "selected_webspace": selected_webspace,
            "webspace_guidance": webspace_guidance,
            "webspace_total": 0,
            "active_webspace_total": 0,
            "webspaces": {},
        }

    try:
        from adaos.services.yjs.store import ystore_runtime_snapshot

        store_runtime = ystore_runtime_snapshot(
            webspace_id=selected_webspace_id or None,
            now_ts=now,
        )
    except Exception as exc:
        return {
            "available": False,
            "scope": "hub_local_only",
            "selected_webspace_id": selected_webspace_id or None,
            "assessment": {
                "state": "unavailable",
                "reason": f"failed to load Yjs store runtime: {exc}",
            },
            "channel_contract": channel_contract,
            "transport": {},
            "ownership_boundaries": ownership_boundaries,
            "action_overrides": action_overrides,
            "recovery_playbook": recovery_playbook,
            "recovery_guidance": recovery_guidance,
            "selected_webspace": selected_webspace,
            "webspace_guidance": webspace_guidance,
            "webspace_total": 0,
            "active_webspace_total": 0,
            "webspaces": {},
        }

    try:
        from adaos.services.yjs.gateway_ws import gateway_transport_snapshot

        gateway = gateway_transport_snapshot()
    except Exception:
        gateway = {}
    try:
        from adaos.services.weather.observer import weather_observer_snapshot

        weather_observer = weather_observer_snapshot(webspace_id=selected_webspace_id or None)
    except Exception:
        weather_observer = {}
    try:
        from adaos.services.yjs.load_mark import yjs_load_mark_snapshot

        load_mark = yjs_load_mark_snapshot(
            webspace_id=selected_webspace_id or None,
            now_ts=now,
        )
    except Exception:
        load_mark = {
            "window_sec": 60,
            "bucket_sec": 1,
            "thresholds": {
                "high_bps": 32 * 1024,
                "critical_bps": 128 * 1024,
            },
            "assessment": {
                "state": "unavailable",
                "reason": "failed_to_load_yjs_load_mark",
            },
            "selected_webspace_id": selected_webspace_id or None,
            "selected_webspace": {},
            "webspace_total": 0,
            "active_root_total": 0,
            "webspaces": {},
        }
    transports = gateway.get("transports") if isinstance(gateway.get("transports"), dict) else {}
    ownership = gateway.get("ownership") if isinstance(gateway.get("ownership"), dict) else {}
    yws_transport = transports.get("yws") if isinstance(transports.get("yws"), dict) else {}
    yws_ownership = ownership.get("yws") if isinstance(ownership.get("yws"), dict) else {}
    servers = gateway.get("servers") if isinstance(gateway.get("servers"), dict) else {}
    yws_server = servers.get("yws") if isinstance(servers.get("yws"), dict) else {}
    gateway_rooms = gateway.get("rooms") if isinstance(gateway.get("rooms"), dict) else {}
    gateway_commands = gateway.get("commands") if isinstance(gateway.get("commands"), dict) else {}
    try:
        from adaos.services.webrtc.peer import webrtc_peer_snapshot

        webrtc = webrtc_peer_snapshot()
    except Exception:
        webrtc = {}
    webspaces = store_runtime.get("webspaces") if isinstance(store_runtime.get("webspaces"), dict) else {}
    webspace_total = int(store_runtime.get("webspace_total") or len(webspaces))
    active_webspace_total = int(store_runtime.get("active_webspace_total") or 0)
    assessment_state = "nominal"
    reasons: list[str] = []
    max_fill_ratio = 0.0
    compacted_total = 0
    compaction_eligible_total = 0
    replay_window_total = 0
    replay_window_byte_total = 0
    update_log_total = 0
    backup_fast_path_total = 0
    backup_skipped_total = 0
    state_vector_fast_path_total = 0
    state_vector_compute_total = 0
    for ws_id, item in list(webspaces.items()):
        if not isinstance(item, dict):
            continue
        ws_load_mark = load_mark.get("webspaces", {}).get(str(ws_id)) if isinstance(load_mark.get("webspaces"), dict) else {}
        if isinstance(ws_load_mark, dict):
            item["load_mark"] = dict(ws_load_mark)
        update_entries = int(item.get("update_log_entries") or 0)
        max_entries = int(item.get("max_update_log_entries") or 0)
        replay_window_total += int(item.get("replay_window_entries") or 0)
        replay_window_byte_total += int(item.get("replay_window_bytes") or 0)
        update_log_total += update_entries
        compacted_total += 1 if int(item.get("compact_total") or 0) > 0 else 0
        compaction_eligible_total += 1 if bool(item.get("runtime_compaction_eligible")) else 0
        backup_fast_path_total += int(item.get("backup_fast_path_total") or 0)
        backup_skipped_total += int(item.get("backup_skipped_total") or 0)
        state_vector_fast_path_total += int(item.get("state_vector_fast_path_total") or 0)
        state_vector_compute_total += int(item.get("state_vector_compute_total") or 0)
        if max_entries > 0:
            max_fill_ratio = max(max_fill_ratio, float(update_entries) / float(max_entries))
    if webspace_total <= 0:
        assessment_state = "idle"
        reasons.append("no_yjs_webspaces_cached")
    elif max_fill_ratio >= 0.9:
        assessment_state = "pressure"
        reasons.append("bounded_replay_window_near_limit")
    else:
        reasons.append("bounded_sync_runtime_observed")
    if bool(yws_transport.get("storm_detected")):
        if assessment_state in {"nominal", "idle"}:
            assessment_state = "pressure"
        reasons.append("browser_yjs_reconnect_storm")
    if yws_server and not bool(yws_server.get("ready")):
        if assessment_state == "nominal":
            assessment_state = "degraded"
        reasons.append("yjs_websocket_server_not_ready")

    if not selected_webspace_id:
        try:
            from adaos.services.yjs.webspace import default_webspace_id as _default_webspace_id

            default_ws = _default_webspace_id()
        except Exception:
            default_ws = "default"
        if default_ws in webspaces:
            selected_webspace_id = default_ws
        elif webspaces:
            selected_webspace_id = sorted(str(key) for key in webspaces.keys())[0]
    selected_entry = webspaces.get(selected_webspace_id) if isinstance(webspaces.get(selected_webspace_id), dict) else {}
    replay_pressure_compaction_requested = _request_yjs_replay_pressure_compaction(
        selected_webspace_id,
        selected_entry,
        assessment_state=assessment_state,
        reasons=reasons,
    )
    selected_webspace = _with_live_yjs_materialization_snapshot(
        selected_webspace_id,
        _build_yjs_selected_webspace_snapshot(selected_webspace_id),
    )
    selected_load_mark = load_mark.get("selected_webspace") if isinstance(load_mark.get("selected_webspace"), dict) else {}
    last_reload = (
        dict(gateway_commands.get("last_reload") or {})
        if isinstance(gateway_commands.get("last_reload"), dict)
        else {}
    )
    last_reset = (
        dict(gateway_commands.get("last_reset") or {})
        if isinstance(gateway_commands.get("last_reset"), dict)
        else {}
    )
    recent_commands = [
        dict(item)
        for item in list(gateway_commands.get("recent") or [])
        if isinstance(item, dict)
        and str(item.get("webspace_id") or "").strip() == selected_webspace_id
    ]
    (
        action_overrides,
        recovery_playbook,
        recovery_guidance,
    ) = _build_yjs_recovery_policy(selected_entry, selected_webspace)
    webspace_guidance = _build_yjs_webspace_guidance(selected_webspace, action_overrides)
    ownership_boundaries = _build_yjs_ownership_boundaries(
        selected_webspace_id=selected_webspace_id,
        selected_webspace=selected_webspace,
        transport={
            "owner": yws_ownership.get("current_owner") or "runtime",
            "lifecycle_manager": yws_ownership.get("lifecycle_manager"),
            "planned_owner": yws_ownership.get("planned_owner"),
            "migration_phase": yws_ownership.get("migration_phase"),
            "handoff_ready": bool(yws_ownership.get("handoff_ready")),
            "handoff_blockers": list(yws_ownership.get("handoff_blockers") or []),
        },
    )

    return {
        "available": True,
        "scope": "hub_local_only",
        "selected_webspace_id": selected_webspace_id or None,
        "assessment": {
            "state": assessment_state,
            "reason": "; ".join(reasons),
        },
        "channel_contract": channel_contract,
        "transport": {
            "active_yws_connections": int(yws_transport.get("active_connections") or 0),
            "last_open_ago_s": yws_transport.get("last_open_ago_s"),
            "last_close_ago_s": yws_transport.get("last_close_ago_s"),
            "recent_open_10s": int(yws_transport.get("recent_open_10s") or 0),
            "recent_open_60s": int(yws_transport.get("recent_open_60s") or 0),
            "storm_detected": bool(yws_transport.get("storm_detected")),
            "hot_client_total": len(list(yws_transport.get("hot_clients") or [])),
            "hot_clients": list(yws_transport.get("hot_clients") or []),
            "active_clients": list(yws_transport.get("active_clients") or []),
            "guard": dict(yws_transport.get("guard") or {})
            if isinstance(yws_transport.get("guard"), dict)
            else {},
            "owner": yws_ownership.get("current_owner") or "runtime",
            "lifecycle_manager": yws_ownership.get("lifecycle_manager"),
            "planned_owner": yws_ownership.get("planned_owner"),
            "migration_phase": yws_ownership.get("migration_phase"),
            "handoff_ready": bool(yws_ownership.get("handoff_ready")),
            "handoff_blockers": list(yws_ownership.get("handoff_blockers") or []),
            "server_requested": bool(yws_server.get("requested")),
            "server_started_event": bool(yws_server.get("started_event")),
            "server_task_running": bool(yws_server.get("task_running")),
            "room_total": int(yws_server.get("room_total") or 0),
            "server_ready": bool(yws_server.get("ready")),
            "server_error": yws_server.get("error"),
            "active_room_total": int(yws_transport.get("active_room_total") or 0),
            "room_create_total": int(yws_transport.get("room_create_total") or 0),
            "room_reset_total": int(yws_transport.get("room_reset_total") or 0),
            "room_drop_total": int(yws_transport.get("room_drop_total") or 0),
            "room_generation_max": int(yws_transport.get("room_generation_max") or 0),
            "room_open_total": int(yws_transport.get("room_open_total") or 0),
            "room_cold_open_total": int(yws_transport.get("room_cold_open_total") or 0),
            "room_reuse_total": int(yws_transport.get("room_reuse_total") or 0),
            "room_single_pass_bootstrap_total": int(yws_transport.get("room_single_pass_bootstrap_total") or 0),
            "update_stream_buffer_used_total": int(yws_transport.get("update_stream_buffer_used_total") or 0),
            "update_stream_waiting_send_total": int(yws_transport.get("update_stream_waiting_send_total") or 0),
            "update_stream_waiting_receive_total": int(yws_transport.get("update_stream_waiting_receive_total") or 0),
            "reload_command_total": int(gateway_commands.get("reload_total") or 0),
            "reload_duplicate_total": int(gateway_commands.get("reload_duplicate_total") or 0),
            "reload_recent_60s": int(gateway_commands.get("reload_recent_60s") or 0),
            "reset_command_total": int(gateway_commands.get("reset_total") or 0),
            "reset_duplicate_total": int(gateway_commands.get("reset_duplicate_total") or 0),
            "reset_recent_60s": int(gateway_commands.get("reset_recent_60s") or 0),
            "last_reload_client": str(last_reload.get("client") or "").strip() or None,
            "last_reload_age_s": last_reload.get("age_s"),
            "last_reload_fingerprint": str(last_reload.get("fingerprint") or "").strip() or None,
            "last_reload_duplicate_recent": bool(last_reload.get("duplicate_recent")),
            "last_reload_webspace_id": str(last_reload.get("webspace_id") or "").strip() or None,
            "last_reset_client": str(last_reset.get("client") or "").strip() or None,
            "last_reset_age_s": last_reset.get("age_s"),
            "last_reset_fingerprint": str(last_reset.get("fingerprint") or "").strip() or None,
            "last_reset_duplicate_recent": bool(last_reset.get("duplicate_recent")),
            "last_reset_webspace_id": str(last_reset.get("webspace_id") or "").strip() or None,
            "webrtc_peer_total": int(webrtc.get("peer_total") or 0),
            "webrtc_connected_peers": int(webrtc.get("connected_peers") or 0),
            "webrtc_open_events_channels": int(webrtc.get("open_events_channels") or 0),
            "webrtc_open_yjs_channels": int(webrtc.get("open_yjs_channels") or 0),
            "webrtc_pruned_stale_peers": int(webrtc.get("pruned_stale_peers") or 0),
        },
        "ownership_boundaries": ownership_boundaries,
        "action_overrides": action_overrides,
        "recovery_playbook": recovery_playbook,
        "recovery_guidance": recovery_guidance,
        "load_mark": load_mark,
        "selected_webspace": {
            **selected_webspace,
            "load_mark": dict(selected_load_mark),
            "gateway_room": dict(gateway_rooms.get(selected_webspace_id) or {})
            if isinstance(gateway_rooms.get(selected_webspace_id), dict)
            else {},
            "weather_observer": dict(weather_observer.get("selected") or {})
            if isinstance(weather_observer, dict)
            else {},
            "command_trace": {
                "last_reload": last_reload if str(last_reload.get("webspace_id") or "").strip() == selected_webspace_id else {},
                "last_reset": last_reset if str(last_reset.get("webspace_id") or "").strip() == selected_webspace_id else {},
                "recent": recent_commands,
            },
        },
        "webspace_guidance": webspace_guidance,
        "webspace_total": webspace_total,
        "active_webspace_total": active_webspace_total,
        "compacted_webspace_total": compacted_total,
        "compaction_eligible_webspace_total": compaction_eligible_total,
        "update_log_total": update_log_total,
        "replay_window_total": replay_window_total,
        "replay_window_byte_total": replay_window_byte_total,
        "backup_fast_path_total": backup_fast_path_total,
        "backup_skipped_total": backup_skipped_total,
        "state_vector_fast_path_total": state_vector_fast_path_total,
        "state_vector_compute_total": state_vector_compute_total,
        "replay_pressure_compaction_requested": replay_pressure_compaction_requested,
        "webspaces": webspaces,
    }


def _request_yjs_replay_pressure_compaction(
    webspace_id: str | None,
    selected_entry: dict[str, Any] | None,
    *,
    assessment_state: str,
    reasons: list[str],
) -> bool:
    if str(os.getenv("ADAOS_YSTORE_AUTOCOMPACT_ON_REPLAY_PRESSURE", "1") or "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    key = str(webspace_id or "").strip()
    entry = selected_entry if isinstance(selected_entry, dict) else {}
    if not key or str(assessment_state or "").strip().lower() != "pressure":
        return False
    if "bounded_replay_window_near_limit" not in [str(item or "").strip() for item in reasons]:
        return False
    if not bool(entry.get("runtime_compaction_eligible")):
        return False
    replay_limit = int(entry.get("replay_window_limit") or 0)
    replay_entries = int(entry.get("replay_window_entries") or 0)
    if replay_limit <= 0 or (float(replay_entries) / float(replay_limit)) < 0.9:
        return False
    try:
        quiet_sec = float(str(os.getenv("ADAOS_YSTORE_AUTOCOMPACT_REPLAY_PRESSURE_QUIET_SEC") or "2.0").strip())
    except Exception:
        quiet_sec = 2.0
    quiet_sec = max(0.0, min(300.0, quiet_sec))
    if bool(entry.get("auto_backup_inflight")):
        return False
    last_write_ago = entry.get("last_write_ago_s")
    if quiet_sec > 0.0 and isinstance(last_write_ago, (int, float)) and float(last_write_ago) < quiet_sec:
        return False

    async def _runner() -> None:
        try:
            from adaos.services.yjs.store import get_ystore_for_webspace

            store = get_ystore_for_webspace(key)
            await store.request_runtime_compaction(
                reason="replay_pressure",
                min_quiet_sec=quiet_sec,
            )
        except Exception:
            _log.debug("failed to request YStore replay-pressure compaction webspace=%s", key, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        thread = threading.Thread(
            target=lambda: asyncio.run(_runner()),
            name=f"adaos-ystore-replay-compact-{key}",
            daemon=True,
        )
        thread.start()
        return True
    loop.create_task(_runner())
    return True


def _build_yjs_sync_channel_contract() -> dict[str, Any]:
    return {
        "channel_type": "sync_channel",
        "transport_independence": "bounded_runtime_and_resync",
        "recovery_model": "snapshot_plus_diff",
        "replay_window": "bounded",
        "browser_local_persistence": "optional_indexeddb",
        "explicit_resync_controls": ["reload", "restore", "reset"],
        "awareness_semantics": "ephemeral",
        "transport_paths": ["yws", "webrtc_data:yjs"],
        "completed_for_scope": True,
    }


def _build_yjs_ownership_boundaries(
    *,
    selected_webspace_id: str | None,
    selected_webspace: dict[str, Any] | None,
    transport: dict[str, Any] | None,
) -> dict[str, Any]:
    selected = selected_webspace if isinstance(selected_webspace, dict) else {}
    rebuild = selected.get("rebuild") if isinstance(selected.get("rebuild"), dict) else {}
    materialization = (
        rebuild.get("materialization")
        if isinstance(rebuild.get("materialization"), dict)
        else {}
    )
    has_materialization = bool(materialization)
    compatibility = (
        materialization.get("compatibility_caches")
        if isinstance(materialization.get("compatibility_caches"), dict)
        else {}
    )
    current_scenario = (
        str(materialization.get("current_scenario") or compatibility.get("current_scenario") or "").strip()
        or None
    )
    home_scenario = str(selected.get("home_scenario") or "").strip() or None
    missing_effective = {
        str(path or "").strip()
        for path in list(materialization.get("missing_branches") or [])
        if str(path or "").strip()
    }
    effective_specs = (
        ("ui.application", "desktop_application_projection", "semantic_rebuild"),
        ("data.catalog", "scenario_catalog_projection", "semantic_rebuild"),
        ("data.installed", "desktop_installed_overlay", "semantic_rebuild"),
        ("data.desktop", "desktop_surface_projection", "semantic_rebuild"),
        ("data.routing", "route_projection", "semantic_rebuild"),
        ("registry.merged", "registry_projection", "semantic_rebuild"),
    )
    effective_branches = [
        {
            "path": path,
            "role": role,
            "owner": "runtime",
            "source_of_truth": source_of_truth,
            "status": "tracked" if not has_materialization else ("missing" if path in missing_effective else "ready"),
        }
        for path, role, source_of_truth in effective_specs
    ]
    required_compatibility = [
        str(path or "").strip()
        for path in list(compatibility.get("required_branches") or [])
        if str(path or "").strip()
    ]
    present_compatibility = {
        str(path or "").strip()
        for path in list(compatibility.get("present_branches") or [])
        if str(path or "").strip()
    }
    compatibility_branches = [
        {
            "path": path,
            "role": "scenario_compatibility_cache",
            "owner": "runtime",
            "source_of_truth": "compatibility_cache",
            "status": "present" if path in present_compatibility else "missing",
        }
        for path in required_compatibility
    ]
    compatibility_mode = "not_applicable"
    if required_compatibility:
        compatibility_mode = "legacy_fallback_active" if bool(compatibility.get("legacy_fallback_active")) else "fallback_cache"
    transport_state = transport if isinstance(transport, dict) else {}
    transport_owner = str(transport_state.get("owner") or "").strip() or "runtime"
    planned_owner = str(transport_state.get("planned_owner") or "").strip() or None
    selector_status = "ready" if current_scenario else "unset"
    summary = (
        "ui.current_scenario stays shared, effective desktop/state branches are runtime-materialized, "
        f"compatibility caches run as {compatibility_mode}, and yws transport stays {transport_owner}-owned"
    )
    if planned_owner and planned_owner != transport_owner:
        summary += f" until {planned_owner} handoff is complete"
    return {
        "state": "explicit",
        "summary": summary,
        "selected_webspace_id": str(selected_webspace_id or "").strip() or None,
        "selector": {
            "path": "ui.current_scenario",
            "owner": "shared",
            "authority": "scenario_selection",
            "source_of_truth": "live_yjs_selector",
            "status": selector_status,
            "current_scenario": current_scenario,
            "home_scenario": home_scenario,
        },
        "effective_projection": {
            "owner": "runtime",
            "authority": "semantic_rebuild",
            "source_of_truth": "scenario_projection_plus_overlays",
            "ready": bool(materialization.get("ready")),
            "readiness_state": str(materialization.get("readiness_state") or "").strip() or None,
            "missing_branches": sorted(missing_effective),
            "branch_total": len(effective_branches),
            "branches": effective_branches,
        },
        "compatibility_caches": {
            "owner": "runtime",
            "mode": compatibility_mode,
            "legacy_fallback_active": bool(compatibility.get("legacy_fallback_active")),
            "runtime_removal_ready": bool(compatibility.get("runtime_removal_ready")),
            "runtime_removal_blockers": list(compatibility.get("runtime_removal_blockers") or []),
            "present_count": int(compatibility.get("present_count") or 0),
            "required_count": int(compatibility.get("required_count") or 0),
            "branch_total": len(compatibility_branches),
            "branches": compatibility_branches,
        },
        "runtime_meta": {
            "path": "registry.runtime_meta.effective_branch_fingerprints",
            "owner": "runtime",
            "source_of_truth": "semantic_rebuild",
            "status": "tracked",
            "purpose": "effective_branch_fingerprints",
        },
        "transport_session": {
            "path": "yws.transport_session",
            "owner": transport_owner,
            "planned_owner": planned_owner,
            "lifecycle_manager": transport_state.get("lifecycle_manager"),
            "migration_phase": transport_state.get("migration_phase"),
            "ownership_boundary": "transport_only",
            "handoff_ready": bool(transport_state.get("handoff_ready")),
            "handoff_blockers": list(transport_state.get("handoff_blockers") or []),
        },
    }


def _event_model_phase0_task(
    *,
    task_id: str,
    status: str,
    summary: str,
    completed_criteria: list[str] | None = None,
    pending_criteria: list[str] | None = None,
    pending_reasons: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": str(status or "").strip() or "in_progress",
        "summary": str(summary or "").strip(),
        "completed_criteria": list(completed_criteria or []),
        "pending_criteria": list(pending_criteria or []),
        "pending_reasons": list(pending_reasons or []),
        "evidence": dict(evidence or {}),
    }


def _is_local_http_base(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _supervisor_public_base_candidates() -> list[str]:
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
    result: list[str] = []
    seen: set[str] = set()
    for base in bases:
        token = str(base or "").strip().rstrip("/")
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _supervisor_browser_safe_surface(*, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    available = bool(data.get("available"))
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    blockers: list[str] = []
    transition_mode_visible = "transition_mode" in runtime
    candidate_runtime_visible = all(
        key in runtime
        for key in (
            "candidate_slot",
            "candidate_runtime_url",
            "candidate_runtime_port",
            "candidate_runtime_instance_id",
            "candidate_runtime_state",
            "candidate_runtime_api_ready",
            "candidate_transition_role",
        )
    )
    warm_switch_visible = any(
        key in runtime
        for key in (
            "warm_switch_supported",
            "warm_switch_allowed",
            "warm_switch_reason",
        )
    )
    if not available:
        blockers.append("supervisor.public_update_status.unavailable")
    if not transition_mode_visible:
        blockers.append("supervisor.transition_mode.hidden")
    if not candidate_runtime_visible:
        blockers.append("supervisor.candidate_runtime.hidden")
    if not warm_switch_visible:
        blockers.append("supervisor.warm_switch.hidden")
    ready = not blockers
    return {
        "state": "ready" if ready else ("unavailable" if not available else "in_progress"),
        "ready": ready,
        "carried_by_reliability": True,
        "transition_state": str(status.get("state") or "").strip().lower() or None,
        "transition_phase": str(status.get("phase") or "").strip().lower() or None,
        "transition_mode_visible": transition_mode_visible,
        "candidate_runtime_visible": candidate_runtime_visible,
        "warm_switch_visible": warm_switch_visible,
        "served_by": str(data.get("_served_by") or "").strip() or None,
        "blockers": blockers,
    }


def _supervisor_required_upstream_link(*, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    embedded = (
        runtime.get("required_upstream_link")
        if isinstance(runtime.get("required_upstream_link"), dict)
        else {}
    )
    if embedded:
        return dict(embedded)
    role = str(runtime.get("transition_role") or "").strip().lower() or None
    hub_root = runtime.get("hub_root_watchdog") if isinstance(runtime.get("hub_root_watchdog"), dict) else {}
    member_hub = runtime.get("member_hub_watchdog") if isinstance(runtime.get("member_hub_watchdog"), dict) else {}

    if role == "member":
        kind = "member_hub"
        watchdog = member_hub
    elif role == "hub":
        kind = "hub_root"
        watchdog = hub_root
    elif member_hub:
        kind = "member_hub"
        watchdog = member_hub
    else:
        kind = "hub_root"
        watchdog = hub_root

    state = str(watchdog.get("last_state") or "").strip().lower() or ("unavailable" if not data.get("available") else "unknown")
    reason = str(watchdog.get("last_reason") or "").strip() or None
    reconnect_total = int(watchdog.get("reconnect_total") or 0)
    cooldown_sec = float(watchdog.get("cooldown_sec") or 0.0)
    verify_timeout_sec = float(watchdog.get("verify_timeout_sec") or 0.0)
    visible = bool(watchdog)
    blockers: list[str] = []
    if not data.get("available"):
        blockers.append("supervisor.public_update_status.unavailable")
    if not visible:
        blockers.append(f"supervisor.{kind}.watchdog.hidden")
    ready_states = {"ready", "not_applicable"}
    paused_states = {"waiting_restart", "restarting", "paused_for_update", "cooldown"}
    ready = state in ready_states or state in paused_states
    owner = "supervisor" if bool(data.get("available")) else "unknown"
    return {
        "kind": kind,
        "role": role,
        "owner": owner,
        "state": state,
        "reason": reason,
        "ready": ready if visible else False,
        "visible": visible,
        "reconnect_total": reconnect_total,
        "cooldown_sec": cooldown_sec,
        "verify_timeout_sec": verify_timeout_sec,
        "served_by": str(data.get("_served_by") or "").strip() or None,
        "blockers": blockers,
    }


def _enrich_required_upstream_link_with_sidecar(
    *,
    required_upstream_link: dict[str, Any] | None,
    sidecar_runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    link = dict(required_upstream_link or {})
    sidecar = sidecar_runtime if isinstance(sidecar_runtime, dict) else {}
    if not link:
        return link

    continuity_contract = (
        sidecar.get("continuity_contract")
        if isinstance(sidecar.get("continuity_contract"), dict)
        else {}
    )
    route_tunnel_contract = (
        sidecar.get("route_tunnel_contract")
        if isinstance(sidecar.get("route_tunnel_contract"), dict)
        else {}
    )
    transport_owner = str(sidecar.get("transport_owner") or "").strip().lower() or None
    sidecar_enabled = bool(sidecar.get("enabled"))
    lifecycle_manager = str(sidecar.get("lifecycle_manager") or "").strip().lower() or None
    current_support = str(continuity_contract.get("current_support") or "").strip().lower() or None
    link["sidecar_enabled"] = sidecar_enabled
    link["sidecar_lifecycle_manager"] = lifecycle_manager
    if current_support:
        link["current_support"] = current_support

    kind = str(link.get("kind") or "").strip().lower()
    if kind == "hub_root":
        if transport_owner == "sidecar":
            link["current_owner"] = "sidecar"
            link["planned_owner"] = "sidecar"
            link["continuity_mode"] = "slot_sticky"
        link["handoff_state"] = "ready" if str(link.get("current_owner") or "") == "sidecar" else "not_applicable"
        link["handoff_ready"] = bool(str(link.get("current_owner") or "") == "sidecar")
        link["recovery_policy"] = {
            "on_runtime_restart": "preserve_sidecar" if str(link.get("current_owner") or "") == "sidecar" else "runtime_reconnect",
            "while_owner_runtime": "runtime_reconnect",
            "while_owner_sidecar": "preserve_sidecar",
        }
        return link

    if kind != "member_hub":
        return link

    ws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")
    ws_planned = str(ws_entry.get("planned_owner") or "").strip().lower() == "sidecar"
    yws_planned = str(yws_entry.get("planned_owner") or "").strip().lower() == "sidecar"
    ws_current = str(ws_entry.get("current_owner") or "").strip().lower() == "sidecar"
    yws_current = str(yws_entry.get("current_owner") or "").strip().lower() == "sidecar"
    ws_ready = bool(ws_entry.get("handoff_ready"))
    yws_ready = bool(yws_entry.get("handoff_ready"))
    handoff_planned = sidecar_enabled and (ws_planned or yws_planned)
    handoff_ready = sidecar_enabled and ws_current and yws_current and ws_ready and yws_ready
    blockers: list[str] = [str(item).strip() for item in (link.get("blockers") or []) if str(item).strip()]
    if handoff_planned and not handoff_ready:
        for boundary, entry in (("browser_events_ws", ws_entry), ("browser_yjs_ws", yws_entry)):
            for item in (entry.get("blockers") or []):
                text = str(item).strip()
                if text:
                    blockers.append(f"{boundary}: {text}")
                    break
    if handoff_ready:
        link["current_owner"] = "sidecar"
        link["planned_owner"] = "sidecar"
        link["continuity_mode"] = "slot_sticky"
        link["handoff_state"] = "ready"
    elif handoff_planned:
        link["planned_owner"] = "sidecar"
        link["continuity_mode"] = "handoff_planned"
        listener_ready = bool(ws_entry.get("listener_ready")) or bool(yws_entry.get("listener_ready"))
        proxy_ready = (
            str(ws_entry.get("current_support") or "").strip().lower() == "proxy_ready"
            or str(yws_entry.get("current_support") or "").strip().lower() == "proxy_ready"
        )
        link["handoff_state"] = "in_progress" if listener_ready or proxy_ready else "planned"
    else:
        link["handoff_state"] = "not_planned" if sidecar_enabled else "disabled"
    link["handoff_ready"] = handoff_ready
    link["blockers"] = blockers
    link["recovery_policy"] = {
        "on_runtime_restart": "preserve_sidecar" if handoff_ready else "runtime_reconnect",
        "while_owner_runtime": "runtime_reconnect",
        "while_owner_sidecar": "preserve_sidecar",
    }
    return link


def _ws_base_from_http_base(value: str | None) -> str | None:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return None
    if raw.startswith("https://"):
        return "wss://" + raw[len("https://"):]
    if raw.startswith("http://"):
        return "ws://" + raw[len("http://"):]
    return raw


def _routed_browser_supervisor_surface(
    *,
    protocol_payload: dict[str, Any] | None,
    supervisor_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    protocol = protocol_payload if isinstance(protocol_payload, dict) else {}
    supervisor = supervisor_payload if isinstance(supervisor_payload, dict) else {}
    route_runtime = (
        protocol.get("route_runtime")
        if isinstance(protocol.get("route_runtime"), dict)
        else {}
    )
    runtime = (
        supervisor.get("runtime")
        if isinstance(supervisor.get("runtime"), dict)
        else {}
    )
    source = str(route_runtime.get("local_base_last_source") or "").strip() or None
    selected_http_base = str(route_runtime.get("local_base_last_value") or "").strip().rstrip("/") or None
    runtime_url = str(runtime.get("runtime_url") or "").strip().rstrip("/") or None
    if not selected_http_base and runtime_url and _is_local_http_base(runtime_url):
        selected_http_base = runtime_url
        if not source:
            source = "supervisor_runtime"
    selected_ws_base = _ws_base_from_http_base(selected_http_base)
    discovery_total = int(route_runtime.get("local_base_discovery_total") or 0)
    cache_hit_total = int(route_runtime.get("local_base_cache_hit_total") or 0)
    runtime_port_shortcut_total = int(route_runtime.get("local_base_runtime_port_shortcut_total") or 0)
    error_total = int(route_runtime.get("local_base_error_total") or 0)
    last_error = str(route_runtime.get("local_base_last_error") or "").strip() or None
    last_open_base_total = int(route_runtime.get("last_open_base_total") or 0)
    ready = bool(selected_http_base)
    blockers: list[str] = []
    if not ready:
        blockers.append("route_runtime.active_runtime_base.unresolved")
        if last_error:
            blockers.append(last_error)
    if source in {"supervisor_public_status", "cache", "supervisor_runtime"}:
        selection_mode = "supervisor_active_runtime"
    elif source in {"runtime_port_env", "runtime_port_probe"}:
        selection_mode = "runtime_port_env"
    else:
        selection_mode = None
    return {
        "state": "ready" if ready else "in_progress",
        "ready": ready,
        "summary": (
            "root-routed browser proxy can resolve the active runtime base during supervisor-managed transitions"
            if ready
            else "root-routed browser proxy has not observed an active runtime base for supervisor-managed transitions yet"
        ),
        "source": source,
        "selection_mode": selection_mode,
        "selected_http_base": selected_http_base,
        "selected_ws_base": selected_ws_base,
        "active_runtime_visible": bool(runtime_url and _is_local_http_base(runtime_url)),
        "discovery_total": discovery_total,
        "cache_hit_total": cache_hit_total,
        "runtime_port_shortcut_total": runtime_port_shortcut_total,
        "error_total": error_total,
        "last_error": last_error,
        "last_open_base_total": last_open_base_total,
        "blockers": blockers,
    }


def _planned_transition_snapshot_from_supervisor_status(
    status: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    data = status if isinstance(status, dict) else {}
    state = str(data.get("state") or "").strip().lower()
    phase = str(data.get("phase") or "").strip().lower()
    action = str(data.get("action") or "").strip().lower() or None
    if state in {"countdown", "preparing"} or phase in {"scheduled", "prepare", "drain"}:
        return "waiting_restart", {"active": True, "reason": action or "core_update"}
    if state in {"restarting", "validated"} or phase in {"launch", "root_promotion_pending", "root_promoted"}:
        return "restarting", {"active": True, "reason": action or "core_update"}
    return "ready", {"active": False, "reason": None}


def _map_connectivity_transport_state(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"ready", "reachable", "connected", "nominal", "stable", "attached", "active"}:
        return "ready"
    if token in {"degraded", "pressure", "unstable", "flapping", "reconnecting", "cooldown"}:
        return "degraded"
    if token in {"down", "disconnected", "failed", "offline"}:
        return "disconnected"
    if token in {"disabled", "not_applicable"}:
        return "not_applicable"
    return "unknown"


def _connectivity_transition_state_for_link(
    link: dict[str, Any] | None,
    *,
    supervisor_status: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    data = link if isinstance(link, dict) else {}
    raw_state = str(data.get("state") or "").strip().lower()
    if raw_state in {"waiting_restart", "restarting", "paused_for_update", "disabled", "not_applicable"}:
        active = raw_state in {"waiting_restart", "restarting", "paused_for_update"}
        reason = str(data.get("reason") or "").strip() or None
        return raw_state, {"active": active, "reason": reason}
    if raw_state in {"cooldown", "reconnect", "reconnecting", "verify", "verifying"}:
        reason = str(data.get("reason") or "").strip() or None
        return "reconnecting", {"active": True, "reason": reason}
    derived_state, planned = _planned_transition_snapshot_from_supervisor_status(supervisor_status)
    if planned.get("active"):
        return derived_state, planned
    transport_state = _map_connectivity_transport_state(raw_state)
    if transport_state == "ready":
        return "ready", {"active": False, "reason": None}
    if transport_state == "disconnected":
        return "reconnecting", {"active": False, "reason": str(data.get("reason") or "").strip() or None}
    if transport_state == "degraded":
        return "reconnecting", {"active": False, "reason": str(data.get("reason") or "").strip() or None}
    return "unknown", {"active": False, "reason": str(data.get("reason") or "").strip() or None}


def _connectivity_transition_state_for_browser_route(
    route_item: dict[str, Any] | None,
    *,
    supervisor_status: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    item = route_item if isinstance(route_item, dict) else {}
    derived_state, planned = _planned_transition_snapshot_from_supervisor_status(supervisor_status)
    if planned.get("active"):
        return derived_state, planned
    effective_state = str(item.get("effective_state") or "").strip().lower()
    transport_state = _map_connectivity_transport_state(item.get("effective_status"))
    if effective_state in {"flapping", "unstable"}:
        return "reconnecting", {"active": False, "reason": effective_state}
    if transport_state == "ready":
        return "ready", {"active": False, "reason": None}
    if transport_state in {"degraded", "disconnected"}:
        return "reconnecting", {"active": False, "reason": effective_state or transport_state}
    if transport_state == "not_applicable":
        return "disabled", {"active": False, "reason": "not_applicable"}
    return "unknown", {"active": False, "reason": effective_state or None}


def _connectivity_snapshot(
    *,
    node_id: str | None,
    channel_overview: dict[str, Any] | None,
    supervisor_runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    overview = channel_overview if isinstance(channel_overview, dict) else {}
    supervisor = supervisor_runtime if isinstance(supervisor_runtime, dict) else {}
    status = supervisor.get("status") if isinstance(supervisor.get("status"), dict) else {}
    required_link = (
        supervisor.get("required_upstream_link")
        if isinstance(supervisor.get("required_upstream_link"), dict)
        else {}
    )
    browser_route = (
        overview.get("hub_root_browser")
        if isinstance(overview.get("hub_root_browser"), dict)
        else {}
    )
    link_transition_state, link_planned = _connectivity_transition_state_for_link(
        required_link,
        supervisor_status=status,
    )
    route_transition_state, route_planned = _connectivity_transition_state_for_browser_route(
        browser_route,
        supervisor_status=status,
    )
    return {
        "required_upstream_link": {
            "kind": str(required_link.get("kind") or "").strip() or "unknown",
            "scope_id": str(node_id or "").strip() or None,
            "transport_state": _map_connectivity_transport_state(required_link.get("state")),
            "transition_state": link_transition_state,
            "planned_transition": link_planned,
            "reason": str(required_link.get("reason") or "").strip() or None,
            "blockers": list(required_link.get("blockers") or []),
            "served_by": str(required_link.get("served_by") or required_link.get("owner") or "").strip() or None,
        },
        "browser_control_route": {
            "kind": "browser_control_route",
            "scope_id": str(node_id or "").strip() or None,
            "transport_state": _map_connectivity_transport_state(browser_route.get("effective_status")),
            "transition_state": route_transition_state,
            "planned_transition": route_planned,
            "reason": str(browser_route.get("effective_state") or "").strip() or None,
            "blockers": list(
                (
                    browser_route.get("diagnostics")
                    if isinstance(browser_route.get("diagnostics"), dict)
                    else {}
                ).get("blockers")
                or []
            ),
            "served_by": "runtime",
        },
    }


def _state_sync_snapshot(sync_runtime: dict[str, Any] | None) -> dict[str, Any]:
    runtime = sync_runtime if isinstance(sync_runtime, dict) else {}
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    transport = runtime.get("transport") if isinstance(runtime.get("transport"), dict) else {}
    selected_webspace = (
        runtime.get("selected_webspace")
        if isinstance(runtime.get("selected_webspace"), dict)
        else {}
    )
    selected_webspace_id = str(
        runtime.get("selected_webspace_id")
        or selected_webspace.get("webspace_id")
        or ""
    ).strip() or None
    load_mark = (
        selected_webspace.get("load_mark")
        if isinstance(selected_webspace.get("load_mark"), dict)
        else {}
    )
    rebuild = (
        selected_webspace.get("rebuild")
        if isinstance(selected_webspace.get("rebuild"), dict)
        else {}
    )
    materialization = (
        rebuild.get("materialization")
        if isinstance(rebuild.get("materialization"), dict)
        else {}
    )
    gateway_room = (
        selected_webspace.get("gateway_room")
        if isinstance(selected_webspace.get("gateway_room"), dict)
        else {}
    )
    webspaces = runtime.get("webspaces") if isinstance(runtime.get("webspaces"), dict) else {}
    selected_entry = (
        webspaces.get(selected_webspace_id)
        if selected_webspace_id and isinstance(webspaces.get(selected_webspace_id), dict)
        else {}
    )

    assessment_state = str(assessment.get("state") or "").strip().lower() or "unknown"
    assessment_reasons = [
        item.strip()
        for item in str(assessment.get("reason") or "").split(";")
        if item.strip()
    ]
    maintenance_pressure_only = bool(
        assessment_state == "pressure"
        and assessment_reasons
        and all(item == "bounded_replay_window_near_limit" for item in assessment_reasons)
    )
    if not bool(runtime.get("available")) and assessment_state == "not_applicable":
        transport_state = "not_applicable"
    elif bool(transport.get("server_ready")) or bool(gateway_room.get("ready")):
        transport_state = "attached"
    elif int(transport.get("active_yws_connections") or 0) > 0 or int(transport.get("webrtc_open_yjs_channels") or 0) > 0:
        transport_state = "attached"
    elif assessment_state in {"degraded", "pressure", "unavailable"}:
        transport_state = "degraded"
    elif not bool(runtime.get("available")):
        transport_state = "disconnected"
    else:
        transport_state = "unknown"

    if transport_state == "not_applicable":
        first_sync_state = "not_applicable"
    elif bool(gateway_room.get("ready")) or int(transport.get("active_yws_connections") or 0) > 0 or int(transport.get("webrtc_open_yjs_channels") or 0) > 0:
        first_sync_state = "complete"
    elif int(gateway_room.get("open_total") or 0) > 0 or int(transport.get("room_open_total") or 0) > 0:
        first_sync_state = "timeout" if assessment_state in {"degraded", "pressure", "unavailable"} else "pending"
    else:
        first_sync_state = "pending"

    materialization_ready = bool(materialization.get("ready"))
    if transport_state == "not_applicable":
        semantic_state = "not_applicable"
    elif materialization_ready and (assessment_state in {"nominal", "idle"} or maintenance_pressure_only):
        semantic_state = "ready"
    elif materialization_ready and assessment_state in {"pressure", "degraded"}:
        semantic_state = "degraded"
    else:
        semantic_state = "stale"

    freshness_state = (
        "fresh"
        if semantic_state == "ready"
        else "aging"
        if semantic_state == "degraded"
        else "stale"
        if semantic_state == "stale"
        else semantic_state
    )
    last_materialization_at = rebuild.get("finished_at") or rebuild.get("updated_at") or load_mark.get("updated_at")
    last_good_sync_at = gateway_room.get("last_open_at") or last_materialization_at
    replay_entries = int(selected_entry.get("replay_window_entries") or 0)
    replay_limit = int(selected_entry.get("replay_window_limit") or 0)

    blockers: list[str] = []
    reason = str(assessment.get("reason") or "").strip()
    if reason:
        blockers.append(reason)
    if semantic_state == "stale" and not materialization_ready:
        blockers.append(str(materialization.get("readiness_state") or "materialization_not_ready"))
    for item in list(materialization.get("missing_branches") or []):
        text = str(item).strip()
        if text:
            blockers.append(f"missing_branch:{text}")
    error = str(rebuild.get("error") or "").strip()
    if error:
        blockers.append(error)

    return {
        "webspace_id": selected_webspace_id,
        "transport_state": transport_state,
        "first_sync_state": first_sync_state,
        "semantic_state": semantic_state,
        "freshness_state": freshness_state,
        "last_good_sync_at": last_good_sync_at,
        "last_materialization_at": last_materialization_at,
        "replay": {
            "mode": str(
                (
                    runtime.get("channel_contract")
                    if isinstance(runtime.get("channel_contract"), dict)
                    else {}
                ).get("recovery_model")
                or "snapshot_plus_diff"
            ).strip()
            or "snapshot_plus_diff",
            "cursor": f"{replay_entries}/{replay_limit}" if replay_limit > 0 else f"{replay_entries}/0",
        },
        "fallback_mode": (
            "hard_degraded_recovery"
            if not bool(runtime.get("available")) and assessment_state != "not_applicable"
            else "off"
        ),
        "blockers": blockers,
    }


def _yjs_pressure_snapshot(sync_runtime: dict[str, Any] | None) -> dict[str, Any]:
    runtime = sync_runtime if isinstance(sync_runtime, dict) else {}
    selected_webspace_id = str(runtime.get("selected_webspace_id") or "").strip() or None
    try:
        from adaos.services.yjs.load_mark import yjs_primary_doc_policy_snapshot
        from adaos.services.yjs.governance import primary_doc_governance_snapshot

        payload = yjs_primary_doc_policy_snapshot(webspace_id=selected_webspace_id)
        if isinstance(payload, dict):
            governance = primary_doc_governance_snapshot(
                webspace_id=str(payload.get("webspace_id") or selected_webspace_id or "").strip() or None,
                owner=str(payload.get("owner") or "").strip() or None,
            )
            blocked_roots = list(payload.get("blocked_roots") or [])
            throttled_roots = list(payload.get("throttled_roots") or [])
            return {
                "webspace_id": str(payload.get("webspace_id") or selected_webspace_id or "").strip() or None,
                "owner": str(payload.get("owner") or "").strip() or None,
                "recent_bytes": int(payload.get("recent_bytes") or 0),
                "recent_writes": int(payload.get("recent_writes") or 0),
                "peak_bps": float(payload.get("peak_bps") or 0.0),
                "peak_wps": float(payload.get("peak_wps") or 0.0),
                "policy_state": str(payload.get("policy_state") or "ok").strip() or "ok",
                "target": str(payload.get("target") or "primary_shared_doc").strip() or "primary_shared_doc",
                "reason": str(payload.get("reason") or "healthy").strip() or "healthy",
                "blocked_roots": blocked_roots,
                "throttled_roots": throttled_roots,
                "affected_roots": blocked_roots or throttled_roots,
                "observed_state": str(payload.get("observed_state") or "idle").strip() or "idle",
                "blocked_total": int(governance.get("blocked_total") or 0),
                "throttled_total": int(governance.get("throttled_total") or 0),
                "quarantined": bool(governance.get("quarantined")),
                "quarantine_total": int(governance.get("quarantine_total") or 0),
                "quarantine_denied_total": int(governance.get("quarantine_denied_total") or 0),
                "quarantine_remaining_s": float(governance.get("quarantine_remaining_s") or 0.0) or None,
                "quarantine_reason": str(governance.get("quarantine_reason") or "").strip() or None,
                "quarantine_trigger": str(governance.get("quarantine_trigger") or "").strip() or None,
                "quarantine_path": str(governance.get("quarantine_path") or "").strip() or None,
                "quarantine_tool": str(governance.get("quarantine_tool") or "").strip() or None,
                "last_policy_state": str(governance.get("last_policy_state") or "").strip() or None,
                "last_reason": str(governance.get("last_reason") or "").strip() or None,
                "last_path": str(governance.get("last_path") or "").strip() or None,
                "last_at": float(governance.get("last_at") or 0.0) or None,
                "last_blocked_roots": list(governance.get("last_blocked_roots") or []),
                "last_throttled_roots": list(governance.get("last_throttled_roots") or []),
                "last_affected_roots": list(governance.get("last_affected_roots") or []),
            }
    except Exception:
        pass

    return {
        "webspace_id": selected_webspace_id,
        "owner": None,
        "recent_bytes": 0,
        "recent_writes": 0,
        "peak_bps": 0.0,
        "peak_wps": 0.0,
        "policy_state": "ok",
        "target": "primary_shared_doc",
        "reason": "healthy",
        "blocked_roots": [],
        "throttled_roots": [],
        "affected_roots": [],
        "observed_state": "idle",
        "blocked_total": 0,
        "throttled_total": 0,
        "quarantined": False,
        "quarantine_total": 0,
        "quarantine_denied_total": 0,
        "quarantine_remaining_s": None,
        "quarantine_reason": None,
        "quarantine_trigger": None,
        "quarantine_path": None,
        "quarantine_tool": None,
        "last_policy_state": None,
        "last_reason": None,
        "last_path": None,
        "last_at": None,
        "last_blocked_roots": [],
        "last_throttled_roots": [],
        "last_affected_roots": [],
    }


def supervisor_transition_runtime_snapshot(*, timeout_sec: float = 1.0) -> dict[str, Any]:
    if str(os.getenv("ADAOS_SUPERVISOR_ENABLED", "0") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        payload = {
            "available": False,
            "source": "supervisor.disabled",
            "supervisor_url": None,
            "status": {},
            "attempt": {},
            "runtime": {},
            "_served_by": None,
        }
        payload["browser_safe_surface"] = _supervisor_browser_safe_surface(payload=payload)
        payload["required_upstream_link"] = _supervisor_required_upstream_link(payload=payload)
        return payload

    try:
        import requests  # type: ignore
    except Exception as exc:
        payload = {
            "available": False,
            "source": "supervisor.requests_unavailable",
            "supervisor_url": None,
            "status": {},
            "attempt": {},
            "runtime": {},
            "_served_by": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
        payload["browser_safe_surface"] = _supervisor_browser_safe_surface(payload=payload)
        payload["required_upstream_link"] = _supervisor_required_upstream_link(payload=payload)
        return payload

    session = requests.Session()
    try:
        with contextlib.suppress(Exception):
            session.trust_env = False
        last_error = ""
        for base in _supervisor_public_base_candidates():
            try:
                response = session.get(
                    base + "/api/supervisor/public/update-status",
                    headers={"Accept": "application/json"},
                    timeout=max(0.1, float(timeout_sec)),
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if int(response.status_code) != 200:
                last_error = f"status:{response.status_code}"
                continue
            try:
                body = response.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if not isinstance(body, dict):
                last_error = "non_dict_payload"
                continue
            runtime = body.get("runtime") if isinstance(body.get("runtime"), dict) else {}
            payload = {
                "available": True,
                "source": "supervisor.public_update_status",
                "supervisor_url": base,
                "status": dict(body.get("status") or {}) if isinstance(body.get("status"), dict) else {},
                "attempt": dict(body.get("attempt") or {}) if isinstance(body.get("attempt"), dict) else {},
                "runtime": {
                    **dict(runtime),
                    "supervisor_url": base,
                },
                "_served_by": str(body.get("_served_by") or "").strip() or None,
            }
            payload["browser_safe_surface"] = _supervisor_browser_safe_surface(payload=payload)
            payload["required_upstream_link"] = _supervisor_required_upstream_link(payload=payload)
            return payload
        payload = {
            "available": False,
            "source": "supervisor.public_update_status_unavailable",
            "supervisor_url": None,
            "status": {},
            "attempt": {},
            "runtime": {},
            "_served_by": None,
            "error": last_error or None,
        }
        payload["browser_safe_surface"] = _supervisor_browser_safe_surface(payload=payload)
        payload["required_upstream_link"] = _supervisor_required_upstream_link(payload=payload)
        return payload
    finally:
        with contextlib.suppress(Exception):
            session.close()


def _event_model_phase0_communication_checkpoint(
    *,
    sync_runtime: dict[str, Any] | None,
    sidecar_runtime: dict[str, Any] | None,
    hub_root_protocol: dict[str, Any] | None,
    supervisor_runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    sync_payload = sync_runtime if isinstance(sync_runtime, dict) else {}
    sidecar_payload = sidecar_runtime if isinstance(sidecar_runtime, dict) else {}
    protocol_payload = hub_root_protocol if isinstance(hub_root_protocol, dict) else {}
    supervisor_payload = supervisor_runtime if isinstance(supervisor_runtime, dict) else {}

    channel_contract = sync_payload.get("channel_contract") if isinstance(sync_payload.get("channel_contract"), dict) else {}
    transport = sync_payload.get("transport") if isinstance(sync_payload.get("transport"), dict) else {}
    continuity = (
        sidecar_payload.get("continuity_contract")
        if isinstance(sidecar_payload.get("continuity_contract"), dict)
        else {}
    )
    progress = sidecar_payload.get("progress") if isinstance(sidecar_payload.get("progress"), dict) else {}
    route_tunnel_contract = (
        sidecar_payload.get("route_tunnel_contract")
        if isinstance(sidecar_payload.get("route_tunnel_contract"), dict)
        else {}
    )
    hardening = (
        protocol_payload.get("hardening_coverage")
        if isinstance(protocol_payload.get("hardening_coverage"), dict)
        else {}
    )
    supervisor_surface = (
        supervisor_payload.get("browser_safe_surface")
        if isinstance(supervisor_payload.get("browser_safe_surface"), dict)
        else {}
    )
    routed_browser_surface = _routed_browser_supervisor_surface(
        protocol_payload=protocol_payload,
        supervisor_payload=supervisor_payload,
    )
    sidecar_enabled = bool(sidecar_payload.get("enabled"))
    ws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "ws")
    yws_entry = _sidecar_route_tunnel_entry(route_tunnel_contract, "yws")
    ws_handoff_state = _sidecar_route_tunnel_state(enabled=sidecar_enabled, entry=ws_entry)
    yws_handoff_state = _sidecar_route_tunnel_state(enabled=sidecar_enabled, entry=yws_entry)
    ws_handoff_ready = ws_handoff_state == "ready"
    yws_handoff_ready = yws_handoff_state == "ready"
    yjs_sync_scope_ready = bool(channel_contract.get("completed_for_scope"))
    hub_root_class_a_ready = str(hardening.get("state") or "").strip().lower() == "complete"
    continuity_required = bool(continuity.get("required"))
    continuity_state = str(continuity.get("current_support") or "").strip().lower() or "unknown"
    continuity_ready = continuity_state == "ready" or not continuity_required
    supervisor_ready = bool(supervisor_surface.get("ready"))
    ws_blocker = next(
        (str(item).strip() for item in (ws_entry.get("blockers") or []) if str(item).strip()),
        "",
    )
    yws_blocker = next(
        (str(item).strip() for item in (yws_entry.get("blockers") or []) if str(item).strip()),
        "",
    )

    node_completed = ["browser_member_semantic_channels"]
    node_pending: list[str] = []
    node_pending_reasons: list[str] = []
    if yjs_sync_scope_ready:
        node_completed.append("yjs_as_sync_channel")
    else:
        node_pending.append("yjs_as_sync_channel")
        node_pending_reasons.append("sync.channel_contract.incomplete")
    if yws_handoff_ready:
        node_completed.append("browser_yjs_ws_handoff")
    else:
        node_pending.append("browser_yjs_ws_handoff")
        node_pending_reasons.append(
            yws_blocker or f"sidecar.browser_yjs_ws_handoff.{yws_handoff_state}"
        )
    node_status = "done" if not node_pending else "in_progress"
    node_summary = (
        "browser/member communication prerequisites are ready for the current scope"
        if node_status == "done"
        else "browser/member semantic channels and Yjs SyncChannel are in place, but browser /yws transport ownership migration is still incomplete"
    )

    runtime_completed: list[str] = []
    runtime_pending: list[str] = []
    runtime_pending_reasons: list[str] = []
    if hub_root_class_a_ready:
        runtime_completed.append("hub_root_class_a_hardening")
    else:
        runtime_pending.append("hub_root_class_a_hardening")
        runtime_pending_reasons.append(
            f"hub_root.class_a.{str(hardening.get('state') or 'unknown').strip().lower() or 'unknown'}"
        )
    if ws_handoff_ready:
        runtime_completed.append("browser_events_ws_handoff")
    else:
        runtime_pending.append("browser_events_ws_handoff")
        runtime_pending_reasons.append(
            ws_blocker or f"sidecar.browser_events_ws_handoff.{ws_handoff_state}"
        )
    if yws_handoff_ready:
        runtime_completed.append("browser_yjs_ws_handoff")
    else:
        runtime_pending.append("browser_yjs_ws_handoff")
        runtime_pending_reasons.append(
            yws_blocker or f"sidecar.browser_yjs_ws_handoff.{yws_handoff_state}"
        )
    if continuity_ready:
        runtime_completed.append("sidecar_continuity")
    else:
        runtime_pending.append("sidecar_continuity")
        runtime_pending_reasons.append(f"sidecar.continuity.{continuity_state}")
    if supervisor_ready:
        runtime_completed.append("browser_safe_supervisor_continuity")
    else:
        runtime_pending.append("browser_safe_supervisor_continuity")
        runtime_pending_reasons.extend(
            [
                str(item).strip()
                for item in list(supervisor_surface.get("blockers") or [])
                if str(item).strip()
            ]
            or ["supervisor.browser_safe_continuity.in_progress"]
        )
    runtime_status = "done" if not runtime_pending else "in_progress"
    runtime_summary = (
        "hub-root Class A coverage, sidecar ownership expansion, and browser-safe supervisor continuity are ready"
        if runtime_status == "done"
        else (
            "hub-root Class A hardening is explicit, browser-safe supervisor state now rides through shared runtime surfaces, and routed browser proxy can follow the active runtime base, but sidecar ownership expansion still keeps runtime communication prerequisites open"
            if bool(routed_browser_surface.get("ready"))
            else "hub-root Class A hardening is explicit, and browser-safe supervisor state now rides through shared runtime surfaces, but sidecar ownership expansion still keeps runtime communication prerequisites open"
        )
    )

    tasks = {
        "phase0.node_browser_ready": _event_model_phase0_task(
            task_id="phase0.node_browser_ready",
            status=node_status,
            summary=node_summary,
            completed_criteria=node_completed,
            pending_criteria=node_pending,
            pending_reasons=node_pending_reasons,
            evidence={
                "yjs_sync_channel_ready": yjs_sync_scope_ready,
                "browser_yjs_ws_handoff": {
                    "state": yws_handoff_state,
                    "owner": str(yws_entry.get("current_owner") or "").strip().lower() or None,
                    "planned_owner": str(yws_entry.get("planned_owner") or "").strip().lower() or None,
                    "handoff_ready": bool(yws_entry.get("handoff_ready")),
                    "blocker": yws_blocker or None,
                },
                "sync_transport_owner": str(transport.get("owner") or "").strip().lower() or None,
                "sync_transport_planned_owner": str(transport.get("planned_owner") or "").strip().lower() or None,
            },
        ),
        "phase0.runtime_comm_ready": _event_model_phase0_task(
            task_id="phase0.runtime_comm_ready",
            status=runtime_status,
            summary=runtime_summary,
            completed_criteria=runtime_completed,
            pending_criteria=runtime_pending,
            pending_reasons=runtime_pending_reasons,
            evidence={
                "hub_root_class_a": {
                    "state": str(hardening.get("state") or "").strip().lower() or "unknown",
                    "covered_flows": int(hardening.get("covered_flows") or 0),
                    "total_flows": int(hardening.get("total_flows") or 0),
                },
                "browser_events_ws_handoff": {
                    "state": ws_handoff_state,
                    "owner": str(ws_entry.get("current_owner") or "").strip().lower() or None,
                    "planned_owner": str(ws_entry.get("planned_owner") or "").strip().lower() or None,
                    "handoff_ready": bool(ws_entry.get("handoff_ready")),
                    "blocker": ws_blocker or None,
                },
                "browser_yjs_ws_handoff": {
                    "state": yws_handoff_state,
                    "owner": str(yws_entry.get("current_owner") or "").strip().lower() or None,
                    "planned_owner": str(yws_entry.get("planned_owner") or "").strip().lower() or None,
                    "handoff_ready": bool(yws_entry.get("handoff_ready")),
                    "blocker": yws_blocker or None,
                },
                "sidecar_continuity": {
                    "state": continuity_state,
                    "required": continuity_required,
                    "hub_runtime_update": str(continuity.get("hub_runtime_update") or "").strip().lower() or None,
                    "pending_boundaries": list(continuity.get("pending_boundaries") or []),
                },
                "browser_safe_supervisor_continuity": {
                    "state": str(supervisor_surface.get("state") or "").strip().lower() or ("ready" if supervisor_ready else "in_progress"),
                    "summary": (
                        "browser-safe supervisor transition state is carried through the shared reliability runtime surface, and routed browser proxy can resolve the active runtime base during warm-switch handoff"
                        if supervisor_ready
                        else "browser-safe supervisor and warm-switch continuity hardening still remains open across browser topologies"
                    ),
                    "source": str(supervisor_payload.get("source") or "").strip() or None,
                    "served_by": str(supervisor_payload.get("_served_by") or "").strip() or None,
                    "transition_state": str(supervisor_surface.get("transition_state") or "").strip().lower() or None,
                    "transition_phase": str(supervisor_surface.get("transition_phase") or "").strip().lower() or None,
                    "carried_by_reliability": bool(supervisor_surface.get("carried_by_reliability")),
                    "transition_mode_visible": bool(supervisor_surface.get("transition_mode_visible")),
                    "candidate_runtime_visible": bool(supervisor_surface.get("candidate_runtime_visible")),
                    "warm_switch_visible": bool(supervisor_surface.get("warm_switch_visible")),
                    "routed_browser_proxy": routed_browser_surface,
                    "blockers": list(supervisor_surface.get("blockers") or []),
                },
                "sidecar_progress": {
                    "state": str(progress.get("state") or "").strip().lower() or "unknown",
                    "completed_milestones": int(progress.get("completed_milestones") or 0),
                    "milestone_total": int(progress.get("milestone_total") or 0),
                    "current_milestone": str(progress.get("current_milestone") or "").strip() or None,
                },
            },
        ),
    }
    remaining_tasks = [
        task_id
        for task_id, entry in tasks.items()
        if isinstance(entry, dict) and str(entry.get("status") or "") != "done"
    ]
    return {
        "state": "ready" if not remaining_tasks else "in_progress",
        "ready": not remaining_tasks,
        "tracked_tasks": list(tasks.keys()),
        "completed_task_total": len(tasks) - len(remaining_tasks),
        "task_total": len(tasks),
        "remaining_tasks": remaining_tasks,
        "tasks": tasks,
    }


def _build_yjs_selected_webspace_snapshot(webspace_id: str | None) -> dict[str, Any]:
    target_webspace_id = str(webspace_id or "").strip() or "default"
    try:
        from adaos.services.scenario.webspace_runtime import describe_webspace_rebuild_state
        from adaos.services.workspaces import index as workspace_index

        row = workspace_index.get_workspace(target_webspace_id) or workspace_index.ensure_workspace(target_webspace_id)
        target_space = "dev" if bool(getattr(row, "is_dev", False)) else "workspace"
        active_scenario = None
        active_space = None
        active_matches_target = None
        try:
            registry = get_ctx().projections
            raw_snapshot = registry.snapshot() if hasattr(registry, "snapshot") else {}
            registry_snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}
            token = str(registry_snapshot.get("active_scenario_id") or "").strip()
            active_scenario = token or None
            token = str(registry_snapshot.get("active_space") or "").strip()
            active_space = token or None
            if active_scenario and active_space:
                active_matches_target = (
                    active_scenario == str(getattr(row, "effective_home_scenario", "") or "").strip()
                    and active_space == target_space
                )
        except Exception:
            pass
        rebuild = describe_webspace_rebuild_state(target_webspace_id)
        return {
            "webspace_id": target_webspace_id,
            "title": str(getattr(row, "title", "") or target_webspace_id),
            "kind": str(getattr(row, "effective_kind", "") or "workspace"),
            "source_mode": str(getattr(row, "effective_source_mode", "") or target_space),
            "is_dev": bool(getattr(row, "is_dev", False)),
            "home_scenario": str(getattr(row, "effective_home_scenario", "") or "") or None,
            "projection_target_space": target_space,
            "projection_active_scenario": active_scenario,
            "projection_active_space": active_space,
            "projection_matches_home": active_matches_target,
            "rebuild": rebuild if isinstance(rebuild, dict) else {},
        }
    except Exception as exc:
        return {
            "webspace_id": target_webspace_id,
            "title": target_webspace_id,
            "kind": "workspace",
            "source_mode": "workspace",
            "is_dev": False,
            "home_scenario": None,
            "projection_target_space": "workspace",
            "projection_active_scenario": None,
            "projection_active_space": None,
            "projection_matches_home": None,
            "rebuild": {"status": "unknown", "error": f"{type(exc).__name__}: {exc}"},
            "error": f"{type(exc).__name__}: {exc}",
        }


def _as_runtime_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _as_runtime_plain(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_as_runtime_plain(item) for item in value]
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            raw = to_json()
            if isinstance(raw, str):
                return json.loads(raw)
            return _as_runtime_plain(raw)
        except Exception:
            pass
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return {key: _as_runtime_plain(nested) for key, nested in items()}
        except Exception:
            pass
    if isinstance(value, tuple):
        return [_as_runtime_plain(item) for item in value]
    return value


def _as_runtime_dict(value: Any) -> dict[str, Any]:
    plain = _as_runtime_plain(value)
    return dict(plain) if isinstance(plain, dict) else {}


def _as_runtime_list(value: Any) -> list[Any]:
    plain = _as_runtime_plain(value)
    return list(plain) if isinstance(plain, list) else []


def _live_yjs_materialization_snapshot(webspace_id: str | None) -> dict[str, Any] | None:
    target_webspace_id = str(webspace_id or "").strip()
    if not target_webspace_id:
        return None
    try:
        from adaos.services.yjs.doc import _can_access_live_room_directly, _resolve_live_room  # noqa: PLC2701

        room = _resolve_live_room(target_webspace_id)
        if not _can_access_live_room_directly(room):
            return None
        ui_map = room.ydoc.get_map("ui")
        data_map = room.ydoc.get_map("data")
        registry_map = room.ydoc.get_map("registry")
        application = _as_runtime_dict(ui_map.get("application") or {})
        desktop = _as_runtime_dict(application.get("desktop") or {})
        modals = _as_runtime_dict(application.get("modals") or {})
        catalog = _as_runtime_dict(data_map.get("catalog") or {})
        apps = _as_runtime_list(catalog.get("apps"))
        widgets = _as_runtime_list(catalog.get("widgets"))
        page_schema = _as_runtime_dict(desktop.get("pageSchema") or {})
        page_widgets = _as_runtime_list(page_schema.get("widgets"))
        topbar = _as_runtime_list(desktop.get("topbar"))
        current_scenario = str(ui_map.get("current_scenario") or "").strip() or None
        has_ui_application = bool(application)
        has_desktop_config = bool(desktop)
        has_desktop_page_schema = bool(page_schema)
        has_apps_catalog_modal = "apps_catalog" in modals
        has_widgets_catalog_modal = "widgets_catalog" in modals
        has_catalog_apps = isinstance(catalog.get("apps"), list)
        has_catalog_widgets = isinstance(catalog.get("widgets"), list)
        missing_branches: list[str] = []
        if not has_ui_application:
            missing_branches.append("ui.application")
        if not has_desktop_config:
            missing_branches.append("ui.application.desktop")
        if not has_desktop_page_schema:
            missing_branches.append("ui.application.desktop.pageSchema")
        if not has_apps_catalog_modal:
            missing_branches.append("ui.application.modals.apps_catalog")
        if not has_widgets_catalog_modal:
            missing_branches.append("ui.application.modals.widgets_catalog")
        if not has_catalog_apps:
            missing_branches.append("data.catalog.apps")
        if not has_catalog_widgets:
            missing_branches.append("data.catalog.widgets")
        ready = not missing_branches
        return {
            "ready": ready,
            "readiness_state": "ready" if ready else "pending_structure",
            "missing_branches": missing_branches,
            "webspace_id": target_webspace_id,
            "current_scenario": current_scenario,
            "has_ui_application": has_ui_application,
            "has_desktop_config": has_desktop_config,
            "has_desktop_page_schema": has_desktop_page_schema,
            "has_apps_catalog_modal": has_apps_catalog_modal,
            "has_widgets_catalog_modal": has_widgets_catalog_modal,
            "has_catalog_apps": has_catalog_apps,
            "has_catalog_widgets": has_catalog_widgets,
            "catalog_counts": {
                "apps": len(apps),
                "widgets": len(widgets),
            },
            "topbar_count": len(topbar),
            "page_widget_count": len(page_widgets),
            "snapshot_source": "live_yjs_runtime",
            "observed_at": time.time(),
            "stale": False,
        }
    except Exception:
        return None


def _with_live_yjs_materialization_snapshot(webspace_id: str | None, selected_webspace: dict[str, Any] | None) -> dict[str, Any]:
    selected = dict(selected_webspace or {})
    rebuild = dict(selected.get("rebuild") or {})
    cached = rebuild.get("materialization") if isinstance(rebuild.get("materialization"), dict) else {}
    if cached and bool(cached.get("ready")) and not bool(cached.get("stale")):
        return selected
    live = _live_yjs_materialization_snapshot(webspace_id)
    if not live:
        return selected
    if bool(live.get("ready")) or not cached:
        rebuild["materialization"] = live
        selected["rebuild"] = rebuild
        selected["live_materialization_observed"] = True
    return selected


def _build_yjs_recovery_policy(
    selected_entry: dict[str, Any] | None,
    selected_webspace: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    entry = selected_entry if isinstance(selected_entry, dict) else {}
    snapshot_exists = bool(entry.get("snapshot_file_exists")) if entry else False
    selected_update_entries = int(entry.get("update_log_entries") or 0) if entry else 0
    selected_replay_entries = int(entry.get("replay_window_entries") or 0) if entry else 0
    selected_replay_limit = int(entry.get("replay_window_limit") or 0) if entry else 0
    selected_backup_total = int(entry.get("backup_total") or 0) if entry else 0
    selected_log_mode = str(entry.get("log_mode") or "") if entry else ""
    selected_ws = selected_webspace if isinstance(selected_webspace, dict) else {}
    projection_matches_home = selected_ws.get("projection_matches_home")
    home_scenario = str(selected_ws.get("home_scenario") or "").strip() or None
    projection_target_space = str(selected_ws.get("projection_target_space") or "").strip() or None
    projection_active_scenario = str(selected_ws.get("projection_active_scenario") or "").strip() or None
    projection_active_space = str(selected_ws.get("projection_active_space") or "").strip() or None
    projection_candidate_matches_target = bool(projection_active_scenario) and (
        not projection_target_space or not projection_active_space or projection_active_space == projection_target_space
    )
    set_home_current_enabled = (
        projection_candidate_matches_target
        and bool(projection_active_scenario)
        and projection_active_scenario != home_scenario
    )
    backup_first = selected_update_entries > 0 and (
        not snapshot_exists or selected_backup_total <= 0 or selected_replay_entries > 0
    )
    action_overrides = {
        "backup": {
            "enabled": True,
            "source_of_truth": "current_runtime",
            "reason": "persist current in-memory Yjs state to disk snapshot",
        },
        "reload": {
            "enabled": True,
            "source_of_truth": "scenario",
            "reason": "reseed the selected webspace from its scenario source",
        },
        "reset": {
            "enabled": True,
            "source_of_truth": "scenario",
            "reason": "hard-reset the selected webspace from its scenario source",
        },
        "restore": {
            "enabled": snapshot_exists,
            "source_of_truth": "snapshot",
            "reason": (
                "restore the selected webspace from its last persisted disk snapshot"
                if snapshot_exists
                else "disk snapshot is missing for the selected webspace"
            ),
        },
        "go_home": {
            "enabled": bool(home_scenario) and projection_matches_home is not True,
            "source_of_truth": "manifest_home_scenario",
            "reason": (
                "switch the selected webspace back to its persisted home scenario"
                if bool(home_scenario) and projection_matches_home is not True
                else "selected webspace already aligns with its manifest home scenario"
                if projection_matches_home is True
                else "selected webspace has no persisted home scenario"
            ),
        },
        "set_home_current": {
            "enabled": set_home_current_enabled,
            "source_of_truth": "current_projection",
            "scenario_id": projection_active_scenario,
            "reason": (
                "persist the current projected scenario as the new home scenario"
                if set_home_current_enabled
                else "current projected scenario already matches the persisted home scenario"
                if projection_active_scenario and projection_active_scenario == home_scenario
                else "current projected scenario is unavailable for this webspace"
                if not projection_active_scenario
                else "current projected scenario does not match this webspace target space"
            ),
        },
    }
    recovery_order: list[str] = []
    if backup_first:
        recovery_order.append("backup")
    recovery_order.append("reload")
    if snapshot_exists:
        recovery_order.append("restore")
    recovery_order.append("reset")
    warnings: list[str] = []
    if selected_replay_limit > 0 and selected_replay_entries >= max(1, int(selected_replay_limit * 0.9)):
        warnings.append("bounded replay window is near its limit")
    if selected_update_entries > 0 and not snapshot_exists:
        warnings.append("restore is unavailable until a disk snapshot exists")
    if selected_log_mode == "append_only" and selected_update_entries > 0:
        warnings.append("current state only lives in the append log; preserve it before destructive recovery")
    if backup_first:
        recommended_action = "backup"
        recommended_reason = "persist the current in-memory state before reload, restore, or reset"
    else:
        recommended_action = "reload"
        recommended_reason = "scenario remains the canonical source for routine Yjs reseed and recovery"
    recovery_guidance = {
        "backup_first": backup_first,
        "recommended_action": recommended_action,
        "recommended_reason": recommended_reason,
        "risk_level": "warn" if warnings else "ok",
        "warnings": warnings,
        "operator_summary": (
            f"{recommended_action} first; then "
            + " -> ".join(step for step in recovery_order[1:])
            if recovery_order and recovery_order[0] == recommended_action and len(recovery_order) > 1
            else f"{recommended_action} first"
        ),
    }
    recovery_playbook = {
        "default_action": "reload",
        "default_reason": "scenario is the canonical source for routine webspace reseed and recovery",
        "escalation_action": "restore" if snapshot_exists else None,
        "escalation_reason": (
            "use the last persisted disk snapshot when scenario reseed would discard wanted collaborative state"
            if snapshot_exists
            else "disk snapshot is unavailable, so recovery escalation skips restore"
        ),
        "last_resort_action": "reset",
        "last_resort_reason": "hard-reset the webspace only when scenario reload or snapshot restore are unsuitable",
        "action_order": recovery_order,
    }
    return action_overrides, recovery_playbook, recovery_guidance


def _build_yjs_webspace_guidance(
    selected_webspace: dict[str, Any] | None,
    action_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = selected_webspace if isinstance(selected_webspace, dict) else {}
    overrides = action_overrides if isinstance(action_overrides, dict) else {}
    projection_matches_home = selected.get("projection_matches_home")
    rebuild = selected.get("rebuild") if isinstance(selected.get("rebuild"), dict) else {}
    home_scenario = str(selected.get("home_scenario") or "").strip() or None
    projection_active_scenario = str(selected.get("projection_active_scenario") or "").strip() or None
    rebuild_status = str(rebuild.get("status") or "").strip() or None
    set_home_current_override = (
        overrides.get("set_home_current") if isinstance(overrides.get("set_home_current"), dict) else {}
    )
    warnings: list[str] = []
    recommended_action: str | None = None
    recommended_reason: str | None = None
    alternate_action: str | None = None
    alternate_reason: str | None = None
    if rebuild_status in {"running", "failed"}:
        warnings.append(f"webspace rebuild state is {rebuild_status}")
    if projection_matches_home is False and home_scenario:
        recommended_action = "go_home"
        recommended_reason = "projection target diverges from the persisted home scenario"
        if bool(set_home_current_override.get("enabled")):
            alternate_action = "set_home_current"
            alternate_reason = str(set_home_current_override.get("reason") or "").strip() or None
    elif not home_scenario and bool(set_home_current_override.get("enabled")):
        recommended_action = "set_home_current"
        recommended_reason = "selected webspace has no persisted home scenario yet"
    risk_level = "warn" if warnings or recommended_action else "ok"
    if recommended_action == "go_home":
        operator_summary = "go_home to return the webspace to home scenario"
        if alternate_action == "set_home_current":
            operator_summary += "; or set_home_current to adopt the current projection as home"
    elif recommended_action == "set_home_current":
        operator_summary = "set_home_current to persist the current projection as the webspace home scenario"
    elif projection_matches_home is True:
        operator_summary = "webspace projection already follows its persisted home scenario"
    else:
        operator_summary = "webspace scenario guidance unavailable"
    return {
        "recommended_action": recommended_action,
        "recommended_reason": recommended_reason,
        "alternate_action": alternate_action,
        "alternate_reason": alternate_reason,
        "projection_active_scenario": projection_active_scenario,
        "risk_level": risk_level,
        "warnings": warnings,
        "operator_summary": operator_summary,
    }


def media_plane_runtime_snapshot(
    *,
    role: str,
    route_mode: str | None,
    connected_to_hub: bool | None,
) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    try:
        from adaos.services.media_library import media_runtime_snapshot as _media_runtime_snapshot
    except Exception:
        return {
            "available": False,
            "assessment": {
                "state": "unavailable",
                "reason": "media runtime module is unavailable",
            },
            "transport": {
                "role": role_norm or None,
                "route_mode": str(route_mode or "").strip() or None,
                "connected_to_subnet": _connected_to_subnet_alias(connected_to_hub),
                "connected_to_hub": connected_to_hub,
                "control_readiness_impact": "none",
            },
        }

    runtime = _media_runtime_snapshot()
    paths = runtime.get("paths") if isinstance(runtime.get("paths"), dict) else {}
    direct_local = paths.get("direct_local_http") if isinstance(paths.get("direct_local_http"), dict) else {}
    root_routed = paths.get("root_routed_http") if isinstance(paths.get("root_routed_http"), dict) else {}
    webrtc_tracks = paths.get("webrtc_tracks") if isinstance(paths.get("webrtc_tracks"), dict) else {}
    runtime["transport"] = {
        "role": role_norm or None,
        "route_mode": str(route_mode or "").strip() or None,
        "connected_to_subnet": _connected_to_subnet_alias(connected_to_hub),
        "connected_to_hub": connected_to_hub,
        "control_readiness_impact": "none",
        "hub_member_semantic_state": "isolated_from_phase4_control_and_sync",
        "direct_local_ready": bool(direct_local.get("ready")),
        "root_routed_ready": bool(root_routed.get("ready")),
        "broadcast_ready": bool(webrtc_tracks.get("ready")),
    }
    runtime["update_guard"] = _media_update_guard_snapshot(role=role_norm, runtime=runtime)
    return runtime


def _runtime_snapshot_timeout_sec() -> float:
    try:
        return max(0.1, float(str(os.getenv("ADAOS_RELIABILITY_RUNTIME_SECTION_TIMEOUT_SEC") or "1.5").strip()))
    except Exception:
        return 1.5


def _run_bounded_runtime_section(
    *,
    section: str,
    fn,
    timeout_sec: float,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    result_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            payload = fn()
            if not isinstance(payload, dict):
                payload = {
                    **fallback,
                    "available": False,
                    "assessment": {
                        "state": "unavailable",
                        "reason": f"{section} runtime returned non-dict payload",
                    },
                }
            result_q.put(payload)
        except Exception as exc:
            result_q.put(
                {
                    **fallback,
                    "available": False,
                    "assessment": {
                        "state": "unavailable",
                        "reason": f"{section} runtime failed: {type(exc).__name__}: {exc}",
                    },
                    "_timed_out": False,
                    "_section": section,
                }
            )

    thread = threading.Thread(target=_worker, name=f"adaos-reliability-{section}", daemon=True)
    thread.start()
    thread.join(max(0.1, float(timeout_sec)))
    if thread.is_alive():
        return {
            **fallback,
            "available": False,
            "assessment": {
                "state": "degraded",
                "reason": f"{section} runtime timed out after {round(float(timeout_sec), 3)}s",
            },
            "_timed_out": True,
            "_section": section,
        }
    try:
        payload = result_q.get_nowait()
    except Exception:
        payload = {
            **fallback,
            "available": False,
            "assessment": {
                "state": "unavailable",
                "reason": f"{section} runtime returned no payload",
            },
            "_timed_out": False,
            "_section": section,
        }
    return payload if isinstance(payload, dict) else dict(fallback)


def reliability_snapshot(
    *,
    node_id: str,
    subnet_id: str,
    role: str,
    zone_id: str | None = None,
    local_ready: bool,
    node_state: str,
    draining: bool,
    route_mode: str | None,
    connected_to_hub: bool | None,
    node_names: list[str] | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    zone_id = (
        str(zone_id or "").strip().lower()
        or str(os.getenv("ADAOS_ZONE_ID", "") or "").strip().lower()
        or None
    )
    channel_diagnostics = channel_diagnostics_snapshot()
    transport_strategy = hub_root_transport_strategy_snapshot()
    selected_server = str(transport_strategy.get("selected_server") or "").strip()
    active_zone_id = None
    if selected_server:
        lower = selected_server.lower()
        if "://ru.inimatic.com" in lower or lower.endswith("ru.inimatic.com"):
            active_zone_id = "ru"
        elif "://api.inimatic.com" in lower or lower.endswith("api.inimatic.com"):
            active_zone_id = canonical_zone_id(zone_id) if canonical_zone_id(zone_id) in {"us", "eu", "in", "ch"} else "us"
    hub_root_protocol = hub_root_protocol_snapshot()
    hub_member_channels = hub_member_semantic_channels_snapshot(
        role=role,
        route_mode=route_mode,
        connected_to_hub=connected_to_hub,
        hub_root_protocol=hub_root_protocol,
    )
    hub_member_connection_state = hub_member_connection_state_snapshot(
        role=role,
        route_mode=route_mode,
        connected_to_hub=connected_to_hub,
        node_id=node_id,
        node_names=node_names,
    )
    readiness_tree = build_readiness_tree(
        role=role,
        local_ready=local_ready,
        node_state=node_state,
        draining=draining,
        connected_to_hub=connected_to_hub,
        channel_diagnostics=channel_diagnostics,
        hub_member_channels=hub_member_channels,
        hub_member_connection_state=hub_member_connection_state,
    )
    degraded_matrix = build_degraded_matrix(role=role, readiness_tree=readiness_tree)
    channel_overview = channel_overview_snapshot(
        readiness_tree=readiness_tree,
        channel_diagnostics=channel_diagnostics,
        transport_strategy=transport_strategy,
    )
    section_timeout = _runtime_snapshot_timeout_sec()
    sync_runtime = _run_bounded_runtime_section(
        section="sync",
        timeout_sec=section_timeout,
        fn=lambda: yjs_sync_runtime_snapshot(role=role, webspace_id=webspace_id),
        fallback={
            "available": False,
            "scope": "hub_local_only",
            "selected_webspace_id": str(webspace_id or "").strip() or None,
            "channel_contract": _build_yjs_sync_channel_contract(),
            "transport": {},
            "ownership_boundaries": {},
            "action_overrides": {},
            "recovery_playbook": {},
            "recovery_guidance": {},
            "selected_webspace": {},
            "webspace_guidance": {},
            "webspace_total": 0,
            "active_webspace_total": 0,
            "webspaces": {},
        },
    )
    media_runtime = _run_bounded_runtime_section(
        section="media",
        timeout_sec=section_timeout,
        fn=lambda: media_plane_runtime_snapshot(
            role=role,
            route_mode=route_mode,
            connected_to_hub=connected_to_hub,
        ),
        fallback={
            "available": False,
            "transport": {
                "role": str(role or "").strip().lower() or None,
                "route_mode": str(route_mode or "").strip() or None,
                "connected_to_subnet": _connected_to_subnet_alias(connected_to_hub),
                "connected_to_hub": connected_to_hub,
                "control_readiness_impact": "none",
            },
        },
    )
    supervisor_runtime = _run_bounded_runtime_section(
        section="supervisor",
        timeout_sec=section_timeout,
        fn=lambda: supervisor_transition_runtime_snapshot(
            timeout_sec=min(0.35, max(0.1, section_timeout / 2.0))
        ),
        fallback={
            "available": False,
            "status": {},
            "attempt": {},
            "runtime": {},
            "browser_safe_surface": {
                "state": "unavailable",
                "ready": False,
                "carried_by_reliability": True,
                "transition_mode_visible": False,
                "candidate_runtime_visible": False,
                "warm_switch_visible": False,
                "blockers": ["supervisor.runtime.unavailable"],
            },
        },
    )
    sidecar_runtime = sidecar_runtime_snapshot(
        role=role,
        readiness_tree=readiness_tree,
        hub_root_protocol=hub_root_protocol,
        transport_strategy=transport_strategy,
        media_runtime=media_runtime,
    )
    if isinstance(supervisor_runtime, dict):
        supervisor_runtime["required_upstream_link"] = _enrich_required_upstream_link_with_sidecar(
            required_upstream_link=(
                supervisor_runtime.get("required_upstream_link")
                if isinstance(supervisor_runtime.get("required_upstream_link"), dict)
                else {}
            ),
            sidecar_runtime=sidecar_runtime,
        )
    connectivity = _connectivity_snapshot(
        node_id=node_id,
        channel_overview=channel_overview,
        supervisor_runtime=supervisor_runtime,
    )
    state_sync = _state_sync_snapshot(sync_runtime)
    yjs_pressure = _yjs_pressure_snapshot(sync_runtime)
    event_model_phase0_communication = _event_model_phase0_communication_checkpoint(
        sync_runtime=sync_runtime,
        sidecar_runtime=sidecar_runtime,
        hub_root_protocol=hub_root_protocol,
        supervisor_runtime=supervisor_runtime,
    )
    return {
        "ok": True,
        "node": {
            "node_id": node_id,
            "subnet_id": subnet_id,
            "zone_id": zone_id,
            "role": role,
            "ready": bool(local_ready and not draining),
            "node_state": node_state,
            "draining": bool(draining),
            "route_mode": route_mode,
            "connected_to_subnet": _connected_to_subnet_alias(connected_to_hub),
            "connected_to_hub": connected_to_hub,
            "node_names": list(node_names or []),
        },
        "model": reliability_model_snapshot(),
        "runtime": {
            "signals": runtime_signal_snapshot(),
            "readiness_tree": readiness_tree,
            "degraded_matrix": degraded_matrix,
            "channel_diagnostics": channel_diagnostics,
            "channel_overview": channel_overview,
            "hub_root_transport_strategy": transport_strategy,
            "hub_root_zone": {
                "configured_zone_id": zone_id,
                "active_zone_id": active_zone_id,
                "selected_server": selected_server or None,
            },
            "hub_root_protocol": hub_root_protocol,
            "hub_member_channels": hub_member_channels,
            "hub_member_connection_state": hub_member_connection_state,
            "sidecar_runtime": sidecar_runtime,
            "sync_runtime": sync_runtime,
            "connectivity": connectivity,
            "state_sync": state_sync,
            "yjs_pressure": yjs_pressure,
            "media_runtime": media_runtime,
            "supervisor_runtime": supervisor_runtime,
            "event_model_phase0_communication": event_model_phase0_communication,
        },
    }
