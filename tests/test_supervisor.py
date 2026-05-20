from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from adaos.apps import supervisor
from adaos.services.core_update import read_plan, read_status, write_plan, write_status


def test_reconcile_update_status_marks_stale_attempt_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC", "60")
    monkeypatch.setattr(supervisor, "rollback_to_previous_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "rollback_installed_skill_runtimes",
        lambda: {"ok": True, "total": 1, "failed_total": 0, "rollback_total": 1, "skills": []},
    )

    monkeypatch.setattr(supervisor.time, "time", lambda: 120.0)
    write_status(
        {
            "state": "restarting",
            "phase": "shutdown",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.update",
        }
    )
    write_plan({"state": "pending_restart", "target_rev": "rev2026", "expires_at": 9999999999.0})
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.update",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 10.0,
        }
    )

    monkeypatch.setattr(supervisor.time, "time", lambda: 240.0)
    payload = supervisor._reconcile_update_status({"ok": True, "status": read_status(), "_served_by": "supervisor_fallback"})

    assert payload["status"]["state"] == "failed"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["status"]["restored_slot"] == "A"
    assert payload["status"]["rollback"]["ok"] is True
    assert payload["status"]["skill_runtime_rollback"]["rollback_total"] == 1
    assert payload["_served_by"] == "supervisor_timeout_recovery"
    assert read_plan() is None
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["contract_version"] == "1"
    assert attempt["authority"] == "supervisor"
    assert attempt["state"] == "failed"
    assert attempt["last_status"]["state"] == "failed"


def test_update_attempt_read_write_normalizes_contract(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    written = supervisor._write_update_attempt(
        {
            "state": "ACTIVE",
            "action": "Update",
            "target_rev": "rev2026",
            "reason": "test.update",
            "requested_at": "100.0",
            "subsequent_transition_request": {"action": "update", "target_rev": "rev2027"},
        }
    )

    loaded = supervisor._read_update_attempt()

    assert written["contract_version"] == "1"
    assert written["authority"] == "supervisor"
    assert written["state"] == "active"
    assert written["action"] == "update"
    assert loaded == written


def test_reconcile_update_status_completes_attempt_on_terminal_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {"state": "succeeded", "phase": "validate", "updated_at": 499.0},
            "_served_by": "runtime",
        }
    )

    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["last_status"]["state"] == "succeeded"


def test_reconcile_update_status_ignores_stale_targetless_terminal_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "target_version": "1111111111111111111111111111111111111111",
            "git_commit": "1111111111111111111111111111111111111111",
            "git_short_commit": "1111111",
        },
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "2222222222222222222222222222222222222222",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {
                "state": "succeeded",
                "phase": "validate",
                "updated_at": 455.0,
            },
            "_served_by": "runtime",
        }
    )

    assert payload["_served_by"] == "supervisor_stale_terminal_status_ignored"
    assert payload["status"]["state"] == "succeeded"
    assert not payload["status"].get("active_slot_target_mismatch")
    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "active"
    assert attempt.get("completion_reason") is None


def test_reconcile_update_status_rejects_terminal_success_for_wrong_active_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "target_version": "1111111111111111111111111111111111111111",
            "git_commit": "1111111111111111111111111111111111111111",
            "git_short_commit": "1111111",
        },
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "2222222222222222222222222222222222222222",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {
                "state": "succeeded",
                "phase": "validate",
                "target_rev": "rev2026",
                "target_version": "2222222222222222222222222222222222222222",
                "updated_at": 499.0,
            },
            "_served_by": "runtime",
        }
    )

    assert payload["status"]["state"] == "failed"
    assert payload["status"]["active_slot_target_mismatch"] is True
    assert payload["_served_by"] == "supervisor_target_mismatch_recovery"
    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"
    assert attempt["completion_reason"] == "active slot target mismatch"
    assert attempt["last_status"]["target_version"] == "2222222222222222222222222222222222222222"


def test_sidecar_role_falls_back_to_load_config_when_ctx_config_is_missing(monkeypatch) -> None:
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token=None)

    class _Ctx:
        config = None

    monkeypatch.setattr(supervisor, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(supervisor, "load_config", lambda ctx=None: type("Conf", (), {"role": "hub"})())

    assert manager._sidecar_role() == "hub"


def test_sidecar_repo_root_prefers_shared_dotenv_project_root_over_venv_ctx_repo_root(monkeypatch, tmp_path) -> None:
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token=None)
    project_root = tmp_path / "adaos"
    project_root.mkdir()
    (project_root / ".env").write_text("ADAOS_TOKEN=test\n", encoding="utf-8")
    (project_root / ".git").mkdir()
    venv_repo_root = tmp_path / "venv" / "lib" / "python3.11"
    (venv_repo_root / "src" / "adaos").mkdir(parents=True)

    class _Paths:
        def repo_root(self):
            return venv_repo_root

    class _Ctx:
        paths = _Paths()

    monkeypatch.setattr(supervisor, "get_ctx", lambda: _Ctx())
    monkeypatch.setenv("ADAOS_SHARED_DOTENV_PATH", str(project_root / ".env"))

    assert manager._sidecar_repo_root() == project_root.resolve()


def test_reconcile_update_status_completes_awaiting_root_restart_attempt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {
                "state": "succeeded",
                "phase": "validate",
                "root_restart_completed_at": 499.0,
                "updated_at": 499.0,
            },
            "_served_by": "runtime",
        }
    )

    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["completion_reason"] == "root restart completed"
    assert attempt["last_status"]["root_restart_completed_at"] == 499.0


def test_reconcile_update_status_marks_stale_awaiting_root_restart_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC", "60")
    monkeypatch.setattr(supervisor, "finalize_runtime_boot_status", lambda: None)
    monkeypatch.setattr(supervisor.time, "time", lambda: 120.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.root_restart",
            "updated_at": 10.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.root_restart",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 10.0,
        }
    )

    monkeypatch.setattr(supervisor.time, "time", lambda: 240.0)
    payload = supervisor._reconcile_update_status({"ok": True, "status": read_status(), "_served_by": "supervisor_fallback"})

    assert payload["status"]["state"] == "failed"
    assert payload["status"]["phase"] == "root_restart_timeout"
    assert payload["_served_by"] == "supervisor_timeout_recovery"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"
    assert attempt["completion_reason"] == "root restart timeout"


def test_reconcile_update_status_self_heals_stale_awaiting_root_restart_when_runtime_can_finalize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC", "60")
    monkeypatch.setattr(supervisor.time, "time", lambda: 120.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.root_restart",
            "target_slot": "A",
            "updated_at": 10.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.root_restart",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 10.0,
        }
    )
    monkeypatch.setattr(
        supervisor,
        "finalize_runtime_boot_status",
        lambda: {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "target_rev": "rev2026",
            "target_slot": "A",
            "root_restart_completed_at": 119.0,
            "updated_at": 119.0,
        },
    )

    payload = supervisor._reconcile_update_status({"ok": True, "status": read_status(), "_served_by": "supervisor_fallback"})

    assert payload["status"]["state"] == "succeeded"
    assert payload["status"]["phase"] == "validate"
    assert payload["status"]["root_restart_completed_at"] == 119.0
    assert payload["_served_by"] == "supervisor_timeout_finalize"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["completion_reason"] == "root restart completed"


def test_reconcile_update_status_finalizes_stale_launch_when_runtime_is_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "restarting",
            "phase": "launch",
            "action": "update",
            "target_rev": "rev2026",
            "target_slot": "B",
            "reason": "test.launch",
            "updated_at": 10.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 11.0,
        }
    )
    monkeypatch.setattr(
        supervisor,
        "finalize_runtime_boot_status",
        lambda: {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "target_rev": "rev2026",
            "target_slot": "B",
            "validated_at": 120.0,
            "updated_at": 120.0,
        },
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": read_status(),
            "runtime": {
                "runtime_state": "ready",
                "runtime_api_ready": True,
                "listener_running": True,
                "active_slot": "B",
            },
            "_served_by": "supervisor_monitor",
        }
    )

    assert payload["status"]["state"] == "succeeded"
    assert payload["status"]["phase"] == "validate"
    assert payload["_served_by"] == "supervisor_runtime_ready_finalize"
    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"


def test_reconcile_update_status_does_not_finalize_ready_runtime_for_other_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "restarting",
            "phase": "launch",
            "action": "update",
            "target_rev": "rev2026",
            "target_slot": "B",
            "reason": "test.launch",
            "updated_at": 10.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 11.0,
        }
    )
    monkeypatch.setattr(
        supervisor,
        "finalize_runtime_boot_status",
        lambda: (_ for _ in ()).throw(AssertionError("should not finalize a different active slot")),
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": read_status(),
            "runtime": {
                "runtime_state": "ready",
                "runtime_api_ready": True,
                "listener_running": True,
                "active_slot": "A",
            },
            "_served_by": "supervisor_monitor",
        }
    )

    assert payload["status"]["state"] == "restarting"
    assert payload["_served_by"] == "supervisor_monitor"


def test_reconcile_update_status_clears_stale_candidate_prewarm_fields_when_root_restart_completes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "awaiting_restart": True,
            "restart_required": True,
            "candidate_prewarm_state": "starting",
            "candidate_prewarm_message": "passive candidate runtime is still warming on http://127.0.0.1:8778",
            "candidate_prewarm_ready_at": 430.0,
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {
                "state": "succeeded",
                "phase": "validate",
                "root_restart_completed_at": 499.0,
                "updated_at": 499.0,
            },
            "_served_by": "runtime",
        }
    )

    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["awaiting_restart"] is False
    assert attempt["restart_required"] is False
    assert attempt["candidate_prewarm_state"] is None
    assert attempt["candidate_prewarm_message"] is None
    assert attempt["candidate_prewarm_ready_at"] is None


def test_last_update_completion_at_ignores_idle_status() -> None:
    assert supervisor._last_update_completion_at({"state": "idle", "updated_at": 123.0}, None) == 0.0


def test_runtime_shutdown_request_timeout_scales_with_drain_window() -> None:
    assert supervisor._runtime_shutdown_request_timeout(drain_timeout_sec=10.0, signal_delay_sec=0.25) >= 12.0


def test_runtime_self_heal_restarts_when_managed_process_does_not_match_active_slot(monkeypatch) -> None:
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._stopping = False
    manager._managed_runtime_cwd = "/slots/A/repo"
    manager._last_start_at = 100.0

    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "restarting", "phase": "launch"})
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["/slots/B/venv/bin/python"], "cwd": "/slots/B/repo"},
    )
    monkeypatch.setattr(
        supervisor,
        "_proc_details",
        lambda proc, cwd_hint=None: {
            "managed_pid": 4321,
            "managed_alive": True,
            "managed_cmdline": ["/slots/A/venv/bin/python", "-m", "adaos.apps.autostart_runner"],
            "managed_executable": "/slots/A/venv/bin/python",
            "managed_cwd": "/slots/A/repo",
        },
    )

    decision = manager._runtime_self_heal_decision(now=120.0)

    assert isinstance(decision, dict)
    assert decision["reason"] == "supervisor.runtime.slot_mismatch"
    assert decision["active_slot"] == "B"
    assert decision["managed_executable"] == "/slots/A/venv/bin/python"
    assert decision["expected_managed_executable"] == "/slots/B/venv/bin/python"


