"""Web server for the STA assistant widget.

Run:   uvicorn server:app --reload --port 8000
Open:  http://localhost:8000

Endpoints
  GET  /                        demo host page with the floating chat widget
  GET  /widget.js, /widget.css  the widget (embed it on any page with one <script> tag)
  GET  /api/config              assistant name, topics and follow-up questions
  POST /api/chat                answer a question (scope gate + official-source search)
  GET  /forms/{code}/view       open a form in the browser (PDF inline)
  GET  /forms/{code}/download   download a form (PDF attachment)

Environment (all optional)
  OPENAI_API_KEY   read from the environment or .streamlit/secrets.toml
  DEMO_MODE=1      canned answers, no OpenAI calls (to preview the UI)
  BOT_NAME         assistant name shown in the widget (default: Sia)
  ALLOWED_ORIGINS  comma-separated origins allowed to call the API when the
                   widget is embedded on another site (default: same origin only)
"""
from __future__ import annotations

import collections
import json
import os
import re
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent


import rag_index  # noqa: E402  (stdlib-only import; shares the secrets loader)

rag_index.load_secrets()  # must run before sta_bot reads its model settings

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import companies as company_db  # noqa: E402
import otp_service  # noqa: E402
import presentation  # noqa: E402
import sta_bot  # noqa: E402

BOT_NAME = os.getenv("BOT_NAME", "Nia")
TAGLINE = os.getenv("BOT_TAGLINE", "Your shareholder e-assistant")
DEMO_MODE = os.getenv("DEMO_MODE") == "1"

CACHE_DIR = BASE_DIR / "form_cache"
CACHE_TTL = 7 * 24 * 3600  # re-download forms from the official site weekly
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; STA-Assistant form fetch)"}

RATE_LIMIT = 20  # chat requests per IP per minute
_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_hits_lock = threading.Lock()

app = FastAPI(title="STA Assistant", docs_url=None, redoc_url=None)

_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"]
    )


# ------------------------------------------------------------------- helpers
def _read_json(name: str):
    return json.loads((BASE_DIR / name).read_text(encoding="utf-8"))


def _forms() -> dict:
    return {f["code"]: f for f in _read_json("forms.json")}


def _clean_name(raw: str | None) -> str | None:
    cleaned = re.sub(r"[^\w '-]", "", raw or "").strip()[:40]
    return cleaned or None


def _rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests")
        q.append(now)


def _public_form(f: dict) -> dict:
    return {
        "code": f["code"],
        "label": f.get("label", ""),
        "page": None if _local_form(f) else f.get("page"),
        "name": f["name"],
        "use": f.get("use", ""),
        "type": f.get("type", "none"),
        "version": f.get("version", ""),
        "note": f.get("note", ""),
        **({"url": f["url"]} if f.get("type") == "link" else {}),
    }


def _demo_reply(question: str) -> sta_bot.BotReply:
    time.sleep(0.9)
    registry = _forms()
    curated = sta_bot.curated_forms(question)  # same reviewed mapping the live bot uses
    pick = curated if curated is not None else []
    text = (
        f"This is a **demo answer** for: *{question}*\n\n"
        "In the live version the answer comes from official sources (SEBI, exchanges, depositories, IEPF, MCA).\n\n"
        "- First step shown here\n- Second step shown here\n- Third step shown here\n\n"
        + "A long answer follows so you can test follow-up questions.\n\n" + ("More detail. " * 200)
    )
    sources = [{"title": "SEBI FAQs (Jan 2026)", "url": "https://www.sebi.gov.in/sebi_data/faqfiles/jan-2026/1767611333081.pdf"}]
    text = presentation.finish_text(text)
    return sta_bot.BotReply(text, sources=sources)


# ----------------------------------------------------------------------- API
class Turn(BaseModel):
    role: str
    # Generous cap only to block abuse. Long earlier answers must NOT be rejected;
    # they are trimmed below before being used as context.
    content: str = Field(max_length=20000)


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=20)
    name: str | None = Field(default=None, max_length=60)
    topic: str | None = Field(default=None, max_length=30)  # menu topic the user last opened


@app.get("/api/config")
def config():
    topics = sta_bot.load_topics()
    return {
        "botName": BOT_NAME,
        "tagline": TAGLINE,
        "topics": [
            {
                "id": t["id"],
                "label": t["label"],
                "special": t.get("special"),
                "questions": [q["q"] for q in t["questions"]],
            }
            for t in topics
        ],
        "folioLookup": _folio_enabled(),
        "welcomeList": [t["label"] for t in topics if t.get("special") != "isin"],
    }


