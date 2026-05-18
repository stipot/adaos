from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from adaos.services import named_entities


class _FakeDeviceInventory:
    def __init__(self, devices: list[dict[str, object]]) -> None:
        self._devices = list(devices)

    def list_devices(self, kind=None) -> list[dict[str, object]]:
        return [dict(item) for item in self._devices]


def _empty_lookup_provider(*, webspace_id: str = "desktop") -> dict[str, object]:
    return {"webspace_id": webspace_id, "lookups": {}}


def test_named_entity_service_exposes_current_subnet_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    from adaos.services import node_config, subnet_alias

    monkeypatch.setattr(
        node_config,
        "load_config",
        lambda: SimpleNamespace(subnet_id="sn_home", node_id="node-1"),
    )
    monkeypatch.setattr(subnet_alias, "load_subnet_alias", lambda *, subnet_id=None: "Home Assistant")

    service = named_entities.NamedEntityService(
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
        include_runtime_subnet_entity=True,
    )

    records = service.list_entities(kind="assistant")

    assert records[0].canonical_ref == "assistant:sn_home"
    assert records[0].display_label == "Home Assistant"


def test_named_entity_display_priority_prefers_registered_over_fallback() -> None:
    record = named_entities.NamedEntityRecord(
        canonical_ref="device:member:node-1",
        kind="device.member",
        registered_names=("Kitchen Display",),
        observed_name="ZVERZVE-A1BNQF7",
        fallback_label="Node 0",
    )

    assert record.display_label == "Kitchen Display"
    assert [label for label, _kind in record.label_candidates()] == [
        "Kitchen Display",
        "ZVERZVE-A1BNQF7",
    ]
    assert [label for label, _kind in record.label_candidates(include_fallback=True)][-1] == "Node 0"


def test_named_entity_record_exposes_locale_label_metadata() -> None:
    record = named_entities.NamedEntityRecord(
        canonical_ref="device:member:node-1",
        kind="device.member",
        display_name="Kitchen Display",
        labels=[
            {
                "text": "кухонный экран",
                "locale": "ru",
                "role": "alias",
                "source": "user",
            }
        ],
    )

    labels = record.to_dict()["labels"]

    assert {"text": "Kitchen Display", "locale": "und", "role": "display", "status": "confirmed"} in labels
    assert {
        "text": "кухонный экран",
        "locale": "ru",
        "role": "alias",
        "status": "confirmed",
        "source": "user",
    } in labels


def test_named_entity_service_builds_device_records_from_inventory() -> None:
    service = named_entities.NamedEntityService(
        device_inventory_service=_FakeDeviceInventory(
            [
                {
                    "ref": "member:node-1",
                    "kind": "member",
                    "identity": {
                        "node_id": "node-1",
                        "hostname": "ZVERZVE-A1BNQF7",
                        "node_names": ["Kitchen Display"],
                    },
                    "policy": {"display_name": "", "managed_state": "observed_only"},
                    "observation": {"source": "subnet_directory", "last_seen_at": 100.0},
                    "diagnostics": {"policy_source": "none"},
                }
            ]
        ),
        lookup_payload_provider=_empty_lookup_provider,
    )

    records = service.list_entities()

    assert len(records) == 1
    assert records[0].canonical_ref == "device:member:node-1"
    assert records[0].kind == "device.member"
    assert records[0].display_label == "Kitchen Display"
    assert records[0].observed_name == "ZVERZVE-A1BNQF7"


def test_named_entity_service_suggests_browser_draft_name_without_display_overwrite() -> None:
    service = named_entities.NamedEntityService(
        device_inventory_service=_FakeDeviceInventory(
            [
                {
                    "ref": "browser:browser-1",
                    "kind": "browser",
                    "identity": {
                        "browser_device_id": "browser-1",
                        "browser_family": "edge",
                        "os_name": "windows",
                        "form_factor": "desktop",
                    },
                    "policy": {"display_name": "", "managed_state": "observed_only"},
                    "observation": {"source": "browser_session", "last_seen_at": 120.0},
                    "diagnostics": {},
                }
            ]
        ),
        lookup_payload_provider=_empty_lookup_provider,
    )

    record = service.list_entities()[0]

    assert record.canonical_ref == "device:browser:browser-1"
    assert record.display_name is None
    assert record.draft_name == "Edge on Windows"
    assert record.display_label == "Edge on Windows"
    assert record.status == "draft"


