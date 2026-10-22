#!/usr/bin/env python3
"""Flag a silent scheduled task: is the newest `Automation status` comment stale?

    python3 scripts/heartbeat.py --last <ISO timestamp|none>

Prints a markdown section; exits 1 when stale (or no comment ever), else 0.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

MAX_DAYS = 3


def parse(iso):
    """Parse the API's `2026-10-22T18:17:58Z` (3.9's fromisoformat rejects the Z)."""
    dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(last_comment_iso, now, max_days=MAX_DAYS):
    if not last_comment_iso:
        return True
    return now - parse(last_comment_iso) > timedelta(days=max_days)


def report(last_comment_iso, now, max_days=MAX_DAYS):
    if is_stale(last_comment_iso, now, max_days):
        if last_comment_iso:
            what = ("The automation hasn't checked in since "
                    + parse(last_comment_iso).strftime("%Y-%m-%d"))
        else:
            what = "The automation has never checked in"
        return (f"## Automation heartbeat\n\n**Stale.** {what} "
                f"(no `Automation status` comment in over {max_days} days). "
                "The daily scheduled task may have turned itself off. "
                "Check https://claude.ai/code/routines.\n")
    return ("## Automation heartbeat\n\nOK. Last check-in "
            f"{parse(last_comment_iso).strftime('%Y-%m-%d')}.\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", required=True, help="ISO timestamp, or 'none'")
    args = ap.parse_args(argv)
    last = None if args.last.strip().lower() in ("", "none") else args.last
    now = datetime.now(timezone.utc)
    print(report(last, now))
    return 1 if is_stale(last, now) else 0


if __name__ == "__main__":
    sys.exit(main())
