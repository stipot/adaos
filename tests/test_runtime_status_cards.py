from __future__ import annotations

from adaos.services.status_card_registry import clear_status_card_registry, status_card_projection_record
from adaos.services.runtime_status_cards import publish_runtime_status_card


def setup_function() -> None:
    clear_status_card_registry()


def test_runtime_status_card_publishes_ready_lifecycle() -> None:
    card = publish_runtime_status_card(
        webspace_id="desktop",
        node_id="node-a",
        lifecycle={
            "node_state": "ready",
            "reason": "",
            "draining": False,
            "accepting_new_work": True,
        },
        updated_at=10.0,
    )

    assert card.id == "runtime"
    assert card.owner == "core:runtime"
    assert card.status == "online"
    assert card.summary == "Runtime ready"
    assert card.scope["node_id"] == "node-a"
    assert card.scope["accepting_new_work"] is True
    assert card.details_ref is not None
    assert card.details_ref.path == "/api/node/status"


def test_runtime_status_card_maps_draining_to_warning() -> None:
    card = publish_runtime_status_card(
        webspace_id="desktop",
        lifecycle={
            "node_state": "draining",
            "reason": "update",
            "draining": True,
            "accepting_new_work": False,
        },
        updated_at=10.0,
    )

    assert card.status == "warning"
    assert card.severity == "medium"
    assert card.summary == "Runtime draining: update"


def test_runtime_status_card_is_available_as_projection_record() -> None:
    publish_runtime_status_card(
        webspace_id="desktop",
        lifecycle={
            "node_state": "ready",
            "draining": False,
            "accepting_new_work": True,
        },
        updated_at=10.0,
    )

    record = status_card_projection_record(card_id="runtime", webspace_id="desktop", now=11.0)

    assert record is not None
    assert record.status == "ready"
    assert record.data["status"] == "online"
    assert record.meta.projection_key == "status-card:runtime"
