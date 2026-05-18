from __future__ import annotations

from adaos.services.yjs import owner_guard


def _reset_owner_guard_state() -> None:
    with owner_guard._LOCK:
        owner_guard._DECISIONS.clear()
        owner_guard._QUARANTINES.clear()
        owner_guard._QUARANTINE_INCIDENTS.clear()
        owner_guard._QUARANTINE_TOTAL = 0
        owner_guard._DENIED_TOTAL = 0


def test_owner_quarantine_escalates_repeated_incidents(monkeypatch) -> None:
    _reset_owner_guard_state()
    monkeypatch.setattr(owner_guard, "_QUARANTINE_TTL_S", 10.0)
    monkeypatch.setattr(owner_guard, "_QUARANTINE_MAX_TTL_S", 60.0)
    monkeypatch.setattr(owner_guard, "_QUARANTINE_ESCALATION_WINDOW_S", 3600.0)
    monkeypatch.setattr(owner_guard, "_publish_quarantine_service_node", lambda _webspace_id: None)

    first = owner_guard.quarantine_owner(
        webspace_id="desktop",
        owner="skill:infrastate_skill",
        tool="infrastate_skill:get_snapshot",
        trigger="throttle_streak",
    )
    key = "desktop\0skill:infrastate_skill"
    with owner_guard._LOCK:
        owner_guard._QUARANTINES[key]["quarantine_until"] = 0.0
    second = owner_guard.quarantine_owner(
        webspace_id="desktop",
        owner="skill:infrastate_skill",
        tool="infrastate_skill:get_snapshot",
        trigger="throttle_streak",
    )
    with owner_guard._LOCK:
        owner_guard._QUARANTINES[key]["quarantine_until"] = 0.0
    third = owner_guard.quarantine_owner(
        webspace_id="desktop",
        owner="skill:infrastate_skill",
        tool="infrastate_skill:get_snapshot",
        trigger="throttle_streak",
    )

    assert first["quarantine_ttl_s"] == 10.0
    assert first["incident_count"] == 1
    assert second["quarantine_ttl_s"] == 20.0
    assert second["incident_count"] == 2
    assert third["quarantine_ttl_s"] == 40.0
    assert third["incident_count"] == 3


def test_read_only_skill_tool_admission_bypasses_active_quarantine(monkeypatch) -> None:
    _reset_owner_guard_state()
    monkeypatch.setattr(owner_guard, "_publish_quarantine_service_node", lambda _webspace_id: None)

    owner_guard.quarantine_owner(
        webspace_id="desktop",
        owner="skill:infrastate_skill",
        tool="infrastate_skill:refresh_snapshot",
        trigger="throttle_streak",
    )

    allowed = owner_guard.admit_skill_tool(
        skill_name="infrastate_skill",
        tool="get_snapshot",
        payload={"webspace_id": "desktop", "project": False},
        read_only=True,
    )

    assert allowed["allowed"] is True
    assert allowed["read_only"] is True
    assert allowed["policy_state"] == "read_only"
    with owner_guard._LOCK:
        assert owner_guard._DENIED_TOTAL == 0
