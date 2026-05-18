from __future__ import annotations

from adaos.services.nlu.neural_usage_stats import (
    neural_usage_stats_path,
    read_neural_usage_stats,
    record_neural_fallback_outcome,
    record_neural_usage,
)


def test_record_neural_usage_aggregates_status_latency_and_canonicalization():
    record_neural_usage(
        status="accepted",
        reason="neural_accepted",
        text="open weather on Kitchen",
        webspace_id="desktop",
        request_id="rid-1",
        intent="weather.get",
        confidence=0.91,
        latency_ms=12.5,
        model_id="test-model",
        entity_resolution={
            "normalized_text": "open weather on {device}",
            "resolved_entities": [{"canonical_ref": "device:member:node-1"}],
            "ambiguities": [],
        },
        fallback_to_rasa=False,
    )
    record_neural_usage(
        status="low_confidence",
        reason="neural_low_confidence",
        text="open maybe something",
        webspace_id="desktop",
        request_id="rid-2",
        intent="weather.get",
        confidence=0.51,
        latency_ms=20.0,
        entity_resolution={"unresolved_entity_spans": [{"text": "maybe"}]},
        fallback_to_rasa=True,
    )

    path = neural_usage_stats_path()
    assert path.exists()
    stats = read_neural_usage_stats()

    assert stats["totals"]["requests"] == 2
    assert stats["totals"]["fallback_to_rasa"] == 1
    assert stats["totals"]["fallback_ratio"] == 0.5
    assert stats["by_status"]["accepted"] == 1
    assert stats["by_status"]["low_confidence"] == 1
    assert stats["by_intent"]["weather.get"]["accepted"] == 1
    assert stats["by_intent"]["weather.get"]["low_confidence"] == 1
    assert stats["confidence_bands"]["0.80-1.00"] == 1
    assert stats["confidence_bands"]["0.45-0.79"] == 1
    assert stats["canonicalization"]["hit"] == 1
    assert stats["canonicalization"]["unresolved"] == 1
    assert stats["latency_ms"]["count"] == 2
    assert stats["latency_ms"]["min"] == 12.5
    assert stats["latency_ms"]["max"] == 20.0
    assert stats["recent"][-1]["fallback_to_rasa"] is True
    assert stats["review_samples"][-1]["text"] == "open maybe something"


def test_record_neural_fallback_outcome_links_downstream_rasa_result():
    record_neural_usage(
        status="low_confidence",
        reason="neural_low_confidence",
        text="open maybe something",
        webspace_id="desktop",
        request_id="rid-downstream",
        intent="weather.get",
        confidence=0.51,
        fallback_to_rasa=True,
    )

    stats = record_neural_fallback_outcome(
        request_id="rid-downstream",
        status="accepted",
        reason="rasa_accepted",
        intent="desktop.open_weather",
        confidence=0.87,
    )

    downstream = stats["downstream"]["rasa_after_neural"]
    assert downstream["total"] == 1
    assert downstream["by_status"]["accepted"] == 1
    assert downstream["by_reason"]["rasa_accepted"] == 1
    assert downstream["by_intent"]["desktop.open_weather"] == 1
    assert stats["recent"][-1]["downstream_rasa"]["intent"] == "desktop.open_weather"
    assert stats["review_samples"][-1]["downstream_rasa"]["status"] == "accepted"
