"""Current-claim admission and atomic persistence for the existing form worker.

Uses only the existing Google Sheets/Drive clients supplied by the caller.  No
customer action is implemented here.  Denied writes are surfaced unchanged.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from time import sleep as sleep_seconds, monotonic as monotonic_seconds
import uuid

from sheets_persistence import (Layout, Lease, acquire_requests, cell_update,
    commit_and_release_requests, release_requests, verify_event_readback,
    GuardObservation, GuardAcquireRejection, GUARD_CONTENTION_MESSAGE, PROTOCOL,
    acquire_with_release_wait, master_control)

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
CONFIG_TAB = "SalesOS_Goal_Config"
EVENT_TAB = "SalesOS_Action_Events"
FORM_TASK_ID = "6ab3aacf782c819187278ca4be5d4751"
POLICY_DOCUMENT_ID = "1IzGKFUHxEHmaxlUfJIoPlNqwrLOqrpsJ0V378cOypbQ"
CONTROL_KEYS = frozenset({"AUMS_MASTER_RUN_STATE", "CHATGPT_FORM_RUN_STATE",
    "FORM_EXECUTOR_RUN_STATE", "FORM_PRODUCTION_BATCH_SIZE",
    "OUTBOUND_COPY_QUALITY_HEALTH", "OUTBOUND_RECONCILE_HEALTH",
    "OUTBOUND_PACING_LEASE_OWNER", "OUTBOUND_PACING_LEASE_TOKEN",
    "OUTBOUND_PACING_LEASE_UNTIL"})
TERMINAL_STATES = {"FORM_SENT", "FORM_UNCONFIRMED", "FORM_SUPPRESSED"}
PRIOR_CONTACT_STATES = {"VALID_SENT", "DELIVERED_CONFIRMED", "SENT", "UNKNOWN",
    "FORM_SENT", "FORM_UNCONFIRMED", "SUBMIT_REQUESTED"}
MANUAL_STATES = {"手動対応中", "手動完了", "HUMAN_REQUIRED"}
BLOCKED_STATUSES = {"返信あり", "アポ確定", "商談化", "商談中", "商談実施", "提案",
    "提案済み", "受注", "合意・契約締結", "拒否", "NG", "配信停止", "DO_NOT_CONTACT",
    "手動対応中", "手動完了"}


def utcnow():
    return datetime.now(timezone.utc)


def native_guard_rejection(error):
    """Return retry proof only for the exact real native HttpError rejection."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return None
    if not isinstance(error, HttpError) or getattr(getattr(error, "resp", None), "status", None) != 400:
        return None
    try:
        payload = json.loads(error.content)
        detail = payload["error"]
        if not isinstance(detail, dict):
            return None
        rejection = GuardAcquireRejection(error.resp.status, detail.get("code"),
            detail.get("status", "INVALID_ARGUMENT"), detail.get("message"))
        return rejection if rejection.is_exact_contention() else None
    except (AttributeError, TypeError, ValueError, KeyError):
        return None


def known_guard_contention(error):
    return native_guard_rejection(error) is not None


def column_label(number):
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def strict_json(value):
    if value in (None, ""):
        return {}
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, dict):
        raise ValueError("FORM_META_MUST_BE_OBJECT")
    return result


def discover(svc):
    data = svc.spreadsheets().get(spreadsheetId=SSOT_ID,
        fields="sheets(properties),namedRanges").execute()
    tabs = {}
    for item in data.get("sheets", []):
        p = item["properties"]
        if p["title"] in tabs:
            raise ValueError("DUPLICATE_TAB_TITLE")
        tabs[p["title"]] = p
    for title in (SALES_TAB, CONFIG_TAB, EVENT_TAB):
        if title not in tabs:
            raise ValueError("MISSING_TAB:" + title)
    return tabs


