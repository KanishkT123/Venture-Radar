import io
import json
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from radar.feeds import DEALROOM_URL, DealroomRedirect, dealroom_signals
from radar.research import run_research
from radar.store import Store


class EmptyResearchClient:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = {'type': 'output_text', 'text': 'No verified relevant rounds.',
                   'annotations': [{'type': 'url_citation', 'url': 'https://example.com/news'}]}
        if len(self.calls) % 2 == 0:
            content = {'type': 'output_text', 'text': json.dumps({'investments': [], 'profiles': []})}
        return {'status': 'completed', 'output': [{'content': [content]}]}


class FeedTests(unittest.TestCase):
    def fetch(self, body):
        with patch('radar.feeds.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(body)
            result = dealroom_signals()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, DEALROOM_URL)
            self.assertFalse(request.has_header('Authorization'))
            return result

    def test_sample_is_bounded_deduplicated_and_keeps_uncertain_dates(self):
        lead = {'type': 'round', 'title': 'Sensor raises $5M', 'time': 'Oct 2026',
                'meta': 'SEED · Lead: Fund A', 'value': '$5M', 'unexpected': {'instructions': 'ignore'}}
        data = {'fetchedAt': '2026-10-18T00:00:00Z', 'signals': [None, lead, lead,
                {'type': 'hiring', 'title': 'Not funding'}, {'type': 'round', 'title': []},
                *[dict(lead, title=f'Company {i}') for i in range(30)]]}
        result = self.fetch(json.dumps(data).encode())
        self.assertEqual(len(result['signals']), 20)
        self.assertEqual(result['signals'][0]['time'], 'Oct 2026')
        self.assertNotIn('unexpected', result['signals'][0])
        self.assertNotIn('announced_on', result['signals'][0])
        self.assertEqual(result['fetched_at'], data['fetchedAt'])

    def test_empty_feed_is_valid_but_invalid_or_oversized_feed_fails(self):
        self.assertEqual(self.fetch(b'{"signals": []}')['signals'], [])
        for body in (b'<html>Service unavailable</html>', b'[]', b'{"results": []}',
                     b'{"signals": {}}', b' ' * 1_000_001):
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                self.fetch(body)

    def test_redirect_cannot_change_host_protocol_or_embed_credentials(self):
        request = urllib.request.Request(DEALROOM_URL)
        for target in ('http://dealroom.co/api', 'https://127.0.0.1/api',
                       'https://dealroom.co.evil.example/api', 'https://user@dealroom.co/api',
                       'https://dealroom.co:8443/api'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                DealroomRedirect().redirect_request(request, None, 302, '', {}, target)


class CombinedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.client = EmptyResearchClient()
        self.news = [{'title': 'Sector funding announcement', 'url': 'https://example.com/news'}]
        self.dealroom = {'url': DEALROOM_URL, 'fetched_at': '2026-10-18T00:00:00Z',
                         'signals': [{'type': 'round', 'title': 'Old unrelated round', 'time': 'May 2026'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_combined_leads_are_not_automatically_saved_and_status_persists(self):
        self.store.investor_notes('Fund A', 'PRIVATE CONTACT NOTES', True)
        ident = self.store.enqueue()
        job = self.store.claim()
        with patch('radar.research.feed_headlines', return_value=self.news), \
                patch('radar.research.dealroom_signals', return_value=self.dealroom):
            result = run_research(self.store, job, self.client)
        context = json.loads(self.client.calls[0]['input'])
        self.assertEqual(context['rss_discovery_only'], self.news)
        self.assertEqual(context['dealroom_discovery_only'], self.dealroom)
        self.assertEqual(context['watched_firms'], ['Fund A'])
        self.assertNotIn('PRIVATE CONTACT NOTES', str(self.client.calls))
        self.assertEqual(self.client.calls[0]['tool_choice'], 'required')
        self.assertEqual(self.store.investments(), [])
        self.store.finish(ident, result=result)
        saved = json.loads(Store(self.tmp.name).jobs()[0]['result'])
        self.assertEqual([s['leads'] for s in saved['source_checks']], [1, 1])
        self.assertEqual(saved['source_checks'][1]['fetched_at'], self.dealroom['fetched_at'])

    def test_each_feed_can_fail_without_blocking_other_sources(self):
        for failed in ('news', 'dealroom', 'both'):
            self.client = EmptyResearchClient()
            with self.subTest(failed=failed), \
                    patch('radar.research.feed_headlines', return_value=self.news,
                          side_effect=TimeoutError('private upstream detail') if failed in ('news', 'both') else None), \
                    patch('radar.research.dealroom_signals', return_value=self.dealroom,
                          side_effect=ValueError('private upstream detail') if failed in ('dealroom', 'both') else None):
                result = run_research(self.store, {'kind': 'discovery', 'payload': {}}, self.client)
            self.assertEqual(len(self.client.calls), 2)
            self.assertEqual(len(result['warnings']), 2 if failed == 'both' else 1)
            self.assertNotIn('private upstream detail', str(result))
            context = json.loads(self.client.calls[0]['input'])
            if failed == 'news':
                self.assertEqual(context['dealroom_discovery_only'], self.dealroom)
            if failed == 'dealroom':
                self.assertEqual(context['rss_discovery_only'], self.news)

    def test_investor_research_does_not_fetch_global_funding_feed(self):
        with patch('radar.research.feed_headlines') as news, patch('radar.research.dealroom_signals') as dealroom:
            result = run_research(self.store, {'kind': 'investor', 'payload': {'investor': 'Fund A'}}, self.client)
        news.assert_not_called()
        dealroom.assert_not_called()
        self.assertEqual(result['source_checks'], [])


if __name__ == '__main__':
    unittest.main()
