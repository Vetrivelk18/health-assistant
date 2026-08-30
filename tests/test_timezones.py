"""
Tests for timezone resolution (utils/timezones.py) and the per-user delivery
hour gating in the daily dispatcher.

The theme: a timezone left at the UTC default means summaries cover the
wrong calendar day AND arrive at the wrong hour, so both halves matter.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import routes.internal as internal_module
from models import User
from utils import timezones


# ------------------------------------------------------------ resolution ----

@pytest.mark.parametrize("query,expected", [
    ("Asia/Kolkata", "Asia/Kolkata"),        # exact
    ("asia/kolkata", "Asia/Kolkata"),        # case-insensitive
    ("ASIA/KOLKATA", "Asia/Kolkata"),
    ("Kolkata", "Asia/Kolkata"),             # bare city, unambiguous
    ("kolkata", "Asia/Kolkata"),
    ("Europe/London", "Europe/London"),
    ("Tokyo", "Asia/Tokyo"),
])
def test_recognised_inputs_resolve(query, expected):
    resolved, suggestions = timezones.resolve(query)
    assert resolved == expected
    assert suggestions == []


def test_city_with_underscores_resolves_from_spaces():
    """Nobody types 'New_York'."""
    resolved, _ = timezones.resolve("new york")
    assert resolved == "America/New_York"


def test_ambiguous_abbreviation_is_never_guessed():
    """IST is India, Ireland and Israel. Silently picking one would put the
    user's whole day boundary in the wrong place."""
    resolved, suggestions = timezones.resolve("IST")
    assert resolved is None


def test_ambiguous_input_offers_choices_instead_of_picking():
    """'Argentina' names a dozen zones — offer them rather than choose."""
    resolved, suggestions = timezones.resolve("Argentina")
    assert resolved is None
    assert len(suggestions) > 1
    assert all(s.startswith("America/Argentina/") for s in suggestions)


def test_a_real_zone_named_like_an_abbreviation_still_resolves():
    """GMT is an actual IANA zone name, so it should resolve exactly rather
    than being treated as an ambiguous abbreviation."""
    resolved, suggestions = timezones.resolve("GMT")
    assert resolved == "GMT"
    assert suggestions == []


def test_unknown_input_resolves_to_nothing():
    resolved, suggestions = timezones.resolve("Mars/Olympus_Mons")
    assert resolved is None
    assert suggestions == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_input_is_handled(blank):
    assert timezones.resolve(blank) == (None, [])


def test_is_valid():
    assert timezones.is_valid("Asia/Kolkata")
    assert not timezones.is_valid("Not/A_Zone")


def test_suggestions_are_capped():
    """A wall of 200 zones is not a useful reply in a chat window."""
    _, suggestions = timezones.resolve("America")
    assert len(suggestions) <= timezones.MAX_SUGGESTIONS


# -------------------------------------------------------- delivery hour ----

def _at(hour_utc: int):
    """Freeze 'now' so the local-hour check is deterministic."""
    fixed = datetime(2026, 8, 30, hour_utc, 0, tzinfo=ZoneInfo("UTC"))
    return patch(
        "routes.internal.datetime",
        **{"now.side_effect": lambda tz=None: fixed.astimezone(tz) if tz else fixed},
    )


def test_utc_user_is_due_at_their_own_hour():
    user = User(timezone="UTC", summary_hour=7)
    with _at(7):
        assert internal_module._is_delivery_hour(user) is True
    with _at(8):
        assert internal_module._is_delivery_hour(user) is False


def test_ist_user_is_due_at_0130_utc_not_0700_utc():
    """The whole point: 07:00 IST is 01:30 UTC. Under the old single daily
    job at 07:00 UTC this user got 'good morning' at 12:30 in the afternoon."""
    user = User(timezone="Asia/Kolkata", summary_hour=7)

    with _at(1):        # 06:30 IST — not yet
        assert internal_module._is_delivery_hour(user) is False
    with _at(2):        # 07:30 IST — hour 7 locally
        assert internal_module._is_delivery_hour(user) is True
    with _at(7):        # 12:30 IST — the old delivery time
        assert internal_module._is_delivery_hour(user) is False


def test_user_west_of_utc():
    user = User(timezone="America/New_York", summary_hour=7)
    with _at(11):       # 07:00 EDT
        assert internal_module._is_delivery_hour(user) is True
    with _at(7):
        assert internal_module._is_delivery_hour(user) is False


def test_invalid_timezone_falls_back_to_utc():
    user = User(timezone="Not/A_Zone", summary_hour=7)
    with _at(7):
        assert internal_module._is_delivery_hour(user) is True


def test_missing_summary_hour_defaults_to_seven():
    """Rows predating the column being used have it NULL."""
    user = User(timezone="UTC", summary_hour=None)
    with _at(7):
        assert internal_module._is_delivery_hour(user) is True


def test_short_queries_match_word_starts_not_substrings():
    """A plain substring test suggests Brazil to someone typing IST, because
    'ist' appears inside 'Boa_Vista'."""
    _, suggestions = timezones.resolve("IST")
    assert suggestions
    assert all("Istanbul" in s for s in suggestions)
    assert not any("Vista" in s for s in suggestions)


def test_partial_city_still_suggests():
    _, suggestions = timezones.resolve("lond")
    assert "Europe/London" in suggestions
