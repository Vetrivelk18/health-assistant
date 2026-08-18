"""
Claude AI integration.

Two entry points:
  generate_daily_summary(day_data)
      One-shot: turn a day of Google Health metrics (the dict returned by
      GoogleHealthClient.fetch_day) into a short markdown summary. No tool
      use — the data is already in hand.

  answer_health_query(user_message, access_token, health_client)
      Interactive: Claude decides which metrics (if any) it needs and calls
      back into the Google Health API via tool use before answering. See
      the "Interactive Query Pipeline" in README.md.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import anthropic

from config import settings
from services.google_health import DATA_TYPES, GoogleHealthClient, GoogleHealthError

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)

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
    """The tool definition Claude is given for interactive health queries."""
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


def generate_daily_summary(day_data: dict[str, Any]) -> str:
    response = _client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=DAILY_SUMMARY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Today's health data:\n\n{json.dumps(day_data, indent=2)}",
        }],
    )
    return _text(response)


async def answer_health_query(
    user_message: str,
    access_token: str,
    health_client: GoogleHealthClient,
    *,
    max_tool_iterations: int = 4,
) -> str:
    """Run the tool-use loop until Claude answers or the iteration cap is hit."""
    tools = tool_schema()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    for _ in range(max_tool_iterations):
        response = _client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=QUERY_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            return _text(response)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            result = await _run_tool(block.input, access_token, health_client)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result["content"],
                "is_error": result["is_error"],
            })
        messages.append({"role": "user", "content": tool_results})

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


def _text(response: anthropic.types.Message) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()