def test_hub_root_watchdog_requests_reconnect_when_root_control_is_down(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")

    decision = manager._hub_root_watchdog_decision(
        {
            "readiness_tree": {"root_control": {"status": "down"}},
            "channel_overview": {
                "hub_root": {
                    "effective_status": "down",
                    "effective_state": "down",
                }
            },
            "hub_root_transport_strategy": {
                "last_event": "failure",
                "last_summary": "watchdog._reading_task",
            },
        },
        now=100.0,
    )

    assert isinstance(decision, dict)
    assert decision["reason"] == "supervisor.hub_root.watchdog_reconnect"
    assert decision["action"] == "runtime_reconnect"
    assert decision["transport_owner"] == "runtime"
    assert decision["root_control_status"] == "down"
    assert decision["last_summary"] == "watchdog._reading_task"


def test_hub_root_watchdog_respects_reconnect_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    monkeypatch.setenv("ADAOS_SUPERVISOR_HUB_ROOT_RECONNECT_COOLDOWN_SEC", "30")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._hub_root_watchdog_last_reconnect_at = 95.0
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")

    decision = manager._hub_root_watchdog_decision(
        {
            "readiness_tree": {"root_control": {"status": "down"}},
            "channel_overview": {"hub_root": {"effective_status": "down"}},
        },
        now=100.0,
    )

    assert decision is None
    assert manager._hub_root_watchdog_last_state == "cooldown"
    assert "cooldown" in str(manager._hub_root_watchdog_last_reason)


def test_hub_root_watchdog_invokes_runtime_reconnect(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    monkeypatch.setenv("ADAOS_SUPERVISOR_HUB_ROOT_VERIFY_TIMEOUT_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    calls: list[dict[str, object]] = []
    manager._proc = _Proc()
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.5: {
            "readiness_tree": {"root_control": {"status": "down"}},
            "channel_overview": {"hub_root": {"effective_status": "down"}},
        },
    )

    def _request(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True}

    monkeypatch.setattr(manager, "_runtime_request_json", _request)

    asyncio.run(manager._maybe_reconnect_hub_root_from_watchdog())

    assert len(calls) == 1
    assert calls[0]["path"] == "/api/node/hub-root/reconnect"
    assert manager._hub_root_watchdog_reconnect_total == 1
    assert manager._hub_root_watchdog_last_result["result"]["ok"] is True
    assert manager._hub_root_watchdog_last_result["verification"]["ok"] is False


def test_hub_root_watchdog_resets_browser_route_when_root_control_is_ready(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    monkeypatch.setenv("ADAOS_SUPERVISOR_HUB_ROOT_ROUTE_DEGRADED_RESET", "1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_HUB_ROOT_VERIFY_TIMEOUT_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    calls: list[dict[str, object]] = []
    manager._proc = _Proc()
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.5: {
            "readiness_tree": {
                "root_control": {"status": "ready"},
                "route": {"status": "degraded"},
            },
            "channel_overview": {
                "hub_root": {"effective_status": "ready", "effective_state": "stable"},
                "hub_root_browser": {"effective_status": "degraded", "effective_state": "unstable"},
            },
        },
    )

    def _request(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True}

    monkeypatch.setattr(manager, "_runtime_request_json", _request)

    asyncio.run(manager._maybe_reconnect_hub_root_from_watchdog())

    assert len(calls) == 1
    assert calls[0]["path"] == "/api/node/hub-root/route-reset"
    assert calls[0]["payload"] == {
        "reason": "supervisor_route_watchdog",
        "notify_browser": True,
    }
    assert manager._hub_root_watchdog_last_result["action"] == "runtime_route_reset"
    assert manager._hub_root_watchdog_last_result["decision"]["hub_root_status"] == "ready"
    assert manager._hub_root_watchdog_last_result["decision"]["hub_root_browser_status"] == "degraded"


def test_hub_root_watchdog_preserves_runtime_route_on_degraded_browser_route_by_default(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    monkeypatch.delenv("ADAOS_SUPERVISOR_HUB_ROOT_ROUTE_DEGRADED_RESET", raising=False)
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    calls: list[dict[str, object]] = []
    manager._proc = _Proc()
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.5: {
            "readiness_tree": {
                "root_control": {"status": "ready"},
                "route": {"status": "degraded"},
            },
            "channel_overview": {
                "hub_root": {"effective_status": "ready", "effective_state": "stable"},
                "hub_root_browser": {"effective_status": "degraded", "effective_state": "flapping"},
            },
        },
    )
    monkeypatch.setattr(manager, "_runtime_request_json", lambda **kwargs: calls.append(dict(kwargs)) or {"ok": True})

    asyncio.run(manager._maybe_reconnect_hub_root_from_watchdog())

    assert calls == []
    assert manager._hub_root_watchdog_last_state == "degraded"
    assert manager._hub_root_watchdog_last_reason == "browser route degraded; preserving active runtime-owned tunnels"
    assert manager._hub_root_watchdog_reconnect_total == 0


def test_hub_root_watchdog_restarts_sidecar_when_sidecar_owns_transport(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_HUB_ROOT_VERIFY_TIMEOUT_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")
    snapshots = [
        {
            "readiness_tree": {"root_control": {"status": "down"}},
            "channel_overview": {"hub_root": {"effective_status": "down"}},
        },
        {
            "readiness_tree": {
                "root_control": {"status": "ready"},
                "route": {"status": "ready"},
            },
            "channel_overview": {
                "hub_root": {"effective_status": "ready", "effective_state": "stable"},
                "hub_root_browser": {"effective_status": "ready", "effective_state": "stable"},
            },
        },
    ]

    def _runtime_payload(timeout=1.5):
        if len(snapshots) > 1:
            return snapshots.pop(0)
        return snapshots[0]

    sidecar_calls: list[dict[str, object]] = []

    async def _restart_sidecar(**kwargs):
        sidecar_calls.append(dict(kwargs))
        return {"ok": True, "restart": {"ok": True}, "reconnect": {"ok": True}}

    monkeypatch.setattr(manager, "_runtime_reliability_payload", _runtime_payload)
    monkeypatch.setattr(manager, "restart_sidecar", _restart_sidecar)

    asyncio.run(manager._maybe_reconnect_hub_root_from_watchdog())

    assert len(sidecar_calls) == 1
    assert sidecar_calls[0]["reconnect_hub_root"] is True
    assert manager._hub_root_watchdog_last_result["action"] == "sidecar_restart"
    assert manager._hub_root_watchdog_last_result["verification"]["ok"] is True
    assert manager._hub_root_watchdog_last_state == "ready"
    events = supervisor._read_jsonl_tail(supervisor._supervisor_hub_root_watchdog_log_path(), limit=5)
    assert events[-1]["action"] == "sidecar_restart"
    assert events[-1]["verification"]["ok"] is True


def test_member_hub_watchdog_requests_reconnect_when_member_link_is_down(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "member")

    decision = manager._member_hub_watchdog_decision(
        {
            "node": {"role": "member"},
            "readiness_tree": {
                "route": {"status": "down"},
                "hub_member": {"status": "down"},
            },
            "hub_member_connection_state": {
                "state": "disconnected",
                "assessment": {"state": "degraded", "reason": "member_link_down"},
                "hub": {
                    "connected": False,
                    "hub_url": "https://ru.api.inimatic.com/hubs/sn_demo",
                },
            },
        },
        now=100.0,
    )

    assert isinstance(decision, dict)
    assert decision["reason"] == "supervisor.member_hub.watchdog_reconnect"
    assert decision["action"] == "runtime_reconnect"
    assert decision["transport_owner"] == "runtime"
    assert decision["member_state"] == "disconnected"
    assert decision["continuity_mode"] == "runtime_bound"
    assert decision["handoff_state"] == "unknown"


def test_member_hub_watchdog_uses_runtime_required_upstream_link_context(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "member")

    decision = manager._member_hub_watchdog_decision(
        {
            "node": {"role": "member"},
            "required_upstream_link": {
                "kind": "member_hub",
                "current_owner": "sidecar",
                "planned_owner": "sidecar",
                "continuity_mode": "slot_sticky",
                "handoff_state": "ready",
                "handoff_ready": True,
                "recovery_policy": {
                    "on_runtime_restart": "preserve_sidecar",
                    "while_owner_runtime": "runtime_reconnect",
                    "while_owner_sidecar": "preserve_sidecar",
                },
                "sidecar_enabled": True,
                "blockers": [],
            },
            "readiness_tree": {
                "route": {"status": "down"},
                "hub_member": {"status": "down"},
            },
            "hub_member_connection_state": {
                "state": "disconnected",
                "assessment": {"state": "degraded", "reason": "member_link_down"},
                "hub": {
                    "connected": False,
                    "hub_url": "https://ru.api.inimatic.com/hubs/sn_demo",
                },
            },
        },
        now=100.0,
    )

    assert isinstance(decision, dict)
    assert decision["transport_owner"] == "sidecar"
    assert decision["continuity_mode"] == "slot_sticky"
    assert decision["handoff_state"] == "ready"
    assert decision["handoff_ready"] is True
    assert decision["recovery_policy"]["on_runtime_restart"] == "preserve_sidecar"
    assert decision["required_upstream_link"]["current_owner"] == "sidecar"


def test_member_hub_watchdog_skips_recovery_during_restart_transition(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "member")

    decision = manager._member_hub_watchdog_decision(
        {
            "node": {"role": "member"},
            "hub_member_connection_state": {
                "state": "restarting",
                "assessment": {"state": "degraded", "reason": "restarting"},
                "hub": {
                    "connected": False,
                    "transition_state": "restarting",
                    "transition_reason": "core update launch",
                },
            },
        },
        now=100.0,
    )

    assert decision is None
    assert manager._member_hub_watchdog_last_state == "restarting"
    assert manager._member_hub_watchdog_last_reason == "core update launch"


def test_member_hub_watchdog_invokes_runtime_reconnect(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMBER_HUB_VERIFY_TIMEOUT_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return None

    calls: list[dict[str, object]] = []
    manager._proc = _Proc()
    monkeypatch.setattr(manager, "_sidecar_role", lambda: "member")
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.5: {
            "node": {"role": "member"},
            "readiness_tree": {
                "route": {"status": "down"},
                "hub_member": {"status": "down"},
            },
            "hub_member_connection_state": {
                "state": "disconnected",
                "assessment": {"state": "degraded", "reason": "member_link_down"},
                "hub": {
                    "connected": False,
                    "hub_url": "https://ru.api.inimatic.com/hubs/sn_demo",
                },
            },
        },
    )

    def _request(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "_runtime_request_json", _request)

    asyncio.run(manager._maybe_reconnect_member_hub_from_watchdog())

    assert len(calls) == 1
    assert calls[0]["path"] == "/api/node/member-hub/reconnect"
    assert manager._member_hub_watchdog_reconnect_total == 1
    assert manager._member_hub_watchdog_last_result["result"]["accepted"] is True
    assert manager._member_hub_watchdog_last_result["verification"]["ok"] is False
    events = supervisor._read_jsonl_tail(supervisor._supervisor_member_hub_watchdog_log_path(), limit=5)
    assert events[-1]["action"] == "runtime_reconnect"


def test_required_upstream_link_maintenance_dispatches_to_member_watchdog(monkeypatch) -> None:
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._managed_transition_role = "member"
    calls: list[str] = []

    async def _member() -> None:
        calls.append("member")

    async def _hub() -> None:
        calls.append("hub")

    monkeypatch.setattr(manager, "_maybe_reconnect_member_hub_from_watchdog", _member)
    monkeypatch.setattr(manager, "_maybe_reconnect_hub_root_from_watchdog", _hub)

    asyncio.run(manager._maybe_maintain_required_upstream_link())

    assert calls == ["member"]


def test_required_upstream_link_snapshot_prefers_runtime_payload(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    payload = manager._required_upstream_link_snapshot(
        runtime={
            "required_upstream_link": {
                "kind": "member_hub",
                "current_owner": "sidecar",
                "handoff_state": "ready",
            }
        },
        role="member",
    )

    assert payload["kind"] == "member_hub"
    assert payload["current_owner"] == "sidecar"
    assert payload["handoff_state"] == "ready"


def test_required_upstream_link_maintenance_dispatches_to_hub_watchdog(monkeypatch) -> None:
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._managed_transition_role = "hub"
    calls: list[str] = []

    async def _member() -> None:
        calls.append("member")

    async def _hub() -> None:
        calls.append("hub")

    monkeypatch.setattr(manager, "_maybe_reconnect_member_hub_from_watchdog", _member)
    monkeypatch.setattr(manager, "_maybe_reconnect_hub_root_from_watchdog", _hub)

    asyncio.run(manager._maybe_maintain_required_upstream_link())

    assert calls == ["hub"]


def test_supervisor_start_update_and_cancel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "0")
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "prepared",
            "phase": "prepare",
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "plan": {"target_slot": "B"},
            "finished_at": 123.0,
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "active"
        assert attempt["action"] == "update"
        cancelled = await manager.cancel_update(reason="test.cancel")
        assert cancelled["accepted"] is True
        assert cancelled["status"]["state"] == "cancelled"
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "cancelled"

    asyncio.run(_exercise())


def test_supervisor_prepare_failure_does_not_request_runtime_shutdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "0")
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "failed",
            "phase": "prepare",
            "message": "prepare exploded",
            "target_slot": "B",
            "plan": {"target_slot": "B"},
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _unexpected_shutdown(**kwargs):
        raise AssertionError("runtime shutdown must not be requested when prepare fails")

    monkeypatch.setattr(manager, "_request_runtime_shutdown", _unexpected_shutdown)

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        task = manager._update_task
        assert task is not None
        await task
        status = read_status()
        assert status["state"] == "failed"
        assert status["phase"] == "prepare"
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "failed"

    asyncio.run(_exercise())


def test_prepare_worker_writes_prepared_restart_plan_and_reenables_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "prepared",
            "phase": "prepare",
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "plan": {"target_slot": "B"},
            "finished_at": 222.0,
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    lifecycle_calls: list[str] = []
    desired_running_states: list[bool] = []
    activated_slots: list[str] = []
    candidate_calls: list[tuple[str, str | None]] = []
    promote_calls: list[tuple[str, str]] = []

    async def _shutdown(**kwargs):
        lifecycle_calls.append("shutdown")
        return {"ok": True}

    async def _ensure_stopped(**kwargs):
        lifecycle_calls.append("stopped")
        return {"ok": True, "forced": False}

    async def _candidate_prewarm(*, target_slot: str | None):
        candidate_calls.append(("prewarm", target_slot))
        return {
            "attempted": True,
            "state": "ready",
            "message": "passive candidate runtime is ready on http://127.0.0.1:8778",
            "ready_at": 223.0,
        }

    async def _cleanup_candidate_runtime(*, reason: str, slot: str | None = None):
        candidate_calls.append((reason, slot))
        return {"ok": True, "stopped": True, "slot": slot}

    async def _promote_candidate_runtime(*, slot: str, reason: str):
        promote_calls.append((slot, reason))
        return {
            "ok": True,
            "accepted": True,
            "runtime": {
                "transition_role": "active",
                "runtime_instance_id": "rt-b-c-12345678",
                "runtime_port": 8778,
            },
        }

    monkeypatch.setattr(manager, "_request_runtime_shutdown", _shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _ensure_stopped)
    monkeypatch.setattr(manager, "_candidate_prewarm", _candidate_prewarm)
    monkeypatch.setattr(manager, "_cleanup_candidate_runtime", _cleanup_candidate_runtime)
    monkeypatch.setattr(manager, "_promote_candidate_runtime", _promote_candidate_runtime)
    monkeypatch.setattr(supervisor, "activate_slot", lambda slot: activated_slots.append(str(slot)))
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: desired_running_states.append(bool(manager._desired_running)))

    asyncio.run(
        manager._prepare_and_countdown_update_worker(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    plan = read_plan()
    assert isinstance(plan, dict)
    assert plan["state"] == "prepared_restart"
    assert plan["target_slot"] == "B"
    status = read_status()
    assert status["state"] == "restarting"
    assert status["phase"] == "launch"
    assert status["candidate_prewarm_state"] == "promoted_to_active"
    assert activated_slots == ["B"]
    assert lifecycle_calls == ["shutdown", "stopped"]
    assert candidate_calls == [("prewarm", "B")]
    assert promote_calls == [("B", "supervisor.fast_cutover")]
    assert False in desired_running_states
    assert desired_running_states[-1] is True


def test_prepare_worker_falls_back_to_stop_and_switch_when_candidate_cutover_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "prepared",
            "phase": "prepare",
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "plan": {"target_slot": "B"},
            "finished_at": 222.0,
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    cleanup_calls: list[tuple[str, str | None]] = []

    async def _shutdown(**kwargs):
        return {"ok": True}

    async def _ensure_stopped(**kwargs):
        return {"ok": True, "forced": False}

    async def _candidate_prewarm(*, target_slot: str | None):
        return {
            "attempted": True,
            "state": "ready",
            "message": "passive candidate runtime is ready on http://127.0.0.1:8778",
            "ready_at": 223.0,
        }

    async def _cleanup_candidate_runtime(*, reason: str, slot: str | None = None):
        cleanup_calls.append((reason, slot))
        return {"ok": True, "stopped": True, "slot": slot}

    async def _promote_candidate_runtime(*, slot: str, reason: str):
        raise RuntimeError("candidate reconnect failed")

    monkeypatch.setattr(manager, "_request_runtime_shutdown", _shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _ensure_stopped)
    monkeypatch.setattr(manager, "_candidate_prewarm", _candidate_prewarm)
    monkeypatch.setattr(manager, "_cleanup_candidate_runtime", _cleanup_candidate_runtime)
    monkeypatch.setattr(manager, "_promote_candidate_runtime", _promote_candidate_runtime)
    monkeypatch.setattr(supervisor, "activate_slot", lambda slot: None)
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)

    asyncio.run(
        manager._prepare_and_countdown_update_worker(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    status = read_status()
    assert status["state"] == "restarting"
    assert status["phase"] == "launch"
    assert status["candidate_prewarm_state"] == "cutover_fallback"
    assert "candidate reconnect failed" in str(status["candidate_prewarm_message"] or "")
    assert cleanup_calls == [("supervisor.candidate.cutover_fallback", "B")]


def test_promote_candidate_runtime_adopts_candidate_process(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _CandidateProc:
        pid = 42424

        @staticmethod
        def poll():
            return None

    manager._candidate_proc = _CandidateProc()
    manager._candidate_slot = "B"
    manager._candidate_runtime_instance_id = "rt-b-c-12345678"
    manager._candidate_transition_role = "candidate"

    class _Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "ok": True,
                "accepted": True,
                "runtime": {
                    "transition_role": "active",
                    "runtime_instance_id": "rt-b-c-12345678",
                    "runtime_port": 8778,
                },
            }

    captured: dict[str, object] = {}

    def _post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Response()

    persisted: list[bool] = []

    monkeypatch.setattr(supervisor.requests, "post", _post)
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: persisted.append(True))

    payload = asyncio.run(manager._promote_candidate_runtime(slot="B", reason="test.cutover"))

    assert payload["accepted"] is True
    assert captured["url"] == "http://127.0.0.1:8778/api/admin/runtime/promote-active"
    assert captured["kwargs"]["json"]["reason"] == "test.cutover"
    assert manager._proc is not None
    assert manager._candidate_proc is None
    assert manager._managed_runtime_instance_id == "rt-b-c-12345678"
    assert manager._managed_transition_role == "active"
    assert persisted


def test_supervisor_monitor_cleans_idle_candidate_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 9999

        @staticmethod
        def poll():
            return None

    manager._candidate_proc = _Proc()
    manager._candidate_slot = "B"
    manager._candidate_runtime_instance_id = "rt-b-c-12345678"
    manager._candidate_transition_role = "candidate"
    write_status({"state": "idle", "updated_at": 10.0})
    supervisor._write_update_attempt({"state": "completed", "updated_at": 9.0})

    cleanup_calls: list[tuple[str, str | None]] = []

    async def _cleanup_candidate_runtime(*, reason: str, slot: str | None = None):
        cleanup_calls.append((reason, slot))
        manager._candidate_proc = None
        manager._candidate_slot = None
        manager._candidate_runtime_instance_id = None
        manager._candidate_transition_role = None
        return {"ok": True, "stopped": True}

    monkeypatch.setattr(manager, "_cleanup_candidate_runtime", _cleanup_candidate_runtime)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert cleanup_calls == [("supervisor.candidate.idle_cleanup", None)]
    assert manager._candidate_proc is None


def test_supervisor_start_update_schedules_when_min_period_not_elapsed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "completed_at": 450.0,
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["planned"] is True
    status = read_status()
    assert status["state"] == "planned"
    assert status["planned_reason"] == "minimum_update_period"
    assert status["scheduled_for"] == 750.0
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "planned"
    assert attempt["scheduled_for"] == 750.0


def test_supervisor_start_update_deduplicates_active_slot_before_min_period(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    target = "4c1806aa70b040db61199707e0b739b244d7af04"
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "target_rev": "rev2026",
            "target_version": target,
            "git_commit": target,
            "git_short_commit": target[:7],
        },
    )
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
            "finished_at": 450.0,
            "updated_at": 450.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
            "completed_at": 450.0,
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version=target,
            reason="test.same-active",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["deduplicated"] is True
    assert result["same_target"] is True
    assert result["planned"] is False
    status = read_status()
    assert status["state"] == "succeeded"
    assert status["same_target_deduped_reason"] == "active_slot_same_target"
    assert status["scheduled_for"] is None
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "deduplicated"
    assert supervisor._last_update_completion_at(status, attempt) == 450.0


def test_supervisor_planned_update_resumes_through_prepare(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    target = "9c7e221b5157c46d84f64e43822357d5cffec4b0"
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(manager, "status", lambda: {})
    monkeypatch.setattr(manager, "_transition_continuity_guard_decision", lambda operation: None)
    monkeypatch.setattr(
        manager,
        "_begin_prepare_transition",
        lambda request: calls.append(("prepare", dict(request))) or {"ok": True},
    )
    monkeypatch.setattr(
        manager,
        "_begin_countdown_transition",
        lambda request, **kwargs: calls.append(("countdown", dict(request))) or {"ok": True},
    )
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
            "scheduled_for": 450.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
            "scheduled_for": 450.0,
            "updated_at": 400.0,
        }
    )

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert calls == [
        (
            "prepare",
            {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": target,
                "reason": "",
                "countdown_sec": 0.0,
                "drain_timeout_sec": 10.0,
                "signal_delay_sec": 0.25,
                "requested_at": 500.0,
            },
        )
    ]


def test_supervisor_promote_root_refuses_active_slot_target_mismatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    target = "9c7e221b5157c46d84f64e43822357d5cffec4b0"
    active = "2ba8453f42daaa8f89fad848d92a1481bd3a6a4d"

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "target_rev": "rev2026",
            "target_version": active,
            "git_commit": active,
            "git_short_commit": active[:7],
        },
    )
    monkeypatch.setattr(
        supervisor,
        "resolved_root_promotion_requirement",
        lambda manifest: (False, {"required": False, "basis": "test"}),
    )
    write_status(
        {
            "state": "validated",
            "phase": "root_promotion_pending",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": target,
            "updated_at": 400.0,
        }
    )

    result = asyncio.run(manager.promote_root(reason="test.root"))

    assert result["ok"] is False
    status = read_status()
    assert status["state"] == "failed"
    assert status["root_promotion_refused_reason"] == "active_slot_target_mismatch"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"
    assert attempt["completion_reason"] == "active slot target mismatch"


