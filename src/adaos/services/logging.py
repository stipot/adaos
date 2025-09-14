from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from adaos.domain import Event
from adaos.ports.paths import PathProvider
from adaos.ports import EventBus


def _json_formatter(record: logging.LogRecord) -> str:
    base = {
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
        "time": getattr(record, "asctime", None),
    }
    if hasattr(record, "extra"):
        try:
            base.update(record.extra)  # type: ignore[attr-defined]
        except Exception:
            pass
    return json.dumps(base, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _json_formatter(record)


def setup_logging(paths: PathProvider, level: str = "INFO") -> logging.Logger:
    """
    Настройка логов:
      - консоль (stderr)
      - файл {logs_dir}/adaos.log (ротация)
    JSON формат, чтобы легко парсить.
    """
    logs_dir = Path(paths.logs_dir())
    logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / "adaos.log"

    logger = logging.getLogger("adaos")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(JsonFormatter())
    stream_h.setLevel(logger.level)

    file_h = RotatingFileHandler(logfile, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(JsonFormatter())
    file_h.setLevel(logger.level)

    logger.addHandler(stream_h)
    logger.addHandler(file_h)
    logger.propagate = False
    logger.info("logging.initialized", extra={"extra": {"logfile": str(logfile)}})
    return logger


def attach_event_logger(bus: EventBus, logger: Optional[logging.Logger] = None) -> None:
    """
    Подписывает логгер на все события шины.
    """
    base_logger = logger or logging.getLogger("adaos.events")

    def _handler(ev: Event) -> None:
        iso_time = datetime.fromtimestamp(getattr(ev, "ts", 0), tz=timezone.utc).isoformat() if getattr(ev, "ts", None) else None
        base_logger.info(
            "event",
            extra={
                "extra": {
                    "time": iso_time,
                    "type": ev.type,
                    "source": ev.source,
                    "ts": ev.ts,
                    "payload": ev.payload,
                }
            },
        )

    bus.subscribe("", _handler)
