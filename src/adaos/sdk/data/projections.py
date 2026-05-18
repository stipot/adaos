"""Shared projection runtime helpers for browser-facing skills.

The module is intentionally SDK-local and import-light. Runtime services are
resolved lazily through ctx_subnet only when a projection write is applied.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from typing import Any, Callable, Iterable, Mapping


BuildFn = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ProjectionSlot:
    """Declarative browser-facing Yjs projection slot."""

    name: str
    yjs_path: str | None = None
    build: BuildFn | None = None
    events: tuple[str, ...] = ()
    scope: str = "webspace"
    audience: str = "shared"
    min_interval_s: float = 0.0


@dataclass(frozen=True, slots=True)
class StreamReceiver:
    """Declarative volatile stream receiver."""

    name: str
    build: BuildFn | None = None
    min_interval_s: float = 0.0
    audience: str = "shared"


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    """Small context object passed to projection/stream builders."""

    skill_id: str
    webspace_id: str | None = None
    receiver: str | None = None
    event_topic: str | None = None
    node_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionWriteResult:
    """Result of one set-if-changed projection attempt."""

    skill_id: str
    slot: str
    webspace_id: str
    fingerprint: str
    written: bool
    skipped: bool
    reason: str
    force: bool = False
    throttled: bool = False
    pressure_blocked: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "slot": self.slot,
            "webspace_id": self.webspace_id,
            "fingerprint": self.fingerprint,
            "written": self.written,
            "skipped": self.skipped,
            "reason": self.reason,
            "force": self.force,
            "throttled": self.throttled,
            "pressure_blocked": self.pressure_blocked,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ProjectionRefreshResult:
    """Result of one dirty-section refresh attempt."""

    skill_id: str
    webspace_id: str
    sections: tuple[str, ...]
    results: tuple[ProjectionWriteResult, ...]
    coalesced: bool = False
    reason: str = "refreshed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "webspace_id": self.webspace_id,
            "sections": list(self.sections),
            "results": [result.as_dict() for result in self.results],
            "coalesced": self.coalesced,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StreamPublishResult:
    """Result of one volatile stream publish attempt."""

    skill_id: str
    receiver: str
    webspace_id: str
    fingerprint: str
    published: bool
    skipped: bool
    reason: str
    rate_limited: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "receiver": self.receiver,
            "webspace_id": self.webspace_id,
            "fingerprint": self.fingerprint,
            "published": self.published,
            "skipped": self.skipped,
            "reason": self.reason,
            "rate_limited": self.rate_limited,
            "error": self.error,
        }


@dataclass(slots=True)
class ProjectionDiagnostics:
    applied_total: int = 0
    skipped_unchanged_total: int = 0
    throttled_total: int = 0
    pressure_blocked_total: int = 0
    errored_total: int = 0
    last_result: ProjectionWriteResult | None = None
    by_slot: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_started_total: int = 0
    refresh_coalesced_total: int = 0

    def record(self, result: ProjectionWriteResult) -> None:
        self.last_result = result
        slot_state = self.by_slot.setdefault(
            result.slot,
            {
                "applied_total": 0,
                "skipped_unchanged_total": 0,
                "throttled_total": 0,
                "pressure_blocked_total": 0,
                "errored_total": 0,
                "last_webspace_id": None,
                "last_reason": None,
                "last_fingerprint": None,
            },
        )
        slot_state["last_webspace_id"] = result.webspace_id
        slot_state["last_reason"] = result.reason
        slot_state["last_fingerprint"] = result.fingerprint
        if result.error:
            self.errored_total += 1
            slot_state["errored_total"] = int(slot_state["errored_total"]) + 1
        elif result.pressure_blocked:
            self.pressure_blocked_total += 1
            slot_state["pressure_blocked_total"] = int(slot_state["pressure_blocked_total"]) + 1
        elif result.throttled:
            self.throttled_total += 1
            slot_state["throttled_total"] = int(slot_state["throttled_total"]) + 1
        elif result.written:
            self.applied_total += 1
            slot_state["applied_total"] = int(slot_state["applied_total"]) + 1
        elif result.skipped:
            self.skipped_unchanged_total += 1
            slot_state["skipped_unchanged_total"] = int(slot_state["skipped_unchanged_total"]) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "applied_total": self.applied_total,
            "skipped_unchanged_total": self.skipped_unchanged_total,
            "throttled_total": self.throttled_total,
            "pressure_blocked_total": self.pressure_blocked_total,
            "errored_total": self.errored_total,
            "last_result": self.last_result.as_dict() if self.last_result else None,
            "by_slot": json.loads(json.dumps(self.by_slot, sort_keys=True)),
            "refresh_started_total": self.refresh_started_total,
            "refresh_coalesced_total": self.refresh_coalesced_total,
        }


@dataclass(slots=True)
class StreamDiagnostics:
    published_total: int = 0
    skipped_unchanged_total: int = 0
    rate_limited_total: int = 0
    errored_total: int = 0
    last_result: StreamPublishResult | None = None
    by_receiver: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, result: StreamPublishResult) -> None:
        self.last_result = result
        receiver_state = self.by_receiver.setdefault(
            result.receiver,
            {
                "published_total": 0,
                "skipped_unchanged_total": 0,
                "rate_limited_total": 0,
                "errored_total": 0,
                "last_webspace_id": None,
                "last_reason": None,
                "last_fingerprint": None,
            },
        )
        receiver_state["last_webspace_id"] = result.webspace_id
        receiver_state["last_reason"] = result.reason
        receiver_state["last_fingerprint"] = result.fingerprint
        if result.error:
            self.errored_total += 1
            receiver_state["errored_total"] = int(receiver_state["errored_total"]) + 1
        elif result.rate_limited:
            self.rate_limited_total += 1
            receiver_state["rate_limited_total"] = int(receiver_state["rate_limited_total"]) + 1
        elif result.published:
            self.published_total += 1
            receiver_state["published_total"] = int(receiver_state["published_total"]) + 1
        elif result.skipped:
            self.skipped_unchanged_total += 1
            receiver_state["skipped_unchanged_total"] = int(receiver_state["skipped_unchanged_total"]) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "published_total": self.published_total,
            "skipped_unchanged_total": self.skipped_unchanged_total,
            "rate_limited_total": self.rate_limited_total,
            "errored_total": self.errored_total,
            "last_result": self.last_result.as_dict() if self.last_result else None,
            "by_receiver": json.loads(json.dumps(self.by_receiver, sort_keys=True)),
        }


@dataclass(slots=True)
class _SectionCacheEntry:
    value: Any
    fingerprint: str
    expires_at: float | None
    stored_at: float


class SectionCache:
    """Bounded TTL cache keyed by webspace and semantic section."""

    def __init__(
        self,
        *,
        default_ttl_s: float = 0.0,
        max_entries: int = 128,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.default_ttl_s = max(0.0, float(default_ttl_s))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.time
        self._items: dict[tuple[str, str], _SectionCacheEntry] = {}
        self._lock = threading.RLock()

    def get(self, section: str, *, webspace_id: str | None = None) -> Any | None:
        key = (_webspace_token(webspace_id), _section_name(section))
        now = self._clock()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= now:
                self._items.pop(key, None)
                return None
            return entry.value

    def set(
        self,
        section: str,
        value: Any,
        *,
        webspace_id: str | None = None,
        ttl_s: float | None = None,
    ) -> Any:
        ttl = self.default_ttl_s if ttl_s is None else max(0.0, float(ttl_s))
        now = self._clock()
        expires_at = now + ttl if ttl > 0.0 else None
        key = (_webspace_token(webspace_id), _section_name(section))
        with self._lock:
            self._items[key] = _SectionCacheEntry(
                value=value,
                fingerprint=stable_payload_fingerprint(value),
                expires_at=expires_at,
                stored_at=now,
            )
            self._trim_locked()
        return value

    def get_or_build(
        self,
        section: str,
        build: Callable[[], Any],
        *,
        webspace_id: str | None = None,
        ttl_s: float | None = None,
    ) -> Any:
        cached = self.get(section, webspace_id=webspace_id)
        if cached is not None:
            return cached
        return self.set(section, build(), webspace_id=webspace_id, ttl_s=ttl_s)

    def invalidate(self, section: str | None = None, *, webspace_id: str | None = None) -> int:
        section_name = _section_name(section) if section is not None else None
        ws_id = _webspace_token(webspace_id) if webspace_id is not None else None
        removed = 0
        with self._lock:
            for key in list(self._items):
                key_ws, key_section = key
                if ws_id is not None and key_ws != ws_id:
                    continue
                if section_name is not None and key_section != section_name:
                    continue
                self._items.pop(key, None)
                removed += 1
        return removed

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            entries = []
            for (webspace_id, section), entry in self._items.items():
                entries.append(
                    {
                        "webspace_id": webspace_id,
                        "section": section,
                        "fingerprint": entry.fingerprint,
                        "stored_at": entry.stored_at,
                        "expires_at": entry.expires_at,
                        "expired": entry.expires_at is not None and entry.expires_at <= now,
                    }
                )
            return {
                "entries": len(entries),
                "max_entries": self.max_entries,
                "default_ttl_s": self.default_ttl_s,
                "items": entries,
            }

    def _trim_locked(self) -> None:
        if len(self._items) <= self.max_entries:
            return
        overflow = len(self._items) - self.max_entries
        ordered = sorted(self._items.items(), key=lambda item: item[1].stored_at)
        for key, _entry in ordered[:overflow]:
            self._items.pop(key, None)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))

    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            raw = to_json()
            if isinstance(raw, str):
                return _json_safe(json.loads(raw))
            return _json_safe(raw)
        except Exception:
            return repr(value)

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=repr))

    items = getattr(value, "items", None)
    if callable(items):
        try:
            return {str(key): _json_safe(item) for key, item in items()}
        except Exception:
            return repr(value)

    return repr(value)


def stable_payload_fingerprint(value: Any) -> str:
    """Return a deterministic SHA-256 fingerprint for JSON-like payloads."""

    normalized = _json_safe(value)
    raw = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DirtyRouter:
    """Maps event topics to dirty projection sections."""

    def __init__(self) -> None:
        self._routes: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def on(self, *patterns: str) -> "_DirtyRouteBuilder":
        return _DirtyRouteBuilder(self, tuple(str(pattern or "").strip() for pattern in patterns if str(pattern or "").strip()))

    def add(self, patterns: Iterable[str], sections: Iterable[str]) -> "DirtyRouter":
        clean_patterns = tuple(str(pattern or "").strip() for pattern in patterns if str(pattern or "").strip())
        clean_sections = tuple(str(section or "").strip() for section in sections if str(section or "").strip())
        if clean_patterns and clean_sections:
            self._routes.append((clean_patterns, clean_sections))
        return self

    def dirty_for(self, topic: str) -> set[str]:
        token = str(topic or "").strip()
        dirty: set[str] = set()
        for patterns, sections in self._routes:
            if any(_topic_matches(pattern, token) for pattern in patterns):
                dirty.update(sections)
        return dirty

    def snapshot(self) -> list[dict[str, list[str]]]:
        return [{"patterns": list(patterns), "sections": list(sections)} for patterns, sections in self._routes]


class _DirtyRouteBuilder:
    def __init__(self, router: DirtyRouter, patterns: tuple[str, ...]) -> None:
        self._router = router
        self._patterns = patterns

    def dirty(self, *sections: str) -> DirtyRouter:
        return self._router.add(self._patterns, sections)


def _topic_matches(pattern: str, topic: str) -> bool:
    if not pattern:
        return False
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith("*"):
        return topic.startswith(pattern[:-1])
    return False


class ProjectionRuntime:
    """Minimal per-skill set-if-changed projection runtime."""

    def __init__(
        self,
        skill_id: str,
        *,
        ctx_subnet: Any | None = None,
        projections: Iterable[ProjectionSlot] | None = None,
        router: DirtyRouter | None = None,
        section_cache: SectionCache | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.skill_id = str(skill_id or "unknown_skill").strip() or "unknown_skill"
        self._ctx_subnet = ctx_subnet
        self._clock = clock or time.monotonic
        self._fingerprints: dict[tuple[str, str], str] = {}
        self._last_write_at: dict[tuple[str, str], float] = {}
        self._pending_refresh: dict[tuple[str, tuple[str, ...]], asyncio.Task[ProjectionRefreshResult]] = {}
        self._projections: dict[str, ProjectionSlot] = {}
        self._router = router or DirtyRouter()
        self.section_cache = section_cache or SectionCache()
        self._diagnostics = ProjectionDiagnostics()
        self._lock = threading.RLock()
        self.register_projections(projections or ())

    def register_projection(self, slot: ProjectionSlot) -> ProjectionSlot:
        slot_name = _slot_name(slot)
        with self._lock:
            self._projections[slot_name] = slot
        return slot

    def register_projections(self, slots: Iterable[ProjectionSlot]) -> None:
        for slot in slots:
            self.register_projection(slot)

    def bind_ctx_subnet(self, ctx_subnet: Any | None) -> "ProjectionRuntime":
        with self._lock:
            self._ctx_subnet = ctx_subnet
        return self

    def dirty_for(self, topic: str) -> set[str]:
        dirty = set(self._router.dirty_for(topic))
        token = str(topic or "").strip()
        with self._lock:
            projections = tuple(self._projections.values())
        for slot in projections:
            if any(_topic_matches(pattern, token) for pattern in slot.events):
                dirty.add(slot.name)
        return dirty

    async def refresh_dirty(
        self,
        topic: str,
        *,
        webspace_id: str | None = None,
        force: bool = False,
        context: ProjectionContext | None = None,
        reason: str | None = None,
    ) -> ProjectionRefreshResult:
        return await self.refresh_sections(
            self.dirty_for(topic),
            webspace_id=webspace_id,
            force=force,
            context=context
            or ProjectionContext(
                skill_id=self.skill_id,
                webspace_id=_webspace_token(webspace_id),
                event_topic=topic,
                reason=reason,
            ),
            reason=reason or topic or "dirty_refresh",
        )

    async def refresh_sections(
        self,
        sections: Iterable[str],
        *,
        webspace_id: str | None = None,
        force: bool = False,
        context: ProjectionContext | None = None,
        reason: str | None = None,
    ) -> ProjectionRefreshResult:
        ws_id = _webspace_token(webspace_id)
        section_names = tuple(sorted({_section_name(section) for section in sections if str(section or "").strip()}))
        if not section_names:
            return ProjectionRefreshResult(
                skill_id=self.skill_id,
                webspace_id=ws_id,
                sections=(),
                results=(),
                reason="no_dirty_sections",
            )

        refresh_context = context or ProjectionContext(skill_id=self.skill_id, webspace_id=ws_id, reason=reason)
        key = (ws_id, section_names)
        reused_task: asyncio.Task[ProjectionRefreshResult] | None = None
        with self._lock:
            current = self._pending_refresh.get(key)
            if current is not None and not current.done():
                self._diagnostics.refresh_coalesced_total += 1
                reused_task = current
            else:
                self._diagnostics.refresh_started_total += 1
                task = asyncio.create_task(
                    self._refresh_sections_now(
                        section_names,
                        webspace_id=ws_id,
                        force=force,
                        context=refresh_context,
                        reason=reason,
                    )
                )
                self._pending_refresh[key] = task

        if reused_task is not None:
            result = await reused_task
            return replace(result, coalesced=True)

        task = self._pending_refresh[key]
        try:
            return await task
        finally:
            with self._lock:
                if self._pending_refresh.get(key) is task:
                    self._pending_refresh.pop(key, None)

    async def _refresh_sections_now(
        self,
        sections: tuple[str, ...],
        *,
        webspace_id: str,
        force: bool,
        context: ProjectionContext,
        reason: str | None,
    ) -> ProjectionRefreshResult:
        results: list[ProjectionWriteResult] = []
        for section in sections:
            slot = self._projections.get(section)
            if slot is None:
                result = ProjectionWriteResult(
                    skill_id=self.skill_id,
                    slot=section,
                    webspace_id=webspace_id,
                    fingerprint="",
                    written=False,
                    skipped=False,
                    reason="projection_slot_missing",
                    force=bool(force),
                    error="projection slot is not registered",
                )
                with self._lock:
                    self._diagnostics.record(result)
                results.append(result)
                continue
            if slot.build is None:
                result = ProjectionWriteResult(
                    skill_id=self.skill_id,
                    slot=section,
                    webspace_id=webspace_id,
                    fingerprint="",
                    written=False,
                    skipped=False,
                    reason="projection_builder_missing",
                    force=bool(force),
                    error="projection slot has no build function",
                )
                with self._lock:
                    self._diagnostics.record(result)
                results.append(result)
                continue
            try:
                value = await _call_build(slot.build, context)
            except Exception as exc:
                result = ProjectionWriteResult(
                    skill_id=self.skill_id,
                    slot=section,
                    webspace_id=webspace_id,
                    fingerprint="",
                    written=False,
                    skipped=False,
                    reason="projection_build_failed",
                    force=bool(force),
                    error=f"{type(exc).__name__}: {exc}",
                )
                with self._lock:
                    self._diagnostics.record(result)
                results.append(result)
                continue
            results.append(
                await self.set_if_changed(
                    slot,
                    value,
                    webspace_id=webspace_id,
                    force=force,
                    reason=reason or "refresh_dirty",
                )
            )
        return ProjectionRefreshResult(
            skill_id=self.skill_id,
            webspace_id=webspace_id,
            sections=sections,
            results=tuple(results),
            reason=reason or "refreshed",
        )

    async def set_if_changed(
        self,
        slot: ProjectionSlot | str,
        value: Any,
        *,
        webspace_id: str | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> ProjectionWriteResult:
        slot_name = _slot_name(slot)
        ws_id = str(webspace_id or "default").strip() or "default"
        fingerprint = stable_payload_fingerprint(value)
        key = (ws_id, slot_name)
        slot_decl = slot if isinstance(slot, ProjectionSlot) else self._projections.get(slot_name)

        with self._lock:
            previous = self._fingerprints.get(key)
            if previous == fingerprint:
                result = ProjectionWriteResult(
                    skill_id=self.skill_id,
                    slot=slot_name,
                    webspace_id=ws_id,
                    fingerprint=fingerprint,
                    written=False,
                    skipped=True,
                    reason="unchanged",
                    force=bool(force),
                )
                self._diagnostics.record(result)
                return result
            min_interval_s = max(0.0, float(getattr(slot_decl, "min_interval_s", 0.0) or 0.0))
            last_write_at = float(self._last_write_at.get(key) or 0.0)
            now = self._clock()
            if min_interval_s > 0.0 and last_write_at > 0.0 and now - last_write_at < min_interval_s and not force:
                result = ProjectionWriteResult(
                    skill_id=self.skill_id,
                    slot=slot_name,
                    webspace_id=ws_id,
                    fingerprint=fingerprint,
                    written=False,
                    skipped=True,
                    reason="rate_limited",
                    throttled=True,
                    force=bool(force),
                )
                self._diagnostics.record(result)
                return result

        try:
            setter = self._ctx_subnet or _default_ctx_subnet()
            await setter.set_async(slot_name, value, webspace_id=ws_id)
        except Exception as exc:
            result = ProjectionWriteResult(
                skill_id=self.skill_id,
                slot=slot_name,
                webspace_id=ws_id,
                fingerprint=fingerprint,
                written=False,
                skipped=False,
                reason=str(reason or "write_failed"),
                force=bool(force),
                error=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                self._diagnostics.record(result)
            return result

        result = ProjectionWriteResult(
            skill_id=self.skill_id,
            slot=slot_name,
            webspace_id=ws_id,
            fingerprint=fingerprint,
            written=True,
            skipped=False,
            reason=str(reason or ("force_recomputed" if force else "changed")),
            force=bool(force),
        )
        with self._lock:
            self._fingerprints[key] = fingerprint
            self._last_write_at[key] = self._clock()
            self._diagnostics.record(result)
        return result

    def set_if_changed_sync(
        self,
        slot: ProjectionSlot | str,
        value: Any,
        *,
        webspace_id: str | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> ProjectionWriteResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.set_if_changed(slot, value, webspace_id=webspace_id, force=force, reason=reason)
            )
        raise RuntimeError("ProjectionRuntime.set_if_changed_sync cannot run inside an active event loop")

    def reset(
        self,
        *,
        webspace_id: str | None = None,
        slot: ProjectionSlot | str | None = None,
    ) -> None:
        slot_name = _slot_name(slot) if slot is not None else None
        ws_id = str(webspace_id or "").strip() or None
        with self._lock:
            if ws_id is None and slot_name is None:
                self._fingerprints.clear()
                self._last_write_at.clear()
                self._diagnostics = ProjectionDiagnostics()
                return
            for key in set(self._fingerprints) | set(self._last_write_at):
                key_ws, key_slot = key
                if ws_id is not None and key_ws != ws_id:
                    continue
                if slot_name is not None and key_slot != slot_name:
                    continue
                self._fingerprints.pop(key, None)
                self._last_write_at.pop(key, None)

    def diagnostics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = self._diagnostics.snapshot()
            payload["skill_id"] = self.skill_id
            payload["fingerprint_entries"] = len(self._fingerprints)
            payload["last_write_entries"] = len(self._last_write_at)
            payload["pending_refresh_entries"] = sum(1 for task in self._pending_refresh.values() if not task.done())
            payload["registered_projections"] = sorted(self._projections)
            payload["dirty_routes"] = self._router.snapshot()
            payload["ts"] = time.time()
            return payload


class StreamRuntime:
    """Minimal volatile stream runtime for browser-facing receivers."""

    def __init__(
        self,
        skill_id: str,
        *,
        receivers: Iterable[StreamReceiver] | None = None,
        stream_publish: Callable[..., Mapping[str, Any]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.skill_id = str(skill_id or "unknown_skill").strip() or "unknown_skill"
        self._stream_publish = stream_publish
        self._clock = clock or time.time
        self._receivers: dict[str, StreamReceiver] = {}
        self._active_receivers: set[tuple[str, str]] = set()
        self._fingerprints: dict[tuple[str, str], str] = {}
        self._last_publish_at: dict[tuple[str, str], float] = {}
        self._diagnostics = StreamDiagnostics()
        self._lock = threading.RLock()
        for receiver in receivers or ():
            self.register_receiver(receiver)

    def register_receiver(self, receiver: StreamReceiver) -> StreamReceiver:
        name = _receiver_name(receiver)
        with self._lock:
            self._receivers[name] = receiver
        return receiver

    def remember_receiver(self, receiver: StreamReceiver | str, *, webspace_id: str | None = None) -> None:
        key = (_webspace_token(webspace_id), _receiver_name(receiver))
        with self._lock:
            self._active_receivers.add(key)

    def forget_receiver(self, receiver: StreamReceiver | str, *, webspace_id: str | None = None) -> None:
        key = (_webspace_token(webspace_id), _receiver_name(receiver))
        with self._lock:
            self._active_receivers.discard(key)
            self._fingerprints.pop(key, None)
            self._last_publish_at.pop(key, None)

    def active_receivers_snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"webspace_id": webspace_id, "receiver": receiver}
                for webspace_id, receiver in sorted(self._active_receivers)
            ]

    def reset(
        self,
        *,
        webspace_id: str | None = None,
        receiver: StreamReceiver | str | None = None,
        forget_active: bool = True,
    ) -> None:
        ws_id = _webspace_token(webspace_id) if webspace_id is not None else None
        receiver_name = _receiver_name(receiver) if receiver is not None else None
        with self._lock:
            for key in set(self._fingerprints) | set(self._last_publish_at) | set(self._active_receivers):
                key_ws, key_receiver = key
                if ws_id is not None and key_ws != ws_id:
                    continue
                if receiver_name is not None and key_receiver != receiver_name:
                    continue
                self._fingerprints.pop(key, None)
                self._last_publish_at.pop(key, None)
                if forget_active:
                    self._active_receivers.discard(key)
            if ws_id is None and receiver_name is None:
                self._diagnostics = StreamDiagnostics()

    def publish_snapshot(
        self,
        receiver: StreamReceiver | str,
        data: Any,
        *,
        webspace_id: str | None = None,
        force: bool = False,
        ts: float | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> StreamPublishResult:
        receiver_name = _receiver_name(receiver)
        ws_id = _webspace_token(webspace_id)
        key = (ws_id, receiver_name)
        fingerprint = stable_payload_fingerprint(data)
        now = self._clock()
        with self._lock:
            registered = self._receivers.get(receiver_name)
            min_interval_s = float(getattr(registered, "min_interval_s", 0.0) or 0.0)
            previous = self._fingerprints.get(key)
            if previous == fingerprint and not force:
                result = StreamPublishResult(
                    skill_id=self.skill_id,
                    receiver=receiver_name,
                    webspace_id=ws_id,
                    fingerprint=fingerprint,
                    published=False,
                    skipped=True,
                    reason="unchanged",
                )
                self._diagnostics.record(result)
                return result
            last_at = float(self._last_publish_at.get(key) or 0.0)
            if min_interval_s > 0.0 and last_at > 0.0 and now - last_at < min_interval_s and not force:
                result = StreamPublishResult(
                    skill_id=self.skill_id,
                    receiver=receiver_name,
                    webspace_id=ws_id,
                    fingerprint=fingerprint,
                    published=False,
                    skipped=True,
                    reason="rate_limited",
                    rate_limited=True,
                )
                self._diagnostics.record(result)
                return result

        try:
            publisher = self._stream_publish or _default_stream_publish()
            effective_meta = dict(meta or {})
            effective_meta.setdefault("webspace_id", ws_id)
            publish_result = publisher(receiver_name, data, ts=ts, _meta=effective_meta)
            if isinstance(publish_result, Mapping) and publish_result.get("ok") is False:
                raise RuntimeError("stream publish returned ok=false")
        except Exception as exc:
            result = StreamPublishResult(
                skill_id=self.skill_id,
                receiver=receiver_name,
                webspace_id=ws_id,
                fingerprint=fingerprint,
                published=False,
                skipped=False,
                reason="publish_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                self._diagnostics.record(result)
            return result

        result = StreamPublishResult(
            skill_id=self.skill_id,
            receiver=receiver_name,
            webspace_id=ws_id,
            fingerprint=fingerprint,
            published=True,
            skipped=False,
            reason="force" if force else "changed",
        )
        with self._lock:
            self._active_receivers.add(key)
            self._fingerprints[key] = fingerprint
            self._last_publish_at[key] = now
            self._diagnostics.record(result)
        return result

    def diagnostics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = self._diagnostics.snapshot()
            payload["skill_id"] = self.skill_id
            payload["fingerprint_entries"] = len(self._fingerprints)
            payload["active_receivers"] = self.active_receivers_snapshot()
            payload["registered_receivers"] = sorted(self._receivers)
            payload["ts"] = self._clock()
            return payload

    def publish_receiver_snapshot(
        self,
        receiver: StreamReceiver | str,
        *,
        webspace_id: str | None = None,
        force: bool = False,
        context: ProjectionContext | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> StreamPublishResult | None:
        receiver_name = _receiver_name(receiver)
        with self._lock:
            registered = self._receivers.get(receiver_name)
        if registered is None or registered.build is None:
            return None
        ws_id = _webspace_token(webspace_id)
        build_context = context or ProjectionContext(
            skill_id=self.skill_id,
            webspace_id=ws_id,
            receiver=receiver_name,
            reason="stream_snapshot",
        )
        if build_context.receiver != receiver_name or build_context.webspace_id != ws_id:
            build_context = replace(build_context, webspace_id=ws_id, receiver=receiver_name)
        try:
            data = _call_build_sync(registered.build, build_context)
        except Exception as exc:
            result = StreamPublishResult(
                skill_id=self.skill_id,
                receiver=receiver_name,
                webspace_id=ws_id,
                fingerprint="",
                published=False,
                skipped=False,
                reason="stream_build_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                self._diagnostics.record(result)
            return result
        return self.publish_snapshot(receiver_name, data, webspace_id=ws_id, force=force, meta=meta)

    def handle_snapshot_requested(
        self,
        event: Any,
        *,
        receiver_prefix: str | None = None,
        force: bool = True,
    ) -> StreamPublishResult | None:
        payload = _event_payload(event)
        receiver_name = _receiver_from_payload(payload)
        if not receiver_name:
            return None
        if receiver_prefix and not receiver_name.startswith(str(receiver_prefix)):
            return None
        with self._lock:
            if receiver_name not in self._receivers:
                return None
        ws_id = _webspace_from_payload(payload)
        self.remember_receiver(receiver_name, webspace_id=ws_id)
        return self.publish_receiver_snapshot(
            receiver_name,
            webspace_id=ws_id,
            force=force,
            context=ProjectionContext(
                skill_id=self.skill_id,
                webspace_id=ws_id,
                receiver=receiver_name,
                event_topic=_event_topic(event),
                reason="snapshot_requested",
            ),
        )

    def handle_subscription_changed(
        self,
        event: Any,
        *,
        receiver_prefix: str | None = None,
        publish_on_subscribe: bool = True,
    ) -> StreamPublishResult | None:
        payload = _event_payload(event)
        receiver_name = _receiver_from_payload(payload)
        if not receiver_name:
            return None
        if receiver_prefix and not receiver_name.startswith(str(receiver_prefix)):
            return None
        ws_id = _webspace_from_payload(payload)
        action = ""
        if isinstance(payload, Mapping):
            action = str(payload.get("action") or "").strip().lower()
        if action == "unsubscribed":
            self.forget_receiver(receiver_name, webspace_id=ws_id)
            return None
        with self._lock:
            if receiver_name not in self._receivers:
                return None
        self.remember_receiver(receiver_name, webspace_id=ws_id)
        if not publish_on_subscribe:
            return None
        return self.publish_receiver_snapshot(
            receiver_name,
            webspace_id=ws_id,
            force=True,
            context=ProjectionContext(
                skill_id=self.skill_id,
                webspace_id=ws_id,
                receiver=receiver_name,
                event_topic=_event_topic(event),
                reason="subscription_changed",
            ),
        )


def _slot_name(slot: ProjectionSlot | str | None) -> str:
    if isinstance(slot, ProjectionSlot):
        name = slot.name
    else:
        name = str(slot or "")
    token = str(name or "").strip()
    if not token:
        raise ValueError("projection slot name is required")
    return token


def _receiver_name(receiver: StreamReceiver | str | None) -> str:
    if isinstance(receiver, StreamReceiver):
        name = receiver.name
    else:
        name = str(receiver or "")
    token = str(name or "").strip()
    if not token:
        raise ValueError("stream receiver name is required")
    return token


def _section_name(section: str | None) -> str:
    token = str(section or "").strip()
    if not token:
        raise ValueError("section name is required")
    return token


def _webspace_token(webspace_id: str | None) -> str:
    return str(webspace_id or "default").strip() or "default"


def _event_payload(event: Any) -> Any:
    return getattr(event, "payload", event)


def _event_topic(event: Any) -> str:
    for attr in ("topic", "type", "name"):
        token = str(getattr(event, attr, "") or "").strip()
        if token:
            return token
    payload = _event_payload(event)
    if isinstance(payload, Mapping):
        for key in ("topic", "type", "event_type"):
            token = str(payload.get(key) or "").strip()
            if token:
                return token
    return ""


def _receiver_from_payload(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("receiver") or "").strip()


def _webspace_from_payload(payload: Any) -> str:
    if isinstance(payload, Mapping):
        token = str(payload.get("webspace_id") or payload.get("workspace_id") or "").strip()
        if token:
            return token
        meta = payload.get("_meta")
        if isinstance(meta, Mapping):
            token = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
            if token:
                return token
    return "default"


async def _call_build(build: BuildFn, context: ProjectionContext) -> Any:
    try:
        signature = inspect.signature(build)
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
        use_context = bool(required)
    except (TypeError, ValueError):
        use_context = True
    value = build(context) if use_context else build()
    if inspect.isawaitable(value):
        return await value
    return value


def _call_build_sync(build: BuildFn, context: ProjectionContext) -> Any:
    try:
        signature = inspect.signature(build)
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
        use_context = bool(required)
    except (TypeError, ValueError):
        use_context = True
    value = build(context) if use_context else build()
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    close = getattr(value, "close", None)
    if callable(close):
        close()
    raise RuntimeError("stream receiver build returned awaitable inside active event loop")


def _default_ctx_subnet() -> Any:
    from adaos.sdk.data.ctx import subnet

    return subnet


def _default_stream_publish() -> Callable[..., Mapping[str, Any]]:
    from adaos.sdk.io.out import stream_publish

    return stream_publish


_RUNTIMES: dict[str, ProjectionRuntime] = {}
_RUNTIMES_LOCK = threading.RLock()


def get_projection_runtime(skill_id: str, *, ctx_subnet: Any | None = None) -> ProjectionRuntime:
    token = str(skill_id or "unknown_skill").strip() or "unknown_skill"
    if ctx_subnet is not None:
        return ProjectionRuntime(token, ctx_subnet=ctx_subnet)
    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.get(token)
        if runtime is None:
            runtime = ProjectionRuntime(token)
            _RUNTIMES[token] = runtime
        return runtime


async def set_projection_if_changed(
    skill_id: str,
    slot: ProjectionSlot | str,
    value: Any,
    *,
    webspace_id: str | None = None,
    force: bool = False,
    reason: str | None = None,
) -> ProjectionWriteResult:
    runtime = get_projection_runtime(skill_id)
    return await runtime.set_if_changed(slot, value, webspace_id=webspace_id, force=force, reason=reason)


def clear_projection_runtime_state(skill_id: str | None = None) -> None:
    with _RUNTIMES_LOCK:
        if skill_id is None:
            _RUNTIMES.clear()
            return
        _RUNTIMES.pop(str(skill_id or "").strip(), None)


__all__ = [
    "DirtyRouter",
    "ProjectionContext",
    "ProjectionDiagnostics",
    "ProjectionRefreshResult",
    "ProjectionRuntime",
    "ProjectionSlot",
    "ProjectionWriteResult",
    "SectionCache",
    "StreamDiagnostics",
    "StreamPublishResult",
    "StreamReceiver",
    "StreamRuntime",
    "clear_projection_runtime_state",
    "get_projection_runtime",
    "set_projection_if_changed",
    "stable_payload_fingerprint",
]
