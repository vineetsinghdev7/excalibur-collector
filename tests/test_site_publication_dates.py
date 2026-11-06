import unittest
from datetime import datetime, timezone
from collector.sources import sites, posting_dates

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

def posting(raw):
    job = sites._job_from_posting(
        {"title": "Software Engineer", "datePosted": raw},
        "https://example.com/jobs/1", "Example", "example.com/jobs")
    job["posted_checked_at"] = NOW.isoformat()
    return job, posting_dates.publication(job, NOW)

class StructuredPublicationDates(unittest.TestCase):
    def test_explicit_utc_preserves_the_publisher_instant(self):
        for source, iso in [
            ("2026-08-27 07:17:31 UTC", "2026-08-27T07:17:31Z"),
            ("2026-08-27 07:17:31.125 UTC", "2026-08-27T07:17:31.125Z"),
        ]:
            job, parsed = posting(source)
            self.assertEqual(job["posted_at"], iso)
            self.assertEqual(parsed["precision"], "instant")
            self.assertEqual(parsed["ts"], datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
            self.assertEqual(parsed["field"], "JobPosting.datePosted")

    def test_missing_timezone_keeps_day_precision(self):
        for source in ("2026-08-27", "2026-08-27 07:17:31"):
            job, parsed = posting(source)
            self.assertEqual(job["posted_at"], source)
            self.assertEqual(parsed["precision"], "day")
            self.assertIsNone(posting_dates.instant(job["posted_at"]))

    def test_ambiguous_invalid_and_future_dates_stay_unknown(self):
        for source in ("2026-08-27 07:17:31 IST", "2026-08-27 UTC",
                       "2026-02-30 07:17:31 UTC", "2026-08-27 07:17:31 UTC extra",
                       "2027-08-27 07:17:31 UTC"):
            self.assertIsNone(posting(source)[1], source)

    def test_existing_offset_keeps_the_publishers_calendar_date(self):
        source = "2026-08-27T23:30:00-05:00"
        job, parsed = posting(source)
        self.assertEqual(job["posted_at"], source)
        self.assertEqual(parsed["date"], "2026-08-27")
        self.assertEqual(parsed["ts"], datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc).timestamp())
