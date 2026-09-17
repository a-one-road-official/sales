"""Website-first contact discovery for the isolated EC sacrifice lane.

The Playwright mode is the production path for the EC sacrifice runner. It
renders JavaScript-heavy pages and exposes only same-host GET/HEAD snapshots.
Requests mode remains available for internal non-outbound callers.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from browser_fetch import TrustedBrowserFetcher
from sales_leads_sacrifice import _host

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CONTACT_WORDS = (
    "contact",
    "inquiry",
    "enquiry",
    "sales",
    "support",
    "demo",
    "get in touch",
    "talk to sales",
    "お問い合わせ",
    "partnership", "get-in-touch", "book-a-call", "connect-with",
)


def contact_priority(url: str) -> tuple[int, int]:
    """Contact intent outranks product pages containing the word 'sales'."""
    path = urlparse(url).path.casefold()
    segments = re.split(r"[/_-]+", path)
    score = 0
    if any(word in segments for word in ('contact', 'inquiry', 'enquiry')):
        score += 20
    if 'partner' in segments or 'partnership' in segments or 'partnerships' in segments:
        score += 12
    if any(word in path for word in ('get-in-touch', 'book-a-call', 'talk-to')):
        score += 10
    if 'demo' in segments:
        score += 5
    if any(word in segments for word in ('services', 'products', 'features', 'news', 'blog', 'careers', 'login', 'signup', 'privacy', 'support')):
        score -= 15
    # Prefer the general or English contact page over a lexicographically later
    # translated page. This does not change the recipient's regional selection.
    if re.search(r'^/(?:fr|de|es|pt|it|ko|zh)(?:[-/]|$)', path):
        score -= 8
    return score, -len(path)


def ordered_contact_links(links):
    cleaned = list(dict.fromkeys(urldefrag(link)[0] for link in links))
    return sorted(cleaned, key=contact_priority, reverse=True)


def _request_timeout() -> float:
    try:
        value = float(os.getenv("OUTREACH_SITE_REQUEST_TIMEOUT_SECONDS", "12") or 12)
    except (TypeError, ValueError):
        value = 12.0
    return max(3.0, min(60.0, value))


def _append_page(
    pages: list[dict],
    emails: set[str],
    forms: list[str],
    current: str,
    *,
    status_code: int,
    title: str,
    text: str,
    links: list[str],
) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(text, "html.parser")
    found = set(EMAIL_RE.findall(text))
    emails.update(found)
    # Newsletter/search/login forms cannot carry an outreach message. Embedded
    # contact forms are opened by the executor from the contact links below.
    def message_form(form):
        return bool(form.find("textarea") or form.find("input", attrs={"name": re.compile(r"message|inquiry|enquiry|comment", re.I)}))
    page_forms = [current] if any(message_form(f) for f in soup.find_all("form")) else []
    forms.extend(page_forms)
    pages.append(
        {
            "url": current,
            "status_code": int(status_code or 0),
            "title": title or (soup.title.get_text(strip=True) if soup.title else ""),
            "emails": sorted(found),
            "form_count": len(page_forms),
            "text_excerpt": soup.get_text(" ", strip=True)[:1500],
        }
    )
    contact_links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(current, str(anchor["href"]))
        label = f"{anchor.get_text(' ', strip=True)} {href}".lower()
        if any(word in label for word in CONTACT_WORDS):
            contact_links.append(href)
    contact_links.extend(
        value for value in links
        if any(word in str(value).lower() for word in CONTACT_WORDS)
    )
    return ordered_contact_links(contact_links), page_forms


def _inspect_with_requests(
    root: str,
    *,
    max_pages: int,
) -> tuple[list[dict], set[str], list[str], list[str]]:
    queue, seen = [root], set()
    pages: list[dict] = []
    emails: set[str] = set()
    forms: list[str] = []
    contact_links: list[str] = []
    headers = {"User-Agent": "A-one-road/1.0 contact-research"}
    while queue and len(pages) < max_pages:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            response = requests.get(
                current,
                headers=headers,
                timeout=_request_timeout(),
                allow_redirects=True,
            )
            text = response.text[:800_000]
            links, _ = _append_page(
                pages,
                emails,
                forms,
                response.url,
                status_code=response.status_code,
                title="",
                text=text,
                links=[],
            )
            for href in links:
                if _host(href) == _host(root) and href not in seen and href not in queue:
                    queue.append(href)
            contact_links.extend(links)
        except Exception as exc:
            pages.append(
                {
                    "url": current,
                    "status": "FETCH_FAILED",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
    return pages, emails, forms, ordered_contact_links(contact_links)[:20]


def _inspect_with_playwright(
    root: str,
    *,
    max_pages: int,
) -> tuple[list[dict], set[str], list[str], list[str]]:
    queue, seen = [root], set()
    pages: list[dict] = []
    emails: set[str] = set()
    forms: list[str] = []
    contact_links: list[str] = []
    # TrustedBrowserFetcher applies the same-host restriction and blocks all
    # non-GET/HEAD requests during research. One fetcher is reused for the
    # bounded page budget.
    fetcher = TrustedBrowserFetcher(
        root,
        rps=max(0.05, float(os.getenv("OUTREACH_SITE_RPS", "0.5") or 0.5)),
        max_requests=max_pages,
        max_bytes=800_000,
    )
    while queue and len(pages) < max_pages:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            snapshot = fetcher.fetch(current)
            links, _ = _append_page(
                pages,
                emails,
                forms,
                snapshot.final_url or current,
                status_code=snapshot.status_code,
                title=snapshot.title,
                text=snapshot.text,
                links=snapshot.links,
            )
            for href in links:
                if _host(href) == _host(root) and href not in seen and href not in queue:
                    queue.append(href)
            contact_links.extend(links)
        except Exception as exc:
            pages.append(
                {
                    "url": current,
                    "status": "FETCH_FAILED",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
    return pages, emails, forms, ordered_contact_links(contact_links)[:20]


def inspect_official_site(
    url: str,
    *,
    max_pages: int = 5,
    expected_company: str = "",
) -> dict:
    if not url or not _host(url):
        return {"status": "NO_SITE", "official_website": "", "pages": [], "emails": [], "forms": []}
    root = url if "://" in url else f"https://{url}"
    max_pages = max(1, min(8, int(max_pages or 1)))
    mode = str(os.getenv("OUTREACH_SITE_FETCH_MODE", "REQUESTS") or "REQUESTS").strip().upper()
    inspector = _inspect_with_playwright if mode == "PLAYWRIGHT" else _inspect_with_requests
    try:
        pages, emails, forms, contact_links = inspector(root, max_pages=max_pages)
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "official_website": root,
            "site_host": _host(root),
            "identity_match": False,
            "identity_reason": f"inspector_failed:{type(exc).__name__}:{exc}",
            "pages": [],
            "contact_links": [],
            "emails": [],
            "forms": [],
        }

    ok = any(200 <= int(page.get("status_code", 0) or 0) < 400 for page in pages)
    identity_match = True
    identity_reason = ""
    if expected_company and ok:
        expected_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", str(expected_company).lower())
            if len(token) >= 4
            and token not in {"company", "group", "holdings", "technologies"}
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
            and any(
                token in visible_compact or token in host_compact
                for token in expected_tokens
            )
        )
        if not identity_match:
            identity_reason = "expected_company_not_present_in_site_identity"
    verified = ok and identity_match
    return {
        "status": "VERIFIED" if verified else "UNAVAILABLE" if not ok else "IDENTITY_MISMATCH",
        "official_website": pages[0].get("url", root) if pages else root,
        "site_host": _host(root),
        "identity_match": identity_match,
        "identity_reason": identity_reason,
        "pages": pages,
        "contact_links": contact_links[:20],
        "emails": sorted(emails),
        "forms": forms[:20],
    }
