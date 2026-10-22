import tests  # noqa: F401  (puts scripts/ on sys.path)
import unittest
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    unittest.main()
