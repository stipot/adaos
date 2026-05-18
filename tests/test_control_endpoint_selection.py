from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

if "nats" not in sys.modules:
    sys.modules["nats"] = types.SimpleNamespace()
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.apps.cli import active_control
from adaos.services.subnet.link_client import MemberLinkClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = int(status_code)
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def test_probe_control_api_returns_runtime_ping_payload(monkeypatch) -> None:
    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/node/status"):
                raise RuntimeError("node status unavailable")
            if url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "candidate",
                            "runtime_instance_id": "rt-b-c-12345678",
                            "admin_mutation_allowed": False,
                        },
                    },
                )
            raise AssertionError(url)

    monkeypatch.setattr(active_control.requests, "Session", _FakeSession)

    code, payload = active_control.probe_control_api(
        base_url="http://127.0.0.1:8778",
        token="dev-local-token",
        timeout_s=0.2,
    )

    assert code == 200
    assert payload["runtime"]["transition_role"] == "candidate"
    assert payload["runtime"]["admin_mutation_allowed"] is False


def test_looks_like_control_api_response_rejects_candidate_runtime_payload() -> None:
    assert (
        active_control._looks_like_control_api_response(
            200,
            {
                "ok": True,
                "runtime": {
                    "transition_role": "candidate",
                    "admin_mutation_allowed": False,
                },
            },
        )
        is False
    )
    assert (
        active_control._looks_like_control_api_response(
            200,
            {
                "ok": True,
                "runtime": {
                    "transition_role": "active",
                    "admin_mutation_allowed": True,
                },
            },
        )
        is True
    )


def test_resolve_control_base_url_skips_candidate_ping_candidates(monkeypatch) -> None:
    monkeypatch.setattr(active_control, "_node_config_control_url", lambda: ("hub", None))
    monkeypatch.setattr(active_control, "_pick_env_override_url", lambda: "")
    monkeypatch.setattr(active_control, "_pick_local_env_url", lambda: "")
    monkeypatch.setattr(active_control, "_autostart_control_url", lambda: "")
    monkeypatch.setattr(active_control, "_supervisor_public_runtime_url", lambda: None)
    monkeypatch.setattr(active_control, "_pidfile_control_urls", lambda: [])
    monkeypatch.setattr(active_control, "resolve_control_token", lambda *args, **kwargs: "dev-local-token")

    def _probe(*, base_url: str, token: str, timeout_s: float = 2.0):
        if base_url.endswith(":8778"):
            return 200, {"ok": True, "runtime": {"transition_role": "candidate", "admin_mutation_allowed": False}}
        if base_url.endswith(":8777"):
            return 200, {"ok": True, "runtime": {"transition_role": "active", "admin_mutation_allowed": True}}
        return None, None

    monkeypatch.setattr(active_control, "probe_control_api", _probe)

    base = active_control.resolve_control_base_url()

    assert base == "http://127.0.0.1:8777"


def test_resolve_control_base_url_prefer_local_ignores_member_hub_url(monkeypatch) -> None:
    monkeypatch.setattr(active_control, "_node_config_control_url", lambda: ("member", "https://ru.api.inimatic.com"))
    monkeypatch.setattr(active_control, "_pick_env_override_url", lambda: "https://ru.api.inimatic.com")
    monkeypatch.setattr(active_control, "_pick_local_env_url", lambda: "http://127.0.0.1:8778")
    monkeypatch.setattr(active_control, "_autostart_control_url", lambda: "")
    monkeypatch.setattr(active_control, "_supervisor_public_runtime_url", lambda: None)
    monkeypatch.setattr(active_control, "_pidfile_control_urls", lambda: [])
    monkeypatch.setattr(active_control, "resolve_control_token", lambda *args, **kwargs: "dev-local-token")

    def _probe(*, base_url: str, token: str, timeout_s: float = 2.0):
        if base_url.endswith(":8778"):
            return 200, {"ok": True, "runtime": {"transition_role": "active", "admin_mutation_allowed": True}}
        return None, None

    monkeypatch.setattr(active_control, "probe_control_api", _probe)

    base = active_control.resolve_control_base_url(prefer_local=True)

    assert base == "http://127.0.0.1:8778"


