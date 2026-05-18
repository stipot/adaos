from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from types import SimpleNamespace

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=object,
        apply_update=lambda *args, **kwargs: None,
        encode_state_as_update=lambda *args, **kwargs: b"",
        encode_state_vector=lambda *args, **kwargs: b"",
    )

existing_ypy_websocket = sys.modules.get("ypy_websocket")
if existing_ypy_websocket is None or not hasattr(existing_ypy_websocket, "__path__"):
    ystore_mod = types.ModuleType("ypy_websocket.ystore")
    ystore_mod.BaseYStore = object
    ystore_mod.YDocNotFound = RuntimeError

    class _StubStarted:
        async def wait(self) -> None:
            return None

        def is_set(self) -> bool:
            return False

    class _StubWebsocketServer:
        def __init__(self, *args, **kwargs) -> None:
            self.rooms = {}
            self.rooms_ready = SimpleNamespace()
            self.log = SimpleNamespace()
            self.started = _StubStarted()

        async def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        async def start_room(self, room) -> None:  # noqa: ARG002
            return None

        async def get_room(self, name) -> object:
            room = self.rooms.get(name)
            if room is None:
                room = _StubYRoom()
                self.rooms[name] = room
            return room

        async def serve(self, adapter) -> None:  # noqa: ARG002
            return None

    class _StubMap(dict):
        pass

    class _StubYDoc:
        def get_map(self, name: str) -> _StubMap:  # noqa: ARG002
            return _StubMap()

    class _StubYRoom:
        def __init__(self, *, ready=None, ystore=None, log=None) -> None:
            self.ready = ready
            self.ystore = ystore
            self.log = log
            self.ydoc = _StubYDoc()

        async def stop(self) -> None:
            return None

    ypy_websocket_mod = types.ModuleType("ypy_websocket")
    ypy_websocket_mod.__path__ = []  # type: ignore[attr-defined]
    ypy_websocket_mod.ystore = ystore_mod

    websocket_mod = types.ModuleType("ypy_websocket.websocket")
    websocket_mod.Websocket = object

    websocket_server_mod = types.ModuleType("ypy_websocket.websocket_server")
    websocket_server_mod.WebsocketServer = _StubWebsocketServer

    yroom_mod = types.ModuleType("ypy_websocket.yroom")
    yroom_mod.YRoom = _StubYRoom

    yutils_mod = types.ModuleType("ypy_websocket.yutils")
    yutils_mod.create_update_message = lambda update: b"update:" + bytes(update or b"")

    sys.modules["ypy_websocket"] = ypy_websocket_mod
    sys.modules["ypy_websocket.ystore"] = ystore_mod
    sys.modules["ypy_websocket.websocket"] = websocket_mod
    sys.modules["ypy_websocket.websocket_server"] = websocket_server_mod
    sys.modules["ypy_websocket.yroom"] = yroom_mod
    sys.modules["ypy_websocket.yutils"] = yutils_mod

from adaos.services.workspaces import ensure_workspace, set_workspace_manifest
from adaos.services.yjs import gateway_ws as gateway_module
from adaos.services.yjs.update_origin import mark_backend_room_update, reset_backend_room_update_markers


class _FakeYStore:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.apply_updates_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    async def apply_updates(self, ydoc) -> None:  # noqa: ARG002
        self.apply_updates_calls += 1


class _FakeWriteYStore:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def write(self, update: bytes) -> None:
        self.writes.append(update)


class _FakeBus:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, object]] = []

    def subscribe(self, prefix: str, handler: object) -> None:
        self.subscriptions.append((prefix, handler))


class _FakeEventWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_text(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


def _fake_log() -> SimpleNamespace:
    return SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )


def test_gateway_coerces_legacy_default_webspace_to_runtime_default() -> None:
    assert gateway_module._coerce_gateway_webspace_id("") == "desktop"
    assert gateway_module._coerce_gateway_webspace_id("default") == "desktop"
    assert gateway_module._coerce_gateway_webspace_id("lab") == "lab"


def test_browser_auth_response_marks_denial_as_terminal_login() -> None:
    payload = gateway_module._browser_auth_response_payload(
        dev_id="dev_tv",
        webspace_id="default",
        allowed=False,
        reason="revoked",
    )

    assert payload["allowed"] is False
    assert payload["reason"] == "revoked"
    assert payload["connection_state"] == "revoked"
    assert payload["next"] == "login"
    assert payload["terminal"] is True
    assert payload["webspace_id"] == "desktop"


def test_browser_session_authorize_reports_revoked_device(monkeypatch) -> None:
    touched: list[dict[str, object]] = []

    from adaos.services import access_links

    monkeypatch.setattr(
        access_links,
        "authorize_link",
        lambda kind, entry_id: (False, "revoked"),
    )
    monkeypatch.setattr(
        access_links,
        "touch_browser_session",
        lambda device_id, **kwargs: touched.append({"device_id": device_id, **kwargs}) or {},
    )

    payload = asyncio.run(
        gateway_module.browser_session_authorize(
            dev="dev_tv",
            ws="default",
            browser_family="Chrome",
            os_name="Android",
            form_factor="TV",
            user_agent="ua",
        )
    )

    assert payload["allowed"] is False
    assert payload["reason"] == "revoked"
    assert payload["next"] == "login"
    assert touched == [
        {
            "device_id": "dev_tv",
            "webspace_id": "desktop",
            "connection_state": "revoked",
            "online": False,
            "browser_family": "Chrome",
            "os_name": "Android",
            "form_factor": "TV",
            "user_agent": "ua",
        }
    ]


def test_yws_denied_browser_accepts_before_policy_close(monkeypatch) -> None:
    touched: list[dict[str, object]] = []

    class FakeWebSocket:
        query_params = {
            "dev": "dev_tv",
            "browser_family": "Chrome",
            "os_name": "Android",
            "form_factor": "TV",
        }

        def __init__(self) -> None:
            self.accepted = False
            self.closed: dict[str, object] | None = None

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed = {"code": code, "reason": reason}

    from adaos.services import access_links

    monkeypatch.setattr(
        access_links,
        "authorize_link",
        lambda kind, entry_id: (False, "revoked"),
    )
    monkeypatch.setattr(
        access_links,
        "touch_browser_session",
        lambda device_id, **kwargs: touched.append({"device_id": device_id, **kwargs}) or {},
    )

    websocket = FakeWebSocket()
    asyncio.run(gateway_module._yws_impl(websocket, room="default"))

    assert websocket.accepted is True
    assert websocket.closed == {"code": 1008, "reason": "device_revoked"}
    assert touched == [
        {
            "device_id": "dev_tv",
            "webspace_id": "desktop",
            "connection_state": "revoked",
            "online": False,
            "browser_family": "Chrome",
            "os_name": "Android",
            "form_factor": "TV",
        }
    ]


