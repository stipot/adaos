from __future__ import annotations

import json

from typer.testing import CliRunner


def test_neural_probe_joins_extra_text_words(monkeypatch):
    from adaos.apps.cli.commands import interpreter

    seen = {}

    async def _fake_parse_text(text, **kwargs):
        seen["text"] = text
        seen["kwargs"] = kwargs
        return {"ok": True, "accepted": True, "intent": "weather.get"}

    monkeypatch.setattr(interpreter.neural_service_bridge, "parse_text", _fake_parse_text)

    result = CliRunner().invoke(
        interpreter.app,
        ["neural-probe", "what", "is", "weather", "--locale", "en"],
    )

    assert result.exit_code == 0
    assert seen["text"] == "what is weather"
    assert seen["kwargs"]["locale"] == "en"
    assert json.loads(result.output)["intent"] == "weather.get"


def test_neural_diagnostics_combines_readiness_and_usage(monkeypatch):
    from adaos.apps.cli.commands import interpreter

    async def _fake_readiness(**kwargs):
        assert kwargs["start_service"] is True
        assert kwargs["stop_after"] is True
        return {"ok": True, "checks": {"model_loaded": True}}

    monkeypatch.setattr(interpreter.neural_service_bridge, "diagnose_readiness", _fake_readiness)
    monkeypatch.setattr(
        interpreter,
        "read_neural_usage_stats",
        lambda: {
            "schema_version": 1,
            "totals": {"requests": 3},
            "recent": [{"id": 1}, {"id": 2}, {"id": 3}],
            "review_samples": [{"id": "a"}, {"id": "b"}],
        },
    )

    result = CliRunner().invoke(
        interpreter.app,
        ["neural-diagnostics", "--start", "--stop-after", "--recent", "2", "--review-samples", "1"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["readiness"]["checks"]["model_loaded"] is True
    assert payload["usage_stats"]["totals"]["requests"] == 3
    assert payload["usage_stats"]["recent"] == [{"id": 2}, {"id": 3}]
    assert payload["usage_stats"]["review_samples"] == [{"id": "b"}]