def test_supervisor_active_slot_dedupe_preserves_different_planned_update(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    active_target = "4c1806aa70b040db61199707e0b739b244d7af04"
    planned_target = "9a9b9c9d00000000000000000000000000000000"
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "target_rev": "rev2026",
            "target_version": active_target,
            "git_commit": active_target,
            "git_short_commit": active_target[:7],
        },
    )
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": planned_target,
            "reason": "test.future",
            "scheduled_for": 800.0,
            "planned_reason": "minimum_update_period",
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": planned_target,
            "reason": "test.future",
            "scheduled_for": 800.0,
            "planned_reason": "minimum_update_period",
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version=active_target,
            reason="test.same-active-probe",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["deduplicated"] is True
    assert result["same_target"] is True
    assert result["preserved_planned_transition"] is True
    status = read_status()
    assert status["state"] == "planned"
    assert status["target_version"] == planned_target
    assert status["scheduled_for"] == 800.0
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "planned"
    assert attempt["target_version"] == planned_target


def test_supervisor_start_update_refreshes_existing_planned_update(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.older",
            "scheduled_for": 750.0,
            "planned_reason": "minimum_update_period",
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.older",
            "scheduled_for": 750.0,
            "planned_reason": "minimum_update_period",
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.refresh",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["planned"] is True
    assert result["status"]["scheduled_for"] == 750.0
    assert result["status"]["message"] == "planned core update refreshed while waiting for scheduled window"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "planned"
    assert attempt["target_version"] == "1.2.3"
    assert attempt["scheduled_for"] == 750.0


def test_supervisor_start_update_queues_subsequent_transition_while_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.active",
            "scheduled_for": 530.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.active",
            "scheduled_for": 530.0,
            "updated_at": 500.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.subsequent",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["deferred"] is True
    assert result["subsequent_transition"] is True
    status = read_status()
    assert status["subsequent_transition"] is True
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["subsequent_transition"] is True
    assert attempt["subsequent_transition_request"]["target_version"] == "1.2.3"


def test_supervisor_start_update_deduplicates_same_target_subsequent_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 600.0)
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "671903ec01044b16865a366c81bf27f758823595",
            "reason": "test.active",
            "scheduled_for": 630.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "671903e",
            "reason": "test.active",
            "scheduled_for": 630.0,
            "updated_at": 600.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="671903ec01044b16865a366c81bf27f758823595",
            reason="test.same-target",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["deduplicated"] is True
    assert result["same_target"] is True
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt.get("subsequent_transition") is not True
    status = read_status()
    assert status["same_target_subsequent_deduped_reason"] == "active_transition_same_target"


