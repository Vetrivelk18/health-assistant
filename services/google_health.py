"""
Google Health API client  (health.googleapis.com, v4)

Replaces the earlier module, which targeted the Google Fit REST API
(fitness.googleapis.com). That API has been closed to new developer sign-ups
since 1 May 2024 and is being deprecated in 2026 — a new Google Cloud project
cannot use it. The Google Health API is the supported successor and is also
the migration target for the Fitbit Web API, which sunsets September 2026.

Docs
  Service endpoint : https://health.googleapis.com
  List data points : GET /v4/users/me/dataTypes/{dataType}/dataPoints
  Reference        : https://developers.google.com/health/reference/rest

Filter grammar — confirmed against a real account via probe_health_api.py
(2026-08-22), and it is NOT one shared grammar: each data type restricts on
a different member path, always prefixed with the type's own (snake_case)
name — not its kebab-case URL segment, and not camelCase:

  sleep           sleep.interval.end_time
  steps           steps.interval.start_time
  heart-rate      heart_rate.sample_time.physical_time
  active-minutes  active_minutes.interval.start_time

`FILTER_TEMPLATE_BY_TYPE` below holds the confirmed template per type, keyed
by the kebab-case URL segment. `total-calories` only supports `rollup`/
`dailyRollUp` — `list` 400s on it (UNSUPPORTED_DATA_TYPE_ACTION).

Daily rollups
  POST /v4/users/me/dataTypes/{dataType}/dataPoints:dailyRollUp
  body {"range": {"start": CivilDateTime, "end": CivilDateTime},
        "windowSizeDays": 1}           (end exclusive, max 14 days)
Used for every type in ROLLUP_TYPES — see the comment there for why. Each
rollup point carries a civil day and one aggregate, e.g.
  steps          {"countSum": "6686"}
  heart-rate     {"beatsPerMinuteAvg": 73.4, "beatsPerMinuteMax": 143,
                  "beatsPerMinuteMin": 46}
  active-minutes {"activeMinutesRollupByActivityLevel":
                  [{"activityLevel": "LIGHT", "activeMinutesSum": "278"}, …]}
  total-calories {"kcalSum": 2595.3}
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

HEALTH_BASE = "https://health.googleapis.com/v4"

# The googlehealth.* scopes are Restricted. Request only what the product
# actually reads.
#
# `openid` and `email` are non-sensitive and add no verification burden;
# they're what make the token response carry an id_token, which is how we
# learn the user's real Google account id and address. Without them the
# only identity available is the Telegram chat id, and the users table has
# unique constraints on both google_user_id and email.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

# Friendly metric name -> kebab-case dataType path segment.
DATA_TYPES = {
    "sleep": "sleep",
    "steps": "steps",
    "heart_rate": "heart-rate",
    "active_minutes": "active-minutes",
    "calories": "total-calories",
}

# pageSize ceilings differ by type: sleep and exercise cap at 25.
PAGE_SIZE = {
    "sleep": 25,
    "steps": 1440,
    "heart-rate": 1440,
    "active-minutes": 1440,
    "total-calories": 1440,
}

# Confirmed filter grammar per type — see module docstring. Keyed by the
# kebab-case dataType URL segment (DATA_TYPES' values).
FILTER_TEMPLATE_BY_TYPE: dict[str, str] = {
    "sleep": 'sleep.interval.end_time >= "{start_rfc}" '
             'AND sleep.interval.end_time < "{end_rfc}"',
    "steps": 'steps.interval.start_time >= "{start_rfc}" '
             'AND steps.interval.start_time < "{end_rfc}"',
    "heart-rate": 'heart_rate.sample_time.physical_time >= "{start_rfc}" '
                  'AND heart_rate.sample_time.physical_time < "{end_rfc}"',
    "active-minutes": 'active_minutes.interval.start_time >= "{start_rfc}" '
                       'AND active_minutes.interval.start_time < "{end_rfc}"',
}


# Types read through `dailyRollUp` rather than `list`. total-calories rejects
# `list` outright. The other three are rolled up because the raw stream is
# unusable for a daily summary: heart rate samples every ~3 s (30k+ points,
# ~20 MB a day), and steps arrive from every connected device — a Fitbit and
# an iPhone each report the same walk, so summing raw points double-counts
# while the rollup merges them. The rollup also buckets by the user's civil
# day rather than UTC midnight. Confirmed against a real account 2026-10-02.
# Sleep is the exception: dailyRollUp 400s on it, and its list response
# already carries Google's own per-night summary.
ROLLUP_TYPES = {"steps", "heart-rate", "active-minutes", "total-calories"}

# dailyRollUp caps the range per request; 14 days is the limit for
# total-calories (and the other high-frequency types).
ROLLUP_MAX_DAYS = 14

# Ceiling on pages followed per list call, so a runaway token chain can't
# turn one request into hundreds.
MAX_PAGES = 10


def _civil_midnight(day: date) -> dict[str, Any]:
    """`day` at 00:00 as the API's CivilDateTime."""
    return {
        "date": {"year": day.year, "month": day.month, "day": day.day},
        "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0},
    }


