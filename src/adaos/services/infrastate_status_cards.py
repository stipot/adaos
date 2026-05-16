from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from adaos.domain import StatusCard
from adaos.sdk import status as status_sdk


INFRASTATE_STATUS_OWNER = "skill:infrastate_skill"


@dataclass(frozen=True, slots=True)
class InfrastateStatusCardSpec:
    id: str
    kind: str
    status: str
    summary: str
    scope: Mapping[str, Any]
    ttl_ms: int = 30000
    details_ref: Mapping[str, Any] | None = None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


def _status_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"ready", "idle", "running", "nominal", "ok", "stable", "succeeded", "success", "healthy"}:
        return "running"
    if token in {
        "degraded",
        "partial",
        "warning",
        "warn",
        "pending",
        "planned",
        "countdown",
        "restarting",
        "stale",
        "aging",
        "throttle",
        "throttled",
        "draining",
    }:
        return "warning"
    if token in {"failed", "failure", "error", "offline", "down", "critical", "blocked", "broken"}:
        return "failed"
    return "unknown"


def _summary_text(*parts: Any, fallback: str) -> str:
    text = " | ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or fallback


def _snapshot_summary_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrastateStatusCardSpec:
    summary = _mapping(snapshot.get("summary"))
    value = str(summary.get("value") or summary.get("status") or "unknown").strip() or "unknown"
    label = str(summary.get("label") or "Infra State").strip() or "Infra State"
    description = str(summary.get("description") or summary.get("subtitle") or "").strip()
    return InfrastateStatusCardSpec(
        id="infrastate-summary",
        kind="infrastate",
        status=_status_token(value),
        summary=_summary_text(label, value, description, fallback="Infra State"),
        scope={"webspace_id": webspace_id, "section": "summary"},
        details_ref={"kind": "api", "path": "/api/node/infrastate/snapshot", "params": {"webspace_id": webspace_id}},
    )


def _operations_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrastateStatusCardSpec:
    operations = _mapping(snapshot.get("operations"))
    active = _items(operations.get("active_items") or operations.get("active") or operations.get("items"))
    total = len(active)
    return InfrastateStatusCardSpec(
        id="infrastate-operations",
        kind="operations",
        status="running" if total else "ready",
        summary=f"{total} active operation{'s' if total != 1 else ''}",
        scope={"webspace_id": webspace_id, "section": "operations", "active_total": total},
        details_ref={"kind": "stream", "receiver": "infrastate.operations.active", "params": {"webspace_id": webspace_id}},
    )


def _status_from_rows(rows: list[Any]) -> str:
    statuses = [str(_mapping(row).get("status") or _mapping(row).get("state") or "").strip().lower() for row in rows]
    if any(_status_token(item) == "failed" for item in statuses):
        return "failed"
    if any(_status_token(item) == "warning" for item in statuses):
        return "warning"
    if statuses:
        return "running"
    return "unknown"


def _realtime_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrastateStatusCardSpec:
    rows = _items(snapshot.get("realtime"))
    state = _status_from_rows(rows)
    return InfrastateStatusCardSpec(
        id="infrastate-realtime",
        kind="realtime",
        status=state,
        summary=f"Realtime {state}" if rows else "Realtime status unknown",
        scope={"webspace_id": webspace_id, "section": "realtime", "item_total": len(rows)},
        details_ref={"kind": "stream", "receiver": "infrastate.realtime", "params": {"webspace_id": webspace_id}},
    )


