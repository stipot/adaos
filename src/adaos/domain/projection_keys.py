from __future__ import annotations

from typing import Any


PROJECTION_KEY_PREFIX = "projection:"
STATUS_CARD_PROJECTION_PREFIX = "status-card:"
NODE_SCOPED_PROJECTION_PREFIX = f"{PROJECTION_KEY_PREFIX}node/"
SURFACE_PROJECTION_KINDS = {"page", "widget", "modal", "panel"}


def _required_token(value: Any, name: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{name} is required")
    return token


def _node_token(value: Any) -> str:
    return _required_token(value, "node_id").strip("/").replace("/", "-")


def status_card_projection_key(card_id: Any) -> str:
    return f"{STATUS_CARD_PROJECTION_PREFIX}{_required_token(card_id, 'card_id')}"


def status_card_id_from_projection_key(projection_key: Any) -> str:
    token = _required_token(projection_key, "projection_key")
    if not token.startswith(STATUS_CARD_PROJECTION_PREFIX):
        raise ValueError("status-card projection key is required")
    return _required_token(token[len(STATUS_CARD_PROJECTION_PREFIX) :], "card_id")


def surface_projection_key(
    surface_kind: Any,
    surface_id: Any,
    *,
    node_id: Any | None = None,
) -> str:
    kind = _required_token(surface_kind, "surface_kind").lower()
    if kind not in SURFACE_PROJECTION_KINDS:
        raise ValueError(f"unsupported surface projection kind: {kind}")
    key = f"{PROJECTION_KEY_PREFIX}{kind}/{_required_token(surface_id, f'{kind}_id')}"
    if node_id is None:
        return key
    return node_scoped_projection_key(node_id=node_id, projection_key=key)


def page_projection_key(page_id: Any, *, node_id: Any | None = None) -> str:
    return surface_projection_key("page", page_id, node_id=node_id)


def widget_projection_key(widget_id: Any, *, node_id: Any | None = None) -> str:
    return surface_projection_key("widget", widget_id, node_id=node_id)


def modal_projection_key(modal_id: Any, *, node_id: Any | None = None) -> str:
    return surface_projection_key("modal", modal_id, node_id=node_id)


def panel_projection_key(panel_id: Any, *, node_id: Any | None = None) -> str:
    return surface_projection_key("panel", panel_id, node_id=node_id)


def node_scoped_projection_key(*, node_id: Any, projection_key: Any) -> str:
    node = _node_token(node_id)
    key = _required_token(projection_key, "projection_key").lstrip("/")
    return f"{NODE_SCOPED_PROJECTION_PREFIX}{node}/{key}"


__all__ = [
    "NODE_SCOPED_PROJECTION_PREFIX",
    "PROJECTION_KEY_PREFIX",
    "STATUS_CARD_PROJECTION_PREFIX",
    "SURFACE_PROJECTION_KINDS",
    "modal_projection_key",
    "node_scoped_projection_key",
    "page_projection_key",
    "panel_projection_key",
    "status_card_id_from_projection_key",
    "status_card_projection_key",
    "surface_projection_key",
    "widget_projection_key",
]
