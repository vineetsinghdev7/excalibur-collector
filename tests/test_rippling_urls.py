import unittest
from collector.sources import webscout


class RipplingURLs(unittest.TestCase):
    def test_languages_resolve_to_one_employer_instead_of_locale_boards(self):
        for prefix in ('', 'en-US/', 'fr-FR/', 'pt-PT/', 'es-419/'):
            with self.subTest(prefix=prefix):
                url='https://ats.rippling.com/'+prefix+'acme-inc/jobs/123'
                self.assertEqual(webscout.fingerprint(url),('rippling','acme-inc'))
        self.assertEqual(webscout.fingerprint('https://ats.rippling.com/es-419/jobs'),('', ''))
