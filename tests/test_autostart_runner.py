from __future__ import annotations

import json
import signal
import types
from pathlib import Path

from adaos.apps import autostart_runner
from adaos.services import runtime_memory_profile
from adaos.services.supervisor_memory import read_memory_session_summary, supervisor_memory_session_artifacts_dir


def test_autostart_runner_initializes_context_before_pidfile(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(autostart_runner, "_parse_args", lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})())
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: calls.append("init_ctx"))
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append("write_status"))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: calls.append("stop_previous"))

    def _pidfile(host, port):
        calls.append("pidfile")
        raise SystemExit(0)

    monkeypatch.setattr(autostart_runner, "_pidfile_path", _pidfile)

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert calls[:3] == ["init_ctx", "write_status", "stop_previous"]
    assert "pidfile" in calls


def test_autostart_runner_reconciles_root_promotion_restart_before_idle(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(autostart_runner, "_parse_args", lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})())
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {"state": "succeeded", "phase": "root_promoted", "message": "restart adaos.service to activate"},
    )
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert captured
    assert captured[0]["state"] == "succeeded"
    assert captured[0]["phase"] == "root_promoted"
    assert "awaiting runtime boot validation" in captured[0]["message"]


def test_reconcile_post_root_promotion_restart_clears_stale_candidate_prewarm_fields() -> None:
    payload = autostart_runner._reconcile_post_root_promotion_restart(
        {
            "state": "succeeded",
            "phase": "root_promoted",
            "candidate_prewarm_state": "starting",
            "candidate_prewarm_message": "passive candidate runtime is still warming on http://127.0.0.1:8778",
            "candidate_prewarm_ready_at": 123.0,
        }
    )

    assert isinstance(payload, dict)
    assert payload["phase"] == "root_promoted"
    assert payload["candidate_prewarm_state"] is None
    assert payload["candidate_prewarm_message"] is None
    assert payload["candidate_prewarm_ready_at"] is None


