"""
Hub-side WebRTC peer connection management.

Each browser device that negotiates WebRTC gets a ``HubPeer`` instance holding
an ``RTCPeerConnection`` with two DataChannels:

* **events** – JSON commands (same protocol as the ``/ws`` endpoint)
* **yjs** – binary Yjs CRDT sync (same protocol as ``/yws``)

Signaling (SDP offer/answer + ICE candidates) flows through the existing
Events WebSocket which is already tunnelled via NATS.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable
from pathlib import Path

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
    from aiortc.contrib.media import MediaRelay
    from aiortc.sdp import candidate_from_sdp
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "aiortc is required for WebRTC support. "
        "Install via `pip install aiortc` or add it to pyproject.toml."
    ) from exc

from adaos.services.webrtc.yjs_adapter import DataChannelYjsAdapter
from adaos.services.media_library import (
    ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    guess_media_type,
    media_file_path,
)
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit

_log = logging.getLogger("adaos.webrtc.peer")
_media_relay = MediaRelay()
_REUSABLE_CONNECTION_STATES = {"new", "connecting", "connected"}
_TERMINAL_CONNECTION_STATES = {"failed", "closed", "disconnected"}
_LIVE_CHANNEL_STATES = {"connecting", "open"}
_STUCK_PEER_GRACE_SECONDS = 5.0
_REPLACE_CLOSE_TIMEOUT_SECONDS = 1.5

STUN_CONFIG = RTCConfiguration(
    iceServers=[
        RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
    ]
)

# Active peers keyed by device_id.
_peers: dict[str, HubPeer] = {}
_EVENT_CHANNEL_SUBSCRIPTIONS_LOCK = threading.RLock()
_EVENT_CHANNEL_SUBSCRIBERS: dict[int, dict[str, Any]] = {}
_EVENT_CHANNEL_FORWARDER_INSTALLED = False


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


def _build_event_channel_message(
    event_type: str,
    payload: Any,
    *,
    source: str = "webrtc.events",
    ts: float | None = None,
) -> dict[str, Any]:
    return {
        "ch": "events",
        "t": "evt",
        "kind": str(event_type or "").strip(),
        "payload": payload if isinstance(payload, dict) else {"value": payload},
        "source": str(source or "webrtc.events").strip() or "webrtc.events",
        "ts": float(ts or time.time()),
    }


def _ensure_event_channel_forwarder() -> None:
    global _EVENT_CHANNEL_FORWARDER_INSTALLED
    with _EVENT_CHANNEL_SUBSCRIPTIONS_LOCK:
        if _EVENT_CHANNEL_FORWARDER_INSTALLED:
            return
        get_ctx().bus.subscribe("*", _forward_event_channel_bus_event)
        _EVENT_CHANNEL_FORWARDER_INSTALLED = True


def _register_event_channel_subscriptions(
    peer: "HubPeer",
    loop: asyncio.AbstractEventLoop,
    raw_topics: Any,
) -> set[str]:
    if not isinstance(raw_topics, list):
        return set()
    topics = {
        topic
        for topic in (str(raw or "").strip() for raw in raw_topics)
        if topic
    }
    if not topics:
        return set()
    _ensure_event_channel_forwarder()
    with _EVENT_CHANNEL_SUBSCRIPTIONS_LOCK:
        entry = _EVENT_CHANNEL_SUBSCRIBERS.setdefault(
            id(peer),
            {
                "peer": peer,
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
            transport="webrtc_data:events",
            connection_id=str(id(peer)),
        )
    return added


def _unregister_event_channel_subscriptions(peer: "HubPeer") -> None:
    with _EVENT_CHANNEL_SUBSCRIPTIONS_LOCK:
        entry = _EVENT_CHANNEL_SUBSCRIBERS.pop(id(peer), None)
    topics = set(entry.get("topics") or []) if isinstance(entry, dict) else set()
    if topics:
        _publish_webio_stream_subscription_change(
            topics,
            action="unsubscribed",
            transport="webrtc_data:events",
            connection_id=str(id(peer)),
        )


def _unregister_event_channel_subscription_topics(peer: "HubPeer", raw_topics: Any) -> set[str]:
    if not isinstance(raw_topics, list):
        return set()
    topics = {
        topic
        for topic in (str(raw or "").strip() for raw in raw_topics)
        if topic
    }
    if not topics:
        return set()
    with _EVENT_CHANNEL_SUBSCRIPTIONS_LOCK:
        entry = _EVENT_CHANNEL_SUBSCRIBERS.get(id(peer))
        if not isinstance(entry, dict):
            return set()
        tracked = entry.setdefault("topics", set())
        removed = set(topics) & set(tracked)
        tracked.difference_update(removed)
        if not tracked:
            _EVENT_CHANNEL_SUBSCRIBERS.pop(id(peer), None)
    if removed:
        _publish_webio_stream_subscription_change(
            removed,
            action="unsubscribed",
            transport="webrtc_data:events",
            connection_id=str(id(peer)),
        )
    return removed


def _iter_initial_event_channel_messages(topics: set[str]) -> list[dict[str, Any]]:
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
                    _build_event_channel_message(
                        "node.status",
                        _current_node_status_push_payload(),
                        source="node.status",
                    )
                )
        except Exception:
            _log.debug("failed to snapshot node.status for WebRTC event subscriber", exc_info=True)
    if any(_ws_event_topic_matches(topic, "core.update.status") for topic in topics):
        try:
            from adaos.services.core_update import read_status as _read_core_update_status

            messages.append(
                _build_event_channel_message(
                    "core.update.status",
                    _read_core_update_status() or {},
                    source="core.update.status",
                )
            )
        except Exception:
            _log.debug("failed to snapshot core.update.status for WebRTC event subscriber", exc_info=True)
    if any(_ws_event_topic_matches(topic, "supervisor.update.status.raw") for topic in topics):
        try:
            from adaos.services.core_update import read_public_update_status as _read_public_update_status

            messages.append(
                _build_event_channel_message(
                    "supervisor.update.status.raw",
                    _read_public_update_status(),
                    source="supervisor.update.status.raw",
                )
            )
        except Exception:
            _log.debug("failed to snapshot supervisor.update.status.raw for WebRTC event subscriber", exc_info=True)
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
            webspace_id = "default"
            node_id = parts[1]
            receiver_parts = parts[2:]
        else:
            webspace_id = parts[0]
            receiver_parts = parts[1:]
        if len(receiver_parts) >= 3 and receiver_parts[0] == "nodes":
            node_id = receiver_parts[1]
            receiver_parts = receiver_parts[2:]
        receiver = ".".join(receiver_parts).strip()
        if not webspace_id or not receiver:
            continue
        try:
            ctx = get_ctx()
            payload = {
                "topic": token,
                "webspace_id": webspace_id,
                "receiver": receiver,
                "transport": str(transport or "webrtc_data:events"),
            }
            if node_id:
                payload["node_id"] = node_id
            bus_emit(
                ctx.bus,
                "webio.stream.snapshot.requested",
                payload,
                "webrtc.peer",
            )
        except Exception:
            _log.debug("failed to request webio stream snapshot topic=%s", token, exc_info=True)


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
            webspace_id = "default"
            node_id = parts[1]
            receiver_parts = parts[2:]
        else:
            webspace_id = parts[0]
            receiver_parts = parts[1:]
        if len(receiver_parts) >= 3 and receiver_parts[0] == "nodes":
            node_id = receiver_parts[1]
            receiver_parts = receiver_parts[2:]
        receiver = ".".join(receiver_parts).strip()
        if not webspace_id or not receiver:
            continue
        try:
            ctx = get_ctx()
            payload = {
                "topic": token,
                "webspace_id": webspace_id,
                "receiver": receiver,
                "transport": str(transport or "webrtc_data:events"),
                "action": str(action or "").strip() or "subscribed",
            }
            if connection_id:
                payload["connection_id"] = str(connection_id)
                payload["subscription_id"] = f"{transport}:{connection_id}:{token}"
            if node_id:
                payload["node_id"] = node_id
            bus_emit(
                ctx.bus,
                "webio.stream.subscription.changed",
                payload,
                "webrtc.peer",
            )
        except Exception:
            _log.debug("failed to publish webio stream subscription change topic=%s", token, exc_info=True)


def _forward_event_channel_bus_event(ev: Any) -> None:
    event_type = str(getattr(ev, "type", "") or "").strip()
    if not event_type:
        return
    with _EVENT_CHANNEL_SUBSCRIPTIONS_LOCK:
        subscribers = [
            dict(entry)
            for entry in _EVENT_CHANNEL_SUBSCRIBERS.values()
            if any(_ws_event_topic_matches(topic, event_type) for topic in entry.get("topics", set()))
        ]
    if not subscribers:
        return
    message = _build_event_channel_message(
        event_type,
        getattr(ev, "payload", {}) or {},
        source=str(getattr(ev, "source", "") or "webrtc.events"),
        ts=float(getattr(ev, "ts", 0.0) or time.time()),
    )
    for entry in subscribers:
        peer = entry.get("peer")
        loop = entry.get("loop")
        if peer is None or not isinstance(loop, asyncio.AbstractEventLoop):
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                peer._send_event_channel_message(message),
                loop,
            )
        except Exception:
            _unregister_event_channel_subscriptions(peer)


class HubPeer:
    """Manages a single WebRTC peer connection from a browser device."""

    def __init__(
        self,
        device_id: str,
        webspace_id: str,
        send_ice_cb: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.device_id = device_id
        self.webspace_id = webspace_id
        self._send_ice = send_ice_cb

        self.pc = RTCPeerConnection(configuration=STUN_CONFIG)
        self._yjs_adapter: DataChannelYjsAdapter | None = None
        self._yjs_task: asyncio.Task[None] | None = None
        self._local_desc_task: asyncio.Task[None] | None = None
        self._events_channel: Any | None = None
        self._yjs_channel: Any | None = None
        self._media_channel: Any | None = None
        self._incoming_tracks: dict[str, dict[str, Any]] = {}
        self._loopback_tracks: dict[str, dict[str, Any]] = {}
        self._media_upload: dict[str, Any] | None = None
        self._offer_lock = asyncio.Lock()
        self._scheduled_close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._created_at = time.time()
        self._last_activity_at = self._created_at
        self._last_state_change_at = self._created_at

        # Browser creates the DataChannels – hub receives them here.
        @self.pc.on("datachannel")
        def on_datachannel(channel) -> None:  # type: ignore[no-untyped-def]
            self._touch()
            _log.info("datachannel opened: label=%s device=%s", channel.label, self.device_id)
            if channel.label == "events":
                self._setup_events_channel(channel)
            elif channel.label == "yjs":
                self._setup_yjs_channel(channel)
            elif channel.label == "media":
                self._setup_media_channel(channel)
            else:
                _log.warning("unknown datachannel label=%s", channel.label)

        @self.pc.on("icecandidate")
        def on_ice(candidate) -> None:  # type: ignore[no-untyped-def]
            if candidate is None:
                return
            asyncio.ensure_future(self._send_ice({
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            }))

        @self.pc.on("connectionstatechange")
        def on_state() -> None:  # type: ignore[no-untyped-def]
            state = self._connection_state()
            self._touch()
            self._last_state_change_at = time.time()
            _log.info("peer %s connectionState=%s", self.device_id, state)
            self._emit_state_event(reason=f"connection_state:{state}")
            if state == "connected":
                self._cancel_scheduled_close()
                return
            if state in _TERMINAL_CONNECTION_STATES:
                delay = 0.0 if state in {"failed", "closed"} else 1.0
                self._schedule_close(reason=f"connection_state:{state}", delay=delay)

        @self.pc.on("track")
        def on_track(track) -> None:  # type: ignore[no-untyped-def]
            self._touch()
            track_id = str(getattr(track, "id", "") or f"{track.kind}:{len(self._incoming_tracks) + 1}")
            now = time.time()
            self._incoming_tracks[track_id] = {
                "id": track_id,
                "kind": str(getattr(track, "kind", "") or "unknown"),
                "ready_state": str(getattr(track, "readyState", "") or "live"),
                "received_at": now,
                "ended_at": None,
                "loopback": False,
            }
            _log.info(
                "media track received: kind=%s id=%s device=%s",
                getattr(track, "kind", "unknown"),
                track_id,
                self.device_id,
            )
            try:
                loopback_track = _media_relay.subscribe(track)
                sender = self.pc.addTrack(loopback_track)
                self._loopback_tracks[track_id] = {
                    "id": track_id,
                    "kind": str(getattr(track, "kind", "") or "unknown"),
                    "added_at": now,
                    "sender": sender,
                    "sender_kind": str(getattr(getattr(sender, "track", None), "kind", "") or getattr(track, "kind", "unknown")),
                }
                self._incoming_tracks[track_id]["loopback"] = True
            except Exception:
                _log.warning(
                    "failed to attach loopback media track device=%s id=%s",
                    self.device_id,
                    track_id,
                    exc_info=True,
                )
            self._emit_state_event(reason=f"track:{getattr(track, 'kind', 'unknown')}:received")

            @track.on("ended")
            async def on_track_ended() -> None:  # type: ignore[no-untyped-def]
                rec = self._incoming_tracks.get(track_id)
                if rec is not None:
                    rec["ready_state"] = "ended"
                    rec["ended_at"] = time.time()
                self._touch()
                loopback = self._loopback_tracks.pop(track_id, None)
                await self._detach_loopback_sender(loopback)
                _log.info(
                    "media track ended: kind=%s id=%s device=%s",
                    getattr(track, "kind", "unknown"),
                    track_id,
                    self.device_id,
                )
                self._emit_state_event(reason=f"track:{getattr(track, 'kind', 'unknown')}:ended")
                self._touch()
                self._schedule_close_if_orphaned(reason=f"track:{getattr(track, 'kind', 'unknown')}:ended")

    def _touch(self) -> None:
        self._last_activity_at = time.time()

    def _connection_state(self) -> str:
        try:
            return str(getattr(self.pc, "connectionState", "") or "unknown").strip().lower() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _channel_state(channel: Any | None) -> str:
        try:
            return str(getattr(channel, "readyState", "") or "missing").strip().lower() or "missing"
        except Exception:
            return "missing"

    def _events_state(self) -> str:
        return self._channel_state(self._events_channel)

    def _yjs_state(self) -> str:
        return self._channel_state(self._yjs_channel)

    def _media_state(self) -> str:
        return self._channel_state(self._media_channel)

    def _has_live_channels_or_tracks(self) -> bool:
        if any(
            state in _LIVE_CHANNEL_STATES
            for state in (self._events_state(), self._yjs_state(), self._media_state())
        ):
            return True
        return bool(self._incoming_tracks or self._loopback_tracks)

    def _has_bound_transport(self) -> bool:
        if self._events_channel is not None or self._yjs_channel is not None or self._media_channel is not None:
            return True
        if self._yjs_adapter is not None:
            return True
        task = self._yjs_task
        return bool(task is not None and not task.done())

    @staticmethod
    def _close_data_channel(channel: Any | None) -> None:
        if channel is None:
            return
        try:
            close = getattr(channel, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
        except Exception:
            _log.debug("datachannel close failed", exc_info=True)

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None, *, label: str) -> None:
        if task is None or task.done():
            return
        task.cancel()

        async def _await_cancel() -> None:
            with suppress(asyncio.CancelledError, Exception):
                await task

        try:
            asyncio.create_task(_await_cancel(), name=label)
        except Exception:
            pass

    def _cancel_scheduled_close(self) -> None:
        task = self._scheduled_close_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        self._scheduled_close_task = None

    def is_reusable_for_offer(self) -> bool:
        if self._closed:
            return False
        if self._connection_state() not in _REUSABLE_CONNECTION_STATES:
            return False
        task = self._scheduled_close_task
        if task and not task.done():
            return False
        if self._has_live_channels_or_tracks():
            return False
        if self._has_bound_transport():
            return False
        return True

    def is_stale(self, *, now_ts: float | None = None) -> bool:
        if self._closed:
            return True
        state = self._connection_state()
        if state in _TERMINAL_CONNECTION_STATES:
            return True
        if self._has_live_channels_or_tracks():
            return False
        now = time.time() if now_ts is None else float(now_ts)
        last_seen = max(self._created_at, self._last_activity_at, self._last_state_change_at)
        return (now - last_seen) >= _STUCK_PEER_GRACE_SECONDS

    def _schedule_close(self, *, reason: str, delay: float = 0.0) -> bool:
        if self._closed:
            return False
        task = self._scheduled_close_task
        if task is not None and not task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        async def _runner() -> None:
            try:
                if delay > 0.0:
                    await asyncio.sleep(delay)
                if not self.is_stale():
                    return
                await self.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.debug("scheduled peer close failed device=%s reason=%s", self.device_id, reason, exc_info=True)
            finally:
                try:
                    current = asyncio.current_task()
                except RuntimeError:
                    current = None
                if self._scheduled_close_task is current:
                    self._scheduled_close_task = None

        self._scheduled_close_task = loop.create_task(
            _runner(),
            name=f"webrtc-peer-close:{self.device_id}:{reason}",
        )
        return True

    def _schedule_close_if_orphaned(self, *, reason: str, delay: float = 1.0) -> bool:
        if self._has_live_channels_or_tracks():
            return False
        if self._connection_state() == "connected":
            return False
        return self._schedule_close(reason=reason, delay=delay)

    # -- DataChannel handlers -------------------------------------------------

    def _setup_events_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *events* DataChannel to the same command processing as ``/ws``."""
        from adaos.services.yjs.gateway_ws import process_events_command

        previous_channel = self._events_channel
        if previous_channel is not None and previous_channel is not channel:
            self._close_data_channel(previous_channel)
        self._events_channel = channel
        self._touch()
        state = {"webspace_id": self.webspace_id}
        self._emit_state_event(reason="events_channel:open")

        async def _send(msg: dict[str, Any]) -> None:
            await self._send_event_channel_message(msg)

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            self._touch()
            text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
            try:
                msg = json.loads(text)
            except Exception:
                return
            if msg.get("type") == "subscribe":
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is None:
                    return
                added = _register_event_channel_subscriptions(self, loop, msg.get("topics"))
                if added:
                    async def _send_initial() -> None:
                        for item in _iter_initial_event_channel_messages(added):
                            await self._send_event_channel_message(item)

                    asyncio.ensure_future(_send_initial())
                    _request_webio_stream_snapshots(added, transport="webrtc_data:events")
                return
            if msg.get("type") == "unsubscribe":
                _unregister_event_channel_subscription_topics(self, msg.get("topics"))
                return
            ch = msg.get("ch")
            t = msg.get("t")
            if ch != "events" or t != "cmd":
                return
            cmd_id = msg.get("id", "")
            kind = msg.get("kind", "")
            payload = msg.get("payload") or {}

            async def _handle() -> None:
                new_ws = await process_events_command(
                    kind=kind,
                    cmd_id=cmd_id,
                    payload=payload,
                    device_id=self.device_id,
                    webspace_id=state["webspace_id"],
                    client_label=f"webrtc:{self.device_id}",
                    send_response=_send,
                )
                if new_ws:
                    state["webspace_id"] = new_ws
                    if self._yjs_channel is None:
                        self.webspace_id = new_ws

            asyncio.ensure_future(_handle())

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._touch()
            _unregister_event_channel_subscriptions(self)
            self._events_channel = None
            self._emit_state_event(reason="events_channel:closed")
            self._schedule_close_if_orphaned(reason="events_channel:closed")

    async def _send_event_channel_message(self, msg: dict[str, Any]) -> None:
        channel = self._events_channel
        if channel is None or str(getattr(channel, "readyState", "") or "").strip().lower() != "open":
            _unregister_event_channel_subscriptions(self)
            return
        try:
            channel.send(json.dumps(msg))
        except Exception:
            _unregister_event_channel_subscriptions(self)
            _log.warning("events dc send failed device=%s", self.device_id, exc_info=True)

    def _setup_yjs_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *yjs* DataChannel to ``ypy-websocket``."""
        previous_channel = self._yjs_channel
        previous_adapter = self._yjs_adapter
        previous_task = self._yjs_task
        if previous_adapter is not None:
            try:
                previous_adapter.close()
            except Exception:
                _log.debug("previous yjs adapter close failed device=%s", self.device_id, exc_info=True)
        if previous_task is not None:
            self._cancel_task(previous_task, label=f"webrtc-yjs-replaced:{self.device_id}")
        if previous_channel is not None and previous_channel is not channel:
            self._close_data_channel(previous_channel)
        self._yjs_channel = channel
        self._touch()
        self._yjs_adapter = DataChannelYjsAdapter(channel, self.webspace_id)
        self._yjs_task = asyncio.ensure_future(
            self._yjs_adapter.serve(),
            # name kwarg is py3.11+ for asyncio.ensure_future but Task() accepts it.
        )
        self._yjs_task.add_done_callback(
            lambda _t: _log.debug("yjs dc task done device=%s", self.device_id)
        )
        self._emit_state_event(reason="yjs_channel:open")

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._touch()
            self._yjs_channel = None
            self._emit_state_event(reason="yjs_channel:closed")
            self._schedule_close_if_orphaned(reason="yjs_channel:closed")

    def _setup_media_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Accept direct binary media upload chunks over a dedicated DataChannel."""
        previous_channel = self._media_channel
        if previous_channel is not None and previous_channel is not channel:
            self._cleanup_media_upload(remove_temp=True)
            self._close_data_channel(previous_channel)
        self._media_channel = channel
        self._touch()

        async def _send(msg: dict[str, Any]) -> None:
            try:
                channel.send(json.dumps(msg))
            except Exception:
                _log.warning("media dc send failed device=%s", self.device_id, exc_info=True)

        async def _fail(upload_id: str, detail: str, *, code: str = "media_upload_failed") -> None:
            await _send({
                "ch": "media",
                "t": "error",
                "uploadId": upload_id,
                "error": code,
                "detail": detail,
            })

        async def _handle_json(msg: dict[str, Any]) -> None:
            upload_id = str(msg.get("uploadId") or "").strip()
            if not upload_id:
                return
            kind = str(msg.get("t") or "").strip().lower()
            if kind == "start":
                if self._media_upload is not None:
                    await _fail(upload_id, "media_upload_busy", code="media_upload_busy")
                    return
                try:
                    target = media_file_path(str(msg.get("filename") or ""))
                except ValueError as exc:
                    await _fail(upload_id, str(exc), code="media_upload_bad_request")
                    return
                expected_size = max(0, int(msg.get("sizeBytes") or 0))
                if expected_size > int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES):
                    await _fail(upload_id, "media_upload_too_large", code="media_upload_too_large")
                    return
                tmp_path = target.with_name(
                    f"{target.name}.p2p-{int(time.time() * 1000)}.part"
                )
                try:
                    handle = tmp_path.open("wb")
                except Exception as exc:
                    await _fail(upload_id, str(exc), code="media_upload_open_failed")
                    return
                self._media_upload = {
                    "upload_id": upload_id,
                    "target": target,
                    "tmp_path": tmp_path,
                    "handle": handle,
                    "size_bytes": 0,
                    "expected_size": expected_size,
                    "replaced": target.exists(),
                    "mime_type": guess_media_type(target.name),
                }
                await _send({
                    "ch": "media",
                    "t": "progress",
                    "uploadId": upload_id,
                    "receivedBytes": 0,
                })
                return
            if kind == "end":
                upload = self._media_upload
                if not upload or str(upload.get("upload_id") or "") != upload_id:
                    await _fail(upload_id, "media_upload_missing_session", code="media_upload_missing_session")
                    return
                try:
                    handle = upload.get("handle")
                    if handle:
                        handle.close()
                    target = Path(upload["target"])
                    tmp_path = Path(upload["tmp_path"])
                    tmp_path.replace(target)
                    size_bytes = int(upload.get("size_bytes") or 0)
                    mime_type = str(upload.get("mime_type") or guess_media_type(target.name))
                    self._cleanup_media_upload(remove_temp=False)
                    await _send({
                        "ch": "media",
                        "t": "done",
                        "uploadId": upload_id,
                        "sizeBytes": size_bytes,
                        "mimeType": mime_type,
                    })
                except Exception as exc:
                    self._cleanup_media_upload(remove_temp=True)
                    await _fail(upload_id, str(exc), code="media_upload_finalize_failed")
                return
            if kind == "abort":
                upload = self._media_upload
                if upload and str(upload.get("upload_id") or "") == upload_id:
                    self._cleanup_media_upload(remove_temp=True)
                return

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            self._touch()
            async def _handle() -> None:
                if isinstance(data, str):
                    try:
                        msg = json.loads(data)
                    except Exception:
                        return
                    await _handle_json(msg if isinstance(msg, dict) else {})
                    return
                upload = self._media_upload
                if not upload:
                    return
                try:
                    blob = bytes(data)
                    size_bytes = int(upload.get("size_bytes") or 0) + len(blob)
                    if size_bytes > int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES):
                        upload_id = str(upload.get("upload_id") or "")
                        self._cleanup_media_upload(remove_temp=True)
                        await _fail(upload_id, "media_upload_too_large", code="media_upload_too_large")
                        return
                    handle = upload.get("handle")
                    if not handle:
                        upload_id = str(upload.get("upload_id") or "")
                        self._cleanup_media_upload(remove_temp=True)
                        await _fail(upload_id, "media_upload_no_handle", code="media_upload_no_handle")
                        return
                    handle.write(blob)
                    upload["size_bytes"] = size_bytes
                    await _send({
                        "ch": "media",
                        "t": "progress",
                        "uploadId": str(upload.get("upload_id") or ""),
                        "receivedBytes": size_bytes,
                    })
                except Exception as exc:
                    upload_id = str(upload.get("upload_id") or "")
                    self._cleanup_media_upload(remove_temp=True)
                    await _fail(upload_id, str(exc), code="media_upload_write_failed")

            asyncio.ensure_future(_handle())

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._touch()
            self._cleanup_media_upload(remove_temp=True)
            self._media_channel = None
            self._schedule_close_if_orphaned(reason="media_channel:closed")

    def _cleanup_media_upload(self, *, remove_temp: bool) -> None:
        upload = self._media_upload
        self._media_upload = None
        if not upload:
            return
        handle = upload.get("handle")
        try:
            if handle:
                handle.close()
        except Exception:
            pass
        if remove_temp:
            try:
                Path(upload["tmp_path"]).unlink(missing_ok=True)
            except Exception:
                pass

    async def _detach_loopback_sender(self, loopback: dict[str, Any] | None) -> None:
        if not isinstance(loopback, dict):
            return
        sender = loopback.get("sender")
        if sender is None:
            return
        try:
            remove_track = getattr(self.pc, "removeTrack", None)
            if callable(remove_track):
                result = remove_track(sender)
                if asyncio.iscoroutine(result):
                    await result
                return
        except Exception:
            _log.debug("removeTrack failed device=%s", self.device_id, exc_info=True)
        try:
            replace_track = getattr(sender, "replaceTrack", None)
            if callable(replace_track):
                result = replace_track(None)
                if asyncio.iscoroutine(result):
                    await result
                return
        except Exception:
            _log.debug("replaceTrack(None) failed device=%s", self.device_id, exc_info=True)
        try:
            stop_sender = getattr(sender, "stop", None)
            if callable(stop_sender):
                result = stop_sender()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            _log.debug("sender stop failed device=%s", self.device_id, exc_info=True)

    # -- SDP / ICE ------------------------------------------------------------

    async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
        async with self._offer_lock:
            self._touch()
            await self._cancel_local_desc_task()
            offer = RTCSessionDescription(sdp=sdp, type=type)
            await self.pc.setRemoteDescription(offer)
            answer = await self.pc.createAnswer()
            self._local_desc_task = asyncio.ensure_future(
                self._set_local_description(answer)
            )
        # Run setLocalDescription in background — avoids blocking on STUN
        # resolution (2-5 s).  ICE candidates trickle via the on_ice callback.
        return {
            "sdp": answer.sdp,
            "type": answer.type,
        }

    async def _set_local_description(self, answer: RTCSessionDescription) -> None:
        try:
            await self.pc.setLocalDescription(answer)
        except Exception:
            _log.warning("setLocalDescription failed device=%s", self.device_id, exc_info=True)

    async def _cancel_local_desc_task(self) -> None:
        task = self._local_desc_task
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.debug("previous local description task failed device=%s", self.device_id, exc_info=True)
        finally:
            if self._local_desc_task is task:
                self._local_desc_task = None

    async def add_ice_candidate(self, candidate_dict: dict[str, Any]) -> None:
        if not candidate_dict:
            return
        self._touch()
        # Parse ICE candidate from SDP string format
        sdp_line = candidate_dict.get("candidate", "")
        if not sdp_line:
            return
        candidate = candidate_from_sdp(sdp_line)
        candidate.sdpMid = candidate_dict.get("sdpMid")
        candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
        await self.pc.addIceCandidate(candidate)

    def snapshot_record(self) -> dict[str, Any]:
        connection_state = self._connection_state()
        events_state = self._events_state()
        yjs_state = self._yjs_state()
        incoming = self._incoming_tracks if isinstance(getattr(self, "_incoming_tracks", None), dict) else {}
        loopback = self._loopback_tracks if isinstance(getattr(self, "_loopback_tracks", None), dict) else {}
        incoming_audio_tracks = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "audio"
            and str(item.get("ready_state") or "live") != "ended"
        )
        incoming_video_tracks = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "video"
            and str(item.get("ready_state") or "live") != "ended"
        )
        loopback_audio_tracks = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "audio"
        )
        loopback_video_tracks = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "video"
        )
        return {
            "device_id": self.device_id,
            "webspace_id": str(getattr(self, "webspace_id", "") or ""),
            "connection_state": connection_state,
            "events_channel_state": events_state,
            "yjs_channel_state": yjs_state,
            "incoming_audio_tracks": incoming_audio_tracks,
            "incoming_video_tracks": incoming_video_tracks,
            "loopback_audio_tracks": loopback_audio_tracks,
            "loopback_video_tracks": loopback_video_tracks,
            "media_track_total": incoming_audio_tracks + incoming_video_tracks,
        }

    def _emit_state_event(self, *, reason: str) -> None:
        try:
            ctx = get_ctx()
        except Exception:
            return
        try:
            payload = {**self.snapshot_record(), "reason": str(reason or "state.changed")}
            bus_emit(ctx.bus, "webrtc.peer.state.changed", payload, "webrtc.peer")
        except Exception:
            _log.debug("failed to emit webrtc peer state device=%s reason=%s", self.device_id, reason, exc_info=True)

    # -- lifecycle ------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        scheduled = self._scheduled_close_task
        if scheduled is not None and scheduled is not current and not scheduled.done():
            scheduled.cancel()
        self._scheduled_close_task = None
        if _peers.get(self.device_id) is self:
            del _peers[self.device_id]
        pc = self.pc
        loopbacks = list(self._loopback_tracks.values())
        for loopback in loopbacks:
            with suppress(asyncio.CancelledError, Exception):
                await self._detach_loopback_sender(loopback)
        adapter = self._yjs_adapter
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            if self._yjs_task and not self._yjs_task.done():
                self._yjs_task.cancel()
        except Exception:
            pass
        try:
            if self._local_desc_task and not self._local_desc_task.done():
                self._local_desc_task.cancel()
        except Exception:
            pass
        self._close_data_channel(self._events_channel)
        self._close_data_channel(self._yjs_channel)
        self._close_data_channel(self._media_channel)
        self._incoming_tracks.clear()
        self._loopback_tracks.clear()
        self._cleanup_media_upload(remove_temp=True)
        yjs_task = self._yjs_task
        local_desc_task = self._local_desc_task
        self._yjs_task = None
        self._local_desc_task = None
        self._yjs_adapter = None
        self._events_channel = None
        self._yjs_channel = None
        self._media_channel = None
        self.pc = None  # type: ignore[assignment]
        if yjs_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await yjs_task
        if local_desc_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await local_desc_task
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
        self._emit_state_event(reason="peer.closed")
        # Only remove ourselves — a replacement peer may already be registered.
        _log.info("peer closed device=%s", self.device_id)


