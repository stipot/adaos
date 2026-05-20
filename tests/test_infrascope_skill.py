from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg


class _FakeCanonicalObject:
    def __init__(
        self,
        object_id: str,
        kind: str,
        title: str,
        *,
        status: str = "online",
        summary: str = "",
    ) -> None:
        self.id = object_id
        self.kind = kind
        self.title = title
        self.status = status
        self.summary = summary

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
        }


def _load_infrascope_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "infrascope_skill" / "handlers" / "main.py"
    module_name = f"test_infrascope_skill_{uuid4().hex}"
    service_key = "adaos.services.system_model.service"
    previous_service_module = sys.modules.get(service_key)
    service_module = types.ModuleType("adaos.services.system_model.service")
    service_module.current_control_plane_objects = lambda webspace_id=None: []
    service_module.current_object_inspector = lambda object_id, task_goal=None, webspace_id=None: None
    service_module.current_overview_projection = lambda webspace_id=None: None
    sys.modules[service_key] = service_module
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_service_module is None:
            sys.modules.pop(service_key, None)
        else:
            sys.modules[service_key] = previous_service_module


def test_infrascope_skill_projects_overview_summary_and_incident_rows(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("local", "hub", "Local hub")
    member = _FakeCanonicalObject("member-1", "member", "Kitchen member", status="degraded", summary="link flaps")
    projection = SimpleNamespace(
        subject=local,
        objects=[member],
        context={
            "summary_tile": {
                "label": "health",
                "value": "degraded",
                "subtitle": "1 active incident",
            },
            "health_strip": [
                {
                    "id": "health:member-1",
                    "object_id": "member-1",
                    "summary": "Link is degraded",
                    "details": {"large": "payload"},
                }
            ],
            "active_incidents": [
                {
                    "id": "incident:member-1",
                    "object_id": "member-1",
                    "title": "Kitchen member",
                    "severity": "high",
                    "summary": "Link is degraded",
                }
            ],
        },
    )

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)

    summary = mod.get_overview_summary()
    incidents = mod.list_overview_collection("active_incidents")
    health = mod.list_overview_collection("health_strip")

    assert summary["object_id"] == "local"
    assert summary["buttons"][-1]["id"] == "inspect_local"
    assert incidents == [
        {
            "id": "incident:member-1",
            "object_id": "member-1",
            "object_title": "Kitchen member",
            "title": "Kitchen member",
            "subtitle": "high | degraded",
            "summary": "Link is degraded",
            "severity": "high",
            "status": "degraded",
            "icon": "git-branch-outline",
            "details_ref": {
                "kind": "stream",
                "receiver": "infrascope.inspector.member-1",
            },
        }
    ]
    assert health[0]["object_id"] == "member-1"
    assert health[0]["icon"] == "git-branch-outline"
    assert "details" not in health[0]
    assert health[0]["details_ref"]["receiver"] == "infrascope.inspector.member-1"


def test_infrascope_skill_inventory_and_inspector_shape(monkeypatch):
    mod = _load_infrascope_module()

    skill = _FakeCanonicalObject("skill:watchdog", "skill", "Watchdog", status="warning", summary="restart suggested")
    scenario = _FakeCanonicalObject("scenario:ops", "scenario", "Ops Desk", status="online")
    subject = _FakeCanonicalObject("hub-1", "hub", "Hub 1", status="degraded", summary="packet loss")
    projection = SimpleNamespace(
        subject=subject,
        context={
            "inspector": {
                "label": "hub",
                "value": "degraded",
                "subtitle": "Hub 1",
            },
            "actions": [{"id": "restart", "title": "Restart"}],
            "recent_changes": [{"id": "change:1", "title": "Config updated"}],
            "topology": {"edges": [{"from": "hub-1", "to": "member-1"}]},
            "task_packet": {"task_goal": "assist operator"},
            "subnet_planning": {"summary": {"node_total": 2}},
        },
        incidents=[{"id": "incident:hub-1", "summary": "packet loss"}],
        summary="Hub 1 is degraded",
    )

    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [skill, scenario])
    monkeypatch.setattr(mod, "current_object_inspector", lambda object_id, task_goal=None, webspace_id=None: projection)

    inventory = mod.list_inventory("skills")
    inspector = mod.get_object_inspector("hub-1", task_goal="assist operator")

    assert [item["object_id"] for item in inventory] == ["skill:watchdog"]
    assert inventory[0]["status"] == "warning"
    assert inventory[0]["icon"] == "extension-puzzle-outline"
    assert inspector["object_id"] == "hub-1"
    assert inspector["object"]["kind"] == "hub"
    assert inspector["actions"] == [{"id": "restart", "title": "Restart"}]
    assert inspector["task_packet"] == {"task_goal": "assist operator"}
    assert inspector["subnet_planning"] == {"summary": {"node_total": 2}}
    assert inspector["topology"] == {"edges": [{"from": "hub-1", "to": "member-1"}]}