def test_diagnostic_room_skips_duplicate_backend_persisted_update() -> None:
    reset_backend_room_update_markers()
    ystore = _FakeWriteYStore()
    room = gateway_module.DiagnosticYRoom(ystore=ystore, log=_fake_log())
    room._webspace_id = "desktop"

    mark_backend_room_update("desktop", b"backend-update", source="async_get_ydoc", owner="skill:infrastate_skill")

    asyncio.run(room._tracked_ystore_write(b"backend-update"))
    asyncio.run(room._tracked_ystore_write(b"backend-update"))

    assert ystore.writes == [b"backend-update"]
    assert room._diag_backend_persist_skip_total == 1
    assert room._diag_backend_persist_skip_bytes == len(b"backend-update")


def test_diagnostic_room_persists_unmarked_browser_update() -> None:
    reset_backend_room_update_markers()
    ystore = _FakeWriteYStore()
    room = gateway_module.DiagnosticYRoom(ystore=ystore, log=_fake_log())
    room._webspace_id = "desktop"

    asyncio.run(room._tracked_ystore_write(b"browser-update"))

    assert ystore.writes == [b"browser-update"]


def test_request_webio_stream_snapshots_extracts_node_qualified_receiver() -> None:
    published: list[object] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    gateway_module.get_agent_ctx = lambda: SimpleNamespace(bus=_Bus())

    gateway_module._request_webio_stream_snapshots(
        {"webio.stream.default.nodes.member-01.telemetry.feed"},
        transport="ws",
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "payload", {}).get("webspace_id") == "desktop"
    assert getattr(event, "payload", {}).get("receiver") == "telemetry.feed"
    assert getattr(event, "payload", {}).get("node_id") == "member-01"
    assert getattr(event, "payload", {}).get("target_node_id") == "member-01"
    assert getattr(event, "payload", {}).get("_meta", {}).get("target_node_id") == "member-01"


def test_request_webio_stream_snapshots_extracts_global_node_receiver() -> None:
    published: list[object] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    gateway_module.get_agent_ctx = lambda: SimpleNamespace(bus=_Bus())

    gateway_module._request_webio_stream_snapshots(
        {"webio.stream.nodes.member-01.telemetry.feed"},
        transport="ws",
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "payload", {}).get("webspace_id") == "desktop"
    assert getattr(event, "payload", {}).get("receiver") == "telemetry.feed"
    assert getattr(event, "payload", {}).get("node_id") == "member-01"
    assert getattr(event, "payload", {}).get("target_node_id") == "member-01"
    assert getattr(event, "payload", {}).get("_meta", {}).get("target_node_id") == "member-01"


def test_diagnostic_room_skips_empty_y_update() -> None:
    reset_backend_room_update_markers()
    ystore = _FakeWriteYStore()
    room = gateway_module.DiagnosticYRoom(ystore=ystore, log=_fake_log())
    room._webspace_id = "desktop"

    asyncio.run(room._tracked_ystore_write(b"\x00\x00"))

    assert ystore.writes == []
    assert room._diag_empty_update_skip_total == 1
    assert room._diag_empty_update_skip_bytes == 2


def test_ensure_webspace_ready_uses_manifest_defaults(monkeypatch) -> None:
    webspace_id = "gateway-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Gateway Home",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str, ydoc=None) -> None:  # noqa: ANN001
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
                "ydoc": ydoc,
            }
        )

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)

    asyncio.run(gateway_module.ensure_webspace_ready(webspace_id))

    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "prompt_engineer_scenario",
            "space": "dev",
            "ydoc": None,
        }
    ]
    assert fake_store.stop_calls == 1


def test_ensure_webspace_ready_explicit_scenario_overrides_manifest_home(monkeypatch) -> None:
    webspace_id = "gateway-explicit"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Explicit Space",
        kind="workspace",
        source_mode="workspace",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str, ydoc=None) -> None:  # noqa: ANN001
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
                "ydoc": ydoc,
            }
        )

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)

    asyncio.run(gateway_module.ensure_webspace_ready(webspace_id, scenario_id="custom_scenario"))

    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "custom_scenario",
            "space": "workspace",
            "ydoc": None,
        }
    ]


def test_get_room_uses_manifest_defaults_for_room_seed(monkeypatch) -> None:
    webspace_id = "gateway-room"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Room Space",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str, ydoc=None) -> dict[str, object]:  # noqa: ANN001
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
                "ydoc": ydoc,
            }
        )
        return {
            "used_provided_ydoc": bool(ydoc is not None),
            "mode": "scenario_projection",
            "persisted_via": "diff",
            "apply_updates_ms": 1.25,
            "total_ms": 2.5,
        }

    class _Scheduler:
        async def ensure_every(self, **kwargs) -> None:  # noqa: ARG002
            return None

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)
    monkeypatch.setattr(gateway_module, "get_scheduler", lambda: _Scheduler())
    monkeypatch.setattr(gateway_module, "attach_room_observers", lambda _webspace_id, _ydoc: None)

    server = gateway_module.WorkspaceWebsocketServer(auto_clean_rooms=False)
    monkeypatch.setattr(server, "start_room", lambda _room: asyncio.sleep(0))
    room = asyncio.run(server.get_room(webspace_id))

    assert room is server.rooms[webspace_id]
    assert fake_store.apply_updates_calls == 0
    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "prompt_engineer_scenario",
            "space": "dev",
            "ydoc": room.ydoc,
        }
    ]


