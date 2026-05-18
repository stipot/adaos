from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from adaos.apps import supervisor
from adaos.services.root.memory_profile_sync import build_memory_profile_report
from adaos.services.supervisor_memory import (
    MemoryOperationEvent,
    MemorySessionSummary,
    MemoryTelemetrySample,
    append_memory_session_operation,
    append_memory_telemetry_sample,
    ensure_memory_store,
    read_memory_session_operations,
    read_memory_telemetry_tail,
    read_memory_runtime_state,
    read_memory_session_index,
    supervisor_memory_runtime_state_path,
    supervisor_memory_session_operations_path,
    supervisor_memory_sessions_index_path,
    supervisor_memory_telemetry_path,
    write_memory_session_index,
)


def test_memory_store_initializes_runtime_and_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    ensure_memory_store()

    assert supervisor_memory_runtime_state_path().exists()
    assert supervisor_memory_sessions_index_path().exists()
    runtime = read_memory_runtime_state()
    index = read_memory_session_index()

    assert runtime["contract_version"] == "1"
    assert runtime["authority"] == "supervisor"
    assert runtime["current_profile_mode"] == "normal"
    assert index["contract_version"] == "1"
    assert index["sessions"] == []


def test_memory_session_summary_normalizes_artifact_refs() -> None:
    payload = MemorySessionSummary.from_dict(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "planned",
            "trigger_source": "operator",
            "artifact_refs": [
                {"artifact_id": "trace-1", "kind": "snapshot", "size_bytes": "128"},
                {"artifact_id": "report-1", "kind": "summary", "published_ref": "root://report-1"},
            ],
        }
    ).to_dict()

    assert payload["session_id"] == "mem-001"
    assert payload["profile_mode"] == "sampled_profile"
    assert payload["session_state"] == "planned"
    assert payload["trigger_source"] == "operator"
    assert payload["artifact_refs"][0]["size_bytes"] == 128
    assert payload["artifact_refs"][1]["published_ref"] == "root://report-1"


def test_memory_session_operations_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    append_memory_session_operation(
        "mem-001",
        MemoryOperationEvent(
            event_id="op-1",
            event="tool_invoked",
            emitted_at=11.0,
            session_id="mem-001",
            profile_mode="sampled_profile",
            sequence=1,
            details={"action": "profile_start"},
        ),
    )

    path = supervisor_memory_session_operations_path("mem-001")
    items = read_memory_session_operations("mem-001", limit=10)

    assert path.exists()
    assert len(items) == 1
    assert items[0]["event"] == "tool_invoked"
    assert items[0]["details"]["action"] == "profile_start"


