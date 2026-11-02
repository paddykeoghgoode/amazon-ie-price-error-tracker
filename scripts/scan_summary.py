"""Write section progress to the GitHub Actions step summary."""
import argparse
from pathlib import Path
from app.snapshot import read_snapshot


def report(data: dict, published: bool = False) -> str:
    sections = data.get("sections") or {data.get("section_id", "section"): data}
    name = data.get("last_section_name") or data.get("section_name") or "Bestsellers"
    lines = [
        f"## {'Published' if published else 'Completed'}: {name}",
        "",
        f"Scan timestamp (UTC): **{data.get('last_section_at') or data.get('generated_at') or 'unknown'}**",
        "",
        "| Section | Last scan (UTC) | Status | Items | Verified / attempted | Catalogue checked / priced | CeX attempted | CeX matched | Price errors | CeX sale flips | CeX regular flips |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sid, section in sections.items():
        stats = section.get("cex_stats") or {}
        verified = section.get("verification_stats") or {}
        cat = section.get("catalogue_stats") or {}
        progress = (
            f"{int(cat.get('checked') or 0)}/{int(cat.get('priced') or 0)}"
            if "priced" in cat else "not started"
        )
        checked = (
            f"{int(verified.get('amazon_verified') or 0)}/{int(verified.get('attempted') or 0)}"
            if "attempted" in verified else "not measured"
        )
        lines.append(
            f"| {section.get('section_name') or sid} "
            f"| {section.get('generated_at') or 'unknown'} "
            f"| {section.get('scan_status') or ('blocked' if section.get('blocked') else 'completed')} "
            f"| {int(section.get('items_seen') or 0)} "
            f"| {checked} "
            f"| {progress} "
            f"| {int(cat.get('cex_attempted') or 0)} "
            f"| {int(cat.get('cex_matched') or 0)} "
            f"| {int(section.get('new_alerts') or 0)} "
            f"| {int(stats.get('cash_flips') or 0)} "
            f"| {int(stats.get('arbitrage') or 0)} |"
        )
    for sid, section in sections.items():
        reasons = (section.get("verification_stats") or {}).get("rejection_reasons") or {}
        if reasons:
            breakdown = ", ".join(
                f"{reason.replace('_', ' ')}: {count}"
                for reason, count in sorted(reasons.items(), key=lambda item: -int(item[1]))
            )
            lines.append(f"\n**{section.get('section_name') or sid} rejection reasons:** {breakdown}")
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
