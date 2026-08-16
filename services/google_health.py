"""
Google Health API Client
Handles OAuth 2.0 authorization and Fitbit data fetching
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from config import settings

logger = logging.getLogger(__name__)

class GoogleHealthClient:
    """Google Health API (Fitbit) client with OAuth 2.0 support"""

    # Google OAuth endpoints
    OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

    # Google Health (Fitbit) API endpoints
    FITBIT_API_BASE = "https://www.googleapis.com/fitness/v1/users/me"

    def __init__(self):
        self.client_id = settings.GOOGLE_CLIENT_ID
        self.client_secret = settings.GOOGLE_CLIENT_SECRET
        self.redirect_uri = settings.GOOGLE_REDIRECT_URI
        self.scopes = settings.GOOGLE_HEALTH_SCOPES or [
            "https://www.googleapis.com/auth/fitness.sleep.read",
            "https://www.googleapis.com/auth/fitness.activity.read",
            "https://www.googleapis.com/auth/fitness.heart_rate.read",
        ]

    def get_authorization_url(self, state: str) -> str:
        """
        Generate Google OAuth authorization URL
        Returns URL for user to click and authorize
        """
        flow = Flow.from_client_config(
            {
                "installed": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": settings.GOOGLE_AUTH_URI if hasattr(settings, 'GOOGLE_AUTH_URI') else "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": settings.GOOGLE_TOKEN_URI,
                }
            },
            scopes=self.scopes,
            redirect_uri=self.redirect_uri
        )

        auth_url, _ = flow.authorization_url(state=state)
        return auth_url

    async def exchange_code_for_tokens(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for access & refresh tokens
        Returns: {access_token, refresh_token, expires_in, ...}
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.GOOGLE_TOKEN_URI,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                }
            )
            response.raise_for_status()
            tokens = response.json()
            logger.info(f"✅ Successfully exchanged auth code for tokens")
            return tokens

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Refresh an expired access token using refresh token
        Returns: {access_token, expires_in, ...}
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.GOOGLE_TOKEN_URI,
                data={
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                }
            )
            response.raise_for_status()
            tokens = response.json()
            logger.info("✅ Successfully refreshed access token")
            return tokens

    async def fetch_sleep_data(self, access_token: str, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch sleep data from Google Health API
        Returns raw Fitbit sleep data
        """
        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        # Convert date to milliseconds (Google Fit API expects timestamps)
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        start_time = int(date_obj.timestamp() * 1000)
        end_time = int((date_obj + timedelta(days=1)).timestamp() * 1000)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.FITBIT_API_BASE}/dataset:read",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "dataSourceId": "derived:com.google.sleep.segment:com.google.android.gms:merge_sleep_data",
                    "startTimeMillis": start_time,
                    "endTimeMillis": end_time,
                }
            )
            response.raise_for_status()
            return response.json()

    async def fetch_activity_data(self, access_token: str, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch activity data (steps, calories, active zones) from Google Health API
        """
        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        date_obj = datetime.strptime(date, "%Y-%m-%d")
        start_time = int(date_obj.timestamp() * 1000)
        end_time = int((date_obj + timedelta(days=1)).timestamp() * 1000)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.FITBIT_API_BASE}/dataset:read",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "dataSourceId": "derived:com.google.step_count.delta:com.google.android.gms:merge_step_deltas",
                    "startTimeMillis": start_time,
                    "endTimeMillis": end_time,
                }
            )
            response.raise_for_status()
            return response.json()

    async def fetch_heart_rate_data(self, access_token: str, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch heart rate data from Google Health API
        """
        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        date_obj = datetime.strptime(date, "%Y-%m-%d")
        start_time = int(date_obj.timestamp() * 1000)
        end_time = int((date_obj + timedelta(days=1)).timestamp() * 1000)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.FITBIT_API_BASE}/dataset:read",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "dataSourceId": "derived:com.google.heart_rate.bpm:com.google.android.gms:merge_heart_rate_bpm",
                    "startTimeMillis": start_time,
                    "endTimeMillis": end_time,
                }
            )
            response.raise_for_status()
            return response.json()

    async def fetch_all_health_data(self, access_token: str, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch all health data (sleep, activity, heart rate) in one call
        Returns: {sleep: {...}, activity: {...}, heart_rate: {...}}
        """
        sleep_data = await self.fetch_sleep_data(access_token, date)
        activity_data = await self.fetch_activity_data(access_token, date)
        heart_rate_data = await self.fetch_heart_rate_data(access_token, date)

        return {
            "sleep": sleep_data,
            "activity": activity_data,
            "heart_rate": heart_rate_data,
            "date": date or datetime.utcnow().strftime("%Y-%m-%d"),
        }


# Global instance
google_health_client = GoogleHealthClient()
