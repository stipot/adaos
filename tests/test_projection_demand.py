from __future__ import annotations

from adaos.domain import client_subscription_contract_snapshot, make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import (
    clear_projection_demand_registry,
    delete_client_subscription_record,
    demanded_projection_keys,
    projection_demand_consumers,
    projection_demand_snapshot,
    touch_client_subscription_record,
    write_client_subscription_record,
)


def setup_function() -> None:
    clear_projection_demand_registry()


def test_client_subscription_write_replaces_full_set() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
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
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=20.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="projection:hub/overview",
                    consumer_id="page:infrascope",
                    consumer_kind="page",
                )
            ],
        )
    )

    assert demanded_projection_keys(webspace_id="desktop") == ["projection:hub/overview"]
    snapshot = projection_demand_snapshot(webspace_id="desktop", now=21.0)
    assert snapshot["client_total"] == 1
    assert snapshot["consumer_total"] == 1
    assert snapshot["records"][0]["updated_at"] == 20.0


def test_two_consumers_can_demand_same_projection_in_one_webspace() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                ),
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="modal:runtime",
                    consumer_kind="modal",
                    visibility="hidden",
                    pinned=True,
                ),
            ],
        )
    )

    consumers = projection_demand_consumers(webspace_id="desktop")
    snapshot = projection_demand_snapshot(webspace_id="desktop")

    assert [item.consumer_id for item in consumers] == ["modal:runtime", "widget:runtime"]
    assert snapshot["projections"][0]["consumer_total"] == 2
    assert snapshot["projections"][0]["pinned_total"] == 1
    assert snapshot["projections"][0]["visible_total"] == 1


def test_projection_demand_is_isolated_by_webspace() -> None:
    for webspace_id in ["desktop", "dev"]:
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
                        projection_key=f"status-card:{webspace_id}",
                        consumer_id="widget:runtime",
                        consumer_kind="widget",
                    )
                ],
            )
        )

    assert demanded_projection_keys(webspace_id="desktop") == ["status-card:desktop"]
    assert demanded_projection_keys(webspace_id="dev") == ["status-card:dev"]


def test_stale_session_sanitation_marks_but_keeps_pinned_demand() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="pinned:runtime",
                    consumer_kind="pinned-panel",
                    pinned=True,
                )
            ],
        )
    )

    snapshot = projection_demand_snapshot(webspace_id="desktop", stale_after_s=5.0, now=20.0)

    assert snapshot["stale_client_total"] == 1
    assert snapshot["projection_total"] == 1
    assert snapshot["projections"][0]["stale_total"] == 1
    assert snapshot["projections"][0]["pinned_total"] == 1


def test_touch_client_subscription_record_extends_session_without_replacing_demand() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="pinned:runtime",
                    consumer_kind="pinned-panel",
                    pinned=True,
                )
            ],
        )
    )

    record = touch_client_subscription_record(
        client_id="browser-1",
        session_id="session-1",
        webspace_id="desktop",
        updated_at=19.0,
    )
    snapshot = projection_demand_snapshot(webspace_id="desktop", stale_after_s=5.0, now=20.0)

    assert record is not None
    assert record.updated_at == 19.0
    assert record.subscriptions[0].consumer_id == "pinned:runtime"
    assert snapshot["stale_client_total"] == 0
    assert snapshot["projection_total"] == 1
    assert snapshot["projections"][0]["pinned_total"] == 1


def test_explicit_client_delete_removes_demand() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )

    deleted = delete_client_subscription_record(
        client_id="browser-1",
        session_id="session-1",
        webspace_id="desktop",
    )

    assert deleted is True
    assert demanded_projection_keys(webspace_id="desktop") == []


def test_client_subscription_contract_snapshot_exposes_browser_demand_abi() -> None:
    snapshot = client_subscription_contract_snapshot(now=30.0)

    assert snapshot["contract"] == "adaos.client-projection-subscription.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["record_required_fields"] == [
        "client_id",
        "device_id",
        "session_id",
        "webspace_id",
        "role",
        "subscriptions",
        "updated_at",
    ]
    assert snapshot["subscription_required_fields"] == ["projection_key", "consumer_id", "consumer_kind"]
    assert "pinned" in snapshot["subscription_optional_fields"]
    assert snapshot["write_policy"]["mode"] == "replace_full_client_session_set"
    assert snapshot["registry"]["write_endpoint"] == "/api/node/projection-demand/client"
    assert snapshot["sample_record"]["updated_at"] == 30.0
    assert "status-card:runtime" in snapshot["sample_projection_keys"]
