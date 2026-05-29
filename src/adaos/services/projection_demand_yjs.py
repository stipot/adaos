from __future__ import annotations

import json
import time
from typing import Any, Iterable, Mapping

from adaos.domain import (
    ClientSubscriptionRecord,
    make_client_subscription_record,
    normalize_client_subscription_record,
    normalize_projection_subscription,
    projection_fingerprint,
)
from adaos.services.projection_demand import (
    list_client_subscription_records,
    projection_demand_snapshot,
    write_client_subscription_record,
)


PROJECTION_DEMAND_YJS_KEY = "projectionDemand"
PROJECTION_DEMAND_YJS_CLIENTS_KEY = "clients"
PROJECTION_DEMAND_YJS_PATH = "runtime/clients"
PROJECTION_DEMAND_YJS_SCHEMA = "adaos.projection-demand.yjs.v1"
PROJECTION_DEMAND_YJS_ENVELOPE_SCHEMA = "adaos.projection-demand.envelope.v1"
PROJECTION_DEMAND_YJS_OWNER = "core:projection_demand"
PROJECTION_DEMAND_YJS_WRITE_POLICY = "full-client-session-set-if-changed"


def async_get_ydoc(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import async_get_ydoc as _async_get_ydoc

    return _async_get_ydoc(*args, **kwargs)


def async_read_ydoc(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import async_read_ydoc as _async_read_ydoc

    return _async_read_ydoc(*args, **kwargs)


def mutate_live_room(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import mutate_live_room as _mutate_live_room

    return _mutate_live_room(*args, **kwargs)


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _webspace_token(value: Any = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    try:
        from adaos.services.yjs.webspace import default_webspace_id

        return default_webspace_id()
    except Exception:
        return "default"


def _client_session_key(record: ClientSubscriptionRecord) -> str:
    return "\0".join([record.client_id, record.session_id])


def _clients_payload(records: Iterable[ClientSubscriptionRecord]) -> dict[str, Any]:
    clients: dict[str, dict[str, Any]] = {}
    for record in records:
        clients.setdefault(record.client_id, {})[record.session_id] = record.to_dict()
    return clients


def _projection_demand_yjs_envelope(
    *,
    webspace_id: str,
    client_total: int,
    projection_total: int,
    consumer_total: int,
) -> dict[str, Any]:
    return {
        "schema": PROJECTION_DEMAND_YJS_ENVELOPE_SCHEMA,
        "owner": PROJECTION_DEMAND_YJS_OWNER,
        "write_policy": PROJECTION_DEMAND_YJS_WRITE_POLICY,
        "source_of_truth": "runtime.clients full session records",
        "webspace_id": webspace_id,
        "yjs_path": PROJECTION_DEMAND_YJS_PATH,
        "client_total": int(client_total),
        "projection_total": int(projection_total),
        "consumer_total": int(consumer_total),
        "boundaries": {
            "browser_may_write_full_session_record": True,
            "browser_may_patch_projection_cache": False,
            "core_restores_memory_from_yjs": True,
            "restore_writes_projection_payloads": False,
        },
    }


def build_projection_demand_yjs_payload(
    *,
    webspace_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    ts = float(now if now is not None else time.time())
    records = list_client_subscription_records(webspace_id=target_webspace_id)
    snapshot = projection_demand_snapshot(webspace_id=target_webspace_id, now=ts)
    records_payload = [record.to_dict() for record in records]
    clients = _clients_payload(records)
    envelope = _projection_demand_yjs_envelope(
        webspace_id=target_webspace_id,
        client_total=len(records_payload),
        projection_total=int(snapshot.get("projection_total") or 0),
        consumer_total=int(snapshot.get("consumer_total") or 0),
    )
    payload = {
        "schema": PROJECTION_DEMAND_YJS_SCHEMA,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_DEMAND_YJS_PATH,
        "client_total": len(records_payload),
        "projection_total": int(snapshot.get("projection_total") or 0),
        "consumer_total": int(snapshot.get("consumer_total") or 0),
        "projection_keys": sorted(
            {
                subscription.projection_key
                for record in records
                for subscription in record.subscriptions
                if subscription.projection_key
            }
        ),
        "records": records_payload,
        "clients": clients,
        "projections": snapshot.get("projections") or [],
        "envelope": envelope,
        "updated_at": ts,
    }
    payload["fingerprint"] = projection_fingerprint(
        {
            "schema": payload["schema"],
            "webspace_id": payload["webspace_id"],
            "records": records_payload,
            "envelope": envelope,
        }
    )
    return payload


def _write_payload_to_doc(ydoc: Any, txn: Any, payload: Mapping[str, Any]) -> bool:
    runtime_map = ydoc.get_map("runtime")
    current = runtime_map.get(PROJECTION_DEMAND_YJS_KEY)
    if isinstance(current, Mapping) and current.get("fingerprint") == payload.get("fingerprint"):
        return False
    runtime_map.set(txn, PROJECTION_DEMAND_YJS_CLIENTS_KEY, _json_clone(payload.get("clients") or {}))
    runtime_map.set(txn, PROJECTION_DEMAND_YJS_KEY, _json_clone(dict(payload)))
    return True


async def materialize_projection_demand_to_yjs(
    *,
    webspace_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    payload = build_projection_demand_yjs_payload(webspace_id=target_webspace_id, now=now)
    changed = {"value": False}

    def _apply(ydoc: Any, txn: Any) -> None:
        changed["value"] = _write_payload_to_doc(ydoc, txn, payload)

    live_applied = mutate_live_room(
        target_webspace_id,
        _apply,
        root_names=["runtime"],
        source="projection_demand_yjs",
        owner=PROJECTION_DEMAND_YJS_OWNER,
        channel="core.projection_demand.live_room",
    )
    if not live_applied:
        async with async_get_ydoc(
            target_webspace_id,
            publish_live_room=True,
            load_mark_roots=["runtime"],
            write_source="projection_demand_yjs",
            write_owner=PROJECTION_DEMAND_YJS_OWNER,
            write_channel="core.projection_demand.async",
        ) as ydoc:
            with ydoc.begin_transaction() as txn:
                changed["value"] = _write_payload_to_doc(ydoc, txn, payload)

    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_DEMAND_YJS_PATH,
        "schema": PROJECTION_DEMAND_YJS_SCHEMA,
        "client_total": int(payload["client_total"]),
        "projection_total": int(payload["projection_total"]),
        "consumer_total": int(payload["consumer_total"]),
        "projection_keys": list(payload["projection_keys"]),
        "envelope_present": True,
        "envelope_ok": True,
        "envelope": payload["envelope"],
        "fingerprint": payload["fingerprint"],
        "written": bool(changed["value"]),
        "live_room": bool(live_applied),
        "payload": payload,
        "updated_at": payload["updated_at"],
    }


async def safe_materialize_projection_demand_to_yjs(
    *,
    webspace_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    try:
        return await materialize_projection_demand_to_yjs(webspace_id=webspace_id, now=now)
    except Exception as exc:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": _webspace_token(webspace_id),
            "yjs_path": PROJECTION_DEMAND_YJS_PATH,
            "schema": PROJECTION_DEMAND_YJS_SCHEMA,
            "written": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _records_from_clients_payload(
    clients: Mapping[str, Any],
    *,
    webspace_id: str,
) -> list[ClientSubscriptionRecord]:
    records: list[ClientSubscriptionRecord] = []
    for client_id, client_value in sorted(clients.items(), key=lambda item: str(item[0])):
        if isinstance(client_value, Mapping) and isinstance(client_value.get("subscriptions"), list):
            data = dict(client_value)
            data.setdefault("client_id", client_id)
            data.setdefault("webspace_id", webspace_id)
            records.append(normalize_client_subscription_record(data))
            continue
        if not isinstance(client_value, Mapping):
            continue
        for session_id, session_value in sorted(client_value.items(), key=lambda item: str(item[0])):
            if not isinstance(session_value, Mapping):
                continue
            data = dict(session_value)
            data.setdefault("client_id", client_id)
            data.setdefault("session_id", session_id)
            data.setdefault("webspace_id", webspace_id)
            records.append(normalize_client_subscription_record(data))
    return records


def _records_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    clients: Mapping[str, Any] | None = None,
    webspace_id: str,
) -> list[ClientSubscriptionRecord]:
    if isinstance(payload, Mapping) and isinstance(payload.get("records"), list):
        records = []
        for item in payload.get("records") or []:
            if not isinstance(item, Mapping):
                continue
            data = dict(item)
            data.setdefault("webspace_id", webspace_id)
            records.append(normalize_client_subscription_record(data))
        return records
    source_clients = clients
    if source_clients is None and isinstance(payload, Mapping):
        raw_clients = payload.get("clients")
        source_clients = raw_clients if isinstance(raw_clients, Mapping) else None
    if isinstance(source_clients, Mapping):
        return _records_from_clients_payload(source_clients, webspace_id=webspace_id)
    return []


def _demand_cache_summary(
    payload: Mapping[str, Any] | None,
    *,
    clients: Mapping[str, Any] | None = None,
    webspace_id: str,
) -> dict[str, Any]:
    records = _records_from_payload(payload, clients=clients, webspace_id=webspace_id)
    records_payload = [record.to_dict() for record in records]
    projection_keys = sorted(
        {
            subscription.projection_key
            for record in records
            for subscription in record.subscriptions
            if subscription.projection_key
        }
    )
    envelope = payload.get("envelope") if isinstance(payload, Mapping) else None
    fingerprint_source = {
        "schema": PROJECTION_DEMAND_YJS_SCHEMA,
        "webspace_id": webspace_id,
        "records": records_payload,
        "envelope": envelope,
    }
    expected_fingerprint = projection_fingerprint(fingerprint_source) if isinstance(envelope, Mapping) else None
    fingerprint = str(payload.get("fingerprint") or "") if isinstance(payload, Mapping) else ""
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_id,
        "cache_present": isinstance(payload, Mapping) or isinstance(clients, Mapping),
        "yjs_path": PROJECTION_DEMAND_YJS_PATH,
        "schema": payload.get("schema") if isinstance(payload, Mapping) else PROJECTION_DEMAND_YJS_SCHEMA,
        "schema_ok": not isinstance(payload, Mapping) or payload.get("schema") == PROJECTION_DEMAND_YJS_SCHEMA,
        "client_total": len(records),
        "projection_total": len(projection_keys),
        "consumer_total": sum(len(record.subscriptions) for record in records),
        "projection_keys": projection_keys,
        "records": records_payload,
        "clients": _clients_payload(records),
        "envelope_present": isinstance(envelope, Mapping),
        "envelope_ok": isinstance(envelope, Mapping)
        and envelope.get("schema") == PROJECTION_DEMAND_YJS_ENVELOPE_SCHEMA
        and envelope.get("owner") == PROJECTION_DEMAND_YJS_OWNER
        and envelope.get("write_policy") == PROJECTION_DEMAND_YJS_WRITE_POLICY,
        "envelope": _json_clone(dict(envelope)) if isinstance(envelope, Mapping) else None,
        "fingerprint": fingerprint or None,
        "fingerprint_ok": bool(fingerprint and expected_fingerprint and fingerprint == expected_fingerprint),
        "payload": _json_clone(dict(payload)) if isinstance(payload, Mapping) else None,
    }


async def read_projection_demand_yjs(*, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    try:
        async with async_read_ydoc(target_webspace_id) as ydoc:
            runtime_map = ydoc.get_map("runtime")
            payload = runtime_map.get(PROJECTION_DEMAND_YJS_KEY)
            clients = runtime_map.get(PROJECTION_DEMAND_YJS_CLIENTS_KEY)
    except Exception as exc:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "cache_present": False,
            "yjs_path": PROJECTION_DEMAND_YJS_PATH,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, Mapping) and not isinstance(clients, Mapping):
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": target_webspace_id,
            "cache_present": False,
            "yjs_path": PROJECTION_DEMAND_YJS_PATH,
            "schema": PROJECTION_DEMAND_YJS_SCHEMA,
            "client_total": 0,
            "projection_total": 0,
            "consumer_total": 0,
            "projection_keys": [],
            "records": [],
            "clients": {},
            "payload": None,
        }
    return _demand_cache_summary(
        payload if isinstance(payload, Mapping) else None,
        clients=clients if isinstance(clients, Mapping) else None,
        webspace_id=target_webspace_id,
    )


def _filter_record_for_restore(
    record: ClientSubscriptionRecord,
    *,
    include_hidden: bool,
    include_stale: bool,
    stale_after_s: float | None,
    now: float,
) -> tuple[ClientSubscriptionRecord | None, str | None]:
    if stale_after_s is not None and not include_stale:
        try:
            if max(0.0, now - float(record.updated_at)) > max(0.0, float(stale_after_s)):
                return None, "stale"
        except Exception:
            pass
    subscriptions = []
    for subscription in record.subscriptions:
        if not include_hidden and subscription.visibility == "hidden":
            continue
        subscriptions.append(normalize_projection_subscription(subscription.to_dict()))
    if not subscriptions:
        return None, "empty_after_filters"
    return (
        make_client_subscription_record(
            client_id=record.client_id,
            device_id=record.device_id,
            session_id=record.session_id,
            webspace_id=record.webspace_id,
            role=record.role,
            subscriptions=subscriptions,
            updated_at=record.updated_at,
        ),
        None,
    )


async def restore_projection_demand_from_yjs(
    *,
    webspace_id: str | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    ts = float(now if now is not None else time.time())
    cache = await read_projection_demand_yjs(webspace_id=target_webspace_id)
    if not cache.get("ok"):
        return {
            **cache,
            "restored_total": 0,
            "skipped_total": 0,
            "restored": [],
            "skipped": [],
        }
    records = _records_from_payload(
        cache.get("payload") if isinstance(cache.get("payload"), Mapping) else None,
        clients=cache.get("clients") if isinstance(cache.get("clients"), Mapping) else None,
        webspace_id=target_webspace_id,
    )
    restored: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = _client_session_key(record)
        if key in seen:
            skipped.append({"client_id": record.client_id, "session_id": record.session_id, "reason": "duplicate"})
            continue
        seen.add(key)
        if record.webspace_id != target_webspace_id:
            skipped.append(
                {
                    "client_id": record.client_id,
                    "session_id": record.session_id,
                    "webspace_id": record.webspace_id,
                    "reason": "webspace_mismatch",
                }
            )
            continue
        filtered, reason = _filter_record_for_restore(
            record,
            include_hidden=include_hidden,
            include_stale=include_stale,
            stale_after_s=stale_after_s,
            now=ts,
        )
        if filtered is None:
            skipped.append({"client_id": record.client_id, "session_id": record.session_id, "reason": reason})
            continue
        stored = write_client_subscription_record(filtered)
        restored.append(stored.to_dict())
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_DEMAND_YJS_PATH,
        "schema": PROJECTION_DEMAND_YJS_SCHEMA,
        "cache_present": bool(cache.get("cache_present")),
        "restored_total": len(restored),
        "skipped_total": len(skipped),
        "restored": restored,
        "skipped": skipped,
        "snapshot": projection_demand_snapshot(webspace_id=target_webspace_id, now=ts),
        "updated_at": ts,
    }


__all__ = [
    "PROJECTION_DEMAND_YJS_CLIENTS_KEY",
    "PROJECTION_DEMAND_YJS_ENVELOPE_SCHEMA",
    "PROJECTION_DEMAND_YJS_KEY",
    "PROJECTION_DEMAND_YJS_OWNER",
    "PROJECTION_DEMAND_YJS_PATH",
    "PROJECTION_DEMAND_YJS_SCHEMA",
    "PROJECTION_DEMAND_YJS_WRITE_POLICY",
    "build_projection_demand_yjs_payload",
    "materialize_projection_demand_to_yjs",
    "read_projection_demand_yjs",
    "restore_projection_demand_from_yjs",
    "safe_materialize_projection_demand_to_yjs",
]
