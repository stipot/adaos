from __future__ import annotations

from adaos.domain import Event, enrich_event_payload, event_envelope_contract_snapshot, normalize_event_envelope


def test_normalize_event_envelope_accepts_legacy_event() -> None:
    event = Event(type="node.status", payload={"state": "ready"}, source="test", ts=10.5)

    envelope = normalize_event_envelope(event)

    assert envelope.type == "node.status"
    assert envelope.source == "test"
    assert envelope.ts == 10.5
    assert envelope.payload == {"state": "ready"}
    assert envelope.event_id is None


def test_normalize_event_envelope_reads_nested_event_metadata() -> None:
    payload = {
        "state": "ready",
        "_meta": {
            "event": {
                "event_id": "evt-1",
                "trace_id": "trace-1",
                "cause_event_id": "evt-0",
                "source_authority": "platform",
                "scope": {"webspace_id": "desktop", "node_id": "node-a"},
                "actor": {"kind": "system"},
                "schema": "node.status",
                "version": 2,
                "priority": "normal",
            }
        },
    }
    event = Event(type="node.status", payload=payload, source="runtime", ts=11.0)

    envelope = normalize_event_envelope(event)

    assert envelope.event_id == "evt-1"
    assert envelope.trace_id == "trace-1"
    assert envelope.cause_event_id == "evt-0"
    assert envelope.source_authority == "platform"
    assert envelope.scope == {"webspace_id": "desktop", "node_id": "node-a"}
    assert envelope.actor == {"kind": "system"}
    assert envelope.schema == "node.status"
    assert envelope.version == 2
    assert envelope.priority == "normal"


def test_normalize_event_envelope_keeps_flat_meta_compatibility() -> None:
    event = Event(
        type="legacy.event",
        payload={"_meta": {"trace_id": "trace-legacy", "authority": "skill"}},
        source="legacy",
        ts=1.0,
    )

    envelope = normalize_event_envelope(event)

    assert envelope.trace_id == "trace-legacy"
    assert envelope.source_authority == "skill"


def test_enrich_event_payload_copies_payload_and_preserves_existing_meta() -> None:
    original = {"value": 1, "_meta": {"owner": "skill-a"}}

    enriched = enrich_event_payload(
        original,
        event_id="evt-2",
        trace_id="trace-2",
        scope={"webspace_id": "desktop"},
        schema="demo.event",
        version=1,
    )

    assert original == {"value": 1, "_meta": {"owner": "skill-a"}}
    assert enriched["_meta"]["owner"] == "skill-a"
    assert enriched["_meta"]["event"] == {
        "event_id": "evt-2",
        "trace_id": "trace-2",
        "scope": {"webspace_id": "desktop"},
        "schema": "demo.event",
        "version": 1,
    }


def test_enrich_event_payload_can_generate_event_id() -> None:
    enriched = enrich_event_payload({}, generate_event_id=True)

    event_id = enriched["_meta"]["event"]["event_id"]
    assert isinstance(event_id, str)
    assert event_id


def test_event_envelope_contract_snapshot_exposes_shared_abi() -> None:
    snapshot = event_envelope_contract_snapshot(now=20.0)

    assert snapshot["contract"] == "adaos.operational-event-envelope.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["meta_path"] == "_meta.event"
    assert snapshot["required_fields"] == ["type", "source", "ts", "payload"]
    assert "trace_id" in snapshot["metadata_fields"]
    assert snapshot["compatibility"]["legacy_event_supported"] is True
    assert snapshot["compatibility"]["nested_meta_preferred"] is True
    assert "mutating original payload during enrichment" in snapshot["ownership"]["forbidden"]
    assert snapshot["normalized_example"]["event_id"] == "evt-demo-1"
    assert snapshot["dispatcher_ready"] is True
