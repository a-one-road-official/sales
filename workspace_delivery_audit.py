"""Workspace Gmail delivery-audit collector.

Uses Google Workspace Admin Reports Gmail delivery events to determine what
happened *after* Gmail accepted a message.  No LLM/model calls and no sends.

For outbound Gmail messages, action_type values from Google's Gmail log schema:
  2  = Gmail accepted / prepared for delivery
  3  = Gmail handled the message
 10  = outbound SMTP server sent the message
 14  = temporary delivery error / retry scheduled
 18  = delivery failed / bounced
 19  = message dropped by Gmail

A 2xx smtp_reply_code on action_type=10 is positive remote-SMTP acceptance.
4xx is deferred. 5xx/action_type=18 is permanent failure.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from google.auth import default
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

REPORT_SCOPE = "https://www.googleapis.com/auth/admin.reports.audit.readonly"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_ADMIN = "admin@a1-road.com"

REASON_TEXT = {
    1: "default",
    3: "malware",
    4: "dmarc_policy",
    5: "unsupported_attachment",
    6: "receive_limit_exceeded",
    7: "account_over_quota",
    8: "bad_ptr",
    9: "recipient_does_not_exist",
    10: "customer_policy",
    12: "rfc_violation",
    13: "blatant_spam",
    14: "denial_of_service",
    15: "malicious_or_spam_links",
    16: "low_ip_reputation",
    17: "low_domain_reputation",
    18: "public_rbl",
    19: "dos_temporary_reject",
    20: "dos_permanent_reject",
}


def text(value):
    return str(value or "").strip()


def stamp(value):
    dt = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone_required")
    return dt.astimezone(timezone.utc)


def normalize_message_id(value):
    return text(value).strip("<>").casefold()


def delegated_credentials(subject, scopes):
    """Create short-lived delegated Workspace credentials using the existing SA."""
    subject = text(subject)
    scopes = [text(s) for s in scopes if text(s)]
    if not subject or not scopes:
        raise RuntimeError("delegation_subject_and_scopes_required")

    creds, _ = default(scopes=scopes)
    if hasattr(creds, "with_subject"):
        return creds.with_subject(subject)

    candidates = (
        text(os.getenv("LEAD_FACTORY_GMAIL_SIGNING_SERVICE_ACCOUNT")),
        text(os.getenv("LEAD_FACTORY_TASKS_SERVICE_ACCOUNT")),
        text(getattr(creds, "service_account_email", "")),
    )
    signer = next((c for c in candidates if c and c.casefold() != "default"), "")
    if not signer:
        raise RuntimeError("workspace_delegation_service_account_missing")

    base, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    now = int(time.time())
    payload = {
        "iss": signer,
        "sub": subject,
        "scope": " ".join(scopes),
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    iam = build("iamcredentials", "v1", credentials=base, cache_discovery=False)
    signed = text(iam.projects().serviceAccounts().signJwt(
        name=f"projects/-/serviceAccounts/{signer}",
        body={"payload": json.dumps(payload)},
    ).execute().get("signedJwt"))
    if not signed:
        raise RuntimeError("workspace_delegation_signed_jwt_empty")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed,
        }).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        token_payload = json.loads(response.read().decode("utf-8"))
    token = text(token_payload.get("access_token"))
    if not token:
        raise RuntimeError("workspace_delegation_access_token_empty")
    return Credentials(token=token)


def gmail_rfc2822_ids(gmail_ids, *, admin=DEFAULT_ADMIN):
    """Resolve Gmail internal hex IDs to RFC 2822 Message-ID headers."""
    if not gmail_ids:
        return {}
    creds = delegated_credentials(admin, [GMAIL_READ_SCOPE])
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    out = {}
    for gmail_id in dict.fromkeys(text(x) for x in gmail_ids if text(x)):
        msg = gmail.users().messages().get(
            userId="me",
            id=gmail_id,
            format="metadata",
            metadataHeaders=["Message-ID", "To", "From", "Subject"],
        ).execute()
        headers = {
            text(h.get("name")).casefold(): text(h.get("value"))
            for h in (msg.get("payload", {}).get("headers") or [])
        }
        out[gmail_id] = {
            "gmail_message_id": gmail_id,
            "thread_id": text(msg.get("threadId")),
            "rfc2822_message_id": headers.get("message-id", ""),
            "to": headers.get("to", ""),
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
        }
    return out


def _parameter_value(param):
    if "value" in param:
        return param.get("value")
    if "intValue" in param:
        try:
            return int(param.get("intValue"))
        except (TypeError, ValueError):
            return param.get("intValue")
    if "boolValue" in param:
        return bool(param.get("boolValue"))
    if "multiValue" in param:
        return list(param.get("multiValue") or [])
    if "multiIntValue" in param:
        return [int(x) for x in (param.get("multiIntValue") or [])]
    return None


def _flatten_params(parameters, prefix="", out=None):
    """Flatten Reports API nested parameter/messageValue structures."""
    out = out if out is not None else {}
    for param in parameters or []:
        name = text(param.get("name"))
        key = f"{prefix}.{name}" if prefix and name else (name or prefix)
        scalar = _parameter_value(param)
        if scalar is not None and key:
            out[key] = scalar
        nested = param.get("messageValue") or {}
        if nested.get("parameter"):
            _flatten_params(nested["parameter"], key, out)
        for item in param.get("multiMessageValue") or []:
            child = {}
            _flatten_params(item.get("parameter") or [], key, child)
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(child)
    return out


def flatten_activity(activity):
    rows = []
    activity_time = text((activity.get("id") or {}).get("time"))
    resource_ids = list(activity.get("resourceIds") or [])
    for event in activity.get("events") or []:
        if text(event.get("name")) != "delivery":
            continue
        flat = _flatten_params(event.get("parameters") or [])
        flat["_activity_time"] = activity_time
        flat["_resource_ids"] = resource_ids
        flat["_unique_qualifier"] = text((activity.get("id") or {}).get("uniqueQualifier"))
        rows.append(flat)
    return rows


def list_gmail_delivery_events(start, end, *, admin=DEFAULT_ADMIN):
    """Read all Gmail delivery audit events in [start,end]."""
    start_dt, end_dt = stamp(start), stamp(end)
    if not start_dt < end_dt:
        raise ValueError("start_before_end_required")
    if end_dt - start_dt > timedelta(days=30):
        raise ValueError("gmail_audit_window_max_30_days")

    creds = delegated_credentials(admin, [REPORT_SCOPE])
    svc = build("admin", "reports_v1", credentials=creds, cache_discovery=False)
    page = None
    rows = []
    while True:
        req = svc.activities().list(
            userKey="all",
            applicationName="gmail",
            eventName="delivery",
            startTime=start_dt.isoformat().replace("+00:00", "Z"),
            endTime=end_dt.isoformat().replace("+00:00", "Z"),
            maxResults=1000,
            pageToken=page,
        )
        payload = req.execute()
        for activity in payload.get("items") or []:
            rows.extend(flatten_activity(activity))
        page = text(payload.get("nextPageToken"))
        if not page:
            break
    return rows


def _lookup(flat, suffix, default=None):
    if suffix in flat:
        return flat[suffix]
    matches = [v for k, v in flat.items() if k.endswith("." + suffix)]
    return matches[-1] if matches else default


def observation_from_event(flat):
    action_type = int(_lookup(flat, "action_type", 0) or 0)
    mail_event_type = int(_lookup(flat, "mail_event_type", 0) or 0)
    success = bool(_lookup(flat, "success", False))
    smtp_code = int(_lookup(flat, "smtp_reply_code", 0) or 0)
    reason_code = int(_lookup(flat, "smtp_response_reason", 0) or 0)
    description = text(_lookup(flat, "description", ""))
    remote_host = text(_lookup(flat, "smtp_out_remote_host", ""))
    remote_ip = text(_lookup(flat, "smtp_out_connect_ip", ""))
    rfc_id = normalize_message_id(_lookup(flat, "rfc2822_message_id", ""))
    subject = text(_lookup(flat, "subject", ""))
    recipients = _lookup(flat, "destination", []) or []
    recipient_addresses = []
    if isinstance(recipients, list):
        for item in recipients:
            if isinstance(item, dict):
                address = text(item.get("message_info.destination.address") or item.get("destination.address") or item.get("address"))
                if address:
                    recipient_addresses.append(address)

    provider_status = ""
    if action_type == 10 and 200 <= smtp_code < 300 and success:
        provider_status = "DELIVERED"
    elif action_type == 14 or 400 <= smtp_code < 500:
        provider_status = "DEFERRED"
    elif action_type == 18 or mail_event_type == 30:
        provider_status = "BOUNCED"
    elif action_type == 19:
        provider_status = "REJECTED"
    elif 500 <= smtp_code < 600:
        provider_status = "REJECTED" if smtp_code == 550 and reason_code in {10, 13, 15, 16, 17, 18, 20} else "BOUNCED"
    elif action_type in {2, 3} or mail_event_type in {1, 35}:
        provider_status = "GMAIL_ACCEPTED"

    diagnostic_parts = [
        f"action_type={action_type}" if action_type else "",
        f"mail_event_type={mail_event_type}" if mail_event_type else "",
        f"smtp_reply_code={smtp_code}" if smtp_code else "",
        f"smtp_reason={REASON_TEXT.get(reason_code, reason_code)}" if reason_code else "",
        f"remote_host={remote_host}" if remote_host else "",
        description,
    ]
    return {
        "source": "WORKSPACE_EMAIL_LOG",
        "verified": bool(rfc_id),
        "provider_status": provider_status,
        "smtp_code": str(smtp_code) if smtp_code else "",
        "smtp_response_reason": reason_code,
        "diagnostic": " | ".join(x for x in diagnostic_parts if x),
        "observed_at": text(flat.get("_activity_time")),
        "provider_event_id": text(flat.get("_unique_qualifier")),
        "rfc2822_message_id": rfc_id,
        "subject": subject,
        "remote_host": remote_host,
        "remote_ip": remote_ip,
        "recipient_addresses": recipient_addresses,
        "action_type": action_type,
        "mail_event_type": mail_event_type,
        "success": success,
    }


def reconcile_outbounds(outbounds, *, now, admin=DEFAULT_ADMIN):
    """Resolve Workspace delivery observations for Gmail-accepted messages.

    The preferred key is RFC 2822 Message-ID when available.  For legacy rows that
    only stored Gmail's internal hex ID, use exact subject + exact recipient within
    the narrow audit window.  That fallback lets us reconcile today's existing
    73 sends without requiring Gmail domain-wide delegation.
    """
    outbounds = [dict(x) for x in (outbounds or [])]
    if not outbounds:
        return []

    starts = [stamp(x["accepted_at"]) for x in outbounds if x.get("accepted_at")]
    start = min(starts) - timedelta(minutes=10)
    end = stamp(now) + timedelta(minutes=1)
    events = list_gmail_delivery_events(start, end, admin=admin)

    observations = [observation_from_event(flat) for flat in events]
    by_rfc = {}
    by_subject_recipient = {}
    for obs in observations:
        rfc_id = normalize_message_id(obs.get("rfc2822_message_id"))
        if rfc_id:
            by_rfc.setdefault(rfc_id, []).append(obs)
        subject = text(obs.get("subject")).casefold()
        for recipient in obs.get("recipient_addresses") or []:
            by_subject_recipient.setdefault((subject, text(recipient).casefold()), []).append(obs)

    rank = {"DELIVERED": 5, "REJECTED": 5, "BOUNCED": 5, "DEFERRED": 4,
            "GMAIL_ACCEPTED": 2, "": 0}
    results = []
    for outbound in outbounds:
        rfc_id = normalize_message_id(outbound.get("rfc2822_message_id"))
        candidates = list(by_rfc.get(rfc_id, [])) if rfc_id else []
        if not candidates:
            key = (text(outbound.get("subject")).casefold(), text(outbound.get("recipient")).casefold())
            candidates = list(by_subject_recipient.get(key, []))
        candidates = sorted(
            candidates,
            key=lambda o: (rank.get(o.get("provider_status"), 0), text(o.get("observed_at"))),
            reverse=True,
        )
        observation = dict(candidates[0]) if candidates else {
            "source": "WORKSPACE_EMAIL_LOG",
            "verified": False,
            "provider_status": "",
            "observed_at": stamp(now).isoformat(),
            "rfc2822_message_id": rfc_id,
            "search_complete": True,
            "diagnostic": "no_matching_delivery_event_yet",
        }
        observation["message_id"] = text(outbound.get("message_id"))
        observation["thread_id"] = text(outbound.get("thread_id"))
        observation["rfc2822_message_id"] = rfc_id or text(observation.get("rfc2822_message_id"))
        observation["gmail_subject"] = text(outbound.get("subject"))
        # Exact subject+recipient is acceptable as legacy matching evidence only
        # when the provider event itself carries that exact pair.
        if candidates and not observation.get("verified"):
            observation["verified"] = True
            observation["match_method"] = "EXACT_SUBJECT_RECIPIENT"
        elif candidates:
            observation["match_method"] = "RFC2822_MESSAGE_ID" if rfc_id else "EXACT_SUBJECT_RECIPIENT"
        results.append(observation)
    return results


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: python workspace_delivery_audit.py INPUT.json OUTPUT.json")
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = reconcile_outbounds(
        request.get("outbounds") or [],
        now=request["now"],
        admin=request.get("admin", DEFAULT_ADMIN),
    )
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"observations": len(result), "customer_sends": 0, "model_calls": 0}))


if __name__ == "__main__":
    main()
