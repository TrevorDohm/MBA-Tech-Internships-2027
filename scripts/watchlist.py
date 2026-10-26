#!/usr/bin/env python3
"""Load and validate watchlist.json, the per-company ATS feed configuration.

Usage:
    python3 scripts/watchlist.py --hosts   # print the network allowlist, one host per line
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.json"

ATS_TYPES = (
    "greenhouse", "ashby", "lever", "workday", "oracle", "eightfold", "amazon",
    "google", "apple", "tiktok", "ibm", "jibe", "radancy", "avature", "atlassian",
)

# Keys each ATS adapter needs beyond the common ones; absent means none.
ATS_KEYS = {
    "greenhouse": ("board",),
    "ashby": ("org",),
    "lever": ("org",),
    "workday": ("host", "tenant", "site"),
    "oracle": ("host", "site_number"),
    "eightfold": ("host", "domain"),
    "jibe": ("host",),
    "radancy": ("host",),
    "avature": ("host",),
}
COMMON_KEYS = ("company", "ats", "queries", "hosts")
ALWAYS_ALLOWED = ("github.com", "api.github.com")


def validate(entries):
    errors = []
    if not isinstance(entries, list):
        return ["watchlist must be a JSON array"]
    seen = set()
    for i, e in enumerate(entries):
        where = f"entry {i} ({e.get('company', '?') if isinstance(e, dict) else '?'})"
        if not isinstance(e, dict):
            errors.append(f"{where}: not an object")
            continue
        if e.get("ats") not in ATS_TYPES:
            errors.append(f"{where}: unknown ats {e.get('ats')!r}")
            continue
        for key in COMMON_KEYS + ATS_KEYS.get(e["ats"], ()):
            if not e.get(key):
                errors.append(f"{where}: missing {key!r}")
        for key in ("queries", "hosts"):
            v = e.get(key)
            if v and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                errors.append(f"{where}: {key!r} must be a list of strings")
        if e.get("company") in seen:
            errors.append(f"{where}: duplicate company")
        seen.add(e.get("company"))
    return errors


def load_watchlist(path=WATCHLIST):
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate(entries)
    if errors:
        raise ValueError("invalid watchlist:\n  " + "\n  ".join(errors))
    return entries


def hosts_for(company, entries):
    for e in entries:
        if e["company"] == company:
            return set(e["hosts"])
    return set()


def allowlist(entries):
    hosts = {h for e in entries for h in e["hosts"]}
    return sorted(hosts | set(ALWAYS_ALLOWED))


def main(argv):
    if argv == ["--hosts"]:
        print("\n".join(allowlist(load_watchlist())))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
