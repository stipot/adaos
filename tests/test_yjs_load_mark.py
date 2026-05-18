from __future__ import annotations

import sys
import types

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.services.yjs import load_mark as load_mark_module


def _reset_load_mark_state() -> None:
    load_mark_module._WEBSPACE_STATE.clear()
    load_mark_module._ACTIVE_STREAM_SUBSCRIPTIONS.clear()
    load_mark_module._ACTIVE_STREAM_SUBSCRIPTION_KEYS.clear()
    load_mark_module._LAST_STREAM_PUBLISH_AT.clear()
    load_mark_module._OWNER_ALERTS.clear()
    load_mark_module._STREAM_TICKER_STOP.set()
    load_mark_module._STREAM_TICKER_THREAD = None


def test_load_mark_tracks_owner_write_volume_and_rate() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=600,
        root_names=["data"],
        now_ts=10.0,
        source="sdk.web.yjs",
        owner="skill:weather_skill",
    )
    load_mark_module.record_write_update(
        "default",
        total_bytes=400,
        root_names=["data"],
        now_ts=10.2,
        source="sdk.web.yjs",
        owner="skill:weather_skill",
    )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=11.0)
    selected = snapshot["selected_webspace"]
    root_item = selected["roots"]["data"]
    owner_item = selected["owners"]["_by_owner/skill_weather_skill"]

    assert root_item["recent_writes"] == 2
    assert root_item["write_total"] == 2
    assert root_item["avg_wps"] > 0.0
    assert root_item["peak_wps"] >= 1.0
    assert owner_item["recent_writes"] == 2
    assert owner_item["write_total"] == 2
    assert owner_item["recent_bytes"] == 1000
    assert selected["active_owner_total"] == 1


def test_load_mark_surfaces_unknown_owner_for_unattributed_flow() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=256,
        now_ts=20.0,
        source="mystery_writer",
    )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=21.0)
    selected = snapshot["selected_webspace"]

    assert "_by_owner/unknown" in selected["owners"]
    assert selected["owners"]["_by_owner/unknown"]["recent_writes"] == 1
    assert selected["roots"]["_by_initiator/mystery_writer"]["recent_bytes"] == 256


def test_load_mark_stream_payload_includes_owner_and_root_rows() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=512,
        root_names=["data"],
        now_ts=30.0,
        source="sdk.web.yjs",
        owner="skill:adaos_connect",
    )

    rows = load_mark_module._stream_payload_items_locked("default", now_ts=31.0)

    owner_rows = [row for row in rows if row.get("kind") == "owner"]
    root_rows = [row for row in rows if row.get("kind") == "root"]

    assert owner_rows
    assert root_rows
    assert owner_rows[0]["display"] == "_by_owner/skill_adaos_connect"
    assert root_rows[0]["display"] == "data"


def test_load_mark_subscription_unwraps_nested_webspace_id() -> None:
    _reset_load_mark_state()

    load_mark_module.on_webio_stream_subscription_changed(
        {
            "receiver": "infrastate.yjs.load_mark",
            "webspace_id": {"webspace_id": "default"},
            "action": "subscribed",
        }
    )

    assert load_mark_module._ACTIVE_STREAM_SUBSCRIPTIONS == {"default": 1}


def test_load_mark_subscription_tracks_connection_specific_unsubscribe() -> None:
    _reset_load_mark_state()

    payload = {
        "receiver": "infrastate.yjs.load_mark",
        "webspace_id": "default",
        "topic": "webio.stream.default.infrastate.yjs.load_mark",
        "transport": "ws",
        "connection_id": "client-1",
        "subscription_id": "ws:client-1:webio.stream.default.infrastate.yjs.load_mark",
    }

    load_mark_module.on_webio_stream_subscription_changed({**payload, "action": "subscribed"})
    load_mark_module.on_webio_stream_subscription_changed({**payload, "action": "unsubscribed"})

    assert load_mark_module._ACTIVE_STREAM_SUBSCRIPTIONS == {}
    assert load_mark_module._ACTIVE_STREAM_SUBSCRIPTION_KEYS == {}


def test_load_mark_stream_payload_zeroes_stale_rows_between_snapshots() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=256,
        root_names=["data"],
        now_ts=40.0,
        source="sdk.web.yjs",
        owner="skill:test_skill",
    )

    fresh_rows = load_mark_module._stream_payload_items_locked("default", now_ts=40.1, last_published_at=0.0)
    stale_rows = load_mark_module._stream_payload_items_locked("default", now_ts=41.1, last_published_at=40.1)

    fresh_owner = next(row for row in fresh_rows if row.get("kind") == "owner")
    stale_owner = next(row for row in stale_rows if row.get("kind") == "owner")

    assert fresh_owner["recent_writes"] == 1
    assert fresh_owner["status"] != "idle"
    assert stale_owner["recent_writes"] == 0
    assert stale_owner["avg_bps"] == 0.0
    assert stale_owner["status"] == "idle"


