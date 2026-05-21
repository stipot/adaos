from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.desktop_status_cards import publish_desktop_status_card
from adaos.services.io_web.desktop import WebDesktopInstalled, WebDesktopSnapshot
from adaos.services.status_card_registry import (
    clear_status_card_registry,
    get_status_card,
    status_card_projection_record,
)


def setup_function() -> None:
    clear_status_card_registry()


def teardown_function() -> None:
    clear_status_card_registry()


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


def _desktop_snapshot(*, with_page_schema: bool = True) -> WebDesktopSnapshot:
    return WebDesktopSnapshot(
        installed=WebDesktopInstalled(
            apps=["scenario:prompt_engineer_scenario"],
            widgets=["weather", "infra-status"],
        ),
        pinned_widgets=[{"id": "infra-status", "type": "visual.metricTile"}],
        topbar=[{"id": "home"}],
        page_schema={
            "id": "desktop",
            "widgets": [{"id": "desktop-widgets", "type": "desktop.widgets"}],
        }
        if with_page_schema
        else {},
        icon_order=["scenario:prompt_engineer_scenario"],
        widget_order=["weather", "infra-status"],
        hidden_sections=["node:member-01"],
    )


def test_desktop_status_card_publishes_browser_shell_projection() -> None:
    card = publish_desktop_status_card(
        webspace_id="desktop",
        snapshot=_desktop_snapshot(),
        updated_at=10.0,
    )

    assert card.id == "desktop-shell"
    assert card.owner == "core:desktop"
    assert card.kind == "browser-shell"
    assert card.status == "online"
    assert card.scope["app_total"] == 1
    assert card.scope["widget_total"] == 2
    assert card.scope["pinned_widget_total"] == 1
    assert card.scope["page_schema_id"] == "desktop"

    record = status_card_projection_record(
        card_id="desktop-shell",
        webspace_id="desktop",
        now=11.0,
    )
    assert record is not None
    assert record.meta.projection_key == "status-card:desktop-shell"
    assert record.data["details_ref"]["path"] == "/api/node/yjs/webspaces/desktop/desktop"


def test_desktop_status_card_marks_missing_page_schema_as_warning() -> None:
    card = publish_desktop_status_card(
        webspace_id="desktop",
        snapshot=_desktop_snapshot(with_page_schema=False),
        updated_at=10.0,
    )

    assert card.status == "warning"
    assert card.severity == "medium"
    assert card.scope["has_page_schema"] is False
    assert "page schema missing" in card.summary


def test_status_card_api_refreshes_desktop_shell_card(monkeypatch) -> None:
    client = _make_client()

    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return _desktop_snapshot()

    from adaos.apps.api import node_api

    monkeypatch.setattr(node_api, "WebDesktopService", _DesktopService)

    resp = client.post(
        "/api/node/status-cards/desktop/refresh",
        params={"webspace_id": "desktop"},
    )
    snapshot_resp = client.get(
        "/api/node/status-cards",
        params={"webspace_id": "desktop", "include_runtime": False},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["card"]["id"] == "desktop-shell"
    assert payload["card"]["scope"]["widget_total"] == 2
    assert payload["snapshot"]["card_total"] == 1

    assert snapshot_resp.status_code == 200
    assert snapshot_resp.json()["cards"][0]["id"] == "desktop-shell"


def test_status_card_snapshot_can_include_desktop_shell(monkeypatch) -> None:
    client = _make_client()

    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return _desktop_snapshot()

    from adaos.apps.api import node_api

    monkeypatch.setattr(node_api, "WebDesktopService", _DesktopService)

    resp = client.get(
        "/api/node/status-cards",
        params={
            "webspace_id": "desktop",
            "include_runtime": False,
            "include_desktop": True,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["card_total"] == 1
    assert payload["cards"][0]["id"] == "desktop-shell"
    assert payload["refreshes"]["desktop"]["id"] == "desktop-shell"
    assert get_status_card(card_id="desktop-shell", webspace_id="desktop") is not None


def test_desktop_state_endpoint_returns_desktop_shell_status_card(monkeypatch) -> None:
    class _DesktopService:
        async def get_snapshot_async(self, webspace_id: str | None = None):
            assert webspace_id == "desktop"
            return _desktop_snapshot()

    from adaos.apps.api import node_api

    monkeypatch.setattr(node_api, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(
        node_api,
        "yjs_sync_runtime_snapshot",
        lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")},
    )

    result = asyncio.run(node_api.node_yjs_desktop_state("default"))

    assert result["ok"] is True
    assert result["webspace_id"] == "desktop"
    assert result["status_card"]["id"] == "desktop-shell"
    assert result["status_card"]["scope"]["app_total"] == 1
    assert result["runtime"]["webspace_id"] == "desktop"
