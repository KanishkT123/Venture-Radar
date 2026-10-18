"""Bounded access to Dealroom's public discovery feed (no account required)."""
import json
import urllib.parse
import urllib.request

DEALROOM_URL = 'https://dealroom.co/api/live-signals?limit=20'


class DealroomRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != 'https' or target.hostname != 'dealroom.co'
                or target.username or target.password or target.port not in (None, 443)):
            raise ValueError('Unexpected Dealroom feed redirect.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def dealroom_signals():
    """Keep source labels verbatim; relative dates and amounts need verification."""
    request = urllib.request.Request(DEALROOM_URL, headers={
        'User-Agent': 'VentureRadar/1.0', 'Accept': 'application/json'})
    with urllib.request.build_opener(DealroomRedirect()).open(request, timeout=12) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('Dealroom feed exceeded the response limit.')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('signals'), list):
        raise ValueError('Unexpected Dealroom feed format.')
    signals, seen = [], set()
    for item in data['signals']:
        if not isinstance(item, dict) or item.get('type') != 'round':
            continue
        # Ignore unknown fields and nested content; cap text sent to the model.
        lead = {key: item[key].strip()[:1000] if isinstance(item.get(key), str) else ''
                for key in ('type', 'title', 'city', 'meta', 'value', 'time')}
        identity = tuple(lead.values())
        if not lead['title'] or identity in seen:
            continue
        signals.append(lead)
        seen.add(identity)
        if len(signals) == 20:
            break
    fetched = data.get('fetchedAt')
    return {'url': DEALROOM_URL, 'fetched_at': fetched[:100] if isinstance(fetched, str) else None,
            'signals': signals}
