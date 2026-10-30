# Amazon.ie Price Error Tracker

Watch Amazon.ie product URLs and get **immediate Telegram** (or optional Twilio WhatsApp) messages when a listing looks like a pricing mistake.

## How it works

1. You add product URLs (or ASINs) via the web dashboard or API.
2. A background monitor polls Amazon.ie on a configurable interval.
3. When the current price is far below the struck-through “was” price (default **≥75% off** and was price **≥€20**), or far below recent history for that SKU, an alert is stored and notifications are sent right away.
4. Duplicate alerts for the same price are suppressed for **30 minutes** (configurable).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — at minimum set Telegram variables (see below)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open **http://localhost:8080**, paste an Amazon.ie product link, and click **Watch**.

## Telegram setup (recommended)

1. In Telegram, chat with [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **bot token**.
2. Start a chat with your bot (send any message).
3. Get your **chat id** (e.g. message [@userinfobot](https://t.me/userinfobot) or call `getUpdates` on the bot API).
4. Put these in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Restart the app. The dashboard status line should show **Telegram: ready**.

## WhatsApp (optional, via Twilio)

Fill in `TWILIO_*` variables in `.env.example`. Twilio’s WhatsApp sandbox is the easiest way to test.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `POLL_INTERVAL_SECONDS` | 45 | Seconds between full watch-list scans |
| `REQUEST_DELAY_SECONDS` | 2 | Pause between product requests |
| `MIN_DISCOUNT_PERCENT` | 75 | Minimum discount to treat as a price error |
| `MIN_LIST_PRICE_EUR` | 20 | Ignore tiny “was” prices |
| `MAX_ALERT_PRICE_EUR` | 500 | Ignore alerts above this price |
| `COOLDOWN_MINUTES` | 30 | Don’t re-notify the same price |

## API

- `GET /api/health` — notifier status
- `GET /api/products` — watch list
- `POST /api/products` — `{ "url": "https://www.amazon.ie/dp/..." }`
- `GET /api/alerts` — recent detections

## Notes

- Amazon may rate-limit or block automated requests; keep `REQUEST_DELAY_SECONDS` reasonable and only watch products you care about.
- This tool flags **likely** misprices using heuristics; always verify on Amazon before buying.
- Price history is stored in SQLite at `data/tracker.db`.

## Tests

```bash
python -m unittest discover -s tests -v
```
