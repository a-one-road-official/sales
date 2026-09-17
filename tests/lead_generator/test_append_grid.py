import pytest
from lead_generator.sync import Sheets


def test_append_uses_server_allocated_cells_and_literal_values():
    calls=[]
    class API(Sheets):
        def request(self,method,dest,path,**kwargs):
            calls.append((method,dest,path,kwargs))
            return {'sheets':[{'properties':{'sheetId':17,'title':'target'}}]}
    api=API({'ssot':{'tab':'target'}},None)
    api.append('ssot',['Fixture company','=literal text','未接触'])
    assert len(calls)==2
    assert calls[1][2]==':batchUpdate'
    request=calls[1][3]['json']['requests'][0]['appendCells']
    assert request['sheetId']==17
    assert request['rows'][0]['values'][1]=={'userEnteredValue':{'stringValue':'=literal text'}}
    assert 'start' not in request


def test_changed_tab_fails_before_mutation():
    calls=[]
    class API(Sheets):
        def request(self,method,dest,path,**kwargs):
            calls.append(method)
            return {'sheets':[{'properties':{'sheetId':17,'title':'renamed'}}]}
    with pytest.raises(ValueError):API({'ssot':{'tab':'target'}},None).append('ssot',['Fixture'])
    assert calls==['GET']
