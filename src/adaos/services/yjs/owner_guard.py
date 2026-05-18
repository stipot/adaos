from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import suppress
from typing import Any, Mapping, Sequence

_log = logging.getLogger("adaos.yjs.owner_guard")


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(float(minimum), float(str(os.getenv(name) or str(default)).strip()))
    except Exception:
        return max(float(minimum), float(default))


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(minimum), int(str(os.getenv(name) or str(default)).strip()))
    except Exception:
        return max(int(minimum), int(default))


_ENABLED = _bool_env("ADAOS_YJS_OWNER_GUARD_ENABLE", True)
_QUARANTINE_ENABLE = _bool_env("ADAOS_YJS_OWNER_QUARANTINE_ENABLE", True)
_QUARANTINE_TTL_S = _float_env("ADAOS_YJS_OWNER_QUARANTINE_TTL_S", 300.0, 1.0)
_QUARANTINE_MAX_TTL_S = _float_env("ADAOS_YJS_OWNER_QUARANTINE_MAX_TTL_S", 1800.0, 1.0)
_QUARANTINE_ESCALATION_WINDOW_S = _float_env("ADAOS_YJS_OWNER_QUARANTINE_ESCALATION_WINDOW_S", 3600.0, 1.0)
_THROTTLE_STREAK_LIMIT = _int_env("ADAOS_YJS_OWNER_QUARANTINE_THROTTLE_STREAK", 8, 1)

_LOCK = threading.RLock()
_DECISIONS: dict[str, dict[str, Any]] = {}
_QUARANTINES: dict[str, dict[str, Any]] = {}
_QUARANTINE_INCIDENTS: dict[str, dict[str, Any]] = {}
_QUARANTINE_TOTAL = 0
_DENIED_TOTAL = 0
_SERVICE_NODE_NAME = "yjs_qrnt"


def _normalize_webspace(webspace_id: Any) -> str:
    token = str(webspace_id or "").strip()
    try:
        from adaos.services.yjs.webspace import coerce_webspace_id

        return coerce_webspace_id(token or None, fallback=_default_webspace_id())
    except Exception:
        return token or _default_webspace_id()


def _default_webspace_id() -> str:
    try:
        from adaos.services.yjs.webspace import default_webspace_id

        return str(default_webspace_id() or "").strip() or "default"
    except Exception:
        return "default"


def _normalize_owner(owner: Any) -> str:
    token = str(owner or "").strip()
    lower = token.lower()
    if lower.startswith("_by_owner/skill_"):
        return f"skill:{token[len('_by_owner/skill_'):]}"
    if lower.startswith("_by_owner/sdk_"):
        return f"sdk:{token[len('_by_owner/sdk_'):]}"
    return token


def _normalize_roots(root_names: Sequence[str] | None) -> list[str]:
    result: list[str] = []
    for raw in list(root_names or ()):
        token = str(raw or "").strip()
        if token and token not in result:
            result.append(token)
    return result


def _stats_key(webspace_id: str, owner: str) -> str:
    return f"{webspace_id}\0{owner}"


def _governs_owner(owner: Any) -> bool:
    if not _ENABLED:
        return False
    token = _normalize_owner(owner).lower()
    if not token:
        return False
    return (
        token.startswith("skill:")
        or token.startswith("sdk:")
        or token.startswith("_by_owner/skill_")
        or token.startswith("_by_owner/sdk_")
    )


def skill_owner(skill_name: Any) -> str:
    token = str(skill_name or "").strip()
    return f"skill:{token}" if token else ""


def _policy_snapshot(*, webspace_id: str, owner: str, root_names: list[str]) -> dict[str, Any]:
    try:
        from adaos.services.yjs.load_mark import yjs_primary_doc_policy_snapshot

        payload = yjs_primary_doc_policy_snapshot(
            webspace_id=webspace_id,
            owner=owner,
            root_names=root_names,
        )
        if isinstance(payload, dict):
            return payload
    except Exception:
        _log.debug(
            "failed to evaluate YJS owner guard policy webspace=%s owner=%s roots=%s",
            webspace_id,
            owner,
            ",".join(root_names) or "-",
            exc_info=True,
        )
    return {"policy_state": "ok", "reason": "policy_unavailable"}


