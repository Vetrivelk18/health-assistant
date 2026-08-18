# Probe the Google Health API

Answers the one question that decides the project: **does the API return real
data for your account?**

Standalone — no FastAPI, no database, no Telegram.

---

## Why the old module was wrong

`services/google_health.py` previously targeted **`fitness.googleapis.com`** —
the Google Fit REST API. Two problems:

1. *"The Google Fit APIs, including the Google Fit REST API, will be deprecated
   in 2026. As of May 1, 2024, developers cannot sign up to use these APIs."*
   A Google Cloud project created in 2026 cannot enable it at all.
2. The request shape was wrong anyway (`GET /dataset:read` with query params).

The successor — and the migration target the Fitbit portal banner pointed at —
is the **Google Health API** at `health.googleapis.com`. It reads from Fitbit
devices and Pixel Watches, which is exactly this project's data source.

---

## Setup

### 1. Enable the API

Google Cloud console → **APIs & Services → Library** → search
**"Google Health API"** → **Enable**.

Project: `health-assistant-505718`

> If it cannot be enabled, stop here and tell me — that changes the plan again.

### 2. Replace the scopes

The old Fit scopes are dead. In the OAuth consent screen, remove any
`.../auth/fitness.*` scopes and add:

```
https://www.googleapis.com/auth/googlehealth.sleep.readonly
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
```

These are **Restricted** scopes. While the app is in *Testing* you may add
yourself as a test user and proceed — no verification needed.

### 3. Add yourself as a test user

OAuth consent screen → **Audience** → **Test users** → add your Google account.
Must be the account whose Fitbit or Pixel Watch data you want to read.

### 4. Confirm the redirect URI

`http://localhost:5000/auth/callback` must be listed on the OAuth client,
character for character.

### 5. Run it

```bash
pip install httpx python-dotenv
python3 probe_health_api.py
```

A browser opens, you approve, and the script prints a table:

```
==========================================================
DATA TYPE         RESULT                  VIA
==========================================================
sleep             12 points               filtered
steps             1440 points             filtered
heart_rate        288 points              filtered
active_minutes    empty                   filtered
calories          24 points               filtered
==========================================================
```

Raw responses land in `fixtures/*.json`.

---

## Reading the result

| Outcome | Meaning |
|---|---|
| Points returned | ✅ The project works. Build the summary generator against the fixtures. |
| `empty` everywhere | API reachable but no data — is the Google account actually linked to a device that synced recently? |
| `401` | Token or scope problem. Re-run; check the granted scopes in the output. |
| `403` | API not enabled on the project, or the scope was not granted. |
| `404` | Wrong data type path. Report it — the names may have changed. |
| `FAIL` on everything | Read the note column; the status code says which. |

The script also discovers the **filter grammar** by trying candidates until one
is accepted, then prints the winner. Copy it into `FILTER_TEMPLATE_IN_USE` in
`services/google_health.py` and the client is finished.

---

## ⚠️ The seven-day problem

While the OAuth consent screen is in **Testing**, Google expires refresh tokens
after **7 days**. A daily-summary product that silently stops after a week is
the exact failure this design tries to avoid.

Two ways out:

- **Personal use:** re-run `/connect` weekly, or publish the app to *In
  production*.
- **Real users:** OAuth verification — a third-party security review plus a
  published privacy policy and terms of service. Budget weeks, not hours.

Doesn't block the probe. Does decide whether this stays a personal tool or
becomes a product. Worth knowing before you build another phase.

---

## What the probe proves

- Google Health API is enabled and reachable
- Your scopes are granted
- A refresh token was issued (printed explicitly)
- Which data types have data
- What the payloads actually look like
- Which filter grammar works

With `fixtures/` populated, every later phase can be built and iterated offline
in seconds — no browser, no consent screen, no waiting until 7 a.m.
