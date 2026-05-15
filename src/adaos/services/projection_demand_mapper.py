from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from adaos.domain import (
    ClientSubscriptionRecord,
    ProjectionSubscription,
    make_client_subscription_record,
    make_projection_subscription,
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        token = _text(data.get(key))
        if token:
            return token
    return ""


def _projection_keys(data: Mapping[str, Any]) -> list[str]:
    direct = _first_text(data, "projection_key", "projectionKey")
    keys: list[str] = [direct] if direct else []
    for key in ("projection_keys", "projectionKeys"):
        for item in _items(data.get(key)):
            token = _text(item)
            if token:
                keys.append(token)
    for demand in _items(data.get("demands")):
        if isinstance(demand, Mapping):
            token = _first_text(demand, "projection_key", "projectionKey")
        else:
            token = _text(demand)
        if token:
            keys.append(token)
    out: list[str] = []
    for key in keys:
        if key not in out:
            out.append(key)
    return out


def _consumer_id(data: Mapping[str, Any], *, consumer_kind: str, fallback_index: int) -> str:
    token = _first_text(data, "consumer_id", "consumerId")
    if token:
        return token
    raw_id = _first_text(data, "id", "key", "name")
    if raw_id:
        return f"{consumer_kind}:{raw_id}"
    return f"{consumer_kind}:{fallback_index}"


def _node_scope(data: Mapping[str, Any]) -> Any:
    if "node_scope" in data:
        return data.get("node_scope")
    if "nodeScope" in data:
        return data.get("nodeScope")
    node_id = _first_text(data, "node_id", "nodeId", "target_node_id", "targetNodeId")
    return {"node_id": node_id} if node_id else None


def _visibility(data: Mapping[str, Any], *, default: str) -> str:
    token = _first_text(data, "visibility")
    if token:
        return token
    if data.get("visible") is False:
        return "hidden"
    return default


def _params(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    params = data.get("params")
    return params if isinstance(params, Mapping) else None


def projection_subscriptions_from_browser_consumers(
    *,
    page: Mapping[str, Any] | str | None = None,
    widgets: Iterable[Mapping[str, Any] | str] = (),
    modals: Iterable[Mapping[str, Any] | str] = (),
    pinned_panels: Iterable[Mapping[str, Any] | str] = (),
) -> tuple[ProjectionSubscription, ...]:
    subscriptions: list[ProjectionSubscription] = []

    groups: list[tuple[str, Iterable[Any], bool, str]] = [
        ("page", _items(page), False, "visible"),
        ("widget", list(widgets), False, "visible"),
        ("modal", list(modals), False, "visible"),
        ("pinned-panel", list(pinned_panels), True, "visible"),
    ]
    for consumer_kind, values, force_pinned, default_visibility in groups:
        for index, value in enumerate(values):
            data = {"projection_key": value, "id": value} if isinstance(value, str) else _mapping(value)
            if not data:
                continue
            for projection_key in _projection_keys(data):
                subscriptions.append(
                    make_projection_subscription(
                        projection_key=projection_key,
                        consumer_id=_consumer_id(data, consumer_kind=consumer_kind, fallback_index=index),
                        consumer_kind=consumer_kind,
                        node_scope=_node_scope(data),
                        pinned=force_pinned or bool(data.get("pinned")),
                        visibility=_visibility(data, default=default_visibility),
                        params=_params(data),
                    )
                )
    return tuple(subscriptions)


def build_browser_projection_demand_record(
    *,
    client_id: str,
    device_id: str,
    session_id: str,
    webspace_id: str,
    role: str = "operator",
    page: Mapping[str, Any] | str | None = None,
    widgets: Iterable[Mapping[str, Any] | str] = (),
    modals: Iterable[Mapping[str, Any] | str] = (),
    pinned_panels: Iterable[Mapping[str, Any] | str] = (),
    updated_at: float | None = None,
) -> ClientSubscriptionRecord:
    return make_client_subscription_record(
        client_id=client_id,
        device_id=device_id,
        session_id=session_id,
        webspace_id=webspace_id,
        role=role,
        subscriptions=projection_subscriptions_from_browser_consumers(
            page=page,
            widgets=widgets,
            modals=modals,
            pinned_panels=pinned_panels,
        ),
        updated_at=float(updated_at if updated_at is not None else time.time()),
    )


__all__ = [
    "build_browser_projection_demand_record",
    "projection_subscriptions_from_browser_consumers",
]
