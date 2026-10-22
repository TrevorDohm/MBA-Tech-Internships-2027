import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (package init puts scripts/ on sys.path)
import validate_listings as v
from ats import FeedError

HOSTS = {"jobs.example.com"}


def row(**kw):
    r = {"company": "Acme", "role": "MBA Intern", "category": "Strategy & BizOps",
         "locations": ["NYC"], "url": "https://jobs.example.com/1", "date_posted": "2026-10-01",
         "status": "open", "pay": None, "sponsorship": "unknown", "notes": ""}
    r.update(kw)
    return r


def check(new, text, old=None, hosts=HOSTS):
    return v.check_row(old, new, text, hosts)


class CheckRowTests(unittest.TestCase):
    def test_pay_figures_must_appear(self):
        self.assertEqual(check(row(pay="$63–$65/hr"), "Pay: $63 — $65 USD"), [])
        errs = check(row(pay="$63–$65/hr"), "Pay: $70")
        self.assertTrue(errs and "pay" in errs[0])

    def test_pay_month(self):
        self.assertEqual(check(row(pay="$9,400–$11,100/mo"), "$9,400-$11,100 per month"), [])

    def test_pay_k_suffix_currency_and_words(self):
        self.assertEqual(check(row(pay="$169K"), "salary $169,000 per year"), [])
        self.assertEqual(check(row(pay="€72,000/yr"), "EUR 72000"), [])
        self.assertEqual(check(row(pay="$63/hr annualized"), "$63.00 hourly"), [])
        self.assertEqual(check(row(pay="$1–$2 (posting also lists $29.5–$56.5)"), "$1 - $2; $29.5 - $56.5"), [])
        self.assertTrue(check(row(pay="$1–$2 (posting also lists $29.5–$56.5)"), "$1 - $2; $29.5"))
        self.assertTrue(check(row(pay="$63"), "$163 or $630"))

    def test_deadline_formats(self):
        self.assertEqual(check(row(deadline="2026-11-20"), "before 20th November 2026"), [])
        self.assertEqual(check(row(deadline="2026-11-01"), "Sunday, November 1st"), [])
        self.assertEqual(check(row(deadline="2026-11-06"), "by November 6, 2026"), [])
        self.assertEqual(check(row(deadline="2026-11-06"), "Posting end date: 2026-11-06"), [])
        self.assertTrue(check(row(deadline="2026-11-20"), "apply soon, rolling basis"))
        self.assertTrue(check(row(deadline="2026-11-01"), "November 11, 2026"))
        self.assertTrue(check(row(deadline="2026-11-06"), "by November 6, 2025"))

    def test_sponsorship_needs_wording(self):
        self.assertEqual(check(row(sponsorship="no"), "will not require work visa sponsorship"), [])
        self.assertEqual(check(row(sponsorship="no"), "not eligible for immigration sponsorship"), [])
        self.assertTrue(check(row(sponsorship="no"), "a great internship"))

    def test_sponsorship_polarity(self):
        self.assertTrue(check(row(sponsorship="yes"), "We will not sponsor visas"))
        self.assertEqual(check(row(sponsorship="yes"), "We sponsor H-1B visas for interns"), [])
        self.assertEqual(check(row(sponsorship="no"), "will not require work visa sponsorship"), [])
        self.assertEqual(check(row(sponsorship="no"), "not be eligible for immigration sponsorship"), [])
        self.assertEqual(check(row(sponsorship="no"), "authorized to work without the need for current or future company sponsorship"), [])
        self.assertTrue(check(row(sponsorship="no"), "We offer visa sponsorship"))
        for text in ("Visa sponsorship is not available for this role",
                     "This position is ineligible for visa sponsorship",
                     "We do not offer any type of employment-based immigration sponsorship for this role"):
            self.assertEqual(check(row(sponsorship="no"), text), [], text)
            self.assertTrue(check(row(sponsorship="yes"), text), text)
        for text in ("We don't offer visa sponsorship", "We don’t sponsor", "Sponsorship cannot be provided for this role",
                     "Sponsorship is unable to be offered", "Sponsorship: No", "We sponsor no visas",
                     "Applicants who now or in the future require sponsorship cannot be considered",
                     "Must be able to work without sponsorship"):
            self.assertTrue(check(row(sponsorship="yes"), text), text)
            self.assertEqual(check(row(sponsorship="no"), text), [], text)
        for claim in ("yes", "no"):
            self.assertTrue(check(row(sponsorship=claim), "legally authorized to work in the US"))

    def test_pay_false_pass_probes(self):
        self.assertTrue(check(row(pay="$40/hr"), "40 hours per week"))
        self.assertTrue(check(row(pay="$63–$65/hr"), "Req ID 63; suite 65"))
        self.assertTrue(check(row(pay="$63/hr"), "salary $63K"))
        self.assertTrue(check(row(pay="£50/hr"), "$50 USD"))
        self.assertEqual(check(row(pay="$37–$50/hr"), "$37.00 - $50.00"), [])
        self.assertEqual(check(row(pay="$47–$57/hr"), "$47 - 57 /hr"), [])
        self.assertEqual(check(row(pay="$38–$94/hr"), "38 USD - 94 USD"), [])
        self.assertEqual(check(row(pay="$55/hr"), "rate is $55- $55."), [])

    def test_pay_without_figures_errors(self):
        errs = check(row(pay="competitive"), "anything")
        self.assertTrue(errs and "no figures" in errs[0])

    def test_numeric_dates_are_us_order(self):
        self.assertTrue(check(row(deadline="2026-01-11"), "apply by 11/1/2026"))
        self.assertEqual(check(row(deadline="2026-11-01"), "apply by 11/1/2026"), [])
        self.assertEqual(check(row(deadline="2026-11-20"), "apply by 20/11/2026"), [])

    def test_utc_end_date_is_local_evening(self):
        self.assertEqual(check(row(deadline="2026-10-31"), "Posting end date: 2026-11-01T03:59:00+00:00"), [])
        self.assertTrue(check(row(deadline="2026-10-31"), "Posting end date: 2026-11-01T11:59:00+00:00"))
        self.assertTrue(check(row(deadline="2026-10-31"), "Posted 2026-11-01T08:00Z"))

    def test_malformed_deadline_is_an_error(self):
        errs = check(row(deadline="Nov 1"), "anything")
        self.assertTrue(errs and "YYYY-MM-DD" in errs[0])

    def test_bad_host_skips_claim_checks(self):
        errs = check(row(url="https://evil.example.org/1", pay="$63"), None)
        self.assertEqual(len(errs), 1)

    def test_url_host_must_be_known(self):
        errs = check(row(url="https://evil.example.org/1"), None)
        self.assertTrue(errs and "host" in errs[0])
        self.assertEqual(check(row(url="https://jobs.example.com/x?gh_jid=5"), None), [])
        self.assertEqual(v.check_row(None, row(url="https://anywhere.com/1"), None, None), [])

    def test_notes_no_links(self):
        self.assertTrue(check(row(notes="see http://x.com"), None))
        self.assertTrue(check(row(notes="see [x](y)"), None))
        self.assertTrue(check(row(notes="a" * 301), None))
        self.assertEqual(check(row(notes="a" * 300), None), [])

    def test_closure_skips_fetch(self):
        old = row(pay="$63/hr", sponsorship="no", deadline="2026-11-01")
        self.assertEqual(v.claim_fields(old, dict(old, status="closed")), [])
        self.assertEqual(check(dict(old, status="closed"), None, old=old), [])

    def test_unfetchable_ok_without_claims(self):
        self.assertEqual(check(row(), None), [])
        errs = check(row(pay="$63/hr"), None)
        self.assertTrue(errs and "verify by hand" in errs[0])

    def test_only_changed_fields_checked(self):
        old = row(pay="$63/hr")
        self.assertEqual(check(dict(old, notes="new note"), "no pay here", old=old), [])
        self.assertEqual(check(dict(old, notes="new note"), None, old=old), [])
        self.assertTrue(check(dict(old, pay="$70/hr"), "no pay here", old=old))

    def test_url_change_rechecks_everything(self):
        old = row(pay="$63/hr")
        new = dict(old, url="https://jobs.example.com/2")
        self.assertIn("pay", v.claim_fields(old, new))


