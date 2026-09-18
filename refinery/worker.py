"""Bounded public extraction -> one private tab. No refinement or sending.

Single writer is serialized by the dedicated GitHub Actions concurrency group.
Google Sheets is not a compare-and-swap store: do not run another writer to this
raw tab in parallel. Existing sales tabs are read-only exclusion sources.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from itertools import zip_longest
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import requests
from .core import TAB, HEADER_ROW, HEADERS, SOURCE_HOST, SOURCE_PATH, source_url, parse_listing, new_rows

BOOK='1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo'
OTHER='1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk'
USER_AGENT='A-one-road-raw-material/1.0 (+https://a-oneroad.com/)'


def now():
    return datetime.now(timezone.utc).isoformat()


def bounded_get(session, url, *, robots=False):
    if robots:
        if url != 'https://' + SOURCE_HOST + '/robots.txt':
            raise ValueError('robots_origin_mismatch')
    else:
        source_url(url)
    time.sleep(2)
    with session.get(url, timeout=(7,20), allow_redirects=False, stream=True) as response:
        if robots and response.status_code==404:
            return ''
        # No external redirect, login, paid proxy, captcha bypass or browser fallback.
        if response.status_code != 200:
            raise ValueError('source_http_' + str(response.status_code))
        if not robots and 'text/html' not in response.headers.get('Content-Type','').lower():
            raise ValueError('source_not_html')
        chunks=[]; size=0
        for part in response.iter_content(65536):
            size+=len(part)
            if size>2_000_000:
                raise ValueError('source_response_too_large')
            chunks.append(part)
        return b''.join(chunks).decode(response.encoding or 'utf-8',errors='replace')


class SheetStore:
    def __init__(self, service):
        self.api=service.spreadsheets()
        meta=self.api.get(spreadsheetId=BOOK,fields='sheets.properties').execute()
        sheets=[s['properties'] for s in meta['sheets'] if s['properties']['title']==TAB]
        if len(sheets)!=1:
            raise ValueError('private_raw_tab_not_provisioned')
        self.sheet_id=sheets[0]['sheetId']
        self.row_count=sheets[0]['gridProperties']['rowCount']
        self.q="'"+TAB.replace("'","''")+"'!"
        if self.values(BOOK,self.q+'A6:Z6') != [list(HEADERS)]:
            raise ValueError('raw_schema_mismatch')

    def values(self, book, area):
        return self.api.values().get(spreadsheetId=book,range=area,valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])

    def config(self):
        rows=self.values(BOOK,self.q+'A2:H5')
        cells=[list(r)+['']*(8-len(r)) for r in rows]
        if len(cells)!=4:
            raise ValueError('raw_controls_missing')
        excluded=json.loads(cells[1][1] or '[]')
        if not isinstance(excluded,list) or not all(isinstance(x,str) for x in excluded):
            raise ValueError('excluded_names_must_be_json_list')
        page=int(cells[0][4]); max_pages=int(cells[1][7])
        if not 1<=page<=9999 or not 1<=max_pages<=4:
            raise ValueError('raw_run_bounds_invalid')
        return {'source':source_url(cells[0][1]),'page':page,'command':cells[0][7],
            'excluded':excluded,'max_pages':max_pages,'exhibition':cells[3][4]}

    def existing(self):
        rows=self.values(BOOK,self.q+f'A7:C{self.row_count}')
        result=[]
        for i,r in enumerate(rows,7):
            if not r:
                raise ValueError('raw_hole_requires_review')
            if len(r)<2 or not str(r[0]).startswith('raw:') or not r[1]:
                raise ValueError('raw_identity_missing')
            result.append(dict(zip(HEADERS[:3],r)))
        return result

    def exclusions(self):
        output=[]
        for book,tab,name_col,url_col in ((BOOK,'営業リスト＿Factory/BPO','A','G'),(OTHER,'営業リスト_Vendor','B','F')):
            area="'"+tab.replace("'","''")+"'!"
            response=self.api.values().batchGet(spreadsheetId=book,
                ranges=[area+name_col+':'+name_col,area+url_col+':'+url_col],
                valueRenderOption='UNFORMATTED_VALUE').execute()['valueRanges']
            names=response[0].get('values',[]); urls=response[1].get('values',[])
            if not names or not urls or names[0]!=['company_name'] or urls[0]!=['website']:
                raise ValueError('exclusion_source_schema_changed')
            output.extend(((a or [''])[0],(b or [''])[0]) for a,b in zip_longest(names[1:],urls[1:],fillvalue=[]))
        return output

    def _write(self, requests_):
        # Every request addresses our one new tab, never an existing sales tab.
        for req in requests_:
            op=next(iter(req.values()))
            sid=op.get('sheetId',(op.get('start') or {}).get('sheetId',(op.get('properties') or {}).get('sheetId')))
            if sid!=self.sheet_id:
                raise ValueError('write_outside_raw_tab')
        return self.api.batchUpdate(spreadsheetId=BOOK,body={'requests':requests_}).execute()

    def control(self,row,col,value):
        cell={'numberValue':value} if isinstance(value,int) else {'stringValue':str(value)}
        self._write([{'updateCells':{'start':{'sheetId':self.sheet_id,'rowIndex':row-1,'columnIndex':col-1},
            'rows':[{'values':[{'userEnteredValue':cell}]}],'fields':'userEnteredValue'}}])

    def append(self, rows, before):
        if not rows:return
        needed=HEADER_ROW+before+len(rows)
        if needed>self.row_count:
            # Grow only the owned raw tab; bounded initial implementation.
            if needed>10000:raise ValueError('raw_capacity_review_required')
            self._write([{'updateSheetProperties':{'properties':{'sheetId':self.sheet_id,
                'gridProperties':{'rowCount':max(needed,self.row_count+500)}},'fields':'gridProperties.rowCount'}}])
            self.row_count=max(needed,self.row_count+500)
        self._write([{'appendCells':{'sheetId':self.sheet_id,'rows':[
            {'values':[{'userEnteredValue':{'stringValue':str(value)}} for value in row]} for row in rows],
            'fields':'userEnteredValue'}}])
        check=self.values(BOOK,self.q+f'A{HEADER_ROW+before+1}:I{needed}')
        if check!=[r[:9] for r in rows]:
            raise ValueError('raw_append_readback_mismatch_no_checkpoint_advance')


def run(store, session):
    cfg=store.config()
    if cfg['command']!='START':
        return {'state':'RAW_INTAKE_DISABLED','customer_sends':0,'paid_ai_calls':0}
    current=store.existing(); known=store.exclusions()
    robots_url='https://'+SOURCE_HOST+'/robots.txt'
    robots=RobotFileParser(); robots.parse(bounded_get(session,robots_url,robots=True).splitlines())
    total={'state':'COMPLETE','pages':0,'source_records':0,'duplicates':0,'explicitly_excluded':0,'appended':0,
        'customer_sends':0,'paid_ai_calls':0,'sales_status_writes':0}
    page=cfg['page']
    for _ in range(cfg['max_pages']):
        if store.config()['command']!='START':
            total['state']='RAW_INTAKE_PAUSED';break
        url=cfg['source'].split('?',1)[0]+'?page='+str(page)
        if not robots.can_fetch(USER_AGENT,url):raise ValueError('source_robots_disallowed')
        records,last=parse_listing(bounded_get(session,url),url)
        output,counts=new_rows(records,current,known,cfg['excluded'],now(),cfg['exhibition'])
        store.append(output,len(current))
        current.extend(dict(zip(HEADERS,row)) for row in output)
        for key,value in counts.items():total[key]+=value
        total['pages']+=1
        # Advance only after the entire page's material is durable and read back.
        page=1 if page>=last else page+1
        store.control(2,5,page)
        store.control(3,5,now())
        store.control(4,5,'')
        if page==1:break
    total['next_page']=page
    return total


def main():
    if (os.getenv('GITHUB_ACTIONS')!='true' or os.getenv('GITHUB_REPOSITORY')!='a-one-road-official/sales'
        or os.getenv('GITHUB_WORKFLOW')!='Raw material intake (Python only)' or os.getenv('K_SERVICE')):
        raise SystemExit('approved_free_runner_required')
    # Fail before constructing network clients on any accidental paid-AI flag.
    for key in ('LEAD_FACTORY_VERTEX_ALLOWED','LEAD_FACTORY_PAID_AI_ALLOWED','LEAD_FACTORY_PAID_CLOUD_ALLOWED'):
        if os.getenv(key,'FALSE').upper() not in {'FALSE','0','NO','OFF'}:
            raise SystemExit('paid_execution_forbidden:'+key)
    from google.auth import default
    from googleapiclient.discovery import build
    creds,_=default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
    store=None
    try:
        store=SheetStore(build('sheets','v4',credentials=creds,cache_discovery=False))
        with requests.Session() as session:
            session.trust_env=False
            session.headers['User-Agent']=USER_AGENT
            result=run(store,session)
    except Exception as exc:
        # No raw recipient/body data or authentication messages in public logs.
        result={'state':'ERROR','error_type':type(exc).__name__,'customer_sends':0,'paid_ai_calls':0}
        if store:
            try:store.control(4,5,type(exc).__name__+':'+str(exc)[:250])
            except Exception:result['private_error_write_failed']=True
        print(json.dumps(result));return 1
    print(json.dumps(result,sort_keys=True))
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:
            f.write('### Raw intake (counts only)\n```json\n'+json.dumps(result,sort_keys=True)+'\n```\n')
    return 0

if __name__=='__main__':raise SystemExit(main())
