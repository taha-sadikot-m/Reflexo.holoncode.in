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


def test_access_log_query_secrets_are_masked_without_breaking_the_args() -> None:
    """Live 13 Oct: uvicorn's access log printed Meta's hub.verify_token; signed media links carry sig=."""
    configure_logging("test")
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "1.2.3.4:0",
            "GET",
            "/webhooks/meta_whatsapp?hub.mode=subscribe&hub.verify_token=s3cret_v4lue&hub.challenge=42",
            "1.1",
            200,
        ),
        None,
    )
    assert logging.getLogger("uvicorn.access").filter(record)
    client, method, path, _, status = record.args  # type: ignore[misc]  # still unpackable for uvicorn
    assert (client, method, status) == ("1.2.3.4:0", "GET", 200)
    assert "s3cret_v4lue" not in record.getMessage()
    assert "hub.verify_token=***" in path and "hub.challenge=42" in path
    media = logging.LogRecord("x", logging.INFO, __file__, 1, "GET /media/abc?exp=99&sig=deadbeef", None, None)
    assert logging.getLogger("uvicorn.access").filter(media) and "deadbeef" not in media.getMessage()
