"""
Tests for error classification, graceful degradation and structured logging.

The theme: every outbound call has to fail into a *classified* error, so the
caller can tell "retry me" from "this will never work", and a partial day of
data still produces a summary.
"""

import json
import logging
from datetime import date

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from services.gemini import GeminiError, _wrap_api_error
from services.google_health import (
    NETWORK_ERROR_STATUS,
    GoogleHealthClient,
    GoogleHealthError,
    is_total_outage,
)
from services.telegram_bot import TelegramClient, TelegramError
from utils.logging_config import CloudLoggingFormatter, log_event


# ------------------------------------------------ error classification ----

@pytest.mark.parametrize("status,transient", [
    (500, True), (502, True), (503, True),   # server side, retry
    (429, True),                              # rate limited, retry
    (NETWORK_ERROR_STATUS, True),             # never got a response, retry
    (400, False),                             # bad filter grammar — a bug
    (401, False), (403, False),               # auth/scope — needs a human
    (404, False),
])
def test_google_health_error_transience(status, transient):
    assert GoogleHealthError(status, "body", "url").is_transient is transient


@pytest.mark.asyncio
async def test_network_failure_becomes_a_google_health_error():
    """A timeout must not escape as raw httpx past every `except
    GoogleHealthError` in the app."""
    client = GoogleHealthClient("id", "secret", "uri")

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))):
        with pytest.raises(GoogleHealthError) as exc:
            await client.list_data_points("token", "steps", date(2026, 8, 21), date(2026, 8, 21))

    assert exc.value.status == NETWORK_ERROR_STATUS
    assert exc.value.is_transient


@pytest.mark.asyncio
async def test_token_refresh_network_failure_is_classified():
    client = GoogleHealthClient("id", "secret", "uri")

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ReadTimeout("slow"))):
        with pytest.raises(GoogleHealthError) as exc:
            await client.refresh("refresh-token")

    assert exc.value.is_transient


def test_gemini_error_classification():
    from google.genai import errors as genai_errors

    server = genai_errors.ServerError.__new__(genai_errors.ServerError)
    Exception.__init__(server, "boom")
    wrapped = _wrap_api_error(server)
    assert isinstance(wrapped, GeminiError)  # callers only catch GeminiError
    assert wrapped.is_transient is True

    assert _wrap_api_error(httpx.ConnectError("no route")).is_transient is True
    assert _wrap_api_error(ValueError("bad input")).is_transient is False


# ------------------------------------------ partial vs. total data loss ----

def _day(metrics=(), errors=None):
    return {
        "date": "2026-08-21",
        "metrics": {m: {"dataPoints": []} for m in metrics},
        "errors": errors or {},
    }


def test_watch_left_off_is_not_an_outage():
    """Empty-but-successful responses are real data: the user logged nothing.
    That must still produce a summary, not a retry."""
    day = _day(metrics=("sleep", "steps", "heart_rate", "active_minutes"))
    assert is_total_outage(day) is False


def test_every_type_failing_transiently_is_an_outage():
    errors = {
        m: {"status": 503, "body": "unavailable", "transient": True}
        for m in ("sleep", "steps", "heart_rate", "active_minutes")
    }
    assert is_total_outage(_day(errors=errors)) is True


def test_partial_failure_is_not_an_outage():
    """One metric down is the documented degradation path — summarise what
    did come back rather than retrying the whole user."""
    day = _day(
        metrics=("sleep", "steps"),
        errors={
            "heart_rate": {"status": 503, "body": "x", "transient": True},
            "active_minutes": {"status": 503, "body": "x", "transient": True},
        },
    )
    assert is_total_outage(day) is False


def test_permanent_failures_everywhere_is_not_a_retryable_outage():
    """All 403s means scopes are wrong — retrying five times a day forever
    won't fix it, and it shouldn't look like an outage."""
    errors = {
        m: {"status": 403, "body": "forbidden", "transient": False}
        for m in ("sleep", "steps", "heart_rate", "active_minutes")
    }
    assert is_total_outage(_day(errors=errors)) is False


