from __future__ import annotations

import pytest

from adaos.services.infrastate_status_cards import (
    build_infrastate_status_card_specs,
    publish_infrastate_status_cards,
)
from adaos.services.status_card_registry import clear_status_card_registry, status_card_registry_snapshot


def setup_function() -> None:
    clear_status_card_registry()


def _snapshot() -> dict[str, object]:
    return {
        "summary": {
            "label": "Infra State",
            "value": "ready",
            "description": "hub-root ready",
        },
        "operations": {
            "active_items": [{"id": "op-1"}],
        },
        "realtime": [
            {"id": "route", "status": "ready"},
            {"id": "browser", "status": "degraded"},
        ],
        "reliability": {
            "runtime": {
                "state_sync": {
                    "semantic_state": "stale",
                    "freshness_state": "aging",
                },
                "yjs_pressure": {
                    "policy_state": "throttle",
                },
            }
        },
        "core_update": {
            "state": "idle",
            "phase": "none",
        },
    }


def test_build_infrastate_status_card_specs_identifies_core_families() -> None:
    specs = build_infrastate_status_card_specs(_snapshot(), webspace_id="desktop")
    by_id = {item.id: item for item in specs}

    assert set(by_id) == {
        "infrastate-summary",
        "infrastate-operations",
        "infrastate-realtime",
        "infrastate-yjs",
        "infrastate-core-update",
    }
    assert by_id["infrastate-summary"].status == "running"
    assert by_id["infrastate-operations"].status == "running"
    assert by_id["infrastate-operations"].details_ref["receiver"] == "infrastate.operations.active"
    assert by_id["infrastate-realtime"].status == "warning"
    assert by_id["infrastate-yjs"].status == "warning"
    assert by_id["infrastate-core-update"].details_ref["receiver"] == "infrastate.core_update_diagnostics"


def test_publish_infrastate_status_cards_uses_shared_registry_and_owner() -> None:
    cards = publish_infrastate_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)
    by_id = {item["id"]: item for item in snapshot["cards"]}

    assert len(cards) == 5
    assert snapshot["card_total"] == 5
    assert snapshot["stats"]["changed_total"] == 5
    assert by_id["infrastate-summary"]["owner"] == "skill:infrastate_skill"
    assert by_id["infrastate-realtime"]["details_ref"]["kind"] == "stream"
    assert by_id["infrastate-yjs"]["status"] == "warning"


def test_publish_infrastate_status_cards_dedupes_unchanged_snapshot() -> None:
    publish_infrastate_status_cards(_snapshot(), webspace_id="desktop", updated_at=10.0)
    publish_infrastate_status_cards(_snapshot(), webspace_id="desktop", updated_at=20.0)

    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=21.0)

    assert snapshot["stats"]["publish_total"] == 10
    assert snapshot["stats"]["changed_total"] == 5
    assert snapshot["stats"]["unchanged_total"] == 5


def test_build_infrastate_status_card_specs_requires_webspace_id() -> None:
    with pytest.raises(ValueError, match="webspace_id is required"):
        build_infrastate_status_card_specs(_snapshot(), webspace_id="")
