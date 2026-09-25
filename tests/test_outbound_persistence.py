import outbound_persistence as op


def base_row():
    body = "Hello team, this is a complete approved customer email body."
    subject = "Company — Japan workflow"
    return {
        "営業メール宛先": "sales@example.com",
        "営業メール件名": subject,
        "営業メール本文": body,
        "営業メール根拠": "Official source evidence",
        "営業メール状態": "DRAFT_READY",
        "営業メール承認": "承認済み",
        "営業メール送信可否": "許可",
        "Last_Outbound_Message_ID": "",
        "AI実行JSON": (
            '{"draft":{"subject":"Company — Japan workflow",'
            '"body":"Hello team, this is a complete approved customer email body."}}'
        ),
    }


def test_validate_claim_candidate_passes_complete_exact_draft():
    result = op.validate_claim_candidate(base_row())
    assert result["ok"] is True
    assert result["errors"] == []
    assert len(result["body_sha256"]) == 64


def test_validate_claim_candidate_blocks_empty_body():
    row = base_row()
    row["営業メール本文"] = ""
    result = op.validate_claim_candidate(row)
    assert result["ok"] is False
    assert "EMPTY_BODY" in result["errors"]


def test_validate_claim_candidate_blocks_body_mismatch():
    row = base_row()
    row["営業メール本文"] = "Different body"
    result = op.validate_claim_candidate(row)
    assert result["ok"] is False
    assert "BODY_MISMATCH_META" in result["errors"]


def test_validate_claim_candidate_blocks_prior_send():
    row = base_row()
    row["Last_Outbound_Message_ID"] = "gmail-message-id"
    result = op.validate_claim_candidate(row)
    assert result["ok"] is False
    assert "PRIOR_MESSAGE_ID_PRESENT" in result["errors"]


def test_validate_claim_candidate_blocks_unapproved_state():
    row = base_row()
    row["営業メール承認"] = ""
    row["営業メール送信可否"] = ""
    result = op.validate_claim_candidate(row)
    assert result["ok"] is False
    assert "NOT_APPROVED:" in result["errors"]
    assert "NOT_PERMITTED:" in result["errors"]