def test_memory_telemetry_tail_round_trips_samples(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    append_memory_telemetry_sample(
        MemoryTelemetrySample(
            sampled_at=10.0,
            slot="A",
            runtime_instance_id="rt-a-a-1",
            managed_pid=999,
            process_rss_bytes=123,
            family_rss_bytes=456,
        )
    )

    tail = read_memory_telemetry_tail(limit=5)

    assert len(tail) == 1
    assert tail[0]["slot"] == "A"
    assert tail[0]["family_rss_bytes"] == 456


def test_memory_telemetry_compacts_large_jsonl(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_MAX_BYTES", "700")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_COMPACT_KEEP_LINES", "3")

    for index in range(20):
        append_memory_telemetry_sample(
            {
                "sampled_at": float(index),
                "slot": "A",
                "family_rss_bytes": index,
                "rss_growth_bytes": index,
            }
        )

    lines = supervisor_memory_telemetry_path().read_text(encoding="utf-8").splitlines()
    tail = read_memory_telemetry_tail(limit=10)

    assert len(lines) <= 4
    assert tail[-1]["sampled_at"] == 19.0
    assert tail[0]["sampled_at"] >= 16.0


def test_memory_telemetry_tail_uses_bounded_file_tail(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    for index in range(30):
        append_memory_telemetry_sample(
            {
                "sampled_at": float(index),
                "slot": "A",
                "runtime_instance_id": "rt-a-a-1",
                "managed_pid": 999,
                "family_rss_bytes": index,
            }
        )
    path = supervisor_memory_telemetry_path()
    assert path.exists()

    def _fail_read_text(self, *args, **kwargs):
        raise AssertionError(f"unexpected full text read for {self}")

    monkeypatch.setattr(Path, "read_text", _fail_read_text)

    tail = read_memory_telemetry_tail(limit=3)

    assert [item["sampled_at"] for item in tail] == [27.0, 28.0, 29.0]


def test_supervisor_manager_memory_status_reports_live_rss(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: (111, 222))
    write_memory_session_index(
        {
            "contract_version": "1",
            "sessions": [{"session_id": "mem-001", "profile_mode": "normal"}],
            "updated_at": 10.0,
        }
    )

    payload = manager.memory_status()

    assert payload["selected_profiler_adapter"] == "tracemalloc"
    assert payload["active_slot"] == "A"
    assert payload["managed_pid"] == 4321
    assert payload["current_process_rss_bytes"] == 111
    assert payload["current_family_rss_bytes"] == 222
    assert payload["sessions_total"] == 1
    assert payload["last_session_id"] == "mem-001"
    assert payload["profile_control_mode"] == "phase2_supervisor_restart"
    assert payload["operation_log_contract_version"] == "1"


def test_memory_status_uses_compact_session_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    write_memory_session_index(
        {
            "contract_version": "1",
            "updated_at": 123.0,
            "sessions": [{"session_id": f"mem-{index:03d}"} for index in range(20)],
        }
    )

    compact = manager._memory_sessions_index_compact(limit=3)

    assert compact["total"] == 20
    assert compact["returned"] == 3
    assert compact["omitted"] == 17
    assert [item["session_id"] for item in compact["sessions"]] == ["mem-017", "mem-018", "mem-019"]


def test_supervisor_manager_profile_intent_flow_updates_session_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "idle", "phase": ""})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: None)
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

    started = manager.start_memory_profile(profile_mode="sampled_profile", reason="operator.request")
    session_id = started["session"]["session_id"]

    assert started["control_mode"] == "phase2_supervisor_restart"
    assert started["session"]["session_state"] == "requested"
    assert started["runtime"]["requested_profile_mode"] == "sampled_profile"
    assert started["runtime"]["requested_session_id"] == session_id

    details = manager.memory_session(session_id)
    assert details is not None
    assert details["operations"][0]["details"]["action"] == "profile_start"

    published = manager.publish_memory_profile(session_id, reason="operator.publish")
    assert published["session"]["publish_state"] == "published"
    assert published["session"]["published_to_root"] is True
    assert published["session"]["published_ref"] == "root-msg-1"
    assert published["runtime"]["publish_request_session_id"] == session_id


def test_publish_memory_profile_stamps_artifact_published_refs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "idle", "phase": ""})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: None)
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
    started = manager.start_memory_profile(profile_mode="sampled_profile", reason="operator.request")
    session_id = started["session"]["session_id"]
    manager._upsert_memory_session_summary(
        {
            **started["session"],
            "artifact_refs": [
                {
                    "artifact_id": "mem-001-final",
                    "kind": "tracemalloc_final_snapshot",
                    "content_type": "application/json",
                    "size_bytes": 128,
                }
            ],
        }
    )

    published = manager.publish_memory_profile(session_id, reason="operator.publish")

    assert published["session"]["artifact_refs"][0]["published_ref"] == f"root://hub-memory-profile/{session_id}/mem-001-final"

    stopped = manager.stop_memory_profile(session_id, reason="operator.stop")
    assert stopped["session"]["session_state"] == "cancelled"
    assert stopped["runtime"]["requested_session_id"] is None
    assert stopped["runtime"]["requested_profile_mode"] is None


