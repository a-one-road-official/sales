from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from gate_worker import GateWorker
from mittelstand_worker import MittelstandWorker
from orchestrator import LeadFactory as BaseLeadFactory
from safe_fetch import TrustedFetcher
from source_universe import for_lane as bootstrap_sources_for_lane
from task_queue import TaskDispatcher
from observability import failure_code, record_event


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


def _parse_iso(value: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class OfficialSiteResolver:
    """Resolve and verify the exact first-party corporate website before screening."""

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

    @staticmethod
    def _is_exhibition(company: dict) -> bool:
        """Exhibitor pages are identity evidence, never website directories.

        Most exhibition indexes expose only a company name and booth number. Trying
        to harvest outbound links from those pages creates a false sense of URL
        coverage and makes the resolver depend on a link that usually does not
        exist. Company-name web search is the authoritative candidate generator for
        this source class.
        """
        source_type = str(company.get("source_type") or "").upper()
        source_name = str(company.get("source_name") or "").lower()
        return "EXHIBITION" in source_type or any(
            token in source_name for token in ("expo", "exhibitor", "messe", "trade fair", "tradefair")
        )

    @staticmethod
    def _name_domain_candidates(company: dict) -> list[str]:
        """Build low-risk domain-shaped hints for a second-pass HTTP check.

        These are only probes. `_verify` must establish first-party identity before
        any candidate is accepted, so a guessed domain can never become SSOT data
        by itself.
        """
        tokens = _tokens(company.get("company_name", ""))
        if len(tokens) < 2:
            return []
        slug = "".join(tokens)
        dashed = "-".join(tokens)
        country = str(company.get("hq_country") or "").strip().lower()
        suffixes = [".com"]
        country_suffixes = {
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
            "taiwan": [".tw"],
        }
        suffixes[0:0] = country_suffixes.get(country, [])
        out = []
        for base in (slug, dashed):
            if not base:
                continue
            for suffix in suffixes:
                candidate = f"https://{base}{suffix}"
                if candidate not in out:
                    out.append(candidate)
        return out[:8]

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
        search_research: dict = {}
        existing = str(company.get("website") or company.get("domain") or "").strip()
        exhibition = self._is_exhibition(company)
        if existing and not exhibition:
            if not existing.startswith(("http://", "https://")):
                existing = "https://" + existing
            candidates.append((existing, False, "raw_candidate"))

        # For exhibitor sources, the source page is intentionally not crawled for
        # company URLs. It remains in the candidate context as provenance only.
        if self.llm is not None:
            try:
                search_research = self.llm.resolve_company_domain(company)
                for key in ("official_website", "official_domain"):
                    value = str(search_research.get(key) or "").strip()
                    if value:
                        if not value.startswith(("http://", "https://")):
                            value = "https://" + value
                        candidates.append((value, False, "company_name_web_search"))
            except Exception:
                pass

        # A name-shaped domain is only a bounded second pass after web search. It
        # is never accepted without a first-party content check in `_verify`.
        for value in self._name_domain_candidates(company):
            candidates.append((value, False, "name_domain_probe"))

        if existing and exhibition:
            # An exhibition row may carry a prefilled domain from a separate
            # authoritative signal. Verify it, but do not treat the exhibitor page
            # as the reason it is trusted.
            if not existing.startswith(("http://", "https://")):
                existing = "https://" + existing
            candidates.append((existing, False, "raw_candidate"))

        seen: set[str] = set()
        rejected: list[str] = []
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
            # A search result can be conclusive while the Cloud Run egress path
            # is temporarily unable to fetch the candidate site. Keep that row
            # moving when the model returned the exact candidate, HIGH confidence,
            # and a source URL; guessed name-shaped domains never use this path.
            if (
                origin == "company_name_web_search"
                and str(search_research.get("confidence") or "").upper() == "HIGH"
                and str(result.get("reason") or "").startswith("fetch:")
                and _host(str(search_research.get("official_website") or search_research.get("official_domain") or "")) == h
                and search_research.get("evidence")
            ):
                official = str(search_research.get("official_website") or url).strip()
                if not official.startswith(("http://", "https://")):
                    official = "https://" + official
                return {
                    "official_domain": h,
                    "official_website": official,
                    "hq_country": company.get("hq_country", ""),
                    "confidence": "HIGH",
                    "verification": "VERIFIED_BY_COMPANY_NAME_SEARCH",
                    "evidence": [origin] + [str(x) for x in search_research.get("evidence", [])] + [str(result.get("reason"))],
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
        result = self._verify(company, value, source_direct=False)
        if result.get("verified") or self.llm is None:
            return result
        # Re-check the same persisted domain through company-name search when a
        # transient HTTP fetch failure prevents direct page verification.
        if not str(result.get("reason") or "").startswith("fetch:"):
            return result
        try:
            research = self.llm.resolve_company_domain(company)
            confidence = str(research.get("confidence") or "").upper()
            candidate = str(research.get("official_website") or research.get("official_domain") or "").strip()
            if candidate and not candidate.startswith(("http://", "https://")):
                candidate = "https://" + candidate
            if confidence == "HIGH" and _host(candidate) == _host(value) and research.get("evidence"):
                return {
                    "verified": True,
                    "official_domain": _host(value),
                    "official_website": candidate or value,
                    "confidence": "HIGH",
                    "reason": "verified_by_company_name_search_http_unavailable",
                    "evidence": [str(x) for x in research.get("evidence", [])] + [str(result.get("reason"))],
                }
        except Exception:
            pass
        return result


class StrictGateWorker(GateWorker):
    def __init__(self, sheets_repo, drive_repo, llm=None, resolver: OfficialSiteResolver | None = None):
        super().__init__(sheets_repo, drive_repo, llm)
        self.resolver = resolver or OfficialSiteResolver(sheets_repo, llm)

    def process_company(self, company: dict) -> dict:
        verification = self.resolver.verify_existing(company)
        if not verification.get("verified"):
            row = int(company.get("row_number") or 0)
            if row > 0:
                self.sheets.update_range(f"LeadFactory_Raw!C{row}:D{row}", [["", ""]])
                self.sheets.update_range(f"LeadFactory_Raw!R{row}", [["NEEDS_DOMAIN"]])
            return {"lead_id": company.get("lead_id", ""), "final_result": "REQUEUED_NEEDS_OFFICIAL_SITE"}
        canonical = dict(company)
        canonical["domain"] = verification.get("official_domain", "")
        canonical["website"] = verification.get("official_website", "")
        return self.evaluate_and_persist(canonical)

    def process_pending(self, limit: int = 20, lane: str = "GROWTH") -> dict:
        lane_key = str(lane or "GROWTH").upper()
        pending = self.sheets.list_pending_mittelstand(limit=limit) if lane_key == "MITTELSTAND" else self.sheets.list_pending_gate(limit=limit)
        results = []
        for company in pending:
            try:
                results.append(self.process_company(company))
            except Exception as exc:
                results.append({
                    "lead_id": company.get("lead_id", ""),
                    "company_name": company.get("company_name", ""),
                    "final_result": "ERROR",
                    "error": f"{type(exc).__name__}:{exc}",
                })
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "NO-GO": sum(1 for r in results if r.get("final_result") == "NO-GO"),
            "REQUEUED": sum(1 for r in results if r.get("final_result") == "REQUEUED_NEEDS_OFFICIAL_SITE"),
            "ERROR": sum(1 for r in results if r.get("final_result") == "ERROR"),
            "results": results,
        }


class UnifiedMittelstandWorker:
    """Strict official-domain verification followed by the separate revenue Gate."""

    def __init__(self, sheets, drive, llm, resolver: OfficialSiteResolver):
        self.sheets = sheets
        self.drive = drive
        self.llm = llm
        self.resolver = resolver
        self.formal_gate = MittelstandWorker(sheets, drive, llm)

    def process_one(self, company: dict) -> dict:
        verification = self.resolver.verify_existing(company)
        if not verification.get("verified"):
            row = int(company.get("row_number") or 0)
            if row > 0:
                self.sheets.update_range(f"LeadFactory_Raw!C{row}:D{row}", [["", ""]])
                self.sheets.update_range(f"LeadFactory_Raw!R{row}", [["NEEDS_DOMAIN"]])
            return {"lead_id": company.get("lead_id", ""), "final_result": "REQUEUED_NEEDS_OFFICIAL_SITE"}
        canonical = dict(company)
        canonical["domain"] = verification.get("official_domain", "")
        canonical["website"] = verification.get("official_website", "")
        return self.formal_gate.evaluate_and_persist(canonical)

    def process_pending(self, limit: int = 20) -> dict:
        pending = self.sheets.list_pending_mittelstand(limit=limit)
        results = []
        for company in pending:
            try:
                results.append(self.process_one(company))
            except Exception as exc:
                results.append({
                    "lead_id": company.get("lead_id", ""),
                    "company_name": company.get("company_name", ""),
                    "final_result": "ERROR",
                    "error": f"{type(exc).__name__}:{exc}",
                })
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "UNKNOWN": sum(1 for r in results if r.get("final_result") == "UNKNOWN"),
            "NO": sum(1 for r in results if r.get("final_result") == "NO"),
            "REQUEUED": sum(1 for r in results if r.get("final_result") == "REQUEUED_NEEDS_OFFICIAL_SITE"),
            "ERROR": sum(1 for r in results if r.get("final_result") == "ERROR"),
            "results": results,
        }

    def evaluate_and_persist(self, company_context: dict) -> dict:
        return self.process_one(company_context)

