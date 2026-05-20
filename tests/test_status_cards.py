from __future__ import annotations

from types import SimpleNamespace

from adaos.domain import Event
from adaos.sdk import status as sdk_status
from adaos.services.eventbus import LocalEventBus
from adaos.services.status import (
    HotEventBudget,
    StatusRegistry,
    guard_status_cards_from_runtime,
    make_status_card,
    register_status_registry,
)


def test_status_card_normalizes_canonical_status_and_staleness() -> None:
    card = make_status_card(
        id="runtime",
        owner="skill:infrastate_skill",
        kind="runtime",
        scope="infrastate",
        status="ready",
        summary="ready",
        updated_at=10.0,
        ttl_ms=1000,
        details_ref={"kind": "stream", "receiver": "infrastate.runtime"},
        route={"kind": "stream", "receiver": "infrastate.runtime"},
    )

    payload = card.to_dict(now_ts=12.0)

    assert payload["status"] == "online"
    assert payload["severity"] == "info"
    assert payload["stale"] is True
    assert payload["expires_at"] == 11.0
    assert payload["details_ref"]["receiver"] == "infrastate.runtime"


def test_status_card_fingerprint_ignores_volatile_times() -> None:
    first = make_status_card(
        id="runtime",
        owner="skill:infrastate_skill",
        kind="runtime",
        scope="infrastate",
        status="warning",
        summary="degraded",
        updated_at=10.0,
    )
    second = make_status_card(
        id="runtime",
        owner="skill:infrastate_skill",
        kind="runtime",
        scope="infrastate",
        status="warning",
        summary="degraded",
        updated_at=20.0,
        version=9,
    )

    assert first.fingerprint == second.fingerprint
    assert make_status_card(
        id="runtime",
        owner="skill:infrastate_skill",
        kind="runtime",
        scope="infrastate",
        status="degraded",
        summary="changed",
        fingerprint=first.fingerprint,
    ).fingerprint != first.fingerprint


def test_status_registry_dedupes_versions_and_marks_stale() -> None:
    registry = StatusRegistry()

    first = registry.publish(
        {
            "id": "runtime",
            "owner": "skill:infrastate_skill",
            "kind": "runtime",
            "scope": "infrastate",
            "status": "ready",
            "summary": "ready",
            "updated_at": 10.0,
            "ttl_ms": 1000,
        }
    )
    duplicate = registry.publish(
        {
            "id": "runtime",
            "owner": "skill:infrastate_skill",
            "kind": "runtime",
            "scope": "infrastate",
            "status": "ready",
            "summary": "ready",
            "updated_at": 10.5,
            "ttl_ms": 1000,
        }
    )
    changed = registry.publish(
        {
            "id": "runtime",
            "owner": "skill:infrastate_skill",
            "kind": "runtime",
            "scope": "infrastate",
            "status": "degraded",
            "summary": "route unstable",
            "updated_at": 12.0,
            "ttl_ms": 1000,
        }
    )
    snapshot = registry.snapshot(now_ts=14.0)

    assert first["changed"] is True
    assert duplicate["changed"] is False
    assert duplicate["card"]["version"] == 1
    assert changed["card"]["version"] == 2
    assert snapshot["cards"][0]["stale"] is True
    assert snapshot["diagnostics"]["changed_total"] == 2
    assert snapshot["diagnostics"]["unchanged_total"] == 1


def test_status_registry_reports_oversized_card_boundary() -> None:
    registry = StatusRegistry(max_card_bytes=256)

    result = registry.publish(
        {
            "id": "runtime",
            "owner": "skill:infrastate_skill",
            "kind": "runtime",
            "scope": "infrastate",
            "status": "ready",
            "summary": "ready",
            "metadata": {"rows": ["x" * 512]},
        }
    )
    diagnostics = registry.diagnostics()

    assert result["changed"] is True
    assert diagnostics["oversized_card_total"] == 1
    assert diagnostics["max_card_bytes"] == 256
    assert diagnostics["max_card_bytes_observed"] > 256
    assert diagnostics["last_oversized_card"]["id"] == "runtime"


