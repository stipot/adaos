from __future__ import annotations

import json


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
