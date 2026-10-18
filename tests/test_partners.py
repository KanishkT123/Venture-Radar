import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from radar.models import investment, INVESTMENT
from radar.partners import partner_profile
from radar.research import run_research
from radar.store import Store
from radar.cloud import CloudStore
from test_cloud import MemoryBlob
from test_workflow import event, HTTPTests as BaseHTTP


def profile():
    return {'firm':'Fund A','partner_name':'Alex Example','role':'Partner','profile_url':'https://example.com/news',
            'fit_assessment':'Sector fit; stage still needs verification.',
            'email':{'address':None,'status':'not_found','source_url':None,'evidence':'No public address found.'},
            'theses':[{'claim':'Interested in biosensing.','source_urls':['https://example.com/news']}],
            'interests':[], 'big_wins':[], 'writings':[], 'gaps':['No public email verified.'],
            'draft':{'subject':'Biosensing conversation','message':'Hi Alex,\nI am building Cicada Health.\n[Your name]'},
            'sources':event()['sources']}


class PartnerTests(unittest.TestCase):
    def test_contact_and_claims_require_citations(self):
        p=profile();self.assertIsNone(partner_profile(p)['email']['address'])
        p['email']={'address':'alex@example.com','status':'public_direct','source_url':'https://uncited.example/contact','evidence':'Address printed here.'}
        with self.assertRaisesRegex(ValueError,'uncited'):partner_profile(p)
        p['email']['source_url']='https://example.com/news'
        self.assertEqual(partner_profile(p)['email']['address'],'alex@example.com')
        p['email']['status']='guessed'
        with self.assertRaises(ValueError):partner_profile(p)
        p=profile();p['theses'][0]['source_urls']=[]
        with self.assertRaises(ValueError):partner_profile(p)
        with self.assertRaises(ValueError):partner_profile(profile(),'Different Fund')

    def test_general_contact_without_verified_partner(self):
        p=profile();p['partner_name']=None;p['profile_url']=None
        p['email']={'address':'team@example.com','status':'public_general','source_url':'https://example.com/news','evidence':'Official general contact.'}
        self.assertEqual(partner_profile(p)['email']['status'],'public_general')
        p['email']['status']='public_direct'
        with self.assertRaises(ValueError):partner_profile(p)

    def test_profile_refresh_preserves_saved_draft_in_sqlite_and_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            for store,reopen in [(Store(directory),lambda:Store(directory)),(CloudStore(blob:=MemoryBlob()),lambda:CloudStore(blob))]:
                store.save_partner_profile(profile());store.save_partner_draft('Fund A','Edited subject','Private edited draft')
                new=profile();new['draft']['message']='New AI text';store.save_partner_profile(new)
                p=reopen().partner_profiles()[0]
                self.assertEqual(p['saved_draft']['message'],'Private edited draft')
                self.assertEqual(p['draft']['message'],'New AI text')

    def test_duplicate_click_does_not_consume_another_job(self):
        with tempfile.TemporaryDirectory() as directory:
            s=Store(directory);s.set_settings({'max_runs_per_day':1})
            a=s.enqueue('partner',{'investor':'Fund A'})
            self.assertEqual(a,s.enqueue('partner',{'investor':'fund a'}))
            s.claim(a)
            self.assertEqual(a,s.enqueue('partner',{'investor':'Fund A'}))
            self.assertEqual(s.jobs()[0]['target_investor'],'Fund A')
            self.assertNotIn('payload',s.jobs()[0])
            s.finish(a,{'partner_profile':True})
            with self.assertRaises(ValueError):s.enqueue('partner',{'investor':'Fund A'})

    def test_research_dispatch_provenance_and_private_data(self):
        class Client:
            def __init__(self,data):self.calls=[];self.data=data
            def create(self,**kwargs):
                self.calls.append(kwargs)
                return {'status':'completed','output':[{'content':[{'type':'output_text',
                    'text':'Cited public report' if len(self.calls)==1 else json.dumps(self.data),
                    'annotations':[{'type':'url_citation','url':'https://example.com/news'}] if len(self.calls)==1 else []}]}]}
        with tempfile.TemporaryDirectory() as directory:
            s=Store(directory);s.investor_notes('Fund A','PRIVATE_SENTINEL',True)
            s.save_partner_profile(profile());s.save_partner_draft('Fund A','Subject','PRIVATE_DRAFT')
            client=Client(profile());result=run_research(s,{'kind':'partner','payload':{'investor':'Fund A'}},client)
            self.assertTrue(result['partner_profile']);self.assertEqual(len(client.calls),2)
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(client.calls));self.assertNotIn('PRIVATE_DRAFT',json.dumps(client.calls))
            bad=profile();bad['sources'][0]['url']='https://fabricated.example/news'
            with self.assertRaises(ValueError):run_research(s,{'kind':'partner','payload':{'investor':'Fund A'}},Client(bad))
            self.assertEqual(s.partner_profiles()[0]['saved_draft']['message'],'PRIVATE_DRAFT')

    def test_undisclosed_currency_regression_and_no_usd_guess(self):
        for code in [None,'undisclosed','not disclosed','unknown','']:
            row=investment(event(amount=None,amount_basis='undisclosed',currency=code))
            self.assertIsNone(row['currency']);self.assertIsNone(row['amount_usd'])
        for code in [None,'unknown','US Dollars']:
            with self.assertRaises(ValueError):investment(event(currency=code))
        self.assertEqual(investment(event(currency='eur'))['currency'],'EUR')
        self.assertIsNone(investment(event(currency='EUR'))['amount_usd'])
        self.assertEqual(INVESTMENT['properties']['currency']['pattern'],'^[A-Z]{3}$')

    def test_discovery_saves_undisclosed_currency(self):
        calls=[]
        class Client:
            def create(self,**kwargs):
                calls.append(kwargs)
                return {'status':'completed','output':[{'content':[{'type':'output_text',
                    'text':'Public facts' if len(calls)==1 else json.dumps({'investments':[event(amount=None,amount_basis='undisclosed',currency=None)],'profiles':[]}),
                    'annotations':[{'type':'url_citation','url':'https://example.com/news'}]}]}]}
        with tempfile.TemporaryDirectory() as directory,patch('radar.research.feed_headlines',return_value=[]),patch('radar.research.dealroom_signals',return_value={'signals':[]}):
            s=Store(directory);self.assertEqual(run_research(s,{'kind':'discovery','payload':{}},Client())['added'],1)
            self.assertIsNone(s.investments()[0]['currency'])


