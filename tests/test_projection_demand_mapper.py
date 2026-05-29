from __future__ import annotations

from adaos.services.projection_demand_mapper import (
    browser_surface_lifecycle_contract_snapshot,
    build_browser_projection_demand_record,
)


def test_browser_demand_mapper_maps_page_widget_modal_and_pinned_panel() -> None:
    record = build_browser_projection_demand_record(
        client_id="browser-1",
        device_id="desktop",
        session_id="session-1",
        webspace_id="desktop",
        role="operator",
        updated_at=10.0,
        page={
            "id": "infrascope",
            "projectionKeys": ["projection:hub/overview", "projection:hub/inventory"],
        },
        widgets=[
            {
                "id": "infra-state",
                "projection_key": "status-card:runtime",
                "nodeId": "node-a",
                "params": {"compact": True},
            }
        ],
        modals=[
            {
                "id": "runtime-details",
                "projection_key": "projection:hub/object-inspector",
                "consumerId": "modal:runtime-details",
                "visible": False,
            }
        ],
        pinned_panels=[
            {
                "id": "runtime-pinned",
                "projection_key": "status-card:runtime",
            }
        ],
    )

    payload = record.to_dict()

    assert payload["client_id"] == "browser-1"
    assert payload["updated_at"] == 10.0
    assert [item["projection_key"] for item in payload["subscriptions"]] == [
        "projection:hub/overview",
        "projection:hub/inventory",
        "status-card:runtime",
        "projection:hub/object-inspector",
        "status-card:runtime",
    ]
    assert payload["subscriptions"][0]["consumer_id"] == "page:infrascope"
    assert payload["subscriptions"][2]["consumer_id"] == "widget:infra-state"
    assert payload["subscriptions"][2]["node_scope"] == {"node_id": "node-a"}
    assert payload["subscriptions"][2]["params"] == {"compact": True}
    assert payload["subscriptions"][3]["consumer_id"] == "modal:runtime-details"
    assert payload["subscriptions"][3]["visibility"] == "hidden"
    assert payload["subscriptions"][4]["consumer_kind"] == "pinned-panel"
    assert payload["subscriptions"][4]["pinned"] is True


def test_browser_demand_mapper_ignores_consumers_without_projection_key() -> None:
    record = build_browser_projection_demand_record(
        client_id="browser-1",
        device_id="desktop",
        session_id="session-1",
        webspace_id="desktop",
        widgets=[{"id": "plain-widget"}],
        updated_at=10.0,
    )

    assert record.to_dict()["subscriptions"] == []


def test_surface_lifecycle_contract_snapshot_exposes_mapping_rules() -> None:
    snapshot = browser_surface_lifecycle_contract_snapshot(now=40.0)

    assert snapshot["contract"] == "adaos.browser-surface-lifecycle-subscriptions.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["input_groups"] == ["page", "widgets", "modals", "pinnedPanels"]
    assert snapshot["server_endpoint"] == "/api/node/projection-demand/browser-state"
    assert snapshot["output_contract"] == "adaos.client-projection-subscription.v1"
    assert snapshot["sample_record"]["updated_at"] == 40.0
    assert snapshot["sample_subscription_total"] == 5
    assert snapshot["sample_consumer_kinds"] == ["modal", "page", "pinned-panel", "widget"]
    assert "projection:hub/object-inspector" in snapshot["sample_projection_keys"]
    assert snapshot["direct_client_hookup"]["status"] == "pending"
