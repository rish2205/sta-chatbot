"""Mask personal details in user text before it leaves the server for OpenAI, and restore them in the reply.

One Masker per request: the same value always gets the same placeholder (<PAN_1>), across the question, the
history and the user's name, so the model can still reason about "the PAN you mentioned". Nothing is stored.

Detection is local pattern/context based (no model call): shapes (PAN, email, phone...), labels ("Email - x",
"password is x"), and introductions ("my name is x", "Mr x", "shares of x").
"""
import re

_CAP = r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}"
_ORG = r"[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,3}"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"

# Label -> kind, for "Label - value" / "Label: value" / "Label is value" lists.
_LABELS = {
    "name": "NAME", "holder": "NAME", "shareholder": "NAME", "holder name": "NAME", "full name": "NAME",
    "company": "COMPANY", "company name": "COMPANY", "organisation": "COMPANY", "organization": "COMPANY",
    "shares": "SHARES", "share": "SHARES", "no of shares": "SHARES", "no. of shares": "SHARES",
    "number of shares": "SHARES", "quantity": "SHARES", "holding": "SHARES",
    "email": "EMAIL", "e-mail": "EMAIL", "email id": "EMAIL", "mail": "EMAIL",
    "phone": "PHONE", "mobile": "PHONE", "contact": "PHONE", "mobile number": "PHONE", "phone number": "PHONE",
    "password": "SECRET", "pwd": "SECRET", "passcode": "SECRET", "pin": "SECRET", "otp": "SECRET",
    "dob": "DOB", "date of birth": "DOB", "birth date": "DOB",
    "address": "ADDRESS", "pan": "PAN", "pan no": "PAN", "pan number": "PAN",
    "aadhaar": "AADHAAR", "aadhar": "AADHAAR", "aadhaar number": "AADHAAR", "aadhar number": "AADHAAR",
    "folio": "FOLIO", "folio no": "FOLIO", "folio number": "FOLIO",
    "account": "ACCOUNT", "account no": "ACCOUNT", "account number": "ACCOUNT", "a/c": "ACCOUNT",
    "dp id": "DPID", "client id": "DPID", "ifsc": "IFSC",
}
_LABEL_RX = re.compile(
    r"(?i:\b(" + "|".join(sorted((re.escape(k) for k in _LABELS), key=len, reverse=True)) + r")\.?\s*(?:[:=\-–]|\b(?:is|as)\b)\s*)"
    r"([^,;\n]+?)(?=\s*(?:[,;\n]|\band\b|$))")

# Words that follow "I am" / "this is" without being a name ("I am unable to...", "this is regarding...").
_NOT_AFTER_IAM = ("a|an|the|not|unable|trying|writing|looking|having|holding|facing|getting|unsure|sure|also|still|"
                  "currently|interested|from|in|on|at|new|old|now|here|there|very|so|just|already|wondering|asking|"
                  "requesting|applying|planning|calling|contacting|one|his|her|their|shareholder|holder|nominee|legal|"
                  "joint|first|sole|minor|senior|resident|deceased|married|unaware|aware|confused|worried|happy|glad|"
                  "ready|able|going|being|done|regarding|about|for|with|to|and|my|our|your|this|that|what|how|why|"
                  "when|where|which|who|it|is|are|was|were|will|can|could|would|should|name|case|related|concerning|"
                  "totally|really|actually|basically|simply|only|too|never|always|often|sorry|thankful|grateful|"
                  "available|present|absent|confirmed|verified|registered|eligible|entitled|claiming|seeking|needing")

