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

Caveat, deliberately not hidden:
  The exact `filter` expression grammar is not fully specified in the public
  reference. `FILTER_TEMPLATES` below holds several candidate forms; the probe
  script (probe_health_api.py) tries each and reports which the API accepts.
  Once you know, set FILTER_TEMPLATE_IN_USE and delete the rest.
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

# dataType path segments are kebab-case.
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

# Candidate filter grammars — see module docstring.
FILTER_TEMPLATES = [
    'dailySummaryDate >= "{start_date}" AND dailySummaryDate <= "{end_date}"',
    'startTime >= "{start_rfc}" AND endTime <= "{end_rfc}"',
    'sampleTime >= "{start_rfc}" AND sampleTime <= "{end_rfc}"',
]
FILTER_TEMPLATE_IN_USE: str | None = None  # set once the probe confirms one


class GoogleHealthError(RuntimeError):
    """Raised when the Health API returns a non-2xx response."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"{status} from {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


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

    async def exchange_code(self, code: str) -> TokenBundle:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(TOKEN_URI, data={
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
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(TOKEN_URI, data={
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

        template = filter_template or FILTER_TEMPLATE_IN_USE
        if template:
            params["filter"] = template.format(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                start_rfc=f"{start.isoformat()}T00:00:00Z",
                end_rfc=f"{end.isoformat()}T23:59:59Z",
            )

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(url, params=params,
                            headers={"Authorization": f"Bearer {access_token}"})

        if r.status_code >= 400:
            raise GoogleHealthError(r.status_code, r.text, str(r.request.url))
        return r.json()

    async def fetch_day(self, access_token: str, day: date) -> dict[str, Any]:
        """
        Every metric for a single day, keyed by friendly name.

        A failure on one data type does not sink the others — a missing metric
        produces a shorter summary, which is the documented degradation path.
        """
        out: dict[str, Any] = {"date": day.isoformat(), "metrics": {}, "errors": {}}

        for friendly, path in DATA_TYPES.items():
            try:
                out["metrics"][friendly] = await self.list_data_points(
                    access_token, path, day, day
                )
            except GoogleHealthError as e:
                logger.warning("fetch %s failed: %s", friendly, e)
                out["errors"][friendly] = {"status": e.status, "body": e.body[:400]}

        return out
