from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


DEFAULT_DESKTOP_SCENARIO_ID = "web_desktop"

_DEFAULT_DESKTOP_NLU: dict[str, Any] = {
    "intents": {
        "desktop.open_modal": {
            "description": "Open a desktop modal by modal id.",
            "scope": "scenario",
            "examples": [
                "open [apps_catalog](modal_id)",
                "show [widgets_catalog](modal_id)",
                "open modal [nlu_teacher_modal](modal_id)",
                "open [workspace_manager](modal_id)",
                "show [notification_history](modal_id)",
                "\u043e\u0442\u043a\u0440\u043e\u0439 [apps_catalog](modal_id)",
                "\u043f\u043e\u043a\u0430\u0436\u0438 [widgets_catalog](modal_id)",
                "\u043e\u0442\u043a\u0440\u043e\u0439 \u043c\u043e\u0434\u0430\u043b\u043a\u0443 [nlu_teacher_modal](modal_id)",
            ],
            "actions": [
                {
                    "type": "callHost",
                    "target": "desktop.modal.open",
                    "params": {
                        "modal_id": "$slot.modal_id",
                        "webspace_id": "$ctx.webspace_id",
                    },
                }
            ],
        },
        "desktop.open_node_modal": {
            "description": "Open a desktop modal in a named node context.",
            "scope": "scenario",
            "examples": [
                "open [apps_catalog](modal_id) on node [member-1](node_ref)",
                "show [widgets_catalog](modal_id) for node [kitchen](node_ref)",
                "open modal [nlu_teacher_modal](modal_id) on [hall-display](node_ref)",
                "\u043e\u0442\u043a\u0440\u043e\u0439 [apps_catalog](modal_id) \u043d\u0430 \u043d\u043e\u0434\u0435 [member-1](node_ref)",
                "\u043f\u043e\u043a\u0430\u0436\u0438 [nlu_teacher_modal](modal_id) \u0434\u043b\u044f \u043d\u043e\u0434\u044b [kitchen](node_ref)",
            ],
            "actions": [
                {
                    "type": "callHost",
                    "target": "desktop.modal.open",
                    "params": {
                        "modal_id": "$slot.modal_id",
                        "node_ref": "$slot.node_ref",
                        "target_node_id": "$slot.node_ref",
                        "webspace_id": "$ctx.webspace_id",
                    },
                }
            ],
        },
    }
}


def default_desktop_nlu() -> dict[str, Any]:
    from adaos.services.nlu.system_actions_catalog import system_action_nlu_intents

    payload = deepcopy(_DEFAULT_DESKTOP_NLU)
    catalog_intents = system_action_nlu_intents()
    default_intents = payload.get("intents") if isinstance(payload.get("intents"), dict) else {}
    payload["intents"] = {**catalog_intents, **default_intents}
    return payload


def merge_default_desktop_nlu(scenario_id: str, nlu: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(nlu or {})
    if str(scenario_id or "").strip() != DEFAULT_DESKTOP_SCENARIO_ID:
        return payload

    merged = default_desktop_nlu()
    default_intents = merged.get("intents")
    payload_intents = payload.get("intents")
    if isinstance(default_intents, dict) and isinstance(payload_intents, Mapping):
        merged["intents"] = {**default_intents, **dict(payload_intents)}
    elif isinstance(payload_intents, Mapping):
        merged["intents"] = dict(payload_intents)

    for key, value in payload.items():
        if key == "intents":
            continue
        merged[key] = value
    return merged
