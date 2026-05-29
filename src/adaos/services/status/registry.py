from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from adaos.domain import Event
from adaos.services.eventbus import emit

from .cards import (
    STATUS_CARD_MAX_BYTES,
    StatusCard,
    normalize_status_card,
    status_card_boundary_diagnostics,
)


def _card_key(card: StatusCard) -> str:
    return "\0".join([card.scope, card.owner, card.webspace_id or "", card.id])


class StatusRegistry:
    def __init__(
        self,
        *,
        bus: Any | None = None,
        max_cards: int = 10000,
        max_card_bytes: int = STATUS_CARD_MAX_BYTES,
    ) -> None:
        self._lock = threading.RLock()
        self._cards: dict[str, StatusCard] = {}
        self._bus = bus
        self._max_cards = max(1, int(max_cards or 10000))
        self._max_card_bytes = max(1, int(max_card_bytes or STATUS_CARD_MAX_BYTES))
        self._publish_total = 0
        self._changed_total = 0
        self._unchanged_total = 0
        self._oversized_card_total = 0
        self._max_card_bytes_observed = 0
        self._last_oversized_card: dict[str, Any] | None = None
        self._last_publish_latency_ms = 0.0
        self._last_changed_at: float | None = None

    def publish(self, card: StatusCard | dict[str, Any], *, emit_event: bool = True) -> dict[str, Any]:
        started = time.perf_counter()
        incoming = normalize_status_card(card)
        key = _card_key(incoming)
        changed = False
        with self._lock:
            previous = self._cards.get(key)
            if previous and previous.fingerprint == incoming.fingerprint:
                stored = incoming.with_registry_state(
                    version=previous.version,
                    fingerprint=previous.fingerprint,
                    changed_at=previous.changed_at,
                )
                self._unchanged_total += 1
            else:
                stored = incoming.with_registry_state(
                    version=(previous.version + 1) if previous else max(1, incoming.version),
                    fingerprint=incoming.fingerprint,
                    changed_at=incoming.updated_at,
                )
                changed = True
                self._changed_total += 1
                self._last_changed_at = stored.changed_at
            self._cards[key] = stored
            self._publish_total += 1
            self._record_boundary_diagnostics_locked(stored)
            self._prune_locked()
            self._last_publish_latency_ms = (time.perf_counter() - started) * 1000.0
        if changed and emit_event and self._bus is not None:
            scope = {"webspace_id": stored.webspace_id} if stored.webspace_id else None
            emit(
                self._bus,
                "adaos.status.card.changed",
                {"card": stored.to_dict(), "webspace_id": stored.webspace_id},
                "status.registry",
                source_authority="platform",
                scope=scope,
                schema="adaos.status.card.changed",
                version=1,
                priority="normal",
                generate_event_id=True,
            )
        return {"changed": changed, "card": stored.to_dict(), "key": key}

    def publish_many(self, cards: Iterable[StatusCard | dict[str, Any]], *, emit_event: bool = True) -> dict[str, Any]:
        results = [self.publish(card, emit_event=emit_event) for card in cards]
        return {
            "published": len(results),
            "changed": sum(1 for item in results if item.get("changed")),
            "unchanged": sum(1 for item in results if not item.get("changed")),
            "cards": [item["card"] for item in results],
        }

    def snapshot(
        self,
        *,
        owner: str | None = None,
        scope: str | None = None,
        webspace_id: str | None = None,
        include_stale: bool = True,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        token_owner = str(owner or "").strip()
        token_scope = str(scope or "").strip()
        token_ws = str(webspace_id or "").strip()
        with self._lock:
            rows = list(self._cards.values())
        if token_owner:
            rows = [card for card in rows if card.owner == token_owner]
        if token_scope:
            rows = [card for card in rows if card.scope == token_scope]
        if token_ws:
            rows = [card for card in rows if (card.webspace_id or "") == token_ws]
        if not include_stale:
            rows = [card for card in rows if not card.is_stale(now_ts=now)]
        rows.sort(key=lambda card: (card.scope, card.owner, card.kind, card.id))
        return {
            "schema": "adaos.status_registry.v1",
            "updated_at": now,
            "cards": [card.to_dict(now_ts=now) for card in rows],
            "total": len(rows),
            "diagnostics": self.diagnostics(now_ts=now),
        }

    def diagnostics(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            cards = list(self._cards.values())
            return {
                "schema": "adaos.status_registry.diagnostics.v1",
                "card_count": len(cards),
                "publish_total": int(self._publish_total),
                "changed_total": int(self._changed_total),
                "unchanged_total": int(self._unchanged_total),
                "max_card_bytes": int(self._max_card_bytes),
                "max_card_bytes_observed": int(self._max_card_bytes_observed),
                "oversized_card_total": int(self._oversized_card_total),
                "last_oversized_card": dict(self._last_oversized_card) if self._last_oversized_card else None,
                "stale_count": sum(1 for card in cards if card.is_stale(now_ts=now)),
                "last_publish_latency_ms": float(self._last_publish_latency_ms),
                "last_changed_at": self._last_changed_at,
            }

    def _record_boundary_diagnostics_locked(self, card: StatusCard) -> None:
        diagnostics = status_card_boundary_diagnostics(
            card.to_dict(now_ts=card.updated_at),
            max_bytes=self._max_card_bytes,
        )
        size_bytes = int(diagnostics.get("bytes") or 0)
        self._max_card_bytes_observed = max(self._max_card_bytes_observed, size_bytes)
        if not diagnostics.get("oversized"):
            return
        self._oversized_card_total += 1
        self._last_oversized_card = {
            "id": card.id,
            "owner": card.owner,
            "scope": card.scope,
            "webspace_id": card.webspace_id,
            "bytes": size_bytes,
            "max_bytes": int(diagnostics.get("max_bytes") or self._max_card_bytes),
        }

    def _prune_locked(self) -> None:
        if len(self._cards) <= self._max_cards:
            return
        stale = sorted(self._cards.items(), key=lambda item: float(item[1].updated_at or 0.0))
        for key, _card in stale[: max(0, len(stale) - self._max_cards)]:
            self._cards.pop(key, None)


def register_status_registry(bus: Any, registry: StatusRegistry | None = None) -> StatusRegistry:
    target = registry or StatusRegistry(bus=bus)

    def _publish_one(event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        card = payload.get("card") if isinstance(payload.get("card"), dict) else payload
        if isinstance(card, dict):
            target.publish(card)

    def _publish_many(event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        cards = payload.get("cards") if isinstance(payload.get("cards"), list) else []
        target.publish_many([card for card in cards if isinstance(card, dict)])

    bus.subscribe("adaos.status.card.single", _publish_one)
    bus.subscribe("adaos.status.card.batch", _publish_many)
    try:
        from adaos.services.projection_event_bridge import register_projection_event_bridge

        register_projection_event_bridge(bus)
    except Exception:
        pass
    return target


__all__ = ["StatusRegistry", "register_status_registry"]
