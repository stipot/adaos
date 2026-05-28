from __future__ import annotations

from adaos.services.projection_runtime_ownership import projection_runtime_ownership_contract_snapshot


def test_projection_runtime_ownership_contract_snapshot_defines_boundaries() -> None:
    snapshot = projection_runtime_ownership_contract_snapshot(now=50.0)

    assert snapshot["contract"] == "adaos.projection-runtime-ownership.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 50.0
    assert snapshot["boundary_total"] == 5
    assert snapshot["summary"]["core"].startswith("Owns event normalization")
    assert "/api/node/projection-demand/contract" in snapshot["evidence"]
    boundaries = {item["area"]: item for item in snapshot["boundaries"]}
    assert "write shared data/projectionRecords cache" in boundaries["yjs_projection_cache"]["core_owned"]
    assert "read demanded projection cache" in boundaries["yjs_projection_cache"]["browser_owned"]
    assert "source richer in-memory payload state before publication" in boundaries["yjs_projection_cache"]["skill_owned"]
    assert "broad browser writes to shared projection cache" in boundaries["yjs_projection_cache"]["forbidden"]
