"""Existing SSOT adapter. No new workbook, queue, timer, transport or model.

One sender owns reservations. Generation only writes draft fields; CRM avoids
active reservations. Sheets is not compare-and-swap: callers must re-read before
handoff, and an uncertain mutation must be reconciled, never blindly retried.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from customer_care import (SSOT_ID, SSOT_TAB, LEDGER_TAB, TRACKING_HEADERS, VERSION,
                           company_id, domain, norm, text, dumps, transition)


def col(number):
    value = ""
    while number:
        number, digit = divmod(number - 1, 26)
        value = chr(65 + digit) + value
    return value


def cell(value):
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


class CustomerSheet:
    def __init__(self, service):
        self.service = service
        self.api = service.spreadsheets().values()
        meta = service.spreadsheets().get(spreadsheetId=SSOT_ID, fields="sheets.properties").execute()
        self.tabs = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
        self.props = self.tabs[SSOT_TAB]
        self.headers = self._values(f"'{SSOT_TAB}'!A1:{col(self.props['gridProperties']['columnCount'])}1")[0]
        self.index = {name: i for i, name in enumerate(self.headers) if name}
        for name in (*TRACKING_HEADERS, "company_name", "Status", "website", "Sales_History_JSON"):
            if name not in self.index:
                raise ValueError("SSOT_SCHEMA_NOT_MIGRATED:" + name)
        self._identity = None

    def _values(self, range_):
        return self.api.get(spreadsheetId=SSOT_ID, range=range_, valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])

    def identities(self):
        if self._identity is None:
            end = int(self.props["gridProperties"]["rowCount"])
            fields = ["company_name", "website", "AI_会社ID", "LF_lead_id"]
            ranges = [f"'{SSOT_TAB}'!{col(self.index[k]+1)}2:{col(self.index[k]+1)}{end}" for k in fields]
            response = self.api.batchGet(spreadsheetId=SSOT_ID, ranges=ranges).execute().get("valueRanges", [])
            columns = [r.get("values", []) for r in response]
            if len(columns) != len(fields):
                raise ValueError("IDENTITY_READ_INCOMPLETE")
            self._identity = []
            for offset in range(max(map(len, columns), default=0)):
                row = {key: (values[offset][0] if offset < len(values) and values[offset] else "") for key, values in zip(fields, columns)}
                if row["company_name"]:
                    row["row_number"] = offset + 2
                    self._identity.append(row)
        return self._identity

    def find(self, name, website, key=""):
        host = domain(website)
        if not host or not text(name):
            raise ValueError("IDENTITY_LOOKUP_REQUIRES_NAME_AND_DOMAIN")
        related = [r for r in self.identities() if norm(r["company_name"]) == norm(name) or domain(r["website"]) == host or (key and text(r.get("AI_会社ID") or r.get("LF_lead_id")) == key)]
        if not related:
            return None
        exact = [r for r in related if norm(r["company_name"]) == norm(name) and domain(r["website"]) == host and (not key or company_id(r) == key)]
        if len(exact) != 1 or len(related) != 1:
            raise ValueError("SSOT_IDENTITY_AMBIGUOUS_REVIEW_REQUIRED")
        return self.read(exact[0]["row_number"])

    def read(self, row_number):
        if int(row_number) < 2:
            raise ValueError("CUSTOMER_ROW_REQUIRED")
        got = self._values(f"'{SSOT_TAB}'!A{row_number}:{col(len(self.headers))}{row_number}")
        if not got:
            raise ValueError("SSOT_CUSTOMER_ROW_MISSING")
        values = list(got[0]) + [""] * len(self.headers)
        row = {k: values[i] for i, k in enumerate(self.headers) if k}
        row["row_number"] = int(row_number)
        return row

    def apply(self, row_number, event):
        before = self.read(row_number)
        changes = transition(before, event)
        if not changes:
            return {"written": False, "duplicate": True, "row_number": row_number}
        self._commit(row_number, before, changes, event)
        return {"written": True, "row_number": row_number, "state": changes.get("営業メール状態", before.get("営業メール状態", ""))}

    def _commit(self, row_number, before, changes, event):
        # Fresh identity/state check immediately before the bounded atomic write.
        fresh = self.read(row_number)
        check_fields = {"company_name", "website", "AI_会社ID", "Status", "Sales_History_JSON", "AI_手動対応", "AI_送信予約ID", "営業メール状態"} | set(changes)
        if any(fresh.get(k, "") != before.get(k, "") for k in check_fields):
            raise ValueError("SSOT_CHANGED_REPLAN_REQUIRED")
        requests = []
        for name, value in changes.items():
            if name not in self.index:
                raise ValueError("SSOT_FIELD_MISSING:" + name)
            requests.append({"updateCells": {"start": {"sheetId": self.props["sheetId"], "rowIndex": int(row_number)-1, "columnIndex": self.index[name]},
                "rows": [{"values": [cell(value)]}], "fields": "userEnteredValue"}})
        ledger = self.tabs[LEDGER_TAB]
        ledger_headers = self._values(f"'{LEDGER_TAB}'!A1:{col(ledger['gridProperties']['columnCount'])}1")[0]
        at = event["occurred_at"]
        canonical = {"event_id": event["event_id"], "occurred_at": at,
            "date": datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Tokyo')).date().isoformat(),
            "source_row": str(row_number), "company_key": company_id(before), "company_name": before["company_name"],
            "from_status": before.get("Status", ""), "to_status": changes.get("Status", before.get("Status", "")),
            "action_type": "NEW_DM" if event["kind"] == "SENT" else "MANUAL_SEND" if event["kind"] == "MANUAL_SENT" else event["kind"],
            "source": "CUSTOMER_FIRST", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "lead_id": company_id(before), "writer": event["run_id"], "reason": event.get("reason", event["kind"]),
            "evidence": dumps(event), "code_version": VERSION, "idempotency_key": event["event_id"],
            "canonical_action_id": event["event_id"], "source_origins": "CUSTOMER_FIRST"}
        requests.append({"appendCells": {"sheetId": ledger["sheetId"], "rows": [{"values": [cell(canonical.get(h, "")) for h in ledger_headers]}], "fields": "userEnteredValue"}})
        # Single attempt. On a lost response, resolve event_id from row/ledger.
        self.service.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": requests}).execute()
        observed = self.read(row_number)
        if any(observed.get(k, "") != v for k, v in changes.items()):
            raise RuntimeError("SSOT_WRITE_READBACK_MISMATCH_RECONCILE")

    def register(self, candidate, evidence, run_id):
        """Register a researched entity first; no send permission is granted."""
        name, website = text(candidate.get("company_name")), text(candidate.get("website"))
        existing = self.find(name, website)
        if existing:
            return existing
        if evidence.get("company_verified") is not True or evidence.get("decision") != "GO" or not evidence.get("source_url") or not evidence.get("quote"):
            raise ValueError("PRIMARY_QUALIFICATION_EVIDENCE_REQUIRED")
        if candidate.get("hq_country") in {"Japan", "日本", "JP"} or evidence.get("ceased") is True:
            raise ValueError("EXCLUDED_COMPANY")
        record = {"company_name": name, "website": website, "Status": "未接触",
            "Category": candidate.get("Category", "その他"), "hq_country": candidate.get("hq_country", ""),
            "record_origin": VERSION, "added_at": datetime.now(timezone.utc).isoformat(),
            "営業判定": "GO", "research_sources": evidence["source_url"],
            "selection_reason": evidence.get("reason", evidence["quote"]),
            "営業メール状態": "QUALIFIED", "AI_次アクション": "企業別営業精査・個別文面作成",
            "営業メール送信可否": "未承認", "AI_会社ID": company_id(candidate),
            "AI_状態更新日時": datetime.now(timezone.utc).isoformat()}
        # appendCells reserves the row server-side; never overwrite an estimated tail.
        self.service.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": [{"appendCells": {
            "sheetId": self.props["sheetId"], "rows": [{"values": [cell(record.get(h, "")) for h in self.headers]}], "fields": "userEnteredValue"}}]}).execute()
        self._identity = None
        refreshed = self.service.spreadsheets().get(spreadsheetId=SSOT_ID, fields="sheets.properties").execute()
        self.props = next(s["properties"] for s in refreshed["sheets"] if s["properties"]["title"] == SSOT_TAB)
        found = self.find(name, website, record["AI_会社ID"])
        if not found:
            raise RuntimeError("SSOT_REGISTER_UNCONFIRMED_RECONCILE")
        return found

    def record_execution(self, candidate, result, run_id):
        """Mirror all attempted outcomes, including pre-claim failures, to SSOT.

        Missing/ambiguous SSOT identity is an explicit blocker. The legacy log is
        retained as evidence. This function never invents customer rows or receipts.
        """
        name = candidate.get("company_name", "")
        website = (result.get("audit") or {}).get("official_website") or candidate.get("candidate_website") or candidate.get("website", "")
        row = self.find(name, website)
        if not row:
            raise ValueError("SSOT_REGISTRATION_REQUIRED")
        at = result.get("finished_at") or datetime.now(timezone.utc).isoformat()
        status = text(result.get("status")).upper()
        execution, form = result.get("execution") or {}, result.get("form_execution") or {}
        unknown = status in {"SENT_UNVERIFIED", "FORM_UNCONFIRMED"} or form.get("submission_attempted") or execution.get("send_attempted")
        kind = "SEND_UNKNOWN" if unknown else "SEND_FAILED"
        reason = result.get("error_message") or execution.get("reason") or form.get("reason") or status
        if status in {"READY_FOR_CONNECTOR_SEND", "READY", "PREPARED", "SENT", "FORM_SENT"}:
            # Connector/CRM imports provider proof using apply(). This observation
            # does not assert receipt from a label in the Python return value.
            kind = "QUALITY_HOLD"
            reason = "HANDOFF_REVIEW_REQUIRED" if status == "READY_FOR_CONNECTOR_SEND" else "PROVIDER_RECEIPT_RECONCILIATION_REQUIRED" if status in {"SENT", "FORM_SENT"} else "DRAFT_REVIEW_REQUIRED"
        event = {"event_id": "execution:" + run_id + ":" + company_id(row), "run_id": run_id,
            "company_id": company_id(row), "company_name": row["company_name"], "website": row["website"],
            "occurred_at": at, "kind": kind, "stage": result.get("stage") or "EXECUTION",
            "reason": str(reason), "definitely_not_sent": kind == "SEND_FAILED" and not unknown}
        before = self.read(row["row_number"])
        changes = transition(before, event)
        if not changes:
            return {"duplicate": True, "row_number": row["row_number"]}
        # Preserve the original bytes for human rescue even when quality failed.
        draft = result.get("draft") or {}
        for key, value in (("営業メール件名", draft.get("subject")), ("営業メール本文", draft.get("body")),
                           ("営業メール宛先", (result.get("audit") or {}).get("recipient"))):
            if value and not before.get(key):
                changes[key] = value
        self._commit(row["row_number"], before, changes, event)
        return {"written": True, "row_number": row["row_number"], "state": changes.get("営業メール状態")}
