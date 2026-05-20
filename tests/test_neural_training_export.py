from __future__ import annotations

import json
from pathlib import Path


def test_neural_training_export_preserves_ownership_and_plain_text() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import IntentMapping, InterpreterWorkspace
    from adaos.services.nlu.data_registry import sync_from_scenarios_and_skills

    ctx = get_ctx()
    sync_from_scenarios_and_skills(ctx)
    ws = InterpreterWorkspace(ctx)
    ws.upsert_intent(
        IntentMapping(
            intent="skill.weather.lookup",
            skill="weather_skill",
            examples=["weather in [Berlin](city)"],
        )
    )
    ws.upsert_intent(
        IntentMapping(
            intent="scenario.modal.open",
            scenario="custom_scenario",
            examples=["open [apps_catalog](modal_id)"],
        )
    )

    summary = ws.export_neural_training_data()

    assert summary["ok"] is True
    assert summary["owners"]["skill"] >= 1
    assert summary["owners"]["scenario"] >= 1
    assert summary["owners"]["system_action"] >= 1

    rows = [
        json.loads(line)
        for line in (ws.root / "neural_training" / "examples_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_intent = {row["intent"]: row for row in rows}

    assert by_intent["skill.weather.lookup"]["text"] == "weather in Berlin"
    assert by_intent["skill.weather.lookup"]["raw_example"] == "weather in [Berlin](city)"
    assert by_intent["skill.weather.lookup"]["owner"] == {"type": "skill", "id": "weather_skill"}

    assert by_intent["scenario.modal.open"]["text"] == "open apps_catalog"
    assert by_intent["scenario.modal.open"]["owner"] == {"type": "scenario", "id": "custom_scenario"}

    assert by_intent["desktop.reload_webspace"]["owner"] == {
        "type": "system_action",
        "id": "host.desktop.webspace.reload",
    }

    labels = json.loads((ws.root / "neural_training" / "labels.json").read_text(encoding="utf-8"))
    assert "desktop.reload_webspace" in labels
    assert "skill.weather.lookup" in labels


def test_neural_curated_reindex_plan_blocks_unknown_active_labels() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import IntentMapping, InterpreterWorkspace

    ctx = get_ctx()
    ws = InterpreterWorkspace(ctx)
    ws.upsert_intent(
        IntentMapping(
            intent="skill.weather.lookup",
            skill="weather_skill",
            examples=["weather in Berlin"],
        )
    )
    active_root = ctx.paths.state_dir() / "nlu" / "neural"
    active_root.mkdir(parents=True)
    (active_root / "model.pt").write_text("model", encoding="utf-8")
    (active_root / "labels.json").write_text(json.dumps(["weather.get"]), encoding="utf-8")
    (active_root / "examples_manifest.jsonl").write_text(
        json.dumps({"intent": "weather.get", "text": "weather"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    plan = ws.plan_neural_curated_reindex(export=True)
    applied = ws.apply_neural_curated_reindex(plan=plan)

    assert plan["compatible_for_active_model"] is False
    assert "skill.weather.lookup" in plan["changes"]["new_labels"]
    assert plan["apply_allowed"] is False
    assert applied["ok"] is False
    assert applied["reason"] == "curated_bundle_incompatible_with_active_model"


def test_neural_curated_reindex_apply_replaces_examples_and_removes_indexes() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import IntentMapping, InterpreterWorkspace

    ctx = get_ctx()
    ws = InterpreterWorkspace(ctx)
    ws.upsert_intent(
        IntentMapping(
            intent="skill.weather.lookup",
            skill="weather_skill",
            examples=["weather in Berlin"],
        )
    )
    ws.export_neural_training_data()
    exported_labels = json.loads((ws.root / "neural_training" / "labels.json").read_text(encoding="utf-8"))
    active_root = ctx.paths.state_dir() / "nlu" / "neural"
    active_root.mkdir(parents=True)
    (active_root / "model.pt").write_text("model", encoding="utf-8")
    (active_root / "labels.json").write_text(json.dumps(exported_labels), encoding="utf-8")
    (active_root / "examples_manifest.jsonl").write_text(
        json.dumps({"intent": "skill.weather.lookup", "text": "old weather"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (active_root / "example_index.pt").write_text("stale", encoding="utf-8")
    (active_root / "faiss.index.json").write_text("stale", encoding="utf-8")

    plan = ws.plan_neural_curated_reindex(export=False)
    applied = ws.apply_neural_curated_reindex(plan=plan)

    assert plan["apply_allowed"] is True
    assert applied["ok"] is True
    assert applied["backup_examples_path"]
    assert not (active_root / "example_index.pt").exists()
    assert not (active_root / "faiss.index.json").exists()
    rows = [json.loads(line) for line in (active_root / "examples_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["intent"] == "skill.weather.lookup" and row["text"] == "weather in Berlin" for row in rows)
    assert (active_root / "curated_reindex.json").exists()


def test_promote_neural_candidate_backs_up_active_model_and_writes_pointer() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import InterpreterWorkspace

    ctx = get_ctx()
    ws = InterpreterWorkspace(ctx)
    active_root = ctx.paths.state_dir() / "nlu" / "neural"
    active_root.mkdir(parents=True)
    (active_root / "model.pt").write_text("old model", encoding="utf-8")
    (active_root / "labels.json").write_text(json.dumps(["old.intent"]), encoding="utf-8")
    (active_root / "vocab.json").write_text(json.dumps(["<pad>"]), encoding="utf-8")
    (active_root / "examples_manifest.jsonl").write_text(
        json.dumps({"intent": "old.intent", "text": "old"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (active_root / "ranker_config.json").write_text("{}", encoding="utf-8")
    (active_root / "metrics.json").write_text(json.dumps({"model_id": "old-model"}), encoding="utf-8")
    (active_root / "faiss.index").write_text("stale", encoding="utf-8")

    candidate = ws.root / "neural_candidates" / "unit"
    candidate.mkdir(parents=True)
    for name, content in {
        "model.pt": "new model",
        "labels.json": json.dumps(["new.intent"]),
        "vocab.json": json.dumps(["<pad>", "n"]),
        "examples_manifest.jsonl": json.dumps({"intent": "new.intent", "text": "new"}, ensure_ascii=False) + "\n",
        "ranker_config.json": "{}",
        "metrics.json": json.dumps({"model_id": "new-model"}),
        "intent_map.json": json.dumps({"schema_version": 1, "intents": []}),
        "intents_manifest.json": json.dumps({"schema_version": 1, "intents": []}),
        "training_report.json": json.dumps({"ok": True}),
    }.items():
        (candidate / name).write_text(content, encoding="utf-8")

    promoted = ws.promote_neural_candidate(candidate_dir=candidate, reason="unit")

    assert promoted["ok"] is True
    assert promoted["model_id"] == "new-model"
    assert (active_root / "model.pt").read_text(encoding="utf-8") == "new model"
    assert not (active_root / "faiss.index").exists()
    pointer = json.loads((active_root / "active_model.json").read_text(encoding="utf-8"))
    assert pointer["rollback_dir"]
    assert (active_root / "rollback" / "latest.json").exists()
    assert (Path(pointer["rollback_dir"]) / "model.pt").read_text(encoding="utf-8") == "old model"
