import unittest
from collector.sources.sites import _location_hint, _location


class NativeLocationTests(unittest.TestCase):
    def test_applicant_country_array_is_readable(self):
        job = {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {'@type': 'Country', 'name': ['United States', 'India']}}
        self.assertEqual(_location(job), 'Remote (United States, India)')

    def test_job_link_actions_are_not_locations(self):
        for action in ('View role →', 'View job', 'View details »', 'Apply now'):
            self.assertEqual(_location_hint('PHP Developer · Kochi / Bangalore '+action, ''), 'Kochi / Bangalore')

    def test_remote_and_unfamiliar_locations_are_preserved(self):
        self.assertEqual(_location_hint('Developer · Remote / India View role →', ''), 'Remote / India')
        self.assertEqual(_location_hint('Developer · Remote - Canada View job', ''), 'Remote - Canada')

    def test_company_geography_does_not_override_link_location(self):
        self.assertEqual(_location_hint('Developer · Remote - US only View role →', 'We have offices in Mumbai.'), 'Remote - US only')