def test_reset_live_webspace_room_releases_refs_and_requests_compaction(monkeypatch) -> None:
    class _FakeRoom:
        def __init__(self) -> None:
            self.ydoc = object()
            self.ystore = _FakeYStore()
            self._loop = object()
            self._thread_id = 123
            self.ready = object()
            self.log = object()
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    async def _fake_close(_webspace_id: str, *, code: int = 1012, reason: str = "webspace_reload") -> int:  # noqa: ARG001
        return 0

    async def _fake_close_webrtc(_webspace_id: str, *, reason: str = "webspace_reload") -> int:  # noqa: ARG001
        return 2

    async def _fake_route_reset(*, reason: str = "route_reset", notify_browser: bool = True) -> dict[str, object]:  # noqa: ARG001
        return {"ok": True, "closed_tunnels": 1, "notify_browser": notify_browser, "reason": reason}

    room = _FakeRoom()
    backup_jobs_deleted: list[str] = []

    async def _fake_delete(name: str) -> None:
        backup_jobs_deleted.append(name)

    async def _fake_evict_ystore_for_webspace(
        webspace_id: str,
        *,
        store=None,
        persist_snapshot: bool = True,
        compact_runtime: bool = True,
        backup_kind: str = "evict",
        delete_snapshot: bool = False,
    ) -> dict[str, object]:
        assert webspace_id == "gateway-room-reset"
        assert store is room.ystore
        assert persist_snapshot is True
        assert compact_runtime is True
        assert delete_snapshot is False
        return {
            "ok": True,
            "webspace_id": webspace_id,
            "ystore_found": True,
            "persisted": True,
            "backup_skipped": False,
            "released_update_entries": 3,
            "released_update_bytes": 128,
        }

    gateway_module.y_server.rooms["gateway-room-reset"] = room
    gateway_module._room_locks["gateway-room-reset"] = asyncio.Lock()

    monkeypatch.setattr(gateway_module, "close_webspace_yws_connections", _fake_close)
    monkeypatch.setattr(gateway_module, "close_webspace_webrtc_peers", _fake_close_webrtc)
    monkeypatch.setattr(gateway_module, "reset_hub_route_runtime", _fake_route_reset)
    monkeypatch.setattr(gateway_module, "evict_ystore_for_webspace", _fake_evict_ystore_for_webspace)
    monkeypatch.setattr(gateway_module, "get_scheduler", lambda: SimpleNamespace(delete=_fake_delete))
    monkeypatch.setattr(gateway_module.gc, "collect", lambda: 7)
    monkeypatch.setattr(gateway_module, "_trim_allocator_after_yjs_room_reset", lambda: True)

    result = asyncio.run(gateway_module.reset_live_webspace_room("gateway-room-reset"))

    assert gateway_module.y_server.rooms.get("gateway-room-reset") is None
    assert gateway_module._room_locks.get("gateway-room-reset") is None
    assert room.stop_calls == 1
    assert room.ystore is None
    assert room.ydoc is None
    assert result["room_dropped"] is True
    assert result["room_stopped"] is True
    assert result["ystore_stopped"] is True
    assert result["ystore_evicted"] is True
    assert result["ystore_snapshot_persisted"] is True
    assert result["scheduler_job_deleted"] is True
    assert result["closed_webrtc_peers"] == 2
    assert result["route_reset"]["closed_tunnels"] == 1
    assert result["runtime_compaction_requested"] is True
    assert result["room_refs_released"] is True
    assert result["gc_collected"] == 7
    assert result["malloc_trimmed"] is True
    assert result["prewarm_after_reset"] is True
    assert backup_jobs_deleted == ["ystores.backup.gateway-room-reset"]


def test_yws_tracking_cancels_pending_idle_room_reset(monkeypatch) -> None:
    gateway_module.y_server.rooms["idle-room"] = object()
    gateway_module._IDLE_ROOM_RESET_TASKS.clear()

    reset_calls: list[tuple[str, str]] = []

    async def _fake_reset(webspace_id: str, *, close_reason: str = "webspace_reload") -> dict[str, object]:
        reset_calls.append((webspace_id, close_reason))
        return {"ok": True}

    monkeypatch.setattr(gateway_module, "_IDLE_ROOM_EVICT_SEC", 0.05)
    monkeypatch.setattr(gateway_module, "reset_live_webspace_room", _fake_reset)
    monkeypatch.setattr(gateway_module, "_active_webrtc_peer_total_for_webspace", lambda _webspace_id: 0)

    async def _exercise() -> None:
        websocket = SimpleNamespace(query_params={"dev": "dev-1"})
        gateway_module._track_yws_connection("idle-room", websocket, device_id="dev-1")
        gateway_module._untrack_yws_connection("idle-room", websocket)
        gateway_module._track_yws_connection("idle-room", websocket, device_id="dev-1")
        await asyncio.sleep(0.08)

    asyncio.run(_exercise())

    assert reset_calls == []
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._IDLE_ROOM_RESET_TASKS.clear()
    gateway_module.y_server.rooms.clear()


