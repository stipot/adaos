from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def _load_browsers_skill_module():
    if "adaos.sdk.data.ctx" not in sys.modules:
        fake_ctx = types.ModuleType("adaos.sdk.data.ctx")

        class _FakeSubnet:
            def set(self, slot, value, *, webspace_id=None):
                return None

            async def set_async(self, slot, value, *, webspace_id=None):
                return None

        fake_ctx.subnet = _FakeSubnet()
        fake_ctx.current_user = object()
        fake_ctx.selected_user = object()
        sys.modules["adaos.sdk.data.ctx"] = fake_ctx

    if "adaos.services.workspaces.index" not in sys.modules:
        fake_index = types.ModuleType("adaos.services.workspaces.index")
        fake_index.list_workspaces = lambda: []
        sys.modules["adaos.services.workspaces.index"] = fake_index
        if "adaos.services.workspaces" not in sys.modules:
            fake_pkg = types.ModuleType("adaos.services.workspaces")
            fake_pkg.index = fake_index
            sys.modules["adaos.services.workspaces"] = fake_pkg

    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "browsers_skill" / "handlers" / "main.py"
    module_name = f"test_browsers_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_browsers_skill_detach_link_refreshes_snapshot_without_nameerror(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()
    mod._SELECTED_BROWSER_BY_WS["desktop"] = "missing-browser"

    browser_entry = {
        "id": "browser-1",
        "display_name": "Living room browser",
        "hostname": "tv-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    published: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        published.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(
        mod.workspace_index,
        "list_workspaces",
        lambda: [
            SimpleNamespace(workspace_id="desktop"),
            SimpleNamespace(workspace_id="default"),
        ],
    )
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")
    monkeypatch.setattr(
        mod.sdk_device_access,
        "detach_device",
        lambda device_ref: {"ok": True, "device_ref": str(device_ref or "").strip(), "entry": {"revoked": True}},
    )

    result = mod.detach_link(node_id="member-1", webspace_id="desktop")

    assert result["ok"] is True
    assert result["device_ref"] == "member:member-1"
    assert mod._SELECTED_BROWSER_BY_WS["desktop"] == "browser-1"
    assert any(slot == "browsers.current_name" and webspace_id == "desktop" for slot, webspace_id, _value in published)
    assert not any(slot == "browsers.current_name" and webspace_id == "default" for slot, webspace_id, _value in published)


