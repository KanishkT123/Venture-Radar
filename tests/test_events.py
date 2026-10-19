import asyncio
import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from api.index import app
from radar.cloud import CloudStore, Conflict, process_one
from radar.events import event, today_in_ma, tracked_firms
from radar.research import run_research
from radar.store import Store
from test_cloud import MemoryBlob
from test_workflow import HTTPTests as BaseHTTP, event as investment

EVENT_URL = 'https://example.com/boston-event'
FIRM_URL = 'https://example.com/firm-announcement'


def fixture(**updates):
    when = (today_in_ma() + dt.timedelta(days=10)).isoformat()
    row = {'title': 'Example Investor Forum', 'starts_on': when, 'ends_on': None,
           'start_time': '17:30', 'end_time': '19:00', 'city': 'Cambridge', 'state': 'MA',
           'format': 'in_person', 'status': 'scheduled', 'event_url': EVENT_URL,
           'firms': [{'firm': 'Fund A', 'role': 'sponsor', 'person': None,
                      'source_url': FIRM_URL, 'evidence': 'Fund A is named as a sponsor of this forum.'}],
           'sources': [{'url': EVENT_URL, 'title': 'Organizer event listing', 'publisher': 'Organizer',
                        'evidence': 'The organizer publishes the date, local times and Cambridge venue.'},
                       {'url': FIRM_URL, 'title': 'Firm announcement', 'publisher': 'Fund A',
                        'evidence': 'The firm announces its sponsorship of this event.'}]}
    row.update(updates)
    return row


