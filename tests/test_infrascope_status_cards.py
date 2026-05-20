from __future__ import annotations

import pytest

from adaos.services.infrascope_status_cards import (
    build_infrascope_status_card_specs,
    normalize_infrascope_status_card_ids,
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


def test_publish_infrascope_status_cards_uses_shared_registry_and_owner() -> None:
    cards = publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)
    by_id = {item["id"]: item for item in snapshot["cards"]}

    assert len(cards) == 7
    assert snapshot["card_total"] == 7
    assert snapshot["stats"]["changed_total"] == 7
    assert by_id["infrascope-overview"]["owner"] == "skill:infrascope_skill"
    assert by_id["infrascope-overview"]["details_ref"]["kind"] == "tool"
    assert by_id["infrascope-incidents"]["details_ref"]["kind"] == "stream"
    assert by_id["infrascope-inventory"]["status"] == "degraded"
    assert by_id["infrascope-operations"]["status"] == "warning"
    assert by_id["infrascope-browsers"]["details_ref"]["receiver"] == "infrascope.inventory.browsers"
    assert by_id["infrascope-runtimes"]["status"] == "warning"
    assert by_id["infrascope-registry"]["scope"]["skill_total"] == 1


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

    assert snapshot["stats"]["publish_total"] == 14
    assert snapshot["stats"]["changed_total"] == 7
    assert snapshot["stats"]["unchanged_total"] == 7


def test_build_infrascope_status_card_specs_requires_webspace_id() -> None:
    with pytest.raises(ValueError, match="webspace_id is required"):
        build_infrascope_status_card_specs(_snapshot(), webspace_id="")
