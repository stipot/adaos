from __future__ import annotations

from adaos.domain import (
    make_projection_record,
    normalize_projection_access_metadata,
    normalize_projection_record,
    projection_fingerprint,
)


def test_projection_fingerprint_is_stable_for_json_like_data() -> None:
    left = {"status": "ready", "counts": {"b": 2, "a": 1}}
    right = {"counts": {"a": 1, "b": 2}, "status": "ready"}

    assert projection_fingerprint(left) == projection_fingerprint(right)


def test_make_projection_record_uses_canonical_shape() -> None:
    record = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        node_id="node-a",
        data={"summary": "ready"},
        source="runtime",
        source_authority="platform",
        access={"visibility": "operator"},
        updated_at=10.0,
    )

    payload = record.to_dict()

    assert set(payload) == {"status", "data", "meta"}
    assert payload["status"] == "ready"
    assert payload["data"] == {"summary": "ready"}
    assert payload["meta"]["projection_key"] == "status-card:runtime"
    assert payload["meta"]["kind"] == "status-card"
    assert payload["meta"]["webspace_id"] == "desktop"
    assert payload["meta"]["node_id"] == "node-a"
    assert payload["meta"]["version"] == 1
    assert payload["meta"]["updated_at"] == 10.0
    assert payload["meta"]["changed_at"] == 10.0
    assert payload["meta"]["source"] == "runtime"
    assert payload["meta"]["source_authority"] == "platform"
    assert payload["meta"]["access"] == {
        "visibility": "operator",
        "audience": "shared",
        "read_only": False,
        "sensitive": False,
        "actions_allowed": [],
        "display_hints": {},
    }


def test_make_projection_record_preserves_change_time_when_fingerprint_matches() -> None:
    first = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        updated_at=10.0,
    )

    second = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        previous=first,
        updated_at=20.0,
    )

    assert second.meta.version == 1
    assert second.meta.updated_at == 20.0
    assert second.meta.changed_at == 10.0


def test_make_projection_record_increments_version_when_payload_changes() -> None:
    first = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        updated_at=10.0,
    )

    second = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "degraded"},
        previous=first,
        updated_at=20.0,
    )

    assert second.meta.version == 2
    assert second.meta.changed_at == 20.0


def test_normalize_projection_record_accepts_existing_mapping() -> None:
    record = normalize_projection_record(
        {
            "status": "stale",
            "data": {"value": 1},
            "meta": {
                "projection_key": "demo",
                "kind": "test",
                "webspace_id": "desktop",
                "fingerprint": "abc",
                "access": {"role": "operator"},
            },
            "error": {"message": "old"},
        }
    )

    assert record.status == "stale"
    assert record.data == {"value": 1}
    assert record.meta.projection_key == "demo"
    assert record.meta.access == {
        "role": "operator",
        "audience": "shared",
        "read_only": False,
        "sensitive": False,
        "actions_allowed": [],
        "display_hints": {},
    }
    assert record.error == {"message": "old"}


def test_projection_access_metadata_keeps_owner_and_guest_payload_shared() -> None:
    data = {"summary": "same shared payload"}
    owner = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data=data,
        access=normalize_projection_access_metadata(audience="owner", actions_allowed=["rename"]),
    )
    guest = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data=data,
        access=normalize_projection_access_metadata(
            audience="guest",
            read_only=True,
            display_hints={"actions": "hidden"},
        ),
    )

    assert owner.data == guest.data
    assert owner.meta.fingerprint == guest.meta.fingerprint
    assert owner.meta.access["audience"] == "owner"
    assert owner.meta.access["actions_allowed"] == ["rename"]
    assert guest.meta.access["audience"] == "guest"
    assert guest.meta.access["read_only"] is True
    assert guest.meta.access["display_hints"] == {"actions": "hidden"}


def test_projection_access_metadata_supports_dev_audience() -> None:
    record = make_projection_record(
        projection_key="projection:prompt-debug",
        kind="diagnostic",
        webspace_id="dev-prompt-engineer",
        data={"panel": "prompt-debug"},
        access=normalize_projection_access_metadata(
            audience="dev",
            sensitive=True,
            actions_allowed=["inspect", "replay"],
            display_hints={"surface": "prompt-engineer"},
        ),
    )

    assert record.meta.access == {
        "audience": "dev",
        "read_only": False,
        "sensitive": True,
        "actions_allowed": ["inspect", "replay"],
        "display_hints": {"surface": "prompt-engineer"},
    }
