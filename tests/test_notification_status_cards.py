from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import adaos.services.io_web.toast as toast_module
from adaos.services.io_web.toast import WebToastService
from adaos.services.status_card_registry import (
    clear_status_card_registry,
    get_status_card,
    status_card_projection_record,
)


class _FakeMap(dict):
    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, default)

    def set(self, txn, key, value):
        self[key] = value


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeYDoc:
    def __init__(self):
        self._maps = {"data": _FakeMap()}

    def get_map(self, name: str):
        return self._maps.setdefault(name, _FakeMap())

    def begin_transaction(self):
        return _FakeTxn()


def setup_function() -> None:
    clear_status_card_registry()


def teardown_function() -> None:
    clear_status_card_registry()


def test_web_toast_publishes_notifications_status_card(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(toast_module, "async_get_ydoc", _async_get_ydoc)

    asyncio.run(
        WebToastService(SimpleNamespace()).push(
            "Scenario welcome completed",
            level="success",
            code="operation.completed",
            source="operations",
            webspace_id="desktop",
            max_items=5,
        )
    )

    desktop = docs["desktop"].get_map("data")["desktop"]
    assert desktop["toasts"][0]["message"] == "Scenario welcome completed"

    card = get_status_card(card_id="notifications", webspace_id="desktop")
    assert card is not None
    assert card.owner == "core:notifications"
    assert card.kind == "notifications"
    assert card.status == "online"
    assert card.scope["recent_total"] == 1
    assert card.scope["level_counts"] == {"success": 1}
    assert card.scope["last"]["code"] == "operation.completed"

    record = status_card_projection_record(
        card_id="notifications",
        webspace_id="desktop",
        now=card.updated_at,
    )
    assert record is not None
    assert record.meta.projection_key == "status-card:notifications"
    assert record.data["details_ref"]["path"] == "/api/node/status-cards"


def test_web_toast_notifications_status_card_tracks_error_state(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(toast_module, "async_get_ydoc", _async_get_ydoc)
    service = WebToastService(SimpleNamespace())

    asyncio.run(
        service.push(
            "First warning",
            level="warning",
            code="first.warning",
            webspace_id="desktop",
            max_items=5,
        )
    )
    first = get_status_card(card_id="notifications", webspace_id="desktop")
    assert first is not None
    assert first.status == "warning"

    asyncio.run(
        service.push(
            "Widget failed",
            level="error",
            code="widget.failed",
            webspace_id="desktop",
            max_items=5,
        )
    )

    card = get_status_card(card_id="notifications", webspace_id="desktop")
    assert card is not None
    assert card.status == "degraded"
    assert card.version == 2
    assert card.scope["recent_total"] == 2
    assert card.scope["level_counts"] == {"error": 1, "warning": 1}
    assert card.scope["last"]["message"] == "Widget failed"