def test_supervisor_monitor_runs_subsequent_transition_once_after_completion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 800.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "target_rev": "rev2026",
            "updated_at": 799.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "subsequent_transition": True,
            "subsequent_transition_requested_at": 780.0,
            "subsequent_transition_request": {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": "1.2.3",
                "reason": "test.subsequent",
                "countdown_sec": 15.0,
                "drain_timeout_sec": 10.0,
                "signal_delay_sec": 0.25,
                "requested_at": 780.0,
            },
            "updated_at": 799.0,
        }
    )
    calls: list[dict[str, object]] = []

    async def _capture(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "start_update", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert len(calls) == 1
    assert calls[0]["target_version"] == "1.2.3"
    assert calls[0]["bypass_min_period"] is True


def test_supervisor_monitor_drops_same_target_subsequent_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 900.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "671903ec01044b16865a366c81bf27f758823595",
            "updated_at": 899.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "671903e",
            "subsequent_transition": True,
            "subsequent_transition_requested_at": 880.0,
            "subsequent_transition_request": {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": "671903ec01044b16865a366c81bf27f758823595",
                "reason": "test.same-target",
                "countdown_sec": 0.0,
                "drain_timeout_sec": 10.0,
                "signal_delay_sec": 0.25,
                "requested_at": 880.0,
            },
            "updated_at": 899.0,
        }
    )
    calls: list[dict[str, object]] = []

    async def _capture(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "start_update", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert calls == []
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["subsequent_transition"] is False
    assert not attempt.get("subsequent_transition_request")
    status = read_status()
    assert status["subsequent_transition"] is False
    assert status["same_target_subsequent_deduped_reason"] == "completed_transition_same_target"


def test_supervisor_start_update_queues_subsequent_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "scheduled_for": 9999999999.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "requested_at": 1.0,
            "updated_at": 1.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2027",
            target_version="2.0.0",
            reason="test.update.next",
            countdown_sec=45.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        assert result["deferred"] is True
        assert result["subsequent_transition"] is True
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["subsequent_transition"] is True
        assert attempt["subsequent_transition_request"]["target_rev"] == "rev2027"
        status = read_status()
        assert status["subsequent_transition"] is True

    asyncio.run(_exercise())


def test_supervisor_start_update_schedules_planned_update_when_min_period_not_elapsed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    monkeypatch.setattr(supervisor.time, "time", lambda: 150.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "finished_at": 100.0,
            "updated_at": 100.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.4",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        assert result["planned"] is True
        status = read_status()
        assert status["state"] == "planned"
        assert status["phase"] == "scheduled"
        assert status["planned_reason"] == "minimum_update_period"
        assert status["scheduled_for"] == 400.0
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "planned"
        assert attempt["scheduled_for"] == 400.0

    asyncio.run(_exercise())


def test_supervisor_start_update_defers_when_live_media_guard_blocks_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(
        manager,
        "_runtime_request_json",
        lambda **kwargs: {
            "ok": True,
            "runtime": {
                "media_runtime": {
                    "update_guard": {
                        "role": "hub",
                        "live_session_present": True,
                        "observed_live_topology": "member_browser_direct",
                        "hub_runtime_update": "preserve_sidecar",
                        "hub_sidecar_continuity_required": True,
                        "current_support": "planned",
                        "reason": "live media continuity requires independent sidecar ownership",
                    }
                },
                "sidecar_runtime": {
                    "continuity_contract": {
                        "required": True,
                        "enabled": False,
                        "hub_runtime_update": "preserve_sidecar",
                        "current_support": "planned",
                        "reason": "live media continuity requires independent sidecar ownership",
                    }
                },
            },
        },
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.live_media",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["planned"] is True
    status = read_status()
    assert status["state"] == "planned"
    assert status["planned_reason"] == "live_media_guard"
    assert status["scheduled_for"] == 800.0
    assert status["guard_code"] == "hub_sidecar_continuity_pending"
    assert status["continuity_contract"]["required"] is True
    assert status["live_media_guard"]["observed_live_topology"] == "member_browser_direct"


def test_supervisor_defer_update_reschedules_active_countdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "scheduled_for": 200.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "requested_at": 100.0,
            "updated_at": 100.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _sleep_forever() -> None:
        await asyncio.Future()

    async def _exercise() -> None:
        monkeypatch.setattr(supervisor.time, "time", lambda: 150.0)
        manager._update_task = asyncio.create_task(_sleep_forever())
        try:
            result = await manager.defer_update(delay_sec=300.0, reason="test.defer")
        finally:
            if manager._update_task is not None and not manager._update_task.done():
                manager._update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await manager._update_task
        assert result["accepted"] is True
        assert result["planned"] is True
        status = read_status()
        assert status["state"] == "planned"
        assert status["planned_reason"] == "operator_defer"
        assert status["scheduled_for"] == 450.0
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "planned"
        assert attempt["scheduled_for"] == 450.0

    import contextlib

    asyncio.run(_exercise())


def test_supervisor_monitor_resumes_due_planned_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
            "updated_at": 490.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    calls: list[dict] = []

    def _capture(request: dict) -> dict:
        calls.append({"request": dict(request)})
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "_begin_prepare_transition", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert calls
    assert calls[0]["request"]["target_rev"] == "rev2026"


def test_supervisor_monitor_reschedules_due_planned_transition_when_live_media_guard_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.live_media",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.live_media",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
            "updated_at": 490.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    calls: list[dict] = []

    monkeypatch.setattr(
        manager,
        "_runtime_request_json",
        lambda **kwargs: {
            "ok": True,
            "runtime": {
                "media_runtime": {
                    "update_guard": {
                        "role": "hub",
                        "live_session_present": True,
                        "observed_live_topology": "hub_webrtc_loopback",
                        "hub_runtime_update": "preserve_sidecar",
                        "hub_sidecar_continuity_required": True,
                        "current_support": "planned",
                        "reason": "hub participates in the active live media path",
                    }
                },
                "sidecar_runtime": {
                    "continuity_contract": {
                        "required": True,
                        "enabled": False,
                        "hub_runtime_update": "preserve_sidecar",
                        "current_support": "planned",
                        "reason": "hub participates in the active live media path",
                    }
                },
            },
        },
    )

    def _capture(request: dict, *, countdown_sec: float | None = None) -> dict:
        calls.append({"request": dict(request), "countdown_sec": countdown_sec})
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "_begin_countdown_transition", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert not calls
    status = read_status()
    assert status["state"] == "planned"
    assert status["planned_reason"] == "live_media_guard"
    assert status["scheduled_for"] == 800.0
    assert status["guard_code"] == "hub_sidecar_continuity_pending"


def test_supervisor_runtime_restart_blocks_when_live_media_continuity_is_not_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(
        manager,
        "_runtime_request_json",
        lambda **kwargs: {
            "ok": True,
            "runtime": {
                "media_runtime": {
                    "update_guard": {
                        "role": "hub",
                        "live_session_present": True,
                        "observed_live_topology": "member_browser_direct",
                        "hub_runtime_update": "preserve_sidecar",
                        "hub_sidecar_continuity_required": True,
                        "current_support": "planned",
                        "reason": "live media continuity requires independent sidecar ownership",
                    }
                },
                "sidecar_runtime": {
                    "continuity_contract": {
                        "required": True,
                        "enabled": False,
                        "hub_runtime_update": "preserve_sidecar",
                        "current_support": "planned",
                        "reason": "live media continuity requires independent sidecar ownership",
                    }
                },
            },
        },
    )

    with pytest.raises(supervisor.HTTPException) as excinfo:
        asyncio.run(manager.restart_runtime())

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["planned_reason"] == "live_media_guard"
    assert excinfo.value.detail["guard_code"] == "hub_sidecar_continuity_pending"
    assert excinfo.value.detail["continuity_contract"]["required"] is True


def test_supervisor_countdown_worker_writes_plan_and_requests_shutdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    shutdown_calls: list[dict] = []
    stop_calls: list[dict] = []

    async def _fake_sleep(_value: float) -> None:
        return None

    async def _fake_shutdown(*, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict:
        shutdown_calls.append(
            {
                "reason": reason,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
            }
        )
        return {"ok": True, "accepted": True}

    async def _fake_ensure_stopped(*, drain_timeout_sec: float, signal_delay_sec: float, reason: str) -> dict:
        stop_calls.append(
            {
                "reason": reason,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
            }
        )
        return {"ok": True, "forced": False, "reason": reason}

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_request_runtime_shutdown", _fake_shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _fake_ensure_stopped)

    asyncio.run(
        manager._countdown_update_worker(
            action="rollback",
            target_rev="",
            target_version="",
            reason="test.rollback",
            countdown_sec=0.0,
            drain_timeout_sec=5.0,
            signal_delay_sec=0.1,
        )
    )

    plan = read_plan()
    status = read_status()
    assert isinstance(plan, dict)
    assert plan["action"] == "rollback"
    assert status["state"] == "restarting"
    assert status["phase"] == "shutdown"
    assert shutdown_calls and shutdown_calls[0]["reason"] == "test.rollback"
    assert stop_calls and stop_calls[0]["reason"] == "test.rollback"


def test_supervisor_countdown_worker_marks_failed_when_shutdown_request_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _fake_sleep(_value: float) -> None:
        return None

    async def _fake_shutdown(*, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict:
        raise RuntimeError("runtime shutdown API unavailable")

    async def _fake_ensure_stopped(*, drain_timeout_sec: float, signal_delay_sec: float, reason: str) -> dict:
        raise RuntimeError("runtime process did not exit")

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_request_runtime_shutdown", _fake_shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _fake_ensure_stopped)
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "requested_at": 1.0,
            "transitioned_at": 2.0,
            "updated_at": 2.0,
        }
    )

    asyncio.run(
        manager._countdown_update_worker(
            action="update",
            target_rev="HEAD",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=5.0,
            signal_delay_sec=0.1,
        )
    )

    assert read_plan() is None
    status = read_status()
    assert status["state"] == "failed"
    assert status["phase"] == "shutdown"
    assert status["error_type"] == "RuntimeError"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"


def test_supervisor_countdown_worker_continues_when_shutdown_request_fails_but_runtime_stops(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _fake_sleep(_value: float) -> None:
        return None

    async def _fake_shutdown(*, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict:
        raise RuntimeError("runtime shutdown API unavailable")

    async def _fake_ensure_stopped(*, drain_timeout_sec: float, signal_delay_sec: float, reason: str) -> dict:
        return {"ok": True, "forced": True, "reason": reason}

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_request_runtime_shutdown", _fake_shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _fake_ensure_stopped)
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "requested_at": 1.0,
            "transitioned_at": 2.0,
            "updated_at": 2.0,
        }
    )

    asyncio.run(
        manager._countdown_update_worker(
            action="update",
            target_rev="HEAD",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=5.0,
            signal_delay_sec=0.1,
        )
    )

    status = read_status()
    assert status["state"] == "restarting"
    assert status["phase"] == "shutdown"
    assert status["forced_shutdown"] is True
    assert status["shutdown_request_error_type"] == "RuntimeError"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "active"


def test_ensure_runtime_stopped_for_update_forces_hung_process(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    timeline = {"now": 0.0}

    class _Proc:
        def __init__(self) -> None:
            self._alive = True
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return None if self._alive else 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self._alive = False

    proc = _Proc()
    manager._proc = proc

    async def _fake_sleep(value: float) -> None:
        timeline["now"] += max(0.1, float(value))

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(supervisor.time, "time", lambda: timeline["now"])

    result = asyncio.run(
        manager._ensure_runtime_stopped_for_update(
            drain_timeout_sec=1.0,
            signal_delay_sec=0.1,
            reason="test.hung_shutdown",
        )
    )

    assert result["ok"] is True
    assert result["forced"] is True
    assert proc.terminate_calls >= 1
    assert proc.kill_calls == 1
    assert proc.poll() == 0


def test_runtime_state_payload_reports_listener_and_api_readiness(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/B/repo", "venv_dir": "/slots/B/venv"},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["active_slot"] == "B"
    assert payload["managed_alive"] is True
    assert payload["listener_running"] is True
    assert payload["runtime_api_ready"] is False
    assert payload["runtime_state"] == "starting"
    assert payload["managed_executable"] == "python"
    assert payload["managed_matches_active_slot"] is True
    assert payload["slot_structure"]["ok"] is True
    assert payload["managed_cmdline"][1:3] == ["-m", "adaos.apps.autostart_runner"]


def test_runtime_state_payload_surfaces_previous_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"active_slot": "B", "previous_slot": "A", "slots": {}},
    )
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/B/repo", "venv_dir": "/slots/B/venv"},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["active_slot"] == "B"
    assert payload["previous_slot"] == "A"


def test_runtime_state_payload_surfaces_required_upstream_link_for_member(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._managed_transition_role = "member"
    manager._member_hub_watchdog_last_state = "ready"
    manager._member_hub_watchdog_last_reason = "member-hub link is connected"
    manager._member_hub_watchdog_reconnect_total = 3
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"active_slot": "B", "previous_slot": "A", "slots": {}},
    )
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/B/repo", "venv_dir": "/slots/B/venv"},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)

    payload = manager.status()

    assert payload["required_upstream_link"]["kind"] == "member_hub"
    assert payload["required_upstream_link"]["owner"] == "supervisor"
    assert payload["required_upstream_link"]["state"] == "ready"
    assert payload["required_upstream_link"]["ready"] is True
    assert payload["required_upstream_link"]["desired_state"] == "connected"
    assert payload["required_upstream_link"]["current_owner"] == "runtime"
    assert payload["required_upstream_link"]["planned_owner"] == "runtime"
    assert payload["required_upstream_link"]["future_owner"] == "sidecar"
    assert payload["required_upstream_link"]["continuity_mode"] == "runtime_bound"
    assert payload["required_upstream_link"]["reconnect_total"] == 3


def test_runtime_state_payload_reports_slot_mismatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["/wrong/python", "-m", "adaos.apps.autostart_runner"]
        cwd = "/wrong"

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["/expected/python", "-m", "adaos.apps.autostart_runner"],
            "cwd": "/expected",
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": False, "issues": ["nested_slot_dir:/slots/A/A"]},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["runtime_state"] == "spawned"
    assert payload["managed_matches_active_slot"] is False


def test_runtime_state_payload_uses_supervisor_recorded_cwd_when_subprocess_hides_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._managed_runtime_cwd = str(tmp_path)
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/A/repo", "venv_dir": "/slots/A/venv"},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["managed_cwd"] == str(tmp_path)
    assert payload["managed_matches_active_slot"] is True


def test_runtime_state_payload_includes_sidecar_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/A/repo", "venv_dir": "/slots/A/venv"},
    )
    monkeypatch.setattr(
        supervisor,
        "realtime_sidecar_listener_snapshot",
        lambda proc=None: {"listener_running": True, "managed_pid": 45678, "port": 7422},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["sidecar"]["enabled"] is True
    assert payload["sidecar"]["process"]["listener_running"] is True
    assert payload["sidecar"]["process"]["port"] == 7422


def test_supervisor_restart_sidecar_updates_process_and_optionally_reconnects_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._sidecar_proc = "old-proc"

    async def _restart_sidecar(*, proc, role=None):
        assert proc == "old-proc"
        assert role == "hub"
        return "new-proc", {"ok": True, "accepted": True, "reason": "restarted"}

    monkeypatch.setattr(manager, "_sidecar_role", lambda: "hub")
    monkeypatch.setattr(supervisor, "restart_realtime_sidecar_subprocess", _restart_sidecar)
    monkeypatch.setattr(manager, "_runtime_request_json", lambda **kwargs: {"ok": True, "accepted": True})
    monkeypatch.setattr(manager, "_runtime_sidecar_runtime_payload", lambda: {"transport_owner": "sidecar"})
    monkeypatch.setattr(
        supervisor,
        "realtime_sidecar_listener_snapshot",
        lambda proc=None: {"listener_running": True, "managed_pid": 77777, "proc": proc},
    )
    persisted: list[bool] = []
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: persisted.append(True))

    payload = asyncio.run(manager.restart_sidecar(reconnect_hub_root=True))

    assert manager._sidecar_proc == "new-proc"
    assert payload["restart"]["accepted"] is True
    assert payload["reconnect"]["ok"] is True
    assert payload["runtime"]["transport_owner"] == "sidecar"
    assert payload["process"]["proc"] == "new-proc"
    assert persisted


def test_runtime_state_payload_surfaces_root_promotion_requirement(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)

    payload = manager.status()

    assert payload["root_promotion_required"] is True
    assert "src/adaos/apps/supervisor.py" in payload["bootstrap_update"]["changed_paths"]


def test_runtime_state_payload_clears_root_promotion_requirement_when_root_matches_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    root_dir = tmp_path / "root"
    slot_repo = tmp_path / "slots" / "B" / "repo"
    (root_dir / "src" / "adaos" / "apps").mkdir(parents=True, exist_ok=True)
    (slot_repo / "src" / "adaos" / "apps").mkdir(parents=True, exist_ok=True)
    (root_dir / "src" / "adaos" / "apps" / "supervisor.py").write_text("same\n", encoding="utf-8")
    (slot_repo / "src" / "adaos" / "apps" / "supervisor.py").write_text("same\n", encoding="utf-8")

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "repo_dir": str(slot_repo),
            "root_repo_root": str(root_dir),
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)

    payload = manager.status()

    assert payload["root_promotion_required"] is False
    assert payload["bootstrap_update"]["required"] is True
    assert payload["bootstrap_update"]["effective_required"] is False
    assert payload["bootstrap_update"]["effective_mismatched_paths"] == []