def test_build_memory_profile_report_marks_remote_artifact_policy(tmp_path) -> None:
    inline_path = tmp_path / "inline.json"
    inline_path.write_text(json.dumps({"top_allocations": []}), encoding="utf-8")
    oversize_path = tmp_path / "oversize.json"
    oversize_path.write_text("x" * (256 * 1024 + 32), encoding="utf-8")

    conf = type("Cfg", (), {"subnet_id": "subnet-test-1", "node_id": "node-1", "role": "hub"})()
    report = build_memory_profile_report(
        conf,
        session_summary={
            "session_id": "mem-001",
            "profile_mode": "trace_profile",
            "session_state": "finished",
            "artifact_refs": [
                {
                    "artifact_id": "mem-001-final",
                    "kind": "tracemalloc_final_snapshot",
                    "content_type": "application/json",
                    "path": str(inline_path),
                },
                {
                    "artifact_id": "mem-001-big",
                    "kind": "tracemalloc_trace_final",
                    "content_type": "application/json",
                    "path": str(oversize_path),
                },
                {
                    "artifact_id": "mem-001-raw",
                    "kind": "heap_dump",
                    "content_type": "application/octet-stream",
                    "path": str(tmp_path / "raw.bin"),
                },
            ],
        },
    )

    refs = report["session"]["artifact_refs"]
    assert refs[0]["publish_status"] == "inline_available"
    assert refs[0]["remote_available"] is True
    assert refs[0]["fetch_strategy"] == "inline_content"
    assert refs[0]["source_api_path"] == "/api/supervisor/memory/sessions/mem-001/artifacts/mem-001-final"
    assert refs[1]["publish_status"] == "size_limit_exceeded"
    assert refs[1]["remote_available"] is False
    assert refs[1]["fetch_strategy"] == "local_control_pull"
    assert refs[2]["publish_status"] == "kind_not_allowed"
    assert refs[2]["remote_available"] is False
    assert refs[2]["fetch_strategy"] == "local_control_pull"
    assert len(report["artifact_payloads"]) == 1
    assert report["artifact_payloads"][0]["artifact_id"] == "mem-001-final"
    assert report["artifact_policy"]["delivery_mode"] == "inline_json_only"
    assert report["artifact_policy"]["fallback_delivery_mode"] == "local_control_pull"


def test_memory_session_artifact_chunk_supports_binary_transfer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(b"\x00\x01\x02\x03" * 32)
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-001",
            "profile_mode": "trace_profile",
            "session_state": "finished",
            "artifact_refs": [
                {
                    "artifact_id": "mem-001-bin",
                    "kind": "heap_dump",
                    "path": str(artifact_path),
                    "content_type": "application/octet-stream",
                    "size_bytes": artifact_path.stat().st_size,
                }
            ],
        }
    )

    payload = manager.memory_session_artifact_chunk("mem-001", "mem-001-bin", offset=0, max_bytes=32)

    assert payload is not None
    assert payload["exists"] is True
    assert payload["transfer"]["encoding"] == "base64"
    assert payload["transfer"]["chunk_bytes"] == 32
    assert payload["transfer"]["truncated"] is True
    assert isinstance(payload["content_base64"], str)


def test_supervisor_manager_samples_memory_telemetry_and_marks_suspicion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_SEC", "5")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_WINDOW_SEC", "60")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_GROWTH_BYTES", str(32 * 1024 * 1024))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_SLOPE_BYTES_PER_MIN", str(8 * 1024 * 1024))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 4321

        @staticmethod
        def poll():
            return None

    samples = iter(
        [
            (100 * 1024 * 1024, 100 * 1024 * 1024),
            (100 * 1024 * 1024, 160 * 1024 * 1024),
            (100 * 1024 * 1024, 160 * 1024 * 1024),
            (100 * 1024 * 1024, 160 * 1024 * 1024),
        ]
    )
    times = iter([10.0, 70.0, 71.0, 72.0, 73.0, 74.0])
    manager._proc = _Proc()
    manager._managed_runtime_instance_id = "rt-a-a-1"
    manager._managed_transition_role = "active"

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: next(samples))
    monkeypatch.setattr(supervisor, "_available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(supervisor.time, "time", lambda: next(times, 999.0))
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)

    first = manager._sample_memory_telemetry()
    second = manager._sample_memory_telemetry()
    monkeypatch.setattr(
        supervisor,
        "_process_family_rss_bytes",
        lambda pid: (100 * 1024 * 1024, 160 * 1024 * 1024),
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 80.0)
    status = manager.memory_status()

    assert first is not None
    assert second is not None
    assert status["telemetry_samples_total"] == 2
    assert status["baseline_family_rss_bytes"] == 100 * 1024 * 1024
    assert status["rss_growth_bytes"] == 60 * 1024 * 1024
    assert status["suspicion_state"] == "suspected"
    assert status["suspicion_reason"] == "growth_and_slope_threshold"
    assert status["requested_profile_mode"] == "sampled_profile"
    assert status["requested_session_id"]


