import copy
import json
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import tests  # noqa: F401  (package init puts scripts/ on sys.path)
import ats
from ats import FeedError, list_jobs, posting_text
from watchlist import load_watchlist

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENTRIES = {e["company"]: e for e in load_watchlist()}


def fixture(ats_name, kind):
    ext = ats.EXTENSIONS[ats_name][0 if kind == "list" else 1]
    return (FIXTURES / f"{ats_name}_{kind}.{ext}").read_text(encoding="utf-8")


def serving(text):
    """An http fake that answers every request with `text`."""
    def http(url, method="GET", body=None, headers=None):
        return 200, text
    return http


# ats -> (company in watchlist.json, first job's id, title, url, sentence in the detail fixture)
CASES = {
    "greenhouse": ("Airbnb", "7789554", "Associate Legal Counsel, Japan",
                   "https://careers.airbnb.com/positions/7789554",
                   "Airbnb was born in 2007 when two hosts welcomed three guests"),
    "ashby": ("Snowflake", "479e06f1-273d-4a1b-af56-6543761ebd75", "Lead, Strategic Finance - Deals (International)",
              "https://jobs.ashbyhq.com/snowflake/479e06f1-273d-4a1b-af56-6543761ebd75",
              "we are powering the era of the agentic enterprise"),
    "lever": ("Spotify", "f7eb82ce-b57e-4dc0-b84a-7f7ec05bf66e", "Agency Development Lead - France",
              "https://jobs.lever.co/spotify/f7eb82ce-b57e-4dc0-b84a-7f7ec05bf66e",
              "Sell what you love."),
    "workday": ("Nike", "R-95789-1", "Converse Social Community Impact Graduate Internship",
                "https://nike.wd1.myworkdayjobs.com/nke/job/Boston-Massachusetts/"
                "Converse-Social-Community-Impact-Graduate-Internship_R-95789-1",
                "The annual base salary for this position ranges from $63,500.00"),
    "oracle": ("JPMorgan Chase", "210796897", "Portfolio Manager - Asset Based Lending Credit Risk - Executive Director",
               "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210796897",
               "Bring your expertise to JPMorgan Chase and join a leading team of Asset Based Lending credit risk"),
    "eightfold": ("Microsoft", "1970393557040991",
                  "Data Science - Forward Deployed Engineer: PhD Internship Opportunities",
                  "https://apply.careers.microsoft.com/careers/job/1970393557040991",
                  "Come build community, explore your passions"),
    "jibe": ("AMD", "84741", "Director, Product Planning-ASIC/SoC",
             "https://careers.amd.com/careers-home/jobs/84741",
             "WHAT YOU DO AT AMD CHANGES EVERYTHING"),
    "radancy": ("Intuit", "101313096080", "Summer 2027: MBA Corporate Strategy & Development Intern",
                "https://jobs.intuit.com/job/mountain-view/summer-2027-mba-corporate-strategy-and-development-intern/"
                "27595/101313096080",
                "Intuit internships are 12 weeks throughout the summer"),
    "avature": ("Electronic Arts", "216272", "Product Manager Intern, MBA Level - Summer 2027 (Apex Legends)",
                "https://jobs.ea.com/en_US/careers/JobDetail/Product-Manager-Intern-MBA-Level-Summer-2027-Apex-Legends/216272",
                "Worker Type Intern - Temporary Employee"),
    "amazon": ("Amazon", "10586117", "Launch MBA Program",
               "https://www.amazon.jobs/en/jobs/10586117/launch-mba-program",
               "Amazon’s corporate MBA program for Europe"),
    "google": ("Google", "110899190426411718", "MBA Intern, 2027",
               "https://www.google.com/about/careers/applications/jobs/results/110899190426411718-mba-intern-2027",
               "Currently enrolled in a MBA program."),
    "apple": ("Apple", "200690876", "Principle Product Manager, Search Platform",
              "https://jobs.apple.com/en-us/details/200690876/principle-product-manager-search-platform",
              "Build the future of Apple's search platform at the core of Apple's AI strategy."),
    "tiktok": ("TikTok", "7669871213245974837",
               "Project Management Office Associate Graduate (Finance) - 2027 Start (MBA)",
               "https://lifeattiktok.com/search/7669871213245974837",
               "We partner closely with business leaders and cross-functional teams"),
    "ibm": ("IBM", "136426", "Product Management Intern 2027",
            "https://careers.ibm.com/careers/JobDetail?jobId=136426",
            "IBM Quantum is building the world’s leading quantum computing systems"),
    "atlassian": ("Atlassian", "25480", "Account Executive, Mid Market",
                  "https://www.atlassian.com/company/careers/details/25480",
                  "Atlassians can choose where they work"),
}