def test_register_status_registry_consumes_sdk_status_events(monkeypatch) -> None:
    bus = LocalEventBus()
    registry = register_status_registry(bus)
    changed: list[Event] = []
    bus.subscribe("adaos.status.card.changed", lambda event: changed.append(event))
    monkeypatch.setattr(sdk_status, "get_ctx", lambda: SimpleNamespace(bus=bus))
    monkeypatch.setattr(
        sdk_status,
        "get_current_skill",
        lambda: SimpleNamespace(name="infrastate_skill"),
    )

    result = sdk_status.publish_status(
        id="runtime",
        kind="runtime",
        scope="infrastate",
        status="ready",
        summary="ready",
        webspace_id="desktop",
    )
    snapshot = registry.snapshot(webspace_id="desktop")

    assert result["ok"] is True
    assert snapshot["total"] == 1
    assert snapshot["cards"][0]["owner"] == "skill:infrastate_skill"
    assert changed[0].payload["card"]["id"] == "runtime"


def test_publish_status_initializes_lazy_context_registry(monkeypatch) -> None:
    bus = LocalEventBus()

    class _Ctx:
        def __init__(self) -> None:
            self.bus = bus
            self._registry = None

        @property
        def status_registry(self):
            if self._registry is None:
                self._registry = register_status_registry(self.bus)
            return self._registry

    ctx = _Ctx()
    monkeypatch.setattr(sdk_status, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        sdk_status,
        "get_current_skill",
        lambda: SimpleNamespace(name="infrascope_skill"),
    )

    sdk_status.publish_status(
        id="overview",
        kind="overview",
        scope="infrascope",
        status="ready",
        summary="ready",
        webspace_id="desktop",
    )

    snapshot = ctx.status_registry.snapshot(webspace_id="desktop")
    assert snapshot["total"] == 1
    assert snapshot["cards"][0]["owner"] == "skill:infrascope_skill"
    assert snapshot["diagnostics"]["publish_total"] == 1


def test_agent_context_status_registry_registers_bus_subscription(tmp_path) -> None:
    from adaos.services.eventbus import emit
    from adaos.services.testing.bootstrap import bootstrap_test_ctx

    slot_dir = tmp_path / ".adaos" / "skills" / "infrastate_skill" / "slots" / "dev"
    slot_dir.mkdir(parents=True)
    handle = bootstrap_test_ctx(skill_name="infrastate_skill", skill_slot_dir=slot_dir, secrets={})
    try:
        registry = handle.ctx.status_registry
        emit(
            handle.ctx.bus,
            "adaos.status.card.single",
            {
                "card": {
                    "id": "runtime",
                    "owner": "skill:infrastate_skill",
                    "kind": "runtime",
                    "scope": "infrastate",
                    "status": "ready",
                }
            },
            "test",
        )

        snapshot = registry.snapshot()

        assert snapshot["total"] == 1
        assert snapshot["cards"][0]["status"] == "online"
    finally:
        handle.teardown()


def test_publish_status_stream_publishes_card_and_stream_variable(monkeypatch) -> None:
    bus = LocalEventBus()
    seen_status: list[Event] = []
    seen_stream: list[Event] = []
    bus.subscribe("adaos.status.card.batch", lambda event: seen_status.append(event))
    bus.subscribe("io.out.stream.publish", lambda event: seen_stream.append(event))
    monkeypatch.setattr(sdk_status, "get_ctx", lambda: SimpleNamespace(bus=bus))
    monkeypatch.setattr("adaos.sdk.io.out.get_ctx", lambda: SimpleNamespace(bus=bus))
    monkeypatch.setattr("adaos.sdk.io.out.load_config", lambda: SimpleNamespace(node_id="node-1"))
    monkeypatch.setattr(
        sdk_status,
        "get_current_skill",
        lambda: SimpleNamespace(name="infrastate_skill"),
    )
    monkeypatch.setattr(
        "adaos.sdk.io.out.get_current_skill",
        lambda: SimpleNamespace(name="infrastate_skill"),
    )

    result = sdk_status.publish_status_stream(
        "infrastate.runtime",
        id="runtime",
        kind="runtime",
        scope="infrastate",
        status="warning",
        summary="route reconnecting",
        webspace_id="desktop",
        ttl_ms=30000,
        seq=7,
        _meta={"webspace_id": "desktop"},
    )

    assert result["ok"] is True
    assert seen_status[0].payload["cards"][0]["details_ref"]["receiver"] == "infrastate.runtime"
    stream_payload = seen_stream[0].payload
    assert stream_payload["receiver"] == "infrastate.runtime"
    assert stream_payload["data"]["id"] == "runtime"
    assert stream_payload["data"]["seq"] == 7
    assert stream_payload["data"]["value"]["route"]["receiver"] == "infrastate.runtime"


