from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from html import unescape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from parsel import Selector
from readability import Document

FACTORY_TERMS = {
    "additive manufacturing", "industrial automation", "factory automation",
    "manufacturing", "machining", "robotics", "inspection", "production engineering",
    "metrology", "industrial software", "machine tools", "quality control",
}
LOGISTICS_TERMS = {"logistics", "warehouse automation", "supply chain", "intralogistics"}
EXCLUDE_TERMS = {
    "childcare", "daycare", "nursery", "education", "restaurant", "real estate",
    "consumer app", "marketing agency", "staffing agency",
}
DISCOVERY_PATHS = ("/about", "/products", "/solutions", "/industries", "/applications", "/contact")


@dataclass(frozen=True)
class Evidence:
    company_name: str
    url: str
    canonical_url: str
    title: str
    description: str
    headings: tuple[str, ...]
    body: str
    emails: tuple[str, ...]
    links: tuple[str, ...]
    content_hash: str
    fetched_at: str
    http_status: int

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(value: str | None, limit: int = 4000) -> str:
    value = re.sub(r"\s+", " ", unescape(value or "")).strip()
    return value[:limit]


def _absolute(base: str, href: str | None) -> str | None:
    if not href:
        return None
    candidate = urljoin(base, href.strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    return candidate.split("#", 1)[0]


def _same_host(left: str, right: str) -> bool:
    return (urlparse(left).hostname or "").lower().removeprefix("www.") == (urlparse(right).hostname or "").lower().removeprefix("www.")


def extract_evidence(company_name: str, url: str, html: str, status: int, fetched_at: str) -> Evidence:
    selector = Selector(text=html)
    title = _clean(selector.css("title::text").get())
    description = _clean(selector.css('meta[name="description"]::attr(content)').get())
    headings = tuple(_clean(x, 300) for x in selector.css("h1::text, h2::text, h3::text").getall() if _clean(x, 300))[:30]
    body = _clean(Document(html).summary(), 12000)
    links = tuple(dict.fromkeys(x for x in (_absolute(url, h) for h in selector.css("a::attr(href)").getall()) if x))[:100]
    emails = tuple(sorted(set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}", html, re.I))))[:20]
    canonical = _absolute(url, selector.css('link[rel="canonical"]::attr(href)').get()) or url
    fingerprint = hashlib.sha256("|".join((title, description, " ".join(headings), body)).encode()).hexdigest()
    return Evidence(company_name, url, canonical, title, description, headings, body, emails, links, fingerprint, fetched_at, status)


def classify_evidence(evidence: Evidence) -> dict:
    text = " ".join((evidence.title, evidence.description, *evidence.headings, evidence.body)).lower()
    factory_hits = sorted(term for term in FACTORY_TERMS if term in text)
    logistics_hits = sorted(term for term in LOGISTICS_TERMS if term in text)
    exclude_hits = sorted(term for term in EXCLUDE_TERMS if term in text)
    if exclude_hits and not factory_hits and not logistics_hits:
        decision = "NO-GO"
    elif factory_hits or logistics_hits:
        decision = "GO"
    else:
        decision = "REVIEW"
    category = "Factory" if factory_hits else "Logistics" if logistics_hits else "Other"
    return {"decision": decision, "category": category, "factory_hits": factory_hits, "logistics_hits": logistics_hits, "exclude_hits": exclude_hits}


def crawl_company(company_name: str, url: str, *, timeout: float = 12.0, max_bytes: int = 1_000_000, fetched_at: str = "") -> dict:
    headers = {"User-Agent": "A-one-road-LeadCrawler/1.0 (+https://a1-road.com)"}
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        response = client.get(url)
        content = response.content[: max_bytes + 1]
        if len(content) > max_bytes:
            raise ValueError("response_too_large")
        html = content.decode(response.encoding or "utf-8", errors="replace")
        evidence = extract_evidence(company_name, str(response.url), html, response.status_code, fetched_at)
    decision = classify_evidence(evidence)
    return {"evidence": evidence.to_dict(), "decision": decision}


def dedupe_records(records: Iterable[dict]) -> list[dict]:
    seen: set[str] = set()
    output: list[dict] = []
    for record in records:
        evidence = record.get("evidence", {})
        key = (urlparse(evidence.get("canonical_url", "")).hostname or "").lower().removeprefix("www.")
        key = key or hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output
