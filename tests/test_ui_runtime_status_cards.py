from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.adapters.fs.path_provider import PathProvider
from adaos.adapters.sdk.inproc_skill_context import InprocSkillContext
from adaos.apps.api.auth import require_token
from adaos.services.agent_context import clear_ctx, set_ctx
from adaos.services.projection_demand import clear_projection_demand_registry
from adaos.services.projection_dispatcher import clear_projection_dispatcher
from adaos.services.projection_records import clear_projection_record_registry
from adaos.services.status_card_registry import (
    clear_status_card_registry,
    get_status_card,
    status_card_projection_record,
)
from adaos.services.ui_runtime_diagnostics import ingest_ui_runtime_diagnostics


def setup_function() -> None:
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_projection_record_registry()
    clear_status_card_registry()
    clear_ctx()


def teardown_function() -> None:
    clear_ctx()


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


def _set_test_context(tmp_path: Path) -> PathProvider:
    paths = PathProvider(tmp_path)
    paths.ensure_tree()
    set_ctx(SimpleNamespace(paths=paths, skill_ctx=InprocSkillContext()))
    return paths


def test_ui_runtime_diagnostics_publish_status_card(tmp_path: Path) -> None:
    paths = _set_test_context(tmp_path)

    result = asyncio.run(
        ingest_ui_runtime_diagnostics(
            {
                "webspace_id": "desktop",
                "events": [
                    {
                        "level": "warning",
                        "source": "ui.modal",
                        "code": "modal.not_found",
                        "message": "Modal missing.",
                        "skillId": "browsers_skill",
                        "details": {"requestedId": "browser_link_settings_modal"},
                    }
                ],
            }
        )
    )

    assert result["accepted"] == 1
    assert result["status_card"]["id"] == "ui-runtime"
    assert result["status_card"]["status"] == "warning"
    assert result["status_card"]["scope"]["level_counts"] == {"WARNING": 1}
    assert result["status_card"]["scope"]["skill_ids"] == ["browsers_skill"]

    card = get_status_card(card_id="ui-runtime", webspace_id="desktop")
    assert card is not None
    assert card.owner == "core:ui-runtime"
    assert card.kind == "ui-runtime-diagnostics"

    record = status_card_projection_record(card_id="ui-runtime", webspace_id="desktop", now=card.updated_at)
    assert record is not None
    assert record.meta.projection_key == "status-card:ui-runtime"
    assert record.data["details_ref"]["path"] == "/api/node/logs"

    log_path = paths.skill_ui_diagnostics_log_path("browsers_skill")
    line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert line["code"] == "modal.not_found"


def test_ui_runtime_diagnostics_skip_status_card_when_no_events(tmp_path: Path) -> None:
    _set_test_context(tmp_path)

    result = asyncio.run(
        ingest_ui_runtime_diagnostics(
            {"webspace_id": "desktop", "events": [{"message": ""}]}
        )
    )

    assert result["accepted"] == 0
    assert result["status_card"] is None
    assert get_status_card(card_id="ui-runtime", webspace_id="desktop") is None


def test_ui_runtime_diagnostics_api_exposes_status_card(tmp_path: Path) -> None:
    _set_test_context(tmp_path)
    client = _make_client()

    resp = client.post(
        "/api/node/ui/diagnostics",
        json={
            "webspace_id": "desktop",
            "events": [
                {
                    "level": "error",
                    "source": "ui.widget",
                    "code": "widget.render_failed",
                    "message": "Widget render failed.",
                    "skillId": "weather_skill",
                }
            ],
        },
    )
    snapshot_resp = client.get(
        "/api/node/status-cards",
        params={"webspace_id": "desktop", "include_runtime": False},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status_card"]["id"] == "ui-runtime"
    assert payload["status_card"]["status"] == "degraded"
    assert payload["status_card"]["scope"]["codes"] == ["widget.render_failed"]

    assert snapshot_resp.status_code == 200
    cards = snapshot_resp.json()["cards"]
    assert [card["id"] for card in cards] == ["ui-runtime"]
    assert cards[0]["scope"]["skill_ids"] == ["weather_skill"]
