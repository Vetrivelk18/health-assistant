"""
Structured logging for Google Cloud Logging.

Cloud Run captures stdout automatically, but a plain text line arrives as an
undifferentiated blob: every entry shows up at INFO severity, and the only
way to find one user's failures is a substring search. Emitting one JSON
object per line instead gets the entry parsed into real fields — severity
becomes filterable, and anything passed via `fields=` becomes a queryable
column.

That turns the questions you actually ask during an incident into one-line
queries:

    severity>=ERROR                          -- everything that broke
    jsonPayload.user_id="abc-123"            -- one user's whole history
    jsonPayload.event="daily_summary_failed" -- which users missed today

Free tier is 5 GB/month of ingestion, which this uses a rounding error of.
The `_LOG_` field-name prefix is avoided deliberately; Cloud Logging
reserves `logging.googleapis.com/*` keys and passes everything else through
into `jsonPayload` untouched.

Local development keeps the human-readable format — JSON lines are miserable
to read in a terminal, and there's no log explorer to make up for it.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Cloud Logging's severity vocabulary happens to be a superset of Python's
# level names, so they pass straight through. The one exception is that
# Python's WARNING maps to Cloud Logging's WARNING (not WARN), which is
# already what levelname gives us.
_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, shaped the way Cloud Logging expects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        if record.exc_info:
            # Cloud Error Reporting picks up a stack trace on this key and
            # groups recurring exceptions together.
            entry["exception"] = self.formatException(record.exc_info)
            entry["stack_trace"] = self.formatException(record.exc_info)

        # Anything passed as logger.info(..., extra={"fields": {...}}) or as
        # loose extra=... kwargs lands in jsonPayload as its own field.
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "fields" and not key.startswith("_"):
                entry.setdefault(key, value)

        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO", *, structured: bool = True) -> None:
    """Install a single stdout handler. Call once, at startup.

    Replaces any handlers already present rather than adding to them —
    uvicorn installs its own, and leaving those in place double-logs every
    line, which on a metered service is real waste.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        CloudLoggingFormatter()
        if structured
        else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn's loggers propagate to root by default but ship their own
    # handlers too; drop those so each line is emitted exactly once.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Log with structured fields attached.

    `log_event(logger, logging.ERROR, "summary failed", user_id=u, reason=r)`
    emits a queryable `jsonPayload.user_id` rather than burying the id in a
    message string that can only be grepped.
    """
    logger.log(level, message, exc_info=exc_info, extra={"fields": fields})
