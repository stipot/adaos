from __future__ import annotations

import asyncio
from dataclasses import dataclass

from adaos.sdk.data.projections import (
    DirtyRouter,
    ProjectionRuntime,
    ProjectionSlot,
    SectionCache,
    StreamReceiver,
    StreamRuntime,
    stable_payload_fingerprint,
)


class _FakeSubnet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str | None]] = []

    async def set_async(self, slot: str, value: object, *, webspace_id: str | None = None) -> None:
        self.calls.append((slot, value, webspace_id))


@dataclass
class _Payload:
    name: str
    values: list[int]


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
        ("infrastate.logs.recent", {"lines": [1]}, {"webspace_id": "desktop"}),
        ("infrastate.logs.recent", {"lines": [2]}, {"webspace_id": "desktop"}),
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
            {"webspace_id": "desktop"},
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
