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
`dailyRollUp` actions — `list` 400s on it unconditionally
(UNSUPPORTED_DATA_TYPE_ACTION) — so it has no filter template and stays
broken via this client until someone builds the separate rollup request
shape (a different HTTP method/body, not just a filter).
"""

from __future__ import annotations

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

# Scopes are Restricted. Request only what the product actually reads.
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

# dataType path segments are kebab-case. "calories" always 400s via this
# client — total-calories only supports rollup/dailyRollUp, not list. Left
# in so it degrades gracefully (fetch_day records it under "errors") rather
# than silently vanishing from the schema.
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

    @property
    def is_expiring_soon(self) -> bool:
        # Refresh proactively so a request never fails on a stale token.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(minutes=5)


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
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc)
                       + timedelta(seconds=int(data.get("expires_in", 3600))),
            scope=data.get("scope", ""),
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
    ) -> dict[str, Any]:
        """One page of data points for a kebab-case dataType over [start, end]."""
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
        return r.json()

    async def fetch_day(self, access_token: str, day: date) -> dict[str, Any]:
        """
        Every metric for a single day, keyed by friendly name.

        A failure on one data type does not sink the others — a missing metric
        produces a shorter summary, which is the documented degradation path.
        Each recorded error carries `transient`, so a caller can tell a real
        gap in the data from an outage that's worth retrying.
        """
        out: dict[str, Any] = {"date": day.isoformat(), "metrics": {}, "errors": {}}

        for friendly, path in DATA_TYPES.items():
            try:
                out["metrics"][friendly] = await self.list_data_points(
                    access_token, path, day, day
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

    `total-calories` is excluded: it fails permanently by design (list isn't
    supported on it), so counting it would make a total outage impossible to
    detect.
    """
    errors = day_data.get("errors", {})
    retryable = {k: v for k, v in errors.items() if k != "calories"}
    expected = {k for k in DATA_TYPES if k != "calories"}

    if set(retryable) != expected:
        return False  # at least one type came back — partial data is usable
    return any(e.get("transient") for e in retryable.values())
