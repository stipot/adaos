from __future__ import annotations

import pytest

from adaos.services.infrascope_status_cards import (
    build_infrascope_status_card_specs,
    infrascope_demanded_only_contract_snapshot,
    infrascope_platform_errors_contract_snapshot,
    infrascope_projection_family_contract_snapshot,
    normalize_infrascope_status_card_ids,
    publish_infrascope_platform_status_card,
    publish_infrascope_status_cards,
)
from adaos.services.status_card_registry import clear_status_card_registry, status_card_registry_snapshot


def setup_function() -> None:
    clear_status_card_registry()


def _snapshot() -> dict[str, object]:
    return {
        "summary": {
            "label": "Infrascope",
            "value": "degraded",
            "subtitle": "1 active incident",
        },
        "overview": {
            "active_incidents": [
                {
                    "id": "incident:member-1",
                    "object_id": "member-1",
                    "severity": "high",
                    "summary": "Link is degraded",
                }
            ],
            "health_strip": [
                {
                    "id": "health:member-1",
                    "status": "degraded",
                    "summary": "Member link degraded",
                }
            ],
            "active_runtimes": [
                {"id": "runtime:yjs", "status": "warning", "summary": "Yjs pressure"}
            ],
        },
        "inventory": {
            "all": [
                {"object_id": "hub-1", "kind": "hub", "status": "online"},
                {"object_id": "member-1", "kind": "member", "status": "degraded"},
            ],
            "browsers": [{"object_id": "browser-1", "kind": "browser_session", "status": "online"}],
            "hubs": [{"object_id": "hub-1"}],
            "members": [{"object_id": "member-1"}],
            "runtimes": [{"object_id": "runtime:yjs", "kind": "runtime", "status": "warning"}],
            "skills": [{"object_id": "skill:weather", "kind": "skill", "status": "online"}],
            "scenarios": [{"object_id": "scenario:ops", "kind": "scenario", "status": "online"}],
        },
        "operations": {
            "items": [
                {
                    "id": "core-update",
                    "status": "queued",
                    "summary": "Core update waiting",
                }
            ]
        },
        "inspectors": {
            "local": {
                "object_id": "local",
                "value": "degraded",
                "object": {"status": "degraded"},
                "incidents": [{"severity": "high", "summary": "Local pressure"}],
                "topology": {"edges": [{"from": "local", "to": "member-1"}]},
            }
        },
    }


def test_build_infrascope_status_card_specs_identifies_first_projection_families() -> None:
    specs = build_infrascope_status_card_specs(_snapshot(), webspace_id="desktop")
    by_id = {item.id: item for item in specs}

    assert set(by_id) == {
        "infrascope-overview",
        "infrascope-incidents",
        "infrascope-inventory",
        "infrascope-operations",
        "infrascope-browsers",
        "infrascope-runtimes",
        "infrascope-registry",
        "infrascope-inspectors",
        "infrascope-topology",
    }
    assert by_id["infrascope-overview"].status == "degraded"
    assert by_id["infrascope-overview"].details_ref["tool"] == "get_snapshot"
    assert by_id["infrascope-incidents"].status == "degraded"
    assert by_id["infrascope-incidents"].details_ref["receiver"] == "infrascope.overview.active_incidents"
    assert by_id["infrascope-inventory"].scope["object_total"] == 2
    assert by_id["infrascope-inventory"].scope["counts"] == {
        "all": 2,
        "browsers": 1,
        "hubs": 1,
        "members": 1,
        "runtimes": 1,
        "scenarios": 1,
        "skills": 1,
    }
    assert by_id["infrascope-operations"].status == "warning"
    assert by_id["infrascope-operations"].details_ref["receiver"] == "infrascope.operations.active"
    assert by_id["infrascope-browsers"].scope["browser_total"] == 1
    assert by_id["infrascope-browsers"].details_ref["receiver"] == "infrascope.inventory.browsers"
    assert by_id["infrascope-runtimes"].status == "warning"
    assert by_id["infrascope-runtimes"].scope["active_runtime_total"] == 1
    assert by_id["infrascope-registry"].scope["skill_total"] == 1
    assert by_id["infrascope-registry"].scope["scenario_total"] == 1
    assert by_id["infrascope-registry"].details_ref["receiver"] == "infrascope.inventory.skills"
    assert by_id["infrascope-inspectors"].status == "degraded"
    assert by_id["infrascope-inspectors"].scope["inspector_total"] == 1
    assert by_id["infrascope-inspectors"].details_ref["receiver"] == "infrascope.inspector.local"
    assert by_id["infrascope-topology"].status == "online"
    assert by_id["infrascope-topology"].scope["edge_total"] == 1
    assert by_id["infrascope-topology"].details_ref["receiver"] == "infrascope.inspector_field.topology.local"


def test_infrascope_projection_family_contract_snapshot_exposes_split_rules() -> None:
    snapshot = infrascope_projection_family_contract_snapshot(now=100.0)

    assert snapshot["contract"] == "adaos.infrascope.projection-families.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 100.0
    assert snapshot["owner"] == "skill:infrascope_skill"
    assert snapshot["family_total"] == 9
    assert "status-card:infrascope-overview" in snapshot["projection_keys"]
    assert "inspectors" in snapshot["sections"]
    assert snapshot["boundaries"]["uses_shared_status_card_abi"] is True
    assert snapshot["boundaries"]["introduces_infrascope_specific_abi"] is False
    assert snapshot["boundaries"]["pre_materialize_all_inspector_details"] is False
    families = {item["id"]: item for item in snapshot["families"]}
    assert families["infrascope-inspectors"]["details_receiver"] == "infrascope.inspector.local"
    assert families["infrascope-topology"]["demand_filterable"] is True


