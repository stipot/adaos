from __future__ import annotations

from adaos.services.eventbus import LocalEventBus, emit
from adaos.domain import normalize_event_envelope


def test_eventbus_emit_enriches_event_envelope_metadata() -> None:
    bus = LocalEventBus()
    events = []
    bus.subscribe("demo.event", lambda event: events.append(event))

    emit(
        bus,
        "demo.event",
        {"value": 1},
        "test",
        source_authority="platform",
        scope={"webspace_id": "desktop"},
        schema="demo.event",
        version=1,
        generate_event_id=True,
        ts=12.0,
    )

    envelope = normalize_event_envelope(events[0])
    assert envelope.type == "demo.event"
    assert envelope.source_authority == "platform"
    assert envelope.scope == {"webspace_id": "desktop"}
    assert envelope.schema == "demo.event"
    assert envelope.version == 1
    assert envelope.event_id
