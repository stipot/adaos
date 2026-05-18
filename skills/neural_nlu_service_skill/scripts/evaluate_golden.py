from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from handlers.upstream_detector_port import Detector


DEFAULT_CASES: list[dict[str, str]] = [
    {"id": "weather.moscow", "text": "какая погода в москве", "expected_intent": "weather.get"},
    {"id": "timer.ten_minutes", "text": "поставь таймер на 10 минут", "expected_intent": "timer.start"},
    {"id": "alarm.morning", "text": "разбуди меня в 7:30", "expected_intent": "alarm.set"},
    {"id": "music.play", "text": "включи музыку", "expected_intent": "music.play"},
    {"id": "time.now", "text": "сколько времени", "expected_intent": "time.now"},
]


def _default_artifact_root() -> Path:
    base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
    if base_dir:
        return Path(base_dir).expanduser().resolve() / "state" / "nlu" / "neural"
    return Path.home() / ".adaos" / "state" / "nlu" / "neural"


def _load_cases(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return [dict(item) for item in DEFAULT_CASES]
    cases: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        expected = str(item.get("expected_intent") or item.get("intent") or "").strip()
        if text and expected:
            cases.append(
                {
                    "id": str(item.get("id") or f"case-{len(cases) + 1}"),
                    "text": text,
                    "expected_intent": expected,
                }
            )
    return cases


def evaluate_cases(detector: Any, cases: Iterable[dict[str, str]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        result = detector.detect(str(case.get("text") or ""), webspace_id="desktop", locale="ru")
        latency_ms = (time.perf_counter() - started) * 1000.0
        intent = str(result.get("top_intent") or result.get("intent") or "")
        expected = str(case.get("expected_intent") or "")
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        rows.append(
            {
                "id": str(case.get("id") or ""),
                "text": str(case.get("text") or ""),
                "expected_intent": expected,
                "top_intent": intent,
                "passed": bool(intent == expected),
                "confidence": float(result.get("confidence") or 0.0),
                "latency_ms": round(latency_ms, 3),
                "slots": dict(result.get("slots") or {}) if isinstance(result.get("slots"), dict) else {},
                "backend": evidence.get("backend"),
                "ranker": evidence.get("ranker"),
                "example_index": evidence.get("example_index"),
            }
        )
    total = len(rows)
    passed = sum(1 for row in rows if row["passed"])
    return {
        "schema_version": 1,
        "created_at": time.time(),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": round((passed / total) if total else 0.0, 6),
        "cases": rows,
        "health": detector.health() if hasattr(detector, "health") else {},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate Neural NLU active artifacts against golden phrases.")
    parser.add_argument("--cases", type=Path, default=None, help="JSONL with text and expected_intent.")
    parser.add_argument("--out", type=Path, default=None, help="Report path. Defaults to artifact root golden_report.json.")
    parser.add_argument("--min-accuracy", type=float, default=0.0, help="Exit non-zero if accuracy is below this value.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases = _load_cases(args.cases)
    detector = Detector()
    report = evaluate_cases(detector, cases)
    out_path = args.out or (_default_artifact_root() / "golden_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["accuracy"] >= float(args.min_accuracy), "report": str(out_path), **report}, ensure_ascii=False, indent=2))
    return 0 if report["accuracy"] >= float(args.min_accuracy) else 2


if __name__ == "__main__":
    raise SystemExit(main())