def test_resolver_matches_exact_labels_without_dispatch_side_effects() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
                aliases=("kitchen screen",),
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    result = service.resolve_text("show logs for kitchen screen")

    assert result.normalized_text == "show logs for kitchen screen"
    assert [item.canonical_ref for item in result.resolved_entities] == ["device:member:node-1"]
    assert result.resolved_entities[0].match_type == "alias"
    assert result.ambiguities == ()


def test_resolver_accepts_locale_hints_without_changing_canonical_refs() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
                labels=[
                    named_entities.EntityLabel(
                        text="кухонный экран",
                        locale="ru",
                        role="alias",
                        source="user",
                    )
                ],
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    result = service.resolve_text(
        "покажи логи кухонный экран",
        request_locale="ru-RU",
        preferred_locales=("en",),
    )

    assert [item.canonical_ref for item in result.resolved_entities] == ["device:member:node-1"]
    assert result.resolved_entities[0].locale == "ru"
    assert result.to_dict()["request_locale"] == "ru-RU"
    assert result.to_dict()["preferred_locales"] == ["ru-RU", "ru", "en"]


def test_resolver_reports_ambiguity_instead_of_guessing() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
                aliases=("screen",),
            ),
            named_entities.NamedEntityRecord(
                canonical_ref="device:browser:browser-1",
                kind="device.browser",
                display_name="Edge on Windows",
                aliases=("screen",),
            ),
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    result = service.resolve_text("open screen settings")

    assert result.resolved_entities == ()
    assert len(result.ambiguities) == 1
    assert result.ambiguities[0].normalized == "screen"
    assert {item.canonical_ref for item in result.ambiguities[0].candidates} == {
        "device:browser:browser-1",
        "device:member:node-1",
    }


def test_sdk_entities_helpers_delegate_to_service(monkeypatch) -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="webspace:desktop",
                kind="webspace",
                display_name="Desktop",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )
    monkeypatch.setattr(named_entities, "get_named_entity_service", lambda: service)

    from adaos.sdk.data import entities as sdk_entities

    assert sdk_entities.list_entities()[0]["canonical_ref"] == "webspace:desktop"
    assert sdk_entities.resolve_text("open Desktop")["resolved_entities"][0]["kind"] == "webspace"


def test_named_entity_service_projects_lookup_tables_as_addressed_entities() -> None:
    def _lookup_payload_provider(*, webspace_id: str) -> dict[str, object]:
        return {
            "webspace_id": webspace_id,
            "lookups": {
                "modal_id": [
                    {
                        "value": "browser_link_settings_modal",
                        "labels": ["Browser Link Settings"],
                        "sources": ["registry.modals"],
                    }
                ],
                "app_id": [{"value": "browsers_app", "labels": ["Browsers"]}],
                "scenario_id": [{"value": "web_desktop"}],
                "webspace_id": [{"value": "desktop"}],
                "skill_id": [{"value": "browsers_skill", "labels": ["Browsers Skill", "browser tools"]}],
                "node_ref": [{"value": "Node 0", "labels": ["Node 0"]}],
            },
        }

    service = named_entities.NamedEntityService(
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_lookup_payload_provider,
    )

    records = service.list_entities(webspace_id="desktop")
    refs = {record.canonical_ref: record for record in records}

    assert "modal:browser_link_settings_modal" in refs
    assert refs["modal:browser_link_settings_modal"].display_label == "Browser Link Settings"
    assert refs["modal:browser_link_settings_modal"].registered_names == ("browser_link_settings_modal",)
    assert "app:browsers_app" in refs
    assert "scenario:web_desktop" in refs
    assert "webspace:desktop" in refs
    assert "skill:browsers_skill" in refs
    assert refs["skill:browsers_skill"].display_label == "Browsers Skill"
    assert refs["skill:browsers_skill"].aliases == ("browser tools",)
    assert "node:Node 0" not in refs

    result = service.resolve_text("open Browser Link Settings", webspace_id="desktop")
    assert result.resolved_entities[0].canonical_ref == "modal:browser_link_settings_modal"


