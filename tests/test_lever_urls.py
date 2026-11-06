import unittest
from collector.sources import webscout


class LeverURLs(unittest.TestCase):
    def test_encoded_names_remain_complete_for_careers_and_api_urls(self):
        for root in ('https://jobs.lever.co/', 'https://jobs.eu.lever.co/',
                     'https://api.lever.co/v0/postings/'):
            with self.subTest(root=root):
                self.assertEqual(webscout.fingerprint(root + 'Loop%20AI/123?source=LinkedIn'),
                                 ('lever', 'Loop%20AI'))
                self.assertEqual(webscout.fingerprint(root + 'willowhealth.com'),
                                 ('lever', 'willowhealth.com'))
                self.assertEqual(webscout.fingerprint(root + 'plain-company'),
                                 ('lever', 'plain-company'))
