from __future__ import annotations

from copy import deepcopy
from typing import Any


SYSTEM_ACTION_CATALOG_VERSION = "2026-05-20"
SYSTEM_ACTION_SCOPE = "system"


def _call_host(target: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "callHost", "target": target, "params": params}]


_SYSTEM_ACTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "host.desktop.scenario.set",
        "kind": "host_action",
        "status": "active",
        "action": "desktop.scenario.set",
        "description": "Switch current desktop scenario for a webspace.",
        "params": {"scenario_id": "string", "webspace_id": "string"},
        "slots": {"scenario_id": {"type": "scenario_id", "required": True}},
        "nlu_intents": [
            {
                "intent": "desktop.switch_scenario",
                "description": "Switch the current desktop scenario.",
                "examples": [
                    "switch to [web_desktop](scenario_id)",
                    "use scenario [web_desktop](scenario_id)",
                    "open scenario [web_desktop](scenario_id)",
                    "\u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438 \u043d\u0430 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 [web_desktop](scenario_id)",
                    "\u0432\u043a\u043b\u044e\u0447\u0438 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 [web_desktop](scenario_id)",
                ],
                "actions": _call_host(
                    "desktop.scenario.set",
                    {"scenario_id": "$slot.scenario_id", "webspace_id": "$ctx.webspace_id"},
                ),
            }
        ],
    },
    {
        "id": "host.desktop.toggle_install_app",
        "kind": "host_action",
        "status": "active",
        "action": "desktop.toggleInstall",
        "description": "Toggle install of an app on the desktop.",
        "params": {"type": '"app"', "id": "string", "webspace_id": "string"},
        "slots": {"app_id": {"type": "app_id", "required": True}},
        "nlu_intents": [
            {
                "intent": "desktop.toggle_app_install",
                "description": "Install or remove an app on the current desktop.",
                "examples": [
                    "toggle app [nlu_teacher_app](app_id)",
                    "install app [nlu_teacher_app](app_id)",
                    "remove app [nlu_teacher_app](app_id)",
                    "\u0432\u043a\u043b\u044e\u0447\u0438 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 [nlu_teacher_app](app_id)",
                    "\u0443\u0431\u0435\u0440\u0438 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 [nlu_teacher_app](app_id)",
                ],
                "actions": _call_host(
                    "desktop.toggleInstall",
                    {"type": "app", "id": "$slot.app_id", "webspace_id": "$ctx.webspace_id"},
                ),
            }
        ],
    },
    {
        "id": "host.desktop.modal.open",
        "kind": "host_action",
        "status": "active",
        "action": "desktop.modal.open",
        "description": "Request opening a registered desktop modal by id, optionally scoped to a node.",
        "params": {
            "modal_id": "string",
            "webspace_id": "string",
            "node_ref?": "string",
            "target_node_id?": "string",
        },
        "slots": {
            "modal_id": {"type": "modal_id", "required": True},
            "node_ref": {"type": "node_ref", "required": False},
        },
        "nlu_intents": [
            {
                "intent": "desktop.open_modal",
                "description": "Open a desktop modal by modal id.",
                "examples": [
                    "open [apps_catalog](modal_id)",
                    "show [widgets_catalog](modal_id)",
                    "open modal [nlu_teacher_modal](modal_id)",
                ],
                "actions": _call_host(
                    "desktop.modal.open",
                    {"modal_id": "$slot.modal_id", "webspace_id": "$ctx.webspace_id"},
                ),
            },
            {
                "intent": "desktop.open_node_modal",
                "description": "Open a desktop modal in a named node context.",
                "examples": [
                    "open [apps_catalog](modal_id) on node [member-1](node_ref)",
                    "show [widgets_catalog](modal_id) for node [kitchen](node_ref)",
                    "open modal [nlu_teacher_modal](modal_id) on [hall-display](node_ref)",
                ],
                "actions": _call_host(
                    "desktop.modal.open",
                    {
                        "modal_id": "$slot.modal_id",
                        "node_ref": "$slot.node_ref",
                        "target_node_id": "$slot.node_ref",
                        "webspace_id": "$ctx.webspace_id",
                    },
                ),
            },
        ],
    },
    {
        "id": "host.desktop.webspace.reload",
        "kind": "host_action",
        "status": "active",
        "action": "desktop.webspace.reload",
        "description": "Reload the current webspace UI/data projections.",
        "params": {"webspace_id": "string"},
        "slots": {},
        "nlu_intents": [
            {
                "intent": "desktop.reload_webspace",
                "description": "Reload the current webspace.",
                "examples": [
                    "reload desktop",
                    "refresh desktop",
                    "reload current webspace",
                    "\u043f\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0437\u0438 \u0440\u0430\u0431\u043e\u0447\u0438\u0439 \u0441\u0442\u043e\u043b",
                    "\u043e\u0431\u043d\u043e\u0432\u0438 \u0440\u0430\u0431\u043e\u0447\u0435\u0435 \u043f\u0440\u043e\u0441\u0442\u0440\u0430\u043d\u0441\u0442\u0432\u043e",
                ],
                "actions": _call_host("desktop.webspace.reload", {"webspace_id": "$ctx.webspace_id"}),
            }
        ],
    },
    {
        "id": "host.desktop.webspace.reset",
        "kind": "host_action",
        "status": "active",
        "action": "desktop.webspace.reset",
        "description": "Reset the current webspace UI/data projections.",
        "params": {"webspace_id": "string"},
        "slots": {},
        "nlu_intents": [
            {
                "intent": "desktop.reset_webspace",
                "description": "Reset the current webspace projections.",
                "examples": [
                    "reset desktop",
                    "restore current webspace",
                    "reset webspace",
                    "\u0441\u0431\u0440\u043e\u0441\u044c \u0440\u0430\u0431\u043e\u0447\u0438\u0439 \u0441\u0442\u043e\u043b",
                    "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u0438 \u0440\u0430\u0431\u043e\u0447\u0435\u0435 \u043f\u0440\u043e\u0441\u0442\u0440\u0430\u043d\u0441\u0442\u0432\u043e",
                ],
                "actions": _call_host("desktop.webspace.reset", {"webspace_id": "$ctx.webspace_id"}),
            }
        ],
    },
    {
        "id": "host.scenario.workflow.action",
        "kind": "host_action",
        "status": "active",
        "action": "scenario.workflow.action",
        "description": "Trigger a scenario workflow action (Prompt IDE etc).",
        "params": {"action": "string", "object_type": "string", "object_id": "string", "webspace_id": "string"},
        "slots": {
            "action": {"type": "string", "required": True},
            "object_type": {"type": "string", "required": True},
            "object_id": {"type": "string", "required": True},
        },
        "nlu_intents": [],
    },
    {
        "id": "host.scenario.workflow.set_state",
        "kind": "host_action",
        "status": "active",
        "action": "scenario.workflow.set_state",
        "description": "Set scenario workflow state (Prompt IDE etc).",
        "params": {"state": "string", "object_type": "string", "object_id": "string", "webspace_id": "string"},
        "slots": {
            "state": {"type": "string", "required": True},
            "object_type": {"type": "string", "required": True},
            "object_id": {"type": "string", "required": True},
        },
        "nlu_intents": [],
    },
    {
        "id": "host.nlp.teacher.candidate.apply",
        "kind": "host_action",
        "status": "active",
        "action": "nlp.teacher.candidate.apply",
        "description": "Apply an NLU Teacher candidate (regex_rule/skill/scenario plan item).",
        "params": {"candidate_id": "string", "webspace_id": "string", "target?": "{type,id}"},
        "slots": {},
        "nlu_intents": [],
    },
    {
        "id": "host.nlp.teacher.revision.apply",
        "kind": "host_action",
        "status": "active",
        "action": "nlp.teacher.revision.apply",
        "description": "Apply an NLU Teacher revision to scenario NLU examples.",
        "params": {"revision_id": "string", "intent": "string", "examples": "string[]", "slots": "object"},
        "slots": {},
        "nlu_intents": [],
    },
    {
        "id": "host.nlp.teacher.example.save",
        "kind": "host_action",
        "status": "active",
        "action": "nlp.teacher.example.save",
        "description": "Save an operator-approved NLU example into a selected skill/scenario/system-action target.",
        "params": {"text": "string", "intent": "string", "target": "{type,id}", "slots": "object"},
        "slots": {},
        "nlu_intents": [],
    },
)