def test_runtime_memory_profile_session_writes_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_PROFILE_MODE", "sampled_profile")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PROFILE_SESSION_ID", "mem-123")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PROFILE_TRIGGER", "policy:growth")

    class _Stat:
        def __init__(self, label: str, *, size: int = 0, count: int = 0, size_diff: int = 0, count_diff: int = 0) -> None:
            self.traceback = label
            self.size = size
            self.count = count
            self.size_diff = size_diff
            self.count_diff = count_diff

    class _Snapshot:
        def __init__(self, start: bool) -> None:
            self._start = start

        def statistics(self, key: str):
            assert key == "lineno"
            return [_Stat("app.py:10", size=256 if self._start else 384, count=2)]

        def compare_to(self, other, key: str):
            assert key == "lineno"
            return [_Stat("app.py:10", size=384, count=3, size_diff=128, count_diff=1)]

    snapshots = iter([_Snapshot(True), _Snapshot(False)])
    monkeypatch.setattr(autostart_runner.tracemalloc, "start", lambda frames: None)
    monkeypatch.setattr(autostart_runner.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(autostart_runner.tracemalloc, "take_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(autostart_runner.tracemalloc, "stop", lambda: None)
    monkeypatch.setattr(autostart_runner.time, "time", lambda: 100.0)

    session = autostart_runner._RuntimeMemoryProfileSession()
    session.start()
    session.finish()

    artifacts_dir = supervisor_memory_session_artifacts_dir("mem-123")
    summary = read_memory_session_summary("mem-123")

    assert (artifacts_dir / "tracemalloc-start.json").exists()
    assert (artifacts_dir / "tracemalloc-final.json").exists()
    assert (artifacts_dir / "tracemalloc-top-growth.json").exists()
    assert summary is not None
    assert summary["session_state"] == "finished"
    assert len(summary["artifact_refs"]) == 3
    growth_payload = json.loads((artifacts_dir / "tracemalloc-top-growth.json").read_text(encoding="utf-8"))
    assert growth_payload["top_growth_sites"][0]["size_diff_bytes"] == 128


def test_runtime_trace_profile_session_writes_trace_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_PROFILE_MODE", "trace_profile")
    monkeypatch.setenv("ADAOS_SUPERVISOR_PROFILE_SESSION_ID", "mem-trace")

    class _Stat:
        def __init__(self, label: str, *, size: int = 0, count: int = 0, size_diff: int = 0, count_diff: int = 0) -> None:
            self.traceback = [label, "worker.py:20"]
            self.size = size
            self.count = count
            self.size_diff = size_diff
            self.count_diff = count_diff

    class _Snapshot:
        def __init__(self, start: bool) -> None:
            self._start = start

        def statistics(self, key: str):
            assert key in {"lineno", "traceback"}
            return [_Stat("app.py:10", size=256 if self._start else 512, count=2, size_diff=256, count_diff=1)]

        def compare_to(self, other, key: str):
            assert key == "lineno"
            return [_Stat("app.py:10", size=512, count=3, size_diff=256, count_diff=1)]

    snapshots = iter([_Snapshot(True), _Snapshot(False)])
    monkeypatch.setattr(autostart_runner.tracemalloc, "start", lambda frames: None)
    monkeypatch.setattr(autostart_runner.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(autostart_runner.tracemalloc, "take_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(autostart_runner.tracemalloc, "stop", lambda: None)
    monkeypatch.setattr(autostart_runner.time, "time", lambda: 100.0)

    session = autostart_runner._RuntimeMemoryProfileSession()
    session.start()
    session.finish()

    artifacts_dir = supervisor_memory_session_artifacts_dir("mem-trace")
    summary = read_memory_session_summary("mem-trace")

    assert (artifacts_dir / "tracemalloc-trace-start.json").exists()
    assert (artifacts_dir / "tracemalloc-trace-final.json").exists()
    assert summary is not None
    assert len(summary["artifact_refs"]) == 5
    trace_payload = json.loads((artifacts_dir / "tracemalloc-trace-final.json").read_text(encoding="utf-8"))
    assert trace_payload["trace_frames"] == 25
    assert trace_payload["top_tracebacks"][0]["traceback"][0] == "app.py:10"


def test_runtime_profile_signal_handler_finishes_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    installed: dict[int, object] = {}
    restored: dict[int, object] = {}

    monkeypatch.setattr(autostart_runner.signal, "getsignal", lambda sig: f"previous-{sig}")
    monkeypatch.setattr(
        autostart_runner.signal,
        "signal",
        lambda sig, handler: installed.setdefault(int(sig), handler) if callable(handler) else restored.setdefault(int(sig), handler),
    )

    calls: list[str] = []

    class _Session:
        def finish(self) -> None:
            calls.append("finish")

    runtime_memory_profile.register_active_runtime_memory_profile(_Session())  # type: ignore[arg-type]
    restore = autostart_runner._install_runtime_profile_signal_handlers(_Session())  # type: ignore[arg-type]

    assert int(signal.SIGTERM) in installed
    handler = installed[int(signal.SIGTERM)]
    try:
        handler(signal.SIGTERM, None)  # type: ignore[misc]
    except SystemExit as exc:
        assert exc.code == 128 + int(signal.SIGTERM)

    assert calls == ["finish"]
    restore()
    runtime_memory_profile.register_active_runtime_memory_profile(None)
    assert restored[int(signal.SIGTERM)] == f"previous-{int(signal.SIGTERM)}"


def test_finish_active_runtime_memory_profile_reports_missing_session() -> None:
    runtime_memory_profile.register_active_runtime_memory_profile(None)

    result = runtime_memory_profile.finish_active_runtime_memory_profile()

    assert result == {"ok": False, "found": False, "reason": "no_active_session"}


def test_finish_active_runtime_memory_profile_reports_finish_error() -> None:
    class _Session:
        session_id = "mem-oops"
        profile_mode = "sampled_profile"
        started = True
        _finished = False

        def finish(self) -> None:
            raise RuntimeError("boom")

    runtime_memory_profile.register_active_runtime_memory_profile(_Session())  # type: ignore[arg-type]
    try:
        result = runtime_memory_profile.finish_active_runtime_memory_profile()
    finally:
        runtime_memory_profile.register_active_runtime_memory_profile(None)

    assert result["ok"] is False
    assert result["found"] is True
    assert result["session_id"] == "mem-oops"
    assert result["profile_mode"] == "sampled_profile"
    assert result["error_type"] == "RuntimeError"
    assert result["error"] == "boom"
    assert "RuntimeError: boom" in result["traceback"]


def test_finish_active_runtime_memory_profile_writes_debug_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    class _Session:
        session_id = "mem-debug"
        profile_mode = "sampled_profile"
        started = True
        _finished = False

        def finish(self) -> None:
            self._finished = True

    runtime_memory_profile.register_active_runtime_memory_profile(_Session())  # type: ignore[arg-type]
    try:
        result = runtime_memory_profile.finish_active_runtime_memory_profile()
    finally:
        runtime_memory_profile.register_active_runtime_memory_profile(None)

    artifacts_dir = supervisor_memory_session_artifacts_dir("mem-debug")
    summary = read_memory_session_summary("mem-debug")

    assert result["ok"] is True
    assert (artifacts_dir / "runtime-profile-finalize-debug.json").exists()
    assert summary is not None
    assert any(item.get("kind") == "runtime_profile_finalize_debug" for item in summary.get("artifact_refs", []))


def test_launch_active_slot_validates_required_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def __init__(self) -> None:
            self.wait_called = False

        def wait(self, timeout=None):
            self.wait_called = True
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    proc = _Proc()
    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, "ok"))
    monkeypatch.setattr(
        autostart_runner,
        "_run_post_commit_skill_checks",
        lambda: {
            "ok": False,
            "failed_total": 1,
            "deactivated_total": 1,
            "skills": [
                {
                    "skill": "voice_skill",
                    "ok": False,
                    "failure_kind": "lifecycle",
                    "failed_stage": "rehydrate",
                    "deactivated": True,
                    "deactivation": {
                        "committed_core_switch": True,
                        "failure_kind": "lifecycle",
                        "failed_stage": "rehydrate",
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )
    captured: list[dict] = []
    clear_calls: list[str] = []
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: clear_calls.append("clear"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert proc.wait_called is True
    assert clear_calls == ["clear"]
    assert captured[-1]["state"] == "succeeded"
    assert captured[-1]["phase"] == "validate"
    assert captured[-1]["skill_post_commit_checks"]["deactivated_total"] == 1
    assert "skills degraded after commit" in captured[-1]["message"]
    assert "quarantine=voice_skill:lifecycle/rehydrate" in captured[-1]["message"]


def test_launch_active_slot_rolls_back_on_failed_validation(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    proc = _Proc()
    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (False, "http://127.0.0.1:8777/api/admin/update/status returned 500"))
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(autostart_runner, "rollback_to_previous_slot", lambda: "A")
    monkeypatch.setattr(
        autostart_runner,
        "rollback_installed_skill_runtimes",
        lambda: {"ok": True, "total": 1, "failed_total": 0, "rollback_total": 1, "skills": [{"skill": "weather_skill", "ok": True}]},
    )
    captured: list[dict] = []
    clear_calls: list[str] = []
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: clear_calls.append("clear"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit")

    assert proc.terminated is True
    assert clear_calls == ["clear"]
    assert captured[-1]["phase"] == "validate"
    assert captured[-1]["restored_slot"] == "A"
    assert captured[-1]["rollback"]["ok"] is True
    assert captured[-1]["skill_runtime_rollback"]["rollback_total"] == 1


def test_autostart_runner_preserves_plan_during_successful_apply_until_validation(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: {"target_rev": "rev2026", "target_slot": "B"})
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "execute_pending_update",
        lambda plan: {
            "state": "succeeded",
            "returncode": 0,
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "started_at": 10.0,
        },
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", dict(payload))))
    monkeypatch.setattr(autostart_runner, "finalize_runtime_boot_status", lambda: (_ for _ in ()).throw(AssertionError("should skip boot finalization after apply")))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: calls.append("stop_previous"))
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: calls.append(("launch_active_slot", dict(kwargs))) or (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert "clear_plan" not in calls
    status_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "write_status"]
    assert status_calls
    payload = status_calls[-1][1]
    assert payload["state"] == "restarting"
    assert payload["phase"] == "launch"
    assert payload["target_slot"] == "B"
    launch_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "launch_active_slot"]
    assert launch_calls
    assert launch_calls[-1][1]["validate"] is True


def test_autostart_runner_does_not_reapply_update_when_transition_already_reached_launch(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_plan",
        lambda: {"action": "update", "target_rev": "rev2026", "target_slot": "B"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {"state": "restarting", "phase": "launch", "target_slot": "B"},
    )
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "execute_pending_update",
        lambda plan: (_ for _ in ()).throw(AssertionError("should not re-run apply after launch handoff")),
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", dict(payload))))
    monkeypatch.setattr(autostart_runner, "finalize_runtime_boot_status", lambda: (_ for _ in ()).throw(AssertionError("should skip boot finalization while resuming launch")))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: calls.append("stop_previous"))
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: calls.append(("launch_active_slot", dict(kwargs))) or (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert "clear_plan" not in calls
    status_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "write_status"]
    assert not status_calls
    launch_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "launch_active_slot"]
    assert launch_calls
    assert launch_calls[-1][1]["validate"] is True


def test_autostart_runner_prepared_restart_preserves_plan_until_validation(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_plan",
        lambda: {
            "state": "prepared_restart",
            "action": "update",
            "target_slot": "B",
            "prepared_at": 10.0,
        },
    )
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "env": {}, "cwd": str(tmp_path), "skill_runtime_migration": {"deferred": True}},
    )
    monkeypatch.setattr(
        autostart_runner,
        "_run_prepared_restart_skill_migration",
        lambda slot, manifest: (
            {"ok": True, "total": 1, "failed_total": 0, "rollback_total": 0, "deferred": False, "skills": []},
            {**dict(manifest), "skill_runtime_migration": {"ok": True, "deferred": False}},
        ),
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", dict(payload))))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert "clear_plan" not in calls
    status_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "write_status"]
    assert status_calls
    payload = status_calls[0][1]
    assert payload["state"] == "restarting"
    assert payload["phase"] == "launch"
    assert payload["target_slot"] == "B"
    assert payload["skill_runtime_migration"]["ok"] is True


def test_autostart_runner_prepared_restart_defers_skill_migration_under_memory_pressure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    manifests: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_plan",
        lambda: {
            "state": "prepared_restart",
            "action": "update",
            "target_slot": "B",
            "prepared_at": 10.0,
        },
    )
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {
            "state": "restarting",
            "phase": "shutdown",
            "target_slot": "B",
            "candidate_prewarm_state": "skipped",
            "candidate_prewarm_message": "insufficient memory for warm switch; using stop-and-switch",
        },
    )
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "env": {},
            "cwd": str(tmp_path),
            "skill_runtime_migration": {"deferred": True},
        },
    )
    monkeypatch.setattr(
        autostart_runner,
        "_run_prepared_restart_skill_migration",
        lambda slot, manifest: (_ for _ in ()).throw(
            AssertionError("migration should be deferred under memory pressure")
        ),
    )
    monkeypatch.setattr(
        autostart_runner,
        "write_slot_manifest",
        lambda slot, manifest: manifests.append((slot, dict(manifest))),
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(
        autostart_runner,
        "write_status",
        lambda payload: calls.append(("write_status", dict(payload))),
    )
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(
        autostart_runner,
        "_advertise_base",
        lambda host, port: f"http://{host}:{port}",
    )
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(
        autostart_runner,
        "_write_pidfile",
        lambda path, **kwargs: path.write_text("{}", encoding="utf-8"),
    )
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert "clear_plan" not in calls
    assert manifests
    assert manifests[0][0] == "B"
    status_calls = [
        item for item in calls if isinstance(item, tuple) and item[0] == "write_status"
    ]
    assert status_calls
    payload = status_calls[0][1]
    migration = payload["skill_runtime_migration"]
    assert migration["ok"] is True
    assert migration["deferred"] is True
    assert migration["defer_reason"] == "pressure_stop_and_switch"
    assert migration["safe_for_core_update"] is True
    assert manifests[0][1]["skill_runtime_migration"]["defer_reason"] == (
        "pressure_stop_and_switch"
    )


def test_launch_active_slot_marks_child_to_skip_pending_update(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    captured_env: dict[str, str] = {}

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    def _popen(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _Proc()

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", _popen)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: payload)

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert captured_env[autostart_runner._SKIP_PENDING_UPDATE_ENV] == "1"


def test_launch_active_slot_marks_root_promotion_pending_when_manifest_requires_it(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "env": {},
            "cwd": "",
            "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
        },
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    captured: list[dict] = []
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert captured[-1]["state"] == "validated"
    assert captured[-1]["phase"] == "root_promotion_pending"
    assert captured[-1]["root_promotion_required"] is True


def test_launch_active_slot_runs_post_commit_webspace_refresh(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    refresh_calls: list[dict[str, object]] = []
    captured: list[dict] = []

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: refresh_calls.append(dict(kwargs)) or {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert refresh_calls == [
        {
            "webspace_id": "default",
            "action": "core_update_post_boot_sync",
            "source_of_truth": "scenario_projection",
        }
    ]
    assert captured[-1]["post_commit_webspace_refresh"]["ok"] is True


def test_run_slot_cli_smoke_check_uses_slot_python_and_repo_cwd(monkeypatch, tmp_path: Path) -> None:
    slot_root = tmp_path / "slotB"
    python_bin = slot_root / "venv" / "bin" / "python"
    repo_dir = slot_root / "repo"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("", encoding="utf-8")

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = "cli_import_ok\n"
        stderr = ""

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _Completed()

    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: slot_root)
    monkeypatch.setattr(autostart_runner.subprocess, "run", _run)

    result = autostart_runner._run_slot_cli_smoke_check("B", manifest={"cwd": ""}, env={"ADAOS_ACTIVE_CORE_SLOT": "B"})

    assert result["ok"] is True
    assert captured["cmd"] == [str(python_bin), "-c", "from adaos.apps.cli.app import app as _app; print('cli_import_ok')"]
    assert captured["cwd"] == str(repo_dir.resolve())


def test_launch_active_slot_fails_when_cli_smoke_check_fails(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def __init__(self) -> None:
            self.terminated = False

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            return None

    proc = _Proc()
    captured: list[dict] = []

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing module")),
    )
    monkeypatch.setattr(autostart_runner, "rollback_to_previous_slot", lambda: "A")
    monkeypatch.setattr(autostart_runner, "rollback_installed_skill_runtimes", lambda: {"ok": True, "rollback_total": 0, "failed_total": 0, "skills": []})
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit")

    assert proc.terminated is True
    assert captured[-1]["phase"] == "validate"
    assert "CLI smoke check failed" in str(captured[-1]["validation_error_summary"] or "")


def test_launch_active_slot_clears_plan_only_after_validation_status_is_written(monkeypatch) -> None:
    events: list[object] = []

    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "env": {},
            "cwd": "",
            "bootstrap_update": {"required": False, "changed_paths": []},
        },
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(
        autostart_runner,
        "_run_slot_cli_smoke_check",
        lambda *args, **kwargs: {"ok": True, "slot": "B", "stdout": "cli_import_ok"},
    )
    monkeypatch.setattr(
        autostart_runner,
        "rebuild_webspace_projection_sync",
        lambda **kwargs: {"ok": True, "webspace_id": kwargs.get("webspace_id")},
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: events.append("clear_plan"))
    monkeypatch.setattr(
        autostart_runner,
        "write_status",
        lambda payload: events.append(("write_status", str(payload.get("state") or ""), str(payload.get("phase") or ""))),
    )

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert events[-2] == ("write_status", "succeeded", "validate")
    assert events[-1] == "clear_plan"


def test_launch_active_slot_respects_process_slot_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setattr(autostart_runner, "active_slot_manifest", lambda: (_ for _ in ()).throw(AssertionError("should not read manifest")))
    monkeypatch.setattr(autostart_runner.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not launch subprocess")))

    args = types.SimpleNamespace(token="dev-local-token")

    assert autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8778, validate=False) is None


def test_autostart_runner_skips_pending_update_when_requested(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: calls.append("read_plan"))
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "execute_pending_update", lambda plan: calls.append(("execute", plan)))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", payload.get("state"))))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setenv(autostart_runner._SKIP_PENDING_UPDATE_ENV, "1")

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert "read_plan" not in calls
    assert not any(isinstance(item, tuple) and item[0] == "execute" for item in calls)


def test_autostart_runner_preserves_root_promotion_pending_status_on_boot(monkeypatch, tmp_path: Path) -> None:
    writes: list[dict] = []
    finalized_calls: list[bool] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {"state": "restarting", "phase": "launch", "target_slot": "B"},
    )
    monkeypatch.setattr(autostart_runner, "_reconcile_post_root_promotion_restart", lambda current: None)
    monkeypatch.setattr(
        autostart_runner,
        "finalize_runtime_boot_status",
        lambda: finalized_calls.append(True) or {
            "state": "validated",
            "phase": "root_promotion_pending",
            "target_slot": "B",
            "root_promotion_required": True,
        },
    )
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: writes.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert finalized_calls == [True]
    assert not any(str(item.get("state") or "").strip().lower() == "idle" for item in writes)


def test_autostart_runner_marks_interrupted_restarting_without_plan_failed(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {"state": "restarting", "phase": "shutdown", "target_slot": "B"},
    )
    monkeypatch.setattr(autostart_runner, "_reconcile_post_root_promotion_restart", lambda current: None)
    monkeypatch.setattr(autostart_runner, "finalize_runtime_boot_status", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert captured[-1]["state"] == "failed"
    assert captured[-1]["phase"] == "shutdown"
    assert captured[-1]["interrupted_transition_state"] == "restarting"
    assert "interrupted before validation commit" in str(captured[-1]["message"] or "")


def test_autostart_runner_writes_failed_status_on_boot_exception(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    try:
        autostart_runner.main()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert captured[-1]["state"] == "failed"
    assert captured[-1]["phase"] == "launch_active_slot"
    assert captured[-1]["error_type"] == "RuntimeError"
    assert "boom" in str(captured[-1]["error"])


def test_validate_sidecar_runtime_payload_requires_listener_when_enabled() -> None:
    ok, error, details = autostart_runner._validate_sidecar_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "enabled": True,
                "status": "unknown",
                "local_listener_state": "down",
            },
            "process": {
                "listener_running": False,
            },
        }
    )

    assert ok is False
    assert "listener" in str(error).lower()
    assert details["enabled"] is True
    assert details["listener_running"] is False


