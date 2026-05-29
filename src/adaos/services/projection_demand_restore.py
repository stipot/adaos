from __future__ import annotations

from typing import Any


PROJECTION_DEMAND_RESTORE_CONTRACT = "adaos.projection-demand.restore-from-yjs.v1"


def projection_demand_restore_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return startup restoration rules for active projection demand."""

    return {
        "contract": PROJECTION_DEMAND_RESTORE_CONTRACT,
        "ready_for_mvp": True,
        "status": "implemented",
        "updated_at": float(now if now is not None else 0.0),
        "source_of_truth": {
            "active_demand": "/api/node/projection-demand",
            "yjs_path": "runtime/clients",
            "yjs_endpoint": "/api/node/projection-demand/yjs",
            "client_subscription_record": "adaos.client-projection-subscription.v1",
            "surface_lifecycle_mapping": "adaos.browser-surface-lifecycle-subscriptions.v1",
        },
        "runtime_helpers": {
            "projection_runtime": "ProjectionRuntime.restore_active_demand",
            "stream_runtime": "planned StreamRuntime.restore_active_demand",
        },
        "restore_modes": [
            {
                "runtime": "projection",
                "maps_projection_to": "registered projection slot",
                "active_state": "active_projection_demand",
                "optional_publish": False,
            },
            {
                "runtime": "stream",
                "maps_projection_to": "registered stream receiver",
                "active_state": "active_receivers",
                "optional_publish": True,
            },
        ],
        "filters": {
            "webspace_id": "restore only matching webspace when provided",
            "include_hidden": "hidden consumers are skipped unless explicitly included",
            "include_stale": "stale consumers are skipped unless explicitly included",
            "projection_prefix": "projection runtime can restrict restored projection keys",
            "receiver_prefix": "stream runtime can restrict restored projection keys",
        },
        "skip_reasons": [
            "projection_key_missing",
            "webspace_mismatch",
            "projection_prefix_mismatch",
            "receiver_prefix_mismatch",
            "hidden",
            "stale",
            "slot_mapping_empty",
            "slot_unregistered",
            "receiver_mapping_empty",
            "receiver_unregistered",
        ],
        "startup_sequence": [
            "read active client subscription records",
            "filter consumers by webspace, visibility, staleness, and prefix",
            "map projection_key to registered slot or receiver",
            "restore active demand in runtime memory",
            "optionally publish stream receiver snapshot on restore",
        ],
        "boundaries": {
            "core_reads_demand": True,
            "skill_restores_local_memory": True,
            "browser_writes_restore_state": False,
            "restore_writes_yjs_directly": False,
            "restore_writes_projection_payloads": False,
        },
        "evidence": [
            "/api/node/projection-demand",
            "/api/node/projection-demand/contract",
            "/api/node/projection-demand/yjs",
            "/api/node/projection-demand/yjs/materialize",
            "/api/node/projection-demand/yjs/restore",
            "ProjectionRuntime.restore_active_demand",
        ],
        "remaining_work": [
            "restore stream receiver demand during skill activation",
            "add regression coverage for stale and hidden consumers",
        ],
    }


__all__ = ["PROJECTION_DEMAND_RESTORE_CONTRACT", "projection_demand_restore_contract_snapshot"]
