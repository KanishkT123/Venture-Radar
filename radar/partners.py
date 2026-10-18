"""Cited partner profiles and draft-only outreach."""
import datetime as dt
import json
import re
from .models import S, SOURCE, obj, norm, source, string, url

NULLABLE = {'type': ['string', 'null']}
URLS = {'type': 'array', 'items': S}
CLAIM = obj({'claim': S, 'source_urls': URLS})
PARTNER = obj({
    'firm': S, 'partner_name': NULLABLE, 'role': S, 'profile_url': NULLABLE, 'fit_assessment': S,
    'email': obj({'address': NULLABLE, 'status': {'type': 'string', 'enum': ['public_direct', 'public_general', 'not_found']}, 'source_url': NULLABLE, 'evidence': S}),
    'theses': {'type': 'array', 'items': CLAIM}, 'interests': {'type': 'array', 'items': CLAIM},
    'big_wins': {'type': 'array', 'items': obj({'company': S, 'outcome': S, 'partner_role': S, 'source_urls': URLS})},
    'writings': {'type': 'array', 'items': obj({'title': S, 'url': S, 'published_on': NULLABLE, 'authorship': S, 'summary': S, 'relevance': S})},
    'gaps': {'type': 'array', 'items': S}, 'draft': obj({'subject': S, 'message': S}),
    'sources': {'type': 'array', 'items': SOURCE}})


def partner_profile(raw, expected_firm=None, allowed=None):
    def items(key, limit=8):
        value = raw.get(key)
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError('Invalid partner profile ' + key + '.')
        return value
    data = {key: string(raw.get(key, ''), key) for key in ('firm', 'role', 'fit_assessment')}
    if not data['firm'] or (expected_firm and norm(data['firm']) != norm(expected_firm)):
        raise ValueError('Partner profile does not match the requested investor.')
    data['sources'] = [source(s) for s in items('sources', 30)]
    cited = {s['url'] for s in data['sources']}
    if not cited or (allowed is not None and not cited.issubset(allowed)):
        raise ValueError('Partner research contains missing or uncited evidence.')
    def cited_url(value):
        value = url(value)
        if value not in cited:
            raise ValueError('Partner detail uses an uncited source URL.')
        return value
    def claim_urls(row):
        links = row.get('source_urls')
        if not isinstance(links, list) or not 1 <= len(links) <= 8:
            raise ValueError('Partner claims require source URLs.')
        return [cited_url(u) for u in links]
    name = raw.get('partner_name')
    data['partner_name'] = string(name, 'partner name', 200) if name is not None else None
    data['profile_url'] = cited_url(raw['profile_url']) if raw.get('profile_url') else None
    if data['partner_name'] and (not data['profile_url'] or not data['role']):
        raise ValueError('A named partner requires a sourced role and profile URL.')
    email = raw['email']; status = email.get('status'); address = email.get('address'); link = email.get('source_url')
    evidence = string(email.get('evidence', ''), 'email evidence', 1500)
    if status not in ('public_direct', 'public_general', 'not_found'):
        raise ValueError('Invalid contact evidence status.')
    if status == 'not_found':
        if address is not None or link is not None:
            raise ValueError('Unverified email addresses must be omitted.')
    else:
        if not isinstance(address, str) or len(address) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", address):
            raise ValueError('Invalid publicly listed email address.')
        if not link or not evidence or (status == 'public_direct' and not data['partner_name']):
            raise ValueError('Email requires a public source and attribution.')
        link = cited_url(link)
    data['email'] = {'address': address, 'status': status, 'source_url': link, 'evidence': evidence}
    for key in ('theses', 'interests'):
        data[key] = [{'claim': string(row['claim'], key), 'source_urls': claim_urls(row)} for row in items(key)]
    data['big_wins'] = [{**{k: string(row[k], k) for k in ('company', 'outcome', 'partner_role')}, 'source_urls': claim_urls(row)} for row in items('big_wins', 5)]
    data['writings'] = []
    for row in items('writings', 6):
        published = row['published_on']
        if published is not None and dt.date.fromisoformat(published) > dt.date.today():
            raise ValueError('Writing publication date cannot be in the future.')
        data['writings'].append({**{k: string(row[k], k) for k in ('title', 'authorship', 'summary', 'relevance')}, 'url': cited_url(row['url']), 'published_on': published})
    data['gaps'] = [string(value, 'research gap', 1500) for value in items('gaps')]
    data['draft'] = {'subject': string(raw['draft']['subject'], 'draft subject', 200), 'message': string(raw['draft']['message'], 'draft message', 10000)}
    return data


def run_partner_research(store, job, client):
    from .research import read_skill, response_text, cited_urls
    firm = job['payload']['investor']; settings = store.settings()
    profile = next((p for p in store.profiles() if norm(p['name']) == norm(firm)), {})
    context = {'as_of': dt.date.today().isoformat(), 'firm': firm, 'venture': settings['venture'], 'focus': settings['focus'],
               'firm_context': {k: profile.get(k, '') for k in ('thesis', 'partners', 'stage', 'geography')},
               'tracked_companies': list(dict.fromkeys(e['company'] for e in store.investments() if e['status'] == 'confirmed' and any(norm(i['name']) == norm(firm) for i in e['investors'])))[:12]}
    instructions = read_skill('research-investor-partner')
    researched = client.create(instructions=instructions, input=json.dumps(context), tools=[{'type': 'web_search'}], tool_choice='required', max_tool_calls=8, max_output_tokens=10000)
    report, allowed = response_text(researched), cited_urls(researched)
    if not allowed:
        raise ValueError('Partner research returned no cited sources; the saved profile was retained.')
    extracted = client.create(instructions=instructions + '\nExtract only the cited report into the schema. Use only allowed source URLs and include every detail URL in sources. Preserve unknowns. The firm field must exactly match the requested firm. Return at most 8 theses, 8 interests, 5 wins, 6 writings, 8 gaps and 30 sources.',
                              input=json.dumps({'firm': firm, 'venture': settings['venture'], 'focus': settings['focus'], 'report': report, 'allowed_source_urls': sorted(allowed)}),
                              text={'format': {'type': 'json_schema', 'name': 'partner_profile', 'strict': True, 'schema': PARTNER}}, max_output_tokens=10000)
    data = partner_profile(json.loads(response_text(extracted)), firm, allowed)
    store.save_partner_profile(data)
    return {'investor': firm, 'partner_name': data['partner_name'], 'partner_profile': True, 'warnings': data['gaps'], 'source_urls': sorted(allowed), 'usage': [researched.get('usage'), extracted.get('usage')]}
