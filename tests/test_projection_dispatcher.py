from __future__ import annotations

from adaos.domain import Event, make_client_subscription_record, make_projection_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import (
    clear_projection_dispatcher,
    core_skill_refresh_contract_snapshot,
    demanded_projection_refresh_contexts,
    dispatch_demanded_projection_refresh,
    projection_dispatcher_memory_contract_snapshot,
    projection_dispatcher_snapshot,
    register_projection_refresh_handler,
)
from adaos.services.projection_records import clear_projection_record_registry, get_projection_record


def setup_function() -> None:
    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_projection_record_registry()


def _write_demand(
    webspace_id: str,
    projection_key: str,
    *,
    client_id: str | None = None,
    consumer_id: str = "widget:runtime",
    session_id: str = "session-1",
) -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id=client_id or f"browser-{webspace_id}",
            device_id="desktop",
            session_id=session_id,
            webspace_id=webspace_id,
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key=projection_key,
                    consumer_id=consumer_id,
                    consumer_kind=consumer_id.split(":", 1)[0],
                )
            ],
        )
    )


def test_dispatcher_selects_only_demanded_projection_in_event_webspace() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("dev", "status-card:runtime")
    event = Event(
        type="node.status",
        payload={"webspace_id": "desktop"},
        source="test",
        ts=20.0,
    )

    contexts = demanded_projection_refresh_contexts(event, now=20.0)

    assert [(item.webspace_id, item.projection_key) for item in contexts] == [
        ("desktop", "status-card:runtime")
    ]


def test_dispatcher_does_not_cross_webspace_when_explicit_scope_is_used() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("dev", "status-card:runtime")
    refreshed: list[tuple[str, str]] = []

    def _handler(context):
        refreshed.append((context.webspace_id, context.projection_key))
        return {"status": "ready", "data": {"ok": True}}

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, webspace_ids=["dev"], now=20.0))

    assert refreshed == [("dev", "status-card:runtime")]
    assert [(item.webspace_id, item.projection_key) for item in report.selected] == [
        ("dev", "status-card:runtime")
    ]
    assert len(report.refreshed) == 1


def test_dispatcher_skips_demand_without_registered_handler() -> None:
    _write_demand("desktop", "projection:missing")
    event = Event(type="demo.event", payload={"webspace_id": "desktop"}, source="test", ts=20.0)

    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))

    assert len(report.refreshed) == 0
    assert len(report.skipped) == 1
    assert report.skipped[0].projection_key == "projection:missing"
    assert report.skipped[0].reason == "no_handler"
    snapshot = projection_dispatcher_snapshot()
    assert snapshot["stats"]["skipped_total"] == 1
    assert snapshot["lifecycle"][0]["status"] == "stale"


def test_dispatcher_can_filter_projection_keys() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("desktop", "projection:hub/overview", consumer_id="page:infrascope")
    event = Event(type="demo.event", payload={"webspace_id": "desktop"}, source="test", ts=20.0)

    contexts = demanded_projection_refresh_contexts(
        event,
        projection_keys=["projection:hub/overview"],
        now=20.0,
    )

    assert [(item.webspace_id, item.projection_key) for item in contexts] == [
        ("desktop", "projection:hub/overview")
    ]


def test_core_skill_refresh_contract_reports_handler_coverage() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("desktop", "projection:missing", consumer_id="widget:missing", session_id="session-2")

    def _handler(context):
        return {"status": "ready", "data": {"projection_key": context.projection_key}}

    register_projection_refresh_handler("status-card:*", _handler)

    snapshot = core_skill_refresh_contract_snapshot(
        Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0),
        now=20.0,
    )

    demands = {item["projection_key"]: item for item in snapshot["demands"]}
    assert snapshot["contract"] == "adaos.core-skill-projection-refresh.v1"
    assert snapshot["demand_total"] == 2
    assert snapshot["covered_total"] == 1
    assert snapshot["uncovered_total"] == 1
    assert snapshot["uncovered_projection_keys"] == ["projection:missing"]
    assert snapshot["readiness"]["ready_for_dispatch"] is False
    assert snapshot["readiness"]["coverage_ratio"] == 0.5
    assert snapshot["readiness"]["status"] == "warn"
    assert demands["status-card:runtime"]["handler"] == {
        "covered": True,
        "key": "status-card:*",
        "kind": "wildcard",
    }
    assert demands["projection:missing"]["handler"]["covered"] is False
    assert "ProjectionRecord materialization" in demands["status-card:runtime"]["ownership"]["core_owned"]
    assert "payload refresh" in demands["status-card:runtime"]["ownership"]["skill_owned"]
    assert demands["projection:missing"]["ownership"]["skill_owned"] == []
    assert "browser writes to data/projectionRecords" in demands["status-card:runtime"]["ownership"]["forbidden"]
    assert demands["status-card:runtime"]["refresh_contract"]["core_selects_demand"] is True
    assert demands["status-card:runtime"]["refresh_contract"]["skill_refreshes_payload"] is True
    assert demands["projection:missing"]["refresh_contract"]["skill_refreshes_payload"] is False


