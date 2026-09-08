from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from gate_worker import GateWorker
from mittelstand_worker import MittelstandWorker
from orchestrator import LeadFactory as BaseLeadFactory
from safe_fetch import TrustedFetcher


THIRD_PARTY_HOSTS = {
    "weblio.jp", "wikipedia.org", "wikidata.org", "linkedin.com", "crunchbase.com",
    "facebook.com", "instagram.com", "youtube.com", "x.com", "twitter.com",
    "bloomberg.com", "reuters.com", "pitchbook.com", "zoominfo.com", "rocketreach.co",
    "kompass.com", "europages.com", "globalspec.com", "indiamart.com", "tradeindia.com",
    "alibaba.com", "made-in-china.com", "google.com", "bing.com", "yahoo.com",
}

LEGAL_STOPWORDS = {
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "company", "co",
    "gmbh", "ag", "sa", "sas", "spa", "plc", "private", "pvt", "llc", "kg", "kgaa",
    "srl", "bv", "nv", "oy", "ab", "as", "group", "holding", "holdings",
}


def _host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        return (urlparse(candidate).hostname or "").lower().rstrip(".").removeprefix("www.")
    except Exception:
        return ""


def _blocked(host: str) -> bool:
    host = _host(host)
    return bool(host and any(host == b or host.endswith("." + b) for b in THIRD_PARTY_HOSTS))


def _tokens(name: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", str(name or "").lower())
        if len(t) >= 3 and t not in LEGAL_STOPWORDS
    ]