# -- Public API ---------------------------------------------------------------


async def handle_rtc_offer(
    offer_sdp: str,
    offer_type: str,
    device_id: str,
    webspace_id: str,
    send_ice_cb: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, str]:
    """
    Called from ``gateway_ws.py`` when browser sends ``rtc.offer``.

    Returns the SDP answer payload ``{"sdp": ..., "type": "answer"}``.
    """
    existing = _peers.get(device_id)
    if existing:
        state = existing._connection_state() if hasattr(existing, "_connection_state") else str(getattr(existing.pc, "connectionState", "") or "").strip().lower()
        reusable = bool(getattr(existing, "is_reusable_for_offer", lambda: state in _REUSABLE_CONNECTION_STATES)())
        if reusable:
            existing.webspace_id = webspace_id
            existing._send_ice = send_ice_cb
            existing._emit_state_event(reason="offer.renegotiate")
            return await existing.handle_offer(offer_sdp, offer_type)
        _log.info(
            "replacing stale peer for device=%s on new offer state=%s",
            device_id,
            state or "unknown",
        )
        try:
            await asyncio.wait_for(existing.close(), timeout=_REPLACE_CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _log.warning(
                "timed out closing stale peer for device=%s state=%s; continuing replacement",
                device_id,
                state or "unknown",
            )
        except Exception:
            _log.warning(
                "failed closing stale peer for device=%s state=%s; continuing replacement",
                device_id,
                state or "unknown",
                exc_info=True,
            )

    peer = HubPeer(device_id, webspace_id, send_ice_cb)
    _peers[device_id] = peer
    peer._emit_state_event(reason="offer.accepted")
    return await peer.handle_offer(offer_sdp, offer_type)


async def handle_remote_ice(device_id: str, candidate: dict[str, Any] | None) -> None:
    """Called from ``gateway_ws.py`` when browser sends ``rtc.ice``."""
    peer = _peers.get(device_id)
    if not peer:
        _log.debug("rtc.ice for unknown device=%s (ignored)", device_id)
        return
    await peer.add_ice_candidate(candidate or {})


async def close_peers_for_webspace(
    webspace_id: str,
    *,
    reason: str = "webspace_reload",
) -> int:
    key = str(webspace_id or "").strip() or "default"
    peers = [
        peer
        for peer in list(_peers.values())
        if str(getattr(peer, "webspace_id", "") or "").strip() == key
    ]
    if not peers:
        return 0
    _log.info("closing webrtc peers for webspace=%s count=%s reason=%s", key, len(peers), reason)
    closed = 0
    for peer in peers:
        try:
            await peer.close()
            closed += 1
        except Exception:
            _log.debug(
                "failed to close webrtc peer device=%s webspace=%s reason=%s",
                getattr(peer, "device_id", "unknown"),
                key,
                reason,
                exc_info=True,
            )
    return closed


def webrtc_peer_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    stale_peers = [
        peer
        for peer in list(_peers.values())
        if hasattr(peer, "is_stale") and bool(peer.is_stale(now_ts=now))
    ]
    for peer in stale_peers:
        if _peers.get(getattr(peer, "device_id", None)) is peer:
            del _peers[peer.device_id]
        schedule_close = getattr(peer, "_schedule_close", None)
        if callable(schedule_close):
            try:
                schedule_close(reason="stale_peer_prune", delay=0.0)
            except Exception:
                _log.debug(
                    "failed to schedule stale peer prune device=%s",
                    getattr(peer, "device_id", "unknown"),
                    exc_info=True,
                )
    peers: list[dict[str, Any]] = []
    connection_states: dict[str, int] = {}
    open_events_channels = 0
    open_yjs_channels = 0
    incoming_audio_tracks = 0
    incoming_video_tracks = 0
    loopback_audio_tracks = 0
    loopback_video_tracks = 0
    for device_id, peer in list(_peers.items()):
        state = peer._connection_state() if hasattr(peer, "_connection_state") else str(getattr(peer.pc, "connectionState", "") or "unknown").strip().lower() or "unknown"
        connection_states[state] = int(connection_states.get(state) or 0) + 1

        events_state = peer._events_state() if hasattr(peer, "_events_state") else str(getattr(getattr(peer, "_events_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        yjs_state = peer._yjs_state() if hasattr(peer, "_yjs_state") else str(getattr(getattr(peer, "_yjs_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        if events_state == "open":
            open_events_channels += 1
        if yjs_state == "open":
            open_yjs_channels += 1
        incoming = peer._incoming_tracks if isinstance(getattr(peer, "_incoming_tracks", None), dict) else {}
        loopback = peer._loopback_tracks if isinstance(getattr(peer, "_loopback_tracks", None), dict) else {}
        peer_incoming_audio = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "audio"
            and str(item.get("ready_state") or "live") != "ended"
        )
        peer_incoming_video = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "video"
            and str(item.get("ready_state") or "live") != "ended"
        )
        peer_loopback_audio = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "audio"
        )
        peer_loopback_video = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "video"
        )
        incoming_audio_tracks += peer_incoming_audio
        incoming_video_tracks += peer_incoming_video
        loopback_audio_tracks += peer_loopback_audio
        loopback_video_tracks += peer_loopback_video
        peers.append(
            {
                "device_id": device_id,
                "webspace_id": str(getattr(peer, "webspace_id", "") or ""),
                "connection_state": state,
                "events_channel_state": events_state,
                "yjs_channel_state": yjs_state,
                "incoming_audio_tracks": peer_incoming_audio,
                "incoming_video_tracks": peer_incoming_video,
                "loopback_audio_tracks": peer_loopback_audio,
                "loopback_video_tracks": peer_loopback_video,
                "media_track_total": peer_incoming_audio + peer_incoming_video,
            }
        )
    return {
        "peer_total": len(peers),
        "connected_peers": int(connection_states.get("connected") or 0),
        "connecting_peers": int(connection_states.get("connecting") or 0),
        "open_events_channels": open_events_channels,
        "open_yjs_channels": open_yjs_channels,
        "incoming_audio_tracks": incoming_audio_tracks,
        "incoming_video_tracks": incoming_video_tracks,
        "loopback_audio_tracks": loopback_audio_tracks,
        "loopback_video_tracks": loopback_video_tracks,
        "connection_states": connection_states,
        "pruned_stale_peers": len(stale_peers),
        "peers": peers,
        "updated_at": now,
    }
