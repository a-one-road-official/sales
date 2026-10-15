"""Current-claim admission and atomic persistence for the existing form worker.

Uses only the existing Google Sheets/Drive clients supplied by the caller.  No
customer action is implemented here.  Denied writes are surfaced unchanged.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import threading
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


def runtime_log(phase, **details):
    """Preserve infrastructure and pre/post-submit evidence even if Sheets fails."""
    record = dict(details, phase=phase, recorded_at=utcnow().isoformat(),
        github_run_id=os.getenv("GITHUB_RUN_ID", ""),
        shard_index=os.getenv("FORM_PRODUCTION_SHARD_INDEX", "0"))
    line = json.dumps(record, ensure_ascii=False, default=str)
    print(line, flush=True)
    directory = os.getenv("OUTREACH_EVIDENCE_DIR", "")
    if directory:
        try:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            with (root / "form-runtime-events.jsonl").open("a", encoding="utf-8") as out:
                out.write(line + "\n")
        except OSError as exc:
            print(json.dumps({"phase": "LOCAL_EVIDENCE_WRITE_FAILED", "error": str(exc)}), flush=True)
    return record


def read_error_status(error):
    from googleapiclient.errors import HttpError
    if isinstance(error, HttpError):
        return getattr(error.resp, "status", None)
    return None


class FormReadService:
    """Form-only read pacing shared by all Sheets clients in one shard.

    Four shards at >=5 seconds/request consume at most 48 steady-state
    reads/minute together. Writes retain their original no-replay protocol.
    """
    def __init__(self, resource, *, interval=None, sleep=sleep_seconds,
                 monotonic=monotonic_seconds, jitter=random.random, log=runtime_log):
        self.resource = resource
        try:
            configured = float(os.getenv("FORM_SHEETS_READ_INTERVAL_SECONDS", "5")) if interval is None else interval
            self.interval = max(5.0, float(configured))
        except (ValueError, TypeError):
            self.interval = 5.0
        self.sleep, self.monotonic, self.jitter, self.log = sleep, monotonic, jitter, log
        self.next_read = 0.0
        self.lock = threading.Lock()
        self.read_count = 0
        self.retry_count = 0

    def _read(self, request, operation):
        for attempt in range(6):
            with self.lock:
                delay = max(0.0, self.next_read - self.monotonic())
                if delay:
                    self.sleep(delay)
                self.next_read = self.monotonic() + self.interval
                self.read_count += 1
                try:
                    # Hidden SDK retries would bypass the shared read pacing.
                    return request.execute(num_retries=0)
                except Exception as exc:
                    code = read_error_status(exc)
                    if code not in {429, 500, 502, 503, 504} or attempt == 5:
                        self.log("FORM_SHEETS_READ_ERROR", operation=operation,
                            provider_code=code, attempt=attempt + 1, original_error=str(exc),
                            read_requests=self.read_count, external_action_retried=False)
                        raise
                    delay = max(self.interval, min(60.0, 4.0 * (2 ** attempt) + self.jitter()))
                    try:
                        delay = max(delay, min(60.0, float(exc.resp.get("retry-after", 0))))
                    except (AttributeError, TypeError, ValueError):
                        pass
                    self.retry_count += 1
                    self.log("FORM_SHEETS_READ_BACKOFF", operation=operation,
                        provider_code=code, attempt=attempt + 1, delay_seconds=delay,
                        original_error=str(exc), external_action_retried=False)
                    self.next_read = self.monotonic() + delay

    def __getattr__(self, name):
        return getattr(_FormReadResource(self.resource, self), name)


class _FormReadResource:
    def __init__(self, resource, owner):
        self.resource, self.owner = resource, owner

    def __getattr__(self, name):
        method = getattr(self.resource, name)
        def call(*args, **kwargs):
            result = method(*args, **kwargs)
            if name in {"spreadsheets", "values"}:
                return _FormReadResource(result, self.owner)
            if name in {"get", "batchGet"}:
                owner = self.owner
                class ReadRequest:
                    def execute(self, **unused):
                        return owner._read(result, name)
                return ReadRequest()
            return result
        return call


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
            detail.get("status"), detail.get("message"))
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


def discover(svc, data=None):
    if data is None:
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
        range=f"'{CONFIG_TAB}'!A1:B{end}",
        valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
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
    controls = {}
    for key in keys:
        value = values[rows[key] - 1]
        literal = value[1] if len(value) > 1 else ""
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


def legacy_handoff_event_ids(handoff):
    """Recognize the two immutable event IDs used by original scheduled Handoff writers."""
    if (handoff.get('source') == 'DRAFT_READY_HANDOFF'
            and handoff.get('origin') == 'SCHEDULED_AUTOMATION'
            and not handoff.get('task_id') and not handoff.get('phase')
            and handoff.get('run_id') and handoff.get('verified_at')
            and isinstance(handoff.get('raw_row'), int)
            and isinstance(handoff.get('main_row'), int)):
        run_id, raw_row, main_row = (handoff['run_id'], handoff['raw_row'], handoff['main_row'])
        return (
            f"{run_id}:DRAFT_READY:raw{raw_row}:main{main_row}",
            f"draft-ready:{run_id}:main{main_row}",
        )
    return ()


def legacy_handoff_event_id(handoff):
    """Backward-compatible canonical ID; admission reads every exact original variant."""
    ids = legacy_handoff_event_ids(handoff)
    return ids[0] if ids else None


def require_legacy_handoff_receipt(row, packet, handoff, receipt):
    """Validate actual immutable legacy evidence against unchanged saved copy."""
    evidence = strict_json(receipt.get('evidence'))
    identity = handoff.get('identity') or {}
    fields = row['fields']
    subject, body = fields.get('営業メール件名'), fields.get('営業メール本文')
    keys = ('run_id', 'origin', 'raw_row', 'main_row', 'generation_run_id',
            'copy_source_id', 'copy_source_revision', 'copy_source_sha256',
            'input_sha256', 'email_sha256', 'decision_id')
    event_ids = legacy_handoff_event_ids(handoff)
    valid = (event_ids
        and receipt.get('event_id') in event_ids
        and receipt.get('source') == 'DRAFT_READY_HANDOFF'
        and receipt.get('writer') == 'SCHEDULED_AUTOMATION'
        and receipt.get('to_status') == receipt.get('action_type') == 'DRAFT_READY'
        and receipt.get('occurred_at') == handoff.get('verified_at')
        and str(receipt.get('source_row')) == str(packet.get('source_row'))
        and handoff.get('main_row') == packet.get('source_row')
        and receipt.get('company_name') == row.get('company') == evidence.get('company')
        and evidence.get('domain') == packet.get('canonical_domain')
        and all(handoff.get(k) and evidence.get(k) == handoff[k] for k in keys)
        and evidence.get('customer_actions') == 0
        and all((evidence.get('identity') or {}).get(k) is True
                for k in ('source_exact', 'company_exact', 'domain_exact'))
        and all(identity.get(k) is True for k in
                ('source_record_url_exact', 'company_name_exact', 'official_domain_exact'))
        and evidence.get('source_record_url')
        == identity.get('raw_source_record_url') == identity.get('main_source_record_url')
        and isinstance(subject, str) and isinstance(body, str))
    if valid:
        actual = hashlib.sha256((subject + '\n' + body.replace('\r\n', '\n')).encode()).hexdigest()
        valid = actual == handoff.get('email_sha256')
    if not valid:
        raise ValueError('FORM_LEGACY_HANDOFF_RECEIPT_MISMATCH')


def require_saved_ready_copy(row, packet, historical_handoff=None, *, legacy_receipt=None):
    """Bind new downstream form claims to Handoff's exact stored customer copy."""
    invoked = datetime.fromisoformat(str(scheduled_claim(packet)["invoked_at"]).replace("Z", "+00:00"))
    if invoked < datetime(2026, 10, 13, 3, 18, 12, tzinfo=timezone.utc):
        return  # Historical claims retain their original reconciliation contract.
    fields = row["fields"]
    handoff = row["meta"].get("handoff") or historical_handoff or {}
    original_source = (handoff.get("origin") == "SCHEDULED_AUTOMATION"
                       and handoff.get("phase") == "DRAFT_READY_HANDOFF"
                       and handoff.get("run_id") and handoff.get("decision_id")) or (
        handoff.get("task_id") == "6ac5b018a72c81919b80cf3daa6d3cc5"
        and handoff.get("source") == "DRAFT_READY_HANDOFF" and handoff.get("verified_at"))
    if legacy_receipt is not None:
        require_legacy_handoff_receipt(row, packet, handoff, legacy_receipt)
        original_source = True
    if (not original_source or not handoff.get("email_sha256")
            or packet.get("draft_ready_handoff") != handoff):
        raise ValueError("FORM_DRAFT_READY_HANDOFF_REQUIRED")
    subject = fields.get("営業メール件名")
    body = fields.get("営業メール本文")
    if not isinstance(subject, str) or not subject or not isinstance(body, str) or not body:
        raise ValueError("FORM_SAVED_READY_COPY_MISSING")
    if packet.get("subject") != subject or packet.get("form_message") != body:
        raise ValueError("FORM_SAVED_READY_COPY_CHANGED")


