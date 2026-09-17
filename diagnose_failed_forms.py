"""Inspect the historical last ten failures without resending any outreach.

The old static catalog is forensic input only; the live send queue continues to
come exclusively from the workbook. Full observations survive in the artifact.
"""
from __future__ import annotations

import json
import argparse
from pathlib import Path

from form_execution import PublicContactFormExecutor
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice_run import _preferred_form_url, _verified_site_draft


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest')
    args = parser.parse_args()
    root = Path('outreach-evidence')
    root.mkdir(exist_ok=True)
    rows = json.loads(Path(args.manifest or 'data/sales_leads_bpo_verified.json').read_text())[-10:]
    for row in rows:
        record = {'company_name': row['company_name'], 'source_row': row['source_row'],
                  'historical_result': 'NOT_SENT' if args.manifest else 'UNRECONCILED', 'inspection': 'CURRENT_SITE_PREVIEW_ONLY',
                  'external_submissions': 0}
        try:
            site = inspect_official_site(row['website'], max_pages=3, expected_company=row['company_name'])
            record['website_research'] = site
            draft = _verified_site_draft({**row, 'sacrifice_lane': row.get('sacrifice_lane', 'BPO')}, site)
            record['draft'] = draft
            if site['status'] != 'VERIFIED':
                record.update(status=site['status'], reason=site.get('identity_reason') or 'site_unavailable')
            else:
                urls = site.get('forms', []) + site.get('contact_links', [])
                form_url = _preferred_form_url(row['company_name'], site['official_website'], urls)
                if not form_url:
                    record.update(status='NO_CONTACT_FORM', reason='no_verified_contact_page')
                else:
                    preview = PublicContactFormExecutor().preview_candidates(
                        form_urls=[form_url] + urls, website=site['official_website'],
                        company_name=row['company_name'], subject=draft['subject'], message=draft['body'],
                        compact_message=draft.get('compact_body', ''))
                    if preview['ready']:
                        record['draft']['body'] = preview['message']
                    record['form_previews'] = preview['attempts']
                    result = preview['attempts'][-1]
                    record.update(status=result['status'], reason=result.get('reason'), form_execution=result)
        except Exception as exc:
            record.update(status='INSPECTION_ERROR', reason=f'{type(exc).__name__}:{exc}')
        (root / (row['source_row'] + '-diagnosis.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(json.dumps({k: record.get(k) for k in ('company_name', 'source_row', 'status', 'reason', 'external_submissions')}, ensure_ascii=False), flush=True)
        form = record.get('form_execution') or {}
        print(json.dumps({'company_name': row['company_name'], 'form_url': form.get('form_url'),
                          'missing_required': form.get('missing_required'), 'core_unfilled': form.get('core_unfilled'),
                          'fields': [{k: f.get(k) for k in ('key', 'label', 'type', 'required', 'maxlength', 'action', 'error')}
                                     for f in form.get('field_audit', [])],
                          'pages': [{k: p.get(k) for k in ('url', 'status_code', 'status', 'error')}
                                    for p in record.get('website_research', {}).get('pages', [])]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
