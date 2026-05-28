from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.projection_pilot_readiness import projection_pilot_readiness_contract_snapshot


def _make_client() -> TestClient:
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


def test_projection_pilot_readiness_contract_snapshot_selects_followup() -> None:
    snapshot = projection_pilot_readiness_contract_snapshot(now=130.0)

    assert snapshot["contract"] == "adaos.projection-pilot.readiness.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 130.0
    assert snapshot["infrascope_after_prereqs"]["selected"] is False
    assert snapshot["infrascope_after_prereqs"]["status"] == "blocked_until_platform_emitter_validation"
    assert "platform_status_card_projection_contract" in snapshot["infrascope_after_prereqs"]["required_gate_ids"]
    assert snapshot["dev_scenario_followup"]["scenario_id"] == "prompt_engineer_scenario"
    assert snapshot["dev_scenario_followup"]["skill_id"] == "prompt_engineer_skill"
    assert snapshot["simple_skills_deferred"]["status"] == "deferred_until_adapter_stable"
    assert snapshot["boundaries"]["does_not_create_pilot_specific_abi"] is True


def test_projection_pilot_readiness_contract_endpoint_exposes_followup() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-pilot/readiness-contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.projection-pilot.readiness.v1"
    assert payload["dev_scenario_followup"]["scenario_id"] == "prompt_engineer_scenario"
    assert payload["simple_skills_deferred"]["selected"] is True
    assert payload["boundaries"]["uses_acceptance_summary_as_gate_source"] is True
