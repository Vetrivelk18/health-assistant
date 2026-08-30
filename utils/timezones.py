"""
Resolving what a person types into an IANA timezone name.

IANA names are the only correct way to store a timezone — a fixed UTC offset
silently breaks twice a year wherever DST applies — but they're an awkward
thing to ask someone to type. "Asia/Kolkata" is not what anyone calls where
they live.

So this accepts the forms people actually reach for ("kolkata", "asia/
kolkata", "London") and resolves them, while refusing to *guess* when the
input is genuinely ambiguous. Abbreviations are the important case: "IST" is
India, Ireland and Israel, and picking one silently would put a user's whole
day-boundary in the wrong place. Better to show the candidates and let them
choose.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

MAX_SUGGESTIONS = 8


def is_valid(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _city_of(zone: str) -> str:
    """'Asia/Kolkata' -> 'kolkata'. The part people actually recognise."""
    return zone.rsplit("/", 1)[-1].replace("_", " ").lower()


def resolve(query: str) -> tuple[str | None, list[str]]:
    """Turn user input into an IANA name.

    Returns (resolved, suggestions). Exactly one of them is meaningful:
    a resolved name means we're confident, and suggestions are only
    populated when we're not — so the caller never has to guess either.
    """
    query = (query or "").strip()
    if not query:
        return None, []

    zones = available_timezones()

    # Exact, then case-insensitive exact. "asia/kolkata" is unambiguous.
    if query in zones:
        return query, []
    lowered = {z.lower(): z for z in zones}
    if query.lower() in lowered:
        return lowered[query.lower()], []

    needle = query.replace("_", " ").lower()

    # A city name that matches exactly one zone is unambiguous enough to
    # accept — "kolkata" can only mean Asia/Kolkata.
    city_matches = sorted(z for z in zones if _city_of(z) == needle)
    if len(city_matches) == 1:
        return city_matches[0], []
    if city_matches:
        return None, city_matches[:MAX_SUGGESTIONS]

    # Otherwise suggest, matching only at word starts. A plain substring
    # test is far too loose on short inputs — "IST" appears inside
    # "Boa_Vista", so it would suggest Brazil to someone in India.
    # Deliberately no abbreviation table either: "IST" is India, Ireland and
    # Israel, and silently choosing one misplaces every day boundary.
    partial = sorted(
        z for z in zones
        if any(word.startswith(needle) for word in _words(z))
    )
    return None, partial[:MAX_SUGGESTIONS]


def _words(zone: str) -> list[str]:
    """'America/Argentina/Buenos_Aires' -> ['america','argentina','buenos','aires']"""
    return zone.replace("/", " ").replace("_", " ").lower().split()
