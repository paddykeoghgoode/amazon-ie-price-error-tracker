"""Recover automatic bestseller discovery if a dispatch was missed.

Normal scans queue the next run *after* publishing, inside pages.yml. This
cheap watchdog runs twice an hour and only dispatches when no broad scan is
running/queued and the last broad scan is stale. It also prevents repeated
failures from consuming unlimited GitHub Actions minutes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

WATCHDOG_DELAY = timedelta(minutes=25)
FAILURE_LIMIT = 3
BROAD_EVENTS = frozenset(("push", "workflow_dispatch"))
FAILED_CONCLUSIONS = frozenset(("failure", "timed_out", "cancelled", "action_required"))


def decide_successor(runs: list[dict], current_run_id: int) -> tuple[bool, str]:
    """A waiting broad run will continue the chain; do not add another."""
    current = next((r for r in runs if r.get('id') == current_run_id), None)
    if not current or current.get('head_branch') != 'main':
        return False, 'Cannot verify the current main-branch run; leaving recovery to the watchdog.'
    for run in runs:
        if (run.get('id') != current_run_id and run.get('head_branch') == 'main'
                and run.get('event') in BROAD_EVENTS and run.get('status') != 'completed'):
            return False, f"Broad run {run['id']} already exists; no extra successor."
    return True, 'No other broad run is waiting; queue one successor.'


def fetch_runs(repo: str, token: str) -> list[dict]:
    """Inspect recent history plus ALL active pages, including old pending runs."""
    runs = {}
    base = f'https://api.github.com/repos/{repo}/actions/workflows/pages.yml/runs?branch=main&per_page=100'
    for status in ('', 'queued', 'pending', 'in_progress', 'waiting', 'requested'):
        for page in range(1, 11):
            uri = base + f'&page={page}' + (f'&status={status}' if status else '')
            request = Request(uri, headers={'Accept': 'application/vnd.github+json',
                              'Authorization': f'Bearer {token}', 'X-GitHub-Api-Version': '2022-11-28'})
            with urlopen(request, timeout=25) as response:
                data = json.load(response)
                runs.update((r['id'], r) for r in data['workflow_runs'])
                if not status or 'rel="next"' not in response.headers.get('Link', ''):
                    break
        else:
            raise RuntimeError('Active run listing truncated; refusing another dispatch')
    return list(runs.values())


def parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


def decide_recovery(runs: list[dict], now: datetime, *, force: bool = False) -> tuple[bool, str]:
    """Return a safe yes/no, using only previous broad crawler runs.

    Ten-minute watchlist-only scheduled events are intentionally ignored.
    Never enqueue a new broad run while another is active or pending.
    """
    broad = sorted(
        (
            run for run in runs
            if run.get("event") in BROAD_EVENTS
            and run.get("head_branch", "main") == "main"
        ),
        key=lambda run: (str(run.get("created_at") or ""), int(run.get("id") or 0)),
        reverse=True,
    )
    if any(run.get("status") != "completed" for run in broad):
        return False, "Another broad scan is running, pending or queued."
    if not broad:
        return True, "No prior broad run found; starting discovery."

    last = broad[0]
    failed_in_a_row = 0
    for run in broad:
        if run.get("conclusion") in FAILED_CONCLUSIONS:
            failed_in_a_row += 1
        else:
            break
    if failed_in_a_row >= FAILURE_LIMIT and not force:
        return False, (
            f"Paused after {failed_in_a_row} consecutive failed broad scans. "
            "Fix the failure, then manually run the workflow or use watchdog force_start."
        )

    ended = parse_timestamp(last.get("updated_at")) or parse_timestamp(last.get("created_at"))
    if ended is None:
        return False, "Cannot determine the last scan completion time."
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = now - ended
    if not force and age < WATCHDOG_DELAY:
        return False, f"Latest broad scan is only {max(age.total_seconds(), 0) / 60:.1f} minutes old."
    return True, (
        "Manual recovery requested." if force else
        f"No broad scan active; latest run completed {age.total_seconds() / 60:.1f} minutes ago."
    )


def main() -> None:
    import sys
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    force = os.environ.get("FORCE_START", "").lower() == "true"
    if not repo or "/" not in repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
    runs = fetch_runs(repo, token)
    if '--successor' in sys.argv:
        allowed, reason = decide_successor(runs, int(os.environ['GITHUB_RUN_ID']))
    else:
        allowed, reason = decide_recovery(runs, datetime.now(timezone.utc), force=force)
    print(f"Recovery decision: {'DISPATCH' if allowed else 'SKIP'} — {reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as report:
            report.write(f"### Bestseller discovery watchdog\n\n{reason}\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        raise SystemExit("GITHUB_OUTPUT is required")
    with open(output, "a", encoding="utf-8") as target:
        target.write(f"dispatch={'true' if allowed else 'false'}\n")


if __name__ == "__main__":
    main()
