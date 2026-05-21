from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Mapping, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.status_card_registry import publish_status_card
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.io_web.toast")
NOTIFICATIONS_STATUS_CARD_ID = "notifications"
NOTIFICATIONS_STATUS_CARD_OWNER = "core:notifications"


@dataclass(slots=True)
class WebToast:
    level: str
    message: str
    code: Optional[str] = None
    source: Optional[str] = None
    ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "code": self.code,
            "source": self.source,
            "ts": self.ts or datetime.now(timezone.utc).isoformat(),
        }


class WebToastService:
    """
    Core helper for pushing transient toast notifications into Yjs.

    Toasts are stored under ``data/desktop/toasts`` as a bounded list
    so that multiple browsers attached to the same webspace can render
    them independently while keeping hub-side logic minimal.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    async def push(
        self,
        message: str,
        *,
        level: str = "info",
        code: Optional[str] = None,
        source: Optional[str] = None,
        webspace_id: Optional[str] = None,
        max_items: int = 20,
    ) -> None:
        webspace = (webspace_id or "").strip() or default_webspace_id()
        toast = WebToast(
            level=level,
            message=message,
            code=code,
            source=source,
            ts=datetime.now(timezone.utc).isoformat(),
        )

        async with ystore_write_metadata(
            root_names=["data"],
            source="io_web.toast",
            owner="core:toast",
            channel="core.toast.async",
        ):
            async with async_get_ydoc(webspace) as ydoc:
                data_map = ydoc.get_map("data")
                with ydoc.begin_transaction() as txn:
                    desktop = data_map.get("desktop")
                    if not isinstance(desktop, dict):
                        desktop = {}
                    raw_toasts = desktop.get("toasts") or []
                    items: List[Dict[str, Any]] = []
                    if isinstance(raw_toasts, list):
                        items = [it for it in raw_toasts if isinstance(it, dict)]
                    items.append(toast.to_dict())
                    # Keep only the last max_items entries.
                    if max_items > 0 and len(items) > max_items:
                        items = items[-max_items:]
                    desktop["toasts"] = json.loads(json.dumps(items))
                    data_map.set(txn, "desktop", json.loads(json.dumps(desktop)))

        publish_notification_status_card(
            toast=toast,
            recent_toasts=items,
            webspace_id=webspace,
            max_items=max_items,
            updated_at=time.time(),
        )
        _log.debug("toast pushed webspace=%s level=%s code=%s", webspace, level, code)


def publish_notification_status_card(
    *,
    toast: WebToast,
    recent_toasts: List[Mapping[str, Any]],
    webspace_id: str,
    max_items: int,
    updated_at: float | None = None,
):
    items = [dict(item) for item in recent_toasts if isinstance(item, Mapping)]
    level_counts = _level_counts(items)
    return publish_status_card(
        id=NOTIFICATIONS_STATUS_CARD_ID,
        owner=NOTIFICATIONS_STATUS_CARD_OWNER,
        kind="notifications",
        scope={
            "recent_total": len(items),
            "max_items": max(0, int(max_items)),
            "level_counts": level_counts,
            "last": toast.to_dict(),
        },
        webspace_id=webspace_id,
        status=_notification_status(toast.level),
        summary=_notification_summary(toast),
        ttl_ms=60000,
        details_ref={
            "kind": "api",
            "path": "/api/node/status-cards",
            "params": {"webspace_id": webspace_id},
        },
        updated_at=updated_at if updated_at is not None else time.time(),
    )


def _notification_status(level: Any) -> str:
    token = str(level or "").strip().lower()
    if token in {"error", "danger", "fatal", "critical"}:
        return "degraded"
    if token in {"warn", "warning"}:
        return "warning"
    return "online"


def _notification_summary(toast: WebToast) -> str:
    level = str(toast.level or "info").strip().lower() or "info"
    message = str(toast.message or "").strip()
    if len(message) > 96:
        message = f"{message[:96]}..."
    return f"Notification {level}: {message}" if message else f"Notification {level}"


def _level_counts(items: List[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        token = str(item.get("level") or "info").strip().lower() or "info"
        counts[token] = counts.get(token, 0) + 1
    return dict(sorted(counts.items()))