@app.get("/api/companies")
def companies():
    try:
        return {"companies": company_db.fetch_companies()}
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="The company list is temporarily unavailable. Please try again.")


# Folio lookup does NOT authenticate ownership. It is available only in an explicit local test mode
# (FOLIO_LOOKUP_MODE = "local_test" in secrets) and only from the local machine. Production must add
# shareholder verification before names or holdings are returned.
def _folio_enabled() -> bool:
    return os.getenv("FOLIO_LOOKUP_MODE") == "local_test"


class FolioIn(BaseModel):
    comp_gid: int = Field(ge=1)
    folio_no: str = Field(min_length=1, max_length=32)


@app.post("/api/folio/lookup")
def folio_lookup(body: FolioIn, request: Request):
    _rate_limit(request)
    if not _folio_enabled() or not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="Folio lookup is not available. Shareholder verification is required.")
    folio_no = body.folio_no.strip()
    if not folio_no:
        raise HTTPException(status_code=422, detail="Enter a folio number.")
    try:
        return company_db.lookup_folio(body.comp_gid, folio_no)
    except company_db.FolioNotFound:
        raise HTTPException(status_code=404, detail="Folio does not exist for the selected company.")
    except company_db.FolioAmbiguous:
        raise HTTPException(status_code=409, detail="More than one record matches. Please contact GNSA to resolve this folio.")
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="Folio lookup is temporarily unavailable. Please try again.")


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PAN_MAX_FAILS = 5          # failed verifications allowed ...
PAN_LOCK_WINDOW = 15 * 60  # ... per (client IP, company, folio) within this many seconds
_pan_fails: dict[tuple, collections.deque] = collections.defaultdict(collections.deque)
_pan_lock = threading.Lock()


def _pan_key(request: Request, comp_gid: int, folio_no: str) -> tuple:
    return (request.client.host if request.client else "unknown", comp_gid, folio_no)


def _pan_prune(key: tuple, now: float) -> collections.deque:
    q = _pan_fails[key]
    while q and now - q[0] > PAN_LOCK_WINDOW:
        q.popleft()
    return q


class PanIn(BaseModel):
    # No length/pattern constraints here: pydantic's 422 body would echo the PAN back. Validated manually below.
    comp_gid: int = Field(ge=1)
    folio_no: str = Field(min_length=1, max_length=32)
    pan: str = Field(default="")


@app.post("/api/folio/verify-pan")
def folio_verify_pan(body: PanIn, request: Request):
    """PAN matching for a folio (not full identity authentication). The PAN is read from the POST body only and is
    never logged. Names and shares are returned only on a match for a currently compliant folio."""
    _rate_limit(request)
    if not _folio_enabled() or not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="Folio lookup is not available. Shareholder verification is required.")
    folio_no = body.folio_no.strip()
    if not folio_no:
        raise HTTPException(status_code=422, detail="Enter a folio number.")
    pan = body.pan.strip().upper()
    if not pan:
        raise HTTPException(status_code=422, detail="Enter your PAN.")
    if not PAN_RE.fullmatch(pan):
        raise HTTPException(status_code=422, detail="Enter a valid 10-character PAN (for example ABCDE1234F).")
    key = _pan_key(request, body.comp_gid, folio_no)
    now = time.time()
    with _pan_lock:
        q = _pan_prune(key, now)
        if not q:
            _pan_fails.pop(key, None)  # keep the table from growing with idle keys
        if len(q) >= PAN_MAX_FAILS:
            raise HTTPException(status_code=429, detail="Too many failed attempts. Please try again later.")
    try:
        result = company_db.verify_pan(body.comp_gid, folio_no, pan)
    except company_db.FolioNotFound:
        raise HTTPException(status_code=404, detail="Folio does not exist for the selected company.")
    except company_db.FolioAmbiguous:
        raise HTTPException(status_code=409, detail="More than one record matches. Please contact GNSA to resolve this folio.")
    except company_db.CompaniesUnavailable:  # database failure: not a mismatch, so it does not count as a failed attempt
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable. Please try again.")
    if result is None:
        with _pan_lock:
            _pan_prune(key, time.time()).append(time.time())
        return JSONResponse({"verified": False, "detail": "PAN does not match this folio. Please check and try again."},
                            status_code=403, headers={"Cache-Control": "no-store"})
    if result["kyc_status"] != "KYC_COMPLIANT":
        return JSONResponse({"verified": False, "kyc_status": result["kyc_status"]}, headers={"Cache-Control": "no-store"})
    with _pan_lock:
        _pan_fails.pop(key, None)
    # PAN matched: NO details yet. Send an e-mail OTP; details are released only by /api/folio/otp/verify.
    try:
        sent = otp_service.start(body.comp_gid, result["folio_gid"], result["mail_id"])
    except otp_service.NoUsableEmail:
        return _nostore({"verified": False, "otp_sent": False,
                         "detail": "We do not have a usable email id for this folio please send your email id to GNSA."})
    except otp_service.SendFailed:
        raise HTTPException(status_code=502, detail="We could not send the code. Please try again.")
    except otp_service.ResendNotAllowed:
        raise HTTPException(status_code=429, detail="Too many codes requested. Please try again later or contact GNSA.")
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable. Please try again.")
    return _otp_response(sent)


