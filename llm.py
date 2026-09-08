from __future__ import annotations

import json
import os
import re

from openai import OpenAI


class LLM:
    def __init__(self, model: str):
        self.model = model
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    @staticmethod
    def _json(text: str):
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json|python)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
        return json.loads(text[start:])

    def discover_growth_sources(self, strategy: str = "EXHIBITION", limit: int = 20) -> list[dict]:
        strategy = str(strategy or "EXHIBITION").upper()
        if strategy == "FUNDING_FEED":
            objective = """
Find CURRENT repeatable funding-news source feeds/pages that can continuously surface newly funded industrial/deeptech startups.
Prefer RSS/Atom feeds or stable funding/category pages from reliable business/technology publications, accelerator/newsroom feeds,
and industrial-tech media. The source should repeatedly publish company-level funding events, especially Series B/C or comparable growth rounds.
Return source_type=GROWTH_FUNDING_FEED. `exhibitor_directory_url` should contain the actual feed/category URL to crawl.
"""
        else:
            objective = """
Find CURRENT official exhibitor/company-directory pages for manufacturing, robotics, automation, machine tools, additive manufacturing,
advanced materials, industrial AI/software, inspection, semiconductor manufacturing, and industrial logistics events.
Prioritize upcoming/current events and directories with many company records. Return source_type=GROWTH_EXHIBITION.
`exhibitor_directory_url` must be the actual exhibitor/company listing entry point.
"""
        prompt = f"""
You are the Growth/Startup Source Discovery manager for A-one road's autonomous lead factory.
Your job is to continuously replenish a source queue. You do not screen companies.

STRATEGY: {strategy}
{objective}

Return ONLY a JSON array of up to {limit} objects with:
source_type, source_name, source_url, country, event_year, exhibitor_directory_url.
Use primary/official sources for exhibitions. For funding feeds, reliable recurring media feeds/category pages are allowed.
Do not return generic search-result pages, Wikipedia, or one-off single-company articles.
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        return data if isinstance(data, list) else []

    def discover_sources(self, limit: int = 20) -> list[dict]:
        # Backward-compatible alias for the growth exhibition lane.
        return self.discover_growth_sources("EXHIBITION", limit=limit)


    def discover_trigger_signals(self, limit: int = 20) -> list[dict]:
        prompt = f"""
You are the Trigger / Signal Discovery lane for A-one road's internal manufacturing lead factory.
Find CURRENT public business events that create a concrete reason to research an industrial/manufacturing/deeptech company now.
Prioritize: FUNDING, M&A, SUCCESSION, NEW_FACTORY, CAPACITY_EXPANSION, APAC_EXPANSION, INTERNATIONAL_SALES_HIRE, and NEW_BUSINESS.
Focus on companies plausibly connected to manufacturing, robotics, automation, machine tools, industrial software, advanced materials, additive manufacturing, inspection, semiconductor manufacturing, or industrial logistics.
This stage does NOT decide company eligibility and does NOT contact anyone.
Return ONLY a JSON array of up to {limit} objects with:
company_name, domain, website, signal_type, signal_date, signal_strength, headline, source_name, source_url, source_record_url, notes.
signal_strength must be HIGH, MEDIUM, or LOW and should reflect how directly the event indicates budget, expansion, transformation, or international GTM timing.
Use current primary/official sources where possible; reliable business news is allowed for event evidence. Never invent a domain or event.
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        return data if isinstance(data, list) else []


    def discover_growth_funding_signals(self, limit: int = 20) -> list[dict]:
        prompt = f"""
You are the breaking-news funding scout for A-one road's autonomous Growth/Startup lead pipeline.
Find CURRENT newly funded industrial/deeptech companies globally, prioritizing funding announced in the last 30 days.
Strong preference: explicitly announced Series B or Series C rounds. Also allow clearly comparable growth rounds only when the source
shows substantial institutional funding and the company is industrial/manufacturing/deeptech.

Relevant company domains include manufacturing, robotics, automation, machine tools, additive manufacturing, advanced materials,
inspection/metrology, semiconductor manufacturing, industrial AI/software, factory logistics, and industrial infrastructure technology.

This stage does not decide Japan GTM eligibility and does not contact anyone.
Return ONLY a JSON array of up to {limit} objects with:
company_name, domain, website, signal_type, signal_date, signal_strength, headline, source_name, source_url, source_record_url, notes.
- signal_type must be FUNDING_SERIES_B, FUNDING_SERIES_C, or FUNDING_GROWTH_ROUND.
- signal_strength: HIGH for explicit B/C, MEDIUM for comparable growth round, LOW only when evidence is weaker.
- Never invent round stage, amount, date, domain, or company.
Use primary company/investor announcements where possible; reliable business news may corroborate.
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        return data if isinstance(data, list) else []


    def resolve_company_domain(self, company_context: dict) -> dict:
        """Resolve one company to its official website/domain using fresh web search.

        The caller advances the row only when confidence=HIGH. Ambiguity must remain unresolved.
        """
        prompt = f"""
