from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import y_py as Y
import yaml

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.reliability import (
    ReadinessStatus,
    observe_hub_root_integration_outbox,
    set_integration_readiness,
)
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.root.client import RootHttpClient
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id

from .ycoerce import coerce_dict, is_iterable_like, iter_mappings, iter_scalars

_log = logging.getLogger("adaos.nlu.teacher.llm")

_TEACHER_ENABLED = os.getenv("ADAOS_NLU_TEACHER") == "1"
_LLM_TEACHER_ENABLED = os.getenv("ADAOS_NLU_LLM_TEACHER") == "1"
_MODEL = os.getenv("ADAOS_NLU_LLM_MODEL") or os.getenv("OPENAI_RESPONSES_MODEL") or "gpt-4o-mini"
_MAX_TOKENS = int(os.getenv("ADAOS_NLU_LLM_MAX_TOKENS", "500") or "500")
_TIMEOUT_S = float(os.getenv("ADAOS_NLU_LLM_TIMEOUT_S", "20") or "20")


def _nlu_llm_write_meta():
    return ystore_write_metadata(
        root_names=["data"],
        source="nlu.llm_teacher_runtime",
        owner="core:nlu.llm_teacher",
        channel="core.nlu.llm_teacher.async",
    )


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = coerce_dict(payload.get("_meta"))
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _teacher_obj(data_map: Any) -> dict[str, Any]:
    return coerce_dict(data_map.get("nlu_teacher"))