# Status code used for "the request never got a response at all" — a DNS
# failure, connection reset or timeout. Not a real HTTP status; it exists so
# every failure out of this client is a GoogleHealthError and callers only
# need one except clause.
NETWORK_ERROR_STATUS = 0


class GoogleHealthError(RuntimeError):
    """Raised when the Health API fails — a non-2xx response, or no response."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"{status} from {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url

    @property
    def is_transient(self) -> bool:
        """Whether retrying this later could plausibly succeed.

        Server errors, rate limits and network failures are worth a retry.
        A 400 (bad filter grammar) or 403 (missing scope) is a bug or a
        consent problem — retrying just burns the budget.
        """
        return (
            self.status == NETWORK_ERROR_STATUS
            or self.status == 429
            or self.status >= 500
        )


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str = ""
    # From the id_token, when the openid/email scopes were granted. None on
    # a plain refresh, which doesn't reissue an id_token.
    google_user_id: str | None = None
    email: str | None = None

    @property
    def is_expiring_soon(self) -> bool:
        # Refresh proactively so a request never fails on a stale token.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(minutes=5)


def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Read the claims out of an id_token without verifying the signature.

    Safe *specifically here* because this token came straight back from
    Google's token endpoint over an authenticated TLS connection, which
    Google's own documentation names as the case where verification can be
    skipped. Anything arriving from a client instead must be verified with
    google.oauth2.id_token.verify_oauth2_token.

    Returns {} rather than raising: identity is useful metadata, not
    something worth failing an otherwise successful connection over.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError) as e:
        logger.warning("Could not decode id_token claims: %s", e)
        return {}


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class GoogleHealthClient:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 scopes: list[str] | None = None, timeout: float = 30.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or SCOPES
        self.timeout = timeout

    # ---------------- OAuth ----------------

    def authorization_url(self, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            # offline + consent are both required to reliably receive a
            # refresh_token; without prompt=consent Google omits it on repeat
            # authorisations for the same user.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{AUTH_URI}?{urlencode(params)}"

    async def _post_token(self, data: dict[str, str]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                return await c.post(TOKEN_URI, data=data)
        except httpx.HTTPError as e:
            raise GoogleHealthError(
                NETWORK_ERROR_STATUS, f"{type(e).__name__}: {e}", TOKEN_URI
            ) from e

    async def exchange_code(self, code: str) -> TokenBundle:
        r = await self._post_token({
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        })
        if r.status_code >= 400:
            raise GoogleHealthError(r.status_code, r.text, TOKEN_URI)
        return self._bundle(r.json())

    async def refresh(self, refresh_token: str) -> TokenBundle:
        r = await self._post_token({
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        })
        if r.status_code >= 400:
            # invalid_grant here usually means the consent screen is still in
            # "Testing", where refresh tokens expire after seven days.
            raise GoogleHealthError(r.status_code, r.text, TOKEN_URI)
        data = r.json()
        data.setdefault("refresh_token", refresh_token)  # not always returned
        return self._bundle(data)

    async def revoke(self, token: str) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            await c.post(REVOKE_URI, data={"token": token})

    @staticmethod
    def _bundle(data: dict[str, Any]) -> TokenBundle:
        # id_token is present on the initial code exchange (given the openid
        # scope) but not on a refresh, so these stay None there.
        claims = decode_id_token_claims(data["id_token"]) if data.get("id_token") else {}
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc)
                       + timedelta(seconds=int(data.get("expires_in", 3600))),
            scope=data.get("scope", ""),
            google_user_id=claims.get("sub"),
            email=claims.get("email"),
        )

    # ---------------- Data ----------------

    async def list_data_points(
        self,
        access_token: str,
        data_type: str,
        start: date,
        end: date,
        *,
        filter_template: str | None = None,
        page_size: int | None = None,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any]:
        """Data points for a kebab-case dataType over [start, end].

        Follows `nextPageToken` up to `max_pages`: a watch that samples heart
        rate more than once a minute overflows the 1440-point page, and stats
        computed from the first page alone would silently cover only part of
        the day. If the cap is hit the last token is left in the result, so a
        truncated fetch stays distinguishable from a complete one.
        """
        url = f"{HEALTH_BASE}/users/me/dataTypes/{data_type}/dataPoints"

        params: dict[str, Any] = {
            "pageSize": page_size or PAGE_SIZE.get(data_type, 1440),
        }

        template = filter_template if filter_template is not None else FILTER_TEMPLATE_BY_TYPE.get(data_type)
        if template:
            # end_rfc is the start of the day *after* `end` — every confirmed
            # template uses a strict `<` upper bound, so this is what makes
            # the end date itself inclusive.
            params["filter"] = template.format(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                start_rfc=f"{start.isoformat()}T00:00:00Z",
                end_rfc=f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
            )

        result: dict[str, Any] = {}
        points: list[Any] = []
        for _ in range(max(1, max_pages)):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.get(url, params=params,
                                    headers={"Authorization": f"Bearer {access_token}"})
            except httpx.HTTPError as e:
                # A timeout or connection reset otherwise escapes as a raw httpx
                # exception, past every `except GoogleHealthError` in the app.
                raise GoogleHealthError(
                    NETWORK_ERROR_STATUS, f"{type(e).__name__}: {e}", url
                ) from e

            if r.status_code >= 400:
                raise GoogleHealthError(r.status_code, r.text, str(r.request.url))

            result = r.json()
            points.extend(result.get("dataPoints") or [])
            token = result.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token

        if "dataPoints" in result or points:
            result["dataPoints"] = points
        return result

    async def daily_rollup(
        self,
        access_token: str,
        data_type: str,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        """One rolled-up data point per civil day over [start, end].

        The API caps the range per request (ROLLUP_MAX_DAYS), so a longer
        span is fetched in chunks and the points concatenated.
        """
        url = f"{HEALTH_BASE}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"

        points: list[Any] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=ROLLUP_MAX_DAYS - 1))
            # The range is closed-open, so the end is the day *after* the
            # last day wanted.
            after = chunk_end + timedelta(days=1)
            body = {
                "range": {
                    "start": _civil_midnight(chunk_start),
                    "end": _civil_midnight(after),
                },
                "windowSizeDays": 1,
            }
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.post(url, json=body,
                                     headers={"Authorization": f"Bearer {access_token}"})
            except httpx.HTTPError as e:
                raise GoogleHealthError(
                    NETWORK_ERROR_STATUS, f"{type(e).__name__}: {e}", url
                ) from e

            if r.status_code >= 400:
                raise GoogleHealthError(r.status_code, r.text, url)

            points.extend(r.json().get("rollupDataPoints") or [])
            chunk_start = after

        return {"rollupDataPoints": points}

    async def fetch_metric(
        self, access_token: str, friendly: str, start: date, end: date
    ) -> dict[str, Any]:
        """Raw response for one friendly metric name, via `dailyRollUp` or
        `list` as ROLLUP_TYPES dictates."""
        path = DATA_TYPES[friendly]
        if path in ROLLUP_TYPES:
            return await self.daily_rollup(access_token, path, start, end)
        return await self.list_data_points(access_token, path, start, end)

    async def fetch_day(self, access_token: str, day: date) -> dict[str, Any]:
        """
        Every metric for a single day, keyed by friendly name.

        A failure on one data type does not sink the others — a missing metric
        produces a shorter summary, which is the documented degradation path.
        Each recorded error carries `transient`, so a caller can tell a real
        gap in the data from an outage that's worth retrying.
        """
        out: dict[str, Any] = {"date": day.isoformat(), "metrics": {}, "errors": {}}

        for friendly in DATA_TYPES:
            try:
                out["metrics"][friendly] = await self.fetch_metric(
                    access_token, friendly, day, day
                )
            except GoogleHealthError as e:
                logger.warning("fetch %s failed: %s", friendly, e)
                out["errors"][friendly] = {
                    "status": e.status,
                    "body": e.body[:400],
                    "transient": e.is_transient,
                }

        return out


def is_total_outage(day_data: dict[str, Any]) -> bool:
    """True when every data type failed and at least one failure was transient.

    This is the distinction that decides whether a thin day is real. A watch
    left on the charger returns 200s with empty point lists — genuinely no
    data, and "you didn't log anything yesterday" is the correct summary. An
    API outage returns nothing but 5xx, which looks identical downstream once
    the errors are swallowed. Sending "no data yesterday" in that case tells
    the user something false about their own health, so the caller should
    retry instead.

    Every type counts, calories included — it used to be excluded because
    `list` always failed on it, but it is now read through dailyRollUp.
    """
    errors = day_data.get("errors", {})

    if set(errors) != set(DATA_TYPES):
        return False  # at least one type came back — partial data is usable
    return any(e.get("transient") for e in errors.values())