def test_infrascope_profileops_panel_uses_root_mcp_contracts(monkeypatch):
    mod = _load_infrascope_module()
    if not hasattr(mod, "get_profileops_panel"):
        return

    local = _FakeCanonicalObject("hub:local", "hub", "Local hub", status="online")
    projection = SimpleNamespace(
        subject=local,
        objects=[],
        context={
            "summary_tile": {"label": "health", "value": "online", "subtitle": "steady"},
            "health_strip": [],
            "active_incidents": [],
            "active_runtimes": [],
            "quota_summary": [],
            "recent_changes": [],
        },
    )

    class _FakeProfileOpsClient:
        def get_profileops_status(self, target_id: str) -> dict[str, object]:
            assert target_id == "hub:test-subnet"
            return {"response": {"result": {"target_id": target_id, "report_count": 2}}}

        def list_profileops_sessions(self, target_id: str, *, state: str | None = None, suspected_only: bool = False) -> dict[str, object]:
            assert target_id == "hub:test-subnet"
            return {"response": {"result": {"sessions": [{"session_id": "mem-001"}, {"session_id": "mem-002"}]}}}

        def list_profileops_incidents(self, target_id: str) -> dict[str, object]:
            assert target_id == "hub:test-subnet"
            return {"response": {"result": {"incidents": [{"session_id": "mem-002", "severity": "high"}]}}}

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [local])
    monkeypatch.setattr(mod, "current_object_inspector", lambda object_id, task_goal=None, webspace_id=None: SimpleNamespace(subject=local, context={}, incidents=[], summary="ok"))
    monkeypatch.setattr(mod, "_profileops_target_id", lambda: "hub:test-subnet")
    monkeypatch.setattr(mod, "_profileops_client", lambda: _FakeProfileOpsClient())

    panel = mod.get_profileops_panel()
    snapshot = mod._snapshot()

    assert panel["available"] is True
    assert panel["source"] == "root_mcp_profileops"
    assert panel["status"]["report_count"] == 2
    assert snapshot["operations"]["profileops"]["available"] is True
    assert snapshot["operations"]["profileops"]["sessions"][0]["session_id"] == "mem-001"
    assert snapshot["inspectors"]["local"]["profileops"]["incidents"][0]["session_id"] == "mem-002"


def test_infrascope_skill_returns_safe_fallback_for_unknown_object(monkeypatch):
    mod = _load_infrascope_module()

    def _raise(*args, **kwargs):
        raise KeyError("missing")

    monkeypatch.setattr(mod, "current_object_inspector", _raise)

    payload = mod.get_object_inspector("missing-object")

    assert payload["value"] == "unknown"
    assert payload["warning"] == "Object not found: missing-object"
    assert payload["topology"] == {"edges": []}
    assert payload["task_packet"] == {}
    assert payload["subnet_planning"] == {}


