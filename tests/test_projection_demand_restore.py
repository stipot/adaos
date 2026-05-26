from __future__ import annotations

from adaos.services.projection_demand_restore import projection_demand_restore_contract_snapshot


def test_projection_demand_restore_contract_snapshot_exposes_startup_rules() -> None:
    snapshot = projection_demand_restore_contract_snapshot(now=80.0)

    assert snapshot["contract"] == "adaos.projection-demand.restore-from-yjs.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 80.0
    assert snapshot["runtime_helpers"]["projection_runtime"] == "ProjectionRuntime.restore_active_demand"
    assert snapshot["runtime_helpers"]["stream_runtime"] == "StreamRuntime.restore_active_demand"
    assert snapshot["restore_modes"][0]["active_state"] == "active_projection_demand"
    assert snapshot["restore_modes"][1]["optional_publish"] is True
    assert "hidden" in snapshot["skip_reasons"]
    assert "receiver_unregistered" in snapshot["skip_reasons"]
    assert snapshot["boundaries"]["browser_writes_restore_state"] is False
