# AGENTS.md

Instructions for coding agents (Claude Code, etc.) working in this repo.

## What this is

FastAPI backend that reads Fitbit/Pixel Watch data through the **Google
Health API** (`health.googleapis.com`), summarizes it with Gemini, and
delivers it over a Telegram bot. Production target is Cloud Run + Cloud
Scheduler + Cloud Tasks + Neon Postgres — see `DEPLOY.md`. See `README.md`
for the full architecture and setup instructions.

**Cost is a hard constraint on this project.** Every infrastructure choice
defaults to a free tier and scale-to-zero. Before proposing anything that
adds a component, state its cost — and prefer the free option even when it's
less convenient. Avoid always-on resources (idle VMs, provisioned DB
instances, load balancers, Redis) unless explicitly asked for. This is why
there's no in-process scheduler, why `--min-instances=0`, and why OAuth
state lives in a process dict rather than a Redis instance.

## Current state — read before touching OAuth or the Health API client

`services/google_health.py` was rewritten to target the Google Health API.
The old code targeted `fitness.googleapis.com` (Google Fit), which is closed
to new sign-ups and is being deprecated in 2026. `routes/auth.py` and the
`GOOGLE_HEALTH_SCOPES` default in `config.py` have been updated to match the
new `GoogleHealthClient` interface (constructor takes `client_id`/
`client_secret`/`redirect_uri`/`scopes`; methods are `authorization_url()`,
`exchange_code()`, `refresh()`, returning a `TokenBundle` instead of a raw
dict) — `tests/test_auth.py` covers the OAuth routes and passes.

`services/gemini.py` (Gemini function-calling loop) and `routes/mcp_tools.py`
(`GET /mcp/tools`, `POST /mcp/query`) are built and wired into `app.py`.
`services/gemini.py` defines one tool, `get_health_metric`, that Gemini calls
to pull a Fitbit/Pixel Watch metric via `GoogleHealthClient.list_data_points`;
`tests/test_mcp_tools.py` covers the routes with mocked Gemini/Google calls.
It uses `google-genai` (not the deprecated `google-generativeai`) via the
async client (`client.aio.models.generate_content`); the system prompt goes
in `GenerateContentConfig(system_instruction=...)`, not a top-level `system=`
kwarg the way Anthropic did it. `_client` is constructed lazily and stays
`None` if `GEMINI_API_KEY` is unset, so an unconfigured key doesn't crash
module import — it only raises (`_require_client()`) when something actually
tries to call the API.

Swapping Claude for Gemini also forced a dependency cleanup: `anthropic` and
the unused `python-telegram-bot`/`mcp` packages are gone, and `fastapi` (→
`0.115.0`), `httpx` (→ `0.28.1`), and `pydantic`/`pydantic-settings` (→
`2.9.2`/`2.5.2`) were bumped because `google-genai` needs `anyio>=4.8` /
`httpx>=0.28.1` / `pydantic>=2.9`, which the old pins couldn't satisfy.

`config.py`'s `GEMINI_MODEL` default is `gemini-3.5-flash-lite`, not
`gemini-2.5-flash-lite` — pricing docs say 2.5-flash-lite is cheaper, but the
live API 404s it for this account ("no longer available to new users") and
names 3.5-flash-lite as the replacement. Both `generate_daily_summary` and
`answer_health_query` have been run against the real API with a real key and
work end-to-end (confirmed function calling actually round-trips: Gemini
calls `get_health_metric`, gets real tool output, answers from it). One
cosmetic-only issue: the SDK logs a pydantic `UserWarning` about
`Content`/`Part`/`File` type mismatches when re-appending a `types.Content`
returned from a previous turn back into the `contents` list for the next
function-calling round — harmless, doesn't affect the result, not worth
chasing.

`routes/telegram.py` (webhook: `/start`, `/connect`, `/disconnect`,
`/status`, plus plain-text queries routed through `services/gemini.py`) and
`routes/internal.py` (`POST /internal/run-daily` called by Cloud Scheduler,
which fans out over Cloud Tasks to `POST /internal/run-single-user` —
see the Gotchas below) are built and wired in. `routes/health.py` (the
separate `/api/health/*` endpoints, distinct from the existing `GET /health`
liveness check) doesn't exist yet.