class Client:
    def __init__(self, events=None, urls=None, fail=False):
        self.events = [fixture()] if events is None else events
        self.urls = [EVENT_URL, FIRM_URL] if urls is None else urls
        self.fail = fail
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise ValueError('OpenAI connection failed. Retry manually.')
        first = len(self.calls) == 1
        return {'status': 'completed', 'output': [{'content': [{'type': 'output_text',
                'text': 'Cited event research.' if first else json.dumps({'events': self.events}),
                'annotations': [{'type': 'url_citation', 'url': u} for u in self.urls] if first else []}]}]}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.investor_notes('Fund A', 'PRIVATE_NOTES', False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation_requires_upcoming_ma_dates_roles_and_citations(self):
        self.assertEqual(event(fixture(), ['Fund A'], {EVENT_URL, FIRM_URL})['start_time'], '17:30')
        bad = [fixture(state='NY'), fixture(format='online'), fixture(city=''),
               fixture(starts_on=(today_in_ma()-dt.timedelta(days=1)).isoformat()),
               fixture(starts_on=(today_in_ma()+dt.timedelta(days=91)).isoformat()),
               fixture(starts_on='20261030'), fixture(start_time='5:30 pm'),
               fixture(start_time=None, end_time='19:00'), fixture(end_time='12:00'),
               fixture(ends_on=today_in_ma().isoformat()), fixture(event_url='javascript:alert(1)'),
               fixture(event_url='https://uncited.example/event'), fixture(sources=[])]
        for key, value in [('firm', 'Untracked Fund'), ('role', 'might_attend'),
                           ('evidence', ''), ('source_url', 'https://uncited.example/firm')]:
            row = fixture(); row['firms'][0][key] = value; bad.append(row)
        row = fixture(); row['sources'][0]['evidence'] = ''; bad.append(row)
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                event(row, ['Fund A'], {EVENT_URL, FIRM_URL})
        with self.assertRaises(ValueError):
            event(fixture(), ['Fund A'], {EVENT_URL})
        with self.assertRaises(ValueError):
            event(fixture(), ['Fund A'], set())

    def test_unknown_time_stays_null_and_names_are_canonical(self):
        row = fixture(start_time=None, end_time=None)
        row['firms'][0]['firm'] = 'fund a'
        clean = event(row, ['Fund A'], {EVENT_URL, FIRM_URL})
        self.assertIsNone(clean['start_time'])
        self.assertEqual(clean['firms'][0]['firm'], 'Fund A')

    def test_missing_timezone_data_does_not_break_existing_dashboard_data(self):
        with patch('radar.events.MA_TIMEZONE', None):
            self.assertIn('unavailable', self.store.investor_events())
            self.assertEqual(self.store.profiles()[0]['name'], 'Fund A')
            with self.assertRaisesRegex(ValueError, 'tzdata'):
                today_in_ma()

    def test_uses_live_dashboard_union_including_unwatched_investors(self):
        self.store.add_investment(investment(investors=[{'name': 'Round-only Fund', 'role': 'lead', 'check_usd': None}]), 'confirmed')
        self.store.add_investment(investment(company='Grant recipient', kind='grant',
            investors=[{'name': 'Grant Agency', 'role': 'grantor', 'check_usd': None}]), 'confirmed')
        self.assertEqual(set(tracked_firms(self.store)), {'Fund A', 'Round-only Fund'})

    def test_research_dispatch_has_citations_and_excludes_private_context(self):
        self.store.add_edge({'from_name': 'You', 'to_name': 'PRIVATE_CONTACT', 'relationship': 'Friend',
                             'evidence': 'PRIVATE_GRAPH', 'confirmed': True, 'strength': 1})
        self.store.set_settings({'focus': 'PRIVATE_FOCUS'})
        with self.store.db() as c:
            c.execute('INSERT INTO partner_profiles VALUES(?,?,?,?,?)', ('funda', '{}', 0, 'PRIVATE_SUBJECT', 'PRIVATE_DRAFT'))
        client = Client()
        result = run_research(self.store, {'kind': 'events', 'payload': {}}, client)
        self.assertTrue(result['investor_events'])
        self.assertEqual(result['found'], 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]['tools'], [{'type': 'web_search'}])
        self.assertEqual(client.calls[0]['max_tool_calls'], 6)
        self.assertTrue(client.calls[1]['text']['format']['strict'])
        sent = json.dumps(client.calls)
        for private in ['PRIVATE_NOTES', 'PRIVATE_CONTACT', 'PRIVATE_GRAPH', 'PRIVATE_FOCUS', 'PRIVATE_SUBJECT', 'PRIVATE_DRAFT']:
            self.assertNotIn(private, sent)
        self.assertEqual(len(self.store.investor_events()['events']), 1)

    def test_invalid_batch_or_failed_call_preserves_saved_results(self):
        self.store.save_investor_events([fixture()])
        before = self.store.investor_events()
        bad = fixture(title='Bad event'); bad['firms'][0]['source_url'] = 'https://uncited.example/firm'
        for client in [Client([fixture(title='New good event'), bad]), Client(fail=True), Client([fixture()], urls=[])]:
            with self.assertRaises(ValueError):
                run_research(self.store, {'kind': 'events', 'payload': {}}, client)
            self.assertEqual(self.store.investor_events(), before)
        with self.assertRaises(ValueError):
            self.store.save_investor_events([fixture(title='Another good event'), bad])
        self.assertEqual(self.store.investor_events(), before)

    def test_empty_search_is_successful_and_retains_previous_matches(self):
        result = run_research(self.store, {'kind': 'events', 'payload': {}}, Client([], urls=[]))
        self.assertEqual(result['found'], 0)
        self.assertIsNotNone(self.store.investor_events()['last_checked_at'])
        self.store.save_investor_events([fixture()])
        run_research(self.store, {'kind': 'events', 'payload': {}}, Client([]))
        self.assertEqual(len(self.store.investor_events()['events']), 1)

    def test_duplicates_merge_firms_but_keep_sessions_and_recurring_dates(self):
        self.store.investor_notes('Fund B', '', False)
        a, b = fixture(), fixture()
        b['firms'][0]['firm'] = 'Fund B'
        b['firms'][0]['role'] = 'speaker'
        b['firms'][0]['person'] = 'Example Partner'
        self.store.save_investor_events([a, b, a])
        saved = self.store.investor_events()['events']
        self.assertEqual(len(saved), 1)
        self.assertEqual({f['firm'] for f in saved[0]['firms']}, {'Fund A', 'Fund B'})
        self.store.save_investor_events([fixture(title='Different conference session'),
            fixture(starts_on=(today_in_ma()+dt.timedelta(days=11)).isoformat())])
        self.assertEqual(len(self.store.investor_events()['events']), 3)

    def test_cancellations_past_events_and_removed_firms_are_hidden(self):
        self.store.save_investor_events([fixture()])
        self.store.save_investor_events([fixture(status='cancelled')])
        self.assertEqual(self.store.investor_events()['events'], [])
        self.store.save_investor_events([fixture()])
        with self.store.db() as c:
            c.execute('DELETE FROM profiles')
        self.assertEqual(self.store.investor_events()['events'], [])
        self.store.investor_notes('Fund A', '', False)
        with self.store.db() as c:
            c.execute('UPDATE investor_events SET starts_on=?', ((today_in_ma()-dt.timedelta(days=1)).isoformat(),))
        self.assertEqual(self.store.investor_events()['events'], [])

    def test_shared_cap_and_active_request_deduplication(self):
        self.store.set_settings({'max_runs_per_day': 1})
        ident = self.store.enqueue('events')
        self.assertEqual(self.store.enqueue('events'), ident)
        self.store.claim(ident)
        self.assertEqual(self.store.enqueue('events'), ident)
        with self.assertRaises(ValueError):
            self.store.enqueue('discovery')
        self.store.finish(ident, {'investor_events': True, 'found': 0})
        with self.assertRaises(ValueError):
            self.store.enqueue('events')

    def test_sqlite_backup_and_cloud_conflict_replay_preserve_events(self):
        self.store.save_investor_events([fixture()])
        self.assertEqual(len(Store(self.tmp.name).investor_events()['events']), 1)
        self.store.backup(Path(self.tmp.name)/'backup.sqlite3')
        blob = MemoryBlob()
        cloud = CloudStore(blob)
        cloud.investor_notes('Fund A', '', False)
        original_write = blob.write
        attempts = []
        def conflict_once(raw, etag):
            attempts.append(1)
            if len(attempts) == 1:
                raise Conflict()
            return original_write(raw, etag)
        with patch.object(blob, 'write', side_effect=conflict_once):
            run_research(cloud, {'kind': 'events', 'payload': {}}, client := Client())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(CloudStore(blob).investor_events()['events']), 1)