def test_resolve_control_base_url_prefers_supervisor_public_runtime_url(monkeypatch) -> None:
    monkeypatch.setattr(active_control, "_node_config_control_url", lambda: ("hub", None))
    monkeypatch.setattr(active_control, "_pick_env_override_url", lambda: "")
    monkeypatch.setattr(active_control, "_pick_local_env_url", lambda: "http://127.0.0.1:8778")
    monkeypatch.setattr(active_control, "_autostart_control_url", lambda: "")
    monkeypatch.setattr(active_control, "_supervisor_public_runtime_url", lambda: "http://127.0.0.1:8777")
    monkeypatch.setattr(active_control, "_pidfile_control_urls", lambda: [])
    monkeypatch.setattr(active_control, "resolve_control_token", lambda *args, **kwargs: "dev-local-token")

    def _probe(*, base_url: str, token: str, timeout_s: float = 2.0):
        if base_url.endswith(":8777"):
            return 200, {"ok": True, "runtime": {"transition_role": "active", "admin_mutation_allowed": True}}
        if base_url.endswith(":8778"):
            return 200, {"ok": True, "runtime": {"transition_role": "candidate", "admin_mutation_allowed": False}}
        return None, None

    monkeypatch.setattr(active_control, "probe_control_api", _probe)

    base = active_control.resolve_control_base_url(prefer_local=True)

    assert base == "http://127.0.0.1:8777"


def test_resolve_control_token_prefers_candidate_that_authenticates_with_local_control(monkeypatch) -> None:
    monkeypatch.setattr(
        active_control,
        "_control_token_candidates",
        lambda explicit=None: ["stale-config-token", "wrapper-service-token"],
    )

    def _probe(*, base_url: str, token: str, timeout_s: float = 0.5):
        assert base_url == "http://127.0.0.1:8777"
        return 200 if token == "wrapper-service-token" else 401

    monkeypatch.setattr(active_control, "_probe_control_token_status", _probe)

    token = active_control.resolve_control_token(
        explicit="stale-config-token",
        base_url="http://127.0.0.1:8777",
    )

    assert token == "wrapper-service-token"


def test_member_link_resolve_local_control_base_skips_candidate_ping(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_SUPERVISOR_URL", raising=False)
    monkeypatch.delenv("ADAOS_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("ADAOS_CONTROL_URL", raising=False)
    monkeypatch.delenv("ADAOS_CONTROL_BASE", raising=False)

    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/supervisor/public/update-status"):
                return _FakeResponse(503)
            if url.startswith("http://127.0.0.1:8777") and url.endswith("/api/ping"):
                raise RuntimeError("active port down")
            if url.startswith("http://127.0.0.1:8778") and url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "candidate",
                            "admin_mutation_allowed": False,
                        },
                    },
                )
            if url.startswith("http://127.0.0.1:8779") and url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "active",
                            "admin_mutation_allowed": True,
                        },
                    },
                )
            raise RuntimeError(f"unexpected url: {url}")

    monkeypatch.setattr("adaos.services.subnet.link_client.requests.Session", _FakeSession)

    base = MemberLinkClient._resolve_local_control_base()

    assert base == "http://127.0.0.1:8779"