The `filter` query grammar for `GET /v4/.../dataPoints` is confirmed (ran
`probe_health_api.py` against a real account, 2026-08-22 — standalone, no
FastAPI/DB needed, see `README_PROBE.md`) and lives in
`FILTER_TEMPLATE_BY_TYPE` in `services/google_health.py`. The public docs
don't spell this out, and it is **not one shared grammar** — it's genuinely
per data type, each restricting on a different member path always prefixed
with the type's snake_case name, e.g. `sleep.interval.end_time` for sleep
but `heart_rate.sample_time.physical_time` for heart rate (mixing
`interval.start_time`/`interval.end_time` vs `sample_time.physical_time` per
type, discovered by testing each candidate directly — the Anthropic-style
"try templates until one works" approach the old `FILTER_TEMPLATES` list
assumed doesn't hold here, since a template that works for one type 400s on
another). `list_data_points` applies the right one automatically now — no
per-call filter setup needed. `total-calories` has none: `list` 400s on it
unconditionally (`UNSUPPORTED_DATA_TYPE_ACTION` — only `rollup`/
`dailyRollUp` are supported), which this client doesn't implement; it's left
in `DATA_TYPES` so it degrades gracefully (`fetch_day` records it under
`errors`) rather than silently disappearing from the tool schema.

One thing still open: that same probe run returned zero data points for
every type over a 30-day window against the test account — plumbing (auth,
scopes, filter grammar) is confirmed correct, but no actual device data has
been confirmed yet. Re-run `probe_health_api.py` against an account with a
Fitbit/Pixel Watch that's synced recently before assuming this is a code
bug.

The daily run was reworked on 2026-08-22/23 into a **Cloud Tasks fan-out**
(`services/tasks.py` + a dispatcher/worker split in `routes/internal.py`),
with failure classification across every outbound call and structured JSON
logging (`utils/logging_config.py`). The Gotchas section below covers the
non-obvious parts — read it before changing `routes/internal.py`,
`services/tasks.py`, or any error handling. All of it is **test-verified but
not yet deployed**: the queue, its IAM bindings and the log-based metric in
`DEPLOY.md` §5–6 have not been created in a real GCP project.

## Commands

```bash
# Install deps (project uses a venv/ directory — do not commit it)
pip install -r requirements.txt

# Run the app
python app.py                  # or: uvicorn app:app --reload

# Tests — these run against a throwaway `<your db>_test` database that
# tests/conftest.py creates automatically. They never touch your dev data.
pytest
pytest --cov=.
TEST_DATABASE_URL=postgresql://... pytest   # point somewhere else

# Lint / format
black .
flake8 .

# Database migrations (see Gotchas — never use create_all for changes)
alembic revision --autogenerate -m "describe the change"
alembic upgrade head --sql     # preview SQL without applying
alembic upgrade head
alembic stamp head             # baseline a DB that already has the tables

# Standalone Google Health API probe (no server/DB required)
python3 probe_health_api.py
```

## Conventions

- Config is centralized in `config.py` (`Settings`, loaded from `.env` via
  `python-dotenv`). Don't read `os.environ` directly elsewhere.
- Business logic lives in `services/`; `routes/` should stay thin.
- Secrets belong in `.env`, never `.env.example`. Never commit `.env`,
  `.probe_tokens.json`, or anything under `fixtures/` (all gitignored) —
  the probe script writes real OAuth tokens and raw API responses to those.
- `venv/` is a real virtualenv (~200MB) and must stay gitignored; don't
  `git add -A` without checking `git status` first.

## Gotchas

- **macOS `localhost` vs `127.0.0.1`**: `localhost` can resolve to IPv6 `::1`
  while a plain `HTTPServer` binds IPv4 only, causing
  `ERR_CONNECTION_REFUSED` right after a successful consent screen. Use
  literal `127.0.0.1` in redirect URIs during local dev.
- **7-day refresh tokens**: while the OAuth consent screen is in *Testing*,
  Google expires refresh tokens after 7 days. Not a bug — either re-auth
  weekly or move the consent screen to *Production* (which needs OAuth
  verification for real users).
- **Restricted scopes**: the `googlehealth.*.readonly` scopes require adding
  yourself as a test user on the OAuth consent screen while in Testing mode;
  no verification needed until you leave Testing.
- **No in-process scheduler, on purpose**: Cloud Run containers don't stay
  alive between requests, so anything like APScheduler (previously listed in
  `requirements.txt`, never actually wired up) would silently never fire.
  The daily job is triggered externally — Cloud Scheduler calls
  `POST /internal/run-daily`. Don't defer work with a background task —
  Cloud Run freezes CPU the instant the response is sent, so anything
  deferred past that point never completes. Cloud Tasks is the supported way
  to get a "later" here.
