from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Awaitable, Callable, Mapping

from adaos.domain import Event, EventEnvelope, normalize_event_envelope
from adaos.services.projection_demand import ProjectionDemandConsumer, projection_demand_consumers
from adaos.services.projection_records import write_projection_record_if_valid


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
    errors: tuple[ProjectionRefreshResult, ...] = field(default_factory=tuple)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "webspace_ids": list(self.webspace_ids),
            "selected": [item.to_dict() for item in self.selected],
            "refreshed": [item.to_dict() for item in self.refreshed],
            "skipped": [item.to_dict() for item in self.skipped],
            "errors": [item.to_dict() for item in self.errors],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


ProjectionRefreshHandler = Callable[[ProjectionRefreshContext], Any | Awaitable[Any]]
PROJECTION_DISPATCHER_MEMORY_CONTRACT = "adaos.projection-dispatcher.memory-vs-yjs.v1"


_LOCK = RLock()
_HANDLERS: dict[str, ProjectionRefreshHandler] = {}
_ACTIVE_REFRESHES: set[tuple[str, str]] = set()
_LIFECYCLE: dict[tuple[str, str], dict[str, Any]] = {}
_STATS: dict[str, int] = {
    "incoming_total": 0,
    "selected_total": 0,
    "refreshed_total": 0,
    "skipped_total": 0,
    "error_total": 0,
    "coalesced_total": 0,
    "dropped_total": 0,
    "superseded_total": 0,
}


def clear_projection_dispatcher() -> None:
    with _LOCK:
        _HANDLERS.clear()
        _ACTIVE_REFRESHES.clear()
        _LIFECYCLE.clear()
        for key in list(_STATS):
            _STATS[key] = 0


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


def _inc_stat(name: str, amount: int = 1) -> None:
    with _LOCK:
        _STATS[name] = int(_STATS.get(name) or 0) + int(amount)


def _set_lifecycle(
    *,
    context: ProjectionRefreshContext,
    status: str,
    reason: str | None = None,
    error: str | None = None,
    ts: float | None = None,
) -> None:
    now = float(ts if ts is not None else time.time())
    with _LOCK:
        previous = dict(_LIFECYCLE.get((context.webspace_id, context.projection_key)) or {})
        changed_at = float(previous.get("changed_at") or now)
        if previous.get("status") != status:
            changed_at = now
        _LIFECYCLE[(context.webspace_id, context.projection_key)] = {
            "webspace_id": context.webspace_id,
            "projection_key": context.projection_key,
            "status": status,
            "reason": reason,
            "error": error,
            "consumer_total": len(context.consumers),
            "updated_at": now,
            "changed_at": changed_at,
            "event_type": context.event.type,
        }


def projection_dispatcher_snapshot() -> dict[str, Any]:
    with _LOCK:
        lifecycle = [
            dict(value)
            for _, value in sorted(_LIFECYCLE.items(), key=lambda item: (item[0][0], item[0][1]))
        ]
        stats = dict(_STATS)
        active = [
            {"webspace_id": webspace_id, "projection_key": projection_key}
            for webspace_id, projection_key in sorted(_ACTIVE_REFRESHES)
        ]
        handlers = sorted(_HANDLERS)
    return {
        "ok": True,
        "handlers": handlers,
        "handler_total": len(handlers),
        "active": active,
        "active_total": len(active),
        "lifecycle": lifecycle,
        "stats": stats,
        "updated_at": time.time(),
    }


def _handler_for(projection_key: str) -> ProjectionRefreshHandler | None:
    with _LOCK:
        exact = _HANDLERS.get(projection_key)
        if exact is not None:
            return exact
        wildcard_matches = [
            (token[:-1], handler)
            for token, handler in _HANDLERS.items()
            if token.endswith("*") and projection_key.startswith(token[:-1])
        ]
        if not wildcard_matches:
            return None
        return max(wildcard_matches, key=lambda item: len(item[0]))[1]


def _handler_match(projection_key: str) -> dict[str, Any]:
    with _LOCK:
        if projection_key in _HANDLERS:
            return {
                "covered": True,
                "key": projection_key,
                "kind": "exact",
            }
        wildcard_matches = [
            token
            for token in _HANDLERS
            if token.endswith("*") and projection_key.startswith(token[:-1])
        ]
    if not wildcard_matches:
        return {
            "covered": False,
            "key": None,
            "kind": "none",
        }
    key = max(wildcard_matches, key=lambda item: len(item[:-1]))
    return {
        "covered": True,
        "key": key,
        "kind": "wildcard",
    }


