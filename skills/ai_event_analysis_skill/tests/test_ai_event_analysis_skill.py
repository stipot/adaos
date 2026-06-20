from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = SKILL_ROOT / "handlers" / "main.py"
    module_name = f"test_ai_event_analysis_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_agent_module():
    path = SKILL_ROOT / "handlers" / "projection_relevance_agent.py"
    module_name = f"test_projection_relevance_agent_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_measurable_tools_and_stream_wakeup() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "ai_event_analysis_skill"
    assert "webio.stream.snapshot.requested" in manifest["events"]["subscribe"]
    assert "ai_event_analysis.evaluate_requested" in manifest["events"]["subscribe"]
    assert {tool["name"] for tool in manifest["tools"]} == {
        "get_lab_snapshot",
        "refresh_snapshot",
        "rehydrate",
        "run_demo_evaluation",
        "run_trial_suite",
        "run_real_trial",
        "train_projection_relevance_agent",
        "predict_projection_refresh_plan",
        "run_projection_relevance_agent_trial",
        "summarize_projection_relevance_agent",
        "evaluate_windows",
        "import_local_logs",
        "build_event_windows",
        "analyze_local_logs",
        "analyze_observability_health",
        "analyze_subscription_flow",
        "export_event_windows_jsonl",
    }
    assert manifest["lifecycle"]["rehydrate"] == "rehydrate"
    projection_slots = {entry["slot"] for entry in manifest["data_projections"]}
    assert {entry["scope"] for entry in manifest["data_projections"]} == {"subnet"}
    assert {
        "ai_event_analysis.summary",
        "ai_event_analysis.task",
        "ai_event_analysis.dataset",
        "ai_event_analysis.windows",
        "ai_event_analysis.metrics",
        "ai_event_analysis.per_class",
        "ai_event_analysis.chart",
        "ai_event_analysis.event_volume_chart",
        "ai_event_analysis.class_distribution_chart",
        "ai_event_analysis.subscription_summary",
        "ai_event_analysis.subscription_edges",
        "ai_event_analysis.subscription_metrics",
        "ai_event_analysis.subscription_chart",
        "ai_event_analysis.experiments",
        "ai_event_analysis.agent_summary",
        "ai_event_analysis.agent_plan",
        "ai_event_analysis.agent_gates",
    }.issubset(projection_slots)


def test_webui_declares_app_widget_and_results_receiver() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))

    assert webui["apps"][0]["id"] == "ai_event_analysis_app"
    assert webui["widgets"][0]["id"] == "ai_event_analysis_widget"
    assert webui["webio"]["receivers"]["ai_event_analysis.results"]["snapshotPolicy"] == "on_subscribe"
    widgets = webui["registry"]["modals"]["ai_event_analysis_modal"]["schema"]["widgets"]
    assert any(widget["type"] == "visual.metricChart" for widget in widgets)
    assert any(widget["type"] == "ui.table" for widget in widgets)
    assert any(widget["type"] == "ui.list" for widget in widgets)
    tabs = next(widget for widget in widgets if widget["id"] == "ai-event-analysis-tabs")
    assert any(button["id"] == "windows" for button in tabs["inputs"]["buttons"])
    assert any(button["id"] == "agent" for button in tabs["inputs"]["buttons"])
    assert any(button["id"] == "subscriptions" for button in tabs["inputs"]["buttons"])
    actions = next(widget for widget in widgets if widget["id"] == "ai-event-analysis-actions")
    assert any(button["id"] == "refresh_snapshot" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "run_trials" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "run_real_trial" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "run_agent_trial" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "analyze_logs" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "analyze_health" for button in actions["inputs"]["buttons"])
    assert any(button["id"] == "analyze_subscriptions" for button in actions["inputs"]["buttons"])
    readiness = next(widget for widget in widgets if widget["id"] == "ai-event-analysis-chart")
    assert readiness["title"] == "Готовность анализа"
    assert "subscriptions" not in readiness["visibleIf"]


