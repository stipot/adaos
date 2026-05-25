from __future__ import annotations

from adaos.domain import make_client_subscription_record, make_projection_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_records import (
    browser_projection_record_snapshot,
    clear_projection_record_registry,
    get_projection_record,
    projection_record_registry_snapshot,
    write_projection_record,
    write_projection_record_if_valid,
)


def setup_function() -> None:
    clear_projection_record_registry()
    clear_projection_demand_registry()


def test_projection_record_registry_writes_and_reads_canonical_records() -> None:
    record = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        updated_at=10.0,
    )

    written = write_projection_record(record)
    stored = get_projection_record(webspace_id="desktop", projection_key="status-card:runtime")
    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert written.meta.projection_key == "status-card:runtime"
    assert stored is not None
    assert stored.data == {"summary": "ready"}
    assert snapshot["record_total"] == 1
    assert snapshot["ready_total"] == 1
    assert snapshot["registry_version"] == 1


def test_projection_record_registry_tracks_unchanged_writes_by_content() -> None:
    first = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        updated_at=10.0,
    )
    second = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        previous=first,
        updated_at=20.0,
    )

    write_projection_record(first)
    write_projection_record(second)
    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert snapshot["registry_version"] == 1
    assert snapshot["stats"]["write_total"] == 2
    assert snapshot["stats"]["changed_total"] == 1
    assert snapshot["stats"]["unchanged_total"] == 1


def test_projection_record_registry_filters_by_webspace() -> None:
    for webspace_id in ["desktop", "dev"]:
        write_projection_record(
            make_projection_record(
                projection_key=f"status-card:{webspace_id}",
                kind="status-card",
                webspace_id=webspace_id,
                data={"webspace_id": webspace_id},
            )
        )

    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert snapshot["record_total"] == 1
    assert snapshot["records"][0]["meta"]["projection_key"] == "status-card:desktop"


def test_projection_record_registry_ignores_non_canonical_records() -> None:
    assert write_projection_record_if_valid({"status": "ready", "data": {"ok": True}}) is None
    assert projection_record_registry_snapshot(webspace_id="desktop")["record_total"] == 0


def test_browser_projection_record_snapshot_returns_only_demanded_records() -> None:
    write_projection_record(
        make_projection_record(
            projection_key="status-card:runtime",
            kind="status-card",
            webspace_id="desktop",
            data={"summary": "runtime ready"},
        )
    )
    write_projection_record(
        make_projection_record(
            projection_key="status-card:unused",
            kind="status-card",
            webspace_id="desktop",
            data={"summary": "unused"},
        )
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
                ),
                make_projection_subscription(
                    projection_key="status-card:missing",
                    consumer_id="widget:missing",
                    consumer_kind="widget",
                ),
            ],
            updated_at=10.0,
        )
    )

    snapshot = browser_projection_record_snapshot(webspace_id="desktop", now=20.0)

    assert snapshot["kind"] == "browser-demanded-projection-records"
    assert snapshot["read_path"] == "data/projectionRecords.records[projection_key]"
    assert snapshot["demanded_projection_total"] == 2
    assert snapshot["record_total"] == 1
    assert snapshot["missing_record_total"] == 1
    assert snapshot["projection_keys"] == ["status-card:missing", "status-card:runtime"]
    assert snapshot["missing_projection_keys"] == ["status-card:missing"]
    assert set(snapshot["records"]) == {"status-card:runtime"}
    assert snapshot["records"]["status-card:runtime"]["data"]["summary"] == "runtime ready"
    assert snapshot["cache_contract"]["browser_read"] is True
    assert snapshot["cache_contract"]["browser_write"] is False