def test_infrascope_skill_projects_snapshot_only_when_payload_changes(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("hub:local", "hub", "Local hub", status="online")
    browser = _FakeCanonicalObject("browser:dev-1", "browser_session", "Browser dev-1", status="warning", summary="connecting")

    overview_projection = SimpleNamespace(
        subject=local,
        objects=[browser],
        context={
            "summary_tile": {
                "label": "health",
                "value": "warning",
                "subtitle": "1 browser session",
            },
            "health_strip": [
                {
                    "id": "health:browsers",
                    "object_id": "browser:dev-1",
                    "title": "Browsers",
                    "summary": "Browser dev-1 is connecting",
                }
            ],
            "active_incidents": [],
            "active_runtimes": [],
            "quota_summary": [],
            "recent_changes": [],
        },
    )

    def _inspector(object_id, task_goal=None, webspace_id=None):
        subject = local if object_id in {"local", "hub:local"} else browser
        return SimpleNamespace(
            subject=subject,
            context={
                "inspector": {
                    "label": subject.kind,
                    "value": subject.status,
                    "subtitle": subject.title,
                },
                "actions": [],
                "recent_changes": [],
                "topology": {"edges": []},
                "task_packet": {"task_goal": task_goal or "assist operator"},
                "subnet_planning": {"summary": {"node_total": 1}},
            },
            incidents=[],
            summary=f"{subject.title} summary",
        )

    writes: list[tuple[str, str | None, dict[str, object]]] = []

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: overview_projection)
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [local, browser])
    monkeypatch.setattr(mod, "current_object_inspector", _inspector)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: [str(webspace_id or "ws-1")])
    monkeypatch.setattr(
        mod,
        "ctx_subnet",
        SimpleNamespace(set=lambda slot, value, webspace_id=None: writes.append((slot, webspace_id, value))),
    )

    full_snapshot = mod._snapshot(webspace_id="ws-1")
    snapshot = mod.get_snapshot(webspace_id="ws-1")
    first = mod.refresh_snapshot(webspace_id="ws-1")
    second = mod.refresh_snapshot(webspace_id="ws-1")

    assert full_snapshot["inventory"]["browsers"][0]["object_id"] == "browser:dev-1"
    assert snapshot["inspectors"]["local"]["object_id"] == "hub:local"
    assert "subnet_planning" not in snapshot["inspectors"]["local"]
    assert full_snapshot["meta"]["overview_section_bytes"]["health_strip"] > 0
    assert full_snapshot["inspectors"]["local"]["subnet_planning"] == {"summary": {"node_total": 1}}
    browser_stream_payload = mod._stream_payload_for_receiver(full_snapshot, "infrascope.inspector.browser:dev-1")
    assert browser_stream_payload["object"]["kind"] == "browser_session"
    assert first["projected"] == 1
    assert second["projected"] == 0
    assert len(writes) == 1
    slot, webspace_id, projected = writes[0]
    assert slot == "infrascope.snapshot"
    assert webspace_id == "ws-1"
    assert projected["summary"]["value"] == "warning"
    assert "inventory" not in projected
    assert "operations" not in projected
    assert "inspectors" in projected


def test_infrascope_webspace_event_invalidates_projection_state(monkeypatch):
    mod = _load_infrascope_module()
    scheduled: list[dict[str, object]] = []

    mod._last_projected_fingerprints.clear()
    mod._last_projected_at_mono.clear()
    mod._last_stream_fingerprints.clear()
    mod._last_projected_fingerprints["desktop"] = "fp-1"
    mod._last_projected_at_mono["desktop"] = 123.0
    mod._last_stream_fingerprints["desktop\0inventory:all"] = "stream-fp"

    monkeypatch.setattr(mod, "_schedule_snapshot_refresh", lambda **kwargs: scheduled.append(dict(kwargs)))

    evt = SimpleNamespace(type="desktop.webspace.reloaded", payload={"webspace_id": "desktop"})
    mod.on_webspace_event(evt)

    assert "desktop" not in mod._last_projected_fingerprints
    assert "desktop" not in mod._last_projected_at_mono
    assert "desktop\0inventory:all" not in mod._last_stream_fingerprints
    assert scheduled == [{"webspace_id": "desktop", "reason": "desktop.webspace.reloaded"}]


