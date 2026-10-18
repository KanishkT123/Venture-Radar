"""Send a signed news lead. Reuse --delivery on retries. No third-party modules."""
import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request
import uuid

p = argparse.ArgumentParser()
p.add_argument('endpoint', help='https://your-host/api/webhooks/news')
p.add_argument('--url', required=True)
p.add_argument('--title', default='')
p.add_argument('--summary', default='')
p.add_argument('--delivery', default=None, help='Stable ID for retries')
a = p.parse_args()
secret = os.environ.get('RADAR_WEBHOOK_SECRET')
if not secret:
    p.error('Set RADAR_WEBHOOK_SECRET in the sender environment.')
stamp, delivery = str(int(time.time())), a.delivery or str(uuid.uuid4())
body = json.dumps({'url': a.url, 'title': a.title, 'summary': a.summary}).encode()
signature = hmac.new(secret.encode(), stamp.encode()+b'.'+delivery.encode()+b'.'+body, hashlib.sha256).hexdigest()
headers = {'Content-Type':'application/json', 'X-Radar-Timestamp':stamp,
           'X-Radar-Delivery':delivery, 'X-Radar-Signature':'sha256='+signature}
# Optional platform-level access, only after the project owner authorizes it.
if os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET'):
    headers['x-vercel-protection-bypass'] = os.environ['VERCEL_AUTOMATION_BYPASS_SECRET']
request = urllib.request.Request(a.endpoint, data=body, headers=headers)
print('Delivery ID:', delivery)
with urllib.request.urlopen(request, timeout=45) as response:
    print(response.status, response.read().decode())
