"""Website-first contact discovery for the isolated EC sacrifice lane."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from sales_leads_sacrifice import _host

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CONTACT_WORDS = ("contact", "inquiry", "enquiry", "sales", "support", "demo", "get in touch", "talk to sales", "お問い合わせ")


def inspect_official_site(url: str, *, max_pages: int = 5, expected_company: str = "") -> dict:
    if not url or not _host(url):
        return {"status": "NO_SITE", "official_website": "", "pages": [], "emails": [], "forms": []}
    root = url if "://" in url else f"https://{url}"
    queue, seen, pages, emails, forms, contact_links = [root], set(), [], set(), [], []
    headers = {"User-Agent": "A-one-road/1.0 contact-research"}
    while queue and len(pages) < max_pages:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            response = requests.get(current, headers=headers, timeout=20, allow_redirects=True)
            text = response.text[:800_000]
            soup = BeautifulSoup(text, "html.parser")
            found = set(EMAIL_RE.findall(text))
            emails.update(found)
            page_forms = [urljoin(response.url, str(f.get("action") or response.url)) for f in soup.find_all("form")]
            forms.extend(page_forms)
            pages.append({"url": response.url, "status_code": response.status_code,
                          "title": soup.title.get_text(strip=True) if soup.title else "",
                          "emails": sorted(found), "form_count": len(page_forms),
                          "text_excerpt": soup.get_text(" ", strip=True)[:1500]})
            for anchor in soup.find_all("a", href=True):
                href = urljoin(response.url, anchor["href"])
                if urlparse(href).netloc and _host(href) != _host(root):
                    continue
                label = f"{anchor.get_text(' ', strip=True)} {href}".lower()
                if any(word in label for word in CONTACT_WORDS) and href not in contact_links:
                    contact_links.append(href)
                    queue.append(href)
        except Exception as exc:
            pages.append({"url": current, "status": "FETCH_FAILED", "error": f"{type(exc).__name__}:{exc}"})
    ok = any(int(page.get("status_code", 0) or 0) < 400 for page in pages)
    identity_match = True
    identity_reason = ""
    if expected_company and ok:
        expected_tokens = [
            token for token in re.findall(r"[a-z0-9]+", str(expected_company).lower())
            if len(token) >= 4 and token not in {"company", "group", "holdings", "technologies"}
        ]
        visible = " ".join(
            f"{page.get('title', '')} {page.get('text_excerpt', '')}"
            for page in pages[:3]
            if isinstance(page, dict)
        ).lower()
        visible_compact = re.sub(r"[^a-z0-9]", "", visible)
        host_compact = re.sub(r"[^a-z0-9]", "", _host(root).lower())
        identity_match = bool(
            expected_tokens
            and any(token in visible_compact or token in host_compact for token in expected_tokens)
        )
        if not identity_match:
            identity_reason = "expected_company_not_present_in_site_identity"
    verified = ok and identity_match
    return {"status": "VERIFIED" if verified else "UNAVAILABLE" if not ok else "IDENTITY_MISMATCH",
            "official_website": pages[0].get("url", root) if pages else root,
            "site_host": _host(root), "identity_match": identity_match,
            "identity_reason": identity_reason, "pages": pages,
            "contact_links": contact_links[:20], "emails": sorted(emails), "forms": forms[:20]}