def _core_skill_ownership_policy(*, handler_covered: bool) -> dict[str, Any]:
    return {
        "core_owned": [
            "projection demand selection",
            "projection lifecycle bookkeeping",
            "ProjectionRecord materialization",
            "data/projectionRecords cache writes",
        ],
        "skill_owned": [
            "semantic source state",
            "payload refresh",
            "domain-specific error mapping",
        ]
        if handler_covered
        else [],
        "browser_owned": [
            "active subscription set",
            "consumer identity",
            "view lifecycle demand",
        ],
        "forbidden": [
            "browser writes to data/projectionRecords",
            "skill direct writes to data/projectionRecords",
            "skill-local replacement of ProjectionRecord lifecycle",
        ],
    }


def core_skill_refresh_contract_snapshot(
    event: Any | None = None,
    *,
    webspace_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    projection_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the core-to-skill demanded refresh contract without dispatching."""

    ts = float(now if now is not None else time.time())
    source_event = event or Event(
        type="projection.core_skill.contract.inspect",
        payload={},
        source="projection_dispatcher",
        ts=ts,
    )
    envelope = normalize_event_envelope(source_event)
    contexts = demanded_projection_refresh_contexts(
        envelope,
        webspace_ids=webspace_ids,
        projection_keys=projection_keys,
        include_hidden=include_hidden,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=ts,
    )
    demands = []
    covered_total = 0
    uncovered_total = 0
    uncovered_projection_keys: list[str] = []
    for context in contexts:
        handler = _handler_match(context.projection_key)
        if handler["covered"]:
            covered_total += 1
        else:
            uncovered_total += 1
            uncovered_projection_keys.append(context.projection_key)
        demands.append(
            {
                "webspace_id": context.webspace_id,
                "projection_key": context.projection_key,
                "consumer_total": len(context.consumers),
                "consumers": [item.to_dict() for item in context.consumers],
                "handler": handler,
                "ownership": _core_skill_ownership_policy(handler_covered=bool(handler["covered"])),
                "refresh_contract": {
                    "core_selects_demand": True,
                    "skill_refreshes_payload": bool(handler["covered"]),
                    "core_materializes_projection_record": True,
                    "lifecycle_sequence": ["pending", "refreshing", "ready|stale|error"],
                    "write_policy": "ProjectionRecord registry -> data/projectionRecords",
                },
            }
        )
    coverage_ratio = round(float(covered_total) / float(len(demands)), 4) if demands else 1.0
    ready_for_dispatch = uncovered_total == 0
    return {
        "ok": True,
        "source": "projection_dispatcher.core_skill_refresh_contract",
        "contract": "adaos.core-skill-projection-refresh.v1",
        "event": envelope.to_dict(),
        "webspace_ids": list(_normalize_webspace_ids(envelope, webspace_ids)),
        "projection_keys": [str(item or "").strip() for item in projection_keys or [] if str(item or "").strip()],
        "demand_total": len(demands),
        "covered_total": covered_total,
        "uncovered_total": uncovered_total,
        "uncovered_projection_keys": uncovered_projection_keys,
        "readiness": {
            "ready_for_dispatch": ready_for_dispatch,
            "coverage_ratio": coverage_ratio,
            "status": "pass" if ready_for_dispatch else "warn",
            "reason": "all demanded projections have refresh handlers"
            if ready_for_dispatch
            else "some demanded projections have no refresh handler",
            "recommended_next_step": None
            if ready_for_dispatch
            else "register projection refresh handlers or narrow projection_keys before dispatch",
        },
        "demands": demands,
        "dispatcher": projection_dispatcher_snapshot(),
        "updated_at": ts,
    }


def projection_dispatcher_memory_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the dispatcher rule that memory may be richer than Yjs publication."""

    return {
        "contract": PROJECTION_DISPATCHER_MEMORY_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else time.time()),
        "principle": "Handlers may keep rich semantic state in memory, but publish only compact canonical ProjectionRecords.",
        "memory_allowed": [
            "domain-specific source snapshots",
            "inspection indexes",
            "handler-local debounce/coalescing state",
            "temporary error context",
            "semantic objects richer than the published view",
        ],
        "yjs_publication": {
            "path": "data/projectionRecords",
            "record_shape": ["status", "data", "meta", "error"],
            "write_owner": "core:projection_records",
            "write_policy": "ProjectionRecord registry -> Yjs cache materialization",
        },
        "dispatcher_boundaries": {
            "handler_input": "ProjectionRefreshContext with event, webspace_id, projection_key, and consumers",
            "handler_output": "ProjectionRefreshResult or ProjectionRecord-shaped mapping",
            "core_materializes_record": True,
            "handler_writes_yjs_directly": False,
            "browser_reads_yjs_cache": True,
            "browser_writes_yjs_cache": False,
        },
        "compaction_rules": [
            "Publish demanded projections only.",
            "Keep projection data consumer-oriented and serializable.",
            "Store node scope and access metadata in ProjectionRecord.meta.",
            "Expose detailed diagnostics through separate operator/status-card projections when needed.",
        ],
        "evidence": [
            "/api/node/projection-dispatcher",
            "/api/node/projection-dispatcher/core-skill-contract",
            "/api/node/projection-records/yjs/cache",
            "/api/node/projection-runtime-ownership",
        ],
    }