def test_runtime_self_heal_decision_restarts_after_listener_loss_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_RUNTIME_STARTUP_GRACE_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._last_start_at = 100.0

    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_listener_restart_timeout_sec", lambda: 45.0)

    assert manager._runtime_self_heal_decision(now=120.0) is None
    assert manager._runtime_unhealthy_kind == "listener_lost"
    assert manager._runtime_unhealthy_since == 120.0
    assert manager._runtime_self_heal_decision(now=160.0) is None

    payload = manager._runtime_self_heal_decision(now=166.0)

    assert payload is not None
    assert payload["reason"] == "supervisor.runtime.listener_lost"
    assert payload["runtime_port"] == 8778


def test_runtime_self_heal_decision_respects_listener_startup_grace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._last_start_at = 100.0

    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_listener_restart_timeout_sec", lambda: 45.0)
    monkeypatch.setattr(supervisor, "_runtime_listener_startup_grace_sec", lambda: 90.0)

    assert manager._runtime_self_heal_decision(now=120.0) is None
    assert manager._runtime_unhealthy_kind == "listener_lost"
    assert manager._runtime_unhealthy_since == 120.0
    assert manager._runtime_self_heal_decision(now=160.0) is None
    assert manager._runtime_self_heal_decision(now=189.0) is None

    payload = manager._runtime_self_heal_decision(now=191.0)

    assert payload is not None
    assert payload["reason"] == "supervisor.runtime.listener_lost"
    assert payload["runtime_port"] == 8778


