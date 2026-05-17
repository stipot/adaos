from __future__ import annotations

from adaos.domain import Event, make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import (
    clear_projection_dispatcher,
    dispatch_demanded_projection_refresh,
    projection_dispatcher_snapshot,
)
from adaos.services.status_card_registry import (
    clear_status_card_registry,
    ensure_status_card_dispatcher_handler,
    publish_status_card,
    status_card_projection_key,
    status_card_projection_record,
    status_card_registry_snapshot,
    sweep_status_card_registry,
)


def setup_function() -> None:
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_status_card_registry()


def test_status_card_registry_tracks_versions_per_webspace() -> None:
    first = publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )
    second = publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=20.0,
    )
    other_webspace = publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="dev",
        status="failed",
        summary="Runtime down",
        updated_at=30.0,
    )

    assert second.version == first.version
    assert second.changed_at == first.changed_at
    assert other_webspace.version == 1
    assert other_webspace.status == "offline"


def test_status_card_projection_record_marks_ttl_stale() -> None:
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=10.0,
    )

    record = status_card_projection_record(card_id="runtime", webspace_id="desktop", now=16.0)

    assert record is not None
    assert record.status == "stale"
    assert record.meta.lifecycle_reason == "ttl_expired"


def test_status_card_dispatcher_handler_refreshes_demanded_cards() -> None:
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
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
                    projection_key=status_card_projection_key("runtime"),
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )
    ensure_status_card_dispatcher_handler()

    report = _run(
        dispatch_demanded_projection_refresh(
            Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0),
            now=20.0,
        )
    )
    snapshot = projection_dispatcher_snapshot()

    assert len(report.refreshed) == 1
    assert report.refreshed[0].record["data"]["summary"] == "Runtime ready"
    assert report.refreshed[0].record["meta"]["projection_key"] == "status-card:runtime"
    assert report.refreshed[0].reason == "materialized"
    assert snapshot["handler_total"] == 1
    assert snapshot["stats"]["refreshed_total"] == 1


def test_status_card_dispatcher_handler_reports_missing_card_as_unavailable() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key=status_card_projection_key("runtime"),
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )
    ensure_status_card_dispatcher_handler()

    report = _run(
        dispatch_demanded_projection_refresh(
            Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0),
            now=20.0,
        )
    )

    assert len(report.refreshed) == 1
    assert report.refreshed[0].status == "unavailable"
    assert report.refreshed[0].reason == "status_card_missing"
    assert report.refreshed[0].record is None


def test_status_card_registry_snapshot_exposes_projection_records() -> None:
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )

    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=20.0)

    assert snapshot["card_total"] == 1
    assert snapshot["projection_total"] == 1
    assert snapshot["ready_total"] == 1
    assert snapshot["stale_total"] == 0
    assert snapshot["stats"]["publish_total"] == 1
    assert snapshot["stats"]["changed_total"] == 1
    assert snapshot["records"][0]["meta"]["projection_key"] == "status-card:runtime"


def test_status_card_registry_snapshot_counts_unchanged_and_stale_cards() -> None:
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=10.0,
    )
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=11.0,
    )

    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=20.0)

    assert snapshot["stale_total"] == 1
    assert snapshot["ready_total"] == 0
    assert snapshot["stats"]["publish_total"] == 2
    assert snapshot["stats"]["changed_total"] == 1
    assert snapshot["stats"]["unchanged_total"] == 1
    assert snapshot["stats"]["last_publish_latency_ms"] is not None


def test_status_card_registry_sweeps_expired_cards_per_webspace() -> None:
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=10.0,
    )
    publish_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="dev",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=100.0,
    )

    preview = sweep_status_card_registry(webspace_id="desktop", now=20.0, dry_run=True)
    result = sweep_status_card_registry(webspace_id="desktop", now=20.0)
    desktop_snapshot = status_card_registry_snapshot(webspace_id="desktop", now=20.0)
    dev_snapshot = status_card_registry_snapshot(webspace_id="dev", now=20.0)

    assert preview["accepted"] is False
    assert preview["removed_total"] == 0
    assert preview["stale_total"] == 1
    assert result["accepted"] is True
    assert result["removed_total"] == 1
    assert result["cards"][0]["id"] == "runtime"
    assert result["stats"]["sweep_total"] == 1
    assert result["stats"]["swept_total"] == 1
    assert desktop_snapshot["card_total"] == 0
    assert dev_snapshot["card_total"] == 1


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
