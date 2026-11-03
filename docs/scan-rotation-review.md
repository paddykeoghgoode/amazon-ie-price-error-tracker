# Rotation review — 3 November 2026

Recommendation: adjust PR #36 before merging. This branch includes the adjustment and merges current main (69fb46b), preserving its high-value alert policy and notification deduplication. No production merge or extra live scan is part of this review.

## Observed durations and request success

The complete consecutive four-section cycle below used main commit 2df90f4, before the later alert-policy and discovery-feed changes. Durations are workflow start to update/completion, including setup and publication; they are not pure scrape times. Successful shorter or failed runs should not be treated as faster full discovery.

| Section | Workflow run | Start UTC | Finish UTC | Elapsed | Amazon list requests | Failed / blocked |
| --- | --- | --- | --- | --- | --- | --- |
| Gaming & tech | [41637407170](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/41637407170) | 12:38:21 | 12:47:48 | 9m27s | 211 | 0 / false |
| Home | [41638718467](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/41638718467) | 12:47:46 | 12:53:42 | 5m56s | 118 | 0 / false |
| Lifestyle | [41639545931](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/41639545931) | 12:53:39 | 12:59:29 | 5m50s | 118 | 0 / false |
| General | [41640346144](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/41640346144) | 12:59:26 | 13:06:55 | 7m29s | 136 | 0 / false |

The successor started at 13:06:52: start-to-start cycle = 28m31s, mean slot = 7m07.75s. The few seconds of workflow overlap are publication/dispatch cleanup, not evidence of simultaneous broad crawls. More recent runs have queue waits: run 41666676486 was created at 15:42:33 but its crawl selected its section at 15:49:01. Do not add queue time twice when estimating a sequential cycle.

At the measured average, four turns revisit a dedicated section in 28m31s; seven equal turns imply 49m54s, +75%. README's 5–8-minute range implies 20–32 minutes versus 35–56 minutes. These are scenarios, not measured seven-section performance. Each section's workloads, item cap, duplicate products, provider latency and queueing determine the actual interval. A product observed late in one crawl and early in the next has a different interval from a workflow start-to-start measure.

A budget-sensitive model gives a wider and more useful range. Those four crawls spent 1,339.5 seconds on 583 list requests (about 2.30 seconds/request including the configured delay), leaving about 93 seconds/turn for other work and publication. If the new sections use the same fraction of their request ceilings (583/820), seven turns model at about 42.4 minutes; if they fill all 1,160 list requests, about 55.3 minutes. The extra 12 live CeX operations in the priority turn can add further latency. Treat **roughly 43–56 minutes** as a planning range under these stated assumptions, not a confidence interval. Item caps, differently sized trees, cached quotes and failures can move actual results outside it.

For the adjusted four-turn round, the average list ceiling is 710 (one 650-request coffee round and three 730-request rounds). The same model gives **about 25.5–33.4 minutes** for priority revisits, and **102–134 minutes** for a complete 16-turn secondary cycle. This improves expected priority discovery without claiming every category or every feed gets faster.

## Configured ceilings

| Budget per complete rotation | Existing four | Original PR seven |
| --- | ---: | ---: |
| Amazon list request ceiling | 820 | 1,160 (+41.5%) |
| Item ceiling, before cross-section deduplication | 12,500 | 17,800 (+42.4%) |
| Catalogue candidates | 480 | 870 (+81.3%) |
| Live CeX research operations | 40 | 82 (+105%) |
| CeX processing time ceiling | 1,440s | 2,580s |
| List-request pacing alone at 1.5s/request | 20m30s | 29m00s |

The top-level max_requests=800 is not a shared rotation allowance: explicit section overrides are applied independently. Catalogue candidates are not new Amazon detail requests, and a 150-candidate batch does not mean 150 live CeX researches. A live research operation can issue both a title search and a box-detail request.

In the four logged sections, each 120-row batch used 10 live operations and 4–12 cache hits; 98–106 candidates were deferred. CeX reported no errors or rate limit in this sample. Thus increasing 120 to 150 primarily enlarges processing/deferred work unless the live allowance or cache coverage changes. At the equal-slot scenario, total live ceilings rise from about 84 to 99 operations/hour, despite slower category refreshes. Actual requests and successful exact matches can be much lower.

