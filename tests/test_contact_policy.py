import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from contact_policy import reason, block_reason
from workbook_sales import SendAuthorization, WORKBOOK_ID, authorized, claim_candidate


class ContactPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'enabled': True, 'accounts': {'company:1': {
            'mode': 'BULK_ALLOWED', 'company_name': 'Acme', 'website': 'https://acme.example',
            'lane': 'BPO', 'campaign_id': 'c1', 'approved_by': 'operator',
            'approval_evidence': 'review:1', 'expires_at': '2026-09-30T00:00:00Z'}},
            'campaigns': {'c1': {'enabled': True, 'quality_passed': True, 'quality_evidence': 'audit:1'}}}

    def check(self, **kw):
        args = dict(policy=self.policy, company_id='company:1', company_name='Acme',
                    website='https://acme.example', lane='BPO', now=datetime(2026, 9, 17, tzinfo=timezone.utc))
        args.update(kw)
        return reason(**args)

    def test_explicit_permit(self):
        self.assertEqual(self.check(), '')

    def test_every_protected_or_missing_mode_blocks(self):
        for mode in ('PERSONAL', 'HOLD', 'DO_NOT_CONTACT', '', None):
            self.policy['accounts']['company:1']['mode'] = mode
            self.assertTrue(self.check())

    def test_missing_company_never_inherits_industry_permission(self):
        self.assertEqual(self.check(company_id='company:2'), 'account_permission_missing')

    def test_identity_and_lane_must_match(self):
        for kw in ({'company_name': 'Other'}, {'website': 'https://other.example'}, {'lane': 'EC_SACRIFICE'}):
            self.assertTrue(self.check(**kw))

    def test_expired_naive_or_invalid_time_blocks(self):
        for expiry in ('2026-09-01T00:00:00Z', '2026-09-30T00:00:00', 'bad'):
            self.policy['accounts']['company:1']['expires_at'] = expiry
            self.assertTrue(self.check())

    def test_quality_and_approval_required(self):
        self.policy['campaigns']['c1']['quality_passed'] = False
        self.assertTrue(self.check())
        self.policy['campaigns']['c1']['quality_passed'] = True
        self.policy['accounts']['company:1']['approval_evidence'] = ''
        self.assertTrue(self.check())

    def test_missing_malformed_and_disabled_policy_blocks(self):
        for policy in (None, [], {}, {'enabled': True}):
            self.assertTrue(self.check(policy=policy))
        with patch('contact_policy.POLICY_PATH') as path:
            path.read_text.side_effect = OSError('unavailable')
            self.assertEqual(block_reason('x', 'x', 'x', 'x'), 'contact_policy_unavailable')

    def test_shipped_policy_blocks_claim_before_api(self):
        sheets = Mock(spreadsheet_id=WORKBOOK_ID)
        with self.assertRaisesRegex(ValueError, 'contact_policy_paused'):
            claim_candidate(sheets, {'company_id': 'company:1'}, 'BPO', 'run1')
        self.assertEqual(sheets.mock_calls, [])

    def test_existing_token_is_denied_when_paused(self):
        auth = SendAuthorization('company:1', 'Acme', 'https://acme.example', 'BPO', 'run1')
        self.assertFalse(authorized(auth, Mock(spreadsheet_id=WORKBOOK_ID), 'Acme', 'https://acme.example'))
