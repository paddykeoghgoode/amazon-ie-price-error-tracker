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
        "| Section | Last scan (UTC) | Status | Items | Amazon-verified offers | Price errors | CeX sale flips | CeX regular flips |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sid, section in sections.items():
        stats = section.get("cex_stats") or {}
        verified = section.get("verification_stats") or {}
        lines.append(
            f"| {section.get('section_name') or sid} "
            f"| {section.get('generated_at') or 'unknown'} "
            f"| {section.get('scan_status') or ('blocked' if section.get('blocked') else 'completed')} "
            f"| {int(section.get('items_seen') or 0)} "
            f"| {int(verified.get('amazon_verified') or 0)} "
            f"| {int(section.get('new_alerts') or 0)} "
            f"| {int(stats.get('cash_flips') or 0)} "
            f"| {int(stats.get('arbitrage') or 0)} |"
        )
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
