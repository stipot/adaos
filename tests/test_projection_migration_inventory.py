from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx
from adaos.services.projection_migration_inventory import (
    legacy_projection_branch_compatibility,
    projection_migration_acceptance_summary,
    projection_migration_metrics,
    projection_migration_monolith_inventory,
    projection_migration_recommendations,
)


def _write_skill(root: Path, name: str, *, skill_yaml: str, webui: dict, handler_text: str | None = None) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(skill_yaml, encoding="utf-8")
    (skill_dir / "webui.json").write_text(json.dumps(webui), encoding="utf-8")
    if handler_text is not None:
        handler_path = skill_dir / "handlers" / "main.py"
        handler_path.parent.mkdir(parents=True, exist_ok=True)
        handler_path.write_text(handler_text, encoding="utf-8")


def _voice_skill_yaml() -> str:
    return """
name: voice_chat_skill
version: 0.1
data_projections:
- scope: subnet
  slot: voice_chat.state
  targets:
  - backend: yjs
    path: data/voice_chat
"""


def _single_slot_skill_yaml() -> str:
    return """
name: adaos_connect
version: 0.16
data_projections:
- scope: subnet
  slot: adaos_connect.current
  targets:
  - backend: yjs
    path: data/adaos_connect/current
    projection_key: projection:panel/adaos_connect-current
"""


def _infrascope_skill_yaml() -> str:
    return """
name: infrascope_skill
version: 0.28
data_projections:
- scope: subnet
  slot: infrascope.snapshot
  targets:
  - backend: yjs
    path: data/infrascope
"""


def _static_skill_yaml() -> str:
    return "name: prompt_engineer_skill\nversion: 0.1\n"


def test_legacy_projection_branch_compatibility_rules() -> None:
    monolithic = legacy_projection_branch_compatibility(
        "data/voice_chat",
        shape="monolithic-yjs-root",
        projection_key="projection:panel/voice_chat",
    )
    assert monolithic["compatible"] is True
    assert monolithic["legacy_branch"] is True
    assert monolithic["classification"] == "legacy-monolithic-root"
    assert monolithic["write_policy"] == "projection-record-only"
    assert monolithic["projection_record_required"] is True
    assert monolithic["migration_action"] == "split_monolithic_root_to_projection_records"

    single_slot = legacy_projection_branch_compatibility("y:data/adaos_connect/current", shape="single-yjs-slot")
    assert single_slot["path"] == "data/adaos_connect/current"
    assert single_slot["classification"] == "legacy-single-slot"
    assert single_slot["migration_action"] == "map_slot_to_projection_key"

    cache = legacy_projection_branch_compatibility("data/projectionRecords")
    assert cache["legacy_branch"] is False
    assert cache["classification"] == "projection-record-cache"
    assert cache["write_policy"] == "core-owned-cache"
    assert cache["projection_record_required"] is False

    unsupported = legacy_projection_branch_compatibility("ui/current_scenario")
    assert unsupported["compatible"] is False
    assert unsupported["classification"] == "unsupported-yjs-path"
    assert unsupported["write_policy"] == "reject"


