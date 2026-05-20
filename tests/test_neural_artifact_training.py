from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_train_module():
    pytest.importorskip("torch")
    skill_root = Path("skills/neural_nlu_service_skill").resolve()
    script = skill_root / "scripts" / "train_artifacts.py"
    sys.path.insert(0, str(skill_root))
    try:
        spec = importlib.util.spec_from_file_location("neural_train_artifacts_test", script)
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


def test_train_artifacts_writes_runtime_compatible_candidate(tmp_path):
    module = _load_train_module()
    examples = tmp_path / "examples_manifest.jsonl"
    examples.write_text(
        "\n".join(
            [
                json.dumps({"intent": "weather.get", "text": "weather in Berlin"}),
                json.dumps({"intent": "weather.get", "text": "weather in Moscow"}),
                json.dumps({"intent": "timer.start", "text": "timer for 10 minutes"}),
                json.dumps({"intent": "timer.start", "text": "start timer for 5 minutes"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "candidate"

    result = module.train_artifacts(examples_path=examples, out_dir=out_dir, epochs=1, batch_size=2, min_dev_accuracy=0.0)

    assert result["ok"] is True
    assert (out_dir / "model.pt").exists()
    assert json.loads((out_dir / "labels.json").read_text(encoding="utf-8")) == ["timer.start", "weather.get"]
    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["examples_total"] == 4
    assert metrics["labels_total"] == 2
    assert metrics["model_sha256"]
    report = json.loads((out_dir / "training_report.json").read_text(encoding="utf-8"))
    assert report["split_strategy"] == "auto_holdout"
