"""Token-free discovery and evidence-qualified intake on a standard runner.

Catalog profiles remain unqualified. Only complete evidence records can append.
No private CRM identity is used as input to public website requests.
"""
import json
import os
import re
import tempfile
import time
from urllib.parse import urljoin,urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .checkpoint import SheetCheckpoint
from .discovery import DIRECTORIES,fetch_directory
from .policy import company_key, classify, normalize_country, EXCLUDED_COUNTRIES, FINAL_TARGET, FIRST_MILESTONE
from .store import Store,now
from .sync import Sheets,Mirror

CONFIG={
    'run_id':'overnight-20260917',
    'ssot':{'spreadsheet_id':'1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo','tab':'営業リスト＿Factory/BPO'},
    'sacrifice':{'spreadsheet_id':'1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk','tab':'営業リスト_Vendor'},
    'control':{'tab':'Sales Control'},
}

class PublicAccessBlocked(ValueError):pass

_COUNTRIES = (
    "United States of America", "United States", "South Korea", "Republic of Korea", "North Korea",
    "United Kingdom", "New Zealand", "Saudi Arabia", "United Arab Emirates", "Czech Republic",
    "Bosnia and Herzegovina", "North Macedonia", "Dominican Republic", "Costa Rica", "South Africa",
    "Taiwan", "Israel", "India", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Colombia",
    "Peru", "Uruguay", "Paraguay", "Bolivia", "Ecuador", "Australia", "Singapore", "Malaysia",
    "Indonesia", "Thailand", "Vietnam", "Philippines", "Pakistan", "Bangladesh", "Sri Lanka",
    "Turkey", "Türkiye", "Germany", "France", "Italy", "Spain", "Portugal", "Netherlands",
    "Belgium", "Luxembourg", "Switzerland", "Austria", "Poland", "Czechia", "Slovakia",
    "Slovenia", "Croatia", "Serbia", "Romania", "Bulgaria", "Hungary", "Greece", "Cyprus",
    "Malta", "Ireland", "Denmark", "Norway", "Sweden", "Finland", "Iceland", "Estonia",
    "Latvia", "Lithuania", "Ukraine", "Georgia", "Armenia", "Azerbaijan", "Kazakhstan",
    "Uzbekistan", "Egypt", "Morocco", "Tunisia", "Kenya", "Nigeria", "Ghana", "China",
    "Hong Kong", "Macau", "Japan",
)
_EXTERNAL_BLOCKED = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "instagram.com",
    "constantcontact.com", "imts.com", "automate.org", "packexpointernational.com",
    "robobusiness.com", "fabtechexpo.com", "manufacturingtomorrow.com", "roboticstomorrow.com",
    "altenergymag.com", "google.com", "bing.com", "yahoo.com",
}


def _root_host(url):
    host=(urlparse(str(url or "")).hostname or "").casefold().removeprefix("www.").rstrip(".")
    return host


def _blocked_external(url):
    host=_root_host(url)
    return not host or any(host==d or host.endswith("."+d) for d in _EXTERNAL_BLOCKED)


def _company_tokens(name):
    stop={"inc","corp","corporation","company","co","ltd","limited","llc","gmbh","ag","sa","bv","srl","oy","ab","group","systems","system","technology","technologies","solutions","international"}
    return [x for x in re.findall(r"[a-z0-9]+",str(name or "").casefold()) if len(x)>=3 and x not in stop]


def _country(location):
    low=" "+str(location or "").casefold()+" "
    for value in sorted(_COUNTRIES,key=len,reverse=True):
        if re.search(r"(?<![a-z])"+re.escape(value.casefold())+r"(?![a-z])",low):
            return normalize_country(value)
    return ""


def _proof(url,excerpt,status=200):
    return {"url":url,"excerpt":str(excerpt or "")[:1000],"checked_at":now(),"http_status":status}