_NOSTORE = {"Cache-Control": "no-store"}


def _nostore(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NOSTORE)


def _otp_response(call) -> JSONResponse:
    """OTP-sent payload: session id, masked recipient, UTC expiry. The OTP itself never appears in a response."""
    return _nostore({"verified": False, "otp_sent": True, **call})


class OtpSessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class OtpVerifyIn(OtpSessionIn):
    otp: str = Field(default="", max_length=16)


def _guard(request: Request) -> None:
    if not _folio_enabled() or not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="Folio lookup is not available. Shareholder verification is required.")


@app.post("/api/folio/otp/verify")
def otp_verify(body: OtpVerifyIn, request: Request):
    _guard(request)
    otp = body.otp.strip()
    if not otp_service.OTP_RE.fullmatch(otp):  # malformed input is not an attempt
        raise HTTPException(status_code=422, detail="Enter the 6-digit code.")
    try:
        res = otp_service.verify(body.session_id, otp)
        if res["status"] == "verified":
            return _nostore({"verified": True, **company_db.folio_details(res["comp_gid"], res["folio_gid"])})
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable. Please try again.")
    return _nostore({"verified": False, "status": res["status"], "attempts_left": res.get("attempts_left", 0)}, 403)


@app.post("/api/folio/otp/regenerate")
def otp_regenerate(body: OtpSessionIn, request: Request):
    _guard(request)
    try:
        return _otp_response(otp_service.regenerate(body.session_id))
    except otp_service.UnknownSession:
        raise HTTPException(status_code=404, detail="Verification session not found. Please start again.")
    except otp_service.ResendNotAllowed as e:
        msg = {"limit": "Too many codes requested. Please try again later or contact GNSA.",
               "verified": "Already verified."}.get(e.reason, "The current code is still valid.")
        raise HTTPException(status_code=429 if e.reason == "limit" else 409, detail=msg)
    except otp_service.NoUsableEmail:
        raise HTTPException(status_code=422, detail="We do not have a usable email address for this folio. Please contact GNSA.")
    except otp_service.SendFailed:
        raise HTTPException(status_code=502, detail="We could not send the code. Please try again.")
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable. Please try again.")


@app.post("/api/folio/details")
def folio_details(body: OtpSessionIn, request: Request):
    """Holder details only for a session whose OTP was verified (and not reset or timed out)."""
    _guard(request)
    try:
        data = otp_service.details(body.session_id)
    except company_db.CompaniesUnavailable:
        raise HTTPException(status_code=503, detail="Verification is temporarily unavailable. Please try again.")
    if data is None:
        raise HTTPException(status_code=403, detail="Email verification is required to view shareholder details.")
    return _nostore({"verified": True, **data})


@app.post("/api/folio/otp/reset")
def otp_reset(body: OtpSessionIn, request: Request):
    _guard(request)
    try:
        otp_service.reset(body.session_id)
    except company_db.CompaniesUnavailable:
        pass
    return _nostore({"ok": True})


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    _rate_limit(request)
    history = [
        {"role": t.role, "content": t.content[:1200]}
        for t in body.history
        if t.role in ("user", "assistant")
    ][-6:]
    if DEMO_MODE:
        reply = _demo_reply(body.question)
    else:
        reply = sta_bot.answer(body.question, history, user_name=_clean_name(body.name),
                               topic=body.topic if body.topic in sta_bot.ROUTE_IDS else None)
    return {
        "text": reply.text,
        "kind": reply.kind,
        "sources": reply.sources,
        "forms": [],  # form cards removed: form names are linked inline in the answer text
    }


