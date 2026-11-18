"""Logging configuration: plain text for development, JSON lines for production.

Engine modules log with `extra={...}` (conversation_id, routing, latency, ...);
the JSON formatter emits those keys as top-level fields so a log pipeline can
index them. Message bodies are never logged — only ids, lengths, and decisions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging(level: str = "INFO", *, json_lines: bool = False) -> None:
    root = logging.getLogger("sms_engine")
    root.setLevel(level.upper())
    if any(getattr(h, "_sms_engine", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler._sms_engine = True  # type: ignore[attr-defined]
    handler.setFormatter(
        JsonFormatter() if json_lines
        else KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
