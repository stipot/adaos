from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


@pytest.mark.anyio
async def test_teacher_save_example_routes_to_scenario_artifact() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.teacher_runtime import _on_example_save
    from adaos.services.yjs.doc import async_get_ydoc

    ctx = get_ctx()
    webspace_id = "ws-save-scenario"
    scenario_id = "web_desktop"
    scenario_root = Path(ctx.paths.scenarios_dir()) / scenario_id
    scenario_root.mkdir(parents=True, exist_ok=True)
    scenario_json = scenario_root / "scenario.json"
    scenario_json.write_text(
        json.dumps({"id": scenario_id, "version": "0.0.1", "nlu": {"intents": {}}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    emitted: list[tuple[str, dict]] = []
    ctx.bus.subscribe("nlp.teacher.example.saved", lambda ev: emitted.append((ev.type, dict(ev.payload or {}))))

    await _on_example_save(
        {
            "webspace_id": webspace_id,
            "request_id": "rid-save-1",
            "text": "open diagnostics panel",
            "intent": "desktop.open_modal",
            "slots": {"modal_id": "diagnostics"},
            "target": {"type": "scenario", "id": scenario_id},
            "_meta": {"webspace_id": webspace_id},
        }
    )

    saved = json.loads(scenario_json.read_text(encoding="utf-8"))
    examples = saved["nlu"]["intents"]["desktop.open_modal"]["examples"]
    assert examples == ["open diagnostics panel"]

    async with async_get_ydoc(webspace_id) as ydoc:
        dataset = ((ydoc.get_map("data").get("nlu_teacher") or {}).get("dataset")) or []
    assert dataset[-1]["status"] == "positive_feedback"
    assert dataset[-1]["audit"]["request_id"] == "rid-save-1"
    assert emitted[-1][0] == "nlp.teacher.example.saved"


@pytest.mark.anyio
async def test_teacher_save_example_routes_to_skill_artifact() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.teacher_runtime import _on_example_save

    ctx = get_ctx()
    skill_name = "weather_skill"
    skill_root = Path(ctx.paths.skills_dir()) / skill_name
    skill_root.mkdir(parents=True, exist_ok=True)
    skill_yaml = skill_root / "skill.yaml"
    skill_yaml.write_text(
        yaml.safe_dump(
            {
                "name": skill_name,
                "version": "0.1.0",
                "nlu": {"intents": [{"intent": "weather.lookup", "examples": ["weather in Berlin"]}]},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    await _on_example_save(
        {
            "webspace_id": "ws-save-skill",
            "text": "forecast for Paris",
            "intent": "weather.lookup",
            "target": {"type": "skill", "id": skill_name},
        }
    )

    saved = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
    intents = ((saved.get("nlu") or {}).get("intents")) or []
    weather = next(item for item in intents if item.get("intent") == "weather.lookup")
    assert weather["examples"] == ["weather in Berlin", "forecast for Paris"]


@pytest.mark.anyio
async def test_teacher_save_example_routes_to_system_action_feedback_and_exports() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import InterpreterWorkspace
    from adaos.services.nlu.data_registry import sync_from_scenarios_and_skills
    from adaos.services.nlu.feedback_examples import system_action_feedback_path
    from adaos.services.nlu.teacher_runtime import _on_example_save

    ctx = get_ctx()
    await _on_example_save(
        {
            "webspace_id": "ws-save-system",
            "request_id": "rid-save-system",
            "text": "reload this workspace now",
            "intent": "desktop.reload_webspace",
            "slots": {},
            "target": {"type": "system_action", "id": "host.desktop.webspace.reload"},
            "source": "unit-test",
        }
    )

    feedback_path = system_action_feedback_path(ctx)
    rows = [json.loads(line) for line in feedback_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["target"] == {"type": "system_action", "id": "host.desktop.webspace.reload"}
    assert rows[-1]["audit"]["request_id"] == "rid-save-system"

    sync_from_scenarios_and_skills(ctx)
    ws = InterpreterWorkspace(ctx)
    project = ws.build_rasa_project()
    dataset = yaml.safe_load((project / "data" / "intents_from_config.yml").read_text(encoding="utf-8")) or {}
    examples = ""
    for entry in dataset.get("nlu") or []:
        if isinstance(entry, dict) and entry.get("intent") == "desktop.reload_webspace":
            examples = str(entry.get("examples") or "")
            break
    assert "reload this workspace now" in examples

    summary = ws.export_neural_training_data()
    assert summary["owners"]["system_action"] >= 1
    manifest = (ws.root / "neural_training" / "examples_manifest.jsonl").read_text(encoding="utf-8")
    assert "reload this workspace now" in manifest

