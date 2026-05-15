from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Awaitable, Callable, Mapping

from adaos.domain import EventEnvelope, normalize_event_envelope
from adaos.services.projection_demand import ProjectionDemandConsumer, projection_demand_consumers


@dataclass(frozen=True, slots=True)
class ProjectionRefreshContext:
    event: EventEnvelope
    webspace_id: str
    projection_key: str
    consumers: tuple[ProjectionDemandConsumer, ...]
    requested_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "webspace_id": self.webspace_id,
            "projection_key": self.projection_key,
            "consumers": [item.to_dict() for item in self.consumers],
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class ProjectionRefreshResult:
    projection_key: str
    webspace_id: str
    status: str = "ready"
    record: Mapping[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_key": self.projection_key,
            "webspace_id": self.webspace_id,
            "status": self.status,
            "record": dict(self.record) if isinstance(self.record, Mapping) else self.record,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProjectionDispatchReport:
    event_type: str
    webspace_ids: tuple[str, ...]
    selected: tuple[ProjectionRefreshContext, ...] = field(default_factory=tuple)
    refreshed: tuple[ProjectionRefreshResult, ...] = field(default_factory=tuple)
    skipped: tuple[ProjectionRefreshResult, ...] = field(default_factory=tuple)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "webspace_ids": list(self.webspace_ids),
            "selected": [item.to_dict() for item in self.selected],
            "refreshed": [item.to_dict() for item in self.refreshed],
            "skipped": [item.to_dict() for item in self.skipped],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


ProjectionRefreshHandler = Callable[[ProjectionRefreshContext], Any | Awaitable[Any]]


_LOCK = RLock()
_HANDLERS: dict[str, ProjectionRefreshHandler] = {}


def clear_projection_dispatcher() -> None:
    with _LOCK:
        _HANDLERS.clear()


def register_projection_refresh_handler(
    projection_key: str,
    handler: ProjectionRefreshHandler,
) -> None:
    token = str(projection_key or "").strip()
    if not token:
        raise ValueError("projection_key is required")
    with _LOCK:
        _HANDLERS[token] = handler


def unregister_projection_refresh_handler(projection_key: str) -> bool:
    token = str(projection_key or "").strip()
    with _LOCK:
        return _HANDLERS.pop(token, None) is not None


def registered_projection_refresh_handlers() -> list[str]:
    with _LOCK:
        return sorted(_HANDLERS)


def _handler_for(projection_key: str) -> ProjectionRefreshHandler | None:
    with _LOCK:
        return _HANDLERS.get(projection_key)


def _event_scope_webspace_ids(event: EventEnvelope) -> list[str]:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    scope = event.scope if isinstance(event.scope, Mapping) else {}
    candidates = [
        scope.get("webspace_id"),
        payload.get("webspace_id"),
        payload.get("workspace_id"),
    ]
    out: list[str] = []
    for value in candidates:
        token = str(value or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _normalize_webspace_ids(
    event: EventEnvelope,
    webspace_ids: list[str] | tuple[str, ...] | set[str] | None,
) -> tuple[str, ...]:
    raw = list(webspace_ids or []) or _event_scope_webspace_ids(event)
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return tuple(out)


def _projection_key_allowed(projection_key: str, allowed: set[str] | None) -> bool:
    return allowed is None or projection_key in allowed


def demanded_projection_refresh_contexts(
    event: Any,
    *,
    webspace_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    projection_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> tuple[ProjectionRefreshContext, ...]:
    envelope = normalize_event_envelope(event)
    target_webspaces = _normalize_webspace_ids(envelope, webspace_ids)
    allowed = {str(item or "").strip() for item in projection_keys or [] if str(item or "").strip()} or None
    ts = float(now if now is not None else time.time())
    contexts: list[ProjectionRefreshContext] = []
    for webspace_id in target_webspaces:
        consumers_by_projection: dict[str, list[ProjectionDemandConsumer]] = {}
        for consumer in projection_demand_consumers(
            webspace_id=webspace_id,
            include_hidden=include_hidden,
            include_stale=include_stale,
            stale_after_s=stale_after_s,
            now=ts,
        ):
            if not _projection_key_allowed(consumer.projection_key, allowed):
                continue
            consumers_by_projection.setdefault(consumer.projection_key, []).append(consumer)
        for projection_key, consumers in sorted(consumers_by_projection.items()):
            contexts.append(
                ProjectionRefreshContext(
                    event=envelope,
                    webspace_id=webspace_id,
                    projection_key=projection_key,
                    consumers=tuple(consumers),
                    requested_at=ts,
                )
            )
    return tuple(contexts)


def _result_from_handler_output(context: ProjectionRefreshContext, value: Any) -> ProjectionRefreshResult:
    if isinstance(value, ProjectionRefreshResult):
        return value
    status = "ready"
    reason = None
    record = value if isinstance(value, Mapping) else None
    if isinstance(value, Mapping):
        status = str(value.get("status") or status)
        reason_value = value.get("reason")
        reason = str(reason_value) if reason_value is not None else None
    return ProjectionRefreshResult(
        projection_key=context.projection_key,
        webspace_id=context.webspace_id,
        status=status,
        record=record,
        reason=reason,
    )


async def dispatch_demanded_projection_refresh(
    event: Any,
    *,
    webspace_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    projection_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> ProjectionDispatchReport:
    started_at = float(now if now is not None else time.time())
    envelope = normalize_event_envelope(event)
    target_webspaces = _normalize_webspace_ids(envelope, webspace_ids)
    selected = demanded_projection_refresh_contexts(
        envelope,
        webspace_ids=target_webspaces,
        projection_keys=projection_keys,
        include_hidden=include_hidden,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=started_at,
    )
    refreshed: list[ProjectionRefreshResult] = []
    skipped: list[ProjectionRefreshResult] = []
    for context in selected:
        handler = _handler_for(context.projection_key)
        if handler is None:
            skipped.append(
                ProjectionRefreshResult(
                    projection_key=context.projection_key,
                    webspace_id=context.webspace_id,
                    status="skipped",
                    reason="no_handler",
                )
            )
            continue
        value = handler(context)
        if inspect.isawaitable(value):
            value = await value
        refreshed.append(_result_from_handler_output(context, value))
    return ProjectionDispatchReport(
        event_type=envelope.type,
        webspace_ids=target_webspaces,
        selected=selected,
        refreshed=tuple(refreshed),
        skipped=tuple(skipped),
        started_at=started_at,
        finished_at=time.time(),
    )


__all__ = [
    "ProjectionDispatchReport",
    "ProjectionRefreshContext",
    "ProjectionRefreshHandler",
    "ProjectionRefreshResult",
    "clear_projection_dispatcher",
    "demanded_projection_refresh_contexts",
    "dispatch_demanded_projection_refresh",
    "register_projection_refresh_handler",
    "registered_projection_refresh_handlers",
    "unregister_projection_refresh_handler",
]
