from __future__ import annotations

from adaos.domain import make_client_subscription_record, make_projection_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_records import (
    browser_projection_adapter_contract_snapshot,
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


def test_browser_projection_adapter_contract_snapshot_exposes_adapter_rules() -> None:
    snapshot = browser_projection_adapter_contract_snapshot(now=90.0)

    assert snapshot["contract"] == "adaos.projection-records.browser-adapter.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 90.0
    assert snapshot["source_of_truth"]["canonical_yjs_path"] == "data/projectionRecords"
    assert snapshot["adapter_rules"]["read_projection_records"] is True
    assert snapshot["adapter_rules"]["cache_by_projection_key"] is True
    assert snapshot["adapter_rules"]["reuse_cached_views"] is True
    assert snapshot["adapter_rules"]["avoid_observe_deep_data"] is True
    assert snapshot["cache_model"]["if_none_match"] == "supported"
    assert "yjs.reduce_broad_observers" in snapshot["roadmap_items"]


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
    assert snapshot["cache"]["key"] == "browser-projection-records:desktop:*:*:status-card:missing,status-card:runtime"
    assert snapshot["cache"]["etag"] == snapshot["etag"]
    assert snapshot["cache"]["if_none_match_supported"] is True
    entries = {entry["projection_key"]: entry for entry in snapshot["entries"]}
    runtime_entry = entries["status-card:runtime"]
    missing_entry = entries["status-card:missing"]
    assert runtime_entry["cache"]["key"] == "browser-projection-records:desktop:*:*:status-card:runtime"
    assert runtime_entry["cache"]["record_fingerprint"] == runtime_entry["record"]["meta"]["fingerprint"]
    assert runtime_entry["cache"]["missing_reason"] is None
    assert missing_entry["cache"]["key"] == "browser-projection-records:desktop:*:*:status-card:missing"
    assert missing_entry["cache"]["record_fingerprint"] is None
    assert missing_entry["cache"]["missing_reason"] == "demanded_projection_record_not_materialized"
    assert snapshot["entry_cache_keys"] == [
        "browser-projection-records:desktop:*:*:status-card:missing",
        "browser-projection-records:desktop:*:*:status-card:runtime",
    ]
    assert snapshot["entry_fingerprints"]["status-card:runtime"] == runtime_entry["cache"]["fingerprint"]
    assert snapshot["fingerprint"]
    assert snapshot["cache_contract"]["browser_read"] is True
    assert snapshot["cache_contract"]["browser_write"] is False


def test_browser_projection_record_snapshot_can_scope_to_client_session() -> None:
    for projection_key in ["status-card:runtime", "status-card:desktop-shell"]:
        write_projection_record(
            make_projection_record(
                projection_key=projection_key,
                kind="status-card",
                webspace_id="desktop",
                data={"summary": projection_key},
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
                )
            ],
        )
    )
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-2",
            device_id="desktop",
            session_id="session-2",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:desktop-shell",
                    consumer_id="widget:desktop-shell",
                    consumer_kind="widget",
                )
            ],
        )
    )

    snapshot = browser_projection_record_snapshot(
        webspace_id="desktop",
        client_id="browser-1",
        session_id="session-1",
    )

    assert snapshot["session_scoped"] is True
    assert snapshot["client_id"] == "browser-1"
    assert snapshot["session_id"] == "session-1"
    assert snapshot["projection_keys"] == ["status-card:runtime"]
    assert set(snapshot["records"]) == {"status-card:runtime"}
    assert snapshot["cache_contract"]["client_session_filter"] is True


def test_browser_projection_record_snapshot_can_filter_projection_keys() -> None:
    for projection_key in ["status-card:runtime", "status-card:desktop-shell"]:
        write_projection_record(
            make_projection_record(
                projection_key=projection_key,
                kind="status-card",
                webspace_id="desktop",
                data={"summary": projection_key},
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
                    projection_key="status-card:desktop-shell",
                    consumer_id="widget:desktop-shell",
                    consumer_kind="widget",
                ),
            ],
        )
    )

    snapshot = browser_projection_record_snapshot(
        webspace_id="desktop",
        projection_keys=["status-card:desktop-shell"],
    )

    assert snapshot["projection_scoped"] is True
    assert snapshot["requested_projection_keys"] == ["status-card:desktop-shell"]
    assert snapshot["projection_keys"] == ["status-card:desktop-shell"]
    assert set(snapshot["records"]) == {"status-card:desktop-shell"}


