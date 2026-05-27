# Mawa Concierge — WhatsApp AI Receptionist

A WhatsApp AI receptionist for [Mawa.om](https://www.mawa.om) Omani real estate, built by **Bznsflow** as a pitch asset.

**Layla** (ليلى) searches live listings scraped from Mawa.om and converses in Arabic or English via WhatsApp.

---

## Architecture

Two independent scripts sharing one Supabase database:

| Component | File | Purpose |
|-----------|------|---------|
| Scraper | `scrape_mawa.py` | Nightly Playwright scrape → Supabase |
| Bot | `bot.py` | FastAPI + Groq LLM → Twilio WhatsApp |

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Supabase project
- Twilio account with WhatsApp sandbox
- Groq API key

### 2. Setup

```bash
cp .env.example .env
# Fill in all values in .env

pip install -r requirements.txt
playwright install chromium
```

### 3. Database

Run `supabase_schema.sql` once in your Supabase SQL Editor.

### 4. Run the scraper (test mode)

```bash
python scrape_mawa.py --limit 10
```

Check your Supabase `listings` table for rows.

### 5. Run the bot

```bash
uvicorn bot:app --reload --port 8000
```

Expose it with ngrok for local Twilio webhook testing:

```bash
ngrok http 8000
```

Set the ngrok HTTPS URL + `/webhook/twilio` as your Twilio WhatsApp sandbox webhook.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✅ | Your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | Service role key (bypasses RLS) |
| `GROQ_API_KEY` | ✅ | Groq API key |
| `TWILIO_ACCOUNT_SID` | ✅ | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | ✅ | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | ✅ | Twilio sandbox number |
| `LEAD_NOTIFICATION_WHATSAPP` | ✅ | Your WhatsApp for lead alerts |
| `SCRAPER_ENABLED` | — | Set `false` to disable scraper |
| `TWILIO_WEBHOOK_VALIDATE` | — | Set `false` for local dev |
| `MAWA_REQUEST_DELAY_SECONDS` | — | Politeness delay (default: 5) |

---

## Deploy to Railway

1. Push this repo to GitHub
2. New Railway project → Deploy from GitHub
3. Add all env vars in Railway dashboard
4. Railway auto-detects `deploy/Dockerfile`
5. Set `APP_PORT=8000` (Railway forwards automatically)

For the nightly scraper, add a Railway Cron job:
```
python scrape_mawa.py
```
Schedule: `0 2 * * *` (2 AM GST = 22:00 UTC)

---

## Demo Flow

Send from Ahmed's phone to the Twilio sandbox number:

> أبغى شقة للإيجار في الموج غرفتين بميزانية 800 ريال

Expected:
1. Layla replies in Arabic with up to 3 matching listings
2. Ahmed picks one and provides his name
3. Lead row appears in Supabase `leads` table
4. Notification arrives on `LEAD_NOTIFICATION_WHATSAPP`

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check |
| `POST` | `/webhook/twilio` | Twilio WhatsApp webhook |

---

Built by [Bznsflow](https://bznsflow.com) · Ahmed Darwish · Muscat, Oman