## Discovery value and urgent coverage

The proposed PC root has deeper computer coverage and dedicated wish-list/movers feeds; games have their own feeds and CeX gets phones/headphones/computers/games plus a larger research allowance. These can improve relevance and first discovery, but no seven-section production evidence yet establishes more real price errors or profitable trades. Some old roots disappear or lose depth. Original crawling exhausts one root's descendants before later roots: the gaming sample stopped at 3,500 items after games/electronics, so configuring phones/computers later did not ensure a visit.

Shared roots give partial extra refreshes: shallow games appear in both videogames and cex_priority, and computer/electronics roots in pc_hardware and cex_priority. In the original seven-turn sequence their maximum root-level gaps can be five and six turns respectively, rather than seven, but deeper lists and section-local CeX queues do not get the same benefit. Current main also pulls common electronics/computer discovery feeds on every section; removing these global feeds reduces their frequency. Neither shared roots nor feeds establish full ASIN coverage.

Watchlist cron is configured every 10 minutes (minutes 5,15,…55), with its own price history and rapid-drop checks, but covers only watchlist/issue-intake products. Hotlist cron is configured every 10 minutes (2,12,…52), takes at most 12 already qualifying drop/CeX rows from the last 48 hours, and alerts on qualifying further drops. It cannot discover an unseen error. [Hotlist run 41657977373](https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/41657977373) at 14:52 took 35 seconds but read only 1 of 7 prices. Available scheduled run history also has a large gap from the preceding hotlist run at 06:59. Configuration is not proof of timely or successful price reads. No additional cron or retries are introduced here.

## Changes included before merge

1. Mark games, PC hardware and CeX as priority_rotation sections. Use published timestamps to scan all three before one secondary turn; rotate secondary turns through coffee, home, lifestyle and general. Restart/migration and failed unpublished attempts preserve fair selection. Manual all visits all seven once.
2. Keep the successful-publication chain, serialized queue, existing independent schedules, request delays and caches. Give all three priority sections 10 live CeX operations, each secondary one, and all sections 120 candidates / 360 seconds. A four-turn round allows 31 live operations and 650–730 list requests, compared with the old 40 and 820. This reallocates discovery; it does not guarantee a fixed hourly usage ceiling.
3. Share a breadth-first queue across category roots so early branches cannot consume the whole budget before later seeds. The same request/item caps and captcha/consecutive-failure stop rules apply. A blocked crawl now fails before replacing published results or chaining another scan; the existing watchdog's repeated-failure pause remains available.
4. In priority sections, keyword candidate ordering uses unexamined/oldest research first, with higher price breaking ties. Keep the bounded urgent lane and the remaining fair catalogue queue. Reuse same-ASIN/same-title quotes and delivered-alert fingerprints across published sections, taking the newest valid timestamp; this avoids duplicate research and repeated CeX notifications as overlapping roots move between sections.
5. Preserve current main's high-value phone-alert thresholds, exact model/EAN match requirement, fresh CeX quote checks, cost/haircut calculations and delivered-alert deduplication. Coffee's requested current-price/review gates remain; missing review counts suppress signals, and a high-value machine dropping below €100 is also suppressed by that coffee gate. This is a deliberate coverage limitation to reconsider separately.

At unchanged slot time, dedicated priorities return about every 28m31s instead of 49m54s; secondary categories return every 16 slots, about 114 minutes. New root traversal and scoped feeds can change those numbers substantially. Three fresh consistent history observations are still required before a stable reference: lower revisit latency also builds useful baselines sooner.

GitHub Actions usage cannot honestly be guaranteed unchanged from configuration alone: a continuous chain keeps scanning, and shorter scans can incur more setup/publication overhead per hour. No parallelism or new workflow triggers are added. Before claiming the usage/failure constraint is met, compare a full representative 16-turn cycle against the baseline using billable job minutes, actual Amazon requests/hour, successful prices read, 403/429/503/captcha rates, CeX exact-match yield and unique qualifying alerts. Keep the PR open until checks pass; merge only after reviewing this slower-secondary tradeoff. Do not lower pacing or enable manual all as an automatic workaround.
