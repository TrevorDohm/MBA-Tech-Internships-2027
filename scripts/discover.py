#!/usr/bin/env python3
"""Find postings at watched companies that listings.json does not yet cover.

    python3 scripts/discover.py [--listings PATH] [--out candidates.json] [--replay]

Reads every feed in watchlist.json, drops postings already listed or in
ignored.json, keeps internship postings whose text mentions MBA/graduate and
2027/summer, and groups the same role posted in several cities. A listed open
row whose posting vanished while the feed carries the same title under a new
id is reported as a repost (update the row in place, do not add a new one).

Exit status: 0 normally; 2 when some feeds failed but candidates were still
written; anything else is fatal.

--replay checks the feeds against the listings instead: recall is the share of
open listed rows (at watched companies) whose job id or URL a feed returns.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ats import FeedError, fetch, list_jobs, posting_text
from watchlist import load_watchlist

ROOT = Path(__file__).resolve().parent.parent
LISTINGS = ROOT / "listings.json"
IGNORED = ROOT / "ignored.json"

# --- ids ---------------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Path shapes, tried in order; group 1 is the id. Each mirrors what ats.py assigns as Job.id.
_PATH_IDS = [re.compile(p, re.I) for p in (
    r"/jobs?/(\d+)(?:[/?#-]|$)",                   # Greenhouse, Duolingo, Amazon, Jibe, Oracle, Eightfold
    r"/positions/(\d+)(?:[/?#]|$)",                # Airbnb
    r"/job-description/(\d+)(?:[/?#]|$)",          # c3.ai
    r"/results/(\d+)-",                            # Google
    r"/(?:details|job-detail)/(\d+)(?:[/?#]|$)",   # Apple, Atlassian, Bank of America
    r"/search/(\d+)(?:[/?#]|$)",                   # TikTok
    r"/JobDetail/.*/(\d+)/?$",                     # Electronic Arts (Avature)
    r"/(" + _UUID + r")/?$",                       # Ashby, Lever
    r"/(\d{6,})/?$",                               # Radancy (Intuit, Capital One)
)]


def job_id(url):
    """The ATS requisition id in a posting URL, as the adapters assign it; None for landing pages."""
    if not url:
        return None
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    for key in ("gh_jid", "jobId"):  # Waymo/Epic/Robinhood and IBM carry the id only here
        if q.get(key) and q[key][0]:
            return q[key][0]
    path = parts.path
    if parts.netloc.lower().endswith("myworkdayjobs.com"):
        i = path.find("/job/")
        if i < 0:
            return None
        last = path[i:].rstrip("/").rsplit("/", 1)[-1]
        return last.rsplit("_", 1)[-1] if "_" in last else last
    for rx in _PATH_IDS:
        m = rx.search(path)
        if m:
            return m.group(1)
    return None


def norm_url(url):
    p = urlsplit(url or "")
    return f"{p.netloc.lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


# --- title rules ---------------------------------------------------------------

def title_stem(title):
    """Lowercased title without year, MBA tags, trailing parenthetical locations or punctuation."""
    s = title.lower()
    s = re.sub(r"\(\s*mba\s*\)", " ", s)
    s = re.sub(r"[-–—]\s*mba\b", " ", s)
    s = re.sub(r"\([^)]*\)\s*$", " ", s)  # a trailing "(Seattle, WA)"
    s = re.sub(r"\bsummer\s+2027\b", " ", s)
    s = re.sub(r"\b2027\b", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


_INTERN = re.compile(r"\b(intern|internship|summer associate|graduate internship)\b", re.I)
_MBA = re.compile(r"\bMBA\b", re.I)
_GRAD = re.compile(r"\b(graduate|master'?s)\b", re.I)
_YEAR = re.compile(r"2027|summer", re.I)
_NOT_MBA_TITLE = re.compile(r"\b(phd|undergraduate|bachelor'?s)\b", re.I)


def is_internship_title(title):
    return bool(_INTERN.search(title))


def is_candidate(title, text):
    if not is_internship_title(title):
        return False
    both = f"{title}\n{text}"
    has_mba = bool(_MBA.search(both))
    if not (has_mba or _GRAD.search(both)):
        return False
    if not _YEAR.search(both):
        return False
    return not (_NOT_MBA_TITLE.search(title) and not has_mba)


# --- discovery -----------------------------------------------------------------------

def _known(listings, ignored):
    """Sets of (company, id) and (company, normalised url) for listed and ignored postings."""
    ids, urls = set(), set()
    for r in listings:
        i = job_id(r["url"])
        if i:
            ids.add((r["company"], i))
        urls.add((r["company"], norm_url(r["url"])))
    for r in ignored:
        if r.get("job_id"):
            ids.add((r["company"], str(r["job_id"])))
        if r.get("url"):
            urls.add((r["company"], norm_url(r["url"])))
    return ids, urls


def discover(entries, listings, ignored, http=fetch):
    known_ids, known_urls = _known(listings, ignored)
    out = {"candidates": [], "reposts": [], "feed_failures": [], "checked": 0}
    for entry in entries:
        company = entry["company"]
        try:
            jobs = list_jobs(entry, http=http)
        except FeedError as e:
            out["feed_failures"].append({"company": company, "error": str(e)})
            continue
        out["checked"] += len(jobs)
        feed_ids = {j["id"] for j in jobs}
        feed_urls = {norm_url(j["url"]) for j in jobs}
        fresh = [j for j in jobs
                 if (company, j["id"]) not in known_ids and (company, norm_url(j["url"])) not in known_urls]

        # Listed open rows that left the feed, matched to a fresh job with the same title stem.
        gone = []
        for r in listings:
            i = job_id(r["url"])
            if (r["company"] == company and r.get("status") == "open" and i
                    and i not in feed_ids and norm_url(r["url"]) not in feed_urls):
                gone.append(r)
        taken = set()
        for r in gone:
            stem = title_stem(r["role"])
            for j in fresh:
                if j["id"] not in taken and stem and title_stem(j["title"]) == stem:
                    taken.add(j["id"])
                    out["reposts"].append({"company": company, "old_url": r["url"],
                                           "new_url": j["url"], "title": j["title"]})
                    break

        # Sibling postings (same role, another city) of a row we already list are covered by that row.
        listed_stems = {title_stem(r["role"]) for r in listings if r["company"] == company} - {""}
        groups = {}
        for j in fresh:
            if j["id"] in taken or not is_internship_title(j["title"]) or title_stem(j["title"]) in listed_stems:
                continue
            try:
                text = posting_text(entry, j, http=http)
            except FeedError:
                out["feed_failures"].append({"company": company,
                                             "error": f"posting text unavailable: {j['url']}"})
                continue
            if not is_candidate(j["title"], text):
                continue
            key = title_stem(j["title"])
            c = groups.get(key)
            if c is None:
                groups[key] = {"company": company, "title": j["title"], "url": j["url"], "job_id": j["id"],
                               "locations": [], "members": [], "text": text}
                c = groups[key]
            c["members"].append({"id": j["id"], "url": j["url"], "location": j["location"]})
            if j["location"] and j["location"] not in c["locations"]:
                c["locations"].append(j["location"])
        out["candidates"] += groups.values()
    return out


def recall(entries, listings, http=fetch):
    """(hits, total, misses, feed_failures) over open listed rows at companies whose feed loaded."""
    hit, total, misses, failures = 0, 0, [], []
    for entry in entries:
        company = entry["company"]
        try:
            jobs = list_jobs(entry, http=http)
        except FeedError as e:
            failures.append({"company": company, "error": str(e)})
            continue
        ids = {j["id"] for j in jobs}
        urls = {norm_url(j["url"]) for j in jobs}
        for r in listings:
            if r["company"] != company or r.get("status") != "open":
                continue
            total += 1
            if job_id(r["url"]) in ids or norm_url(r["url"]) in urls:
                hit += 1
            else:
                misses.append(r)
    return hit, total, misses, failures


# --- CLI ----------------------------------------------------------------------------------

def _load_json(path, default):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def main(argv):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--listings", default=str(LISTINGS))
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--replay", action="store_true", help="print recall of the feeds against the listed open rows")
    args = ap.parse_args(argv)

    entries = load_watchlist()
    listings = _load_json(args.listings, [])
    if args.replay:
        hit, total, misses, failures = recall(entries, listings)
        for r in misses:
            print(f"missed: {r['company']}: {r['role']} {r['url']}")
        for f in failures:
            print(f"feed failure: {f['company']}: {f['error']}", file=sys.stderr)
        print(f"recall: {hit}/{total} = {hit / total:.0%}" if total else "recall: no open rows at watched companies")
        return 2 if failures else 0

    result = discover(entries, listings, _load_json(IGNORED, []))
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for f in result["feed_failures"]:
        print(f"feed failure: {f['company']}: {f['error']}", file=sys.stderr)
    print(f"checked {result['checked']} postings: {len(result['candidates'])} candidates, "
          f"{len(result['reposts'])} reposts, {len(result['feed_failures'])} feed failures")
    return 2 if result["feed_failures"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
