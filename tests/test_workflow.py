import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from pathlib import Path
from radar.models import investment
from radar.store import Store, DEFAULTS
from radar.ranking import rankings, paths
from radar.research import run_research, response_text
from radar.server import create_server, verify_webhook, csv_safe
from radar.lock import runner_lock


def event(**updates):
    d={'company':'Test Sensor','round':'Series A','announced_on':'2026-01-01','sector':'Biosensing','kind':'equity',
       'amount':10000000,'currency':'USD','amount_basis':'exact','valuation_usd':None,'valuation_kind':'unspecified','notes':'Fixture',
       'investors':[{'name':'Fund A','role':'lead','check_usd':None}],
       'sources':[{'url':'https://example.com/news','title':'Funding announcement','publisher':'Example','evidence':'Fixture funding event.'}]}
    d.update(updates);return d


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_persistence_and_backup(self):
        self.store.add_investment(event(),'confirmed')
        self.store.investor_notes('Fund A','Private notes',True)
        other=Store(self.tmp.name)
        self.assertEqual(other.profiles()[0]['notes'],'Private notes')
        backup=Path(self.tmp.name)/'backup.db';self.store.backup(backup)
        import sqlite3
        with sqlite3.connect(backup) as c:self.assertEqual(c.execute('select count(*) from investments').fetchone()[0],1)
    def test_duplicate_and_revision_family(self):
        first,_=self.store.add_investment(event(),'confirmed')
        duplicate,new=self.store.add_investment(event(announced_on='2026-01-02'))
        self.assertFalse(new);self.assertEqual(first,duplicate)
        second,_=self.store.add_investment(event(amount=12000000))
        third,_=self.store.add_investment(event(amount=13000000))
        self.assertEqual(len(self.store.investments()),3)
        self.store.review(second,'confirmed');self.store.review(third,'confirmed')
        self.assertEqual([r['id'] for r in self.store.investments() if r['status']=='confirmed'],[third])
        self.store.review(first,'confirmed')
        self.assertEqual([r['id'] for r in self.store.investments() if r['status']=='confirmed'],[first])
    def test_note_correction_is_not_lost(self):
        first,_=self.store.add_investment(event(),'confirmed')
        new,_=self.store.add_investment(event(notes='Corrected'),replaces=first)
        self.store.review(new,'confirmed')
        self.assertEqual(next(r for r in self.store.investments() if r['status']=='confirmed')['notes'],'Corrected')
    def test_webhook_idempotency_and_daily_cap(self):
        self.store.set_settings({'max_runs_per_day':1})
        a=self.store.enqueue(delivery_id='abc');b=self.store.enqueue(delivery_id='abc')
        self.assertEqual(a,b)
        with self.assertRaises(ValueError):self.store.enqueue(delivery_id='def')
    def test_worker_recovery_and_claim(self):
        ident=self.store.enqueue();self.assertEqual(self.store.claim()['id'],ident)
        self.assertIsNone(self.store.claim());self.store.recover()
        self.assertEqual(self.store.claim()['id'],ident);self.store.recover()
        self.assertIsNone(self.store.claim());self.assertEqual(self.store.jobs()[0]['status'],'failed')
    def test_schedule_does_not_duplicate_active_job(self):
        self.store.set_settings({'schedule_enabled':True})
        self.store.schedule_if_due();self.store.schedule_if_due()
        self.assertEqual(len(self.store.jobs()),1)
    def test_validation_rejects_nonfinite_and_future(self):
        for value in [float('nan'),float('inf'),-1,True]:
            with self.assertRaises(ValueError):investment(event(amount=value))
        with self.assertRaises(ValueError):investment(event(announced_on='2099-01-01'))
        with self.assertRaises(ValueError):investment(event(amount=None))
    def test_non_usd_is_not_silently_converted(self):
        self.assertIsNone(investment(event(currency='CAD'))['amount_usd'])
    def test_ranking_monotonic_and_exclusions(self):
        def ranked(amount,date):
            e=investment(event(amount=amount,announced_on=date));e['status']='confirmed'
            return rankings([e],[],DEFAULTS,dt.date(2026,10,18))[0]['score']
        self.assertGreater(ranked(10000000,'2026-10-01'),ranked(10000000,'2026-01-01'))
        self.assertGreater(ranked(100000000,'2026-10-01'),ranked(10000000,'2026-10-01'))
        for updates in [{'status':'needs_review'},{'amount_basis':'cumulative'},{'kind':'debt'}]:
            e={**investment(event()),'status':'confirmed',**updates}
            self.assertEqual(rankings([e],[],DEFAULTS),[])
    def test_intro_does_not_invent_first_hop(self):
        ev={**investment(event()),'status':'confirmed'}
        self.assertEqual(paths('Fund A',[],[ev])['paths'],[])
        edge=dict(from_name='You',to_name='Test Sensor',relationship='Advisor',evidence='Known founder',confirmed=True,strength=.9)
        found=paths('Fund A',[edge],[ev])['paths'][0]
        self.assertFalse(found['confirmed']);self.assertEqual(len(found['edges']),2)
    def test_runner_lock_is_exclusive(self):
        with runner_lock(Path(self.tmp.name)):
            with self.assertRaises(ValueError):
                with runner_lock(Path(self.tmp.name)):pass
    def test_two_stage_research_and_uncited_url_rejection(self):
        def response(text,annotations=None):return {'status':'completed','output':[{'content':[{'type':'output_text','text':text,'annotations':annotations or []}]}]}
        class Client:
            def __init__(self,data):self.data=data;self.calls=[]
            def create(self,**kwargs):
                self.calls.append(kwargs)
                return response('Researched facts',[{'type':'url_citation','url':'https://example.com/news'}]) if len(self.calls)==1 else response(json.dumps(self.data))
        job={'kind':'discovery','payload':{}}
        client=Client({'investments':[event()],'profiles':[]})
        with patch('radar.research.feed_headlines',return_value=[]), patch('radar.research.dealroom_signals',return_value={'signals':[], 'fetched_at':None}):
            result=run_research(self.store,job,client)
        self.assertEqual(result['added'],1);self.assertEqual(self.store.investments()[0]['status'],'needs_review')
        self.assertEqual(client.calls[0]['tools'],[{'type':'web_search'}]);self.assertTrue(client.calls[1]['text']['format']['strict'])
        bad=event(company='Bad',sources=[{'url':'https://fabricated.example/news'}])
        with patch('radar.research.feed_headlines',return_value=[]), patch('radar.research.dealroom_signals',return_value={'signals':[], 'fetched_at':None}):
            with self.assertRaises(ValueError):run_research(self.store,job,Client({'investments':[bad],'profiles':[]}))
        self.assertEqual(len(self.store.investments()),1)
        with self.assertRaises(ValueError):response_text({'status':'incomplete'})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.env=patch.dict(os.environ,{'RADAR_ACCESS_TOKEN':'test-access','RADAR_WEBHOOK_SECRET':'test-hook','OPENAI_API_KEY':'test-key'})
        self.env.start();self.server=create_server(self.store,port=0)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.env.stop();self.tmp.cleanup()
    def request(self,path,data=None,headers=None):
        h={'Authorization':'Bearer test-access','Content-Type':'application/json'};h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=h)
        with urllib.request.urlopen(req) as r:return r.status,r.read()
    def test_ui_api_database_flow(self):
        status,page=self.request('/');self.assertEqual(status,200);self.assertIn(b'Investment dashboard',page)
        self.request('/api/import',{'investments':[event()]})
        _,raw=self.request('/api/state');data=json.loads(raw);ident=data['investments'][0]['id']
        self.assertEqual(data['investments'][0]['status'],'needs_review')
        self.request('/api/review',{'id':ident,'status':'confirmed'})
        _,raw=self.request('/api/state');self.assertEqual(json.loads(raw)['investors'][0]['deal_count'],1)
        self.request('/api/investor/notes',{'name':'Fund A','notes':'Saved through HTTP','watched':True})
        self.assertEqual(Store(self.tmp.name).profiles()[0]['notes'],'Saved through HTTP')
        self.assertNotIn(b'test-key',raw)
    def test_auth_and_origin(self):
        for headers in [{'Authorization':''},{'Origin':'https://evil.example'}]:
            with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/state',headers=headers)
            self.assertEqual(cm.exception.code,401)
    def test_signed_webhook_and_tampering(self):
        body=json.dumps({'title':'Funding tip','url':'https://example.com/news'}).encode()
        stamp=str(int(time.time()));delivery='event-123'
        sig='sha256='+hmac.new(b'test-hook',stamp.encode()+b'.'+delivery.encode()+b'.'+body,hashlib.sha256).hexdigest()
        headers={'X-Radar-Timestamp':stamp,'X-Radar-Delivery':delivery,'X-Radar-Signature':sig,'Content-Type':'application/json'}
        def send():
            req=urllib.request.Request(self.base+'/api/webhooks/news',data=body,headers=headers)
            with urllib.request.urlopen(req) as r:return json.load(r)
        self.assertEqual(send()['job_id'],send()['job_id']);self.assertEqual(len(self.store.jobs()),1)
        with self.assertRaises(PermissionError):verify_webhook(headers,body+b' ', 'test-hook')
        headers['X-Radar-Timestamp']=str(int(stamp)-600)
        with self.assertRaises(PermissionError):verify_webhook(headers,body,'test-hook')
    def test_csv_formula_injection(self):
        self.assertTrue(csv_safe('=HYPERLINK("evil")').startswith("'"))
        self.assertTrue(csv_safe('  +cmd').startswith("'"))
    def test_bad_import_is_not_partially_written(self):
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/import',{'investments':[event(),event(company='Invalid',announced_on='bad')]})
        self.assertEqual(self.store.investments(),[])

if __name__=='__main__':unittest.main()