def _visible_text(raw: str) -> str:
    try:
        soup = BeautifulSoup(str(raw or ""), "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(raw or ""))).strip()


class OfficialSiteResolver:
    def __init__(self, sheets, llm=None):
        self.sheets = sheets
        self.llm = llm

    def _cfg(self) -> dict[str, str]:
        try:
            return self.sheets.get_config()
        except Exception:
            return {}

    def _fetch(self, url: str, max_requests: int = 2):
        cfg = self._cfg()
        return TrustedFetcher(
            source_url=url,
            rps=float(cfg.get("LEAD_FACTORY_SOURCE_RPS", "0.5") or 0.5),
            max_requests=max_requests,
            max_bytes=int(cfg.get("OFFICIAL_SITE_HTTP_MAX_BYTES", "750000") or 750000),
        ).fetch(url)

    def _same_source_host(self, url: str, company: dict) -> bool:
        h = _host(url)
        sources = {_host(company.get("source_record_url", "")), _host(company.get("source_url", ""))}
        sources.discard("")
        return bool(h and h in sources)

    def _verify(self, company: dict, url: str, source_direct: bool = False) -> dict:
        h = _host(url)
        if not h:
            return {"verified": False, "reason": "invalid_host"}
        if _blocked(h):
            return {"verified": False, "reason": "third_party_host"}
        if self._same_source_host(url, company):
            return {"verified": False, "reason": "source_directory_host"}
        try:
            snap = self._fetch(url, 2)
        except Exception as exc:
            return {"verified": False, "reason": f"fetch:{type(exc).__name__}"}
        if int(getattr(snap, "status_code", 500) or 500) >= 400:
            return {"verified": False, "reason": f"http_{getattr(snap, 'status_code', 500)}"}
        final_url = str(getattr(snap, "final_url", "") or url)
        final_host = _host(final_url)
        if not final_host or _blocked(final_host) or self._same_source_host(final_url, company):
            return {"verified": False, "reason": "redirect_not_first_party"}

        company_tokens = _tokens(company.get("company_name", ""))
        host_flat = final_host.replace("-", "").replace("_", "")
        host_hits = [t for t in company_tokens if t in host_flat]
        text = _visible_text(getattr(snap, "text", ""))[:160000].lower()
        text_hits = [t for t in company_tokens if re.search(rf"\b{re.escape(t)}\b", text)]
        score = (3 if host_hits else 0) + (2 if text_hits else 0) + (2 if source_direct else 0)
        country = str(company.get("hq_country") or "").strip().lower()
        if country and country in text:
            score += 1
        verified = bool(host_hits or text_hits) and score >= 4
        return {
            "verified": verified,
            "official_domain": final_host if verified else "",
            "official_website": final_url if verified else "",
            "confidence": "HIGH" if verified else "LOW",
            "reason": "verified_first_party_identity" if verified else "insufficient_first_party_identity",
            "evidence": [final_url],
        }

    def resolve(self, company: dict) -> dict:
        candidates: list[tuple[str, bool, str]] = []
        existing = str(company.get("website") or company.get("domain") or "").strip()
        if existing:
            if not existing.startswith(("http://", "https://")):
                existing = "https://" + existing
            candidates.append((existing, False, "raw_candidate"))

        record_url = str(company.get("source_record_url") or company.get("source_url") or "").strip()
        if record_url.startswith(("http://", "https://")):
            try:
                snap = self._fetch(record_url, 1)
                for url in list(getattr(snap, "external_links", []) or []):
                    candidates.append((str(url), True, "source_external_link"))
            except Exception:
                pass

        if self.llm is not None:
            try:
                research = self.llm.resolve_company_domain(company)
                for key in ("official_website", "official_domain"):
                    value = str(research.get(key) or "").strip()
                    if value:
                        if not value.startswith(("http://", "https://")):
                            value = "https://" + value
                        candidates.append((value, False, "research_candidate"))
            except Exception:
                pass

        seen: set[str] = set()
        rejected: list[str] = []
        candidates.sort(key=lambda x: (not x[1], 0 if x[2] == "raw_candidate" else 1))
        for url, source_direct, origin in candidates:
            h = _host(url)
            if not h or h in seen:
                continue
            seen.add(h)
            result = self._verify(company, url, source_direct=source_direct)
            if result.get("verified"):
                return {
                    "official_domain": result["official_domain"],
                    "official_website": result["official_website"],
                    "hq_country": company.get("hq_country", ""),
                    "confidence": "HIGH",
                    "verification": "VERIFIED_FIRST_PARTY",
                    "evidence": [origin] + list(result.get("evidence", [])),
                }
            rejected.append(f"{origin}:{h}:{result.get('reason', 'rejected')}")
        return {
            "official_domain": "",
            "official_website": "",
            "hq_country": company.get("hq_country", ""),
            "confidence": "LOW",
            "verification": "UNRESOLVED_OFFICIAL_SITE",
            "evidence": rejected[:20] or ["no_official_site_candidate"],
        }

    def verify_existing(self, company: dict) -> dict:
        value = str(company.get("website") or company.get("domain") or "").strip()
        if not value:
            return {"verified": False, "reason": "missing_website"}
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        return self._verify(company, value, source_direct=False)


class StrictGateWorker(GateWorker):
    def __init__(self, sheets_repo, drive_repo, llm=None, resolver: OfficialSiteResolver | None = None):
        super().__init__(sheets_repo, drive_repo, llm)
        self.resolver = resolver or OfficialSiteResolver(sheets_repo, llm)

    def process_pending(self, limit: int = 20) -> dict:
        pending = self.sheets.list_pending_gate(limit=limit)
        results = []
        for company in pending:
            verification = self.resolver.verify_existing(company)
            if not verification.get("verified"):
                row = int(company.get("row_number") or 0)
                if row > 0:
                    self.sheets.update_range(f"LeadFactory_Raw!C{row}:D{row}", [["", ""]])
                    self.sheets.update_range(f"LeadFactory_Raw!R{row}", [["NEEDS_DOMAIN"]])
                results.append({"lead_id": company.get("lead_id", ""), "final_result": "REQUEUED_NEEDS_OFFICIAL_SITE"})
                continue
            canonical = dict(company)
            canonical["domain"] = verification.get("official_domain", "")
            canonical["website"] = verification.get("official_website", "")
            results.append(self.evaluate_and_persist(canonical))
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "NO-GO": sum(1 for r in results if r.get("final_result") == "NO-GO"),
            "REQUEUED": sum(1 for r in results if r.get("final_result") == "REQUEUED_NEEDS_OFFICIAL_SITE"),
            "results": results,
        }