def test_supervisor_manager_marks_plateaued_growth_as_suspected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_SEC", "5")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_WINDOW_SEC", "60")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_GROWTH_BYTES", str(32 * 1024 * 1024))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_SLOPE_BYTES_PER_MIN", str(1024 * 1024 * 1024))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 4321

        @staticmethod
        def poll():
            return None

    samples = iter(
        [
            (100 * 1024 * 1024, 100 * 1024 * 1024),
            (100 * 1024 * 1024, 160 * 1024 * 1024),
        ]
    )
    times = iter([10.0, 70.0])
    manager._proc = _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: next(samples))
    monkeypatch.setattr(supervisor, "_available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(supervisor.time, "time", lambda: next(times, 999.0))
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)

    first = manager._sample_memory_telemetry()
    second = manager._sample_memory_telemetry()

    assert first is not None
    assert second is not None
    assert second["suspicion_state"] == "suspected"
    assert second["rss_growth_bytes"] == 60 * 1024 * 1024


def test_supervisor_manager_ignores_zero_memory_baseline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_SEC", "5")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 4321

        @staticmethod
        def poll():
            return None

    samples = iter([(0, 0), (100, 160)])
    times = iter([10.0, 70.0])
    manager._proc = _Proc()
    manager._memory_baseline_family_rss_bytes = 0

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: next(samples))
    monkeypatch.setattr(supervisor, "_available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(supervisor.time, "time", lambda: next(times, 999.0))
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: None)

    first = manager._sample_memory_telemetry()
    second = manager._sample_memory_telemetry()

    assert first is not None
    assert first["baseline_rss_bytes"] is None
    assert first["rss_growth_bytes"] == 0
    assert second is not None
    assert second["baseline_rss_bytes"] == 160
    assert second["rss_growth_bytes"] == 0
    assert manager._memory_baseline_family_rss_bytes == 160


def test_spawn_runtime_locked_sets_profile_launch_env_for_requested_session(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(supervisor, "active_slot_manifest", lambda: {"slot": "A", "argv": ["/slot/python"], "cwd": "/slot/repo"})
    monkeypatch.setattr(supervisor, "core_slot_status", lambda: {"slots": {"A": {"path": "/slots/A"}}})
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    started = manager.start_memory_profile(profile_mode="trace_profile", reason="operator.request")
    session_id = started["session"]["session_id"]
    asyncio.run(manager._spawn_runtime_locked(reason="test.memory.profile"))

    env = captured["kwargs"]["env"]
    assert env["ADAOS_SUPERVISOR_PROFILE_MODE"] == "trace_profile"
    assert env["ADAOS_SUPERVISOR_PROFILE_SESSION_ID"] == session_id
    assert env["ADAOS_SUPERVISOR_PROFILE_TRIGGER"].startswith("operator:")
    assert manager.memory_status()["current_profile_mode"] == "trace_profile"


def test_supervisor_memory_telemetry_endpoint_and_session_details_include_tail(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "idle", "phase": ""})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: None)
    session = manager.start_memory_profile(profile_mode="sampled_profile", reason="operator.request")["session"]
    session_id = session["session_id"]
    manager._upsert_memory_session_summary({**session, "runtime_instance_id": "rt-a-a-1", "session_state": "running", "started_at": 10.0})
    append_memory_telemetry_sample(
        MemoryTelemetrySample(
            sampled_at=11.0,
            slot="A",
            runtime_instance_id="rt-a-a-1",
            profile_mode="sampled_profile",
            suspicion_state="suspected",
            family_rss_bytes=256,
            rss_growth_bytes=64,
        )
    )

    telemetry = manager.memory_telemetry(limit=10)
    details = manager.memory_session(session_id)

    assert telemetry["total"] == 1
    assert telemetry["items"][0]["profile_mode"] == "sampled_profile"
    assert details is not None
    assert details["telemetry"][0]["runtime_instance_id"] == "rt-a-a-1"
    assert details["artifacts_dir"].endswith(f"{session_id}\\artifacts") or details["artifacts_dir"].endswith(f"{session_id}/artifacts")


