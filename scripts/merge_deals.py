from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.deals import merge_section_deals
from app.snapshot import read_snapshot, write_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge a section crawl into deals.json")
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    previous = read_snapshot(args.previous) if args.previous.exists() else None
    incoming = read_snapshot(args.incoming)
    merged = merge_section_deals(previous, incoming)
    write_snapshot(args.output, merged)
    print(json.dumps({"sections": list((merged.get("sections") or {}).keys()), "last": merged.get("last_section_id")}))


if __name__ == "__main__":
    main()
