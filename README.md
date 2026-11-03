# Amazon.ie Price Error Tracker

### [Open the price tracker dashboard](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/)

GitHub Actions automatically queues the next bestseller section after the previous section successfully scans and publishes; the separate watchlist continues every 10 minutes. It covers about 10,000 bestsellers plus your own watch list, and sends a **Telegram message as soon as a likely price error turns up**.

**Dashboard:** [paddykeoghgoode.github.io/amazon-ie-price-error-tracker](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/) (times are Ireland / Europe/Dublin)

## Set up

1. Add GitHub Actions **secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
2. Run **Actions → Scan and publish tracker → Run workflow** once. The manual default is now **all**: it scans Gaming, Home, Lifestyle and Books & media **one block at a time**, publishes each block as soon as it completes, and shows each block's status in GitHub Actions. You can choose **rotate** to run just one section. The watchlist also sends its own Telegram summary.
3. After that it runs by itself at :05 and :35 past every hour. The dashboard and Telegram update **as each scan finishes** (watchlist and one bestseller section per run), not only when the whole workflow is done.

Settings → Pages → Source must be **GitHub Actions**, and the repo must be **public**. Continuous bestseller crawls use public-repository GitHub Actions runners; the exact completion-to-next-start delay depends on GitHub's queue. On a private repo, only manual runs crawl bestsellers.

### Telegram