def test_validate_sidecar_runtime_payload_requires_transport_when_enabled() -> None:
    ok, error, details = autostart_runner._validate_sidecar_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "enabled": True,
                "status": "degraded",
                "local_listener_state": "ready",
                "transport_ready": False,
                "control_ready": "unknown",
            },
            "process": {
                "listener_running": True,
            },
        }
    )

    assert ok is False
    assert "hub-root transport is not ready" in str(error)
    assert details["listener_running"] is True
    assert details["transport_ready"] is False


def test_validate_sidecar_runtime_payload_accepts_ready_transport_when_enabled() -> None:
    ok, error, details = autostart_runner._validate_sidecar_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "enabled": True,
                "status": "ready",
                "local_listener_state": "ready",
                "transport_ready": True,
                "control_ready": "ready",
            },
            "process": {
                "listener_running": True,
            },
        }
    )

    assert ok is True
    assert error is None
    assert details["listener_running"] is True
    assert details["transport_ready"] is True


def test_validate_yjs_runtime_payload_requires_server_ready() -> None:
    ok, error, details = autostart_runner._validate_yjs_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "available": True,
                "selected_webspace_id": "default",
                "assessment": {
                    "state": "degraded",
                    "reason": "yjs_websocket_server_not_ready",
                },
                "transport": {
                    "server_requested": True,
                    "server_task_running": False,
                    "server_ready": False,
                    "server_error": "RuntimeError: bind failed",
                },
            },
        },
        expected_webspace_id="default",
    )

    assert ok is False
    assert "bind failed" in str(error)
    assert details["server_ready"] is False
    assert details["selected_webspace_id"] == "default"