def test_infrascope_browser_runtime_refresh_is_budgeted_with_trailing_refresh(monkeypatch):
    mod = _load_infrascope_module()
    scheduled: list[tuple[str | None, str]] = []
    delayed: list[tuple[str, str | None, str, int]] = []

    monkeypatch.setattr(
        mod,
        "_HOT_REFRESH_BUDGET",
        mod.HotEventBudget(debounce_ms=1000, window_ms=5000, max_events=2),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda *, webspace_id=None, reason="runtime.event": scheduled.append((webspace_id, reason)),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_trailing_hot_refresh",
        lambda *, topic, webspace_id=None, reason="runtime.event", delay_ms=1000: delayed.append(
            (topic, webspace_id, reason, delay_ms)
        ),
    )

    evt = SimpleNamespace(type="browser.session.changed", payload={"webspace_id": "desktop"})
    mod.on_browser_runtime_changed(evt)
    mod.on_browser_runtime_changed(evt)

    assert scheduled == [("desktop", "browser.session.changed")]
    assert len(delayed) == 1
    assert delayed[0][0:3] == ("infrascope.browser_runtime.changed", "desktop", "browser.session.changed")
    assert delayed[0][3] > 0
    assert mod._hot_refresh_diag["suppressed_total"] >= 1


def test_infrascope_member_snapshot_refresh_is_budgeted_with_trailing_refresh(monkeypatch):
    mod = _load_infrascope_module()
    scheduled: list[tuple[str | None, str]] = []
    delayed: list[tuple[str, str | None, str, int]] = []

    monkeypatch.setattr(
        mod,
        "_HOT_REFRESH_BUDGET",
        mod.HotEventBudget(debounce_ms=1000, window_ms=5000, max_events=2),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_snapshot_refresh",
        lambda *, webspace_id=None, reason="runtime.event": scheduled.append((webspace_id, reason)),
    )
    monkeypatch.setattr(
        mod,
        "_schedule_trailing_hot_refresh",
        lambda *, topic, webspace_id=None, reason="runtime.event", delay_ms=1000: delayed.append(
            (topic, webspace_id, reason, delay_ms)
        ),
    )

    evt = SimpleNamespace(type="subnet.member.snapshot.changed", payload={"webspace_id": "desktop"})
    mod.on_runtime_event(evt)
    mod.on_runtime_event(evt)

    assert scheduled == [("desktop", "subnet.member.snapshot.changed")]
    assert len(delayed) == 1
    assert delayed[0][0:3] == (
        "infrascope.member_snapshot.changed",
        "desktop",
        "subnet.member.snapshot.changed",
    )
    assert delayed[0][3] > 0
    assert mod._hot_refresh_diag["last_event"] == "subnet.member.snapshot.changed"


