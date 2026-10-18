"""Private Blob SQLite snapshots with optimistic concurrency for one workspace.

Every mutation reloads origin and uses its ETag. Conflicts replay only the local
database operation, never an OpenAI call. This is bounded to 8 MB, not a shared
database for a large multi-user service.
"""
import json
import os
import random
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .store import Store

LIMIT = 8 * 1024 * 1024


class Conflict(Exception):
    pass


class Blob:
    def __init__(self):
        self.token = os.environ.get('BLOB_READ_WRITE_TOKEN', '')
        match = re.fullmatch(r'vercel_blob_rw_([A-Za-z0-9]+)_.+', self.token)
        if not match:
            raise ValueError('Connect a private Vercel Blob store to enable saved data.')
        self.host = match[1].lower() + '.private.blob.vercel-storage.com'
        environment = 'production' if os.getenv('VERCEL_ENV') == 'production' else 'preview'
        self.path = 'venture-radar/' + environment + '.sqlite3'

    def read(self):
        req = urllib.request.Request('https://' + self.host + '/' + self.path + '?cache=0',
                                     headers={'Authorization': 'Bearer ' + self.token})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = response.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError('Workspace exceeds 8 MB; export and migrate to PostgreSQL.')
                etag = response.headers.get('ETag')
                if not etag:
                    raise RuntimeError('Storage returned no ETag; refusing unsafe writes.')
                return data, etag
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise RuntimeError('Private storage read failed (HTTP %s).' % e.code) from None

    def write(self, data, etag):
        if len(data) > LIMIT:
            raise ValueError('Workspace exceeds 8 MB; export and migrate to PostgreSQL.')
        headers = {'Authorization': 'Bearer ' + self.token, 'x-api-version': '12',
                   'x-vercel-blob-access': 'private', 'x-add-random-suffix': '0',
                   'x-allow-overwrite': '1' if etag else '0',
                   'x-content-type': 'application/vnd.sqlite3', 'Content-Type': 'application/octet-stream'}
        if etag:
            headers['x-if-match'] = etag
        req = urllib.request.Request('https://vercel.com/api/blob?' + urllib.parse.urlencode({'pathname': self.path}),
                                     data=data, headers=headers, method='PUT')
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)['etag']
        except urllib.error.HTTPError as e:
            if e.code in (409, 412):
                raise Conflict() from None
            raw = e.read(2000)
            if not etag and e.code == 400 and b'already exists' in raw.lower():
                raise Conflict() from None
            raise RuntimeError('Private storage write failed (HTTP %s).' % e.code) from None


class CloudStore:
    READS = {'settings', 'investments', 'profiles', 'edges', 'jobs'}
    WRITES = {'set_settings', 'add_investment', 'review', 'save_profile', 'investor_notes',
              'add_edge', 'delete_edge', 'enqueue', 'claim', 'finish', 'schedule_if_due',
              'expire_jobs', 'apply_batch'}

    def __init__(self, blob=None):
        self.blob = blob or Blob()
        self.cache = None

    def _run(self, method, args, kwargs, mutate):
        for attempt in range(6):
            raw, etag = self.blob.read() if mutate or self.cache is None else self.cache
            with tempfile.TemporaryDirectory(prefix='radar-') as directory:
                if raw:
                    (Path(directory) / 'radar.sqlite3').write_bytes(raw)
                local = Store(directory)
                if raw is None:
                    local.seed()
                    local.set_settings({'schedule_enabled': True, 'interval_hours': 24})
                result = getattr(local, method)(*args, **kwargs)
                if mutate or raw is None:
                    snapshot = Path(directory) / 'snapshot.sqlite3'
                    local.backup(snapshot)
                    new = snapshot.read_bytes()
                    try:
                        etag = self.blob.write(new, etag)
                    except Conflict:
                        self.cache = None
                        time.sleep(random.uniform(.03, .15) * (attempt + 1))
                        continue
                    raw = new
                self.cache = (raw, etag)
                return result
        raise ValueError('Workspace changed concurrently. Please retry your action.')

    def __getattr__(self, name):
        if name not in self.READS | self.WRITES:
            raise AttributeError(name)
        return lambda *a, **kw: self._run(name, a, kw, name in self.WRITES)


def process_one(store=None):
    if not os.getenv('OPENAI_API_KEY'):
        return
    from .research import OpenAI, run_research
    store = store or CloudStore()
    store.expire_jobs()
    job = store.claim()
    if not job:
        return
    try:
        client = OpenAI(deadline=time.monotonic() + 220)
        store.finish(job['id'], result=run_research(store, job, client))
    except Exception as e:
        message = str(e)[:800] if isinstance(e, ValueError) else 'Research failed; check service configuration and retry manually.'
        store.finish(job['id'], error=message)
