import workspace_delivery_audit as w


def activity(action_type, smtp_code=0, reason=0, mail_event_type=0, success=True):
    connection = [
        {"name": "smtp_reply_code", "intValue": str(smtp_code)} if smtp_code else {},
        {"name": "smtp_response_reason", "intValue": str(reason)} if reason else {},
        {"name": "smtp_out_remote_host", "value": "mx.example.net"},
    ]
    connection = [x for x in connection if x]
    message = [
        {"name": "action_type", "intValue": str(action_type)},
        {"name": "rfc2822_message_id", "value": "<abc@example.com>"},
        {"name": "description", "value": "synthetic delivery event"},
        {"name": "connection_info", "messageValue": {"parameter": connection}},
        {"name": "destination", "multiMessageValue": [
            {"parameter": [{"name": "address", "value": "buyer@example.net"}]}
        ]},
    ]
    event_info = [
        {"name": "timestamp_usec", "intValue": "1789992000000000"},
        {"name": "success", "boolValue": success},
        {"name": "mail_event_type", "intValue": str(mail_event_type)},
    ]
    return {
        "id": {"time": "2026-09-21T12:00:00Z", "uniqueQualifier": "evt-1"},
        "events": [{
            "name": "delivery",
            "parameters": [
                {"name": "event_info", "messageValue": {"parameter": event_info}},
                {"name": "message_info", "messageValue": {"parameter": message}},
            ],
        }],
    }


def one_obs(a):
    flat = w.flatten_activity(a)[0]
    return w.observation_from_event(flat)


def test_outbound_smtp_250_is_delivered():
    obs = one_obs(activity(10, smtp_code=250, success=True))
    assert obs["provider_status"] == "DELIVERED"
    assert obs["rfc2822_message_id"] == "abc@example.com"
    assert obs["recipient_addresses"] == ["buyer@example.net"]


def test_action_14_is_deferred():
    obs = one_obs(activity(14, smtp_code=451, success=False))
    assert obs["provider_status"] == "DEFERRED"


def test_action_18_is_bounced():
    obs = one_obs(activity(18, smtp_code=550, reason=9, mail_event_type=30, success=False))
    assert obs["provider_status"] == "BOUNCED"
    assert "recipient_does_not_exist" in obs["diagnostic"]


def test_reputation_policy_is_rejected():
    obs = one_obs(activity(19, smtp_code=550, reason=16, success=False))
    assert obs["provider_status"] == "REJECTED"
    assert "low_ip_reputation" in obs["diagnostic"]


def test_gmail_acceptance_is_not_remote_delivery():
    obs = one_obs(activity(2, smtp_code=0, mail_event_type=35, success=True))
    assert obs["provider_status"] == "GMAIL_ACCEPTED"


def test_normalize_message_id():
    assert w.normalize_message_id(" <AbC@Example.COM> ") == "abc@example.com"
