"""Read-only company list from the local MySQL database (backend only).

Settings come from the environment, which rag_index.load_secrets() fills from .streamlit/secrets.toml.
Credentials and raw driver errors are never returned or logged.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("nia")

CONNECT_TIMEOUT = 5  # seconds
QUERY = "SELECT comp_gid AS id, comp_name AS company_name, isin_id AS ISIN FROM sta_mst_tcompany where delete_flag='N' and active_flag='Y' ORDER BY comp_name"


class CompaniesUnavailable(Exception):
    """The company list cannot be read (configuration, network, authentication or query problem)."""


def _settings() -> dict:
    try:
        host, user = os.environ["DB_HOST"], os.environ["DB_USER"]
        password, name = os.environ["DB_PASSWORD"], os.environ["DB_NAME"]
        port = int(os.environ.get("DB_PORT", "3306"))  # DB_PORT is text in the environment
    except (KeyError, ValueError):
        raise CompaniesUnavailable("database settings are missing or invalid") from None
    return {"host": host, "port": port, "user": user, "password": password, "database": name}


def _run(sql: str, params: tuple = ()) -> list:
    """Run one read-only SELECT with a timeout; cursor and connection are always closed."""
    try:
        import pymysql
    except ImportError:
        raise CompaniesUnavailable("MySQL driver is not installed") from None
    cfg = _settings()
    conn = cursor = None
    try:
        conn = pymysql.connect(**cfg, charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT,
                               read_timeout=CONNECT_TIMEOUT, write_timeout=CONNECT_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    except pymysql.MySQLError as exc:
        # Class and error code only: driver messages can contain host and user names.
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else "-"
        log.warning("database query failed: %s (%s)", type(exc).__name__, code)
        raise CompaniesUnavailable("database error") from None
    except OSError as exc:
        log.warning("database connection failed: %s", type(exc).__name__)
        raise CompaniesUnavailable("database unreachable") from None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def fetch_companies() -> list[dict]:
    return [{"id": r[0], "company_name": r[1], "ISIN": r[2]} for r in _run(QUERY)]


class FolioNotFound(Exception):
    pass


class FolioAmbiguous(Exception):
    pass


# KYC rule (application-specific): a non-deleted row in sta_trn_tfoliofreeze means KYC pending; none means compliant.
# foliofreeze_flag and remarks are deliberately not used. The freeze delete_flag condition must stay in the LEFT JOIN's
# ON clause: in WHERE it would drop compliant folios (no freeze row). folio_gid is selected only so that several
# freeze rows for ONE folio can be told apart from several different folios sharing a folio_no.
FOLIO_QUERY = """
SELECT t.holder1_name, t.holder1_fh_name, t.holder2_name, t.folio_shares, t.folio_no, t.comp_gid, t.folio_gid,
       s.comp_name,
       CASE WHEN u.foliofreeze_gid IS NOT NULL THEN 'KYC_PENDING' ELSE 'KYC_COMPLIANT' END AS kyc_status
FROM sta_trn_tfolio t
LEFT JOIN sta_trn_tfoliofreeze u
    ON t.comp_gid = u.comp_gid AND t.folio_gid = u.folio_gid AND u.delete_flag = 'N'
JOIN sta_mst_tcompany s ON t.comp_gid = s.comp_gid
WHERE t.delete_flag = 'N' AND t.comp_gid = %s AND t.folio_no = %s
"""


def _folio_status(comp_gid: int, folio_no: str) -> str:
    """Eligibility of a folio: NOT_A_SHAREHOLDER, KYC_PENDING or KYC_COMPLIANT. Never returns holder details.
    Raises FolioNotFound, FolioAmbiguous, or CompaniesUnavailable (database failure - never mapped to
    'not found' or 'compliant')."""
    rows = _run(FOLIO_QUERY, (comp_gid, str(folio_no)))  # folio_no stays text: leading zeros are kept
    if not rows:
        raise FolioNotFound
    # Several freeze rows repeat the same folio: collapse by folio_gid.
    by_folio: dict = {}
    for r in rows:
        by_folio.setdefault(r[6], []).append(r)
    if len(by_folio) > 1:
        raise FolioAmbiguous
    group = next(iter(by_folio.values()))
    shares = group[0][3]
    if shares is None or shares < 0:  # unusable share count: an error, not "zero shares"
        raise CompaniesUnavailable("incomplete folio record")
    if shares == 0:  # checked BEFORE KYC: no holding means not a shareholder and not eligible for KYC
        return "NOT_A_SHAREHOLDER"
    if any(r[8] == "KYC_PENDING" for r in group):
        return "KYC_PENDING"
    return "KYC_COMPLIANT"


def lookup_folio(comp_gid: int, folio_no: str) -> dict:
    """Return {"kyc_status": ...} only. Holder names and shares are released solely by verify_pan()."""
    return {"kyc_status": _folio_status(comp_gid, folio_no)}


# PAN match for the SAME company and folio. Parameter order: comp_gid, folio_no, PAN.
PAN_QUERY = """
SELECT folio_gid, folio_mail_id
FROM sta_trn_tfolio
WHERE comp_gid = %s
    AND folio_no = %s
    AND holder1_pan_no = %s
    AND delete_flag = 'N'
"""

DETAILS_QUERY = """
SELECT holder1_name, holder2_name, holder3_name, folio_shares
FROM sta_trn_tfolio
WHERE comp_gid = %s AND folio_gid = %s AND delete_flag = 'N'
"""

MAIL_QUERY = "SELECT folio_mail_id FROM sta_trn_tfolio WHERE comp_gid = %s AND folio_gid = %s AND delete_flag = 'N'"


def verify_pan(comp_gid: int, folio_no: str, pan: str) -> dict | None:
    """PAN match only: returns {"kyc_status", "folio_gid", "mail_id"} on a match, None on a mismatch. NO holder details
    are returned here; they are released by folio_details() after OTP verification. Eligibility and KYC are rechecked
    (never trusting the client); a non-compliant folio returns {"kyc_status": ...}.
    Raises FolioNotFound, FolioAmbiguous, or CompaniesUnavailable (a database failure is not a mismatch)."""
    status = _folio_status(comp_gid, folio_no)
    if status != "KYC_COMPLIANT":
        return {"kyc_status": status}
    rows = _run(PAN_QUERY, (comp_gid, str(folio_no), pan))
    if not rows:
        return None
    if len(rows) > 1:
        raise FolioAmbiguous
    return {"kyc_status": "KYC_COMPLIANT", "folio_gid": int(rows[0][0]), "mail_id": rows[0][1]}


def folio_mail(comp_gid: int, folio_gid: int) -> str | None:
    rows = _run(MAIL_QUERY, (comp_gid, folio_gid))
    return rows[0][0] if rows else None


def folio_details(comp_gid: int, folio_gid: int) -> dict:
    """Holder details for an OTP-authorized folio. Additional holders come from holder2_name / holder3_name."""
    rows = _run(DETAILS_QUERY, (comp_gid, folio_gid))
    if len(rows) != 1:
        raise CompaniesUnavailable("incomplete folio record")
    name, h2, h3, shares = rows[0]
    if shares is None or shares <= 0 or not (name or "").strip():
        raise CompaniesUnavailable("incomplete folio record")
    joint = [n.strip() for n in (h2, h3) if (n or "").strip()]
    return {"kyc_status": "KYC_COMPLIANT", "shareholder_name": name.strip(),
            "joint_holder_names": joint, "shares": int(shares)}
