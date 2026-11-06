"""Posting-level eligibility: positive routes, negative constraints and uncertainty."""
import unittest
from collector.sources import eligibility as e

class EligibilityTests(unittest.TestCase):
    def check(self, location, description="", title="Software Engineer"):
        return e.evidence(dict(location=location, description=description, title=title))

    def test_india_locations(self):
        for location in ("India", "Bengaluru, India", "Remote, India", "Pune", "NCR"):
            self.assertEqual(self.check(location)["basis"], "india")

    def test_unknown_remote_and_company_geography_do_not_qualify(self):
        for location in ("Remote", "APAC", "EMEA", "London", "Indiana", "Remote - US only"):
            self.assertIsNone(self.check(location, "Our company operates worldwide and has offices in India."))

    def test_explicit_worldwide_remote(self):
        for location in ("Worldwide", "Remote, Worldwide", "Anywhere", "Home based - Worldwide"):
            self.assertEqual(self.check(location)["basis"], "worldwide_remote")
        self.assertEqual(self.check("Remote", "You can work from anywhere in the world.")["basis"], "worldwide_remote")

    def test_regional_location_wins_over_remote_benefit(self):
        self.assertIsNone(self.check("Remote - United States", "Work from anywhere in the world."))

    def test_india_exclusion_wins(self):
        self.assertIsNone(self.check("Worldwide", "We hire worldwide excluding India."))
        self.assertIsNone(self.check("Worldwide except India"))
        self.assertIsNone(self.check("Worldwide", "We do not hire candidates from India."))
        self.assertIsNone(self.check("Worldwide", "India is not supported."))
        self.assertIsNone(self.check("Worldwide", "You must be based in Europe. We also have offices in India."))
        self.assertIsNone(self.check("India", "You must be a US citizen and based in India."))
        self.assertIsNone(self.check("India", "You must be based in India; US only."))
        self.assertIsNone(self.check("India", "You must be based in India and hold a valid work permit in the UK."))
        self.assertIsNone(self.check("Worldwide", "You must already have the right to work in the UK, while based in India."))

    def test_explicit_visa_offers(self):
        for description in ("We offer visa sponsorship.", "Visa sponsorship is available.",
                            "We can sponsor your work visa.", "We provide visa support.",
                            "Work permit assistance is provided."):
            self.assertEqual(self.check("Tokyo, Japan", description)["basis"], "visa_sponsorship", description)

    def test_sponsorship_questions_and_ambiguous_mentions_are_unknown(self):
        for description in ("Will you require visa sponsorship?", "We sponsor conferences.",
                            "Visa sponsorship", "Relocation assistance", "We are an international team."):
            self.assertIsNone(self.check("Berlin", description), description)

    def test_negative_visa_wording_wins(self):
        for negative in ("We do not provide visa sponsorship.", "We don't offer visa sponsorship.",
                         "We don’t offer visa sponsorship.", "Sponsorship is not available.",
                         "No visa sponsorship.", "We are unable to sponsor.", "We cannot provide visa assistance."):
            self.assertIsNone(self.check("London", "We offer visa sponsorship. " + negative), negative)

    def test_no_visa_required_for_india_or_worldwide_remote(self):
        for location in ("Bengaluru, India", "Worldwide"):
            self.assertIsNotNone(self.check(location, "No visa sponsorship."))

    def test_existing_work_rights_and_citizenship_win(self):
        for restriction in ("You must already have the right to work in the UK.",
                            "Authorization to work in the United States is required.",
                            "US citizens only.", "You must be a US citizen.",
                            "Candidates must hold a valid work permit.",
                            "You must be based in Europe.", "We only hire residents of Canada.",
                            "This role requires active security clearance."):
            self.assertIsNone(self.check("Worldwide", restriction + " We offer visa sponsorship."), restriction)

    def test_explicit_india_residence_is_compatible(self):
        self.assertIsNotNone(self.check("Remote, India", "You must be based in India."))
        self.assertIsNotNone(self.check("Bengaluru", "You must already have the right to work in India."))

    def test_distinct_html_bullets_do_not_create_false_offers(self):
        self.assertIsNone(self.check("Tokyo", "<li>Customer support</li><li>Visa sponsorship required?</li>"))
        self.assertEqual(self.check("Tokyo", "Japan Relocation Support\nVisa sponsorship\nFlight ticket support")["basis"], "visa_sponsorship")
        self.assertEqual(self.check("Tokyo", "Visa sponsorship and overseas relocation support available for eligible candidates.")["basis"], "visa_sponsorship")

    def test_relocation_option_does_not_require_existing_residence(self):
        self.assertIsNotNone(self.check("London", "We only consider applicants who are currently based in, or willing to relocate to, London or Amsterdam. Visa sponsorship is provided."))
        self.assertEqual(self.check("London", "Relocation support, including visa sponsorship and allowance.")["basis"], "visa_sponsorship")
        self.assertEqual(self.check("London", "Relocation support and visa sponsorship where applicable.")["basis"], "visa_sponsorship")
        self.assertIsNone(self.check("Home based - EMEA"))

    def test_international_relocation_is_separate(self):
        result=self.check("Tokyo", "International relocation assistance.")
        self.assertEqual(result["basis"], "international_relocation")
        self.assertIn("International", result["evidence"])
        self.assertIsNone(self.check("Tokyo", "Relocation assistance."))
        self.assertIsNone(self.check("Tokyo", "No international relocation assistance."))

    def test_combined_visa_and_relocation_offer(self):
        offer = "Visa sponsorship and relocation support will be provided."
        self.assertEqual(self.check("Tokyo, Japan", offer)["basis"], "visa_sponsorship")
        self.assertIsNone(self.check("Tokyo", "Visa sponsorship and relocation support will not be provided."))
        self.assertIsNone(self.check("Tokyo", "Will visa sponsorship and relocation support be provided?"))
        self.assertIsNone(self.check("Tokyo", offer + " Candidates must hold a valid work permit."))
        self.assertIsNone(self.check("Tokyo", offer + " We do not hire candidates from India."))
        self.assertIsNone(self.check("Tokyo", offer + " We cannot provide visa sponsorship for this position."))

    def test_negative_visa_blocks_relocation(self):
        self.assertIsNone(self.check("Berlin", "International relocation support. No visa sponsorship."))

    def test_html_script_and_bad_inputs(self):
        self.assertEqual(self.check("Tokyo", "<p>We <b>offer</b> visa sponsorship.</p>")["basis"], "visa_sponsorship")
        self.assertIsNone(self.check("Tokyo", "<script>We offer visa sponsorship.</script>"))
        self.assertIsNone(e.evidence([]))
        self.assertIsNone(e.evidence({"location": {}}))

if __name__ == "__main__": unittest.main()
