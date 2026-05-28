from __future__ import annotations

from dataclasses import dataclass
import time
from threading import RLock
from typing import Any, Iterable, Mapping

from adaos.domain import (
    ClientSubscriptionRecord,
    ProjectionSubscription,
    make_client_subscription_record,
    normalize_client_subscription_record,
)


@dataclass(frozen=True, slots=True)
class ProjectionDemandConsumer:
    client_id: str
    device_id: str
    session_id: str
    webspace_id: str
    role: str
    projection_key: str
    consumer_id: str
    consumer_kind: str
    node_scope: Any = None
    pinned: bool = False
    visibility: str = "visible"
    params: Mapping[str, Any] | None = None
    updated_at: float = 0.0
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "webspace_id": self.webspace_id,
            "role": self.role,
            "projection_key": self.projection_key,
            "consumer_id": self.consumer_id,
            "consumer_kind": self.consumer_kind,
            "node_scope": self.node_scope,
            "pinned": self.pinned,
            "visibility": self.visibility,
            "params": dict(self.params) if isinstance(self.params, Mapping) else self.params,
            "updated_at": self.updated_at,
            "stale": self.stale,
        }


_LOCK = RLock()
_RECORDS: dict[tuple[str, str, str], ClientSubscriptionRecord] = {}


def _record_key(record: ClientSubscriptionRecord) -> tuple[str, str, str]:
    return (record.webspace_id, record.client_id, record.session_id)


def _coerce_record(value: Mapping[str, Any] | ClientSubscriptionRecord) -> ClientSubscriptionRecord:
    record = normalize_client_subscription_record(value)
    if not record.client_id.strip():
        raise ValueError("client_id is required")
    if not record.session_id.strip():
        raise ValueError("session_id is required")
    if not record.webspace_id.strip():
        raise ValueError("webspace_id is required")
    return record


def clear_projection_demand_registry() -> None:
    with _LOCK:
        _RECORDS.clear()


def write_client_subscription_record(
    record: Mapping[str, Any] | ClientSubscriptionRecord,
) -> ClientSubscriptionRecord:
    """Replace the full current subscription set for one browser client session."""

    normalized = _coerce_record(record)
    with _LOCK:
        _RECORDS[_record_key(normalized)] = normalized
    return normalized


def delete_client_subscription_record(
    *,
    client_id: str,
    session_id: str,
    webspace_id: str,
) -> bool:
    key = (str(webspace_id), str(client_id), str(session_id))
    with _LOCK:
        return _RECORDS.pop(key, None) is not None


def touch_client_subscription_record(
    *,
    client_id: str,
    session_id: str,
    webspace_id: str,
    device_id: str | None = None,
    role: str | None = None,
    updated_at: float | None = None,
) -> ClientSubscriptionRecord | None:
    key = (str(webspace_id), str(client_id), str(session_id))
    with _LOCK:
        current = _RECORDS.get(key)
        if current is None:
            return None
        next_record = make_client_subscription_record(
            client_id=current.client_id,
            device_id=current.device_id if device_id is None else str(device_id),
            session_id=current.session_id,
            webspace_id=current.webspace_id,
            role=current.role if role is None else str(role),
            subscriptions=current.subscriptions,
            updated_at=updated_at,
        )
        _RECORDS[key] = next_record
        return next_record


def list_client_subscription_records(*, webspace_id: str | None = None) -> list[ClientSubscriptionRecord]:
    token = str(webspace_id or "").strip()
    with _LOCK:
        records = list(_RECORDS.values())
    if token:
        records = [record for record in records if record.webspace_id == token]
    return sorted(records, key=lambda item: (item.webspace_id, item.client_id, item.session_id))


def _is_stale(record: ClientSubscriptionRecord, *, now: float, stale_after_s: float | None) -> bool:
    if stale_after_s is None:
        return False
    try:
        age_s = max(0.0, float(now) - float(record.updated_at))
    except Exception:
        return False
    return age_s > max(0.0, float(stale_after_s))


