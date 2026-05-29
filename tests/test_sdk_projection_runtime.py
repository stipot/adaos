from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.sdk.data.projections import (
    DirtyRouter,
    ProjectionRuntime,
    ProjectionSlot,
    SectionCache,
    StreamReceiver,
    StreamRuntime,
    clear_projection_demand,
    has_projection_demand,
    register_projection_dispatcher_handlers,
    restore_active_projection_demand,
    stable_payload_fingerprint,
    unregister_projection_dispatcher_handlers,
)
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import (
    clear_projection_dispatcher,
    dispatch_demanded_projection_refresh,
    registered_projection_refresh_handlers,
)
from adaos.services.projection_records import clear_projection_record_registry, get_projection_record


class _FakeSubnet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str | None]] = []

    async def set_async(self, slot: str, value: object, *, webspace_id: str | None = None) -> None:
        self.calls.append((slot, value, webspace_id))


@dataclass
class _Payload:
    name: str
    values: list[int]


@pytest.fixture(autouse=True)
def _clear_projection_demand_registry() -> None:
    clear_projection_demand()
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_projection_record_registry()
    yield
    clear_projection_demand()
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_projection_record_registry()


def test_stable_payload_fingerprint_is_order_independent_for_mappings() -> None:
    left = {"b": 2, "a": {"z": 1, "y": [3, 2, 1]}}
    right = {"a": {"y": [3, 2, 1], "z": 1}, "b": 2}

    assert stable_payload_fingerprint(left) == stable_payload_fingerprint(right)
    assert stable_payload_fingerprint(_Payload("demo", [1, 2])) == stable_payload_fingerprint(
        {"name": "demo", "values": [1, 2]}
    )


def test_projection_runtime_writes_once_and_skips_identical_even_when_forced() -> None:
    subnet = _FakeSubnet()
    runtime = ProjectionRuntime("browsers_skill", ctx_subnet=subnet)
    slot = ProjectionSlot("browsers.summary", "data/browsers/summary")

    first = asyncio.run(runtime.set_if_changed(slot, {"count": 1}, webspace_id="desktop"))
    second = asyncio.run(runtime.set_if_changed(slot, {"count": 1}, webspace_id="desktop"))
    forced = asyncio.run(
        runtime.set_if_changed(slot, {"count": 1}, webspace_id="desktop", force=True, reason="reload")
    )

    assert first.written is True
    assert second.skipped is True
    assert forced.skipped is True
    assert forced.force is True
    assert subnet.calls == [("browsers.summary", {"count": 1}, "desktop")]

    diagnostics = runtime.diagnostics_snapshot()
    assert diagnostics["applied_total"] == 1
    assert diagnostics["skipped_unchanged_total"] == 2
    assert diagnostics["by_slot"]["browsers.summary"]["applied_total"] == 1


def test_projection_runtime_tracks_webspace_state_independently() -> None:
    subnet = _FakeSubnet()
    runtime = ProjectionRuntime("browsers_skill", ctx_subnet=subnet)

    asyncio.run(runtime.set_if_changed("browsers.summary", {"count": 1}, webspace_id="desktop"))
    asyncio.run(runtime.set_if_changed("browsers.summary", {"count": 1}, webspace_id="ops"))
    asyncio.run(runtime.set_if_changed("browsers.summary", {"count": 1}, webspace_id="desktop"))

    assert subnet.calls == [
        ("browsers.summary", {"count": 1}, "desktop"),
        ("browsers.summary", {"count": 1}, "ops"),
    ]
    assert runtime.diagnostics_snapshot()["fingerprint_entries"] == 2


def test_projection_runtime_writes_when_payload_changes() -> None:
    subnet = _FakeSubnet()
    runtime = ProjectionRuntime("infrastate_skill", ctx_subnet=subnet)

    first = asyncio.run(runtime.set_if_changed("infrastate.summary", {"state": "ok"}, webspace_id="desktop"))
    second = asyncio.run(runtime.set_if_changed("infrastate.summary", {"state": "warn"}, webspace_id="desktop"))

    assert first.written is True
    assert second.written is True
    assert [call[1] for call in subnet.calls] == [{"state": "ok"}, {"state": "warn"}]


