import unittest

from collector.sources import eligibility, indiafeeds, regions


class IndianCityAliases(unittest.TestCase):
    def test_publisher_locations_are_recognized_without_rewriting(self):
        for location, city in (("Sector 82, Mohali", "Mohali"), ("Bhilai (C.G)", "Bhilai"),
                               ("Vellore, Tamil Nadu", "Vellore"), ("Calicut", "Kozhikode"),
                               ("Kozhikode, Kerala", "Kozhikode")):
            with self.subTest(location=location):
                self.assertTrue(indiafeeds.india_eligible(location))
                self.assertEqual(regions.india_cities(location), [city])
                self.assertEqual(regions.first(location), "India")
                job = {"title": "Software Engineer", "location": location}
                self.assertEqual(eligibility.evidence(job), {"basis": "india", "evidence": location})
                self.assertEqual(job["location"], location)

    def test_city_names_require_word_boundaries(self):
        for location in ("NotMohali", "Mohalipur", "BhilaiCorp", "Bhilaiman", "VelloreCorp", "Keraland", "CalicutCorp", "Kozhikodeville"):
            with self.subTest(location=location):
                self.assertFalse(regions.is_india(location))
                self.assertEqual(regions.india_cities(location), [])
                self.assertFalse(indiafeeds.india_eligible(location))
                self.assertIsNone(eligibility.evidence({"title": "Software Engineer", "location": location}))

    def test_explicit_restrictions_override_indian_city_evidence(self):
        for location in ("Sector 82, Mohali", "Bhilai (C.G)", "Vellore, Tamil Nadu", "Kerala"):
            for restriction in ("This role is not open to India.", "US-only."):
                with self.subTest(location=location, restriction=restriction):
                    self.assertIsNone(eligibility.evidence({"title": "Software Engineer",
                                                           "location": location,
                                                           "description": restriction}))

    def test_state_location_does_not_invent_a_city_or_accept_ambiguous_punjab(self):
        self.assertEqual(regions.first("Kerala"), "India")
        self.assertTrue(indiafeeds.india_eligible("Kerala"))
        self.assertEqual(regions.india_cities("Kerala"), [])
        self.assertFalse(indiafeeds.india_eligible("Punjab"))
