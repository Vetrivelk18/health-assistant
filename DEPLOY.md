# Deploying to Cloud Run + Cloud Scheduler + Neon

Cloud Run containers don't stay alive between requests, so there's no
in-process scheduler here — Cloud Scheduler calls `POST /internal/run-daily`
once a day and the whole job (refresh token → fetch → summarise → store →
send) runs synchronously inside that one request.

## 1. Neon Postgres

1. Create a project at [neon.tech](https://neon.tech) (free tier is enough
   for personal use).
2. Copy the connection string from the Neon dashboard. It comes back as
   plain `postgresql://user:pass@ep-xxxx.neon.tech/dbname` — append
   `?sslmode=require` (Neon requires TLS):

   ```
   DATABASE_URL=postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/health_assistant?sslmode=require
   ```

3. Apply the schema once, from your machine, before the first deploy:

   ```bash
   DATABASE_URL="<neon connection string from above>" \
     python -c "from database import init_db; init_db()"
   ```

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
for name in GEMINI_API_KEY GOOGLE_CLIENT_SECRET TELEGRAM_BOT_TOKEN DATABASE_URL SECRET_KEY; do
  gcloud secrets create "$name" --replication-policy=automatic
done
# then, per secret:
echo -n "<value>" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
```

## 3. Deploy to Cloud Run

```bash
gcloud run deploy health-assistant \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --set-env-vars="FASTAPI_ENV=production,GEMINI_MODEL=gemini-3.5-flash-lite,GOOGLE_REDIRECT_URI=https://<your-service-url>/auth/callback,TELEGRAM_WEBHOOK_URL=https://<your-service-url>/webhook/telegram" \
  --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest,GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,DATABASE_URL=DATABASE_URL:latest,SECRET_KEY=SECRET_KEY:latest"
```

- `--allow-unauthenticated` is required — Telegram must be able to reach
  `POST /webhook/telegram`. `POST /internal/run-daily` protects itself
  instead (see step 5).
- `--min-instances=0` matters: setting it to 1 bills continuously and takes
  you off the free tier. Cold starts are the tradeoff; they're fine for a
  service that gets a handful of requests a day.

After the first deploy, note the service URL (`https://health-assistant-xxxxx-uc.a.run.app`)
and:
- Register it as the OAuth redirect URI (`<url>/auth/callback`) in the
  Google Cloud Console.
- Register `<url>/webhook/telegram` with Telegram:
  `curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<url>/webhook/telegram"`.
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
  --attempt-deadline=180s

gcloud run services update health-assistant \
  --region us-central1 \
  --set-env-vars="RUN_DAILY_AUDIENCE=https://<your-service-url>,SCHEDULER_SERVICE_ACCOUNT_EMAIL=health-assistant-scheduler@<project-id>.iam.gserviceaccount.com"
```

`/internal/run-daily` verifies the token's audience and that the
service-account email matches exactly — anything else gets a 403.
`--attempt-deadline` caps at 30 minutes; 180s (3 min, the default) is ample
since the whole batch runs synchronously per user with no background work.

Note: `/internal/run-daily` currently loops over every connected user and
sends each summary as soon as it's generated. If Cloud Scheduler retries a
timed-out attempt, a user whose `last_summary_sent` hadn't been stamped yet
could receive two messages that day — acceptable for personal use with a
handful of users; worth a `pg_try_advisory_lock` guard around the whole
batch if this grows into a shared multi-user product.

## 5. Budget alert

Free tier requires a card on file and Google does not cap spend on its own:

```bash
gcloud billing budgets create \
  --billing-account=<billing-account-id> \
  --display-name="health-assistant $1 alert" \
  --budget-amount=1USD \
  --threshold-rule=percent=1.0
```

## 6. Sanity check

```bash
curl https://<your-service-url>/health          # {"status": "healthy", ...}
curl https://<your-service-url>/mcp/tools       # tool schema
```

`/health` executes `SELECT 1` against Neon and returns 503 if the database
is unreachable — worth hitting once after every deploy.