def read_controls(svc, tabs=None, *, preserve_lease_literals=False):
    tabs = tabs or discover(svc)
    end = tabs[CONFIG_TAB]["gridProperties"]["rowCount"]
    values = svc.spreadsheets().values().get(spreadsheetId=SSOT_ID,
        range=f"'{CONFIG_TAB}'!A1:A{end}").execute().get("values", [])
    rows = {}
    for number, row in enumerate(values, 1):
        key = str(row[0] if row else "").strip()
        if key in CONTROL_KEYS:
            if key in rows:
                raise ValueError("DUPLICATE_CONTROL:" + key)
            rows[key] = number
    keys = sorted(rows, key=rows.get)
    if not keys:
        return {}, rows
    result = svc.spreadsheets().values().batchGet(spreadsheetId=SSOT_ID,
        ranges=[f"'{CONFIG_TAB}'!B{rows[key]}:B{rows[key]}" for key in keys],
        valueRenderOption="UNFORMATTED_VALUE").execute().get("valueRanges", [])
    if len(result) != len(keys):
        raise ValueError("CONTROL_READ_INCOMPLETE")
    controls = {}
    for key, block in zip(keys, result):
        val = block.get("values", [[]])
        literal = val[0][0] if val and val[0] else ""
        controls[key] = literal if preserve_lease_literals and key in {
            "OUTBOUND_PACING_LEASE_OWNER", "OUTBOUND_PACING_LEASE_TOKEN", "OUTBOUND_PACING_LEASE_UNTIL"
        } else str(literal).strip()
    return controls, rows


def require_form_start(controls):
    for key in ("AUMS_MASTER_RUN_STATE",):
        if controls.get(key) != "START":
            raise ValueError("FORM_CONTROL_BLOCK:" + key + "=" + str(controls.get(key, "MISSING")))
    if controls.get("OUTBOUND_COPY_QUALITY_HEALTH", "").split(":", 1)[0] != "PASS":
        raise ValueError("FORM_COPY_QUALITY_NOT_PASS")
    health = controls.get("OUTBOUND_RECONCILE_HEALTH", "")
    if health.split(":", 1)[0].strip().upper() not in {"OK", "PASS"}:
        raise ValueError("FORM_PERSISTENCE_HEALTH_BLOCK:" + health)


def policy_from_drive(drive):
    from lead_generator.policy import bind_policy
    fields = "id,version,modifiedTime,mimeType"
    before = drive.files().get(fileId=POLICY_DOCUMENT_ID, fields=fields).execute()
    if before.get("mimeType") != "application/vnd.google-apps.document":
        raise ValueError("CANONICAL_POLICY_NOT_NATIVE_DOC")
    raw = drive.files().export(fileId=POLICY_DOCUMENT_ID, mimeType="text/plain").execute()
    after = drive.files().get(fileId=POLICY_DOCUMENT_ID, fields=fields).execute()
    if before != after:
        raise ValueError("CANONICAL_POLICY_CHANGED_DURING_READ")
    if not after.get("version") or not after.get("modifiedTime"):
        raise ValueError("CANONICAL_POLICY_VERSION_MISSING")
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if not isinstance(text, str):
        raise ValueError("CANONICAL_POLICY_EXPORT_NOT_TEXT")
    return bind_policy(text, document_id=POLICY_DOCUMENT_ID,
        revision_id="drive-version:" + str(after["version"]) + ":" + after["modifiedTime"])


