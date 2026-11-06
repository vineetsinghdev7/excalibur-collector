import unittest
from unittest.mock import patch
from collector.sources import adapters, webscout


def posting(i):
    return dict(id=str(i), text="Software Engineer", hostedUrl=f"https://jobs.lever.co/acme/{i}",
                categories={"location": "Bengaluru, India"}, createdAt=1793876400000)


class LeverPaging(unittest.TestCase):
    def test_pages_deduplicate_and_keep_publication_unknown(self):
        with patch.object(adapters, '_LEVER_PAGE_SIZE', 2), patch.object(adapters, '_get_json',
                side_effect=[[posting(1), posting(2)], [posting(2), posting(3)], []]) as get:
            jobs = adapters.lever('acme')
        self.assertEqual([j['url'].rsplit('/', 1)[-1] for j in jobs], ['1', '2', '3'])
        self.assertTrue(all(j['posted_at'] == '' for j in jobs))
        self.assertEqual([c.args[0].split('skip=')[1] for c in get.call_args_list], ['0', '2', '4'])

    def test_failed_later_page_is_not_success_and_does_not_change_region(self):
        with patch.object(adapters, '_LEVER_PAGE_SIZE', 2), patch.object(adapters, '_get_json',
                side_effect=[None, [posting(1), posting(2)], None]) as get:
            self.assertEqual(adapters.lever('acme'), [])
        self.assertEqual(len(get.call_args_list), 3)
        self.assertTrue(all('api.eu.lever.co' in c.args[0] for c in get.call_args_list[1:]))

    def test_repeated_page_or_work_budget_does_not_silently_truncate(self):
        for pages in (2, 50):
            with self.subTest(pages=pages), patch.object(adapters, '_LEVER_PAGE_SIZE', 1), \
                    patch.object(adapters, '_LEVER_MAX_PAGES', pages), \
                    patch.object(adapters, '_get_json', side_effect=[[posting(1)], [posting(1)]]) as get:
                self.assertEqual(adapters.lever('acme'), [])
                self.assertEqual(get.call_count, 2)
        with patch.object(adapters, '_LEVER_PAGE_SIZE', 1), patch.object(adapters, '_LEVER_MAX_PAGES', 2), \
                patch.object(adapters, '_get_json', side_effect=[[posting(1)], [posting(2)]]):
            self.assertEqual(adapters.lever('acme'), [])

    def test_infrastructure_links_do_not_hide_employer_board(self):
        text = 'https://assets-cdn.breezy.hr/x https://gallery-cdn.breezy.hr/y https://anovia.breezy.hr'
        self.assertEqual(webscout.fingerprint(text), ('breezy', 'anovia'))
        self.assertEqual(webscout.fingerprint('https://careers-analytics.recruitee.com'), ('', ''))