class PartnerHTTPTests(unittest.TestCase):
    setUp = BaseHTTP.setUp
    tearDown = BaseHTTP.tearDown
    request = BaseHTTP.request
    def test_top_five_authorization_saved_profile_and_export(self):
        self.store.seed()
        _,raw=self.request('/api/state');ranked=json.loads(raw)['investors']
        first=ranked[0]['name'];sixth=ranked[5]['name']
        _,raw=self.request('/api/investor/deep-dive',{'investor':first});ident=json.loads(raw)['job_id']
        _,raw=self.request('/api/investor/deep-dive',{'investor':first});self.assertEqual(ident,json.loads(raw)['job_id'])
        self.assertEqual(self.store.jobs()[0]['kind'],'partner')
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/investor/deep-dive',{'investor':sixth})
        self.assertEqual(e.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/investor/deep-dive',{'investor':first},headers={'Authorization':''})
        self.assertEqual(e.exception.code,401)
        p=profile();p['firm']=first;self.store.save_partner_profile(p)
        self.request('/api/partner/draft',{'investor':first,'subject':'Updated','message':'My private draft'})
        _,raw=self.request('/api/export');self.assertEqual(json.loads(raw)['partner_profiles'][0]['saved_draft']['message'],'My private draft')
        _,raw=self.request('/api/state');self.assertEqual(json.loads(raw)['partner_profiles'][0]['firm'],first)

del BaseHTTP
