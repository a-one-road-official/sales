"""Token-free discovery and evidence-qualified intake on a standard runner.

Catalog profiles remain unqualified. Only complete evidence records can append.
No private CRM identity is used as input to public website requests.
"""
import json
import os
import tempfile
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .checkpoint import SheetCheckpoint
from .discovery import DIRECTORY,fetch_directory
from .policy import company_key
from .store import Store,now
from .sync import Sheets,Mirror

CONFIG={
    'run_id':'overnight-20260917',
    'ssot':{'spreadsheet_id':'1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo','tab':'営業リスト＿Factory/BPO'},
    'sacrifice':{'spreadsheet_id':'1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk','tab':'営業リスト_Vendor'},
    'control':{'tab':'Sales Control'},
}

class PublicAccessBlocked(ValueError):pass


def public_profile(session,url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='www.roboticstomorrow.com' or not parsed.path.startswith('/company_directory/'):
        raise ValueError('profile_not_on_allowed_public_catalog')
    # No redirects to unreviewed destinations; catalog access blocks remain errors.
    with session.get(url,timeout=(8,20),stream=True,allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code!=200: raise ValueError('profile_redirect_or_incomplete')
        parts=[];size=0
        for part in response.iter_content(65536):
            size+=len(part)
            if size>2_000_000:raise ValueError('profile_too_large')
            parts.append(part)
        soup=BeautifulSoup(b''.join(parts),'html.parser')
    for tag in soup.select('script,style,nav,footer,header'):tag.decompose()
    main=soup.select_one('main') or soup.select_one('.main') or soup
    text=main.get_text(' ',strip=True)
    if len(text)<100 or any(x in text.lower() for x in ('verify you are human','checking your browser','just a moment...')):
        raise PublicAccessBlocked('profile_block_or_incomplete')
    return {'url':url,'title':soup.title.get_text(' ',strip=True) if soup.title else '',
            'text':text[:12000],'checked_at':now(),'qualification':'REVIEW',
            'reason':'Catalog profile is not proof of exhibition, budget, or Japan absence.'}


def run(api,store,checkpoint,budget_seconds=600):
    checkpoint.load(store)
    if api.control_command()!='START':
        store.set('command','STOP');store.set('state','STOPPED');api.publish_status(store)
        return
    if store.get('source_blocked')=='true':
        store.set('state','SOURCE_ACCESS_BLOCKED');api.publish_status(store);return
    if float(store.get('source_retry_after','0'))>time.time():
        store.set('state','SOURCE_BACKOFF');api.publish_status(store);return
    store.set('command','START');store.set('run_id',CONFIG['run_id'])
    mirror=Mirror(store,api,CONFIG['run_id'])
    # Recover the exact prior company set from the journal and fresh destination rows.
    if store.completed(): mirror.reconcile_completed()
    if len(store.completed())>=2000:
        store.set('state','TARGET_REACHED');store.set('command','STOP')
        store.set('target_verified',now());checkpoint.save(store);api.publish_status(store);return
    session=requests.Session();session.headers['User-Agent']='A-one-road-public-directory-research/1.0'
    start=time.monotonic()
    try:
        store.set('state','COLLECTING_PUBLIC_PROFILES');api.publish_status(store)
        robots_response=session.get('https://www.roboticstomorrow.com/robots.txt',timeout=(8,20),allow_redirects=False)
        robots=RobotFileParser()
        if robots_response.status_code==200:robots.parse(robots_response.text.splitlines())
        elif robots_response.status_code==404:robots.parse([])
        else:raise ValueError('robots_unavailable_or_access_blocked')
        if not store.db.execute('SELECT count(*) FROM seeds').fetchone()[0]:
            if not robots.can_fetch(session.headers['User-Agent'],DIRECTORY):raise PublicAccessBlocked('directory_robots_disallowed')
            fetch_directory(session,store);checkpoint.save(store)
        # Detailed profile text is held in existing seed descriptions so it is part
        # of the portable journal; no new CRM companies are created by this step.
        seeds=store.db.execute("SELECT * FROM seeds WHERE state IN ('DISCOVERED','FETCH_FAILED_1','FETCH_FAILED_2') ORDER BY CASE WHEN location LIKE '%India%' OR location LIKE '%Taiwan%' OR location LIKE '%Korea%' OR location LIKE '%Poland%' OR location LIKE '%Israel%' OR location LIKE '%Czech%' THEN 0 ELSE 1 END,profile_url").fetchall()
        attempted=0;consecutive_failures=0
        for seed in seeds:
            if attempted>=60 or time.monotonic()-start>=budget_seconds:break
            if api.control_command()!='START':store.set('command','STOP');break
            # Provenance is restricted to the independently collected public catalog.
            if seed['source_url']!=DIRECTORY:continue
            if not robots.can_fetch(session.headers['User-Agent'],seed['profile_url']):
                with store.db:store.db.execute("UPDATE seeds SET state='ROBOTS_DISALLOWED_REVIEW' WHERE profile_url=?",(seed['profile_url'],))
                continue
            attempted+=1
            try:
                profile=public_profile(session,seed['profile_url'])
                with store.db:
                    store.db.execute("UPDATE seeds SET description=?,state='PROFILE_COLLECTED_REVIEW' WHERE profile_url=?",
                                     (json.dumps(profile,ensure_ascii=False),seed['profile_url']))
                consecutive_failures=0
            except Exception as exc:
                consecutive_failures+=1
                attempt=int(seed['state'].rsplit('_',1)[-1])+1 if seed['state'].startswith('FETCH_FAILED_') else 1
                with store.db:
                    store.db.execute("UPDATE seeds SET state=? WHERE profile_url=?",('FETCH_FAILED_'+str(attempt),seed['profile_url']))
                store.event('PUBLIC_PROFILE_ERROR',{'type':type(exc).__name__,'url':seed['profile_url']})
                if isinstance(exc,PublicAccessBlocked) or (isinstance(exc,requests.HTTPError) and exc.response is not None and exc.response.status_code in (401,403,429)):
                    store.set('state','SOURCE_ACCESS_BLOCKED');store.set('source_blocked','true');break
            if attempted%10==0:
                store.set('heartbeat',now());checkpoint.save(store);api.publish_status(store)
            if consecutive_failures>=3:
                store.set('source_retry_after',time.time()+3600)
                store.set('state','SOURCE_ERROR_REQUIRES_REVIEW');break
            time.sleep(2)
        # Evidence records are produced independently. No catalog data is promoted
        # merely because a keyword or country matched.
        for row in store.db.execute("SELECT payload FROM records WHERE decision='PASS' ORDER BY updated_at").fetchall():
            if time.monotonic()-start>=budget_seconds or api.control_command()!='START':break
            record=json.loads(row['payload']);key=company_key(record)
            if key in store.completed():continue
            if len(store.completed())>=2000:break
            mirror.sync_one(key,record)
            checkpoint.save(store)
            if len(store.completed())>=500 and not store.get('milestone_verified'):
                mirror.reconcile_completed();store.set('milestone_verified',now());checkpoint.save(store)
        if len(store.completed())>=2000:
            mirror.reconcile_completed();store.set('target_verified',now());store.set('state','TARGET_REACHED');store.set('command','STOP')
        elif store.get('command')!='START':store.set('state','STOPPED')
        elif store.get('state') not in ('SOURCE_ERROR_REQUIRES_REVIEW','SOURCE_ACCESS_BLOCKED'):
            remaining=store.db.execute("SELECT count(*) FROM seeds WHERE state IN ('DISCOVERED','FETCH_FAILED_1','FETCH_FAILED_2')").fetchone()[0]
            store.set('state','PUBLIC_RESEARCH_CONTINUES' if remaining else 'WAITING_FOR_QUALIFIED_EVIDENCE')
    except Exception as exc:
        store.set('state','ERROR');store.event('RUN_ERROR',{'type':type(exc).__name__})
        store.set('source_retry_after',time.time()+3600)
        raise
    finally:
        session.close();store.set('heartbeat',now());checkpoint.save(store);api.publish_status(store)


def main():
    # Additional guard if workflow is copied to a private/billable context.
    if os.environ.get('AONE_FREE_PUBLIC_RUNNER')!='true':raise SystemExit('Free public runner required')
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
    api=Sheets(CONFIG,AuthorizedSession(credentials))
    with tempfile.TemporaryDirectory() as temp:
        store=Store(temp+'/state.sqlite')
        try:
            run(api,store,SheetCheckpoint(api))
        except Exception as exc:
            # Public Actions logs must never include private payloads or API bodies.
            print('Worker stopped: '+type(exc).__name__);raise SystemExit(1)
        finally:store.db.close()
    print('Worker cycle finished. Detailed progress remains in private Sales Control.')


if __name__=='__main__':main()
