from outreach_execution import is_sacrificial_lane, semantic_email_preflight


def _row(**overrides):
    row = {
        "draft_id": "d1", "company_name": "Shop Example", "lane": "EC",
        "recipient": "founder@example.com", "recipient_verified": "TRUE",
        "subject": "A specific idea",
        "body": "Hello. https://calendar.app.google/adKEhXC4UWhQXfJp6",
    }
    row.update(overrides)
    return row


def test_sacrificial_lane_is_explicit_and_factory_is_blocked():
    cfg = {"OUTREACH_SACRIFICE_LANES": "EC,RETAIL"}
    assert is_sacrificial_lane(_row(), cfg)
    assert not is_sacrificial_lane(_row(lane="GROWTH"), cfg)
    assert not is_sacrificial_lane(_row(lane="MITTELSTAND"), cfg)


def test_email_url_is_allowed_but_identity_corruption_is_critical():
    cfg = {"OUTREACH_SACRIFICE_LANES": "EC"}
    assert semantic_email_preflight(_row(), cfg)["ok"] is True