def test_refresh_snapshot_projects_all_first_paint_sections(monkeypatch) -> None:
    mod = _load_module()
    written: list[tuple[str, object, str]] = []
    monkeypatch.setattr(mod, "set_current_skill", lambda _name: True)
    monkeypatch.setattr(mod, "clear_current_skill", lambda: None)
    monkeypatch.setattr(
        mod.ctx_subnet,
        "set",
        lambda slot, value, webspace_id=None: written.append((slot, value, webspace_id)),
    )
    mod._PROJECTION_FINGERPRINTS.clear()

    result = mod.refresh_snapshot({"webspace_id": "$runtime.webspace_id"})

    assert result["ok"] is True
    assert result["projected"]["webspace_id"] == "desktop"
    slots = {slot for slot, _value, _webspace_id in written}
    assert slots == {
        "ai_event_analysis.summary",
        "ai_event_analysis.task",
        "ai_event_analysis.dataset",
        "ai_event_analysis.windows",
        "ai_event_analysis.metrics",
        "ai_event_analysis.per_class",
        "ai_event_analysis.chart",
        "ai_event_analysis.event_volume_chart",
        "ai_event_analysis.class_distribution_chart",
        "ai_event_analysis.experiments",
        "ai_event_analysis.progress",
    }
    assert all(webspace_id == "desktop" for _slot, _value, webspace_id in written)
    assert next(value for slot, value, _ in written if slot == "ai_event_analysis.dataset")["items"]
    assert next(value for slot, value, _ in written if slot == "ai_event_analysis.task")["items"]

    written.clear()
    result = mod.refresh_snapshot({"webspace_id": "desktop"})
    assert result["projected"]["written"]
    assert written


def test_webspace_template_literal_falls_back_to_desktop() -> None:
    mod = _load_module()

    assert mod._webspace_id_from_payload({"webspace_id": "$runtime.webspace_id"}) == "desktop"
    assert mod._webspace_id_from_payload({"_meta": {"webspace_id": "$runtime.webspace_id"}}) == "desktop"
    assert mod._webspace_id_from_payload({"webspace_id": "operations"}) == "operations"


def test_rule_baseline_returns_required_measurement_fields() -> None:
    mod = _load_module()

    result = mod.run_demo_evaluation({"webspace_id": "test"})["result"]

    assert result["model"] == "rule_baseline_v1"
    assert result["window_count"] >= 10
    assert result["macro_f1"] >= 0.75
    assert result["critical_recall"] >= 0.85
    assert result["false_positive_rate"] <= 0.15
    assert result["top_reason_hit_rate"] > 0
    assert result["per_class"]


def test_trial_suite_populates_operational_and_subscription_data() -> None:
    mod = _load_module()

    result = mod.run_trial_suite({"webspace_id": "test"})["result"]

    assert result["mode"] == "trial_suite"
    assert result["scenario_count"] >= 8
    assert result["readiness_score"] >= 0.5
    assert result["baseline_result"]["macro_f1"] >= 0.75
    assert len(result["scenario_classes"]) >= 6
    assert result["subscription_result"]["summary"]["declared_subscriptions"] >= 4
    assert result["subscription_result"]["summary"]["missing_consumers"] >= 1
    assert result["chart"]["title"] == "Готовность синтетической проверки"
    assert any(point["ts"] == "routing health" for point in result["chart"]["points"])


def test_real_trial_emits_events_and_analyzes_read_back_logs(monkeypatch) -> None:
    mod = _load_module()
    emitted: list[tuple[str, object, str]] = []
    published: list[object] = []

    def fake_publish(event_type: str, payload: object, source: str | None = None) -> None:
        emitted.append((event_type, payload, source or ""))

    def fake_import(_payload: object) -> dict[str, object]:
        records = [
            {
                "id": f"real:{index}",
                "ts": 1000 + index,
                "topic": "eventbus.pressure" if "backpressure" in event_type else "projection.lifecycle",
                "severity": "error" if "failed" in event_type or "drop" in event_type else "info",
                "message": json.dumps({"type": event_type, "source": source, "trial_id": payload["trial_id"]}),
            }
            for index, (event_type, payload, source) in enumerate(emitted)
        ]
        return {"ok": True, "records": records, "summary": {"sources": [{"path": "fake.log", "records": len(records)}]}}

    monkeypatch.setattr(mod, "publish_event", fake_publish)
    monkeypatch.setattr(mod, "stream_publish", lambda _receiver, payload, _meta=None: published.append(payload))
    monkeypatch.setattr(mod, "import_local_logs", fake_import)
    monkeypatch.setattr(
        mod,
        "_run_cross_skill_probes",
        lambda webspace_id, trial_id: [{"skill": "demo_metrics_skill", "tool": "emit_demo_event", "ok": True, "duration_ms": 1.0}],
    )

    result = mod.run_real_trial({"webspace_id": "test", "trial_id": "real-test", "event_count": 12})["result"]

    assert result["mode"] == "real_trial"
    assert result["emitted_event_count"] >= 12
    assert result["trial_record_count"] == result["record_count"]
    assert result["cross_skill_probe_count"] == 1
    assert result["cross_skill_ok_count"] == 1
    assert result["window_count"] >= 1
    assert result["baseline_result"]["window_count"] == result["window_count"]
    assert result["rows"]
    assert published


