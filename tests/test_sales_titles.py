import unittest
from collector.sources import scorer


class SalesTitleTests(unittest.TestCase):
    def test_sales_sde_is_not_software_development(self):
        for title in ('Sales Development Executive(SDE)', 'Sales Development Representative - SDE'):
            self.assertFalse(scorer.is_software_title(title), title)
        for title in ('Software Development Engineer', 'SDE-1', 'Salesforce Developer'):
            self.assertTrue(scorer.is_software_title(title), title)