def test_infrascope_scenario_declares_inventory_drilldown_and_inspector_flow():
    root = Path(__file__).resolve().parents[1]
    scenario_path = root / ".adaos" / "workspace" / "scenarios" / "infrascope" / "scenario.json"
    skill_path = root / ".adaos" / "workspace" / "skills" / "infrascope_skill" / "skill.yaml"

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    skill_yaml = skill_path.read_text(encoding="utf-8")
    widgets = {item["id"]: item for item in scenario["ui"]["application"]["desktop"]["pageSchema"]["widgets"]}

    inventory = widgets["inventory-list"]
    incidents = widgets["overview-incidents"]
    operations = widgets["overview-operations"]
    summary = widgets["selected-object-summary"]
    mode = widgets["infrascope-mode"]
    inventory_tabs = widgets["infrascope-inventory-tabs"]
    inspector_tabs = widgets["inspector-tabs"]

    assert scenario["type"] == "desktop"
    assert inventory["visibleIf"] == "$state.infrascopeMode === 'inventory'"
    assert inventory["dataSource"]["kind"] == "stream"
    assert inventory["dataSource"]["receiver"] == "infrascope.inventory.$state.inventoryKind"
    assert "refreshMs" not in inventory.get("inputs", {})
    assert incidents["actions"][0]["params"]["inspectorTab"] == "incidents"
    assert operations["dataSource"]["kind"] == "stream"
    assert operations["dataSource"]["receiver"] == "infrascope.operations.active"
    assert summary["dataSource"]["kind"] == "stream"
    assert summary["dataSource"]["receiver"] == "infrascope.inspector.$state.selectedObjectId"
    assert widgets["overview-summary"]["dataSource"]["path"] == "data/infrascope/summary"
    assert mode["inputs"]["selectedStateKey"] == "infrascopeMode"
    assert inventory_tabs["inputs"]["selectedStateKey"] == "inventoryKind"
    assert inspector_tabs["inputs"]["selectedStateKey"] == "inspectorTab"
    assert "get_overview_summary" in skill_yaml
    assert "get_object_inspector" in skill_yaml
    assert "get_snapshot" in skill_yaml
    assert "refresh_snapshot" in skill_yaml
    assert "data_projections" in skill_yaml
    assert "infrascope.snapshot" in skill_yaml
    assert "device.registered" in skill_yaml
    assert "browser.session.changed" in skill_yaml
    assert "webrtc.peer.state.changed" in skill_yaml
    assert "workspace." in skill_yaml
    assert "user.profile.changed" in skill_yaml
    assert "capacity.changed" in skill_yaml


def test_infrascope_stream_snapshot_request_publishes_requested_receiver(monkeypatch):
    mod = _load_infrascope_module()

    published: list[tuple[str, object, dict[str, object] | None]] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **kwargs: published.append((receiver, data, kwargs.get("_meta"))) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_last_good_snapshots",
        {
            "ws-1": {
                "inventory": {"members": [{"id": "member-1"}]},
                "operations": {"items": [{"id": "op-1"}]},
                "inspectors": {"local": {"object_id": "local"}, "member-1": {"object_id": "member-1"}},
            }
        },
    )
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [{"id": "member-1"}])

    mod.on_webio_stream_snapshot_requested(
        {
            "webspace_id": "ws-1",
            "receiver": "infrascope.inventory.members",
        }
    )

    assert published == [
        (
            "infrascope.inventory.members",
            [
                {
                    "id": "member-1",
                    "object_id": "member-1",
                    "details_receiver": "infrascope.inspector.member-1",
                    "has_details": True,
                }
            ],
            {"webspace_id": "ws-1"},
        )
    ]


def test_infrascope_inspector_stream_uses_compact_summary_payload(monkeypatch):
    mod = _load_infrascope_module()

    payload = mod._stream_payload_for_receiver(
        {
            "inspectors": {
                "local": {
                    "label": "hub",
                    "value": "warning",
                    "subtitle": "Local hub",
                    "description": "summary only",
                    "object_id": "local",
                    "object_title": "Local hub",
                    "object": {
                        "id": "local",
                        "kind": "hub",
                        "title": "Local hub",
                        "status": "warning",
                        "raw": "large-object-payload",
                    },
                    "topology": {"edges": [{"from": "local", "to": "member"}]},
                    "task_packet": {"context": {"very": "large"}},
                    "subnet_planning": {"details": ["large"]},
                    "actions": [{"id": "restart"}],
                }
            }
        },
        "infrascope.inspector.local",
    )

    assert payload["object_id"] == "local"
    assert payload["object"]["kind"] == "hub"
    assert "topology" not in payload
    assert "task_packet" not in payload
    assert "subnet_planning" not in payload
    assert "actions" not in payload


