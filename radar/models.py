"""Validation and the shared strict extraction schema."""
import datetime as dt
import math
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def norm(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower())


def url(value):
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('A public http(s) source URL is required.')
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith('utm_')]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip('/'), urlencode(query), ''))


def string(value, name, limit=3000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'Invalid {name}.')
    return value.strip()


def number(value, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
        raise ValueError(f'{name} must be a nonnegative number or null.')
    return value


def source(value):
    return {'url': url(value['url']), 'title': string(value.get('title', ''), 'source title'),
            'publisher': string(value.get('publisher', ''), 'publisher', 200),
            'evidence': string(value.get('evidence', ''), 'source evidence', 1500)}


def investment(data):
    d = dict(data)
    for k in ('company', 'round', 'announced_on', 'sector', 'notes'):
        d[k] = string(d.get(k, ''), k)
    if not d['company'] or not d['round']:
        raise ValueError('Company and round are required.')
    when = dt.date.fromisoformat(d['announced_on'])
    if when > dt.date.today():
        raise ValueError('Future announcements are not completed investments.')
    for k, choices, default in [('kind', ['equity', 'grant', 'debt', 'acquisition'], 'equity'),
                                ('amount_basis', ['exact', 'at_least', 'approximate', 'undisclosed', 'cumulative'], 'exact'),
                                ('valuation_kind', ['pre-money', 'post-money', 'unspecified'], 'unspecified')]:
        d[k] = d.get(k, default)
        if d[k] not in choices:
            raise ValueError(f'Invalid {k}.')
    d['currency'] = string(d.get('currency', 'USD'), 'currency', 3).upper()
    if not re.fullmatch('[A-Z]{3}', d['currency']):
        raise ValueError('Currency must be a three-letter code.')
    for k in ('amount', 'valuation_usd'):
        d[k] = number(d.get(k), k)
    if (d['amount'] is None) != (d['amount_basis'] == 'undisclosed'):
        raise ValueError('Use undisclosed for a null amount; otherwise select an amount basis.')
    d['amount_usd'] = d['amount'] if d['currency'] == 'USD' else None
    d['investors'] = []
    for inv in data.get('investors', []):
        name = string(inv.get('name', ''), 'investor name', 200)
        if not name:
            raise ValueError('Investor names cannot be empty.')
        role = inv.get('role', 'participant')
        if role not in ('lead', 'participant', 'grantor', 'acquirer'):
            raise ValueError('Invalid investor role.')
        d['investors'].append({'name': name, 'role': role, 'check_usd': number(inv.get('check_usd'), 'investor check')})
    if not d['investors']:
        raise ValueError('At least one named investor or grantor is required.')
    d['sources'] = [source(s) for s in d.get('sources', [])]
    if not d['sources']:
        raise ValueError('At least one source is required.')
    return {k: d[k] for k in ('company', 'round', 'announced_on', 'sector', 'notes', 'kind', 'amount_basis',
                              'valuation_kind', 'currency', 'amount', 'amount_usd', 'valuation_usd', 'investors', 'sources')}


def profile(data):
    d = {k: string(data.get(k, ''), k) for k in ('name', 'thesis', 'partners', 'stage', 'geography', 'fit_notes')}
    if not d['name']:
        raise ValueError('Investor name required.')
    d['sources'] = [source(s) for s in data.get('sources', [])]
    if not d['sources']:
        raise ValueError('Investor profiles require sources.')
    return d


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


S = {'type': 'string'}
N = {'type': ['number', 'null']}
SOURCE = obj({'url': S, 'title': S, 'publisher': S, 'evidence': S})
INVESTMENT = obj({**{k: S for k in ('company', 'round', 'announced_on', 'sector', 'notes', 'currency')},
    'kind': {'type': 'string', 'enum': ['equity', 'grant', 'debt', 'acquisition']},
    'amount_basis': {'type': 'string', 'enum': ['exact', 'at_least', 'approximate', 'undisclosed', 'cumulative']},
    'valuation_kind': {'type': 'string', 'enum': ['pre-money', 'post-money', 'unspecified']},
    'amount': N, 'valuation_usd': N,
    'investors': {'type': 'array', 'items': obj({'name': S, 'role': {'type': 'string', 'enum': ['lead', 'participant', 'grantor', 'acquirer']}, 'check_usd': N})},
    'sources': {'type': 'array', 'items': SOURCE}})
PROFILE = obj({**{k: S for k in ('name', 'thesis', 'partners', 'stage', 'geography', 'fit_notes')}, 'sources': {'type': 'array', 'items': SOURCE}})
EXTRACTION = obj({'investments': {'type': 'array', 'items': INVESTMENT}, 'profiles': {'type': 'array', 'items': PROFILE}})
