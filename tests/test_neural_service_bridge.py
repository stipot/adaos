from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.anyio
async def test_neural_bridge_passes_canonicalization_evidence(monkeypatch):
    from adaos.services.nlu import neural_service_bridge as bridge

    emitted = []
    posted = {}
    usage = []

    class Supervisor:
        async def refresh_discovered(self, force=False):
            posted["refresh_force"] = force

        async def start(self, name):
            posted["started"] = name

        def resolve_base_url(self, name):
            return "http://127.0.0.1:18091"

    def fake_post(url, payload, *, timeout_ms):
        posted["url"] = url
        posted["payload"] = payload
        return {
            "ok": True,
            "result": {
                "top_intent": "weather.get",
                "confidence": 0.91,
                "alternatives": [{"intent": "time.now", "confidence": 0.03}],
                "slots": {},
                "model_id": "test-model",
                "evidence": {"backend": "test"},
            },
        }

    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "_http_post_json", fake_post)
    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(bridge, "bus_emit", lambda _bus, et, payload, source=None: emitted.append((et, payload, source)))
    monkeypatch.setattr(bridge, "record_neural_usage", lambda **kwargs: usage.append(kwargs))
    monkeypatch.setattr(
        bridge,
        "build_entity_trace_stage",
        lambda payload, include_miss=False: {
            "raw": {
                "normalized_text": "open weather on {device}",
                "resolved_entities": [{"canonical_ref": "device:member:node-1"}],
                "ambiguities": [],
            }
        },
    )

    await bridge._on_nlp_intent_detect_neural(
        {
            "text": "open weather on Kitchen",
            "webspace_id": "desktop",
            "request_id": "rid-1",
            "request_locale": "en",
            "preferred_locales": ["en", "ru"],
        }
    )

    assert posted["started"] == "neural_nlu_service_skill"
    assert posted["url"] == "http://127.0.0.1:18091/parse"
    assert posted["payload"]["canonicalized_text"] == "open weather on {device}"
    assert posted["payload"]["entities"]["resolved_entities"][0]["canonical_ref"] == "device:member:node-1"
    assert posted["payload"]["locale"] == "en"
    assert posted["payload"]["preferred_locales"] == ["en", "ru"]
    assert any(event_type == "nlu.trace.stage" and payload["stage"] == "neural" and payload["status"] == "hit" for event_type, payload, _ in emitted)
    detected = [payload for event_type, payload, _ in emitted if event_type == "nlp.intent.detected"][0]
    assert detected["intent"] == "weather.get"
    assert detected["via"] == "neural"
    assert usage[-1]["status"] == "accepted"
    assert usage[-1]["intent"] == "weather.get"
    assert usage[-1]["confidence"] == 0.91
    assert usage[-1]["fallback_to_rasa"] is False
    assert usage[-1]["entity_resolution"]["normalized_text"] == "open weather on {device}"


@pytest.mark.anyio
async def test_neural_bridge_falls_back_without_hot_path_install(monkeypatch):
    from adaos.services.nlu import neural_service_bridge as bridge

    emitted = []
    calls = []
    usage = []

    class Supervisor:
        async def refresh_discovered(self, force=False):
            calls.append(("refresh", force))

        async def start(self, name):
            calls.append(("start", name))

        def resolve_base_url(self, name):
            calls.append(("resolve", name))
            return None

    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(bridge, "bus_emit", lambda _bus, et, payload, source=None: emitted.append((et, payload, source)))
    monkeypatch.setattr(bridge, "record_neural_usage", lambda **kwargs: usage.append(kwargs))

    await bridge._on_nlp_intent_detect_neural({"text": "unmatched", "webspace_id": "desktop", "request_id": "rid-2"})

    assert ("start", "neural_nlu_service_skill") not in calls
    fallback = [payload for event_type, payload, _ in emitted if event_type == "nlp.intent.detect.rasa"][0]
    assert fallback["text"] == "unmatched"
    assert fallback["request_id"] == "rid-2"
    assert any(
        event_type == "nlu.trace.stage" and payload["reason"] == "neural_base_url_unresolved"
        for event_type, payload, _ in emitted
    )
    assert usage[-1]["status"] == "unavailable"
    assert usage[-1]["reason"] == "neural_base_url_unresolved"
    assert usage[-1]["fallback_to_rasa"] is True


@pytest.mark.anyio
async def test_neural_parse_text_uses_bridge_thresholds_and_stats(monkeypatch):
    from adaos.services.nlu import neural_service_bridge as bridge

    usage = []
    posted = {}

    class Supervisor:
        async def refresh_discovered(self, force=False):
            posted["refresh_force"] = force

        async def start(self, name):
            posted["started"] = name

        def resolve_base_url(self, name):
            return "http://127.0.0.1:18091"

    def fake_post(url, payload, *, timeout_ms):
        posted["url"] = url
        posted["payload"] = payload
        return {
            "ok": True,
            "result": {
                "top_intent": "weather.get",
                "confidence": 0.81,
                "slots": {"city": "moscow"},
                "model_id": "unit-model",
                "evidence": {"backend": "test"},
            },
        }

    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "_http_post_json", fake_post)
    monkeypatch.setattr(bridge, "record_neural_usage", lambda **kwargs: usage.append(kwargs))

    result = await bridge.parse_text(
        "weather in moscow",
        webspace_id="desktop",
        locale="ru",
        entity_resolution={"normalized_text": "weather in {city}"},
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["intent"] == "weather.get"
    assert result["slots"] == {"city": "moscow"}
    assert posted["payload"]["canonicalized_text"] == "weather in {city}"
    assert usage[-1]["status"] == "accepted"
    assert usage[-1]["intent"] == "weather.get"
    assert usage[-1]["model_id"] == "unit-model"


@pytest.mark.anyio
async def test_neural_parse_text_low_confidence_falls_back(monkeypatch):
    from adaos.services.nlu import neural_service_bridge as bridge

    usage = []

    class Supervisor:
        async def refresh_discovered(self, force=False):
            pass

        async def start(self, name):
            pass

        def resolve_base_url(self, name):
            return "http://127.0.0.1:18091"

    def fake_post(url, payload, *, timeout_ms):
        return {
            "ok": True,
            "result": {
                "top_intent": "weather.get",
                "confidence": 0.50,
                "slots": {},
                "model_id": "unit-model",
            },
        }

    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "_http_post_json", fake_post)
    monkeypatch.setattr(bridge, "record_neural_usage", lambda **kwargs: usage.append(kwargs))

    result = await bridge.parse_text("maybe weather", webspace_id="desktop", record_usage_stats=True)

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["reason"] == "neural_low_confidence"
    assert result["fallback_to_rasa"] is True
    assert usage[-1]["status"] == "low_confidence"
    assert usage[-1]["fallback_to_rasa"] is True
