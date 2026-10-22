#!/usr/bin/env python3
"""Fail a PR whose changed listing rows make claims the posting does not support.

For every row added or changed against --base, re-fetch the company's own posting and check that
pay figures, the deadline and the sponsorship claim actually appear in it, that the URL host is
on the company's watchlist, and that notes carry no links.

Usage:
    python3 scripts/validate_listings.py --base origin/main [--labels verified-by-hand,...]

The `verified-by-hand` label turns "verify by hand" (posting unfetchable) errors into warnings.
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from ats import FeedError, fetch, posting_text
from discover import job_id
from watchlist import hosts_for, load_watchlist

ROOT = Path(__file__).resolve().parent.parent
LISTINGS = ROOT / "listings.json"
CLAIMS = ("pay", "deadline", "sponsorship")
MAX_NOTES = 300
OVERRIDE_LABEL = "verified-by-hand"
BY_HAND = "verify by hand"
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


# --- fetching ------------------------------------------------------------------------------------------------

def fetch_text(company, url, entries, http=fetch):
    """Plain text of the row's posting, or None if it cannot be fetched or has no job id."""
    entry = next((e for e in entries if e["company"] == company), None)
    jid = job_id(url)
    if entry is None or jid is None:
        return None
    host = urlparse(url).hostname or ""
    if entry["ats"] == "workday" and host != entry["host"]:
        # Public career-site URLs (Capital One) are Radancy pages, not the Workday req itself.
        entry = dict(entry, ats="radancy", host=host)
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


# --- pay -----------------------------------------------------------------------------------------------------

_CODES = {"usd": "USD", "eur": "EUR", "gbp": "GBP", "jpy": "JPY", "cny": "CNY", "cad": "CAD",
          "aud": "AUD", "chf": "CHF", "inr": "INR"}
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_CODE_RX = "|".join(_CODES)
_TOKEN = re.compile(
    r"(?P<pre>[$€£¥]|\b(?:%s)\s*\$?)?\s*(?<![\d.])(?P<num>\d+(?:\.\d+)?)(?P<k>\s?k\b)?"
    r"(?:\s*(?P<post>%s)\b)?" % (_CODE_RX, _CODE_RX))
_RANGE_GAP = re.compile(r"^\s*(?:-|to)\s*$")


def _norm(s):
    s = s.replace("’", "'")
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    s = re.sub(r"[‒-―−]", "-", s)
    return s.lower()


def _cur(mark):
    if not mark:
        return None
    mark = mark.strip().rstrip("$").strip() or "$"
    return _SYMBOLS.get(mark) or _CODES.get(mark) or "USD"


def _tokens(s):
    """[(number, currency or None)] with currency inherited across ranges like '$47 - 57' or '63 - 65 USD'."""
    toks = []
    for m in _TOKEN.finditer(_norm(s)):
        n = float(m.group("num"))
        if m.group("k"):
            n *= 1000
        toks.append([n, _cur(m.group("pre") or m.group("post")), m.start(), m.end()])
    norm = _norm(s)
    for i in range(1, len(toks)):  # forward: second member of a range inherits
        if toks[i][1] is None and toks[i - 1][1] and _RANGE_GAP.match(norm[toks[i - 1][3]:toks[i][2]]):
            toks[i][1] = toks[i - 1][1]
    for i in range(len(toks) - 2, -1, -1):  # backward: first member of '63 - $65'
        if toks[i][1] is None and toks[i + 1][1] and _RANGE_GAP.match(norm[toks[i][3]:toks[i + 1][2]]):
            toks[i][1] = toks[i + 1][1]
    return [(n, c) for n, c, _, _ in toks]


def pay_missing(pay, text):
    """Numbers in the pay value the posting does not state as money; None if the value has no figures."""
    want = _tokens(pay)
    if not want:
        return None
    have = _tokens(text)
    any_number = {n for n, _ in have}
    by_cur = {}
    for n, c in have:
        if c:
            by_cur.setdefault(c, set()).add(n)
    out = []
    for n, c in want:
        ok = n in by_cur.get(c, set()) if c else n in any_number
        if not ok:
            out.append((n, c))
    return out


# --- deadline ------------------------------------------------------------------------------------------------

def deadline_in(iso, text):
    d = date.fromisoformat(iso)
    t = _norm(text)
    day = r"0?%d" % d.day
    m = r"(?:%s|%s)\.?" % (MONTHS[d.month - 1], MONTHS[d.month - 1][:3])
    if re.search(r"(?<!\d)%s-%02d-%02d(?!\d)" % (d.year, d.month, d.day), t):
        return True
    # A UTC timestamp early in the day is the previous evening in the US ("2026-11-01T03:59:00+00:00").
    for hit in re.finditer(r"(?<!\d)(\d{4}-\d{2}-\d{2})t(\d{2}):\d{2}(?::\d{2})?(?:z|\+00:?00)", t):
        if int(hit.group(2)) < 12 and date.fromisoformat(hit.group(1)) - timedelta(days=1) == d:
            return True
    year_after = r"(?:,?\s*(\d{4})(?!\d))?"
    pats = (r"\b%s\s+(?<!\d)%s(?!\d)(?:st|nd|rd|th)?%s" % (m, day, year_after),
            r"(?<!\d)%s(?!\d)(?:st|nd|rd|th)?(?:\s+of)?\s+%s%s" % (day, m, year_after))
    for p in pats:
        for hit in re.finditer(p, t):
            y = hit.group(1)
            if y is None or int(y) == d.year:
                return True
    # Numeric dates are US month/day; day/month only when the day cannot be a month.
    numeric = r"(?<![\d/])0?%d/0?%d(?:/(\d{2,4}))?(?![\d/])"
    pairs = [(d.month, d.day)] + ([(d.day, d.month)] if d.day > 12 else [])
    for a, b in pairs:
        for hit in re.finditer(numeric % (a, b), t):
            y = hit.group(1)
            if y is None or int(y) % 100 == d.year % 100:
                return True
    return False


