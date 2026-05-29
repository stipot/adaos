import asyncio

import pytest

from adaos.domain import Event
import adaos.services.eventbus as eventbus_module
from adaos.services.eventbus import LocalEventBus


@pytest.mark.asyncio
async def test_local_event_bus_waits_for_async_handlers():
    bus = LocalEventBus()
    seen: list[str] = []

    async def handler(event: Event):
        await asyncio.sleep(0.05)
        seen.append(event.type)

    bus.subscribe("subnet.", handler)
    bus.publish(Event(type="subnet.stopping", payload={}, source="test", ts=0.0))

    ok = await bus.wait_for_idle(timeout=1.0)

    assert ok is True
    assert seen == ["subnet.stopping"]


def test_local_event_bus_subscribe_debug_is_quiet_by_default(monkeypatch):
    monkeypatch.delenv("ADAOS_EVENTBUS_TRACE_SUBSCRIBE", raising=False)
    messages: list[str] = []
    monkeypatch.setattr(eventbus_module._log, "debug", lambda message, *args, **kwargs: messages.append(str(message)))
    bus = LocalEventBus()

    def handler(event: Event):
        return None

    bus.subscribe("subnet.", handler)

    assert not messages


def test_local_event_bus_subscribe_debug_can_be_enabled(monkeypatch):
    monkeypatch.setenv("ADAOS_EVENTBUS_TRACE_SUBSCRIBE", "1")
    messages: list[str] = []
    monkeypatch.setattr(eventbus_module._log, "debug", lambda message, *args, **kwargs: messages.append(str(message)))
    bus = LocalEventBus()

    def handler(event: Event):
        return None

    bus.subscribe("subnet.", handler)

    assert any("bus.subscribe" in message for message in messages)


def test_browser_session_changed_is_bounded_by_default(monkeypatch):
    monkeypatch.delenv("ADAOS_EVENTBUS_BOUNDED_TOPICS", raising=False)
    monkeypatch.delenv("ADAOS_EVENTBUS_SUPERSEDE_BY_HANDLER_TOPICS", raising=False)

    bus = LocalEventBus()
    snapshot = bus.backlog_snapshot()

    assert "browser.session.changed" in snapshot["bounded_topics"]


@pytest.mark.asyncio
async def test_browser_session_changed_supersedes_queued_handler_work(monkeypatch):
    monkeypatch.delenv("ADAOS_EVENTBUS_BOUNDED_TOPICS", raising=False)
    monkeypatch.delenv("ADAOS_EVENTBUS_SUPERSEDE_BY_HANDLER_TOPICS", raising=False)
    bus = LocalEventBus()
    release = asyncio.Event()
    seen: list[int] = []

    async def handler(event: Event):
        seen.append(int(event.payload.get("seq") or 0))
        await release.wait()

    bus.subscribe("browser.session.changed", handler)
    for seq in range(5):
        bus.publish(
            Event(
                type="browser.session.changed",
                payload={
                    "webspace_id": "desktop",
                    "device_id": "dev-1",
                    "seq": seq,
                },
                source="test",
                ts=0.0,
            )
        )

    snapshot = bus.backlog_snapshot()
    superseded = dict(snapshot["top_bounded_superseded_types"])

    assert snapshot["bounded_queue_total"] <= 1
    assert superseded["browser.session.changed"] >= 4

    release.set()
    ok = await bus.wait_for_idle(timeout=1.0)

    assert ok is True
    assert seen == [4]


@pytest.mark.asyncio
async def test_local_event_bus_reports_webio_stream_control_pressure():
    bus = LocalEventBus()
    seen: list[str] = []

    async def handler(event: Event):
        await asyncio.sleep(0.05)
        seen.append(str(event.payload.get("stream_id") or ""))

    bus.subscribe("webio.stream.snapshot.requested", handler)
    for _idx in range(3):
        bus.publish(
            Event(
                type="webio.stream.snapshot.requested",
                payload={
                    "webspace_id": "desktop",
                    "target_node_id": "node-1",
                    "stream_id": "infrastate.realtime",
                    "source": "events_ws",
                },
                source="test",
                ts=0.0,
            )
        )

    snapshot = bus.backlog_snapshot()
    controls = snapshot["top_webio_stream_controls"]
    row = next(item for item in controls if item["receiver"] == "infrastate.realtime")

    assert row["event_type"] == "webio.stream.snapshot.requested"
    assert row["webspace_id"] == "desktop"
    assert row["source"] == "events_ws"
    assert row["incoming_total"] == 3
    assert row["queued_total"] == 3
    assert row["superseded_total"] >= 1

    ok = await bus.wait_for_idle(timeout=1.0)
    assert ok is True
    assert seen == ["infrastate.realtime"]