def test_member_link_post_local_admin_resolves_token_for_selected_base(monkeypatch) -> None:
    monkeypatch.setattr(MemberLinkClient, "_resolve_local_control_base", staticmethod(lambda: "http://127.0.0.1:8779"))
    monkeypatch.setattr(
        "adaos.services.subnet.link_client.resolve_control_token",
        lambda *, explicit=None, base_url=None: "wrapper-service-token" if base_url == "http://127.0.0.1:8779" else "stale-config-token",
    )

    class _FakeSession:
        trust_env = False

        def post(self, url: str, headers=None, json=None, timeout=None):
            assert url == "http://127.0.0.1:8779/api/admin/update/start"
            assert headers["X-AdaOS-Token"] == "wrapper-service-token"
            return _FakeResponse(200, {"ok": True, "accepted": True})

    monkeypatch.setattr("adaos.services.subnet.link_client.requests.Session", _FakeSession)

    payload = MemberLinkClient._post_local_admin("/api/admin/update/start", {"reason": "test"})

    assert payload["ok"] is True
    assert payload["accepted"] is True


def test_member_link_client_does_not_reemit_hub_mirrored_events(monkeypatch) -> None:
    class _FakeBus:
        def __init__(self) -> None:
            self.subscriber = None
            self.published = []

        def subscribe(self, prefix, handler) -> None:
            assert prefix == "*"
            self.subscriber = handler

        def publish(self, event) -> None:
            self.published.append(event)

    fake_bus = _FakeBus()
    fake_ctx = types.SimpleNamespace(bus=fake_bus, config=types.SimpleNamespace(node_id="member-1"))
    monkeypatch.setattr("adaos.services.subnet.link_client.get_ctx", lambda: fake_ctx)

    client = MemberLinkClient()
    client._connected.set()
    client._bus_prefixes = None
    client._hub_node_id = "hub-1"
    client._ensure_bus_subscription()

    asyncio.run(
        client._on_hub_event(
            {
                "event": {
                    "type": "node.status",
                    "payload": {"ready": True},
                    "source": "lifecycle",
                    "ts": 123.0,
                }
            }
        )
    )

    assert fake_bus.subscriber is not None
    assert len(fake_bus.published) == 1
    mirrored = fake_bus.published[0]
    assert mirrored.payload["_meta"]["subnet_hub_mirrored"] is True
    assert mirrored.payload["_meta"]["subnet_hub_node_id"] == "hub-1"

    fake_bus.subscriber(mirrored)

    assert client._out_q.empty()


@pytest.mark.parametrize(
    ("event_type", "reason"),
    [
        ("desktop.webspace.reload", "desktop.webspace.reload"),
        ("desktop.webspace.reloaded", "desktop.webspace.reloaded"),
        ("desktop.webspace.reset", "desktop.webspace.reset"),
    ],
)
def test_member_link_client_requests_local_snapshot_sync_on_desktop_rebuild_events(
    monkeypatch,
    event_type: str,
    reason: str,
) -> None:
    client = MemberLinkClient()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        client,
        "_request_local_snapshot_sync",
        lambda **kwargs: calls.append(dict(kwargs)),
    )

    asyncio.run(
        client._on_hub_event(
            {
                "event": {
                    "type": event_type,
                    "payload": {"webspace_id": "default"},
                    "source": "hub",
                    "ts": 123.0,
                }
            }
        )
    )

    assert calls == [{"webspace_id": "default", "reason": reason}]


def test_member_link_client_persists_node_display_assignment(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "adaos.services.subnet.link_client.save_node_runtime_state",
        lambda **kwargs: calls.append(dict(kwargs)),
    )

    client = MemberLinkClient()

    asyncio.run(
        client._on_node_display_assignment(
            {
                "node_display": {
                    "node_index": 4,
                    "node_color_index": 6,
                    "node_label": "Edge One",
                    "node_compact_label": "N4",
                    "node_color": "#B07AA1",
                }
            }
        )
    )

    assert calls == [
        {
            "node_display": {
                "display_index": 4,
                "accent_index": 6,
                "node_label": "Edge One",
                "node_compact_label": "N4",
                "node_color": "#B07AA1",
            }
        }
    ]