def _ydoc_to_snapshot(ydoc: Any) -> dict[str, Any]:
    def _normalize(node: Any):
        if isinstance(node, dict):
            return {str(k): _normalize(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_normalize(x) for x in node]
        if isinstance(node, Y.YMap):
            keys = list(node.keys())
            return {str(k): _normalize(node.get(k)) for k in keys}
        if isinstance(node, Y.YArray):
            return [_normalize(x) for x in node]
        if node is None:
            return None
        return node

    try:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
    except Exception:
        return {}
    return {"ui": _normalize(ui_map) or {}, "data": _normalize(data_map) or {}}


def _extract_webspace_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    ui = coerce_dict(snapshot.get("ui"))
    data = coerce_dict(snapshot.get("data"))
    current_scenario = ui.get("current_scenario")
    if not isinstance(current_scenario, str):
        current_scenario = None

    catalog = coerce_dict(data.get("catalog"))
    apps = list(iter_mappings(catalog.get("apps")))
    widgets = list(iter_mappings(catalog.get("widgets")))

    installed = coerce_dict(data.get("installed"))
    installed_apps = list(iter_scalars(installed.get("apps")))
    installed_widgets = list(iter_scalars(installed.get("widgets")))

    def _strip_app(app: Any) -> Optional[dict[str, Any]]:
        if not isinstance(app, Mapping):
            return None
        out = {
            "id": app.get("id"),
            "title": app.get("title"),
            "scenario_id": app.get("scenario_id"),
            "launchModal": app.get("launchModal"),
            "origin": app.get("origin"),
        }
        return {k: v for k, v in out.items() if v is not None}

    def _strip_widget(w: Any) -> Optional[dict[str, Any]]:
        if not isinstance(w, Mapping):
            return None
        out = {"id": w.get("id"), "title": w.get("title"), "type": w.get("type"), "origin": w.get("origin")}
        return {k: v for k, v in out.items() if v is not None}

    # Backward-compatible (legacy) per-webspace rules.
    nlu = coerce_dict(data.get("nlu"))
    regex_rules = list(iter_mappings(nlu.get("regex_rules")))

    def _strip_rule(rule: Any) -> Optional[dict[str, Any]]:
        if not isinstance(rule, Mapping):
            return None
        out = {
            "id": rule.get("id"),
            "intent": rule.get("intent"),
            "pattern": rule.get("pattern"),
            "enabled": rule.get("enabled"),
            "source": rule.get("source"),
        }
        out = {k: v for k, v in out.items() if v is not None}
        if not isinstance(out.get("intent"), str) or not out.get("intent"):
            return None
        if not isinstance(out.get("pattern"), str) or not out.get("pattern"):
            return None
        return out

    out: dict[str, Any] = {
        "current_scenario": current_scenario,
        "catalog": {
            "apps": [x for x in (_strip_app(a) for a in apps) if x],
            "widgets": [x for x in (_strip_widget(w) for w in widgets) if x],
        },
        "installed": {
            "apps": [x for x in installed_apps if isinstance(x, (str, int))],
            "widgets": [x for x in installed_widgets if isinstance(x, (str, int))],
        },
        "regex_rules": [x for x in (_strip_rule(r) for r in regex_rules) if x][:50],
    }

    # Provide a lightweight view of existing skill-level NLU artifacts, so the LLM can
    # propose improvements to existing skills instead of always creating new ones.
    try:
        skills = _infer_skills_from_catalog(apps=apps, widgets=widgets)
        out["skill_nlu"] = _load_skill_nlu_artifacts(skills)
    except Exception:
        out["skill_nlu"] = {}
        skills = []
    out["skills"] = [s for s in skills if isinstance(s, str)]

    # Prefer workspace-owned regex rules (scenario/skills) so the LLM can avoid duplicates.
    try:
        collected: list[dict[str, Any]] = list(out.get("regex_rules") or [])

        # Scenario rules
        if isinstance(current_scenario, str) and current_scenario:
            try:
                content = scenarios_loader.read_content(current_scenario)
            except Exception:
                content = {}
            nlu_section = content.get("nlu") if isinstance(content, dict) else None
            rr = (nlu_section or {}).get("regex_rules") if isinstance(nlu_section, dict) else None
            if isinstance(rr, list):
                for x in (_strip_rule(r) for r in rr):
                    if not x:
                        continue
                    x["owner"] = {"type": "scenario", "id": current_scenario}
                    collected.append(x)

        # Skill rules
        ctx = get_ctx()
        skills_dir = Path(ctx.paths.skills_dir())
        for skill_name in skills:
            path = skills_dir / skill_name / "skill.yaml"
            if not path.exists():
                continue
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            nlu_section = payload.get("nlu")
            if not isinstance(nlu_section, dict):
                continue
            rr = nlu_section.get("regex_rules")
            if not isinstance(rr, list):
                continue
            for x in (_strip_rule(r) for r in rr):
                if not x:
                    continue
                x["owner"] = {"type": "skill", "id": skill_name}
                collected.append(x)

        # De-dupe
        uniq: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in collected:
            intent = r.get("intent")
            pattern = r.get("pattern")
            if not isinstance(intent, str) or not isinstance(pattern, str):
                continue
            key = (intent, pattern)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        out["regex_rules"] = uniq[:80]
    except Exception:
        pass

    return out


def _build_intent_routes_and_policies(
    *, scenario_nlu: Mapping[str, Any], skills: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """
    Derive routing hints for the LLM:
    - intent -> callSkill topic -> skill name (by subscription)
    - intent -> callHost target (system action)

    Also returns per-skill llm_policy flags so we can auto-apply teacher changes when trusted.
    """
    intents_map = scenario_nlu.get("intents") if isinstance(scenario_nlu.get("intents"), dict) else None
    if not isinstance(intents_map, dict):
        return ([], {}, [])

    ctx = get_ctx()
    skills_dir = Path(ctx.paths.skills_dir())

    topic_to_skill: dict[str, str] = {}
    skill_policies: dict[str, Any] = {}
    skill_manifests: list[dict[str, Any]] = []
    for skill_name in [s for s in skills if isinstance(s, str) and s.strip()]:
        path = skills_dir / skill_name / "skill.yaml"
        if not path.exists():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        llm_policy = payload.get("llm_policy")
        skill_policies[skill_name] = llm_policy if isinstance(llm_policy, dict) else {}

        try:
            tools = payload.get("tools")
            if isinstance(tools, list):
                tools_list = [
                    {"name": t.get("name"), "description": t.get("description")}
                    for t in tools
                    if isinstance(t, dict) and isinstance(t.get("name"), str)
                ]
            else:
                tools_list = []
        except Exception:
            tools_list = []

        events = payload.get("events")
        subs = (events or {}).get("subscribe") if isinstance(events, dict) else None
        pubs = (events or {}).get("publish") if isinstance(events, dict) else None
        try:
            nlu = payload.get("nlu")
            rr = (nlu or {}).get("regex_rules") if isinstance(nlu, dict) else None
            regex_count = len(rr) if isinstance(rr, list) else 0
        except Exception:
            regex_count = 0
        skill_manifests.append(
            {
                "name": skill_name,
                "description": payload.get("description"),
                "llm_policy": skill_policies.get(skill_name) or {},
                "events": {
                    "subscribe": [x for x in (subs or []) if isinstance(x, str)][:50] if isinstance(subs, list) else [],
                    "publish": [x for x in (pubs or []) if isinstance(x, str)][:50] if isinstance(pubs, list) else [],
                },
                "tools": tools_list[:30],
                "regex_rules_count": regex_count,
            }
        )

        events = payload.get("events")
        subs = (events or {}).get("subscribe") if isinstance(events, dict) else None
        if isinstance(subs, list):
            for t in subs:
                if isinstance(t, str) and t.strip() and t.strip() not in topic_to_skill:
                    topic_to_skill[t.strip()] = skill_name

    routes: list[dict[str, Any]] = []
    for intent_name, spec in intents_map.items():
        if not isinstance(intent_name, str) or not isinstance(spec, dict):
            continue
        actions = spec.get("actions")
        if not isinstance(actions, list):
            continue
        for a in actions:
            if not isinstance(a, dict):
                continue
            a_type = a.get("type")
            target = a.get("target")
            if not isinstance(a_type, str) or not isinstance(target, str) or not target.strip():
                continue
            if a_type == "callSkill":
                routes.append(
                    {
                        "intent": intent_name,
                        "action": "callSkill",
                        "target": target.strip(),
                        "skill": topic_to_skill.get(target.strip()),
                    }
                )
            elif a_type == "callHost":
                routes.append(
                    {
                        "intent": intent_name,
                        "action": "callHost",
                        "target": target.strip(),
                    }
                )
    return (routes[:150], skill_policies, skill_manifests[:50])


def _infer_skills_from_catalog(*, apps: list[Any], widgets: list[Any]) -> list[str]:
    skills: set[str] = set()
    for item in list(apps) + list(widgets):
        if not isinstance(item, dict):
            continue
        origin = item.get("origin")
        if not isinstance(origin, str):
            continue
        if origin.startswith("skill:") and len(origin) > len("skill:"):
            skills.add(origin[len("skill:") :].strip())
    return sorted([s for s in skills if s])


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / ".adaos" / "workspace" / "skills").is_dir():
            return p
    return Path.cwd()


def _safe_read_text(path: Path, *, limit: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    s = text.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"... <truncated {len(s) - limit} chars>"


def _load_skill_nlu_artifacts(skills: list[str]) -> dict[str, Any]:
    """
    Returns a small, prompt-friendly mapping of skill->nlu files (raw text).

    This is intentionally conservative: we only include a few files per skill and we truncate.
    """
    root = _find_repo_root()
    base = root / ".adaos" / "workspace" / "skills"
    out: dict[str, Any] = {}

    # Keep token usage bounded.
    for skill in skills[:10]:
        if not skill or "/" in skill or "\\" in skill:
            continue
        skill_dir = base / skill
        if not skill_dir.is_dir():
            continue

        files: dict[str, str] = {}
        intents_yml = skill_dir / "interpreter" / "intents.yml"
        if intents_yml.is_file():
            content = _safe_read_text(intents_yml, limit=8000)
            if content:
                files["interpreter/intents.yml"] = content

        nlu_yml = skill_dir / "interpreter" / "nlu.yml"
        if nlu_yml.is_file() and "interpreter/intents.yml" not in files:
            content = _safe_read_text(nlu_yml, limit=8000)
            if content:
                files["interpreter/nlu.yml"] = content

        if files:
            out[skill] = files

    return out


def _extract_scenario_nlu(*, scenario_id: str | None) -> dict[str, Any]:
    if not scenario_id:
        return {}
    try:
        content = scenarios_loader.read_content(scenario_id)
    except Exception:
        content = {}
    if not isinstance(content, dict):
        content = {}
    nlu = content.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
    try:
        from adaos.services.nlu.baseline_content import merge_default_desktop_nlu

        nlu = merge_default_desktop_nlu(str(scenario_id), nlu)
    except Exception:
        pass
    intents = nlu.get("intents")
    if not isinstance(intents, dict):
        return {}

    out: dict[str, Any] = {"intents": {}}
    for intent, spec in intents.items():
        if not isinstance(intent, str) or not intent:
            continue
        if not isinstance(spec, Mapping):
            continue
        examples = [x for x in spec.get("examples") if isinstance(x, str) and x.strip()] if is_iterable_like(spec.get("examples")) else []
        actions = list(iter_mappings(spec.get("actions")))
        out["intents"][intent] = {
            "description": spec.get("description"),
            "scope": spec.get("scope"),
            "examples": examples[:10],
            "actions": [
                {k: v for k, v in a.items() if k in {"type", "target", "params"}}
                for a in actions
            ][:5],
        }
    return out


def _extract_first_output_text(res: Any) -> str:
    """
    Root /v1/llm/response returns OpenAI Responses API payload.
    Try to extract the first text output robustly.
    """
    if isinstance(res, dict):
        out = res.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") in {"output_text", "text"}:
                        t = c.get("text")
                        if isinstance(t, str) and t.strip():
                            return t.strip()
        # Some proxies might return {choices:[{message:{content:"..."}}]}
        choices = res.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    return ""


def _truncate(text: Any, limit: int) -> Any:
    if not isinstance(text, str):
        return text
    s = text.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"... <truncated {len(s) - limit} chars>"


def _redact_messages(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "role": m.get("role"),
                "content": _truncate(m.get("content"), 1200),
            }
        )
    return out


def _build_prompt(*, request: dict[str, Any], webspace_id: str, context: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You are AdaOS NLU teacher. Decide what to do with a user utterance.\n"
        "You must return ONLY valid JSON (no markdown).\n\n"
        "Output schema:\n"
        "{\n"
        '  \"decision\": \"revise_nlu\" | \"propose_regex_rule\" | \"create_skill_candidate\" | \"create_scenario_candidate\" | \"ignore\",\n'
        '  \"intent\": string|null,\n'
        '  \"regex_rule\": {\"intent\": string, \"pattern\": string} | null,\n'
        '  \"target\": {\"type\": \"skill\"|\"scenario\", \"id\": string} | null,\n'
        '  \"examples\": string[],\n'
        '  \"slots\": object,  // e.g. {\"city\": {\"type\": \"string\"}}\n'
        '  \"confidence\": number, // 0..1\n'
        '  \"notes\": string,\n'
        '  \"candidate\": object|null\n'
        "}\n\n"
        "Rules:\n"
        "- If the utterance is not actionable for AdaOS, decision=ignore.\n"
        "- Prefer existing intents from context (scenario_nlu.intents keys) over inventing new ones.\n"
        "- Use provided context (scenario_nlu, intent_routes, system_actions, host_actions, skills_manifest, builtin_regex, regex_rules, catalog, skill_nlu) to reuse existing intents.\n"
        "- host_actions entries include stable system action ids, host event names, slots, examples, and linked intents.\n"
        "- If it matches a known app/widget/scenario, prefer revise_nlu with an existing intent name.\n"
        "- If an existing intent is the right match but regex stage likely misses it, prefer propose_regex_rule.\n"
        "- propose_regex_rule.pattern MUST be a Python regex with named capture groups for slots (e.g. (?P<city>...)).\n"
        "- Avoid proposing duplicate regex rules if builtin_regex or regex_rules already cover the utterance.\n"
        "- If user asks about weather/temperature but doesn't say the exact keyword, propose a regex rule for intent desktop.open_weather.\n"
        "- Regex rules should be reasonably general (avoid overfitting to a single verb like \"покажи\"); capture city via (?P<city>...).\n"
        "- When proposing a regex rule, also set target to where the rule should be stored:\n"
        "  - Prefer the skill that handles the intent (see context.intent_routes) over the scenario.\n"
        "  - For intents that trigger system actions (callHost targets from context.system_actions/host_actions), target should usually be the scenario.\n"
        "- If it suggests a new capability, propose create_skill_candidate or create_scenario_candidate.\n"
        "- Keep intent names short and namespaced (e.g. desktop.open_weather, smalltalk.how_are_you).\n"
    )
    utterance = request.get("text") if isinstance(request.get("text"), str) else ""
    user = {
        "webspace_id": webspace_id,
        "request": {
            "id": request.get("id"),
            "request_id": request.get("request_id"),
            "text": utterance,
            "reason": request.get("reason"),
            "via": request.get("via"),
        },
        "context": context,
    }
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


async def _llm_call(messages: list[dict[str, str]], *, request_id: str | None = None) -> dict[str, Any]:
    ctx = get_ctx()
    http = RootHttpClient.from_settings(ctx.settings)
    body = {"model": _MODEL, "messages": messages, "max_tokens": _MAX_TOKENS, "temperature": 0.2}
    headers: dict[str, str] = {}
    subnet_id = str(getattr(getattr(ctx, "config", None), "subnet_id", "") or "").strip()
    node_id = str(getattr(getattr(ctx, "config", None), "node_id", "") or "").strip()
    if subnet_id:
        headers["X-AdaOS-Subnet-Id"] = subnet_id
    if node_id:
        headers["X-AdaOS-Node-Id"] = node_id
    req_id = str(request_id or "").strip()
    if req_id:
        body["request_id"] = req_id
    try:
        result = await asyncio.to_thread(
            http.request,
            "POST",
            "/v1/llm/response",
            json=body,
            headers=headers or None,
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        try:
            observe_hub_root_integration_outbox(
                "llm",
                publish_fail=1,
                connected=False,
                operation_key=req_id or None,
                idempotency_mode="request_id" if req_id else "none",
                conflict=1 if "llm_request_id_conflict" in str(exc) else None,
                last_error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        try:
            set_integration_readiness(
                "llm",
                status=ReadinessStatus.DEGRADED,
                summary="root LLM proxy request failed",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
        except Exception:
            pass
        raise

    protocol = result.get("_protocol") if isinstance(result, dict) and isinstance(result.get("_protocol"), dict) else {}
    cache_state = str(protocol.get("dedupe") or "").strip().lower()
    try:
        observe_hub_root_integration_outbox(
            "llm",
            publish_ok=1,
            connected=True,
            operation_key=req_id or None,
            idempotency_mode="request_id" if req_id else "none",
            cache_hit=1 if cache_state == "hit" else None,
            cache_miss=1 if req_id and cache_state != "hit" else None,
        )
    except Exception:
        pass
    try:
        details = {"model": _MODEL}
        if req_id:
            details["request_id"] = req_id
        if cache_state:
            details["cache"] = cache_state
        set_integration_readiness(
            "llm",
            status=ReadinessStatus.READY,
            summary="root LLM proxy request succeeded",
            details=details,
        )
    except Exception:
        pass
    return result


async def _append_llm_log(webspace_id: str, entry: dict[str, Any]) -> None:
    async with _nlu_llm_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _teacher_obj(data_map)
            logs = list(iter_mappings(teacher.get("llm_logs")))
            logs.append(entry)
            teacher["llm_logs"] = logs[-300:]
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)


async def _patch_llm_log(webspace_id: str, *, log_id: str, patch: dict[str, Any]) -> None:
    async with _nlu_llm_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _teacher_obj(data_map)
            logs = list(iter_mappings(teacher.get("llm_logs")))
            next_logs: list[dict[str, Any]] = []
            for item in logs:
                if item.get("id") == log_id:
                    updated = dict(item)
                    updated.update(patch)
                    next_logs.append(updated)
                else:
                    next_logs.append(item)
            teacher["llm_logs"] = next_logs[-300:]
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)


async def _update_revision_by_request_id(
    webspace_id: str,
    *,
    request_id: str,
    patch: dict[str, Any],
) -> Optional[dict[str, Any]]:
    async with _nlu_llm_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _teacher_obj(data_map)
            revisions = list(iter_mappings(teacher.get("revisions")))
            updated: Optional[dict[str, Any]] = None
            cleaned: list[dict[str, Any]] = []
            for item in revisions:
                if item.get("request_id") == request_id and item.get("status") in {"pending", "proposed"}:
                    updated = dict(item)
                    updated.update(patch)
                    cleaned.append(updated)
                else:
                    cleaned.append(item)
            teacher["revisions"] = cleaned
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)
            return updated


async def _append_candidate(webspace_id: str, candidate: dict[str, Any]) -> None:
    async with _nlu_llm_write_meta():
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _teacher_obj(data_map)
            candidates = list(iter_mappings(teacher.get("candidates")))
            candidates.append(candidate)
            teacher["candidates"] = candidates[-200:]
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)