def scheduled_claim(packet):
    context = packet.get("scheduler_context")
    if not isinstance(context, dict):
        raise ValueError("SCHEDULED_FORM_CLAIM_CONTEXT_REQUIRED")
    if context.get("task_id") != FORM_TASK_ID or context.get("origin") != "SCHEDULED_AUTOMATION":
        raise ValueError("FORM_CLAIM_NOT_EXISTING_SCHEDULER")
    if not all(context.get(k) for k in ("run_id", "invoked_at", "nominal_slot", "claim_event_id")):
        raise ValueError("SCHEDULED_FORM_CLAIM_CONTEXT_INCOMPLETE")
    for key in ("invoked_at", "nominal_slot"):
        at = datetime.fromisoformat(str(context[key]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("FORM_CLAIM_TIMEZONE_REQUIRED")
    return context


def merge_owned(fresh, requested):
    """Only deterministic form-owned keys; preserve unrelated EC and hard holds."""
    merged = copy.deepcopy(fresh)
    for key, value in requested.items():
        if key.startswith("form_") or key.startswith("email_auto_suppressed_due_to_form_") or key in {
                "email_fallback_allowed", "channel_router_state"}:
            merged[key] = copy.deepcopy(value)
    if requested.get("auto_outbound_blocked") is True:
        merged["auto_outbound_blocked"] = True
        if not fresh.get("auto_outbound_blocked"):
            for key in ("suppression_reason", "suppression_scope"):
                if key in requested:
                    merged[key] = requested[key]
    if fresh.get("auto_outbound_blocked"):
        merged["email_fallback_allowed"] = False
    return merged


def classify_form_result(result):
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or result.get("confirmation") or status or "FORM_FAILED")
    if status == "FORM_SENT":
        return "FORM_SENT", reason
    if result.get("submission_attempted") and not result.get("explicit_negative_confirmation"):
        return "FORM_UNCONFIRMED", reason
    if status in {"BLOCKED", "DUPLICATE_BLOCKED"}:
        return "FORM_SUPPRESSED", reason
    if status == "FORM_UNCONFIRMED":
        return "FORM_UNCONFIRMED", reason
    return "FORM_FAILED", reason


def execution_pending(meta):
    attempt = meta.get("form_executor_attempt") or {}
    if not attempt.get("started_at"):
        return False
    # Replacing the packet must not erase an older unresolved external attempt.
    safely_failed = (attempt.get("outcome") == "FORM_FAILED" and attempt.get("finished_at")
        and (attempt.get("submission_attempted") is False
             or attempt.get("explicit_negative_confirmation") is True))
    return not safely_failed


def packet_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def canonical_domain(value):
    from lead_generator.policy import domain
    raw = str(value or "").strip()
    return domain(raw if "://" in raw else "https://" + raw) if raw else ""


def execution_payload(number, row):
    packet = row["meta"].get("form_submission_packet_v1") or {}
    payload = {"form_url": str(packet.get("form_url") or "").strip(),
        "website": str(packet.get("website") or row["website"] or "").strip(),
        "company_name": str(packet.get("company_name") or row["company"] or "").strip(),
        "subject": str(packet.get("subject") or "").strip(),
        "message": str(packet.get("form_message") or "").strip(),
        "draft_id": str(packet.get("claim_id") or "").strip(),
        "source_row": str(number), "preview_only": False,
        "idempotency_key": "form-domain:" + canonical_domain(packet.get("canonical_domain") or row["website"]),
        "field_overrides": copy.deepcopy(packet.get("field_overrides"))
            if isinstance(packet.get("field_overrides"), dict) else None}
    if not all(payload[key] for key in ("form_url", "website", "company_name", "message", "draft_id")):
        raise ValueError("INCOMPLETE_FORM_SUBMISSION_PACKET")
    return payload


class FormPersistence:
    def __init__(self, svc, policy_loader, *, now=utcnow, sleep=sleep_seconds,
                 monotonic=monotonic_seconds, remaining_budget_seconds=None):
        self.svc, self.policy_loader, self.now, self.sleep = svc, policy_loader, now, sleep
        self.monotonic = monotonic
        # Caller may reserve finalization/readback time in its remaining budget.
        # The default cap is per acquisition episode, not a job-wide result ban.
        self.remaining_budget_seconds = remaining_budget_seconds
        self.tabs = discover(svc)
        controls, rows = read_controls(svc, self.tabs)
        self.initial_controls = controls
        owner, token, until = (rows[k] for k in ("OUTBOUND_PACING_LEASE_OWNER",
            "OUTBOUND_PACING_LEASE_TOKEN", "OUTBOUND_PACING_LEASE_UNTIL"))
        ep = self.tabs[EVENT_TAB]
        headers = self.values(f"'{EVENT_TAB}'!A1:{column_label(ep['gridProperties']['columnCount'])}1")
        self.layout = Layout(self.tabs[CONFIG_TAB]["sheetId"], ep["sheetId"], owner, token, until,
            tuple(headers[0] if headers else []))
        sp = self.tabs[SALES_TAB]
        headers = self.values(f"'{SALES_TAB}'!A1:{column_label(sp['gridProperties']['columnCount'])}1")
        self.header = {}
        for pos, header in enumerate(headers[0] if headers else [], 1):
            if header:
                if header in self.header:
                    raise ValueError("DUPLICATE_COMPANY_HEADER:" + header)
                self.header[header] = pos
        for key in ("company_name", "Status", "website", "AI実行JSON", "LF_screening_status",
                    "LF_gate_version", "LF_error", "営業判定", "営業メール状態", "AI担当状態"):
            if key not in self.header:
                raise ValueError("MISSING_COMPANY_HEADER:" + key)
        self.run_id = "form-executor-" + uuid.uuid4().hex

    def values(self, a1):
        return self.svc.spreadsheets().values().get(spreadsheetId=SSOT_ID, range=a1,
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])

    def guard_observation(self, *, require_start=False):
        before = self.svc.spreadsheets().get(spreadsheetId=SSOT_ID, fields="namedRanges").execute()
        controls, rows = read_controls(self.svc, preserve_lease_literals=True)
        after = self.svc.spreadsheets().get(spreadsheetId=SSOT_ID, fields="namedRanges").execute()
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("FORM_GUARD_METADATA_READ_INVALID")
        expected = {"OUTBOUND_PACING_LEASE_OWNER": self.layout.owner_row,
            "OUTBOUND_PACING_LEASE_TOKEN": self.layout.token_row,
            "OUTBOUND_PACING_LEASE_UNTIL": self.layout.until_row}
        if any(rows.get(key) != number for key, number in expected.items()):
            raise ValueError("FORM_GUARD_CONTROL_LAYOUT_CHANGED")
        if require_start:
            require_form_start(controls)
        return GuardObservation(before.get("namedRanges", []), after.get("namedRanges", []),
            {"owner": controls["OUTBOUND_PACING_LEASE_OWNER"],
             "token": controls["OUTBOUND_PACING_LEASE_TOKEN"],
             "until": controls["OUTBOUND_PACING_LEASE_UNTIL"]},
            master_control([["AUMS_MASTER_RUN_STATE", controls.get("AUMS_MASTER_RUN_STATE", "")]]),
            PROTOCOL)

    def row(self, number):
        data = self.values(f"'{SALES_TAB}'!A{number}:{column_label(max(self.header.values()))}{number}")
        vals = list(data[0] if data else [])
        vals += [""] * max(0, max(self.header.values()) - len(vals))
        mapped = {key: vals[pos - 1] for key, pos in self.header.items()}
        return {"company": str(mapped["company_name"]).strip(), "status": str(mapped["Status"]).strip(),
            "website": str(mapped["website"]).strip(), "meta": strict_json(mapped["AI実行JSON"]),
            "fields": mapped}

    def event(self, number, row, action, reason, evidence, claim_id):
        at = self.now().isoformat()
        domain = str((row["meta"].get("form_submission_packet_v1") or {}).get("canonical_domain") or row["website"])
        event_id = f"form-production:{domain or number}:{action}:{claim_id}"
        payload = dict(evidence, run_id=self.run_id, phase=action, origin="GITHUB_EXISTING_FORM_EXECUTOR",
            claim_id=claim_id, source_row=number)
        return {"event_id": event_id, "occurred_at": at, "date": at[:10], "source_row": str(number),
            "company_key": domain, "company_name": row["company"], "action_type": action,
            "source": "FORM_PRODUCTION_PLAYWRIGHT", "recorded_at": at, "lead_id": f"ssot-row:{number}",
            "writer": "form_production", "reason": reason, "evidence": json.dumps(evidence, ensure_ascii=False),
            "timestamp": at, "code_version": "FORM_PERSISTENCE_V3_20261004",
            "idempotency_key": event_id, "canonical_action_id": event_id,
            "source_origins": "existing_chatgpt_scheduler_claim|github_form_executor",
            "business_segment": "Factory/BPO", "industry": "", "crm_payload": payload, "crm_result": "RECORDED"}

    def existing_events(self, identity):
        tabs = discover(self.svc)
        end = tabs[EVENT_TAB]["gridProperties"]["rowCount"]
        col = column_label(self.layout.event_headers.index("event_id") + 1)
        hits = []
        for start in range(2, end + 1, 2000):
            values = self.values(f"'{EVENT_TAB}'!{col}{start}:{col}{min(end,start+1999)}")
            for offset, value in enumerate(values):
                if value and str(value[0]) == identity:
                    hits.append(start + offset)
        out = []
        last_col = column_label(len(self.layout.event_headers))
        for number in hits:
            out.extend(self.values(f"'{EVENT_TAB}'!A{number}:{last_col}{number}"))
        return out

    def assert_admission(self, number, expected_claim, *, allow_started=False, peers=()):
        require_form_start(read_controls(self.svc)[0])
        context = self.policy_loader()
        from lead_generator.policy import validate_receipt, policy_context
        current = self.row(number)
        meta = current["meta"]
        packet = meta.get("form_submission_packet_v1") or {}
        scheduler = scheduled_claim(packet)
        claim_rows = self.existing_events(scheduler["claim_event_id"])
        if len(claim_rows) != 1:
            raise ValueError("FORM_SCHEDULER_CLAIM_RECEIPT_UNRESOLVED")
        receipt_row = dict(zip(self.layout.event_headers, claim_rows[0]))
        receipt_payload = strict_json(receipt_row.get("crm_payload"))
        if (receipt_payload.get("run_id") != scheduler["run_id"]
                or receipt_payload.get("origin") != "SCHEDULED_AUTOMATION"
                or receipt_payload.get("task_id") != FORM_TASK_ID
                or receipt_payload.get("claim_id") != expected_claim
                or receipt_row.get("action_type") != "FORM_SUBMIT_REQUESTED"
                or str(receipt_row.get("source_row")) != str(number)
                or receipt_row.get("company_name") != current["company"]):
            raise ValueError("FORM_SCHEDULER_CLAIM_RECEIPT_MISMATCH")
        current["claim_receipt_sha256"] = packet_digest(receipt_row)
        if packet.get("claim_id") != expected_claim or meta.get("form_state") != "FORM_SUBMIT_REQUESTED":
            raise ValueError("FORM_CLAIM_CHANGED_OR_CLOSED")
        if int(packet.get("source_row", -1)) != number or packet.get("company_name") != current["company"]:
            raise ValueError("FORM_CLAIM_IDENTITY_MISMATCH")
        email_state = str(current["fields"].get("営業メール状態", "")).upper()
        if email_state in PRIOR_CONTACT_STATES:
            raise ValueError("FORM_PRIOR_OR_AMBIGUOUS_CONTACT:" + email_state)
        if current["status"] in BLOCKED_STATUSES or meta.get("auto_outbound_blocked"):
            raise ValueError("FORM_COMPANY_SUPPRESSED")
        if str(current["fields"].get("AI担当状態", "")) in MANUAL_STATES:
            raise ValueError("FORM_MANUAL_STATE")
        for peer in peers:
            p = self.row(peer)
            pm = p["meta"]
            if p["status"] in BLOCKED_STATUSES or pm.get("auto_outbound_blocked") or pm.get("form_state") in TERMINAL_STATES:
                raise ValueError("FORM_COMPANY_PEER_SUPPRESSED")
            if str(p["fields"].get("営業メール状態", "")).upper() in PRIOR_CONTACT_STATES:
                raise ValueError("FORM_COMPANY_PEER_PRIOR_OR_AMBIGUOUS_CONTACT")
            if str(p["fields"].get("AI担当状態", "")) in MANUAL_STATES:
                raise ValueError("FORM_COMPANY_PEER_MANUAL_STATE")
            if pm.get("form_state") == "FORM_SUBMIT_REQUESTED" or execution_pending(pm):
                raise ValueError("FORM_COMPANY_PEER_CLAIM_UNRESOLVED")
        if execution_pending(meta) and not allow_started:
            raise ValueError("FORM_EXECUTION_OUTCOME_RECONCILE_REQUIRED")
        if allow_started:
            attempt = meta.get("form_executor_attempt") or {}
            if (attempt.get("claim_id") != expected_claim or attempt.get("run_id") != self.run_id
                    or attempt.get("outcome") != "UNRESOLVED" or not attempt.get("started_at")):
                raise ValueError("FORM_ACTIVE_EXECUTOR_CLAIM_MISMATCH")
        gate = meta.get("common_gate") or {}
        receipt = gate.get("admission_result") or gate.get("result") or {}
        if gate.get("admission_result") and gate.get("result") and gate["admission_result"] != gate["result"]:
            raise ValueError("CONFLICTING_FORM_GATE_RECEIPTS")
        result = validate_receipt(gate.get("admission_packet") or {}, receipt)
        if result.get("domain") != packet.get("canonical_domain"):
            raise ValueError("FORM_CANONICAL_DOMAIN_MISMATCH")
        fields = current["fields"]
        if result.get("decision") != "PASS" or fields["LF_screening_status"] != "PASS" or fields["LF_gate_version"] != context["policy_version"]:
            raise ValueError("FORM_CURRENT_GATE_NOT_PASS")
        if fields["LF_error"]:
            raise ValueError("FORM_CURRENT_GATE_ERROR")
        if fields["営業判定"] != policy_context()["policy"]["output"]["PASS"]:
            raise ValueError("FORM_CURRENT_GATE_PROJECTION_MISMATCH")
        return current

    def submission_guard(self, number, expected_row, actual_payload, *, peers=()):
        expected = copy.deepcopy(expected_row)
        payload = copy.deepcopy(actual_payload)
        if payload != execution_payload(number, expected):
            raise ValueError("FORM_EXECUTION_PAYLOAD_NOT_BOUND_TO_CLAIM")
        def check():
            fresh = self.assert_admission(number, payload["draft_id"], allow_started=True, peers=peers)
            for key in ("form_submission_packet_v1", "common_gate"):
                if fresh["meta"].get(key) != expected["meta"].get(key):
                    raise ValueError("FORM_EXECUTION_SNAPSHOT_CHANGED:" + key)
            if (fresh.get("claim_receipt_sha256") != expected.get("claim_receipt_sha256")
                    or not expected.get("claim_receipt_sha256")):
                raise ValueError("FORM_SCHEDULER_RECEIPT_CHANGED")
            if execution_payload(number, fresh) != payload:
                raise ValueError("FORM_EXECUTION_PAYLOAD_CHANGED")
            return fresh
        return check

    def commit(self, number, expected_row, requested, event, *, require_start=False):
        claim = (expected_row["meta"].get("form_submission_packet_v1") or {}).get("claim_id")
        # Scan immutable identities before acquiring the short state guard.
        if self.existing_events(event["event_id"]):
            raise ValueError("FORM_EVENT_ALREADY_EXISTS_RECONCILE")
        event_tail_start = max(2, discover(self.svc)[EVENT_TAB]["gridProperties"]["rowCount"] - 5)
        acquired = False
        try:
            # Known native owners can be observed before the first acquire.
            # Polling does not burn attempts; ambiguous mutations never retry.
            def waited(decision):
                print(json.dumps({"phase": "FORM_GUARD_RELEASE_WAIT",
                    "acquire_attempts": decision.state.acquire_attempts,
                    "elapsed_seconds": decision.state.elapsed_seconds,
                    "next_poll_seconds": decision.delay_seconds, "reason": decision.reason,
                    "owner": decision.owner.owner, "token": decision.owner.token,
                    "immutable_until": decision.owner.until,
                    "existing_owner_unchanged": True, "customer_action_retried": False}, ensure_ascii=False))
            lease = acquire_with_release_wait(self.layout,
                observe=lambda: self.guard_observation(require_start=require_start),
                make_lease=lambda now: Lease(self.run_id, "form_guard_" + uuid.uuid4().hex,
                    now, now + timedelta(seconds=120)),
                acquire=lambda lease, now: self.svc.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID,
                    body={"requests": acquire_requests(self.layout, lease, now)}).execute(),
                rejection_from_error=native_guard_rejection, now=self.now,
                monotonic=self.monotonic, sleep=self.sleep, require_start=require_start,
                remaining_budget_seconds=self.remaining_budget_seconds, on_wait=waited)
            acquired = True
            if require_start:
                require_form_start(read_controls(self.svc)[0])
            fresh = self.row(number)
            if (fresh["company"], fresh["website"]) != (expected_row["company"], expected_row["website"]):
                raise ValueError("FORM_IDENTITY_CHANGED_BEFORE_COMMIT")
            if fresh["meta"].get("form_state") in TERMINAL_STATES:
                raise ValueError("FORM_TERMINAL_STATE_PRESERVED")
            if (fresh["meta"].get("form_submission_packet_v1") or {}).get("claim_id") != claim:
                raise ValueError("FORM_CLAIM_CHANGED_BEFORE_COMMIT")
            if fresh["meta"].get("form_state") != expected_row["meta"].get("form_state"):
                raise ValueError("FORM_STATE_CHANGED_BEFORE_COMMIT")
            for key in requested:
                if key.startswith("form_") and fresh["meta"].get(key) != expected_row["meta"].get(key):
                    raise ValueError("CONCURRENT_FORM_FIELD_CHANGE:" + key)
            if require_start:
                if fresh["status"] in BLOCKED_STATUSES or fresh["meta"].get("auto_outbound_blocked"):
                    raise ValueError("FORM_SUPPRESSION_CHANGED_BEFORE_COMMIT")
                for key in ("form_submission_packet_v1", "common_gate"):
                    if fresh["meta"].get(key) != expected_row["meta"].get(key):
                        raise ValueError("FORM_ADMISSION_CHANGED_BEFORE_COMMIT:" + key)
                if execution_pending(fresh["meta"]):
                    raise ValueError("FORM_EXECUTION_ALREADY_STARTED")
            event_end = discover(self.svc)[EVENT_TAB]["gridProperties"]["rowCount"]
            id_column = column_label(self.layout.event_headers.index("event_id") + 1)
            tail_ids = self.values(f"'{EVENT_TAB}'!{id_column}{event_tail_start}:{id_column}{event_end}")
            if any(item and str(item[0]) == event["event_id"] for item in tail_ids):
                raise ValueError("FORM_EVENT_ALREADY_EXISTS_RECONCILE")
            merged = merge_owned(fresh["meta"], requested)
            update = cell_update(self.tabs[SALES_TAB]["sheetId"], number, self.header["AI実行JSON"], merged)
            requests = commit_and_release_requests(self.layout, lease, self.now(), [update], [event])
            self.svc.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": requests}).execute()
            acquired = False
            actual = self.row(number)["meta"]
            for key in requested:
                if key.startswith("form_") and actual.get(key) != merged.get(key):
                    raise ValueError("FORM_STATE_READBACK_MISMATCH:" + key)
            observed = self.existing_events(event["event_id"])
            checked = verify_event_readback(self.layout, [event], observed)
            if not checked["verified"]:
                raise ValueError("FORM_EVENT_READBACK_UNRESOLVED:" + json.dumps(checked))
            return merged
        finally:
            if acquired:
                try:
                    self.svc.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID,
                        body={"requests": release_requests(self.layout, lease)}).execute()
                except Exception as cleanup_error:
                    print(json.dumps({"phase": "OWN_GUARD_CLEANUP_FAILED", "error": str(cleanup_error),
                        "owner": lease.owner, "token": lease.token}, ensure_ascii=False))

    def begin(self, number, row, peers=()):
        packet = row["meta"].get("form_submission_packet_v1") or {}
        claim_id = packet.get("claim_id", "")
        fresh = self.assert_admission(number, claim_id, peers=peers)
        attempt = {"claim_id": claim_id, "run_id": self.run_id, "started_at": self.now().isoformat(),
            "origin": "GITHUB_EXISTING_FORM_EXECUTOR", "status": "STARTED", "outcome": "UNRESOLVED",
            "packet_sha256": packet_digest(fresh["meta"].get("form_submission_packet_v1")),
            "gate_sha256": packet_digest(fresh["meta"].get("common_gate")),
            "claim_receipt_sha256": fresh["claim_receipt_sha256"]}
        event = self.event(number, fresh, "FORM_EXECUTION_STARTED", "Durable no-replay barrier", attempt, claim_id)
        merged = self.commit(number, fresh, {"form_executor_attempt": attempt}, event, require_start=True)
        fresh["meta"] = merged
        return fresh
