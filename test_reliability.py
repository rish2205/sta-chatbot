"""Offline checks for answer grounding and official-source fetching.
Run: python -m unittest test_reliability test_rag -v"""
import unittest
from unittest.mock import patch
import sta_bot
from official_sources import allowed, collect


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.docs = [{'id': 'S1', 'title': 'Official', 'url': 'https://www.sebi.gov.in/test.pdf', 'excerpt': 'Evidence'}]
        self.registry = [{'code': 'ISR-1', 'url': 'https://www.sebi.gov.in/form.pdf', 'note': ''}]
        self.result = {'status': 'answer', 'answer': 'Use ISR-1 for physical updates. [S1]',
                       'claims': [{'text': 'Use ISR-1 for physical updates.', 'source_ids': ['S1']}],
                       'forms': [{'code': 'ISR-1', 'reason': 'Physical updates only.', 'source_ids': ['S1']}]}

    def test_valid_evidence_and_conditional_form(self):
        r = sta_bot.grounded_reply(self.result, self.docs, self.registry)
        self.assertEqual(r.kind, 'answer')
        self.assertIn('Physical updates only', r.forms[0]['note'])

    def test_invented_source_rejected(self):
        self.result['claims'][0]['source_ids'] = ['S99']
        self.assertEqual(sta_bot.grounded_reply(self.result, self.docs, self.registry).kind, 'unverified')

    def test_unknown_form_rejected(self):
        self.result['forms'][0]['code'] = 'ISR-99'
        self.assertEqual(sta_bot.grounded_reply(self.result, self.docs, self.registry).kind, 'unverified')

    def test_supported_form_card_does_not_require_exact_code_in_prose(self):
        self.result['answer'] = 'Use the attached KYC update form. [S1]'
        reply = sta_bot.grounded_reply(self.result, self.docs, self.registry)
        self.assertEqual(reply.kind, 'answer')
        self.assertEqual(reply.forms[0]['code'], 'ISR-1')

    def test_empty_claims_rejected(self):
        self.result['claims'] = []
        self.assertEqual(sta_bot.grounded_reply(self.result, self.docs, self.registry).kind, 'unverified')

    def test_fetch_failure_has_no_evidence(self):
        with patch('official_sources._cached_fetch', side_effect=ValueError('failed')):
            self.assertEqual(collect([{'url': 'https://www.sebi.gov.in/a'}], 'kyc', ['sebi.gov.in']), [])

    def test_pdf_attachment_prioritised(self):
        def fake(url, domains):
            if url.endswith('/a'): return url, [(1, 'A' * 500)], ['https://www.sebi.gov.in/a.pdf']
            return url, [(1, 'P' * 500)], []
        with patch('official_sources._cached_fetch', side_effect=fake):
            docs = collect([{'url': 'https://www.sebi.gov.in/a'}], 'kyc', ['sebi.gov.in'])
        self.assertEqual(docs[0]['url'], 'https://www.sebi.gov.in/a.pdf')

    def test_second_batch_tried_when_first_urls_fail(self):
        candidates = [{"url": f"https://www.iepf.gov.in/{i}.pdf"} for i in range(8)]
        def document(item, domains):
            if item["url"].endswith("7.pdf"):
                return {"url": item["url"], "title": "IEPF", "pages": [(1, "SRN means Service Request Number. " * 20)]}
            return None
        with patch("official_sources._document", side_effect=document) as fetch:
            docs = collect(candidates, "What is SRN?", ["iepf.gov.in"])
        self.assertEqual(fetch.call_count, 8)
        self.assertIn("Service Request Number", docs[0]["excerpt"])

    def test_url_boundaries(self):
        self.assertTrue(allowed('https://www.sebi.gov.in/a', ['sebi.gov.in']))
        for u in ['https://sebi.gov.in.evil.com/a', 'http://sebi.gov.in/a', 'https://sebi.gov.in:8000/a', 'https://user@sebi.gov.in/a']:
            self.assertFalse(allowed(u, ['sebi.gov.in']))


class SpeedTests(unittest.TestCase):
    def test_cached_document_avoids_refetch(self):
        import tempfile
        from pathlib import Path
        import official_sources as sources
        with tempfile.TemporaryDirectory() as tmp, patch.object(sources, 'CACHE_DIR', Path(tmp)), patch.object(sources, 'fetch', return_value=('https://www.sebi.gov.in/cache.pdf', [(1, 'x' * 500)], [])) as download:
            sources._cached_fetch('https://www.sebi.gov.in/cache.pdf', ['sebi.gov.in'])
            sources._cached_fetch('https://www.sebi.gov.in/cache.pdf', ['sebi.gov.in'])
            self.assertEqual(download.call_count, 1)
            self.assertRegex(sources.retrieved_on('https://www.sebi.gov.in/cache.pdf'), r'^\d{4}-\d{2}-\d{2}$')

    def test_relevant_content_beyond_page_start(self):
        import official_sources as sources
        with patch.object(sources, '_cached_fetch', return_value=('https://www.sebi.gov.in/test.pdf', [(1, 'Header ' * 400), (2, 'Other ' * 600 + 'email mobile contact ISR-1 ' * 20)], [])):
            docs = sources.collect([{'url': 'https://www.sebi.gov.in/test.pdf'}], 'email mobile', ['sebi.gov.in'], budget=4500)
        self.assertIn('ISR-1', docs[0]['excerpt'])


if __name__ == '__main__': unittest.main()