def _active(action: dict[str, Any]) -> bool:
    return str(action.get("status") or "").strip().lower() == "active"


def _dedupe_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        item = value.strip()
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _iter_action_intents(action: dict[str, Any]) -> list[dict[str, Any]]:
    intents = action.get("nlu_intents")
    if not isinstance(intents, list):
        return []
    return [item for item in intents if isinstance(item, dict) and isinstance(item.get("intent"), str)]


def describe_system_actions() -> list[dict[str, Any]]:
    """
    Public host/system actions that can be triggered by scenarios (callHost) or UI.

    This list is intentionally conservative and mirrors host events accepted by
    the hub/runtime. It is provided to the LLM NLU Teacher as capabilities
    context and to NLU exporters as system-owned training metadata.
    """
    result: list[dict[str, Any]] = []
    for action in _SYSTEM_ACTIONS:
        item = {
            "schema_version": SYSTEM_ACTION_CATALOG_VERSION,
            "id": action["id"],
            "kind": action["kind"],
            "status": action["status"],
            "action": action["action"],
            "description": action.get("description"),
            "params": deepcopy(action.get("params") or {}),
            "slots": deepcopy(action.get("slots") or {}),
        }
        intents = _iter_action_intents(action)
        if intents:
            item["intents"] = [spec["intent"] for spec in intents]
            item["examples"] = _dedupe_strings(
                [example for spec in intents for example in list(spec.get("examples") or [])]
            )
        result.append(item)
    return result


