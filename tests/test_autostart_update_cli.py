from __future__ import annotations

import json
import sys
import types

from typer.testing import CliRunner

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.apps.cli.commands.setup import autostart_app
from adaos.apps.cli.commands import setup as setup_cmd
from requests import ConnectionError as RequestsConnectionError


def test_autostart_admin_base_url_prefers_local_control_on_member(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(setup_cmd, "get_ctx", lambda: types.SimpleNamespace(config=types.SimpleNamespace()))
    monkeypatch.setattr(setup_cmd, "autostart_status", lambda ctx: {})
    monkeypatch.setattr(setup_cmd, "_resolve_stop_bind", lambda conf: None)
    monkeypatch.setattr(setup_cmd, "_autostart_cli_token", lambda token=None: "")
    monkeypatch.setattr(setup_cmd, "resolve_control_token", lambda explicit=None: "dev-local-token")
    monkeypatch.setattr(
        setup_cmd,
        "resolve_control_base_url",
        lambda **kwargs: calls.append(dict(kwargs)) or "http://127.0.0.1:8779",
    )
    monkeypatch.setattr(
        setup_cmd,
        "probe_control_api",
        lambda *, base_url, token, timeout_s=0.75: (200, {"ok": True}),
    )

    base = setup_cmd._autostart_admin_base_url()

    assert base == "http://127.0.0.1:8779"
    assert calls == [{"prefer_local": True}]


def test_autostart_admin_headers_resolve_token_for_selected_base(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(setup_cmd, "_autostart_cli_token", lambda token=None: "")
    monkeypatch.setattr(
        setup_cmd,
        "resolve_control_token",
        lambda *, explicit=None, base_url=None: calls.append({"explicit": explicit, "base_url": base_url}) or "wrapper-service-token",
    )

    headers = setup_cmd._autostart_admin_headers(base_url="http://127.0.0.1:8779")

    assert headers == {
        "Content-Type": "application/json",
        "X-AdaOS-Token": "wrapper-service-token",
    }
    assert calls == [{"explicit": None, "base_url": "http://127.0.0.1:8779"}]


def test_autostart_update_status_uses_local_admin_api(monkeypatch) -> None:
    runner = CliRunner()
    def _fake_supervisor_get(path, *, token=None):
        if path == "/api/supervisor/update/status":
            return {
                "ok": True,
                "status": {"state": "idle", "message": "boot", "target_rev": "rev2026"},
                "slots": {
                    "active_slot": "A",
                    "previous_slot": "B",
                    "slots": {
                        "A": {
                            "manifest": {
                                "target_version": "0.1.0",
                                "git_short_commit": "8e2f6e75",
                                "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                                "git_branch": "rev2026",
                                "git_subject": "feat: add git webhook",
                            }
                        },
                        "B": {"manifest": {"target_version": "0.1.0", "git_short_commit": "4a525775", "git_branch": "rev2026"}},
                    },
                },
            }
        if path == "/api/supervisor/public/memory-status":
            return {
                "ok": True,
                "memory": {
                    "current_profile_mode": "normal",
                    "profile_control_mode": "phase2_supervisor_restart",
                    "suspicion_state": "idle",
                    "sessions_total": 1,
                    "last_session": {
                        "session_id": "mem-001",
                        "session_state": "requested",
                        "profile_mode": "sampled_profile",
                        "publish_state": "local_only",
                    },
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_get", _fake_supervisor_get)
    monkeypatch.setattr(setup_cmd, "_slot_build_version", lambda slot_id: "0.1.0+42.8e2f6e75" if slot_id == "A" else "")

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "target rev: rev2026" in result.output
    assert "active build version: 0.1.0+42.8e2f6e75" in result.output
    assert "memory: mode=normal control=phase2_supervisor_restart suspicion=idle sessions=1" in result.output
    assert "memory last session: id=mem-001 state=requested mode=sampled_profile publish=local_only" in result.output
    assert "active slot: A | 0.1.0+42.8e2f6e75 | 8e2f6e75 | rev2026" in result.output
    assert "active commit: 8e2f6e7529b60f67094a7951e690558c67fdf333" in result.output


def test_autostart_update_status_falls_back_to_active_manifest_payload(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "idle", "message": "boot"},
            "slots": {
                "active_slot": "B",
                "previous_slot": "A",
                "slots": {
                    "A": {"manifest": {}},
                    "B": {"manifest": {}},
                },
            },
            "active_manifest": {
                "target_version": "0.1.0",
                "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                "git_branch": "rev2026",
                "git_subject": "feat: add git webhook",
            },
        },
    )
    monkeypatch.setattr(setup_cmd, "_slot_build_version", lambda slot_id: "0.1.0+42.8e2f6e75" if slot_id == "B" else "")

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "active build version: 0.1.0+42.8e2f6e75" in result.output
    assert "active slot: B | 0.1.0+42.8e2f6e75 | 8e2f6e75 | rev2026" in result.output
    assert "active commit: 8e2f6e7529b60f67094a7951e690558c67fdf333" in result.output

def test_autostart_update_status_prints_supervisor_attempt(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "attempt": {
                "state": "awaiting_root_restart",
                "contract_version": "1",
                "authority": "supervisor",
                "planned_reason": "root promotion is staged and waiting for restart",
                "completion_reason": "runtime handoff is blocked on service restart",
            },
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "supervisor attempt: awaiting_root_restart" in result.output
    assert "attempt contract: v1" in result.output
    assert "attempt authority: supervisor" in result.output
    assert "planned reason: root promotion is staged and waiting for restart" in result.output
    assert "completion reason: runtime handoff is blocked on service restart" in result.output
    assert "next step: supervisor/bootstrap update is promoted; ensure adaos.service restart completes" in result.output


def test_autostart_update_status_falls_back_to_public_supervisor_surface(monkeypatch) -> None:
    runner = CliRunner()

    def _supervisor_get(path, *, token=None):
        if path == "/api/supervisor/update/status":
            raise RuntimeError("private supervisor surface unavailable")
        if path == "/api/supervisor/public/update-status":
            return {
                "ok": True,
                "status": {"state": "planned", "phase": "scheduled", "scheduled_for": 1776000000.0},
                "attempt": {
                    "state": "planned",
                    "contract_version": "1",
                    "authority": "supervisor",
                    "planned_reason": "maintenance window opens after countdown",
                },
                "runtime": {"active_slot": "A"},
            }
        raise AssertionError(path)

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_get", _supervisor_get)

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: planned" in result.output
    assert "supervisor attempt: planned" in result.output
    assert "attempt contract: v1" in result.output
    assert "attempt authority: supervisor" in result.output
    assert "planned reason: maintenance window opens after countdown" in result.output


def test_autostart_update_status_prints_planned_schedule_and_subsequent_transition(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {
                "state": "planned",
                "phase": "scheduled",
                "scheduled_for": 1776000000.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 1775999700.0,
            },
            "attempt": {"state": "planned"},
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "scheduled for:" in result.output
    assert "subsequent transition: queued" in result.output


def test_autostart_update_defer_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True, "planned": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["update-defer", "--delay-sec", "900", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/defer"
    assert captured["body"]["delay_sec"] == 900.0
    assert captured["body"]["reason"] == "cli.core_update.defer"


def test_autostart_update_status_prints_scheduled_and_subsequent_transition(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {
                "state": "planned",
                "phase": "scheduled",
                "scheduled_for": 1_775_966_400.0,
                "subsequent_transition": True,
            },
            "attempt": {
                "state": "planned",
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 1_775_966_100.0,
                "candidate_prewarm_state": "starting",
                "candidate_prewarm_message": "passive candidate runtime is still warming on http://127.0.0.1:8778",
            },
            "runtime": {
                "transition_mode": "warm_switch",
                "candidate_slot": "B",
                "candidate_runtime_state": "starting",
                "candidate_runtime_url": "http://127.0.0.1:8778",
            },
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "scheduled for:" in result.output
    assert "subsequent transition: queued" in result.output


def test_autostart_restart_calls_restart_service(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _restart() -> dict[str, object]:
        captured["called"] = True
        return {
            "ok": True,
            "scope": "system",
            "service": "adaos.service",
            "service_ref": "/etc/systemd/system/adaos.service",
        }

    monkeypatch.setattr(setup_cmd, "_restart_autostart_service", _restart)

    result = runner.invoke(autostart_app, ["restart"])

    assert result.exit_code == 0, result.output
    assert captured["called"] is True
    assert "[AdaOS] autostart restarted" in result.output
    assert "scope: system" in result.output
    assert "service: adaos.service" in result.output


def test_autostart_restart_json(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_restart_autostart_service",
        lambda: {
            "ok": True,
            "scope": "user",
            "service": "adaos.service",
        },
    )

    result = runner.invoke(autostart_app, ["restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "ok": True,
        "scope": "user",
        "service": "adaos.service",
    }


def test_autostart_update_defer_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True, "planned": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["update-defer", "--delay-sec", "900", "--reason", "test.defer", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/defer"
    assert captured["body"] == {"delay_sec": 900.0, "reason": "test.defer"}


def test_autostart_smoke_update_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+1.abc"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["smoke-update", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/start"
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+1.abc"
    assert captured["body"]["reason"] == "cli.smoke_update"


def test_autostart_update_start_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+2.def"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["update-start", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/start"
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+2.def"


def test_autostart_update_start_does_not_fallback_to_runtime_admin(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("local AdaOS supervisor API is unavailable at http://127.0.0.1:8776")),
    )

    def _unexpected_admin_post(*args, **kwargs):
        raise AssertionError("runtime admin fallback must not be used for autostart update mutations")

    monkeypatch.setattr(setup_cmd, "_autostart_admin_post", _unexpected_admin_post)

    result = runner.invoke(autostart_app, ["update-start"])

    assert result.exit_code == 1, result.output
    assert "http://127.0.0.1:8776" in result.output


def test_autostart_cli_token_reads_shared_dotenv_when_wrapper_has_no_token(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_cmd,
        "autostart_status",
        lambda ctx: {
            "shared_dotenv_path": "/root/adaos/.env",
            "wrapper_env": {"ADAOS_SUPERVISOR_PORT": "8776"},
        },
    )
    monkeypatch.setattr(
        setup_cmd,
        "_parse_env_file",
        lambda path: {"ADAOS_TOKEN": "dotenv-token"},
    )

    assert setup_cmd._autostart_service_token() == "dotenv-token"
    assert setup_cmd._autostart_cli_token() == "dotenv-token"


def test_autostart_update_promote_root_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_update_post", _post)

    result = runner.invoke(autostart_app, ["update-promote-root", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/promote-root"
    assert captured["body"]["reason"] == "cli.core_update.root_promotion"


def test_autostart_update_complete_promotes_root_and_restarts_service(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {
            "ok": True,
            "accepted": True,
            "restart_required": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "restart": {"ok": True, "requested": False, "mode": "manual"},
        }

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)
    monkeypatch.setattr(
        setup_cmd,
        "_restart_autostart_service",
        lambda: {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]},
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/complete"
    assert captured["body"]["reason"] == "cli.core_update.complete"
    assert '"service": "adaos.service"' in result.output


def test_autostart_update_complete_retries_restart_when_root_already_promoted(monkeypatch) -> None:
    runner = CliRunner()
    captured = {"restart_calls": 0}

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: {
            "ok": True,
            "accepted": True,
            "restart_required": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "attempt": {"state": "awaiting_root_restart"},
            "restart": {"ok": True, "requested": False, "mode": "manual"},
            "message": "root promotion already completed; retrying autostart service restart",
        },
    )

    def _restart():
        captured["restart_calls"] += 1
        return {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]}

    monkeypatch.setattr(setup_cmd, "_restart_autostart_service", _restart)

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["restart_calls"] == 1
    assert "already completed" in result.output


def test_autostart_update_complete_noops_when_root_promotion_not_required(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: {
            "ok": True,
            "noop": True,
            "restart_required": False,
            "status": {"state": "succeeded", "phase": "validate"},
            "message": "root promotion is not required for the current update state",
        },
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert '"noop": true' in result.output.lower()
    assert "root promotion is not required" in result.output


def test_autostart_update_complete_noops_when_runtime_flag_is_resolved_even_if_manifest_history_remains(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: {
            "ok": True,
            "noop": True,
            "restart_required": False,
            "status": {"state": "succeeded", "phase": "validate"},
            "message": "root promotion is not required for the current update state",
        },
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert '"noop": true' in result.output.lower()
    assert "root promotion is not required" in result.output


def test_autostart_update_complete_falls_back_to_legacy_flow_when_supervisor_endpoint_is_unavailable(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {"promote_paths": []}

    def _post(path, *, body=None, token=None):
        if path == "/api/supervisor/update/complete":
            raise RuntimeError("update-complete endpoint unavailable")
        captured["promote_paths"].append(path)
        captured["body"] = body
        return {
            "ok": True,
            "accepted": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
        }

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_update_get",
        lambda token=None: {
            "status": {"state": "validated", "phase": "root_promotion_pending"},
            "attempt": {"state": "active"},
            "runtime": {"root_promotion_required": True},
        },
    )
    monkeypatch.setattr(
        setup_cmd,
        "_restart_autostart_service",
        lambda: {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]},
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["_legacy"] is True
    assert captured["promote_paths"] == ["/api/supervisor/update/promote-root"]
    assert captured["body"] == {"reason": "cli.core_update.complete"}
    assert payload["restart"]["service"] == "adaos.service"


def test_autostart_update_complete_retries_restart_for_root_promoted_without_attempt_payload(monkeypatch) -> None:
    runner = CliRunner()
    captured = {"restart_calls": 0}
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "restart_required": True,
            "restart": {"ok": True, "requested": False, "mode": "manual"},
        },
    )

    def _restart():
        captured["restart_calls"] += 1
        return {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]}

    monkeypatch.setattr(setup_cmd, "_restart_autostart_service", _restart)

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["restart_calls"] == 1
    assert "autostart service restart requested" in result.output


def test_restart_autostart_service_delegates_to_shared_helper(monkeypatch) -> None:
    ctx = object()
    monkeypatch.setattr(setup_cmd, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        setup_cmd,
        "autostart_restart_service",
        lambda actual_ctx: {"ok": True, "ctx": actual_ctx},
    )

    payload = setup_cmd._restart_autostart_service()

    assert payload["ctx"] is ctx


def test_autostart_update_restore_root_restores_backup_and_optionally_restarts(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        setup_cmd,
        "restore_root_promotion_backup",
        lambda *, backup_dir, target_root=None: {
            "ok": True,
            "backup_dir": backup_dir,
            "target_root": target_root or "/root/adaos",
            "restart_required": True,
        },
    )
    monkeypatch.setattr(
        setup_cmd,
        "_restart_autostart_service",
        lambda: {"ok": True, "service": "adaos.service"},
    )

    result = runner.invoke(
        autostart_app,
        ["update-restore-root", "--backup-dir", "/tmp/root-backup", "--restart", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["restore"]["backup_dir"] == "/tmp/root-backup"
    assert payload["restart"]["ok"] is True


def test_autostart_update_status_reports_service_unavailable(monkeypatch) -> None:
    runner = CliRunner()

    def _boom(path, *, token=None):
        raise RuntimeError(
            "local AdaOS admin API is unavailable; the service may be restarting or failed to boot. "
            "Inspect 'journalctl --user -u adaos.service -n 120 --no-pager' and '.adaos/state/core_update/status.json'."
        )

    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _boom)

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code != 0
    assert "local AdaOS admin API is unavailable" in result.output


def test_autostart_update_status_falls_back_to_local_runner_state(monkeypatch) -> None:
    runner = CliRunner()

    def _boom(path, *, token=None):
        raise RuntimeError(
            "local AdaOS admin API is unavailable at http://127.0.0.1:8777; the service may be restarting or failed to boot. "
            "Inspect 'journalctl --user -u adaos.service -n 120 --no-pager' and '.adaos/state/core_update/status.json'."
        )

    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _boom)
    monkeypatch.setattr(
        setup_cmd,
        "_local_autostart_update_payload",
        lambda: {
            "ok": True,
            "status": {"state": "idle", "message": "autostart runner boot"},
            "slots": {
                "active_slot": "B",
                "previous_slot": "A",
                "slots": {
                    "A": {"manifest": {"target_version": "0.1.0", "git_short_commit": "54e4a96a", "git_branch": "rev2026"}},
                    "B": {"manifest": {"target_version": "0.1.1", "git_short_commit": "8e2f6e75", "git_branch": "rev2026"}},
                },
            },
            "active_manifest": {
                "target_version": "0.1.1",
                "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                "git_branch": "rev2026",
            },
            "memory": {
                "profile_control_mode": "phase2_supervisor_restart",
                "current_profile_mode": "normal",
                "requested_profile_mode": "sampled_profile",
                "suspicion_state": "idle",
                "sessions_total": 2,
                "last_session": {
                    "session_id": "mem-002",
                    "session_state": "requested",
                    "profile_mode": "sampled_profile",
                    "publish_state": "publish_requested",
                },
            },
            "_local_fallback": True,
        },
    )
    monkeypatch.setattr(setup_cmd, "_slot_build_version", lambda slot_id: "0.1.1+43.8e2f6e75" if slot_id == "B" else "")

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "message: autostart runner boot" in result.output
    assert "active build version: 0.1.1+43.8e2f6e75" in result.output
    assert "memory: mode=normal control=phase2_supervisor_restart suspicion=idle sessions=2 requested=sampled_profile" in result.output
    assert "active slot: B | 0.1.1+43.8e2f6e75 | 8e2f6e75 | rev2026" in result.output


def test_autostart_update_get_uses_bounded_status_timeouts_and_local_fallback(monkeypatch) -> None:
    calls: list[tuple[str, str, float | None]] = []

    def _supervisor_get(path, *, token=None, timeout=None):
        calls.append(("supervisor", path, timeout))
        raise RuntimeError("supervisor busy")

    def _admin_get(path, *, token=None, timeout=None):
        calls.append(("admin", path, timeout))
        raise RuntimeError("runtime busy")

    monkeypatch.setenv("ADAOS_AUTOSTART_UPDATE_STATUS_HTTP_TIMEOUT_S", "1.25")
    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_get", _supervisor_get)
    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _admin_get)
    monkeypatch.setattr(
        setup_cmd,
        "_local_autostart_update_payload",
        lambda: {
            "ok": True,
            "status": {"state": "preparing", "phase": "prepare"},
            "attempt": {"state": "active"},
            "_local_fallback": True,
        },
    )

    payload = setup_cmd._autostart_update_get(token="token")

    assert payload["_local_fallback"] is True
    assert payload["status"]["state"] == "preparing"
    assert calls == [
        ("supervisor", "/api/supervisor/update/status", 1.25),
        ("supervisor", "/api/supervisor/public/update-status", 1.25),
        ("admin", "/api/admin/update/status", 1.25),
    ]


def test_autostart_inspect_renders_hot_children_and_services(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_collect_autostart_inspect",
        lambda sample_sec=0.2, token=None: {
            "autostart": {
                "enabled": True,
                "active": True,
                "listening": True,
                "url": "http://127.0.0.1:8777",
            },
            "bind": {"host": "127.0.0.1", "port": 8777},
            "process": {
                "pid": 3210,
                "root": {
                    "pid": 3210,
                    "kind": "autostart_runner",
                    "status": "running",
                    "cpu_percent": 12.5,
                    "rss_bytes": 64 * 1024 * 1024,
                    "threads": 17,
                    "age_sec": 93,
                    "cmdline_text": "python -m adaos.apps.autostart_runner --host 127.0.0.1 --port 8777",
                },
                "top_children": [
                    {
                        "pid": 4001,
                        "kind": "skill_runtime",
                        "cpu_percent": 97.2,
                        "rss_bytes": 128 * 1024 * 1024,
                        "threads": 9,
                        "age_sec": 40,
                        "cmdline_text": "python skills/runtime_runner.py weather",
                    }
                ],
            },
            "services": [
                {
                    "name": "weather",
                    "running": True,
                    "pid": 4001,
                    "base_url": "http://127.0.0.1:9123",
                    "health_ok": True,
                }
            ],
        },
    )

    result = runner.invoke(autostart_app, ["inspect"])

    assert result.exit_code == 0, result.output
    assert "autostart: enabled=True active=True listening=True" in result.output
    assert "runtime: pid=3210 kind=autostart_runner status=running cpu=12.5%" in result.output
    assert "pid=4001 kind=skill_runtime cpu=97.2%" in result.output
    assert "weather: running pid=4001 http://127.0.0.1:9123 health=ok" in result.output


def test_autostart_inspect_renders_service_and_supervisor_sections(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_collect_autostart_inspect",
        lambda sample_sec=0.2, token=None: {
            "autostart": {
                "enabled": True,
                "active": True,
                "listening": False,
                "url": "http://127.0.0.1:8777",
            },
            "bind": {"host": "127.0.0.1", "port": 8777},
            "service_process": {
                "pid": 11939,
                "root": {
                    "pid": 11939,
                    "kind": "supervisor",
                    "status": "sleeping",
                    "cpu_percent": 0.0,
                    "rss_bytes": 32 * 1024 * 1024,
                    "threads": 4,
                    "age_sec": 20,
                    "cmdline_text": "python -m adaos.apps.supervisor --host 127.0.0.1 --port 8777",
                },
            },
            "supervisor": {
                "url": "http://127.0.0.1:8776",
                "reachable": True,
                "status": {
                    "active_slot": "B",
                    "runtime_state": "ready",
                    "runtime_api_ready": True,
                    "managed_start_reason": "supervisor.monitor.respawn_after_exit",
                    "last_stop_reason": "test.restart",
                    "candidate_slot": "A",
                    "candidate_runtime_state": "warming",
                    "candidate_runtime_api_ready": False,
                    "candidate_start_reason": "supervisor.candidate.prewarm",
                },
                "process": {
                    "pid": 11939,
                    "kind": "supervisor",
                    "status": "sleeping",
                    "cpu_percent": 0.0,
                    "rss_bytes": 32 * 1024 * 1024,
                    "threads": 4,
                    "age_sec": 20,
                    "cmdline_text": "python -m adaos.apps.supervisor --host 127.0.0.1 --port 8777",
                },
            },
            "runtime_process": {
                "pid": 11941,
                "root": {
                    "pid": 11941,
                    "kind": "autostart_runner",
                    "status": "running",
                    "cpu_percent": 18.5,
                    "rss_bytes": 64 * 1024 * 1024,
                    "threads": 7,
                    "age_sec": 11,
                    "cmdline_text": "python -m adaos.apps.autostart_runner --host 127.0.0.1 --port 8777",
                },
                "top_children": [],
            },
            "services": [],
        },
    )

    result = runner.invoke(autostart_app, ["inspect"])

    assert result.exit_code == 0, result.output
    assert "service: pid=11939 kind=supervisor" in result.output
    assert "supervisor: url=http://127.0.0.1:8776 reachable=True" in result.output
    assert (
        "supervisor runtime: slot=B state=ready api_ready=True "
        "start_reason=supervisor.monitor.respawn_after_exit last_stop_reason=test.restart"
    ) in result.output
    assert (
        "supervisor candidate: slot=A state=warming api_ready=False start_reason=supervisor.candidate.prewarm"
    ) in result.output
    assert "runtime: pid=11941 kind=autostart_runner" in result.output


def test_autostart_inspect_surfaces_recent_candidate_stop_reason_without_active_candidate(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_collect_autostart_inspect",
        lambda sample_sec=0.2, token=None: {
            "autostart": {
                "enabled": True,
                "active": True,
                "listening": True,
                "url": "http://127.0.0.1:8778",
            },
            "bind": {"host": "127.0.0.1", "port": 8778},
            "supervisor": {
                "url": "http://127.0.0.1:8776",
                "reachable": True,
                "status": {
                    "active_slot": "B",
                    "runtime_state": "ready",
                    "runtime_api_ready": True,
                    "candidate_last_stop_reason": "supervisor.candidate.exited",
                },
            },
            "services": [],
        },
    )

    result = runner.invoke(autostart_app, ["inspect"])

    assert result.exit_code == 0, result.output
    assert (
        "supervisor candidate: slot=- state=- api_ready=False last_stop_reason=supervisor.candidate.exited"
    ) in result.output


def test_probe_http_json_uses_default_autostart_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"ok": True}

    def _get(url: str, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _Response()

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_headers",
        lambda token=None, base_url=None: {"X-AdaOS-Token": "dev-local-token"},
    )
    monkeypatch.setattr(setup_cmd.requests, "get", _get)

    payload = setup_cmd._probe_http_json("http://127.0.0.1:8776", "/api/supervisor/status")

    assert payload == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:8776/api/supervisor/status"
    assert captured["headers"]["X-AdaOS-Token"] == "dev-local-token"
    assert captured["headers"]["Accept"] == "application/json"


def test_probe_http_result_reports_http_error_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        ok = False
        status_code = 401
        text = '{"detail":"Invalid or missing X-AdaOS-Token"}'

        @staticmethod
        def json() -> dict:
            return {"detail": "Invalid or missing X-AdaOS-Token"}

    def _get(url: str, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _Response()

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_headers",
        lambda token=None, base_url=None: {"X-AdaOS-Token": "dev-local-token"},
    )
    monkeypatch.setattr(setup_cmd.requests, "get", _get)

    payload = setup_cmd._probe_http_result("http://127.0.0.1:8776", "/api/supervisor/status")

    assert payload["ok"] is False
    assert payload["status_code"] == 401
    assert payload["json"] == {"detail": "Invalid or missing X-AdaOS-Token"}
    assert captured["url"] == "http://127.0.0.1:8776/api/supervisor/status"
    assert captured["headers"]["X-AdaOS-Token"] == "dev-local-token"
    assert captured["headers"]["Accept"] == "application/json"


def test_autostart_admin_headers_prefer_wrapper_service_token_over_local_cli_token(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_TOKEN", "shell-token")
    monkeypatch.setattr(
        setup_cmd,
        "get_ctx",
        lambda: types.SimpleNamespace(config=types.SimpleNamespace(token="stale-config-token")),
    )
    monkeypatch.setattr(
        setup_cmd,
        "autostart_status",
        lambda ctx: {"wrapper_env": {"ADAOS_TOKEN": "wrapper-service-token"}},
    )

    headers = setup_cmd._autostart_admin_headers()

    assert headers["X-AdaOS-Token"] == "wrapper-service-token"


def test_autostart_inspect_json_outputs_payload(monkeypatch) -> None:
    runner = CliRunner()
    payload = {
        "autostart": {"enabled": True, "active": True, "listening": True},
        "bind": {"host": "127.0.0.1", "port": 8777},
        "process": None,
        "services": [],
    }
    monkeypatch.setattr(setup_cmd, "_collect_autostart_inspect", lambda sample_sec=0.2, token=None: payload)

    result = runner.invoke(autostart_app, ["inspect", "--json"])

    assert result.exit_code == 0, result.output
    assert '"host": "127.0.0.1"' in result.output
    assert '"port": 8777' in result.output


def test_select_autostart_target_pid_prefers_pidfile_candidate(monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "_pidfile_path", lambda host, port: object())
    monkeypatch.setattr(setup_cmd, "_read_pidfile", lambda path: {"pid": 2222})
    monkeypatch.setattr(setup_cmd, "_find_listening_server_pid", lambda host, port: 3333)
    monkeypatch.setattr(setup_cmd, "_find_matching_server_pids", lambda host, port, protected_pids=None: [4444])
    monkeypatch.setattr(setup_cmd, "_current_process_family_pids", lambda: {9999})

    class _FakeProc:
        def __init__(self, pid: int):
            self.pid = pid

        def status(self):
            return "running"

    monkeypatch.setattr(setup_cmd.psutil, "Process", _FakeProc)

    pid = setup_cmd._select_autostart_target_pid({}, "127.0.0.1", 8777)

    assert pid == 2222
