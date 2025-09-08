# tests/smoke/test_eventbus_logging.py
from adaos.apps.bootstrap import init_ctx
from adaos.services.eventbus import emit


def test_emit_event(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = init_ctx()
    emit(ctx.bus, "demo.started", {"x": 1}, "smoke")
    # если дошли сюда — publish/логгер не упали
    assert (tmp_path / "base" / "logs" / "adaos.log").exists()
