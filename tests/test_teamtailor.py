import unittest
from unittest.mock import patch
from collector.sources import adapters, indiafeeds


class TeamtailorLocations(unittest.TestCase):
    def rows(self, postings):
        items = [dict(url=f'https://acme.teamtailor.com/jobs/{i}', title='Software Engineer',
                      content_html='<p>Our offices include India and London.</p>',
                      date_published='2026-10-15T17:28:11+05:30', _jobposting=p)
                 for i, p in enumerate(postings)]
        with patch.object(adapters, '_get_json', return_value={'title': 'Acme', 'items': items}):
            return adapters.teamtailor('acme')

    def test_structured_india_locations_and_feed_date_are_preserved(self):
        rows = self.rows([
            {'jobLocation': [{'address': {'addressLocality': 'Bengaluru',
              'addressRegion': 'Bengaluru', 'addressCountry': 'IN'}}]},
            {'jobLocation': {'address': {'addressCountry': {'name': 'IN'}}}},
            {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {'name': 'India'}},
        ])
        self.assertEqual([r['location'] for r in rows], ['Bengaluru, India', 'India', 'Remote (India)'])
        self.assertTrue(all(indiafeeds.india_eligible(r['location']) for r in rows))
        self.assertTrue(all(r['posted_at'] == '2026-10-15T17:28:11+05:30' and
                            r['posted_field'] == 'teamtailor.date_published' for r in rows))

    def test_prose_and_remote_without_eligibility_never_imply_india(self):
        rows = self.rows([
            {}, None, 'malformed',
            {'jobLocationType': 'TELECOMMUTE'},
            {'jobLocation': {'address': {'addressLocality': 'Indianapolis',
              'addressRegion': 'IN', 'addressCountry': 'US'}}},
            {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {'name': 'US'}},
            {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {'name': 'US'},
             'jobLocation': {'address': {'addressCountry': 'IN'}}},
        ])
        self.assertFalse(any(indiafeeds.india_eligible(r['location']) for r in rows))
        self.assertEqual(rows[0]['location'], '')
