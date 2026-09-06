from __future__ import annotations

import re
from bs4 import BeautifulSoup

from browser_fetch import TrustedBrowserFetcher
from models import ProbeResult, Source
from safe_fetch import TrustedFetcher


def _build_probe(source: Source, snap, render_mode: str) -> ProbeResult:
    auth, auth_reason = TrustedFetcher.auth_required(snap)
    soup = BeautifulSoup(snap.text, "html.parser") if "html" in snap.content_type.lower() or "<html" in snap.text[:1000].lower() else None
    title = snap.title or (soup.title.get_text(" ", strip=True) if soup and soup.title else "")
    html = snap.text
    low = html.lower()
    nextish = []
    if soup:
        for a in soup.find_all("a", href=True):
            label = a.get_text(" ", strip=True).lower()
            if label in ("next", "next ›", ">", "›") or re.search(r"page[=/]\d+", a.get("href", ""), re.I):
                nextish.append(a.get("href", ""))
    signals = {
        "has_next_or_page_links": bool(nextish),
        "script_count": len(soup.find_all("script")) if soup else 0,
        "has_next_data": "__next_data__" in low,
        "has_graphql_text": "graphql" in low or any("graphql" in x.get("url", "").lower() for x in (snap.network or [])),
        "has_api_text": bool(re.search(r"/api/|api\.", low)) or bool(snap.api_payloads),
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "link_count": len(snap.links),
        "network_count": len(snap.network or []),
        "api_payload_count": len(snap.api_payloads or []),
    }
    return ProbeResult(
        source_id=source.source_id, url=source.crawl_url, final_url=snap.final_url,
        status_code=snap.status_code, content_type=snap.content_type, render_mode=render_mode,
        auth_required=auth, auth_reason=auth_reason, title=title, html=html,
        links=snap.links, signals=signals, network=snap.network or [], api_payloads=snap.api_payloads or [],
    )


def probe_source(source: Source, fetcher: TrustedFetcher, browser_fetcher: TrustedBrowserFetcher | None = None) -> ProbeResult:
    snap = fetcher.fetch(source.crawl_url)
    preliminary = _build_probe(source, snap, "HTML")
    likely_js = preliminary.signals["has_next_data"] or (
        preliminary.signals["script_count"] > 20 and len(BeautifulSoup(snap.text, "html.parser").get_text(" ", strip=True)) < 3000
    )
    if "application/pdf" in snap.content_type.lower():
        preliminary.render_mode = "PDF"
        return preliminary
    # If HTML looks auth-gated but an approved browser session exists, retry through the trusted browser first.
    if browser_fetcher is not None and (likely_js or preliminary.auth_required):
        rendered = browser_fetcher.fetch(source.crawl_url)
        rendered_probe = _build_probe(source, rendered, "RENDERED_HTML")
        if not rendered_probe.auth_required or likely_js:
            return rendered_probe
    return preliminary