def test_idle_room_reset_evicts_without_prewarm_or_route_reset(monkeypatch) -> None:
    gateway_module.y_server.rooms["idle-room-evict"] = object()
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._IDLE_ROOM_RESET_TASKS.clear()

    reset_calls: list[dict[str, object]] = []

    async def _fake_reset(
        webspace_id: str,
        *,
        close_reason: str = "webspace_reload",
        reset_route_runtime: bool = True,
        prewarm_after_reset: bool | None = None,
    ) -> dict[str, object]:
        reset_calls.append(
            {
                "webspace_id": webspace_id,
                "close_reason": close_reason,
                "reset_route_runtime": reset_route_runtime,
                "prewarm_after_reset": prewarm_after_reset,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(gateway_module, "_IDLE_ROOM_EVICT_SEC", 0.02)
    monkeypatch.setattr(gateway_module, "reset_live_webspace_room", _fake_reset)
    monkeypatch.setattr(gateway_module, "_active_webrtc_peer_total_for_webspace", lambda _webspace_id: 0)

    async def _exercise() -> None:
        assert gateway_module._schedule_idle_room_reset("idle-room-evict") is True
        await asyncio.sleep(0.06)

    asyncio.run(_exercise())

    assert reset_calls == [
        {
            "webspace_id": "idle-room-evict",
            "close_reason": "idle_room_eviction",
            "reset_route_runtime": False,
            "prewarm_after_reset": False,
        }
    ]
    gateway_module._IDLE_ROOM_RESET_TASKS.clear()
    gateway_module.y_server.rooms.clear()


def test_gateway_transport_snapshot_reports_room_diagnostics() -> None:
    class _FakeStatsStream:
        def __init__(self, *, buffer_used: int, waiting_send: int, waiting_receive: int) -> None:
            self._buffer_used = buffer_used
            self._waiting_send = waiting_send
            self._waiting_receive = waiting_receive

        def statistics(self):
            return SimpleNamespace(
                current_buffer_used=self._buffer_used,
                max_buffer_size=65536,
                open_send_streams=1,
                open_receive_streams=1,
                tasks_waiting_send=self._waiting_send,
                tasks_waiting_receive=self._waiting_receive,
            )

    class _Started:
        def is_set(self) -> bool:
            return True

    class _FakeRoom:
        def __init__(self) -> None:
            self.ydoc = object()
            self.ystore = object()
            self.clients = [object(), object()]
            self._ready = True
            self._started = _Started()
            self._task_group = object()
            self._update_send_stream = _FakeStatsStream(buffer_used=5, waiting_send=2, waiting_receive=1)
            self._update_receive_stream = _FakeStatsStream(buffer_used=5, waiting_send=2, waiting_receive=1)

    key = "gateway-room-debug"
    room = _FakeRoom()
    gateway_module.y_server.rooms[key] = room
    gateway_module._YROOM_LIFECYCLE.clear()
    gateway_module._mark_room_created(key, room)
    gateway_module._mark_room_open(
        key,
        room,
        created=True,
        open_total_ms=12.5,
        seed_result={
            "used_provided_ydoc": True,
            "mode": "scenario_projection",
            "persisted_via": "diff",
            "apply_updates_ms": 3.0,
            "total_ms": 6.0,
        },
    )
    gateway_module._mark_room_reset(
        key,
        close_reason="manual_test",
        room=room,
        room_dropped=False,
        closed_connections=1,
        closed_webrtc_peers=2,
    )

    snapshot = gateway_module.gateway_transport_snapshot()
    room_info = snapshot["rooms"][key]
    transport = snapshot["transports"]["yws"]

    assert room_info["active"] is True
    assert room_info["generation"] == 1
    assert room_info["client_total"] == 2
    assert room_info["cold_open_total"] == 1
    assert room_info["single_pass_bootstrap_total"] == 1
    assert room_info["last_open_mode"] == "cold_open"
    assert room_info["last_open_bootstrap_mode"] == "scenario_projection"
    assert room_info["update_send_stream"]["current_buffer_used"] == 5
    assert room_info["update_send_stream"]["tasks_waiting_send"] == 2
    assert room_info["last_reset_reason"] == "manual_test"
    assert room_info["last_reset_closed_webrtc_peers"] == 2
    assert transport["active_room_total"] >= 1
    assert transport["room_generation_max"] >= 1
    assert transport["room_cold_open_total"] >= 1
    assert transport["room_single_pass_bootstrap_total"] >= 1
    assert transport["update_stream_buffer_used_total"] >= 5

    gateway_module.y_server.rooms.pop(key, None)
    gateway_module._YROOM_LIFECYCLE.clear()


def test_process_events_command_publishes_go_home(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.go_home",
            cmd_id="cmd-1",
            payload={"webspace_id": "default"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == [("desktop.webspace.go_home", {"webspace_id": "default"})]
    assert responses[-1]["ok"] is True


def test_process_events_command_preserves_weather_node_target(monkeypatch) -> None:
    published: list[object] = []
    responses: list[dict[str, object]] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    monkeypatch.setattr(gateway_module, "get_agent_ctx", lambda: SimpleNamespace(bus=_Bus()))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="weather.city_changed",
            cmd_id="cmd-weather-1",
            payload={"city": "Berlin", "node_id": "member-01", "webspace_id": "desktop"},
            device_id="dev-1",
            webspace_id="desktop",
            send_response=_send_response,
        )
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "type", "") == "weather.city_changed"
    payload = getattr(event, "payload", {})
    assert payload["city"] == "Berlin"
    assert payload["node_id"] == "member-01"
    assert payload["target_node_id"] == "member-01"
    assert payload["_meta"]["target_node_id"] == "member-01"
    assert responses[-1]["ok"] is True


def test_process_events_command_publishes_generic_skill_event(monkeypatch) -> None:
    published: list[object] = []
    responses: list[dict[str, object]] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    monkeypatch.setattr(gateway_module, "get_agent_ctx", lambda: SimpleNamespace(bus=_Bus()))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="skill.event.publish",
            cmd_id="cmd-skill-event-1",
            payload={
                "event_type": "custom.location.requested",
                "payload": {"city": "Berlin", "request_id": "req-1"},
                "node_id": "member-01",
                "webspace_id": "desktop",
                "_meta": {"trace_id": "trace-1"},
            },
            device_id="dev-1",
            webspace_id="desktop",
            send_response=_send_response,
        )
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "type", "") == "custom.location.requested"
    payload = getattr(event, "payload", {})
    assert payload["city"] == "Berlin"
    assert payload["request_id"] == "req-1"
    assert payload["node_id"] == "member-01"
    assert payload["target_node_id"] == "member-01"
    assert payload["webspace_id"] == "desktop"
    assert payload["_meta"]["trace_id"] == "trace-1"
    assert payload["_meta"]["target_node_id"] == "member-01"
    assert responses[-1]["ok"] is True
    assert responses[-1]["data"] == {"event_type": "custom.location.requested"}


def test_process_events_command_accepts_demo_metrics_host_action(monkeypatch) -> None:
    published: list[object] = []
    responses: list[dict[str, object]] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    monkeypatch.setattr(gateway_module, "get_agent_ctx", lambda: SimpleNamespace(bus=_Bus()))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="demo_metrics.host_action",
            cmd_id="cmd-demo-host-1",
            payload={"action_id": "demo", "metric_id": "cpu", "webspace_id": "desktop"},
            device_id="dev-1",
            webspace_id="desktop",
            send_response=_send_response,
        )
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "type", "") == "demo_metrics.host_action"
    payload = getattr(event, "payload", {})
    assert payload["action_id"] == "demo"
    assert payload["metric_id"] == "cpu"
    assert payload["webspace_id"] == "desktop"
    assert responses[-1]["ok"] is True


