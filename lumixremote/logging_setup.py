"""Structured logging: one JSON object per line on stdout.

Every line carries a time stamp (local time per TZ, with its offset), the
level and a short, stable event name; the details are fields of their own,
passed as `extra={...}`. That keeps the log searchable and lets a bug report
quote exact fields. Session IDs are never logged.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # Local time with UTC offset; TZ selects the zone, UTC prints as Z.
            "ts": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        # Anything passed via log.info(..., extra={...}) becomes a field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure(level: str = "INFO") -> None:
    """Install the JSON formatter as the only handler, for every logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
