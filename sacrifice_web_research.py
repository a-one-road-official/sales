"""Website-first research for the isolated sales_leads sacrifice lane."""
from __future__ import annotations

import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from safe_fetch import TrustedFetcher
from sales_leads_sacrifice import _host

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CONTACT_WORDS = ("contact", "inquiry", "enquiry", "お問い合わせ", "相談", "support", "sales")

def inspect_official_site(url: str, *, max_pages: int = 5) -> dict:
    if not url or not _host(url):
        return {"status": "NO_SITE", "official_website": "", "pages": [], "emails": [], "forms": []}
    root = url if "://" in url else f"https://{url}"
    fetcher = TrustedFetcher(root, rps=1.0, max_requests=max_pages, max_bytes=800_000)
    queue, seen, pages, emails, forms, contact_links = [root], set(), [], set(), [], []
    while queue and len(pages) < max_pages:
        current = queue.pop(0)
        if current in seen: continue
        seen.add(current)
        try: snap = fetcher.fetch(current)
        except Exception as exc:
            pages.append({"url": current, "status": "FETCH_FAILED", "error": f"{type(exc).__name__}:{exc}"}); continue
        soup = BeautifulSoup(snap.text, "html.parser")
        found = set(EMAIL_RE.findall(snap.text)); emails.update(found)
        page_forms = [str(f.get("action") or snap.final_url) for f in soup.find_all("form")]; forms.extend(page_forms)
        pages.append({"url": snap.final_url, "status_code": snap.status_code, "title": soup.title.get_text(strip=True) if soup.title else "", "emails": sorted(found), "form_count": len(page_forms), "text_excerpt": soup.get_text(" ", strip=True)[:1200]})
        for href in snap.links:
            path = urlparse(href).path.lower(); label = href.lower()
            if any(word in label or word in path for word in CONTACT_WORDS) and href not in contact_links:
                contact_links.append(href); queue.append(href)
    return {"status": "VERIFIED" if pages and any(p.get("status_code", 0) < 400 for p in pages) else "UNAVAILABLE", "official_website": pages[0].get("url", root) if pages else root, "site_host": _host(root), "pages": pages, "contact_links": contact_links[:20], "emails": sorted(emails), "forms": forms[:20]}
