from __future__ import annotations

import time
from typing import Any

from adaos.services.desktop_status_cards import DESKTOP_STATUS_CARD_ID, DESKTOP_STATUS_CARD_OWNER
from adaos.services.io_web.toast import NOTIFICATIONS_STATUS_CARD_ID, NOTIFICATIONS_STATUS_CARD_OWNER
from adaos.services.runtime_status_cards import RUNTIME_STATUS_CARD_ID, RUNTIME_STATUS_CARD_OWNER
from adaos.services.ui_runtime_diagnostics import UI_RUNTIME_STATUS_CARD_ID, UI_RUNTIME_STATUS_CARD_OWNER


_STATUS_CARD_FAMILY = "status-card"


def _status_card_emitter(
    *,
    emitter_id: str,
    owner: str,
    kind: str,
    category: str,
    source: str,
    details_path: str,
) -> dict[str, Any]:
    projection_key = f"{_STATUS_CARD_FAMILY}:{emitter_id}"
    return {
        "id": emitter_id,
        "family": _STATUS_CARD_FAMILY,
        "projection_key": projection_key,
        "owner": owner,
        "kind": kind,
        "category": category,
        "source": source,
        "status_card_id": emitter_id,
        "details_ref": {
            "kind": "api",
            "path": details_path,
        },
        "contract": {
            "shape": "ProjectionRecord(status,data,meta,error)",
            "materialized_as": projection_key,
            "lifecycle": ["ready", "stale", "error"],
            "write_policy": "platform-emitter -> status-card registry -> ProjectionRecord",
            "browser_write": False,
            "skill_direct_write": False,
        },
    }


def platform_emitter_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the server-side platform emitter families covered by shared ABI."""

    emitters = [
        _status_card_emitter(
            emitter_id=RUNTIME_STATUS_CARD_ID,
            owner=RUNTIME_STATUS_CARD_OWNER,
            kind="runtime",
            category="runtime_lifecycle",
            source="runtime_lifecycle_snapshot",
            details_path="/api/node/status",
        ),
        _status_card_emitter(
            emitter_id=DESKTOP_STATUS_CARD_ID,
            owner=DESKTOP_STATUS_CARD_OWNER,
            kind="browser-shell",
            category="browser_runtime",
            source="web_desktop_snapshot",
            details_path="/api/node/status-cards",
        ),
        _status_card_emitter(
            emitter_id=NOTIFICATIONS_STATUS_CARD_ID,
            owner=NOTIFICATIONS_STATUS_CARD_OWNER,
            kind="notifications",
            category="notifications",
            source="io_web.toast",
            details_path="/api/node/status-cards",
        ),
        _status_card_emitter(
            emitter_id=UI_RUNTIME_STATUS_CARD_ID,
            owner=UI_RUNTIME_STATUS_CARD_OWNER,
            kind="ui-runtime-diagnostics",
            category="diagnostics",
            source="ui_runtime_diagnostics",
            details_path="/api/node/logs",
        ),
    ]
    categories = sorted({str(item["category"]) for item in emitters})
    projection_keys = [str(item["projection_key"]) for item in emitters]
    surface_readiness = {
        "web_desktop": {
            "status": "ready",
            "projection_key": f"{_STATUS_CARD_FAMILY}:{DESKTOP_STATUS_CARD_ID}",
            "evidence": ["desktop-shell status card", "web_desktop_snapshot"],
        },
        "notifications": {
            "status": "ready",
            "projection_key": f"{_STATUS_CARD_FAMILY}:{NOTIFICATIONS_STATUS_CARD_ID}",
            "evidence": ["notifications status card", "io_web.toast"],
        },
        "diagnostics": {
            "status": "ready",
            "projection_key": f"{_STATUS_CARD_FAMILY}:{UI_RUNTIME_STATUS_CARD_ID}",
            "evidence": ["ui-runtime diagnostics status card", "ui_runtime_diagnostics"],
        },
        "workspace_manager": {
            "status": "covered_by_desktop_shell",
            "projection_key": f"{_STATUS_CARD_FAMILY}:{DESKTOP_STATUS_CARD_ID}",
            "evidence": ["desktop shell surface", "shared status-card ABI"],
        },
        "related_modals": {
            "status": "contract_ready",
            "projection_key": f"{_STATUS_CARD_FAMILY}:{DESKTOP_STATUS_CARD_ID}",
            "evidence": ["browser surface lifecycle contract", "pinned/modal demand semantics"],
        },
    }
    return {
        "ok": True,
        "source": "adaos.platform_emitters",
        "contract": "adaos.platform-emitters.status-card.v1",
        "family": _STATUS_CARD_FAMILY,
        "emitter_total": len(emitters),
        "categories": categories,
        "projection_keys": projection_keys,
        "emitters": emitters,
        "coverage": {
            "runtime_lifecycle": "runtime_lifecycle" in categories,
            "browser_runtime": "browser_runtime" in categories,
            "notifications": "notifications" in categories,
            "diagnostics": "diagnostics" in categories,
            "system_errors": "diagnostics" in categories or "notifications" in categories,
        },
        "surface_readiness": surface_readiness,
        "surface_ready_total": sum(1 for item in surface_readiness.values() if item["status"] in {"ready", "covered_by_desktop_shell", "contract_ready"}),
        "pilot_order": [
            "runtime_lifecycle",
            "web_desktop",
            "notifications",
            "diagnostics",
            "workspace_manager",
            "related_modals",
            "heavy_skill_pilot",
        ],
        "ready_for_mvp": len(emitters) >= 4,
        "updated_at": float(now if now is not None else time.time()),
    }


__all__ = [
    "platform_emitter_contract_snapshot",
]
