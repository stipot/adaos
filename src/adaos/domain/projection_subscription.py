from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Iterable, Mapping


CLIENT_SUBSCRIPTION_CONTRACT = "adaos.client-projection-subscription.v1"
CLIENT_SUBSCRIPTION_REQUIRED_FIELDS = (
    "client_id",
    "device_id",
    "session_id",
    "webspace_id",
    "role",
    "subscriptions",
    "updated_at",
)
PROJECTION_SUBSCRIPTION_REQUIRED_FIELDS = (
    "projection_key",
    "consumer_id",
    "consumer_kind",
)
PROJECTION_SUBSCRIPTION_OPTIONAL_FIELDS = (
    "node_scope",
    "pinned",
    "visibility",
    "params",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


@dataclass(frozen=True, slots=True)
class ProjectionSubscription:
    projection_key: str
    consumer_id: str
    consumer_kind: str
    node_scope: Any = None
    pinned: bool = False
    visibility: str = "visible"
    params: Mapping[str, Any] | None = None

    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.projection_key,
            self.consumer_id,
            self.consumer_kind,
            str(self.node_scope or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "projection_key": self.projection_key,
                "consumer_id": self.consumer_id,
                "consumer_kind": self.consumer_kind,
                "node_scope": self.node_scope,
                "pinned": self.pinned,
                "visibility": self.visibility,
                "params": dict(self.params) if isinstance(self.params, Mapping) else self.params,
            }
        )


@dataclass(frozen=True, slots=True)
class ClientSubscriptionRecord:
    client_id: str
    device_id: str
    session_id: str
    webspace_id: str
    role: str
    subscriptions: tuple[ProjectionSubscription, ...]
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "webspace_id": self.webspace_id,
            "role": self.role,
            "subscriptions": [item.to_dict() for item in self.subscriptions],
            "updated_at": self.updated_at,
        }


def normalize_projection_subscription(value: Mapping[str, Any] | ProjectionSubscription) -> ProjectionSubscription:
    if isinstance(value, ProjectionSubscription):
        return value

    data = _mapping(value)
    return ProjectionSubscription(
        projection_key=str(data.get("projection_key") or ""),
        consumer_id=str(data.get("consumer_id") or ""),
        consumer_kind=str(data.get("consumer_kind") or ""),
        node_scope=data.get("node_scope"),
        pinned=bool(data.get("pinned", False)),
        visibility=str(data.get("visibility") or "visible"),
        params=_mapping(data.get("params")) or None,
    )


def make_projection_subscription(
    *,
    projection_key: str,
    consumer_id: str,
    consumer_kind: str,
    node_scope: Any = None,
    pinned: bool = False,
    visibility: str = "visible",
    params: Mapping[str, Any] | None = None,
) -> ProjectionSubscription:
    return ProjectionSubscription(
        projection_key=str(projection_key),
        consumer_id=str(consumer_id),
        consumer_kind=str(consumer_kind),
        node_scope=node_scope,
        pinned=bool(pinned),
        visibility=str(visibility or "visible"),
        params=params,
    )


def make_client_subscription_record(
    *,
    client_id: str,
    device_id: str,
    session_id: str,
    webspace_id: str,
    role: str,
    subscriptions: Iterable[Mapping[str, Any] | ProjectionSubscription],
    updated_at: float | None = None,
) -> ClientSubscriptionRecord:
    return ClientSubscriptionRecord(
        client_id=str(client_id),
        device_id=str(device_id),
        session_id=str(session_id),
        webspace_id=str(webspace_id),
        role=str(role),
        subscriptions=tuple(normalize_projection_subscription(item) for item in subscriptions),
        updated_at=float(updated_at if updated_at is not None else time.time()),
    )


def normalize_client_subscription_record(
    value: Mapping[str, Any] | ClientSubscriptionRecord,
) -> ClientSubscriptionRecord:
    if isinstance(value, ClientSubscriptionRecord):
        return value

    data = _mapping(value)
    return make_client_subscription_record(
        client_id=str(data.get("client_id") or ""),
        device_id=str(data.get("device_id") or ""),
        session_id=str(data.get("session_id") or ""),
        webspace_id=str(data.get("webspace_id") or ""),
        role=str(data.get("role") or ""),
        subscriptions=list(data.get("subscriptions") or []),
        updated_at=float(data.get("updated_at") or time.time()),
    )


def client_subscription_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the browser-written projection subscription ABI contract."""

    sample_record = make_client_subscription_record(
        client_id="browser-1",
        device_id="desktop",
        session_id="session-1",
        webspace_id="desktop",
        role="operator",
        updated_at=float(now if now is not None else 0.0),
        subscriptions=[
            make_projection_subscription(
                projection_key="status-card:runtime",
                consumer_id="widget:runtime",
                consumer_kind="widget",
                node_scope={"node_id": "node-a"},
                visibility="visible",
            ),
            make_projection_subscription(
                projection_key="projection:hub/object-inspector",
                consumer_id="modal:runtime-details",
                consumer_kind="modal",
                pinned=True,
                visibility="hidden",
                params={"object_id": "runtime"},
            ),
        ],
    )
    return {
        "contract": CLIENT_SUBSCRIPTION_CONTRACT,
        "ready_for_mvp": True,
        "record_required_fields": list(CLIENT_SUBSCRIPTION_REQUIRED_FIELDS),
        "subscription_required_fields": list(PROJECTION_SUBSCRIPTION_REQUIRED_FIELDS),
        "subscription_optional_fields": list(PROJECTION_SUBSCRIPTION_OPTIONAL_FIELDS),
        "write_policy": {
            "mode": "replace_full_client_session_set",
            "touch_extends_session_without_replacing_demand": True,
            "delete_removes_session_demand": True,
        },
        "semantics": {
            "pinned": "Keeps demand meaningful even when the consumer is temporarily hidden or stale.",
            "visibility": "visible consumers count toward visible_total; hidden consumers still keep explicit demand.",
            "node_scope": "Limits a projection demand to a node-aware view when the projection supports node multiplicity.",
            "params": "Carries consumer-specific projection parameters without changing the projection_key identity.",
        },
        "registry": {
            "snapshot_endpoint": "/api/node/projection-demand",
            "write_endpoint": "/api/node/projection-demand/client",
            "browser_state_endpoint": "/api/node/projection-demand/browser-state",
            "delete_endpoint": "/api/node/projection-demand/client/{client_id}/{session_id}",
        },
        "sample_record": sample_record.to_dict(),
        "sample_projection_keys": sorted({item.projection_key for item in sample_record.subscriptions}),
    }


__all__ = [
    "CLIENT_SUBSCRIPTION_CONTRACT",
    "ClientSubscriptionRecord",
    "PROJECTION_SUBSCRIPTION_REQUIRED_FIELDS",
    "ProjectionSubscription",
    "client_subscription_contract_snapshot",
    "make_client_subscription_record",
    "make_projection_subscription",
    "normalize_client_subscription_record",
    "normalize_projection_subscription",
]
