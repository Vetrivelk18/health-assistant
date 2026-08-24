# Deploying to Cloud Run + Cloud Scheduler + Cloud Tasks + Neon

Cloud Run containers don't stay alive between requests, so there's no
in-process scheduler here. Cloud Scheduler calls `POST /internal/run-daily`
once a day, which **fans the batch out over Cloud Tasks** — one task per
user — and returns immediately:

```
Cloud Scheduler --07:00--> POST /internal/run-daily
                               |  enqueue 1 task per user, respond <1s
                               v
                          Cloud Tasks queue (dispatch capped at 5/sec)
                               |
                               v  one short request per user
                          POST /internal/run-single-user
                               refresh token → fetch → summarise → store → send
```

Each request stays short, peak memory stays flat no matter how many users
there are, and Cloud Run scales back to zero between dispatches. Every piece
of this stays inside a free tier at personal scale — see
[Cost](#9-cost-what-actually-stays-free).

## 1. Neon Postgres

1. Create a project at [neon.tech](https://neon.tech) (free tier is enough
   for personal use).
2. Copy the connection string from the Neon dashboard. It comes back as
   plain `postgresql://user:pass@ep-xxxx.neon.tech/dbname` — append
   `?sslmode=require` (Neon requires TLS):

   ```
   DATABASE_URL=postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/health_assistant?sslmode=require
   ```

3. Apply the schema with Alembic, from your machine, before the first deploy:

   ```bash
   DATABASE_URL="<neon connection string from above>" alembic upgrade head
   ```

   **If your database already has tables** (created before migrations
   existed, when the app called `create_all()` on boot), don't run the
   upgrade — its `CREATE TABLE`s will fail on tables that exist. Baseline it
   once instead, which records the revision without executing anything:

   ```bash
   DATABASE_URL="<neon connection string>" alembic stamp head
   ```

### Schema changes after the first deploy

`Base.metadata.create_all()` is **not** a migration tool: it creates missing
tables and silently ignores existing ones. Adding a column to `models.py`
under `create_all` reports success, changes nothing, and the app then fails
at runtime on a column the database doesn't have — and the instinctive fix,
dropping and recreating, destroys the OAuth tokens, which can't be
regenerated without every user re-authorising. `init_db()` now refuses to
run outside development for exactly that reason.

So changes go through Alembic:

```bash
# 1. Edit models.py, then generate the migration
alembic revision --autogenerate -m "add users.device_pref"

# 2. READ THE GENERATED FILE. Autogenerate misses things — table renames
#    look like drop+create (which loses the data), server defaults and
#    check constraints aren't detected, and a NOT NULL column added to a
#    populated table needs a default or a backfill or it fails.
$EDITOR alembic/versions/<new_file>.py

# 3. Preview the exact SQL without running it
DATABASE_URL="<neon url>" alembic upgrade head --sql

# 4. Apply
DATABASE_URL="<neon url>" alembic upgrade head
```

Migrations run as a **deliberate step before deploying**, not on app
startup. On startup they'd race between concurrent Cloud Run instances
during a cold start, and a failed migration would crash-loop the service
instead of just failing the deploy.

Neon's branching makes rehearsal free: branch the production database, run
the migration against the branch, confirm, then run it against the parent.

The app stays on the synchronous `psycopg2` driver (plain `postgresql://`,
not `postgresql+asyncpg://`) — every route already uses a sync SQLAlchemy
`Session`, and Neon speaks standard Postgres wire protocol fine over it.
Switching the whole app to an async driver would be a much bigger change
than this deploy needs. `database.py` uses `NullPool` outside of
`FASTAPI_ENV=development` so no persistent pool survives a cold start.

## 2. Secrets

Don't commit `.env`. Put the real values in Secret Manager and inject them
at deploy time instead:

```bash
for name in GEMINI_API_KEY GOOGLE_CLIENT_SECRET TELEGRAM_BOT_TOKEN \
            TELEGRAM_WEBHOOK_SECRET DATABASE_URL SECRET_KEY; do
  gcloud secrets create "$name" --replication-policy=automatic
done
# then, per secret:
echo -n "<value>" | gcloud secrets versions add GEMINI_API_KEY --data-file=-

# TELEGRAM_WEBHOOK_SECRET has no external source — generate it:
openssl rand -hex 32 | tr -d '\n' \
  | gcloud secrets versions add TELEGRAM_WEBHOOK_SECRET --data-file=-
```

Use `echo -n` / `tr -d '\n'` throughout — a trailing newline becomes part of
the secret value and produces authentication failures that look like a wrong
credential.

The Cloud Run runtime service account needs read access:

```bash
for name in GEMINI_API_KEY GOOGLE_CLIENT_SECRET TELEGRAM_BOT_TOKEN \
            TELEGRAM_WEBHOOK_SECRET DATABASE_URL SECRET_KEY; do
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:<cloud-run-runtime-sa>@<project-id>.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 3. Deploy to Cloud Run

```bash
gcloud run deploy health-assistant \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --concurrency=80 \
  --memory=512Mi \
  --cpu=1 \
  --set-env-vars="FASTAPI_ENV=production,GEMINI_MODEL=gemini-3.5-flash-lite,GOOGLE_REDIRECT_URI=https://<your-service-url>/auth/callback,TELEGRAM_WEBHOOK_URL=https://<your-service-url>/webhook/telegram" \
  --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest,GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest,DATABASE_URL=DATABASE_URL:latest,SECRET_KEY=SECRET_KEY:latest"
```

Scaling flags, and why each one:

- `--min-instances=0` — the single most important one for cost. Setting it
  to 1 bills continuously and takes you off the free tier entirely. Cold
  starts are the tradeoff, and they're fine for a service that handles a
  handful of requests a day.
- `--concurrency=80` — every request here is I/O-bound (waiting on Gemini,
  the Health API, Telegram) and the app is `async` throughout, so one
  instance can hold many in flight while using almost no CPU. High
  concurrency means the daily fan-out lands on *one* instance instead of
  scaling out to several, which is both cheaper and faster.
- `--memory=512Mi --cpu=1` — the smallest pairing that comfortably fits the
  Python deps. Cloud Run bills per vCPU-second and GiB-second *while a
  request is in flight*, so smaller directly means cheaper.
- `--max-instances=3` — a spend ceiling, not a capacity target. Even a
  runaway retry loop can't scale past 3 instances.

`--allow-unauthenticated` is required — Telegram must be able to reach
`POST /webhook/telegram`. The `/internal/*` endpoints protect themselves
instead (steps 4 and 5).

After the first deploy, note the service URL (`https://health-assistant-xxxxx-uc.a.run.app`)
and:
- Register it as the OAuth redirect URI (`<url>/auth/callback`) in the
  Google Cloud Console.
- Register `<url>/webhook/telegram` with Telegram, **including the secret
  token** — the endpoint rejects deliveries without it in production:

  ```bash
  curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
    -d "url=https://<your-service-url>/webhook/telegram" \
    -d "secret_token=$(gcloud secrets versions access latest --secret=TELEGRAM_WEBHOOK_SECRET)"
  ```

  Telegram echoes this back in the `X-Telegram-Bot-Api-Secret-Token` header
  on every delivery. It's what separates a genuine update from a forged one:
  the webhook has to stay publicly reachable (Telegram can't present an OIDC
  token the way Cloud Scheduler does), so without the secret anyone who
  learns the URL can POST an update carrying someone else's `chat_id` —
  burning their Gemini quota, reading their summaries back, or spoofing
  `/disconnect`.

  Verify it took, and that nothing is failing:
  ```bash
  curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo" | python3 -m json.tool
  ```
  `has_custom_certificate: false` and an empty `last_error_message` are what
  you want. `getWebhookInfo` does not echo the secret back — to confirm it
  matches, send the bot a message and check the request wasn't 403'd.
- Set `GOOGLE_REDIRECT_URI` and `TELEGRAM_WEBHOOK_URL` env vars to match
  (redeploy, or `gcloud run services update` with `--set-env-vars`).

## 4. Cloud Scheduler → `/internal/run-daily`

Create a dedicated service account for the scheduler job — don't reuse the
Cloud Run runtime service account:

```bash
gcloud iam service-accounts create health-assistant-scheduler \
  --display-name="Health Assistant daily-run scheduler"

gcloud run services add-iam-policy-binding health-assistant \
  --region us-central1 \
  --member="serviceAccount:health-assistant-scheduler@<project-id>.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

Then create the job with OIDC auth, and set `RUN_DAILY_AUDIENCE` /
`SCHEDULER_SERVICE_ACCOUNT_EMAIL` on the Cloud Run service so it knows who
to trust:

```bash
gcloud scheduler jobs create http health-assistant-daily \
  --location us-central1 \
  --schedule="0 7 * * *" \
  --time-zone="UTC" \
  --uri="https://<your-service-url>/internal/run-daily" \
  --http-method=POST \
  --oidc-service-account-email="health-assistant-scheduler@<project-id>.iam.gserviceaccount.com" \
  --oidc-token-audience="https://<your-service-url>" \
  --attempt-deadline=180s \
  --max-retry-attempts=3 \
  --min-backoff=30s \
  --max-backoff=300s \
  --max-retry-duration=1800s

gcloud run services update health-assistant \
  --region us-central1 \
  --set-env-vars="RUN_DAILY_AUDIENCE=https://<your-service-url>,SCHEDULER_SERVICE_ACCOUNT_EMAIL=health-assistant-scheduler@<project-id>.iam.gserviceaccount.com"
```

`/internal/run-daily` verifies the token's audience and that the
service-account email matches exactly — anything else gets a 403.
`--attempt-deadline` caps at 30 minutes; 180s is far more than needed now
that this endpoint only enqueues tasks and returns — it does no per-user
work itself, so its runtime barely moves as users are added.

The retry flags bound the *dispatcher's* failures the same way the queue
bounds the workers'. Cloud Scheduler also defaults to unlimited retries; a
database outage at 07:00 would otherwise retry all day. Three attempts over
at most 30 minutes is the right shape for a job whose output is a *morning*
summary — after that, skip the day rather than deliver it at noon.

Re-enqueueing on a scheduler retry is safe: task names are deterministic, so
users already queued by the failed attempt are rejected as duplicates rather
than summarised twice.

## 5. Cloud Tasks queue → `/internal/run-single-user`

Create the queue the daily run fans out into. The rate limits are the whole
point: they spread the batch out so no single instance is ever doing more
than a couple of summaries at once.

```bash
gcloud services enable cloudtasks.googleapis.com

gcloud tasks queues create health-assistant-daily-queue \
  --location=us-central1 \
  --max-dispatches-per-second=5 \
  --max-concurrent-dispatches=10 \
  --max-attempts=3 \
  --min-backoff=10s \
  --max-backoff=300s \
  --max-retry-duration=3600s
```

- `--max-dispatches-per-second=5` is the knob that keeps RAM and CPU flat.
  Without it Cloud Tasks fires the whole batch at once and you're back to a
  spike.
- `--max-attempts=3` bounds the retries. **The default is `-1` — unlimited**,
  which for a daily job means a permanently broken user is retried forever,
  quietly burning quota and Gemini tokens. Three attempts covers a network
  glitch or a rate limit; anything still failing after that is a real
  problem that a fourth attempt won't fix.
- `--max-retry-duration=3600s` is the belt to that braces: even if attempts
  remain, stop retrying a task after an hour. A summary delivered at 3 PM
  isn't a morning summary any more.
- `--min-backoff=10s --max-backoff=300s` — exponential backoff between
  attempts, so a rate-limited run backs off rather than hammering.

These pair with the endpoint's own classification: `/run-single-user`
returns 503 only for failures a retry could fix, and 200 for anything
permanent, so the three attempts are spent on problems that might actually
resolve.

The queue's tasks authenticate as a service account, which needs invoker
access the same way the scheduler does. Reusing the scheduler's SA is fine;
`TASKS_SERVICE_ACCOUNT_EMAIL` defaults to `SCHEDULER_SERVICE_ACCOUNT_EMAIL`
when unset. The Cloud Run runtime SA also needs permission to *enqueue*:

```bash
# Let the running service create tasks in the queue.
gcloud tasks queues add-iam-policy-binding health-assistant-daily-queue \
  --location=us-central1 \
  --member="serviceAccount:<cloud-run-runtime-sa>@<project-id>.iam.gserviceaccount.com" \
  --role="roles/cloudtasks.enqueuer"

# Let the service account tasks run as mint OIDC tokens for itself.
gcloud iam service-accounts add-iam-policy-binding \
  health-assistant-scheduler@<project-id>.iam.gserviceaccount.com \
  --member="serviceAccount:<cloud-run-runtime-sa>@<project-id>.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

Then point the service at the queue:

```bash
gcloud run services update health-assistant \
  --region us-central1 \
  --set-env-vars="TASKS_QUEUE=health-assistant-daily-queue,TASKS_LOCATION=us-central1,TASKS_TARGET_BASE_URL=https://<your-service-url>"
```

If `TASKS_QUEUE` is unset the app logs a warning and runs the batch inline
instead — that's the local-dev path, not something to rely on in production.

**Duplicate protection.** Tasks are created with a deterministic name
(`daily-{user_id}-{YYYY-MM-DD}`), so a retried Scheduler attempt that
re-enqueues gets `ALREADY_EXISTS` rather than sending a second summary.
`/internal/run-single-user` independently re-checks `last_summary_sent`
before doing any work, which covers the case where a send succeeded but its
response was lost.

## 6. Logging & observability

Nothing to provision — Cloud Run ships stdout to Cloud Logging
automatically. What matters is the *shape* of what's written. In production
(`FASTAPI_ENV != development`) the app emits one JSON object per line, which
Cloud Logging parses into structured fields instead of an opaque string.

That makes the questions you actually ask during an incident one-liners in
the Logs Explorer:

```
# Everything that broke today
severity>=ERROR

# One user's entire history, across every run
jsonPayload.user_id="abc-123"

# Which users missed their summary, and why
jsonPayload.event="summary_failed"

# Failures that were NOT retried — these need a human
jsonPayload.transient=false

# Unhandled exceptions (these should always be zero)
jsonPayload.event="unhandled_exception"
```

Events the daily run emits: `summary_sent`, `summary_failed`,
`health_fetch_failed`, `health_partial`, `health_total_outage`,
`telegram_send_failed`, `enqueue_failed`, `unhandled_exception`. Each
carries `user_id`, and failures carry `transient` so you can separate "will
retry itself" from "needs attention".

A useful standing alert — daily summaries silently stopping is the failure
you'd otherwise notice weeks late:

```bash
gcloud logging metrics create daily_summary_failures \
  --description="Users whose daily summary failed permanently" \
  --log-filter='resource.type="cloud_run_revision"
    jsonPayload.event="summary_failed" jsonPayload.transient=false'
```

Then attach an alerting policy on that metric in Cloud Monitoring.

### Error Reporting

```bash
gcloud services enable clouderrorreporting.googleapis.com
```

Nothing else to wire up — Error Reporting reads the same log stream. It
ingests an entry when that entry carries

```
"@type": "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
```

**and** has the stack trace inside `message`. Both are handled by
`utils/logging_config.py`; a trace filed on any other key is silently
ignored, which is the usual reason "I enabled Error Reporting and nothing
shows up".

What reaches it, deliberately:

| Reaches Error Reporting | Does not |
|---|---|
| Unhandled exceptions (the catch-all handler) | A user's refresh token expiring |
| Failed task enqueues | A user blocking the bot |
| Total Health API outage (`report=True`) | One metric missing for one user |

The split is intentional. Error Reporting is for *the system* being broken;
per-user operational failures are expected and would page you every morning
once someone's 7-day consent lapses. Those are covered by the log-based
metric above instead. To add a new one, pass `report=True` to `log_event`.

Set up notifications in the console (Error Reporting → Configure
notifications) so new error *types* email you — it notifies on the first
occurrence of a novel signature rather than on every repeat.

Free tier covers this at any volume this project will produce.

**Cost.** 5 GB/month of ingestion is free, and the `_Default` bucket keeps
logs 30 days at no charge. This service produces a few hundred lines a day —
low single-digit MB a month. The one way to blow past that is leaving
`LOG_LEVEL=DEBUG` on in production, which makes the HTTP libraries log every
request and response body; keep it at `INFO`.

Locally, logs stay human-readable — JSON lines are unreadable in a terminal
and there's no Logs Explorer to compensate.

## 7. Budget alert

Free tier requires a card on file and Google does not cap spend on its own:

```bash
gcloud billing budgets create \
  --billing-account=<billing-account-id> \
  --display-name="health-assistant $1 alert" \
  --budget-amount=1USD \
  --threshold-rule=percent=1.0
```

## 8. Sanity check

```bash
curl https://<your-service-url>/health          # {"status": "healthy", ...}
curl https://<your-service-url>/mcp/tools       # tool schema
```

`/health` executes `SELECT 1` against Neon and returns 503 if the database
is unreachable — worth hitting once after every deploy.

Force a daily run without waiting for 07:00, then watch the fan-out:

```bash
gcloud scheduler jobs run health-assistant-daily --location us-central1
gcloud tasks queues describe health-assistant-daily-queue --location us-central1
```

The `/run-daily` response reports `"mode": "queued"` and lists the user ids
it enqueued. `"mode": "inline"` means `TASKS_QUEUE` didn't reach the
service.

## 9. Cost: what actually stays free

Every component is chosen to sit inside a permanent free tier at personal
scale. The fan-out turns one request a day into N+1, which sounds worse and
isn't — the free allowances are orders of magnitude above what this uses.

| Service | Free allowance (per month) | This project's usage |
|---|---|---|
| Cloud Run requests | 2,000,000 | daily fan-out + Telegram webhooks — hundreds |
| Cloud Run compute | 180k vCPU-s, 360k GiB-s | seconds per request, scale-to-zero between |
| Cloud Tasks | 1,000,000 operations | one per user per day |
| Cloud Scheduler | 3 jobs | 1 |
| Neon Postgres | 0.5 GB storage | a few summary rows a day |
| Gemini API | free tier on flash-lite | 1 summary + ad-hoc queries per user |
| Telegram Bot API | unlimited, free | — |
| Secret Manager | 6 active versions, 10k accesses | 5 secrets |

The two things that would actually cost money, in order of likelihood:

1. **`--min-instances` above 0.** Billed continuously, ~$5–15/month. Never
   set it.
2. **Secret Manager versions.** Each rotation adds a version; keep at most 6
   active per secret and destroy old ones (`gcloud secrets versions destroy`).

Set the budget alert in step 7 regardless — free tier requires a card on
file and Google does not cap spend on its own.

## 10. Going public: OAuth consent screen

While the consent screen is in **Testing**, two limits apply: at most 100
test users (each added by hand in the console), and **refresh tokens expire
after 7 days**, so every user re-authorises weekly. Moving to **Production**
removes both.

### Read this before starting

The `googlehealth.*` scopes this app uses are **Restricted** — Google's
strictest tier, above "sensitive". Publishing an app that requests them
requires OAuth verification, which for restricted scopes means:

1. A **privacy policy** and **terms of service**, publicly hosted, at a
   domain you own and have verified in Search Console.
2. A **demo video** showing the OAuth flow and how each scope's data is used.
3. A written justification for each scope.
4. An **annual third-party security assessment** (CASA). This is the
   expensive part — it's a paid engagement with an approved assessor,
   renewed every year, and pricing depends on tier and assessor.

Steps 1–3 cost time. Step 4 costs money, recurring, and applies to
restricted scopes specifically.

> **Verify the current requirements yourself** at
> <https://support.google.com/cloud/answer/9110914> before committing —
> Google revises these tiers, and the assessment requirement in particular
> has changed more than once.

### Recommendation

For a personal deployment — you and maybe a few family members — **stay in
Testing**. Re-authorising weekly is a genuine annoyance, but it's free, and
100 test users is far beyond what a personal project needs. The assessment
is a recurring bill against an app with no revenue, which is at odds with
the zero-cost constraint the rest of this document is built around.

Revisit when there's a reason to: real external users, or a scale where
weekly re-auth is what's actually blocking you.

### If you're proceeding anyway

1. Publish a privacy policy and terms of service. GitHub Pages hosts them
   free, but the domain must be one you can verify — a `github.io` subdomain
   generally won't satisfy verification.
2. Verify domain ownership in
   [Search Console](https://search.google.com/search-console), then add it
   under *APIs & Services → OAuth consent screen → Authorised domains*.
3. Fill in the app name, support email, logo, and the policy/ToS links.
4. Record the demo video: the full consent flow, then each scope's data
   visibly in use.
5. *OAuth consent screen → Publish app*, then **Submit for verification**.
6. Expect weeks, and expect follow-up questions. The app keeps working for
   existing test users throughout.

**During review, don't change scopes.** Adding one resets the process.

### Cheaper middle ground

If the 7-day expiry is the only real pain, consider an **Internal** consent
screen instead — no verification, no expiry, unlimited users *within your
Google Workspace organisation*. Requires a Workspace account (which is
itself paid), and won't work for arbitrary Telegram users, but for a
household on one Workspace domain it sidesteps the assessment entirely.

### What the app does either way

Nothing here needs a code change. `routes/auth.py` already requests
`access_type=offline` with `prompt=consent`, which is what reliably returns
a refresh token in both modes. The only difference is how long that token
survives.
