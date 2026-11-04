"""Write section progress to the GitHub Actions step summary."""
import argparse
from pathlib import Path
from app.snapshot import read_snapshot


def report(data: dict, published: bool = False) -> str:
    if data.get("scan_kind") == "hotlist":
        stats = data.get("funnel") or {}
        lines = ["## CeX targets and fast price checks", "",
                 f"Checked at {data.get('generated_at')} · {data.get('scan_status')}", "",
                 "| Stage | Count |", "| --- | ---: |"]
        for key, label in (("catalogue_skus", "CeX target SKUs"), ("saved_mappings", "Saved ASIN mappings"),
                           ("alert_ready_mappings", "Alert-ready mappings"),
                           ("fresh_quotes", "Fresh cash quotes"), ("stale_quotes", "Quotes awaiting refresh"),
                           ("amazon_attempted", "Amazon pages attempted"), ("price_read", "Prices read"),
                           ("identity_mismatch", "Identity missing or changed"),
                           ("above_buy_ceiling", "Above profitable buy ceiling"),
                           ("unreadable", "Unreadable pages"), ("profitable", "Qualifying cash margins"),
                           ("alerts_sent", "Alerts delivered")):
            lines.append(f"| {label} | {stats.get(key, 0)} |")
        return "\n".join(lines) + "\n"
    sections = data.get("sections") or {data.get("section_id", "section"): data}
    name = data.get("last_section_name") or data.get("section_name") or "Bestsellers"
    lines = [
        f"## {'Published' if published else 'Completed'}: {name}",
        "",
        f"Scan timestamp (UTC): **{data.get('last_section_at') or data.get('generated_at') or 'unknown'}**",
        "",
        "| Section | Last scan (UTC) | Status | Items | Seller check | Catalogue checked / priced | CeX attempted | CeX matched | Amazon drops | Price errors | CeX sale flips | CeX regular flips |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    details = []
    for sid, section in sections.items():
        stats = section.get("cex_stats") or {}
        cat = section.get("catalogue_stats") or {}
        progress = (
            f"{int(cat.get('checked') or 0)}/{int(cat.get('priced') or 0)}"
            if "priced" in cat else "not started"
        )
        checked = "disabled"
        lines.append(
            f"| {section.get('section_name') or sid} "
            f"| {section.get('generated_at') or 'unknown'} "
            f"| {section.get('scan_status') or ('blocked' if section.get('blocked') else 'completed')} "
            f"| {int(section.get('items_seen') or 0)} "
            f"| {checked} "
            f"| {progress} "
            f"| {int(cat.get('cex_attempted') or 0)} "
            f"| {int(cat.get('cex_matched') or 0)} "
            f"| {len(section.get('general_drops') or section.get('drops') or [])} "
            f"| {int(section.get('new_alerts') or 0)} "
            f"| {int(stats.get('cash_flips') or 0)} "
            f"| {int(stats.get('arbitrage') or 0)} |"
        )
        detail = stats.get("detail_checks") or {}
        if detail.get('failure_reasons'):
            details.append('\nAmazon detail failures: ' + ', '.join(
                f'{reason}: {count}' for reason, count in sorted(detail['failure_reasons'].items())))
        discovery = stats.get('amazon_target_discovery') or {}
        if discovery:
            details.append(f"\nCeX-to-Amazon discovery: {discovery.get('status')}; "
                           f"{discovery.get('search_requests', 0)} search pages, "
                           f"{discovery.get('candidates', 0)} new candidates within the existing page budget.")
        first = stats.get("cex_first") or {}
        if first:
            details.append(f"\nCeX-first: {first.get('catalogue_skus', 0)} target SKUs; "
                           f"{first.get('saved_mappings', first.get('exact_mappings', 0))} saved Amazon links; "
                           f"{first.get('alert_ready_mappings', 0)} alert-ready; "
                           f"{first.get('fresh_quotes', 0)} fresh cash quotes. "
                           f"Used {first.get('catalogue_searches', 0)} catalogue searches and "
                           f"{first.get('mapped_quote_refreshes', 0)} mapped quote refreshes "
                           "within the existing CeX allowance.\n")
        if detail:
            details.append(
                f"\n{section.get('section_name') or sid}: product detail checks "
                f"{detail.get('price_read', 0)}/{detail.get('attempted', 0)} prices read; "
                f"{detail.get('identity_confirmed', 0)} exact CeX identities confirmed; "
                f"{detail.get('identifiers_missing', 0)} missing identifiers; "
                f"{detail.get('identity_mismatch', 0)} mismatches; "
                f"{detail.get('price_changed', 0)} changed prices.\n"
            )
    lines.extend(details)
    lines.append("\n**Every finding is a research lead. Amazon seller, condition and checkout price are not checked.**")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--published", action="store_true")
    args = parser.parse_args()
    text = report(read_snapshot(args.input), published=args.published)
    with args.summary.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