def test_process_events_command_records_reload_command_trace(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))
    gateway_module._COMMAND_TRACE_HISTORY.clear()
    gateway_module._COMMAND_TRACE_STATS.update(
        {
            "reload_total": 0,
            "reload_duplicate_total": 0,
            "reset_total": 0,
            "reset_duplicate_total": 0,
        }
    )
    gateway_module._COMMAND_TRACE_SEQ = 0

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.reload",
            cmd_id="cmd-reload-1",
            payload={"webspace_id": "default", "scenario_id": "web_desktop"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
            client_label="events_ws:127.0.0.1:12345",
        )
    )

    snapshot = gateway_module.gateway_transport_snapshot()
    commands = snapshot["commands"]

    assert published == [("desktop.webspace.reload", {"webspace_id": "default", "scenario_id": "web_desktop", "_meta": {"cmd_id": "cmd-reload-1", "gateway_client": "events_ws:127.0.0.1:12345", "gateway_command_seq": 1, "gateway_command_fingerprint": commands["last_reload"]["fingerprint"]}})]
    assert responses[-1]["ok"] is True
    assert commands["reload_total"] == 1
    assert commands["reload_recent_60s"] == 1
    assert commands["last_reload"]["cmd_id"] == "cmd-reload-1"
    assert commands["last_reload"]["client"] == "events_ws:127.0.0.1:12345"
    gateway_module._COMMAND_TRACE_HISTORY.clear()
    gateway_module._COMMAND_TRACE_STATS.update(
        {
            "reload_total": 0,
            "reload_duplicate_total": 0,
            "reset_total": 0,
            "reset_duplicate_total": 0,
        }
    )


def test_process_events_command_preserves_target_node_for_voice_chat(monkeypatch) -> None:
    published: list[object] = []
    responses: list[dict[str, object]] = []

    class _Bus:
        def publish(self, event: object) -> None:
            published.append(event)

    monkeypatch.setattr(gateway_module, "get_agent_ctx", lambda: SimpleNamespace(bus=_Bus()))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="voice.chat.user",
            cmd_id="cmd-voice-1",
            payload={"text": "hello", "node_id": "member-01", "webspace_id": "desktop"},
            device_id="dev-1",
            webspace_id="desktop",
            send_response=_send_response,
        )
    )

    assert len(published) == 1
    event = published[0]
    assert getattr(event, "type", "") == "voice.chat.user"
    payload = getattr(event, "payload", {})
    assert payload["text"] == "hello"
    assert payload["target_node_id"] == "member-01"
    assert payload["_meta"]["target_node_id"] == "member-01"
    assert responses[-1]["ok"] is True
    gateway_module._COMMAND_TRACE_SEQ = 0