class EventHTTPTests(unittest.TestCase):
    setUp = BaseHTTP.setUp
    tearDown = BaseHTTP.tearDown
    request = BaseHTTP.request

    def test_auth_enqueue_state_export_and_panel_placement(self):
        self.store.investor_notes('Fund A', '', False)
        for headers in [{'Authorization': ''}, {'Origin': 'https://evil.example'}]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/events/research', {}, headers)
            self.assertEqual(error.exception.code, 401)
        status, raw = self.request('/api/events/research', {})
        self.assertEqual(status, 202)
        ident = json.loads(raw)['job_id']
        self.assertEqual(json.loads(self.request('/api/events/research', {})[1])['job_id'], ident)
        self.assertEqual(len(self.store.jobs()), 1)
        job = self.store.claim(ident)
        self.store.finish(ident, run_research(self.store, job, Client()))
        for path in ['/api/state', '/api/export']:
            status, raw = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(raw)['investor_events']['events']), 1)
            self.assertNotIn(b'test-key', raw)
        _, page = self.request('/')
        self.assertLess(page.index(b'Who is putting capital to work?'), page.index(b'id="investor-events-panel"'))
        self.assertLess(page.index(b'id="investor-events-panel"'), page.index(b'class="ranking-controls"'))

    def test_missing_key_and_empty_firm_list_do_not_enqueue(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/events/research', {})
        self.store.investor_notes('Fund A', '', False)
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/events/research', {})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.store.jobs(), [])


class EventASGITests(unittest.TestCase):
    def test_hosted_route_processes_event_job_and_persists_across_requests(self):
        blob = MemoryBlob()
        cloud = CloudStore(blob)
        cloud.investor_notes('Fund A', '', False)
        async def request(path, method='GET', token='private'):
            sent = []
            body = b'{}' if method == 'POST' else b''
            async def receive(): return {'type': 'http.request', 'body': body}
            async def send(message): sent.append(message)
            scope = {'type': 'http', 'method': method, 'path': path, 'query_string': b'',
                     'headers': [(b'host', b'radar.example'), (b'authorization', ('Bearer '+token).encode()),
                                 (b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]}
            await app(scope, receive, send)
            return sent[0]['status'], json.loads(sent[1]['body'])
        with patch.dict(os.environ, {'RADAR_ACCESS_TOKEN': 'private', 'OPENAI_API_KEY': 'test-key'}), \
             patch('api.index.CloudStore', side_effect=lambda: CloudStore(blob)), \
             patch('api.index.process_one', side_effect=lambda: process_one(CloudStore(blob))), \
             patch('radar.research.OpenAI', return_value=Client()):
            self.assertEqual(asyncio.run(request('/api/events/research', 'POST', 'wrong'))[0], 401)
            self.assertEqual(asyncio.run(request('/api/events/research', 'POST'))[0], 202)
            status, state = asyncio.run(request('/api/state'))
            self.assertEqual(status, 200)
            self.assertEqual(len(state['investor_events']['events']), 1)
            self.assertEqual(state['jobs'][0]['status'], 'completed')


del BaseHTTP

if __name__ == '__main__':
    unittest.main()
