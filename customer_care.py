"""Customer-first outreach contract. No model, transport, timer or paid service.

The existing ChatGPT workers supply observed evidence. These functions validate
it and build narrow SSOT updates. A review record is an auditable assessment,
not independent proof that a commercial claim is true.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

VERSION = "customer-first-v1"
SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SSOT_TAB = "営業リスト＿Factory/BPO"
LEDGER_TAB = "SalesOS_Action_Events"
SENDER = {"first_name": "Kazuma", "last_name": "Tamura", "name": "Kazuma Tamura",
          "company": "A-one road Co., Ltd.", "email": "admin@a1-road.com"}
TRACKING_HEADERS = (
    "AI_会社ID", "AI_最終試行日時", "AI_失敗工程", "AI_失敗理由", "AI_手動対応",
    "AI_品質確認JSON", "AI_送信予約ID", "AI_返信区分", "AI_次アクション",
    "AI_送信予定日時", "AI_タイムゾーン", "AI_状態更新日時", "AI_Campaign",
)
INITIAL = {"", "未接触", "判定中", "AI送信失敗", "AI送信結果不明"}
ACTIVE_SEND = {"SEND_RESERVED", "SENDING", "SEND_UNKNOWN"}
REVIEW_CHECKS = ("identity_correct", "recipient_relevant", "facts_supported",
                 "offer_authorized", "language_reviewed", "company_specific")


def text(value):
    return str(value or "").strip()


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", text(value)).casefold().split())


def domain(value):
    raw = text(value)
    try:
        p = urlparse(raw if "://" in raw else "https://" + raw)
        if p.username or p.password or p.scheme not in {"http", "https"}:
            return ""
        return (p.hostname or "").lower().removeprefix("www.").rstrip(".")
    except ValueError:
        return ""


def company_id(row):
    registered = text(row.get("AI_会社ID") or row.get("LF_lead_id"))
    if registered:
        return registered
    host = domain(row.get("website"))
    if not host or not text(row.get("company_name")):
        raise ValueError("COMPANY_IDENTITY_REQUIRED")
    return "company:" + hashlib.sha256(host.encode()).hexdigest()[:24]


def aware(value):
    dt = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return dt


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def packet_hash(packet):
    fields = ("company_id", "company_name", "website", "recipient", "subject", "body", "prompt_sha256")
    return hashlib.sha256(dumps({k: packet.get(k, "") for k in fields}).encode()).hexdigest()


def identity_field_key(marker, autocomplete=""):
    """Understand split person names before generic 'name' matching."""
    auto = text(autocomplete).lower().split()
    for token, key in (("given-name", "first_name"), ("family-name", "last_name"),
                       ("organization", "company"), ("name", "name")):
        if token in auto:
            return key
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", text(marker))
    words = re.sub(r"[_\[\].-]+", " ", words).lower()
    first = bool(re.search(r"\b(first\s*name|given\s*name|fname)\b|(?:^|\s)名(?:\s|$)", words))
    last = bool(re.search(r"\b(last\s*name|family\s*name|surname|lname)\b|(?:^|\s)姓(?:\s|$)", words))
    if first and last:
        return "ambiguous_person_name"
    return "first_name" if first else "last_name" if last else ""


def validate_identity_fields(fields):
    """Check actual values, including optional fields, before any submit/Next."""
    for field in fields:
        key = field.get("key")
        if key == "ambiguous_person_name":
            raise ValueError("FORM_PERSON_NAME_AMBIGUOUS")
        if key in SENDER and norm(field.get("final_value")) != norm(SENDER[key]):
            raise ValueError("FORM_IDENTITY_VALUE_MISMATCH:" + key)


def validate_customer_text(subject, body):
    value = str(subject or "") + "\n" + str(body or "")
    if not text(subject) or not text(body):
        raise ValueError("EMPTY_CUSTOMER_MESSAGE")
    if "\n" in str(subject) or "\r" in str(subject):
        raise ValueError("SUBJECT_HEADER_INJECTION")
    if re.search(r"\bA[- ]?1\s+A[- ]?1\b|\bA[- ]?one\s+A[- ]?one\b", value, re.I):
        raise ValueError("SENDER_IDENTITY_CORRUPT")
    if re.search(r"\{\{.*?\}\}|\[(?:first.?name|last.?name|company(?:.?name)?|insert[^\]]*)\]|<company>|\ufffd", value, re.I):
        raise ValueError("UNRESOLVED_TEMPLATE_OR_ENCODING")
    if re.search(r"as an ai|language model|here is (?:the|your) (?:email|draft)", value, re.I):
        raise ValueError("INTERNAL_GENERATION_TEXT")


def quality_check(row, packet, current_prompt_sha256):
    """A byte-bound recipient-specific review; editing invalidates approval."""
    if packet.get("company_id") != company_id(row):
        raise ValueError("QUALITY_COMPANY_ID_MISMATCH")
    if norm(packet.get("company_name")) != norm(row.get("company_name")):
        raise ValueError("QUALITY_COMPANY_NAME_MISMATCH")
    if not domain(row.get("website")) or domain(packet.get("website")) != domain(row["website"]):
        raise ValueError("QUALITY_COMPANY_DOMAIN_MISMATCH")
    recipient = text(packet.get("recipient"))
    if parseaddr(recipient)[1] != recipient or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", recipient):
        raise ValueError("QUALITY_RECIPIENT_REQUIRED")
    validate_customer_text(packet.get("subject"), packet.get("body"))
    if not current_prompt_sha256 or packet.get("prompt_sha256") != current_prompt_sha256:
        raise ValueError("QUALITY_PROMPT_CHANGED")
    review = packet.get("review") or {}
    if review.get("packet_sha256") != packet_hash(packet):
        raise ValueError("QUALITY_REVIEW_BYTES_CHANGED")
    if not text(review.get("reviewer_run_id")) or not text(review.get("reason")):
        raise ValueError("QUALITY_REVIEW_PROVENANCE_REQUIRED")
    aware(review.get("reviewed_at"))
    if any(review.get(k) is not True for k in REVIEW_CHECKS):
        raise ValueError("QUALITY_REVIEW_INCOMPLETE")
    recipient_evidence = packet.get("recipient_evidence") or {}
    if recipient_evidence.get("email") != recipient or not recipient_evidence.get("source_url") or not recipient_evidence.get("source_excerpt"):
        raise ValueError("QUALITY_RECIPIENT_EVIDENCE_REQUIRED")
    if not packet.get("evidence"):
        raise ValueError("QUALITY_SOURCE_EVIDENCE_REQUIRED")
    for source in packet["evidence"]:
        quote = text(source.get("quote"))
        if not domain(source.get("url")) or not quote or quote not in str(source.get("source_text") or "") or not text(source.get("relevance")):
            raise ValueError("QUALITY_SOURCE_QUOTE_OR_RELEVANCE_MISSING")
    if not text(packet.get("buyer_workflow")) or not text(packet.get("offer_authority")):
        raise ValueError("QUALITY_BUYER_AND_OFFER_REQUIRED")
    return packet_hash(packet)


def history(row):
    raw = row.get("Sales_History_JSON") or "[]"
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, list) or any(not isinstance(e, dict) for e in parsed):
        raise ValueError("CORRUPT_HISTORY_PRESERVED")
    return parsed


def transition(row, event):
    """Pure projection. Failures preserve drafts and advanced human sales stages."""
    if event.get("company_id") != company_id(row) or norm(event.get("company_name")) != norm(row.get("company_name")) or domain(event.get("website")) != domain(row.get("website")):
        raise ValueError("EVENT_COMPANY_IDENTITY_MISMATCH")
    if not text(event.get("event_id")) or not text(event.get("run_id")):
        raise ValueError("EVENT_PROVENANCE_REQUIRED")
    at = aware(event.get("occurred_at"))
    events = history(row)
    prior = next((e for e in events if e.get("event_id") == event["event_id"]), None)
    if prior:
        expected = {**event, "status": event.get("kind"), "event_type": "OUTBOUND_SENT" if event.get("kind") in {"SENT", "MANUAL_SENT"} else event.get("kind")}
        if prior != expected:
            raise ValueError("EVENT_ID_COLLISION")
        return {}
    stored_event = dict(event)
    stored_event["status"] = event.get("kind")
    stored_event["event_type"] = "OUTBOUND_SENT" if event.get("kind") in {"SENT", "MANUAL_SENT"} else event.get("kind")
    merged = dumps(events + [stored_event])
    if len(merged) > 45000:
        raise ValueError("HISTORY_ARCHIVE_REQUIRED")
    changes = {"AI_会社ID": company_id(row), "Sales_History_JSON": merged}
    if row.get("AI_状態更新日時") and at < aware(row["AI_状態更新日時"]):
        return changes
    kind = event.get("kind")
    state = text(row.get("営業メール状態"))
    initial = text(row.get("Status")) in INITIAL
    changes["AI_状態更新日時"] = at.isoformat()
    if kind == "DRAFT_SAVED":
        if state in ACTIVE_SEND or row.get("AI_手動対応") == "対応中" or not initial:
            raise ValueError("DRAFT_CANNOT_REPLACE_ACTIVE_OR_CONTACTED_WORK")
        packet = event["packet"]
        quality_check(row, packet, event.get("current_prompt_sha256"))
        changes.update({"営業メール宛先": packet["recipient"], "営業メール件名": packet["subject"],
            "営業メール本文": packet["body"], "営業メール根拠": dumps(packet["evidence"]),
            "営業メール生成日時": event["occurred_at"], "営業メール状態": "DRAFT_READY",
            "AI_品質確認JSON": dumps(packet), "AI_失敗工程": "", "AI_失敗理由": "",
            "AI_次アクション": "送信前確認・地域別送信枠へ"})
    elif kind in {"SEND_READY", "SEND_RESERVED"}:
        if not initial or state in ACTIVE_SEND or row.get("AI_手動対応") == "対応中":
            raise ValueError("SEND_OWNERSHIP_OR_SALES_STAGE_BLOCKED")
        if text(row.get("営業メール送信可否")) != "許可" or not row.get("AI_Campaign"):
            raise ValueError("CAMPAIGN_PERMISSION_REQUIRED")
        packet = json.loads(row.get("AI_品質確認JSON") or "{}")
        quality_check(row, packet, event.get("current_prompt_sha256"))
        if any(packet[k] != row.get(v) for k, v in (("recipient", "営業メール宛先"), ("subject", "営業メール件名"), ("body", "営業メール本文"))):
            raise ValueError("SAVED_COPY_CHANGED_AFTER_REVIEW")
        proof = event.get("preflight") or {}
        if any(proof.get(k) is not True for k in ("gmail_history_checked", "no_prior_contact", "no_reply_or_optout", "sender_verified", "recipient_verified")):
            raise ValueError("FRESH_SEND_PREFLIGHT_REQUIRED")
        checked = aware(proof.get("checked_at"))
        if not 0 <= (at - checked).total_seconds() <= 300:
            raise ValueError("SEND_PREFLIGHT_STALE")
        changes["営業メール状態"] = kind
        changes["AI_次アクション"] = "地域別送信枠待ち" if kind == "SEND_READY" else "同じ予約IDで送信・受理確認"
        if kind == "SEND_RESERVED":
            if not text(event.get("claim_id")):
                raise ValueError("CLAIM_ID_REQUIRED")
            changes.update({"AI_送信予約ID": event["claim_id"], "AI_最終試行日時": at.isoformat()})
    elif kind in {"SENT", "MANUAL_SENT"}:
        receipt = event.get("receipt") or {}
        if not receipt.get("message_id") or not receipt.get("thread_id") or "SENT" not in receipt.get("label_ids", []) or receipt.get("sender") != SENDER["email"]:
            raise ValueError("GMAIL_SENT_RECEIPT_REQUIRED")
        if receipt.get("recipient") != row.get("営業メール宛先"):
            raise ValueError("RECEIPT_RECIPIENT_MISMATCH")
        if receipt.get("subject") != row.get("営業メール件名") or receipt.get("body_sha256") != hashlib.sha256(str(row.get("営業メール本文") or "").encode()).hexdigest():
            raise ValueError("RECEIPT_MESSAGE_BYTES_MISMATCH")
        if kind == "SENT" and (not row.get("AI_送信予約ID") or event.get("claim_id") != row["AI_送信予約ID"]):
            raise ValueError("RECEIPT_CLAIM_MISMATCH")
        changes.update({"営業メール状態": "SENT", "Last_Outbound_At": at.isoformat(),
            "Last_Outbound_Message_ID": receipt["message_id"], "Last_Outbound_Thread_ID": receipt["thread_id"],
            "Last_Outbound_Recipient": receipt["recipient"], "AI_次アクション": "返信待ち",
            "AI_失敗工程": "", "AI_失敗理由": "", "AI_手動対応": "完了" if kind == "MANUAL_SENT" else ""})
        if not row.get("First_Contacted_At"):
            changes["First_Contacted_At"] = at.isoformat()
        if initial:
            changes["Status"] = "AI送信済み" if kind == "SENT" else "送付済み"
    elif kind in {"SEND_FAILED", "SEND_UNKNOWN", "QUALITY_HOLD"}:
        if not text(event.get("stage")) or not text(event.get("reason")):
            raise ValueError("FAILURE_STAGE_AND_REASON_REQUIRED")
        if kind == "SEND_FAILED" and event.get("definitely_not_sent") is not True:
            kind = "SEND_UNKNOWN"
        changes.update({"営業メール状態": kind, "AI_失敗工程": event["stage"],
            "AI_失敗理由": event["reason"], "AI_最終試行日時": at.isoformat(),
            "AI_次アクション": "Gmail照合まで再送保留" if kind == "SEND_UNKNOWN" else "不足箇所を確認して手動対応"})
        if initial and kind != "QUALITY_HOLD":
            changes["Status"] = "AI送信結果不明" if kind == "SEND_UNKNOWN" else "AI送信失敗"
    elif kind == "MANUAL_TAKEOVER":
        if state in ACTIVE_SEND:
            raise ValueError("RECONCILE_ACTIVE_SEND_BEFORE_MANUAL_TAKEOVER")
        changes.update({"AI_手動対応": "対応中", "AI_次アクション": "人間が対応中・AI送信停止"})
    elif kind == "REPLIED":
        if event.get("human_reply") is not True or not event.get("message_id") or not text(event.get("reply_class")):
            raise ValueError("HUMAN_REPLY_EVIDENCE_REQUIRED")
        changes.update({"Last_Inbound": at.isoformat(), "Gmail_Thread_ID": event.get("thread_id", row.get("Gmail_Thread_ID", "")),
            "AI_返信区分": event["reply_class"], "AI_次アクション": "返信原文を確認して商談化",
            "営業メール状態": "REPLIED"})
        if initial or row.get("Status") in {"送付済み", "AI送信済み", "リマイン1", "リマイン2", "リマイン3", "DM済"}:
            changes["Status"] = "返信あり"
    elif kind == "BOUNCED":
        if not event.get("message_id") or not event.get("original_message_id") or not text(event.get("reason")):
            raise ValueError("BOUNCE_EVIDENCE_REQUIRED")
        changes.update({"営業メール状態": "BOUNCED", "AI_失敗工程": "DELIVERY", "AI_失敗理由": event["reason"],
            "AI_次アクション": "当該宛先への再送停止・正しい窓口を確認"})
    elif kind in {"MEETING_BOOKED", "MEETING_CANCELLED", "MEETING_HELD"}:
        if not event.get("calendar_event_id"):
            raise ValueError("CALENDAR_EVIDENCE_REQUIRED")
        if kind == "MEETING_HELD" and not event.get("held_evidence"):
            raise ValueError("MEETING_HELD_EVIDENCE_REQUIRED")
        if kind == "MEETING_BOOKED":
            aware(event.get("meeting_at"))
            changes.update({"Next_Meeting": event["meeting_at"], "AI_次アクション": "商談準備・案件へのひも付け確認"})
            if initial or row.get("Status") in {"AI送信済み", "送付済み", "返信あり"}:
                changes["Status"] = "商談化"
        elif kind == "MEETING_CANCELLED":
            if row.get("Next_Meeting") == event.get("meeting_at"):
                changes["Next_Meeting"] = ""
            changes["AI_次アクション"] = "商談取消・再調整確認"
        else:
            held_ids = {e.get("calendar_event_id") for e in events if e.get("kind") == "MEETING_HELD"}
            held_ids.add(event["calendar_event_id"])
            changes["Meeting_Count"] = max(int(row.get("Meeting_Count") or 0), len(held_ids))
            changes["AI_次アクション"] = "議事録・次の約束・有償提案を確認"
    else:
        raise ValueError("UNSUPPORTED_CUSTOMER_EVENT")
    return changes


def in_send_window(now, timezone_name, *, start_hour=8, end_hour=11):
    if now.tzinfo is None or not 0 <= start_hour < end_hour <= 24:
        raise ValueError("INVALID_SEND_WINDOW")
    if not timezone_name:
        return False
    local = now.astimezone(ZoneInfo(timezone_name))
    return local.weekday() < 5 and start_hour <= local.hour < end_hour


def summarize(rows):
    """Current inventory is disjoint; cumulative milestones are separate."""
    states, seen = Counter(), set()
    for row in rows:
        key = company_id(row)
        if key in seen:
            raise ValueError("DUPLICATE_COMPANY_IN_COUNT")
        seen.add(key)
        states[text(row.get("営業メール状態")) or "NOT_STARTED"] += 1
    return {"companies": len(seen), "current_states": dict(states)}