def test_projection_migration_inventory_identifies_monolithic_skill_publishers(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={
            "apps": [{"id": "voice_chat_app"}],
            "registry": {
                "modals": {
                    "voice_chat_modal": {
                        "schema": {
                            "widgets": [
                                {"dataSource": {"kind": "y", "path": "data/voice_chat"}},
                            ]
                        }
                    }
                }
            },
            "ydoc_defaults": {"data/voice_chat": {"messages": []}},
        },
    )
    _write_skill(
        root,
        "adaos_connect",
        skill_yaml=_single_slot_skill_yaml(),
        webui={
            "apps": [{"id": "adaos_connect_app"}],
            "registry": {
                "modals": {
                    "connect_modal": {
                        "schema": {
                            "widgets": [
                                {"source": "y:data/adaos_connect/current"},
                            ]
                        }
                    }
                }
            },
        },
    )
    _write_skill(
        root,
        "infrascope_skill",
        skill_yaml=_infrascope_skill_yaml(),
        webui={
            "webio": {"receivers": {"infrascope.inventory.*": {"mode": "replace"}}},
            "apps": [{"id": "infrascope_app"}],
        },
    )
    _write_skill(
        root,
        "prompt_engineer_skill",
        skill_yaml=_static_skill_yaml(),
        webui={
            "apps": [{"id": "prompt_app"}],
            "registry": {
                "modals": {
                    "prompt_modal": {
                        "schema": {
                            "widgets": [
                                {"dataSource": {"kind": "static", "value": {"ok": True}}},
                            ]
                        }
                    }
                }
            },
        },
    )

    inventory = projection_migration_monolith_inventory(skills_root=root, include_non_browser=True, now=100.0)
    by_skill = {item["skill_id"]: item for item in inventory["items"]}

    assert inventory["monolithic_candidate_total"] == 2
    assert by_skill["voice_chat_skill"]["risk"] == "high"
    assert by_skill["voice_chat_skill"]["roots"][0]["shape"] == "monolithic-yjs-root"
    assert by_skill["voice_chat_skill"]["roots"][0]["compatibility"]["classification"] == "legacy-monolithic-root"
    assert by_skill["infrascope_skill"]["risk"] == "medium"
    assert by_skill["infrascope_skill"]["shared_bridge"] == "status-card-adapter"
    assert by_skill["adaos_connect"]["roots"][0]["shape"] == "single-yjs-slot"
    assert by_skill["adaos_connect"]["roots"][0]["compatibility"]["classification"] == "legacy-single-slot"
    assert by_skill["adaos_connect"]["projection_keyed_yjs_target_total"] == 1
    assert by_skill["adaos_connect"]["manifest_contract"]["schema"] == "adaos.data-projections.v1"
    assert by_skill["voice_chat_skill"]["legacy_monolithic_manifest_target_total"] == 1
    assert by_skill["prompt_engineer_skill"]["monolithic_candidate"] is False
    assert inventory["legacy_compatible_root_total"] == 3
    assert inventory["projection_keyed_yjs_target_total"] == 1


def test_projection_migration_inventory_reports_local_projection_shims(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "legacy_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "legacy_app"}]},
        handler_text="""
from concurrent.futures import ThreadPoolExecutor
from adaos.sdk.data import ctx_subnet

_projection_fingerprints: dict[str, str] = {}
_PROJECTION_EXECUTOR = ThreadPoolExecutor(max_workers=1)

def _ensure_skill_data_projections():
    pass

async def publish(payload, webspace_id):
    await ctx_subnet.set_async("legacy.snapshot", payload, webspace_id=webspace_id)
""",
    )
    _write_skill(
        root,
        "sdk_skill",
        skill_yaml=_single_slot_skill_yaml(),
        webui={"apps": [{"id": "sdk_app"}]},
        handler_text="""
from adaos.sdk.data import ProjectionRuntime, ProjectionSlot

runtime = ProjectionRuntime("sdk_skill", projections=[ProjectionSlot("sdk.summary")])
""",
    )

    inventory = projection_migration_monolith_inventory(skills_root=root, include_non_browser=True, now=100.0)
    by_skill = {item["skill_id"]: item for item in inventory["items"]}
    legacy = by_skill["voice_chat_skill"]
    sdk = by_skill["adaos_connect"]

    assert legacy["shim_total"] == 4
    assert legacy["shim_ids"] == [
        "direct_ctx_subnet_write",
        "local_data_projection_loader",
        "local_executor_bridge",
        "local_fingerprint_cache",
    ]
    assert legacy["sdk_runtime_present"] is False
    assert sdk["shim_total"] == 0
    assert sdk["sdk_runtime_present"] is True
    assert inventory["skill_local_shim_total"] == 1


