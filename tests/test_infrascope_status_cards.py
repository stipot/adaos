from __future__ import annotations

import pytest

from adaos.services.infrascope_status_cards import (
    build_infrascope_status_card_specs,
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
        },
        "inventory": {
            "all": [
                {"object_id": "hub-1", "kind": "hub", "status": "online"},
                {"object_id": "member-1", "kind": "member", "status": "degraded"},
            ],
            "hubs": [{"object_id": "hub-1"}],
            "members": [{"object_id": "member-1"}],
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
    }
    assert by_id["infrascope-overview"].status == "degraded"
    assert by_id["infrascope-overview"].details_ref["tool"] == "get_snapshot"
    assert by_id["infrascope-incidents"].status == "degraded"
    assert by_id["infrascope-incidents"].details_ref["receiver"] == "infrascope.overview.active_incidents"
    assert by_id["infrascope-inventory"].scope["object_total"] == 2
    assert by_id["infrascope-inventory"].scope["counts"] == {"all": 2, "hubs": 1, "members": 1}
    assert by_id["infrascope-operations"].status == "warning"
    assert by_id["infrascope-operations"].details_ref["receiver"] == "infrascope.operations.active"


def test_publish_infrascope_status_cards_uses_shared_registry_and_owner() -> None:
    cards = publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)
    by_id = {item["id"]: item for item in snapshot["cards"]}

    assert len(cards) == 4
    assert snapshot["card_total"] == 4
    assert snapshot["stats"]["changed_total"] == 4
    assert by_id["infrascope-overview"]["owner"] == "skill:infrascope_skill"
    assert by_id["infrascope-overview"]["details_ref"]["kind"] == "tool"
    assert by_id["infrascope-incidents"]["details_ref"]["kind"] == "stream"
    assert by_id["infrascope-inventory"]["status"] == "degraded"
    assert by_id["infrascope-operations"]["status"] == "warning"


def test_publish_infrascope_status_cards_dedupes_unchanged_snapshot() -> None:
    publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    publish_infrascope_status_cards(_snapshot(), webspace_id="desktop", updated_at=20.0)

    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=21.0)

    assert snapshot["stats"]["publish_total"] == 8
    assert snapshot["stats"]["changed_total"] == 4
    assert snapshot["stats"]["unchanged_total"] == 4


def test_build_infrascope_status_card_specs_requires_webspace_id() -> None:
    with pytest.raises(ValueError, match="webspace_id is required"):
        build_infrascope_status_card_specs(_snapshot(), webspace_id="")