def test_infrascope_large_detail_stream_payload_is_truncated(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setenv("INFRASCOPE_STREAM_PAYLOAD_MAX_BYTES", "8192")

    payload = mod._fit_stream_payload(
        "infrascope.inspector_field.task_packet.local",
        {"blob": "x" * 20000},
    )

    assert payload["status"] == "truncated"
    assert "too large" in payload["warning"]


def test_infrascope_stream_snapshot_request_uses_direct_receiver_builders(monkeypatch):
    mod = _load_infrascope_module()

    published: list[tuple[str, object]] = []
    calls = {"snapshot": 0}

    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **kwargs: published.append((receiver, data)) or {"ok": True},
    )
    monkeypatch.setattr(mod, "_last_good_snapshots", {})
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [{"id": "member-1", "details": {"large": "payload"}}])
    monkeypatch.setattr(mod, "_operation_rows", lambda webspace_id=None: [{"id": "op-1"}])

    def _fake_snapshot_or_fallback(webspace_id=None, task_goal=None):
        calls["snapshot"] += 1
        raise AssertionError("direct stream receivers must not build a full snapshot")

    monkeypatch.setattr(mod, "_snapshot_or_fallback", _fake_snapshot_or_fallback)

    mod.on_webio_stream_snapshot_requested(
        {"webspace_id": "ws-1", "receiver": "infrascope.inventory.members"},
    )
    mod.on_webio_stream_snapshot_requested(
        {"webspace_id": "ws-1", "receiver": "infrascope.operations.active"},
    )

    assert calls["snapshot"] == 0
    assert [item[0] for item in published] == [
        "infrascope.inventory.members",
        "infrascope.operations.active",
    ]
    inventory_payload = published[0][1]
    assert inventory_payload == [
        {
            "id": "member-1",
            "object_id": "member-1",
            "details_receiver": "infrascope.inspector.member-1",
            "has_details": True,
        }
    ]


def test_infrascope_overview_stream_snapshot_uses_compact_direct_builder(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("hub:local", "hub", "Local hub", status="online")
    member = _FakeCanonicalObject("member-1", "member", "Kitchen member", status="warning", summary="link flaps")
    projection = SimpleNamespace(
        subject=local,
        objects=[member],
        context={
            "health_strip": [
                {
                    "id": "health:members",
                    "object_id": "member-1",
                    "title": "Members",
                    "summary": "Issues: Kitchen member",
                    "details": [{"large": "payload"}],
                }
            ],
        },
    )
    published: list[tuple[str, object]] = []

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda receiver, data=None, **kwargs: published.append((receiver, data)) or {"ok": True},
    )
    monkeypatch.setattr(
        mod,
        "_snapshot_or_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("overview stream must not build full snapshot")),
    )

    mod.on_webio_stream_snapshot_requested(
        {"webspace_id": "ws-1", "receiver": "infrascope.overview.health_strip"},
    )

    assert published[0][0] == "infrascope.overview.health_strip"
    assert "details" not in published[0][1][0]
    assert published[0][1][0]["details_receiver"] == "infrascope.inspector.member-1"
    diagnostics = mod.get_projection_diagnostics()
    stream_diag = diagnostics["stream_payloads"]
    assert stream_diag["direct_last_receiver"] == "infrascope.overview.health_strip"
    assert stream_diag["direct_last_bytes"] > 0
    assert stream_diag["overview_section_bytes"]["health_strip"] > 0


