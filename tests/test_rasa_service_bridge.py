from types import SimpleNamespace

import pytest


@pytest.mark.anyio
async def test_rasa_service_bridge_reuses_healthy_service_and_emits_detected(monkeypatch):
    from adaos.services.nlu import rasa_service_bridge as bridge

    emitted = []
    downstream = []

    class Supervisor:
        async def refresh_discovered(self, force=False):
            return None

        async def start(self, name):
            raise AssertionError("healthy service should not be restarted")

        def resolve_base_url(self, name):
            return "http://127.0.0.1:18092"

    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "_service_health_ok", lambda base_url: True)
    monkeypatch.setattr(
        bridge,
        "_http_post_json",
        lambda url, payload, *, timeout_ms: {
            "ok": True,
            "result": {
                "intent": {"name": "desktop.open_modal", "confidence": 0.91},
                "entities": [{"entity": "modal_id", "value": "nlu_teacher_modal"}],
            },
        },
    )
    monkeypatch.setattr(bridge, "bus_emit", lambda _bus, event, payload, source=None: emitted.append((event, payload, source)))
    monkeypatch.setattr(bridge, "record_neural_fallback_outcome", lambda **kwargs: downstream.append(kwargs))

    await bridge._parse_and_emit(
        text="открой модалку nlu_teacher_modal",
        webspace_id="ws1",
        request_id="rid1",
        meta={"trace": "test", "neural_fallback": True, "neural_fallback_reason": "neural_low_confidence"},
    )

    assert emitted
    event, payload, source = emitted[-1]
    assert event == "nlp.intent.detected"
    assert source == "nlu.rasa"
    assert payload["intent"] == "desktop.open_modal"
    assert payload["slots"] == {"modal_id": "nlu_teacher_modal"}
    assert payload["webspace_id"] == "ws1"
    assert payload["request_id"] == "rid1"
    assert payload["via"] == "rasa"
    assert downstream == [
        {
            "request_id": "rid1",
            "status": "accepted",
            "reason": "rasa_accepted",
            "intent": "desktop.open_modal",
            "confidence": 0.91,
            "via": "rasa",
        }
    ]


@pytest.mark.anyio
async def test_rasa_service_bridge_does_not_install_runtime_slots(monkeypatch):
    from adaos.services.nlu import rasa_service_bridge as bridge

    class Supervisor:
        async def refresh_discovered(self, force=False):
            return None

        async def start(self, name):
            raise AssertionError("missing Rasa service must not be installed or started from parse path")

        def resolve_base_url(self, name):
            return None

    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())

    result = await bridge.parse_text("open modal", webspace_id="ws1", request_id="rid1")

    assert result == {"ok": False, "reason": "rasa_base_url_unresolved", "via": "rasa"}


@pytest.mark.anyio
async def test_rasa_service_bridge_disabled_emits_not_obtained(monkeypatch):
    from adaos.services.nlu import rasa_service_bridge as bridge

    emitted = []
    monkeypatch.setenv("ADAOS_NLU_RASA", "0")
    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(
        bridge,
        "get_service_supervisor",
        lambda: (_ for _ in ()).throw(AssertionError("disabled Rasa must not touch supervisor")),
    )
    monkeypatch.setattr(bridge, "bus_emit", lambda _bus, event, payload, source=None: emitted.append((event, payload, source)))

    await bridge._parse_and_emit(text="open modal", webspace_id="ws1", request_id="rid1")

    assert emitted[0][0] == "nlu.trace.stage"
    assert emitted[0][1]["stage"] == "rasa"
    assert emitted[0][1]["status"] == "skipped"
    assert emitted[0][1]["reason"] == "rasa_disabled"
    assert emitted[-1] == (
        "nlp.intent.not_obtained",
        {"reason": "rasa_disabled", "text": "open modal", "via": "rasa", "webspace_id": "ws1", "request_id": "rid1"},
        "nlu.rasa",
    )


@pytest.mark.anyio
async def test_rasa_training_bridge_records_successful_training(monkeypatch, tmp_path):
    from adaos.services.nlu import rasa_training_bridge as bridge

    records = []

    class Supervisor:
        async def refresh_discovered(self, force=False):
            return None

        async def start(self, name):
            raise AssertionError("healthy service should not be restarted")

        def resolve_base_url(self, name):
            return "http://127.0.0.1:18092"

    class Workspace:
        def __init__(self, ctx):
            self.ctx = ctx

        def record_training(self, *, note=None, extra=None):
            records.append({"note": note, "extra": extra})
            return {"trained_at": "now"}

    monkeypatch.setenv("ADAOS_NLU_AUTOTRAIN", "1")
    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(paths=SimpleNamespace()))
    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())
    monkeypatch.setattr(bridge, "_service_health_ok", lambda base_url: True)
    monkeypatch.setattr(bridge, "_train_sync", lambda ctx: {"project_dir": str(tmp_path / "project"), "out_dir": str(tmp_path / "models")})
    monkeypatch.setattr(
        bridge,
        "_http_post_json",
        lambda url, payload, *, timeout_ms=600_000: {"ok": True, "model_path": str(tmp_path / "models" / "interpreter_latest.tar.gz")},
    )
    monkeypatch.setattr(bridge, "InterpreterWorkspace", Workspace)

    await bridge._train_if_enabled("manual")

    assert records == [
        {
            "note": "rasa-auto:manual",
            "extra": {
                "engine": "rasa_service",
                "model_path": str(tmp_path / "models" / "interpreter_latest.tar.gz"),
                "reason": "manual",
            },
        }
    ]


@pytest.mark.anyio
async def test_rasa_training_bridge_does_not_install_runtime_slots(monkeypatch):
    from adaos.services.nlu import rasa_training_bridge as bridge

    class Supervisor:
        async def refresh_discovered(self, force=False):
            return None

        async def start(self, name):
            raise AssertionError("missing Rasa service must not be installed or started from train path")

        def resolve_base_url(self, name):
            return None

    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(paths=SimpleNamespace()))
    monkeypatch.setattr(bridge, "get_service_supervisor", lambda: Supervisor())

    result = await bridge.train_rasa_nlu_once(reason="manual")

    assert result == {"ok": False, "skipped": True, "reason": "rasa_base_url_unresolved"}


@pytest.mark.anyio
async def test_rasa_training_bridge_disabled_skips_without_supervisor(monkeypatch):
    from adaos.services.nlu import rasa_training_bridge as bridge

    monkeypatch.setenv("ADAOS_NLU_RASA", "0")
    monkeypatch.setattr(bridge, "get_ctx", lambda: SimpleNamespace(paths=SimpleNamespace()))
    monkeypatch.setattr(
        bridge,
        "get_service_supervisor",
        lambda: (_ for _ in ()).throw(AssertionError("disabled Rasa must not touch supervisor")),
    )

    result = await bridge.train_rasa_nlu_once(reason="manual")

    assert result == {"ok": False, "skipped": True, "reason": "rasa_disabled"}