@subscribe("nlp.teacher.request")
async def _on_teacher_request(evt: Any) -> None:
    if not (_TEACHER_ENABLED and _LLM_TEACHER_ENABLED):
        return

    ctx = None
    webspace_id = None
    try:
        ctx = get_ctx()
        try:
            allow = bool(getattr(getattr(ctx.config, "root_settings", None), "llm", None).allow_nlu_teacher)  # type: ignore[attr-defined]
        except Exception:
            allow = True
        if not allow:
            return
        payload = _payload(evt)
        webspace_id = _resolve_webspace_id(payload)
        req = payload.get("request") if isinstance(payload.get("request"), Mapping) else None
        if not req:
            return

        req_meta = req.get("_meta") if isinstance(req.get("_meta"), Mapping) else {}
        text = req.get("text")
        request_id = req.get("request_id")
        if not isinstance(text, str) or not text.strip():
            return
        if not isinstance(request_id, str) or not request_id.strip():
            return
        text = text.strip()
        request_id = request_id.strip()

        # Build lightweight context snapshot for LLM.
        try:
            async with async_get_ydoc(webspace_id) as ydoc:
                snapshot = _ydoc_to_snapshot(ydoc)
        except Exception:
            snapshot = {}
        context = _extract_webspace_context(snapshot if isinstance(snapshot, dict) else {})
        context["scenario_nlu"] = _extract_scenario_nlu(scenario_id=context.get("current_scenario"))
        try:
            routes, skill_policies, skill_manifests = _build_intent_routes_and_policies(
                scenario_nlu=context.get("scenario_nlu") if isinstance(context.get("scenario_nlu"), Mapping) else {},
                skills=context.get("skills") if isinstance(context.get("skills"), list) else [],
            )
        except Exception:
            routes, skill_policies, skill_manifests = ([], {}, [])
        context["intent_routes"] = routes
        # System actions are "callHost" targets exposed by the current scenario intents.
        context["system_actions"] = sorted(
            {str(r.get("target")) for r in routes if r.get("action") == "callHost" and isinstance(r.get("target"), str)}
        )[:150]
        context["skills_manifest"] = skill_manifests
        try:
            from adaos.services.nlu.system_actions_catalog import (
                SYSTEM_ACTION_CATALOG_VERSION,
                describe_system_actions,
            )

            context["host_actions"] = describe_system_actions()
            context["host_actions_version"] = SYSTEM_ACTION_CATALOG_VERSION
        except Exception:
            context["host_actions"] = []
        try:
            from adaos.services.nlu.pipeline import describe_builtin_regex_rules  # local import to avoid cycles

            context["builtin_regex"] = describe_builtin_regex_rules()
        except Exception:
            context["builtin_regex"] = []

        messages = _build_prompt(request=dict(req), webspace_id=webspace_id, context=context)

        log_id = f"llm.{int(time.time() * 1000)}"
        started_at = time.time()

        try:
            await _append_llm_log(
                webspace_id,
                {
                    "id": log_id,
                    "ts": started_at,
                    "request_id": request_id,
                    "webspace_id": webspace_id,
                    "model": _MODEL,
                    "request": {
                        "messages": _redact_messages(messages),
                        "max_tokens": _MAX_TOKENS,
                        "timeout_s": _TIMEOUT_S,
                    },
                    "status": "request",
                },
            )
        except Exception:
            _log.debug("failed to append llm log webspace=%s", webspace_id, exc_info=True)

        try:
            await append_event(
                webspace_id,
                make_event(
                    webspace_id=webspace_id,
                    request_id=request_id,
                    request_text=text,
                    kind="llm.request",
                    title="LLM request",
                    subtitle=_MODEL,
                    raw={
                        "log_id": log_id,
                        "model": _MODEL,
                        "messages": _redact_messages(messages),
                        "max_tokens": _MAX_TOKENS,
                        "timeout_s": _TIMEOUT_S,
                    },
                    meta=req_meta,
                ),
            )
        except Exception:
            _log.debug("failed to append teacher event (llm.request) webspace=%s", webspace_id, exc_info=True)

        try:
            res = await _llm_call(messages, request_id=request_id or log_id)
        except Exception as exc:
            _log.warning("llm teacher call failed: %s", exc)
            try:
                await _patch_llm_log(
                    webspace_id,
                    log_id=log_id,
                    patch={
                        "status": "error",
                        "error": str(exc),
                        "duration_s": max(0.0, time.time() - started_at),
                    },
                )
            except Exception:
                _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)
            return

        raw_text = _extract_first_output_text(res)
        if not raw_text:
            _log.warning("llm teacher returned empty output")
            try:
                await _patch_llm_log(
                    webspace_id,
                    log_id=log_id,
                    patch={
                        "status": "error",
                        "error": "empty_output",
                        "response": {"raw": None},
                        "duration_s": max(0.0, time.time() - started_at),
                    },
                )
            except Exception:
                _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)
            return

        try:
            suggestion = json.loads(raw_text)
        except Exception:
            suggestion = {"decision": "ignore", "notes": raw_text, "confidence": 0.0}
        if not isinstance(suggestion, dict):
            suggestion = {"decision": "ignore", "notes": raw_text, "confidence": 0.0}

        try:
            await _patch_llm_log(
                webspace_id,
                log_id=log_id,
                patch={
                    "status": "response",
                    "response": {"raw": _truncate(raw_text, 4000), "parsed": suggestion},
                    "duration_s": max(0.0, time.time() - started_at),
                },
            )
        except Exception:
            _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)

        decision = suggestion.get("decision") if isinstance(suggestion.get("decision"), str) else "ignore"
        intent = suggestion.get("intent") if isinstance(suggestion.get("intent"), str) else None
        regex_rule = suggestion.get("regex_rule") if isinstance(suggestion.get("regex_rule"), Mapping) else None
        target = suggestion.get("target") if isinstance(suggestion.get("target"), Mapping) else None
        examples = suggestion.get("examples") if isinstance(suggestion.get("examples"), list) else None
        if examples is None:
            examples = [text]
        examples = [x.strip() for x in examples if isinstance(x, str) and x.strip()]
        slots = suggestion.get("slots") if isinstance(suggestion.get("slots"), Mapping) else {}
        confidence = suggestion.get("confidence")
        try:
            confidence_f = float(confidence) if confidence is not None else 0.0
        except Exception:
            confidence_f = 0.0
        notes = suggestion.get("notes") if isinstance(suggestion.get("notes"), str) else ""

        llm_meta = {"model": _MODEL, "ts": time.time(), "decision": decision, "confidence": confidence_f}

        try:
            await append_event(
                webspace_id,
                make_event(
                    webspace_id=webspace_id,
                    request_id=request_id,
                    request_text=text,
                    kind="llm.response",
                    title="LLM response",
                    subtitle=f"{decision} ({confidence_f:.2f})",
                    raw={"log_id": log_id, "decision": decision, "suggestion": suggestion},
                    meta=req_meta,
                ),
            )
        except Exception:
            _log.debug("failed to append teacher event (llm.response) webspace=%s", webspace_id, exc_info=True)

        if decision == "revise_nlu" and intent:
            patch = {
                "status": "proposed",
                "proposal": {"intent": intent, "examples": examples, "slots": dict(slots)},
                "llm": llm_meta,
                "note": notes or "LLM proposed NLU revision.",
                "proposed_at": time.time(),
            }
            try:
                updated = await _update_revision_by_request_id(webspace_id, request_id=request_id, patch=patch)
            except Exception:
                _log.warning(
                    "failed to update teacher revision webspace=%s request_id=%s", webspace_id, request_id, exc_info=True
                )
                updated = None
            bus_emit(
                ctx.bus,
                "nlp.teacher.revision.suggested",
                {"webspace_id": webspace_id, "request_id": request_id, "revision": updated, "suggestion": suggestion},
                source="nlu.teacher.llm",
            )
            try:
                await append_event(
                    webspace_id,
                    make_event(
                        webspace_id=webspace_id,
                        request_id=request_id,
                        request_text=text,
                        kind="revision.suggested",
                        title="Revision suggested",
                        subtitle=intent,
                        raw=updated if isinstance(updated, Mapping) else {"intent": intent, "examples": examples},
                        meta=req_meta,
                    ),
                )
            except Exception:
                _log.debug("failed to append teacher event (revision.suggested) webspace=%s", webspace_id, exc_info=True)
            return

        if decision == "propose_regex_rule" and regex_rule:
            rr_intent = regex_rule.get("intent")
            rr_pattern = regex_rule.get("pattern")
            if isinstance(rr_intent, str) and rr_intent.strip() and isinstance(rr_pattern, str) and rr_pattern.strip():
                target_out: dict[str, Any] | None = None
                if isinstance(target, Mapping):
                    t_type = target.get("type")
                    t_id = target.get("id")
                    if isinstance(t_type, str) and isinstance(t_id, str) and t_type.strip() and t_id.strip():
                        if t_type.strip() in {"skill", "scenario"}:
                            target_out = {"type": t_type.strip(), "id": t_id.strip()}
                if target_out is None:
                    for r in routes:
                        if r.get("intent") != rr_intent.strip() or r.get("action") != "callSkill":
                            continue
                        skill = r.get("skill")
                        if isinstance(skill, str) and skill.strip():
                            target_out = {"type": "skill", "id": skill.strip()}
                            break
                    if target_out is None:
                        scenario_id = context.get("current_scenario")
                        if isinstance(scenario_id, str) and scenario_id.strip():
                            target_out = {"type": "scenario", "id": scenario_id.strip()}
                entry = {
                    "id": f"cand.{int(time.time()*1000)}",
                    "ts": time.time(),
                    "kind": "regex_rule",
                    "text": text,
                    "request_id": request_id,
                    "origin_scenario_id": context.get("current_scenario")
                    if isinstance(context.get("current_scenario"), str)
                    else None,
                    "candidate": {
                        "name": f"Regex rule for {rr_intent.strip()}",
                        "description": "Proposed regex rule to improve fast NLU stage.",
                    },
                    "regex_rule": {"intent": rr_intent.strip(), "pattern": rr_pattern},
                    **({"target": target_out} if target_out else {}),
                    "llm": llm_meta,
                    "notes": notes,
                    "status": "pending",
                }
                try:
                    await _append_candidate(webspace_id, entry)
                except Exception:
                    _log.debug("failed to append regex rule candidate webspace=%s", webspace_id, exc_info=True)
                bus_emit(
                    ctx.bus,
                    "nlp.teacher.candidate.proposed",
                    {"webspace_id": webspace_id, "candidate": entry, "_meta": dict(req_meta)},
                    source="nlu.teacher.llm",
                )
                # Auto-apply if the owning skill explicitly trusts NLU Teacher output.
                try:
                    if isinstance(target_out, dict) and target_out.get("type") == "skill":
                        skill_name = target_out.get("id")
                        policy = skill_policies.get(skill_name) if isinstance(skill_name, str) else None
                        if isinstance(policy, Mapping) and bool(policy.get("autoapply_nlu_teacher")):
                            bus_emit(
                                ctx.bus,
                                "nlp.teacher.candidate.apply",
                                {
                                    "webspace_id": webspace_id,
                                    "candidate_id": entry.get("id"),
                                    "target": dict(target_out),
                                    "_meta": dict(req_meta),
                                },
                                source="nlu.teacher.llm",
                            )
                except Exception:
                    _log.debug("failed to auto-apply teacher regex candidate webspace=%s", webspace_id, exc_info=True)
                try:
                    await append_event(
                        webspace_id,
                        make_event(
                            webspace_id=webspace_id,
                            request_id=request_id,
                            request_text=text,
                            kind="candidate.proposed",
                            title="Candidate proposed",
                            subtitle=f"regex_rule: {rr_intent.strip()}",
                            raw=entry,
                            meta=req_meta,
                        ),
                    )
                except Exception:
                    _log.debug("failed to append teacher event (candidate.proposed regex_rule) webspace=%s", webspace_id, exc_info=True)

                bus_emit(
                    ctx.bus,
                    "io.out.chat.append",
                    {
                        "id": "",
                        "from": "hub",
                        "text": (
                            f"Я не смог распознать запрос в Rasa: «{text}».\n\n"
                            "Я предложил улучшение NLU в виде regex-правила, чтобы такие запросы распознавались сразу.\n"
                            "Открой «NLU Teacher» (Apps) и нажми Apply у кандидата типа regex_rule.\n"
                            "После Apply тот же запрос начнёт распознаваться на этапе regex без обращения к LLM."
                        ),
                        "ts": time.time(),
                        "_meta": {"webspace_id": webspace_id, **dict(req_meta)},
                    },
                    source="router.nlu",
                )
                return

        if decision in {"create_skill_candidate", "create_scenario_candidate"}:
            candidate = suggestion.get("candidate") if isinstance(suggestion.get("candidate"), Mapping) else {}
            entry = {
                "id": f"cand.{int(time.time()*1000)}",
                "ts": time.time(),
                "kind": "skill" if decision == "create_skill_candidate" else "scenario",
                "text": text,
                "request_id": request_id,
                "candidate": dict(candidate),
                "llm": llm_meta,
                "notes": notes,
                "status": "pending",
            }
            try:
                await _append_candidate(webspace_id, entry)
            except Exception:
                _log.debug("failed to append candidate webspace=%s", webspace_id, exc_info=True)
            bus_emit(
                ctx.bus,
                "nlp.teacher.candidate.proposed",
                {"webspace_id": webspace_id, "candidate": entry, "_meta": dict(req_meta)},
                source="nlu.teacher.llm",
            )
            try:
                await append_event(
                    webspace_id,
                    make_event(
                        webspace_id=webspace_id,
                        request_id=request_id,
                        request_text=text,
                        kind="candidate.proposed",
                        title="Candidate proposed",
                        subtitle=f"{entry.get('kind')}: {(entry.get('candidate') or {}).get('name') or ''}".strip(),
                        raw=entry,
                        meta=req_meta,
                    ),
                )
            except Exception:
                _log.debug("failed to append teacher event (candidate.proposed) webspace=%s", webspace_id, exc_info=True)
            return

        patch = {"status": "ignored", "llm": llm_meta, "note": notes or "LLM decided to ignore.", "ignored_at": time.time()}
        try:
            await _update_revision_by_request_id(webspace_id, request_id=request_id, patch=patch)
        except Exception:
            _log.debug("failed to update ignored revision webspace=%s request_id=%s", webspace_id, request_id, exc_info=True)
        bus_emit(
            ctx.bus,
            "nlp.teacher.ignored",
            {"webspace_id": webspace_id, "request_id": request_id, "suggestion": suggestion},
            source="nlu.teacher.llm",
        )
        try:
            await append_event(
                webspace_id,
                make_event(
                    webspace_id=webspace_id,
                    request_id=request_id,
                    request_text=text,
                    kind="llm.ignored",
                    title="LLM ignored",
                    subtitle=notes or "",
                    raw={"suggestion": suggestion},
                    meta=req_meta,
                ),
            )
        except Exception:
            _log.debug("failed to append teacher event (llm.ignored) webspace=%s", webspace_id, exc_info=True)
    except Exception:
        # Never crash the eventbus handler; log and exit.
        _log.warning("llm teacher handler crashed webspace=%s", webspace_id, exc_info=True)
        return
