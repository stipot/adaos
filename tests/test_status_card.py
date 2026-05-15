from __future__ import annotations

from adaos.domain import (
    is_status_card_stale,
    make_status_card,
    make_status_card_projection_record,
    normalize_status_card_status,
)


def test_status_card_status_normalization_matches_operational_tokens() -> None:
    assert normalize_status_card_status("running") == "online"
    assert normalize_status_card_status("failed") == "offline"
    assert normalize_status_card_status("partial") == "degraded"
    assert normalize_status_card_status("pending_update") == "warning"
    assert normalize_status_card_status("mystery") == "unknown"


def test_make_status_card_builds_platform_emitter_shape() -> None:
    card = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        details_ref={"kind": "tool", "receiver": "infrastate_skill", "tool": "get_snapshot"},
        updated_at=10.0,
    )

    payload = card.to_dict()

    assert payload["id"] == "runtime"
    assert payload["owner"] == "core:runtime"
    assert payload["kind"] == "runtime"
    assert payload["scope"] == {"node_id": "node-a"}
    assert payload["webspace_id"] == "desktop"
    assert payload["status"] == "online"
    assert payload["severity"] == "low"
    assert payload["summary"] == "Runtime ready"
    assert payload["ttl_ms"] == 5000
    assert payload["version"] == 1
    assert payload["changed_at"] == 10.0
    assert payload["fingerprint"]
    assert payload["details_ref"] == {
        "kind": "tool",
        "receiver": "infrastate_skill",
        "tool": "get_snapshot",
    }


def test_status_card_preserves_change_tracking_when_content_is_same() -> None:
    first = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )

    second = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        status="running",
        summary="Runtime ready",
        previous=first,
        updated_at=20.0,
    )

    assert second.version == 1
    assert second.changed_at == 10.0
    assert second.fingerprint == first.fingerprint


def test_status_card_increments_version_when_content_changes() -> None:
    first = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )

    second = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        status="failed",
        summary="Runtime down",
        previous=first,
        updated_at=20.0,
    )

    assert second.version == 2
    assert second.changed_at == 20.0
    assert second.fingerprint != first.fingerprint
    assert second.severity == "critical"


def test_status_card_staleness_uses_ttl_ms() -> None:
    card = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        status="running",
        summary="Runtime ready",
        ttl_ms=5000,
        updated_at=10.0,
    )

    assert not is_status_card_stale(card, now=14.9)
    assert is_status_card_stale(card, now=16.0)


def test_status_card_projection_record_aligns_with_projection_abi() -> None:
    card = make_status_card(
        id="runtime",
        owner="core:runtime",
        kind="runtime",
        scope={"node_id": "node-a"},
        webspace_id="desktop",
        status="running",
        summary="Runtime ready",
        updated_at=10.0,
    )

    record = make_status_card_projection_record(
        card,
        node_id="node-a",
        source_authority="platform",
        access={"visibility": "operator"},
    )
    payload = record.to_dict()

    assert payload["status"] == "ready"
    assert payload["data"]["id"] == "runtime"
    assert payload["meta"]["projection_key"] == "status-card:runtime"
    assert payload["meta"]["kind"] == "status-card"
    assert payload["meta"]["webspace_id"] == "desktop"
    assert payload["meta"]["node_id"] == "node-a"
    assert payload["meta"]["source"] == "core:runtime"
    assert payload["meta"]["source_authority"] == "platform"
    assert payload["meta"]["fingerprint"] == card.fingerprint
    assert payload["meta"]["version"] == card.version
    assert payload["meta"]["changed_at"] == card.changed_at
    assert payload["meta"]["access"] == {"visibility": "operator"}