def test_probe_update_runtime_fails_when_runtime_guard_fails(monkeypatch) -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return dict(self._payload)

    def _fake_get(url: str, headers=None, timeout=None):
        if url.endswith("/api/ping"):
            return _Response(200, {"ok": True})
        if url.endswith("/api/node/sidecar/status"):
            return _Response(200, {"ok": True, "runtime": {"enabled": False}, "process": {}})
        if "/api/node/yjs/webspaces/default/runtime" in url:
            return _Response(
                200,
                {
                    "ok": True,
                    "runtime": {
                        "available": True,
                        "selected_webspace_id": "default",
                        "assessment": {
                            "state": "degraded",
                            "reason": "yjs_websocket_server_not_ready",
                        },
                        "transport": {
                            "server_requested": True,
                            "server_task_running": False,
                            "server_ready": False,
                            "server_error": "RuntimeError: y server task crashed",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected url {url}")

    time_values = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(autostart_runner.requests, "get", _fake_get)
    monkeypatch.setattr(autostart_runner.time, "time", lambda: next(time_values))
    monkeypatch.setattr(autostart_runner.time, "sleep", lambda _: None)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT", raising=False)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME", raising=False)

    ok, details = autostart_runner._probe_update_runtime(
        host="127.0.0.1",
        port=8777,
        token="dev-local-token",
        timeout_sec=0.5,
        expected_slot=None,
    )

    assert ok is False
    assert details["runtime_guards"] is True
    assert details["timeout_sec"] == 1.0
    assert "y server task crashed" in str(details["summary"])


def test_update_validation_timeout_sec_defaults_to_45_seconds(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_TIMEOUT_SEC", raising=False)

    assert autostart_runner._update_validation_timeout_sec() == 45.0


def test_probe_update_runtime_succeeds_after_initial_ping_failures(monkeypatch) -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return dict(self._payload)

    attempts = {"ping": 0}

    def _fake_get(url: str, headers=None, timeout=None):
        if url.endswith("/api/ping"):
            attempts["ping"] += 1
            if attempts["ping"] < 3:
                raise autostart_runner.requests.ConnectionError("connection refused")
            return _Response(200, {"ok": True})
        if url.endswith("/api/status"):
            return _Response(200, {"ok": True})
        if url.endswith("/api/admin/update/status"):
            return _Response(
                200,
                {
                    "ok": True,
                    "slots": {"active_slot": "B"},
                    "active_manifest": {"slot": "B"},
                },
            )
        if url.endswith("/api/node/sidecar/status"):
            return _Response(200, {"ok": True, "runtime": {"enabled": False}, "process": {}})
        if "/api/node/yjs/webspaces/default/runtime" in url:
            return _Response(
                200,
                {
                    "ok": True,
                    "runtime": {
                        "available": True,
                        "selected_webspace_id": "default",
                        "assessment": {"state": "ready"},
                        "transport": {"server_ready": True},
                    },
                },
            )
        raise AssertionError(f"unexpected url {url}")

    class _FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def time(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += float(seconds)

    clock = _FakeClock()
    monkeypatch.setattr(autostart_runner.requests, "get", _fake_get)
    monkeypatch.setattr(autostart_runner.time, "time", clock.time)
    monkeypatch.setattr(autostart_runner.time, "sleep", clock.sleep)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT", raising=False)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME", raising=False)

    ok, details = autostart_runner._probe_update_runtime(
        host="127.0.0.1",
        port=8777,
        token="dev-local-token",
        timeout_sec=2.0,
        expected_slot="B",
    )

    assert ok is True
    assert details["attempts"] == 3
    assert details["timeout_sec"] == 2.0
    assert details["last_attempt"]["checks"][0]["ok"] is True