def test_process_events_command_requires_scenario_id_for_set_home(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.set_home",
            cmd_id="cmd-2",
            payload={"webspace_id": "default"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == []
    assert responses[-1]["ok"] is False
    assert responses[-1]["error"] == "scenario_id required"


def test_process_events_command_ensure_dev_returns_webspace_id(monkeypatch) -> None:
    from adaos.services.scenario import webspace_runtime as webspace_runtime_module

    responses: list[dict[str, object]] = []
    ensured: list[tuple[str, str]] = []

    async def _fake_ensure_dev(
        scenario_id: str,
        *,
        requested_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, object]:
        assert requested_id is None
        assert title == "Prompt IDE"
        return {
            "ok": True,
            "accepted": True,
            "created": True,
            "webspace_id": "dev-prompt-engineer-scenario",
            "scenario_id": scenario_id,
            "home_scenario": scenario_id,
            "kind": "dev",
            "source_mode": "dev",
        }

    async def _fake_ready(webspace_id: str, scenario_id: str | None = None) -> None:
        ensured.append((webspace_id, str(scenario_id or "")))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    monkeypatch.setattr(webspace_runtime_module, "ensure_dev_webspace_for_scenario", _fake_ensure_dev)
    monkeypatch.setattr(gateway_module, "ensure_webspace_ready", _fake_ready)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.ensure_dev",
            cmd_id="cmd-3",
            payload={"scenario_id": "prompt_engineer_scenario", "title": "Prompt IDE"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert ensured == [("dev-prompt-engineer-scenario", "prompt_engineer_scenario")]
    assert responses[-1]["ok"] is True
    assert responses[-1]["data"] == {
        "ok": True,
        "accepted": True,
        "created": True,
        "webspace_id": "dev-prompt-engineer-scenario",
        "scenario_id": "prompt_engineer_scenario",
        "home_scenario": "prompt_engineer_scenario",
        "kind": "dev",
        "source_mode": "dev",
    }


def test_process_events_command_publishes_device_registered(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _fake_start_y_server() -> None:
        return None

    async def _fake_update_device_presence(webspace_id: str, device_id: str) -> None:
        assert webspace_id == "ops"
        assert device_id == "dev-2"

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    monkeypatch.setattr(gateway_module, "start_y_server", _fake_start_y_server)
    monkeypatch.setattr(gateway_module, "_update_device_presence", _fake_update_device_presence)

    asyncio.run(
        gateway_module.process_events_command(
            kind="device.register",
            cmd_id="cmd-4",
            payload={"device_id": "dev-2", "webspace_id": "ops"},
            device_id="dev-2",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == [
        (
            "device.registered",
            {"device_id": "dev-2", "webspace_id": "ops", "kind": "browser"},
        )
    ]
    assert responses[-1]["ok"] is True
    assert responses[-1]["data"] == {"webspace_id": "ops"}


def test_device_register_skips_yjs_post_steps_when_yws_guard_is_active(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()
    gateway_module._ACTIVE_YWS_CONNECTIONS["ops"] = [object()]
    monkeypatch.setattr(gateway_module, "_YWS_MAX_ACTIVE_PER_WEBSPACE", 1)
    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _fake_start_y_server() -> None:
        raise AssertionError("device.register guard should avoid YRoom startup")

    async def _fake_update_device_presence(_webspace_id: str, _device_id: str) -> None:
        raise AssertionError("device.register guard should avoid YDoc writes")

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    monkeypatch.setattr(gateway_module, "start_y_server", _fake_start_y_server)
    monkeypatch.setattr(gateway_module, "_update_device_presence", _fake_update_device_presence)

    asyncio.run(
        gateway_module.process_events_command(
            kind="device.register",
            cmd_id="cmd-guard",
            payload={"device_id": "dev-guard", "webspace_id": "ops"},
            device_id="dev-guard",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == [
        (
            "device.registered",
            {
                "device_id": "dev-guard",
                "webspace_id": "ops",
                "kind": "browser",
                "yjs_post_skipped": True,
                "yjs_guard_reason": "active_limit",
            },
        )
    ]
    assert responses[-1]["ok"] is True
    assert responses[-1]["data"] == {
        "webspace_id": "ops",
        "yjs_post_skipped": True,
        "yjs_guard_reason": "active_limit",
    }
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()


def test_accept_websocket_returns_false_when_handshake_already_closed() -> None:
    class _FakeWebSocket:
        async def accept(self) -> None:
            raise RuntimeError(
                "Expected ASGI message 'websocket.send' or 'websocket.close', but got 'websocket.accept'."
            )

    accepted = asyncio.run(gateway_module._accept_websocket(_FakeWebSocket(), channel="events"))

    assert accepted is False


def test_events_ws_treats_receive_before_accept_runtimeerror_as_disconnect() -> None:
    class _FakeClosedWebSocket:
        query_params: dict[str, str] = {}
        scope = {"client": ("127.0.0.1", 9347)}
        accepted = False

        async def accept(self) -> None:
            self.accepted = True

        async def receive_text(self) -> str:
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

    websocket = _FakeClosedWebSocket()

    asyncio.run(gateway_module.events_ws(websocket))  # type: ignore[arg-type]

    assert websocket.accepted is True


def test_active_browser_session_snapshot_tracks_yws_clients() -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()

    ws = SimpleNamespace(query_params={"dev": "dev-2"})
    gateway_module._track_yws_connection("ops", ws, device_id="dev-2")

    snapshot = gateway_module.active_browser_session_snapshot(now_ts=123.0)

    assert snapshot["peer_total"] == 1
    assert snapshot["peers"] == [
        {
            "device_id": "dev-2",
            "webspace_id": "ops",
            "connection_state": "connected",
            "yjs_channel_state": "open",
            "session_count": 1,
            "source": "yws_gateway",
        }
    ]

    gateway_module._untrack_yws_connection("ops", ws)
    assert gateway_module.active_browser_session_snapshot(now_ts=123.0)["peers"] == []


def test_yws_close_preserves_online_state_when_device_has_replacement_session() -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()

    old_ws = SimpleNamespace(query_params={"dev": "dev-2"})
    new_ws = SimpleNamespace(query_params={"dev": "dev-2"})
    gateway_module._track_yws_connection("ops", old_ws, device_id="dev-2")
    gateway_module._track_yws_connection("desktop", new_ws, device_id="dev-2")

    gateway_module._untrack_yws_connection("ops", old_ws)

    assert gateway_module._active_yws_connection_total_for_device("dev-2") == 1
    assert gateway_module._should_mark_yws_browser_session_offline("dev-2") is False

    gateway_module._untrack_yws_connection("desktop", new_ws)
    assert gateway_module._should_mark_yws_browser_session_offline("dev-2") is True


def test_yws_guard_replaces_existing_client_sessions(monkeypatch) -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._YWS_GUARD_DIAG.clear()
    monkeypatch.setattr(gateway_module, "_YWS_MAX_ACTIVE_PER_CLIENT", 1)

    class _FakeWebSocket:
        query_params = {"dev": "dev-2"}

        def __init__(self) -> None:
            self.closed: list[tuple[int, str]] = []

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed.append((code, str(reason or "")))

    old_ws = _FakeWebSocket()
    gateway_module._track_yws_connection("ops", old_ws, device_id="dev-2")

    closed = asyncio.run(gateway_module._close_existing_yws_client_connections("ops", "dev-2"))

    assert closed == 1
    assert old_ws.closed == [(1012, "replaced_by_new_yws_session")]
    assert gateway_module._YWS_GUARD_DIAG["replaced_total"] == 1
    gateway_module._untrack_yws_connection("ops", old_ws)


def test_yws_impl_aborts_when_room_ready_times_out(monkeypatch) -> None:
    gateway_module._TRANSPORT_STATE["yws"].update(
        {
            "active_connections": 0,
            "open_total": 0,
            "close_total": 0,
            "last_open_at": 0.0,
            "last_close_at": 0.0,
        }
    )
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_MAX_S", 0.01)
    events: list[tuple[str, dict[str, object] | None]] = []

    class _FakeWebSocket:
        query_params = {"dev": "dev-timeout"}
        close_calls: list[tuple[int, str]]

        def __init__(self) -> None:
            self.close_calls = []

        async def accept(self) -> None:
            return None

        async def close(self, *, code: int, reason: str) -> None:
            self.close_calls.append((code, reason))

    async def _fake_start_y_server() -> None:
        return None

    async def _fake_get_room(_name: str) -> object:
        await asyncio.sleep(0.05)
        raise AssertionError("timed wait should cancel before room creation completes")

    monkeypatch.setattr(gateway_module, "start_y_server", _fake_start_y_server)
    monkeypatch.setattr(gateway_module, "_publish_runtime_event", lambda topic, payload=None, source="yjs.gateway": events.append((topic, payload)))
    monkeypatch.setattr(gateway_module.y_server, "get_room", _fake_get_room)

    websocket = _FakeWebSocket()
    asyncio.run(gateway_module._yws_impl(websocket, "desktop"))

    assert websocket.close_calls == [(1013, "room_ready_timeout")]
    assert events == []
    assert gateway_module._TRANSPORT_STATE["yws"]["active_connections"] == 0
    assert gateway_module._ACTIVE_YWS_CONNECTIONS == {}
    assert gateway_module._ACTIVE_YWS_CLIENTS == {}


def test_yws_impl_rejects_before_room_acquire_when_active_limit_is_hit(monkeypatch) -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()
    gateway_module._YWS_GUARD_DIAG.update(
        {
            "reject_total": 0,
            "last_reject_at": 0.0,
            "last_reject_reason": "",
            "last_reject_webspace_id": "",
            "last_reject_dev_id": "",
        }
    )
    monkeypatch.setattr(gateway_module, "_YWS_MAX_ACTIVE_PER_WEBSPACE", 1)
    gateway_module._ACTIVE_YWS_CONNECTIONS["desktop"] = [object()]
    events: list[tuple[str, dict[str, object] | None]] = []
    touched: list[dict[str, object]] = []

    class _FakeWebSocket:
        query_params = {"dev": "dev-over-limit"}

        def __init__(self) -> None:
            self.accepted = False
            self.closed: tuple[int, str] | None = None

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed = (code, str(reason or ""))

    async def _start_y_server_must_not_run() -> None:
        raise AssertionError("guard should reject before starting/acquiring Yjs room")

    from adaos.services import access_links

    monkeypatch.setattr(access_links, "authorize_link", lambda kind, entry_id: (True, "ok"))
    monkeypatch.setattr(
        access_links,
        "touch_browser_session",
        lambda device_id, **kwargs: touched.append({"device_id": device_id, **kwargs}) or {},
    )
    monkeypatch.setattr(gateway_module, "start_y_server", _start_y_server_must_not_run)
    monkeypatch.setattr(gateway_module, "_publish_runtime_event", lambda topic, payload=None, source="yjs.gateway": events.append((topic, payload)))

    websocket = _FakeWebSocket()
    asyncio.run(gateway_module._yws_impl(websocket, "desktop"))

    assert websocket.accepted is True
    assert websocket.closed == (1013, "yws_guard_active_limit")
    assert touched[0]["connection_state"] == "yws_guard_active_limit"
    assert events[0][0] == "browser.session.changed"
    assert events[0][1]["yjs_channel_state"] == "rejected"
    assert events[0][1]["reason"] == "active_limit"
    assert gateway_module._YWS_GUARD_DIAG["last_reject_reason"] == "active_limit"
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()


def test_yws_guard_quarantines_hot_reconnecting_client(monkeypatch) -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_CLIENT_OPEN_15S", 3)
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_COOLDOWN_S", 10.0)
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_MAX_COOLDOWN_S", 40.0)

    for _idx in range(3):
        gateway_module._record_yws_open("desktop", "dev-hot")

    reason, diag = gateway_module._yws_guard_reject_reason("desktop", "dev-hot")
    assert reason == "client_reconnect_storm"
    assert diag["client_open_15s"] == 3
    assert diag["quarantine_ttl_s"] == 10.0

    reason_again, _diag_again = gateway_module._yws_guard_reject_reason("desktop", "dev-hot")
    assert reason_again == "quarantined"
    assert gateway_module._yws_storm_snapshot(time.time())["guard"]["quarantined_total"] == 1
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()


def test_yws_guard_escalates_repeated_client_quarantine(monkeypatch) -> None:
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_CLIENT_OPEN_15S", 2)
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_COOLDOWN_S", 10.0)
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_MAX_COOLDOWN_S", 40.0)
    monkeypatch.setattr(gateway_module, "_YWS_GUARD_ESCALATION_WINDOW_S", 3600.0)

    for _idx in range(2):
        gateway_module._record_yws_open("desktop", "dev-hot")
    reason, diag = gateway_module._yws_guard_reject_reason("desktop", "dev-hot")
    assert reason == "client_reconnect_storm"
    assert diag["quarantine_ttl_s"] == 10.0

    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    for _idx in range(2):
        gateway_module._record_yws_open("desktop", "dev-hot")
    reason2, diag2 = gateway_module._yws_guard_reject_reason("desktop", "dev-hot")
    assert reason2 == "client_reconnect_storm"
    assert diag2["quarantine_ttl_s"] == 20.0
    assert diag2["quarantine_incident_count"] == 2
    gateway_module._YWS_OPEN_HISTORY.clear()
    gateway_module._YWS_CLIENT_OPEN_HISTORY.clear()
    gateway_module._YWS_GUARD_QUARANTINE_UNTIL.clear()
    gateway_module._YWS_GUARD_INCIDENTS.clear()


def test_acquire_yws_room_uses_cache_when_bootstrap_lags(monkeypatch) -> None:
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_MAX_S", 0.05)
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_POLL_S", 0.005)

    class _FakeRoom:
        pass

    room = _FakeRoom()
    original_rooms = gateway_module.y_server.rooms
    gateway_module.y_server.rooms = {}

    async def _fake_get_room(_name: str) -> object:
        await asyncio.sleep(0.2)
        return room

    async def _exercise() -> object:
        task = asyncio.create_task(gateway_module._acquire_yws_room("desktop", "dev-cache"))
        await asyncio.sleep(0.015)
        gateway_module.y_server.rooms["desktop"] = room
        return await task

    monkeypatch.setattr(gateway_module.y_server, "get_room", _fake_get_room)
    try:
        resolved = asyncio.run(_exercise())
    finally:
        gateway_module.y_server.rooms = original_rooms

    assert resolved is room


def test_yws_impl_cleans_up_after_first_message_timeout(monkeypatch) -> None:
    gateway_module._TRANSPORT_STATE["yws"].update(
        {
            "active_connections": 0,
            "open_total": 0,
            "close_total": 0,
            "last_open_at": 0.0,
            "last_close_at": 0.0,
        }
    )
    gateway_module._ACTIVE_YWS_CONNECTIONS.clear()
    gateway_module._ACTIVE_YWS_CLIENTS.clear()
    monkeypatch.setattr(gateway_module, "_YWS_ROOM_READY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(gateway_module, "_YWS_FIRST_MESSAGE_TIMEOUT_S", 0.01)
    events: list[tuple[str, dict[str, object] | None]] = []

    class _FakeWebSocket:
        query_params = {"dev": "dev-first-timeout"}
        close_code = None

        async def accept(self) -> None:
            return None

        async def send_bytes(self, _message: bytes) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return {"type": "websocket.receive", "bytes": b""}

    class _FakeRoom:
        async def serve(self, websocket) -> None:
            async for _message in websocket:
                raise AssertionError("the adapter should stop iteration before yielding a message")

    async def _fake_start_y_server() -> None:
        return None

    async def _fake_get_room(_name: str) -> object:
        return _FakeRoom()

    monkeypatch.setattr(gateway_module, "start_y_server", _fake_start_y_server)
    monkeypatch.setattr(gateway_module, "_publish_runtime_event", lambda topic, payload=None, source="yjs.gateway": events.append((topic, payload)))
    monkeypatch.setattr(gateway_module.y_server, "get_room", _fake_get_room)

    asyncio.run(gateway_module._yws_impl(_FakeWebSocket(), "desktop"))

    assert [topic for topic, _payload in events] == [
        "browser.session.changed",
        "browser.session.changed",
    ]
    assert events[0][1]["connection_state"] == "connected"
    assert events[1][1]["connection_state"] == "closed"
    assert gateway_module._TRANSPORT_STATE["yws"]["active_connections"] == 0
    assert gateway_module._TRANSPORT_STATE["yws"]["open_total"] == 1
    assert gateway_module._TRANSPORT_STATE["yws"]["close_total"] == 1
    assert gateway_module._ACTIVE_YWS_CONNECTIONS == {}
    assert gateway_module._ACTIVE_YWS_CLIENTS == {}


def test_register_ws_event_subscriptions_installs_forwarder_once(monkeypatch) -> None:
    bus = _FakeBus()
    websocket = _FakeEventWebSocket()

    gateway_module._WS_EVENT_SUBSCRIBERS.clear()
    gateway_module._WS_EVENT_FORWARDER_INSTALLED = False
    monkeypatch.setattr(
        gateway_module,
        "get_agent_ctx",
        lambda: SimpleNamespace(bus=bus),
    )

    loop = asyncio.new_event_loop()
    try:
        added = gateway_module._register_ws_event_subscriptions(
            websocket,
            loop,
            ["core.update.status", "core.update.status"],
        )
        second = gateway_module._register_ws_event_subscriptions(
            websocket,
            loop,
            ["core.update.status"],
        )
    finally:
        loop.close()
        gateway_module._unregister_ws_event_subscriptions(websocket)
        gateway_module._WS_EVENT_SUBSCRIBERS.clear()
        gateway_module._WS_EVENT_FORWARDER_INSTALLED = False

    assert added == {"core.update.status"}
    assert second == set()
    assert [(prefix, getattr(handler, "__name__", "")) for prefix, handler in bus.subscriptions] == [
        ("*", "_forward_ws_bus_event")
    ]


def test_iter_initial_ws_event_messages_includes_hub_node_status(monkeypatch) -> None:
    bootstrap_module = types.ModuleType("adaos.services.bootstrap")
    bootstrap_module.load_config = lambda *args, **kwargs: SimpleNamespace(role="hub")
    bootstrap_module.is_ready = lambda *args, **kwargs: True
    monkeypatch.setitem(sys.modules, "adaos.services.bootstrap", bootstrap_module)
    from adaos.services.system_model import service as system_model_service

    monkeypatch.setattr(
        system_model_service,
        "current_node_status_push_payload",
        lambda: {
            "ready": True,
            "updated_at": 123.0,
            "heartbeat_interval_s": 5.0,
        },
    )
    monkeypatch.setattr(gateway_module.time, "time", lambda: 321.0)

    messages = gateway_module._iter_initial_ws_event_messages({"node.status"})

    assert messages == [
        {
            "ch": "events",
            "t": "evt",
            "kind": "node.status",
            "payload": {
                "ready": True,
                "updated_at": 123.0,
                "heartbeat_interval_s": 5.0,
            },
            "source": "node.status",
            "ts": 321.0,
        }
    ]


def test_iter_initial_ws_event_messages_includes_supervisor_raw_status(monkeypatch) -> None:
    from adaos.services import core_update as core_update_module

    monkeypatch.setattr(
        core_update_module,
        "read_public_update_status",
        lambda: {
            "ok": True,
            "status": {"state": "countdown", "phase": "scheduled"},
            "attempt": {"state": "planned"},
            "runtime": {"transition_mode": "warm_switch"},
            "_served_by": "supervisor",
        },
    )
    monkeypatch.setattr(gateway_module.time, "time", lambda: 654.0)

    messages = gateway_module._iter_initial_ws_event_messages({"supervisor.update.status.raw"})

    assert messages == [
        {
            "ch": "events",
            "t": "evt",
            "kind": "supervisor.update.status.raw",
            "payload": {
                "ok": True,
                "status": {"state": "countdown", "phase": "scheduled"},
                "attempt": {"state": "planned"},
                "runtime": {"transition_mode": "warm_switch"},
                "_served_by": "supervisor",
            },
            "source": "supervisor.update.status.raw",
            "ts": 654.0,
        }
    ]


def test_forward_ws_bus_event_delivers_core_update_status(monkeypatch) -> None:
    websocket = _FakeEventWebSocket()

    gateway_module._WS_EVENT_SUBSCRIBERS.clear()
    gateway_module._WS_EVENT_FORWARDER_INSTALLED = False

    loop = asyncio.new_event_loop()
    try:
        gateway_module._WS_EVENT_SUBSCRIBERS[id(websocket)] = {
            "websocket": websocket,
            "loop": loop,
            "topics": {"core.update.status"},
        }

        def _run_coro_threadsafe(coro, target_loop):  # noqa: ANN001
            assert target_loop is loop
            asyncio.run(coro)
            return SimpleNamespace()

        monkeypatch.setattr(
            gateway_module.asyncio,
            "run_coroutine_threadsafe",
            _run_coro_threadsafe,
        )

        gateway_module._forward_ws_bus_event(
            SimpleNamespace(
                type="core.update.status",
                payload={"state": "countdown"},
                source="supervisor",
                ts=321.0,
            )
        )
    finally:
        loop.close()
        gateway_module._WS_EVENT_SUBSCRIBERS.clear()

    assert websocket.messages == [
        {
            "ch": "events",
            "t": "evt",
            "kind": "core.update.status",
            "payload": {"state": "countdown"},
            "source": "supervisor",
            "ts": 321.0,
        }
    ]
