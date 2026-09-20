"""One-shot source-only integration, removed after regression tests pass."""
from pathlib import Path


def change(path, old, new, count=1):
    p = Path(path); value = p.read_text()
    if value.count(old) != count:
        raise RuntimeError(f'anchor_count:{path}:{value.count(old)}:{count}')
    p.write_text(value.replace(old, new), encoding='utf-8')


change('customer_care.py',
    '    fields = ("company_id", "company_name", "website", "recipient", "subject", "body", "prompt_sha256")',
    '    fields = ("company_id", "company_name", "website", "recipient", "subject", "body", "prompt_sha256",\n              "recipient_evidence", "evidence", "buyer_workflow", "offer_authority")')
# Structural evidence errors are reported directly; byte binding additionally
# invalidates a previously reviewed packet after any source/recipient edit.
change('customer_care.py',
    '    if review.get("packet_sha256") != packet_hash(packet):\n        raise ValueError("QUALITY_REVIEW_BYTES_CHANGED")\n', '')
change('customer_care.py',
    '    return packet_hash(packet)\n',
    '''    if review.get("packet_sha256") != packet_hash(packet):
        raise ValueError("QUALITY_REVIEW_BYTES_CHANGED")
    greeting = str(packet.get("body") or "").splitlines()[0].strip()
    first_name = text(recipient_evidence.get("first_name"))
    company_short = text(packet.get("company_short_name")) or text(row.get("company_name"))
    permitted = {"Hi " + company_short + " team,"}
    if first_name and recipient_evidence.get("person_name_source"):
        permitted.add("Hi " + first_name + ",")
    if greeting not in permitted:
        raise ValueError("QUALITY_GREETING_UNVERIFIED")
    if len(dumps(packet)) > 30000:
        raise ValueError("QUALITY_PACKET_TOO_LARGE_PRESERVE_SOURCES")
    return packet_hash(packet)
''')
# Review fingerprints also bind any approved short corporate name.
change('customer_care.py',
    '              "recipient_evidence", "evidence", "buyer_workflow", "offer_authority")',
    '              "recipient_evidence", "evidence", "buyer_workflow", "offer_authority", "company_short_name")')
change('customer_care.py',
    '        if prior != expected:\n',
    '        if {k: v for k, v in prior.items() if k != "run_id"} != {k: v for k, v in expected.items() if k != "run_id"}:\n')
# A reset visible Status never permits a previously contacted company to reopen.
change('customer_care.py',
    '    initial = text(row.get("Status")) in INITIAL\n',
    '''    initial = text(row.get("Status")) in INITIAL
    contacted = bool(row.get("Last_Outbound_Message_ID") or row.get("First_Contacted_At") or any(
        e.get("status") in {"SENT", "FORM_SENT", "MANUAL_SENT"} or e.get("event_type") in {"OUTBOUND_SENT", "MANUAL_SEND"}
        for e in events))
''')
change('customer_care.py',
    '        if state in ACTIVE_SEND or row.get("AI_手動対応") == "対応中" or not initial:',
    '        if state in ACTIVE_SEND or row.get("AI_手動対応") in {"対応中", "完了"} or not initial or contacted:')
change('customer_care.py',
    '        if not initial or state in ACTIVE_SEND or row.get("AI_手動対応") == "対応中":',
    '        if not initial or contacted or state in ACTIVE_SEND or row.get("AI_手動対応") in {"対応中", "完了"}:')
# Never label a provider-uncertain call as a definite failure in its event.
change('customer_care.py',
    '    at = aware(event.get("occurred_at"))\n    events = history(row)\n',
    '''    event = dict(event)
    if event.get("kind") == "SEND_FAILED" and event.get("definitely_not_sent") is not True:
        event["kind"] = "SEND_UNKNOWN"
    at = aware(event.get("occurred_at"))
    events = history(row)
''')
# Preserve late factual receipts while retaining the more advanced current state.
change('customer_care.py',
    '    if row.get("AI_状態更新日時") and at < aware(row["AI_状態更新日時"]):\n        return changes\n',
    '''    stale = bool(row.get("AI_状態更新日時") and at < aware(row["AI_状態更新日時"]))
    if stale and event.get("kind") not in {"SENT", "MANUAL_SENT"}:
        return changes
''')
change('customer_care.py',
    '    return changes\n\n\ndef in_send_window',
    '''    if event.get("kind") in {"SEND_FAILED", "SEND_UNKNOWN", "QUALITY_HOLD"}:
        draft = event.get("rescue_draft") or {}
        for key, value in (("営業メール件名", draft.get("subject")), ("営業メール本文", draft.get("body")),
                           ("営業メール宛先", event.get("rescue_recipient"))):
            if value and not row.get(key):
                changes[key] = value
    if stale:
        # Retain all historical proof; only fill missing or newer contact fields.
        keep = {"AI_会社ID", "Sales_History_JSON", "First_Contacted_At"}
        old_outbound = row.get("Last_Outbound_At")
        if not old_outbound or at >= aware(old_outbound):
            keep |= {"Last_Outbound_At", "Last_Outbound_Message_ID", "Last_Outbound_Thread_ID", "Last_Outbound_Recipient"}
        changes = {key: value for key, value in changes.items() if key in keep}
    return changes


def in_send_window''')
# Use the canonical action vocabulary already counted by Sales Control.
change('customer_sheet.py',
    "    action = 'NEW_DM' if kind == 'SENT' else 'MANUAL_SEND' if kind == 'MANUAL_SENT' else kind\n",
    '''    factual = kind in {'SENT', 'MANUAL_SENT', 'REPLIED', 'MEETING_HELD', 'MEETING_BOOKED'}
    action = {'SENT': 'OUTBOUND_SENT', 'MANUAL_SENT': 'OUTBOUND_SENT', 'REPLIED': 'REPLY_RECEIVED',
              'MEETING_HELD': 'MEETING_COMPLETED', 'MEETING_BOOKED': 'APPOINTMENT_CONFIRMED'}.get(kind, kind)
    canonical_id = event.get('canonical_action_id') or (event.get('receipt') or {}).get('message_id') or (
        event.get('message_id') if kind == 'REPLIED' else '') or event['event_id']
''')
change('customer_sheet.py',
    "'action_type': action, 'source': 'CUSTOMER_FIRST', 'recorded_at':",
    "'action_type': action, 'source': 'EVIDENCE_RECONCILE' if factual else 'CUSTOMER_FIRST', 'recorded_at':")
change('customer_sheet.py',
    "'canonical_action_id': event['event_id'], 'source_origins': 'CUSTOMER_FIRST'}",
    "'canonical_action_id': canonical_id, 'source_origins': 'SSOT / CUSTOMER_FIRST'}")
# A registered customer can be re-read with a changed row position; all
# reservations still require a just-read exact company identity.
print('customer_first_final_patch=applied; live_writes=0; customer_sends=0')
