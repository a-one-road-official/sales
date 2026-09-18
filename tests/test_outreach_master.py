import copy
import json
import pytest
import outreach_master as m
from pathlib import Path

FIXTURE = json.loads((Path(__file__).parents[1] / "data" / "outreach_master_preview.json").read_text())
def result(company="Kimonix"):
    return {"subject": "Japan rollout for " + company, "greeting": "Hi " + company + " team,",
        "paragraphs": [p.replace("Kimonix", company) for p in FIXTURE["paragraphs"]],
        "selected_fact_id": "mhlw-2026-05-wholesale-retail",
        "selected_fact_quote": FIXTURE["candidate"]["japan_research"]["facts"][0]["text"],
        "buyer_segment":"Japanese Shopify fashion retailers", "workflow":"collection sorting",
        "operational_consequence":"less manual merchandising", "japan_maturity":"UNKNOWN"}

def test_each_company_reads_prompt_and_calls_model(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"; prompt.write_text("revision one")
    monkeypatch.setattr(m, "PROMPT_PATH", prompt)
    calls = []
    for company in ("Kimonix", "Example Commerce"):
        candidate = copy.deepcopy(FIXTURE["candidate"]); candidate["company_name"] = company
        def model(messages):
            calls.append(messages)
            return result(company)
        draft = m.generate_email(candidate, FIXTURE["site"], model_call=model)
        assert company in draft["body"]
        assert m.validate_email(draft) in range(110,121)
        assert draft["master_prompt_hash"] == m._hash(prompt.read_text())
        prompt.write_text("revision two")
    assert len(calls) == 2
    assert "revision one" in calls[0][0]["content"]
    assert "revision two" in calls[1][0]["content"]

def test_bad_copy_regenerates_and_unsupported_fact_rejected():
    calls = []
    def model(messages):
        calls.append(messages)
        out = result()
        if len(calls) == 1:
            out["paragraphs"][2] += " If irrelevant, let me know."
        return out
    draft = m.generate_email(FIXTURE["candidate"], FIXTURE["site"], model_call=model)
    assert len(calls) == 2
    assert "If irrelevant" not in draft["body"]
    bad = result(); bad["selected_fact_id"] = "invented"
    with pytest.raises(ValueError, match="UNSUPPORTED_JAPAN_FACT"):
        m.generate_email(FIXTURE["candidate"], FIXTURE["site"], model_call=lambda _:bad)

def test_missing_research_does_not_generate():
    with pytest.raises(ValueError, match="JAPAN_RESEARCH_REQUIRED"):
        m.generate_email({"company_name":"Missing"}, {}, model_call=lambda _:pytest.fail("must not generate"))

def test_no_paid_fallback(monkeypatch):
    monkeypatch.delenv("OUTREACH_LOCAL_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="LOCAL_AI_NOT_CONFIGURED"):
        m.generate_email(FIXTURE["candidate"], FIXTURE["site"])

def test_prompt_change_invalidates_send(tmp_path, monkeypatch):
    path=tmp_path/"prompt.txt"; path.write_text("before")
    monkeypatch.setattr(m,"PROMPT_PATH",path)
    draft=m.generate_email(FIXTURE["candidate"],FIXTURE["site"],model_call=lambda _:result())
    path.write_text("after")
    with pytest.raises(ValueError,match="MASTER_PROMPT_CHANGED"):
        m.verify_prompt_revision(draft)