def test_compact_registry_payload_is_ui_safe_and_fingerprinted() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="skill:browsers_skill",
                kind="skill",
                display_name="Browsers Skill",
                aliases=("browser tools",),
                source="test",
                status="confirmed",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    payload = named_entities.compact_registry_payload(service=service, webspace_id="desktop")

    assert payload["version"] == 1
    assert payload["webspace_id"] == "desktop"
    assert payload["items"] == [
        {
            "canonical_ref": "skill:browsers_skill",
            "kind": "skill",
            "display_label": "Browsers Skill",
            "labels": [
                {
                    "text": "Browsers Skill",
                    "locale": "und",
                    "role": "display",
                    "status": "confirmed",
                    "source": "test",
                },
                {
                    "text": "browser tools",
                    "locale": "und",
                    "role": "alias",
                    "status": "confirmed",
                    "source": "test",
                },
            ],
            "status": "confirmed",
            "scope": {},
            "source": "test",
            "fingerprint": payload["items"][0]["fingerprint"],
        }
    ]
    assert payload["summary"]["count"] == 1
    assert payload["summary"]["conflict_count"] == 0
    assert payload["conflicts"] == []
    assert payload["summary"]["fingerprint"]


def test_compact_registry_payload_reports_label_conflicts_without_resolving_them() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen",
            ),
            named_entities.NamedEntityRecord(
                canonical_ref="device:browser:browser-1",
                kind="device.browser",
                aliases=("Kitchen",),
            ),
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    payload = named_entities.compact_registry_payload(service=service, webspace_id="desktop")

    assert payload["summary"]["conflict_count"] == 1
    assert payload["conflicts"][0]["locale"] == "und"
    assert payload["conflicts"][0]["normalized"] == "kitchen"
    assert {item["canonical_ref"] for item in payload["conflicts"][0]["candidates"]} == {
        "device:browser:browser-1",
        "device:member:node-1",
    }


def test_compact_registry_payload_reports_conflicts_per_locale() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                labels=[
                    named_entities.EntityLabel(text="экран", locale="ru", role="alias"),
                    named_entities.EntityLabel(text="screen", locale="en", role="alias"),
                ],
            ),
            named_entities.NamedEntityRecord(
                canonical_ref="device:browser:browser-1",
                kind="device.browser",
                labels=[
                    named_entities.EntityLabel(text="экран", locale="ru", role="alias"),
                    named_entities.EntityLabel(text="screen", locale="en", role="alias"),
                ],
            ),
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    payload = named_entities.compact_registry_payload(service=service, webspace_id="desktop")

    assert payload["summary"]["conflict_count"] == 2
    assert {(item["locale"], item["normalized"]) for item in payload["conflicts"]} == {
        ("en", "screen"),
        ("ru", "экран"),
    }


def test_entity_event_payload_carries_locale_metadata() -> None:
    payload = named_entities.entity_event_payload(
        entity_ref="device:member:node-1",
        entity_kind="device.member",
        source="test",
        locale="ru-RU",
        preferred_locales=("en",),
    )

    assert payload["locale"] == "ru-RU"
    assert payload["preferred_locales"] == ["ru-RU", "ru", "en"]


def test_device_lifecycle_events_include_browser_observation_and_draft() -> None:
    events = named_entities.device_entity_lifecycle_event_envelopes(
        kind="browser",
        entry_id="dev-browser",
        previous={},
        current={
            "browser_family": "Edge",
            "os_name": "Windows",
            "form_factor": "Desktop",
            "last_webspace_id": "desktop",
        },
        source="access_links",
        reason="browser_session.changed",
    )

    assert [event["topic"] for event in events] == [
        named_entities.ENTITY_OBSERVED,
        named_entities.ENTITY_DRAFT_NAME_SUGGESTED,
    ]
    assert events[0]["payload"]["current"]["browser_family"] == "Edge"
    assert events[1]["payload"]["current"]["draft_name"] == "Edge on Windows"
    assert events[1]["payload"]["scope"]["webspace_id"] == "desktop"


def test_device_lifecycle_events_include_display_and_member_observation() -> None:
    events = named_entities.device_entity_lifecycle_event_envelopes(
        kind="member",
        entry_id="node-1",
        previous={"display_name": "Old node"},
        current={
            "display_name": "Kitchen hub",
            "hostname": "ZVERZVE-A1BNQF7",
            "node_names": ["Kitchen hub"],
        },
        source="access_links",
        reason="member_link.changed",
    )

    assert [event["topic"] for event in events] == [
        named_entities.ENTITY_DISPLAY_NAME_CHANGED,
        named_entities.ENTITY_OBSERVED,
    ]
    assert events[0]["payload"]["current"]["display_name"] == "Kitchen hub"
    assert events[1]["payload"]["current"]["observed_name"] == "ZVERZVE-A1BNQF7"


