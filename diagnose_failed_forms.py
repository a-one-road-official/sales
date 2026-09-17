"""Inspect the historical last ten failures without resending any outreach.

The old static catalog is forensic input only; the live send queue continues to
come exclusively from the workbook. Full observations survive in the artifact.
"""
from __future__ import annotations

import json
from pathlib import Path

from form_execution import PublicContactFormExecutor
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice_run import _preferred_form_url, _verified_site_draft


def main():
    root = Path('outreach-evidence')
    root.mkdir(exist_ok=True)
    rows = json.loads(Path('data/sales_leads_bpo_verified.json').read_text())[-10:]
    for row in rows:
        record = {'company_name': row['company_name'], 'source_row': row['source_row'],
                  'historical_result': 'UNRECONCILED', 'inspection': 'CURRENT_SITE_PREVIEW_ONLY',
                  'external_submissions': 0}
        try:
            site = inspect_official_site(row['website'], max_pages=3, expected_company=row['company_name'])
            record['website_research'] = site
            draft = _verified_site_draft({**row, 'sacrifice_lane': 'BPO'}, site)
            record['draft'] = draft
            if site['status'] != 'VERIFIED':
                record.update(status=site['status'], reason=site.get('identity_reason') or 'site_unavailable')
            else:
                urls = site.get('forms', []) + site.get('contact_links', [])
                form_url = _preferred_form_url(row['company_name'], site['official_website'], urls)
                if not form_url:
                    record.update(status='NO_CONTACT_FORM', reason='no_verified_contact_page')
                else:
                    result = PublicContactFormExecutor().execute(form_url=form_url,
                        website=site['official_website'], company_name=row['company_name'],
                        subject=draft['subject'], message=draft['body'],
                        idempotency_key='diagnostic-preview', preview_only=True)
                    record.update(status=result['status'], reason=result.get('reason'), form_execution=result)
        except Exception as exc:
            record.update(status='INSPECTION_ERROR', reason=f'{type(exc).__name__}:{exc}')
        (root / (row['source_row'] + '-diagnosis.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(json.dumps({k: record.get(k) for k in ('company_name', 'source_row', 'status', 'reason', 'external_submissions')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
