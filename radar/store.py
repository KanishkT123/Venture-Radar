"""SQLite persistence; each method owns its transaction and connection."""
import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .models import investment, profile, norm

DEFAULTS = {'venture': 'Cicada Health', 'focus': 'Saliva biosensing, hormone monitoring, sleep and circadian health, wearable diagnostics, electrochemical aptamer sensors',
            'schedule_enabled': False, 'interval_hours': 6, 'lookback_days': 30,
            'half_life_days': 180, 'size_weight': 0.55, 'major_threshold_usd': 10000000,
            'max_runs_per_day': 8, 'feed_query': '(biosensor OR "hormone monitoring" OR "sleep technology") (funding OR investment OR grant)',
            'last_scheduled_at': 0}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'radar.sqlite3'
        with self.db() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS investments(id TEXT PRIMARY KEY, company_key TEXT, round_key TEXT,
                announced_on TEXT, status TEXT, duplicate_of TEXT, data TEXT, created_at REAL, updated_at REAL);
            CREATE INDEX IF NOT EXISTS event_match ON investments(company_key, round_key);
            CREATE TABLE IF NOT EXISTS profiles(name_key TEXT PRIMARY KEY, data TEXT, notes TEXT DEFAULT '', watched INTEGER DEFAULT 0, updated_at REAL);
            CREATE TABLE IF NOT EXISTS partner_profiles(firm_key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL, draft_subject TEXT, draft_message TEXT);
            CREATE TABLE IF NOT EXISTS investor_events(id TEXT PRIMARY KEY, starts_on TEXT NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT,
                created_at REAL, started_at REAL, ended_at REAL, attempts INTEGER DEFAULT 0, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, job_id TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, event TEXT, detail TEXT, created_at REAL);
            ''')
            c.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('settings', json.dumps(DEFAULTS)))
            c.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('schema_version', '1'))

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=20000')
        try:
            with c:
                yield c
        finally:
            c.close()

    def settings(self):
        with self.db() as c:
            return {**DEFAULTS, **json.loads(c.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])}

    def set_settings(self, values):
        d = self.settings()
        for k, v in values.items():
            if k not in DEFAULTS or k == 'last_scheduled_at':
                continue
            d[k] = v
        for k, lo, hi in [('interval_hours', 1, 168), ('lookback_days', 1, 730), ('half_life_days', 1, 1825),
                          ('size_weight', 0, 1), ('major_threshold_usd', 0, 1e12), ('max_runs_per_day', 1, 100)]:
            v = d[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f'{k} must be between {lo} and {hi}.')
        if type(d['schedule_enabled']) is not bool:
            raise ValueError('schedule_enabled must be true or false.')
        for k in ('focus', 'venture', 'feed_query'):
            if not isinstance(d[k], str) or not 1 <= len(d[k]) <= 2000:
                raise ValueError(f'Invalid {k}.')
        with self.db() as c:
            c.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(d),))
        return d

    def investments(self):
        with self.db() as c:
            return [{**json.loads(r['data']), **{k: r[k] for k in ('id', 'status', 'duplicate_of', 'created_at', 'updated_at')}}
                    for r in c.execute('SELECT * FROM investments ORDER BY announced_on DESC, created_at DESC')]

    def add_investment(self, raw, status='needs_review', replaces=None):
        d = investment(raw)
        now, match = time.time(), None
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if replaces:
                if not c.execute('SELECT 1 FROM investments WHERE id=?', (replaces,)).fetchone():
                    raise ValueError('Original record not found.')
                match = replaces
            rows = c.execute('SELECT * FROM investments WHERE company_key=? AND round_key=?', (norm(d['company']), norm(d['round']))).fetchall()
            for r in rows:
                if replaces:
                    break
                gap = abs((dt.date.fromisoformat(r['announced_on']) - dt.date.fromisoformat(d['announced_on'])).days)
                if gap <= 14:
                    old = json.loads(r['data'])
                    same = all(old[k] == d[k] for k in ('amount', 'currency', 'valuation_usd', 'valuation_kind', 'kind', 'amount_basis'))
                    same = same and sorted((norm(x['name']), x['role'], x['check_usd']) for x in old['investors']) == sorted((norm(x['name']), x['role'], x['check_usd']) for x in d['investors'])
                    if same:
                        old['sources'] = list({s['url']: s for s in old['sources'] + d['sources']}.values())
                        c.execute('UPDATE investments SET data=?, updated_at=? WHERE id=?', (json.dumps(old), now, r['id']))
                        return r['id'], False
                    if r['status'] != 'rejected':
                        match = r['id']
            ident = str(uuid.uuid4())
            c.execute('INSERT INTO investments VALUES(?,?,?,?,?,?,?,?,?)', (ident, norm(d['company']), norm(d['round']), d['announced_on'],
                      'needs_review' if match else status, match, json.dumps(d), now, now))
            c.execute('INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)', ('investment.added', ident, now))
        return ident, True

    def review(self, ident, status):
        if status not in ('confirmed', 'rejected', 'needs_review'):
            raise ValueError('Invalid review status.')
        with self.db() as c:
            row = c.execute('SELECT * FROM investments WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('Investment not found.')
            if status == 'confirmed':
                # Confirming a revision (or restoring its ancestor) must not leave
                # two members of the same revision family in headline metrics.
                related = {ident}
                pairs = c.execute('SELECT id,duplicate_of FROM investments WHERE duplicate_of IS NOT NULL').fetchall()
                while True:
                    expanded = related | {x for r in pairs if r['id'] in related or r['duplicate_of'] in related for x in (r['id'], r['duplicate_of'])}
                    if expanded == related:
                        break
                    related = expanded
                for other in related - {ident}:
                    c.execute("UPDATE investments SET status='rejected',updated_at=? WHERE id=?", (time.time(), other))
            c.execute('UPDATE investments SET status=?,updated_at=? WHERE id=?', (status, time.time(), ident))
            c.execute('INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)', ('investment.' + status, ident, time.time()))

    def save_profile(self, raw):
        d = profile(raw)
        with self.db() as c:
            c.execute('''INSERT INTO profiles(name_key,data,updated_at) VALUES(?,?,?)
                         ON CONFLICT(name_key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at''',
                      (norm(d['name']), json.dumps(d), time.time()))

    def profiles(self):
        with self.db() as c:
            return [{**json.loads(r['data']), 'notes': r['notes'], 'watched': bool(r['watched']), 'updated_at': r['updated_at']}
                    for r in c.execute('SELECT * FROM profiles')]

    def investor_notes(self, name, notes, watched):
        if not isinstance(notes, str) or len(notes) > 10000 or type(watched) is not bool:
            raise ValueError('Invalid investor notes.')
        with self.db() as c:
            if not c.execute('SELECT 1 FROM profiles WHERE name_key=?', (norm(name),)).fetchone():
                d = {'name': name, 'thesis': '', 'partners': '', 'stage': '', 'geography': '', 'fit_notes': '', 'sources': []}
                c.execute('INSERT INTO profiles(name_key,data,updated_at) VALUES(?,?,?)', (norm(name), json.dumps(d), time.time()))
            c.execute('UPDATE profiles SET notes=?,watched=? WHERE name_key=?', (notes, int(watched), norm(name)))

    def partner_profiles(self):
        with self.db() as c:
            return [{**json.loads(r['data']), 'updated_at': r['updated_at'],
                     'saved_draft': {'subject': r['draft_subject'], 'message': r['draft_message']} if r['draft_subject'] is not None else None}
                    for r in c.execute('SELECT * FROM partner_profiles')]

    def save_partner_profile(self, raw):
        from .partners import partner_profile
        data = partner_profile(raw)
        with self.db() as c:
            c.execute("""INSERT INTO partner_profiles(firm_key,data,updated_at) VALUES(?,?,?)
                         ON CONFLICT(firm_key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at""",
                      (norm(data['firm']), json.dumps(data), time.time()))

    def save_partner_draft(self, firm, subject, message):
        from .models import string
        firm = string(firm, 'investor', 200)
        subject = string(subject, 'draft subject', 200)
        message = string(message, 'draft message', 10000)
        with self.db() as c:
            if not c.execute('UPDATE partner_profiles SET draft_subject=?,draft_message=? WHERE firm_key=?',
                             (subject, message, norm(firm))).rowcount:
                raise ValueError('Create a partner profile before saving a draft.')

    def edges(self):
        with self.db() as c:
            return [{**json.loads(r['data']), 'id': r['id']} for r in c.execute('SELECT * FROM edges')]

    def add_edge(self, d):
        from .models import string, url
        edge = {k: string(d.get(k, ''), k, 2000) for k in ('from_name', 'to_name', 'relationship', 'evidence')}
        if not all(edge[k] for k in ('from_name', 'to_name', 'relationship', 'evidence')):
            raise ValueError('Relationship endpoints, description, and evidence are required.')
        edge['confirmed'] = d.get('confirmed', False) is True
        edge['strength'] = float(d.get('strength', 0.6))
        if not 0 < edge['strength'] <= 1:
            raise ValueError('Strength must be greater than 0 and at most 1.')
        edge['source_url'] = url(d['source_url']) if d.get('source_url') else ''
        edge['updated_at'] = time.time()
        with self.db() as c:
            ident = str(uuid.uuid4())
            c.execute('INSERT INTO edges VALUES(?,?)', (ident, json.dumps(edge)))
        return ident

    def delete_edge(self, ident):
        with self.db() as c:
            c.execute('DELETE FROM edges WHERE id=?', (ident,))

    def enqueue(self, kind='discovery', payload=None, delivery_id=None):
        if kind not in ('discovery', 'investor', 'partner', 'events'):
            raise ValueError('Invalid workflow.')
        if kind == 'partner' and (not isinstance((payload or {}).get('investor'), str) or not 1 <= len(payload['investor'].strip()) <= 200):
            raise ValueError('Investor name required.')
        limit = self.settings()['max_runs_per_day']
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if delivery_id:
                old = c.execute('SELECT job_id FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
                if old:
                    return old[0]
            if kind == 'events':
                active = c.execute("SELECT id FROM jobs WHERE kind='events' AND status IN ('queued','running') LIMIT 1").fetchone()
                if active:
                    return active['id']
            if kind == 'partner':
                for row in c.execute("SELECT id,payload FROM jobs WHERE kind='partner' AND status IN ('queued','running')"):
                    if norm(json.loads(row['payload'])['investor']) == norm(payload['investor']):
                        return row['id']
            # Cap queued + executed jobs to bound repeated webhook/API spending.
            count = c.execute('SELECT count(*) FROM jobs WHERE created_at>?', (time.time() - 86400,)).fetchone()[0]
            if count >= limit:
                raise ValueError('Daily research-run limit reached. Change the limit in settings if needed.')
            ident = str(uuid.uuid4())
            c.execute('INSERT INTO jobs(id,kind,payload,status,created_at) VALUES(?,?,?,?,?)', (ident, kind, json.dumps(payload or {}), 'queued', time.time()))
            if delivery_id:
                c.execute('INSERT INTO deliveries VALUES(?,?,?)', (delivery_id, ident, time.time()))
            return ident

    def save_investor_events(self, rows):
        from .events import MAX_EVENTS, event, tracked_firms
        if not isinstance(rows, list) or len(rows) > MAX_EVENTS:
            raise ValueError('Invalid event batch size.')
        firms = tracked_firms(self)
        validated = [event(row, firms) for row in rows]
        now = time.time()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in validated:
                # Alternate registration/source URLs merge, but recurring dates
                # and differently named conference sessions remain separate.
                key = '|'.join((norm(item['title']), item['starts_on'], norm(item['city'])))
                ident = hashlib.sha256(key.encode()).hexdigest()
                old = c.execute('SELECT data FROM investor_events WHERE id=?', (ident,)).fetchone()
                if old:
                    previous = json.loads(old['data'])
                    updated_firms = {norm(f['firm']) for f in item['firms']}
                    item['firms'] = [f for f in previous['firms'] if norm(f['firm']) not in updated_firms] + item['firms']
                    item['sources'] = list({s['url']: s for s in previous['sources'] + item['sources']}.values())
                c.execute('INSERT OR REPLACE INTO investor_events VALUES(?,?,?,?)',
                          (ident, item['starts_on'], json.dumps(item), now))
            c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('events_last_checked', json.dumps(now)))

    def investor_events(self):
        from .events import MA_TIMEZONE, WINDOW_DAYS, tracked_firms
        if MA_TIMEZONE is None:
            return {'events': [], 'last_checked_at': None, 'unavailable': 'Install IANA timezone data on the server to enable Massachusetts events. See README setup instructions.'}
        now = dt.datetime.now(MA_TIMEZONE)
        today = now.date()
        firms = {norm(name) for name in tracked_firms(self)}
        with self.db() as c:
            saved = c.execute("SELECT value FROM meta WHERE key='events_last_checked'").fetchone()
            rows = c.execute('SELECT * FROM investor_events WHERE starts_on>=? AND starts_on<=? ORDER BY starts_on,id',
                             (today.isoformat(), (today + dt.timedelta(days=WINDOW_DAYS)).isoformat())).fetchall()
        events = []
        for row in rows:
            item = json.loads(row['data'])
            item['firms'] = [f for f in item['firms'] if norm(f['firm']) in firms]
            if item['status'] != 'scheduled' or not item['firms']:
                continue
            end_day = item['ends_on'] or item['starts_on']
            end_time = item['end_time'] or (item['start_time'] if not item['ends_on'] else None)
            if end_time and dt.datetime.fromisoformat(end_day+'T'+end_time).replace(tzinfo=MA_TIMEZONE) <= now:
                continue
            events.append({**item, 'id': row['id'], 'updated_at': row['updated_at']})
        events.sort(key=lambda e: (e['starts_on'], e['start_time'] or '23:59', e['title']))
        return {'events': events, 'last_checked_at': json.loads(saved['value']) if saved else None}

    def jobs(self):
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT id,kind,payload,status,created_at,started_at,ended_at,attempts,result,error FROM jobs ORDER BY created_at DESC LIMIT 40')]
            for row in rows:
                row['target_investor'] = json.loads(row.pop('payload')).get('investor')
            return rows

    def recover(self):
        with self.db() as c:
            c.execute("UPDATE jobs SET status=CASE WHEN attempts<2 THEN 'queued' ELSE 'failed' END,error='Interrupted by shutdown; research may incur a repeated API charge.' WHERE status='running'")

    def expire_jobs(self):
        with self.db() as c:
            c.execute("UPDATE jobs SET status='failed',ended_at=?,error='Execution interrupted or timed out. Retry manually; the previous request may have been billed.' WHERE status='running' AND started_at<?", (time.time(), time.time()-600))

    def apply_batch(self, events, profiles):
        results = [self.add_investment(event) for event in events]
        for item in profiles:
            self.save_profile(item)
        return results

    def claim(self, ident=None):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute("SELECT * FROM jobs WHERE status='queued' AND (? IS NULL OR id=?) ORDER BY created_at LIMIT 1", (ident, ident)).fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='running',started_at=?,attempts=attempts+1 WHERE id=?", (time.time(), r['id']))
            return {**dict(r), 'payload': json.loads(r['payload'])}

    def finish(self, ident, result=None, error=None):
        with self.db() as c:
            c.execute('UPDATE jobs SET status=?,ended_at=?,result=?,error=? WHERE id=?',
                      ('failed' if error else 'completed', time.time(), json.dumps(result) if result is not None else None, error, ident))

    def schedule_if_due(self):
        s = self.settings()
        if s['schedule_enabled'] and time.time() - s['last_scheduled_at'] >= s['interval_hours'] * 3600:
            with self.db() as c:
                active = c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone()
            if not active:
                self.enqueue()
                s['last_scheduled_at'] = time.time()
                with self.db() as c:
                    c.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(s),))

    def seed(self):
        with self.db() as c:
            if c.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
                return
        seed = json.loads((Path(__file__).parent.parent / 'examples' / 'starter-data.json').read_text())
        for row in seed['investments']:
            self.add_investment(row, 'confirmed')
        for row in seed['profiles']:
            self.save_profile(row)
        with self.db() as c:
            c.execute("INSERT OR IGNORE INTO meta VALUES('seeded','1')")

    def backup(self, path):
        with self.db() as c:
            target = sqlite3.connect(path)
            try:
                c.backup(target)
            finally:
                target.close()