def test_browser_projection_record_snapshot_exposes_lifecycle_summary() -> None:
    for projection_key, status in [
        ("status-card:ready", "ready"),
        ("status-card:loading", "loading"),
        ("status-card:stale", "stale"),
    ]:
        write_projection_record(
            make_projection_record(
                projection_key=projection_key,
                kind="status-card",
                webspace_id="desktop",
                status=status,
                lifecycle_reason=f"{status}_reason",
                data={"summary": projection_key},
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
                    projection_key=projection_key,
                    consumer_id=f"widget:{projection_key}",
                    consumer_kind="widget",
                )
                for projection_key in [
                    "status-card:ready",
                    "status-card:loading",
                    "status-card:stale",
                    "status-card:missing",
                ]
            ],
        )
    )

    snapshot = browser_projection_record_snapshot(webspace_id="desktop")

    entries = {entry["projection_key"]: entry for entry in snapshot["entries"]}
    assert entries["status-card:ready"]["lifecycle"]["state"] == "ready"
    assert entries["status-card:loading"]["lifecycle"]["state"] == "refreshing"
    assert entries["status-card:stale"]["lifecycle"]["state"] == "stale"
    assert entries["status-card:missing"]["lifecycle"]["state"] == "pending"
    assert snapshot["lifecycle_summary"]["states"] == {
        "pending": 1,
        "refreshing": 1,
        "ready": 1,
        "stale": 1,
        "error": 0,
    }
    assert snapshot["lifecycle_summary"]["ready"] is False
    assert snapshot["lifecycle_summary"]["blocked"] is True
    assert snapshot["lifecycle_summary"]["pending_projection_keys"] == ["status-card:missing"]
    assert snapshot["lifecycle_summary"]["refreshing_projection_keys"] == ["status-card:loading"]
    assert snapshot["lifecycle_summary"]["stale_projection_keys"] == ["status-card:stale"]


def test_browser_projection_record_snapshot_enforces_access_metadata() -> None:
    write_projection_record(
        make_projection_record(
            projection_key="status-card:dev",
            kind="status-card",
            webspace_id="desktop",
            data={"summary": "dev only"},
            access={"audience": "dev", "sensitive": True},
        )
    )
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-guest",
            device_id="desktop",
            session_id="session-guest",
            webspace_id="desktop",
            role="guest",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:dev",
                    consumer_id="widget:dev",
                    consumer_kind="widget",
                )
            ],
        )
    )

    snapshot = browser_projection_record_snapshot(
        webspace_id="desktop",
        client_id="browser-guest",
        session_id="session-guest",
    )

    assert snapshot["record_total"] == 0
    assert snapshot["access_denied_total"] == 1
    assert snapshot["access_denied_projection_keys"] == ["status-card:dev"]
    assert snapshot["entries"][0]["access_denied"] is True
    assert snapshot["entries"][0]["cache"]["missing_reason"] == "projection_access_denied"
    assert snapshot["entries"][0]["lifecycle"]["state"] == "error"


def test_browser_projection_record_snapshot_allows_dev_access_role() -> None:
    write_projection_record(
        make_projection_record(
            projection_key="status-card:dev",
            kind="status-card",
            webspace_id="desktop",
            data={"summary": "dev only"},
            access={"audience": "dev", "sensitive": True},
        )
    )
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-dev",
            device_id="desktop",
            session_id="session-dev",
            webspace_id="desktop",
            role="dev",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:dev",
                    consumer_id="widget:dev",
                    consumer_kind="widget",
                )
            ],
        )
    )

    snapshot = browser_projection_record_snapshot(
        webspace_id="desktop",
        client_id="browser-dev",
        session_id="session-dev",
    )

    assert snapshot["record_total"] == 1
    assert snapshot["access_denied_total"] == 0
    assert snapshot["records"]["status-card:dev"]["data"]["summary"] == "dev only"
