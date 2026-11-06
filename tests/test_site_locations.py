import unittest
from collector.sources.sites import _location_hint, _location
from collector.sources.eligibility import evidence


class NativeLocationTests(unittest.TestCase):
    def test_explicit_country_code_allows_unfamiliar_indian_locality(self):
        for country in ('IN', {'name': 'IN'}, {'@type': 'Country', 'name': 'IN'}):
            with self.subTest(country=country):
                posting = {'jobLocation': {'address': {
                    'addressLocality': 'Sahibzada Ajit Singh Nagar',
                    'addressRegion': 'Punjab', 'addressCountry': country}}}
                location = _location(posting)
                self.assertEqual(location, 'Sahibzada Ajit Singh Nagar, Punjab, India')
                self.assertEqual(evidence({'title': 'Software Engineer', 'location': location})['basis'], 'india')

    def test_indiana_region_is_not_an_indian_country(self):
        posting = {'jobLocation': {'address': {
            'addressLocality': 'Indianapolis', 'addressRegion': 'IN', 'addressCountry': 'US'}}}
        location = _location(posting)
        self.assertEqual(location, 'Indianapolis, IN, US')
        self.assertIsNone(evidence({'title': 'Software Engineer', 'location': location}))

    def test_remote_country_code_is_normalized_only_for_country_objects(self):
        posting = {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {
            '@type': 'Country', 'name': ['United States', 'IN']}}
        self.assertEqual(_location(posting), 'Remote (United States, India)')
        for requirement in ('IN', {'@type': 'AdministrativeArea', 'name': 'IN'}):
            posting['applicantLocationRequirements'] = requirement
            self.assertEqual(_location(posting), 'Remote (IN)')
        self.assertEqual(_location({'jobLocation': {'address': 'IN'}}), 'IN')

    def test_normalized_country_does_not_override_explicit_exclusions(self):
        posting = {'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {
            '@type': 'Country', 'name': 'IN'}}
        location = _location(posting)
        self.assertEqual(location, 'Remote (India)')
        for description in ('This role is not open to India.', 'US only.'):
            with self.subTest(description=description):
                self.assertIsNone(evidence({'title': 'Software Engineer', 'location': location,
                                            'description': description}))

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
