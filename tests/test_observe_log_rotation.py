from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

from adaos.services import observe


def test_write_local_rotates_events_log(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "events.log"
    log_path.write_text("x" * 20, encoding="utf-8")

    monkeypatch.setattr(observe, "_LOG_FILE", log_path)
    monkeypatch.setattr(observe, "_MAX_BYTES", 10)
    monkeypatch.setattr(observe, "_KEEP", 3)

    observe._write_local({"topic": "test.topic", "payload": {"ok": True}})

    rotated = log_path.with_suffix(".log.1.gz")
    assert rotated.exists()
    with gzip.open(rotated, "rt", encoding="utf-8") as fh:
        assert fh.read() == "x" * 20

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"topic": "test.topic", "payload": {"ok": True}}


def test_event_broadcaster_unsubscribe_removes_and_drains_queue() -> None:
    broadcaster = observe.EventBroadcaster()
    q = broadcaster.subscribe(topic_prefix=None, node_id=None, since_ts=None)

    asyncio.run(broadcaster.publish({"topic": "test.topic", "payload": {"ok": True}}))
    assert q.qsize() == 1

    assert broadcaster.unsubscribe(q) is True
    assert broadcaster.stats()["subscribers"] == 0
    assert q.qsize() == 0

    asyncio.run(broadcaster.publish({"topic": "test.topic", "payload": {"ok": False}}))
    assert q.qsize() == 0


def test_event_broadcaster_caps_stale_subscribers(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_OBSERVE_BROADCAST_MAX_SUBSCRIBERS", "2")
    broadcaster = observe.EventBroadcaster()

    first = broadcaster.subscribe(topic_prefix=None, node_id=None, since_ts=None)
    broadcaster.subscribe(topic_prefix=None, node_id=None, since_ts=None)
    broadcaster.subscribe(topic_prefix=None, node_id=None, since_ts=None)

    assert broadcaster.stats()["subscribers"] == 2
    assert broadcaster.stats()["dropped_subscribers"] == 1
    assert first.qsize() == 0