def test_runtime_self_heal_decision_restarts_after_api_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._last_start_at = 100.0

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_restart_timeout_sec", lambda: 60.0)

    assert manager._runtime_self_heal_decision(now=110.0) is None
    assert manager._runtime_unhealthy_kind == "api_unready"
    assert manager._runtime_unhealthy_since == 110.0

    payload = manager._runtime_self_heal_decision(now=171.0)

    assert payload is not None
    assert payload["reason"] == "supervisor.runtime.api_unready"
    assert payload["runtime_port"] == 8777


def test_runtime_self_heal_decision_skips_listener_restart_while_update_apply_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._last_start_at = 100.0
    manager._runtime_unhealthy_since = 120.0
    manager._runtime_unhealthy_kind = "listener_lost"

    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "applying", "phase": "apply"})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)

    payload = manager._runtime_self_heal_decision(now=200.0)

    assert payload is None
    assert manager._runtime_unhealthy_since is None
    assert manager._runtime_unhealthy_kind is None


def test_runtime_self_heal_decision_restarts_slot_mismatch_even_when_apply_status_is_stale(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._desired_running = True
    manager._managed_runtime_cwd = "/slots/B/repo"
    manager._last_start_at = 100.0

    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "applying", "phase": "apply"})
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {"slot": "A", "argv": ["/slots/A/venv/bin/python"], "cwd": "/slots/A/repo"},
    )
    monkeypatch.setattr(
        supervisor,
        "_proc_details",
        lambda proc, cwd_hint=None: {
            "managed_pid": 4321,
            "managed_alive": True,
            "managed_cmdline": ["/slots/B/venv/bin/python", "-m", "adaos.apps.autostart_runner"],
            "managed_executable": "/slots/B/venv/bin/python",
            "managed_cwd": "/slots/B/repo",
        },
    )

    payload = manager._runtime_self_heal_decision(now=200.0)

    assert isinstance(payload, dict)
    assert payload["reason"] == "supervisor.runtime.slot_mismatch"
    assert payload["active_slot"] == "A"
    assert payload["managed_executable"] == "/slots/B/venv/bin/python"
    assert payload["expected_managed_executable"] == "/slots/A/venv/bin/python"


def test_runtime_state_payload_surfaces_warm_switch_admission(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                return type("Mem", (), {"rss": 256 * 1024 * 1024})()

        @staticmethod
        def virtual_memory():
            return type("VM", (), {"available": 1024 * 1024 * 1024})()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "planned_reason": "minimum_update_period",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["runtime_port"] == 8777
    assert payload["candidate_slot"] == "B"
    assert payload["candidate_runtime_port"] == 8778
    assert payload["transition_mode"] == "warm_switch"
    assert payload["warm_switch_supported"] is True
    assert payload["warm_switch_allowed"] is True
    assert payload["slot_ports"]["A"] == 8777
    assert payload["slot_ports"]["B"] == 8778


def test_runtime_state_payload_falls_back_to_stop_and_switch_when_memory_is_low(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                return type("Mem", (), {"rss": 256 * 1024 * 1024})()

        @staticmethod
        def virtual_memory():
            return type("VM", (), {"available": 300 * 1024 * 1024})()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["transition_mode"] == "stop_and_switch"
    assert payload["warm_switch_allowed"] is False
    assert "insufficient memory" in str(payload["warm_switch_reason"] or "")


def test_runtime_state_payload_uses_process_family_rss_for_warm_switch_gate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _PsChild:
        def __init__(self, pid: int, rss: int) -> None:
            self.pid = pid
            self._rss = rss

        def memory_info(self):
            return type("Mem", (), {"rss": self._rss})()

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                if self.pid == 32123:
                    return type("Mem", (), {"rss": 128 * 1024 * 1024})()
                raise AssertionError(f"unexpected pid {self.pid}")

            def children(self, recursive: bool = False):
                assert recursive is True
                return [
                    _PsChild(40001, 256 * 1024 * 1024),
                    _PsChild(40002, 256 * 1024 * 1024),
                ]

        @staticmethod
        def virtual_memory():
            return type("VM", (), {"available": 900 * 1024 * 1024})()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["warm_switch_allowed"] is False
    assert payload["transition_mode"] == "stop_and_switch"
    assert payload["warm_switch_memory"]["current_process_rss_bytes"] == 128 * 1024 * 1024
    assert payload["warm_switch_memory"]["current_family_rss_bytes"] == 640 * 1024 * 1024
    assert payload["warm_switch_memory"]["current_rss_bytes"] == 640 * 1024 * 1024


def test_runtime_state_payload_reserves_total_memory_percent_for_warm_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                return type("Mem", (), {"rss": 1024 * 1024 * 1024})()

            def children(self, recursive: bool = False):
                assert recursive is True
                return []

        @staticmethod
        def virtual_memory():
            return type(
                "VM",
                (),
                {
                    "available": 2200 * 1024 * 1024,
                    "total": 4096 * 1024 * 1024,
                },
            )()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["transition_mode"] == "stop_and_switch"
    assert payload["warm_switch_allowed"] is False
    assert "insufficient memory" in str(payload["warm_switch_reason"] or "")
    assert payload["warm_switch_memory"]["total_bytes"] == 4096 * 1024 * 1024
    assert payload["warm_switch_memory"]["reserve_percent"] == 30.0
    assert payload["warm_switch_memory"]["reserve_bytes"] == int(4096 * 1024 * 1024 * 0.30)


def test_supervisor_promote_root_marks_update_succeeded(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "target_version": "1.2.3",
            "git_commit": "1.2.3",
            "git_short_commit": "1.2.3",
            "repo_dir": str(tmp_path / "slots" / "B" / "repo"),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
            "backup_dir": str(tmp_path / "backup"),
            "promoted_paths": ["src/adaos/apps/supervisor.py"],
            "removed_paths": [],
            "restart_required": True,
        },
    )
    supervisor._write_update_attempt({"state": "active", "action": "update", "updated_at": 1.0})
    write_status({"state": "validated", "phase": "root_promotion_pending", "target_slot": "B"})

    payload = asyncio.run(manager.promote_root(reason="test.root_promotion"))

    assert payload["accepted"] is True
    assert payload["status"]["state"] == "succeeded"
    assert payload["status"]["phase"] == "root_promoted"
    assert payload["root_promotion"]["restart_required"] is True
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"
    assert attempt["last_status"]["phase"] == "root_promoted"


def test_supervisor_promote_root_preserves_subsequent_transition_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "target_version": "1.2.3",
            "git_commit": "1.2.3",
            "git_short_commit": "1.2.3",
            "repo_dir": str(tmp_path / "slots" / "B" / "repo"),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda *, slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "restart_required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
        },
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "subsequent_transition": True,
            "subsequent_transition_requested_at": 410.0,
            "subsequent_transition_request": {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": "1.2.4",
                "reason": "test.subsequent",
            },
            "updated_at": 400.0,
        }
    )
    write_status({"state": "validated", "phase": "root_promotion_pending", "target_slot": "B"})

    payload = asyncio.run(manager.promote_root(reason="test.root_promotion"))

    assert payload["status"]["phase"] == "root_promoted"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"
    assert attempt["subsequent_transition"] is True
    assert attempt["subsequent_transition_request"]["target_version"] == "1.2.4"


def test_supervisor_promote_root_allows_idle_status_when_root_promotion_is_still_required(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "repo_dir": str(tmp_path / "slots" / "B" / "repo"),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(
        supervisor,
        "resolved_root_promotion_requirement",
        lambda manifest: (
            True,
            {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
                "effective_required": True,
            },
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
            "backup_dir": str(tmp_path / "backup"),
            "promoted_paths": ["src/adaos/apps/supervisor.py"],
            "removed_paths": [],
            "restart_required": True,
        },
    )
    write_status({"state": "idle", "message": "autostart runner boot"})

    payload = asyncio.run(manager.promote_root(reason="test.root_promotion"))

    assert payload["accepted"] is True
    assert payload["status"]["phase"] == "root_promoted"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"


def test_supervisor_schedule_service_restart_requests_self_exit(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "_autostart_self_restart_supported", lambda: True)
    monkeypatch.setattr(supervisor, "_root_restart_delay_sec", lambda: 0.1)
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 4321)
    monkeypatch.setattr(manager, "_refresh_autostart_wrapper", lambda reason: {"ok": True, "reason": reason})

    sleeps: list[float] = []
    kills: list[tuple[int, int]] = []

    monkeypatch.setattr(supervisor.time, "sleep", lambda sec: sleeps.append(sec))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    payload = manager._schedule_service_restart(reason="test.root_restart")

    thread = manager._service_restart_thread
    assert thread is not None
    thread.join(timeout=1.0)

    assert payload["requested"] is True
    assert payload["mode"] == "self_exit"
    assert payload["wrapper_refresh"] == {"ok": True, "reason": "test.root_restart"}
    assert sleeps == [0.1]
    assert kills == [(4321, supervisor.signal.SIGTERM)]