def test_projection_relevance_agent_trains_against_rule_baseline() -> None:
    agent = _load_agent_module()

    result = agent.train_projection_relevance_agent(sample_count=280, seed=7, epochs=55)

    assert result["mode"] == "projection_relevance_training"
    assert result["dataset"]["train_count"] > 0
    assert result["dataset"]["eval_count"] > 0
    assert result["metrics"]["f1"] >= result["rule_baseline"]["f1"]
    assert result["metrics"]["write_reduction_ratio"] > result["rule_baseline"]["write_reduction_ratio"]
    assert result["metrics"]["missed_critical_projection_total"] == 0
    assert result["comparison"]["write_reduction_delta"] > 0


def test_projection_relevance_prediction_returns_guarded_refresh_plan() -> None:
    agent = _load_agent_module()

    result = agent.predict_projection_refresh_plan(
        {
            "event_type": "skill.installed",
            "event_class": "domain_fact",
            "active_subscriptions": ["overview", "inventory:skills", "platform:notifications"],
            "projection_statuses": {
                "overview": "ready",
                "inventory:skills": "ready",
                "platform:notifications": "ready",
            },
        }
    )

    assert result["mode"] == "projection_relevance_prediction"
    assert "overview" in result["affected_projections"]
    assert "inventory:skills" in result["affected_projections"]
    assert result["recommended_action"]["overview"] == "refresh"
    assert result["recommended_action"]["inventory:skills"] == "refresh"
    assert result["ranked_refresh_plan"][0]["recommended_action"] == "refresh"
    assert result["decision_contract"]["agent_role"] == "advisory_refresh_optimizer"
    assert "dispatcher" in result["guardrail"]


def test_projection_relevance_summary_reports_advisory_readiness() -> None:
    agent = _load_agent_module()

    result = agent.summarize_projection_relevance_agent(sample_count=260, seed=9, epochs=55)

    assert result["mode"] == "projection_relevance_agent_summary"
    assert result["advisory_ready"] is True
    assert result["decision"] == "ready_for_advisory_refresh_planning"
    assert result["metrics"]["f1"] >= result["rule_baseline"]["f1"]
    assert all(gate["passed"] for gate in result["gates"])
    assert {row["metric"] for row in result["metric_rows"]} >= {
        "F1",
        "Precision@3",
        "Recall@3",
        "Write reduction ratio",
        "Missed critical projections",
    }
    assert result["demo_refresh_plan"]["ranked_refresh_plan"]
    assert "guarded dispatcher" in " ".join(result["agent_boundary"]["not_allowed"])


def test_projection_relevance_trial_compares_agent_with_rule_baseline() -> None:
    agent = _load_agent_module()

    result = agent.build_projection_relevance_trial(
        {
            "event_type": "core.update.status",
            "event_class": "platform_fact",
            "active_subscriptions": [
                "overview",
                "platform:diagnostics",
                "platform:notifications",
                "inventory:skills",
            ],
        }
    )

    assert result["mode"] == "projection_relevance_agent_trial"
    assert result["rule_baseline"]["demanded_refresh"] == [
        "overview",
        "platform:diagnostics",
        "platform:notifications",
    ]
    assert result["comparison"]["missed_critical_projections"] == []
    assert result["safety"]["passed"] is True
    assert result["safety"]["dispatch_applied"] is False
    assert result["safety"]["agent_is_advisory"] is True
    assert {row["projection_key"] for row in result["plan_rows"]} == set(agent.PROJECTION_KEYS)


def test_projection_relevance_tools_project_experiment_result(monkeypatch) -> None:
    mod = _load_module()
    projected: list[tuple[object, object]] = []
    published: list[object] = []

    monkeypatch.setattr(mod, "_project_sections", lambda sections, **kwargs: projected.append((sections, kwargs)))
    monkeypatch.setattr(mod, "stream_publish", lambda _receiver, payload, _meta=None: published.append(payload))

    trained = mod.train_projection_relevance_agent({"webspace_id": "desktop", "sample_count": 180, "epochs": 35})
    predicted = mod.predict_projection_refresh_plan(
        {
            "webspace_id": "desktop",
            "event": {
                "event_type": "skill.installed",
                "active_subscriptions": ["overview", "inventory:skills"],
            },
        }
    )
    summary = mod.summarize_projection_relevance_agent({"webspace_id": "desktop", "sample_count": 180, "epochs": 35})

    assert trained["ok"] is True
    assert predicted["ok"] is True
    assert summary["ok"] is True
    assert trained["result"]["metrics"]["f1"] >= trained["result"]["rule_baseline"]["f1"]
    assert predicted["result"]["affected_projections"]
    assert summary["result"]["advisory_ready"] is True
    assert projected
    assert published


