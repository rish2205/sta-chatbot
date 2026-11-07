import json
import unittest
from types import SimpleNamespace
import answer_contracts as contracts

def client_for(result):
    return SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: SimpleNamespace(output_text=json.dumps(result))))

class ReviewTests(unittest.TestCase):
    def payload(self, profile):
        return {'profile': profile, 'contract': contracts.CONTRACTS[profile], 'question': 'KYC', 'evidence': 'PDF evidence', 'draft': {}}
    def test_blanket_approval_rejected_for_every_kyc_profile(self):
        for profile in ('kyc_completion', 'folio_compliance', 'kyc_documents', 'signature_mismatch', 'signature_change'):
            approved, issues = contracts.review(client_for({'approved': True, 'issues': []}), 'mock', self.payload(profile))
            self.assertFalse(approved)
            self.assertTrue(issues)
    def test_failed_condition_checks_block_approval(self):
        payload = self.payload('kyc_documents')
        checks = [{'index': i+1, 'meaning_preserved': True, 'supported': True, 'conditions_correct': True, 'reason': 'Specific evidence comparison'} for i in range(len(payload['contract']['required']))]
        approved, _ = contracts.review(client_for({'approved': True, 'issues': [], 'required_checks': checks}), 'mock', payload)
        self.assertTrue(approved)
        checks[-1]['conditions_correct'] = False
        approved, _ = contracts.review(client_for({'approved': True, 'issues': [], 'required_checks': checks}), 'mock', payload)
        self.assertFalse(approved)
    def test_iepf_schema_unchanged(self):
        approved, _ = contracts.review(client_for({'approved': True, 'issues': []}), 'mock', self.payload('srn_definition'))
        self.assertTrue(approved)
if __name__ == '__main__': unittest.main()
