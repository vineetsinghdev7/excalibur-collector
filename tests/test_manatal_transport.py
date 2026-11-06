import unittest
from unittest.mock import patch
from collector.sources import adapters, net, webscout


class ManatalPublicTransport(unittest.TestCase):
    def test_real_polite_reader_supports_adapter_and_rejects_robots_denial(self):
        host='acme.careers-page.com'
        body=b'''<article class="job-card"><a class="job-title-link" href="/jobs/one"
            data-job-title="Software Engineer" data-job-city="Pune" data-job-country="India">Engineer</a></article>'''
        calls=[]
        def response(url, *args, **kwargs):
            calls.append(url)
            return (200,url,b'User-agent: *\nCrawl-delay: 2\nAllow: /' if url.endswith('/robots.txt') else body)
        with patch.object(adapters,'_MANATAL_POLITE',None), patch.object(net,'request',side_effect=response), \
                patch.object(webscout.time,'sleep'), patch.object(net,'DEADLINE',float('inf')):
            rows=adapters.manatal(host,'Acme')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['location'],'Pune, India')
        self.assertEqual(calls,['https://'+host+'/robots.txt','https://'+host+'/'])
        with patch.object(adapters,'_MANATAL_POLITE',None), patch.object(net,'request',return_value=(200,'',b'User-agent: *\nDisallow: /')) as request:
            self.assertEqual(adapters.manatal(host),[])
        self.assertEqual(request.call_count,1)

    def test_pacing_honors_collector_deadline(self):
        polite=webscout.Polite()
        with patch.object(net,'DEADLINE',0), patch.object(webscout.time,'sleep') as sleep:
            self.assertFalse(polite._wait('acme.careers-page.com'))
        sleep.assert_not_called()