class StrictMittelstandWorker(MittelstandWorker):
    def __init__(self, sheets, drive, llm, resolver: OfficialSiteResolver | None = None):
        super().__init__(sheets, drive, llm)
        self.resolver = resolver or OfficialSiteResolver(sheets, llm)

    def process_pending(self, limit: int = 20) -> dict:
        pending = self.sheets.list_pending_mittelstand(limit=limit)
        results = []
        for company in pending:
            verification = self.resolver.verify_existing(company)
            if not verification.get("verified"):
                row = int(company.get("row_number") or 0)
                if row > 0:
                    self.sheets.update_range(f"LeadFactory_Raw!C{row}:D{row}", [["", ""]])
                    self.sheets.update_range(f"LeadFactory_Raw!R{row}", [["NEEDS_DOMAIN"]])
                results.append({"lead_id": company.get("lead_id", ""), "final_result": "REQUEUED_NEEDS_OFFICIAL_SITE"})
                continue
            canonical = dict(company)
            canonical["domain"] = verification.get("official_domain", "")
            canonical["website"] = verification.get("official_website", "")
            results.append(self.evaluate_and_persist(canonical))
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "NO": sum(1 for r in results if r.get("final_result") == "NO"),
            "UNKNOWN": sum(1 for r in results if r.get("final_result") == "UNKNOWN"),
            "REQUEUED": sum(1 for r in results if r.get("final_result") == "REQUEUED_NEEDS_OFFICIAL_SITE"),
            "results": results,
        }


