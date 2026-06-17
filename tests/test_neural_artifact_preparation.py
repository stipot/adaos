from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_prepare_module():
    skill_root = Path("skills/neural_nlu_service_skill").resolve()
    script = skill_root / "scripts" / "prepare_artifacts.py"
    sys.path.insert(0, str(skill_root))
    try:
        spec = importlib.util.spec_from_file_location("neural_prepare_artifacts_test", script)
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


def test_prepare_artifacts_builds_notebook_compatible_layout(tmp_path):
    module = _load_prepare_module()
    source = tmp_path / "example"
    source.mkdir()
    (source / "best_model (1).pt").write_bytes(b"fake model bytes")
    (source / "lbd_train_augmented (3).jsonl").write_text(
        "\n".join(
            [
                json.dumps({"skill": "weather.get", "text": "weather at 7:30"}),
                json.dumps({"skill": "timer.start", "text": "timer for 10 minutes"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "lbd_dev_augmented (3).jsonl").write_text(
        json.dumps({"skill": "weather.get", "text": "weather tomorrow"}) + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "state" / "nlu" / "neural"
    metrics = module.prepare_artifacts(source_root=source, out_dir=out_dir, model_id="unit-model")

    assert (out_dir / "model.pt").read_bytes() == b"fake model bytes"
    assert json.loads((out_dir / "labels.json").read_text(encoding="utf-8")) == ["timer.start", "weather.get"]
    vocab = json.loads((out_dir / "vocab.json").read_text(encoding="utf-8"))
    assert vocab[:4] == ["<pad>", "<unk>", "<bos>", "<eos>"]
    assert "{" in vocab and "}" in vocab
    assert metrics["model_id"] == "unit-model"
    assert metrics["examples_total"] == 3
    assert metrics["label_counts"]["weather.get"] == 2
    intent_map = json.loads((out_dir / "intent_map.json").read_text(encoding="utf-8"))
    assert intent_map["intents"][0]["label"] == "timer.start"
    assert intent_map["intents"][0]["canonical_intent"] == "timer.start"
    assert "intent_map.json" in metrics["artifact_files"]
    ranker_config = json.loads((out_dir / "ranker_config.json").read_text(encoding="utf-8"))
    assert ranker_config["negative_margin_threshold"] > 0
    assert ranker_config["negative_penalty"] > 0

    manifest_lines = (out_dir / "examples_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = [json.loads(line) for line in manifest_lines]
    assert manifest[0]["split"] == "train"
    assert manifest[0]["masked"] == "weather at {time}"
    intents = json.loads((out_dir / "intents_manifest.json").read_text(encoding="utf-8"))
    assert intents["intents"][0]["id"] == "timer.start"