# --- sponsorship ---------------------------------------------------------------------------------------------

_NEG_BEFORE = re.compile(r"\b(?:not|no|unable|cannot|can't|won't|doesn't|ineligible|unavailable)\b")
_NEG_AFTER = re.compile(r"\b(?:not|unavailable|ineligible)\b|n't\b")
BEFORE, AFTER = 60, 40  # R16 says ~40 before; 60 covers "do not offer any type of employment-based immigration sponsorship"


def _negated(t, i, j):
    return bool(_NEG_BEFORE.search(t[max(0, i - BEFORE):i]) or _NEG_AFTER.search(t[j:j + AFTER]))


def sponsorship_supports(claim, text):
    """True if the posting's wording supports a sponsorship claim of 'yes' or 'no' (R16)."""
    t = _norm(text)
    hits = [(m.start(), m.end()) for m in re.finditer(r"sponsor\w*", t)]
    negated = [_negated(t, i, j) for i, j in hits]
    if claim == "yes":
        return bool(hits) and not any(negated)
    if any(negated):
        return True
    for m in re.finditer(r"sponsor|authori[zs]", t):
        if "without" in t[max(0, m.start() - 40):m.end() + 40]:
            return True
    return False


# --- per-row check -------------------------------------------------------------------------------------------

def host_ok(row, hosts):
    return hosts is None or (urlparse(row["url"]).hostname or "") in hosts


def check_row(old, new, text, hosts):
    """Error strings for one changed row. `hosts` None means the company is not host-checked."""
    label = "%s / %s" % (new.get("company"), new.get("role"))
    errors = []
    if not host_ok(new, hosts):
        errors.append("%s: URL host %r is not one of the company's watchlist hosts"
                      % (label, urlparse(new["url"]).hostname or ""))
    notes = new.get("notes") or ""
    if "http" in notes.lower() or "](" in notes:
        errors.append("%s: notes must not contain URLs or markdown links" % label)
    if len(notes) > MAX_NOTES:
        errors.append("%s: notes are %d characters (max %d)" % (label, len(notes), MAX_NOTES))
    if errors and not host_ok(new, hosts):
        return errors  # an unknown host is not worth verifying claims against

    fields = claim_fields(old, new)
    if "deadline" in fields:
        try:
            date.fromisoformat(new["deadline"])
        except (ValueError, TypeError):
            errors.append("%s: deadline %r is not a YYYY-MM-DD date" % (label, new["deadline"]))
            fields.remove("deadline")
    if not fields:
        return errors
    if text is None:
        errors.append("%s: posting could not be fetched, so %s cannot be checked; %s"
                      % (label, "/".join(fields), BY_HAND))
        return errors
    if "pay" in fields:
        missing = pay_missing(new["pay"], text)
        if missing is None:
            errors.append("%s: pay %r contains no figures to verify" % (label, new["pay"]))
        elif missing:
            errors.append("%s: pay %r not found in posting (missing %s)" % (label, new["pay"], ", ".join(
                "%s%g" % ({"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(c, (c or "") + " "), n)
                for n, c in missing)))
    if "deadline" in fields and not deadline_in(new["deadline"], text):
        errors.append("%s: deadline %s not found in posting" % (label, new["deadline"]))
    if "sponsorship" in fields and not sponsorship_supports(new["sponsorship"], text):
        errors.append("%s: sponsorship %r not supported by the posting's sponsorship wording"
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
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--base", required=True, help="git ref to diff listings.json against")
    ap.add_argument("--listings", default=str(LISTINGS))
    ap.add_argument("--labels", default="", help="comma-separated PR labels")
    args = ap.parse_args(argv)
    override = OVERRIDE_LABEL in {x.strip() for x in args.labels.split(",")}

    head = json.loads(Path(args.listings).read_text(encoding="utf-8"))
    rows = changed_rows(load_base(args.base), head)
    if not rows:
        print("## Listing validation\n\nNo rows changed; nothing to check.")
        return 0

    entries = load_watchlist()
    watched = {e["company"] for e in entries}

    def work(pair):
        old, new = pair
        hosts = hosts_for(new["company"], entries) if new["company"] in watched else None
        text = None
        if claim_fields(old, new) and host_ok(new, hosts):
            text = fetch_text(new["company"], new["url"], entries)
        return check_row(old, new, text, hosts)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, rows))

    errors, warnings, failed = [], [], 0
    for errs in results:
        hard = [e for e in errs if not (override and e.endswith(BY_HAND))]
        warnings += [e for e in errs if override and e.endswith(BY_HAND)]
        errors += hard
        failed += bool(hard)
    print("## Listing validation\n")
    print("Checked %d changed row(s): %d passed, %d failed.\n" % (len(rows), len(rows) - failed, failed))
    for e in errors:
        print("- " + e)
    if warnings:
        print("\nWarnings (label `%s` set; these rows were not verified against a posting):\n" % OVERRIDE_LABEL)
        for e in warnings:
            print("- " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
