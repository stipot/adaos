from __future__ import annotations

from adaos.domain import make_projection_record
from adaos.services.projection_records import (
    clear_projection_record_registry,
    get_projection_record,
    projection_record_registry_snapshot,
    write_projection_record,
    write_projection_record_if_valid,
)


def setup_function() -> None:
    clear_projection_record_registry()


def test_projection_record_registry_writes_and_reads_canonical_records() -> None:
    record = make_projection_record(
        projection_key="status-card:runtime",
        kind="status-card",
        webspace_id="desktop",
        data={"summary": "ready"},
        updated_at=10.0,
    )

    written = write_projection_record(record)
    stored = get_projection_record(webspace_id="desktop", projection_key="status-card:runtime")
    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert written.meta.projection_key == "status-card:runtime"
    assert stored is not None
    assert stored.data == {"summary": "ready"}
    assert snapshot["record_total"] == 1
    assert snapshot["ready_total"] == 1
    assert snapshot["registry_version"] == 1


def test_projection_record_registry_tracks_unchanged_writes_by_content() -> None:
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

    write_projection_record(first)
    write_projection_record(second)
    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert snapshot["registry_version"] == 1
    assert snapshot["stats"]["write_total"] == 2
    assert snapshot["stats"]["changed_total"] == 1
    assert snapshot["stats"]["unchanged_total"] == 1


def test_projection_record_registry_filters_by_webspace() -> None:
    for webspace_id in ["desktop", "dev"]:
        write_projection_record(
            make_projection_record(
                projection_key=f"status-card:{webspace_id}",
                kind="status-card",
                webspace_id=webspace_id,
                data={"webspace_id": webspace_id},
            )
        )

    snapshot = projection_record_registry_snapshot(webspace_id="desktop")

    assert snapshot["record_total"] == 1
    assert snapshot["records"][0]["meta"]["projection_key"] == "status-card:desktop"


def test_projection_record_registry_ignores_non_canonical_records() -> None:
    assert write_projection_record_if_valid({"status": "ready", "data": {"ok": True}}) is None
    assert projection_record_registry_snapshot(webspace_id="desktop")["record_total"] == 0
