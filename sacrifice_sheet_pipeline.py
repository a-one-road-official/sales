"""Bounded OSS research and outreach against the explicitly named sacrifice workbook.

No factory, SSOT writer, LLM, Vertex, paid-search or cloud deployment dependency.
Execution requires a separately reviewed message in a bounded manifest. Unknown
business judgements remain REVIEW; they never enable a paid fallback.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SACRIFICE_ID = "1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk"
SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
LEADS_TAB = "営業リスト_Vendor"
LOG_TAB = "outreach_engine_log"
ORIGIN = "sacrifice_oss_v1"
PROMPT_HASH = "sacrifice_ec_prompt_v1"
LOG_HEADERS = ["timestamp", "channel", "company_name", "website", "email", "status", "error_message", "subject", "body", "stage"]


def now():
    return datetime.now(timezone.utc).isoformat()


def host(url):
    return (urlparse(str(url)).hostname or "").casefold().removeprefix("www.")


def policy(env):
    if env.get("LEAD_FACTORY_VERTEX_ALLOWED", "FALSE").upper() != "FALSE":
        raise ValueError("vertex_forbidden")
    if env.get("SACRIFICE_SPREADSHEET_ID") != SACRIFICE_ID:
        raise ValueError("sacrifice_workbook_identity_mismatch")


def validate_seed(seed):
    name, url = seed.get("company_name", ""), seed.get("website", "")
    if not name or urlparse(url).scheme != "https" or not host(url):
        raise ValueError("invalid_seed")
    if seed.get("domain") != "EC/リテール":
        raise ValueError("unsupported_campaign")
    if url not in seed.get("evidence_urls", []):
        raise ValueError("official_source_required")
    if seed.get("form_url") and host(seed["form_url"]) != host(url):
        raise ValueError("form_identity_mismatch")


def research_gate(seed, evidence):
    pages = [p for p in evidence.get("pages", []) if 200 <= int(p.get("status_code") or 0) < 300]
    if evidence.get("status") != "VERIFIED" or not pages:
        return "REVIEW_SITE_UNVERIFIED"
    if any(host(p.get("url")) != host(seed["website"]) for p in pages):
        return "REVIEW_REDIRECT_IDENTITY"
    # Require an actual page identity match, not only a guessed hostname.
    name = re.sub(r"\W", "", seed["company_name"]).casefold()
    visible = re.sub(r"\W", "", " ".join(p.get("title", "") + " " + p.get("text_excerpt", "") for p in pages)).casefold()
    if name not in visible:
        return "REVIEW_COMPANY_IDENTITY"
    return "VERIFIED_FACTS"


def message_gate(seed, prompt_hash):
    review = seed.get("message_review") or {}
    body = seed.get("body", "")
    if review.get("decision") != "APPROVED" or not review.get("reviewer"):
        return "REVIEW_MESSAGE"
    if review.get("prompt_hash") != prompt_hash:
        return "REVIEW_PROMPT_CHANGED"
    if review.get("body_sha256") != hashlib.sha256(body.encode()).hexdigest():
        return "REVIEW_MESSAGE_CHANGED"
    if not seed.get("subject") or not body or not seed.get("form_url"):
        return "REVIEW_MESSAGE_INCOMPLETE"
    return "READY"


class SacrificeStore:
    """The only write-capable repository in this entrypoint, pinned to one ID."""
    def __init__(self, svc, spreadsheet_id):
        if spreadsheet_id != SACRIFICE_ID:
            raise ValueError("ssot_write_forbidden")
        self.svc = svc
        self.spreadsheet_id = SACRIFICE_ID
        meta = svc.spreadsheets().get(spreadsheetId=SACRIFICE_ID,
            fields="spreadsheetId,properties(title),sheets(properties)").execute()
        if meta.get("spreadsheetId") != SACRIFICE_ID or meta["properties"]["title"].strip() != "sales_leads（生贄）":
            raise ValueError("sacrifice_metadata_mismatch")
        self.tabs = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
        for tab in (LEADS_TAB, LOG_TAB, "send_log"):
            if tab not in self.tabs:
                raise ValueError("required_sacrifice_tab_missing:" + tab)
        if self.values(LOG_TAB, "A1:J1") != [LOG_HEADERS]:
            raise ValueError("audit_schema_mismatch")
        headers = self.values(LEADS_TAB, "A1:S1")[0]
        for index, expected in {1:"company_name", 2:"domain", 5:"website", 7:"Status", 10:"source"}.items():
            if headers[index] != expected:
                raise ValueError("lead_schema_mismatch")

    def values(self, tab, cells):
        if tab not in self.tabs:
            raise ValueError("unknown_sacrifice_tab")
        return self.svc.spreadsheets().values().get(spreadsheetId=SACRIFICE_ID,
            range=f"'{tab}'!{cells}").execute().get("values", [])

    def lead_rows(self):
        n = self.tabs[LEADS_TAB]["gridProperties"]["rowCount"]
        if n > 10000:
            raise ValueError("lead_read_budget_exceeded")
        return self.values(LEADS_TAB, f"A1:S{n}")

    def existing(self, seed):
        for i, raw in enumerate(self.lead_rows()[1:], 2):
            r = list(raw) + [""] * 19
            if str(r[1]).casefold() == seed["company_name"].casefold() or host(r[5]) == host(seed["website"]):
                return i, r
        return None

    def append_verified(self, seed, evidence):
        if research_gate(seed, evidence) != "VERIFIED_FACTS":
            raise ValueError("unverified_lead_write_blocked")
        found = self.existing(seed)
        if found:
            return found[0], False
        rows = self.lead_rows()
        # Atomically append a complete row, preserving safe native structure.
        template = self.svc.spreadsheets().get(spreadsheetId=SACRIFICE_ID,
            ranges=[f"'{LEADS_TAB}'!A2:S2"], includeGridData=True,
            fields="sheets(data(rowData(values(userEnteredFormat,dataValidation))))").execute()
        cells = template["sheets"][0]["data"][0]["rowData"][0].get("values", [])
        values = ["調査済", seed["company_name"], seed["domain"], "未確認", "未確認", seed["website"],
            seed.get("partner_program_url", ""), "未接触", seed.get("what_it_solves", ""),
            "要確認（未進出とは断定しない）", ORIGIN, (datetime.now(timezone.utc).date()-datetime(1899,12,30).date()).days,
            "要確認", "", "", "公式サイト照合済", "要確認", " | ".join(seed["evidence_urls"]), ""]
        if len(values) != 19:
            raise RuntimeError("lead_payload_width_mismatch")
        output = []
        for i, value in enumerate(values):
            cell = copy.deepcopy(cells[i]) if i < len(cells) else {}
            fmt = cell.get("userEnteredFormat", {})
            fmt.get("textFormat", {}).pop("link", None)
            if i not in (11, 14, 18):
                fmt.pop("numberFormat", None)
            cell["userEnteredValue"] = {"numberValue": value} if isinstance(value, int) else {"stringValue": value}
            output.append(cell)
        self.svc.spreadsheets().batchUpdate(spreadsheetId=SACRIFICE_ID, body={"requests":[{
            "appendCells":{"sheetId":self.tabs[LEADS_TAB]["sheetId"],"rows":[{"values":output}],
                           "fields":"userEnteredValue,userEnteredFormat,dataValidation"}}]}).execute()
        found = self.existing(seed)
        if not found or found[1][10] != ORIGIN or found[1][5] != seed["website"]:
            raise RuntimeError("lead_append_readback_failed")
        return found[0], True

    def history(self, company):
        # A name hit in any legacy log blocks a repeat, including ambiguous sends.
        website_host = host(company)
        for tab in ("send_log", LOG_TAB):
            n = self.tabs[tab]["gridProperties"]["rowCount"]
            if n > 400000:
                raise ValueError("history_read_budget_exceeded")
            for start in range(2, n+1, 40000):
                rows = self.values(tab, f"C{start}:D{min(n, start+39999)}")
                for row in rows:
                    name = str(row[0] if len(row) > 0 else "").strip().casefold()
                    site = host(str(row[1] if len(row) > 1 else ""))
                    if name == company.casefold() or (website_host and site == website_host):
                        return True
        return False

    def audit(self, seed, status, detail, *, stage="PIPELINE"):
        values = [now(), "form", seed["company_name"], seed["website"], "", status, "",
            seed.get("subject", ""), json.dumps(detail, ensure_ascii=False), stage]
        # No retry: an uncertain append must not lead to a duplicate external send.
        self.svc.spreadsheets().values().append(spreadsheetId=SACRIFICE_ID,
            range=f"'{LOG_TAB}'!A:J", valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values":[values]}).execute()

    def update_own_status(self, seed, status):
        found = self.existing(seed)
        if not found:
            raise RuntimeError("source_row_missing")
        number, row = found
        if row[10] != ORIGIN or row[5] != seed["website"] or row[7] != "未接触":
            raise RuntimeError("source_row_changed_preserve_human_status")
        self.svc.spreadsheets().values().update(spreadsheetId=SACRIFICE_ID,
            range=f"'{LEADS_TAB}'!H{number}", valueInputOption="RAW", body={"values":[[status]]}).execute()
        if self.values(LEADS_TAB, f"H{number}:H{number}") != [[status]]:
            raise RuntimeError("status_readback_failed")


def process(seed, store, research, submit, *, execute=False, prompt_hash=""):
    validate_seed(seed)
    evidence = research(seed["website"], max_pages=3, expected_company=seed["company_name"])
    decision = research_gate(seed, evidence)
    result = {"company_name":seed["company_name"],"status":decision,"added":False,"submission_attempted":False,
              "target_spreadsheet_id":SACRIFICE_ID,"vertex_calls":0,"ai_judgement":"EXTERNAL_REVIEW"}
    if decision != "VERIFIED_FACTS":
        return result
    row, added = store.append_verified(seed, evidence)
    result.update(source_row=row, added=added)
    if not execute:
        result["status"] = "RESEARCHED"
        return result
    gate = message_gate(seed, prompt_hash)
    if gate != "READY":
        result["status"] = gate
        return result
    if store.history(seed["company_name"]):
        result["status"] = "DUPLICATE_OR_AMBIGUOUS_BLOCKED"
        return result
    current = store.existing(seed)
    if not current or current[1][10] != ORIGIN or current[1][7] != "未接触" or current[1][5] != seed["website"]:
        result["status"] = "HUMAN_OR_LEGACY_ROW_PRESERVED"
        return result
    # Claim is durable before crossing the external boundary. Crash => no retry.
    store.audit(seed, "CLAIMED", {"source_row":row, "prompt_hash":prompt_hash}, stage="PRE_SEND")
    outcome = submit(form_url=seed["form_url"], website=seed["website"], message=seed["body"],
        subject=seed["subject"], company_name=seed["company_name"],
        idempotency_key="sacrifice:"+host(seed["website"]), source_row=str(row))
    result.update(status=outcome["status"],submission_attempted=outcome.get("submission_attempted",False),outcome=outcome)
    store.audit(seed, result["status"], result, stage="POST_SEND")
    if result["status"] == "FORM_SENT":
        store.update_own_status(seed, "DM済")
    elif result["submission_attempted"]:
        store.update_own_status(seed, "保留")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="data/sacrifice_oss_campaign.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    policy(os.environ)
    from google.auth import default
    from googleapiclient.discovery import build
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    store = SacrificeStore(build("sheets", "v4", credentials=creds, cache_discovery=False), SACRIFICE_ID)
    if args.preflight:
        print(json.dumps({"preflight":"OK","target_spreadsheet_id":SACRIFICE_ID,"ssot_writes":0}))
        return
    seeds = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 10:
        raise ValueError("bounded_manifest_required")
    if args.execute and os.getenv("SACRIFICE_EXPLICIT_SEND_APPROVAL") != "TRUE":
        raise ValueError("explicit_send_approval_required")
    from sacrifice_web_research import inspect_official_site
    from form_execution import PublicContactFormExecutor
    # EC sacrifice uses its own reviewed prompt contract. No model is instantiated.
    prompt_hash = PROMPT_HASH if args.execute else ""
    results = []
    for seed in seeds:
        results.append(process(seed, store, inspect_official_site, PublicContactFormExecutor().execute,
            execute=args.execute, prompt_hash=prompt_hash))
        Path("sacrifice-result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k:v for k,v in results[-1].items() if k != "outcome"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
