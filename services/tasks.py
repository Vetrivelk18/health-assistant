"""
Cloud Tasks client — the fan-out behind the daily summary run.

`POST /internal/run-daily` used to loop over every connected user inside a
single Cloud Scheduler request: one instance held CPU and RAM for the whole
batch, and one slow user could push the run past the attempt deadline. It
now enqueues one task per user here and returns immediately; Cloud Tasks
dispatches those back to `POST /internal/run-single-user` at a fixed rate,
so each request is short and Cloud Run can scale to zero between them.

Cost: the first 1,000,000 Cloud Tasks operations per month are free, and the
fan-out turns one request a day into N+1 — nowhere near the 2M/month Cloud
Run free tier at personal scale.

Two deliberate choices:

- **Async client.** `CloudTasksClient` is blocking; calling it from an async
  route would stall the event loop for the length of every enqueue. The
  library ships `CloudTasksAsyncClient`, so use it.

- **Deterministic task names.** Naming a task `daily-{user_id}-{day}` gets
  queue-level deduplication for free: if Cloud Scheduler retries a
  `/run-daily` attempt that already enqueued, the duplicate is rejected with
  ALREADY_EXISTS instead of sending a user a second summary. Google warns
  that name-based dedup adds dispatch latency and caps throughput, which is
  irrelevant at a handful of tasks a day and worth it for the idempotency.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

_client = None


class TasksNotConfigured(RuntimeError):
    """Raised when an enqueue is attempted without queue settings in place."""


def is_configured() -> bool:
    """Whether there's a real queue to talk to.

    False locally and in tests, where `/internal/run-daily` falls back to
    running the batch inline rather than failing.
    """
    return bool(
        settings.TASKS_QUEUE
        and settings.GOOGLE_PROJECT_ID
        and settings.TASKS_LOCATION
        and settings.TASKS_TARGET_BASE_URL
        and settings.TASKS_SERVICE_ACCOUNT_EMAIL
    )


def _get_client():
    """Built lazily, and `google.cloud` imported inside the function on
    purpose — for two reasons.

    Correctness: constructing the client resolves Application Default
    Credentials, which aren't present on a dev laptop or in CI, so doing it
    at import time would break both.

    Cost: `google-cloud-tasks` pulls in grpcio, which is slow and expensive
    to import. Keeping it out of module scope means that cost is paid once a
    day on the run that actually enqueues, not on every cold start — and
    most cold starts here are Telegram webhooks that never touch a queue.
    """
    global _client
    if _client is None:
        from google.cloud import tasks_v2

        _client = tasks_v2.CloudTasksAsyncClient()
    return _client


def queue_path() -> str:
    return (
        f"projects/{settings.GOOGLE_PROJECT_ID}"
        f"/locations/{settings.TASKS_LOCATION}"
        f"/queues/{settings.TASKS_QUEUE}"
    )


def _task_name(task_id: str) -> str:
    return f"{queue_path()}/tasks/{task_id}"


async def enqueue(path: str, payload: dict[str, Any], *, task_id: str | None = None) -> str | None:
    """
    Enqueue one POST to `path` on our own service, authenticated with an OIDC
    token Cloud Tasks mints for us.

    Returns the created task name, or None when `task_id` was already used —
    a duplicate is a success here, not an error, since it means the work is
    already queued.
    """
    if not is_configured():
        raise TasksNotConfigured(
            "TASKS_QUEUE / GOOGLE_PROJECT_ID / TASKS_TARGET_BASE_URL / "
            "TASKS_SERVICE_ACCOUNT_EMAIL must all be set to enqueue tasks"
        )

    from google.api_core import exceptions as gcloud_exceptions
    from google.cloud import tasks_v2

    task: dict[str, Any] = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.TASKS_TARGET_BASE_URL.rstrip('/')}{path}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            # Audience must match what the receiving endpoint verifies —
            # the same service URL Cloud Scheduler uses.
            "oidc_token": {
                "service_account_email": settings.TASKS_SERVICE_ACCOUNT_EMAIL,
                "audience": settings.RUN_DAILY_AUDIENCE,
            },
        }
    }
    if task_id:
        task["name"] = _task_name(task_id)

    try:
        created = await _get_client().create_task(parent=queue_path(), task=task)
    except gcloud_exceptions.AlreadyExists:
        logger.info("Task %s already queued — skipping duplicate", task_id)
        return None

    return created.name


def daily_task_id(user_id: str, target_day: date) -> str:
    """Deterministic per-user, per-day id, so a retried dispatch dedupes.

    Task ids allow letters, digits, hyphens and underscores only; user ids
    are UUID strings and dates are ISO, both of which already comply.
    """
    return f"daily-{user_id}-{target_day.isoformat()}"
