"""Checks for the Registry move, inline form links, wording, source hygiene and the two new PDFs.
Offline: the model is stubbed; retrieval uses the real local index (lexical fallback if no key)."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import form_links
import presentation
import rag_index
import sta_bot

URL = "https://www.gnsaindia.com/images/{}_new.pdf"


class LinkTests(unittest.TestCase):
    def test_mapped_forms_link_with_spacing_and_dash_variants(self):
        for raw, code in [("ISR-1", "ISR-1"), ("ISR 2", "ISR-2"), ("ISR‑3", "ISR-3"), ("ISR–4", "ISR-4"),
                          ("SH 13", "SH-13"), ("SH‑14", "SH-14")]:
            self.assertIn(f"[{raw}]({URL.format(code)})", form_links.linkify(f"Submit Form {raw} now."))

    def test_idempotent_no_nested_or_duplicate_links(self):
        text = "Use [ISR-1](https://example.org/x) and https://www.gnsaindia.com/images/ISR-1_new.pdf plus ISR-2."
        once = form_links.linkify(text)
        self.assertEqual(once, form_links.linkify(once))
        self.assertIn("[ISR-1](https://example.org/x)", once)
        self.assertEqual(once.count("https://www.gnsaindia.com/images/ISR-1_new.pdf"), 1)

    def test_unmapped_and_longer_codes_stay_plain(self):
        t = "ISR-10, Annexure 3, Form A and ISR-6."
        self.assertEqual(form_links.linkify(t), t)

    def test_transmission_annexures_get_related_gnsa_forms(self):
        U = "https://www.gnsaindia.com/images/"
        out = form_links.linkify("For transmission use Annexure 3 / TX-3, Annexure 4, TX-5 and Annexure 2.")
        self.assertIn(f"[Annexure 3 / TX-3]({U}ISR-5_updated.PDF)", out)
        self.assertIn(f"[Annexure 4]({U}Annexure_E_New.pdf)", out)
        self.assertIn(f"Annexure 5 — [Affidavit]({U}Annexure_D_New.pdf) and [NOC]({U}Annexure_F_New.pdf)", out)
        self.assertNotIn("(GNSA", out)
        self.assertTrue(out.endswith("and Annexure 2."))
        self.assertEqual(form_links.linkify(out), out)

    def test_direct_c_to_f_and_isr5_link_in_transmission_context(self):
        out = form_links.linkify("Transmission: ISR 5 / Annexure C, Annexure D, Annexure E and Annexure F.")
        for name in ("[ISR 5 / Annexure C](", "[Annexure D](", "[Annexure E](", "[Annexure F]("):
            self.assertIn(name, out)
        self.assertEqual(out.count("ISR-5_updated"), 1)

    def test_unrelated_annexures_untouched(self):
        t = "For KYC attach Annexure 3 and Annexure E; loss of share needs Annexure D."
        self.assertEqual(form_links.linkify(t), t)

    def test_map_only_has_the_supplied_urls(self):
        self.assertEqual(sorted(form_links.FORM_URLS), ["IEPF-5", "ISR-1", "ISR-2", "ISR-3", "ISR-4", "SH-13", "SH-14"])
        self.assertEqual(form_links.FORM_URLS["IEPF-5"], "https://www.gnsaindia.com/images/IEPF-5_help.pdf")
        self.assertTrue(all(u == URL.format(c) for c, u in form_links.FORM_URLS.items() if c != "IEPF-5"))


class WordingTests(unittest.TestCase):
    def test_bracket_removed_only_after_form_name_in_numbered_point(self):
        text = ("A mismatch (differs from the specimen) is common.\n"
                "1. Submit SEBI Form ISR-2 (banker confirmation of signature) with a cancelled cheque.\n"
                "2. Send ISR-1 [update request] to GNSA [S1].")
        out = presentation.strip_form_descriptions(text)
        self.assertIn("(differs from the specimen)", out)
        self.assertIn("1. Submit SEBI Form ISR-2 with a cancelled cheque.", out)
        self.assertIn("2. Send ISR-1 to GNSA [S1].", out)

    def test_gnsa_wording_keeps_titles_and_quotes(self):
        self.assertEqual(presentation.gnsa_wording("Send it to the RTA."), "Send it to GNSA.")
        keep = "See the SEBI Master Circular for RTAs."
        self.assertEqual(presentation.gnsa_wording(keep), keep)
        q = 'The circular says "the RTA shall retain".'
        self.assertEqual(presentation.gnsa_wording(q), q)

    def test_iepf_responsibilities_unchanged(self):
        t = "File IEPF-5 on the GNSA portal; GNSA generates the SRN."
        self.assertEqual(presentation.finish_text(t),
                         "File [IEPF-5](https://www.gnsaindia.com/images/IEPF-5_help.pdf) on the GNSA portal; GNSA generates the SRN.")

    def test_clean_sources(self):
        out = presentation.clean_sources([{"title": "", "url": "https://a"}, {"title": "A", "url": "https://a"},
                                          {"title": "A2", "url": "HTTPS://A"}, {"title": "B", "url": ""}])
        self.assertEqual(out, [{"title": "A", "url": "https://a"}])


class RoutingTests(unittest.TestCase):
    def topics(self):
        return {t["id"]: [q["q"] for q in t["questions"]] for t in sta_bot.load_topics()}

    def test_signature_questions_moved_to_registry(self):
        t = self.topics()
        for q in ("Signature mismatch", "Change in specimen signature"):
            self.assertIn(q, t["registry"])
            self.assertNotIn(q, t["kyc"])
            self.assertEqual(sta_bot.menu_entry(q)[0], "registry")

    def test_signature_profiles_read_registry_and_kyc(self):
        for p in sta_bot.SIGNATURE_PROFILES:
            self.assertEqual(sta_bot.categories_for("registry", "signature", p), ["registry", "kyc"])
        self.assertEqual(sta_bot.categories_for("registry", "duplicate certificate"), ["registry"])

    def test_menu_rta_wording(self):
        flat = json.dumps(sta_bot.load_topics())
        self.assertNotIn("from the RTA", flat)
        self.assertNotIn("Which form do I use for an RTA", flat)

    def test_classifier_override_to_registry_and_inline_link(self):
        info = {"category": "in_scope", "route": "kyc", "question": "my signature does not match",
                "clarification": "", "profile": "signature_mismatch"}
        seen = {}

        def fake_generate(client, question, standalone, history, user_name, sources, registry, today,
                          mode_note, profile="general"):
            seen["sources"] = sources
            return sta_bot.BotReply("1. Submit Form ISR-2 (banker form) [S1]", profile=profile)
        with mock.patch.object(sta_bot, "classify", return_value=info), \
                mock.patch.object(sta_bot, "_client", return_value=object()), \
                mock.patch.object(sta_bot, "_generate", side_effect=fake_generate):
            reply = sta_bot.answer("my signature does not match the one on record", topic="kyc")
        self.assertIn("[ISR-2](https://www.gnsaindia.com/images/ISR-2_new.pdf)", reply.text)
        self.assertNotIn("(banker form)", reply.text)
        self.assertEqual(reply.forms, [])
        titles = " ".join(s.get("title", "") for s in seen["sources"])
        self.assertIn("ISR-2", titles)

    def test_menu_signature_question_gets_kyc_and_registry_evidence_and_facts(self):
        seen = {}

        def fake_generate(client, question, standalone, history, user_name, sources, registry, today,
                          mode_note, profile="general"):
            seen["sources"], seen["profile"] = sources, profile
            return sta_bot.BotReply("ok", profile=profile)
        with mock.patch.object(sta_bot, "_client", return_value=object()), \
                mock.patch.object(sta_bot, "_generate", side_effect=fake_generate):
            sta_bot.answer("Signature mismatch", topic="registry")
        self.assertEqual(seen["profile"], "signature_mismatch")
        titles = " ".join(s.get("title", "") for s in seen["sources"])
        self.assertIn("ISR-1", titles)
        self.assertIn("ISR-2", titles)
        self.assertIn("SEBI_Investor_Service_FAQ", " ".join(s.get("header", "") for s in seen["sources"]))
        self.assertTrue(any(s.get("kind") == "policy" for s in seen["sources"]))

    def test_srn_fixed_answer_still_narrow(self):
        self.assertIsNotNone(sta_bot.approved_srn_reply("What is SRN?"))
        self.assertIsNone(sta_bot.approved_srn_reply("What is SRN in IEPF-5 claims and how do I file?"))


class IndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = sta_bot._index()

    def files(self, cat):
        return {f["filename"]: f for f in self.idx.status()["files"] if f["category"] == cat}

    def test_new_pdfs_indexed_in_correct_categories(self):
        self.assertIn("Affidavit cum Indemniity.pdf", self.files("registry"))
        self.assertEqual(self.files("registry")["Affidavit cum Indemniity.pdf"]["form_code"], "FORM-A")
        self.assertIn("Joint_Holder_Transmission_Format (1).pdf", self.files("transmission"))

    def test_lost_certificate_retrieves_form_a_and_isr4(self):
        hits = self.idx.search("registry", "How do I get a duplicate share certificate if mine is lost?")
        codes = {h["form_code"] for h in hits}
        self.assertTrue({"FORM-A", "ISR-4"} <= codes, codes)

    def test_surviving_joint_holder_retrieves_joint_holder_pdf(self):
        hits = self.idx.search("transmission", "surviving joint holder transmission after death of a holder")
        self.assertIn("Joint_Holder_Transmission_Format (1).pdf", {h["filename"] for h in hits})

    def test_joint_holder_header_carries_historical_note(self):
        hits = [h for h in self.idx.search("transmission", "surviving joint holder transmission")
                if h["filename"].startswith("Joint_Holder")]
        header = sta_bot._pdf_sources(hits[:1])[0]["header"]
        self.assertIn("NOTE:", header)
        self.assertIn("historical", header.lower())

    def test_no_duplicate_chunks(self):
        con = sqlite3.connect(self.idx.db_path)
        self.assertEqual(con.execute("select count(*)-count(distinct id) from chunks").fetchone()[0], 0)

    def test_surrogates_are_stripped(self):
        self.assertEqual(rag_index.clean_text("a\ud800b \udb40c"), "ab c")


if __name__ == "__main__":
    unittest.main()
