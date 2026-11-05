import unittest
from unittest.mock import patch
from collector.sources import adapters


class JobviteListings(unittest.TestCase):
    def test_landing_page_cards_tables_and_pagination_preserve_locations(self):
        base = "https://jobs.jobvite.com/acme"
        pages = {
            base + "/jobs": '<form action="/acme/search"></form>',
            base + "/search": '''
                <ul><li><a class="flex" href="/acme/job/one">
                  <div class="jv-job-list-name">Senior <strong>Software</strong> Engineer</div>
                  <div class="ml-auto jv-job-list-location">Bengaluru,<br>Karnataka</div>
                </a></li></ul>
                <div class="jv-job-item card"><div class="card-content">
                  <div class="jv-job-list-name"><a href="/acme/job/two">Data Engineer</a></div>
                  <div class="jv-job-list-location"><div class="jv-meta">2 Locations</div></div>
                </div></div>
                <a href="/acme/search/?p=1" class="jv-pagination-next">Next</a>
                <a href="https://evil.example/acme/search/?p=2" class="jv-pagination-next">Next</a>
                <a href="/other/search/?p=2" class="jv-pagination-next">Next</a>''',
            base + "/search/?p=1": '''<table><tr>
                  <td class="jv-job-list-name"><h3><a href="/acme/job/three">Java Developer</a></h3></td>
                  <td class="jv-job-list-location">Chennai, India</td></tr></table>
                <a href="/acme/search/?p=1" class="jv-pagination-next">Next</a>''',
        }
        with patch.object(adapters, "_page", side_effect=pages.__getitem__) as read:
            jobs = adapters.jobvite("acme", "Acme")
        self.assertEqual(read.call_count, 3)
        self.assertEqual([j["location"] for j in jobs], ["Bengaluru, Karnataka", "2 Locations", "Chennai, India"])
        self.assertEqual(jobs[0]["title"], "Senior Software Engineer")
        self.assertEqual(jobs[2]["url"], base + "/job/three")
        self.assertTrue(all(j["posted_at"] == "" and j.get("posted_field", "") == "" for j in jobs))

    def test_featured_jobs_do_not_hide_full_listing_and_duplicates_merge(self):
        card = '''<a class="jv-featured-job" href="/acme/job/one">
          <div class="jv-featured-job-title">Software Engineer</div>
          <div class="jv-featured-job-location">Pune, India</div></a>'''
        with patch.object(adapters, "_page", side_effect=[
                card + '<a href="/acme/jobs/viewall">View Open Positions</a>',
                card + '''<ul><li><a href="/acme/job/two"><div class="jv-job-list-name">DevOps Engineer</div>
                <div class="jv-job-list-location">India</div></a></li></ul>''']) as read:
            jobs = adapters.jobvite("acme")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(read.call_args.args[0], "https://jobs.jobvite.com/acme/jobs/viewall")
