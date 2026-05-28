from __future__ import annotations

import time
from typing import Any


PROJECTION_PILOT_READINESS_CONTRACT = "adaos.projection-pilot.readiness.v1"


def projection_pilot_readiness_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the pilot ordering contract for projection migration follow-ups."""

    prerequisites = [
        "event_envelope_contract",
        "browser_demand_contract",
        "runtime_ownership_contract",
        "dispatcher_memory_contract",
        "browser_adapter_contract",
        "platform_status_card_projection_contract",
    ]
    return {
        "contract": PROJECTION_PILOT_READINESS_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else time.time()),
        "pilot_order": [
            "status_cards_first",
            "platform_surfaces_first",
            "platform_emitter_validated",
            "infrastate_aligned",
            "infrascope_after_prereqs",
            "prompt_engineer_scenario_followup",
            "simple_skills_deferred",
        ],
        "infrascope_after_prereqs": {
            "selected": False,
            "skill_id": "infrascope_skill",
            "status": "blocked_until_platform_emitter_validation",
            "required_gate_ids": prerequisites,
            "reason": "Infrascope is the heavy-skill pilot and must wait until the shared ABI, browser cache, dispatcher, and platform status-card emitter are accepted.",
        },
        "dev_scenario_followup": {
            "selected": True,
            "scenario_id": "prompt_engineer_scenario",
            "skill_id": "prompt_engineer_skill",
            "status": "selected",
            "selection_reason": "dev-oriented workflow exercises non-operator scenario surfaces without starting from a low-churn trivial skill",
            "expected_checks": [
                "scenario manifest is present",
                "skill webui links scenario:prompt_engineer_scenario",
                "projection demand uses shared browser adapter",
                "no new skill-specific projection ABI",
            ],
        },
        "simple_skills_deferred": {
            "selected": True,
            "status": "deferred_until_adapter_stable",
            "reason": "low-churn skills are poor early indicators for multi-surface projection pressure",
            "resume_after": [
                "prompt_engineer_scenario follow-up contract is accepted",
                "rollout inventory marks remaining candidates",
                "legacy cleanup plan is bounded",
            ],
        },
        "boundaries": {
            "does_not_skip_prerequisites": True,
            "does_not_create_pilot_specific_abi": True,
            "keeps_simple_skills_out_of_first_validation_loop": True,
            "uses_acceptance_summary_as_gate_source": True,
        },
        "evidence": [
            "/api/node/projection-records/browser-cache",
            "/api/node/projection-dispatcher/core-skill-contract",
            "/api/node/projection-platform-emitters",
            "prompt_engineer_scenario",
            "prompt_engineer_skill",
        ],
    }


__all__ = ["PROJECTION_PILOT_READINESS_CONTRACT", "projection_pilot_readiness_contract_snapshot"]