def _try_begin_refresh(context: ProjectionRefreshContext) -> bool:
    key = (context.webspace_id, context.projection_key)
    with _LOCK:
        if key in _ACTIVE_REFRESHES:
            _STATS["coalesced_total"] = int(_STATS.get("coalesced_total") or 0) + 1
            return False
        _ACTIVE_REFRESHES.add(key)
        return True


def _end_refresh(context: ProjectionRefreshContext) -> None:
    with _LOCK:
        _ACTIVE_REFRESHES.discard((context.webspace_id, context.projection_key))


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
    errors: list[ProjectionRefreshResult] = []
    _inc_stat("incoming_total")
    _inc_stat("selected_total", len(selected))
    for context in selected:
        _set_lifecycle(context=context, status="pending", reason="demanded", ts=started_at)
        handler = _handler_for(context.projection_key)
        if handler is None:
            _inc_stat("skipped_total")
            _set_lifecycle(context=context, status="stale", reason="no_handler")
            skipped.append(
                ProjectionRefreshResult(
                    projection_key=context.projection_key,
                    webspace_id=context.webspace_id,
                    status="skipped",
                    reason="no_handler",
                )
            )
            continue
        if not _try_begin_refresh(context):
            _inc_stat("skipped_total")
            _set_lifecycle(context=context, status="stale", reason="coalesced")
            skipped.append(
                ProjectionRefreshResult(
                    projection_key=context.projection_key,
                    webspace_id=context.webspace_id,
                    status="skipped",
                    reason="coalesced",
                )
            )
            continue
        try:
            _set_lifecycle(context=context, status="refreshing", reason="handler_started")
            value = handler(context)
            if inspect.isawaitable(value):
                value = await value
            result = _result_from_handler_output(context, value)
            if isinstance(result.record, Mapping):
                write_projection_record_if_valid(result.record)
            refreshed.append(result)
            _inc_stat("refreshed_total")
            _set_lifecycle(context=context, status=str(result.status or "ready"), reason=result.reason)
        except Exception as exc:
            _inc_stat("error_total")
            text = f"{type(exc).__name__}: {exc}"
            result = ProjectionRefreshResult(
                projection_key=context.projection_key,
                webspace_id=context.webspace_id,
                status="error",
                reason=text,
            )
            errors.append(result)
            _set_lifecycle(context=context, status="error", reason="handler_error", error=text)
        finally:
            _end_refresh(context)
    return ProjectionDispatchReport(
        event_type=envelope.type,
        webspace_ids=target_webspaces,
        selected=selected,
        refreshed=tuple(refreshed),
        skipped=tuple(skipped),
        errors=tuple(errors),
        started_at=started_at,
        finished_at=time.time(),
    )


__all__ = [
    "ProjectionDispatchReport",
    "ProjectionRefreshContext",
    "ProjectionRefreshHandler",
    "ProjectionRefreshResult",
    "clear_projection_dispatcher",
    "core_skill_refresh_contract_snapshot",
    "demanded_projection_refresh_contexts",
    "dispatch_demanded_projection_refresh",
    "projection_dispatcher_memory_contract_snapshot",
    "projection_dispatcher_snapshot",
    "register_projection_refresh_handler",
    "registered_projection_refresh_handlers",
    "unregister_projection_refresh_handler",
]
