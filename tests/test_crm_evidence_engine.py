from crm_evidence_engine import decide_crm_state


def _base(**signals):
    return {
        "assessment_complete": True,
        "buyer_signals": {
            "human_reply": True,
            "meeting_held": True,
            "proposal_requested": False,
            "proposal_reviewed": False,
            "internal_review": False,
            "decision_maker_review": False,
            "budget": "UNKNOWN",
            "scope": "UNKNOWN",
            "timeline": "UNKNOWN",
            "legal": "NONE",
            "procurement": "NONE",
            "contract": "NONE",
            "po": "NONE",
            "payment": "NONE",
            "paused": False,
            "declined": False,
            **signals,
        },
        "decision_reason": "test",
        "citations": [],
    }


def test_estimate_or_proposal_stays_pre_forecast():
    d = decide_crm_state(_base(proposal_requested=True, proposal_reviewed=True))
    assert d.stage == "Proposal"
    assert d.yomi == ""
    assert d.probability == ""


def test_internal_buyer_review_enters_c():
    d = decide_crm_state(_base(internal_review=True))
    assert d.yomi == "C"
    assert d.probability == "0.1"


def test_confirmed_budget_scope_timeline_and_dm_enters_b():
    d = decide_crm_state(_base(
        decision_maker_review=True,
        budget="CONFIRMED",
        scope="AGREED",
        timeline="CONFIRMED",
    ))
    assert d.yomi == "B"
    assert d.probability == "0.35"


def test_legal_or_procurement_enters_a():
    d = decide_crm_state(_base(legal="ACTIVE"))
    assert d.yomi == "A"
    assert d.probability == "0.7"


def test_signed_contract_or_po_enters_s():
    d = decide_crm_state(_base(contract="SIGNED"))
    assert d.yomi == "S"
    assert d.probability == "0.9"
    assert d.status == "合意・契約締結"


def test_payment_received_marks_won():
    d = decide_crm_state(_base(payment="RECEIVED"))
    assert d.stage == "Won"
    assert d.yomi == ""
    assert d.status == "受注"


def test_budget_unavailable_clears_forecast():
    d = decide_crm_state(
        _base(budget="UNAVAILABLE"),
        {"Stage": "Commercial", "Yomi": "B", "Probability": "0.35", "Status": "商談中"},
    )
    assert d.stage == "Hold"
    assert d.yomi == ""
    assert d.probability == ""
    assert d.status == "劣後"


def test_incomplete_evidence_does_not_erase_existing_forecast():
    payload = _base()
    payload["assessment_complete"] = False
    d = decide_crm_state(
        payload,
        {"Stage": "Commercial", "Yomi": "B", "Probability": "0.35", "Status": "商談中"},
    )
    assert d.stage == "Commercial"
    assert d.yomi == "B"
    assert d.probability == "0.35"