You are the official-domain resolver for A-one road's internal Lead Factory.
Research exactly ONE company and identify its official corporate website.
Use web_search. Prefer the company's own legal/corporate site and authoritative association/exhibitor records.
Never return a reseller, distributor, LinkedIn page, directory profile, social network, marketplace, or news article as the official domain.
Never guess from the company name. If multiple companies share the name, use source/country context to disambiguate.

Return ONLY JSON:
{{
  "official_domain": "example.com or empty",
  "official_website": "https://... or empty",
  "hq_country": "country or empty",
  "confidence": "HIGH|MEDIUM|LOW",
  "evidence": ["url"],
  "reason": "concise explanation"
}}

HIGH means the evidence directly establishes that the returned site belongs to this exact company/entity.
If that standard is not met, return MEDIUM/LOW and leave official_domain empty when appropriate.

COMPANY CONTEXT:
{json.dumps(company_context, ensure_ascii=False)[:24000]}
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        if not isinstance(data, dict):
            raise RuntimeError("domain_resolution_not_object")
        return data

    def discover_mittelstand_sources(self, policy_text: str, limit: int = 13) -> list[dict]:
        prompt = f"""
You are the Mature Industrial Source Discovery lane for A-one road.
Find CURRENT official or primary source pages that yield lists of established industrial companies.
Do not search only for the word 'Mittelstand'. Discover the company universes where export-oriented mature industrial firms actually live.

Use this live discovery policy as the governing search strategy:
---
{policy_text[:18000]}
---

Prioritize company-list entry points such as:
- industrial association member directories
- machine-tool / robotics / automation / materials / inspection association directories
- official exhibitor directories for major industrial trade fairs
- industrial clusters and exporter directories
- national technology-industry member lists
- official Taiwan/Korea industrial directories

Return ONLY a JSON array of up to {limit} objects with:
source_type, source_name, source_url, country, event_year, exhibitor_directory_url.
source_type should preferably be one of:
MITTELSTAND_ASSOCIATION, MITTELSTAND_EXHIBITION, MITTELSTAND_CLUSTER, MITTELSTAND_EXPORT_DIRECTORY.
Do NOT decide company eligibility here.
Do NOT return Wikipedia, generic news articles, generic search-result pages, or a single company's homepage as the source.
Use official/primary sources where possible.
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        return data if isinstance(data, list) else []

    def evaluate_mittelstand(self, gate_text: str, policy_text: str, company_context: dict) -> dict:
        """Fresh-context mature-industrial research; formal result is re-derived in Python."""
        prompt = f"""
You are the A-one road Mature Industrial / Mittelstand Research Agent.
Research exactly ONE company. Use web_search and prefer official company pages, annual reports, registries,
industry associations, distributor pages, and reliable business sources.

FORMAL GATE — authoritative. Do not add another eligibility gate:
---
{gate_text}
---

DISCOVERY / PRIORITIZATION CONTEXT — supplemental only, never changes M1-M3:
---
{policy_text[:18000]}
---

Important research rules:
1. Find the actual operating/legal entity and, where relevant, its controlling parent/group.
2. A parent/group roll-up is allowed only when majority ownership, 100% ownership, consolidation or equivalent control is evidenced.
   Never roll up to a distributor, customer, loose partner, introducer, or non-controlling investor.
3. Research employee count and Japan-market signals as supplemental context only.
4. Do not mark a company FAIL because employee count is below 100 or because Japan presence/channel activity exists.
5. Formal eligibility is determined by M2 revenue only:
   PASS when current/reasonably current revenue is at least EUR 20M or USD 30M;
   FAIL only when revenue below that threshold is clearly evidenced;
   UNKNOWN when revenue evidence is insufficient.
6. Parent/group revenue may be used only when control and payment/decision-making relevance are evidenced.
7. Operational routing, Japan openness, employee count, and Why Now may prioritize or route, but never alter the formal revenue result.
8. For giant plant / aircraft-platform / nuclear / EPC-scale offerings, routing may be STRATEGIC_BD even when the formal revenue gate passes.

