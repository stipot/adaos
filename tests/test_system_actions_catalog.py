from __future__ import annotations


def test_system_actions_catalog_exposes_host_action_metadata() -> None:
    from adaos.services.nlu.system_actions_catalog import (
        SYSTEM_ACTION_CATALOG_VERSION,
        describe_system_actions,
        find_system_action_by_id,
        find_system_action_by_intent,
        system_action_nlu_intents,
    )

    actions = describe_system_actions()
    assert actions
    assert all(item.get("schema_version") == SYSTEM_ACTION_CATALOG_VERSION for item in actions)

    reload_action = find_system_action_by_id("host.desktop.webspace.reload")
    assert reload_action is not None
    assert reload_action["action"] == "desktop.webspace.reload"
    assert "desktop.reload_webspace" in reload_action["intents"]

    by_intent = find_system_action_by_intent("desktop.reload_webspace")
    assert by_intent == reload_action

    intents = system_action_nlu_intents()
    reload_intent = intents["desktop.reload_webspace"]
    assert reload_intent["scope"] == "system"
    assert reload_intent["action_id"] == "host.desktop.webspace.reload"
    assert reload_intent["actions"] == [
        {
            "type": "callHost",
            "target": "desktop.webspace.reload",
            "params": {"webspace_id": "$ctx.webspace_id"},
        }
    ]
    assert "reload desktop" in reload_intent["examples"]

