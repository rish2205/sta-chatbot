"""Focused OTP checks with mocked e-mail and an in-memory SQLite stand-in for sta_trn_otp (no live DB or SMTP)."""
import os
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock

os.environ.update(OTP_HMAC_KEY="k" * 40, OTP_EXPIRY_SECONDS="60", OTP_TEST_MODE="true", FOLIO_LOOKUP_MODE="local_test",
                  OTP_FROM_EMAIL="sender@example.com", OTP_TEST_TO_EMAIL="tester@example.com",
                  OTP_MAX_RESENDS="2", OTP_MAX_SENDS_PER_HOUR="50")

import companies as company_db  # noqa: E402
import otp_service as svc  # noqa: E402

sqlite3.register_adapter(datetime, lambda d: d.isoformat(" "))
sqlite3.register_converter("DT", lambda b: datetime.fromisoformat(b.decode()))
SCHEMA = """CREATE TABLE sta_trn_otp (otp_gid INTEGER PRIMARY KEY AUTOINCREMENT, comp_gid INT, folio_gid INT, otp_hash TEXT,
 verification_session_id TEXT, created_at DT, expires_at DT, verified_at DT, invalidated_at DT,
 attempt_count INT DEFAULT 0, max_attempts INT DEFAULT 5)"""


class Cur:
    def __init__(self, c):
        self.c = c.cursor()

    def execute(self, sql, params=()):
        self.c.execute(sql.replace("%s", "?").replace(" FOR UPDATE", ""), params)

    def __getattr__(self, n):
        return getattr(self.c, n)


class OtpTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        self.db.execute(SCHEMA)
        self.clock = datetime(2026, 1, 1, 12, 0, 0)
        self.sent = []
        self.fail_send = False

        @contextmanager
        def tx():
            try:
                yield Cur(self.db)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

        def send(to, otp):
            if self.fail_send:
                raise svc.SendFailed
            self.sent.append((to, otp))

        for p in (mock.patch.object(svc, "_tx", tx), mock.patch.object(svc, "send_email", send),
                  mock.patch.object(svc, "_now", lambda: self.clock),
                  mock.patch.object(company_db, "folio_mail", lambda c, f: "real@corp.com")):
            p.start()
            self.addCleanup(p.stop)

    def begin(self):
        r = svc.start(1, 7, "real@corp.com")
        return r["session_id"], self.sent[-1][1], r

    def test_success_test_mode_recipient_and_masking(self):
        sid, otp, r = self.begin()
        self.assertRegex(otp, r"^\d{6}$")
        self.assertEqual(self.sent[-1][0], "real@corp.com")  # always the folio's mail, even with OTP_TEST_MODE on
        self.assertEqual(r["masked_email"], "r***@c***.com")
        stored = self.db.execute("SELECT otp_hash, attempt_count FROM sta_trn_otp").fetchone()
        self.assertNotIn(otp, stored[0])
        self.assertEqual(stored[1], 0)
        self.assertEqual(svc.verify(sid, otp)["status"], "verified")
        self.assertEqual(self.db.execute("SELECT attempt_count FROM sta_trn_otp").fetchone()[0], 1)  # correct counts too
        self.assertEqual(svc.verify(sid, otp)["status"], "invalid")  # already used
        self.assertEqual(svc.authorized_folio(sid), (1, 7))

    def test_production_uses_folio_mail(self):
        with mock.patch.dict(os.environ, {"OTP_TEST_MODE": "false"}):
            r = svc.start(1, 7, "real@corp.com")
            self.assertEqual(self.sent[-1][0], "real@corp.com")
            self.assertFalse(r["test_mode"])
            with self.assertRaises(svc.NoUsableEmail):
                svc.start(1, 7, "  ")

    def test_wrong_otp_and_fifth_attempt(self):
        sid, otp, _ = self.begin()
        wrong = "000000" if otp != "000000" else "111111"
        for left in (4, 3, 2, 1):
            self.assertEqual(svc.verify(sid, wrong), {"status": "incorrect", "attempts_left": left})
        self.assertEqual(svc.verify(sid, otp)["status"], "verified")  # correct fifth attempt succeeds

        sid, otp, _ = self.begin()
        for _ in range(4):
            svc.verify(sid, wrong)
        self.assertEqual(svc.verify(sid, wrong)["status"], "exhausted")  # incorrect fifth invalidates
        self.assertEqual(svc.verify(sid, otp)["status"], "invalid")  # no sixth attempt, even with the right code
        self.assertEqual(self.db.execute("SELECT MAX(attempt_count) FROM sta_trn_otp").fetchone()[0], 5)
        self.assertIsNone(svc.authorized_folio(sid))

    def test_expiry_uses_same_timestamp(self):
        sid, otp, r = self.begin()
        self.assertEqual(r["expires_at"], "2026-01-01T12:01:00Z")
        self.clock += timedelta(seconds=60)
        self.assertEqual(svc.verify(sid, otp)["status"], "expired")
        self.assertIsNone(svc.authorized_folio(sid))

    def test_regeneration_and_old_code_rejected(self):
        sid, old, _ = self.begin()
        with self.assertRaises(svc.ResendNotAllowed):  # still valid: not allowed
            svc.regenerate(sid)
        self.clock += timedelta(seconds=61)
        r = svc.regenerate(sid)
        new = self.sent[-1][1]
        self.assertEqual(r["session_id"], sid)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sta_trn_otp WHERE invalidated_at IS NULL").fetchone()[0], 1)
        row = self.db.execute("SELECT attempt_count FROM sta_trn_otp ORDER BY otp_gid DESC").fetchone()
        self.assertEqual(row[0], 0)
        if old != new:
            self.assertEqual(svc.verify(sid, old)["status"], "incorrect")  # old code never works again
        self.assertEqual(svc.verify(sid, new)["status"], "verified")

    def test_resend_limit(self):
        sid, _, _ = self.begin()
        for _ in range(2):
            self.clock += timedelta(seconds=61)
            svc.regenerate(sid)
        self.clock += timedelta(seconds=61)
        with self.assertRaises(svc.ResendNotAllowed) as cm:
            svc.regenerate(sid)
        self.assertEqual(cm.exception.reason, "limit")

    def test_send_failure_invalidates(self):
        self.fail_send = True
        with self.assertRaises(svc.SendFailed):
            svc.start(1, 7, "x@y.com")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sta_trn_otp WHERE invalidated_at IS NULL").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sta_trn_otp").fetchone()[0], 1)

    def test_reset_revokes_authorization(self):
        sid, otp, _ = self.begin()
        svc.verify(sid, otp)
        svc.reset(sid)
        self.assertIsNone(svc.authorized_folio(sid))

    def test_unauthorized_details_access(self):
        from fastapi.testclient import TestClient
        import server
        c = TestClient(server.app)
        sid, otp, _ = self.begin()
        self.assertEqual(c.post("/api/folio/details", json={"session_id": sid}).status_code, 403)  # not verified
        self.assertEqual(c.post("/api/folio/details", json={"session_id": "nope"}).status_code, 403)
        details = {"kyc_status": "KYC_COMPLIANT", "shareholder_name": "A", "joint_holder_names": ["B"], "shares": 5}
        with mock.patch.object(company_db, "folio_details", lambda c_, f: details):
            r = c.post("/api/folio/otp/verify", json={"session_id": sid, "otp": otp})
            self.assertEqual(r.json()["shareholder_name"], "A")
            self.assertEqual(c.post("/api/folio/details", json={"session_id": sid}).status_code, 200)
            c.post("/api/folio/otp/reset", json={"session_id": sid})
            self.assertEqual(c.post("/api/folio/details", json={"session_id": sid}).status_code, 403)
        # verify-pan must not return details, only the OTP session
        with mock.patch.object(company_db, "verify_pan",
                               lambda *a: {"kyc_status": "KYC_COMPLIANT", "folio_gid": 7, "mail_id": "r@c.com"}):
            body = c.post("/api/folio/verify-pan", json={"comp_gid": 1, "folio_no": "1", "pan": "ABCDE1234F"}).json()
        self.assertNotIn("shareholder_name", body)
        self.assertNotIn("shares", body)
        self.assertTrue(body["otp_sent"] and body["session_id"])
        self.assertNotIn(self.sent[-1][1], str(body))


if __name__ == "__main__":
    unittest.main()
