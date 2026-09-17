"""Durable local journal. Place database on a persistent disk, outside git."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seeds (
            profile_url TEXT PRIMARY KEY, name TEXT, location TEXT, description TEXT,
            source_url TEXT, collected_at TEXT, state TEXT DEFAULT 'DISCOVERED');
        CREATE TABLE IF NOT EXISTS records (
            company_key TEXT PRIMARY KEY, name_key TEXT NOT NULL, payload TEXT NOT NULL,
            decision TEXT NOT NULL, evaluation TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mirrors (
            company_key TEXT, destination TEXT, state TEXT, row_number INTEGER,
            error TEXT, updated_at TEXT, PRIMARY KEY(company_key,destination));
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, occurred_at TEXT, kind TEXT, detail TEXT);
        ''')
        for k,v in {'command':'STOP','target':'2000','milestone':'500','state':'IDLE'}.items():
            self.db.execute('INSERT OR IGNORE INTO control VALUES (?,?)',(k,v))
        self.db.commit()

    def get(self,k,default=None):
        r=self.db.execute('SELECT value FROM control WHERE key=?',(k,)).fetchone()
        return r[0] if r else default

    def set(self,k,v):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(k,str(v)))

    def event(self,kind,detail):
        with self.db:
            self.db.execute('INSERT INTO events(occurred_at,kind,detail) VALUES (?,?,?)',
                            (now(),kind,json.dumps(detail,ensure_ascii=False)))

    def seed(self,**row):
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO seeds
              (profile_url,name,location,description,source_url,collected_at)
              VALUES (:profile_url,:name,:location,:description,:source_url,:collected_at)''',row)

    def record(self,row):
        from .policy import company_key,name_key,qualification
        result=qualification(row)
        if not row.get('website'):
            raise ValueError('Evidence records require an official website; use seeds for unknown identities')
        k=company_key(row)
        with self.db:
            self.db.execute('''INSERT INTO records VALUES (?,?,?,?,?,?)
                ON CONFLICT(company_key) DO UPDATE SET payload=excluded.payload,
                decision=excluded.decision,evaluation=excluded.evaluation,updated_at=excluded.updated_at''',
                (k,name_key(row.get('company_name')),json.dumps(row,ensure_ascii=False),
                 result['decision'],json.dumps(result,ensure_ascii=False),now()))
        return result

    def mark(self,k,dest,state,row_number=None,error=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO mirrors VALUES (?,?,?,?,?,?)',
                            (k,dest,state,row_number,error,now()))

    def completed(self):
        return [r[0] for r in self.db.execute('''SELECT company_key FROM mirrors
          WHERE state='VERIFIED_NEW' GROUP BY company_key HAVING count(DISTINCT destination)=2''')]

    def status(self):
        return {'control':dict(self.db.execute('SELECT key,value FROM control')),
                'seeds':self.db.execute('SELECT count(*) FROM seeds').fetchone()[0],
                'qualification':dict(self.db.execute('SELECT decision,count(*) FROM records GROUP BY decision')),
                'both_verified_new':len(self.completed()),
                'mirror_states':dict(self.db.execute('SELECT state,count(*) FROM mirrors GROUP BY state')),
                'recent_events':[dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 10')]}
