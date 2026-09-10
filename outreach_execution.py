from __future__ import annotations

import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

from prompt_ssot import prompt_freshness

from google.auth import default
from googleapiclient.discovery import build


CRITICAL_FIELDS = {
    "recipient",
    "subject",
    "body",
    "company_name",
    "lane",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "ON"}


def _sheet_rows_with_retry(sheets):
    if sheets is None:
        return []
    last_error = None
    for attempt in range(5):
        try:
            return sheets._rows_as_dicts("LeadFactory_ExecutionLog", "O")
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("email_idempotency_lookup_unavailable") from last_error


def _gmail_header_map(message: dict) -> dict[str, str]:
    return {
        str(item.get("name") or "").lower(): str(item.get("value") or "")
        for item in (message.get("payload") or {}).get("headers", [])
    }


def _find_existing_gmail_message(service, *, sender: str, recipient: str, idempotency_key: str) -> str:
    """Return any prior outbound message to this exact recipient.

    The idempotency header protects retries from this worker.  The recipient
    fallback also protects against older sends that predate that header.
    """
    query = f"from:{sender} to:{recipient} newer_than:30d"
    listed = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    fallback = ""
    for item in listed.get("messages", []) or []:
        message_id = str(item.get("id") or "").strip()
        if not message_id:
            continue
        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["X-Aone-Idempotency-Key"],
        ).execute()
        headers = _gmail_header_map(message)
        if headers.get("x-aone-idempotency-key") == idempotency_key:
            return message_id
        if not fallback:
            fallback = message_id
    return fallback
