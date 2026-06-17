from types import SimpleNamespace

import pytest


@pytest.mark.anyio
async def test_teacher_bridge_preserves_rasa_evidence_in_teacher_request(monkeypatch):
    from adaos.services.nlu import teacher_bridge as bridge

    appended_items = []
    appended_events = []
    emitted = []

    async def append_teacher_item(webspace_id, item):
        appended_items.append((webspace_id, item))

    async def append_event(webspace_id, event):
        appended_events.append((webspace_id, event))

    monkeypatch.setattr(bridge, "_ENABLED", True)
    monkeypatch.setattr(
        bridge,
        "get_ctx",
        lambda: SimpleNamespace(
            bus=object(),
            config=SimpleNamespace(root_settings=SimpleNamespace(llm=SimpleNamespace(allow_nlu_teacher=True))),
        ),
    )
    monkeypatch.setattr(bridge, "_append_teacher_item", append_teacher_item)
    monkeypatch.setattr(bridge, "append_event", append_event)
    monkeypatch.setattr(bridge, "make_event", lambda **kwargs: kwargs)
    monkeypatch.setattr(bridge, "bus_emit", lambda _bus, event, payload, source=None: emitted.append((event, payload, source)))

    payload = {
        "text": "weather in moscow",
        "reason": "rasa_low_confidence",
        "via": "rasa",
        "webspace_id": "ws1",
        "request_id": "rid-low",
        "intent": "weather.get_forecast",
        "confidence": 0.42,
        "slots": {"city": "moscow"},
        "entities": [{"entity": "city", "value": "moscow"}],
        "intent_ranking": [
            {"name": "weather.get_forecast", "confidence": 0.42},
            {"name": "desktop.open_modal", "confidence": 0.18},
        ],
        "_raw": {"intent": {"name": "weather.get_forecast", "confidence": 0.42}},
        "_meta": {"trace": "test"},
    }

    await bridge._on_not_obtained(payload)

    assert appended_events
    assert len(appended_items) == 1
    webspace_id, item = appended_items[0]
    assert webspace_id == "ws1"
    assert item["reason"] == "rasa_low_confidence"
    assert item["via"] == "rasa"
    assert item["request_id"] == "rid-low"
    assert item["intent"] == "weather.get_forecast"
    assert item["confidence"] == 0.42
    assert item["slots"] == {"city": "moscow"}
    assert item["entities"] == [{"entity": "city", "value": "moscow"}]
    assert item["intent_ranking"] == [
        {"name": "weather.get_forecast", "confidence": 0.42},
        {"name": "desktop.open_modal", "confidence": 0.18},
    ]
    assert item["_raw"] == {"intent": {"name": "weather.get_forecast", "confidence": 0.42}}
    assert item["_meta"] == {"trace": "test"}

    assert emitted == [
        (
            "nlp.teacher.request",
            {"webspace_id": "ws1", "request": item},
            "nlu.teacher",
        )
    ]
