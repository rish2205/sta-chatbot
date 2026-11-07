"""Deterministic form-name linking. URLs come only from form_links.json."""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH = Path(__file__).parent / "form_links.json"
_DASH = "\u2010\u2011\u2012\u2013\u2014\u2212\\-"  # hyphen last/escaped: safe inside a character class


def _load() -> dict[str, str]:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8")).get("forms", {})
    except (OSError, ValueError):
        return {}
    return {k.upper(): v for k, v in data.items() if str(v).startswith("https://")}


FORM_URLS = _load()


def _code_re(urls: dict) -> re.Pattern | None:
    parts = []
    for code in urls:
        prefix, _, num = code.partition("-")
        parts.append(re.escape(prefix) + rf"[\s{_DASH}]*" + re.escape(num))
    if not parts:
        return None
    # Not part of a longer token (ISR-10, xISR-1) and not followed by more digits.
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(parts) + r")(?![A-Za-z0-9])", re.I)


_RE = _code_re(FORM_URLS)
# Spans that must never be touched: existing markdown links (label and target) and bare URLs.
_PROTECTED = re.compile(r"\[[^\]]*\]\([^)\s]*\)|https?://[^\s)\]]+|<a\b.*?</a>", re.I | re.S)


def _canonical(raw: str) -> str:
    m = re.match(r"([A-Za-z]+)[\s%s]*(\d+)" % _DASH, raw)
    return f"{m.group(1).upper()}-{m.group(2)}"


def _load_tx() -> dict[str, str]:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8")).get("transmission", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if str(v).startswith("https://")}


TX_URLS = _load_tx()
# Transmission-specific names are linked only when the answer is about transmission, so Annexures
# in KYC, loss-of-share or other documents are never touched.
_TX_CONTEXT = re.compile(r"transmi|legal\s+heir|deceas|\bQTP\b|\bTX[\s%s]*\d|ISR[\s%s]*5\b|nominee" % (_DASH, _DASH), re.I)
_NUM = lambda n: rf"(?:Annex(?:ure)?\.?[\s{_DASH}]*{n}|TX[\s{_DASH}]*{n})(?![A-Za-z0-9])"
_PAIR = r"(?:\s*[/(,]\s*(?:or\s+)?%s\)?)?"
_TX_NUM = re.compile(r"(?<![A-Za-z0-9])(?:%s)" % "|".join(
    f"(?P<n{n}>{_NUM(n)}{_PAIR % _NUM(n)})" for n in (3, 4, 5)), re.I)
_TX_DIRECT = re.compile(
    rf"(?<![A-Za-z0-9])(?P<isr>ISR[\s{_DASH}]*5(?:\s*/\s*Annex(?:ure)?\.?[\s{_DASH}]*C(?![A-Za-z0-9]))?"
    rf"(?![A-Za-z0-9\-]))|(?<![A-Za-z0-9])(?P<ann>Annex(?:ure)?\.?[\s{_DASH}]*(?-i:[CDEF])(?![A-Za-z0-9]))", re.I)
_FIVE_DONE = re.compile(r"\s*[—–-]+\s*(?:\[)?Affidavit")


def _tx_link(label: str, key: str) -> str:
    return f"[{label}]({TX_URLS[key]})"


def _tx_numbered(n: int, shown: str, after: str) -> str:
    """Annexure 3/4 become the link itself; Annexure 5 gets two separate links (never one combined)."""
    if n == 3 and "ISR-5" in TX_URLS:
        return _tx_link(shown, "ISR-5")
    if n == 4 and "Annexure E" in TX_URLS:
        return _tx_link(shown, "Annexure E")
    if n == 5 and "Annexure D" in TX_URLS and "Annexure F" in TX_URLS and not _FIVE_DONE.match(after):
        return (f"{shown} — {_tx_link('Affidavit', 'Annexure D')} and {_tx_link('NOC', 'Annexure F')}")
    return shown


def _tx_segment(seg: str, rest: str) -> str:
    def num(m: re.Match) -> str:
        n = int(next(g for g in "345" if m.group(f"n{g}")))
        shown = "Annexure 5" if n == 5 else m.group(0)
        return _tx_numbered(n, shown, (seg[m.end():] + rest) if n == 5 else "")

    def direct(m: re.Match) -> str:
        if m.group("isr"):
            return _tx_link(m.group("isr"), "ISR-5") if "ISR-5" in TX_URLS else m.group(0)
        key = "Annexure " + m.group("ann")[-1].upper()
        return _tx_link(m.group("ann"), key) if key in TX_URLS else m.group(0)

    # Numbered annexures first (their suffix holds the only new links); then direct ISR-5 / C-F
    # mentions, skipping spans already inside links just created.
    seg = _TX_NUM.sub(num, seg)
    out, pos = [], 0
    for prot in _PROTECTED.finditer(seg):
        out.append(_TX_DIRECT.sub(direct, seg[pos:prot.start()]))
        out.append(prot.group(0))
        pos = prot.end()
    out.append(_TX_DIRECT.sub(direct, seg[pos:]))
    return "".join(out)


def _link_transmission(text: str) -> str:
    if not TX_URLS or not _TX_CONTEXT.search(text):
        return text
    out, pos = [], 0
    for prot in _PROTECTED.finditer(text):
        out.append(_tx_segment(text[pos:prot.start()], text[prot.start():prot.start() + 40]))
        out.append(prot.group(0))
        pos = prot.end()
    out.append(_tx_segment(text[pos:], ""))
    return "".join(out)


def linkify(text: str) -> str:
    """Wrap each mapped form name in a markdown link, once, outside existing links."""
    if not text:
        return text
    text = _link_transmission(text)
    if _RE is None:
        return text
    out, pos = [], 0
    for prot in _PROTECTED.finditer(text):
        out.append(_link_segment(text[pos:prot.start()]))
        out.append(prot.group(0))
        pos = prot.end()
    out.append(_link_segment(text[pos:]))
    return "".join(out)


def _link_segment(seg: str) -> str:
    def repl(m: re.Match) -> str:
        url = FORM_URLS.get(_canonical(m.group(1)))
        return f"[{m.group(1)}]({url})" if url else m.group(0)
    return _RE.sub(repl, seg)
