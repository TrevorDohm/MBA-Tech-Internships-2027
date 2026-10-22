import json
import unittest
from unittest import mock

import tests  # noqa: F401  (package init puts scripts/ on sys.path)
import ats
import discover
from ats import FeedError, list_jobs
from discover import discover as run_discover
from discover import is_candidate, job_id, title_stem
from tests.test_ats import ENTRIES, fixture, serving
from watchlist import ATS_TYPES

W = {"company": "Acme", "ats": "greenhouse", "board": "acme", "queries": ["intern"], "hosts": ["x.com"]}
V = {"company": "Beta", "ats": "greenhouse", "board": "beta", "queries": ["intern"], "hosts": ["y.com"]}


def job(id_, title, location="", url=None):
    return {"id": id_, "title": title, "url": url or f"https://x.com/jobs/{id_}", "location": location}


def row(url, role="Some Role", company="Acme", status="open"):
    return {"company": company, "role": role, "url": url, "status": status}


def ign(url, job_id=None):
    return {"company": "Acme", "url": url, "job_id": job_id, "reason": "r", "date": "2026-10-01"}


MBA = "MBA graduate internship, summer 2027."


class Feeds:
    """Patches discover's adapters: feeds maps company -> [Job] or an exception; texts maps id -> text."""

    def __init__(self, feeds, texts=None):
        self.feeds, self.texts, self.fetched = feeds, texts or {}, []

    def list_jobs(self, entry, http=None):
        r = self.feeds[entry["company"]]
        if isinstance(r, Exception):
            raise r
        return r

    def posting_text(self, entry, j, http=None):
        self.fetched.append(j["id"])
        return self.texts.get(j["id"], MBA)

    def run(self, entries, listings=(), ignored=()):
        with mock.patch.object(discover, "list_jobs", self.list_jobs), \
                mock.patch.object(discover, "posting_text", self.posting_text):
            return run_discover(entries, list(listings), list(ignored))


class IsCandidateTests(unittest.TestCase):
    def test_is_candidate_word_boundary(self):
        self.assertFalse(is_candidate("Marketing Intern, Mumbai", "Join our summer 2027 team in Mumbai."))

    def test_is_candidate_requires_internship(self):
        self.assertFalse(is_candidate("Sr. Analyst", "MBA preferred. Summer 2027 start."))

    def test_is_candidate_accepts_graduate_internship(self):
        self.assertTrue(is_candidate("NIKE Digital Growth Graduate Internship", "Current MBA students. Summer 2027."))

    def test_is_candidate_needs_year_or_summer(self):
        self.assertFalse(is_candidate("Strategy Intern", "MBA candidates welcome."))

    def test_is_candidate_excludes_phd_unless_mba(self):
        self.assertFalse(is_candidate("PhD Research Intern", "Graduate students. Summer 2027."))
        self.assertTrue(is_candidate("PhD Intern", "MBA or PhD. Summer 2027."))


class TitleStemTests(unittest.TestCase):
    def test_title_stem(self):
        self.assertEqual(title_stem("Summer 2027: Strategy Intern (MBA) (Seattle, WA)"), "strategy intern")
        self.assertEqual(title_stem("Strategy Intern - MBA"), "strategy intern")
        self.assertEqual(title_stem("2027 Product Manager, Intern!"), "product manager intern")


