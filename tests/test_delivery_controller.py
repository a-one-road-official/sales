from datetime import datetime, timezone

import delivery_controller as d


NOW = "2026-09-21T12:00:00+00:00"
ACCEPTED = "2026-09-21T10:00:00+00:00"


def outbound(i=1):
    return {
        "message_id": f"m{i}",
        "thread_id": f"t{i}",
        "recipient": f"user{i}@example.com",
        "accepted_at": ACCEPTED,
    }


def test_auto_ack_proves_delivery():
    obs = {
        "source": "RECIPIENT_AUTO_ACK",
        "verified": True,
        "thread_id": "t1",
        "observed_at": NOW,
    }
    assert d.classify(obs, accepted_at=ACCEPTED, now=NOW) == "DELIVERED"


def test_security_571_reject_opens_circuit_breaker():
    obs = {
        "message_id": "m1",
        "source": "GMAIL_DSN",
        "verified": True,
        "smtp_response": "550 5.7.1 rejected because it violates our security policy; SpamCop",
        "observed_at": NOW,
    }
    health = d.batch_health([outbound()], [obs], target=70, now=NOW)
    assert health["counts"] == {"REJECTED": 1}
    assert health["circuit_breaker"] is True
    assert "security_or_reputation_reject" in health["circuit_breaker_reasons"]
    assert health["can_start_next_cohort"] is False


def test_511_user_unknown_is_bounce_not_global_reputation_breaker():
    obs = {
        "message_id": "m1",
        "source": "GMAIL_DSN",
        "verified": True,
        "smtp_response": "550 5.1.1 The email account does not exist",
        "observed_at": NOW,
    }
    health = d.batch_health([outbound()], [obs], target=70, now=NOW)
    assert health["counts"] == {"BOUNCED": 1}
    assert health["circuit_breaker"] is False


def test_4xx_is_deferred_and_blocks_next_cohort():
    obs = {
        "message_id": "m1",
        "source": "WORKSPACE_EMAIL_LOG",
        "verified": True,
        "smtp_response": "451 4.7.0 Try again later",
        "observed_at": NOW,
    }
    health = d.batch_health([outbound()], [obs], target=70, now=NOW)
    assert health["counts"] == {"DEFERRED": 1}
    assert health["unresolved"] == 1
    assert health["cohort_closed"] is False


def test_unknown_requires_complete_search_and_24_hours():
    accepted = "2026-09-20T10:00:00+00:00"
    now = "2026-09-21T11:00:00+00:00"
    obs = {
        "source": "WORKSPACE_EMAIL_LOG",
        "verified": True,
        "search_complete": True,
        "observed_at": now,
    }
    assert d.classify(obs, accepted_at=accepted, now=now) == "UNKNOWN_LOG_GAP"


def test_five_closed_deliveries_unlock_ten_canary():
    outbounds = [outbound(i) for i in range(1, 6)]
    observations = [{
        "message_id": f"m{i}",
        "source": "WORKSPACE_EMAIL_LOG",
        "verified": True,
        "provider_status": "DELIVERED",
        "observed_at": NOW,
    } for i in range(1, 6)]
    health = d.batch_health(outbounds, observations, target=70, now=NOW)
    assert health["terminal_count"] == 5
    assert health["unresolved"] == 0
    assert health["cohort_closed"] is True
    assert health["next_canary_size"] == 10
    assert health["can_start_next_cohort"] is True


def test_result_partition_is_lossless():
    outbounds = [outbound(i) for i in range(1, 5)]
    observations = [
        {"message_id": "m1", "source": "WORKSPACE_EMAIL_LOG", "verified": True,
         "provider_status": "DELIVERED", "observed_at": NOW},
        {"message_id": "m2", "source": "GMAIL_DSN", "verified": True,
         "smtp_response": "550 5.1.1 no such user", "observed_at": NOW},
        {"message_id": "m3", "source": "WORKSPACE_EMAIL_LOG", "verified": True,
         "smtp_response": "451 4.7.0 try later", "observed_at": NOW},
    ]
    health = d.batch_health(outbounds, observations, target=70, now=NOW)
    assert health["attempted"] == 4
    assert health["terminal_count"] == 2
    assert health["unresolved"] == 2
    assert health["invariant_ok"] is True


def test_event_is_ssot_terminal_compatible():
    obs = {
        "source": "WORKSPACE_EMAIL_LOG",
        "verified": True,
        "provider_status": "DELIVERED",
        "provider_event_id": "provider-1",
        "observed_at": NOW,
    }
    result = d.event_for(outbound(), obs, NOW)
    assert result["state"] == "DELIVERED"
    event = result["event"]
    assert event["kind"] == "DELIVERED"
    assert event["outbound_message_id"] == "m1"
    assert event["delivery_evidence"]["source"] == "WORKSPACE_EMAIL_LOG"
