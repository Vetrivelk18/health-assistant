"""
Tests for the digest Gemini is given, and the rollup/paging fetches behind it.

Samples mirror real response shapes captured by probe_health_api.py on
2026-10-02/05 (values changed). fixtures/ itself is git-ignored and holds
personal data, so the shapes are restated here rather than loaded.
"""

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.google_health import DATA_TYPES, GoogleHealthClient
from services.health_digest import digest_day, digest_metric


def _civil(y, m, d):
    return {"date": {"year": y, "month": m, "day": d}, "time": {}}


def _rollup(day, **value):
    return {"civilStartTime": _civil(2026, 10, day), "civilEndTime": _civil(2026, 10, day + 1), **value}


SLEEP = {"dataPoints": [{
    "sleep": {
        "interval": {
            "startTime": "2026-10-03T23:20:00Z", "startUtcOffset": "19800s",
            "endTime": "2026-10-04T05:15:00Z", "endUtcOffset": "19800s",
        },
        "metadata": {"mainSleep": True},
        "summary": {
            "minutesInSleepPeriod": "355", "minutesAsleep": "344", "minutesAwake": "11",
            "stagesSummary": [
                {"type": "AWAKE", "minutes": "11", "count": "2"},
                {"type": "LIGHT", "minutes": "187", "count": "11"},
                {"type": "DEEP", "minutes": "77", "count": "3"},
                {"type": "REM", "minutes": "80", "count": "7"},
            ],
        },
        "stages": [{"type": "LIGHT"}] * 23,  # dropped by the digest
    },
}]}

STEPS = {"rollupDataPoints": [_rollup(3, steps={"countSum": "6673"}),
                              _rollup(4, steps={"countSum": "6686"})]}
HEART = {"rollupDataPoints": [_rollup(4, heartRate={
    "beatsPerMinuteAvg": 73.36, "beatsPerMinuteMax": 143, "beatsPerMinuteMin": 46})]}
ACTIVE = {"rollupDataPoints": [_rollup(4, activeMinutes={"activeMinutesRollupByActivityLevel": [
    {"activityLevel": "LIGHT", "activeMinutesSum": "278"},
    {"activityLevel": "MODERATE", "activeMinutesSum": "32"},
    {"activityLevel": "VIGOROUS", "activeMinutesSum": "28"},
]})]}
CALORIES = {"rollupDataPoints": [_rollup(4, totalCalories={"kcalSum": 2595.312813})]}


# ------------------------------------------------------------- digests ----

def test_sleep_uses_googles_summary_in_local_time():
    [night] = digest_metric("sleep", SLEEP)
    assert night == {
        "date": "2026-10-04",          # the day you wake up on, local time
        "bedtime": "04:50",            # 23:20Z + 5:30
        "wake_time": "10:45",
        "main_sleep": True,
        "minutes_asleep": 344,
        "minutes_awake": 11,
        "minutes_in_bed": 355,
        "stage_minutes": {"awake": 11, "light": 187, "deep": 77, "rem": 80},
    }


def test_rollup_metrics_digest_to_one_entry_per_day():
    assert digest_metric("steps", STEPS) == [
        {"date": "2026-10-03", "steps": 6673},
        {"date": "2026-10-04", "steps": 6686},
    ]
    assert digest_metric("heart_rate", HEART) == [
        {"date": "2026-10-04", "avg_bpm": 73, "min_bpm": 46, "max_bpm": 143}
    ]
    assert digest_metric("active_minutes", ACTIVE) == [{
        "date": "2026-10-04", "total": 338,
        "by_level": {"light": 278, "moderate": 32, "vigorous": 28},
    }]
    assert digest_metric("calories", CALORIES) == [{"date": "2026-10-04", "kcal": 2595}]


def test_day_with_no_readings_is_skipped_not_zeroed():
    """A watch left off still gets a rollup point per day, just with no
    aggregate. Reporting that as 0 bpm would be invented data."""
    empty = {"rollupDataPoints": [_rollup(4, heartRate={})]}
    assert digest_metric("heart_rate", empty) == []


@pytest.mark.parametrize("raw", [{}, {"dataPoints": []}, {"rollupDataPoints": []}, None])
def test_empty_responses_digest_to_nothing(raw):
    for metric in DATA_TYPES:
        assert digest_metric(metric, raw) == []


def test_malformed_point_is_skipped_without_losing_the_rest():
    raw = {"rollupDataPoints": [{"unexpected": True}, _rollup(4, steps={"countSum": "10"})]}
    assert digest_metric("steps", raw) == [{"date": "2026-10-04", "steps": 10}]


