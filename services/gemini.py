"""
Gemini AI integration.

Two entry points:
  generate_daily_summary(day_data)
      One-shot: turn a day of Google Health metrics (the dict returned by
      GoogleHealthClient.fetch_day) into a short markdown summary. No tool
      use — the data is already in hand.

  answer_health_query(user_message, access_token, health_client)
      Interactive: Gemini decides which metrics (if any) it needs and calls
      back into the Google Health API via function calling before answering.
      See the "Interactive Query Pipeline" in README.md.

Uses the async client (client.aio.models.generate_content) so both entry
points stay `async def`. The system prompt goes in
GenerateContentConfig(system_instruction=...) — Gemini has no top-level
`system=` argument the way Anthropic does.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import settings
from services.google_health import DATA_TYPES, GoogleHealthClient, GoogleHealthError

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    """A Gemini call failed. `is_transient` says whether a retry could help."""

    def __init__(self, message: str, *, is_transient: bool):
        super().__init__(message)
        self.is_transient = is_transient


def _wrap_api_error(e: Exception) -> GeminiError:
    """Classify an SDK exception so callers don't have to know the SDK.

    ServerError (5xx) and network failures are worth retrying. So is 429 —
    the free tier rate-limits, and the daily run bursts. A 400 (malformed
    request) or 403 (bad key) never will be.
    """
    if isinstance(e, genai_errors.ServerError):
        return GeminiError(f"Gemini server error: {e}", is_transient=True)
    if isinstance(e, genai_errors.ClientError):
        code = getattr(e, "code", None)
        return GeminiError(f"Gemini client error ({code}): {e}", is_transient=code == 429)
    if isinstance(e, httpx.HTTPError):
        return GeminiError(f"Gemini network error: {type(e).__name__}: {e}", is_transient=True)
    return GeminiError(f"Gemini call failed: {type(e).__name__}: {e}", is_transient=False)


# Constructed lazily-but-once: genai.Client raises immediately if api_key is
# falsy, so an unset key must not crash module import (routes/tests import
# this module before any request that would actually need the key arrives).
_client = genai.Client(api_key=settings.GEMINI_API_KEY) if settings.GEMINI_API_KEY else None

MAX_TOKENS = 1024

DAILY_SUMMARY_SYSTEM_PROMPT = """\
You are a friendly, encouraging health assistant. You'll be given one day's \
Fitbit/Pixel Watch metrics as JSON. Write a short daily summary as up to 3 \
markdown bullet points covering sleep, activity, and heart rate.

Rules:
- Only report numbers that are actually present in the data — never invent
  or estimate a figure.
- If a metric is missing or empty, skip it silently rather than mentioning
  the gap.
- If every metric is missing, say so briefly instead of producing bullets.
- Keep it warm and motivating. At most one emoji per bullet.
"""

QUERY_SYSTEM_PROMPT = """\
You are a friendly health assistant with access to the user's Fitbit/Pixel \
Watch data through the get_health_metric tool. Call it whenever answering \
requires data you don't already have in this conversation. Answer \
conversationally and cite the specific numbers you found. If the data \
doesn't cover what was asked, say so plainly instead of guessing.
"""


def tool_schema() -> list[dict[str, Any]]:
    """The tool definition Gemini is given for interactive health queries."""
    return [
        {
            "name": "get_health_metric",
            "description": (
                "Fetch one Fitbit/Pixel Watch metric for a date range from "
                "the user's Google Health account."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": sorted(DATA_TYPES),
                        "description": "Which metric to fetch.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD), inclusive. Defaults to yesterday.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD), inclusive. Defaults to start_date.",
                    },
                },
                "required": ["metric"],
            },
        }
    ]


def _gemini_tools() -> list[types.Tool]:
    """tool_schema()'s Anthropic-shaped dicts, wrapped for Gemini function calling.

    FunctionDeclaration.parameters_json_schema takes a raw JSON Schema dict
    directly (mutually exclusive with the `parameters` field, which wants a
    types.Schema object) — so the existing input_schema dicts pass through
    unmodified.
    """
    return [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters_json_schema=t["input_schema"],
            )
            for t in tool_schema()
        ])
    ]


def _require_client() -> genai.Client:
    if _client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return _client


async def generate_daily_summary(day_data: dict[str, Any]) -> str:
    try:
        response = await _require_client().aio.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=f"Today's health data:\n\n{json.dumps(day_data, indent=2)}",
            config=types.GenerateContentConfig(
                system_instruction=DAILY_SUMMARY_SYSTEM_PROMPT,
                max_output_tokens=MAX_TOKENS,
            ),
        )
    except Exception as e:
        raise _wrap_api_error(e) from e

    text = (response.text or "").strip()
    if not text:
        # An empty completion (safety filter, or the token cap hit mid-first-
        # token) would otherwise be stored and sent as a blank message.
        raise GeminiError("Gemini returned an empty summary", is_transient=True)
    return text


async def answer_health_query(
    user_message: str,
    access_token: str,
    health_client: GoogleHealthClient,
    *,
    max_tool_iterations: int = 4,
) -> str:
    """Run the function-calling loop until Gemini answers or the cap is hit."""
    client = _require_client()
    tools = _gemini_tools()
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=user_message)])
    ]

    for _ in range(max_tool_iterations):
        try:
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=QUERY_SYSTEM_PROMPT,
                    max_output_tokens=MAX_TOKENS,
                    tools=tools,
                ),
            )
        except Exception as e:
            raise _wrap_api_error(e) from e

        calls = response.function_calls
        if not calls:
            return (response.text or "").strip()

        contents.append(response.candidates[0].content)

        function_response_parts = []
        for call in calls:
            result = await _run_tool(call.args or {}, access_token, health_client)
            payload = {"error": result["content"]} if result["is_error"] else {"result": result["content"]}
            function_response_parts.append(types.Part(function_response=types.FunctionResponse(
                id=call.id,
                name=call.name,
                response=payload,
            )))
        contents.append(types.Content(role="user", parts=function_response_parts))

    logger.warning("answer_health_query hit max_tool_iterations=%d", max_tool_iterations)
    return "I wasn't able to pull that together — try asking again, maybe more specifically."


async def _run_tool(
    tool_input: dict[str, Any],
    access_token: str,
    health_client: GoogleHealthClient,
) -> dict[str, Any]:
    metric = tool_input.get("metric")
    if metric not in DATA_TYPES:
        return {
            "content": f"Unknown metric '{metric}'. Valid metrics: {sorted(DATA_TYPES)}",
            "is_error": True,
        }

    try:
        end = _parse_date(tool_input.get("end_date")) or _parse_date(tool_input.get("start_date")) \
            or date.today() - timedelta(days=1)
        start = _parse_date(tool_input.get("start_date")) or end
    except ValueError as e:
        return {"content": f"Invalid date: {e}", "is_error": True}

    try:
        result = await health_client.list_data_points(access_token, DATA_TYPES[metric], start, end)
    except GoogleHealthError as e:
        logger.warning("get_health_metric(%s) failed: %s", metric, e)
        return {
            "content": f"Google Health API error ({e.status}) fetching {metric}.",
            "is_error": True,
        }

    return {"content": json.dumps(result), "is_error": False}


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