def test_governed_alias_add_returns_updated_record_and_lifecycle_events() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_add(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
        actor="user:operator",
        source="test",
        request_id="req-1",
    )
    result = service.apply_alias_add(proposal)

    assert proposal.ok is True
    assert proposal.status == "proposed"
    assert result.ok is True
    assert result.status == "applied"
    assert result.updated_record is not None
    assert {
        "text": "kitchen screen",
        "locale": "en",
        "role": "alias",
        "status": "confirmed",
        "source": "test",
    } in [item.to_dict() for item in result.updated_record.label_records()]
    assert [item["topic"] for item in result.events] == [
        named_entities.ENTITY_ALIAS_ADDED,
        named_entities.ENTITY_REGISTRY_CHANGED,
    ]
    assert result.events[0]["payload"]["locale"] == "en"
    assert result.events[0]["payload"]["current"]["label"]["text"] == "kitchen screen"


def test_governed_alias_add_reports_conflict_without_mutation() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
            ),
            named_entities.NamedEntityRecord(
                canonical_ref="device:browser:browser-1",
                kind="device.browser",
                labels=[named_entities.EntityLabel(text="screen", locale="en", role="alias")],
            ),
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_add(
        canonical_ref="device:member:node-1",
        alias="screen",
        locale="en",
        source="test",
    )
    result = service.apply_alias_add(proposal)

    assert proposal.ok is False
    assert proposal.status == "conflict"
    assert proposal.conflicts[0]["canonical_ref"] == "device:browser:browser-1"
    assert result.ok is False
    assert result.updated_record is None
    assert [item["topic"] for item in result.events] == [named_entities.ENTITY_ALIAS_CONFLICT_DETECTED]
    assert result.events[0]["payload"]["current"]["conflicts"][0]["canonical_ref"] == "device:browser:browser-1"


def test_governed_alias_add_rejects_stale_base_fingerprint() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_add(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
        source="test",
        base_fingerprint="stale-fingerprint",
    )
    result = service.apply_alias_add(proposal)

    assert proposal.ok is False
    assert proposal.status == "stale"
    assert proposal.reason == "base_fingerprint_mismatch"
    assert proposal.conflicts[0]["base_fingerprint"] == "stale-fingerprint"
    assert proposal.conflicts[0]["current_fingerprint"]
    assert result.ok is False
    assert result.status == "stale"
    assert [item["topic"] for item in result.events] == [named_entities.ENTITY_ALIAS_CONFLICT_DETECTED]
    assert result.events[0]["payload"]["current"]["base_fingerprint"] == "stale-fingerprint"


def test_governed_alias_remove_returns_updated_record_and_lifecycle_events() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
                labels=[named_entities.EntityLabel(text="kitchen screen", locale="en", role="alias")],
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_remove(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
        actor="user:operator",
        source="test",
        request_id="req-1",
    )
    result = service.apply_alias_remove(proposal)

    assert proposal.ok is True
    assert proposal.action == "alias.remove"
    assert result.ok is True
    assert result.status == "applied"
    assert result.updated_record is not None
    assert [
        item
        for item in result.updated_record.label_records()
        if item.role == "alias" and item.text == "kitchen screen"
    ] == []
    assert [item["topic"] for item in result.events] == [
        named_entities.ENTITY_ALIAS_REMOVED,
        named_entities.ENTITY_REGISTRY_CHANGED,
    ]
    assert result.events[0]["payload"]["current"]["action"] == "alias.remove"


def test_governed_alias_deprecate_marks_label_but_keeps_compat_resolution() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
                labels=[named_entities.EntityLabel(text="kitchen screen", locale="en", role="alias")],
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_deprecate(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
        source="test",
    )
    result = service.apply_alias_deprecate(proposal)

    assert proposal.ok is True
    assert proposal.action == "alias.deprecate"
    assert result.ok is True
    assert result.updated_record is not None
    deprecated = [
        item
        for item in result.updated_record.label_records()
        if item.role == "alias" and item.text == "kitchen screen"
    ]
    assert deprecated[0].status == "deprecated"
    assert [item["topic"] for item in result.events] == [
        named_entities.ENTITY_ALIAS_DEPRECATED,
        named_entities.ENTITY_REGISTRY_CHANGED,
    ]

    resolved = named_entities.NamedEntityService(
        static_entities=[result.updated_record],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    ).resolve_text("open kitchen screen")
    assert [item.canonical_ref for item in resolved.resolved_entities] == ["device:member:node-1"]


