from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Mapping

from adaos.domain import projection_fingerprint
from adaos.services.projection_records import projection_record_registry_snapshot


PLATFORM_NODES_YJS_KEY = "nodes"
PLATFORM_NODES_YJS_PATH = "platform/nodes"
PLATFORM_NODES_YJS_SCHEMA = "adaos.platform-nodes.yjs.v1"
PLATFORM_NODES_YJS_OWNER = "core:platform_nodes"
PLATFORM_NODES_YJS_WRITE_POLICY = "core-owned-reserved-platform-branch"
PLATFORM_NODES_CONTRACT = "adaos.platform-nodes.reserved-yjs-branch.v1"


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


def _node_token(value: Any = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    for env_name in ("ADAOS_NODE_ID", "ADAOS_DEVICE_ID", "COMPUTERNAME", "HOSTNAME"):
        token = str(os.getenv(env_name) or "").strip()
        if token:
            return token
    try:
        token = str(socket.gethostname() or "").strip()
        if token:
            return token
    except Exception:
        pass
    return "local"


def build_platform_node_yjs_payload(
    *,
    webspace_id: str | None = None,
    node_id: str | None = None,
    status: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    projections: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    target_node_id = _node_token(node_id)
    ts = float(now if now is not None else time.time())
    projection_summary = dict(projections) if isinstance(projections, Mapping) else {}
    if not projection_summary:
        registry = projection_record_registry_snapshot(webspace_id=target_webspace_id)
        projection_summary = {
            "schema": "adaos.platform-node.projections.summary.v1",
            "record_total": int(registry.get("record_total") or 0),
            "ready_total": int(registry.get("ready_total") or 0),
            "stale_total": int(registry.get("stale_total") or 0),
            "error_total": int(registry.get("error_total") or 0),
            "projection_keys": [
                str(record.get("meta", {}).get("projection_key") or "")
                for record in registry.get("records", [])
                if isinstance(record, Mapping)
            ],
        }
    node_payload = {
        "schema": PLATFORM_NODES_YJS_SCHEMA,
        "owner": PLATFORM_NODES_YJS_OWNER,
        "write_policy": PLATFORM_NODES_YJS_WRITE_POLICY,
        "node_id": target_node_id,
        "webspace_id": target_webspace_id,
        "status": dict(status) if isinstance(status, Mapping) else {"state": "unknown"},
        "diagnostics": dict(diagnostics) if isinstance(diagnostics, Mapping) else {},
        "projections": projection_summary,
        "updated_at": ts,
    }
    node_payload["fingerprint"] = projection_fingerprint(node_payload)
    payload = {
        "schema": PLATFORM_NODES_YJS_SCHEMA,
        "webspace_id": target_webspace_id,
        "yjs_path": PLATFORM_NODES_YJS_PATH,
        "owner": PLATFORM_NODES_YJS_OWNER,
        "write_policy": PLATFORM_NODES_YJS_WRITE_POLICY,
        "node_ids": [target_node_id],
        "nodes": {target_node_id: node_payload},
        "updated_at": ts,
    }
    payload["fingerprint"] = projection_fingerprint(
        {
            "schema": payload["schema"],
            "webspace_id": payload["webspace_id"],
            "nodes": payload["nodes"],
        }
    )
    return payload


def _write_payload_to_doc(ydoc: Any, txn: Any, payload: Mapping[str, Any]) -> bool:
    platform_map = ydoc.get_map("platform")
    incoming_nodes = payload.get("nodes") if isinstance(payload.get("nodes"), Mapping) else {}
    current_nodes = platform_map.get(PLATFORM_NODES_YJS_KEY)
    nodes = dict(current_nodes) if isinstance(current_nodes, Mapping) else {}
    changed = False
    for node_id, node_payload in incoming_nodes.items():
        node_token = str(node_id or "").strip()
        if not node_token or not isinstance(node_payload, Mapping):
            continue
        current = nodes.get(node_token)
        if isinstance(current, Mapping) and current.get("fingerprint") == node_payload.get("fingerprint"):
            continue
        nodes[node_token] = _json_clone(dict(node_payload))
        changed = True
    if not changed:
        return False
    platform_map.set(txn, "schema", PLATFORM_NODES_YJS_SCHEMA)
    platform_map.set(txn, "owner", PLATFORM_NODES_YJS_OWNER)
    platform_map.set(txn, "write_policy", PLATFORM_NODES_YJS_WRITE_POLICY)
    platform_map.set(txn, PLATFORM_NODES_YJS_KEY, nodes)
    platform_map.set(txn, "updated_at", payload.get("updated_at"))
    platform_map.set(
        txn,
        "fingerprint",
        projection_fingerprint(
            {
                "schema": PLATFORM_NODES_YJS_SCHEMA,
                "webspace_id": payload.get("webspace_id"),
                "nodes": nodes,
            }
        ),
    )
    return True


async def materialize_platform_node_to_yjs(
    *,
    webspace_id: str | None = None,
    node_id: str | None = None,
    status: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    projections: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    payload = build_platform_node_yjs_payload(
        webspace_id=target_webspace_id,
        node_id=node_id,
        status=status,
        diagnostics=diagnostics,
        projections=projections,
        now=now,
    )
    changed = {"value": False}

    def _apply(ydoc: Any, txn: Any) -> None:
        changed["value"] = _write_payload_to_doc(ydoc, txn, payload)

    live_applied = mutate_live_room(
        target_webspace_id,
        _apply,
        root_names=["platform"],
        source="platform_node_yjs",
        owner=PLATFORM_NODES_YJS_OWNER,
        channel="core.platform_nodes.live_room",
    )
    if not live_applied:
        async with async_get_ydoc(
            target_webspace_id,
            publish_live_room=True,
            load_mark_roots=["platform"],
            write_source="platform_node_yjs",
            write_owner=PLATFORM_NODES_YJS_OWNER,
            write_channel="core.platform_nodes.async",
        ) as ydoc:
            with ydoc.begin_transaction() as txn:
                changed["value"] = _write_payload_to_doc(ydoc, txn, payload)
    node_ids = list(payload["node_ids"])
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "node_id": node_ids[0] if node_ids else None,
        "node_ids": node_ids,
        "yjs_path": PLATFORM_NODES_YJS_PATH,
        "schema": PLATFORM_NODES_YJS_SCHEMA,
        "owner": PLATFORM_NODES_YJS_OWNER,
        "write_policy": PLATFORM_NODES_YJS_WRITE_POLICY,
        "written": bool(changed["value"]),
        "live_room": bool(live_applied),
        "payload": payload,
        "updated_at": payload["updated_at"],
    }


async def read_platform_nodes_yjs(*, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    try:
        async with async_read_ydoc(target_webspace_id) as ydoc:
            platform_map = ydoc.get_map("platform")
            nodes = platform_map.get(PLATFORM_NODES_YJS_KEY)
            schema = platform_map.get("schema")
            owner = platform_map.get("owner")
            write_policy = platform_map.get("write_policy")
            fingerprint = platform_map.get("fingerprint")
            updated_at = platform_map.get("updated_at")
    except Exception as exc:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "cache_present": False,
            "yjs_path": PLATFORM_NODES_YJS_PATH,
            "error": f"{type(exc).__name__}: {exc}",
        }
    node_payload = dict(nodes) if isinstance(nodes, Mapping) else {}
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "cache_present": bool(node_payload),
        "yjs_path": PLATFORM_NODES_YJS_PATH,
        "schema": schema or PLATFORM_NODES_YJS_SCHEMA,
        "schema_ok": schema in (None, PLATFORM_NODES_YJS_SCHEMA),
        "owner": owner or PLATFORM_NODES_YJS_OWNER,
        "owner_ok": owner in (None, PLATFORM_NODES_YJS_OWNER),
        "write_policy": write_policy or PLATFORM_NODES_YJS_WRITE_POLICY,
        "write_policy_ok": write_policy in (None, PLATFORM_NODES_YJS_WRITE_POLICY),
        "node_total": len(node_payload),
        "node_ids": sorted(str(key) for key in node_payload),
        "nodes": _json_clone(node_payload),
        "fingerprint": fingerprint,
        "updated_at": updated_at,
    }


def platform_nodes_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    return {
        "contract": PLATFORM_NODES_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else time.time()),
        "yjs_path": PLATFORM_NODES_YJS_PATH,
        "schema": PLATFORM_NODES_YJS_SCHEMA,
        "owner": PLATFORM_NODES_YJS_OWNER,
        "write_policy": PLATFORM_NODES_YJS_WRITE_POLICY,
        "reserved_top_level_branch": "platform",
        "node_branch_shape": {
            "status": "platform/nodes/<node_id>/status",
            "diagnostics": "platform/nodes/<node_id>/diagnostics",
            "projections": "platform/nodes/<node_id>/projections",
        },
        "boundaries": {
            "browser_may_read": True,
            "browser_may_write": False,
            "skill_may_write": False,
            "core_platform_owned": True,
            "projection_cache_replacement": False,
        },
        "compatibility": {
            "data_projection_records_remains_browser_cache": True,
            "node_scope_is_explicit": True,
            "anonymous_single_node_assumption": False,
        },
        "evidence": [
            "/api/node/platform/nodes/contract",
            "/api/node/platform/nodes/yjs",
            "/api/node/platform/nodes/materialize",
        ],
    }


__all__ = [
    "PLATFORM_NODES_CONTRACT",
    "PLATFORM_NODES_YJS_KEY",
    "PLATFORM_NODES_YJS_OWNER",
    "PLATFORM_NODES_YJS_PATH",
    "PLATFORM_NODES_YJS_SCHEMA",
    "PLATFORM_NODES_YJS_WRITE_POLICY",
    "build_platform_node_yjs_payload",
    "materialize_platform_node_to_yjs",
    "platform_nodes_contract_snapshot",
    "read_platform_nodes_yjs",
]
