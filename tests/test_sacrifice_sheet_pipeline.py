import copy
import hashlib
import unittest
from unittest.mock import Mock

from sacrifice_sheet_pipeline import SACRIFICE_ID, SSOT_ID, ORIGIN, SacrificeStore, message_gate, policy, process, research_gate, validate_seed


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.seed = {"company_name":"Kimonix","website":"https://www.kimonix.com/","domain":"EC/リテール",
                     "evidence_urls":["https://www.kimonix.com/"],"form_url":"https://www.kimonix.com/contact",
                     "subject":"Japan hypothesis","body":"Reviewed message"}
        self.seed["message_review"] = {"decision":"APPROVED","reviewer":"test reviewer","prompt_hash":"prompt",
                                       "body_sha256":hashlib.sha256(self.seed["body"].encode()).hexdigest()}
        self.evidence = {"status":"VERIFIED","pages":[{"status_code":200,"url":self.seed["website"],"title":"Kimonix"}]}
        row = [""] * 19
        row[5],row[7],row[10] = self.seed["website"],"未接触",ORIGIN
        self.store = Mock()
        self.store.append_verified.return_value = (710,True)
        self.store.existing.return_value = (710,row)
        self.store.history.return_value = False
        self.research = Mock(return_value=self.evidence)
        self.submit = Mock(return_value={"status":"FORM_SENT","submission_attempted":True})

    def run_one(self,execute=True):
        return process(self.seed,self.store,self.research,self.submit,execute=execute,prompt_hash="prompt")

    def test_ssot_rejected_before_any_api(self):
        api=Mock()
        with self.assertRaisesRegex(ValueError,"ssot_write_forbidden"):
            SacrificeStore(api,SSOT_ID)
        api.assert_not_called()
        api.spreadsheets.assert_not_called()

    def test_env_must_pin_sacrifice(self):
        with self.assertRaises(ValueError):policy({"SACRIFICE_SPREADSHEET_ID":SSOT_ID})
        policy({"SACRIFICE_SPREADSHEET_ID":SACRIFICE_ID})

    def test_vertex_true_rejected(self):
        with self.assertRaisesRegex(ValueError,"vertex_forbidden"):
            policy({"SACRIFICE_SPREADSHEET_ID":SACRIFICE_ID,"LEAD_FACTORY_VERTEX_ALLOWED":"TRUE"})

    def test_cross_company_form_rejected(self):
        self.seed["form_url"]="https://other.example/contact"
        with self.assertRaises(ValueError):validate_seed(self.seed)

    def test_fetch_failure_zero_is_not_success(self):
        self.evidence["pages"]=[{"status_code":0,"url":self.seed["website"],"title":"Kimonix"}]
        self.assertEqual(research_gate(self.seed,self.evidence),"REVIEW_SITE_UNVERIFIED")

    def test_redirect_rejected(self):
        self.evidence["pages"][0]["url"]="https://other.example/"
        self.assertEqual(research_gate(self.seed,self.evidence),"REVIEW_REDIRECT_IDENTITY")

    def test_company_identity_required(self):
        self.evidence["pages"][0]["title"]="Different company"
        self.assertEqual(research_gate(self.seed,self.evidence),"REVIEW_COMPANY_IDENTITY")

    def test_unverified_does_not_write_or_send(self):
        self.evidence["status"]="UNAVAILABLE"
        self.run_one()
        self.store.append_verified.assert_not_called()
        self.submit.assert_not_called()

    def test_research_only_adds_without_send(self):
        r=self.run_one(False)
        self.assertTrue(r["added"])
        self.submit.assert_not_called()

    def test_review_not_ready_never_calls_model_or_send(self):
        self.seed.pop("message_review")
        self.assertEqual(self.run_one()["status"],"REVIEW_MESSAGE")
        self.submit.assert_not_called()

    def test_prompt_revision_change_stops(self):
        self.assertEqual(message_gate(self.seed,"changed"),"REVIEW_PROMPT_CHANGED")

    def test_message_mutation_stops(self):
        self.seed["body"] += " extra"
        self.assertEqual(self.run_one()["status"],"REVIEW_MESSAGE_CHANGED")

    def test_any_contact_history_stops(self):
        self.store.history.return_value=True
        self.assertEqual(self.run_one()["status"],"DUPLICATE_OR_AMBIGUOUS_BLOCKED")
        self.submit.assert_not_called()

    def test_human_status_preserved(self):
        self.store.existing.return_value[1][7]="返信あり"
        self.assertEqual(self.run_one()["status"],"HUMAN_OR_LEGACY_ROW_PRESERVED")
        self.submit.assert_not_called()
        self.store.update_own_status.assert_not_called()

    def test_claim_failure_stops_before_send(self):
        self.store.audit.side_effect=RuntimeError("write failed")
        with self.assertRaises(RuntimeError):self.run_one()
        self.submit.assert_not_called()

    def test_success_claims_then_sends_then_records(self):
        parent=Mock()
        parent.attach_mock(self.store.audit,"audit")
        parent.attach_mock(self.submit,"submit")
        self.assertEqual(self.run_one()["status"],"FORM_SENT")
        self.assertEqual([x[0] for x in parent.mock_calls],["audit","submit","audit"])
        self.store.update_own_status.assert_called_once_with(self.seed,"DM済")

    def test_ambiguous_submission_held(self):
        self.submit.return_value={"status":"FORM_FAILED","submission_attempted":True}
        self.run_one()
        self.store.update_own_status.assert_called_once_with(self.seed,"保留")

    def test_no_submit_does_not_mark_sent(self):
        self.submit.return_value={"status":"FORM_FAILED","submission_attempted":False}
        self.run_one()
        self.store.update_own_status.assert_not_called()


if __name__ == "__main__":unittest.main()
