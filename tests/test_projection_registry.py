from __future__ import annotations

from adaos.services.scenario.projection_registry import (
    PROJECTION_MANIFEST_SCHEMA,
    ProjectionRegistry,
    inspect_projection_manifest_entries,
    projection_manifest_contract,
)


def test_projection_registry_active_scenario_overrides_skill_defaults(monkeypatch) -> None:
    registry = ProjectionRegistry()
    registry.load_entries(
        [
            {
                "scope": "subnet",
                "slot": "weather.snapshot",
                "targets": [
                    {
                        "backend": "yjs",
                        "path": "data/weather/default",
                        "projection_key": "projection:widget/weather",
                    }
                ],
            }
        ]
    )

    monkeypatch.setattr(
        "adaos.services.scenario.projection_registry.read_manifest",
        lambda scenario_id, *, space="workspace": {
            "data_projections": [
                {
                    "scope": "subnet",
                    "slot": "weather.snapshot",
                    "targets": [{"backend": "yjs", "path": f"data/weather/{scenario_id}"}],
                }
            ]
        },
    )

    loaded = registry.load_from_scenario("storm_lab", space="dev")

    resolved = registry.resolve("subnet", "weather.snapshot")
    assert loaded == 1
    assert registry.active_scenario_id() == "storm_lab"
    assert registry.active_space() == "dev"
    assert len(resolved) == 1
    assert resolved[0].path == "data/weather/storm_lab"
    assert registry.snapshot()["schema"] == PROJECTION_MANIFEST_SCHEMA


def test_projection_registry_clears_stale_scenario_overrides(monkeypatch) -> None:
    registry = ProjectionRegistry()
    registry.load_entries(
        [
            {
                "scope": "subnet",
                "slot": "infrastate.snapshot",
                "targets": [{"backend": "yjs", "path": "data/infrastate/base"}],
            }
        ]
    )

    def _read_manifest(scenario_id: str, *, space: str = "workspace") -> dict[str, object]:
        if scenario_id == "with_override":
            return {
                "data_projections": [
                    {
                        "scope": "subnet",
                        "slot": "infrastate.snapshot",
                        "targets": [{"backend": "yjs", "path": "data/infrastate/override"}],
                    }
                ]
            }
        return {"data_projections": []}

    monkeypatch.setattr("adaos.services.scenario.projection_registry.read_manifest", _read_manifest)

    registry.load_from_scenario("with_override", space="workspace")
    overridden = registry.resolve("subnet", "infrastate.snapshot")
    registry.load_from_scenario("without_override", space="dev")
    restored = registry.resolve("subnet", "infrastate.snapshot")

    assert overridden[0].path == "data/infrastate/override"
    assert registry.active_scenario_id() == "without_override"
    assert registry.active_space() == "dev"
    assert restored[0].path == "data/infrastate/base"


def test_projection_registry_preserves_projection_key_on_targets() -> None:
    registry = ProjectionRegistry()

    loaded = registry.load_entries(
        [
            {
                "scope": "subnet",
                "slot": "infrascope.overview",
                "targets": [
                    {
                        "backend": "yjs",
                        "path": "data/infrascope/overview",
                        "projection_key": "status-card:infrascope-overview",
                    }
                ],
            }
        ]
    )

    resolved = registry.resolve("subnet", "infrascope.overview")
    assert loaded == 1
    assert resolved[0].projection_key == "status-card:infrascope-overview"


def test_projection_manifest_inspection_reports_contract_and_risks() -> None:
    report = inspect_projection_manifest_entries(
        [
            {
                "scope": "subnet",
                "slot": "voice.snapshot",
                "targets": [{"backend": "yjs", "path": "data/voice_chat"}],
            },
            {
                "scope": "subnet",
                "slot": "infrascope.overview",
                "targets": [
                    {
                        "backend": "yjs",
                        "path": "data/infrascope/overview",
                        "projection_key": "status-card:infrascope-overview",
                    }
                ],
            },
            {
                "scope": "subnet",
                "slot": "bad.cache",
                "targets": [{"backend": "yjs", "path": "data/projectionRecords"}],
            },
        ]
    )

    assert report["schema"] == PROJECTION_MANIFEST_SCHEMA
    assert report["ok"] is False
    assert report["rule_total"] == 3
    assert report["yjs_target_total"] == 3
    assert report["yjs_target_with_projection_key_total"] == 1
    assert report["legacy_monolithic_target_total"] == 1
    assert report["reserved_cache_target_total"] == 1
    assert {item["finding"] for item in report["findings"]} == {
        "legacy_monolithic_yjs_root",
        "reserved_projection_record_cache_target",
    }


def test_projection_manifest_contract_documents_shared_shape() -> None:
    contract = projection_manifest_contract()

    assert contract["schema"] == PROJECTION_MANIFEST_SCHEMA
    assert contract["logical_identity"] == ["scope", "slot"]
    assert contract["yjs_rules"]["canonical_projection_key"] == "target.projection_key"
    assert contract["yjs_rules"]["reserved_paths"] == ["data/projectionRecords"]