Return ONLY JSON in this exact structure:
{{
  "screening_entity": "",
  "parent_company": "",
  "parent_control_evidence": ["url or concise source"],
  "M1": {{
    "result": "PASS|FAIL|UNKNOWN",
    "employee_count": "number/range/UNKNOWN",
    "reason": "",
    "evidence": ["url"]
  }},
  "M2": {{
    "result": "PASS|FAIL|UNKNOWN",
    "revenue_value": "",
    "revenue_currency": "EUR|USD|other|UNKNOWN",
    "revenue_usd_equivalent": "",
    "reason": "",
    "evidence": ["url"]
  }},
  "M3": {{
    "result": "PASS|FAIL|UNKNOWN",
    "japan_openness": "STRONG_GO|GO|UNKNOWN|NO",
    "japan_presence": "J0|J1|J2|J3|J4|J5|UNKNOWN",
    "channel_structure": "NONE|NONEXCLUSIVE|EXCLUSIVE|JAPAN_SUBSIDIARY|JV|OWNERSHIP_LINK|UNKNOWN",
    "channel_activity": "LOW|ACTIVE|UNKNOWN",
    "exclusivity": "NONEXCLUSIVE|EXCLUSIVE|UNKNOWN",
    "market_role": "FOLLOWER|LEADING|UNKNOWN",
    "partner_need_signal": "HIGH|MEDIUM|LOW|UNKNOWN",
    "reason": "",
    "evidence": ["url"]
  }},
  "supplemental": {{
    "execution_route": "A_ONE_STANDARD|A_ONE_PARTNER_LED|STRATEGIC_BD|UNKNOWN",
    "execution_reason": "",
    "transformation_archetype": "SECOND_GROWTH_INDUSTRIAL|ESTABLISHED_EXPORTER|GROWTH_INDUSTRIAL_TECH|OTHER|UNKNOWN",
    "why_now_signal": "",
    "why_now_reason": "",
    "why_now_evidence": ["url"],
    "priority_signal": "HIGH|MEDIUM|LOW|UNKNOWN"
  }},
  "missing_evidence": [""],
  "research_notes": ""
}}