def test_supervisor_memory_incidents_and_artifact_lookup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    session = manager._upsert_memory_session_summary(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "failed",
            "trigger_source": "policy",
            "suspected_leak": True,
            "artifact_refs": [
                {
                    "artifact_id": "mem-001-growth",
                    "kind": "tracemalloc_top_growth",
                    "path": str((tmp_path / "artifact.json").resolve()),
                    "content_type": "application/json",
                }
            ],
        }
    )
    (tmp_path / "artifact.json").write_text('{"top_growth_sites":[{"size_diff_bytes":128}]}', encoding="utf-8")

    incidents = manager.memory_incidents(limit=10)
    artifact = manager.memory_session_artifact("mem-001", "mem-001-growth")

    assert incidents["total"] == 1
    assert incidents["incidents"][0]["session_id"] == "mem-001"
    assert artifact is not None
    assert artifact["artifact"]["artifact_id"] == "mem-001-growth"
    assert artifact["content"]["top_growth_sites"][0]["size_diff_bytes"] == 128


def test_supervisor_memory_policy_guard_suppresses_repeat_auto_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_TELEMETRY_SEC", "5")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_WINDOW_SEC", "60")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_GROWTH_BYTES", str(32 * 1024 * 1024))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_SLOPE_BYTES_PER_MIN", str(8 * 1024 * 1024))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_PROFILE_COOLDOWN_SEC", "600")
    monkeypatch.setenv("ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC", "0")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 4321

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    manager._managed_runtime_instance_id = "rt-a-a-1"
    manager._managed_transition_role = "active"
    manager._persist_runtime_state = lambda: None
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-prev",
            "profile_mode": "sampled_profile",
            "session_state": "finished",
            "trigger_source": "policy",
            "trigger_reason": "memory.growth_and_slope_threshold",
            "requested_at": 50.0,
            "finished_at": 55.0,
        }
    )

    samples = iter(
        [
            (100 * 1024 * 1024, 100 * 1024 * 1024),
            (100 * 1024 * 1024, 160 * 1024 * 1024),
        ]
    )
    times = iter([100.0, 160.0])

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: next(samples))
    monkeypatch.setattr(supervisor, "_available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(supervisor.time, "time", lambda: next(times, 999.0))

    manager._sample_memory_telemetry()
    manager._sample_memory_telemetry()
    monkeypatch.setattr(
        supervisor,
        "_process_family_rss_bytes",
        lambda pid: (100 * 1024 * 1024, 160 * 1024 * 1024),
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 170.0)

    status = manager.memory_status()

    assert status["suspicion_state"] == "suspected"
    assert status["suspicion_reason"] == "growth_and_slope_threshold"
    assert status["auto_profile_last_block_reason"] == "auto_profile_cooldown"
    assert status["requested_session_id"] is None


def test_supervisor_marks_profile_session_failed_when_profiled_runtime_exits(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        @staticmethod
        def poll():
            return 17

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    manager._proc = _Proc()
    manager._managed_runtime_instance_id = "rt-a-a-1"
    manager._managed_transition_role = "active"
    manager._memory_profile_mode = "sampled_profile"
    manager._desired_running = True
    manager._stopping = False
    manager._persist_runtime_state = lambda: None
    manager._memory_active_session_id = "mem-001"
    manager._memory_requested_profile_mode = "sampled_profile"
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "running",
            "runtime_instance_id": "rt-a-a-1",
            "started_at": 10.0,
            "requested_at": 9.0,
        }
    )

    calls = {"sleep": 0}

    async def _sleep(_: float) -> None:
        calls["sleep"] += 1
        if calls["sleep"] > 1:
            raise RuntimeError("stop-monitor")

    monkeypatch.setattr(supervisor.asyncio, "sleep", _sleep)

    try:
        asyncio.run(manager.monitor_forever())
    except RuntimeError as exc:
        assert str(exc) == "stop-monitor"

    session = manager.memory_session("mem-001")
    assert session is not None
    assert session["session"]["session_state"] == "failed"
    assert session["session"]["stop_reason"] == "runtime_exited_during_profile_mode"
    assert session["operations"][-1]["details"]["action"] == "profile_failed"


