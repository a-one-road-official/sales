"""Additional source-event ordering and qualification regressions. No I/O."""
import ssot_terminal as s
import pytest

NOW = '2026-09-21T07:45:00Z'


def company():
    row = {'company_name':'Fixture Works','website':'https://fixture.example',
           'Status':'AI送信済み','row_number':2,'営業メール状態':'SENT',
           'AI担当状態':'AI','Sales_History_JSON':'[]',
           'AI更新日時':'2026-09-21T07:40:00Z','AI実行JSON':'{}'}
    return row


def event(row, kind, **kwargs):
    key, name, _ = s.identity(row)
    return dict(event_id='fixture:'+kind, kind=kind, company_id=key,
                company_name=name, website=row['website'], occurred_at='2026-09-21T07:30:00Z', **kwargs)


def test_late_reply_still_suppresses_delivery():
    row = company()
    e = event(row,'REPLIED',message_id='reply',thread_id='thread',evidence='gmail:reply',human_reply=True)
    row.update(s.reduce_event(row,e,NOW)['changes'])
    assert row['Status']=='返信あり'
    assert s.load_object(row[s.META])['suppressed']
    assert row['AI更新日時']=='2026-09-21T07:40:00Z'


def test_late_optout_never_disappears():
    row = company()
    e = event(row,'OPTOUT',message_id='optout',evidence='gmail:optout')
    row.update(s.reduce_event(row,e,NOW)['changes'])
    assert row['Status']=='拒否' and s.load_object(row[s.META])['suppressed']


def test_answered_reply_does_not_reopen_human_backlog():
    row = company()
    e = event(row,'REPLIED',message_id='reply',thread_id='thread',evidence='gmail:reply',human_reply=True,awaiting_human=False)
    row.update(s.reduce_event(row,e,NOW)['changes'])
    assert row['AI返信対応']=='対応済み'


def test_qualification_cannot_inject_claim_or_identity():
    row = company(); row['AI更新日時']='2026-09-21T07:00:00Z'
    original = s.identity(row)
    profile = {'evidence':'https://fixture.example/about','maturity':'MATURE',
               'maturity_evidence':'https://fixture.example/history','company_id':'wrong','claim':{'id':'fake'}}
    row.update(s.reduce_event(row,event(row,'QUALIFIED',qualification='GO',profile=profile),NOW)['changes'])
    assert s.identity(row)==original
    assert 'claim' not in s.load_object(row[s.META])


def test_newer_inbound_display_is_preserved():
    row = company(); row['Last_Inbound']='2026-09-21T07:41:00Z'; row['AI返信対応']='対応済み'
    e = event(row,'REPLIED',message_id='older',thread_id='thread',evidence='gmail:older',human_reply=True)
    row.update(s.reduce_event(row,e,NOW)['changes'])
    assert row['Last_Inbound']=='2026-09-21T07:41:00Z' and row['AI返信対応']=='対応済み'


def test_older_calendar_evidence_cannot_restore_cancelled_booking():
    row = company()
    row[s.META]=s.dump({'meetings':{'cal1':{'state':'MEETING_CANCELLED','updated_at':'2026-09-21T07:40:00Z',
                                        'start_at':'2026-09-22T07:00:00Z','evidence':'calendar:cal1'}}})
    e = event(row,'MEETING_BOOKED',calendar_event_id='cal1',identity_verified=True,
              evidence='calendar:cal1',start_at='2026-09-22T07:00:00Z')
    with pytest.raises(ValueError,match='older_calendar_evidence'):
        s.reduce_event(row,e,NOW)
