"""Deterministic outbound delivery reconciliation.

No LLM/model calls, no customer sends, no timers.  The caller supplies Gmail/Workspace
observations and this module classifies each Gmail-accepted message into a delivery
state, emits SSOT-compatible events, and decides whether the next canary may run.

The central invariant is:
    terminal outcomes + unresolved outcomes == attempted Gmail-accepted messages
and a new outbound cohort cannot start while the previous cohort is unresolved.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

TERMINAL = {"DELIVERED", "BOUNCED", "REJECTED", "UNKNOWN_LOG_GAP"}
FAILURE_TERMINAL = {"BOUNCED", "REJECTED", "UNKNOWN_LOG_GAP"}
TRANSIENT = {"DELIVERY_PENDING", "DEFERRED"}
CANARY_STEPS = (5, 10, 20, 35, 70)
SECURITY_MARKERS = (
    "security policy", "spamcop", "mailspike", "blocklist", "blacklist",
    "reputation", "policy rejection", "policy reject", "spam policy",
)


def text(value):
    return str(value or "").strip()


def stamp(value):
    dt = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone_required")
    return dt.astimezone(timezone.utc)


def _event_id(message_id, state, observed_at):
    material = f"{message_id}|{state}|{stamp(observed_at).isoformat()}"
    return "delivery:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def smtp_code(value):
    raw = text(value)
    m = re.search(r"\b([245]\.[0-9]\.[0-9])\b", raw)
    if m:
        return m.group(1)
    m = re.search(r"\b([245][0-9][0-9])\b", raw)
    return m.group(1) if m else ""


def security_reputation_reject(observation):
    diag = " ".join(
        text(observation.get(k)) for k in ("diagnostic", "body", "provider_detail", "smtp_response")
    ).casefold()
    code = smtp_code(text(observation.get("smtp_code")) + " " + diag)
    return code.startswith("5.7") or any(marker in diag for marker in SECURITY_MARKERS)


def classify(observation, *, accepted_at=None, now=None):
    """Classify one recipient/message observation without inference.

    Supported evidence sources:
      WORKSPACE_EMAIL_LOG / PROVIDER_EVENT
      GMAIL_DSN
      RECIPIENT_AUTO_ACK / RECIPIENT_HUMAN_REPLY

    Missing evidence remains DELIVERY_PENDING.  UNKNOWN_LOG_GAP is only produced
    after a complete reconciliation search and at least 24 hours since acceptance.
    """
    obs = dict(observation or {})
    source = text(obs.get("source")).upper()
    provider = text(obs.get("provider_status")).upper()
    diag = " ".join(text(obs.get(k)) for k in ("smtp_code", "enhanced_status", "smtp_response", "diagnostic", "body"))
    code = smtp_code(diag)

    if source in {"RECIPIENT_AUTO_ACK", "RECIPIENT_HUMAN_REPLY"} and obs.get("verified") is True:
        return "DELIVERED"
    if source in {"WORKSPACE_EMAIL_LOG", "PROVIDER_EVENT"} and provider in {"DELIVERED", "ACCEPTED"} and obs.get("verified") is True:
        return "DELIVERED"

    if source in {"WORKSPACE_EMAIL_LOG", "GMAIL_DSN", "PROVIDER_EVENT"} and obs.get("verified") is True:
        if provider in {"REJECTED", "DROPPED"} or security_reputation_reject(obs):
            return "REJECTED"
        if provider == "BOUNCED" or code.startswith("5"):
            return "BOUNCED"
        if provider in {"DEFERRED", "TEMPORARY_FAILURE", "IN_PROGRESS"} or code.startswith("4"):
            return "DEFERRED"

    if obs.get("search_complete") is True and accepted_at and now:
        if stamp(now) - stamp(accepted_at) >= timedelta(hours=24):
            return "UNKNOWN_LOG_GAP"
    return "DELIVERY_PENDING"


def event_for(outbound, observation, now):
    """Build a ssot_terminal delivery event for one Gmail-accepted message."""
    outbound = dict(outbound or {})
    obs = dict(observation or {})
    message_id = text(outbound.get("message_id"))
    thread_id = text(outbound.get("thread_id"))
    recipient = text(outbound.get("recipient")).lower()
    if not message_id or not thread_id or not recipient or not outbound.get("accepted_at"):
        raise ValueError("complete_gmail_acceptance_required")

    observed_at = text(obs.get("observed_at") or now)
    state = classify(obs, accepted_at=outbound["accepted_at"], now=now)
    evidence = {
        "source": text(obs.get("source")).upper(),
        "verified": obs.get("verified") is True,
        "observed_at": stamp(observed_at).isoformat(),
        "provider_status": text(obs.get("provider_status")).upper(),
        "smtp_code": smtp_code(" ".join(text(obs.get(k)) for k in ("smtp_code", "enhanced_status", "smtp_response", "diagnostic", "body"))),
        "diagnostic": text(obs.get("diagnostic") or obs.get("smtp_response") or obs.get("body")),
        "thread_id": text(obs.get("thread_id") or thread_id),
        "provider_event_id": text(obs.get("provider_event_id")),
        "search_complete": obs.get("search_complete") is True,
    }
    if state == "DELIVERY_PENDING":
        return {
            "state": state,
            "message_id": message_id,
            "terminal": False,
            "event": None,
        }

    event = {
        "kind": state,
        "event_id": _event_id(message_id, state, observed_at),
        "occurred_at": stamp(observed_at).isoformat(),
        "outbound_message_id": message_id,
        "message_id": message_id,
        "thread_id": thread_id,
        "recipient": recipient,
        "delivery_evidence": evidence,
        "reason": evidence["diagnostic"] or state,
    }
    return {
        "state": state,
        "message_id": message_id,
        "terminal": state in TERMINAL,
        "event": event,
    }


def batch_health(outbounds, observations, *, target=70, now):
    """Close a cohort only when every attempted message has an explicit outcome."""
    by_message = {text(o.get("message_id")): dict(o) for o in (observations or []) if text(o.get("message_id"))}
    results = []
    for outbound in outbounds or []:
        mid = text(outbound.get("message_id"))
        results.append(event_for(outbound, by_message.get(mid, {}), now))
    counts = Counter(r["state"] for r in results)
    attempted = len(results)
    terminal_count = sum(counts[s] for s in TERMINAL)
    unresolved = attempted - terminal_count

    global_rejects = []
    for outbound in outbounds or []:
        mid = text(outbound.get("message_id"))
        obs = by_message.get(mid, {})
        if classify(obs, accepted_at=outbound.get("accepted_at"), now=now) == "REJECTED" and security_reputation_reject(obs):
            global_rejects.append(mid)

    hard_failures = counts["BOUNCED"] + counts["REJECTED"]
    hard_failure_rate = (hard_failures / attempted) if attempted else 0.0
    breaker_reasons = []
    if global_rejects:
        breaker_reasons.append("security_or_reputation_reject")
    if attempted >= 20 and hard_failure_rate > 0.05:
        breaker_reasons.append("hard_failure_rate_above_5pct")

    cohort_closed = attempted > 0 and unresolved == 0
    breaker = bool(breaker_reasons)
    next_size = 0
    if cohort_closed and not breaker:
        for step in CANARY_STEPS:
            if step > attempted:
                next_size = min(step, int(target))
                break
        if attempted >= target:
            next_size = 0

    return {
        "attempted": attempted,
        "target": int(target),
        "counts": dict(counts),
        "terminal_count": terminal_count,
        "unresolved": unresolved,
        "cohort_closed": cohort_closed,
        "circuit_breaker": breaker,
        "circuit_breaker_reasons": breaker_reasons,
        "hard_failure_rate": hard_failure_rate,
        "next_canary_size": next_size,
        "can_start_next_cohort": cohort_closed and not breaker and attempted < int(target),
        "results": results,
        "invariant_ok": terminal_count + unresolved == attempted,
    }


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: python delivery_controller.py INPUT.json OUTPUT.json")
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    op = request.get("operation")
    if op == "classify":
        result = {"state": classify(request.get("observation") or {},
                                    accepted_at=request.get("accepted_at"),
                                    now=request.get("now"))}
    elif op == "event":
        result = event_for(request["outbound"], request.get("observation") or {}, request["now"])
    elif op == "batch":
        result = batch_health(request.get("outbounds") or [], request.get("observations") or [],
                              target=request.get("target", 70), now=request["now"])
    else:
        raise ValueError("unsupported_operation")
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps({"operation": op, "planned": True, "customer_sends": 0, "model_calls": 0}))


if __name__ == "__main__":
    main()