def test_projection_relevance_agent_trial_projects_web_ui_sections(monkeypatch) -> None:
    mod = _load_module()
    projected: list[tuple[object, object]] = []
    published: list[object] = []
    emitted: list[tuple[str, object]] = []

    monkeypatch.setattr(mod, "_project_sections", lambda sections, **kwargs: projected.append((sections, kwargs)))
    monkeypatch.setattr(mod, "stream_publish", lambda _receiver, payload, _meta=None: published.append(payload))
    monkeypatch.setattr(
        mod,
        "publish_event",
        lambda event_type, payload, source=None: emitted.append((event_type, payload)),
    )

    response = mod.run_projection_relevance_agent_trial(
        {
            "webspace_id": "desktop",
            "trial_id": "agent-test",
            "event_type": "core.update.status",
        }
    )

    result = response["result"]
    assert response["ok"] is True
    assert result["trial_id"] == "agent-test"
    assert result["event_source"] == "controlled_operational_event"
    assert result["demand_source"] == "controlled_web_ui_trial"
    assert result["safety"]["dispatch_applied"] is False
    assert result["comparison"]["missed_critical_projections"] == []
    assert projected
    sections = next(
        sections
        for sections, _kwargs in projected
        if {"agent_summary", "agent_plan", "agent_gates"}.issubset(sections)
    )
    assert {"agent_summary", "agent_plan", "agent_gates"}.issubset(sections)
    assert published
    assert {event_type for event_type, _payload in emitted} == {
        "ai_event_analysis.projection_relevance_agent_trial.started",
        "ai_event_analysis.projection_relevance_agent_trial.completed",
    }

    keyword_response = mod.run_projection_relevance_agent_trial(
        webspace_id="desktop",
        trial_id="agent-keyword-test",
        event_type="skill.installed",
    )
    keyword_result = keyword_response["result"]
    assert keyword_result["trial_id"] == "agent-keyword-test"
    assert keyword_result["event"]["event_type"] == "skill.installed"
    assert keyword_result["agent_plan"]["refresh"] == ["inventory:skills", "overview"]


def test_custom_window_evaluation_reports_false_positive_rate() -> None:
    mod = _load_module()

    windows = [
        {
            "window_id": "normal-1",
            "features": {
                "event_total": 10,
                "error_total": 0,
                "drop_total": 0,
                "projection_refresh_total": 2,
                "same_projection_refresh_max": 1,
                "yjs_write_total": 1,
            },
            "label": {"incident": False, "incident_type": "normal", "severity": "info", "reasons": []},
        }
    ]
    result = mod.evaluate_windows({"windows": windows})["result"]

    assert result["accuracy"] == 1.0
    assert result["false_positive_rate"] == 0.0


def test_local_log_import_builds_redacted_event_windows(tmp_path: Path) -> None:
    mod = _load_module()
    log_path = tmp_path / "runtime.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-05-29T09:00:01Z INFO runtime ready token=abc123",
                "2026-05-29T09:00:02Z WARN projection refresh repeated for status-card",
                "2026-05-29T09:00:03Z ERROR yjs write pressure at C:\\Users\\secret\\node.yaml",
            ]
        ),
        encoding="utf-8",
    )

    imported = mod.import_local_logs({"path": str(log_path), "max_lines": 10})
    assert imported["summary"]["record_count"] == 3
    assert "<redacted>" in imported["records"][0]["message"]
    assert "<path>" in imported["records"][2]["message"]

    built = mod.build_event_windows({"records": imported["records"], "window_seconds": 60})
    windows = built["result"]["windows"]
    assert built["result"]["window_count"] == 1
    assert built["result"]["baseline_result"]["window_count"] == 1
    assert built["result"]["label_source"] == "codex_reviewed_log_heuristic"
    assert windows[0]["features"]["event_total"] == 3
    assert windows[0]["features"]["projection_refresh_total"] == 1
    assert windows[0]["features"]["yjs_write_total"] == 1
    assert windows[0]["label"]["source"] == "codex_reviewed_log_heuristic"