def _active_quarantine_locked(key: str, now: float) -> dict[str, Any] | None:
    row = dict(_QUARANTINES.get(key) or {})
    until = float(row.get("quarantine_until") or 0.0)
    if until > now:
        row["retry_after_s"] = max(0.0, until - now)
        return row
    if row:
        _QUARANTINES.pop(key, None)
    return None


def _next_quarantine_ttl_locked(key: str, *, now: float, requested_ttl_s: float | None) -> tuple[float, int]:
    base_ttl = max(1.0, float(requested_ttl_s if requested_ttl_s is not None else _QUARANTINE_TTL_S))
    incident = dict(_QUARANTINE_INCIDENTS.get(key) or {})
    last_at = float(incident.get("last_at") or 0.0)
    incident_count = int(incident.get("incident_count") or 0)
    if last_at <= 0.0 or now - last_at > _QUARANTINE_ESCALATION_WINDOW_S:
        incident_count = 0
    incident_count += 1
    _QUARANTINE_INCIDENTS[key] = {
        "incident_count": incident_count,
        "last_at": now,
        "base_ttl_s": base_ttl,
    }
    if requested_ttl_s is not None:
        return base_ttl, incident_count
    multiplier = 2 ** max(0, incident_count - 1)
    return min(_QUARANTINE_MAX_TTL_S, base_ttl * multiplier), incident_count


