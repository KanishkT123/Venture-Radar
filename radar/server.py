"""Local HTTP application. Remote access requires a bearer token."""
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .models import investment, url
from .ranking import rankings, paths

STATIC = Path(__file__).parent.parent / 'static'


def csv_safe(value):
    text = str(value if value is not None else '')
    return "'"+text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def verify_webhook(headers, body, secret, now=None):
    if not secret:
        raise PermissionError('Configure RADAR_WEBHOOK_SECRET before accepting webhooks.')
    stamp = headers.get('X-Radar-Timestamp', '')
    delivery = headers.get('X-Radar-Delivery', '')
    signature = headers.get('X-Radar-Signature', '')
    if not delivery or len(delivery) > 128 or not stamp.isdigit() or abs((now or time.time()) - int(stamp)) > 300:
        raise PermissionError('Invalid or expired webhook timestamp/delivery.')
    message = stamp.encode()+b'.'+delivery.encode()+b'.'+body
    expected = 'sha256='+hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PermissionError('Invalid webhook signature.')
    return delivery


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'VentureRadar/1.0'

        def log_message(self, fmt, *args):
            # Do not log query strings or request bodies (they may contain private notes).
            pass

        def send(self, data, status=200, content_type='application/json', attachment=None):
            raw = json.dumps(data, allow_nan=False).encode() if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(raw)

        def authorize(self, api=True):
            host = urlsplit('//'+self.headers.get('Host', '')).hostname
            token = os.getenv('RADAR_ACCESS_TOKEN', '')
            if not token and host not in ('127.0.0.1', 'localhost', '::1'):
                raise PermissionError('A remote host requires RADAR_ACCESS_TOKEN.')
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                raise PermissionError('Cross-origin requests are not allowed.')
            if api and token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token):
                raise PermissionError('Enter the dashboard access token.')

        def do_GET(self):
            try:
                p = urlsplit(self.path)
                self.authorize(api=p.path.startswith('/api/'))
                if p.path == '/api/state':
                    settings = store.settings()
                    events = store.investments()
                    self.send({'settings': settings, 'investments': events,
                               'investors': rankings(events, store.profiles(), settings), 'edges': store.edges(),
                               'partner_profiles': store.partner_profiles(), 'jobs': store.jobs(), 'configured': bool(os.getenv('OPENAI_API_KEY')),
                               'investor_events': store.investor_events(),
                               'model': os.getenv('OPENAI_MODEL', 'gpt-5.5'),
                               'hosted': bool(os.getenv('VERCEL')),
                               'webhook_configured': bool(os.getenv('RADAR_WEBHOOK_SECRET'))})
                elif p.path == '/api/intros':
                    name = parse_qs(p.query).get('investor', [''])[0]
                    self.send(paths(name, store.edges(), store.investments()))
                elif p.path == '/api/export':
                    self.send({'schema_version': 1, 'settings': store.settings(), 'investments': store.investments(),
                               'profiles': store.profiles(), 'partner_profiles': store.partner_profiles(), 'edges': store.edges(), 'jobs': store.jobs(),
                               'investor_events': store.investor_events()}, attachment='venture-radar-export.json')
                elif p.path == '/api/export.csv':
                    output = io.StringIO()
                    writer = csv.writer(output)
                    keys = ['company', 'round', 'announced_on', 'kind', 'amount', 'currency', 'amount_basis', 'valuation_usd', 'valuation_kind', 'status']
                    writer.writerow(keys+['investors', 'sources'])
                    for e in store.investments():
                        writer.writerow([csv_safe(e[k]) for k in keys] + [csv_safe('; '.join(i['name'] for i in e['investors'])), '; '.join(s['url'] for s in e['sources'])])
                    self.send(output.getvalue().encode(), content_type='text/csv; charset=utf-8', attachment='investments.csv')
                elif p.path in ('/', '/index.html', '/app.js', '/style.css', '/favicon.svg'):
                    file = STATIC / ('index.html' if p.path == '/' else p.path.lstrip('/'))
                    self.send(file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                else:
                    self.send({'error': 'Not found'}, 404)
            except PermissionError as e:
                self.send({'error': str(e)}, 401)
            except (ValueError, KeyError) as e:
                self.send({'error': str(e)}, 400)

        def do_POST(self):
            try:
                p = urlsplit(self.path).path
                is_hook = p == '/api/webhooks/news'
                if not is_hook:
                    self.authorize()
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('Content-Type must be application/json.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 262144:
                    raise ValueError('Request must contain at most 256KB of JSON.')
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                delivery = verify_webhook(self.headers, raw, os.getenv('RADAR_WEBHOOK_SECRET', '')) if is_hook else None
                d = json.loads(raw)
                if not isinstance(d, dict):
                    raise ValueError('A JSON object is required.')
                if p == '/api/events/research':
                    if not os.getenv('OPENAI_API_KEY'):
                        raise ValueError('Set OPENAI_API_KEY in your environment and restart or redeploy first.')
                    from .events import tracked_firms
                    if not tracked_firms(store):
                        raise ValueError('Add a tracked investor before researching events.')
                    self.send({'job_id': store.enqueue('events')}, 202)
                elif is_hook or p in ('/api/refresh', '/api/investor/research', '/api/investor/deep-dive'):
                    if not os.getenv('OPENAI_API_KEY'):
                        raise ValueError('Set OPENAI_API_KEY in your environment and restart or redeploy first.')
                    payload = {}
                    if is_hook:
                        tip = {k: str(d.get(k, ''))[:3000] for k in ('title', 'url', 'summary')}
                        if tip['url']:
                            tip['url'] = url(tip['url'])
                        if not tip['title'] and not tip['url']:
                            raise ValueError('Webhook needs a title or article URL.')
                        payload = {'tip': tip}
                    elif p in ('/api/investor/research', '/api/investor/deep-dive'):
                        if not isinstance(d.get('investor'), str) or not 1 <= len(d['investor']) <= 200:
                            raise ValueError('Investor name required.')
                        payload = {'investor': d['investor']}
                    kind = 'investor' if p.endswith('/research') else 'discovery'
                    if p.endswith('/deep-dive'):
                        top = rankings(store.investments(), store.profiles(), store.settings())[:5]
                        if payload['investor'] not in [item['name'] for item in top]:
                            raise ValueError('Deep dives are available for the current top five investors.')
                        kind = 'partner'
                    ident = store.enqueue(kind, payload, delivery)
                    self.send({'job_id': ident}, 202)
                elif p == '/api/partner/draft':
                    store.save_partner_draft(d['investor'], d['subject'], d['message'])
                    self.send({'saved': True})
                elif p == '/api/settings':
                    self.send(store.set_settings(d))
                elif p == '/api/review':
                    store.review(d['id'], d['status'])
                    self.send({'ok': True})
                elif p == '/api/investor/notes':
                    store.investor_notes(d['name'], d['notes'], d['watched'])
                    self.send({'ok': True})
                elif p == '/api/edges':
                    self.send({'id': store.add_edge(d)})
                elif p == '/api/edges/delete':
                    store.delete_edge(d['id'])
                    self.send({'ok': True})
                elif p == '/api/import':
                    rows = d.get('investments')
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise ValueError('Provide an investments array of at most 100 records.')
                    validated = [investment(r) for r in rows]
                    if d.get('replaces') and len(rows) != 1:
                        raise ValueError('Corrections must contain one investment.')
                    added = sum(store.add_investment(r, replaces=d.get('replaces'))[1] for r in validated)
                    self.send({'added': added})
                else:
                    self.send({'error': 'Not found'}, 404)
            except PermissionError as e:
                self.send({'error': str(e)}, 401)
            except (ValueError, KeyError, TypeError, OverflowError) as e:
                self.send({'error': str(e)[:500]}, 400)
            except Exception:
                self.send({'error': 'The request could not be completed. Saved data is retained.'}, 500)
    return Handler


def create_server(store, host='127.0.0.1', port=8765):
    if host not in ('127.0.0.1', 'localhost', '::1') and not os.getenv('RADAR_ACCESS_TOKEN'):
        raise ValueError('Binding to a network interface requires RADAR_ACCESS_TOKEN. Default localhost does not.')
    server = ThreadingHTTPServer((host, port), make_handler(store))
    server.daemon_threads = True
    return server
