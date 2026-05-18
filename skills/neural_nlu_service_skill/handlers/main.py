from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .upstream_detector_port import Detector
except ImportError:  # pragma: no cover - direct-file validation/import fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from upstream_detector_port import Detector

_DETECTOR = Detector()


class Handler(BaseHTTPRequestHandler):
    server_version = "AdaOSNeuralNLU/0.2"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: Any) -> None:  # noqa: A003
        if os.getenv("ADAOS_NEURAL_HTTP_LOG", "0").strip().lower() in {"1", "true", "yes", "on"}:
            super().log_message(_format, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, _DETECTOR.health())
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/parse":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        raw = self.rfile.read(max(length, 0)) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            payload = {}

        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            self._json(400, {"ok": False, "error": "text_required"})
            return

        result = _DETECTOR.detect(
            text=text.strip(),
            webspace_id=payload.get("webspace_id") if isinstance(payload, dict) else None,
            locale=payload.get("locale") if isinstance(payload, dict) else None,
            canonicalized_text=payload.get("canonicalized_text") if isinstance(payload, dict) else None,
            entity_resolution=payload.get("entities") if isinstance(payload, dict) else None,
        )
        response = {"ok": True, **result, "result": result}
        self._json(200, response)


if __name__ == "__main__":
    host = os.getenv("ADAOS_SERVICE_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("ADAOS_SERVICE_PORT", "18091") or "18091")
    except Exception:
        port = 18091
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()