@pytest.mark.asyncio
async def test_member_link_start_clears_stop_and_restarts_finished_task() -> None:
    client = MemberLinkClient()
    calls: list[bool] = []

    async def _fake_run() -> None:
        calls.append(client._stop.is_set())

    client._run = _fake_run  # type: ignore[method-assign]
    client._stop.set()

    await client.start()
    first_task = client._task
    assert first_task is not None
    await first_task

    await client.start()
    second_task = client._task
    assert second_task is not None
    assert second_task is not first_task
    await second_task

    assert calls == [False, False]


def test_member_link_is_connected_treats_stale_activity_as_disconnected(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr("adaos.services.subnet.link_client.time.time", lambda: now["value"])

    client = MemberLinkClient()
    client._connected.set()
    client._connected_at = 50.0
    client._last_message_at = 50.0
    client._last_pong_at = 50.0
    monkeypatch.setattr(client, "_pong_stale_after_s", lambda: 35.0)

    assert not client.is_connected()


@pytest.mark.asyncio
async def test_member_link_ping_loop_exits_when_pong_goes_stale(monkeypatch) -> None:
    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    now = {"value": 100.0}

    async def _fake_sleep(_seconds: float) -> None:
        now["value"] += 10.0

    monkeypatch.setattr("adaos.services.subnet.link_client.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr("adaos.services.subnet.link_client.time.time", lambda: now["value"])

    client = MemberLinkClient()
    client._ws_url = "ws://hub/ws/subnet"
    client._connected_at = 100.0
    client._last_pong_at = 100.0
    monkeypatch.setattr(client, "_pong_stale_after_s", lambda: 15.0)

    ws = _FakeWs()

    await client._ping_loop(ws)

    assert ws.sent == [json.dumps({"t": "ping", "ts": 110.0})]


@pytest.mark.asyncio
async def test_member_link_queue_node_snapshot_prefers_async_snapshot_builder(monkeypatch) -> None:
    client = MemberLinkClient()
    client._loop = asyncio.get_running_loop()

    async def _fake_snapshot_async() -> dict[str, object]:
        await asyncio.sleep(0)
        return {"mode": "async"}

    monkeypatch.setattr(client, "_local_node_snapshot_async", _fake_snapshot_async)
    monkeypatch.setattr(
        client,
        "_local_node_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("sync snapshot should not run while the event loop is active")),
    )

    client._queue_node_snapshot()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    msg = await asyncio.wait_for(client._out_q.get(), timeout=1.0)

    assert msg["t"] == "node.snapshot"
    assert msg["snapshot"] == {"mode": "async"}


def test_member_link_queues_snapshot_after_desktop_yjs_write(monkeypatch) -> None:
    client = MemberLinkClient()
    queued: list[str] = []
    monkeypatch.setenv("ADAOS_SUBNET_FULL_SNAPSHOT_ON_YJS_WRITE", "1")

    monkeypatch.setattr(client, "_queue_node_snapshot", lambda: queued.append("snapshot"))

    client._queue_node_snapshot_from_yjs_write(webspace_id="desktop", meta={"source": "webspace_runtime"})

    assert queued == ["snapshot"]


def test_member_link_throttles_snapshot_after_yjs_write(monkeypatch) -> None:
    client = MemberLinkClient()
    queued: list[str] = []
    now = {"value": 100.0}
    monkeypatch.setenv("ADAOS_SUBNET_FULL_SNAPSHOT_ON_YJS_WRITE", "1")

    monkeypatch.setattr(client, "_queue_node_snapshot", lambda: queued.append("snapshot"))
    monkeypatch.setattr("adaos.services.subnet.link_client.time.time", lambda: now["value"])

    client._queue_node_snapshot_from_yjs_write(webspace_id="desktop", meta={"source": "webspace_runtime"})
    client._queue_node_snapshot_from_yjs_write(webspace_id="desktop", meta={"source": "webspace_runtime"})
    now["value"] += 2.0
    client._queue_node_snapshot_from_yjs_write(webspace_id="desktop", meta={"source": "webspace_runtime"})
    client._queue_node_snapshot_from_yjs_write(webspace_id="project-ws", meta={"source": "webspace_runtime"})

    assert queued == ["snapshot"]
