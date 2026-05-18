from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_eval_module():
    skill_root = Path("skills/neural_nlu_service_skill").resolve()
    script = skill_root / "scripts" / "evaluate_golden.py"
    sys.path.insert(0, str(skill_root))
    try:
        spec = importlib.util.spec_from_file_location("neural_evaluate_golden_test", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        try:
            sys.path.remove(str(skill_root))
        except ValueError:
            pass


def test_evaluate_cases_reports_accuracy_and_compact_evidence():
    module = _load_eval_module()

    class FakeDetector:
        def detect(self, text, **kwargs):
            if "weather" in text:
                return {
                    "top_intent": "weather.get",
                    "confidence": 0.9,
                    "slots": {"city": "moscow"},
                    "evidence": {"backend": "test", "ranker": "softmax", "example_index": "disk"},
                }
            return {"top_intent": "timer.start", "confidence": 0.8, "slots": {}, "evidence": {"backend": "test"}}

        def health(self):
            return {"ok": True, "model_loaded": True}

    report = module.evaluate_cases(
        FakeDetector(),
        [
            {"id": "w", "text": "weather in moscow", "expected_intent": "weather.get"},
            {"id": "t", "text": "start timer", "expected_intent": "timer.start"},
            {"id": "bad", "text": "weather maybe", "expected_intent": "news.get"},
        ],
    )

    assert report["total"] == 3
    assert report["passed"] == 2
    assert report["accuracy"] == 0.666667
    assert report["cases"][0]["example_index"] == "disk"
    assert report["health"]["model_loaded"] is True
