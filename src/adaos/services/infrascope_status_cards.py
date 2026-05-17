from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from adaos.domain import StatusCard
from adaos.sdk import status as status_sdk


INFRASCOPE_STATUS_OWNER = "skill:infrascope_skill"


@dataclass(frozen=True, slots=True)
class InfrascopeStatusCardSpec:
    id: str
    kind: str
    status: str
    summary: str
    scope: Mapping[str, Any]
    ttl_ms: int = 30000
    severity: str | None = None
    details_ref: Mapping[str, Any] | None = None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


def _status_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"online", "ready", "healthy", "active", "ok", "running", "nominal", "stable"}:
        return "online"
    if token in {"offline", "down", "failed", "failure", "broken", "error", "critical"}:
        return "offline"
    if token in {"degraded", "unstable", "limited", "partial", "high"}:
        return "degraded"
    if token in {"warning", "warn", "pending", "planned", "queued", "stale", "medium"}:
        return "warning"
    return "unknown"


def _summary_text(*parts: Any, fallback: str) -> str:
    text = " | ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or fallback


def _worst_status(rows: list[Any], *, empty: str = "unknown") -> str:
    statuses = [
        _status_token(
            _mapping(row).get("status")
            or _mapping(row).get("state")
            or _mapping(row).get("severity")
        )
        for row in rows
    ]
    if any(item == "offline" for item in statuses):
        return "offline"
    if any(item == "degraded" for item in statuses):
        return "degraded"
    if any(item == "warning" for item in statuses):
        return "warning"
    if any(item == "online" for item in statuses):
        return "online"
    return empty


def _inventory_counts(inventory: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, value in inventory.items():
        rows = _items(value)
        if rows:
            counts[str(key)] = len(rows)
    return counts


def _overview_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    summary = _mapping(snapshot.get("summary"))
    value = str(summary.get("value") or summary.get("status") or "unknown").strip() or "unknown"
    label = str(summary.get("label") or "Infrascope").strip() or "Infrascope"
    subtitle = str(summary.get("subtitle") or summary.get("description") or "").strip()
    return InfrascopeStatusCardSpec(
        id="infrascope-overview",
        kind="infrascope",
        status=_status_token(value),
        summary=_summary_text(label, value, subtitle, fallback="Infrascope overview"),
        scope={"webspace_id": webspace_id, "section": "overview", "state": value},
        details_ref={
            "kind": "tool",
            "receiver": "infrascope_skill",
            "tool": "get_snapshot",
            "params": {"webspace_id": webspace_id},
        },
    )


def _incidents_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    overview = _mapping(snapshot.get("overview"))
    incidents = _items(overview.get("active_incidents"))
    total = len(incidents)
    status = _worst_status(incidents, empty="online") if total else "online"
    return InfrascopeStatusCardSpec(
        id="infrascope-incidents",
        kind="incidents",
        status=status,
        summary=f"{total} active incident{'s' if total != 1 else ''}",
        scope={"webspace_id": webspace_id, "section": "active_incidents", "active_total": total},
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.overview.active_incidents",
            "params": {"webspace_id": webspace_id},
        },
    )


def _inventory_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    inventory = _mapping(snapshot.get("inventory"))
    all_rows = _items(inventory.get("all"))
    counts = _inventory_counts(inventory)
    total = len(all_rows) if all_rows else sum(counts.values())
    status = _worst_status(all_rows, empty="unknown") if all_rows else ("online" if total else "unknown")
    return InfrascopeStatusCardSpec(
        id="infrascope-inventory",
        kind="inventory",
        status=status,
        summary=f"{total} inventory object{'s' if total != 1 else ''}",
        scope={"webspace_id": webspace_id, "section": "inventory", "object_total": total, "counts": counts},
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.inventory.all",
            "params": {"webspace_id": webspace_id},
        },
    )


def _operations_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    operations = _mapping(snapshot.get("operations"))
    rows = _items(operations.get("items") or operations.get("active_items") or operations.get("active"))
    total = len(rows)
    status = _worst_status(rows, empty="online") if total else "online"
    return InfrascopeStatusCardSpec(
        id="infrascope-operations",
        kind="operations",
        status=status,
        summary=f"{total} active operation{'s' if total != 1 else ''}",
        scope={"webspace_id": webspace_id, "section": "operations", "active_total": total},
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.operations.active",
            "params": {"webspace_id": webspace_id},
        },
    )


