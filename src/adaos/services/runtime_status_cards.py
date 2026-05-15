from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.domain import StatusCard
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.status_card_registry import publish_status_card


RUNTIME_STATUS_CARD_ID = "runtime"
RUNTIME_STATUS_CARD_OWNER = "core:runtime"


def _runtime_status_from_lifecycle(snapshot: Mapping[str, Any]) -> str:
    node_state = str(snapshot.get("node_state") or "").strip().lower()
    if node_state == "ready":
        return "running"
    if node_state == "draining":
        return "warning"
    if node_state in {"failed", "error", "offline", "down"}:
        return "failed"
    return "unknown"


def _runtime_summary(snapshot: Mapping[str, Any]) -> str:
    node_state = str(snapshot.get("node_state") or "unknown").strip() or "unknown"
    reason = str(snapshot.get("reason") or "").strip()
    if reason:
        return f"Runtime {node_state}: {reason}"
    if bool(snapshot.get("accepting_new_work")):
        return "Runtime ready"
    return f"Runtime {node_state}"


def publish_runtime_status_card(
    *,
    webspace_id: str,
    node_id: str | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    updated_at: float | None = None,
) -> StatusCard:
    snapshot = dict(lifecycle or runtime_lifecycle_snapshot())
    node_token = str(node_id or "").strip() or None
    return publish_status_card(
        id=RUNTIME_STATUS_CARD_ID,
        owner=RUNTIME_STATUS_CARD_OWNER,
        kind="runtime",
        scope={
            "node_id": node_token,
            "node_state": str(snapshot.get("node_state") or "unknown").strip() or "unknown",
            "draining": bool(snapshot.get("draining")),
            "accepting_new_work": bool(snapshot.get("accepting_new_work")),
        },
        webspace_id=webspace_id,
        status=_runtime_status_from_lifecycle(snapshot),
        summary=_runtime_summary(snapshot),
        ttl_ms=15000,
        details_ref={
            "kind": "api",
            "path": "/api/node/status",
        },
        updated_at=updated_at if updated_at is not None else time.time(),
    )


__all__ = [
    "RUNTIME_STATUS_CARD_ID",
    "RUNTIME_STATUS_CARD_OWNER",
    "publish_runtime_status_card",
]
