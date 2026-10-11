"""Structured JSON logging with trace correlation and phone-number masking (POC_01 §6, POC_08 §3.2)."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

# +91 98xxxxxx12, 919812345612, +1 415 555 0100 ... → keep the last 4 digits
_PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d\s-]{8,15}\d(?![\w-])")


def mask_phone_numbers(text: str) -> str:
    def _mask(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 10:
            return m.group(0)
        return "***" + digits[-4:]

    return _PHONE_RE.sub(_mask, text)


def _mask_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = mask_phone_numbers(value)
    return event_dict


def _trace_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


_URL_SECRET = re.compile(r"/bot\d+:[\w-]+")  # Telegram puts the bot token in the request path


class _UrlSecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if _URL_SECRET.search(message):
            record.msg, record.args = _URL_SECRET.sub("/bot***", message), None
        return True


def configure_logging(service: str, level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # httpx logs every request URL at INFO (a Telegram URL carries the bot token); keep library logs at WARNING
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_UrlSecretFilter())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            _trace_processor,
            _mask_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