def test_projection_runtime_rate_limits_changed_payloads_per_slot() -> None:
    subnet = _FakeSubnet()
    clock = [100.0]
    runtime = ProjectionRuntime("infrastate_skill", ctx_subnet=subnet, clock=lambda: clock[0])
    slot = ProjectionSlot("infrastate.summary", "data/infrastate/summary", min_interval_s=5.0)

    first = asyncio.run(runtime.set_if_changed(slot, {"state": "ok"}, webspace_id="desktop"))
    clock[0] = 101.0
    limited = asyncio.run(runtime.set_if_changed(slot, {"state": "warn"}, webspace_id="desktop"))
    clock[0] = 106.0
    changed = asyncio.run(runtime.set_if_changed(slot, {"state": "warn"}, webspace_id="desktop"))

    assert first.written is True
    assert limited.throttled is True
    assert limited.reason == "rate_limited"
    assert changed.written is True
    assert [call[1] for call in subnet.calls] == [{"state": "ok"}, {"state": "warn"}]
    assert runtime.diagnostics_snapshot()["throttled_total"] == 1


def test_dirty_router_matches_exact_and_prefix_patterns() -> None:
    router = (
        DirtyRouter()
        .on("operations.*")
        .dirty("operations.active")
        .on("browser.session.changed", "device.registered")
        .dirty("runtime.status", "summary")
    )

    assert router.dirty_for("operations.started") == {"operations.active"}
    assert router.dirty_for("browser.session.changed") == {"runtime.status", "summary"}
    assert router.dirty_for("skills.registry.changed") == set()


def test_stream_receiver_is_declarative_type() -> None:
    receiver = StreamReceiver("infrastate.logs.recent", min_interval_s=0.5)

    assert receiver.name == "infrastate.logs.recent"
    assert receiver.min_interval_s == 0.5


def test_projection_runtime_refresh_dirty_uses_slot_events() -> None:
    subnet = _FakeSubnet()
    runtime = ProjectionRuntime(
        "browsers_skill",
        ctx_subnet=subnet,
        projections=[
            ProjectionSlot(
                "browsers.summary",
                "data/browsers/summary",
                build=lambda context: {"topic": context.event_topic},
                events=("browser.*",),
            )
        ],
    )

    result = asyncio.run(runtime.refresh_dirty("browser.session.changed", webspace_id="desktop"))

    assert result.sections == ("browsers.summary",)
    assert result.results[0].written is True
    assert subnet.calls == [("browsers.summary", {"topic": "browser.session.changed"}, "desktop")]


def test_projection_runtime_coalesces_concurrent_refreshes() -> None:
    subnet = _FakeSubnet()
    build_calls = 0

    async def _run() -> tuple[object, object]:
        nonlocal build_calls

        async def _build(_context):
            nonlocal build_calls
            build_calls += 1
            await asyncio.sleep(0.01)
            return {"build_calls": build_calls}

        runtime = ProjectionRuntime(
            "browsers_skill",
            ctx_subnet=subnet,
            projections=[ProjectionSlot("browsers.summary", build=_build)],
        )
        return await asyncio.gather(
            runtime.refresh_sections(["browsers.summary"], webspace_id="desktop"),
            runtime.refresh_sections(["browsers.summary"], webspace_id="desktop"),
        )

    first, second = asyncio.run(_run())

    assert build_calls == 1
    assert subnet.calls == [("browsers.summary", {"build_calls": 1}, "desktop")]
    assert {first.coalesced, second.coalesced} == {False, True}