def test_supervisor_profile_mode_shutdown_uses_extended_grace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._memory_profile_mode = "sampled_profile"

    class _Proc:
        pid = 123

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def kill():
            return None

    captured: dict[str, object] = {}
    sleeps: list[float] = []

    def _post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = dict(json or {})
        captured["timeout"] = timeout
        raise RuntimeError("shutdown-api-unavailable")

    times = iter([0.0, 0.0, 10.0, 20.0, 26.0, 26.0, 31.0, 37.0, 37.0, 43.0, 48.0])

    monkeypatch.setattr(supervisor.requests, "post", _post)
    monkeypatch.setattr(supervisor.time, "time", lambda: next(times, 999.0))

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(supervisor.asyncio, "sleep", _sleep)

    proc = _Proc()
    manager._proc = proc

    asyncio.run(manager._terminate_proc_locked(graceful=True, reason="supervisor.runtime.listener_lost"))

    assert captured["json"] == {
        "reason": "supervisor.runtime.listener_lost",
        "drain_timeout_sec": 20.0,
        "signal_delay_sec": 1.0,
    }
    assert captured["timeout"] == 23.0
    assert sleeps


def test_supervisor_profile_session_requests_finalize_after_runtime_window(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_SAMPLED_PROFILE_MAX_RUNTIME_SEC", "40")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._memory_active_session_id = "mem-001"
    manager._memory_profile_mode = "sampled_profile"
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "running",
            "requested_at": 10.0,
            "started_at": 10.0,
        }
    )

    decision = manager._should_finalize_active_memory_profile(now=55.0)

    assert decision is not None
    assert decision["session_id"] == "mem-001"
    assert decision["profile_mode"] == "sampled_profile"
    assert decision["reason"] == "supervisor.memory.profile_window_complete.sampled_profile"


def test_supervisor_observes_runtime_profile_finalize_markers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "running",
            "artifact_refs": [
                {
                    "artifact_id": "mem-001-debug",
                    "kind": "runtime_profile_finalize_debug",
                    "path": str(tmp_path / "debug.json"),
                }
            ],
        }
    )

    assert manager._memory_profile_finalize_observed("mem-001") is True


def test_supervisor_retry_memory_profile_clones_retryable_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "idle", "phase": ""})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: None)
    manager._upsert_memory_session_summary(
        {
            "session_id": "mem-old",
            "profile_mode": "trace_profile",
            "session_state": "failed",
            "trigger_source": "operator",
            "trigger_reason": "operator.request",
            "trigger_threshold": "growth>=1",
            "requested_at": 10.0,
        }
    )

    retried = manager.retry_memory_profile("mem-old", reason="operator.retry")

    assert retried["retry_of_session_id"] == "mem-old"
    assert retried["session"]["profile_mode"] == "trace_profile"
    assert retried["session"]["session_state"] == "requested"
    assert retried["session"]["retry_of_session_id"] == "mem-old"
    assert retried["session"]["retry_root_session_id"] == "mem-old"
    assert retried["session"]["retry_depth"] == 1
    assert retried["session"]["operation_window"]["retry_of_session_id"] == "mem-old"
    details = manager.memory_session(retried["session"]["session_id"])
    assert details is not None
    assert details["operations"][-1]["details"]["action"] == "profile_retry"


