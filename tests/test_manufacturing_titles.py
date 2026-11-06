import unittest
from collector.sources import scorer


class ManufacturingTitles(unittest.TestCase):
    def test_machine_programming_does_not_qualify_a_board_as_software(self):
        for title in ('CAM Programmer', 'CAM - Programmer', 'CNC Programmer', 'PLC Programmer',
                      'CMM Programmer', 'Mastercam Programmer', 'Programmer - CNC'):
            with self.subTest(title=title):
                self.assertFalse(scorer.is_software_title(title))
        for title in ('Software Developer', 'Embedded Software Engineer', 'Firmware Engineer',
                      'Software Engineer - CAD/CAM', 'C++ Programmer'):
            with self.subTest(title=title):
                self.assertTrue(scorer.is_software_title(title))
