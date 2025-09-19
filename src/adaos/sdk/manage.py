"""Control-plane helpers exposed to skills via the SDK."""

from __future__ import annotations

from ._caps import require_capability
from ._runtime import require_ctx
from .validation.skill import ValidationReport, validate_self as _validate_self

__all__ = ["self_validate", "scenario_toggle"]


def self_validate(strict: bool = False, probe_tools: bool = False) -> ValidationReport:
    return _validate_self(strict=strict, probe_tools=probe_tools)


def scenario_toggle(scenario_id: str, enabled: bool) -> None:
    ctx = require_ctx("sdk.manage.scenario_toggle")
    require_capability(ctx, "manage.scenarios.toggle")

    svc = getattr(ctx, "scenario_control", None) or getattr(ctx, "scenario_manager", None)
    if svc is None:
        raise NotImplementedError("Scenario toggle service is not wired into the current runtime")

    toggle = getattr(svc, "toggle", None) or getattr(svc, "set_enabled", None)
    if toggle is None:
        raise NotImplementedError("Scenario service does not expose toggle() or set_enabled()")

    toggle(scenario_id, enabled)