# (kind, regex, group). Specific shapes first, so a PAN is not swallowed by the generic long-number rule.
_RULES = [
    ("ADDRESS", re.compile(r"(?i:\b(?:i live (?:at|in)|my address is|address\s+of\s+mine\s+is|address\s+is|residing at|resident of)\s+)([^.\n]+?)(?=\s*(?:[.\n]|$|,?\s+(?:and\s+)?my\s))"), 1),
    ("SECRET", re.compile(r"(?i:\b(?:password|passwd|pwd|passcode|otp)\s*(?:is|=|:)?\s*)(\S+)"), 1),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\s*[.,]\s*(?:com|in|org|net|co|edu|gov|io)\b|(?:\.[\w-]+)*)", re.I), 0),
    ("FOLIO", re.compile(r"(?i:\bfolio(?:\s*(?:no\.?|number|id|#))?\s*(?:of|is|as|[:\-=])?\s*)((?=[A-Z0-9/-]*\d)[A-Z0-9/-]{3,})", re.I), 1),
    ("ACCOUNT", re.compile(r"(?i:\b(?:a/c|account)(?:\s*(?:no\.?|number))?\s*[:\-]?\s*)(\d{6,18})\b"), 1),
    ("PAN", re.compile(r"\b[A-Z]{5}[\s-]?\d{4}[\s-]?[A-Z]\b", re.I), 0),
    ("AADHAAR", re.compile(r"(?i:\b(?:aadhaa?r|aadhar|uid)(?:\s*(?:no\.?|number|card|id))?\s*(?:of|is|as|[:\-=])?\s*)(\d[\d -]{7,15}\d)"), 1),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)"), 0),
    ("PHONE", re.compile(r"(?<![\d])(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), 0),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), 0),
    ("DPID", re.compile(r"\bIN\d{14}\b|\b\d{16}\b"), 0),
    ("SHARES", re.compile(r"\b\d[\d,]*(?=\s+(?:equity\s+|physical\s+)?shares?\b)|(?i:\b(?:shares?|holding)\s+of\s+)(\d[\d,]*)\b", re.I), 0),
    ("AMOUNT", re.compile(r"(?i:(?:rs\.?|inr|₹)\s*)\d[\d,]*(?:\.\d+)?"), 0),
    ("DOB", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+\d{2,4}\b", re.I), 0),
    ("NUMBER", re.compile(r"(?<![\w-])\d{9,}(?![\w-])"), 0),
    ("COMPANY", re.compile(r"(?i:\bshares?\s+(?:of|in)\s+)(?!(?i:the|my|a|an|this|that|our|his|her|their|any|some|\d)\b)(" + _ORG + r"|[a-z][\w&-]*)"), 1),
    ("COMPANY", re.compile(r"(?i:(?:\b\d[\d,]*|<SHARES_\d+>)\s+(?:shares?\s+)?(?:in|of)\s+)(?!(?i:the|my|a|an|this|that|our|his|her|their|any|some)\b)(" + _ORG + r"|[a-z][\w&-]*)"), 1),
    ("COMPANY", re.compile(r"\b(?:[A-Z][\w&.-]*\s+){1,4}(?:Limited|Ltd\.?|Pvt\.?|Private|Inc\.?|Corporation|Corp\.?|Industries|Bank)\b"), 0),
    ("NAME", re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt|Sri|Late)\.?\s+(" + _CAP + r")\b"), 1),
    ("NAME", re.compile(r"(?i:\b(?:my name is|name is|i am|i'm|this is|in the name of|holder name|name|held by|owner is)\s*:?\s+)(" + _CAP + r")\b"), 1),
    ("NAME", re.compile(r"(?i:\b(?:my name is|name is|name|i am|i'm|this is)\s*:?\s+)(?!(?i:" + _NOT_AFTER_IAM + r")\b)([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"), 1),
]
_NOT_NAMES = {"Form", "The", "Not", "Unable", "Having", "Trying", "Looking", "Sorry", "Nia", "Please", "Hi", "Hello",
              "Mr", "Mrs", "Ms", "Miss", "Dr", "Shri", "Smt", "Sri", "Late", "Sebi", "Iepf", "Kyc", "Rta", "Gnsa", "Aadhaar", "Pan", "Depository"}
_PLACEHOLDER = re.compile(r"<([A-Z]+_\d+)>")


class Masker:
    def __init__(self):
        self._to_ph: dict[str, str] = {}
        self._to_val: dict[str, str] = {}
        self._count: dict[str, int] = {}

    def _ph(self, kind: str, value: str) -> str:
        value = value.strip()
        key = f"{kind}:{value.lower()}"
        if key not in self._to_ph:
            self._count[kind] = self._count.get(kind, 0) + 1
            ph = f"<{kind}_{self._count[kind]}>"
            self._to_ph[key], self._to_val[ph] = ph, value
        return self._to_ph[key]

    def add_name(self, name: str | None) -> str | None:
        """Register a known name (e.g. the signed-in user) and return its placeholder."""
        name = (name or "").strip()
        return self._ph("NAME", name) if name else None

    def _known_names(self) -> list[tuple[str, str]]:
        """(text, placeholder) for every masked name and its separate words (any length, 2+ letters), longest first, so
        'Krishna' alone is caught later in the same request."""
        pairs: dict[str, str] = {}
        for ph, value in self._to_val.items():
            if ph.startswith("<NAME_"):
                for w in re.findall(r"[A-Za-z]{2,}", value):
                    if w.title() not in _NOT_NAMES:
                        pairs.setdefault(w, ph)
                pairs[value] = ph
        return sorted(pairs.items(), key=lambda kv: -len(kv[0]))

    def _sub(self, rx: re.Pattern, kind: str, group: int, text: str) -> str:
        def repl(m: re.Match) -> str:
            value = m.group(group)
            if _PLACEHOLDER.search(value):
                return m.group(0)
            if kind == "NAME" and value.split()[0].title() in _NOT_NAMES:
                return m.group(0)
            ph = self._ph(kind, value)
            return m.group(0)[: m.start(group) - m.start(0)] + ph if group else ph
        return rx.sub(repl, text)

    def mask(self, text: str) -> str:
        if not text:
            return text

        def labelled(m: re.Match) -> str:
            value = m.group(2)
            if _PLACEHOLDER.fullmatch(value.strip()) or value.strip().lower() in {"", "my", "the", "not", "unknown"}:
                return m.group(0)
            return m.group(0)[: m.start(2) - m.start(0)] + self._ph(_LABELS[m.group(1).lower()], value)

        # Emails first: a typo like "a@b,com" must not be cut at the comma by the label rule.
        kind, rx, group = next(r for r in _RULES if r[0] == "EMAIL")
        text = self._sub(rx, kind, group, text)
        text = _LABEL_RX.sub(labelled, text)
        for kind, rx, group in _RULES:
            text = self._sub(rx, kind, group, text)
        # Names seen earlier in this request (user's name, names found above) are masked wherever else they appear.
        # Placeholders are skipped, so an already-masked email or password is never rewritten.
        for name, _ in self._known_names():  # each variant gets its own placeholder, so it is restored as typed
            text = re.sub(r"(?<![\w<])" + re.escape(name) + r"(?![\w>])", lambda m: self._ph("NAME", m.group(0)), text, flags=re.I)
        return text

    def mask_history(self, history: list) -> list:
        return [dict(m, content=self.mask(m.get("content", ""))) if isinstance(m.get("content"), str) else m
                for m in history]

    def unmask(self, text: str) -> str:
        for _ in range(3):  # a value may itself hold a placeholder
            text = _PLACEHOLDER.sub(lambda m: self._to_val.get(m.group(0), m.group(0)), text) if text else text
        return text


# ------------------------------------------------------------------ privacy audit log
# Printed to the terminal whatever the logging setup is. It never prints an original value, only placeholders.
import contextvars
import json
import logging
import sys

audit_log = logging.getLogger("nia.privacy")
if not audit_log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[PRIVACY] %(message)s"))
    audit_log.addHandler(_h)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False

_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("nia_masker", default=None)
_USER_KEYS = ("question", "standalone_question", "user_name", "conversation")


def activate(masker: "Masker") -> None:
    _ACTIVE.set(masker)


def summary(masker: "Masker") -> str:
    counts = {k: n for k, n in masker._count.items() if n}
    return ", ".join(f"{n} {k}" for k, n in counts.items()) or "nothing personal found"


def _leaks(masker: "Masker", text: str) -> list[str]:
    """Placeholders whose original value still appears in `text` (the original is never returned)."""
    return [ph for ph, value in masker._to_val.items()
            if re.search(r"(?<![\w<])" + re.escape(value) + r"(?![\w>])", text, re.I)]


def audit(destination: str, payload) -> None:
    """Log what is about to go to OpenAI and check no masked original value is in it."""
    masker = _ACTIVE.get()
    if masker is None:
        return
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    checked = text
    try:  # answer payloads carry document text too: only the user-derived fields are checked
        body = json.loads(text.split("\n", 1)[1]) if text.startswith("Return JSON only.") else None
        if isinstance(body, dict):
            checked = json.dumps({k: body[k] for k in _USER_KEYS if k in body}, ensure_ascii=False)
            audit_log.info("-> %s | user fields sent: question=%r user_name=%r", destination,
                           body.get("question"), body.get("user_name"))
    except (ValueError, IndexError):
        pass
    leaked = _leaks(masker, checked)
    if leaked:
        audit_log.error("-> %s | LEAK: original value(s) for %s are in the outgoing text!", destination, ", ".join(leaked))
    else:
        audit_log.info("-> %s | OK: no original personal value in the outgoing text (%d protected)",
                       destination, len(masker._to_val))


def guard(client):
    """Wrap an OpenAI client so every responses.create / embeddings.create call is audited first."""
    for resource, method in (("responses", "create"), ("embeddings", "create")):
        target = getattr(client, resource, None)
        original = getattr(target, method, None)
        if original is None or getattr(original, "_audited", False):
            continue

        def wrapped(*a, _orig=original, _name=f"openai.{resource}.{method}", **kw):
            audit(_name, kw.get("input", ""))
            return _orig(*a, **kw)
        wrapped._audited = True
        setattr(target, method, wrapped)
    return client