def test_observability_health_reports_schema_and_invariant_metrics(tmp_path: Path) -> None:
    mod = _load_module()
    log_path = tmp_path / "events.log"
    log_path.write_text(
        "\n".join(
            [
                '{"level":"INFO","logger":"adaos.events","msg":"event","time":"2026-05-31T08:00:00Z","ts":1780214400,"type":"tool.started","source":"tool_bridge","trace":"t1"}',
                '{"level":"WARNING","logger":"adaos.yjs.governance","msg":"blocked YJS primary-doc write reason=write_amplification_blocked","time":"2026-05-31T08:00:01Z","ts":1780214401}',
                '{"level":"WARNING","logger":"adaos.eventbus","msg":"slow async event handler duration=1.2s","time":"2026-05-31T08:00:02Z","ts":1780214402}',
            ]
        ),
        encoding="utf-8",
    )

    result = mod.analyze_observability_health({"path": str(log_path), "max_lines": 20})["result"]

    assert result["mode"] == "observability_health"
    assert result["record_count"] == 3
    assert result["summary"]["blocked_writes"] == 1
    assert result["summary"]["slow_handlers"] == 1
    assert result["summary"]["schema_score"] > 0
    metric_ids = {item["id"] for item in result["metrics"]["items"]}
    assert "observability_score" in metric_ids
    assert "correlation_coverage" in metric_ids


def test_event_window_export_writes_jsonl(tmp_path: Path) -> None:
    mod = _load_module()
    out = tmp_path / "windows.jsonl"
    windows = [
        {
            "window_id": "w1",
            "features": {"event_total": 1},
            "label": {"incident": False, "incident_type": "normal", "severity": "info", "reasons": []},
        }
    ]

    result = mod.export_event_windows_jsonl({"windows": windows, "path": str(out)})

    assert result["ok"] is True
    assert result["export"]["count"] == 1
    assert json.loads(out.read_text(encoding="utf-8"))["window_id"] == "w1"


def test_build_windows_keeps_default_tool_response_compact(tmp_path: Path) -> None:
    mod = _load_module()
    log_path = tmp_path / "runtime.log"
    log_path.write_text(
        "\n".join(f"2026-05-29T09:00:{second:02d}Z WARN projection refresh repeated" for second in range(30)),
        encoding="utf-8",
    )

    result = mod.build_event_windows({"path": str(log_path), "window_seconds": 60})["result"]

    assert result["window_count"] == 1
    assert result["windows"] == []
    assert result["rows"]
    assert len(json.dumps(result, ensure_ascii=False)) < 12288


def test_stream_dataset_publish_is_compact(monkeypatch) -> None:
    mod = _load_module()
    published: list[object] = []
    monkeypatch.setattr(
        mod,
        "stream_publish",
        lambda _receiver, payload, _meta=None: published.append(payload),
    )

    large_result = {
        "window_count": 100,
        "record_count": 500,
        "window_seconds": 60,
        "windows": [{"window_id": str(i), "evidence": [{"message": "x" * 1000}]} for i in range(100)],
        "rows": [{"window_id": str(i), "events": i} for i in range(100)],
        "event_volume_chart": {"points": [{"ts": str(i), "value": i} for i in range(100)]},
        "class_distribution_chart": {"points": [{"ts": "normal", "value": 100}]},
        "built_at": "2026-05-29T09:00:00Z",
    }

    mod._publish_dataset_result(large_result, webspace_id="desktop")

    assert published
    payload_json = json.dumps(published[0], ensure_ascii=False)
    assert "x" * 1000 not in payload_json
    assert len(payload_json.encode("utf-8")) < 12288


def test_subscription_flow_analysis_detects_missing_and_idle_consumers() -> None:
    mod = _load_module()
    records = [
        {
            "message": '{"level":"INFO","logger":"adaos.sdk.subscriptions","msg":"skill=alpha subscriptions=[event.a: on_a, event.b: on_b]"}',
            "severity": "info",
            "topic": "runtime.log",
            "ts": 1,
        },
        {
            "message": '{"level":"INFO","logger":"adaos.events","type":"event.a","source":"publisher.one"}',
            "severity": "info",
            "topic": "runtime.log",
            "ts": 2,
        },
        {
            "message": '{"level":"INFO","logger":"adaos.events","type":"event.c","source":"publisher.two"}',
            "severity": "info",
            "topic": "runtime.log",
            "ts": 3,
        },
    ]

    result = mod.analyze_subscription_flow({"records": records})["result"]

    assert result["summary"]["declared_subscriptions"] == 2
    assert result["summary"]["missing_consumers"] == 1
    assert result["summary"]["idle_subscriptions"] == 1
    states = {row["event_type"]: row["state"] for row in result["rows"]}
    assert states["event.a"] == "active"
    assert states["event.b"] == "idle"
    assert states["event.c"] == "missing_consumer"
