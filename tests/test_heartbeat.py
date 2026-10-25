import tests  # noqa: F401  (puts scripts/ on sys.path)
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import heartbeat

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def iso(days):
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class HeartbeatTests(unittest.TestCase):
    def test_fresh(self):
        self.assertFalse(heartbeat.is_stale(iso(2), NOW))

    def test_stale(self):
        self.assertTrue(heartbeat.is_stale(iso(4), NOW))

    def test_missing(self):
        self.assertTrue(heartbeat.is_stale(None, NOW))

    def test_parses_github_z_suffix(self):
        self.assertFalse(heartbeat.is_stale("2026-10-22T11:17:58Z", NOW))
        self.assertTrue(heartbeat.is_stale("2026-10-18T11:17:58Z", NOW))

    def test_report_mentions_date_or_never(self):
        self.assertIn("2026-10-18", heartbeat.report("2026-10-18T11:17:58Z", NOW))
        self.assertIn("never", heartbeat.report(None, NOW))
        self.assertIn("claude.ai/code/routines", heartbeat.report(None, NOW))

    def test_stale_report_says_successful_check_in(self):
        # FAILED comments are ignored upstream, so "no comment" would be untrue; say what is counted.
        text = heartbeat.report("2026-10-18T11:17:58Z", NOW)
        self.assertIn("no successful `Automation status` check-in in over 3 days", text)
        self.assertIn("FAILED", text)

    def test_report_ok_path(self):
        self.assertEqual(heartbeat.report("2026-10-21T08:00:00Z", NOW),
                         "## Automation heartbeat\n\nOK. Last check-in 2026-10-21.\n")

    def test_exactly_three_days_is_not_stale(self):
        self.assertFalse(heartbeat.is_stale(iso(3), NOW))
        self.assertTrue(heartbeat.is_stale((NOW - timedelta(days=3, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), NOW))


ROOT = Path(__file__).resolve().parent.parent


class HeartbeatLineTests(unittest.TestCase):
    """The weekly check counts only comments matching the success line AUTOMATION.md tells the task to post."""

    def success_pattern(self):
        yml = (ROOT / ".github" / "workflows" / "check-links.yml").read_text(encoding="utf-8")
        m = re.search(r'test\(\\?"(\^[^"\\]*)\\?"\)', yml)  # the jq sits in a "..." shell string: test(\"^...\")
        self.assertIsNotNone(m, "check-links.yml must filter comments with jq test(\"^...\")")
        return re.compile(m.group(1))

    def test_success_line_counts_and_failure_lines_do_not(self):
        rx = self.success_pattern()
        self.assertTrue(rx.search("2026-10-25 · feeds OK 40/41 · candidates 3 · PR #12"))
        self.assertTrue(rx.search("2026-10-25 · feeds OK 41/41 · candidates 0 · PR none"))
        for line in ("2026-10-25 · FAILED: discover failed (exit 1) · PR none",
                     "2026-10-25 · FAILED: merge conflict · PR #12",
                     "looks fine · feeds OK", "Weekly ping 2026-10-25 · feeds OK"):
            self.assertFalse(rx.search(line), line)

    def test_automation_md_uses_the_two_line_formats(self):
        doc = (ROOT / "AUTOMATION.md").read_text(encoding="utf-8")
        self.assertIn('"<date> · feeds OK n/N · candidates c · PR #x"', doc)
        failures = re.findall(r"`<date> · FAILED: [^`]+`", doc)
        self.assertGreaterEqual(len(failures), 5, failures)  # merge conflict, case B, discover, checks, push
        self.assertNotRegex(doc, r"`<date> · (?!FAILED: |feeds OK)")


if __name__ == "__main__":
    unittest.main()
