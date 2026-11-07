import unittest
from pii_mask import Masker


class PiiMaskTests(unittest.TestCase):
    def test_roundtrip_and_hidden(self):
        m = Masker()
        q = "My name is Ramesh Kumar, folio no. ABC12345, I hold 500 shares, PAN ABCDE1234F, a/c 123456789012"
        s = m.mask(q)
        for secret in ("Ramesh", "ABC12345", "500", "ABCDE1234F", "123456789012"):
            self.assertNotIn(secret, s)
        self.assertIn("<ACCOUNT_1>", s)
        self.assertEqual(m.unmask(s), q)

    def test_generic_text_untouched(self):
        m = Masker()
        q = "I am unable to find Form ISR-1 within 7 days, IEPF-5 SRN"
        self.assertEqual(m.mask(q), q)

    def test_known_name_and_history(self):
        m = Masker()
        ph = m.add_name("Rishab Krishna")
        self.assertEqual(m.mask("hello rishab krishna"), f"hello {ph}")
        self.assertEqual(m.mask_history([{"role": "user", "content": "PAN ABCDE1234F"}])[0]["content"], "PAN <PAN_1>")
        self.assertEqual(m.unmask(f"Hi {ph}"), "Hi Rishab Krishna")


if __name__ == "__main__":
    unittest.main()


class ContextTests(unittest.TestCase):
    def check(self, q, secrets, keep=()):
        m = Masker()
        s = m.mask(q)
        for x in secrets:
            self.assertNotIn(x, s, q)
        for x in keep:
            self.assertIn(x, s, q)
        self.assertEqual(m.unmask(s), q)

    def test_sentence(self):
        self.check("My name is Rishab and I hold 500 shares of Infosys. My email is rishab@gmail.com, phone 9600012345, PAN DFUPS4806A.",
                   ["Rishab", "500", "Infosys", "rishab@gmail", "9600012345", "DFUPS4806A"], keep=["My email is"])

    def test_label_list(self):
        self.check("Name - Rishab, Shares - 500, Company - Infosys, Email - rishab@gmail.com, PAN - DFUPS4806A",
                   ["Rishab", "500", "Infosys", "rishab@gmail", "DFUPS4806A"])

    def test_password_and_address(self):
        self.check("my password is Rishab@123", ["Rishab", "123"])
        self.check("I live at 12 MG Road, Chennai 600001, my mobile is +91 96000 12345", ["MG Road", "96000"])

    def test_honorific_and_general_text(self):
        self.check("held by Mr. Rishab Krishna jointly with Mrs Anita Krishna", ["Rishab", "Anita"])
        self.check("What is Form ISR-1, SEBI circular and Depository Participant?", [], keep=["ISR-1", "SEBI"])


class ReportedMessageTests(unittest.TestCase):
    def test_folio_pan_email_aadhaar_variants(self):
        m = Masker()
        q = ("folio number of thups4804 and pan no as dhups 4806a and email id is rishab@gmail,com "
             "and aadhar no is 1234 5678 9012")
        s = m.mask(q)
        for secret in ("thups4804", "dhups", "4806", "rishab@", "1234", "9012"):
            self.assertNotIn(secret, s)
        self.assertEqual(m.unmask(s), q)
