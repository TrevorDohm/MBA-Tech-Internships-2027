#!/usr/bin/env python3
"""Fail a PR whose changed listing rows make claims the posting does not support.

For every row added or changed against --base, re-fetch the company's own posting and check that
pay figures, the deadline and the sponsorship claim actually appear in it, that the URL host is
on the company's watchlist, and that notes carry no links.

Usage:
    python3 scripts/validate_listings.py --base origin/main
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from ats import FeedError, fetch, posting_text
from discover import job_id
from watchlist import hosts_for, load_watchlist

ROOT = Path(__file__).resolve().parent.parent
LISTINGS = ROOT / "listings.json"
CLAIMS = ("pay", "deadline", "sponsorship")
MAX_NOTES = 300
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


# --- fetching ------------------------------------------------------------------------------------------------

def fetch_text(company, url, entries, http=fetch):
    """Plain text of the row's posting, or None if it cannot be fetched or has no job id."""
    entry = next((e for e in entries if e["company"] == company), None)
    jid = job_id(url)
    if entry is None or jid is None:
        return None
    try:
        return posting_text(entry, {"id": jid, "url": url}, http)
    except FeedError:
        return None
    except Exception:  # any fetch/parse failure makes the posting unverifiable, not the PR crash
        return None


# --- diffing -------------------------------------------------------------------------------------------------

def changed_rows(base, head):
    """(old, new) for each head row that is new or differs; old is None for added rows.

    Rows match by URL first, then by (company, role).
    """
    by_url = {r["url"]: r for r in base}
    by_key = {}
    for r in base:
        by_key.setdefault((r["company"], r["role"]), []).append(r)
    head_urls = {r["url"] for r in head}
    out = []
    for new in head:
        old = by_url.get(new["url"])
        if old is None:
            cands = by_key.get((new["company"], new["role"]), [])
            # prefer a base row whose URL still exists in head: that one matched by URL already
            free = [c for c in cands if c["url"] not in head_urls]
            old = free[0] if free else None
        if old is None:
            out.append((None, new))
        elif old != new:
            out.append((old, new))
    return out


def claim_fields(old, new):
    """Claim fields of `new` that need checking against the posting."""
    if old is not None and old["url"] == new["url"]:
        others = {k for k in set(old) | set(new) if k != "status" and old.get(k) != new.get(k)}
        if not others and new.get("status") == "closed":
            return []
    fields = []
    for f in CLAIMS:
        value = new.get(f)
        if f == "sponsorship":
            has = value in ("yes", "no")
        else:
            has = bool(value)
        if has and (old is None or old["url"] != new["url"] or old.get(f) != value):
            fields.append(f)
    return fields


# --- claim matching ------------------------------------------------------------------------------------------

def _norm(s):
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    s = re.sub(r"[‒-―−]", "-", s)
    return s.lower()


_NUM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(k\b)?")


def _numbers(s):
    out = set()
    for m in _NUM.finditer(_norm(s)):
        n = float(m.group(1))
        out.add(n)
        if m.group(2):
            out.add(n * 1000)
    return out


def _value_numbers(s):
    out = []
    for m in _NUM.finditer(_norm(s)):
        n = float(m.group(1))
        out.append(n * 1000 if m.group(2) else n)
    return out


def pay_missing(pay, text):
    have = _numbers(text)
    return [n for n in _value_numbers(pay) if n not in have]


def deadline_in(iso, text):
    d = date.fromisoformat(iso)
    t = _norm(text)
    day = r"0?%d" % d.day
    m = r"(?:%s|%s)\.?" % (MONTHS[d.month - 1], MONTHS[d.month - 1][:3])
    if re.search(r"(?<!\d)%s-%02d-%02d(?!\d)" % (d.year, d.month, d.day), t):
        return True
    year_after = r"(?:,?\s*(\d{4})(?!\d))?"
    pats = (r"\b%s\s+(?<!\d)%s(?!\d)(?:st|nd|rd|th)?%s" % (m, day, year_after),
            r"(?<!\d)%s(?!\d)(?:st|nd|rd|th)?(?:\s+of)?\s+%s%s" % (day, m, year_after))
    for p in pats:
        for hit in re.finditer(p, t):
            y = hit.group(1)
            if y is None or int(y) == d.year:
                return True
    numeric = r"(?<![\d/])0?%d/0?%d(?:/(\d{2,4}))?(?![\d/])"
    for a, b in ((d.month, d.day), (d.day, d.month)):
        for hit in re.finditer(numeric % (a, b), t):
            y = hit.group(1)
            if y is None or int(y) % 100 == d.year % 100:
                return True
    return False


def sponsorship_wording(text):
    t = text.lower()
    return "sponsor" in t or "authoriz" in t or "authoris" in t


# --- per-row check -------------------------------------------------------------------------------------------

def check_row(old, new, text, hosts):
    """Error strings for one changed row. `hosts` None means the company is not host-checked."""
    label = "%s / %s" % (new.get("company"), new.get("role"))
    errors = []
    host = urlparse(new["url"]).hostname or ""
    if hosts is not None and host not in hosts:
        errors.append("%s: URL host %r is not one of the company's watchlist hosts" % (label, host))
    notes = new.get("notes") or ""
    if "http" in notes.lower() or "](" in notes:
        errors.append("%s: notes must not contain URLs or markdown links" % label)
    if len(notes) > MAX_NOTES:
        errors.append("%s: notes are %d characters (max %d)" % (label, len(notes), MAX_NOTES))

    fields = claim_fields(old, new)
    if not fields:
        return errors
    if text is None:
        errors.append("%s: posting could not be fetched, so %s cannot be checked; verify by hand"
                      % (label, "/".join(fields)))
        return errors
    if "pay" in fields:
        missing = pay_missing(new["pay"], text)
        if missing:
            errors.append("%s: pay %r not found in posting (missing %s)"
                          % (label, new["pay"], ", ".join("%g" % n for n in missing)))
    if "deadline" in fields and not deadline_in(new["deadline"], text):
        errors.append("%s: deadline %s not found in posting" % (label, new["deadline"]))
    if "sponsorship" in fields and not sponsorship_wording(text):
        errors.append("%s: sponsorship %r but posting has no sponsor/authorization wording"
                      % (label, new["sponsorship"]))
    return errors


# --- CLI -----------------------------------------------------------------------------------------------------

def load_base(ref):
    p = subprocess.run(["git", "show", "%s:listings.json" % ref], cwd=ROOT,
                       capture_output=True, text=True)
    if p.returncode != 0:  # file absent at that ref: every head row counts as added
        return []
    return json.loads(p.stdout)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="git ref to diff listings.json against")
    ap.add_argument("--listings", default=str(LISTINGS))
    args = ap.parse_args(argv)

    head = json.loads(Path(args.listings).read_text(encoding="utf-8"))
    rows = changed_rows(load_base(args.base), head)
    if not rows:
        print("## Listing validation\n\nNo rows changed; nothing to check.")
        return 0

    entries = load_watchlist()
    watched = {e["company"] for e in entries}

    def work(pair):
        old, new = pair
        text = None
        if claim_fields(old, new):
            text = fetch_text(new["company"], new["url"], entries)
        hosts = hosts_for(new["company"], entries) if new["company"] in watched else None
        return check_row(old, new, text, hosts)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, rows))

    failed = [(new, errs) for (_, new), errs in zip(rows, results) if errs]
    print("## Listing validation\n")
    print("Checked %d changed row(s): %d passed, %d failed.\n"
          % (len(rows), len(rows) - len(failed), len(failed)))
    for new, errs in failed:
        for e in errs:
            print("- " + e)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