def _find_existing_gmail_message_with_retry(service, *, sender: str, recipient: str, idempotency_key: str) -> str:
    last_error = None
    for attempt in range(4):
        try:
            return _find_existing_gmail_message(
                service,
                sender=sender,
                recipient=recipient,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            last_error = exc
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(min(8.0, 2 ** attempt))
    raise RuntimeError("gmail_idempotency_lookup_unavailable") from last_error


PROTECTED_FACTORY_LANES = {
    "FACTORY",
    "BPO",
    "SSOT",
    "FACTORY_SSOT",
    "PRODUCTION_SSOT",
    "MITTELSTAND",
}


def lane_from(row: dict) -> str:
    """Return the explicit normalized outreach lane for a row."""
    data = row if isinstance(row, dict) else {}
    for key in ("lane", "outreach_lane", "source_lane"):
        value = str(data.get(key) or "").strip()
        if value:
            return "".join(
                char if char.isalnum() else "_"
                for char in value.upper()
            ).strip("_")
    return ""


def _lane_flag(lane: str) -> str:
    normalized = "".join(
        char if char.isalnum() else "_"
        for char in str(lane or "").strip().upper()
    ).strip("_")
    if normalized in {"EC", "RETAIL", "SACRIFICE", "EC_SACRIFICE"}:
        return "OUTREACH_SACRIFICE_SEND_ENABLED"
    return f"OUTREACH_{normalized}_SEND_ENABLED"


def _cfg_truthy(cfg: dict[str, str], key: str) -> bool:
    return _truthy(cfg.get(key, os.getenv(key, "FALSE")))


def _list_only_hard_lock(cfg: dict[str, str]) -> bool:
    """Keep outbound execution closed unless an explicit code/test override exists."""
    value = cfg.get(
        "LEAD_FACTORY_LIST_ONLY_LOCK",
        os.getenv("LEAD_FACTORY_LIST_ONLY_LOCK", "TRUE"),
    )
    return not _truthy(value)


def outbound_lane_send_enabled(lane: str, cfg: dict[str, str]) -> bool:
    """Return whether an explicitly approved outbound lane may send."""
    if _list_only_hard_lock(cfg):
        return False
    normalized = lane_from({"lane": lane})
    allowed = {
        item.strip().upper()
        for item in str(cfg.get("OUTREACH_ALLOWED_LANES", "") or "").split(",")
        if item.strip()
    }
    common = (
        _cfg_truthy(cfg, "LEAD_FACTORY_ALLOW_EXTERNAL_WRITE")
        and str(cfg.get("LEAD_FACTORY_SEND_MODE", "")).strip().upper() == "ENABLED"
        and _cfg_truthy(cfg, "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL")
    )
    if normalized == "BPO":
        return (
            "BPO" in allowed
            and common
            and _cfg_truthy(cfg, "OUTREACH_BPO_SEND_ENABLED")
        )
    if normalized == "EC_SACRIFICE":
        target_companies = str(
            cfg.get(
                "OUTREACH_SACRIFICE_TARGET_COMPANIES",
                os.getenv("OUTREACH_SACRIFICE_TARGET_COMPANIES", ""),
            )
            or ""
        ).strip()
        return (
            "EC_SACRIFICE" in allowed
            and common
            and _cfg_truthy(cfg, "LEAD_FACTORY_ISOLATED_SACRIFICE_RUNTIME")
            and _cfg_truthy(cfg, "OUTREACH_SACRIFICE_SEND_ENABLED")
            and bool(target_companies)
        )
    return False


def is_outbound_lane(row: dict, cfg: dict[str, str]) -> bool:
    allowed = {
        item.strip().upper()
        for item in str(
            cfg.get(
                "OUTREACH_ALLOWED_LANES",
                cfg.get(
                    "OUTREACH_SACRIFICE_LANES",
                    "EC,RETAIL,SACRIFICE,EC_SACRIFICE",
                ),
            )
            or ""
        ).split(",")
        if item.strip()
    }
    lane = lane_from(row)
    source = str(row.get("source_type") or "").upper()

    # Protected lanes are evaluated first. A misleading source_type or a broad
    # allowlist must never turn a factory/BPO/SSOT row into an EC send.
    if lane in PROTECTED_FACTORY_LANES:
        return (
            lane in allowed
            and _cfg_truthy(cfg, _lane_flag(lane))
            and _cfg_truthy(cfg, "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL")
        )
    if lane in allowed:
        return True

    # Keep compatibility with older EC rows that carried only source_type.
    return (
        lane in {"", "EC", "RETAIL", "SACRIFICE", "EC_SACRIFICE"}
        and any(token in source for token in ("EC", "RETAIL", "SACRIFICE"))
    )


def is_sacrificial_lane(row: dict, cfg: dict[str, str]) -> bool:
    """Backward-compatible name for the shared lane eligibility check."""
    return is_outbound_lane(row, cfg)


def message_hash(row: dict) -> str:
    raw = "\n".join(str(row.get(k) or "") for k in ("recipient", "subject", "body"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prompt_freshness_preflight(draft: dict, drive, prompt_title: str = "") -> dict:
    """Read the live Prompt immediately before any external send."""
    if drive is None:
        return {
            "ok": False,
            "status": "PROMPT_LIVE_READ_UNAVAILABLE",
            "reason": "drive_repository_required_for_send_preflight",
        }
    title = str(prompt_title or os.getenv("OUTREACH_PROMPT_DOC_TITLE", "outreach_prompt_production_v1")).strip()
    _, metadata = drive.read_live_prompt_by_title(title)
    return prompt_freshness(
        draft.get("prompt_hash") or draft.get("prompt_version_hash"),
        metadata.get("prompt_hash"),
    )


def semantic_email_preflight(row: dict, cfg: dict[str, str]) -> dict:
    """Shared hard preflight for every outbound lane."""
    missing = [key for key in CRITICAL_FIELDS if not str(row.get(key) or "").strip()]
    errors = []
    recipient = str(row.get("recipient") or "").strip()
    body = str(row.get("body") or "")
    if recipient and ("@" not in recipient or " " in recipient):
        errors.append("RECIPIENT_INVALID")
    if not body.strip():
        errors.append("BODY_EMPTY")
    if "A-one A-one" in body or "A-one A-one" in str(row.get("recipient_name") or ""):
        errors.append("IDENTITY_MAPPING_CORRUPT")
    if not is_outbound_lane(row, cfg):
        errors.append("NON_SACRIFICIAL_LANE")
    return {
        "ok": not missing and not errors,
        "missing": missing,
        "critical_errors": errors,
        "message_hash": message_hash(row),
    }


def record_outbound_attempt(sheets, draft: dict, result: dict) -> dict:
    """Persist every non-successful outbound decision in the shared execution log."""
    if sheets is None:
        return {"written": False, "reason": "sheets_unavailable"}
    status = str(result.get("status") or "FAILED").strip() or "FAILED"
    if status == "SENT":
        return {"written": False, "reason": "success_is_logged_by_executor"}
    lane = lane_from(draft) or "UNKNOWN"
    preflight = result.get("preflight") or {}
    prompt_preflight = result.get("prompt_preflight") or result.get("prompt_check") or {}
    message_key = str(preflight.get("message_hash") or message_hash(draft)).strip()
    key = str(result.get("idempotency_key") or "").strip()
    if not key:
        key = f"outbound-attempt:{lane.lower()}:{draft.get('draft_id', '')}:{message_key}"
    recipient = str(result.get("recipient") or draft.get("recipient") or "").strip()
    critical = [str(value) for value in preflight.get("critical_errors", []) if value]
    reason = str(
        result.get("reason")
        or result.get("error_message")
        or prompt_preflight.get("reason")
        or ",".join(critical)
        or ",".join(str(value) for value in preflight.get("missing", []) if value)
        or status
    ).strip()
    record = {
        "idempotency_key": key,
        "draft_id": draft.get("draft_id", ""),
        "source_row": draft.get("source_row", ""),
        "company_name": draft.get("company_name", ""),
        "lane": lane,
        "channel": str(draft.get("channel") or "EMAIL").upper(),
        "status": status,
        "semantic_success": False,
        "message_id": result.get("message_id", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "form_url": draft.get("form_url", ""),
        "confirmation": reason,
        "recipient": recipient,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        existing = _sheet_rows_with_retry(sheets)
        if any(str(row.get("idempotency_key") or "") == key for row in existing):
            return {"written": False, "reason": "already_recorded", "idempotency_key": key}
    except Exception:
        # The write retry below still gives the control plane a durable record.
        pass
    last_error = None
    for attempt in range(4):
        try:
            sheets.append_dict("LeadFactory_ExecutionLog", record)
            return {"written": True, "idempotency_key": key}
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    return {
        "written": False,
        "idempotency_key": key,
        "error": f"{type(last_error).__name__}:{last_error}" if last_error else "append_failed",
    }


class OutboundEmailExecutor:
    """Shared email executor used by EC sacrifice and future SSOT lanes."""

    def __init__(
        self,
        sheets=None,
        drive=None,
        prompt_title: str = "",
        lane: str = "EC_SACRIFICE",
    ):
        self.sheets = sheets
        self.drive = drive
        self.prompt_title = str(prompt_title or "").strip()
        self.lane = lane_from({"lane": lane}) or "EC_SACRIFICE"
        self._sent_keys: set[str] = set()

    def _send_block_reason(self, draft: dict, cfg: dict[str, str]) -> str:
        if _list_only_hard_lock(cfg):
            return "list_only_hard_lock"
        lane = lane_from(draft) or self.lane
        if not _cfg_truthy(cfg, "LEAD_FACTORY_ALLOW_EXTERNAL_WRITE"):
            return "external_write_disabled"
        if str(
            cfg.get("LEAD_FACTORY_SEND_MODE", os.getenv("LEAD_FACTORY_SEND_MODE", "DISABLED"))
        ).strip().upper() != "ENABLED":
            return "send_mode_disabled"
        flag = _lane_flag(lane)
        if not _cfg_truthy(cfg, flag):
            return f"{flag.lower()}_disabled"
        allowed = {
            item.strip().upper()
            for item in str(cfg.get("OUTREACH_ALLOWED_LANES", "") or "").split(",")
            if item.strip()
        }
        if lane == "EC_SACRIFICE":
            if not _cfg_truthy(cfg, "LEAD_FACTORY_ISOLATED_SACRIFICE_RUNTIME"):
                return "isolated_sacrifice_runtime_required"
            target_companies = str(
                cfg.get(
                    "OUTREACH_SACRIFICE_TARGET_COMPANIES",
                    os.getenv("OUTREACH_SACRIFICE_TARGET_COMPANIES", ""),
                )
                or ""
            ).strip()
            if not target_companies:
                return "sacrifice_target_companies_required"
            if lane not in allowed:
                return "sacrifice_lane_not_allowed"
            if not _cfg_truthy(cfg, "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL"):
                return "explicit_sacrifice_send_approval_required"
            return ""
        if lane in PROTECTED_FACTORY_LANES and not _cfg_truthy(
            cfg, "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL"
        ):
            return "explicit_factory_send_approval_required"
        if lane == "BPO":
            if lane not in allowed:
                return "bpo_lane_not_allowed"
            return ""
        return "list_only_mode"


    def _send_enabled(self, draft: dict, cfg: dict[str, str]) -> bool:
        return not self._send_block_reason(draft, cfg)

    def execute(self, draft: dict, cfg: dict[str, str]) -> dict:
        lane = lane_from(draft) or self.lane
        block_reason = self._send_block_reason(draft, cfg)
        if block_reason:
            return {
                "status": "BLOCKED",
                "reason": block_reason,
                "lane": lane,
                "recipient": str(draft.get("recipient") or "").strip(),
            }

        preflight = semantic_email_preflight(draft, cfg)
        if not preflight["ok"]:
            return {"status": "BLOCKED_PREFLIGHT", "recipient": str(draft.get("recipient") or "").strip(), **preflight}

        prompt_check = prompt_freshness_preflight(draft, self.drive, self.prompt_title)
        if not prompt_check.get("ok"):
            return {"status": prompt_check.get("status", "STALE_PROMPT"), "prompt_preflight": prompt_check}

        key = f"outbound:{lane.lower()}:{draft.get('draft_id','')}:{preflight['message_hash']}"
        if key in self._sent_keys:
            return {"status": "DUPLICATE_BLOCKED", "idempotency_key": key, "lane": lane, "recipient": str(draft.get("recipient") or "").strip()}
        sheet_idempotency_error = ""
        try:
            existing = _sheet_rows_with_retry(self.sheets)
        except Exception as exc:
            # The Gmail recipient lookup below remains authoritative for this
            # isolated lane. Keep the attempt auditable, but do not make a
            # transient Sheet-read outage prevent a verified Gmail duplicate
            # check from running.
            sheet_idempotency_error = f"{type(exc).__name__}:{exc}"
            existing = []
        recipient = str(draft.get("recipient") or "").strip().casefold()
        for row in existing:
            row_recipient = str(row.get("recipient") or "").strip().casefold()
            row_status = str(row.get("status") or "").strip().upper()
            if recipient and row_recipient == recipient and row_status in {"SENT", "FORM_SENT"}:
                return {
                    "status": "DUPLICATE_BLOCKED",
                    "idempotency_key": key,
                    "lane": lane,
                    "recipient": str(draft.get("recipient") or "").strip(),
                    "existing_status": row_status,
                    "existing_message_id": row.get("message_id", ""),
                }
        if any(str(row.get("idempotency_key") or "") == key for row in existing):
            return {"status": "DUPLICATE_BLOCKED", "idempotency_key": key, "lane": lane, "recipient": str(draft.get("recipient") or "").strip(), "existing_status": "SHEET_RECORD"}

        sender = os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", "admin@a1-road.com").strip()
        creds, _ = default(scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"])
        if sender and hasattr(creds, "with_subject"):
            creds = creds.with_subject(sender)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        gmail_idempotency_error = ""
        try:
            existing_message_id = _find_existing_gmail_message_with_retry(
                service, sender=sender, recipient=str(draft["recipient"]).strip(), idempotency_key=key
            )
        except Exception as exc:
            gmail_idempotency_error = f"{type(exc).__name__}:{exc}"
            # If the Sheet was readable, the recipient-level Sheet check above
            # is the durable fallback. If both stores are unavailable, fail
            # closed rather than risking a duplicate.
            if sheet_idempotency_error:
                return {
                    "status": "IDEMPOTENCY_LOOKUP_FAILED",
                    "idempotency_key": key,
                    "lane": lane,
                    "recipient": str(draft.get("recipient") or "").strip(),
                    "reason": gmail_idempotency_error,
                }
            existing_message_id = ""
        if existing_message_id:
            return {
                "status": "DUPLICATE_BLOCKED",
                "idempotency_key": key,
                "lane": lane,
                "recipient": str(draft.get("recipient") or "").strip(),
                "existing_message_id": existing_message_id,
            }
        message = MIMEText(str(draft.get("body") or ""), "plain", "utf-8")
        message["to"] = str(draft["recipient"]).strip()
        message["from"] = sender
        message["subject"] = str(draft["subject"]).strip()
        message["X-Aone-Idempotency-Key"] = key
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        # Use the delegated mailbox explicitly. Some Workspace tenants reject
        # userId="me" for service-account delegated sends with a 400 precondition error.
        result = service.users().messages().send(userId=sender, body={"raw": raw}).execute()
        now = datetime.now(timezone.utc).isoformat()
        self._sent_keys.add(key)
        message_id = str(result.get("id") or "").strip()
        audit_log_error = ""
        if self.sheets:
            for attempt in range(4):
                try:
                    self.sheets.append_dict("LeadFactory_ExecutionLog", {
                        "idempotency_key": key,
                        "draft_id": draft.get("draft_id", ""),
                        "source_row": draft.get("source_row", ""),
                        "company_name": draft.get("company_name", ""),
                        "lane": lane_from(draft),
                        "channel": "EMAIL",
                        "status": "SENT",
                        "semantic_success": "PENDING_DELIVERY",
                        "message_id": message_id,
                        "recipient": draft.get("recipient", ""),
                        "executed_at": now,
                    })
                    break
                except Exception as exc:
                    audit_log_error = f"{type(exc).__name__}:{exc}"
                    if attempt < 3:
                        time.sleep(2 * (attempt + 1))
        response = {
            "status": "SENT",
            "message_id": message_id,
            "idempotency_key": key,
            "lane": lane,
            "recipient": str(draft.get("recipient") or "").strip(),
            "preflight": preflight,
            "sender": sender,
            "audit_log_written": not bool(audit_log_error),
            "sheet_idempotency_lookup_error": sheet_idempotency_error,
            "gmail_idempotency_lookup_error": gmail_idempotency_error,
        }
        if audit_log_error:
            response["audit_log_error"] = audit_log_error
        return response


class SacrificialEmailExecutor(OutboundEmailExecutor):
    """Compatibility wrapper; it uses the shared outbound executor."""

    def __init__(self, sheets=None, drive=None, prompt_title: str = "", lane: str = "EC_SACRIFICE"):
        super().__init__(sheets=sheets, drive=drive, prompt_title=prompt_title, lane=lane)