def test_load_mark_stream_payload_adds_heartbeat_for_active_idle_subscription() -> None:
    _reset_load_mark_state()

    with load_mark_module._LOCK:
        load_mark_module._ACTIVE_STREAM_SUBSCRIPTIONS["default"] = 1

    rows = load_mark_module._stream_payload_items_locked("default", now_ts=45.0, last_published_at=44.0)
    mark_ts = load_mark_module._stream_heartbeat_mark_ts(45.0)

    assert rows == [
        {
            "kind": "activity",
            "id": "_activity/stream_heartbeat",
            "display": "stream heartbeat",
            "status": "nominal",
            "avg_bps": 0.0,
            "peak_bps": 0.0,
            "avg_wps": round(1.0 / max(float(load_mark_module._WINDOW_SEC), 1.0), 3),
            "peak_wps": round(1.0 / max(float(load_mark_module._BUCKET_SEC), 1.0), 3),
            "recent_bytes": 0,
            "recent_writes": 1,
            "lifetime_bytes": 0,
            "sample_total": 0,
            "write_total": 0,
            "current_size_bytes": 0,
            "byte_status": "idle",
            "write_status": "nominal",
            "last_source": "load_mark.stream_ticker",
            "last_channel": "infrastate.yjs.load_mark",
            "last_changed_at": mark_ts,
            "last_changed_ago_s": round(45.0 - mark_ts, 3),
            "webspace_id": "default",
        }
    ]


def test_load_mark_gateway_owner_peak_only_burst_does_not_warn(monkeypatch) -> None:
    _reset_load_mark_state()
    calls = []
    monkeypatch.setattr(load_mark_module, "_enqueue_owner_pressure_log", lambda *args: calls.append(args))

    load_mark_module.record_write_update(
        "default",
        total_bytes=64 * 1024,
        now_ts=50.0,
        source="yjs.gateway_ws",
        owner="gateway_ws",
    )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=51.0)
    owner_item = snapshot["selected_webspace"]["owners"]["_by_owner/gateway_ws"]

    assert owner_item["peak_bps"] >= float(load_mark_module._HIGH_BPS)
    assert not calls


def test_load_mark_gateway_tiny_sustained_writes_do_not_warn(monkeypatch) -> None:
    _reset_load_mark_state()
    calls = []
    monkeypatch.setattr(load_mark_module, "_enqueue_owner_pressure_log", lambda *args: calls.append(args))

    for index in range(540):
        load_mark_module.record_write_update(
            "default",
            total_bytes=2,
            now_ts=50.0 + (float(index) / 9.0),
            source="yjs.gateway_ws",
            owner="gateway_ws",
        )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=60.0)
    owner_item = snapshot["selected_webspace"]["owners"]["_by_owner/gateway_ws"]

    assert owner_item["avg_wps"] > float(load_mark_module._HIGH_WPS)
    assert owner_item["avg_bps"] < float(load_mark_module._HIGH_BPS)
    assert not calls


def test_load_mark_non_gateway_peak_burst_still_warns(monkeypatch) -> None:
    _reset_load_mark_state()
    calls = []
    monkeypatch.setattr(load_mark_module, "_enqueue_owner_pressure_log", lambda *args: calls.append(args))

    load_mark_module.record_write_update(
        "default",
        total_bytes=64 * 1024,
        now_ts=60.0,
        source="sdk.web.yjs",
        owner="skill:test_skill",
    )

    assert calls


def test_load_mark_primary_doc_policy_throttles_critical_owner_pressure() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=160 * 1024,
        root_names=["data"],
        now_ts=70.0,
        source="projection_service",
        owner="skill:infrastate_skill",
    )

    policy = load_mark_module.yjs_primary_doc_policy_snapshot(
        webspace_id="default",
        owner="skill:infrastate_skill",
        root_names=["data"],
        now_ts=71.0,
    )

    assert policy["owner"] == "_by_owner/skill_infrastate_skill"
    assert policy["observed_state"] == "critical"
    assert policy["policy_state"] == "throttle"
    assert policy["throttled_roots"] == ["data"]


def test_load_mark_primary_doc_policy_blocks_extreme_owner_pressure() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=320 * 1024,
        root_names=["data"],
        now_ts=72.0,
        source="projection_service",
        owner="skill:infrastate_skill",
    )

    policy = load_mark_module.yjs_primary_doc_policy_snapshot(
        webspace_id="default",
        owner="skill:infrastate_skill",
        root_names=["data"],
        now_ts=73.0,
    )

    assert policy["owner"] == "_by_owner/skill_infrastate_skill"
    assert policy["observed_state"] == "critical"
    assert policy["policy_state"] == "block"
    assert policy["reason"] == "write_amplification_blocked"
    assert policy["blocked_roots"] == ["data"]