def confirmed_email_failure(row, packet, receipt):
    """A prior SENT is recoverable only with exact, immutable terminal DSN proof."""
    proof = packet.get("email_failure_recovery")
    if not isinstance(proof, dict):
        return False
    context = scheduled_claim(packet)
    payload = strict_json(receipt.get("crm_payload"))
    fields = row["fields"]
    expected_tasks = {"A": "6ac4b0e73b548191a3050b65505e67bc",
                      "B": "6ab0b2bbe9b481919513cc9185b0e660"}
    original = proof.get("original_message_id")
    recipient = str(fields.get("Last_Outbound_Recipient") or fields.get("営業メール宛先") or "").strip().casefold()
    history = proof.get("company_history_check") or {}
    status = str(proof.get("dsn_status") or "")
    return bool(
        original and original == fields.get("Last_Outbound_Message_ID")
        and recipient and str(proof.get("original_recipient") or "").strip().casefold() == recipient
        and str(proof.get("dsn_final_recipient") or "").strip().casefold() == recipient
        and proof.get("original_subject") == fields.get("営業メール件名")
        and proof.get("original_body") == fields.get("営業メール本文")
        and proof.get("original_task_id") == expected_tasks.get(proof.get("original_lane"))
        and proof.get("gmail_dsn_message_id") and proof.get("checked_at")
        and proof.get("dsn_action") == "failed"
        and len(status.split(".")) == 3 and status.startswith("5.")
        and all(part.isdigit() for part in status.split("."))
        and proof.get("original_rfc822_message_id")
        and proof.get("dsn_original_rfc822_message_id") == proof.get("original_rfc822_message_id")
        and history.get("full_pagination_complete") is True
        and history.get("human_reply_found") is False
        and history.get("other_successful_contact_found") is False
        and history.get("query") and history.get("checked_at")
        and receipt.get("event_id") == proof.get("evidence_event_id")
        and receipt.get("action_type") == "FORM_EMAIL_FAILURE_VERIFIED"
        and receipt.get("company_name") == row["company"]
        and str(receipt.get("source_row")) == str(packet.get("source_row"))
        and payload.get("origin") == "SCHEDULED_AUTOMATION"
        and payload.get("task_id") == FORM_TASK_ID
        and payload.get("run_id") == context["run_id"]
        and payload.get("email_failure_recovery") == proof
    )


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
        from form_safety import verified_form_success
        if not verified_form_success(result):
            return 'FORM_UNCONFIRMED','FORM_SUCCESS_RECEIPT_NOT_BOUND_TO_SUBMISSION'
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
        before = self.svc.spreadsheets().get(spreadsheetId=SSOT_ID,
            fields="sheets(properties),namedRanges").execute()
        controls, rows = read_controls(self.svc, discover(self.svc, before), preserve_lease_literals=True)
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
        event_id = f"form-production:{domain or number}:{action}:{claim_id}:{self.run_id}"
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
        return self.existing_events_many([identity]).get(identity, [])

    def existing_events_many(self, identities):
        identities = set(identities)
        tabs = discover(self.svc)
        end = tabs[EVENT_TAB]["gridProperties"]["rowCount"]
        col = column_label(self.layout.event_headers.index("event_id") + 1)
        hits = []
        # IDs only: ~44k cells in one read rather than 23 separate calls.
        values = self.values(f"'{EVENT_TAB}'!{col}2:{col}{end}") if end >= 2 else []
        for offset, value in enumerate(values):
            if value and str(value[0]) in identities:
                hits.append((str(value[0]), 2 + offset))
        out = {identity: [] for identity in identities}
        last_col = column_label(len(self.layout.event_headers))
        if hits and len(identities) == 1:
            for identity, number in hits:
                out[identity].extend(self.values(f"'{EVENT_TAB}'!A{number}:{last_col}{number}"))
        elif hits:
            blocks = self.svc.spreadsheets().values().batchGet(spreadsheetId=SSOT_ID,
                ranges=[f"'{EVENT_TAB}'!A{number}:{last_col}{number}" for _, number in hits],
                valueRenderOption="UNFORMATTED_VALUE").execute().get("valueRanges", [])
            if len(blocks) != len(hits):
                raise ValueError("FORM_EVENT_READ_INCOMPLETE")
            for (identity, _), block in zip(hits, blocks):
                out[identity].extend(block.get("values", []))
        return out

    def assert_admission(self, number, expected_claim, *, allow_started=False, peers=()):
        require_form_start(read_controls(self.svc)[0])
        context = self.policy_loader()
        from lead_generator.policy import policy_context
        from form_gate import validate_form_gate, require_form_gate_projection
        current = self.row(number)
        meta = current["meta"]
        packet = meta.get("form_submission_packet_v1") or {}
        scheduler = scheduled_claim(packet)
        historical_handoff = None
        recovery = packet.get("email_failure_recovery")
        lineage = packet.get("recovery") or {}
        if (lineage.get("original_handoff_event_id") and (recovery or {}).get("original_handoff_event_id")
                and lineage["original_handoff_event_id"] != recovery["original_handoff_event_id"]):
            raise ValueError("FORM_ORIGINAL_HANDOFF_ID_CONFLICT")
        historical_id = lineage.get("original_handoff_event_id") or (recovery or {}).get("original_handoff_event_id")
        receipt_ids = [scheduler["claim_event_id"]]
        legacy_ids = legacy_handoff_event_ids(meta.get('handoff') or {})
        receipt_ids.extend(legacy_ids)
        if recovery:
            receipt_ids.append(recovery.get("evidence_event_id"))
            if not meta.get("handoff") and historical_id:
                receipt_ids.append(historical_id)
        receipts = self.existing_events_many(receipt_ids)
        if recovery and not meta.get("handoff"):
            rows = receipts.get(historical_id, []) if historical_id else []
            if len(rows) != 1:
                raise ValueError("FORM_ORIGINAL_HANDOFF_RECEIPT_UNRESOLVED")
            record = dict(zip(self.layout.event_headers, rows[0]))
            hp = strict_json(record.get("crm_payload"))
            historical_handoff = hp.get("handoff") or hp
            if (record.get("source") != "DRAFT_READY_HANDOFF"
                    or record.get("to_status") != "DRAFT_READY"
                    or record.get("action_type") != "DRAFT_READY"
                    or not record.get("occurred_at")
                    or str(record.get("source_row")) != str(number)
                    or record.get("company_name") != current["company"]
                    or historical_handoff.get("main_row") != number
                    or historical_handoff.get("origin") != "SCHEDULED_AUTOMATION"
                    or not historical_handoff.get("run_id")
                    or not (hp.get("phase") == "DRAFT_READY_HANDOFF"
                        or (historical_handoff.get("source") == "DRAFT_READY_HANDOFF"
                            and historical_handoff.get("task_id") == "6ac5b018a72c81919b80cf3daa6d3cc5"))):
                raise ValueError("FORM_ORIGINAL_HANDOFF_RECEIPT_MISMATCH")
        legacy_receipt = None
        if legacy_ids:
            legacy_rows = []
            for legacy_id in legacy_ids:
                rows = receipts.get(legacy_id, [])
                if len(rows) > 1:
                    raise ValueError('FORM_LEGACY_HANDOFF_RECEIPT_UNRESOLVED')
                legacy_rows.extend(rows)
            if len(legacy_rows) != 1:
                raise ValueError('FORM_LEGACY_HANDOFF_RECEIPT_UNRESOLVED')
            legacy_receipt = dict(zip(self.layout.event_headers, legacy_rows[0]))
        require_saved_ready_copy(current, packet, historical_handoff,
                                legacy_receipt=legacy_receipt)
        claim_rows = receipts.get(scheduler["claim_event_id"], [])
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
        failed_delivery = False
        recovery = packet.get("email_failure_recovery")
        if recovery:
            evidence_id = recovery.get("evidence_event_id") if isinstance(recovery, dict) else None
            if not evidence_id:
                raise ValueError("FORM_EMAIL_FAILURE_PROOF_REQUIRED")
            evidence_rows = receipts.get(evidence_id, [])
            if len(evidence_rows) != 1:
                raise ValueError("FORM_EMAIL_FAILURE_RECEIPT_UNRESOLVED")
            failed_delivery = confirmed_email_failure(current, packet,
                dict(zip(self.layout.event_headers, evidence_rows[0])))
            if not failed_delivery:
                raise ValueError("FORM_EMAIL_FAILURE_PROOF_MISMATCH")
        if email_state in PRIOR_CONTACT_STATES:
            if email_state not in {"SENT", "VALID_SENT"} or not failed_delivery:
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
        result = validate_form_gate(gate.get("admission_packet") or {}, receipt,
            historical_handoff or meta.get("handoff"))
        if result.get("domain") != packet.get("canonical_domain"):
            raise ValueError("FORM_CANONICAL_DOMAIN_MISMATCH")
        fields = current["fields"]
        require_form_gate_projection(result, fields, context["policy_version"])
        if result.get("_form_document_revalidation"):
            current["form_gate_audit"] = result["_form_document_revalidation"]
            runtime_log("FORM_GATE_PROVENANCE_REVALIDATED", row=number,
                claim_id=expected_claim, evidence=current["form_gate_audit"])
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
                    runtime_log("OWN_GUARD_CLEANUP_FAILED", original_error=str(cleanup_error),
                        owner=lease.owner, token=lease.token, external_action_retried=False)

    def begin(self, number, row, peers=()):
        packet = row["meta"].get("form_submission_packet_v1") or {}
        claim_id = packet.get("claim_id", "")
        fresh = self.assert_admission(number, claim_id, peers=peers)
        previous = fresh['meta'].get('form_executor_attempt')
        history = copy.deepcopy(fresh['meta'].get('form_executor_attempt_history') or [])
        if previous:
            if (previous.get('claim_id') != claim_id or not previous.get('run_id')
                    or not previous.get('finished_at') or previous.get('outcome') != 'FORM_FAILED'
                    or previous.get('submission_attempted') is not False):
                raise ValueError('FORM_PRIOR_ATTEMPT_RECONCILE_REQUIRED')
            domain = (fresh['meta'].get('form_submission_packet_v1') or {}).get('canonical_domain') or fresh['website']
            base = f'form-production:{domain or number}:FORM_FAILED:{claim_id}'
            receipt_ids = [base, f"{base}:{previous['run_id']}"]
            records = self.existing_events_many(receipt_ids)
            matches = []
            for identity in receipt_ids:
                if len(records.get(identity, [])) > 1:
                    raise ValueError('FORM_PRIOR_NO_SUBMIT_RECEIPT_UNRESOLVED')
                for values in records.get(identity, []):
                    receipt = dict(zip(self.layout.event_headers, values))
                    payload = strict_json(receipt.get('crm_payload'))
                    if payload.get('run_id') == previous['run_id']:
                        matches.append((receipt, payload))
            if len(matches) != 1:
                raise ValueError('FORM_PRIOR_NO_SUBMIT_RECEIPT_UNRESOLVED')
            receipt, payload = matches[0]
            result = payload.get('result') or {}
            if (receipt.get('action_type') != 'FORM_FAILED'
                    or str(receipt.get('source_row')) != str(number)
                    or receipt.get('company_name') != fresh['company']
                    or payload.get('origin') != 'GITHUB_EXISTING_FORM_EXECUTOR'
                    or payload.get('claim_id') != claim_id
                    or result.get('submission_attempted') is not False
                    or result != fresh['meta'].get('form_result')):
                raise ValueError('FORM_PRIOR_NO_SUBMIT_RECEIPT_MISMATCH')
            if not isinstance(history, list):
                raise ValueError('FORM_ATTEMPT_HISTORY_INVALID')
            history.append(dict(attempt=copy.deepcopy(previous), result=copy.deepcopy(result),
                                event_id=receipt['event_id']))
        attempt = {"claim_id": claim_id, "run_id": self.run_id, "started_at": self.now().isoformat(),
            "origin": "GITHUB_EXISTING_FORM_EXECUTOR", "status": "STARTED", "outcome": "UNRESOLVED",
            "packet_sha256": packet_digest(fresh["meta"].get("form_submission_packet_v1")),
            "gate_sha256": packet_digest(fresh["meta"].get("common_gate")),
            "claim_receipt_sha256": fresh["claim_receipt_sha256"]}
        event = self.event(number, fresh, "FORM_EXECUTION_STARTED", "Durable no-replay barrier", attempt, claim_id)
        requested = {'form_executor_attempt': attempt}
        if previous:
            requested['form_executor_attempt_history'] = history
        merged = self.commit(number, fresh, requested, event, require_start=True)
        fresh["meta"] = merged
        return fresh


