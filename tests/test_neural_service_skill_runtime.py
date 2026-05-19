from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_detector_module():
    path = Path("skills/neural_nlu_service_skill/handlers/upstream_detector_port.py").resolve()
    spec = importlib.util.spec_from_file_location("neural_nlu_service_skill_detector_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_detector_abstains_cleanly_without_artifacts_or_torch(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    module = _load_detector_module()

    detector = module.Detector()
    result = detector.detect("weather in Berlin", webspace_id="desktop", locale="en")

    assert result["top_intent"] == ""
    assert result["confidence"] == 0.0
    assert result["alternatives"] == []
    assert result["evidence"]["backend"] == "abstain"
    assert result["evidence"]["reason"] in {"torch_unavailable", "model_artifacts_unavailable"}


def test_detector_masks_canonicalized_text_and_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    module = _load_detector_module()

    detector = module.Detector()
    result = detector.detect(
        "wake me at 7:30 on Kitchen",
        webspace_id="desktop",
        locale="en",
        canonicalized_text="wake me at 7:30 on {device}",
        entity_resolution={"resolved_entities": [{"canonical_ref": "device:member:node-1"}]},
    )

    assert result["slots"]["time"] == "7:30"
    assert result["evidence"]["masked_text"] == "wake me at {time} on {device}"
    assert result["evidence"]["canonicalized_text"] == "wake me at 7:30 on {device}"
    assert result["evidence"]["entity_resolution"]["resolved_entities"][0]["canonical_ref"] == "device:member:node-1"


def test_detector_prefers_faiss_pairs_when_index_backend_is_faiss(monkeypatch):
    module = _load_detector_module()
    detector = object.__new__(module.Detector)
    detector._cfg = module.Config()

    class FakeIndex:
        def search(self, query, k):
            assert query == [["query-vector"]]
            assert k == 2
            return [[0.92, 0.41]], [[1, 0]]

    monkeypatch.setattr(detector, "_vectors_to_float32_numpy", lambda _q_vec: [["query-vector"]])

    examples = [
        module.ExampleEntry(skill="timer.start", text="timer for ten minutes", masked="timer for {duration}"),
        module.ExampleEntry(skill="weather.get", text="weather in berlin", masked="weather in {city}"),
    ]
    ranked = detector._nearest_examples(
        object(),
        {
            "example_index_backend": "faiss",
            "example_index": FakeIndex(),
            "examples": examples,
        },
        query="weather in berlin",
        clf_skill="weather.get",
        clf_prob=0.9,
    )

    assert ranked[0]["intent"] == "weather.get"
    assert ranked[0]["similarity"] == 0.92
    assert ranked[0]["matched_example"] == "weather in {city}"


def test_detector_health_exposes_faiss_and_index_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    module = _load_detector_module()
    detector = object.__new__(module.Detector)
    detector._engine = {
        "model_id": "unit-model",
        "examples": [module.ExampleEntry(skill="weather.get", text="weather", masked="weather")],
        "example_index_source": "faiss_disk",
        "example_index_backend": "faiss",
    }

    health = detector.health()

    assert health["version"] == "0.2.7"
    assert "faiss_available" in health
    assert health["example_index"] == "faiss_disk"
    assert health["example_index_backend"] == "faiss"


def test_detector_auto_backend_migrates_valid_torch_cache_to_faiss(monkeypatch, tmp_path):
    module = _load_detector_module()
    detector = object.__new__(module.Detector)
    monkeypatch.setenv("ADAOS_NEURAL_EXAMPLE_INDEX_BACKEND", "auto")
    monkeypatch.setattr(module, "faiss", object())
    monkeypatch.setattr(detector, "_load_faiss_example_index", lambda **_kwargs: None)
    monkeypatch.setattr(
        detector,
        "_load_torch_example_index",
        lambda **_kwargs: {
            "backend": "torch_tensor",
            "source": "torch_disk",
            "vectors": "cached-vectors",
            "index": None,
        },
    )

    def _save_faiss(**kwargs):
        assert kwargs["vectors"] == "cached-vectors"
        return {
            "backend": "faiss",
            "source": "faiss_built",
            "vectors": None,
            "index": object(),
        }

    monkeypatch.setattr(detector, "_save_faiss_example_index", _save_faiss)

    payload = detector._load_example_index(root=tmp_path, model_id="unit-model", engine={})

    assert payload["backend"] == "faiss"
    assert payload["source"] == "faiss_built"


def test_detector_maps_research_labels_to_canonical_intents():
    module = _load_detector_module()
    detector = object.__new__(module.Detector)
    engine = {
        "intent_map": {
            "weather.get": {
                "canonical_intent": "desktop.open_weather",
                "action_id": "host.open_weather",
                "target": {"kind": "system_action"},
            },
            "time.now": {
                "canonical_intent": "desktop.show_time",
                "action_id": "host.show_time",
            },
        }
    }

    mapped = detector._map_intent("weather.get", engine)
    alternatives = detector._map_alternatives(
        [
            {"intent": "weather.get", "confidence": 0.4},
            {"intent": "time.now", "confidence": 0.3},
            {"intent": "alarm.set", "confidence": 0.1},
        ],
        top_intent="desktop.open_weather",
        engine=engine,
    )

    assert mapped["canonical_intent"] == "desktop.open_weather"
    assert mapped["source_label"] == "weather.get"
    assert mapped["action_id"] == "host.open_weather"
    assert alternatives[0]["intent"] == "desktop.show_time"
    assert alternatives[0]["source_label"] == "time.now"
    assert alternatives[0]["action_id"] == "host.show_time"
    assert alternatives[1]["intent"] == "alarm.set"
