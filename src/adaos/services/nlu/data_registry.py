from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from adaos.services.agent_context import AgentContext
from adaos.services.interpreter.workspace import InterpreterWorkspace, IntentMapping
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.nlu.baseline_content import (
    DEFAULT_DESKTOP_SCENARIO_ID,
    default_desktop_nlu,
    merge_default_desktop_nlu,
)
from adaos.services.nlu.feedback_examples import collect_system_action_feedback_examples

_log = logging.getLogger("adaos.nlu.registry")


def _sync_skill_nlu_metadata(ctx: AgentContext) -> int:
    """
    Project skill-level ``skill.yaml["nlu"]`` sections into interpreter
    metadata files (``<skill>/interpreter/intents.yml``) so that
    InterpreterWorkspace.collect_skill_intents() can see them.
    """
    skills_dir = Path(ctx.paths.skills_dir())
    count = 0
    try:
        skills = ctx.skills_repo.list()
    except Exception:  # pragma: no cover
        _log.warning("failed to list skills for nlu registry", exc_info=True)
        return 0

    for meta in skills:
        skill_id = getattr(meta, "id", None)
        skill_name = getattr(skill_id, "value", None) if skill_id is not None else None
        if not skill_name:
            skill_name = getattr(meta, "name", None)
        if not skill_name:
            continue
        root = skills_dir / str(skill_name)
        skill_yaml = root / "skill.yaml"
        if not skill_yaml.exists():
            continue
        try:
            payload = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            _log.debug("failed to read skill.yaml for %s", skill_name, exc_info=True)
            continue

        nlu_section = payload.get("nlu") or {}
        intents_raw = nlu_section.get("intents") or []
        if isinstance(intents_raw, Mapping):
            intent_entries = [
                {"intent": intent_id, **dict(spec)}
                for intent_id, spec in intents_raw.items()
                if isinstance(intent_id, str) and isinstance(spec, Mapping)
            ]
        elif isinstance(intents_raw, list):
            intent_entries = intents_raw
        else:
            intent_entries = []
        if not intent_entries:
            continue

        intents_doc: List[Dict[str, Any]] = []
        for entry in intent_entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("intent")
            if not isinstance(name, str) or not name.strip():
                continue
            utterances = entry.get("utterances") or entry.get("examples") or []
            if not isinstance(utterances, list) or not utterances:
                continue
            intents_doc.append(
                {
                    "intent": str(name).strip(),
                    "description": entry.get("description"),
                    "skill": str(skill_name),
                    "examples": [str(u) for u in utterances if isinstance(u, str) and u.strip()],
                }
            )

        if not intents_doc:
            continue

        meta_dir = root / InterpreterWorkspace.SKILL_METADATA_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        target = meta_dir / InterpreterWorkspace.SKILL_METADATA_FILE
        try:
            target.write_text(
                yaml.safe_dump({"intents": intents_doc}, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            count += len(intents_doc)
        except Exception:
            _log.warning("failed to write interpreter metadata for skill=%s", skill_name, exc_info=True)
    return count


def _intent_mappings_from_nlu(scenario_id: str, nlu_section: Mapping[str, Any]) -> List[IntentMapping]:
    mappings: List[IntentMapping] = []
    intents = nlu_section.get("intents") or {}
    if not isinstance(intents, dict):
        return mappings
    for intent_id, spec in intents.items():
        if not isinstance(intent_id, str) or not isinstance(spec, dict):
            continue
        examples = spec.get("examples") or []
        if not isinstance(examples, list):
            examples = []
        mapping_scenario = scenario_id
        if spec.get("scope") == "system" and spec.get("action_id"):
            mapping_scenario = "system"
        mappings.append(
            IntentMapping(
                intent=intent_id,
                description=spec.get("description"),
                skill=None,
                tool=None,
                scenario=mapping_scenario,
                examples=[str(e) for e in examples if isinstance(e, str) and e.strip()],
            )
        )
    return mappings


def _collect_scenario_intents(ctx: AgentContext) -> List[IntentMapping]:
    mappings: List[IntentMapping] = []
    seen_scenarios: set[str] = set()
    scenarios_root = Path(ctx.paths.scenarios_dir())
    try:
        children = [p for p in scenarios_root.iterdir() if p.is_dir()]
    except Exception:  # pragma: no cover
        _log.warning("failed to list scenarios_dir=%s", scenarios_root, exc_info=True)
        return _intent_mappings_from_nlu(DEFAULT_DESKTOP_SCENARIO_ID, default_desktop_nlu())

    for child in children:
        scenario_id = child.name
        seen_scenarios.add(scenario_id)
        try:
            content = scenarios_loader.read_content(scenario_id)
        except FileNotFoundError:
            continue
        except Exception:
            _log.warning("failed to read scenario.json for %s", scenario_id, exc_info=True)
            continue
        if not isinstance(content, dict):
            continue
        nlu_section = content.get("nlu") or {}
        nlu_section = merge_default_desktop_nlu(scenario_id, nlu_section if isinstance(nlu_section, Mapping) else {})
        mappings.extend(_intent_mappings_from_nlu(scenario_id, nlu_section))

    if DEFAULT_DESKTOP_SCENARIO_ID not in seen_scenarios:
        mappings.extend(_intent_mappings_from_nlu(DEFAULT_DESKTOP_SCENARIO_ID, default_desktop_nlu()))
    return mappings


def sync_from_scenarios_and_skills(ctx: AgentContext) -> Dict[str, Any]:
    """
    Refresh interpreter workspace datasets/config from installed skills and scenarios.

    This is pure-Python and does not depend on a particular NLU engine.
    """
    ws = InterpreterWorkspace(ctx)
    skill_count = _sync_skill_nlu_metadata(ctx)
    scenario_mappings = _collect_scenario_intents(ctx)
    system_feedback = collect_system_action_feedback_examples(ctx)
    if system_feedback:
        for mapping in scenario_mappings:
            if mapping.scenario != "system" or not mapping.intent:
                continue
            extra = system_feedback.get(mapping.intent) or []
            if extra:
                seen: set[str] = set()
                merged: list[str] = []
                for item in [*mapping.examples, *extra]:
                    if not isinstance(item, str) or not item.strip():
                        continue
                    token = item.strip()
                    if token in seen:
                        continue
                    seen.add(token)
                    merged.append(token)
                mapping.examples = merged
    system_action_count = len(
        {
            mapping.intent
            for mapping in scenario_mappings
            if mapping.scenario == "system" and isinstance(mapping.intent, str) and mapping.intent
        }
    )

    for mapping in scenario_mappings:
        ws.upsert_intent(mapping)

    _log.info(
        "nlu registry sync: skills_intents=%d scenario_intents=%d system_action_intents=%d",
        skill_count,
        len(scenario_mappings),
        system_action_count,
    )
    return {
        "skills_intents": skill_count,
        "scenario_intents": len(scenario_mappings),
        "system_action_intents": system_action_count,
    }