def test_projection_migration_metrics_exposes_control_ratios(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "voice_chat_app"}], "ydoc_defaults": {"data/voice_chat": {"messages": []}}},
    )
    _write_skill(
        root,
        "adaos_connect",
        skill_yaml=_single_slot_skill_yaml(),
        webui={"apps": [{"id": "adaos_connect_app"}]},
    )
    _write_skill(
        root,
        "infrascope_skill",
        skill_yaml=_infrascope_skill_yaml(),
        webui={"webio": {"receivers": {"infrascope.inventory.*": {"mode": "replace"}}}},
        handler_text="""
from concurrent.futures import ThreadPoolExecutor
from adaos.sdk.data import ctx_subnet

_last_projected_fingerprints: dict[str, str] = {}
_PROJECTION_EXECUTOR = ThreadPoolExecutor(max_workers=1)

def project(payload, webspace_id):
    ctx_subnet.set("infrascope.snapshot", payload, webspace_id=webspace_id)
""",
    )

    report = projection_migration_metrics(
        skills_root=root,
        include_non_browser=True,
        now=100.0,
    )
    metrics = report["metrics"]

    assert report["ok"] is True
    assert metrics["skill_total"] == 3
    assert metrics["monolithic_candidate_total"] == 2
    assert metrics["monolithic_root_total"] == 2
    assert metrics["single_yjs_slot_total"] == 1
    assert metrics["stream_receiver_total"] == 1
    assert metrics["shared_bridge_total"] == 1
    assert metrics["skill_local_shim_total"] == 1
    assert metrics["direct_write_skill_total"] == 1
    assert metrics["fingerprint_shim_skill_total"] == 1
    assert metrics["executor_shim_skill_total"] == 1
    assert metrics["local_shim_pressure_score"] == 7
    assert metrics["legacy_compatible_root_total"] == 3
    assert metrics["projection_record_cache_root_total"] == 0
    assert metrics["manifest_yjs_target_total"] == 3
    assert metrics["projection_keyed_yjs_target_total"] == 1
    assert metrics["reserved_cache_manifest_target_total"] == 0
    assert metrics["legacy_monolithic_manifest_target_total"] == 2
    assert metrics["manifest_projection_key_coverage_ratio"] == 0.3333
    assert metrics["modern_surface_total"] == 3
    assert metrics["observed_surface_total"] == 5
    assert metrics["migration_readiness_ratio"] == 0.6
    assert metrics["monolith_exposure_ratio"] == 0.4
    assert metrics["legacy_pressure_score"] == 5
    assert report["top_monolithic_candidates"][0]["skill_id"] == "voice_chat_skill"
    assert {item["metric"] for item in report["metric_definitions"]} == {
        "legacy_pressure_score",
        "local_shim_pressure_score",
        "manifest_projection_key_coverage_ratio",
        "migration_readiness_ratio",
        "monolith_exposure_ratio",
    }


def test_projection_migration_recommendations_prioritize_risky_work(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "voice_chat_app"}]},
        handler_text="""
from adaos.sdk.data import ctx_subnet

_projection_fingerprints: dict[str, str] = {}

async def publish(payload, webspace_id):
    await ctx_subnet.set_async("voice_chat.state", payload, webspace_id=webspace_id)
""",
    )
    _write_skill(
        root,
        "infrascope_skill",
        skill_yaml=_infrascope_skill_yaml(),
        webui={"webio": {"receivers": {"infrascope.inventory.*": {"mode": "replace"}}}},
    )

    report = projection_migration_recommendations(
        skills_root=root,
        include_non_browser=True,
        now=100.0,
    )

    assert report["ok"] is True
    assert report["recommendation_total"] == 2
    assert report["items"][0]["skill_id"] == "voice_chat_skill"
    assert report["items"][0]["recommended_next_step"] == "introduce_projection_slots_or_status_bridge"
    assert "replace_direct_ctx_subnet_write" in {action["id"] for action in report["items"][0]["actions"]}
    assert report["items"][1]["skill_id"] == "infrascope_skill"
    assert report["items"][1]["recommended_next_step"] == "split_monolithic_root_behind_shared_bridge"


