import html
import json
import unittest
from unittest.mock import patch
from collector.sources import adapters


class ZohoVacancies(unittest.TestCase):
    def test_unpublished_role_retained_on_career_page_is_excluded(self):
        job = dict(id="123", Posting_Title="Software Engineer", City="Pune", Country="India",
                   Publish=True, Keep_on_Career_Site=True, Date_Opened="2026-11-01")
        raw = ('<input id="jobs" value="' + html.escape(json.dumps([
            job, {**job, "id": "456", "Publish": False},
            {**job, "id": "789", "Publish": False, "Keep_on_Career_Site": False},
        ]), quote=True) + '">').encode()
        with patch.object(adapters.net, "get_bytes", return_value=raw):
            jobs = adapters.zoho("employer.zohorecruit.in", "Employer")
        self.assertEqual(len(jobs), 1)
        self.assertIn("/123/", jobs[0]["url"])
        self.assertEqual(jobs[0]["posted_at"], "")
