from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import tldextract


LEGAL_WORDS = {
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "company", "co",
    "gmbh", "ag", "sa", "sas", "spa", "plc", "private", "pvt", "llc", "kg", "kgaa",
    "srl", "bv", "nv", "oy", "ab", "as", "group", "holding", "holdings", "the",
    "sti", "stti", "lsti", "anonim", "sirketi", "sir", "tic", "ticaret", "sanayi",
    "hizmetleri", "hizmet", "yazilim",
}

COUNTRY_SUFFIXES = {
    "india": [".in", ".co.in"],
    "germany": [".de"],
    "austria": [".at"],
    "italy": [".it"],
    "netherlands": [".nl"],
    "switzerland": [".ch"],
    "france": [".fr"],
    "united kingdom": [".co.uk"],
    "uk": [".co.uk"],
    "japan": [".co.jp"],
    "taiwan": [".com.tw", ".tw"],
    "turkey": [".com.tr", ".tr"],
    "türkiye": [".com.tr", ".tr"],
    "poland": [".pl"],
    "czech republic": [".cz"],
    "czechia": [".cz"],
    "spain": [".es"],
    "portugal": [".pt"],
    "sweden": [".se"],
    "denmark": [".dk"],
    "norway": [".no"],
    "finland": [".fi"],
    "belgium": [".be"],
    "israel": [".co.il", ".il"],
    "singapore": [".com.sg", ".sg"],
    "south korea": [".co.kr", ".kr"],
    "korea": [".co.kr", ".kr"],
    "australia": [".com.au", ".au"],
}


def ascii_fold(value: str) -> str:
    value = str(value or "")
    # NFKD handles Latin diacritics (e.g. Ü/Ş/İ) without another runtime dependency.
    folded = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).encode("ascii", "ignore").decode("ascii")


def company_tokens(name: str) -> list[str]:
    folded = ascii_fold(name).lower()
    return [
        token
        for token in re.findall(r"[a-z0-9]+", folded)
        if len(token) >= 2 and token not in LEGAL_WORDS
    ]


def company_domain_hints(name: str, country: str = "", limit: int = 24) -> list[str]:
    tokens = company_tokens(name)
    if not tokens:
        return []

    # Distinctive short forms catch cases such as ZWSOFT and ZÜMRESOFT, while
    # compound forms cover conventional legal names.
    bases: list[str] = []
    joined = "".join(tokens)
    dashed = "-".join(tokens)
    first = tokens[0]
    first_two = "".join(tokens[:2]) if len(tokens) >= 2 else first
    first_two_dash = "-".join(tokens[:2]) if len(tokens) >= 2 else first
    for value in (first, first_two, first_two_dash, joined, dashed):
        value = re.sub(r"[^a-z0-9-]", "", value).strip("-")
        if len(value) >= 3 and value not in bases:
            bases.append(value)

    suffixes = list(COUNTRY_SUFFIXES.get(str(country or "").strip().lower(), []))
    for suffix in (".com", ".io", ".ai"):
        if suffix not in suffixes:
            suffixes.append(suffix)

    out: list[str] = []
    for base in bases:
        for suffix in suffixes:
            url = f"https://{base}{suffix}"
            if url not in out:
                out.append(url)
            if len(out) >= limit:
                return out
    return out


def registrable_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    host = urlparse(raw if "://" in raw else f"https://{raw}").hostname or ""
    host = host.lower().rstrip(".")
    if not host:
        return ""
    ext = tldextract.TLDExtract(suffix_list_urls=None)(host)
    return ext.top_domain_under_public_suffix or host


def _identity_names_from_jsonld(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []
    for node in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            payload = json.loads(node.string or node.get_text("", strip=True) or "{}")
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(k).lower() in {"organization", "corporation", "localbusiness", "website"} for k in kinds):
                for key in ("name", "legalName", "alternateName"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        names.append(value.strip())
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
    return names


def page_identity_names(html: str) -> list[str]:
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        return []
    names: list[str] = []
    if soup.title and soup.title.get_text(strip=True):
        names.append(soup.title.get_text(" ", strip=True))
    for attrs in (
        {"property": "og:site_name"},
        {"name": "application-name"},
        {"name": "twitter:title"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            names.append(str(tag.get("content")).strip())
    names.extend(_identity_names_from_jsonld(soup))
    return list(dict.fromkeys(x for x in names if x))


def name_similarity(company_name: str, candidate_name: str) -> float:
    left = " ".join(company_tokens(company_name))
    right = " ".join(company_tokens(candidate_name))
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def candidate_identity_score(company_name: str, url: str, html: str) -> tuple[int, list[str]]:
    tokens = company_tokens(company_name)
    host = registrable_domain(url)
    label = host.split(".", 1)[0].replace("-", "")
    joined = "".join(tokens)

    score = 0
    reasons: list[str] = []
    if joined and (joined in label or label in joined) and min(len(joined), len(label)) >= 4:
        score += 4
        reasons.append("host_name_match")
    elif tokens and any(len(t) >= 4 and t in label for t in tokens):
        score += 3
        reasons.append("host_token_match")

    names = page_identity_names(html)
    best = max((name_similarity(company_name, n) for n in names), default=0.0)
    if best >= 0.88:
        score += 4
        reasons.append("page_identity_strong")
    elif best >= 0.68:
        score += 2
        reasons.append("page_identity_partial")

    visible = BeautifulSoup(str(html or ""), "html.parser").get_text(" ", strip=True).lower()[:120000]
    visible_folded = ascii_fold(visible).lower()
    hits = [t for t in tokens if len(t) >= 4 and re.search(rf"\b{re.escape(t)}\b", visible_folded)]
    if hits:
        score += 2
        reasons.append("visible_name_token")
    return score, reasons


def source_record_candidates(
    source_record_url: str,
    html: str,
    company_name: str,
    blocked_hosts: set[str],
    source_host: str = "",
    limit: int = 12,
) -> list[str]:
    """Extract likely first-party links from an exhibitor/member record or listing.

    Directory links are evidence candidates only; the caller must still verify
    the destination as first-party. This deliberately recovers information that
    many trade-fair directories expose in hrefs while keeping verification strict.
    """
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        return []
    tokens = company_tokens(company_name)
    candidates: list[tuple[float, str]] = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(source_record_url, str(a.get("href") or "").strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")
        if not host or host == source_host or (source_host and host.endswith("." + source_host)):
            continue
        if any(host == b or host.endswith("." + b) for b in blocked_hosts):
            continue

        anchor = ascii_fold(a.get_text(" ", strip=True)).lower()
        href_folded = ascii_fold(absolute).lower()
        score = 0.0
        if tokens:
            score += sum(1.5 for t in tokens if len(t) >= 4 and t in anchor)
            score += sum(1.0 for t in tokens if len(t) >= 4 and t in href_folded)
        if any(k in anchor for k in ("website", "web site", "homepage", "company site", "visit site")):
            score += 1.5
        if score > 0:
            candidates.append((score, absolute))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, url in candidates:
        rd = registrable_domain(url)
        if not rd or rd in seen:
            continue
        seen.add(rd)
        out.append(url)
        if len(out) >= limit:
            break
    return out