def test_supervisor_memory_endpoints_expose_read_only_phase1_surfaces(monkeypatch) -> None:
    class _Manager:
        def memory_status(self) -> dict:
            return {"contract_version": "1", "current_profile_mode": "normal"}

        def memory_telemetry(self, *, limit: int = 100) -> dict:
            return {"ok": True, "items": [{"sampled_at": 1.0}], "total": 1, "limit": limit}

        def memory_incidents(self, *, limit: int = 50) -> dict:
            return {"ok": True, "incidents": [{"session_id": "mem-001", "session_state": "failed"}], "total": 1}

        def memory_sessions(self) -> dict:
            return {"ok": True, "sessions": [{"session_id": "mem-001"}], "total": 1}

        def memory_session(self, session_id: str) -> dict | None:
            if session_id == "mem-001":
                return {"ok": True, "session": {"session_id": session_id, "artifact_refs": [{"artifact_id": "art-1"}]}}
            return None

        def memory_session_artifact(self, session_id: str, artifact_id: str) -> dict | None:
            if session_id == "mem-001" and artifact_id == "art-1":
                return {"ok": True, "artifact": {"artifact_id": "art-1"}, "exists": True, "content": {"ok": True}}
            return None

        def start_memory_profile(self, *, profile_mode: str, reason: str, trigger_source: str = "operator") -> dict:
            return {
                "ok": True,
                "control_mode": "phase2_supervisor_restart",
                "session": {"session_id": "mem-001", "profile_mode": profile_mode, "trigger_source": trigger_source},
            }

        def stop_memory_profile(self, session_id: str, *, reason: str) -> dict:
            return {"ok": True, "session": {"session_id": session_id, "session_state": "cancelled"}}

        def retry_memory_profile(self, session_id: str, *, reason: str) -> dict:
            return {
                "ok": True,
                "retry_of_session_id": session_id,
                "control_mode": "phase2_supervisor_restart",
                "session": {"session_id": "mem-002", "session_state": "requested", "profile_mode": "trace_profile"},
            }

        def publish_memory_profile(self, session_id: str, *, reason: str) -> dict:
            return {
                "ok": True,
                "session": {
                    "session_id": session_id,
                    "publish_state": "published",
                    "published_ref": "root-msg-1",
                },
            }

    monkeypatch.setattr(supervisor, "_manager", lambda: _Manager())
    client = TestClient(supervisor.app)
    headers = {"X-AdaOS-Token": "dev-local-token"}

    status_response = client.get("/api/supervisor/memory/status", headers=headers)
    telemetry_response = client.get("/api/supervisor/memory/telemetry?limit=5", headers=headers)
    incidents_response = client.get("/api/supervisor/memory/incidents?limit=5", headers=headers)
    sessions_response = client.get("/api/supervisor/memory/sessions", headers=headers)
    session_response = client.get("/api/supervisor/memory/sessions/mem-001", headers=headers)
    artifact_response = client.get("/api/supervisor/memory/sessions/mem-001/artifacts/art-1", headers=headers)
    missing_response = client.get("/api/supervisor/memory/sessions/missing", headers=headers)
    missing_artifact_response = client.get("/api/supervisor/memory/sessions/mem-001/artifacts/missing", headers=headers)
    start_response = client.post(
        "/api/supervisor/memory/profile/start",
        headers=headers,
        json={"profile_mode": "sampled_profile", "reason": "operator.request"},
    )
    stop_response = client.post(
        "/api/supervisor/memory/profile/mem-001/stop",
        headers=headers,
        json={"reason": "operator.stop"},
    )
    retry_response = client.post(
        "/api/supervisor/memory/profile/mem-001/retry",
        headers=headers,
        json={"reason": "operator.retry"},
    )
    publish_response = client.post(
        "/api/supervisor/memory/publish",
        headers=headers,
        json={"session_id": "mem-001", "reason": "operator.publish"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["current_profile_mode"] == "normal"
    assert telemetry_response.status_code == 200
    assert telemetry_response.json()["total"] == 1
    assert incidents_response.status_code == 200
    assert incidents_response.json()["incidents"][0]["session_state"] == "failed"
    assert sessions_response.status_code == 200
    assert sessions_response.json()["total"] == 1
    assert session_response.status_code == 200
    assert session_response.json()["session"]["session_id"] == "mem-001"
    assert artifact_response.status_code == 200
    assert artifact_response.json()["artifact"]["artifact_id"] == "art-1"
    assert missing_response.status_code == 404
    assert missing_artifact_response.status_code == 404
    assert start_response.status_code == 200
    assert start_response.json()["session"]["profile_mode"] == "sampled_profile"
    assert stop_response.status_code == 200
    assert stop_response.json()["session"]["session_state"] == "cancelled"
    assert retry_response.status_code == 200
    assert retry_response.json()["retry_of_session_id"] == "mem-001"
    assert retry_response.json()["session"]["session_id"] == "mem-002"
    assert publish_response.status_code == 200
    assert publish_response.json()["session"]["publish_state"] == "published"