@pytest.mark.asyncio
async def test_local_event_bus_preserves_each_webio_stream_control_handler(monkeypatch):
    monkeypatch.delenv("ADAOS_EVENTBUS_BOUNDED_TOPICS", raising=False)
    monkeypatch.delenv("ADAOS_EVENTBUS_SUPERSEDE_BY_HANDLER_TOPICS", raising=False)
    bus = LocalEventBus()
    seen: list[str] = []

    async def first_handler(event: Event):
        await asyncio.sleep(0.01)
        seen.append(f"first:{event.payload.get('receiver')}")

    async def second_handler(event: Event):
        await asyncio.sleep(0.01)
        seen.append(f"second:{event.payload.get('receiver')}")

    bus.subscribe("webio.stream.snapshot.requested", first_handler)
    bus.subscribe("webio.stream.snapshot.requested", second_handler)
    bus.publish(
        Event(
            type="webio.stream.snapshot.requested",
            payload={
                "webspace_id": "desktop",
                "receiver": "infrastate.skills",
                "source": "events_ws",
            },
            source="test",
            ts=0.0,
        )
    )

    controls = bus.backlog_snapshot()["top_webio_stream_controls"]
    row = next(item for item in controls if item["receiver"] == "infrastate.skills")
    assert row["queued_total"] == 2
    assert int(row.get("superseded_total") or 0) == 0

    ok = await bus.wait_for_idle(timeout=1.0)

    assert ok is True
    assert seen == ["first:infrastate.skills", "second:infrastate.skills"]


def test_local_event_bus_keeps_distinct_webio_stream_receivers_for_same_handler():
    async def _run() -> None:
        bus = LocalEventBus()
        seen: list[str] = []
        receivers = ["infrastate.skills", "infrastate.scenarios", "infrastate.yjs.load_mark"]

        async def handler(event: Event):
            await asyncio.sleep(0.01)
            seen.append(str(event.payload.get("receiver") or ""))

        bus.subscribe("webio.stream.snapshot.requested", handler)
        for receiver in receivers:
            bus.publish(
                Event(
                    type="webio.stream.snapshot.requested",
                    payload={
                        "webspace_id": "desktop",
                        "receiver": receiver,
                        "source": "events_ws",
                    },
                    source="test",
                    ts=0.0,
                )
            )

        ok = await bus.wait_for_idle(timeout=1.0)

        assert ok is True
        assert seen == receivers

        controls = bus.backlog_snapshot()["top_webio_stream_controls"]
        by_receiver = {item["receiver"]: item for item in controls}
        for receiver in receivers:
            assert by_receiver[receiver]["incoming_total"] == 1
            assert by_receiver[receiver]["queued_total"] == 1
            assert int(by_receiver[receiver].get("superseded_total") or 0) == 0

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_webio_stream_control_supersedes_per_handler_not_across_handlers():
    bus = LocalEventBus()
    release = asyncio.Event()
    seen: list[tuple[str, int]] = []

    async def handler_a(event: Event):
        seen.append(("a", int(event.payload.get("seq") or 0)))
        await release.wait()

    async def handler_b(event: Event):
        seen.append(("b", int(event.payload.get("seq") or 0)))
        await release.wait()

    bus.subscribe("webio.stream.snapshot.requested", handler_a)
    bus.subscribe("webio.stream.snapshot.requested", handler_b)
    for seq in range(3):
        bus.publish(
            Event(
                type="webio.stream.snapshot.requested",
                payload={
                    "webspace_id": "desktop",
                    "target_node_id": "node-1",
                    "receiver": "browsers.devices",
                    "source": "events_ws",
                    "seq": seq,
                },
                source="test",
                ts=0.0,
            )
        )

    snapshot = bus.backlog_snapshot()
    assert snapshot["bounded_queue_total"] <= 2

    release.set()
    ok = await bus.wait_for_idle(timeout=1.0)

    assert ok is True
    assert sorted(seen) == [("a", 2), ("b", 2)]
