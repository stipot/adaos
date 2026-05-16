from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.domain import Event
from adaos.services.status_card_registry import get_status_card


def status_card_details_ref(*, card_id: str, webspace_id: str) -> dict[str, Any] | None:
    card = get_status_card(card_id=card_id, webspace_id=webspace_id)
    if card is None or card.details_ref is None:
        return None
    return card.details_ref.to_dict()


def request_status_card_details_refresh(
    *,
    card_id: str,
    webspace_id: str,
    bus: Any = None,
    requested_at: float | None = None,
) -> dict[str, Any]:
    card = get_status_card(card_id=card_id, webspace_id=webspace_id)
    if card is None:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "card_id": card_id,
            "reason": "status_card_not_found",
        }
    details_ref = card.details_ref.to_dict() if card.details_ref is not None else None
    if not details_ref:
        return {
            "ok": True,
            "accepted": False,
            "webspace_id": webspace_id,
            "card_id": card.id,
            "reason": "details_ref_missing",
            "details_ref": None,
        }
    kind = str(details_ref.get("kind") or "").strip().lower()
    if kind != "stream":
        return {
            "ok": True,
            "accepted": False,
            "webspace_id": webspace_id,
            "card_id": card.id,
            "reason": f"{kind or 'unknown'}_details_ref",
            "details_ref": details_ref,
        }
    receiver = str(details_ref.get("receiver") or "").strip()
    if not receiver:
        return {
            "ok": True,
            "accepted": False,
            "webspace_id": webspace_id,
            "card_id": card.id,
            "reason": "stream_receiver_missing",
            "details_ref": details_ref,
        }
    publish = getattr(bus, "publish", None)
    if not callable(publish):
        return {
            "ok": True,
            "accepted": False,
            "webspace_id": webspace_id,
            "card_id": card.id,
            "reason": "bus_unavailable",
            "details_ref": details_ref,
        }
    ts = float(requested_at if requested_at is not None else time.time())
    params = details_ref.get("params") if isinstance(details_ref.get("params"), Mapping) else {}
    event_payload = {
        **dict(params),
        "webspace_id": str(params.get("webspace_id") or webspace_id),
        "receiver": receiver,
        "card_id": card.id,
        "details_ref": details_ref,
    }
    event = Event(
        type="webio.stream.snapshot.requested",
        payload=event_payload,
        source="status-card.details",
        ts=ts,
    )
    publish(event)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_id,
        "card_id": card.id,
        "details_ref": details_ref,
        "requested_event": {
            "type": event.type,
            "payload": dict(event.payload),
            "source": event.source,
            "ts": event.ts,
        },
    }


__all__ = ["request_status_card_details_refresh", "status_card_details_ref"]