def public_profile(session,url):
    parsed=urlparse(url)
    allowed_hosts={urlparse(x).hostname for x in DIRECTORIES}
    if parsed.scheme!='https' or parsed.hostname not in allowed_hosts or not parsed.path.startswith('/company_directory/'):
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
    external=[]
    for anchor in soup.select('a[href]'):
        candidate=urljoin(url,anchor.get('href','')).split('#',1)[0]
        if urlparse(candidate).scheme not in {'http','https'} or _blocked_external(candidate):continue
        external.append({"url":candidate,"label":anchor.get_text(' ',strip=True)[:300]})
    for tag in soup.select('script,style,nav,footer,header'):tag.decompose()
    main=soup.select_one('main') or soup.select_one('.main') or soup
    text=main.get_text(' ',strip=True)
    if len(text)<100 or any(x in text.lower() for x in ('verify you are human','checking your browser','just a moment...')):
        raise PublicAccessBlocked('profile_block_or_incomplete')
    return {'url':url,'title':soup.title.get_text(' ',strip=True) if soup.title else '',
            'text':text[:12000],'checked_at':now(),'qualification':'REVIEW',
            'external_links':external[:30],
            'reason':'Catalog profile provides capability discovery; official website is verified separately.'}


def official_homepage(session,name,links):
    tokens=_company_tokens(name)
    ranked=[]
    for item in links or []:
        url=str(item.get('url') or '')
        host=_root_host(url); label=str(item.get('label') or '').casefold()
        score=sum(4 for t in tokens if t in host)+sum(2 for t in tokens if t in label)
        if score: ranked.append((-score,url))
    for _,url in sorted(ranked)[:5]:
        try:
            with session.get(url,timeout=(8,20),stream=True,allow_redirects=True) as response:
                if response.status_code!=200:continue
                ctype=response.headers.get('Content-Type','').casefold()
                if 'html' not in ctype:continue
                parts=[];size=0
                for part in response.iter_content(65536):
                    size+=len(part)
                    if size>1_500_000:break
                    parts.append(part)
                html=b''.join(parts).decode(response.encoding or 'utf-8',errors='replace')
                soup=BeautifulSoup(html,'html.parser')
                title=soup.title.get_text(' ',strip=True) if soup.title else ''
                page=(title+' '+soup.get_text(' ',strip=True)[:5000]).casefold()
                if tokens and not any(t in page or t in _root_host(response.url) for t in tokens):continue
                final=str(response.url)
                return final,_proof(final,title or name,response.status_code)
        except requests.RequestException:
            continue
    return '',{}


