import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

import tests  # noqa: F401  (package init puts scripts/ on sys.path)
import watchlist
from watchlist import ATS_TYPES, hosts_for, load_watchlist

ROOT = Path(__file__).resolve().parent.parent
EXEMPT = {"Meta", "Uber", "Walmart", "Bank of America"}

REQUIRED_KEYS = {
    "greenhouse": {"board"},
    "ashby": {"org"},
    "lever": {"org"},
    "workday": {"host", "tenant", "site"},
    "oracle": {"host", "site_number"},
    "eightfold": {"host", "domain"},
    "jibe": {"host"},
    "radancy": {"host"},
    "avature": {"host"},
}


def listings():
    return json.loads((ROOT / "listings.json").read_text(encoding="utf-8"))


def load_from(entries):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.json"
        p.write_text(json.dumps(entries), encoding="utf-8")
        return load_watchlist(p)


class WatchlistTests(unittest.TestCase):
    def test_loads_repo_watchlist(self):
        entries = load_watchlist()
        self.assertTrue(entries)
        for e in entries:
            self.assertIn(e["ats"], ATS_TYPES, e["company"])
            self.assertLessEqual(REQUIRED_KEYS.get(e["ats"], set()), set(e), e["company"])
            self.assertTrue(e["queries"], e["company"])
            self.assertTrue(e["hosts"], e["company"])

    def test_every_open_company_is_watched_or_exempt(self):
        watched = {e["company"] for e in load_watchlist()}
        for row in listings():
            if row["status"] == "open":
                self.assertTrue(
                    row["company"] in watched or row["company"] in EXEMPT, row["company"]
                )

    def test_hosts_cover_listing_urls(self):
        entries = load_watchlist()
        for row in listings():
            if row["status"] != "open" or row["company"] in EXEMPT:
                continue
            host = urlparse(row["url"]).hostname
            self.assertIn(host, hosts_for(row["company"], entries), row["company"])

    def test_rejects_unknown_ats(self):
        bad = [{"company": "X", "ats": "foo", "queries": ["MBA"], "hosts": ["x.com"]}]
        with self.assertRaises(ValueError):
            load_from(bad)

    def test_rejects_missing_required_key(self):
        bad = [{"company": "X", "ats": "greenhouse", "queries": ["MBA"], "hosts": ["x.com"]}]
        with self.assertRaises(ValueError):
            load_from(bad)

    def test_allowlist_sorted_unique_with_github(self):
        entries = [
            {"company": "A", "hosts": ["b.com", "a.com"]},
            {"company": "B", "hosts": ["a.com"]},
        ]
        self.assertEqual(
            watchlist.allowlist(entries),
            ["a.com", "api.github.com", "b.com", "github.com"],
        )

    def test_hosts_for_unknown_company_is_empty(self):
        self.assertEqual(hosts_for("Nope", []), set())


if __name__ == "__main__":
    unittest.main()
