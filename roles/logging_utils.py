"""Structured logging: a request-id filter and a JSON formatter."""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from contextvars import ContextVar

# ContextVar rather than threading.local: correct under both gunicorn's sync
# workers and any future async worker, and it is what Django's own async-safe
# code uses.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes LogRecord always carries; anything else the caller passed via
# `extra=` is application context and is worth emitting.
_STANDARD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName", "request_id",
}


def set_request_id(value: str) -> object:
    """Bind the id for the current context. Returns a token for reset()."""
    return _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


def reset_request_id(token) -> None:
    try:
        _request_id.reset(token)
    except ValueError:  # pragma: no cover - token from another context
        _request_id.set("-")


class RequestIDFilter(logging.Filter):
    """Attaches the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line, for the droplet's log shipper to ingest."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            payload["error"] = {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc_value),
                "stack": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            }

        return json.dumps(payload, ensure_ascii=False, default=str)