class StrictLeadFactory(BaseLeadFactory):
    def __init__(self, settings):
        super().__init__(settings)
        self.official_site_resolver = OfficialSiteResolver(self.sheets, self.llm)
        self.gate_worker = StrictGateWorker(self.sheets, self.drive, self.llm, self.official_site_resolver)
        self.mittelstand_worker = StrictMittelstandWorker(self.sheets, self.drive, self.llm, self.official_site_resolver)


    def source_tick(self, run_id: str | None = None, lane: str | None = None) -> dict:
        """Run sources and immediately promote tested adapters through Cloud smoke.

        READY_FOR_CLOUD_SMOKE is a queue state, never a terminal production state.
        """
        result = super().source_tick(run_id=run_id, lane=lane)
        for item in result.get("results", []):
            if item.get("status") not in {"READY_FOR_CLOUD_SMOKE", "REPAIR_READY_FOR_CLOUD_SMOKE"}:
                continue
            source_id = str(item.get("source_id") or "")
            if not source_id:
                continue
            try:
                smoke = self.cloud_smoke_source(source_id)
                item["smoke"] = smoke
                if smoke.get("status") == "ACTIVE":
                    item["status"] = "RAW_CAPTURED"
                    runtime = smoke.get("runtime") or {}
                    item["runtime"] = runtime
                    result["new_raw"] += int(runtime.get("new_raw", 0) or 0)
                    result["duplicates"] += int(runtime.get("duplicates", 0) or 0)
            except Exception as exc:
                item["smoke"] = {"status": "ERROR", "error": f"{type(exc).__name__}:{exc}"}
        return result

    def expand_source_frontier(self, lane: str, limit: int = 25) -> dict:
        """Find next-hop public directories from registered source pages."""
        from urllib.parse import urljoin, urlparse
        lane = str(lane or "").upper()
        if lane not in {"GROWTH", "MITTELSTAND"}:
            raise ValueError(f"unsupported_lane:{lane}")
        keywords = ("exhibitor", "member", "association", "directory", "portfolio", "cluster", "index", "firms")
        added, inspected, errors = [], 0, 0
        sources = self.sheets.sources_for_crawl(limit=max(limit * 2, limit), lane=lane.lower(), recrawl_after_minutes=1)
        for source in sources:
            if len(added) >= limit:
                break
            try:
                snap = self._fetcher(source).fetch(source.crawl_url)
                inspected += 1
                for href in list(getattr(snap, "external_links", []) or []):
                    absolute = urljoin(source.crawl_url, str(href))
                    parsed = urlparse(absolute)
                    path = (parsed.path + "?" + parsed.query).lower()
                    if parsed.scheme not in {"http", "https"} or not any(k in path for k in keywords):
                        continue
                    candidate = {
                        "source_type": "MITTELSTAND_DISCOVERED" if lane == "MITTELSTAND" else "GROWTH_DISCOVERED",
                        "source_name": f"discovered:{parsed.netloc}{parsed.path[:80]}",
                        "source_url": absolute, "exhibitor_directory_url": absolute,
                        "country": source.country, "event_year": source.event_year,
                    }
                    is_new, source_id = self.sheets.add_source_if_new(candidate)
                    if is_new:
                        added.append(source_id)
                        if len(added) >= limit:
                            break
            except Exception:
                errors += 1
        return {"status": "COMPLETE", "lane": lane, "inspected": inspected, "added": added, "errors": errors}

    def capacity_tick(self, goal_status: dict) -> dict:
        """Expand production capacity while preserving the authoritative Gate."""
        lane = "MITTELSTAND" if int(goal_status.get("added", 0)) % 2 else "GROWTH"
        frontier = self.expand_source_frontier(lane, limit=25)
        supply = super().supply_tick(lane)
        return {"lane": lane, "frontier": frontier, "supply": supply}

    def domain_tick(self, lane: str | None = None, limit: int | None = None) -> dict:
        cfg = self._config()
        resolved_limit = int(limit if limit is not None else (cfg.get("DOMAIN_RESOLUTION_MAX_PER_RUN", "30") or 30))
        pending = self.sheets.list_needs_domain(limit=resolved_limit, lane=lane)
        results = []
        resolved = ready = duplicate = unresolved = errors = 0
        for company in pending:
            try:
                research = self.official_site_resolver.resolve(company)
                evidence = research.get("evidence", [])
                evidence_text = " | ".join(str(x) for x in evidence) if isinstance(evidence, list) else str(evidence or "")
                result = self.sheets.update_raw_domain_resolution(
                    lead_id=str(company.get("lead_id", "")),
                    domain=str(research.get("official_domain", "") or ""),
                    website=str(research.get("official_website", "") or ""),
                    hq_country=str(research.get("hq_country", "") or ""),
                    confidence=str(research.get("confidence", "LOW") or "LOW"),
                    evidence=evidence_text,
                )
                results.append({"company_name": company.get("company_name", ""), "verification": research.get("verification", ""), **result})
                if result.get("status") == "RESOLVED":
                    resolved += 1
                    if result.get("duplicate_state") == "NEW":
                        ready += 1
                    else:
                        duplicate += 1
                else:
                    unresolved += 1
            except Exception as exc:
                errors += 1
                results.append({"company_name": company.get("company_name", ""), "status": "ERROR", "error": f"{type(exc).__name__}:{exc}"})
        return {
            "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
            "requested": resolved_limit,
            "processed": len(results),
            "resolved": resolved,
            "ready": ready,
            "duplicates_skipped": duplicate,
            "unresolved": unresolved,
            "errors": errors,
            "verification_policy": "VERIFIED_FIRST_PARTY_REQUIRED",
            "results": results,
        }

    def evaluate_gate(self, company_context: dict) -> dict:
        verification = self.official_site_resolver.verify_existing(company_context)
        if not verification.get("verified"):
            return {
                "run_id": f"gate-preflight-{uuid.uuid4()}",
                "final_result": "BLOCKED_UNVERIFIED_OFFICIAL_SITE",
                "verification": verification,
            }
        ctx = dict(company_context)
        ctx["domain"] = verification.get("official_domain", "")
        ctx["website"] = verification.get("official_website", "")
        return super().evaluate_gate(ctx)

    def evaluate_mittelstand(self, company_context: dict) -> dict:
        verification = self.official_site_resolver.verify_existing(company_context)
        if not verification.get("verified"):
            return {
                "run_id": f"mittel-preflight-{uuid.uuid4()}",
                "final_result": "BLOCKED_UNVERIFIED_OFFICIAL_SITE",
                "verification": verification,
            }
        ctx = dict(company_context)
        ctx["domain"] = verification.get("official_domain", "")
        ctx["website"] = verification.get("official_website", "")
        return super().evaluate_mittelstand(ctx)
