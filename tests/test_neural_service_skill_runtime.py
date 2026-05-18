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
