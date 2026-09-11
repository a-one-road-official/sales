from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

from domain_tools import ascii_fold, company_tokens, registrable_domain


@dataclass
class RankedDomain:
    domain: str
    score: float
    confidence: float
    features: dict
    urls: list[str]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", ascii_fold(str(value or "")).lower()).strip()


def _token_overlap(a: str, b: str) -> float:
    left = set(company_tokens(a))
    right = set(company_tokens(b))
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left))


def _host_similarity(company_name: str, domain: str) -> float:
    tokens = company_tokens(company_name)
    label = registrable_domain(domain).split(".", 1)[0].replace("-", "")
    if not tokens or not label:
        return 0.0
    joined = "".join(tokens)
    if joined == label:
        return 1.0
    if joined in label or label in joined:
        return 0.92
    hits = sum(1 for t in tokens if len(t) >= 3 and t in label)
    return min(0.85, hits / max(1, len(tokens)))


def _rank_weight(rank: int) -> float:
    r = max(1, int(rank or 1))
    return 1.0 / math.log2(r + 1.0)


def rank_search_candidates(company: dict, search_results: list[dict], blocked_hosts: set[str]) -> list[RankedDomain]:
    """CBS/urlfinding-inspired aggregate ranking, implemented independently.

    The Dutch design's key idea is preserved: collect several search queries,
    aggregate repeated hosts, combine search rank with identity features, then
    emit a probability-like confidence instead of trusting one search result.
    """
    company_name = str(company.get("company_name") or "")
    country = _norm(str(company.get("hq_country") or ""))
    product = _norm(str(company.get("product_category") or company.get("what_it_solves") or ""))

    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in search_results or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "").strip()
        rd = registrable_domain(url)
        if not rd:
            continue
        if any(rd == b or rd.endswith("." + b) for b in blocked_hosts):
            continue
        grouped[rd].append(item)

    total_hits = max(1, sum(len(v) for v in grouped.values()))
    query_count_total = max(1, len({str(x.get("query_type") or "") for x in search_results if isinstance(x, dict)}))
    ranked: list[RankedDomain] = []

    for domain, items in grouped.items():
        query_types = {str(x.get("query_type") or "") for x in items}
        frequency = len(items) / total_hits
        query_coverage = len(query_types) / query_count_total
        rank_score = sum(_rank_weight(int(x.get("rank") or 10)) for x in items) / max(1, len(items))
        host_sim = _host_similarity(company_name, domain)

        title_overlap = max(
            (_token_overlap(company_name, str(x.get("title") or "")) for x in items),
            default=0.0,
        )
        snippet_overlap = max(
            (_token_overlap(company_name, str(x.get("snippet") or "")) for x in items),
            default=0.0,
        )

        country_hit = 0.0
        if country:
            corpus = " ".join(_norm(str(x.get("title") or "") + " " + str(x.get("snippet") or "")) for x in items)
            country_hit = 1.0 if country in corpus else 0.0

        product_hit = 0.0
        if product:
            corpus = " ".join(_norm(str(x.get("title") or "") + " " + str(x.get("snippet") or "")) for x in items)
            p_tokens = [t for t in product.split() if len(t) >= 4]
            if p_tokens:
                product_hit = sum(1 for t in p_tokens if t in corpus) / len(p_tokens)

        # Weighted score follows the same family of signals as urlfinding:
        # repeated host frequency, position in search results, and similarity of
        # enterprise fields against result title/snippet. Geography/product are
        # A-one additions for international industrial disambiguation.
        score = (
            0.24 * frequency
            + 0.18 * query_coverage
            + 0.17 * rank_score
            + 0.18 * host_sim
            + 0.11 * title_overlap
            + 0.05 * snippet_overlap
            + 0.04 * country_hit
            + 0.03 * product_hit
        )
        score = max(0.0, min(1.0, score))
        # Confidence curve intentionally has a high bar; direct site verification
        # remains authoritative after ranking.
        confidence = 1.0 / (1.0 + math.exp(-10.0 * (score - 0.50)))

        urls = []
        for x in sorted(items, key=lambda y: int(y.get("rank") or 999)):
            u = str(x.get("url") or x.get("link") or "").strip()
            if u and u not in urls:
                urls.append(u)

        ranked.append(RankedDomain(
            domain=domain,
            score=score,
            confidence=confidence,
            features={
                "frequency": round(frequency, 6),
                "query_coverage": round(query_coverage, 6),
                "rank_score": round(rank_score, 6),
                "host_similarity": round(host_sim, 6),
                "title_overlap": round(title_overlap, 6),
                "snippet_overlap": round(snippet_overlap, 6),
                "country_hit": round(country_hit, 6),
                "product_hit": round(product_hit, 6),
                "hit_count": len(items),
                "query_count": len(query_types),
            },
            urls=urls[:5],
        ))

    ranked.sort(key=lambda x: (x.score, x.confidence, len(x.urls)), reverse=True)
    return ranked