class JobIdTests(unittest.TestCase):
    def test_job_id_examples(self):
        for url, want in [
            ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
             "Product-Manager-MBA-Intern--Enterprise-AI---Summer-2027_JR2026707", "JR2026707"),
            ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/New-York---New-York/"
             "Summer-2027-Intern---MBA-Strategic-Selling-Consultant_JR362509-1", "JR362509-1"),
            ("https://job-boards.greenhouse.io/gofundme/jobs/8304644", "8304644"),
            ("https://jobs.ashbyhq.com/teamworks/ba8123e8-22f2-4898-a316-f479c5ac015b",
             "ba8123e8-22f2-4898-a316-f479c5ac015b"),
            ("https://www.amazon.jobs/en/jobs/10491603/2027-mba-leadership-development-program-mldp-intern",
             "10491603"),
            ("https://careers.withwaymo.com/jobs/2027-summer-intern-mba-x-san-francisco?gh_jid=8165014", "8165014"),
            ("https://careers.ibm.com/careers/JobDetail?jobId=130031", "130031"),
            ("https://careers.airbnb.com/positions/8285171/", "8285171"),
            ("https://c3.ai/job-description/8860563002", "8860563002"),
            ("https://www.google.com/about/careers/applications/jobs/results/99770582270124742-x-mba-intern-2027",
             "99770582270124742"),
            ("https://jobs.apple.com/en-us/details/200676091/mba-internships", "200676091"),
            ("https://lifeattiktok.com/search/7676283436713593093", "7676283436713593093"),
            ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210766078", "210766078"),
            ("https://jobs.intuit.com/job/mountain-view/summer-2027-mba-x-intern/27595/101313094448", "101313094448"),
            ("https://jobs.ea.com/en_US/careers/JobDetail/Product-Manager-Intern-MBA-Level/216272", "216272"),
        ]:
            self.assertEqual(job_id(url), want, url)

    def test_job_id_none_for_landing_page(self):
        for url in ["https://careers.snowflake.com/us/en/university", "https://careers.chime.com/jobs/",
                    "https://paypal.wd1.myworkdayjobs.com/jobs", "https://careers.rivian.com/",
                    "https://job-boards.greenhouse.io/gusto", "https://www.metacareers.com/careerprograms/students",
                    "https://jobs.uber.com/en/teams/emerging-talent/", ""]:
            self.assertIsNone(job_id(url), url)

    def test_job_id_on_every_listed_row(self):
        with open(discover.LISTINGS, encoding="utf-8") as f:
            rows = json.load(f)
        found = [job_id(r["url"]) for r in rows]
        self.assertTrue(sum(1 for j in found if j) > len(rows) // 2)
        for r, j in zip(rows, found):
            self.assertTrue(j is None or len(j) >= 5, (r["url"], j))


class JobIdAdapterInvariant(unittest.TestCase):
    """job_id(url) must return the id the adapter assigns the same posting."""


def _make_invariant(name):
    def test(self):
        entry = next(e for e in ENTRIES.values() if e["ats"] == name)
        with mock.patch.multiple("ats.time", sleep=lambda s: None):
            ats._EF_LAST.clear()
            jobs = list_jobs(entry, http=serving(fixture(name, "list")))
        self.assertTrue(jobs)
        for j in jobs:
            self.assertEqual(job_id(j["url"]), j["id"], j["url"])
    return test


for _name in ATS_TYPES:
    setattr(JobIdAdapterInvariant, f"test_job_id_equals_adapter_id_{_name}", _make_invariant(_name))


class DiscoverTests(unittest.TestCase):
    def test_dedupes_listed_and_ignored(self):
        f = Feeds({"Acme": [job("1111111", "Strategy Intern"), job("2222222", "Finance Intern"),
                            job("3333333", "Product Intern")]})
        r = f.run([W], listings=[row("https://x.com/jobs/1111111")], ignored=[ign("https://x.com/jobs/2222222")])
        self.assertEqual([c["job_id"] for c in r["candidates"]], ["3333333"])
        self.assertEqual(r["checked"], 3)

    def test_dedupes_ignored_by_job_id(self):
        f = Feeds({"Acme": [job("2222222", "Finance Intern", url="https://x.com/other?x=1")]})
        r = f.run([W], ignored=[ign("https://x.com/old", "2222222")])
        self.assertEqual(r["candidates"], [])

    def test_detects_repost(self):
        f = Feeds({"Acme": [job("9999999", "Strategy Intern (MBA) - Summer 2027")]})
        r = f.run([W], listings=[row("https://x.com/jobs/1111111", role="Strategy Intern")])
        self.assertEqual(r["candidates"], [])
        self.assertEqual(r["reposts"], [{"company": "Acme", "old_url": "https://x.com/jobs/1111111",
                                         "new_url": "https://x.com/jobs/9999999",
                                         "title": "Strategy Intern (MBA) - Summer 2027"}])

    def test_no_repost_when_old_id_still_in_feed_or_row_not_open(self):
        f = Feeds({"Acme": [job("1111111", "Strategy Intern"), job("9999999", "Strategy Intern")]})
        r = f.run([W], listings=[row("https://x.com/jobs/1111111", role="Strategy Intern")])
        self.assertEqual(r["reposts"], [])
        self.assertEqual(len(r["candidates"]), 1)
        f = Feeds({"Acme": [job("9999999", "Strategy Intern")]})
        r = f.run([W], listings=[row("https://x.com/jobs/1111111", role="Strategy Intern", status="closed")])
        self.assertEqual(r["reposts"], [])
        self.assertEqual(len(r["candidates"]), 1)

    def test_landing_page_row_does_not_crash_and_matches_by_url(self):
        f = Feeds({"Acme": [job("5555555", "Strategy Intern", url="https://x.com/careers/")]})
        r = f.run([W], listings=[row("https://x.com/careers", role="Strategy Intern")])
        self.assertEqual(r["candidates"], [])
        self.assertEqual(r["reposts"], [])

    def test_group_multi_location(self):
        f = Feeds({"Acme": [job("1111111", "Strategy Intern", "Seattle, WA"),
                            job("2222222", "Strategy Intern", "Austin, TX")]})
        r = f.run([W])
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(r["candidates"][0]["locations"], ["Seattle, WA", "Austin, TX"])
        self.assertEqual(set(r["candidates"][0]), {"company", "title", "url", "job_id", "locations", "text"})

    def test_feed_failure_reported(self):
        f = Feeds({"Acme": FeedError("HTTP 500"), "Beta": [job("3333333", "Strategy Intern")]})
        r = f.run([W, V])
        self.assertEqual(r["feed_failures"], [{"company": "Acme", "error": "HTTP 500"}])
        self.assertEqual([c["company"] for c in r["candidates"]], ["Beta"])

    def test_fetches_text_only_for_title_matches(self):
        f = Feeds({"Acme": [job("1111111", "Strategy Intern"), job("2222222", "Software Engineer"),
                            job("3333333", "Listed Intern"), job("4444444", "Ignored Intern")]})
        f.run([W], listings=[row("https://x.com/jobs/3333333")], ignored=[ign("https://x.com/jobs/4444444", "4444444")])
        self.assertEqual(f.fetched, ["1111111"])

    def test_non_candidate_text_is_dropped(self):
        f = Feeds({"Acme": [job("1111111", "Marketing Intern")]}, texts={"1111111": "Undergrads only. Fall."})
        self.assertEqual(f.run([W])["candidates"], [])


class ReplayTests(unittest.TestCase):
    def test_recall(self):
        f = Feeds({"Acme": [job("1111111", "A")]})
        listings = [row("https://x.com/jobs/1111111"), row("https://x.com/jobs/2222222"),
                    row("https://x.com/gone", status="closed"), row("https://z.com/x", company="Exempt")]
        with mock.patch.object(discover, "list_jobs", f.list_jobs):
            hit, total, misses, failures = discover.recall([W], listings)
        self.assertEqual((hit, total), (1, 2))
        self.assertEqual([m["url"] for m in misses], ["https://x.com/jobs/2222222"])
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
