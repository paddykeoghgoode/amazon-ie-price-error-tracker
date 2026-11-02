"""Merge an Amazon listing catalogue fragment with the latest Pages snapshot."""
import argparse
import json
from pathlib import Path

from app.services.catalogue import merge_catalogue, write_catalogue_csv
from app.snapshot import read_snapshot, write_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()
    previous = read_snapshot(args.previous) if args.previous.exists() else {}
    incoming = read_snapshot(args.incoming)
    merged = merge_catalogue(previous, incoming)
    write_snapshot(args.output, merged)
    write_catalogue_csv(merged, args.csv)
    print(json.dumps({
        "sections": list(merged.get("sections") or {}),
        "items": sum(len(s.get("items") or []) for s in merged["sections"].values()),
        "last": merged.get("last_section_id"),
    }))


if __name__ == "__main__":
    main()
