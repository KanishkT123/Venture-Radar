"""ASGI entry point; background work remains in the request lifecycle."""
import asyncio
import hmac
import io
import os
from email.message import Message
from radar.cloud import CloudStore, process_one
from radar.server import make_handler


def dispatch(scope, body):
    store = CloudStore()
    headers = Message()
    for key, value in scope['headers']:
        headers[key.decode('latin1')] = value.decode('latin1')
    path = scope['path']
    if path == '/api/cron':
        secret = os.environ.get('CRON_SECRET', '')
        if scope['method'] != 'GET' or not secret or not hmac.compare_digest(headers.get('Authorization', ''), 'Bearer ' + secret):
            return 401, [], b'{"error":"Unauthorized"}', False
        if not os.getenv('OPENAI_API_KEY'):
            return 200, [], b'{"skipped":"OpenAI key not configured"}', False
        store.expire_jobs()
        store.schedule_if_due()
        return 202, [], b'{"ok":true}', True

    class RequestHandler(make_handler(store)):
        def send_response(self, status, message=None):
            self.response_status = status
        def send_header(self, key, value):
            self.response_headers.append((key.lower().encode(), value.encode()))
        def end_headers(self):
            pass

    request = RequestHandler.__new__(RequestHandler)
    request.headers = headers
    request.path = path + ('?' + scope['query_string'].decode() if scope.get('query_string') else '')
    request.rfile, request.wfile = io.BytesIO(body), io.BytesIO()
    request.response_headers, request.response_status = [], 200
    request.connection = type('Connection', (), {'settimeout': lambda *_: None})()
    if scope['method'] == 'GET':
        request.do_GET()
    elif scope['method'] == 'POST':
        request.do_POST()
    else:
        request.send({'error': 'Method not allowed'}, 405)
    background = request.response_status == 202 or (path == '/api/state' and request.response_status == 200 and
                 any(j['status'] == 'queued' for j in store.jobs()))
    return request.response_status, request.response_headers, request.wfile.getvalue(), background


async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    if scope['type'] != 'http':
        return
    body = bytearray()
    while True:
        message = await receive()
        if message['type'] == 'http.disconnect':
            return
        body.extend(message.get('body', b''))
        if len(body) > 262144:
            status, headers, result, background = 413, [], b'{"error":"Request too large"}', False
            break
        if not message.get('more_body'):
            try:
                status, headers, result, background = await asyncio.to_thread(dispatch, scope, bytes(body))
            except Exception:
                status, headers, result, background = 503, [], b'{"error":"Saved workspace unavailable. Please retry."}', False
            break
    if not headers:
        headers = [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': result})
    if background:
        await asyncio.to_thread(process_one)