def _quarantine_public_row(row: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    owner = str(row.get("owner") or "").strip()
    skill = owner.split(":", 1)[1] if owner.startswith("skill:") else ""
    until = float(row.get("quarantine_until") or 0.0)
    return {
        "webspace_id": str(row.get("webspace_id") or "").strip() or None,
        "owner": owner or None,
        "skill": skill or None,
        "status": "quarantined",
        "reason": str(row.get("reason") or "").strip() or "write_amplification",
        "policy_state": str(row.get("policy_state") or "").strip() or "block",
        "trigger": str(row.get("trigger") or "").strip() or None,
        "retry_after_s": max(0.0, until - now),
        "quarantine_until": until or None,
        "updated_at": float(row.get("updated_at") or 0.0) or None,
        "path": str(row.get("path") or "").strip() or None,
        "source": str(row.get("source") or "").strip() or None,
        "channel": str(row.get("channel") or "").strip() or None,
        "work_kind": str(row.get("work_kind") or "").strip() or None,
        "tool": str(row.get("tool") or "").strip() or None,
        "incident_count": int(row.get("incident_count") or 0) or None,
        "root_names": list(row.get("root_names") or []),
    }


def _service_node_payload_locked(*, webspace_id: str, now: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_owner: dict[str, dict[str, Any]] = {}
    by_skill: dict[str, dict[str, Any]] = {}
    for key in list(_QUARANTINES):
        row = _active_quarantine_locked(key, now)
        if not row or str(row.get("webspace_id") or "") != webspace_id:
            continue
        public = _quarantine_public_row(row, now=now)
        rows.append(public)
        owner = str(public.get("owner") or "").strip()
        skill = str(public.get("skill") or "").strip()
        if owner:
            by_owner[owner] = public
        if skill:
            by_skill[skill] = public
    rows.sort(key=lambda item: float(item.get("quarantine_until") or 0.0), reverse=True)
    return {
        "schema": "adaos.yjs_quarantine.v1",
        "status": "active" if rows else "idle",
        "updated_at": now,
        "webspace_id": webspace_id,
        "items": rows,
        "by_owner": by_owner,
        "by_skill": by_skill,
        "count": len(rows),
        "quarantine_total": int(_QUARANTINE_TOTAL),
        "denied_total": int(_DENIED_TOTAL),
    }


def _publish_quarantine_service_node(webspace_id: str) -> None:
    if not webspace_id:
        return
    now = time.time()
    with _LOCK:
        payload = _service_node_payload_locked(webspace_id=webspace_id, now=now)

    def _mutator(doc: Any, txn: Any) -> None:
        root = doc.get_map("data")
        root.set(txn, _SERVICE_NODE_NAME, payload)

    try:
        from adaos.services.yjs.doc import mutate_live_room

        mutate_live_room(
            webspace_id,
            _mutator,
            root_names=["data"],
            source="yjs.owner_guard",
            owner="core",
            channel="core.yjs.quarantine.service_node",
            governed=True,
        )
    except Exception:
        _log.debug("failed to publish YJS quarantine service node webspace=%s", webspace_id, exc_info=True)


def _publish_many_service_nodes(webspace_ids: set[str]) -> None:
    for webspace_id in sorted(webspace_ids):
        with suppress(Exception):
            _publish_quarantine_service_node(webspace_id)


def _record_decision_locked(
    *,
    key: str,
    webspace_id: str,
    owner: str,
    root_names: list[str],
    path: str,
    source: str,
    channel: str,
    work_kind: str,
    tool: str,
    policy: Mapping[str, Any],
    decision: str,
    now: float,
) -> dict[str, Any]:
    current = dict(_DECISIONS.get(key) or {})
    current["webspace_id"] = webspace_id
    current["owner"] = owner
    current["attempted_total"] = int(current.get("attempted_total") or 0) + 1
    current["last_decision"] = decision
    current["last_policy_state"] = str(policy.get("policy_state") or "ok").strip().lower() or "ok"
    current["last_reason"] = str(policy.get("reason") or "").strip() or None
    current["last_path"] = str(path or "").strip() or None
    current["last_source"] = str(source or "").strip() or None
    current["last_channel"] = str(channel or "").strip() or None
    current["last_work_kind"] = str(work_kind or "").strip() or None
    current["last_tool"] = str(tool or "").strip() or None
    current["last_roots"] = list(root_names)
    current["last_at"] = now
    if decision == "deny":
        current["denied_total"] = int(current.get("denied_total") or 0) + 1
    elif decision == "block":
        current["block_seen_total"] = int(current.get("block_seen_total") or 0) + 1
        current["block_streak"] = int(current.get("block_streak") or 0) + 1
        current["throttle_streak"] = 0
    elif decision == "throttle":
        current["throttle_seen_total"] = int(current.get("throttle_seen_total") or 0) + 1
        current["throttle_streak"] = int(current.get("throttle_streak") or 0) + 1
        current["block_streak"] = 0
    else:
        current["allowed_total"] = int(current.get("allowed_total") or 0) + 1
        current["throttle_streak"] = 0
        current["block_streak"] = 0
    _DECISIONS[key] = current
    return dict(current)


def _activate_quarantine_locked(
    *,
    key: str,
    webspace_id: str,
    owner: str,
    root_names: list[str],
    path: str,
    source: str,
    channel: str,
    work_kind: str,
    tool: str,
    policy: Mapping[str, Any],
    trigger: str,
    now: float,
    ttl_s: float | None = None,
) -> dict[str, Any]:
    global _QUARANTINE_TOTAL

    ttl, incident_count = _next_quarantine_ttl_locked(key, now=now, requested_ttl_s=ttl_s)
    until = now + ttl
    previous = dict(_QUARANTINES.get(key) or {})
    was_active = float(previous.get("quarantine_until") or 0.0) > now
    row = {
        "webspace_id": webspace_id,
        "owner": owner,
        "quarantined": True,
        "quarantine_until": until,
        "quarantine_ttl_s": ttl,
        "retry_after_s": ttl,
        "incident_count": incident_count,
        "reason": str(policy.get("reason") or "write_amplification").strip() or "write_amplification",
        "policy_state": str(policy.get("policy_state") or "block").strip().lower() or "block",
        "trigger": str(trigger or "pressure").strip() or "pressure",
        "path": str(path or "").strip() or None,
        "source": str(source or "").strip() or None,
        "channel": str(channel or "").strip() or None,
        "work_kind": str(work_kind or "").strip() or None,
        "tool": str(tool or "").strip() or None,
        "root_names": list(root_names),
        "updated_at": now,
    }
    _QUARANTINES[key] = row
    if not was_active:
        _QUARANTINE_TOTAL += 1
        _log.warning(
            "YJS owner quarantined webspace=%s owner=%s trigger=%s ttl=%.1fs incident=%s reason=%s path=%s tool=%s",
            webspace_id,
            owner,
            row["trigger"],
            ttl,
            incident_count,
            row["reason"],
            row.get("path") or "-",
            row.get("tool") or "-",
        )
    return dict(row)


def quarantine_owner(
    *,
    webspace_id: str | None,
    owner: str | None,
    root_names: Sequence[str] | None = None,
    path: str = "",
    source: str = "",
    channel: str = "",
    work_kind: str = "",
    tool: str = "",
    policy: Mapping[str, Any] | None = None,
    trigger: str = "manual",
    ttl_s: float | None = None,
) -> dict[str, Any]:
    token_owner = _normalize_owner(owner)
    if not _governs_owner(token_owner) or not _QUARANTINE_ENABLE:
        return {"active": False, "allowed": True, "governed": _governs_owner(token_owner)}
    token_ws = _normalize_webspace(webspace_id)
    roots = _normalize_roots(root_names)
    payload = dict(policy or {"policy_state": "block", "reason": trigger})
    key = _stats_key(token_ws, token_owner)
    now = time.time()
    with _LOCK:
        row = _activate_quarantine_locked(
            key=key,
            webspace_id=token_ws,
            owner=token_owner,
            root_names=roots,
            path=path,
            source=source,
            channel=channel,
            work_kind=work_kind,
            tool=tool,
            policy=payload,
            trigger=trigger,
            now=now,
            ttl_s=ttl_s,
        )
    _publish_quarantine_service_node(token_ws)
    return {"active": True, "allowed": False, **row}


def get_owner_quarantine(*, webspace_id: str | None, owner: str | None) -> dict[str, Any] | None:
    token_owner = _normalize_owner(owner)
    if not _governs_owner(token_owner):
        return None
    token_ws = _normalize_webspace(webspace_id)
    now = time.time()
    publish = False
    with _LOCK:
        key = _stats_key(token_ws, token_owner)
        existed = key in _QUARANTINES
        row = _active_quarantine_locked(key, now)
        publish = existed and not row
    if publish:
        _publish_quarantine_service_node(token_ws)
    return row


def admit_owner_work(
    *,
    webspace_id: str | None,
    owner: str | None,
    root_names: Sequence[str] | None = None,
    path: str = "",
    source: str = "",
    channel: str = "",
    work_kind: str = "",
    tool: str = "",
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    global _DENIED_TOTAL

    token_owner = _normalize_owner(owner)
    governed = _governs_owner(token_owner)
    token_ws = _normalize_webspace(webspace_id)
    roots = _normalize_roots(root_names)
    if not governed:
        return {"allowed": True, "governed": False, "webspace_id": token_ws, "owner": token_owner or None}

    payload = dict(policy or _policy_snapshot(webspace_id=token_ws, owner=token_owner, root_names=roots))
    policy_state = str(payload.get("policy_state") or "ok").strip().lower() or "ok"
    key = _stats_key(token_ws, token_owner)
    now = time.time()
    result: dict[str, Any] | None = None
    publish_ws: str | None = None
    with _LOCK:
        existed = key in _QUARANTINES
        active = _active_quarantine_locked(key, now)
        if existed and not active:
            publish_ws = token_ws
        if active:
            _DENIED_TOTAL += 1
            _record_decision_locked(
                key=key,
                webspace_id=token_ws,
                owner=token_owner,
                root_names=roots,
                path=path,
                source=source,
                channel=channel,
                work_kind=work_kind,
                tool=tool,
                policy={"policy_state": "block", "reason": active.get("reason") or "owner_quarantined"},
                decision="deny",
                now=now,
            )
            result = {
                "allowed": False,
                "governed": True,
                "quarantined": True,
                "webspace_id": token_ws,
                "owner": token_owner,
                "reason": active.get("reason") or "owner_quarantined",
                "policy_state": active.get("policy_state") or "block",
                "retry_after_s": active.get("retry_after_s") or 0.0,
                "quarantine": active,
            }

        elif policy_state == "block":
            stats = _record_decision_locked(
                key=key,
                webspace_id=token_ws,
                owner=token_owner,
                root_names=roots,
                path=path,
                source=source,
                channel=channel,
                work_kind=work_kind,
                tool=tool,
                policy=payload,
                decision="block",
                now=now,
            )
            row = _activate_quarantine_locked(
                key=key,
                webspace_id=token_ws,
                owner=token_owner,
                root_names=roots,
                path=path,
                source=source,
                channel=channel,
                work_kind=work_kind,
                tool=tool,
                policy=payload,
                trigger="policy_block",
                now=now,
            )
            publish_ws = token_ws
            _DENIED_TOTAL += 1
            result = {
                "allowed": False,
                "governed": True,
                "quarantined": True,
                "webspace_id": token_ws,
                "owner": token_owner,
                "reason": row.get("reason") or stats.get("last_reason") or "policy_block",
                "policy_state": policy_state,
                "retry_after_s": row.get("retry_after_s") or _QUARANTINE_TTL_S,
                "quarantine": row,
            }

        elif policy_state == "throttle":
            stats = _record_decision_locked(
                key=key,
                webspace_id=token_ws,
                owner=token_owner,
                root_names=roots,
                path=path,
                source=source,
                channel=channel,
                work_kind=work_kind,
                tool=tool,
                policy=payload,
                decision="throttle",
                now=now,
            )
            observed_state = str(payload.get("observed_state") or "").strip().lower()
            if _QUARANTINE_ENABLE and int(stats.get("throttle_streak") or 0) >= _THROTTLE_STREAK_LIMIT and observed_state in {"high", "critical"}:
                row = _activate_quarantine_locked(
                    key=key,
                    webspace_id=token_ws,
                    owner=token_owner,
                    root_names=roots,
                    path=path,
                    source=source,
                    channel=channel,
                    work_kind=work_kind,
                    tool=tool,
                    policy=payload,
                    trigger="throttle_streak",
                    now=now,
                )
                publish_ws = token_ws
                _DENIED_TOTAL += 1
                result = {
                    "allowed": False,
                    "governed": True,
                    "quarantined": True,
                    "webspace_id": token_ws,
                    "owner": token_owner,
                    "reason": row.get("reason") or "throttle_streak",
                    "policy_state": policy_state,
                    "retry_after_s": row.get("retry_after_s") or _QUARANTINE_TTL_S,
                    "quarantine": row,
                }
            else:
                result = {
                    "allowed": True,
                    "governed": True,
                    "throttled": True,
                    "webspace_id": token_ws,
                    "owner": token_owner,
                    "reason": str(payload.get("reason") or "write_pressure_warning").strip() or "write_pressure_warning",
                    "policy_state": policy_state,
                }

        else:
            _record_decision_locked(
                key=key,
                webspace_id=token_ws,
                owner=token_owner,
                root_names=roots,
                path=path,
                source=source,
                channel=channel,
                work_kind=work_kind,
                tool=tool,
                policy=payload,
                decision="allow",
                now=now,
            )
            result = {
                "allowed": True,
                "governed": True,
                "webspace_id": token_ws,
                "owner": token_owner,
                "policy_state": policy_state,
                "reason": str(payload.get("reason") or "healthy").strip() or "healthy",
            }

    if publish_ws:
        _publish_quarantine_service_node(publish_ws)
    return result or {
        "allowed": True,
        "governed": True,
        "webspace_id": token_ws,
        "owner": token_owner,
        "policy_state": policy_state,
        "reason": str(payload.get("reason") or "healthy").strip() or "healthy",
    }


def admit_skill_tool(
    *,
    skill_name: str,
    tool: str,
    payload: Mapping[str, Any] | None,
    read_only: bool = False,
    root_names: Sequence[str] | None = None,
    path: str = "",
) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = str(body.get("webspace_id") or "").strip() or _default_webspace_id()
    owner = skill_owner(skill_name)
    token_ws = _normalize_webspace(webspace_id)
    token_owner = _normalize_owner(owner)
    governed = _governs_owner(token_owner)
    if read_only:
        return {
            "allowed": True,
            "governed": governed,
            "read_only": True,
            "webspace_id": token_ws,
            "owner": token_owner or None,
            "policy_state": "read_only",
            "reason": "read_only_tool",
            "tool": f"{skill_name}:{tool}",
        }
    return admit_owner_work(
        webspace_id=token_ws,
        owner=owner,
        root_names=root_names or [],
        path=path,
        source="skill_manager",
        channel="skill.tool",
        work_kind="skill_tool",
        tool=f"{skill_name}:{tool}",
    )


def owner_guard_snapshot(*, webspace_id: str | None = None, owner: str | None = None) -> dict[str, Any]:
    token_ws = _normalize_webspace(webspace_id) if webspace_id is not None else ""
    token_owner = _normalize_owner(owner)
    now = time.time()
    publish_ws: set[str] = set()
    with _LOCK:
        keys = list(_QUARANTINES)
        for key in keys:
            before = dict(_QUARANTINES.get(key) or {})
            row = _active_quarantine_locked(key, now)
            if before and not row:
                ws = str(before.get("webspace_id") or "").strip()
                if ws:
                    publish_ws.add(ws)
        rows = [dict(item) for item in _DECISIONS.values()]
        quarantines = [dict(item) for item in _QUARANTINES.values()]
    if publish_ws:
        _publish_many_service_nodes(publish_ws)

    if token_ws:
        rows = [row for row in rows if str(row.get("webspace_id") or "") == token_ws]
        quarantines = [row for row in quarantines if str(row.get("webspace_id") or "") == token_ws]
    if token_owner:
        rows = [row for row in rows if str(row.get("owner") or "") == token_owner]
        quarantines = [row for row in quarantines if str(row.get("owner") or "") == token_owner]
    rows.sort(key=lambda item: float(item.get("last_at") or 0.0), reverse=True)
    quarantines.sort(key=lambda item: float(item.get("quarantine_until") or 0.0), reverse=True)
    active = quarantines[0] if quarantines else {}
    remaining_s = max(0.0, float(active.get("quarantine_until") or 0.0) - now) if active else 0.0
    return {
        "enabled": bool(_ENABLED),
        "quarantine_enabled": bool(_QUARANTINE_ENABLE),
        "active": bool(active),
        "webspace_id": str(active.get("webspace_id") or token_ws or "").strip() or None,
        "owner": str(active.get("owner") or token_owner or "").strip() or None,
        "quarantine_total": int(_QUARANTINE_TOTAL),
        "denied_total": int(_DENIED_TOTAL),
        "quarantine_remaining_s": remaining_s or None,
        "quarantine_until": float(active.get("quarantine_until") or 0.0) or None,
        "quarantine_reason": str(active.get("reason") or "").strip() or None,
        "quarantine_trigger": str(active.get("trigger") or "").strip() or None,
        "quarantine_path": str(active.get("path") or "").strip() or None,
        "quarantine_tool": str(active.get("tool") or "").strip() or None,
        "quarantine_incident_count": int(active.get("incident_count") or 0) or None,
        "active_quarantines": quarantines[:20],
        "rows": rows[:50],
    }


__all__ = [
    "admit_owner_work",
    "admit_skill_tool",
    "get_owner_quarantine",
    "owner_guard_snapshot",
    "quarantine_owner",
    "skill_owner",
]