def evidence_record(seed,profile,session):
    country=_country(seed['location'])
    if not country or country in EXCLUDED_COUNTRIES:return None
    capability=classify(profile.get('text',''))
    if not capability['score']:return None
    website,identity=official_homepage(session,seed['name'],profile.get('external_links'))
    if not website:return None
    profile_url=str(seed['profile_url'])
    return {
        'company_name':str(seed['name']).strip(), 'country':country, 'website':website,
        'product_text':profile.get('text','')[:12000],
        'identity_proof':identity,
        'country_proof':_proof(profile_url,seed['location']),
        'technical_proof':_proof(profile_url,'; '.join(capability['hits'])),
        'commercial_proof':{}, 'payment_capacity':{'basis':'unknown'},
        'initial_offer':{'delivery':'partner_appointments','requires_full_time_fde':False,'requires_joint_research':False},
        'japan':{'direct_presence':False,'country_manager':False,'distributor_only':False,'checks':{}},
        'source_record_url':profile_url,
    }


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
    if len(store.completed())>=FINAL_TARGET:
        store.set('state','TARGET_REACHED');store.set('command','STOP')
        store.set('target_verified',now());checkpoint.save(store);api.publish_status(store);return
    session=requests.Session();session.headers['User-Agent']='A-one-road-public-directory-research/1.0'
    start=time.monotonic()
    try:
        store.set('state','COLLECTING_PUBLIC_PROFILES');api.publish_status(store)
        robots_by_host={}
        for directory in DIRECTORIES:
            p=urlparse(directory); robots_url=f'{p.scheme}://{p.hostname}/robots.txt'
            response=session.get(robots_url,timeout=(8,20),allow_redirects=False)
            robots=RobotFileParser()
            if response.status_code==200:robots.parse(response.text.splitlines())
            elif response.status_code==404:robots.parse([])
            else:raise ValueError('robots_unavailable_or_access_blocked')
            if not robots.can_fetch(session.headers['User-Agent'],directory):raise PublicAccessBlocked('directory_robots_disallowed')
            robots_by_host[p.hostname]=robots
        if store.get('directory_catalog_version')!='aums-v2':
            fetch_directory(session,store);store.set('directory_catalog_version','aums-v2');checkpoint.save(store)
        # Detailed profile text is held in existing seed descriptions so it is part
        # of the portable journal; no new CRM companies are created by this step.
        seeds=store.db.execute("SELECT * FROM seeds WHERE state IN ('DISCOVERED','FETCH_FAILED_1','FETCH_FAILED_2','PROFILE_COLLECTED_REVIEW') ORDER BY CASE WHEN location LIKE '%India%' OR location LIKE '%Taiwan%' OR location LIKE '%Korea%' OR location LIKE '%Poland%' OR location LIKE '%Israel%' OR location LIKE '%Czech%' THEN 0 ELSE 1 END,profile_url").fetchall()
        attempted=0;consecutive_failures=0
        for seed in seeds:
            if attempted>=200 or time.monotonic()-start>=budget_seconds:break
            if api.control_command()!='START':store.set('command','STOP');break
            robots=robots_by_host.get(urlparse(seed['source_url']).hostname)
            if not robots or not robots.can_fetch(session.headers['User-Agent'],seed['profile_url']):
                with store.db:store.db.execute("UPDATE seeds SET state='ROBOTS_DISALLOWED_REVIEW' WHERE profile_url=?",(seed['profile_url'],))
                continue
            attempted+=1
            try:
                profile=public_profile(session,seed['profile_url'])
                record=evidence_record(seed,profile,session)
                decision='REVIEW'
                if record:
                    decision=store.record(record)['decision']
                with store.db:
                    store.db.execute("UPDATE seeds SET description=?,state='PROFILE_COLLECTED_REVIEW' WHERE profile_url=?",
                                     (json.dumps(profile,ensure_ascii=False),seed['profile_url']))
                    store.db.execute("UPDATE seeds SET state=? WHERE profile_url=?",
                                     ('PROFILE_QUALIFIED_'+decision,seed['profile_url']))
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
            time.sleep(.25)
        # Evidence records are produced independently. No catalog data is promoted
        # merely because a keyword or country matched.
        for row in store.db.execute("SELECT payload FROM records WHERE decision='PASS' ORDER BY updated_at").fetchall():
            if time.monotonic()-start>=budget_seconds or api.control_command()!='START':break
            record=json.loads(row['payload']);key=company_key(record)
            if key in store.completed():continue
            if len(store.completed())>=FINAL_TARGET:break
            mirror.sync_one(key,record)
            checkpoint.save(store)
            if len(store.completed())>=FIRST_MILESTONE and not store.get('milestone_verified'):
                mirror.reconcile_completed();store.set('milestone_verified',now());checkpoint.save(store)
        if len(store.completed())>=FINAL_TARGET:
            mirror.reconcile_completed();store.set('target_verified',now());store.set('state','TARGET_REACHED');store.set('command','STOP')
        elif store.get('command')!='START':store.set('state','STOPPED')
        elif store.get('state') not in ('SOURCE_ERROR_REQUIRES_REVIEW','SOURCE_ACCESS_BLOCKED'):
            remaining=store.db.execute("SELECT count(*) FROM seeds WHERE state IN ('DISCOVERED','FETCH_FAILED_1','FETCH_FAILED_2','PROFILE_COLLECTED_REVIEW')").fetchone()[0]
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