def test_browsers_skill_projection_refresh_skips_unchanged_yjs_writes(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    writes: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        writes.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    asyncio.run(mod._publish_snapshot("desktop"))
    first_write_count = len(writes)
    asyncio.run(mod._publish_snapshot("desktop"))

    assert first_write_count == 5
    assert len(writes) == first_write_count


def test_browsers_skill_browser_tiles_include_online_flag(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    tiles = mod._browser_tiles([
        {
            "id": "browser-1",
            "display_name": "Dev Browser",
            "access_class": "device",
            "online": True,
        },
        {
            "id": "browser-2",
            "display_name": "Old Browser",
            "access_class": "device",
            "online": False,
        },
    ])

    assert tiles[0]["title"] == "Dev Browser"
    assert tiles[0]["online"] is True
    assert tiles[0]["status"] == "online"
    assert tiles[1]["online"] is False
    assert tiles[1]["status"] == "offline"


def test_browsers_skill_explicit_refresh_recomputes_without_rewriting_identical_yjs(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    writes: list[tuple[str, str | None, object]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        writes.append((slot, webspace_id, value))

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    assert mod.refresh_snapshot("desktop")["delivery"] == "projection"
    assert mod.refresh_snapshot("desktop")["delivery"] == "projection"

    assert len(writes) == 5
    assert mod._PROJECTION_RUNTIME.diagnostics_snapshot()["skipped_unchanged_total"] == 5


def test_browsers_skill_projection_refresh_does_not_eager_publish_streams(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._SELECTED_BROWSER_BY_WS.clear()
    mod._PROJECTION_RUNTIME.reset()

    browser_entry = {
        "id": "browser-1",
        "display_name": "Main browser",
        "hostname": "main-browser",
        "access_class": "device",
        "lifetime_mode": "permanent",
        "last_webspace_id": "desktop",
        "last_seen_at": 1715000000.0,
        "online": True,
    }
    streams: list[tuple[str, object, dict[str, object] | None]] = []

    async def _fake_set_async(slot, value, *, webspace_id=None):
        return None

    monkeypatch.setattr(mod.ctx_subnet, "set_async", _fake_set_async)
    monkeypatch.setattr(mod, "stream_publish", lambda receiver, data, _meta=None: streams.append((receiver, data, _meta)))
    monkeypatch.setattr(mod.workspace_index, "list_workspaces", lambda: [])
    monkeypatch.setattr(mod.sdk_access_links, "list_browser_links", lambda: [dict(browser_entry)])
    monkeypatch.setattr(
        mod.sdk_access_links,
        "get_browser_link",
        lambda device_id: dict(browser_entry) if str(device_id or "").strip() == "browser-1" else None,
    )
    monkeypatch.setattr(mod.sdk_access_links, "lifetime_label", lambda _entry: "Permanent")

    asyncio.run(mod._publish_snapshot("desktop"))
    assert streams == []

    mod._publish_stream_snapshot("browsers.devices", "desktop")

    assert [item[0] for item in streams] == ["browsers.devices"]
    assert streams[0][2] == {"webspace_id": "desktop"}


def test_browsers_skill_refresh_event_handler_does_not_wait_for_projection(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    mod._PENDING_REFRESH_BY_WS.clear()
    submitted: list[object] = []

    class _Future:
        def done(self) -> bool:
            return False

        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            raise AssertionError("event-loop refresh must not wait for projection result")

    class _Executor:
        def submit(self, fn):
            submitted.append(fn)
            for cell in fn.__closure__ or ():
                value = cell.cell_contents
                if inspect.iscoroutine(value):
                    value.close()
            return _Future()

    monkeypatch.setattr(mod, "_PROJECTION_EXECUTOR", _Executor())
    monkeypatch.setattr(
        mod,
        "_build_snapshot",
        lambda target_ws=None: (
            {
                "summary": {},
                "devices": [],
                "clients": [],
                "current_summary": [],
                "current_name": {"value": ""},
            },
            str(target_ws or "desktop"),
        ),
    )

    async def _invoke() -> None:
        mod._on_refresh(SimpleNamespace(payload={"webspace_id": "desktop"}))

    asyncio.run(_invoke())

    assert submitted
    assert "desktop" in mod._PENDING_REFRESH_BY_WS


def test_browsers_skill_get_link_settings_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    expected = {
        "device_ref": "member:member-2",
        "title": "Kitchen tablet",
        "detach": {"enabled": True, "confirm_message": 'Detach device "Kitchen tablet"?'},
    }
    seen: list[str] = []

    def _fake_get_device_settings(device_ref: str):
        seen.append(str(device_ref or "").strip())
        return dict(expected)

    monkeypatch.setattr(mod.sdk_device_access, "get_device_settings", _fake_get_device_settings)

    result = mod.get_link_settings(node_id="member-2")

    assert result == expected
    assert seen == ["member:member-2"]


def test_browsers_skill_get_device_settings_accepts_generic_device_ref(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    expected = {
        "device_ref": "member:member-4",
        "title": "Workshop display",
    }
    seen: list[str] = []

    def _fake_get_device_settings(device_ref: str):
        seen.append(str(device_ref or "").strip())
        return dict(expected)

    monkeypatch.setattr(mod.sdk_device_access, "get_device_settings", _fake_get_device_settings)

    result = mod.get_device_settings(device_ref="member:member-4")

    assert result == expected
    assert seen == ["member:member-4"]


def test_browsers_skill_rename_device_uses_generic_device_ref(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: {"ok": True, "webspace_id": webspace_id})

    def _fake_rename(device_ref: str, display_name: str):
        seen.append((str(device_ref or "").strip(), str(display_name or "").strip()))
        return {"ok": True, "device_ref": device_ref}

    monkeypatch.setattr(mod.sdk_device_access, "rename_device", _fake_rename)

    result = mod.rename_device(device_ref="member:member-4", name="Workshop display", webspace_id="desktop")

    assert result == {"ok": True, "device_ref": "member:member-4"}
    assert seen == [("member:member-4", "Workshop display")]


def test_browsers_skill_adopt_link_uses_sdk_device_access(monkeypatch) -> None:
    mod = _load_browsers_skill_module()
    seen: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(mod, "_refresh_snapshot_sync", lambda webspace_id=None: {"ok": True, "webspace_id": webspace_id})

    def _fake_adopt(device_ref: str, display_name: str | None = None, preset: str = "permanent"):
        seen.append((str(device_ref or "").strip(), display_name, str(preset or "").strip()))
        return {"ok": True, "device_ref": device_ref}

    monkeypatch.setattr(mod.sdk_device_access, "adopt_device", _fake_adopt)

    result = mod.adopt_link(node_id="member-3", name="Workshop display", preset="7d", webspace_id="desktop")

    assert result == {"ok": True, "device_ref": "member:member-3"}
    assert seen == [("member:member-3", "Workshop display", "7d")]