def system_action_nlu_intents(*, active_only: bool = True) -> dict[str, dict[str, Any]]:
    """
    Return NLU intent specs owned by the system action catalog.

    The shape intentionally mirrors ``scenario.json:nlu.intents`` so the same
    dispatcher/export paths can consume built-in host actions without treating
    them as user skills.
    """
    out: dict[str, dict[str, Any]] = {}
    for action in _SYSTEM_ACTIONS:
        if active_only and not _active(action):
            continue
        for spec in _iter_action_intents(action):
            intent = str(spec.get("intent") or "").strip()
            if not intent:
                continue
            examples = _dedupe_strings(list(spec.get("examples") or []))
            actions = spec.get("actions")
            if not isinstance(actions, list):
                actions = _call_host(str(action.get("action") or ""), {})
            out[intent] = {
                "description": spec.get("description") or action.get("description"),
                "scope": SYSTEM_ACTION_SCOPE,
                "owner": {"type": "system_action", "id": action.get("id")},
                "action_id": action.get("id"),
                "host_action": action.get("action"),
                "examples": examples,
                "actions": deepcopy(actions),
                "slots": deepcopy(action.get("slots") or {}),
            }
    return out


def find_system_action_by_id(action_id: str) -> dict[str, Any] | None:
    token = str(action_id or "").strip()
    if not token:
        return None
    for action in describe_system_actions():
        if action.get("id") == token:
            return action
    return None


def find_system_action_by_intent(intent: str) -> dict[str, Any] | None:
    token = str(intent or "").strip()
    if not token:
        return None
    for action in describe_system_actions():
        intents = action.get("intents")
        if isinstance(intents, list) and token in intents:
            return action
    return None

