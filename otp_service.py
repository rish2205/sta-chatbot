"""Email OTP for shareholder verification (backend only).

Table sta_trn_otp is only written here. Plain OTPs exist in memory long enough to be e-mailed and are never stored or
logged. All timestamps are naive UTC datetimes (the columns are DATETIME). Settings come from the environment, which
rag_index.load_secrets() fills from .streamlit/secrets.toml.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import ssl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import companies as company_db

log = logging.getLogger("nia")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTP_RE = re.compile(r"^[0-9]{6}$")


class OtpError(Exception):
    pass


class NoUsableEmail(OtpError):
    """Production folio has no usable folio_mail_id (or test/sender address is not configured)."""


class SendFailed(OtpError):
    pass


class UnknownSession(OtpError):
    pass


class ResendNotAllowed(OtpError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "not_expired" | "limit" | "verified"


# ------------------------------------------------------------------ settings
def _int(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, default)), 0)
    except ValueError:
        return default


def test_mode() -> bool:
    return os.environ.get("OTP_TEST_MODE", "").strip().lower() in ("true", "1", "yes")


def expiry_seconds() -> int:
    return max(_int("OTP_EXPIRY_SECONDS", 600), 1)  # the ONE expiry setting: backend and UI both use expires_at


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ helpers
def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    host, _, tld = domain.rpartition(".")    
    return f"{local[:1]}***@{host[:1]}***.{tld}"


def _hash(session_id: str, comp_gid: int, folio_gid: int, otp: str) -> str:
    """Keyed HMAC-SHA256, bound to the session and the matched company/folio."""
    key = os.environ.get("OTP_HMAC_KEY", "")
    if len(key) < 32:
        raise company_db.CompaniesUnavailable("OTP key not configured")
    msg = f"{session_id}|{comp_gid}|{folio_gid}|{otp}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def _recipient(folio_mail: str | None) -> str:
    """Always the folio_mail_id of the folio matched on company, folio_no and PAN; never a fixed address."""
    to = (folio_mail or "").strip()
    if not EMAIL_RE.fullmatch(to) or not EMAIL_RE.fullmatch(os.environ.get("OTP_FROM_EMAIL", "").strip()):
        raise NoUsableEmail
    return to


def send_email(to: str, otp: str) -> None:
    """Send OTP over encrypted SMTP without logging credentials or OTP."""
    try:
        secs = expiry_seconds()
        valid_for = (
            f"{secs // 60} minute(s)"
            if secs % 60 == 0
            else f"{secs} seconds"
        )

        host = os.environ.get("SMTP_HOST", "").strip()
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER", "").strip()
        pwd = os.environ.get("SMTP_PASSWORD", "")
        sender = os.environ.get("OTP_FROM_EMAIL", "").strip()

        if not all((host, user, pwd, sender)):
            raise ValueError("Required SMTP configuration is missing")

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = "AuthCode-Token"
        msg.set_content(
            f"Your one-time verification code is {otp}. "
            f"It is valid for {valid_for}. "
            "Do not share it with anyone."
            
        )

        ctx = ssl.create_default_context()
        log.info("SMTP: connecting host=%s port=%s", host, port)

        if port == 465:
            smtp = smtplib.SMTP_SSL(
                host, port, timeout=15, context=ctx
            )
        elif port == 587:
            smtp = smtplib.SMTP(host, port, timeout=15)
        else:
            raise ValueError("Use SMTP_PORT 587 or 465")

        with smtp:
            smtp.ehlo()
            log.info("SMTP: connected")

            if port == 587:
                smtp.starttls(context=ctx)
                smtp.ehlo()

            log.info("SMTP: TLS ready")

            if "LOGIN" not in smtp.esmtp_features.get("auth", "").upper().split():
                raise smtplib.SMTPNotSupportedError(
                    "Server does not advertise LOGIN authentication"
                )

            log.info("SMTP: authenticating")
            smtp.user = user
            smtp.password = pwd
            smtp.auth(
                "LOGIN",
                smtp.auth_login,
                initial_response_ok=False
            )
            log.info("SMTP: authenticated")

            log.info("SMTP: sending")
            smtp.send_message(msg)
            log.info("SMTP: sent")

    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        log.error(
            "SMTP authentication rejected; code=%s; reason=%s",
            exc.smtp_code,
            detail
        )
        raise SendFailed from None

    except ssl.SSLError as exc:
        log.error("SMTP TLS error: %s", str(exc))
        raise SendFailed from None

    except smtplib.SMTPResponseException as exc:
        log.error(
            "SMTP server rejected request; code=%s",
            exc.smtp_code
        )
        raise SendFailed from None

    except smtplib.SMTPServerDisconnected as exc:
        log.warning("OTP SMTP disconnected: %s", str(exc))
        raise SendFailed from None

    except Exception as exc:
        log.warning("OTP email failed: %s", type(exc).__name__)
        raise SendFailed from None

@contextmanager
def _tx():
    """One transaction: commits when the block returns, rolls back when it raises."""
    try:
        import pymysql
    except ImportError:
        raise company_db.CompaniesUnavailable("MySQL driver is not installed") from None
    conn = None
    try:
        conn = pymysql.connect(**company_db._settings(), charset="utf8mb4", connect_timeout=5,
                               read_timeout=5, write_timeout=5, autocommit=False)
        yield conn.cursor()
        conn.commit()
    except (pymysql.MySQLError, OSError) as exc:
        if conn:
            conn.rollback()
        log.warning("OTP database error: %s", type(exc).__name__)
        raise company_db.CompaniesUnavailable("database error") from None
    except BaseException:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


_LATEST = """SELECT otp_gid, comp_gid, folio_gid, otp_hash, expires_at, verified_at, invalidated_at,
       attempt_count, max_attempts FROM sta_trn_otp WHERE verification_session_id = %s
       ORDER BY otp_gid DESC LIMIT 1"""


def _latest(cur, session_id: str, lock: bool = False):
    cur.execute(_LATEST + (" FOR UPDATE" if lock else ""), (session_id,))
    return cur.fetchone()


def _invalidate(otp_gid: int) -> None:
    with _tx() as cur:
        cur.execute("UPDATE sta_trn_otp SET invalidated_at = %s WHERE otp_gid = %s AND invalidated_at IS NULL",
                    (_now(), otp_gid))


# ------------------------------------------------------------------ issue / regenerate
def _issue(session_id: str, comp_gid: int, folio_gid: int, to: str, resend: bool) -> dict:
    """Commit the OTP record, THEN send. A failed send invalidates the new record."""
    now = _now()
    expires = now + timedelta(seconds=expiry_seconds())
    otp = f"{secrets.randbelow(10 ** 6):06d}"  # CSPRNG, leading zeros kept
    digest = _hash(session_id, comp_gid, folio_gid, otp)
    with _tx() as cur:
        if resend:  # row lock serialises concurrent regenerations; eligibility is checked under the lock
            row = _latest(cur, session_id, lock=True)
            if row is None:
                raise UnknownSession
            _, _, _, _, exp, verified, invalidated, count, max_att = row
            if verified is not None:
                raise ResendNotAllowed("verified")
            if not (invalidated is not None or exp <= now or count >= max_att):
                raise ResendNotAllowed("not_expired")
            cur.execute("SELECT COUNT(*) FROM sta_trn_otp WHERE verification_session_id = %s", (session_id,))
            if cur.fetchone()[0] - 1 >= _int("OTP_MAX_RESENDS", 3):
                raise ResendNotAllowed("limit")
        cur.execute("SELECT COUNT(*) FROM sta_trn_otp WHERE comp_gi" 
        "d = %s AND folio_gid = %s AND created_at > %s",
                    (comp_gid, folio_gid, now - timedelta(hours=1)))
        if cur.fetchone()[0] >= _int("OTP_MAX_SENDS_PER_HOUR", 10):
            raise ResendNotAllowed("limit")
        cur.execute("UPDATE sta_trn_otp SET invalidated_at = %s WHERE verification_session_id = %s "
                    "AND invalidated_at IS NULL", (now, session_id))
        cur.execute("INSERT INTO sta_trn_otp (comp_gid, folio_gid, otp_hash, verification_session_id, created_at,"
                    " expires_at, attempt_count) VALUES (%s, %s, %s, %s, %s, %s, 0)",
                    (comp_gid, folio_gid, digest, session_id, now, expires))
        otp_gid = cur.lastrowid
    try:
        send_email(to, otp)
    except SendFailed:
        _invalidate(otp_gid)
        raise
    return {"session_id": session_id, "expires_at": iso(expires), "server_time": iso(_now()),
            "masked_email": mask_email(to), "test_mode": test_mode()}


def start(comp_gid: int, folio_gid: int, folio_mail: str | None) -> dict:
    """New verification session after a PAN match. Raises NoUsableEmail, SendFailed, ResendNotAllowed."""
    to = _recipient(folio_mail)
    return _issue(secrets.token_urlsafe(32), comp_gid, folio_gid, to, resend=False)


def regenerate(session_id: str) -> dict:
    with _tx() as cur:
        row = _latest(cur, session_id)
    if row is None:
        raise UnknownSession
    comp_gid, folio_gid = row[1], row[2]
    to = _recipient(company_db.folio_mail(comp_gid, folio_gid))
    return _issue(session_id, comp_gid, folio_gid, to, resend=True)


# ------------------------------------------------------------------ verify / authorize
def verify(session_id: str, otp: str) -> dict:
    """Returns {"status": verified|incorrect|exhausted|expired|invalid, "attempts_left": n, "comp_gid", "folio_gid"}.
    The row lock makes check, increment and outcome one atomic step, so concurrent guesses cannot exceed the limit."""
    now = _now()
    with _tx() as cur:
        row = _latest(cur, session_id, lock=True)
        if row is None:
            return {"status": "invalid"}
        otp_gid, comp_gid, folio_gid, stored, exp, verified, invalidated, count, max_att = row
        if verified is not None or invalidated is not None:
            return {"status": "invalid"}
        if exp <= now:
            return {"status": "expired"}
        if count >= max_att:
            return {"status": "exhausted"}
        cur.execute("UPDATE sta_trn_otp SET attempt_count = attempt_count + 1 WHERE otp_gid = %s "
                    "AND verified_at IS NULL AND invalidated_at IS NULL AND attempt_count < max_attempts "
                    "AND expires_at > %s", (otp_gid, now))
        if cur.rowcount != 1:
            return {"status": "invalid"}
        count += 1
        if hmac.compare_digest(stored, _hash(session_id, comp_gid, folio_gid, otp)):
            cur.execute("UPDATE sta_trn_otp SET verified_at = %s WHERE otp_gid = %s", (now, otp_gid))
            return {"status": "verified", "comp_gid": comp_gid, "folio_gid": folio_gid}
        if count >= max_att:
            cur.execute("UPDATE sta_trn_otp SET invalidated_at = %s WHERE otp_gid = %s", (now, otp_gid))
            return {"status": "exhausted", "attempts_left": 0}
        return {"status": "incorrect", "attempts_left": max_att - count}


def authorized_folio(session_id: str) -> tuple[int, int] | None:
    """(comp_gid, folio_gid) if this session holds a verified, un-reset, un-timed-out OTP; otherwise None."""
    now = _now()
    with _tx() as cur:
        row = _latest(cur, session_id)
    if row is None:
        return None
    _, comp_gid, folio_gid, _, _, verified, invalidated, _, _ = row
    if verified is None or invalidated is not None:
        return None
    if verified <= now - timedelta(seconds=_int("OTP_AUTH_TTL_SECONDS", 900)):
        return None
    return comp_gid, folio_gid


def details(session_id: str) -> dict | None:
    """Shareholder details, released only for an OTP-authorized session."""
    auth = authorized_folio(session_id)
    return company_db.folio_details(*auth) if auth else None


def reset(session_id: str) -> None:
    """Revoke authorization (company/folio/PAN changed, or conversation reset)."""
    with _tx() as cur:
        cur.execute("UPDATE sta_trn_otp SET invalidated_at = %s WHERE verification_session_id = %s "
                    "AND invalidated_at IS NULL", (_now(), session_id))