COMPANY CONTEXT:
{json.dumps(company_context, ensure_ascii=False)[:30000]}
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        if not isinstance(data, dict):
            raise RuntimeError("mittelstand_result_not_object")
        return data

    def analyze_probe(self, probe_payload: dict) -> dict:
        prompt = f"""
You are a web acquisition architect. Analyze this source snapshot and choose the safest extraction strategy.
You must NOT perform company eligibility screening.
Output ONLY JSON with keys:
adapter_type (HTML|RENDERED_HTML|JSON_API|GRAPHQL|PDF|AUTH_REQUIRED),
auth_required (boolean), auth_reason, expected_count (integer or 0), notes.
Snapshot:
{json.dumps(probe_payload, ensure_ascii=False)[:120000]}
"""
        resp = self.client.responses.create(model=self.model, input=prompt)
        return self._json(resp.output_text)

    def build_adapter(self, probe_payload: dict, prior_error: str = "") -> str:
        prompt = f"""
Write a SAFE Python parser adapter for an autonomous lead factory.
The adapter DOES NOT have network access and MUST NOT open files, spawn processes, use sockets, requests, httpx, selenium, playwright, subprocess, os, pathlib, shutil, or environment variables.
It receives one dict `snapshot` with keys: url, final_url, content_type, text, links, network, api_payloads, title.
Implement exactly:
    def extract(snapshot):
        return {{"records": [{{"company_name": str, "website": str, "domain": str, "source_record_url": str}}], "next_urls": [str]}}
Rules:
- Parse only supplied snapshot content.
- next_urls must be URLs found in the supplied snapshot and relevant to pagination/company listing.
- Prefer official company URL if present, otherwise blank.
- Never invent companies or URLs.
- Deduplicate records within the snapshot.
- Use only these imports if needed: bs4, re, json, html, datetime, urllib.parse, collections, itertools, math, typing.
- Return code only, no markdown.

Probe snapshot:
{json.dumps(probe_payload, ensure_ascii=False)[:160000]}
Previous failure if any:
{prior_error[:12000]}
"""
        resp = self.client.responses.create(model=self.model, input=prompt)
        code = resp.output_text.strip()
        if code.startswith("```"):
            code = re.sub(r"^```(?:python)?\s*", "", code)
            code = re.sub(r"\s*```$", "", code)
        return code

    def evaluate_gate(self, gate_text: str, company_context: dict) -> dict:
        """Evaluate exactly one company using the live authoritative gate text."""
        prompt = f"""
You are the A-one road Japan GTM / Japan Business Build Gate Agent.

AUTHORITATIVE RULE:
- Apply ONLY the gate definition below.
- Do NOT add independent screening criteria.
- Missing information or failure to find information is NOT a reason to FAIL.
- Follow the GO / NO-GO / RESEARCH rules exactly as written in the gate definition.
- Research this ONE company only. Treat this as a fresh evaluation context.

Return ONLY JSON in exactly this shape:
{{
  "final_result": "GO|NO-GO|RESEARCH",
  "G1": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "G2": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "G3": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "G4": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "G5": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "G6": {{"result": "PASS|FAIL|UNKNOWN", "reason": "", "evidence": ["..."]}},
  "projectization_risk": "LOW|MEDIUM|HIGH|UNKNOWN",
  "standard_gtm": "TRUE|FALSE|UNKNOWN",
  "routing": "STANDARD_GTM|STRATEGIC_BD_REVIEW|RESEARCH",
  "most_important_reason": "",
  "first_failed_gate": "G1|G2|G3|G4|G5|G6|",
  "missing_evidence": ["..."]
}}

Projectization Risk is supplemental only. It MUST NOT change final_result.
If projectization_risk is HIGH, set standard_gtm to FALSE and routing to STRATEGIC_BD_REVIEW.
If the formal gate result is RESEARCH, routing may be RESEARCH when the missing evidence prevents a reliable routing decision.

AUTHORITATIVE GATE DEFINITION:
---
{gate_text}
---

COMPANY CONTEXT:
{json.dumps(company_context, ensure_ascii=False)[:30000]}
"""
        resp = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        data = self._json(resp.output_text)
        if not isinstance(data, dict):
            raise RuntimeError("gate_result_not_object")
        return data
    def research_outreach_contact(self, company_context: dict) -> dict:
        """Find one best-fit commercial contact. Exact emails require evidence; guesses are forbidden."""
        prompt = f"""
You are the A-one road internal contact-research worker. Research ONE already-qualified company.
Goal: identify the single best person for a founder-led Japan market-entry conversation.
Prioritize founder/CEO for founder-led firms, then CRO/CCO/VP Sales/Head of International/Business Development/Partnerships.
Use public evidence. Prefer the company's own website/team/news/contact pages and official profiles.
Never invent or pattern-guess an email. `email` must be blank unless an exact address is visibly supported by a cited public source.
Return ONLY JSON:
{{
  "contact_name": "",
  "title": "",
  "email": "",
  "linkedin_url": "",
  "contact_fit": "HIGH|MEDIUM|LOW",
  "confidence": "HIGH|MEDIUM|LOW",
  "research_summary": "",
  "evidence_urls": ["..."],
  "status": "FOUND_WITH_EMAIL|FOUND_NO_EMAIL|NOT_FOUND"
}}
Company:
{json.dumps(company_context, ensure_ascii=False)[:30000]}
"""
        resp = self.client.responses.create(model=self.model, tools=[{"type": "web_search"}], input=prompt)
        data = self._json(resp.output_text)
        if not isinstance(data, dict):
            raise RuntimeError("contact_result_not_object")
        return data

    def draft_outreach_email(self, production_prompt: str, company_context: dict, contact: dict) -> dict:
        """Apply the Drive-owned production prompt and serialize the finished email for internal storage."""
        prompt = f"""
The following Drive document is the authoritative A-one road outreach production prompt. Apply all research, factuality, wording,
length, CTA, calendar-link, signature, and recipient rules in it. The target has already passed its screening gate.
For this machine-only persistence step, serialize the finished email as JSON with `subject` and `body`; this serialization instruction
only changes the container format. The body itself must remain a normal send-ready email and must not contain Subject:/To:/BODY labels.
Return ONLY JSON: {{"subject":"", "body":""}}.

AUTHORITATIVE OUTREACH PROMPT:
---
{production_prompt[:50000]}
---

COMPANY CONTEXT:
{json.dumps(company_context, ensure_ascii=False)[:30000]}

CONTACT (use exact email only if present; do not infer one):
{json.dumps(contact, ensure_ascii=False)[:10000]}
"""
        resp = self.client.responses.create(model=self.model, tools=[{"type": "web_search"}], input=prompt)
        data = self._json(resp.output_text)
        if not isinstance(data, dict):
            raise RuntimeError("draft_result_not_object")
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        if not subject or not body:
            raise RuntimeError("draft_missing_subject_or_body")
        return {"subject": subject, "body": body}

