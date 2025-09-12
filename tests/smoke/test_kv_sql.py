# tests/smoke/test_kv_sql.py
from adaos.services.agent_context import get_ctx


def test_kv(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = get_ctx()
    ctx.kv.set("foo", {"a": 1})
    assert ctx.kv.get("foo") == {"a": 1}