def test_infrascope_status_cards_are_compact_route_refs(monkeypatch):
    mod = _load_infrascope_module()
    published: list[tuple[list[dict[str, object]], dict[str, object]]] = []

    snapshot = {
        "summary": {"value": "warning", "subtitle": "link flaps"},
        "overview": {
            "health_strip": [{"id": "health:member-1", "status": "warning", "details": {"large": "payload"}}],
            "active_incidents": [{"id": "incident:member-1", "status": "warning", "summary": "link flaps"}],
            "active_operations": [{"id": "op-1", "status": "warning", "details": {"large": "payload"}}],
        },
        "inventory": {
            "all": [{"id": "hub-1"}, {"id": "member-1"}],
            "hubs": [{"id": "hub-1"}],
            "members": [{"id": "member-1"}],
            "browsers": [{"id": "browser-1", "status": "online", "details": {"large": "payload"}}],
            "runtimes": [{"id": "runtime-1", "status": "degraded"}],
            "skills": [{"id": "skill:a", "status": "online"}],
            "scenarios": [{"id": "scenario:a", "status": "online"}],
        },
        "operations": {"items": [{"id": "op-1", "status": "warning"}]},
        "meta": {"partial": False, "error_total": 0},
    }

    monkeypatch.setattr(
        mod.status_sdk,
        "publish_status_many",
        lambda cards, **kwargs: published.append((list(cards), dict(kwargs))) or {"ok": True, "published": len(list(cards))},
    )

    cards = mod._infrascope_status_cards(snapshot, webspace_id="desktop")
    by_id = {card["id"]: card for card in cards}
    mod._publish_status_cards(snapshot, webspace_id="desktop")

    assert set(by_id) == {"overview", "active_incidents", "inventory", "browser_runtime", "registry", "operations"}
    assert by_id["overview"]["details_ref"]["receiver"] == "infrascope.overview.health_strip"
    assert by_id["active_incidents"]["status"] == "warning"
    assert by_id["browser_runtime"]["status"] == "degraded"
    assert by_id["inventory"]["metadata"]["objects"] == 2
    assert "details" not in by_id["inventory"]
    assert published[0][1]["owner"] == "skill:infrascope_skill"
    assert published[0][1]["scope"] == "infrascope"
    assert published[0][1]["_meta"] == {"webspace_id": "desktop"}


def test_infrascope_adds_skill_migration_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(
        mod,
        "_skill_runtime_migration_report",
        lambda: {
            "total": 2,
            "failed_total": 1,
            "lifecycle_failed_total": 1,
            "rollback_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {"skill": "voice_skill", "ok": False, "failure_kind": "lifecycle", "failed_stage": "rehydrate"},
            ],
        },
    )

    snapshot = mod._snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-runtime-migration"
    assert rows[0]["status"] == "offline"
    assert "voice_skill:lifecycle/rehydrate" in rows[0]["subtitle"]
    assert "lifecycle_failed=1" in rows[0]["subtitle"]


def test_infrascope_adds_skill_rollback_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda: {})
    monkeypatch.setattr(
        mod,
        "_skill_runtime_rollback_report",
        lambda: {
            "total": 3,
            "failed_total": 1,
            "rollback_total": 2,
            "skipped_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {"skill": "voice_skill", "ok": False, "error": "broken rollback"},
                {"skill": "maps_skill", "ok": True, "skipped": True},
            ],
        },
    )

    snapshot = mod._snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-runtime-rollback"
    assert rows[0]["status"] == "offline"
    assert "2/3 rolled back" in rows[0]["subtitle"]


def test_infrascope_adds_skill_post_commit_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda: {})
    monkeypatch.setattr(
        mod,
        "_skill_post_commit_checks_report",
        lambda: {
            "total": 2,
            "failed_total": 1,
            "lifecycle_failed_total": 1,
            "deactivated_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {
                    "skill": "voice_skill",
                    "ok": False,
                    "failure_kind": "lifecycle",
                    "failed_stage": "rehydrate",
                    "deactivated": True,
                    "deactivation": {
                        "committed_core_switch": True,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                    },
                },
            ],
        },
    )

    snapshot = mod._snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-post-commit-checks"
    assert rows[0]["status"] == "offline"
    assert "voice_skill:lifecycle/rehydrate" in rows[0]["subtitle"]
    assert "lifecycle_failed=1" in rows[0]["subtitle"]
    assert "deactivated=1" in rows[0]["subtitle"]
    assert "quarantine=voice_skill:lifecycle/rehydrate" in rows[0]["subtitle"]


