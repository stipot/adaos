from __future__ import annotations

from types import SimpleNamespace

from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.services.eventbus import LocalEventBus
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import clear_projection_dispatcher, projection_dispatcher_snapshot
from adaos.services.projection_event_bridge import (
    PROJECTION_LIFECYCLE_EVENT,
    projection_event_bridge_snapshot,
    register_projection_event_bridge,
)
from adaos.services.projection_records import clear_projection_record_registry, get_projection_record
from adaos.services.status import StatusRegistry


def setup_function() -> None:
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_projection_record_registry()


def test_projection_event_bridge_refreshes_demanded_status_card(monkeypatch) -> None:
    bus = LocalEventBus()
    registry = StatusRegistry(bus=bus)
    lifecycle_events = []
    materialized = []
    register_projection_event_bridge(bus)
    bus.subscribe(PROJECTION_LIFECYCLE_EVENT, lambda event: lifecycle_events.append(event))
    monkeypatch.setattr("adaos.services.status_projection.get_ctx", lambda: SimpleNamespace(status_registry=registry))

    async def fake_materialize_projection_records_to_yjs(**kwargs):
        materialized.append(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": kwargs["webspace_id"],
            "projection_keys": list(kwargs["projection_keys"] or []),
            "written": True,
        }

    monkeypatch.setattr(
        "adaos.services.projection_event_bridge.materialize_projection_records_to_yjs",
        fake_materialize_projection_records_to_yjs,
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

    registry.publish(
        {
            "id": "runtime",
            "owner": "core:runtime",
            "kind": "runtime",
            "scope": "platform",
            "webspace_id": "desktop",
            "status": "ready",
            "summary": "Runtime ready",
            "updated_at": 10.0,
        }
    )

    stored = get_projection_record(webspace_id="desktop", projection_key="status-card:runtime")
    dispatcher = projection_dispatcher_snapshot()
    assert stored is not None
    assert stored.data["summary"] == "Runtime ready"
    assert dispatcher["stats"]["refreshed_total"] == 1
    assert materialized[0]["webspace_id"] == "desktop"
    assert materialized[0]["projection_keys"] == ["status-card:runtime"]
    assert [event.payload["status"] for event in lifecycle_events] == ["requested", "refreshing", "ready"]


def test_projection_event_bridge_snapshot_exposes_pipeline() -> None:
    snapshot = projection_event_bridge_snapshot(now=90.0)

    assert snapshot["contract"] == "adaos.projection-event-bridge.v1"
    assert snapshot["ready_for_mvp"] is True
    assert "adaos.status.card.changed" in snapshot["topics"]
    assert "ProjectionRecord registry" in snapshot["pipeline"]
