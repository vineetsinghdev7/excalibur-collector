import unittest

from collector.sources import scorer


class BusinessDeveloperTitles(unittest.TestCase):
    def test_business_developer_is_not_a_software_role(self):
        for title in ('Business Developer', 'Senior Business Developer',
                      'International Business Developer – IT Services'):
            with self.subTest(title=title):
                self.assertFalse(scorer.is_software_title(title))

    def test_business_intelligence_developers_remain_software_roles(self):
        for title in ('Business Intelligence Developer', 'Business Applications Developer',
                      'Software Developer, Business Systems'):
            with self.subTest(title=title):
                self.assertTrue(scorer.is_software_title(title))
