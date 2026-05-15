from __future__ import annotations

from adaos.domain import Event, make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import (
    clear_projection_dispatcher,
    demanded_projection_refresh_contexts,
    dispatch_demanded_projection_refresh,
    register_projection_refresh_handler,
)


def setup_function() -> None:
    clear_projection_demand_registry()
    clear_projection_dispatcher()


def _write_demand(webspace_id: str, projection_key: str, *, consumer_id: str = "widget:runtime") -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id=f"browser-{webspace_id}",
            device_id="desktop",
            session_id="session-1",
            webspace_id=webspace_id,
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key=projection_key,
                    consumer_id=consumer_id,
                    consumer_kind=consumer_id.split(":", 1)[0],
                )
            ],
        )
    )


def test_dispatcher_selects_only_demanded_projection_in_event_webspace() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("dev", "status-card:runtime")
    event = Event(
        type="node.status",
        payload={"webspace_id": "desktop"},
        source="test",
        ts=20.0,
    )

    contexts = demanded_projection_refresh_contexts(event, now=20.0)

    assert [(item.webspace_id, item.projection_key) for item in contexts] == [
        ("desktop", "status-card:runtime")
    ]


def test_dispatcher_does_not_cross_webspace_when_explicit_scope_is_used() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("dev", "status-card:runtime")
    refreshed: list[tuple[str, str]] = []

    def _handler(context):
        refreshed.append((context.webspace_id, context.projection_key))
        return {"status": "ready", "data": {"ok": True}}

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, webspace_ids=["dev"], now=20.0))

    assert refreshed == [("dev", "status-card:runtime")]
    assert [(item.webspace_id, item.projection_key) for item in report.selected] == [
        ("dev", "status-card:runtime")
    ]
    assert len(report.refreshed) == 1


def test_dispatcher_skips_demand_without_registered_handler() -> None:
    _write_demand("desktop", "projection:missing")
    event = Event(type="demo.event", payload={"webspace_id": "desktop"}, source="test", ts=20.0)

    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))

    assert len(report.refreshed) == 0
    assert len(report.skipped) == 1
    assert report.skipped[0].projection_key == "projection:missing"
    assert report.skipped[0].reason == "no_handler"


def test_dispatcher_can_filter_projection_keys() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("desktop", "projection:hub/overview", consumer_id="page:infrascope")
    event = Event(type="demo.event", payload={"webspace_id": "desktop"}, source="test", ts=20.0)

    contexts = demanded_projection_refresh_contexts(
        event,
        projection_keys=["projection:hub/overview"],
        now=20.0,
    )

    assert [(item.webspace_id, item.projection_key) for item in contexts] == [
        ("desktop", "projection:hub/overview")
    ]


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
