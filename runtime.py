from __future__ import annotations

from collections import deque
from urllib.parse import urlparse

from models import Source
from safe_fetch import TrustedFetcher
from safety import same_host_or_subdomain
from sandbox_runner import run_adapter


def run_source(source: Source, code: str, fetcher: TrustedFetcher, max_records: int) -> tuple[list[dict], dict]:
    queue = deque([source.crawl_url])
    seen_urls: set[str] = set()
    records: list[dict] = []
    record_keys: set[str] = set()
    failures = 0

    while queue and len(records) < max_records:
        url = queue.popleft()
        if url in seen_urls:
            continue
        if not same_host_or_subdomain(url, source.crawl_url):
            continue
        seen_urls.add(url)
        snap = fetcher.fetch(url)
        out = run_adapter(code, snap.as_dict())
        page_records = out.get("records", []) if isinstance(out, dict) else []
        next_urls = out.get("next_urls", []) if isinstance(out, dict) else []
        if not page_records:
            failures += 1
        for r in page_records:
            name = str(r.get("company_name", "")).strip()
            domain = str(r.get("domain", "")).strip().lower()
            if not name:
                continue
            key = domain or name.lower()
            if key in record_keys:
                continue
            record_keys.add(key)
            records.append(r)
            if len(records) >= max_records:
                break
        for nxt in next_urls:
            if isinstance(nxt, str) and same_host_or_subdomain(nxt, source.crawl_url) and nxt not in seen_urls:
                queue.append(nxt)

    stats = {
        "pages_fetched": len(seen_urls),
        "requests_used": fetcher.requests,
        "records": len(records),
        "empty_pages": failures,
        "remaining_queue": len(queue),
    }
    return records, stats