class ChangedRowsTests(unittest.TestCase):
    def test_added_changed_and_unchanged(self):
        a, b = row(), row(role="Other", url="https://jobs.example.com/2")
        head_b = dict(b, pay="$1")
        c = row(role="New", url="https://jobs.example.com/3")
        out = v.changed_rows([a, b], [a, head_b, c])
        self.assertEqual(out, [(b, head_b), (None, c)])

    def test_match_by_company_role_when_url_changes(self):
        a = row()
        a2 = dict(a, url="https://jobs.example.com/9")
        self.assertEqual(v.changed_rows([a], [a2]), [(a, a2)])

    def test_no_changed_rows(self):
        a = row()
        self.assertEqual(v.changed_rows([a], [a]), [])


class MainTests(unittest.TestCase):
    def run_main(self, base, head, fetch=None, labels=""):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "listings.json"
            p.write_text(json.dumps(head))
            buf = io.StringIO()
            with mock.patch.object(v, "load_base", return_value=base), \
                    mock.patch.object(v, "load_watchlist", return_value=[
                        {"company": "Acme", "hosts": ["jobs.example.com"], "ats": "greenhouse"}]), \
                    mock.patch.object(v, "fetch_text", side_effect=fetch or (lambda *a, **k: None)), \
                    redirect_stdout(buf):
                code = v.main(["--base", "x", "--listings", str(p), "--labels", labels])
        return code, buf.getvalue()

    def test_no_changed_rows(self):
        code, out = self.run_main([row()], [row()])
        self.assertEqual(code, 0)
        self.assertIn("no rows changed", out.lower())

    def test_override_label_downgrades_unfetchable_only(self):
        head = [row(pay="$63/hr")]
        code, out = self.run_main([], head, labels="foo, verified-by-hand")
        self.assertEqual(code, 0)
        self.assertIn("Warnings", out)
        self.assertIn("verify by hand", out)
        code, _ = self.run_main([], head, labels="foo")
        self.assertEqual(code, 1)
        code, _ = self.run_main([], head, fetch=lambda *a, **k: "$70", labels="verified-by-hand")
        self.assertEqual(code, 1)

    def test_bad_host_is_not_fetched(self):
        calls = []
        code, _ = self.run_main([], [row(url="https://evil.example.org/1", pay="$63")],
                                fetch=lambda *a, **k: calls.append(a))
        self.assertEqual((code, calls), (1, []))

    def test_failure_exits_1(self):
        code, out = self.run_main([], [row(pay="$63/hr")], fetch=lambda *a, **k: "pay $70")
        self.assertEqual(code, 1)
        self.assertIn("pay", out)

    def test_pass_exits_0(self):
        code, _ = self.run_main([], [row(pay="$63/hr")], fetch=lambda *a, **k: "pay $63")
        self.assertEqual(code, 0)