def test_digest_day_is_small_and_keeps_errors():
    errors = {"calories": {"status": 503, "body": "x", "transient": True}}
    day = {
        "date": "2026-10-04",
        "metrics": {"sleep": SLEEP, "steps": STEPS, "heart_rate": HEART, "active_minutes": ACTIVE},
        "errors": errors,
    }
    digest = digest_day(day)

    assert digest["date"] == "2026-10-04"
    assert digest["errors"] == errors     # is_total_outage / logging still see it
    assert set(digest["metrics"]) == {"sleep", "steps", "heart_rate", "active_minutes"}
    assert len(json.dumps(digest)) < 2000  # a few hundred tokens, not megabytes


# ------------------------------------------------- rollup / list fetches ----

def _response(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


@pytest.mark.asyncio
async def test_rollup_types_use_daily_rollup_and_sleep_uses_list():
    client = GoogleHealthClient("id", "secret", "uri")
    post = AsyncMock(return_value=_response({"rollupDataPoints": []}))
    get = AsyncMock(return_value=_response({"dataPoints": []}))

    with patch("httpx.AsyncClient.post", new=post), patch("httpx.AsyncClient.get", new=get):
        for metric in DATA_TYPES:
            await client.fetch_metric("token", metric, date(2026, 10, 4), date(2026, 10, 4))

    assert get.await_count == 1  # sleep
    assert post.await_count == 4
    rolled = {c.args[0].split("/dataTypes/")[1].split("/")[0] for c in post.await_args_list}
    assert rolled == {"steps", "heart-rate", "active-minutes", "total-calories"}


@pytest.mark.asyncio
async def test_rollup_range_is_civil_days_end_exclusive():
    client = GoogleHealthClient("id", "secret", "uri")
    post = AsyncMock(return_value=_response({"rollupDataPoints": []}))

    with patch("httpx.AsyncClient.post", new=post):
        await client.daily_rollup("token", "steps", date(2026, 10, 4), date(2026, 10, 4))

    body = post.await_args.kwargs["json"]
    assert body["range"]["start"]["date"] == {"year": 2026, "month": 10, "day": 4}
    assert body["range"]["end"]["date"] == {"year": 2026, "month": 10, "day": 5}
    assert body["windowSizeDays"] == 1


@pytest.mark.asyncio
async def test_long_rollup_range_is_split_into_14_day_requests():
    client = GoogleHealthClient("id", "secret", "uri")
    post = AsyncMock(side_effect=[
        _response({"rollupDataPoints": [_rollup(1, steps={"countSum": "1"})]}),
        _response({"rollupDataPoints": [_rollup(15, steps={"countSum": "2"})]}),
    ])

    with patch("httpx.AsyncClient.post", new=post):
        result = await client.daily_rollup("token", "steps", date(2026, 10, 1), date(2026, 10, 20))

    assert post.await_count == 2
    second = post.await_args_list[1].kwargs["json"]["range"]
    assert second["start"]["date"]["day"] == 15
    assert second["end"]["date"]["day"] == 21
    assert len(result["rollupDataPoints"]) == 2


@pytest.mark.asyncio
async def test_list_follows_page_tokens_up_to_the_cap():
    client = GoogleHealthClient("id", "secret", "uri")
    page = _response({"dataPoints": [{"n": 1}], "nextPageToken": "more"})
    last = _response({"dataPoints": [{"n": 2}]})

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[page, last])):
        result = await client.list_data_points("token", "sleep", date(2026, 10, 4), date(2026, 10, 4))
    assert result["dataPoints"] == [{"n": 1}, {"n": 2}]
    assert "nextPageToken" not in result

    endless = AsyncMock(return_value=_response({"dataPoints": [{"n": 1}], "nextPageToken": "more"}))
    with patch("httpx.AsyncClient.get", new=endless):
        result = await client.list_data_points(
            "token", "sleep", date(2026, 10, 4), date(2026, 10, 4), max_pages=3
        )
    assert endless.await_count == 3
    assert result["nextPageToken"] == "more"  # truncation stays visible


@pytest.mark.asyncio
async def test_query_tool_returns_the_digest_not_raw_data():
    from services.gemini import _run_tool

    health = MagicMock()
    health.fetch_metric = AsyncMock(return_value=STEPS)
    result = await _run_tool({"metric": "steps", "start_date": "2026-10-03", "end_date": "2026-10-04"},
                             "token", health)

    assert result["is_error"] is False
    assert json.loads(result["content"]) == [
        {"date": "2026-10-03", "steps": 6673},
        {"date": "2026-10-04", "steps": 6686},
    ]