- **The daily run fans out over Cloud Tasks**: `/internal/run-daily` no
  longer does per-user work. It enqueues one task per connected user and
  returns in well under a second; Cloud Tasks dispatches those back to
  `POST /internal/run-single-user` at ~5/sec, one short request each. Keep
  it that way — if the dispatcher starts calling Gemini or Telegram itself,
  the queue's whole purpose (flat memory, short requests, scale-to-zero
  between users) is gone. `tests/test_internal.py` asserts the dispatcher
  makes no outbound calls.
- **Retry semantics on `/run-single-user`**: Cloud Tasks retries *any*
  non-2xx. So permanent failures (no token on file, dead refresh token)
  return **200** with a failure body — retrying can't help and would burn
  all five attempts — while transient ones (Gemini, Telegram, Health API
  5xx/429) return **503** to earn a backed-off retry. `_summarise_user`
  returns a `retryable` flag rather than raising, so the inline path and the
  task path can each decide what to do with the same outcome.
- **Idempotency has three layers**, because a retried task must never send a
  second summary: deterministic Cloud Tasks names
  (`daily-{user_id}-{date}`) dedupe at the queue, `/run-single-user`
  re-checks `last_summary_sent` before doing work, and the `HealthSummary`
  write is an upsert on `(user_id, summary_date)` so a retry after a
  Telegram failure updates the existing row instead of piling up duplicates.
- **`services/tasks.py` imports `google.cloud` inside its functions**, not
  at module scope. That's deliberate: it keeps grpcio's slow import off
  every cold start (most are Telegram webhooks that never enqueue), and
  keeps the module importable without Application Default Credentials, which
  don't exist on a dev laptop or in CI.
- **Without `TASKS_QUEUE` set, `/run-daily` runs the batch inline** and logs
  a warning. That's the local-dev and test path; the response's `"mode"`
  field says which path ran (`"queued"` vs `"inline"`).
