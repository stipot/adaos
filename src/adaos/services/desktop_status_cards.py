from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.services.io_web.desktop import WebDesktopSnapshot
from adaos.services.status_card_registry import publish_status_card


DESKTOP_STATUS_CARD_ID = "desktop-shell"
DESKTOP_STATUS_CARD_OWNER = "core:desktop"


def publish_desktop_status_card(
    *,
    webspace_id: str,
    snapshot: WebDesktopSnapshot | Mapping[str, Any],
    updated_at: float | None = None,
):
    to_dict = getattr(snapshot, "to_dict", None)
    if isinstance(snapshot, WebDesktopSnapshot) or callable(to_dict):
        data = to_dict()
    else:
        data = dict(snapshot or {})
    installed = _mapping(data.get("installed"))
    apps = _list(installed.get("apps"))
    widgets = _list(installed.get("widgets"))
    pinned_widgets = _list(data.get("pinnedWidgets"))
    topbar = _list(data.get("topbar"))
    page_schema = _mapping(data.get("pageSchema"))
    page_widgets = _list(page_schema.get("widgets"))
    icon_order = _list(data.get("iconOrder"))
    widget_order = _list(data.get("widgetOrder"))
    hidden_sections = _list(data.get("hiddenSections"))
    page_schema_id = str(page_schema.get("id") or "").strip() or None
    has_page_schema = bool(page_schema)

    return publish_status_card(
        id=DESKTOP_STATUS_CARD_ID,
        owner=DESKTOP_STATUS_CARD_OWNER,
        kind="browser-shell",
        scope={
            "app_total": len(apps),
            "widget_total": len(widgets),
            "pinned_widget_total": len(pinned_widgets),
            "topbar_total": len(topbar),
            "page_widget_total": len(page_widgets),
            "icon_order_total": len(icon_order),
            "widget_order_total": len(widget_order),
            "hidden_section_total": len(hidden_sections),
            "page_schema_id": page_schema_id,
            "has_page_schema": has_page_schema,
        },
        webspace_id=webspace_id,
        status="online" if has_page_schema else "warning",
        summary=_desktop_summary(
            app_total=len(apps),
            widget_total=len(widgets),
            pinned_total=len(pinned_widgets),
            has_page_schema=has_page_schema,
            page_schema_id=page_schema_id,
        ),
        ttl_ms=60000,
        details_ref={
            "kind": "api",
            "path": f"/api/node/yjs/webspaces/{webspace_id}/desktop",
        },
        updated_at=updated_at if updated_at is not None else time.time(),
    )


def _desktop_summary(
    *,
    app_total: int,
    widget_total: int,
    pinned_total: int,
    has_page_schema: bool,
    page_schema_id: str | None,
) -> str:
    page = page_schema_id or ("page schema ready" if has_page_schema else "page schema missing")
    return f"Desktop shell: {app_total} app(s), {widget_total} widget(s), {pinned_total} pinned, {page}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = [
    "DESKTOP_STATUS_CARD_ID",
    "DESKTOP_STATUS_CARD_OWNER",
    "publish_desktop_status_card",
]
