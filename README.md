# Amazon.ie Price Error Tracker

Track Amazon.ie listings on a **GitHub Pages dashboard**. GitHub Actions scans your watch list about every 20 minutes, updates the page, and can send a **Telegram message as soon as a scan finds a likely price error**.

**Live tracker:** [https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/)

## Use the GitHub page

1. Merge this repo to `main` (GitHub only runs the Pages workflow on the default branch).
2. Repo **Settings → Pages → Source: GitHub Actions** (the first workflow run usually creates this).
3. Add GitHub Actions **secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Add products:
   - From the Pages site (opens a `watch` issue), or
   - Put URLs/ASINs in [`watchlist.json`](watchlist.json)
5. Wait for the scheduled scan, or run **Actions → Scan and publish tracker → Run workflow**.

The dashboard URL is:

`https://<you>.github.io/amazon-ie-price-error-tracker/`

Times on the page are shown in **Ireland (Europe/Dublin)**.

### Telegram

1. [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message your bot once, then put your chat id in `TELEGRAM_CHAT_ID` (e.g. via [@userinfobot](https://t.me/userinfobot)).

Optional WhatsApp: set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WHATSAPP_TO` as secrets too.

A “price error” is a **≥75% drop** from the struck-through list price (was ≥ €20), or the same drop vs recent history for that ASIN. Duplicate alerts for the same price are suppressed for 30 minutes.

GitHub’s scheduler is not second-by-second; ~20 minutes is as close as Pages hosting can get. For a tighter loop, run the local server below.

## Local server (optional)

```bash
pip install -r requirements.txt
cp .env.example .env
# set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open http://localhost:8080 — this polls every 45 seconds instead of waiting on Actions.

One-shot scan (same path GitHub Actions uses):

```bash
python -m scripts.scan --watchlist watchlist.json --output docs/data.json
```

## Configuration

| Variable / secret | Default | Meaning |
|-------------------|---------|---------|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Instant Telegram alerts |
| `POLL_INTERVAL_SECONDS` | 45 | Local server only |
| `REQUEST_DELAY_SECONDS` | 2 | Pause between Amazon requests |
| `MIN_DISCOUNT_PERCENT` | 75 | Minimum discount to treat as a price error |
| `MIN_LIST_PRICE_EUR` | 20 | Ignore tiny “was” prices |
| `MAX_ALERT_PRICE_EUR` | 500 | Ignore alerts above this price |
| `COOLDOWN_MINUTES` | 30 | Don’t re-notify the same price |

## Notes

- The Pages site and `watch` issues are **public** if the repo is public.
- Amazon may throttle GitHub-hosted scrapes; keep the watch list small.
- Alerts are heuristic — verify on Amazon before buying.

## Tests

```bash
python -m unittest discover -s tests -v
```