- **Every outbound call fails into a *classified* error.** `GoogleHealthError`
  has `.is_transient`, `GeminiError` has `.is_transient`, and `TelegramError`
  carries `.error_code`. Network failures are wrapped at the source —
  `httpx.HTTPError` becomes `GoogleHealthError(status=0)` or a
  `TelegramError` — so a timeout can't escape past an `except
  GoogleHealthError` clause. If you add an outbound call, wrap it the same
  way; an unclassified exception defaults to non-retryable and silently
  drops that user's summary for the day.
- **`is_total_outage()` distinguishes "watch was off" from "API is down".**
  Both produce a day with no metrics, because `fetch_day` swallows per-type
  errors by design. Only the first should become a summary — telling someone
  they logged nothing when Google was down is worse than being late. Don't
  "simplify" this check away; `tests/test_reliability.py` pins the four
  cases (empty-but-OK, all-transient, partial, all-permanent).
- **`total-calories` is excluded from the outage check** because it fails
  permanently by design. Counting it would make a real outage undetectable.
- **Structured logs, not f-strings, for anything operational.** Use
  `log_event(logger, level, msg, event=..., user_id=..., ...)` from
  `utils/logging_config.py` — those kwargs become queryable
  `jsonPayload` fields in Cloud Logging. A message with the user id
  interpolated into the string can only be grepped. Existing event names are
  listed in `DEPLOY.md` §6; reuse them rather than inventing variants.
- **Retry budgets are bounded, and both defaults were unlimited.** Cloud
  Tasks (`--max-attempts=3`) and Cloud Scheduler (`--max-retry-attempts=3`)
  both default to retrying forever, which for a daily job means a
  permanently broken user burns quota and Gemini tokens indefinitely. The
  endpoint's 200-vs-503 classification is what makes a small budget safe.
- **Tests run against a separate `*_test` database**, derived from
  `DATABASE_URL` and created on demand by `tests/conftest.py`, which also
  wipes every table between tests. Don't reintroduce per-file cleanup
  fixtures as the primary guard — the reason isolation was added is that a
  test erroring *before* its own cleanup ran left `pytest_*` rows behind and
  broke the next run with a UNIQUE violation. Postgres, not SQLite, on
  purpose: the app depends on Postgres behaviour and a suite passing on
  SQLite would be testing the wrong engine.
- **Code must work on 3.11 *and* 3.9.** 3.11 is production (see the
  Dockerfile); 3.9 matters because the checked-in dev venv is macOS system
  Python, where `X | None` in a runtime-evaluated annotation raises
  `TypeError` — that has broken the suite locally twice. Either add
  `from __future__ import annotations` or use `Optional[...]`. This
  constraint goes away once the dev venv is rebuilt on 3.11.
- **Check model/migration drift before pushing**: `alembic upgrade head &&
  alembic check`, and confirm the migration reverses with `alembic downgrade
  base && alembic upgrade head`. Drift is what made the suite fail with
  "column does not exist" — a failure that looked like a code bug and
  wasn't. A downgrade that doesn't work is otherwise only discovered when
  it's needed, which is the worst possible moment.
- **There is no CI workflow in the repo yet.** One is written and verified
  (two jobs: tests on 3.11/3.9 against a Postgres service, plus the
  migration checks above) but pushing anything under `.github/workflows/`
  requires a credential with the `workflow` scope, which the current one
  lacks. Until it's added by hand, the checks above are manual.
- **Never change the schema with `create_all()`.** It creates missing tables
  and silently ignores existing ones — add a column to `models.py` and it
  reports success while doing nothing, then the app fails at runtime on the
  missing column. The instinctive fix (drop and recreate) destroys the OAuth
  tokens, which can't be regenerated without every user re-authorising.
  `init_db()` now raises outside development to make that impossible. Schema
  changes go through Alembic; `app.py` does not migrate on startup, because
  concurrent Cloud Run instances would race during a cold start.
- **Always read an autogenerated migration before applying it.** Alembic
  renders a table rename as drop+create (silent data loss), and misses
  server defaults and check constraints. A NOT NULL column added to a
  populated table needs a default or a backfill or the migration fails.
- **`alembic.ini` has a deliberately empty `sqlalchemy.url`.** The real URL
  carries the Neon password and that file is committed; `alembic/env.py`
  injects it from `config.settings` at runtime. Don't paste a connection
  string in there.
- **The Telegram webhook is authenticated by a shared secret**
  (`TELEGRAM_WEBHOOK_SECRET`, compared with `secrets.compare_digest`). The
  URL is public and Telegram can't present an OIDC token, so this header is
  the only thing distinguishing a real update from one forged by anyone who
  learns the URL. It fails open **only** when `FASTAPI_ENV=development`;
  unset in production is a 403, deliberately. Registering the webhook and
  setting the secret must happen together — `set_webhook(secret_token=...)`.
- **OAuth `state` is a signed token, not a server-side entry.** It was a
  module-level dict, which fails in exactly this deployment: Cloud Run
  scales to zero (cold start empties it) and runs multiple instances (the
  callback can land somewhere the login never touched). Both surface as
  "Invalid state parameter" right after a *successful* consent screen. Don't
  reintroduce server-side state — `utils/oauth_state.py` signs the chat id
  and expiry with `SECRET_KEY` so any instance can verify one it didn't
  issue. `SECRET_KEY` is therefore security-critical, and `validate_settings`
  refuses to start in production if it's still the default.
- **`openid` and `email` scopes are force-added in `config.py`**, after
  reading `GOOGLE_HEALTH_SCOPES` from the environment. They're what make the
  token response carry an `id_token`, which is the only source for the real
  `google_user_id` and `email` — both UNIQUE columns that previously held
  values synthesized from the Telegram chat id. Adding them to the default
  list alone wasn't enough: an existing `.env` overrides it silently.
- **The 7-day refresh-token expiry is a scheduled event, not an edge case**,
  because the consent screen stays in Testing. The daily run therefore
  *notifies* the user (once per `RENAG_AFTER_DAYS`) rather than failing
  silently, and warns a day ahead based on `refresh_token_issued_at`. That
  column is set on `exchange_code` only — a refresh returns a new access
  token but does not restart the refresh token's clock, so setting it there
  would push the warning permanently into the future.
- **Error Reporting ingests an entry only if it carries the
  `ReportedErrorEvent` `@type` *and* has the stack trace inside `message`.**
  A trace on any other key is silently ignored. `utils/logging_config.py`
  handles both. Per-user operational failures deliberately do *not* report —
  an expired 7-day consent would otherwise page someone every morning. Pass
  `report=True` to `log_event` only for failures that mean the system itself
  is broken.
- **`/internal/run-daily` auth**: the Cloud Run service is deployed
  `--allow-unauthenticated` (Telegram has to reach the webhook), so this one
  endpoint verifies the Google-signed OIDC token Cloud Scheduler sends
  instead, checking both `audience` and the exact service-account email. See
  `DEPLOY.md`.