def test_projection_migration_acceptance_summary_marks_server_mvp_ready_with_followups(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "voice_chat_app"}]},
        handler_text="""
from adaos.sdk.data import ctx_subnet

async def publish(payload, webspace_id):
    await ctx_subnet.set_async("voice_chat.state", payload, webspace_id=webspace_id)
""",
    )
    _write_skill(
        root,
        "infrascope_skill",
        skill_yaml=_infrascope_skill_yaml(),
        webui={"webio": {"receivers": {"infrascope.inventory.*": {"mode": "replace"}}}},
    )

    summary = projection_migration_acceptance_summary(
        skills_root=root,
        include_non_browser=True,
        now=100.0,
    )
    checks = {item["id"]: item for item in summary["checks"]}

    assert summary["ok"] is True
    assert summary["server_mvp_ready"] is True
    assert summary["status"] == "ready_with_followups"
    assert summary["fail_total"] == 0
    assert "demonstrable" in summary["interpretation"]["meaning"]
    assert summary["manual_review"]["expected_for_demo"] == "server_mvp_ready=true and fail_total=0"
    assert "checks" in summary["manual_review"]["inspect_first"]
    assert summary["manual_steps"][0]["endpoint"] == "/api/node/projection-migration/acceptance-summary"
    assert summary["manual_steps"][1]["endpoint"] == "/api/node/projection-migration/metrics"
    assert "metrics.migration_readiness_ratio" in summary["manual_steps"][1]["look_at"]
    assert summary["swagger_verification"]["endpoint"] == "/api/node/projection-migration/acceptance-summary"
    assert "server_mvp_ready=true" in summary["swagger_verification"]["expected_ok"]
    assert "risk_register.risks" in summary["swagger_verification"]["inspect_fields"]
    assert summary["request_examples"]["base_url"] == "http://127.0.0.1:8777"
    assert summary["request_examples"]["examples"][0]["id"] == "acceptance_summary"
    assert "x-adaos-token: dev-local-token" in summary["request_examples"]["examples"][0]["curl"]
    assert summary["traceability_matrix"][0]["plan_item"] == "Slice 2 Browser Demand Runtime"
    assert "completion_gates" in summary["traceability_matrix"][-1]["api_fields"][0]
    assert "chapter 3" in summary["traceability_matrix"][3]["vkr_use"]
    evidence_by_metric = {item["metric"]: item for item in summary["evidence_rows"]}
    assert evidence_by_metric["monolith_exposure_ratio"]["direction"] == "lower_is_better"
    assert evidence_by_metric["reserved_cache_manifest_target_total"]["direction"] == "must_be_zero"
    assert "ProjectionRecord ABI" in evidence_by_metric["manifest_projection_key_coverage_ratio"]["vkr_use"]
    measurement_by_metric = {item["metric"]: item for item in summary["measurement_model"]["rows"]}
    assert measurement_by_metric["migration_readiness_ratio"]["comparison_rule"] == "current_value > baseline_value"
    assert measurement_by_metric["monolith_exposure_ratio"]["comparison_rule"] == "current_value < baseline_value"
    assert measurement_by_metric["migration_readiness_ratio"]["current_value"] == 0.5
    assert "legacy_pressure_score" in summary["measurement_model"]["primary_metrics"]
    assert summary["demo_script"]["current_result"] == "Current status is ready_with_followups; fail_total=0, warn_total=1."
    assert "ready for demonstration" in summary["demo_script"]["conclusion"]
    assert summary["progress"]["server_mvp_percent"] == 91.7
    assert summary["progress"]["full_plan_estimate_percent"] == 65.0
    assert "browser client adapter and projection cache" in summary["progress"]["remaining_groups"]
    assert summary["progress"]["remaining_group_details"][0]["group"] == "browser client adapter and projection cache"
    assert "verification" in summary["progress"]["remaining_group_details"][0]
    assert [item["order"] for item in summary["progress"]["followup_roadmap"]] == [1, 2, 3, 4]
    assert summary["progress"]["followup_roadmap"][0]["milestone"] == "browser_projection_record_read_path"
    assert summary["control_snapshot"]["kind"] == "projection-migration-control-snapshot"
    assert summary["control_snapshot"]["result"]["server_mvp_percent"] == 91.7
    assert summary["control_snapshot"]["key_metrics"]["migration_readiness_ratio"] == 0.5
    assert "diploma" in summary["control_snapshot"]["save_hint"]
    assert summary["plan_review"]["reference"] == "docs/architecture/operational-event-model-reference-plan.md"
    assert summary["plan_review"]["overall"]["server_mvp_percent"] == 91.7
    assert [item["slice"] for item in summary["plan_review"]["slices"]] == [1, 2, 3, 4, 5, 6]
    assert summary["plan_review"]["slices"][-1]["state"] == "mvp_acceptance_ready"
    assert summary["completion_gates"]["source"] == "Completion Definition"
    assert summary["completion_gates"]["gate_total"] == 7
    assert summary["completion_gates"]["status"] == "ready_with_followups"
    assert summary["completion_gates"]["pass_total"] == 4
    assert {item["id"] for item in summary["completion_gates"]["gates"]} >= {
        "browser_multi_demand",
        "heavy_pilot_shared_abi",
    }
    assert summary["risk_register"]["status"] == "watch"
    assert summary["risk_register"]["risk_total"] == 4
    assert {item["id"] for item in summary["risk_register"]["risks"]} >= {
        "risk.browser_multi_demand",
        "risk.legacy_projection_backlog",
    }
    assert checks["manifest_contract_guarded"]["status"] == "pass"
    assert checks["shared_bridge_present"]["status"] == "pass"
    assert checks["legacy_work_bounded"]["status"] == "warn"
    assert summary["metrics"]["manifest_projection_key_coverage_ratio"] == 0.0


def _make_api_client() -> TestClient:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)

    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def test_projection_migration_inventory_api_uses_workspace_skills() -> None:
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={
            "apps": [{"id": "voice_chat_app"}],
            "registry": {
                "modals": {
                    "voice_chat_modal": {
                        "schema": {"widgets": [{"dataSource": {"kind": "y", "path": "data/voice_chat"}}]}
                    }
                }
            },
        },
    )
    client = _make_api_client()

    response = client.get("/api/node/projection-migration/monolith-inventory")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["monolithic_candidate_total"] == 1
    assert payload["items"][0]["skill_id"] == "voice_chat_skill"