def test_projection_runtime_records_event_pressure_counters() -> None:
    subnet = _FakeSubnet()
    build_calls = 0

    async def _run() -> dict[str, object]:
        nonlocal build_calls

        async def _build(_context):
            nonlocal build_calls
            build_calls += 1
            await asyncio.sleep(0.01)
            return {"build_calls": build_calls}

        runtime = ProjectionRuntime(
            "browsers_skill",
            ctx_subnet=subnet,
            projections=[
                ProjectionSlot(
                    "browsers.summary",
                    build=_build,
                    events=("browser.*",),
                )
            ],
        )
        await runtime.refresh_dirty("skills.registry.changed", webspace_id="desktop")
        await asyncio.gather(
            runtime.refresh_dirty("browser.session.changed", webspace_id="desktop"),
            runtime.refresh_dirty("browser.session.changed", webspace_id="desktop"),
        )
        return runtime.diagnostics_snapshot()

    diagnostics = asyncio.run(_run())
    by_event = diagnostics["by_event"]

    assert diagnostics["refresh_requested_total"] == 3
    assert diagnostics["refresh_started_total"] == 1
    assert diagnostics["refresh_coalesced_total"] == 1
    assert diagnostics["refresh_no_dirty_total"] == 1
    assert diagnostics["refresh_superseded_total"] == 0
    assert diagnostics["refresh_dropped_total"] == 0
    assert by_event["skills.registry.changed"]["no_dirty_total"] == 1
    assert by_event["browser.session.changed"]["requested_total"] == 2
    assert by_event["browser.session.changed"]["started_total"] == 1
    assert by_event["browser.session.changed"]["coalesced_total"] == 1
    assert by_event["browser.session.changed"]["last_sections"] == ["browsers.summary"]
    assert diagnostics["last_refresh_event"]["topic"] == "browser.session.changed"


def test_projection_runtime_registers_dispatcher_handlers_and_restores_demand() -> None:
    runtime = ProjectionRuntime(
        "demo_skill",
        projections=[
            ProjectionSlot(
                "status-card:runtime",
                build=lambda context: {"summary": context.webspace_id},
                kind="status-card",
                audience="shared",
            )
        ],
    )
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )

    registered = runtime.register_dispatcher_handlers()
    restored = runtime.restore_active_demand(webspace_id="desktop")
    report = asyncio.run(
        dispatch_demanded_projection_refresh(
            {
                "type": "demo.event",
                "payload": {"webspace_id": "desktop"},
                "source": "test",
                "ts": 20.0,
            }
        )
    )
    stored = get_projection_record(webspace_id="desktop", projection_key="status-card:runtime")

    assert registered == ["status-card:runtime"]
    assert registered_projection_refresh_handlers() == ["status-card:runtime"]
    assert restored["restored_total"] == 1
    assert report.refreshed[0].status == "ready"
    assert stored is not None
    assert stored.data == {"summary": "desktop"}
    assert runtime.diagnostics_snapshot()["dispatcher_handlers"] == ["status-card:runtime"]
    assert runtime.unregister_dispatcher_handlers() == ["status-card:runtime"]
    assert registered_projection_refresh_handlers() == []


def test_projection_runtime_module_helpers_register_restore_and_unregister() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:helper",
                    consumer_id="widget:helper",
                    consumer_kind="widget",
                )
            ],
        )
    )

    registered = register_projection_dispatcher_handlers(
        "helper_skill",
        projections=[
            ProjectionSlot(
                "status-card:helper",
                build=lambda: {"summary": "helper"},
                kind="status-card",
            )
        ],
    )
    restored = restore_active_projection_demand("helper_skill", webspace_id="desktop")
    removed = unregister_projection_dispatcher_handlers("helper_skill")

    assert registered == ["status-card:helper"]
    assert restored["restored_total"] == 1
    assert removed == ["status-card:helper"]


def test_section_cache_expires_and_invalidates_by_webspace() -> None:
    clock = [10.0]
    cache = SectionCache(default_ttl_s=5.0, max_entries=4, clock=lambda: clock[0])

    cache.set("summary", {"state": "ok"}, webspace_id="desktop")
    cache.set("summary", {"state": "ops"}, webspace_id="ops")

    assert cache.get("summary", webspace_id="desktop") == {"state": "ok"}
    clock[0] = 16.0
    assert cache.get("summary", webspace_id="desktop") is None
    assert cache.invalidate(webspace_id="ops") == 1
    assert cache.get("summary", webspace_id="ops") is None


