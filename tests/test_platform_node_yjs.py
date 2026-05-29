from __future__ import annotations

import asyncio

from adaos.services.platform_node_yjs import (
    PLATFORM_NODES_YJS_OWNER,
    PLATFORM_NODES_YJS_WRITE_POLICY,
    materialize_platform_node_to_yjs,
    platform_nodes_contract_snapshot,
    read_platform_nodes_yjs,
)


class _FakeMap(dict):
    def set(self, _txn: object, key: str, value: object) -> None:
        self[key] = value


class _FakeDoc:
    def __init__(self) -> None:
        self.maps = {"platform": _FakeMap()}

    def get_map(self, name: str) -> _FakeMap:
        return self.maps.setdefault(name, _FakeMap())

    def begin_transaction(self) -> "_FakeDoc":
        return self

    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeAsyncDocContext:
    def __init__(self, doc: _FakeDoc) -> None:
        self.doc = doc

    async def __aenter__(self) -> _FakeDoc:
        return self.doc

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def test_platform_nodes_contract_snapshot_reserves_branch() -> None:
    snapshot = platform_nodes_contract_snapshot(now=70.0)

    assert snapshot["contract"] == "adaos.platform-nodes.reserved-yjs-branch.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["yjs_path"] == "platform/nodes"
    assert snapshot["node_branch_shape"]["diagnostics"] == "platform/nodes/<node_id>/diagnostics"
    assert snapshot["boundaries"]["browser_may_write"] is False


def test_materialize_platform_node_to_yjs_writes_reserved_platform_branch(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import platform_node_yjs

    monkeypatch.setattr(platform_node_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(platform_node_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    monkeypatch.setattr(platform_node_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))

    result = asyncio.run(
        materialize_platform_node_to_yjs(
            webspace_id="desktop",
            node_id="node-a",
            status={"state": "ready"},
            diagnostics={"cpu": "ok"},
            projections={"record_total": 2},
            now=80.0,
        )
    )
    readback = asyncio.run(read_platform_nodes_yjs(webspace_id="desktop"))

    node = fake_doc.get_map("platform")["nodes"]["node-a"]
    assert result["written"] is True
    assert result["yjs_path"] == "platform/nodes"
    assert node["owner"] == PLATFORM_NODES_YJS_OWNER
    assert node["write_policy"] == PLATFORM_NODES_YJS_WRITE_POLICY
    assert node["status"]["state"] == "ready"
    assert node["diagnostics"]["cpu"] == "ok"
    assert readback["cache_present"] is True
    assert readback["node_ids"] == ["node-a"]
    assert readback["nodes"]["node-a"]["projections"]["record_total"] == 2
