from outreach_execution import (
    OutboundEmailExecutor,
    is_sacrificial_lane,
    semantic_email_preflight,
)


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


def test_bpo_uses_its_own_flag_and_explicit_approval():
    cfg = {
        "LEAD_FACTORY_LIST_ONLY_LOCK": "FALSE",
        "OUTREACH_ALLOWED_LANES": "BPO",
        "OUTREACH_BPO_SEND_ENABLED": "TRUE",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "TRUE",
    }
    assert is_sacrificial_lane(_row(lane="BPO", source_type="BPO"), cfg)
    cfg["OUTREACH_BPO_SEND_ENABLED"] = "FALSE"
    assert not is_sacrificial_lane(_row(lane="BPO", source_type="BPO"), cfg)


def test_protected_ssot_cannot_be_relabelled_as_bpo():
    cfg = {
        "OUTREACH_ALLOWED_LANES": "BPO",
        "OUTREACH_BPO_SEND_ENABLED": "TRUE",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "TRUE",
    }
    assert not is_sacrificial_lane(
        _row(lane="SSOT", source_type="BPO"),
        cfg,
    )


def test_shared_executor_blocks_bpo_without_explicit_approval():
    cfg = {
        "LEAD_FACTORY_LIST_ONLY_LOCK": "FALSE",
        "LEAD_FACTORY_ALLOW_EXTERNAL_WRITE": "TRUE",
        "LEAD_FACTORY_SEND_MODE": "ENABLED",
        "OUTREACH_ALLOWED_LANES": "BPO",
        "OUTREACH_BPO_SEND_ENABLED": "TRUE",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "FALSE",
    }
    result = OutboundEmailExecutor(lane="BPO").execute(
        _row(lane="BPO", source_type="BPO"),
        cfg,
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "explicit_factory_send_approval_required"


def test_protected_ssot_cannot_be_relabelled_as_ec():
    cfg = {"OUTREACH_SACRIFICE_LANES": "EC,RETAIL"}
    assert not is_sacrificial_lane(
        _row(lane="SSOT", source_type="EC_SACRIFICE"),
        cfg,
    )


def test_ssot_requires_its_own_flag_and_explicit_approval():
    cfg = {
        "OUTREACH_ALLOWED_LANES": "SSOT",
        "OUTREACH_SSOT_SEND_ENABLED": "TRUE",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "TRUE",
    }
    assert is_sacrificial_lane(_row(lane="SSOT"), cfg)


def test_shared_executor_blocks_protected_ssot_without_approval():
    cfg = {
        "LEAD_FACTORY_ALLOW_EXTERNAL_WRITE": "TRUE",
        "LEAD_FACTORY_SEND_MODE": "ENABLED",
        "OUTREACH_ALLOWED_LANES": "SSOT",
        "OUTREACH_SSOT_SEND_ENABLED": "TRUE",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "FALSE",
    }
    result = OutboundEmailExecutor(lane="SSOT").execute(_row(lane="SSOT"), cfg)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "explicit_factory_send_approval_required"


def test_email_url_is_allowed_but_identity_corruption_is_critical():
    cfg = {"OUTREACH_SACRIFICE_LANES": "EC"}
    assert semantic_email_preflight(_row(), cfg)["ok"] is True
