import unittest
from unittest.mock import patch, Mock
from collector.sources import adapters
from collector.sources.manatal_list import collect


def card(identifier, city="Pune", country="India", description="Develop embedded software."):
    return f'''<article class="mb-3 job-card"><a class="job-title-link" href="/jobs/{identifier}"
        data-job-title="Embedded Software Engineer" data-job-city="{city}" data-job-country="{country}">
        <h6>Embedded Software Engineer</h6></a><p>{description}</p>
        <a href="/jobs/{identifier}/apply" data-job-title="Embedded Software Engineer">Apply</a></article>'''


class ManatalCareerPages(unittest.TestCase):
    def setUp(self):
        self.polite = Mock()
        self.polite.allowed.return_value = True
        self.polite.crawl_delay.return_value = 2
        self.patcher = patch.object(adapters, '_MANATAL_POLITE', self.polite)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_robots_denial_does_not_fetch_any_listing(self):
        self.polite.allowed.return_value = False
        with patch.object(adapters, '_page') as read:
            self.assertEqual(adapters.manatal('acme.careers-page.com'), [])
        read.assert_not_called()

    def test_pagination_deduplicates_without_fabricating_dates_or_locations(self):
        base = "https://acme.careers-page.com/"
        pages = {base: card("one") + '<a class="btn-pagination" href="?page=2"><span>Next</span></a>',
                 base + '?page=2': card("one") + card("two", "", "") +
                     '<a class="btn-pagination" href="?page=2">Next</a>'}
        with patch.object(adapters, "_page", side_effect=pages.__getitem__) as read:
            jobs = adapters.manatal("acme.careers-page.com", "Acme")
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.polite._wait.call_count, 2)
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j['location'] for j in jobs], ['Pune, India', ''])
        self.assertEqual(jobs[0]['url'], base + 'jobs/one')
        self.assertTrue(all(j['posted_at'] == '' and not j.get('posted_field') for j in jobs))

    def test_explicit_candidate_network_is_not_a_vacancy_and_next_stays_on_board(self):
        html = card('pool', description='We are building a candidate network for future opportunities.') + card('live')
        html += '<a class="btn-pagination" href="https://elsewhere.careers-page.com/?page=2">Next</a>'
        with patch.object(adapters, "_page", return_value=html) as read:
            jobs = adapters.manatal("acme.careers-page.com")
        self.assertEqual([j['url'].rsplit('/',1)[-1] for j in jobs], ['live'])
        self.assertEqual(read.call_count, 1)
        for host in ('www.careers-page.com', 'localhost', 'acme.careers-page.com@evil.test'):
            self.assertEqual(collect(host, lambda _: self.fail('invalid host was fetched')), [])

    def test_page_budget_stops_unbounded_pagination(self):
        count = 0
        def page(url):
            nonlocal count
            count += 1
            return card(str(count)) + f'<a class="btn-pagination" href="?page={count+1}">Next</a>'
        self.assertEqual(len(collect('acme.careers-page.com', page)), 30)
        self.assertEqual(count, 30)

    def test_legacy_board_keeps_public_api_and_unknown_publication_time(self):
        with patch.object(adapters, '_get_json', return_value={'results': [
                {'hash': 'ABC', 'position_name': 'Software Engineer', 'location_display': 'Pune, India',
                 'created_at': '2026-11-01T00:00:00Z'}], 'next': None}) as get:
            jobs = adapters.manatal('acme', 'Acme')
        self.assertEqual(get.call_args.args[0], 'https://api.manatal.com/open/v3/career-page/acme/jobs/')
        self.assertEqual(jobs[0]['url'], 'https://www.careers-page.com/acme/job/ABC')
        self.assertEqual(jobs[0]['posted_at'], '')
