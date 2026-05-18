from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".adaos" / "workspace" / "skills" / "new_face_vision_skill"


def _load_engine_class():
    if str(SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(SKILL_ROOT))
    from service.engine import NewFaceVisionEngine

    return NewFaceVisionEngine


def _load_engine_module():
    if str(SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(SKILL_ROOT))
    from service import engine as engine_module

    return engine_module


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


def _frames_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("frame_000.png", _png_bytes((240, 240, 240)))
        zf.writestr("frame_001.png", _png_bytes((40, 40, 40)))


def test_new_face_vision_engine_snapshot_imports_without_image_dependencies(tmp_path: Path) -> None:
    engine_cls = _load_engine_class()
    engine = engine_cls(tmp_path / "state")

    snapshot = engine.snapshot()

    assert snapshot["ok"] is True
    assert snapshot["status"] == "init"
    assert snapshot["history"] == []


def test_new_face_vision_snapshot_stays_compact_and_stream_payloads_hold_preview(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    engine_cls = _load_engine_class()
    engine = engine_cls(tmp_path / "state")
    archive = tmp_path / "frames.zip"
    _frames_zip(archive)

    load_result = engine.load_frames(str(archive), source_ref={"uri": "skill://upload/frames.zip"})
    assert load_result["ok"] is True

    first = engine.process_frame()
    second = engine.process_frame()
    assert first["frame_idx"] == 0
    assert second["frame_idx"] == 1

    snapshot = engine.snapshot()
    assert "preview_base64" not in snapshot
    assert "preview_base64" not in snapshot["latest"]
    assert snapshot["latest"]["frame_idx"] == 1
    assert snapshot["stats"]["next_frame"] == 0
    assert snapshot["files"]["frames"]["source"]["uri"] == "skill://upload/frames.zip"

    frame_payload = engine.frame_stream_payload(second)
    metrics_payload = engine.metrics_stream_payload(second)
    assert frame_payload["image"]["encoding"] == "base64"
    assert frame_payload["image"]["data"]
    assert frame_payload["image"]["src"].startswith("data:image/jpeg;base64,")
    assert metrics_payload["series"]["pred_ratio"] == second["pred_ratio"]


def test_new_face_vision_errors_are_normalized_and_projectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine_module = _load_engine_module()
    monkeypatch.setattr(engine_module, "Image", object(), raising=False)
    monkeypatch.setattr(engine_module, "np", object(), raising=False)
    engine = engine_module.NewFaceVisionEngine(tmp_path / "state")

    result = engine.process_frame()

    assert result["ok"] is False
    assert result["error"]["code"] == "frames_missing"
    assert result["error"]["message"] == "No frames loaded"
    assert result["error"]["retryable"] is False

    snapshot = engine.snapshot()
    assert snapshot["status"] == "error"
    assert snapshot["error"]["code"] == "frames_missing"
    assert snapshot["operation"]["id"] == "process_frame"
    assert snapshot["operation"]["error"]["code"] == "frames_missing"
    assert "preview_base64" not in snapshot["latest"]


SUPPORTED_CLIENT_WIDGET_TYPES = {
    "collection.grid",
    "collection.tree",
    "visual.metricTile",
    "visual.qrCode",
    "feedback.log",
    "feedback.statusBar",
    "ui.chat",
    "ui.voiceInput",
    "ui.list",
    "ui.table",
    "ui.form",
    "ui.actions",
    "ui.jsonViewer",
    "item.textEditor",
    "item.codeViewer",
    "item.details",
    "input.commandBar",
    "input.fileUpload",
    "input.text",
    "input.selector",
    "desktop.widgets",
    "visual.frameViewer",
    "visual.image",
    "visual.timeseriesChart",
    "media.videoBrowser",
    "host.webspaceControls",
}


def _collect_widget_types(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        raw_type = value.get("type")
        if isinstance(raw_type, str) and "." in raw_type and ("id" in value or "area" in value):
            out.append(raw_type)
        for nested in value.values():
            _collect_widget_types(nested, out)
    elif isinstance(value, list):
        for item in value:
            _collect_widget_types(item, out)


def test_new_face_vision_uses_only_client_supported_widget_types() -> None:
    files = [
        ROOT / ".adaos" / "workspace" / "scenarios" / "new_face_vision" / "scenario.json",
        SKILL_ROOT / "webui.json",
    ]
    unknown: dict[str, list[str]] = {}

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        found: list[str] = []
        _collect_widget_types(payload, found)
        missing = sorted({item for item in found if item not in SUPPORTED_CLIENT_WIDGET_TYPES})
        if missing:
            unknown[str(path.relative_to(ROOT))] = missing

    assert unknown == {}
