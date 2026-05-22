from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from adaos.adapters.fs.path_provider import PathProvider
from adaos.adapters.sdk.inproc_skill_context import InprocSkillContext
from adaos.services.agent_context import clear_ctx, set_ctx
from adaos.services.desktop_status_cards import publish_desktop_status_card
from adaos.services.io_web.desktop import WebDesktopInstalled, WebDesktopSnapshot
from adaos.services.io_web.toast import WebToast, publish_notification_status_card
from adaos.services.runtime_status_cards import publish_runtime_status_card
from adaos.services.status_card_registry import clear_status_card_registry, status_card_projection_record
from adaos.services.ui_runtime_diagnostics import ingest_ui_runtime_diagnostics


def setup_function() -> None:
    clear_ctx()
    clear_status_card_registry()


def teardown_function() -> None:
    clear_ctx()
    clear_status_card_registry()


def test_platform_emitters_publish_shared_status_card_projection_records(tmp_path: Path) -> None:
    paths = PathProvider(tmp_path)
    paths.ensure_tree()
    set_ctx(SimpleNamespace(paths=paths, skill_ctx=InprocSkillContext()))

    publish_runtime_status_card(
        webspace_id="desktop",
        node_id="node-a",
        lifecycle={"node_state": "ready", "draining": False, "accepting_new_work": True},
        updated_at=10.0,
    )
    publish_desktop_status_card(
        webspace_id="desktop",
        snapshot=WebDesktopSnapshot(
            installed=WebDesktopInstalled(apps=["scenario:web_desktop"], widgets=["weather"]),
            pinned_widgets=[],
            topbar=[],
            page_schema={"id": "desktop", "widgets": []},
        ),
        updated_at=10.0,
    )
    publish_notification_status_card(
        toast=WebToast(level="success", message="Operation completed", code="operation.completed"),
        recent_toasts=[
            {"level": "success", "message": "Operation completed", "code": "operation.completed"},
        ],
        webspace_id="desktop",
        max_items=5,
        updated_at=10.0,
    )
    asyncio.run(
        ingest_ui_runtime_diagnostics(
            {
                "webspace_id": "desktop",
                "events": [
                    {
                        "level": "warning",
                        "source": "ui.widget",
                        "code": "widget.warn",
                        "message": "Widget warning.",
                        "skillId": "weather_skill",
                    }
                ],
            }
        )
    )

    records = {
        card_id: status_card_projection_record(card_id=card_id, webspace_id="desktop", now=11.0)
        for card_id in ["runtime", "desktop-shell", "notifications", "ui-runtime"]
    }

    assert all(record is not None for record in records.values())
    for card_id, record in records.items():
        assert record is not None
        assert record.meta.projection_key == f"status-card:{card_id}"
        assert record.meta.kind == "status-card"
        assert record.meta.webspace_id == "desktop"
        assert record.meta.source_authority == "platform-status-registry"
        assert record.meta.lifecycle_reason == "materialized"
        assert record.meta.access["audience"] == "shared"
        assert record.meta.access["read_only"] is False
        assert record.data["owner"].startswith("core:")
        assert record.data["details_ref"]["kind"] == "api"

    assert records["runtime"].data["kind"] == "runtime"
    assert records["desktop-shell"].data["kind"] == "browser-shell"
    assert records["notifications"].data["kind"] == "notifications"
    assert records["ui-runtime"].data["kind"] == "ui-runtime-diagnostics"
