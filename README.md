# Amazon.ie Price Error Tracker

GitHub Actions scans your Amazon.ie watch list about every 20 minutes, rewrites **[TRACKER.md](TRACKER.md)** so you can follow prices in the repo, and can send a **Telegram message as soon as a scan finds a likely price error**.

**Track here:** [TRACKER.md](TRACKER.md)

This repo is **private**. GitHub Pages (`https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/`) only works if you make the repo public, or if the account has GitHub Pro. The workflow still tries to publish Pages when GitHub allows it.

## Use the tracker

1. Merge to `main` (scheduled scans only run on the default branch).
2. Add GitHub Actions **secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Add products:
   - Open a [Watch issue](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/issues/new?template=watch.yml), or
   - Put URLs/ASINs in [`watchlist.json`](watchlist.json)
4. Wait for the scheduled scan, or run **Actions → Scan and publish tracker → Run workflow**.
5. Refresh [TRACKER.md](TRACKER.md) (times are Ireland / Europe/Dublin).

Optional Pages: **Settings → Pages → Source: GitHub Actions**, and make the repo public if you want a standalone URL.

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

- Watch issues and Pages are visible to anyone if you make the repo public.
- Amazon may throttle GitHub-hosted scrapes; keep the watch list small.
- Alerts are heuristic — verify on Amazon before buying.

## Tests

```bash
python -m unittest discover -s tests -v
```
