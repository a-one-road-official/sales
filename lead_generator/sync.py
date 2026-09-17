"""Append-only two-workbook synchronization with read-after-write recovery.

Single writer required: deploy one process with one persistent journal. Never
retry an ambiguous append until a fresh read identifies the unique run marker.
"""
import json
import re
from urllib.parse import quote
from .policy import domain,name_key,qualification
from .store import now

class AmbiguousWrite(RuntimeError): pass


class Sheets:
    def __init__(self, config, session):
        self.config,self.session=config,session

    def request(self,method,dest,path,**kwargs):
        sid=self.config[dest]['spreadsheet_id']
        r=self.session.request(method,f'https://sheets.googleapis.com/v4/spreadsheets/{sid}{path}',
                               timeout=45,**kwargs)
        r.raise_for_status()
        return r.json()

    def control_command(self):
        control=self.config.get('control')
        if not control: return None
        tab=control['tab'].replace("'","''")
        data=self.request('GET','ssot','/values/'+quote(f"'{tab}'!B46",safe=''))
        rows=data.get('values',[])
        # Blank or unexpected command fails closed.
        return 'START' if rows and rows[0] and rows[0][0]=='START' else 'STOP'

    def publish_status(self,store):
        control=self.config.get('control')
        if not control: return
        tab=control['tab'].replace("'","''")
        status=store.status()
        updates={'B47':'配備済','B48':status['control'].get('state',''),
                 'B51':status['seeds'],'B52':status['qualification'].get('PASS',0),
                 'B53':status['both_verified_new'],'B54':now()}
        self.request('POST','ssot','/values:batchUpdate',json={'valueInputOption':'RAW','data':[
            {'range':f"'{tab}'!{cell}",'values':[[value]]} for cell,value in updates.items()]})

    def rows(self,dest):
        tab=self.config[dest]['tab'].replace("'","''")
        return self.request('GET',dest,'/values/'+quote(f"'{tab}'!A:Z",safe=''),
                            params={'valueRenderOption':'UNFORMATTED_VALUE'}).get('values',[])

    def append(self,dest,row):
        tab=self.config[dest]['tab'].replace("'","''")
        return self.request('POST',dest,'/values/'+quote(f"'{tab}'!A1",safe='')+':append',
            params={'valueInputOption':'RAW','insertDataOption':'INSERT_ROWS'},json={'values':[row]})


def layout(dest):
    return {'name':0,'website':6,'marker':9} if dest=='ssot' else {'name':1,'website':5,'marker':10}


def cell(row,i):
    return str(row[i]) if len(row)>i else ''


def find(rows,dest,record,marker):
    col=layout(dest)
    marked=[]; duplicates=[]
    for i,row in enumerate(rows[1:],2):
        same_domain=domain(cell(row,col['website']))==domain(record['website'])
        same_name=name_key(cell(row,col['name']))==name_key(record['company_name'])
        if marker in cell(row,col['marker']):
            if not same_domain: raise AmbiguousWrite('marker_identity_mismatch')
            marked.append(i)
        elif same_domain or same_name: duplicates.append(i)
    if len(marked)>1: raise AmbiguousWrite('multiple_marker_rows')
    return marked,duplicates


def make_row(dest,record,result,marker):
    timestamp=now()
    src=marker+' '+record['exhibition_proof']['url']
    evidence=json.dumps({'policy':result['policy_version'],'payment':result['payment_capacity_level'],
                        'offer':record['initial_offer'],'japan':record['japan'],
                        'proofs':{k:v for k,v in record.items() if k.endswith('_proof')}},ensure_ascii=False)
    if len(evidence)>45000: raise ValueError('evidence_cell_too_large')
    if dest=='ssot':
        return [record['company_name'],'未接触','Factory',
                '選定条件確認済・前払い同意は商談時確認',result['country'],'',record['website'],
                record['product_text'][:2000],result['japan_status'],src,timestamp,
                '代理店のみ' if record.get('japan',{}).get('distributor_only') else '確認範囲で直接拠点なし',
                record['japan']['checks']['official_locations']['url'],timestamp,domain(record['website']),
                result['sector'],result['priority_score'],'evidence_checked',
                '初回:有効リード・顧客/パートナー商談開拓',marker,evidence,timestamp,
                '確認範囲内の判定。営業前に日本拠点・担当者を再確認。',result['country']]
    return ['',record['company_name'],result['sector'],result['country'],'',record['website'],'',
            '未接触',record['product_text'][:2000],result['japan_status'],src,timestamp,
            '代理店のみ' if record.get('japan',{}).get('distributor_only') else '確認範囲で直接拠点なし',
            record['japan']['checks']['official_locations']['url'],timestamp,'確認済','','',timestamp]


class Mirror:
    def __init__(self,store,api,run_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',run_id): raise ValueError('invalid run id')
        self.store,self.api,self.run_id=store,api,run_id

    def sync_one(self,k,record):
        result=qualification(record)
        if result['decision']!='PASS': return 'NOT_QUALIFIED'
        marker=f'leadgen:{self.run_id}:{k}'
        snapshots={d:self.api.rows(d) for d in ('ssot','sacrifice')}
        # Validate headers before either mutation.
        for d,rows in snapshots.items():
            col=layout(d)
            if not rows or cell(rows[0],col['name'])!='company_name' or cell(rows[0],col['website'])!='website':
                raise ValueError('destination_schema_changed:'+d)
        found={d:find(rows,d,record,marker) for d,rows in snapshots.items()}
        # Existing companies are not net-new. Do not alter statuses or copy history.
        if any(duplicates for marked,duplicates in found.values()):
            for d in found: self.store.mark(k,d,'EXISTING_OR_CONFLICT')
            return 'EXISTING_OR_CONFLICT'
        for d in ('ssot','sacrifice'):
            marked,_=found[d]
            if marked:
                self.store.mark(k,d,'VERIFIED_NEW',marked[0]); continue
            remote_command=self.api.control_command() if hasattr(self.api,'control_command') else None
            if remote_command=='STOP': self.store.set('command','STOP')
            if self.store.get('command')!='START': return 'STOPPED'
            self.store.mark(k,d,'PENDING')
            try:
                self.api.append(d,make_row(d,record,result,marker))
            except Exception as exc:
                # The server may have committed. Reconcile once, then pause if ambiguous.
                self.store.mark(k,d,'UNCERTAIN',error=type(exc).__name__)
                try: marked,_=find(self.api.rows(d),d,record,marker)
                except Exception: marked=[]
                if not marked:
                    self.store.set('command','STOP')
                    self.store.set('state','AMBIGUOUS_WRITE_REQUIRES_RECONCILIATION')
                    raise AmbiguousWrite(d) from exc
            marked,_=find(self.api.rows(d),d,record,marker)
            if not marked:
                self.store.set('command','STOP')
                raise AmbiguousWrite('append_readback_missing:'+d)
            self.store.mark(k,d,'VERIFIED_NEW',marked[0])
        self.store.event('BOTH_VERIFIED',{'company_key':k})
        return 'BOTH_VERIFIED'
