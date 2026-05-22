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
    assert by_skill["infrascope_skill"]["risk"] == "medium"
    assert by_skill["infrascope_skill"]["shared_bridge"] == "status-card-adapter"
    assert by_skill["adaos_connect"]["roots"][0]["shape"] == "single-yjs-slot"
    assert by_skill["prompt_engineer_skill"]["monolithic_candidate"] is False


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
    assert metrics["modern_surface_total"] == 3
    assert metrics["observed_surface_total"] == 5
    assert metrics["migration_readiness_ratio"] == 0.6
    assert metrics["monolith_exposure_ratio"] == 0.4
    assert metrics["legacy_pressure_score"] == 5
    assert report["top_monolithic_candidates"][0]["skill_id"] == "voice_chat_skill"
    assert {item["metric"] for item in report["metric_definitions"]} == {
        "legacy_pressure_score",
        "local_shim_pressure_score",
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
