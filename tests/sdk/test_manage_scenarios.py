from __future__ import annotations

from dataclasses import dataclass

import pytest

from adaos.sdk.errors import CapabilityError
from adaos.sdk.manage import scenarios_bind_set, scenarios_toggle
from adaos.services.agent_context import get_ctx


class AllowCaps:
    def __init__(self, allowed: set[str]):
        self._allowed = allowed

    def allows(self, capability: str) -> bool:
        return capability in self._allowed


@dataclass
class FakeScenarioRepo:
    existing: set[str]

    def ensure(self) -> None:
        pass

    def get(self, scenario_id: str):
        return scenario_id if scenario_id in self.existing else None


@pytest.fixture
def scenario_repo():
    ctx = get_ctx()
    repo = FakeScenarioRepo(existing={"morning"})
    object.__setattr__(ctx, "_scenarios_repo", repo)
    return repo


def test_scenarios_toggle_idempotent(scenario_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.scenarios.toggle"})

    res = scenarios_toggle("morning", True, request_id="req-toggle")
    assert res == {"status": "ok", "scenario_id": "morning", "enabled": True, "dry_run": False, "request_id": "req-toggle"}
    assert ctx.kv.get("scenarios/morning/enabled") is True

    # repeat with same request id returns cached result
    assert scenarios_toggle("morning", False, request_id="req-toggle") == res


def test_scenarios_toggle_dry_run(scenario_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.scenarios.toggle"})

    preview = scenarios_toggle("morning", False, request_id="req-dry", dry_run=True)
    assert preview["dry_run"] is True
    assert ctx.kv.get("scenarios/morning/enabled") is None


def test_scenarios_bind_set_not_found(scenario_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.scenarios.bind"})
    scenario_repo.existing.clear()

    res = scenarios_bind_set("missing", key="mic", value="default", request_id="req-bind")
    assert res["status"] == "error"
    assert res["code"] == "not_found"


def test_scenarios_bind_set_ok(scenario_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.scenarios.bind"})

    res = scenarios_bind_set("morning", key="mic", value="default", request_id="req-bind-ok")
    assert res["status"] == "ok"
    assert ctx.kv.get("scenarios/morning/bindings/mic") == "default"


def test_scenarios_toggle_missing_capability(scenario_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps(set())
    with pytest.raises(CapabilityError):
        scenarios_toggle("morning", True, request_id="req-no-cap")