class StrictLeadFactory(BaseLeadFactory):
    def __init__(self, settings):
        super().__init__(settings)
        self.official_site_resolver = OfficialSiteResolver(self.sheets, self.llm)
        self.gate_worker = StrictGateWorker(self.sheets, self.drive, self.llm, self.official_site_resolver)
        self.mittelstand_worker = UnifiedMittelstandWorker(self.sheets, self.drive, self.llm, self.official_site_resolver)

    def _frontier_candidates(self, lane: str, limit: int) -> list[dict]:
        lane_key = str(lane or "").strip().upper()
        known = self.sheets.list_sources()
        known_text = "\n".join(f"- {s.source_name}: {s.crawl_url}" for s in known[-120:])
        try:
            gate_text = self.gate_worker.loader.load().text
        except Exception:
            gate_text = ""
        allowed_types = (
            "MITTELSTAND_ASSOCIATION|MITTELSTAND_EXHIBITION|MITTELSTAND_CLUSTER|MITTELSTAND_EXPORT_DIRECTORY"
            if lane_key == "MITTELSTAND" else
            "GROWTH_EXHIBITION|GROWTH_ASSOCIATION|GROWTH_DIRECTORY|GROWTH_FUNDING_FEED"
        )
        prompt = f"""
You are the autonomous Source Frontier Explorer for A-one road's internal Lead Factory.
Search the public web and find up to {max(1, limit)} NEW, high-yield company-list sources for lane={lane_key}.
The goal is to continuously expand the reachable company universe, including while the founder is offline.

A useful source is a repeatable page/feed/directory that exposes MANY company records: official industrial association member lists,
official trade-fair exhibitor indexes, industrial cluster/exporter directories, official startup/portfolio directories, or recurring
funding feeds. Prefer sources with 100+ company records and direct company profile/website links. Search globally inside the regions
and verticals defined by the live policy. Follow adjacent associations/events/directories suggested by already-known sources.
Do not return a single company page, generic search-results page, Wikipedia, LinkedIn, a generic news homepage, or a duplicate below.

Allowed source_type values: {allowed_types}
Return ONLY a JSON array of objects with:
source_type, source_name, source_url, country, event_year, exhibitor_directory_url.
`exhibitor_directory_url` must be the actual company-list/feed entry point.

LIVE POLICY SSOT (use its DISCOVERY section as governing context):
---
{gate_text[:12000]}
---

ALREADY KNOWN SOURCES — find different/adjacent sources:
{known_text[:24000]}
"""
        resp = self.llm.client.responses.create(model=self.llm.model, tools=[{"type": "web_search"}], input=prompt)
        data = self.llm._json(resp.output_text)
        return data if isinstance(data, list) else []

    def discover_lane(self, run_id: str, lane: str) -> dict:
        """Continuously widen the source graph; bootstrap sources guarantee a cold start."""
        lane_key = str(lane or "").strip().upper()
        if lane_key not in {"GROWTH", "MITTELSTAND"}:
            raise ValueError(f"unsupported_lane:{lane}")
        cfg = self._config()
        added: list[str] = []
        existing = rejected = 0

        # Deterministic bootstrap from high-yield official directories.
        for candidate in bootstrap_sources_for_lane(lane_key):
            try:
                is_new, sid = self.sheets.add_source_if_new(candidate)
                if is_new:
                    added.append(sid)
                else:
                    existing += 1
            except Exception:
                rejected += 1

        frontier_limit = int(cfg.get("SOURCE_FRONTIER_DISCOVERY_LIMIT", os.getenv("LEAD_FACTORY_SOURCE_FRONTIER_LIMIT", "40")) or 40)
        try:
            candidates = self._frontier_candidates(lane_key, frontier_limit)
        except Exception as exc:
            candidates = []
            frontier_error = f"{type(exc).__name__}:{exc}"
        else:
            frontier_error = ""

        for c in candidates:
            try:
                source_type = str(c.get("source_type", "") or "").upper()
                is_mittel = source_type.startswith("MITTELSTAND_")
                if (lane_key == "MITTELSTAND") != is_mittel:
                    rejected += 1
                    continue
                url = str(c.get("exhibitor_directory_url") or c.get("source_url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    rejected += 1
                    continue
                is_new, sid = self.sheets.add_source_if_new(c)
                if is_new:
                    added.append(sid)
                else:
                    existing += 1
            except Exception:
                rejected += 1

        return {
            "status": "COMPLETE_WITH_FRONTIER_ERROR" if frontier_error else "COMPLETE",
            "lane": lane_key,
            "bootstrap_count": len(bootstrap_sources_for_lane(lane_key)),
            "frontier_candidates": len(candidates),
            "added": added,
            "existing": existing,
            "rejected": rejected,
            "frontier_error": frontier_error,
        }

    def _run_source_with_run_id(self, source, run_id: str) -> dict:
        """Build -> S1-S7 -> cloud smoke -> ACTIVE -> crawl in one autonomous path."""
        result = super()._run_source_with_run_id(source, run_id)
        status = str(result.get("status", "")).upper()
        if status not in {"READY_FOR_CLOUD_SMOKE", "REPAIR_READY_FOR_CLOUD_SMOKE"}:
            return result
        smoke = self.cloud_smoke_source(source.source_id)
        smoke_status = str(smoke.get("status", "")).upper()
        if smoke_status in {"ACTIVE", "ALREADY_ACTIVE"}:
            runtime = smoke.get("runtime") if isinstance(smoke.get("runtime"), dict) else {}
            return {"status": "RAW_CAPTURED", "source_id": source.source_id, "smoke": smoke, "runtime": runtime}
        return {"status": smoke_status or "SMOKE_FAILED", "source_id": source.source_id, "smoke": smoke}

    def _raw_by_id(self, lead_id: str) -> dict | None:
        headers_rows = self.sheets.read("LeadFactory_Raw!1:1")
        if not headers_rows:
            return None
        headers = headers_rows[0]
        for row_number, row in enumerate(self.sheets.read("LeadFactory_Raw!A2:R"), start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            item = dict(zip(headers, padded))
            if str(item.get("lead_id", "")) == str(lead_id):
                item["row_number"] = row_number
                return item
        return None

    def domain_one(self, lead_id: str) -> dict:
        company = self._raw_by_id(lead_id)
        if not company:
            return {"status": "NOT_FOUND", "lead_id": lead_id}
        if str(company.get("intake_status", "")).upper() != "NEEDS_DOMAIN":
            return {"status": "NOOP_ALREADY_ADVANCED", "lead_id": lead_id, "intake_status": company.get("intake_status", "")}
        try:
            research = self.official_site_resolver.resolve(company)
        except Exception as exc:
            record_event(
                self.sheets, event_type="PIPELINE_FAILURE",
                reason_code=failure_code(exc), reason_note=f"domain_resolution:{type(exc).__name__}:{exc}",
                company_name=str(company.get("company_name") or ""),
                source_id=str(company.get("lead_id") or lead_id),
                raw_ref=str(company.get("source_record_url") or ""), status="DOMAIN_FAILED",
            )
            raise
        evidence = research.get("evidence", [])
        evidence_text = " | ".join(str(x) for x in evidence) if isinstance(evidence, list) else str(evidence or "")
        result = self.sheets.update_raw_domain_resolution(
            lead_id=lead_id,
            domain=str(research.get("official_domain", "") or ""),
            website=str(research.get("official_website", "") or ""),
            hq_country=str(research.get("hq_country", "") or ""),
            confidence=str(research.get("confidence", "LOW") or "LOW"),
            evidence=evidence_text,
        )
        resolved = str(research.get("official_domain") or "").strip()
        unresolved_reason = evidence_text or str(research.get("verification") or "")
        record_event(
            self.sheets,
            event_type="PIPELINE_STAGE" if resolved else "PIPELINE_FAILURE",
            reason_code="DOMAIN_RESOLVED" if resolved else (failure_code(unresolved_reason) if unresolved_reason else "OFFICIAL_SITE_NOT_VERIFIED"),
            reason_note=unresolved_reason,
            company_name=str(company.get("company_name") or ""),
            domain=resolved,
            source_id=str(company.get("lead_id") or lead_id),
            raw_ref=str(company.get("source_record_url") or ""),
            status="DOMAIN_RESOLVED" if resolved else "DOMAIN_UNRESOLVED",
        )
        return {"lead_id": lead_id, "verification": research.get("verification", ""), **result}


    def source_tick(self, run_id: str | None = None, lane: str | None = None, limit: int | None = None) -> dict:
        """Run sources and immediately activate tested adapters through Cloud smoke."""
        result = super().source_tick(run_id=run_id, lane=lane, limit=limit)
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
        """Discover next-hop public directories from registered source pages."""
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
                    candidate = {"source_type": "MITTELSTAND_DISCOVERED" if lane == "MITTELSTAND" else "GROWTH_DISCOVERED",
                                 "source_name": f"discovered:{parsed.netloc}{parsed.path[:80]}",
                                 "source_url": absolute, "exhibitor_directory_url": absolute,
                                 "country": source.country, "event_year": source.event_year}
                    is_new, source_id = self.sheets.add_source_if_new(candidate)
                    if is_new:
                        added.append(source_id)
                        if len(added) >= limit:
                            break
            except Exception:
                errors += 1
        return {"status": "COMPLETE", "lane": lane, "inspected": inspected, "added": added, "errors": errors}

    def capacity_tick(self, goal_status: dict) -> dict:
        """Increase exploration/processing while preserving the authoritative Gate."""
        lane = "MITTELSTAND" if int(goal_status.get("added", 0)) % 2 else "GROWTH"
        frontier = self.expand_source_frontier(lane, limit=25)
        supply = super().supply_tick(lane)
        return {"lane": lane, "frontier": frontier, "supply": supply}

    def domain_tick(self, lane: str | None = None, limit: int | None = None) -> dict:
        cfg = self._config()
        resolved_limit = int(limit if limit is not None else (cfg.get("DOMAIN_RESOLUTION_MAX_PER_RUN", "100") or 100))
        pending = self.sheets.list_needs_domain(limit=resolved_limit, lane=lane)
        results = []
        for company in pending:
            try:
                results.append(self.domain_one(str(company.get("lead_id", ""))))
            except Exception as exc:
                results.append({"lead_id": company.get("lead_id", ""), "status": "ERROR", "error": f"{type(exc).__name__}:{exc}"})
        return {
            "status": "COMPLETE",
            "requested": resolved_limit,
            "processed": len(results),
            "resolved": sum(1 for r in results if r.get("status") == "RESOLVED"),
            "unresolved": sum(1 for r in results if r.get("status") == "UNRESOLVED"),
            "errors": sum(1 for r in results if r.get("status") == "ERROR"),
            "results": results,
        }

    def gate_one(self, lead_id: str) -> dict:
        company = self._raw_by_id(lead_id)
        if not company:
            return {"status": "NOT_FOUND", "lead_id": lead_id}
        intake = str(company.get("intake_status", "")).upper()
        screening = str(company.get("screening_status", "")).upper()
        if intake not in {"READY_FOR_GATE", "READY_FOR_MITTELSTAND_GATE"} or screening not in {"", "PENDING"}:
            return {"status": "NOOP_ALREADY_ADVANCED", "lead_id": lead_id, "intake_status": intake, "screening_status": screening}
        if intake == "READY_FOR_MITTELSTAND_GATE":
            return self.mittelstand_worker.process_one(company)
        return self.gate_worker.process_company(company)

    def dispatch_lane(self, lane: str) -> dict:
        """Fan out source/domain/gate work into Cloud Tasks for parallel independent jobs."""
        if not self._enabled():
            return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
        lane_key = str(lane or "").strip().upper()
        if lane_key not in {"GROWTH", "MITTELSTAND"}:
            raise ValueError(f"unsupported_lane:{lane}")
        cfg = self._config()
        dispatcher = TaskDispatcher()
        source_limit = int(cfg.get("DISPATCH_SOURCE_MAX", os.getenv("LEAD_FACTORY_DISPATCH_SOURCE_MAX", "50")) or 50)
        domain_limit = int(cfg.get("DISPATCH_DOMAIN_MAX", os.getenv("LEAD_FACTORY_DISPATCH_DOMAIN_MAX", "2000")) or 2000)
        gate_limit = int(cfg.get("DISPATCH_GATE_MAX", os.getenv("LEAD_FACTORY_DISPATCH_GATE_MAX", "2000")) or 2000)
        recrawl = int(cfg.get("SOURCE_RECRAWL_AFTER_MINUTES", "1440") or 1440)
        sources = self.sheets.sources_for_crawl(limit=source_limit, lane=lane_key.lower(), recrawl_after_minutes=recrawl)
        domains = self.sheets.list_needs_domain(limit=domain_limit, lane=lane_key)
        gates = self.sheets.list_pending_mittelstand(limit=gate_limit) if lane_key == "MITTELSTAND" else self.sheets.list_pending_gate(limit=gate_limit)
        now = datetime.now(timezone.utc)
        bucket = f"{now:%Y%m%d%H}{(now.minute // 30) * 30:02d}"
        enqueued = {"source": 0, "domain": 0, "gate": 0, "already": 0, "errors": 0}
        details = []

        def add(path: str, payload: dict, key: str, stage: str):
            try:
                res = dispatcher.enqueue(path, payload, key)
                details.append({"stage": stage, "key": key, **res})
                if res.get("status") == "ENQUEUED":
                    enqueued[stage] += 1
                else:
                    enqueued["already"] += 1
            except Exception as exc:
                enqueued["errors"] += 1
                details.append({"stage": stage, "key": key, "status": "ERROR", "error": f"{type(exc).__name__}:{exc}"})

        for source in sources:
            add("/worker/source", {"source_id": source.source_id}, f"source:{source.source_id}:{bucket}", "source")
        for company in domains:
            lead_id = str(company.get("lead_id", ""))
            if lead_id:
                add("/worker/domain", {"lead_id": lead_id}, f"domain:{lead_id}:{bucket}", "domain")
        for company in gates:
            lead_id = str(company.get("lead_id", ""))
            if lead_id:
                add("/worker/gate", {"lead_id": lead_id}, f"gate:{lead_id}:{bucket}", "gate")

        return {
            "status": "DISPATCHED",
            "lane": lane_key,
            "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
            "enqueued": enqueued,
            "details": details[:100],
        }

    def _backlog_snapshot(self) -> dict:
        raw = self.sheets.read("LeadFactory_Raw!A2:R")
        needs_domain = ready_growth = ready_mittel = 0
        latest_raw = None
        for r in raw:
            padded = r + [""] * (18 - len(r))
            intake = str(padded[17] or "").upper()
            screening = str(padded[11] or "").upper()
            if intake == "NEEDS_DOMAIN" and screening in {"", "PENDING"}:
                needs_domain += 1
            elif intake == "READY_FOR_GATE" and screening in {"", "PENDING"}:
                ready_growth += 1
            elif intake == "READY_FOR_MITTELSTAND_GATE" and screening in {"", "PENDING"}:
                ready_mittel += 1
            ts = _parse_iso(padded[9] if len(padded) > 9 else "")
            if ts and (latest_raw is None or ts > latest_raw):
                latest_raw = ts

        sources = self.sheets.read("LeadFactory_Sources!A2:L")
        source_work = 0
        latest_source = None
        for r in sources:
            padded = r + [""] * (12 - len(r))
            status = str(padded[9] or "").upper()
            if status in {"", "DISCOVERED", "READY", "RETRY", "ERROR", "DEGRADED", "READY_FOR_CLOUD_SMOKE", "REPAIR_READY_FOR_CLOUD_SMOKE"}:
                source_work += 1
            ts = _parse_iso(padded[7] if len(padded) > 7 else "")
            if ts and (latest_source is None or ts > latest_source):
                latest_source = ts
        return {
            "needs_domain": needs_domain,
            "ready_growth_gate": ready_growth,
            "ready_mittelstand_gate": ready_mittel,
            "source_work": source_work,
            "latest_raw_at": latest_raw.isoformat() if latest_raw else "",
            "latest_source_at": latest_source.isoformat() if latest_source else "",
            "raw_total": len(raw),
            "source_total": len(sources),
            "promoted_total": self.sheets.count_promoted_leads(),
        }

    def _set_config_value(self, key: str, value: str) -> None:
        rows = self.sheets.read("Config!A2:B200")
        for row_number, row in enumerate(rows, start=2):
            if row and str(row[0]) == str(key):
                self.sheets.update_range(f"Config!B{row_number}", [[str(value)]])
                return
        self.sheets.append("Config", [str(key), str(value)])

    def control_tick(self) -> dict:
        """Stop only after the source frontier and all processing backlogs are genuinely quiet."""
        if not self._enabled():
            return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
        cfg = self._config()
        quiet_minutes = int(cfg.get("AUTONOMY_EXHAUSTION_QUIET_MINUTES", os.getenv("LEAD_FACTORY_EXHAUSTION_QUIET_MINUTES", "180")) or 180)
        snapshot = self._backlog_snapshot()
        if snapshot["needs_domain"] or snapshot["ready_growth_gate"] or snapshot["ready_mittelstand_gate"] or snapshot["source_work"]:
            return {"status": "RUNNING_BACKLOG", "quiet_minutes_required": quiet_minutes, **snapshot}
        now = datetime.now(timezone.utc)
        latest_times = [_parse_iso(snapshot.get("latest_raw_at", "")), _parse_iso(snapshot.get("latest_source_at", ""))]
        latest_times = [x for x in latest_times if x is not None]
        minutes_since_activity = min((now - max(latest_times)).total_seconds() / 60.0, 10**9) if latest_times else 10**9
        if minutes_since_activity < quiet_minutes:
            return {"status": "RUNNING_FRONTIER_QUIET_WINDOW", "minutes_since_activity": round(minutes_since_activity, 1), "quiet_minutes_required": quiet_minutes, **snapshot}

        # Quiet time is a diagnostic condition, not permission to stop production.
        # Keep the global enable flag untouched; the next scheduler cycle must
        # expand discovery or switch lanes. Notify internally and continue.
        result = {
            "status": "FRONTIER_QUIET_CONTINUE_DISCOVERY",
            "minutes_since_activity": round(minutes_since_activity, 1),
            "quiet_minutes_required": quiet_minutes,
            "next_action": "REQUEUE_SOURCE_DISCOVERY_AND_KEEP_OTHER_LANES_RUNNING",
            **snapshot,
        }
        notice = self.notifier.notify(
            subject="A-one Lead Factory warning: frontier quiet; continuing",
            body=(
                "Internal warning: no recent activity was observed, but production was not stopped. "
                "The next scheduler cycle must expand discovery or switch lanes.\n\n"
                + json.dumps(result, ensure_ascii=False, indent=2)
                + "\n\nNo customer-facing action was executed."
            ),
