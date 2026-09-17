"""No model imports, paid cloud services, email sending, or background AI loops."""
import argparse
import fcntl
import json
import time
from pathlib import Path
import requests
from .store import Store,now
from .discovery import fetch_directory,parse_directory
from .sync import Sheets,Mirror


def worker(store,config_path):
    # Config is private runtime data; no customer records in this repository.
    config=json.loads(Path(config_path).read_text())
    run_id=config['run_id']
    previous=store.get('run_id')
    if previous and previous!=run_id: raise ValueError('run_id must be stable across restarts')
    store.set('run_id',run_id)
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
    api=Sheets(config,AuthorizedSession(credentials))
    mirror=Mirror(store,api,run_id)
    remote=api.control_command()
    if remote is not None: store.set('command',remote)
    store.set('state','RUNNING' if store.get('command')=='START' else 'STOPPED')
    try:
        while store.get('command')=='START':
            remote=api.control_command()
            if remote=='STOP':
                store.set('command','STOP');store.set('state','STOPPED');break
            store.set('heartbeat',now())
            api.publish_status(store)
            completed=set(store.completed())
            target=min(2000,max(1,int(store.get('target','2000'))))
            if len(completed)>=target:
                store.set('state','TARGET_REACHED');store.set('command','STOP');break
            pending=store.db.execute("SELECT company_key,payload FROM records WHERE decision='PASS' ORDER BY updated_at").fetchall()
            candidates=[r for r in pending if r['company_key'] not in completed and not store.db.execute(
                "SELECT 1 FROM mirrors WHERE company_key=? AND state='EXISTING_OR_CONFLICT'",(r['company_key'],)).fetchone()]
            if not candidates:
                store.set('state','WAITING_FOR_EVIDENCE')
                # Return to the scheduler. Do not spend tokens or busy-poll indefinitely.
                break
            r=candidates[0]
            mirror.sync_one(r['company_key'],json.loads(r['payload']))
            count=len(store.completed())
            if count>=500 and not store.get('milestone_verified'):
                # Fresh two-book read, verify every completed record before expansion.
                from .sync import find
                snapshots={d:api.rows(d) for d in ('ssot','sacrifice')}
                for k in store.completed():
                    record=json.loads(store.db.execute('SELECT payload FROM records WHERE company_key=?',(k,)).fetchone()[0])
                    for d,rows in snapshots.items():
                        marked,dup=find(rows,d,record,f'leadgen:{run_id}:{k}')
                        if not marked or dup: raise RuntimeError('milestone_reconciliation_failed')
                store.set('milestone_verified',now())
                store.event('MILESTONE_VERIFIED',{'same_new_companies':count})
            # Rate limit, with STOP checked before the next network write.
            time.sleep(2)
    except Exception as exc:
        store.set('state','ERROR');store.event('ERROR',{'type':type(exc).__name__})
        raise
    finally:
        store.set('heartbeat',now())
        api.publish_status(store)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--db',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('status','start','stop'): sub.add_parser(name)
    d=sub.add_parser('discover');d.add_argument('--html')
    i=sub.add_parser('import-evidence');i.add_argument('path')
    w=sub.add_parser('work');w.add_argument('--config',required=True)
    a=p.parse_args();store=Store(a.db)
    if a.action in ('start','stop'):
        store.set('command',a.action.upper());store.event('CONTROL',{'command':a.action.upper()})
    elif a.action=='discover':
        if a.html:
            for row in parse_directory(Path(a.html).read_text()):store.seed(**row)
        else:
            session=requests.Session();session.headers['User-Agent']='A-one-road-lead-discovery/1.0'
            for attempt in range(3):
                try: fetch_directory(session,store);break
                except Exception as exc:
                    store.event('DISCOVERY_ERROR',{'attempt':attempt+1,'type':type(exc).__name__})
                    if attempt==2:raise
                    time.sleep(2**attempt)
    elif a.action=='import-evidence':
        for line in Path(a.path).read_text().splitlines():
            if line.strip():store.record(json.loads(line))
    elif a.action=='work':
        with open(a.db+'.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            worker(store,a.config)
    print(json.dumps(store.status(),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