def test_calories_alone_failing_is_ignored():
    """total-calories fails permanently by design (list is unsupported on
    it); counting it would make a real outage undetectable."""
    day = _day(
        metrics=("sleep", "steps", "heart_rate", "active_minutes"),
        errors={"calories": {"status": 400, "body": "unsupported", "transient": False}},
    )
    assert is_total_outage(day) is False


# ------------------------------------------------------------ telegram ----

@pytest.mark.asyncio
async def test_telegram_network_failure_becomes_telegram_error():
    client = TelegramClient("token")
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectError("down"))):
        with pytest.raises(TelegramError):
            await client.send_message("chat", "hello")


@pytest.mark.asyncio
async def test_telegram_html_error_page_becomes_telegram_error():
    """Telegram serves HTML on some gateway errors; a raw JSON decode error
    must not escape."""
    client = TelegramClient("token")
    response = httpx.Response(502, text="<html>Bad Gateway</html>")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        with pytest.raises(TelegramError) as exc:
            await client.send_message("chat", "hello")
    assert exc.value.error_code == 502


# ------------------------------------------------- structured logging ----

def test_formatter_emits_parseable_json_with_severity():
    record = logging.LogRecord("test", logging.ERROR, "f.py", 1, "it broke", None, None)
    parsed = json.loads(CloudLoggingFormatter().format(record))

    assert parsed["severity"] == "ERROR"
    assert parsed["message"] == "it broke"
    assert parsed["logger"] == "test"


def test_log_event_attaches_queryable_fields(caplog):
    logger = logging.getLogger("test.events")
    formatter = CloudLoggingFormatter()

    with caplog.at_level(logging.ERROR, logger="test.events"):
        log_event(logger, logging.ERROR, "summary failed",
                  event="summary_failed", user_id="abc-123", transient=True)

    parsed = json.loads(formatter.format(caplog.records[0]))
    assert parsed["event"] == "summary_failed"
    assert parsed["user_id"] == "abc-123"
    assert parsed["transient"] is True


def test_exception_logging_includes_stack_trace():
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            "test", logging.ERROR, "f.py", 1, "failed", None, sys.exc_info()
        )

    parsed = json.loads(CloudLoggingFormatter().format(record))
    assert "kaboom" in parsed["stack_trace"]


def test_exception_entry_is_tagged_for_error_reporting():
    """Cloud Error Reporting only ingests entries carrying this @type with
    the trace inside `message` — a trace on any other key is ignored and the
    error silently never surfaces."""
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            "test", logging.ERROR, "f.py", 1, "failed", None, sys.exc_info()
        )

    parsed = json.loads(CloudLoggingFormatter().format(record))
    assert parsed["@type"].endswith("ReportedErrorEvent")
    assert "kaboom" in parsed["message"]  # must be in message, not just alongside


def test_report_flag_surfaces_non_exception_errors(caplog):
    logger = logging.getLogger("test.report")
    with caplog.at_level(logging.ERROR, logger="test.report"):
        log_event(logger, logging.ERROR, "everything is down",
                  event="health_total_outage", report=True)

    parsed = json.loads(CloudLoggingFormatter().format(caplog.records[0]))
    assert parsed["@type"].endswith("ReportedErrorEvent")


def test_ordinary_errors_are_not_reported(caplog):
    """An expired refresh token is expected operationally — it must not page
    anyone through Error Reporting."""
    logger = logging.getLogger("test.report")
    with caplog.at_level(logging.ERROR, logger="test.report"):
        log_event(logger, logging.ERROR, "token expired",
                  event="summary_failed", user_id="u-1", transient=False)

    parsed = json.loads(CloudLoggingFormatter().format(caplog.records[0]))
    assert "@type" not in parsed
