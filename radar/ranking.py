"""Transparent activity ranking and evidence-aware introduction paths."""
import datetime as dt
import math
from .models import norm


def rankings(investments, profiles, settings, today=None):
    today = today or dt.date.today()
    rows = {norm(p['name']): {**p, 'events': []} for p in profiles}
    for event in investments:
        if event['status'] != 'confirmed' or event['kind'] not in ('equity', 'grant') or event['amount_basis'] == 'cumulative':
            continue
        for inv in event['investors']:
            key = norm(inv['name'])
            item = rows.setdefault(key, {'name': inv['name'], 'events': [], 'notes': '', 'watched': False, 'sources': []})
            item['events'].append({**event, 'role': inv['role'], 'check_usd': inv['check_usd']})
    result = []
    for item in rows.values():
        events = item['events']
        biggest = max((e['amount_usd'] or 0 for e in events), default=0)
        latest = max((e['announced_on'] for e in events), default=None)
        age = max(0, (today - dt.date.fromisoformat(latest)).days) if latest else None
        recency = 2 ** (-age / settings['half_life_days']) if age is not None else 0
        # Fixed $1B reference, logarithmic sizing: $1M=.50, $100M=.83, $1B=1.
        size = min(1, math.log1p(biggest) / math.log1p(1_000_000_000))
        w = settings['size_weight']
        item.update(score=round(100 * (w * size + (1-w) * recency), 1),
                    biggest_round=biggest or None, latest=latest, deal_count=len(events),
                    size_component=round(size*100, 1), recency_component=round(recency*100, 1),
                    disclosed_checks=sum(e['check_usd'] or 0 for e in events),
                    grantor=bool(events) and all(e['kind']=='grant' for e in events))
        result.append(item)
    return sorted(result, key=lambda p: (-p['score'], p['name']))


def paths(target, edges, investments):
    """Rank <=3-hop directed paths. Strength is a heuristic, never a probability."""
    graph = {}
    for edge in edges:
        graph.setdefault(norm(edge['from_name']), []).append(edge)
    public_bridges = []
    for e in investments:
        if e['status'] != 'confirmed':
            continue
        for inv in e['investors']:
            if norm(inv['name']) == norm(target):
                bridge = {'from_name': e['company'], 'to_name': inv['name'], 'relationship': 'Portfolio funding relationship; willingness to introduce unverified',
                          'evidence': f"{e['round']}, {e['announced_on']}", 'source_url': e['sources'][0]['url'], 'confirmed': False, 'strength': 0.5}
                public_bridges.append(bridge)
                graph.setdefault(norm(e['company']), []).append(bridge)
    found = []

    def visit(node, chain, seen, strength):
        if node == norm(target) and chain:
            found.append({'edges': chain, 'score': round(100*strength*(0.8**(len(chain)-1)), 1),
                          'confirmed': all(e['confirmed'] for e in chain)})
            return
        if len(chain) == 3:
            return
        for e in graph.get(node, []):
            nxt = norm(e['to_name'])
            if nxt in seen:
                continue
            visit(nxt, chain+[e], seen | {nxt}, strength*e['strength'])
    visit(norm('You'), [], {norm('You')}, 1)
    found.sort(key=lambda p: (not p['confirmed'], -p['score']))
    return {'paths': found[:5], 'public_bridges': list({norm(e['from_name']): e for e in public_bridges}.values()),
            'explanation': 'Confirmed routes first; then relationship strengths multiplied with a 20% penalty per extra hop. A ranking heuristic, not a probability of an introduction.'}