def test_governed_alias_remove_rejects_stale_base_fingerprint() -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                labels=[named_entities.EntityLabel(text="kitchen screen", locale="en", role="alias")],
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )

    proposal = service.propose_alias_remove(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
        source="test",
        base_fingerprint="stale-fingerprint",
    )
    result = service.apply_alias_remove(proposal)

    assert proposal.ok is False
    assert proposal.status == "stale"
    assert proposal.action == "alias.remove"
    assert result.ok is False
    assert result.status == "stale"
    assert [item["topic"] for item in result.events] == [named_entities.ENTITY_ALIAS_CONFLICT_DETECTED]


def test_sdk_entities_alias_helpers_delegate_to_named_entity_service(monkeypatch) -> None:
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="device:member:node-1",
                kind="device.member",
                display_name="Kitchen Display",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )
    monkeypatch.setattr(named_entities, "get_named_entity_service", lambda: service)

    from adaos.sdk.data import entities as sdk_entities

    proposal = sdk_entities.propose_alias_add(
        canonical_ref="device:member:node-1",
        alias="kitchen screen",
        locale="en",
    )
    result = sdk_entities.apply_alias_add(proposal)

    assert proposal["status"] == "proposed"
    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["updated_record"]["labels"][-1]["text"] == "kitchen screen"


@pytest.mark.anyio
async def test_project_named_entity_registry_writes_compact_yjs_branch(monkeypatch) -> None:
    pytest.importorskip("y_py")
    from adaos.services import named_entity_projection
    from adaos.services.yjs.doc import async_get_ydoc
    from adaos.services.yjs.store import reset_ystore_for_webspace

    webspace_id = f"named-entities-{uuid4().hex}"
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="skill:browsers_skill",
                kind="skill",
                display_name="Browsers Skill",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )
    monkeypatch.setattr(named_entities, "get_named_entity_service", lambda: service)

    try:
        payload = await named_entity_projection.project_named_entity_registry(webspace_id=webspace_id)

        async with async_get_ydoc(webspace_id, read_only=True, load_mark_roots=["registry"]) as ydoc:
            current = ydoc.get_map("registry").get("named_entities")
        assert current["summary"]["fingerprint"] == payload["summary"]["fingerprint"]
        assert current["items"][0]["canonical_ref"] == "skill:browsers_skill"
    finally:
        reset_ystore_for_webspace(webspace_id)


@pytest.mark.anyio
async def test_subnet_alias_change_projects_named_entities_to_current_and_default_webspaces(monkeypatch) -> None:
    pytest.importorskip("y_py")
    from adaos.services import named_entity_projection
    from adaos.services.yjs.doc import async_get_ydoc
    from adaos.services.yjs.store import reset_ystore_for_webspace

    webspace_id = f"named-entities-{uuid4().hex}"
    default_id = f"named-default-{uuid4().hex}"
    service = named_entities.NamedEntityService(
        static_entities=[
            named_entities.NamedEntityRecord(
                canonical_ref="assistant:sn_home",
                kind="assistant",
                display_name="HomeAssistant",
            )
        ],
        device_inventory_service=_FakeDeviceInventory([]),
        lookup_payload_provider=_empty_lookup_provider,
    )
    monkeypatch.setattr(named_entities, "get_named_entity_service", lambda: service)
    monkeypatch.setattr(named_entity_projection, "default_webspace_id", lambda: default_id)

    try:
        await named_entity_projection.on_entity_registry_changed(
            SimpleNamespace(type="subnet.alias.changed", payload={"webspace_id": webspace_id}),
        )

        async with async_get_ydoc(webspace_id, read_only=True, load_mark_roots=["registry"]) as ydoc:
            current = ydoc.get_map("registry").get("named_entities")
        assert current["items"][0]["display_label"] == "HomeAssistant"

        async with async_get_ydoc(default_id, read_only=True, load_mark_roots=["registry"]) as ydoc:
            default_current = ydoc.get_map("registry").get("named_entities")
        assert default_current["items"][0]["display_label"] == "HomeAssistant"
    finally:
        reset_ystore_for_webspace(webspace_id)
        reset_ystore_for_webspace(default_id)