def _runtime_payload(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    reliability = _mapping(snapshot.get("reliability"))
    runtime = _mapping(reliability.get("runtime"))
    return runtime or _mapping(snapshot.get("yjs_runtime"))


def _yjs_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrastateStatusCardSpec:
    runtime = _runtime_payload(snapshot)
    state_sync = _mapping(runtime.get("state_sync"))
    yjs_pressure = _mapping(runtime.get("yjs_pressure"))
    semantic_state = str(state_sync.get("semantic_state") or state_sync.get("freshness_state") or "").strip()
    policy_state = str(yjs_pressure.get("policy_state") or yjs_pressure.get("observed_state") or "").strip()
    state = "running"
    for candidate in (policy_state, semantic_state):
        mapped = _status_token(candidate)
        if mapped == "failed":
            state = "failed"
            break
        if mapped == "warning":
            state = "warning"
    return InfrastateStatusCardSpec(
        id="infrastate-yjs",
        kind="yjs",
        status=state,
        summary=_summary_text(
            f"state_sync={semantic_state}" if semantic_state else "",
            f"pressure={policy_state}" if policy_state else "",
            fallback="Yjs state nominal",
        ),
        scope={
            "webspace_id": webspace_id,
            "section": "yjs",
            "semantic_state": semantic_state or None,
            "policy_state": policy_state or None,
        },
        details_ref={"kind": "stream", "receiver": "infrastate.yjs.load_mark", "params": {"webspace_id": webspace_id}},
    )


def _core_update_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrastateStatusCardSpec:
    summary = _mapping(snapshot.get("summary"))
    core_update = _mapping(snapshot.get("core_update") or snapshot.get("update_status"))
    value = str(core_update.get("state") or summary.get("value") or "unknown").strip() or "unknown"
    return InfrastateStatusCardSpec(
        id="infrastate-core-update",
        kind="core-update",
        status=_status_token(value),
        summary=_summary_text("Core update", value, core_update.get("phase"), fallback="Core update status unknown"),
        scope={"webspace_id": webspace_id, "section": "core-update", "state": value},
        details_ref={
            "kind": "stream",
            "receiver": "infrastate.core_update_diagnostics",
            "params": {"webspace_id": webspace_id},
        },
    )


def build_infrastate_status_card_specs(
    snapshot: Mapping[str, Any],
    *,
    webspace_id: str,
) -> list[InfrastateStatusCardSpec]:
    target_webspace = str(webspace_id or "").strip()
    if not target_webspace:
        raise ValueError("webspace_id is required")
    data = _mapping(snapshot)
    return [
        _snapshot_summary_spec(data, webspace_id=target_webspace),
        _operations_spec(data, webspace_id=target_webspace),
        _realtime_spec(data, webspace_id=target_webspace),
        _yjs_spec(data, webspace_id=target_webspace),
        _core_update_spec(data, webspace_id=target_webspace),
    ]


def publish_infrastate_status_cards(
    snapshot: Mapping[str, Any],
    *,
    webspace_id: str,
    owner: str = INFRASTATE_STATUS_OWNER,
    updated_at: float | None = None,
) -> list[StatusCard]:
    cards: list[StatusCard] = []
    for spec in build_infrastate_status_card_specs(snapshot, webspace_id=webspace_id):
        details = dict(spec.details_ref or {})
        if details.get("kind") == "stream":
            cards.append(
                status_sdk.publish_status_stream(
                    id=spec.id,
                    owner=owner,
                    kind=spec.kind,
                    webspace_id=webspace_id,
                    status=spec.status,
                    summary=spec.summary,
                    scope=_compact(spec.scope),
                    receiver=str(details.get("receiver") or ""),
                    path=details.get("path"),
                    tool=details.get("tool"),
                    params=_mapping(details.get("params")) or None,
                    ttl_ms=spec.ttl_ms,
                    updated_at=updated_at,
                )
            )
        else:
            cards.append(
                status_sdk.publish_status(
                    id=spec.id,
                    owner=owner,
                    kind=spec.kind,
                    webspace_id=webspace_id,
                    status=spec.status,
                    summary=spec.summary,
                    scope=_compact(spec.scope),
                    details_ref=details or None,
                    ttl_ms=spec.ttl_ms,
                    updated_at=updated_at,
                )
            )
    return cards


__all__ = [
    "INFRASTATE_STATUS_OWNER",
    "InfrastateStatusCardSpec",
    "build_infrastate_status_card_specs",
    "publish_infrastate_status_cards",
]