def _browser_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    inventory = _mapping(snapshot.get("inventory"))
    rows = _items(inventory.get("browsers"))
    total = len(rows)
    status = _worst_status(rows, empty="unknown") if total else "unknown"
    return InfrascopeStatusCardSpec(
        id="infrascope-browsers",
        kind="browser-runtime",
        status=status,
        summary=f"{total} browser session{'s' if total != 1 else ''}",
        scope={"webspace_id": webspace_id, "section": "browsers", "browser_total": total},
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.inventory.browsers",
            "params": {"webspace_id": webspace_id},
        },
    )


def _runtime_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    inventory = _mapping(snapshot.get("inventory"))
    overview = _mapping(snapshot.get("overview"))
    rows = _items(inventory.get("runtimes"))
    active_rows = _items(overview.get("active_runtimes"))
    total = len(rows)
    status = _worst_status(rows + active_rows, empty="unknown") if rows or active_rows else "unknown"
    return InfrascopeStatusCardSpec(
        id="infrascope-runtimes",
        kind="runtime",
        status=status,
        summary=f"{total} runtime object{'s' if total != 1 else ''}",
        scope={
            "webspace_id": webspace_id,
            "section": "runtimes",
            "runtime_total": total,
            "active_runtime_total": len(active_rows),
        },
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.inventory.runtimes",
            "params": {"webspace_id": webspace_id},
        },
    )


def _registry_spec(snapshot: Mapping[str, Any], *, webspace_id: str) -> InfrascopeStatusCardSpec:
    inventory = _mapping(snapshot.get("inventory"))
    skill_rows = _items(inventory.get("skills"))
    scenario_rows = _items(inventory.get("scenarios"))
    total = len(skill_rows) + len(scenario_rows)
    status = _worst_status(skill_rows + scenario_rows, empty="unknown") if total else "unknown"
    return InfrascopeStatusCardSpec(
        id="infrascope-registry",
        kind="registry",
        status=status,
        summary=f"{len(skill_rows)} skill{'s' if len(skill_rows) != 1 else ''} | {len(scenario_rows)} scenario{'s' if len(scenario_rows) != 1 else ''}",
        scope={
            "webspace_id": webspace_id,
            "section": "registry",
            "skill_total": len(skill_rows),
            "scenario_total": len(scenario_rows),
        },
        details_ref={
            "kind": "stream",
            "receiver": "infrascope.inventory.skills",
            "params": {
                "webspace_id": webspace_id,
                "related_receivers": ["infrascope.inventory.scenarios"],
            },
        },
    )


def build_infrascope_status_card_specs(
    snapshot: Mapping[str, Any],
    *,
    webspace_id: str,
) -> list[InfrascopeStatusCardSpec]:
    target_webspace = str(webspace_id or "").strip()
    if not target_webspace:
        raise ValueError("webspace_id is required")
    data = _mapping(snapshot)
    return [
        _overview_spec(data, webspace_id=target_webspace),
        _incidents_spec(data, webspace_id=target_webspace),
        _inventory_spec(data, webspace_id=target_webspace),
        _operations_spec(data, webspace_id=target_webspace),
        _browser_spec(data, webspace_id=target_webspace),
        _runtime_spec(data, webspace_id=target_webspace),
        _registry_spec(data, webspace_id=target_webspace),
    ]


def publish_infrascope_status_cards(
    snapshot: Mapping[str, Any],
    *,
    webspace_id: str,
    owner: str = INFRASCOPE_STATUS_OWNER,
    updated_at: float | None = None,
) -> list[StatusCard]:
    cards: list[StatusCard] = []
    for spec in build_infrascope_status_card_specs(snapshot, webspace_id=webspace_id):
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
                    severity=spec.severity,
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
                    severity=spec.severity,
                    details_ref=details or None,
                    ttl_ms=spec.ttl_ms,
                    updated_at=updated_at,
                )
            )
    return cards


__all__ = [
    "INFRASCOPE_STATUS_OWNER",
    "InfrascopeStatusCardSpec",
    "build_infrascope_status_card_specs",
    "publish_infrascope_status_cards",
]