def _consumer_from_subscription(
    record: ClientSubscriptionRecord,
    subscription: ProjectionSubscription,
    *,
    stale: bool,
) -> ProjectionDemandConsumer:
    return ProjectionDemandConsumer(
        client_id=record.client_id,
        device_id=record.device_id,
        session_id=record.session_id,
        webspace_id=record.webspace_id,
        role=record.role,
        projection_key=subscription.projection_key,
        consumer_id=subscription.consumer_id,
        consumer_kind=subscription.consumer_kind,
        node_scope=subscription.node_scope,
        pinned=subscription.pinned,
        visibility=subscription.visibility,
        params=subscription.params,
        updated_at=record.updated_at,
        stale=stale,
    )


def projection_demand_consumers(
    *,
    webspace_id: str | None = None,
    projection_key: str | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> list[ProjectionDemandConsumer]:
    ts = float(now if now is not None else time.time())
    projection_token = str(projection_key or "").strip()
    consumers: list[ProjectionDemandConsumer] = []
    for record in list_client_subscription_records(webspace_id=webspace_id):
        stale = _is_stale(record, now=ts, stale_after_s=stale_after_s)
        if stale and not include_stale:
            continue
        for subscription in record.subscriptions:
            if projection_token and subscription.projection_key != projection_token:
                continue
            if not include_hidden and subscription.visibility == "hidden":
                continue
            consumers.append(_consumer_from_subscription(record, subscription, stale=stale))
    return sorted(
        consumers,
        key=lambda item: (
            item.webspace_id,
            item.projection_key,
            item.consumer_kind,
            item.consumer_id,
            item.client_id,
            item.session_id,
        ),
    )


def demanded_projection_keys(
    *,
    webspace_id: str | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> list[str]:
    keys = {
        item.projection_key
        for item in projection_demand_consumers(
            webspace_id=webspace_id,
            include_hidden=include_hidden,
            include_stale=include_stale,
            stale_after_s=stale_after_s,
            now=now,
        )
        if item.projection_key
    }
    return sorted(keys)


def _projection_summary(consumers: Iterable[ProjectionDemandConsumer]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ProjectionDemandConsumer]] = {}
    for consumer in consumers:
        grouped.setdefault(consumer.projection_key, []).append(consumer)
    return [
        {
            "projection_key": key,
            "consumer_total": len(items),
            "pinned_total": sum(1 for item in items if item.pinned),
            "visible_total": sum(1 for item in items if item.visibility != "hidden"),
            "stale_total": sum(1 for item in items if item.stale),
            "consumers": [item.to_dict() for item in items],
        }
        for key, items in sorted(grouped.items())
    ]


def projection_demand_snapshot(
    *,
    webspace_id: str | None = None,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ts = float(now if now is not None else time.time())
    records = list_client_subscription_records(webspace_id=webspace_id)
    consumers = projection_demand_consumers(
        webspace_id=webspace_id,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=ts,
    )
    stale_records = [record for record in records if _is_stale(record, now=ts, stale_after_s=stale_after_s)]
    return {
        "ok": True,
        "webspace_id": str(webspace_id or "").strip() or None,
        "client_total": len(records),
        "stale_client_total": len(stale_records),
        "projection_total": len({item.projection_key for item in consumers if item.projection_key}),
        "consumer_total": len(consumers),
        "records": [record.to_dict() for record in records],
        "projections": _projection_summary(consumers),
        "updated_at": ts,
    }


def make_empty_client_subscription_record(
    *,
    client_id: str,
    device_id: str,
    session_id: str,
    webspace_id: str,
    role: str,
    updated_at: float | None = None,
) -> ClientSubscriptionRecord:
    return make_client_subscription_record(
        client_id=client_id,
        device_id=device_id,
        session_id=session_id,
        webspace_id=webspace_id,
        role=role,
        subscriptions=[],
        updated_at=updated_at,
    )


__all__ = [
    "ProjectionDemandConsumer",
    "clear_projection_demand_registry",
    "delete_client_subscription_record",
    "demanded_projection_keys",
    "list_client_subscription_records",
    "make_empty_client_subscription_record",
    "projection_demand_consumers",
    "projection_demand_snapshot",
    "touch_client_subscription_record",
    "write_client_subscription_record",
]