def test_infrascope_adds_existing_quarantine_to_post_commit_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda: {})
    monkeypatch.setattr(
        mod,
        "_skill_post_commit_checks_report",
        lambda: {
            "total": 1,
            "failed_total": 0,
            "deactivated_total": 1,
            "skills": [
                {
                    "skill": "voice_skill",
                    "ok": True,
                    "skipped": True,
                    "deactivated": True,
                    "deactivation": {
                        "committed_core_switch": True,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                    },
                }
            ],
        },
    )

    snapshot = mod._snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-post-commit-checks"
    assert "quarantine=voice_skill:lifecycle/rehydrate" in rows[0]["subtitle"]


def test_infrascope_list_inventory_returns_local_fallback_when_empty(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])

    rows = mod.list_inventory("all", webspace_id="ws-1")

    assert rows
    assert rows[0]["object_id"] == "local"
    assert rows[0]["title"] == "Local node"


def test_infrascope_fallback_snapshot_keeps_local_inventory():
    mod = _load_infrascope_module()

    snapshot = mod._fallback_snapshot(FileNotFoundError("missing snapshot"), webspace_id="ws-1")

    assert snapshot["inventory"]["all"]
    assert snapshot["inventory"]["all"][0]["object_id"] == "local"
    assert snapshot["inventory"]["hubs"][0]["object_id"] == "local"


def test_infrascope_snapshot_keeps_partial_yjs_payload_when_one_inspector_fails(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("local", "hub", "Local hub", status="online")
    member = _FakeCanonicalObject("member-1", "member", "Kitchen member", status="degraded")
    projection = SimpleNamespace(
        subject=local,
        objects=[member],
        context={
            "summary_tile": {"label": "scope", "value": "warning", "subtitle": "partial"},
            "health_strip": [],
            "active_incidents": [],
            "quota_summary": [],
            "active_runtimes": [],
            "recent_changes": [],
        },
    )

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [local, member])

    def _inspector(object_id, task_goal=None, webspace_id=None):
        if object_id == "member-1":
            raise FileNotFoundError("missing member artifact")
        return {
            "object_id": "local",
            "object_title": "Local hub",
            "label": "hub",
            "value": "online",
            "topology": {"edges": []},
            "task_packet": {},
        }

    monkeypatch.setattr(mod, "get_object_inspector", _inspector)

    snapshot = mod._snapshot(webspace_id="ws-1")

    assert snapshot["summary"]["value"] == "warning"
    assert snapshot["inventory"]["all"]
    member_payload = mod._inspector_payload_from_snapshot(snapshot, "member-1")
    assert member_payload["warning"] == "FileNotFoundError: missing member artifact"
    assert snapshot["summary"]["object_id"] == "local"
    assert "warning" not in snapshot["summary"]
    assert "errors" not in snapshot
    assert snapshot["meta"]["partial"] is False
    assert snapshot["meta"]["error_total"] == 0


def test_infrascope_snapshot_reuses_last_good_snapshot_when_refresh_fails(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("local", "hub", "Local hub", status="online")
    projection = SimpleNamespace(
        subject=local,
        objects=[],
        context={
            "summary_tile": {"label": "scope", "value": "online", "subtitle": "steady"},
            "health_strip": [],
            "active_incidents": [],
            "quota_summary": [],
            "active_runtimes": [],
            "recent_changes": [],
        },
    )

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [local])
    monkeypatch.setattr(
        mod,
        "get_object_inspector",
        lambda object_id, task_goal=None, webspace_id=None: {
            "object_id": "local",
            "object_title": "Local hub",
            "label": "hub",
            "value": "online",
            "topology": {"edges": []},
            "task_packet": {},
        },
    )

    first = mod._snapshot_or_fallback(webspace_id="ws-1")
    assert first["summary"]["value"] == "online"

    def _raise_snapshot(*args, **kwargs):
        raise FileNotFoundError("missing control-plane file")

    monkeypatch.setattr(mod, "_snapshot", _raise_snapshot)

    second = mod._snapshot_or_fallback(webspace_id="ws-1")

    assert second["summary"]["value"] == "online"
    assert second["summary"]["warning"] == "FileNotFoundError: missing control-plane file"
    assert second["meta"]["stale"] is True
    assert second["inventory"]["all"][0]["object_id"] == "local"