def test_stream_runtime_tracks_receivers_dedupes_and_rate_limits() -> None:
    calls: list[tuple[str, object, dict]] = []
    clock = [100.0]

    def _publish(receiver, data, *, ts=None, _meta=None):  # noqa: ANN001
        calls.append((receiver, data, dict(_meta or {})))
        return {"ok": True}

    runtime = StreamRuntime(
        "infrastate_skill",
        receivers=[StreamReceiver("infrastate.logs.recent", min_interval_s=5.0)],
        stream_publish=_publish,
        clock=lambda: clock[0],
    )

    first = runtime.publish_snapshot("infrastate.logs.recent", {"lines": [1]}, webspace_id="desktop")
    unchanged = runtime.publish_snapshot("infrastate.logs.recent", {"lines": [1]}, webspace_id="desktop")
    clock[0] = 101.0
    limited = runtime.publish_snapshot("infrastate.logs.recent", {"lines": [2]}, webspace_id="desktop")
    clock[0] = 106.0
    changed = runtime.publish_snapshot("infrastate.logs.recent", {"lines": [2]}, webspace_id="desktop")

    assert first.published is True
    assert unchanged.skipped is True
    assert unchanged.reason == "unchanged"
    assert limited.rate_limited is True
    assert changed.published is True
    assert calls == [
        (
            "infrastate.logs.recent",
            {"lines": [1]},
            {
                "webspace_id": "desktop",
                "owner": "skill:infrastate_skill",
                "skill_id": "infrastate_skill",
                "skill_name": "infrastate_skill",
            },
        ),
        (
            "infrastate.logs.recent",
            {"lines": [2]},
            {
                "webspace_id": "desktop",
                "owner": "skill:infrastate_skill",
                "skill_id": "infrastate_skill",
                "skill_name": "infrastate_skill",
            },
        ),
    ]
    assert runtime.active_receivers_snapshot() == [
        {"webspace_id": "desktop", "receiver": "infrastate.logs.recent"}
    ]
    runtime.forget_receiver("infrastate.logs.recent", webspace_id="desktop")
    assert runtime.active_receivers_snapshot() == []


def test_stream_runtime_handles_snapshot_requested_event() -> None:
    calls: list[tuple[str, object, dict]] = []

    def _publish(receiver, data, *, ts=None, _meta=None):  # noqa: ANN001
        calls.append((receiver, data, dict(_meta or {})))
        return {"ok": True}

    runtime = StreamRuntime(
        "browsers_skill",
        receivers=[
            StreamReceiver(
                "browsers.devices",
                build=lambda context: {
                    "receiver": context.receiver,
                    "webspace": context.webspace_id,
                    "reason": context.reason,
                },
            )
        ],
        stream_publish=_publish,
    )

    result = runtime.handle_snapshot_requested(
        {
            "receiver": "browsers.devices",
            "_meta": {"webspace_id": "desktop"},
        },
        receiver_prefix="browsers.",
    )

    assert result is not None
    assert result.published is True
    assert calls == [
        (
            "browsers.devices",
            {"receiver": "browsers.devices", "webspace": "desktop", "reason": "snapshot_requested"},
            {
                "webspace_id": "desktop",
                "owner": "skill:browsers_skill",
                "skill_id": "browsers_skill",
                "skill_name": "browsers_skill",
            },
        )
    ]


def test_stream_runtime_handles_subscription_changed_unsubscribed() -> None:
    calls: list[tuple[str, object, dict]] = []
    runtime = StreamRuntime(
        "browsers_skill",
        receivers=[StreamReceiver("browsers.devices", build=lambda _context: {"items": []})],
        stream_publish=lambda receiver, data, *, ts=None, _meta=None: calls.append((receiver, data, dict(_meta or {}))) or {"ok": True},
    )

    runtime.handle_subscription_changed(
        {"receiver": "browsers.devices", "action": "subscribed", "webspace_id": "desktop"},
        receiver_prefix="browsers.",
    )
    runtime.handle_subscription_changed(
        {"receiver": "browsers.devices", "action": "unsubscribed", "webspace_id": "desktop"},
        receiver_prefix="browsers.",
    )

    assert len(calls) == 1
    assert runtime.active_receivers_snapshot() == []