def test_projection_migration_metrics_api_uses_workspace_skills() -> None:
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "voice_chat_app"}]},
    )
    client = _make_api_client()

    response = client.get("/api/node/projection-migration/metrics")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["metrics"]["monolithic_candidate_total"] == 1
    assert payload["metric_definitions"][0]["metric"] == "monolith_exposure_ratio"


def test_projection_migration_recommendations_api_uses_workspace_skills() -> None:
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    _write_skill(
        root,
        "voice_chat_skill",
        skill_yaml=_voice_skill_yaml(),
        webui={"apps": [{"id": "voice_chat_app"}]},
        handler_text="""
from adaos.sdk.data import ctx_subnet

async def publish(payload, webspace_id):
    await ctx_subnet.set_async("voice_chat.state", payload, webspace_id=webspace_id)
""",
    )
    client = _make_api_client()

    response = client.get("/api/node/projection-migration/recommendations?limit=1")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["recommendation_total"] == 1
    assert payload["items"][0]["skill_id"] == "voice_chat_skill"
    assert payload["items"][0]["actions"][0]["category"] == "monolith"


def test_projection_migration_acceptance_summary_api_uses_workspace_skills() -> None:
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    _write_skill(
        root,
        "infrascope_skill",
        skill_yaml=_infrascope_skill_yaml(),
        webui={"webio": {"receivers": {"infrascope.inventory.*": {"mode": "replace"}}}},
    )
    client = _make_api_client()

    response = client.get("/api/node/projection-migration/acceptance-summary")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["server_mvp_ready"] is True
    assert payload["scope"] == "server-side operational event model MVP"
    assert payload["manual_review"]["acceptable_warning_status"] == "ready_with_followups"
    assert payload["interpretation"]["how_to_read"][0].startswith("server_mvp_ready=true")
    assert [item["endpoint"] for item in payload["manual_steps"]] == [
        "/api/node/projection-migration/acceptance-summary",
        "/api/node/projection-migration/metrics",
        "/api/node/projection-migration/monolith-inventory",
        "/api/node/projection-migration/recommendations",
    ]
    assert payload["swagger_verification"]["headers"]["x-adaos-token"] == "dev-local-token"
    assert "ready_with_followups" in payload["swagger_verification"]["acceptable_warning"]
    assert payload["request_examples"]["examples"][1]["id"] == "migration_metrics"
    assert payload["request_examples"]["examples"][2]["url"].endswith("/api/node/projection-migration/recommendations")
    assert payload["traceability_matrix"][-1]["plan_item"] == "Completion Definition"
    assert "request_examples" in payload["traceability_matrix"][-1]["api_fields"]
    assert {item["metric"] for item in payload["evidence_rows"]} >= {
        "monolith_exposure_ratio",
        "migration_readiness_ratio",
        "reserved_cache_manifest_target_total",
    }
    assert payload["measurement_model"]["endpoint"] == "/api/node/projection-migration/metrics"
    assert payload["measurement_model"]["rows"][0]["baseline_source"]
    assert payload["demo_script"]["expected_result"] == "The demo is acceptable when server_mvp_ready=true and fail_total=0."
    assert payload["progress"]["server_mvp_percent"] >= 80.0
    assert "migration_readiness_ratio" in payload["progress"]["headline_metrics"]
    assert payload["progress"]["remaining_group_details"][0]["verification"]
    assert payload["progress"]["followup_roadmap"][-1]["milestone"] == "legacy_projection_cleanup"
    assert payload["control_snapshot"]["source_endpoint"] == "/api/node/projection-migration/acceptance-summary"
    assert payload["control_snapshot"]["result"]["server_mvp_ready"] is True
    assert payload["plan_review"]["overall"]["server_mvp_ready"] is True
    assert payload["plan_review"]["slices"][1]["name"] == "Browser Demand Runtime"
    assert payload["completion_gates"]["server_mvp_ready"] is True
    assert payload["completion_gates"]["warn_total"] >= 1
    assert payload["risk_register"]["risk_total"] >= 3
    assert payload["risk_register"]["risks"][0]["mitigation"]
    assert {item["id"] for item in payload["checks"]} >= {
        "inventory_observable",
        "manifest_contract_guarded",
        "migration_backlog_ranked",
    }