def test_infrascope_demanded_only_contract_snapshot_exposes_selection_rules() -> None:
    snapshot = infrascope_demanded_only_contract_snapshot(now=110.0)

    assert snapshot["contract"] == "adaos.infrascope.demanded-only-refresh.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 110.0
    assert snapshot["refresh_endpoint"] == "/api/node/status-cards/infrascope/refresh"
    assert snapshot["selection_rules"]["demanded_only_flag"] == "demanded_only=true"
    assert snapshot["selection_rules"]["implicit_card_ids"] == "demanded_projection_keys(webspace_id)"
    assert snapshot["selection_rules"]["webspace_scoped"] is True
    assert snapshot["boundaries"]["publishes_only_requested_cards"] is True
    assert snapshot["boundaries"]["cross_webspace_churn"] is False
    assert "requested_card_ids" in snapshot["response_fields"]


def test_infrascope_platform_errors_contract_snapshot_exposes_separate_cards() -> None:
    snapshot = infrascope_platform_errors_contract_snapshot(now=120.0)

    assert snapshot["contract"] == "adaos.infrascope.platform-errors.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 120.0
    assert snapshot["owner"] == "core:infrascope-platform"
    assert snapshot["projection_keys"] == [
        "status-card:infrascope-platform-warning",
        "status-card:infrascope-materialization-error",
    ]
    assert snapshot["separation_rules"]["not_hidden_inside_data_infrascope"] is True
    assert snapshot["separation_rules"]["uses_shared_status_card_abi"] is True
    assert snapshot["boundaries"]["skill_payload_remains_domain_snapshot"] is True


def test_publish_infrascope_platform_status_card_uses_separate_projection() -> None:
    card = publish_infrascope_platform_status_card(
        webspace_id="desktop",
        card_id="infrascope-materialization-error",
        status="error",
        summary="Infrascope refresh failed",
        reason="projection_materialization_failed",
        source="status-card-refresh",
        updated_at=30.0,
    )
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=31.0)

    assert card.id == "infrascope-materialization-error"
    assert card.owner == "core:infrascope-platform"
    assert card.kind == "materialization-error"
    assert card.status == "offline"
    assert card.scope["reason"] == "projection_materialization_failed"
    assert snapshot["card_total"] == 1
    assert snapshot["cards"][0]["id"] == "infrascope-materialization-error"
    assert snapshot["cards"][0]["details_ref"]["path"] == "/api/node/projection-diagnostics"


def test_publish_infrascope_status_cards_uses_shared_registry_and_owner() -> None:
    cards = publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)
    by_id = {item["id"]: item for item in snapshot["cards"]}

    assert len(cards) == 9
    assert snapshot["card_total"] == 9
    assert snapshot["stats"]["changed_total"] == 9
    assert by_id["infrascope-overview"]["owner"] == "skill:infrascope_skill"
    assert by_id["infrascope-overview"]["details_ref"]["kind"] == "tool"
    assert by_id["infrascope-incidents"]["details_ref"]["kind"] == "stream"
    assert by_id["infrascope-inventory"]["status"] == "degraded"
    assert by_id["infrascope-operations"]["status"] == "warning"
    assert by_id["infrascope-browsers"]["details_ref"]["receiver"] == "infrascope.inventory.browsers"
    assert by_id["infrascope-runtimes"]["status"] == "warning"
    assert by_id["infrascope-registry"]["scope"]["skill_total"] == 1
    assert by_id["infrascope-inspectors"]["scope"]["inspector_total"] == 1
    assert by_id["infrascope-topology"]["scope"]["edge_total"] == 1


def test_publish_infrascope_status_cards_can_filter_requested_projection_keys() -> None:
    assert normalize_infrascope_status_card_ids(
        [
            "status-card:infrascope-overview",
            "infrascope-registry",
            "status-card:unknown",
        ]
    ) == ["infrascope-overview", "infrascope-registry"]

    cards = publish_infrascope_status_cards(
        _snapshot(),
        webspace_id="desktop",
        updated_at=10.0,
        card_ids=[
            "status-card:infrascope-overview",
            "infrascope-registry",
            "status-card:unknown",
        ],
    )
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)

    assert [card.id for card in cards] == ["infrascope-overview", "infrascope-registry"]
    assert snapshot["card_total"] == 2
    assert {card["id"] for card in snapshot["cards"]} == {
        "infrascope-overview",
        "infrascope-registry",
    }


def test_publish_infrascope_status_cards_dedupes_unchanged_snapshot() -> None:
    publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=20.0)

    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=21.0)

    assert snapshot["stats"]["publish_total"] == 18
    assert snapshot["stats"]["changed_total"] == 9
    assert snapshot["stats"]["unchanged_total"] == 9


def test_build_infrascope_status_card_specs_requires_webspace_id() -> None:
    with pytest.raises(ValueError, match="webspace_id is required"):
        build_infrascope_status_card_specs(_snapshot(), webspace_id="")