def test_supervisor_complete_update_promotes_root_and_requests_self_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "repo_dir": str(tmp_path / "slots" / "B" / "repo"),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(
        supervisor,
        "resolved_root_promotion_requirement",
        lambda manifest: (
            True,
            {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
                "effective_required": True,
            },
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
            "backup_dir": str(tmp_path / "backup"),
            "promoted_paths": ["src/adaos/apps/supervisor.py"],
            "removed_paths": [],
            "restart_required": True,
        },
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "root_promotion_required": str(read_status().get("phase") or "").strip().lower() == "root_promotion_pending",
            "active_slot": "B",
            "runtime_state": "ready",
            "runtime_url": "http://127.0.0.1:8778",
            "runtime_port": 8778,
        },
    )

    restart_reasons: list[str] = []

    def _schedule_service_restart(*, reason: str) -> dict[str, object]:
        restart_reasons.append(reason)
        return {"ok": True, "requested": True, "mode": "self_exit", "delay_sec": 0.25}

    monkeypatch.setattr(manager, "_schedule_service_restart", _schedule_service_restart)

    supervisor._write_update_attempt({"state": "active", "action": "update", "requested_at": 1.0, "updated_at": 1.0})
    write_status({"state": "validated", "phase": "root_promotion_pending", "action": "update", "target_slot": "B"})

    payload = asyncio.run(manager.complete_update(reason="test.complete"))

    assert payload["accepted"] is True
    assert payload["restart_required"] is True
    assert payload["status"]["phase"] == "root_promoted"
    assert payload["status"]["root_promotion_required"] is False
    assert payload["status"]["restart_mode"] == "self_exit"
    assert payload["restart"]["requested"] is True
    assert payload["runtime"]["root_promotion_required"] is False
    assert restart_reasons == ["test.complete"]
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"
    assert attempt["restart_mode"] == "self_exit"
    assert attempt["restart_requested_at"] > 0


def test_supervisor_auto_complete_does_not_repeat_root_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "active_slot": "B",
            "runtime_state": "starting",
            "runtime_url": "http://127.0.0.1:8778",
            "runtime_port": 8778,
        },
    )
    restart_reasons: list[str] = []
    monkeypatch.setattr(
        manager,
        "_schedule_service_restart",
        lambda *, reason: restart_reasons.append(reason) or {"ok": True, "requested": True},
    )
    write_status(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "action": "update",
            "target_slot": "B",
            "restart_requested_at": 431.0,
            "restart_mode": "self_exit",
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "awaiting_restart": True,
            "restart_required": True,
            "restart_requested_at": 431.0,
            "restart_mode": "self_exit",
            "updated_at": 431.0,
        }
    )

    payload = asyncio.run(manager.complete_update(reason="supervisor.auto_update_complete", auto=True))

    assert payload["accepted"] is False
    assert payload["noop"] is True
    assert payload["restart"]["already_requested"] is True
    assert payload["restart"]["restart_requested_at"] == 431.0
    assert restart_reasons == []
    assert supervisor._read_update_attempt()["state"] == "awaiting_root_restart"


def test_supervisor_maybe_resume_auto_completes_root_promotion_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "_autostart_self_restart_supported", lambda: True)
    write_status({"state": "validated", "phase": "root_promotion_pending", "action": "update"})
    supervisor._write_update_attempt({"state": "active", "action": "update", "updated_at": 1.0})

    captured: dict[str, object] = {}

    async def _complete_update(*, reason: str, auto: bool = False) -> dict[str, object]:
        captured["reason"] = reason
        captured["auto"] = auto
        return {"ok": True}

    monkeypatch.setattr(manager, "complete_update", _complete_update)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert captured == {"reason": "supervisor.auto_update_complete", "auto": True}


def test_public_update_status_payload_is_browser_safe() -> None:
    payload = supervisor._public_update_status_payload(
        {
            "status": {
                "action": "update",
                "state": "restarting",
                "phase": "shutdown",
                "message": "countdown completed; pending update written",
                "target_rev": "rev2026",
                "target_version": "0.1.0+1.abc",
                "planned_reason": "minimum_update_period",
                "min_update_period_sec": 300.0,
                "scheduled_for": 456.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 400.0,
                "candidate_prewarm_state": "ready",
                "candidate_prewarm_message": "passive candidate runtime is ready on http://127.0.0.1:8778",
                "candidate_prewarm_ready_at": 430.0,
                "restart_mode": "self_exit",
                "restart_requested_at": 431.0,
                "updated_at": 123.0,
                "error": "hidden",
            },
            "runtime": {
                "active_slot": "A",
                "runtime_state": "spawned",
                "runtime_url": "http://127.0.0.1:8777",
                "runtime_port": 8777,
                "runtime_instance_id": "rt-a-a1b2c3d4",
                "transition_role": "active",
                "listener_running": False,
                "runtime_api_ready": False,
                "candidate_slot": "B",
                "candidate_runtime_url": "http://127.0.0.1:8778",
                "candidate_runtime_port": 8778,
                "candidate_runtime_instance_id": "rt-b-c9d8e7f6",
                "candidate_transition_role": "candidate",
                "candidate_listener_running": True,
                "candidate_runtime_api_ready": True,
                "candidate_runtime_state": "ready",
                "transition_mode": "warm_switch",
                "warm_switch_supported": True,
                "warm_switch_allowed": True,
                "warm_switch_reason": "warm switch admitted",
                "slot_ports": {"A": 8777, "B": 8778},
                "root_promotion_required": True,
                "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
                "managed_cmdline": ["hidden"],
            },
            "attempt": {
                "action": "update",
                "state": "awaiting_root_restart",
                "awaiting_restart": True,
                "planned_reason": "minimum_update_period",
                "scheduled_for": 456.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 400.0,
                "candidate_prewarm_state": "ready",
                "candidate_prewarm_message": "passive candidate runtime is ready on http://127.0.0.1:8778",
                "restart_mode": "self_exit",
                "restart_requested_at": 431.0,
                "updated_at": 222.0,
            },
            "_served_by": "supervisor_fallback",
        }
    )

    assert payload["ok"] is True
    assert payload["status"]["action"] == "update"
    assert payload["status"]["state"] == "restarting"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["status"]["planned_reason"] == "minimum_update_period"
    assert payload["status"]["scheduled_for"] == 456.0
    assert payload["status"]["subsequent_transition"] is True
    assert payload["status"]["candidate_prewarm_state"] == "ready"
    assert payload["status"]["candidate_prewarm_ready_at"] == 430.0
    assert payload["status"]["restart_mode"] == "self_exit"
    assert payload["status"]["restart_requested_at"] == 431.0
    assert payload["attempt"]["state"] == "awaiting_root_restart"
    assert payload["attempt"]["contract_version"] == "1"
    assert payload["attempt"]["authority"] == "supervisor"
    assert payload["attempt"]["action"] == "update"
    assert payload["attempt"]["awaiting_restart"] is True
    assert payload["attempt"]["planned_reason"] == "minimum_update_period"
    assert payload["attempt"]["scheduled_for"] == 456.0
    assert payload["attempt"]["subsequent_transition"] is True
    assert payload["attempt"]["candidate_prewarm_state"] == "ready"
    assert payload["attempt"]["restart_mode"] == "self_exit"
    assert payload["attempt"]["restart_requested_at"] == 431.0
    assert payload["runtime"]["active_slot"] == "A"
    assert payload["runtime"]["runtime_instance_id"] == "rt-a-a1b2c3d4"
    assert payload["runtime"]["transition_role"] == "active"
    assert payload["runtime"]["runtime_url"] == "http://127.0.0.1:8777"
    assert payload["runtime"]["candidate_runtime_url"] == "http://127.0.0.1:8778"
    assert payload["runtime"]["candidate_runtime_instance_id"] == "rt-b-c9d8e7f6"
    assert payload["runtime"]["candidate_transition_role"] == "candidate"
    assert payload["runtime"]["candidate_runtime_state"] == "ready"
    assert payload["runtime"]["candidate_runtime_api_ready"] is True
    assert payload["runtime"]["transition_mode"] == "warm_switch"
    assert payload["runtime"]["slot_ports"]["B"] == 8778
    assert payload["runtime"]["root_promotion_required"] is True
    assert payload["_served_by"] == "supervisor_fallback"
    assert "managed_cmdline" not in payload["runtime"]
    assert "error" not in payload["status"]


def test_public_update_status_payload_prefers_runtime_root_promotion_flag() -> None:
    payload = supervisor._public_update_status_payload(
        {
            "status": {
                "state": "succeeded",
                "phase": "validate",
            },
            "runtime": {
                "root_promotion_required": False,
                "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
            },
        }
    )

    assert payload["runtime"]["root_promotion_required"] is False


def test_public_update_status_endpoint_is_unauthenticated(monkeypatch) -> None:
    class _Manager:
        def public_update_status(self) -> dict:
            return {
                "ok": True,
                "status": {"state": "restarting", "phase": "shutdown"},
                "runtime": {"runtime_state": "spawned"},
            }

    monkeypatch.setattr(supervisor, "_manager", lambda: _Manager())
    client = TestClient(supervisor.app)

    response = client.get("/api/supervisor/public/update-status")

    assert response.status_code == 200
    assert response.json()["status"]["state"] == "restarting"


def test_public_memory_status_endpoint_is_unauthenticated(monkeypatch) -> None:
    class _Manager:
        def public_memory_status(self) -> dict:
            return {
                "ok": True,
                "memory": {
                    "current_profile_mode": "normal",
                    "profile_control_mode": "phase2_supervisor_restart",
                    "sessions_total": 2,
                },
            }

    monkeypatch.setattr(supervisor, "_manager", lambda: _Manager())
    client = TestClient(supervisor.app)

    response = client.get("/api/supervisor/public/memory-status")

    assert response.status_code == 200
    assert response.json()["memory"]["profile_control_mode"] == "phase2_supervisor_restart"


def test_public_update_status_does_not_probe_runtime_admin_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "ok": True,
            "runtime_api_ready": False,
            "runtime_state": "spawned",
            "active_slot": "A",
        },
    )
    write_status(
        {
            "state": "restarting",
            "phase": "shutdown",
            "action": "update",
            "message": "countdown completed; pending update written",
        }
    )

    def _unexpected_get(*args, **kwargs):
        raise AssertionError("public_update_status must not call runtime admin update endpoint")

    monkeypatch.setattr(supervisor.requests, "get", _unexpected_get)

    payload = manager.public_update_status()

    assert payload["status"]["state"] == "restarting"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["runtime"]["runtime_state"] == "spawned"


def test_public_memory_status_uses_compact_last_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "load_config", lambda: object())
    monkeypatch.setattr(
        supervisor,
        "report_hub_memory_profile",
        lambda conf, session_summary, operations=None, telemetry=None: {
            "ok": True,
            "reported_at": 33.0,
            "_protocol": {"message_id": "root-msg-1", "cursor": 1},
        },
    )

    manager.start_memory_profile(profile_mode="sampled_profile", reason="operator.request")
    session_id = manager.memory_status()["requested_session_id"]
    manager.publish_memory_profile(session_id, reason="operator.publish")

    payload = manager.public_memory_status()

    assert payload["memory"]["profile_control_mode"] == "phase2_supervisor_restart"
    assert payload["memory"]["last_session"]["session_id"] == session_id
    assert payload["memory"]["last_session"]["publish_state"] == "published"
    assert payload["memory"]["auto_profile_min_uptime_sec"] == 300.0


def test_memory_policy_auto_profile_waits_for_min_uptime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._last_start_at = 100.0

    allowed, reason = manager._memory_policy_auto_profile_guard(now=250.0)

    assert allowed is False
    assert str(reason).startswith("auto_profile_min_uptime:")

    allowed_after_grace, reason_after_grace = manager._memory_policy_auto_profile_guard(now=401.0)

    assert allowed_after_grace is True
    assert reason_after_grace is None


