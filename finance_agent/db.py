import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


WATCHES = [
    ('中电金信', ['Gientech', '中电源启'], '金融科技'),
    ('恒生电子', ['Hundsun'], '金融科技'),
    ('蚂蚁集团', ['Ant Group', '蚂蚁数科'], '金融科技'),
    ('腾讯金融科技', ['Tencent', '腾讯云', '财付通'], '金融科技'),
    ('工商银行', ['ICBC', '工行'], '银行'),
    ('招商银行', ['China Merchants Bank', '招行'], '银行'),
    ('摩根大通银行', ['JPMorgan', 'JPMORGAN CHASE BANK', 'J.P. Morgan'], '银行'),
    ('美国银行', ['Bank of America'], '银行'),
    ('花旗银行', ['Citibank', 'Citigroup', 'Citi'], '银行'),
]
SOURCES = [
    ('gientech', '中电金信 · 公司动态', 'html', 'https://www.gientech.com/news.html', '金融科技',
     {'selector': 'a[href]', 'link_contains': '/detail/', 'content_selector': '.m3nContent', 'company': '中电金信'}),
    ('fed', '美联储 · 监管与政策', 'rss', 'https://www.federalreserve.gov/feeds/press_all.xml', '银行', {}),
    ('bis', '国际清算银行 · 新闻', 'rss', 'https://www.bis.org/doclist/all_pressrels.rss', '银行', {}),
    ('ecb', '欧洲央行 · 新闻', 'rss', 'https://www.ecb.europa.eu/rss/press.html', '银行', {}),
    ('fdic-jpm', 'FDIC · 摩根大通银行', 'fdic', 'https://api.fdic.gov/banks/financials', '银行', {'cert': 628, 'company': '摩根大通银行'}),
    ('fdic-bac', 'FDIC · 美国银行', 'fdic', 'https://api.fdic.gov/banks/financials', '银行', {'cert': 3510, 'company': '美国银行'}),
    ('fdic-citi', 'FDIC · 花旗银行', 'fdic', 'https://api.fdic.gov/banks/financials', '银行', {'cert': 7213, 'company': '花旗银行'}),
]


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('FINANCE_DB', Path(__file__).resolve().parents[1] / 'data/research.sqlite'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS watches (id INTEGER PRIMARY KEY, name TEXT UNIQUE, aliases TEXT, industry TEXT);
            CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, kind TEXT, url TEXT, industry TEXT, config TEXT, enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, source_id TEXT, title TEXT, url TEXT UNIQUE, body TEXT, summary TEXT, category TEXT, industry TEXT, companies TEXT, published_at TEXT, fetched_at TEXT, updated_at TEXT, content_hash TEXT);
            CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, article_id TEXT, content_hash TEXT, body TEXT, recorded_at TEXT, UNIQUE(article_id,content_hash));
            CREATE TABLE IF NOT EXISTS metrics (id TEXT PRIMARY KEY, source_id TEXT, company TEXT, metric TEXT, period TEXT, period_type TEXT, value REAL, unit TEXT, currency TEXT, source_url TEXT, evidence TEXT, verified INTEGER, fetched_at TEXT);
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, source_id TEXT, started_at TEXT, finished_at TEXT, status TEXT, added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, unchanged INTEGER DEFAULT 0, metrics INTEGER DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, created_at TEXT, kind TEXT, actor TEXT, channel TEXT, group_hash TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0, due_at TEXT, error TEXT, created_at TEXT);
            CREATE INDEX IF NOT EXISTS article_date ON articles(published_at);
            CREATE INDEX IF NOT EXISTS usage_time ON usage(created_at);
            ''')
            if not c.execute("SELECT value FROM settings WHERE key='seeded'").fetchone():
                for name, aliases, industry in WATCHES:
                    c.execute('INSERT OR IGNORE INTO watches(name,aliases,industry) VALUES(?,?,?)', (name, encode(aliases), industry))
                for sid, name, kind, url, industry, config in SOURCES:
                    c.execute('INSERT OR IGNORE INTO sources(id,name,kind,url,industry,config) VALUES(?,?,?,?,?,?)', (sid, name, kind, url, industry, encode(config)))
                c.execute("INSERT INTO settings(key,value) VALUES('seeded','1')")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.conn() as c:
            return [dict(row) for row in c.execute(sql, args).fetchall()]

    def sources(self):
        rows = self.rows('SELECT * FROM sources ORDER BY rowid')
        for r in rows:
            r['config'] = json.loads(r['config'])
        return rows

    def watches(self):
        rows = self.rows('SELECT * FROM watches ORDER BY id')
        for r in rows:
            r['aliases'] = json.loads(r['aliases'])
        return rows

    def companies_for(self, text, force=''):
        lower = text.lower()
        return sorted({w['name'] for w in self.watches() if w['name'] == force or any(x.lower() in lower for x in [w['name']] + w['aliases'])} | ({force} if force else set()))

    def upsert_article(self, item):
        content_hash = hashlib.sha256((item['title'] + '\n' + item['body']).encode()).hexdigest()
        aid = hashlib.sha256(item['url'].encode()).hexdigest()[:24]
        with self.conn() as c:
            old = c.execute('SELECT content_hash FROM articles WHERE id=?', (aid,)).fetchone()
            if old and old['content_hash'] == content_hash:
                c.execute('UPDATE articles SET fetched_at=?,category=?,companies=?,industry=?,summary=?,published_at=COALESCE(?,published_at) WHERE id=?', (now(),item['category'],encode(item['companies']),item['industry'],item['summary'],item.get('published_at'),aid))
                return 'unchanged'
            item = dict(item, id=aid, content_hash=content_hash, fetched_at=now(), updated_at=now())
            item['companies'] = encode(item['companies'])
            fields = ['id','source_id','title','url','body','summary','category','industry','companies','published_at','fetched_at','updated_at','content_hash']
            c.execute(f"INSERT INTO articles({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) ON CONFLICT(id) DO UPDATE SET " + ','.join(f'{f}=excluded.{f}' for f in fields[1:]), [item.get(f) for f in fields])
            c.execute('INSERT OR IGNORE INTO versions(article_id,content_hash,body,recorded_at) VALUES(?,?,?,?)', (aid, content_hash, item['body'], now()))
            return 'updated' if old else 'added'

    def upsert_metric(self, item):
        fields = ['id','source_id','company','metric','period','period_type','value','unit','currency','source_url','evidence','verified','fetched_at']
        item = dict(item, fetched_at=now())
        item['id'] = hashlib.sha256('|'.join(str(item[x]) for x in ['source_id','company','metric','period','period_type','unit','currency']).encode()).hexdigest()[:24]
        with self.conn() as c:
            c.execute(f"INSERT INTO metrics({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) ON CONFLICT(id) DO UPDATE SET " + ','.join(f'{f}=excluded.{f}' for f in fields[1:]), [item.get(f) for f in fields])

    def articles(self, company='', industry='', q='', days=0, limit=250):
        clauses, args = [], []
        if company:
            clauses.append("EXISTS (SELECT 1 FROM json_each(a.companies) WHERE value=?)")
            args.append(company)
        if industry:
            clauses.append('a.industry=?'); args.append(industry)
        if days:
            clauses.append('a.published_at>=?'); args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
        if q:
            clauses.append('(a.title LIKE ? ESCAPE \'\\\' OR a.body LIKE ? ESCAPE \'\\\')')
            escaped = q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            args.extend(['%'+escaped+'%']*2)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.rows('SELECT a.*,s.name AS source_name FROM articles a LEFT JOIN sources s ON s.id=a.source_id' + where + ' ORDER BY COALESCE(a.published_at,a.fetched_at) DESC LIMIT ?', args+[limit])
        for r in rows:
            r['companies'] = json.loads(r['companies'])
        return rows

    def usage(self, kind, actor='local', channel='web', group='', detail=''):
        # Only pseudonymous audience identifiers, never raw prompts or messages.
        actor_hash = hashlib.sha256(actor.encode()).hexdigest()[:20]
        group_hash = hashlib.sha256(group.encode()).hexdigest()[:20] if group else ''
        with self.conn() as c:
            c.execute('INSERT INTO usage(created_at,kind,actor,channel,group_hash,detail) VALUES(?,?,?,?,?,?)', (now(), kind, actor_hash, channel, group_hash, detail))

    def reserve_model_call(self, daily_limit=25):
        day = now()[:10]
        with self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            used = c.execute("SELECT count(*) FROM usage WHERE kind='model_call' AND created_at>=?", (day,)).fetchone()[0]
            if used >= daily_limit:
                return False
            c.execute("INSERT INTO usage(created_at,kind,actor,channel,group_hash,detail) VALUES(?,'model_call','','model','','')", (now(),))
            return True

    def enqueue(self, jid, payload):
        with self.conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO jobs(id,payload,status,due_at,created_at) VALUES(?,?,'queued',?,?)", (jid,encode(payload),now(),now()))
            return bool(cur.rowcount)

    def claim_job(self):
        with self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            # Reclaim a job if a worker crashed after acquiring its five-minute lease.
            r = c.execute("SELECT * FROM jobs WHERE status IN ('queued','processing') AND due_at<=? ORDER BY created_at LIMIT 1", (now(),)).fetchone()
            if not r:
                return None
            lease = (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(timespec='seconds')
            c.execute("UPDATE jobs SET status='processing',attempts=attempts+1,due_at=? WHERE id=?", (lease,r['id']))
            result = dict(r); result['payload'] = json.loads(result['payload']); result['attempts'] += 1
            return result

    def finish_job(self, job, success, error=''):
        status = 'done' if success else ('failed' if job['attempts'] >= 4 else 'queued')
        due = (datetime.now(timezone.utc)+timedelta(seconds=30*2**job['attempts'])).isoformat(timespec='seconds')
        with self.conn() as c:
            c.execute('UPDATE jobs SET status=?,due_at=?,error=? WHERE id=?', (status,due,error,job['id']))
            if success:
                c.execute("UPDATE jobs SET payload='{}' WHERE id=?",(job['id'],))
