# Amazon.ie Price Error Tracker

### [Open the price tracker dashboard](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/)

Every 30 minutes, GitHub Actions checks Amazon.ie for price errors. It covers about 10,000 bestsellers plus your own watch list, and sends a **Telegram message as soon as a likely price error turns up**.

**Dashboard:** [paddykeoghgoode.github.io/amazon-ie-price-error-tracker](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/) (times are Ireland / Europe/Dublin)

## Set up

1. Add GitHub Actions **secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
2. Run **Actions → Scan and publish tracker → Run workflow** once. You should get a "✅ connected" message on Telegram after the first bestseller section crawl (~5–8 minutes). The watchlist scan also sends a short summary each run.
3. After that it runs by itself at :05 and :35 past every hour. The dashboard and Telegram update **as each scan finishes** (watchlist and one bestseller section per run), not only when the whole workflow is done.

Settings → Pages → Source must be **GitHub Actions**, and the repo must be **public**. Scheduled bestseller crawls take about 16 minutes each, which is only free on public repos. On a private repo, only manual runs crawl bestsellers.

### Telegram

1. [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message your bot once, then put your chat id in `TELEGRAM_CHAT_ID` (e.g. via [@userinfobot](https://t.me/userinfobot)).

Optional WhatsApp: set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WHATSAPP_TO` as secrets too.

Besides price-error alerts, the bot sends:

- a **welcome** message the first time it's connected;
- a **warning** if Amazon blocks the crawl or it finds suspiciously few items (at most every 12 hours), and a message when it recovers;
- a **daily summary** after 09:00 Ireland time, so silence never means "broken". Change or disable it with `daily_summary_hour` in [`bestsellers.json`](bestsellers.json) (`null` turns it off).

## Bestseller scan (thousands of items, no manual adding)

Each scheduled run crawls **one section** from [`bestsellers.json`](bestsellers.json) (~5–8 minutes), **publishes the dashboard**, sends a **section summary on Telegram**, and rotates **gaming → home → lifestyle → general** every 30 minutes. Run **Actions → Scan and publish tracker** with section **all** when you want a full crawl (~20 minutes, with a publish after each section).

- Bestseller pages don't show a "was" price, so each item is judged against **its own price history**. It needs **three prior, consistent price observations** (within 20% of their median) before a stable reference can be established. On the fourth observation, a price ≥75% below that reference (reference ≥€20) can trigger an alert. The reference is kept separate from the newest price, so a prolonged sale cannot immediately erase the original drop.
- Items sold in several variants at different prices (for example "€9.99 – €24.99") are skipped, because their lowest price jumps between variants.
- One crawl sends at most 15 Telegram alerts, plus a single "…and N more" message. The same item won't alert again within 24 hours unless its price drops further.
- The dashboard also lists **deep drops** at **40%, 50% and 66%** below each item's recent Amazon price (handy for electronics and games even when it's not a full "price error").
- For those deep drops, the crawler looks up a **CeX.ie** match and estimates **cash trade-in** profit after configured costs and profit thresholds (`cex_min_cash_profit_eur` / `cex_min_cash_profit_percent` in [`bestsellers.json`](bestsellers.json)). Matching rejects obvious model, storage, platform, edition and pack-size mismatches, and validates the CeX box-detail title again. **Still verify exact SKU, region, condition, accessories and grade manually before buying.** Set `"cex_enabled": false` to turn CeX lookups off.
- It also scans **regular-price** gaming/electronics bestsellers (`cex_scan_any_price`) and lists items where CeX cash trade-in still clears your margin **without** an Amazon sale (`cex_arbitrage` on the dashboard). Tune keywords with `cex_priority_keywords`. Category paths are the part of an Amazon.ie bestseller URL after `amazon.ie`, for example `/gp/bestsellers/home/94816616031` for Coffee Machines.

Run it locally:

```bash
python -m scripts.crawl_bestsellers --max-items 500 --no-notify
```

### Where previous prices live

- Each ASIN's recent observations (price, timestamp, scraped list price) are written to the SQLite `price_checks` table in `data/bestsellers.db`. The newest **20 checks** are kept locally for bestsellers, while the separate `price_baselines` table holds the longer-lived reference price.
- GitHub Actions stores the SQLite DB in its **Actions cache**. Caches are not durable archives. The best current baseline for every crawled ASIN is also checkpointed in the relevant section of the published `docs/deals.json` so another run can restore it if the cache disappears.
- A new product needs three **prior** observations to establish its price reference; early drops cannot be reliably confirmed by Amazon history alone. The reference updates gently for normal-price movements, but does not follow a sharp price cut. If it has not been confirmed close to normal for 30 days, the system tries to re-establish a stable price reference or retires the stale reference.
- For recent big drops, the output also includes up to twelve recent timestamped price observations and the reference confirmation time. Existing old prices not previously collected cannot be recovered.
- The public dashboard only displays a subset of opportunities; `deals.json` contains the compact baseline checkpoint for the visited sections. This is a resilience measure rather than a long-term complete price-history service.

### CeX profitability assumptions

CeX's quoted cash figure is **not guaranteed**: inspection, condition, grading, accessories, quote changes, trade-in limits and stock can alter the outcome. This tracker does **not** place orders or confirm CeX will accept an item.

The estimate uses:

```text
Expected cash = quoted CeX cash × (1 − payout haircut % / 100)
Estimated net cash profit = expected cash − Amazon purchase price − extra costs
Required profit = max(minimum EUR profit, Amazon price × minimum profit % / 100)
```

The defaults in `bestsellers.json` now use `cex_extra_cost_eur: 5` and `cex_payout_haircut_percent: 10`, as example buffers for delivery/travel/packing and a lower trade-in payout. **Tune these to your own actual costs; they are not CeX fees.** Voucher value is not cash and should not be treated as a cash flip. Profit estimates exclude possible tax liabilities, return costs and other unexpected expenses.

Eligible Amazon offers now include **sold by Amazon** and **third-party sold / fulfilled by Amazon (FBA)**. A marketplace listing only qualifies when the selected buy box explicitly states Amazon dispatch/fulfilment and identifies the seller; Prime eligibility alone does not count. Seller-fulfilled or ambiguous marketplace offers remain excluded. FBA reduces logistics risk but does not guarantee authenticity, grading, unchanged pricing or CeX acceptance.

A listing match is still heuristic. Check the exact variant, whether the Amazon listing is physical and new, Ireland delivery and final checkout total, CeX's current cash quote and store acceptance conditions before spending money.

## Your own watch list

- Open a [Watch issue](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/issues/new?template=watch.yml) (or use the box on the dashboard), or
- put URLs/ASINs in [`watchlist.json`](watchlist.json).

Watched products are checked on every run, including straight after you open the issue. A price error there is a **≥75% drop** from the struck-through list price (was ≥ €20), or the same drop vs recent history for that ASIN.

GitHub's scheduler can run a few minutes late, so expect alerts within roughly 30–40 minutes of a price changing. For a tighter loop, run the local server below.

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
