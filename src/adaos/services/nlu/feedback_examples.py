from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.scenarios import loader as scenarios_loader


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _record_id(*, intent: str, text: str, target: Mapping[str, Any]) -> str:
    digest = hashlib.sha1(
        json.dumps(
            {"intent": intent, "text": text, "target": dict(target)},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return "fb." + digest[:16]


def system_action_feedback_path(ctx: AgentContext | None = None) -> Path:
    ctx = ctx or get_ctx()
    return (Path(ctx.paths.state_dir()) / "interpreter" / "system_action_feedback.jsonl").resolve()


def _normalize_target(target: Mapping[str, Any] | None, *, intent: str) -> dict[str, Any]:
    target = target if isinstance(target, Mapping) else {}
    target_type = str(target.get("type") or "").strip()
    target_id = str(target.get("id") or "").strip()
    if target_type == "system_action" and not target_id:
        try:
            from adaos.services.nlu.system_actions_catalog import find_system_action_by_intent

            action = find_system_action_by_intent(intent)
            if isinstance(action, dict) and isinstance(action.get("id"), str):
                target_id = action["id"]
        except Exception:
            target_id = ""
    out = {"type": target_type}
    if target_id:
        out["id"] = target_id
    return out


def write_scenario_example(*, scenario_id: str, intent: str, example: str) -> dict[str, Any]:
    scenario_id = str(scenario_id or "").strip()
    intent = str(intent or "").strip()
    example = str(example or "").strip()
    if not scenario_id or not intent or not example:
        return {"ok": False, "reason": "missing_scenario_intent_or_example"}

    path = scenarios_loader.scenario_root(scenario_id) / "scenario.json"
    if not path.exists():
        return {"ok": False, "reason": "scenario_not_found", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "reason": "scenario_read_failed", "path": str(path), "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "scenario_payload_not_object", "path": str(path)}

    nlu = payload.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
        payload["nlu"] = nlu
    intents = nlu.get("intents")
    if not isinstance(intents, dict):
        intents = {}
        nlu["intents"] = intents
    spec = intents.get(intent)
    if not isinstance(spec, dict):
        spec = {"scope": "scenario", "examples": []}
        intents[intent] = spec

    existing = spec.get("examples")
    existing_list = [str(item) for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    spec["examples"] = _dedupe_keep_order([*existing_list, example])

    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "reason": "scenario_write_failed", "path": str(path), "error": str(exc)}
    scenarios_loader.invalidate_cache(scenario_id=scenario_id, space="workspace")
    return {"ok": True, "target": {"type": "scenario", "id": scenario_id}, "path": str(path)}


def _upsert_skill_intent_list(intents: list[Any], *, intent: str, example: str) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = [dict(item) for item in intents if isinstance(item, dict)]
    for item in cleaned:
        name = item.get("name") or item.get("intent")
        if isinstance(name, str) and name.strip() == intent:
            examples = item.get("examples")
            if not isinstance(examples, list):
                examples = item.get("utterances")
            existing = [str(value) for value in examples if isinstance(value, str)] if isinstance(examples, list) else []
            item["intent"] = intent
            item["examples"] = _dedupe_keep_order([*existing, example])
            if "utterances" in item:
                item["utterances"] = item["examples"]
            return cleaned
    cleaned.append({"intent": intent, "examples": [example]})
    return cleaned


def write_skill_example(*, ctx: AgentContext | None = None, skill_name: str, intent: str, example: str) -> dict[str, Any]:
    ctx = ctx or get_ctx()
    skill_name = str(skill_name or "").strip()
    intent = str(intent or "").strip()
    example = str(example or "").strip()
    if not skill_name or not intent or not example:
        return {"ok": False, "reason": "missing_skill_intent_or_example"}

    path = Path(ctx.paths.skills_dir()) / skill_name / "skill.yaml"
    if not path.exists():
        return {"ok": False, "reason": "skill_not_found", "path": str(path)}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"ok": False, "reason": "skill_read_failed", "path": str(path), "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "skill_payload_not_object", "path": str(path)}

    nlu = payload.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
        payload["nlu"] = nlu
    intents_raw = nlu.get("intents")
    if isinstance(intents_raw, dict):
        spec = intents_raw.get(intent)
        if not isinstance(spec, dict):
            spec = {}
            intents_raw[intent] = spec
        existing = spec.get("examples")
        existing_list = [str(item) for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
        spec["examples"] = _dedupe_keep_order([*existing_list, example])
    else:
        intents_list = intents_raw if isinstance(intents_raw, list) else []
        nlu["intents"] = _upsert_skill_intent_list(intents_list, intent=intent, example=example)

    try:
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "reason": "skill_write_failed", "path": str(path), "error": str(exc)}
    return {"ok": True, "target": {"type": "skill", "id": skill_name}, "path": str(path)}


def append_system_action_feedback(
    *,
    ctx: AgentContext | None = None,
    action_id: str,
    intent: str,
    example: str,
    slots: Mapping[str, Any] | None = None,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = ctx or get_ctx()
    action_id = str(action_id or "").strip()
    intent = str(intent or "").strip()
    example = str(example or "").strip()
    if not action_id or not intent or not example:
        return {"ok": False, "reason": "missing_action_intent_or_example"}

    target = {"type": "system_action", "id": action_id}
    record = {
        "schema_version": 1,
        "id": _record_id(intent=intent, text=example, target=target),
        "ts": time.time(),
        "target": target,
        "intent": intent,
        "example": example,
        "slots": dict(slots or {}),
        "audit": dict(audit or {}),
    }
    path = system_action_feedback_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_ids: set[str] = set()
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    existing_ids.add(item["id"])
        except Exception:
            existing_ids = set()
    if record["id"] not in existing_ids:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "target": target, "path": str(path), "record": record}


def collect_system_action_feedback_examples(ctx: AgentContext | None = None) -> dict[str, list[str]]:
    path = system_action_feedback_path(ctx)
    out: dict[str, list[str]] = {}
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        intent = item.get("intent")
        example = item.get("example")
        if not isinstance(intent, str) or not isinstance(example, str):
            continue
        out.setdefault(intent.strip(), [])
        out[intent.strip()] = _dedupe_keep_order([*out[intent.strip()], example])
    return out


def save_feedback_example(
    *,
    ctx: AgentContext | None = None,
    target: Mapping[str, Any] | None,
    intent: str,
    example: str,
    slots: Mapping[str, Any] | None = None,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = ctx or get_ctx()
    intent = str(intent or "").strip()
    example = str(example or "").strip()
    normalized_target = _normalize_target(target, intent=intent)
    target_type = normalized_target.get("type")
    target_id = normalized_target.get("id")
    if target_type == "scenario" and isinstance(target_id, str):
        result = write_scenario_example(scenario_id=target_id, intent=intent, example=example)
    elif target_type == "skill" and isinstance(target_id, str):
        result = write_skill_example(ctx=ctx, skill_name=target_id, intent=intent, example=example)
    elif target_type == "system_action" and isinstance(target_id, str):
        result = append_system_action_feedback(
            ctx=ctx,
            action_id=target_id,
            intent=intent,
            example=example,
            slots=slots,
            audit=audit,
        )
    else:
        return {"ok": False, "reason": "unsupported_or_missing_target", "target": normalized_target}
    if result.get("ok"):
        result["intent"] = intent
        result["example"] = example
    return result