# --------------------------------------------------------------------- forms
LOCAL_DIR = BASE_DIR / "forms_local"  # optional: drop official PDFs here as ISR-3.pdf, SH-13.pdf ...
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.sebi.gov.in/",
}
_last_fetch_error: dict[str, str] = {}


def _safe_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", code)


UPLOADS_DIR = BASE_DIR / "uploads"
_index = rag_index.RagIndex()  # used only to resolve indexed documents for /source-docs


def _local_form(form: dict) -> Path | None:
    """A form file we hold: forms_local/<code>.pdf override, else the forms.json "file" under uploads/."""
    override = LOCAL_DIR / f"{_safe_code(form['code'])}.pdf"
    if override.exists():
        return override
    rel = form.get("file")
    if rel:
        path = (UPLOADS_DIR / rel).resolve()
        if path.is_file() and UPLOADS_DIR.resolve() in path.parents:
            return path
    return None


def _fetch_pdf(form: dict) -> Path | None:
    """Return a PDF for the form: a local copy, a fresh cache, or a download from the official site."""
    code = _safe_code(form["code"])
    local = _local_form(form)
    if local:
        return local
    if not form.get("url"):
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{code}.pdf"
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return path
    err = ""
    for attempt in range(3):
        try:
            r = httpx.get(form["url"], headers=BROWSER_HEADERS, timeout=40, follow_redirects=True)
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
            elif b"%PDF-" not in r.content[:1024]:
                err = f"not a PDF (content-type {r.headers.get('content-type')})"
            elif not sta_bot.is_allowed_url(str(r.url)):
                err = f"redirected to a non-official host {r.url}"
            else:
                path.write_bytes(r.content)
                _last_fetch_error.pop(code, None)
                return path
        except httpx.HTTPError as exc:
            err = f"{type(exc).__name__}: {exc}"
        time.sleep(1 + attempt)
    _last_fetch_error[code] = err
    print(f"[forms] could not fetch {form['code']} from {form['url']}: {err}")
    return path if path.exists() else None  # an older copy beats nothing


@app.get("/forms/{code}/{mode}")
def form_file(code: str, mode: str):
    form = _forms().get(code.upper())
    if not form or mode not in ("view", "download"):
        raise HTTPException(status_code=404, detail="Form not found")
    if form.get("type") == "pdf" and (_local_form(form) or (form.get("url") and sta_bot.is_allowed_url(form["url"]))):
        path = _fetch_pdf(form)
        if path is not None:
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=f"Form_{form['code']}.pdf",
                content_disposition_type="inline" if mode == "view" else "attachment",
            )
    if not form.get("url") or not sta_bot.is_allowed_url(form["url"]):
        raise HTTPException(status_code=404, detail="Form not found")
    return RedirectResponse(form["url"])  # page-type form (IEPF-5) or the PDF could not be fetched


@app.get("/source-docs/{relpath:path}")
def source_doc(relpath: str):
    """View a cited document. Only files present in the document index can be opened."""
    path = _index.doc_path(relpath)
    if path is None:
        raise HTTPException(status_code=404, detail="Document not found")
    is_pdf = path.suffix.lower() == ".pdf"
    return FileResponse(path, media_type="application/pdf" if is_pdf else None, filename=path.name,
                        content_disposition_type="inline" if is_pdf else "attachment")


@app.get("/api/forms/check")
def forms_check():
    """Diagnostics: for each PDF form, can the server serve it directly? Open http://localhost:8000/api/forms/check"""
    out = {}
    for code, f in _forms().items():
        if f.get("type") != "pdf":
            out[code] = "page/none (opens official page)"
            continue
        ok = _fetch_pdf(f) is not None
        out[code] = "OK, served directly" if ok else f"FAILED: {_last_fetch_error.get(_safe_code(code), 'unknown')}"
    return out


# Static files (demo page, widget.js, widget.css). Mounted last so the routes above win.
class _RevalidatingStatic(StaticFiles):
    """Always revalidate (ETag still avoids re-downloads) so an updated widget.js is never served from a stale browser cache."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", _RevalidatingStatic(directory=BASE_DIR / "static", html=True), name="static")