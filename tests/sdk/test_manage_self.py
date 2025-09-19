from __future__ import annotations

from pathlib import Path

import pytest

from adaos.sdk.errors import CapabilityError, SdkRuntimeNotInitialized
from adaos.sdk.manage import self_state_get, self_state_put, self_update_request, self_validate
from adaos.services.agent_context import clear_ctx, get_ctx, set_ctx
from adaos.services.skill.update import SkillUpdateResult, SkillUpdateService
from adaos.services.skill.validation import Issue, SkillValidationService, ValidationReport


class AllowCaps:
    def __init__(self, allowed: set[str]):
        self._allowed = allowed

    def allows(self, capability: str) -> bool:
        return capability in self._allowed


def _ensure_skill(ctx, name: str = "demo") -> None:
    skill_dir = Path(ctx.paths.skills_dir()) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    ctx.skill_ctx.set(name, skill_dir)


def test_self_validate_ok(monkeypatch):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.self.validate"})
    _ensure_skill(ctx, "demo")

    report = ValidationReport(ok=True, issues=[Issue(level="warning", code="warn", message="note", where="skill.yaml")])
    monkeypatch.setattr(SkillValidationService, "validate", lambda self, skill_id, strict=False, probe_tools=False: report)

    result = self_validate(strict=True, probe_tools=True)
    assert result["ok"] is True
    assert result["issues"] == [
        {"level": "warning", "code": "warn", "message": "note", "where": "skill.yaml"}
    ]


def test_self_validate_missing_capability():
    ctx = get_ctx()
    ctx.caps = AllowCaps(set())
    with pytest.raises(CapabilityError):
        self_validate()


def test_self_validate_without_context():
    ctx = get_ctx()
    clear_ctx()
    with pytest.raises(SdkRuntimeNotInitialized):
        self_validate()
    set_ctx(ctx)


def test_self_state_put_idempotent():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.self.state"})
    _ensure_skill(ctx, "demo_state")

    result1 = self_state_put("last_run", {"ts": 1}, request_id="req-1")
    assert result1["stored"] is True
    assert ctx.kv.get("skills/demo_state/state/last_run") == {"ts": 1}

    # Repeating with same request id should return cached result without overwriting
    result2 = self_state_put("last_run", {"ts": 2}, request_id="req-1")
    assert result2 == result1
    assert ctx.kv.get("skills/demo_state/state/last_run") == {"ts": 1}

    # Dry-run must not write
    preview = self_state_put("preview", {"foo": "bar"}, request_id="dry-1", dry_run=True)
    assert preview["stored"] is False
    assert preview["dry_run"] is True
    assert ctx.kv.get("skills/demo_state/state/preview") is None


def test_self_state_get_with_default():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.self.state"})
    _ensure_skill(ctx, "demo_get")
    ctx.kv.set("skills/demo_get/state/value", 42)

    found = self_state_get("value")
    missing = self_state_get("missing", default="n/a")

    assert found == {"status": "ok", "key": "value", "value": 42, "found": True}
    assert missing == {"status": "ok", "key": "missing", "value": "n/a", "found": False}


def test_self_update_request(monkeypatch):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.self.update"})
    _ensure_skill(ctx, "demo_update")

    def _fake_update(self, skill_id: str, dry_run: bool = False) -> SkillUpdateResult:
        return SkillUpdateResult(updated=not dry_run, version="1.2.3")

    monkeypatch.setattr(SkillUpdateService, "request_update", _fake_update)

    res = self_update_request("req-up-1")
    assert res == {"status": "ok", "updated": True, "version": "1.2.3", "dry_run": False, "request_id": "req-up-1"}

    # Repeated request id returns cached result
    assert self_update_request("req-up-1") == res

    # Dry-run should not persist cached result for later real call
    preview = self_update_request("req-up-2", dry_run=True)
    assert preview == {"status": "ok", "updated": False, "version": "1.2.3", "dry_run": True, "request_id": "req-up-2"}
    actual = self_update_request("req-up-2")
    assert actual["updated"] is True


def test_self_update_request_without_skill():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.self.update"})
    ctx.skill_ctx.clear()

    res = self_update_request("req-no-skill")
    assert res["status"] == "error"
    assert res["code"] == "skill_not_selected"
    assert res["updated"] is False
