"""Secrets in library log lines (live finding: httpx logged the Telegram bot token in the request URL)."""

from __future__ import annotations

import logging

from reflexo.common.logging import _UrlSecretFilter, configure_logging


def test_httpx_request_logs_are_silenced_and_bot_tokens_masked() -> None:
    configure_logging("test")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    record = logging.LogRecord(
        "x",
        logging.WARNING,
        __file__,
        1,
        'POST https://api.telegram.org/bot123456:AAE-secret_Token/sendMessage "%s"',
        ("200 OK",),
        None,
    )
    _UrlSecretFilter().filter(record)
    assert record.getMessage() == 'POST https://api.telegram.org/bot***/sendMessage "200 OK"'
