from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _examples_for(payload: dict, intent: str) -> str:
    entries = payload.get("nlu") or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("intent") == intent:
            return str(entry.get("examples") or "")
    return ""


def _lookup_examples_for(payload: dict, lookup: str) -> str:
    entries = payload.get("nlu") or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("lookup") == lookup:
            return str(entry.get("examples") or "")
    return ""


def test_default_desktop_nlu_sync_exports_modal_intents_to_rasa_project() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.interpreter.workspace import InterpreterWorkspace
    from adaos.services.nlu.data_registry import sync_from_scenarios_and_skills

    ctx = get_ctx()
    summary = sync_from_scenarios_and_skills(ctx)

    assert summary["scenario_intents"] >= 2
    assert summary["system_action_intents"] >= 4

    ws = InterpreterWorkspace(ctx)
    project = ws.build_rasa_project()
    dataset = yaml.safe_load((project / "data" / "intents_from_config.yml").read_text(encoding="utf-8")) or {}

    open_modal_examples = _examples_for(dataset, "desktop.open_modal")
    open_node_modal_examples = _examples_for(dataset, "desktop.open_node_modal")

    assert "[apps_catalog](modal_id)" in open_modal_examples
    assert "[nlu_teacher_modal](modal_id)" in open_modal_examples
    assert "[member-1](node_ref)" in open_node_modal_examples
    assert "reload desktop" in _examples_for(dataset, "desktop.reload_webspace")
    assert "switch to [web_desktop](scenario_id)" in _examples_for(dataset, "desktop.switch_scenario")
    assert "- apps_catalog" in _lookup_examples_for(dataset, "modal_id")
    assert "- nlu_teacher_modal" in _lookup_examples_for(dataset, "modal_id")
    assert "- nlu_teacher_app" in _lookup_examples_for(dataset, "app_id")
    assert "- web_desktop" in _lookup_examples_for(dataset, "scenario_id")
    assert (Path(project) / "data" / "lookup_tables.json").exists()

    rasa_config = yaml.safe_load((Path(project) / "config.yml").read_text(encoding="utf-8")) or {}
    assert "policies" not in rasa_config
    pipeline_names = [item.get("name") for item in rasa_config.get("pipeline", []) if isinstance(item, dict)]
    assert "DIETClassifier" not in pipeline_names
    assert "CRFEntityExtractor" in pipeline_names
    assert "LogisticRegressionClassifier" in pipeline_names


@pytest.mark.anyio
async def test_default_desktop_nlu_dispatches_named_node_modal_open() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.dispatcher import _on_nlp_intent_detected

    ctx = get_ctx()
    emitted: list[dict] = []
    ctx.bus.subscribe("desktop.modal.open", lambda ev: emitted.append(dict(ev.payload or {})))

    await _on_nlp_intent_detected(
        {
            "intent": "desktop.open_node_modal",
            "confidence": 0.95,
            "slots": {"modal_id": "apps_catalog", "node_ref": "member-1"},
            "text": "open apps_catalog on node member-1",
            "webspace_id": "desktop",
        }
    )

    assert emitted == [
        {
            "modal_id": "apps_catalog",
            "node_ref": "member-1",
            "target_node_id": "member-1",
            "webspace_id": "desktop",
            "slots": {"modal_id": "apps_catalog", "node_ref": "member-1"},
            "text": "open apps_catalog on node member-1",
            "_meta": {"webspace_id": "desktop", "scenario_id": "web_desktop"},
        }
    ]


@pytest.mark.anyio
async def test_default_desktop_nlu_dispatches_system_webspace_reload() -> None:
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.dispatcher import _on_nlp_intent_detected

    ctx = get_ctx()
    emitted: list[dict] = []
    ctx.bus.subscribe("desktop.webspace.reload", lambda ev: emitted.append(dict(ev.payload or {})))

    await _on_nlp_intent_detected(
        {
            "intent": "desktop.reload_webspace",
            "confidence": 0.95,
            "slots": {},
            "text": "reload desktop",
            "webspace_id": "desktop",
        }
    )

    assert emitted == [
        {
            "webspace_id": "desktop",
            "text": "reload desktop",
            "_meta": {"webspace_id": "desktop", "scenario_id": "web_desktop"},
        }
    ]