def test_dispatcher_memory_contract_allows_rich_memory_but_compact_yjs() -> None:
    snapshot = projection_dispatcher_memory_contract_snapshot(now=70.0)

    assert snapshot["contract"] == "adaos.projection-dispatcher.memory-vs-yjs.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 70.0
    assert "domain-specific source snapshots" in snapshot["memory_allowed"]
    assert snapshot["yjs_publication"]["path"] == "data/projectionRecords"
    assert snapshot["dispatcher_boundaries"]["handler_writes_yjs_directly"] is False
    assert snapshot["dispatcher_boundaries"]["browser_writes_yjs_cache"] is False
    assert "/api/node/projection-runtime-ownership" in snapshot["evidence"]


def test_dispatcher_groups_multiple_clients_into_one_projection_context() -> None:
    _write_demand(
        "desktop",
        "status-card:runtime",
        client_id="browser-a",
        consumer_id="widget:runtime",
        session_id="session-1",
    )
    _write_demand(
        "desktop",
        "status-card:runtime",
        client_id="browser-b",
        consumer_id="panel:runtime",
        session_id="session-2",
    )
    handled: list[tuple[str, int]] = []

    def _handler(context):
        handled.append((context.projection_key, len(context.consumers)))
        return {"status": "ready", "data": {"consumer_total": len(context.consumers)}}

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))

    assert len(report.selected) == 1
    assert [item.consumer_id for item in report.selected[0].consumers] == [
        "panel:runtime",
        "widget:runtime",
    ]
    assert handled == [("status-card:runtime", 2)]
    assert report.refreshed[0].record["data"]["consumer_total"] == 2


def test_dispatcher_records_ready_lifecycle_and_pressure_stats() -> None:
    _write_demand("desktop", "status-card:runtime")

    def _handler(context):
        return {"status": "ready", "data": {"projection_key": context.projection_key}}

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))
    snapshot = projection_dispatcher_snapshot()

    assert len(report.refreshed) == 1
    assert report.refreshed[0].status == "ready"
    assert snapshot["stats"]["incoming_total"] == 1
    assert snapshot["stats"]["selected_total"] == 1
    assert snapshot["stats"]["refreshed_total"] == 1
    assert snapshot["lifecycle"][0]["status"] == "ready"
    assert snapshot["lifecycle"][0]["projection_key"] == "status-card:runtime"


def test_dispatcher_materializes_canonical_projection_records() -> None:
    _write_demand("desktop", "status-card:runtime")

    def _handler(context):
        return make_projection_record(
            projection_key=context.projection_key,
            kind="status-card",
            webspace_id=context.webspace_id,
            data={"summary": "Runtime ready"},
            updated_at=context.requested_at,
        ).to_dict()

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))
    stored = get_projection_record(webspace_id="desktop", projection_key="status-card:runtime")

    assert report.refreshed[0].record["meta"]["projection_key"] == "status-card:runtime"
    assert stored is not None
    assert stored.data == {"summary": "Runtime ready"}


def test_dispatcher_uses_wildcard_family_handler() -> None:
    _write_demand("desktop", "status-card:runtime")
    _write_demand("desktop", "status-card:link", session_id="session-2")
    handled: list[str] = []

    def _handler(context):
        handled.append(context.projection_key)
        return {"status": "ready", "data": {"projection_key": context.projection_key}}

    register_projection_refresh_handler("status-card:*", _handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))

    assert handled == ["status-card:link", "status-card:runtime"]
    assert [item.projection_key for item in report.refreshed] == ["status-card:link", "status-card:runtime"]


def test_dispatcher_prefers_exact_handler_over_wildcard_family_handler() -> None:
    _write_demand("desktop", "status-card:runtime")

    def _family_handler(_context):
        return {"status": "ready", "data": {"handler": "family"}}

    def _exact_handler(_context):
        return {"status": "ready", "data": {"handler": "exact"}}

    register_projection_refresh_handler("status-card:*", _family_handler)
    register_projection_refresh_handler("status-card:runtime", _exact_handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))

    assert report.refreshed[0].record["data"]["handler"] == "exact"


def test_dispatcher_records_handler_errors_without_crashing() -> None:
    _write_demand("desktop", "status-card:runtime")

    def _handler(_context):
        raise RuntimeError("boom")

    register_projection_refresh_handler("status-card:runtime", _handler)

    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)
    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))
    snapshot = projection_dispatcher_snapshot()

    assert len(report.errors) == 1
    assert report.errors[0].status == "error"
    assert snapshot["stats"]["error_total"] == 1
    assert snapshot["lifecycle"][0]["status"] == "error"
    assert "RuntimeError" in snapshot["lifecycle"][0]["error"]


def test_dispatcher_coalesces_refresh_already_in_progress() -> None:
    _write_demand("desktop", "status-card:runtime")
    nested_reports = []
    event = Event(type="node.status", payload={"webspace_id": "desktop"}, source="test", ts=20.0)

    async def _handler(_context):
        nested_reports.append(await dispatch_demanded_projection_refresh(event, now=21.0))
        return {"status": "ready"}

    register_projection_refresh_handler("status-card:runtime", _handler)

    report = _run(dispatch_demanded_projection_refresh(event, now=20.0))
    snapshot = projection_dispatcher_snapshot()

    assert len(report.refreshed) == 1
    assert len(nested_reports) == 1
    assert nested_reports[0].skipped[0].reason == "coalesced"
    assert snapshot["stats"]["coalesced_total"] == 1
    assert snapshot["stats"]["skipped_total"] == 1


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
