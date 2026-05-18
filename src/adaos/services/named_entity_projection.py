from __future__ import annotations

import logging
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services import named_entities
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.named_entities.projection")


def _payload(evt: Any) -> dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _topic(evt: Any) -> str:
    if isinstance(evt, dict):
        return str(evt.get("type") or evt.get("topic") or "").strip()
    return str(getattr(evt, "type", "") or getattr(evt, "topic", "") or "").strip()


def _resolve_webspace_id(payload: Mapping[str, Any] | None = None) -> str:
    payload = payload if isinstance(payload, Mapping) else {}
    scope = payload.get("scope") if isinstance(payload.get("scope"), Mapping) else {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    token = (
        payload.get("webspace_id")
        or payload.get("workspace_id")
        or scope.get("webspace_id")
        or meta.get("webspace_id")
        or meta.get("workspace_id")
    )
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _write_payload_to_doc(ydoc: Any, txn: Any, payload: Mapping[str, Any]) -> None:
    registry_map = ydoc.get_map("registry")
    current = registry_map.get("named_entities")
    current_summary = current.get("summary") if isinstance(current, Mapping) else {}
    next_summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    if (
        isinstance(current_summary, Mapping)
        and current_summary.get("fingerprint")
        and current_summary.get("fingerprint") == next_summary.get("fingerprint")
    ):
        return
    registry_map.set(txn, "named_entities", dict(payload))


async def project_named_entity_registry(*, webspace_id: str | None = None) -> dict[str, Any]:
    from adaos.services.yjs.doc import async_get_ydoc, mutate_live_room

    webspace = webspace_id or default_webspace_id()
    payload = named_entities.compact_registry_payload(webspace_id=webspace)

    def _apply(ydoc: Any, txn: Any) -> None:
        _write_payload_to_doc(ydoc, txn, payload)

    if mutate_live_room(
        webspace,
        _apply,
        root_names=["registry"],
        source="named_entity_projection",
        owner="core:named_entities",
        channel="core.named_entities.live_room",
    ):
        return payload

    async with ystore_write_metadata(
        root_names=["registry"],
        source="named_entity_projection",
        owner="core:named_entities",
        channel="core.named_entities.async",
    ):
        async with async_get_ydoc(
            webspace,
            publish_live_room=False,
            load_mark_roots=["registry"],
            write_source="named_entity_projection",
            write_owner="core:named_entities",
            write_channel="core.named_entities.async",
        ) as ydoc:
            with ydoc.begin_transaction() as txn:
                _apply(ydoc, txn)
    return payload


@subscribe("sys.ready")
async def on_sys_ready(evt: Any) -> None:
    await on_entity_registry_changed(evt)


@subscribe(named_entities.ENTITY_REGISTRY_CHANGED)
@subscribe("subnet.alias.changed")
async def on_entity_registry_changed(evt: Any) -> None:
    try:
        payload = _payload(evt)
        webspace_ids = [_resolve_webspace_id(payload)]
        if _topic(evt) == "subnet.alias.changed":
            default_webspace = default_webspace_id()
            if default_webspace not in webspace_ids:
                webspace_ids.append(default_webspace)
        for webspace_id in webspace_ids:
            await project_named_entity_registry(webspace_id=webspace_id)
    except Exception:
        _log.debug("failed to project named entity registry", exc_info=True)


__all__ = ["on_entity_registry_changed", "on_sys_ready", "project_named_entity_registry"]
