import asyncio
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from radar.cloud import CloudStore, Conflict
from api.index import app


class MemoryBlob:
    def __init__(self):
        self.data, self.etag = None, None
        self.lock = threading.Lock()
        self.read_barrier = None

    def read(self):
        with self.lock:
            result = self.data, self.etag
        if self.read_barrier:
            self.read_barrier.wait(timeout=5)
        return result

    def write(self, data, etag):
        with self.lock:
            if self.etag != etag:
                raise Conflict()
            self.data, self.etag = data, str(int(self.etag or '0')+1)
            return self.etag


class CloudTests(unittest.TestCase):
    def test_concurrent_writers_preserve_both_changes(self):
        blob = MemoryBlob()
        CloudStore(blob).settings()
        # Force two writers to read the same initial version.
        barrier = threading.Barrier(2, action=lambda: setattr(blob, 'read_barrier', None))
        blob.read_barrier = barrier
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(CloudStore(blob).investor_notes, 'Firm A', 'note A', True)
            b = pool.submit(CloudStore(blob).investor_notes, 'Firm B', 'note B', True)
            a.result(); b.result()
        notes = {p['name']: p['notes'] for p in CloudStore(blob).profiles()}
        self.assertEqual(notes['Firm A'], 'note A')
        self.assertEqual(notes['Firm B'], 'note B')

    def test_concurrent_job_claim_has_one_winner(self):
        blob = MemoryBlob()
        ident = CloudStore(blob).enqueue()
        barrier = threading.Barrier(2, action=lambda: setattr(blob, 'read_barrier', None))
        blob.read_barrier = barrier
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(lambda _: CloudStore(blob).claim(), range(2)))
        self.assertEqual([j['id'] for j in result if j], [ident])

    def test_asgi_auth_and_persistence(self):
        blob = MemoryBlob()
        async def request(path, method='GET', body=None, token='private'):
            sent=[]
            raw=json.dumps(body).encode() if body is not None else b''
            async def receive(): return {'type':'http.request','body':raw}
            async def send(message): sent.append(message)
            scope={'type':'http','method':method,'path':path,'query_string':b'',
                   'headers':[(b'host',b'radar.example'),(b'authorization',('Bearer '+token).encode()),
                              (b'content-type',b'application/json'),(b'content-length',str(len(raw)).encode())]}
            await app(scope,receive,send)
            return sent[0]['status'],json.loads(sent[1]['body'])
        with patch('api.index.CloudStore',side_effect=lambda:CloudStore(blob)), patch.dict(os.environ,{'RADAR_ACCESS_TOKEN':'private'}):
            self.assertEqual(asyncio.run(request('/api/state',token='wrong'))[0],401)
            self.assertEqual(asyncio.run(request('/api/settings','POST',{'venture':'Saved cloud venture'}))[0],200)
            status,data=asyncio.run(request('/api/state'))
            self.assertEqual(status,200)
            self.assertEqual(data['settings']['venture'],'Saved cloud venture')
            self.assertEqual(asyncio.run(request('/api/cron'))[0],401)


if __name__ == '__main__':
    unittest.main()