def test_guard_status_cards_project_runtime_pressure() -> None:
    cards = guard_status_cards_from_runtime(
        {
            "yjs_pressure": {
                "webspace_id": "desktop",
                "owner": "skill:infrastate_skill",
                "policy_state": "block",
                "observed_state": "critical",
                "reason": "write_amplification_blocked",
                "recent_bytes": 1024,
                "recent_writes": 8,
                "last_route": {"kind": "yjs_projection", "surface": "subnet.infrastate.snapshot"},
                "quarantine_remaining_s": 30,
            },
            "webio_stream_guard": {
                "available": True,
                "webspace_id": "desktop",
                "items": [
                    {
                        "receiver": "infrastate.realtime",
                        "owner": "skill:infrastate_skill",
                        "surface": "widget:realtime",
                        "suppressed_total": 1,
                        "throttled_total": 2,
                        "declared_max_payload_bytes": 4096,
                        "last_reason": "payload_budget",
                    }
                ],
                "totals": {"attempted": 5, "published": 2, "suppressed": 1, "throttled": 2},
            },
            "eventbus_backlog": {
                "top_webio_stream_controls": [
                    {
                        "event_type": "webio.stream.snapshot.requested",
                        "webspace_id": "desktop",
                        "receiver": "infrascope.inspector.local",
                        "incoming_total": 3,
                        "queued_total": 9,
                        "superseded_total": 6,
                    }
                ]
            },
        },
        webspace_id="desktop",
        updated_at=10.0,
    )
    by_id = {card.id: card.to_dict(now_ts=10.0) for card in cards}

    assert by_id["guard:yjs_pressure"]["status"] == "degraded"
    assert by_id["guard:yjs_pressure"]["severity"] == "critical"
    assert by_id["guard:yjs_pressure"]["guard_ref"]["quarantine_ttl_s"] == 30.0
    assert by_id["guard:webio_stream"]["guard_ref"]["receiver"] == "infrastate.realtime"
    assert by_id["guard:webio_stream_control"]["status"] == "warning"
    assert by_id["guard:webio_stream_control"]["guard_ref"]["observed_pressure"]["superseded"] == 6


def test_hot_event_budget_debounces_and_tracks_window_budget() -> None:
    budget = HotEventBudget(debounce_ms=1000, window_ms=5000, max_events=2)

    first = budget.admit("browser.session.changed", key="desktop", now_ts=10.0)
    debounced = budget.admit("browser.session.changed", key="desktop", now_ts=10.5)
    second = budget.admit("browser.session.changed", key="desktop", now_ts=11.2)
    limited = budget.admit("browser.session.changed", key="desktop", now_ts=12.4)
    reset = budget.admit("browser.session.changed", key="desktop", now_ts=16.1)
    snapshot = budget.snapshot(now_ts=16.1)

    assert first.admitted is True
    assert debounced.admitted is False
    assert debounced.reason == "debounce"
    assert second.admitted is True
    assert limited.admitted is False
    assert limited.reason == "budget_exceeded"
    assert reset.admitted is True
    assert snapshot["items"][0]["suppressed_total"] == 2
