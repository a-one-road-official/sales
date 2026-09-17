import tempfile
from unittest.mock import patch
import pytest
from lead_generator.store import Store
from lead_generator.checkpoint import encode,decode,restore,SheetCheckpoint


def test_checkpoint_roundtrip_and_tamper_detection():
    with tempfile.TemporaryDirectory() as d:
        first=Store(d+'/first.sqlite');second=Store(d+'/second.sqlite')
        first.set('target','2000');first.event('TEST',{'data':'日本語の根拠'})
        chunks,digest=encode(first);restore(second,decode(chunks,digest))
        assert second.get('target')=='2000'
        assert second.status()['recent_events'][0]['kind']=='TEST'
        with pytest.raises(ValueError):decode(chunks,'wrong')
        first.db.close();second.db.close()


class FakeCheckpoint(SheetCheckpoint):
    def __init__(self):self.manifest=None;self.cells={};self.fail_manifest=False
    def read(self,area):
        import re
        a,b=(area.split(':')+[area])[:2]
        def point(s):
            m=re.fullmatch(r'([I-P])(\d+)',s);return int(m[2]),ord(m[1])-ord('I')
        r,c=point(a);last_r,last_c=point(b)
        rows=[[self.cells.get((i,j),'') for j in range(c,last_c+1)] for i in range(r,last_r+1)]
        return rows if any(any(row) for row in rows) else []
    def write(self,area,values):
        import re
        if area=='I64:J64' and self.fail_manifest:raise TimeoutError()
        m=re.match(r'([I-P])(\d+)',area);r=int(m[2]);c=ord(m[1])-ord('I')
        for i,row in enumerate(values):
            for j,value in enumerate(row):self.cells[r+i,c+j]=value


def test_interrupted_save_keeps_previous_slot_readable():
    with tempfile.TemporaryDirectory() as d:
        s=Store(d+'/state.sqlite');cp=FakeCheckpoint()
        s.set('revision','1');cp.save(s)
        s.set('revision','2');cp.fail_manifest=True
        with pytest.raises(TimeoutError):cp.save(s)
        restored=Store(d+'/restored.sqlite');cp.load(restored)
        assert restored.get('revision')=='1'
        s.db.close();restored.db.close()


def test_stop_does_not_start_public_research():
    from lead_generator.scheduled import run
    class API:
        def control_command(self):return 'STOP'
        def publish_status(self,store):assert store.get('state')=='STOPPED'
    class CP:
        def load(self,store):return False
    with tempfile.TemporaryDirectory() as d:
        s=Store(d+'/state.sqlite')
        with patch('lead_generator.scheduled.requests.Session') as public:
            run(API(),s,CP());public.assert_not_called()
        s.db.close()
