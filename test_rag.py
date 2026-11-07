"""Offline tests for routing, PDF-first retrieval, restricted fallback and indexing.
No network and no API key: embeddings and the model client are faked.
Run: python -m unittest test_rag -v"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rag_index
import sta_bot

BASE = Path(__file__).parent


def fake_embed(texts):
    """Deterministic bag-of-words vectors; counts calls so tests can prove what was embedded."""
    fake_embed.seen.extend(texts)
    out = []
    for t in texts:
        v = [0.0] * 128
        for w in rag_index.tokens(t):
            v[hash(w) % 128] += 1.0
        out.append(v)
    return out


fake_embed.seen = []


def make_docx(path: Path, paragraphs: list[str]):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.uploads = self.tmp / "uploads"
        shutil.copytree(BASE / "uploads", self.uploads)
        fake_embed.seen = []
        self.idx = rag_index.RagIndex(self.tmp / "t.sqlite3", embed=fake_embed, uploads_dir=self.uploads)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_uploads_indexed_with_metadata(self):
        rep = self.idx.sync()
        self.assertEqual(rep["errors"], [])
        st = self.idx.status()
        names = {(f["category"], f["filename"]) for f in st["files"]}
        self.assertIn(("kyc", "ISR-2_Banker_Signature_Confirmation.pdf"), names)
        self.assertEqual({c for c, _ in names}, {"kyc", "transmission", "registry", "dividend", "iepf"})
        self.assertFalse([n for c, n in names if n.startswith("TX-")], "annexure extracts must not be indexed")
        self.assertIn(("transmission", "SEBI_Transmission_Framework_2026-07-23.pdf"), names)
        circ = [f for f in st["files"] if f["filename"].startswith("SEBI_Transmission")][0]
        self.assertEqual((circ["doc_date"], circ["effective"]), ("2026-07-23", "2026-08-22"))
        isr1 = [f for f in st["files"] if f["form_code"] == "ISR-1"][0]
        self.assertIn("2023", isr1["version"])

    def test_broad_kyc_questions_retrieve_fields_and_submission(self):
        self.idx.sync(lexical_only=True)
        for question in ("How can I complete my KYC process?",
                         "How can I make my folio KYC compliant?",
                         "What documents are acceptable for KYC?",
                         "Which proofs should I submit?",
                         "What papers do I need for KYC?"):
            hits = self.idx.search("kyc", question, use_vectors=False)
            self.assertTrue(hits)
            self.assertTrue(any(h["form_code"] == "ISR-1" for h in hits))
            self.assertTrue(any("FAQ" in h["filename"] for h in hits))
            self.assertTrue(all(h["category"] == "kyc" for h in hits))
            text = " ".join(h["text"].lower() for h in hits)
            for term in ("pan", "bank", "signature", "post", "e-sign", "passport", "utility", "cancelled cheque"):
                self.assertIn(term, text)

    def test_iepf_definitions_and_paraphrases_have_local_evidence(self):
        self.idx.sync(lexical_only=True)
        for question, expected in (
            ("What is SRN?", "service request number"),
            ("Explain the service request number on my claim", "future correspondence"),
            ("What is IEPF?", "investor education and protection fund"),
            ("When is unpaid dividend transferred to IEPF?", "unpaid dividend account"),
            ("How long before unclaimed dividends go to the fund?", "seven years"),
        ):
            hits = self.idx.search("iepf", question, use_vectors=False)
            self.assertTrue(all(h["category"] == "iepf" for h in hits))
            self.assertIn(expected, " ".join(h["text"].lower() for h in hits), question)

    def test_iepf_filing_retrieves_actual_sample_checklist(self):
        self.idx.sync(lexical_only=True)
        for question in ("IEPF online sample forms and procedures", "How do I file a refund claim?",
                         "Which documents should I submit with the IEPF application?"):
            hits = self.idx.search("iepf", question, use_vectors=False)
            self.assertEqual(hits[0]["form_code"], "IEPF-5-SAMPLE")
            self.assertEqual(hits[0]["page"], 5)
            text = " ".join(h["text"].lower() for h in hits)
            for term in ("nodal officer", "indemnity", "acknowledgement", "cancelled cheque", "client master list"):
                self.assertIn(term, text)
        self.assertFalse(rag_index.is_iepf_filing_query("What is SRN?"))
        self.assertFalse(rag_index.is_iepf_filing_query("Procedure for transfer of unpaid dividend to IEPF"))

    def test_repeated_semantic_query_reuses_embedding(self):
        self.idx.sync()
        fake_embed.seen = []
        self.idx.search("iepf", "What is SRN?")
        self.idx.search("iepf", "What is SRN?")
        self.assertEqual(len(fake_embed.seen), 1)

    def test_glyph_encoded_pdf_is_readable(self):
        self.idx.sync()
        hits = self.idx.search("dividend", "declaration without deduction of tax", use_vectors=False)
        self.assertTrue(any("15G" in h["filename"] for h in hits))
        self.assertNotIn("/c49", " ".join(h["text"] for h in hits))

    def test_reindex_is_duplicate_free_and_embeds_nothing_twice(self):
        self.idx.sync()
        con = self.idx._con()
        n1 = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        ids = con.execute("SELECT COUNT(DISTINCT id) FROM chunks").fetchone()[0]
        con.close()
        self.assertEqual(n1, ids)
        fake_embed.seen = []
        rep = self.idx.sync()
        self.assertEqual(fake_embed.seen, [])
        self.assertTrue(all(r["status"] == "unchanged" for r in rep["categories"].values()))
        fake_embed.seen = []
        self.idx.sync(rebuild=True)  # re-chunk everything: cached embeddings are reused
        self.assertEqual(fake_embed.seen, [])
        self.assertEqual(self.idx.status()["total_chunks"], n1)

    def test_changed_file_embeds_only_new_text_and_deleted_file_is_removed(self):
        docx = self.uploads / "Registry" / "Duplicate_Securities.docx"
        make_docx(docx, ["Duplicate securities procedure. " * 6, "Submit form ISR-4 with an affidavit and indemnity. " * 5])
        self.idx.sync()
        fake_embed.seen = []
        make_docx(docx, ["Duplicate securities procedure. " * 6, "A brand new paragraph about newspaper publication. " * 5])
        self.idx.sync()
        self.assertTrue(fake_embed.seen)
        self.assertTrue(all("newspaper" in t.lower() or "duplicate securities" in t.lower() for t in fake_embed.seen))
        self.assertEqual(self.idx.search("registry", "newspaper publication", use_vectors=False)[0]["filename"],
                         "Duplicate_Securities.docx")
        docx.unlink()
        self.idx.sync()
        self.assertFalse([h for h in self.idx.search("registry", "newspaper publication", use_vectors=False)
                          if h["filename"] == "Duplicate_Securities.docx"])
        con = self.idx._con()
        distinct = con.execute("SELECT COUNT(DISTINCT text_sha) FROM chunks").fetchone()[0]
        con.close()
        self.assertEqual(self.idx.status()["embeddings"], distinct, "stale embeddings must be removed")

    def test_identical_chunks_stored_once(self):
        text = "This identical paragraph about duplicate share certificates appears in two files. " * 4
        make_docx(self.uploads / "Registry" / "a.docx", [text])
        make_docx(self.uploads / "Registry" / "b.docx", [text])
        rep = self.idx.sync()
        self.assertGreaterEqual(rep["categories"]["registry"]["duplicates_skipped"], 1)

    def test_docx_skipped_when_same_name_pdf_exists(self):
        make_docx(self.uploads / "Registry" / "ISR-4_Duplicate_Certificate_Other_Services.docx", ["x" * 100])
        rep = self.idx.sync()
        self.assertTrue(any("indexing the PDF only" in w for w in rep["warnings"]))

    def test_search_is_scoped_to_category_and_mapped_forms(self):
        self.idx.sync()
        hits = self.idx.search("dividend", "banker confirms signature of the holder", use_vectors=False)
        self.assertTrue(all(h["category"] == "dividend" or h["form_code"] in ("ISR-1",) for h in hits))
        self.assertFalse([h for h in hits if h["form_code"] == "ISR-2"], "ISR-2 is not mapped for dividend")
        hits = self.idx.search("registry", "banker confirms signature of the holder", use_vectors=False)
        self.assertEqual(hits[0]["filename"], "ISR-2_Banker_Signature_Confirmation.pdf")  # mapped supporting form
        self.assertEqual(hits[0]["category"], "kyc")

    def test_citation_fields_present(self):
        self.idx.sync()
        h = self.idx.search("kyc", "specimen signature banker", use_vectors=False)[0]
        for key in ("category", "filename", "page", "relpath"):
            self.assertTrue(h[key])

    def test_doc_path_only_resolves_indexed_files(self):
        self.idx.sync()
        self.assertIsNotNone(self.idx.doc_path("KYC/ISR-1_PAN_KYC_Update.pdf"))
        self.assertIsNone(self.idx.doc_path("../rag_index.py"))
        self.assertIsNone(self.idx.doc_path("Transmission/TX-2_QTP_Request_Form.pdf"))  # not indexed


class FakeClient:
    """Stands in for OpenAI: classifier and answer calls are told apart by their instructions."""

    def __init__(self, classify=None, answers=()):
        self.classify_result = classify or {}
        self.answers = list(answers)
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if kw.get("instructions") == sta_bot.SCOPE_PROMPT:
            return SimpleNamespace(output_text=json.dumps(self.classify_result))
        if kw.get("tools"):
            return SimpleNamespace(output=[], output_text="research")
        return SimpleNamespace(output_text=json.dumps(self.answers.pop(0)))


HIT = {"id": "x", "category": "kyc", "filename": "ISR-2_Banker_Signature_Confirmation.pdf", "relpath":
       "KYC/ISR-2_Banker_Signature_Confirmation.pdf", "page": 1, "text": "Banker confirms the signature.",
       "doc_date": "2023-03-16", "effective": None, "version": "SEBI circular of Mar 16, 2023", "title": None,
       "supersedes": None, "form_code": "ISR-2", "score": 0.9, "cos": 0.5, "lex": 3.0}
GOOD = {"status": "answer", "answer": "1. Get the banker to confirm your signature on ISR-2. [S1]",
        "claims": [{"text": "banker confirms signature", "source_ids": ["S1"]}],
        "forms": [{"code": "ISR-2", "reason": "Signature confirmation by banker.", "source_ids": ["S1"]}]}
INSUFFICIENT = {"status": "insufficient", "answer": "", "claims": [], "forms": []}


class RoutingTests(unittest.TestCase):
    def test_menu_options_exact(self):
        topics = {t["id"]: [q["q"] for q in t["questions"]] for t in sta_bot.load_topics()}
        self.assertEqual(topics["kyc"], [
            "How can I complete my KYC process?", "How can I make my folio KYC compliant?",
            "What documents are acceptable for KYC?", "Signature mismatch", "Change in specimen signature"])
        self.assertEqual(topics["iepf"], [
            "What is IEPF?", "What is SRN?", "IEPF online sample forms and procedures",
            "Transfer of unclaimed/unpaid dividend to IEPF"])
        self.assertIn("What is transmission of shares?", topics["transmission"])  # untouched
        self.assertIn("Why have I not received my dividend?", topics["dividend"])

    def test_menu_question_routes_without_classifier(self):
        self.assertEqual(sta_bot.menu_entry("signature mismatch")[0], "kyc")
        self.assertEqual(sta_bot.menu_entry("What is SRN?")[0], "iepf")
        self.assertIsNone(sta_bot.menu_entry("Who won the match?"))

    def test_topic_to_category(self):
        self.assertEqual(sta_bot.categories_for("kyc", "x"), ["kyc"])
        self.assertEqual(sta_bot.categories_for("nomination", "x"), ["kyc"])
        self.assertEqual(sta_bot.categories_for("demat", "how to convert"), [])
        self.assertEqual(sta_bot.categories_for("dividend", "did my dividend move to IEPF"), ["dividend", "iepf"])

    def test_fallback_domains_are_restricted(self):
        self.assertEqual(sta_bot.fallback_domains("kyc", "signature"), ["sebi.gov.in"])
        d = sta_bot.fallback_domains("iepf", "what is srn")
        self.assertEqual(set(d), {"sebi.gov.in", "iepf.gov.in", "mca.gov.in"})
        self.assertNotIn("nseindia.com", sta_bot.ALLOWED_DOMAINS)


class AnswerFlowTests(unittest.TestCase):
    def run_answer(self, question, client, hits=(HIT,), history=None, topic=None, fallback=None):
        fake_index = SimpleNamespace(calls=[], search=None)

        def search_fn(cat, q, *a, **k):
            fake_index.calls.append((cat, q))
            return list(hits)
        fake_index.search = search_fn
        fb = fallback or patch.object(sta_bot, "_official_fallback", side_effect=AssertionError("fallback used"))
        with patch.object(sta_bot, "_client", return_value=client), patch.object(sta_bot, "_index", return_value=fake_index), fb as fb_mock:
            reply = sta_bot.answer(question, history or [], topic=topic)
        return reply, fake_index, fb_mock

    def test_menu_question_is_pdf_first_with_page_citation_and_form(self):
        client = FakeClient(answers=[GOOD])
        reply, index, _ = self.run_answer("Signature mismatch", client)
        self.assertEqual(index.calls[0][0], "kyc")
        self.assertEqual(reply.kind, "answer")
        self.assertNotIn("ISR-2_Banker_Signature_Confirmation.pdf", reply.text)
        self.assertIn("page 1", reply.sources[0]["title"])
        self.assertEqual(reply.sources[0]["url"], "/source-docs/KYC/ISR-2_Banker_Signature_Confirmation.pdf#page=1")
        self.assertEqual([f["code"] for f in reply.forms], ["ISR-2"])
        self.assertEqual(len(client.calls), 1, "menu question needs no classifier call and no web search")

    def test_failed_iepf_generation_uses_actual_pdf_checklist_without_web(self):
        index = rag_index.RagIndex()
        hits = index.search("iepf", "IEPF online sample forms and procedures", use_vectors=False)
        reply, _, fb = self.run_answer("IEPF online sample forms and procedures",
                                      FakeClient(answers=[INSUFFICIENT]), hits=hits)
        self.assertEqual(reply.kind, "answer")
        self.assertNotIn("Source: Uploaded PDFs", reply.text)
        self.assertIn("historical 2022", reply.text)
        self.assertIn("Cancelled Cheque", reply.text)
        self.assertIn("Death certificate", reply.text)
        self.assertTrue(any("#page=5" in item["url"] for item in reply.sources))
        fb.assert_not_called()

    def test_empty_checklist_does_not_manufacture_a_procedure(self):
        self.assertIsNone(sta_bot._sample_checklist_reply([HIT]))

    def test_pdf_sources_are_in_panel_not_answer_header(self):
        reply, _, fb = self.run_answer("Signature mismatch", FakeClient(answers=[GOOD]))
        self.assertNotIn("Source: Uploaded PDFs", reply.text)
        self.assertTrue(reply.sources)
        fb.assert_not_called()

    def test_historical_iepf_sample_is_labelled_and_does_not_use_web(self):
        sample = dict(HIT, category="iepf", filename="IEPF-5-ONLINE-CLAIM_FORM (1).pdf",
                      relpath="IEPF/IEPF-5-ONLINE-CLAIM_FORM (1).pdf", page=5,
                      form_code="IEPF-5-SAMPLE", version="MCA printout 2022", doc_date=None,
                      text="After filing send the signed printout to the company Nodal Officer.")
        result = {"status": "answer", "answer": "The sample says to send the signed printout to the company Nodal Officer. [S1]",
                  "claims": [{"text": "Send printout to the Nodal Officer.", "source_ids": ["S1"]}], "forms": []}
        client = FakeClient(answers=[result])
        reply, _, fb = self.run_answer("IEPF online sample forms and procedures", client, hits=[sample])
        self.assertNotIn("Source: Uploaded PDFs", reply.text)
        self.assertIn("historical 2022 IEPF sample", reply.text)
        self.assertIn("Nodal Officer", reply.text)
        self.assertIn("historical sample guidance", client.calls[0]["instructions"])
        fb.assert_not_called()

    def test_supported_partial_pdf_answer_stays_local(self):
        partial = dict(GOOD, answer="Use ISR-2 for banker confirmation. [S1] The PDF does not state processing time.")
        reply, _, fb = self.run_answer("Signature mismatch", FakeClient(answers=[partial]))
        self.assertIn("does not state processing time", reply.text)
        fb.assert_not_called()

    def test_pdf_evidence_insufficient_uses_restricted_fallback(self):
        client = FakeClient(answers=[INSUFFICIENT])
        fallback = patch.object(sta_bot, "_official_fallback", return_value=sta_bot.BotReply("from sebi", kind="answer"))
        reply, _, fb = self.run_answer("What documents are acceptable for KYC?", client, fallback=fallback)
        self.assertEqual(reply.text, "from sebi")
        fb.assert_called_once()

    def test_no_pdf_hits_goes_to_fallback_without_model_call_on_pdfs(self):
        client = FakeClient()
        fallback = patch.object(sta_bot, "_official_fallback", return_value=sta_bot.BotReply("x", kind="answer"))
        _, _, fb = self.run_answer("What is SRN?", client, hits=[], fallback=fallback)
        fb.assert_called_once()
        self.assertEqual(client.calls, [])

    def test_unrelated_question_refused_without_search(self):
        client = FakeClient(classify={"category": "out_of_scope"})
        reply, index, _ = self.run_answer("Who won the cricket match?", client)
        self.assertEqual(reply.kind, "refusal")
        self.assertEqual(index.calls, [])
        self.assertLess(len(reply.text), 300)

    def test_followup_uses_context_and_standalone_rewrite(self):
        client = FakeClient(classify={"category": "in_scope", "route": "kyc",
                                      "standalone_question": "What documents are needed to change specimen signature?"},
                            answers=[GOOD])
        hist = [{"role": "user", "content": "Change in specimen signature"},
                {"role": "assistant", "content": "Use ISR-2."}]
        reply, index, _ = self.run_answer("what documents are needed?", client, history=hist, topic="kyc")
        self.assertEqual(index.calls[0], ("kyc", "What documents are needed to change specimen signature?"))
        self.assertIn("Change in specimen signature", client.calls[0]["input"])  # history reached the classifier
        self.assertEqual(reply.kind, "answer")

    def test_typed_question_about_other_topic_overrides_menu_hint(self):
        client = FakeClient(classify={"category": "in_scope", "route": "iepf", "standalone_question": "What is SRN?"}, answers=[GOOD])
        _, index, _ = self.run_answer("srn meaning", client, topic="kyc")
        self.assertEqual(index.calls[0][0], "iepf")

    def test_unclear_followup_asks_one_clarification(self):
        client = FakeClient(classify={"category": "unclear", "clarification": "Which form do you mean?"})
        reply, index, _ = self.run_answer("and that one?", client)
        self.assertEqual((reply.kind, reply.text), ("clarification", "Which form do you mean?"))
        self.assertEqual(index.calls, [])

    def test_conflicting_evidence_is_reported_not_resolved(self):
        conflict = dict(GOOD, status="conflict", forms=[], answer="The two documents differ: [S1] says A.")
        reply, _, _ = self.run_answer("Signature mismatch", FakeClient(answers=[conflict]))
        self.assertEqual(reply.kind, "answer")
        self.assertIn("differ", reply.text)

    def test_curated_forms_override_for_menu_question(self):
        good = dict(GOOD, forms=[])
        reply, _, _ = self.run_answer("IEPF online sample forms and procedures", FakeClient(answers=[good]))
        self.assertEqual([f["code"] for f in reply.forms], ["IEPF-5", "IEPF-5-SAMPLE"])

    def test_grouped_citations_are_validated_and_rendered(self):
        good = dict(GOOD, answer="Use ISR-2. [S1, S2]")
        hits = [HIT, dict(HIT, id="y", page=2)]
        reply, _, _ = self.run_answer("Signature mismatch", FakeClient(answers=[good]), hits=hits)
        self.assertNotIn("[S1", reply.text)
        self.assertEqual(len(reply.sources), 2)
        bad = dict(GOOD, answer="Use ISR-2. [S1, S99]")
        fallback = patch.object(sta_bot, "_official_fallback", return_value=sta_bot.BotReply("unverified", kind="unverified"))
        _, _, fb = self.run_answer("Signature mismatch", FakeClient(answers=[bad]), fallback=fallback)
        fb.assert_called_once()

    def test_pdf_stage_failure_never_invents_a_citation(self):
        bad = dict(GOOD, claims=[{"text": "x", "source_ids": ["S42"]}])
        fallback = patch.object(sta_bot, "_official_fallback", return_value=sta_bot.BotReply("unverified", kind="unverified"))
        reply, _, fb = self.run_answer("Signature mismatch", FakeClient(answers=[bad]), fallback=fallback)
        fb.assert_called_once()
        self.assertEqual(reply.kind, "unverified")


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [patch.object(sta_bot, "FALLBACK_CACHE_DIR", self.tmp)]
        for p in self.patches:
            p.start()
        self.doc = [{"id": "S1", "title": "SEBI FAQ", "url": "https://www.sebi.gov.in/faq.pdf",
                     "excerpt": "Banker confirmation is needed.", "retrieved_on": "2026-10-06"}]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, route, text, client):
        with patch.object(sta_bot, "collect", return_value=list(self.doc)) as col:
            reply = sta_bot._official_fallback(client, text, text, route, [], None, sta_bot._load_json("forms.json"), "06 October 2026")
        return reply, col

    def test_search_domains_and_labelled_answer(self):
        client = FakeClient(answers=[GOOD])
        reply, col = self.call("kyc", "signature", client)
        search_call = [c for c in client.calls if c.get("tools")][0]
        self.assertEqual(search_call["tools"][0]["filters"]["allowed_domains"], ["sebi.gov.in"])
        self.assertEqual(col.call_args.args[2], ["sebi.gov.in"])
        self.assertTrue(reply.text.startswith("**Source: Official websites**"))
        self.assertIn("retrieved 2026-10-06", reply.text.lower())
        self.assertIn("retrieved 2026-10-06", reply.sources[0]["title"])

    def test_iepf_allows_iepf_and_mca(self):
        client = FakeClient(answers=[GOOD])
        self.call("iepf", "what is srn", client)
        search_call = [c for c in client.calls if c.get("tools")][0]
        self.assertEqual(set(search_call["tools"][0]["filters"]["allowed_domains"]),
                         {"sebi.gov.in", "iepf.gov.in", "mca.gov.in"})

    def test_discovery_cached_then_expires(self):
        search = lambda cl: [c for c in cl.calls if c.get("tools")]  # noqa: E731
        c1 = FakeClient(answers=[GOOD])
        found = [{"title": "t", "url": "https://www.sebi.gov.in/x.pdf"}]
        with patch.object(sta_bot, "_extract_sources", return_value=found):
            self.call("kyc", "signature", c1)
            c2 = FakeClient(answers=[GOOD])
            self.call("kyc", "signature", c2)
            self.assertEqual((len(search(c1)), len(search(c2))), (1, 0), "second call must hit the cache")
            entry = list(json.loads((self.tmp / "fallback_search.json").read_text()).values())[0]
            self.assertEqual(entry["sources"], found)
            self.assertRegex(entry["retrieved"], r"^\d{4}-\d{2}-\d{2}$")
            with patch.dict(os.environ, {"FALLBACK_CACHE_HOURS": "0"}):
                c3 = FakeClient(answers=[GOOD])
                self.call("kyc", "signature", c3)
                self.assertEqual(len(search(c3)), 1, "expiry is configurable")

    def test_discovery_failure_still_tries_official_seeds(self):
        client = FakeClient(answers=[GOOD])
        original = client.responses.create
        def create(**kwargs):
            if kwargs.get("tools"):
                raise TimeoutError("discovery timed out")
            return original(**kwargs)
        client.responses.create = create
        reply, fetched = self.call("iepf", "What is SRN?", client)
        self.assertEqual(reply.kind, "answer")
        self.assertTrue(any("IEPF-5-20240718" in item["url"] for item in fetched.call_args.args[0]))

    def test_no_readable_official_source_is_unverified(self):
        with patch.object(sta_bot, "collect", return_value=[]):
            reply = sta_bot._official_fallback(FakeClient(), "q", "q", "kyc", [], None, [], "today")
        self.assertEqual(reply.kind, "unverified")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import server
        cls.client = TestClient(server.app)

    def test_config_menu_unchanged_shape(self):
        cfg = self.client.get("/api/config").json()
        kyc = [t for t in cfg["topics"] if t["id"] == "kyc"][0]
        self.assertEqual(len(kyc["questions"]), 5)
        self.assertIn("Transmission", cfg["welcomeList"])

    def test_form_view_and_download_use_uploaded_files(self):
        r = self.client.get("/forms/ISR-1/view")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "application/pdf"))
        self.assertTrue(r.headers["content-disposition"].startswith("inline"))
        r = self.client.get("/forms/TX-2/download")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(self.client.get("/forms/FORM-15G/view").status_code, 200)
        self.assertEqual(self.client.get("/forms/NOPE/view").status_code, 404)

    def test_form_without_local_file_still_has_official_link(self):
        r = self.client.get("/forms/IEPF-5/view", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (307, "https://www.iepf.gov.in/IEPF/refund.html"))

    def test_source_docs_cannot_escape_the_index(self):
        for path in ("../server.py", "..%2Fserver.py", "KYC/../../secrets.toml", "Transmission/TX-2_QTP_Request_Form.pdf"):
            self.assertEqual(self.client.get("/source-docs/" + path).status_code, 404, path)

    def test_chat_passes_topic(self):
        with patch.object(sta_bot, "answer", return_value=sta_bot.BotReply("hi")) as ans, \
                patch("server.DEMO_MODE", False):
            self.client.post("/api/chat", json={"question": "q", "history": [], "topic": "kyc"})
            self.client.post("/api/chat", json={"question": "q", "history": [], "topic": "bogus"})
        self.assertEqual([c.kwargs["topic"] for c in ans.call_args_list], ["kyc", None])


if __name__ == "__main__":
    unittest.main()
