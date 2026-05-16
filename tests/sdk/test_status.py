from __future__ import annotations

from pathlib import Path

import pytest

from adaos.sdk import status
from adaos.services.agent_context import clear_ctx, get_ctx
from adaos.services.status_card_registry import (
    clear_status_card_registry,
    status_card_projection_record,
    status_card_registry_snapshot,
)


def setup_function() -> None:
    clear_status_card_registry()


def test_publish_status_accepts_explicit_owner_without_yjs() -> None:
    clear_ctx()

    card = status.publish_status(
        id="runtime",
        owner="core:test",
        kind="runtime",
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )
    record = status_card_projection_record(card_id="runtime", webspace_id="desktop", now=11.0)

    assert card.owner == "core:test"
    assert card.status == "online"
    assert record is not None
    assert record.meta.projection_key == "status-card:runtime"


def test_publish_status_preserves_current_skill_owner() -> None:
    ctx = get_ctx()
    skill_dir = Path(ctx.paths.skills_dir()) / "weather_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    assert ctx.skill_ctx.set("weather_skill", skill_dir)

    try:
        card = status.publish_status(
            id="weather",
            kind="widget",
            webspace_id="desktop",
            status="partial",
            summary="Weather source degraded",
            updated_at=10.0,
        )
    finally:
        ctx.skill_ctx.clear()

    assert card.owner == "skill:weather_skill"
    assert card.status == "degraded"


def test_publish_status_requires_owner_without_current_skill() -> None:
    ctx = get_ctx()
    ctx.skill_ctx.clear()

    with pytest.raises(ValueError, match="owner is required"):
        status.publish_status(
            id="weather",
            kind="widget",
            webspace_id="desktop",
            status="running",
            summary="Weather ready",
        )


def test_publish_status_many_applies_shared_defaults() -> None:
    cards = status.publish_status_many(
        [
            {
                "id": "weather",
                "status": "running",
                "summary": "Weather ready",
            },
            {
                "id": "voice",
                "status": "failed",
                "summary": "Voice unavailable",
            },
        ],
        owner="skill:dashboard",
        kind="widget",
        webspace_id="desktop",
        updated_at=10.0,
    )
    snapshot = status_card_registry_snapshot(webspace_id="desktop", now=11.0)

    assert [card.id for card in cards] == ["weather", "voice"]
    assert snapshot["card_total"] == 2
    assert snapshot["stats"]["changed_total"] == 2


def test_publish_status_stream_points_details_to_receiver() -> None:
    card = status.publish_status_stream(
        id="operations",
        owner="skill:infrastate",
        kind="operations",
        webspace_id="desktop",
        status="running",
        summary="Operations active",
        receiver="infrastate.operations.active",
        path="streams/operations",
        params={"limit": 10},
        updated_at=10.0,
    )

    payload = card.to_dict()

    assert payload["details_ref"] == {
        "kind": "stream",
        "receiver": "infrastate.operations.active",
        "path": "streams/operations",
        "params": {"limit": 10},
    }