def test_available_memory_bytes_and_total_memory_bytes_read_psutil(monkeypatch) -> None:
    class _Vm:
        available = 123
        total = 456

    monkeypatch.setattr(supervisor, "psutil", SimpleNamespace(virtual_memory=lambda: _Vm()))

    assert supervisor._available_memory_bytes() == 123
    assert supervisor._total_memory_bytes() == 456


def test_memory_policy_auto_profile_is_blocked_while_hub_has_connected_members(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._last_start_at = 100.0
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.0: {
            "node": {"role": "hub"},
            "hub_member_connection_state": {"connected_total": 1},
        },
    )

    allowed, reason = manager._memory_policy_auto_profile_guard(now=401.0)

    assert allowed is False
    assert reason == "subnet_members_connected:1"


def test_policy_memory_profile_restart_is_delayed_during_min_uptime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 12345
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()  # type: ignore[assignment]
    manager._last_start_at = 100.0
    manager._memory_active_session_id = "mem-policy"
    manager._memory_requested_profile_mode = "sampled_profile"
    supervisor.write_memory_session_summary(
        "mem-policy",
        {
            "session_id": "mem-policy",
            "profile_mode": "sampled_profile",
            "session_state": "requested",
            "trigger_source": "policy",
            "trigger_reason": "memory.growth_and_slope_threshold",
            "requested_at": 150.0,
        },
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 200.0)
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)
    restarts: list[str] = []

    async def _restart_runtime(*, reason: str):
        restarts.append(reason)
        return {"ok": True}

    monkeypatch.setattr(manager, "restart_runtime", _restart_runtime)

    asyncio.run(manager._maybe_apply_memory_profile_mode())

    assert restarts == []
    assert str(manager._memory_auto_profile_last_block_reason).startswith("auto_profile_min_uptime:")


def test_policy_memory_profile_restart_is_blocked_while_member_link_is_connected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 22334
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()  # type: ignore[assignment]
    manager._last_start_at = 100.0
    manager._memory_active_session_id = "mem-member"
    manager._memory_requested_profile_mode = "sampled_profile"
    supervisor.write_memory_session_summary(
        "mem-member",
        {
            "session_id": "mem-member",
            "profile_mode": "sampled_profile",
            "session_state": "requested",
            "trigger_source": "policy",
            "trigger_reason": "memory.growth_and_slope_threshold",
            "requested_at": 150.0,
        },
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.0: {
            "node": {"role": "member", "connected_to_hub": True},
            "hub_member_connection_state": {"hub": {"connected": True}},
        },
    )
    restarts: list[str] = []

    async def _restart_runtime(*, reason: str):
        restarts.append(reason)
        return {"ok": True}

    monkeypatch.setattr(manager, "restart_runtime", _restart_runtime)

    asyncio.run(manager._maybe_apply_memory_profile_mode())

    assert restarts == []
    assert manager._memory_auto_profile_last_block_reason == "member_hub_connected"


def test_policy_memory_profile_restart_is_blocked_by_connected_to_subnet_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 22334
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()  # type: ignore[assignment]
    manager._last_start_at = 100.0
    manager._memory_active_session_id = "mem-member"
    manager._memory_requested_profile_mode = "sampled_profile"
    supervisor.write_memory_session_summary(
        "mem-member",
        {
            "session_id": "mem-member",
            "profile_mode": "sampled_profile",
            "session_state": "requested",
            "trigger_source": "policy",
            "trigger_reason": "memory.growth_and_slope_threshold",
            "requested_at": 150.0,
        },
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.0: {
            "node": {"role": "member", "connected_to_subnet": True},
            "hub_member_connection_state": {"hub": {"connected": False}},
        },
    )
    restarts: list[str] = []

    async def _restart_runtime(*, reason: str):
        restarts.append(reason)
        return {"ok": True}

    monkeypatch.setattr(manager, "restart_runtime", _restart_runtime)

    asyncio.run(manager._maybe_apply_memory_profile_mode())

    assert restarts == []
    assert manager._memory_auto_profile_last_block_reason == "member_hub_connected"


def test_critical_memory_restart_is_allowed_while_live_subnet_is_present(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_AVAILABLE_PERCENT", "5")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_AVAILABLE_BYTES", "64")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_CRITICAL_DURATION_SEC", "20")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 9988
        args = ["python", "-m", "adaos.apps.autostart_runner"]

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()  # type: ignore[assignment]
    manager._desired_running = True
    manager._stopping = False
    manager._memory_last_available_bytes = 32
    monkeypatch.setattr(supervisor, "_total_memory_bytes", lambda: 1024)
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: {})
    monkeypatch.setattr(
        manager,
        "_runtime_reliability_payload",
        lambda timeout=1.0: {
            "node": {"role": "hub"},
            "hub_member_connection_state": {"connected_total": 2},
        },
    )

    first = manager._memory_critical_restart_decision(now=100.0)
    second = manager._memory_critical_restart_decision(now=121.0)

    assert first is None
    assert second is not None
    assert second["reason"] == "supervisor.memory.critical_pressure"
    assert second["subnet_live"] is True
    assert second["subnet_reason"] == "subnet_members_connected:2"


def test_spawn_runtime_locked_prefers_active_slot_manifest(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 4242

        @staticmethod
        def poll():
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": "/slot/repo",
            "env": {"PYTHONPATH": "/slot/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"A": {"path": "/slots/A"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_runtime_locked(reason="test.spawn"))

    assert captured["args"][0] == "/slot/python"
    assert captured["kwargs"]["cwd"] == "/slot/repo"
    assert captured["kwargs"]["env"]["PYTHONPATH"] == "/slot/repo/src"
    assert captured["kwargs"]["env"]["ADAOS_ACTIVE_CORE_SLOT"] == "A"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_TRANSITION_ROLE"] == "active"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8777"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-a-a-")
    assert manager.status()["managed_start_reason"] == "test.spawn"


def test_spawn_runtime_locked_uses_slot_specific_port_for_slot_b(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 4343

        @staticmethod
        def poll():
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": "/slot/repo",
            "env": {"PYTHONPATH": "/slot/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"B": {"path": "/slots/B"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_runtime_locked())

    assert captured["args"][-1] == "8778"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8778"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-b-a-")


def test_spawn_candidate_runtime_locked_uses_candidate_role_and_skips_pending_update(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 5151

        @staticmethod
        def poll():
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "read_slot_manifest",
        lambda slot: {
            "slot": slot,
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": f"/slots/{slot}/repo",
            "env": {"PYTHONPATH": f"/slots/{slot}/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"B": {"path": "/slots/B"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_candidate_runtime_locked(slot="B", reason="test.candidate"))

    assert captured["args"][-1] == "8778"
    assert captured["kwargs"]["cwd"] == "/slots/B/repo"
    assert captured["kwargs"]["env"]["ADAOS_ACTIVE_CORE_SLOT"] == "B"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_TRANSITION_ROLE"] == "candidate"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8778"
    assert captured["kwargs"]["env"]["ADAOS_SKIP_PENDING_CORE_UPDATE"] == "1"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-b-c-")
    assert manager.status()["candidate_start_reason"] == "test.candidate"


def test_restart_runtime_records_last_stop_and_start_reason(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _CurrentProc:
        pid = 6060

        @staticmethod
        def poll():
            return None

    class _SpawnedProc:
        pid = 6161

        @staticmethod
        def poll():
            return None

    async def _fake_terminate_proc_locked(*, proc=None, base_url=None, graceful: bool, reason: str) -> None:
        captured["terminate"] = {
            "proc": proc,
            "base_url": base_url,
            "graceful": graceful,
            "reason": reason,
        }
        manager._proc = None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _SpawnedProc()

    manager._proc = _CurrentProc()
    monkeypatch.setattr(manager, "_transition_continuity_guard_decision", lambda operation: None)
    monkeypatch.setattr(manager, "_terminate_proc_locked", _fake_terminate_proc_locked)
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": "/slot/repo",
            "env": {"PYTHONPATH": "/slot/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"A": {"path": "/slots/A"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    payload = asyncio.run(manager.restart_runtime(reason="test.restart"))

    assert captured["terminate"]["reason"] == "test.restart"
    assert captured["args"][0] == "/slot/python"
    assert payload["managed_start_reason"] == "test.restart"
    assert payload["last_stop_reason"] == "test.restart"
    assert payload["restart_count"] == 1


def test_stop_candidate_runtime_persists_last_stop_reason_after_candidate_clears(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _CandidateProc:
        pid = 7171

        @staticmethod
        def poll():
            return None

    async def _fake_terminate_proc_locked(*, proc=None, base_url=None, graceful: bool, reason: str) -> None:
        captured["terminate"] = {
            "proc": proc,
            "base_url": base_url,
            "graceful": graceful,
            "reason": reason,
        }

    manager._candidate_proc = _CandidateProc()
    manager._candidate_slot = "B"
    manager._candidate_runtime_instance_id = "rt-b-c-test"
    manager._candidate_transition_role = "candidate"
    monkeypatch.setattr(manager, "_terminate_proc_locked", _fake_terminate_proc_locked)

    payload = asyncio.run(manager.stop_candidate_runtime(reason="test.candidate.stop"))

    assert captured["terminate"]["reason"] == "test.candidate.stop"
    assert payload["candidate_slot"] is None
    assert payload["candidate_last_stop_reason"] == "test.candidate.stop"


def test_runtime_state_payload_surfaces_candidate_runtime_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _ActiveProc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path / "active")

        @staticmethod
        def poll():
            return None

    class _CandidateProc:
        pid = 32124
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8778"]
        cwd = str(tmp_path / "candidate")

        @staticmethod
        def poll():
            return None

    manager._proc = _ActiveProc()
    manager._candidate_proc = _CandidateProc()
    manager._candidate_slot = "B"
    manager._candidate_runtime_instance_id = "rt-b-c-12345678"
    manager._candidate_transition_role = "candidate"
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path / "active"),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "read_slot_manifest",
        lambda slot: {
            "slot": slot,
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path / "candidate"),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(
        supervisor,
        "_listener_running",
        lambda host, port, **kwargs: int(port) in {8777, 8778},
    )
    monkeypatch.setattr(
        supervisor,
        "_runtime_api_ready",
        lambda base_url, **kwargs: base_url.endswith(":8777") or base_url.endswith(":8778"),
    )

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["candidate_runtime_port"] == 8778
    assert payload["candidate_runtime_instance_id"] == "rt-b-c-12345678"
    assert payload["candidate_transition_role"] == "candidate"
    assert payload["candidate_runtime_state"] == "ready"
    assert payload["candidate_runtime_api_ready"] is True


def test_runtime_state_payload_hides_candidate_after_root_restart_completion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8778"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "target_slot": "B",
            "root_restart_completed_at": 499.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_slot": "B",
            "updated_at": 499.0,
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "A")

    payload = manager.status()

    assert payload["candidate_slot"] is None
    assert payload["candidate_runtime_url"] is None
    assert payload["candidate_runtime_state"] is None
    assert payload["candidate_transition_role"] is None