def _make_tests():
    def list_test(name, case):
        company, id_, title, url, _ = case

        def test(self):
            jobs = list_jobs(ENTRIES[company], http=serving(fixture(name, "list")))
            self.assertEqual(len(jobs), 3)
            self.assertEqual((jobs[0]["id"], jobs[0]["title"], jobs[0]["url"]), (id_, title, url))
            for j in jobs:
                self.assertEqual(set(j), {"id", "title", "url", "location"})
                # Public and query-free, except IBM's jobId= which is the only identifier it has.
                self.assertIn(urlsplit(j["url"]).query, ("", f"jobId={j['id']}"))
        return test

    def text_test(name, case):
        company, id_, _, url, sentence = case

        def test(self):
            # Task 4 calls posting_text with only id and url (ruling R2).
            text = posting_text(ENTRIES[company], {"id": id_, "url": url}, http=serving(fixture(name, "detail")))
            self.assertIn(sentence, text)
            self.assertNotRegex(text, r"<[a-z/]")
            self.assertNotIn("  ", text)
        return test

    for name, case in CASES.items():
        setattr(AdapterTests, f"test_list_jobs_{name}", list_test(name, case))
        setattr(AdapterTests, f"test_posting_text_{name}", text_test(name, case))


class FakeClock:
    """Replaces time.sleep/monotonic so Eightfold spacing is checked without waiting."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.requests = []  # clock reading at each http call

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def recording(self, http):
        def wrapped(url, method="GET", body=None, headers=None):
            self.requests.append(self.now)
            return http(url, method=method, body=body, headers=headers)
        return wrapped

    def patched(self):
        stack = mock.patch.multiple("ats.time", sleep=self.sleep, monotonic=lambda: self.now)
        ats._EF_LAST.clear()
        return stack


class AdapterTests(unittest.TestCase):
    def setUp(self):
        ats._EF_LAST.clear()

    def test_every_ats_has_a_case_and_fixtures(self):
        from watchlist import ATS_TYPES
        self.assertEqual(set(CASES), set(ATS_TYPES))
        for name in ATS_TYPES:
            for kind in ("list", "detail"):
                self.assertTrue(fixture(name, kind))

    def test_workday_end_date_appended(self):
        company, id_, _, url, _ = CASES["workday"]
        text = posting_text(ENTRIES[company], {"id": id_, "url": url}, http=serving(fixture("workday", "detail")))
        self.assertTrue(text.endswith("Posting end date: 2026-11-30"), text[-60:])

    def test_workday_detail_url_tolerates_locale_prefix(self):
        seen = []

        def http(url, method="GET", body=None, headers=None):
            seen.append(url)
            return 200, fixture("workday", "detail")

        url = "https://nike.wd1.myworkdayjobs.com/en-US/nke/job/Boston-Massachusetts/X_R-95789-1"
        posting_text(ENTRIES["Nike"], {"id": "R-95789-1", "url": url}, http=http)
        self.assertEqual(seen, ["https://nike.wd1.myworkdayjobs.com/wday/cxs/nike/nke"
                                "/job/Boston-Massachusetts/X_R-95789-1"])

    def test_oracle_end_date_appended(self):
        detail = json.loads(fixture("oracle", "detail"))
        detail["items"][0]["ExternalPostedEndDate"] = "2026-12-01"
        text = posting_text(ENTRIES["JPMorgan Chase"], {"id": "210796897", "url": "u"},
                            http=serving(json.dumps(detail)))
        self.assertTrue(text.endswith("Posting end date: 2026-12-01"))
        self.assertIn("$153,140.00-$280,000.00", text)  # pay from the requisition's flex fields

    def test_oracle_amex_public_host(self):
        listing = json.loads(fixture("oracle", "list"))
        jobs = list_jobs(ENTRIES["American Express"], http=serving(json.dumps(listing)))
        self.assertEqual(jobs[0]["url"], "https://careers.americanexpress.com/en/sites/CX_1/job/210796897")

    def test_greenhouse_keeps_gh_jid_when_path_has_no_id(self):
        board = {"jobs": [{"id": 8234553, "title": "T", "location": {"name": "X"},
                           "absolute_url": "https://careers.withwaymo.com/jobs?gh_jid=8234553"},
                          {"id": 5, "title": "U", "absolute_url": "https://x.example/positions/5?gh_jid=5"}]}
        jobs = list_jobs(ENTRIES["Waymo"], http=serving(json.dumps(board)))
        self.assertEqual([j["url"] for j in jobs],
                         ["https://careers.withwaymo.com/jobs?gh_jid=8234553", "https://x.example/positions/5"])

    def test_queries_are_all_run_and_deduped(self):
        calls = []

        def http(url, method="GET", body=None, headers=None):
            calls.append(json.loads(body)["searchText"])
            return 200, fixture("workday", "list")

        jobs = list_jobs(ENTRIES["Nike"], http=http)  # queries: MBA, intern
        self.assertEqual(calls, ["MBA", "intern"])
        self.assertEqual(len(jobs), 3)

    def test_workday_paginates_limit_20(self):
        entry = dict(ENTRIES["Nike"], queries=["MBA"])
        bodies = []

        def http(url, method="GET", body=None, headers=None):
            b = json.loads(body)
            bodies.append(b)
            n = min(20, 45 - b["offset"])
            posts = [{"title": f"t{b['offset'] + i}", "externalPath": f"/job/X/t{b['offset'] + i}_R-{b['offset'] + i}",
                      "locationsText": "L"} for i in range(n)]
            return 200, json.dumps({"total": 45, "jobPostings": posts})

        jobs = list_jobs(entry, http=http)
        self.assertEqual([b["limit"] for b in bodies], [20, 20, 20])
        self.assertEqual([b["offset"] for b in bodies], [0, 20, 40])
        self.assertEqual(len(jobs), 45)
        self.assertEqual(bodies[0]["searchText"], "MBA")

    def test_workday_total_only_on_first_page(self):
        entry = dict(ENTRIES["Nike"], queries=["MBA"])
        offsets = []

        def http(url, method="GET", body=None, headers=None):
            b = json.loads(body)
            offsets.append(b["offset"])
            n = min(20, 30 - b["offset"])
            posts = [{"title": "t", "externalPath": f"/job/X/t_R-{b['offset'] + i}"} for i in range(n)]
            return 200, json.dumps({"total": 30 if b["offset"] == 0 else 0, "jobPostings": posts})

        self.assertEqual(len(list_jobs(entry, http=http)), 30)
        self.assertEqual(offsets, [0, 20])

    def test_workday_stops_when_page_repeats(self):
        entry = dict(ENTRIES["General Motors"], queries=["MBA"])
        page = json.dumps({"total": 100, "jobPostings": [
            {"title": f"t{i}", "externalPath": f"/job/X/t{i}_R-{i}", "locationsText": "L"} for i in range(20)]})
        calls = []

        def http(url, method="GET", body=None, headers=None):
            calls.append(json.loads(body)["offset"])
            return 200, page

        jobs = list_jobs(entry, http=http)
        self.assertEqual(len(jobs), 20)
        self.assertEqual(len({j["id"] for j in jobs}), 20)
        self.assertEqual(calls, [0, 20])  # the second page added nothing, so it stopped

    def test_zero_results_is_feed_error(self):
        for name, empty in (("greenhouse", {"jobs": []}), ("workday", {"total": 0, "jobPostings": []}),
                            ("amazon", {"hits": 0, "jobs": []}), ("atlassian", [])):
            with self.subTest(name):
                entry = ENTRIES[CASES[name][0]]
                with self.assertRaises(FeedError):
                    list_jobs(entry, http=serving(json.dumps(empty)))

    def test_zero_results_across_all_queries_only(self):
        # One empty query is fine as long as another returned something.
        def http(url, method="GET", body=None, headers=None):
            if json.loads(body)["searchText"] == "MBA":
                return 200, json.dumps({"total": 0, "jobPostings": []})
            return 200, fixture("workday", "list")

        self.assertEqual(len(list_jobs(ENTRIES["Nike"], http=http)), 3)

    def test_http_error_is_feed_error(self):
        for name in ("greenhouse", "workday", "google", "avature"):
            with self.subTest(name):
                with self.assertRaises(FeedError):
                    list_jobs(ENTRIES[CASES[name][0]], http=lambda url, **kw: (503, ""))
        with self.assertRaises(FeedError):
            posting_text(ENTRIES["Airbnb"], {"id": "1", "url": "u"}, http=lambda url, **kw: (404, ""))

    def test_malformed_response_is_feed_error(self):
        for body in ("not json", "{}", "[]"):
            with self.subTest(body):
                with self.assertRaises(FeedError):
                    list_jobs(ENTRIES["Airbnb"], http=serving(body))

    def test_eightfold_spacing(self):
        entry = dict(ENTRIES["Microsoft"], queries=["a", "b"])
        calls = []

        def http(url, method="GET", body=None, headers=None):
            calls.append(url)
            positions = [{"id": len(calls) * 100 + i, "name": "n", "positionUrl": "/careers/job/1"} for i in range(10)]
            return 200, json.dumps({"data": {"positions": positions, "count": 20}})

        clock = FakeClock()
        with clock.patched():
            jobs = list_jobs(entry, http=clock.recording(http))
        self.assertEqual(len(calls), 4)  # two pages per query
        self.assertEqual(len(jobs), 40)
        self.assertEqual(clock.sleeps, [4, 4, 4])  # between requests, not before the first
        self.assertGreaterEqual(min(b - a for a, b in zip(clock.requests, clock.requests[1:])), 4)

    def test_eightfold_spacing_holds_across_calls(self):
        # list_jobs then posting_text, then posting_text again, on the same host.
        entry = dict(ENTRIES["Microsoft"], queries=["a"])
        clock = FakeClock()

        def http(url, method="GET", body=None, headers=None):
            return 200, fixture("eightfold", "detail" if "position_details" in url else "list")

        with clock.patched():
            h = clock.recording(http)
            jobs = list_jobs(entry, http=h)
            posting_text(entry, jobs[0], http=h)
            posting_text(entry, jobs[1], http=h)
        self.assertEqual(len(clock.requests), 3)
        self.assertGreaterEqual(min(b - a for a, b in zip(clock.requests, clock.requests[1:])), 4)

    def test_eightfold_spacing_is_per_host(self):
        clock = FakeClock()
        with clock.patched():
            h = clock.recording(lambda url, **kw: (200, fixture("eightfold", "list")))
            list_jobs(dict(ENTRIES["Microsoft"], queries=["a"]), http=h)
            list_jobs(dict(ENTRIES["Qualcomm"], queries=["a"]), http=h)
        self.assertEqual(clock.sleeps, [])  # different hosts do not wait on each other

    def test_eightfold_backs_off_on_429(self):
        statuses = iter([429, 200])

        def http(url, method="GET", body=None, headers=None):
            s = next(statuses)
            return s, "" if s == 429 else fixture("eightfold", "list")

        clock = FakeClock()
        with clock.patched():
            jobs = list_jobs(dict(ENTRIES["Qualcomm"], queries=["x"]), http=clock.recording(http))
        self.assertEqual(len(jobs), 3)
        self.assertGreaterEqual(clock.requests[1] - clock.requests[0], 4)

    def test_fetch_uses_browser_user_agent(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        resp.read.return_value = b"ok"
        with mock.patch("ats.urlopen", return_value=resp) as urlopen:
            self.assertEqual(ats.fetch("https://x.example/a", method="POST", body='{"a":1}',
                                       headers={"X-Test": "1"}), (200, "ok"))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b'{"a":1}')
        self.assertTrue(req.get_header("User-agent").startswith("Mozilla/5.0"))
        self.assertEqual(req.get_header("X-test"), "1")


_make_tests()

if __name__ == "__main__":
    unittest.main()