class FetchTextTests(unittest.TestCase):
    ENTRIES = [{"company": "Acme", "ats": "greenhouse", "board": "acme", "hosts": ["h"]}]

    def test_workday_company_with_public_radancy_url(self):
        entries = [{"company": "Cap", "ats": "workday", "host": "cap.wd12.myworkdayjobs.com",
                    "tenant": "t", "site": "s", "hosts": ["cap.wd12.myworkdayjobs.com", "www.cap.com"]}]
        url = "https://www.cap.com/job/mclean/x/1732/101906667360"
        with mock.patch.object(v, "posting_text", return_value="T") as pt:
            self.assertEqual(v.fetch_text("Cap", url, entries), "T")
            e = pt.call_args[0][0]
            self.assertEqual((e["ats"], e["host"]), ("radancy", "www.cap.com"))
            self.assertEqual(pt.call_args[0][1]["id"], "101906667360")

    def test_paths(self):
        url = "https://h/x?gh_jid=123"
        with mock.patch.object(v, "posting_text", return_value="T") as pt:
            self.assertEqual(v.fetch_text("Acme", url, self.ENTRIES), "T")
            self.assertEqual(pt.call_args[0][1], {"id": "123", "url": url})
        self.assertIsNone(v.fetch_text("Nobody", url, self.ENTRIES))
        self.assertIsNone(v.fetch_text("Acme", "https://h/landing", self.ENTRIES))
        with mock.patch.object(v, "posting_text", side_effect=FeedError("x")):
            self.assertIsNone(v.fetch_text("Acme", url, self.ENTRIES))
        with mock.patch.object(v, "posting_text", side_effect=OSError("x")):
            self.assertIsNone(v.fetch_text("Acme", url, self.ENTRIES))


if __name__ == "__main__":
    unittest.main()
