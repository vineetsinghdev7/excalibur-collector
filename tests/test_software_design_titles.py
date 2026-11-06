import unittest

from collector.sources import scorer


class SoftwareDesignTitles(unittest.TestCase):
    def test_explicit_software_design_role(self):
        for title in (
            'Software Design Engineer (TFS Migration and DotNet)',
            'Software Design Engineer (C#, Azure, Power shell)',
            'Senior Software Design Engineer',
        ):
            with self.subTest(title=title):
                self.assertTrue(scorer.is_software_title(title))

    def test_scoring_preserves_the_publishers_title(self):
        title = 'Software Design Engineer (C#, Azure, Power shell)'
        job = {'title': title, 'tech': 'Python', 'yoe': 3, 'location': 'Hyderabad'}
        self.assertGreater(scorer.score(job, {'finder': {'skills': ['Python']}})[0], 0)
        self.assertEqual(job['title'], title)

    def test_existing_physical_and_customer_exclusions_still_win(self):
        for title in (
            'Power Systems Engineer',
            'Power Electronics Engineer',
            'Software Design Engineer - Power Electronics',
            'Software Design Engineer - Power Electronics (Power shell)',
            'Electrical Software Design Engineer',
            'Software Design Engineer - Hardware',
            'Software Design Engineer / Sales Engineer',
            'Software Design Engineer / Technical Support Engineer',
            'Manager, Software Design Engineer',
            'Mechanical Design Engineer',
            'Design Engineer',
        ):
            with self.subTest(title=title):
                self.assertFalse(scorer.is_software_title(title))
