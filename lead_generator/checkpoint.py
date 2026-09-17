"""Private, checksummed two-slot journal inside existing Sales Control cells.

No new tabs, public artifacts, repository data, or extra storage service.
An interrupted save leaves the previous slot readable; manifest switches last.
"""
import base64
import hashlib
import json
import zlib

TABLES=('control','seeds','records','mirrors','events')
CHUNK_SIZE=30000
MAX_CHUNKS=480
MAX_EXPANDED=64*1024*1024
FORMAT='aone-leadgen-checkpoint-v1'


def encode(store):
    data={table:[dict(r) for r in store.db.execute('SELECT * FROM '+table)] for table in TABLES}
    data['events']=data['events'][-2000:]
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    if len(raw)>MAX_EXPANDED: raise ValueError('checkpoint_capacity_exceeded')
    packed=zlib.compress(raw,6)
    text=base64.b64encode(packed).decode()
    chunks=[text[i:i+CHUNK_SIZE] for i in range(0,len(text),CHUNK_SIZE)]
    if len(chunks)>MAX_CHUNKS: raise ValueError('checkpoint_capacity_exceeded')
    return chunks,hashlib.sha256(packed).hexdigest()


def decode(chunks,digest):
    packed=base64.b64decode(''.join(chunks),validate=True)
    if hashlib.sha256(packed).hexdigest()!=digest: raise ValueError('checkpoint_checksum_mismatch')
    dec=zlib.decompressobj()
    raw=dec.decompress(packed,MAX_EXPANDED+1)
    if len(raw)>MAX_EXPANDED or not dec.eof: raise ValueError('checkpoint_invalid_size')
    data=json.loads(raw)
    if set(data)!=set(TABLES): raise ValueError('checkpoint_schema_mismatch')
    return data


def restore(store,data):
    # Restore only known columns using parameterized SQL; never execute a dump.
    with store.db:
        for table in TABLES:
            columns=[r[1] for r in store.db.execute('PRAGMA table_info('+table+')')]
            store.db.execute('DELETE FROM '+table)
            for row in data[table]:
                if set(row)!=set(columns): raise ValueError('checkpoint_columns_mismatch')
                store.db.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+
                                 ','.join('?' for _ in columns)+')',[row[c] for c in columns])


class SheetCheckpoint:
    def __init__(self,api):
        self.api=api
        self.tab="'"+api.config['control']['tab'].replace("'","''")+"'!"
        self.manifest=None

    def read(self,area):
        from urllib.parse import quote
        return self.api.request('GET','ssot','/values/'+quote(self.tab+area,safe='')).get('values',[])

    def write(self,area,values):
        self.api.request('POST','ssot','/values:batchUpdate',json={'valueInputOption':'RAW',
            'data':[{'range':self.tab+area,'values':values}]})

    def load(self,store):
        head=self.read('I64:J64')
        if not head:
            if any(any(str(c or '') for c in row) for row in self.read('I64:P200')):
                raise ValueError('checkpoint_area_already_in_use')
            return False
        if len(head[0])!=2 or head[0][0]!=FORMAT: raise ValueError('checkpoint_manifest_invalid')
        m=json.loads(head[0][1])
        if m.get('slot') not in (0,1) or not 0<m.get('chunks',0)<=MAX_CHUNKS:
            raise ValueError('checkpoint_manifest_invalid')
        start=65+68*m['slot']; end=start+(m['chunks']-1)//8
        values=self.read(f'I{start}:P{end}')
        chunks=[str(v) for row in values for v in row][:m['chunks']]
        if len(chunks)!=m['chunks']: raise ValueError('checkpoint_truncated')
        restore(store,decode(chunks,m['sha256']));self.manifest=m
        return True

    def save(self,store):
        from .store import now
        # Re-read after any ambiguous previous manifest write before selecting
        # the inactive slot. Never accidentally overwrite the active snapshot.
        head=self.read('I64:J64')
        if head:
            if len(head[0])!=2 or head[0][0]!=FORMAT:raise ValueError('checkpoint_manifest_invalid')
            latest=json.loads(head[0][1])
            if latest.get('slot') not in (0,1):raise ValueError('checkpoint_manifest_invalid')
            self.manifest=latest
        chunks,digest=encode(store)
        slot=1-self.manifest['slot'] if self.manifest else 0
        start=65+68*slot
        # Small requests stay below Sheets' recommended request-size range.
        for offset in range(0,len(chunks),16):
            batch=chunks[offset:offset+16]
            rows=[batch[i:i+8] for i in range(0,len(batch),8)]
            self.write(f'I{start+offset//8}',rows)
        # Verify private payload before switching the manifest.
        end=start+(len(chunks)-1)//8
        actual=[str(v) for row in self.read(f'I{start}:P{end}') for v in row][:len(chunks)]
        decode(actual,digest)
        m={'slot':slot,'chunks':len(chunks),'sha256':digest,'saved_at':now()}
        self.write('I64:J64',[[FORMAT,json.dumps(m,separators=(',',':'))]])
        self.manifest=m
