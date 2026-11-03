"""Publish the newest completed watchlist snapshot, not a delayed older artifact.

A frequent lightweight watchlist check may finish while a full discovery run
is publishing. Their Pages deployments are serialized, but a job can still
have an old watchlist artifact. Compare timestamps *inside* the publish lock
to prevent the old artifact rolling back watchlist prices and issue statuses.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _time(snapshot: dict) -> datetime | None:
    raw = snapshot.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def latest_watchlist(previous: dict, incoming: dict) -> dict:
    if not incoming:
        return previous or {}
    if not previous:
        return incoming
    prior_time, incoming_time = _time(previous), _time(incoming)
    if prior_time is not None and (incoming_time is None or prior_time > incoming_time):
        return previous
    return incoming


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prior, incoming = _read(args.previous), _read(args.incoming)
    output = latest_watchlist(prior, incoming)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
    chosen = "existing published" if output is prior else "incoming"
    print(f"Watchlist publish: chose {chosen} snapshot ({output.get('generated_at', 'unknown timestamp')})")


if __name__ == "__main__":
    main()
