from __future__ import annotations

from typing import Any


RUNTIME_OWNERSHIP_CONTRACT = "adaos.projection-runtime-ownership.v1"


def projection_runtime_ownership_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the projection runtime ownership split as an inspectable contract."""

    boundaries = [
        {
            "area": "event_envelope",
            "core_owned": ["normalize legacy/enriched events", "preserve trace, scope, and causal metadata"],
            "producer_owned": ["event type", "event payload", "source authority when known"],
            "browser_owned": [],
            "skill_owned": [],
            "forbidden": ["dispatcher-specific metadata before shared envelope normalization"],
        },
        {
            "area": "browser_demand",
            "core_owned": ["store active client subscription records", "compute demanded projection keys"],
            "browser_owned": ["write full active subscription set", "touch session liveness", "delete closed session demand"],
            "skill_owned": [],
            "forbidden": ["browser writes to data/projectionRecords", "partial browser demand patches without full session context"],
        },
        {
            "area": "refresh_dispatch",
            "core_owned": ["select demanded projections", "route to registered handlers", "materialize ProjectionRecord"],
            "skill_owned": ["refresh payload for owned projection keys", "return semantic status and error details"],
            "browser_owned": ["declare demand only"],
            "forbidden": ["skill selects browser demand", "skill writes canonical ProjectionRecord cache directly"],
        },
        {
            "area": "platform_emitters",
            "core_owned": ["publish runtime lifecycle status cards", "own platform status-card ABI"],
            "skill_owned": [],
            "browser_owned": [],
            "forbidden": ["platform diagnostics hidden inside a skill-local snapshot branch"],
        },
        {
            "area": "yjs_projection_cache",
            "core_owned": ["write shared data/projectionRecords cache", "write node-aware envelope"],
            "skill_owned": ["source richer in-memory payload state before publication"],
            "browser_owned": ["read demanded projection cache"],
            "forbidden": ["anonymous node ownership", "broad browser writes to shared projection cache"],
        },
        {
            "area": "platform_nodes_branch",
            "core_owned": ["reserve platform/nodes/<node_id>", "write node status, diagnostics, and projection summaries"],
            "skill_owned": [],
            "browser_owned": ["read platform node state"],
            "forbidden": ["skill writes to platform/nodes", "browser writes to platform/nodes"],
        },
    ]
    return {
        "contract": RUNTIME_OWNERSHIP_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else 0.0),
        "boundary_total": len(boundaries),
        "boundaries": boundaries,
        "summary": {
            "core": "Owns event normalization, demand registry, dispatch selection, and canonical ProjectionRecord materialization.",
            "browser": "Owns active subscription intent only; browser reads projection records but does not write the canonical cache.",
            "skill": "Owns semantic payload refresh for registered projections, not demand selection or cache ownership.",
            "platform": "Owns built-in operational status-card emitters through the shared ProjectionRecord/status-card ABI.",
        },
        "evidence": [
            "/api/node/event-envelope-contract",
            "/api/node/projection-demand/contract",
            "/api/node/projection-demand/surface-lifecycle-contract",
            "/api/node/projection-dispatcher/core-skill-contract",
            "/api/node/projection-platform-emitters",
            "/api/node/projection-records/yjs/read",
            "/api/node/platform/nodes/contract",
        ],
        "forbidden_total": sum(len(item.get("forbidden") or []) for item in boundaries),
    }


__all__ = ["RUNTIME_OWNERSHIP_CONTRACT", "projection_runtime_ownership_contract_snapshot"]
