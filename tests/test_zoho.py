import html
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from collector.sources import adapters


class ZohoVacancies(unittest.TestCase):
    def test_html_request_works_through_the_real_transport_and_cache(self):
        from collector.sources import net
        url = "https://employer.zohorecruit.in/jobs/Careers"
        job = dict(id="123", Posting_Title="Software Engineer", City="Pune", Country="India", Publish=True)
        raw = ('<input id="jobs" value="' + html.escape(json.dumps([job]), quote=True) + '">').encode()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(net, "CACHE", Path(folder) / "http.sqlite"), \
                patch.object(net, "_init", False), patch.object(net, "_last", {}), \
                patch.object(net, "_blocked", {}), patch.object(net, "DEADLINE", float("inf")), \
                patch.object(net.urllib.request, "urlopen") as send:
            response = send.return_value.__enter__.return_value
            response.status = 200
            response.geturl.return_value = url
            response.read.return_value = raw
            response.headers = {}
            jobs = adapters.zoho("employer.zohorecruit.in", "Employer")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["location"], "Pune, India")
            self.assertEqual(send.call_args.args[0].get_header("Accept"), "text/html, */*")

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