1. [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message your bot once, then put your chat id in `TELEGRAM_CHAT_ID` (e.g. via [@userinfobot](https://t.me/userinfobot)).

Optional WhatsApp: set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WHATSAPP_TO` as secrets too.

Besides price-error alerts, the bot sends:

- a **welcome** message the first time it's connected;
- a **warning** if Amazon blocks the crawl or it finds suspiciously few items (at most every 12 hours), and a message when it recovers;
- a **daily summary** after 09:00 Ireland time, so silence never means "broken". Change or disable it with `daily_summary_hour` in [`bestsellers.json`](bestsellers.json) (`null` turns it off).

## Irish retailer price comparisons for your watchlist

The **Watchlist → Retail Price Comparison** card now supports retailer price research alongside Amazon. To add a new product, enter its Amazon link and open **“Add Irish retailer price comparisons and RRP”** on the dashboard. Enter an exact model/MPN or EAN, optional manufacturer RRP and source, and up to eight HTTPS **direct product-page URLs** from supported Irish retailers (Currys, Harvey Norman, DID, ElectroCity, Peter Murphy Electrical, Euronics, Expert, Joyces, Arnotts, Keans, Irwin Electrical). You can also edit an existing `Watch:` GitHub issue and add lines like:

```text
Model: SES875BTR2GUK1
EAN: 9312432032056
Compare: https://www.petermurphyelectrical.ie/products/your-exact-product
RRP: 599.99
RRP source: https://www.sageappliances.com/eu/your-model
```

Or configure `watchlist.json` for a specific ASIN:

```json
{
  "products": [
    {
      "asin": "B0EXAMPLE1",
      "model": "SES875BTR2GUK1",
      "barcode": "9312432032056",
      "retailer_urls": [
        "https://www.petermurphyelectrical.ie/products/your-exact-product"
      ],
      "rrp_eur": 599.99,
      "rrp_source": "https://www.sageappliances.com/eu/your-model"
    }
  ]
}
```

**Important:** Replace example product links/ASINs with the real ones. The scanner never guesses a retailer item from a similar title, nor scrapes Google Shopping automatically. It checks only the exact retailer product URLs you supply; a shop page needs structured `Product → Offer` EUR pricing and an exact MPN or barcode match. If an item is out of stock, a model differs, the page hides prices, or access is denied, the dashboard shows the reason and does **not** count it as the best price. Multiple variant prices are rejected as ambiguous.

**Freshness and request limits:** watchlist Amazon checks continue every 10 minutes, but known retailer URLs are refreshed **at most every 12 hours** (3-hour backoff on missing/error data), limited to **12 retailer requests per watchlist run** with delays. Results, timestamps and supplier-specific errors are cached in the published `data.json`. This is not a nationwide price guarantee. The "lowest checked retailer" is item-only EUR and **excludes delivery**, stock may change and final checkout totals may differ.

**RRP:** this is optional and labelled **user supplied**, with an optional manufacturer source URL. Amazon's strikethrough / “was” price is shown separately and is **not** assumed to be official RRP. No RRP is fabricated from competing listings. No purchases are automated.

## Add products to the Watchlist from GitHub Pages

The site is hosted on **static GitHub Pages**, so it cannot safely save products directly into the repository from a browser. Open the **Watchlist** tab, paste an Amazon.ie URL, Amazon **amzn.eu** short link, or 10-character ASIN, and select **Add via GitHub**. GitHub opens a prefilled issue; tap **Create Issue** once. That's the complete submission step.

The issue event now runs **only the watchlist job**, rather than needlessly launching a full bestseller crawl. The job resolves trusted HTTPS Amazon short-link redirects to an ASIN, deduplicates the product, checks its price, publishes **Watchlist requests** on the dashboard and comments on newly opened/edited issues to confirm success or explain an invalid link. Issue titles starting with `Watch:` are accepted even when the GitHub mobile app does not add the `watch` label.

Shortlink resolution is cached by issue content across subsequent scans. If Amazon refuses the redirect, edit the **existing issue** with the full Amazon product URL (for example `https://www.amazon.ie/dp/B0CX1234AB`) or its ASIN; the scanner retries. The Watchlist requests table distinguishes **Tracking**, **Fetch failed**, **Price unavailable** and **Link not resolved**. A product can be registered even if Amazon temporarily blocks its price page, in which case its status shows the failure.

For repository maintainers, `watchlist.json` also accepts Amazon URLs or short links. The watchlist is checked by the existing scheduled workflow; no access token is stored in GitHub Pages or sent to Amazon. All offers remain unverified leads and require a manual checkout check.

## Seller verification temporarily disabled

The Amazon seller check is **disabled**. Bestseller prices are treated as **observations**, and all CeX comparisons and price-drop notifications are **research leads**, not confirmed retail offers or guaranteed profit. A seller cannot be inferred from Prime eligibility. Before any purchase, manually check the final seller, dispatch method, item condition, exact colour/model, availability and delivered checkout total; independently confirm CeX will buy that exact item/grade for the quoted **cash** amount. No buying is automated. This removes the old zero-verified-products blocker without inventing seller confidence.

## New dashboard tabs and general Amazon price drops

The GitHub Pages dashboard now has five views: **Overview**, **Amazon Drops**, **CeX Opportunities**, **All Listings** and **Watchlist**. Tabs support arrow keys and bookmarks such as [#amazon-drops](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/#amazon-drops).

The **Amazon Drops** tab is independent of CeX matches and the strict price-error alert threshold. It reports **all current observed discounts of at least 10%** from the item's stable Amazon price history, including **historical reference (€), current observed price (€), euro saving, percentage reduction, first found time, last section scan and category**. Filter by minimum reduction (10–75%), section or product name, and sort by percentage, savings or recency; results are paginated without the previous top-50 cap. Change `general_drop_percent` in `bestsellers.json` if you want to collect a different minimum percentage. Once collected, use the on-page minimum to narrow the results.

The reference requires **three earlier consistent observations** and does not drift downward during repeated 10–25% discounts. The first runs after deployment may still show older ≥30% rows in sections that have not been rescanned by the new code; each new block replaces those with the broader ≥10% results. **All prices are observed bestseller-card prices, not verified checkout offers or manufacturer RRPs.**

## Bestseller scan (thousands of items, no manual adding)


### CeX caching, profit alerts and seller risk

Amazon bestseller discovery now **queues the next rotating section only after successful scan and publication**, rather than using a 20-minute cron. The four-section cycle time depends on actual run and deployment durations. CeX is a **separate, much less frequent data source**:

- A CeX product cash quote is stored in the published catalogue alongside its exact box ID and last successful quote time. Changing Amazon's observed price recalculates estimated resale profit using that cached quote; it **does not trigger another CeX search**.
- Successful CeX matches are refreshed every **72 hours** by default; items with no match are searched again after **7 days**. Provider errors get **6 hours** of backoff, and live CeX searches are capped at **10 per section run** to avoid increasing its hourly request volume when scans complete more frequently. HTTP 403/429/503 pauses further CeX calls in that run. Set these values in \`bestsellers.json\` and respect the provider's terms and rate limits.
- A potentially profitable research lead with a CeX quote older than **12 hours** triggers an isolated CeX SKU detail refresh where possible. If the fresh quote cannot be obtained, the cached value stays visible as **research only**, with a quote timestamp, **not** as a buying alert. A cached CeX price is never a guaranteed CeX trade-in payout.
- A suspected Amazon bestseller price-error lead is created from an observed bestseller-card price after sufficient stable price history; no seller check is performed. Duplicate notifications are suppressed for 24 hours at the same or higher observed price; a deeper drop can alert again. Alerts explicitly warn that checkout terms are unverified.
- **Amazon offer condition is not verified, and no buying is automatic.** Used – Like New does *not* guarantee CeX Grade B: CeX checks physical condition, box/accessories and functionality, and some categories use non-graded pricing or different console grades. It should be researched with a separate conservative Grade B/C scenario before inclusion.
- Default profit calculation uses CeX **cash** (not voucher), a 10% payout reduction, a configurable delivery estimate and €5 additional expenses. It does not model every tax, grading or transport consequence. Buy only if the **checkout total** and the matching CeX SKU/grade still clear your desired margin. CeX may reduce trade-in values for multiple copies.

This design reduces repeated CeX calls; it does **not** guarantee that CeX remains accessible, that bestseller prices match checkout offers, or that the seller/condition is acceptable.

### Full Amazon listing catalogue and progressive CeX checks

The tracker now publishes **every discovered ASIN, title, Amazon URL, observed bestseller price, section and rank** to [catalogue.json](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/catalogue.json) and [downloadable catalogue.csv](https://paddykeoghgoode.github.io/amazon-ie-price-error-tracker/catalogue.csv), not only the top 25/50 candidates. The dashboard has a searchable, paginated "All Amazon bestseller listings" table that loads this catalogue when requested.

A new scan creates a row for *all* items in its section, including seller-check **disabled**, CeX research status, last attempt time and a CeX link/profit estimate if matched. **An observed bestseller price is not a verified buy-box/checkout price.** Any potential opportunity still needs manual seller, variant, checkout price, CeX SKU/grade and payout confirmation.

Each section examines up to **120 queued priced products per run**, in groups of 20, with a CeX research processing time budget of **360 seconds** after the bestseller crawl. These are *per-run* limits rather than a permanent shortlist. The queue always takes previously unexamined/oldest items first (plus a small priority lane for big fresh drops), with progress restored from the previously published catalogue. **Every priced listing in a batch** gets a CeX search **regardless of category, keyword, Amazon price, or whether the Amazon seller was recognised**. CeX matches are always **provisional research leads**. Potentially profitable ones can trigger an explicitly unverified Telegram alert when the CeX quote is fresh enough. Unmatched items remain in the queue. CeX cash prices are not guaranteed.

You can tune \`catalogue_checks_per_run\` and \`catalogue_time_budget_seconds\` in \`bestsellers.json\`. Do not raise them dramatically without checking GitHub Action duration and Amazon/CeX throttling: all 12,000 products cannot be fetched safely in one job. The four sections rotate across sequential runs, so full first-pass CeX research of a large catalogue can take days. Listings without usable prices are still listed but cannot enter a CeX profit calculation until priced. Products dropping out of a bestseller ranking remain in the exported catalogue for up to 30 days as archived rows, but are not rechecked while absent. Updated price or product title clears old estimates, while reusable CeX box quotations are cached separately when the product identity remains unchanged; all current prices need checkout rechecking before buying. A failed or throttled section can resume from the last successful published checkpoint.

Each **automatic** run crawls **one section** from [`bestsellers.json`](bestsellers.json) (~5–8 minutes, subject to provider response), **publishes that section**, sends a **section summary on Telegram**, and then **queues the next run**. The sequence is **gaming → home → lifestyle → general**, resuming from the last successfully published section rather than GitHub's run number. Broad scans use a serialized GitHub Actions queue and do not start while an earlier one is still active. A **manual Run workflow defaults to `all`** and starts four sequential jobs. Each job scans one section, advances CeX research without seller verification, merges just that section into the latest published `deals.json`, and deploys the updated site before the next section begins. The Actions view displays an independent job and timestamp for each block; the dashboard updates as each publish succeeds. Selecting `rotate` instead runs only one section.

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


### Profit radar (recommended checks)

- **Product identity:** Historical/detail-page barcodes and models can improve matching where present, but the main bestseller scan no longer fetches individual Amazon product pages to gather them. CeX details are compared only if they expose explicit identifiers. A conflicting model or barcode is rejected. `match_confidence` is `barcode_verified`, `model_verified`, or `title_only`; missing CeX identity is never described as an exact match.
- **Maximum buy price:** CeX cash quote is reduced by the configured payout haircut, then delivery and extra costs are subtracted. Each opportunity shows expected net cash profit, ROI, the break-even Amazon item price, and the maximum Amazon item price that still meets the minimum euro margin and ROI. Configure `cex_delivery_eur` (default €0, estimate only) and `cex_min_roi_percent` (default 15%). Shipping and checkout totals must be verified before buying. These figures do not include taxes or unexpected deductions.
- **Fast hotlist:** `Fast checks for price-drop leads` runs independently every 10 minutes (:02, :12, :22, :32, :42 and :52 UTC), checking up to 12 recently observed Amazon price drops of at least 30% from the last 48 hours. It republishes `hotlist.json` and sends a research alert for a further product-page price cut; it does not verify sellers or checkout offers. Scheduled runs may be delayed. It is not a complete scrape of Amazon every 15 minutes.
- **Tiered alerts:** Where barcode/model evidence is available and estimated profit is ≥€25, the tracker can send a high-potential research alert with CeX quote timestamp, ROI, matching level and maximum estimated purchase price. Seller and checkout price are not verified. Title-only matches remain lower confidence; no automation purchases anything.

### CeX profitability assumptions

CeX's quoted cash figure is **not guaranteed**: inspection, condition, grading, accessories, quote changes, trade-in limits and stock can alter the outcome. This tracker does **not** place orders or confirm CeX will accept an item.

The estimate uses:

```text
Expected cash = quoted CeX cash × (1 − payout haircut % / 100)
Estimated net cash profit = expected cash − Amazon purchase price − extra costs
Required profit = max(minimum EUR profit, Amazon price × minimum profit % / 100)
```

The defaults in `bestsellers.json` now use `cex_extra_cost_eur: 5` and `cex_payout_haircut_percent: 10`, as example buffers for delivery/travel/packing and a lower trade-in payout. **Tune these to your own actual costs; they are not CeX fees.** Voucher value is not cash and should not be treated as a cash flip. Profit estimates exclude possible tax liabilities, return costs and other unexpected expenses.

**Seller checks are disabled for now.** Research alerts may include Amazon retail, FBA, seller-shipped, or unknown offers because the scanner no longer attempts to distinguish them. You must manually assess the seller and dispatch method before buying; Prime membership or fulfilment alone does not prove that Amazon is the seller.

A listing match is still heuristic. Check the exact variant, whether the Amazon listing is physical and new, Ireland delivery and final checkout total, CeX's current cash quote and store acceptance conditions before spending money.

## Live GitHub Actions activity on the dashboard

The **Overview → Live scan activity** panel uses GitHub's **public** Actions REST API
(no GitHub token in the browser) to show the newest tracker and priority-drop
runs, including **queued**, **running**, **completed**, **failed** or **cancelled**.
Each card links directly to its GitHub Actions run and shows the current
job/step when GitHub reports it.

**Percentage is workflow-step completion, not product scan progress.**
For example, the "Crawl bestseller section" step may take several minutes;
the percentage will stay unchanged while products are processed. Newly
created matrix jobs can change the set of steps. The dashboard therefore
labels the denominator as *reported* steps and never invents an ASIN-count
completion percentage. If detailed job data is not available, the card says so
and links to GitHub instead of showing a false number.

The dashboard checks recent workflow runs about every **2½ minutes**, but
fetches the heavier per-run job details no more often than every **4 minutes**,
for at most two active runs. **No browser refresh is required.** The bar and
numeric label animate smoothly over 900ms from the last GitHub-reported
percentage to a new, higher reported percentage. They stay still between
actual reports (rather than fabricating constant progress). If GitHub adds
more steps and the reported percentage decreases, the display moves to that
lower value without a misleading backwards animation. A user's OS-level
reduced-motion setting disables the animation. This is deliberate to avoid exceeding the
unauthenticated GitHub API rate limit. If GitHub rate-limits the browser,
the panel keeps the last fetched state and reports that the status may be
delayed. Scheduled scans and publishing continue independently.

## Automatic scan scheduling (UTC)

| Scan | Trigger | Target frequency |
|---|---|---|
| Watched products | :05, :15, :25, :35, :45, :55 | Every 10 minutes, independently of broad scans |
| Targeted recent Amazon price drops | :02, :12, :22, :32, :42, :52 | Every 10 minutes, independently |
| Amazon bestseller discovery | Successful completion **and publication** of previous rotating section | Queue next section immediately; actual start depends on GitHub's runner availability |
| Recovery watchdog | :13 and :43 (checks status, does **not** scan unless stale) | Recovers if no broad run is active and the latest broad run has been complete for ≥25 min |
| CeX quotes | Existing cached data | Refresh only when due or for a promising lead; at most 10 live lookups per broad scan |

All cron times are UTC; the Irish summer offset changes the displayed local hour. Broad discovery no longer has a 20-minute fixed schedule; each successful publish dispatches a new `workflow_dispatch` run with `auto_continue=true`, queued behind the current `tracker-main` lock. Watchlist-only schedule ticks and Watch issues never launch a bestseller crawl. Manually selecting `all` still publishes four sections sequentially, but does not create an endless chain.

The separate **Recover stalled bestseller scans** workflow checks twice an hour: if the chain is missing and no broad scan is running or queued, it starts a replacement only once the last run is at least 25 minutes old. After three consecutive failures it pauses automatic recovery; fix the failing scan before manually restarting. Set repository variable `PAUSE_AUTOMATIC_SCAN=true` to stop new automated sections without disabling your watchlist or hotlist. GitHub Actions triggers and queues can be delayed or skipped, so this cannot guarantee an exact restart time. More frequent crawling may increase Amazon request pressure, so monitor timeouts, 429/503 responses and Actions usage; CeX requests are capped at 10 live lookups per broad run to limit that risk.

## Your own watch list

- Open a [Watch issue](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/issues/new?template=watch.yml) (or use the box on the dashboard), or
- put URLs/ASINs in [`watchlist.json`](watchlist.json).

Watched products are scheduled every **10 minutes** (:05, :15, :25, :35, :45 and :55 UTC), as well as immediately after opening or editing a Watch issue. Watchlist-only runs do **not** start a bestseller crawl. A price error there is a **≥75% drop** from the struck-through list price (was ≥ €20), or the same drop vs recent history for that ASIN.

GitHub's scheduler can start late, skip a tick, or queue behind another deployment. The 10-minute cron is a **target cadence, not a guaranteed alert time**. For tighter and more predictable checking, run the local server below.

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
