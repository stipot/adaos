from __future__ import annotations

from adaos.domain import (
    make_client_subscription_record,
    make_projection_subscription,
    normalize_client_subscription_record,
    normalize_projection_subscription,
)


def test_projection_subscription_uses_browser_demand_shape() -> None:
    item = make_projection_subscription(
        projection_key="status-card:runtime",
        consumer_id="widget:infra-state",
        consumer_kind="widget",
        node_scope={"node_id": "node-a"},
        pinned=True,
        visibility="visible",
        params={"limit": 5},
    )

    assert item.to_dict() == {
        "projection_key": "status-card:runtime",
        "consumer_id": "widget:infra-state",
        "consumer_kind": "widget",
        "node_scope": {"node_id": "node-a"},
        "pinned": True,
        "visibility": "visible",
        "params": {"limit": 5},
    }


def test_client_subscription_record_keeps_full_subscription_set() -> None:
    record = make_client_subscription_record(
        client_id="browser-1",
        device_id="desktop",
        session_id="session-1",
        webspace_id="desktop",
        role="operator",
        updated_at=10.0,
        subscriptions=[
            make_projection_subscription(
                projection_key="status-card:runtime",
                consumer_id="widget:infra-state",
                consumer_kind="widget",
            ),
            make_projection_subscription(
                projection_key="status-card:runtime",
                consumer_id="modal:runtime-details",
                consumer_kind="modal",
                visibility="hidden",
            ),
        ],
    )

    payload = record.to_dict()

    assert payload["client_id"] == "browser-1"
    assert payload["device_id"] == "desktop"
    assert payload["session_id"] == "session-1"
    assert payload["webspace_id"] == "desktop"
    assert payload["role"] == "operator"
    assert payload["updated_at"] == 10.0
    assert [item["consumer_id"] for item in payload["subscriptions"]] == [
        "widget:infra-state",
        "modal:runtime-details",
    ]


def test_normalize_projection_subscription_accepts_mapping() -> None:
    item = normalize_projection_subscription(
        {
            "projection_key": "projection:node/overview",
            "consumer_id": "page:infrascope",
            "consumer_kind": "page",
            "node_scope": "node-a",
            "pinned": False,
            "visibility": "visible",
        }
    )

    assert item.projection_key == "projection:node/overview"
    assert item.consumer_id == "page:infrascope"
    assert item.consumer_kind == "page"
    assert item.node_scope == "node-a"


def test_normalize_client_subscription_record_accepts_existing_mapping() -> None:
    record = normalize_client_subscription_record(
        {
            "client_id": "browser-1",
            "device_id": "desktop",
            "session_id": "session-1",
            "webspace_id": "desktop",
            "role": "operator",
            "updated_at": 20.0,
            "subscriptions": [
                {
                    "projection_key": "status-card:runtime",
                    "consumer_id": "widget:infra-state",
                    "consumer_kind": "widget",
                    "params": {"compact": True},
                }
            ],
        }
    )

    assert record.updated_at == 20.0
    assert len(record.subscriptions) == 1
    assert record.subscriptions[0].params == {"compact": True}
