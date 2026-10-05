"""
Condense raw Google Health responses into the small digest Gemini is given.

The raw responses are far too big to hand to a model: one day of heart rate
alone is ~20 MB. Sleep comes from `list` and already carries Google's own
per-night summary; every other type comes from `dailyRollUp` (see
ROLLUP_TYPES in services/google_health.py) and is one aggregate per civil
day. So this module only picks fields out and reshapes them — no arithmetic
over raw samples, which keeps the numbers exactly what Google reports.

Every metric digests to a list of small dicts, each carrying the local date
it belongs to, so a one-day summary and a multi-day question share one shape:

    {"date": "2026-10-04", "steps": 6686}

Pure functions, no I/O. Anything missing or malformed is skipped rather than
raised: a summary with one metric short beats no summary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def digest_day(day_data: dict[str, Any]) -> dict[str, Any]:
    """fetch_day's output with each metric's raw response replaced by its digest.

    `date` and `errors` pass through untouched, so is_total_outage and the
    partial-data logging still see what they expect.
    """
    return {
        "date": day_data.get("date"),
        "metrics": {
            friendly: digest_metric(friendly, raw)
            for friendly, raw in (day_data.get("metrics") or {}).items()
        },
        "errors": day_data.get("errors") or {},
    }


def digest_metric(friendly: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Digest one metric's raw response (as returned by fetch_metric)."""
    digester = _DIGESTERS.get(friendly)
    if digester is None:
        return []

    out = []
    points = (raw or {}).get("rollupDataPoints") or (raw or {}).get("dataPoints") or []
    for point in points:
        try:
            entry = digester(point)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Skipping malformed %s point: %s", friendly, e)
            continue
        if entry:
            out.append(entry)
    return out


# ------------------------------------------------------------- per type ----

def _sleep(point: dict[str, Any]) -> dict[str, Any] | None:
    sleep = point["sleep"]
    interval = sleep["interval"]
    summary = sleep.get("summary") or {}
    start = _local(interval["startTime"], interval.get("startUtcOffset"))
    end = _local(interval["endTime"], interval.get("endUtcOffset"))

    return {
        # A night belongs to the day you wake up on.
        "date": end.date().isoformat(),
        "bedtime": start.strftime("%H:%M"),
        "wake_time": end.strftime("%H:%M"),
        "main_sleep": bool((sleep.get("metadata") or {}).get("mainSleep")),
        "minutes_asleep": _int(summary.get("minutesAsleep")),
        "minutes_awake": _int(summary.get("minutesAwake")),
        "minutes_in_bed": _int(summary.get("minutesInSleepPeriod")),
        "stage_minutes": {
            s["type"].lower(): _int(s.get("minutes"))
            for s in summary.get("stagesSummary") or []
            if s.get("type")
        },
    }


def _steps(point: dict[str, Any]) -> dict[str, Any]:
    return {"date": _civil_date(point), "steps": _int(point["steps"]["countSum"])}


def _heart_rate(point: dict[str, Any]) -> dict[str, Any] | None:
    hr = point["heartRate"]
    if "beatsPerMinuteAvg" not in hr:
        return None  # no readings that day (watch off)
    return {
        "date": _civil_date(point),
        "avg_bpm": round(float(hr["beatsPerMinuteAvg"])),
        "min_bpm": _int(hr.get("beatsPerMinuteMin")),
        "max_bpm": _int(hr.get("beatsPerMinuteMax")),
    }


def _active_minutes(point: dict[str, Any]) -> dict[str, Any]:
    levels = {
        lvl["activityLevel"].lower(): _int(lvl.get("activeMinutesSum"))
        for lvl in point["activeMinutes"].get("activeMinutesRollupByActivityLevel") or []
        if lvl.get("activityLevel")
    }
    return {"date": _civil_date(point), "total": sum(levels.values()), "by_level": levels}


def _calories(point: dict[str, Any]) -> dict[str, Any]:
    return {"date": _civil_date(point), "kcal": round(float(point["totalCalories"]["kcalSum"]))}


_DIGESTERS = {
    "sleep": _sleep,
    "steps": _steps,
    "heart_rate": _heart_rate,
    "active_minutes": _active_minutes,
    "calories": _calories,
}


# ------------------------------------------------------------- helpers ----

def _civil_date(point: dict[str, Any]) -> str:
    d = point["civilStartTime"]["date"]
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _local(utc_timestamp: str, offset: str | None) -> datetime:
    """A UTC RFC 3339 timestamp shifted by the API's "19800s"-style offset."""
    utc = datetime.fromisoformat(utc_timestamp.replace("Z", "+00:00"))
    seconds = int((offset or "0s").rstrip("s"))
    return (utc + timedelta(seconds=seconds)).replace(tzinfo=None)


def _int(value: Any) -> int:
    # The API sends int64 fields as strings ("6686").
    return int(value) if value not in (None, "") else 0
