"""One-shot source migration. Executed by the temporary branch-only builder.
Deleted with its workflow after a successful test run. Never accesses live data.
"""
from pathlib import Path
import re


def replace(path, old, new, count=1):
    p = Path(path); value = p.read_text()
    if value.count(old) != count:
        raise RuntimeError(f'patch_anchor_count:{path}:{value.count(old)} expected {count}')
    p.write_text(value.replace(old, new), encoding='utf-8')


replace('form_execution.py', 'from playwright.sync_api import sync_playwright\n',
    'from playwright.sync_api import sync_playwright\nfrom customer_care import identity_field_key, validate_identity_fields\n')
replace('form_execution.py',
    '    tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()\n    if tag == "textarea":',
    '    tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()\n    if tag == "input" and typ in {"text", ""}:\n        identity_key = identity_field_key(marker, el.get_attribute("autocomplete") or "")\n        if identity_key:\n            return identity_key\n    if tag == "textarea":')
helper = '''def _verify_identity_dom(form):
    """Read actual person/company values, including values changed by page JS."""
    observed = []
    controls = form.locator("input:not([type=hidden]), textarea, select")
    for index in range(controls.count()):
        el = controls.nth(index)
        if not el.is_visible() or not el.is_enabled():
            continue
        key = _field_key(el, _label_for(el))
        if key in {"name", "first_name", "last_name", "company", "email", "ambiguous_person_name"}:
            observed.append({"key": key, "final_value": _current_value(el)})
    validate_identity_fields(observed)


'''
replace('form_execution.py', 'class PublicContactFormExecutor:\n', helper + 'class PublicContactFormExecutor:\n')
replace('form_execution.py',
    '                    control_label = _control_label(submit)\n',
    '                    _verify_identity_dom(form)\n                    control_label = _control_label(submit)\n')
replace('form_execution.py',
    '                if preview_only:\n                    return result_payload("FORM_PREVIEW_READY", reason="PREVIEW_NO_SUBMISSION", submitted_message=matching_messages[0])',
    '                _verify_identity_dom(form)\n                if preview_only:\n                    return result_payload("FORM_PREVIEW_READY", reason="PREVIEW_NO_SUBMISSION", submitted_message=matching_messages[0])')
replace('outreach_master.py', 'def validate_email(draft):\n',
    'def validate_email(draft):\n    from customer_care import validate_customer_text\n    validate_customer_text(draft.get("subject"), draft.get("body"))\n')
# Preserve known contact history under the new user-requested status label.
replace('sales_history.py', 'CONTACTED_STATUSES = {\n', 'CONTACTED_STATUSES = {\n    "AI送信済み",\n    "リマイン3",\n')
replace('sales_history.py', '    "リマイン1": 2, "リマイン2": 3,\n',
    '    "AI送信済み": 1, "リマイン1": 2, "リマイン2": 3, "リマイン3": 3,\n')
# Add SSOT observability without enabling any previously unapproved campaign.
replace('sales_leads_sacrifice_run.py', '    results = []\n\n    for candidate in candidates:\n',
    '    results = []\n    ssot_tracker = None\n    if _cfg_truthy(cfg, "OUTREACH_TRACK_SSOT"):\n        from customer_sheet import CustomerSheet\n        ssot_tracker = CustomerSheet(sheets.svc)\n\n    for candidate in candidates:\n')
replace('sales_leads_sacrifice_run.py',
    '            else:\n                email = ""\n                proposed_email = ""\n',
    '''            else:
                if ssot_tracker is not None:
                    name = candidate.get("company_name", "")
                    official = site.get("official_website", site_url)
                    customer = ssot_tracker.find(name, official)
                    if customer is None:
                        page_evidence = (site.get("pages") or [{}])[0]
                        quote = str(page_evidence.get("text_excerpt") or page_evidence.get("text") or page_evidence.get("title") or "")
                        customer = ssot_tracker.register(
                            {"company_name": name, "website": official,
                             "hq_country": candidate.get("country", ""),
                             "Category": candidate.get("domain", "その他")},
                            {"company_verified": True, "decision": "GO", "source_url": official,
                             "quote": quote, "reason": "Existing permitted campaign; official site identity verified"}, run_id)
                    result["ssot_row"] = customer["row_number"]
                email = ""
                proposed_email = ""
''')
replace('sales_leads_sacrifice_run.py',
    '        results.append(result)\n        from outreach_evidence import save_local_evidence\n',
    '''        if ssot_tracker is not None:
            try:
                result["ssot_tracking"] = ssot_tracker.record_execution(candidate, result, run_id)
            except Exception as exc:
                result["ssot_tracking_error"] = f"{type(exc).__name__}:{exc}"
        results.append(result)
        from outreach_evidence import save_local_evidence
''')
replace('sales_leads_sacrifice_run.py',
    '        if execute_external and authorization is not None:\n            _record_attempt(',
    '        if execute_external:\n            _record_attempt(')
# Expose SSOT persistence failures in the existing counts-only cycle summary.
replace('outreach_cycle.py', '        if row.get("critical_errors"):\n',
    '        if row.get("ssot_tracking_error"):\n            blockers["ssot_tracking_unconfirmed"] += 1\n        if row.get("critical_errors"):\n')
# A returned ID must identify the exact customer message, not another old email.
replace('customer_care.py',
    '        if kind == "SENT" and (not row.get("AI_送信予約ID")',
    '        if receipt.get("subject") != row.get("営業メール件名") or receipt.get("body_sha256") != hashlib.sha256(str(row.get("営業メール本文") or "").encode()).hexdigest():\n            raise ValueError("RECEIPT_MESSAGE_BYTES_MISMATCH")\n        if kind == "SENT" and (not row.get("AI_送信予約ID")')
# Make canonical legacy contact detection understand these factual events.
replace('customer_care.py', '    merged = dumps(events + [event])\n',
    '    stored_event = dict(event)\n    stored_event["status"] = event.get("kind")\n    stored_event["event_type"] = "OUTBOUND_SENT" if event.get("kind") in {"SENT", "MANUAL_SENT"} else event.get("kind")\n    merged = dumps(events + [stored_event])\n')
replace('customer_care.py', '        if prior != event:\n',
    '        expected = {**event, "status": event.get("kind"), "event_type": "OUTBOUND_SENT" if event.get("kind") in {"SENT", "MANUAL_SENT"} else event.get("kind")}\n        if prior != expected:\n')
# Do not expand operational authorization in a code migration. The existing
# sender remains on its current campaign until the private SSOT schema is ready.
print('customer_first_patch=applied; production_writes=0; customer_sends=0')
