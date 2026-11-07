"""Deterministic answer presentation applied after the grounded answer is produced.

* GNSA wording for the investor's own registrar (conservative; titles and quotes untouched)
* removal of a bracketed form description in Signature mismatch numbered points
* inline form links from form_links.json (never model-written URLs)
* clean Sources list (no blank titles, no duplicates)
No form cards are produced anywhere.
"""
from __future__ import annotations

import re

import form_links

_DASHES = "‐‑‒–—―\\-"

# A parenthetical or [square] description directly after a form name. Markdown links "[x](url)"
# and source tags "[S1]" are excluded by the lookaheads.
_BRACKET_AFTER_FORM = re.compile(
    r"(?P<form>\b(?:ISR|SH)[\s" + _DASHES + r"]*\d+\b)\s*"
    r"(?:\((?![^)]*https?://)[^)\n]{3,200}\)|\[(?!S\d+\])[^\]\n]{3,200}\](?!\())"
)
_NUMBERED = re.compile(r"\s*\d+[.)]\s")


def strip_form_descriptions(text: str) -> str:
    """Drop the descriptive bracket after a form name, only inside numbered points."""
    lines = []
    for line in text.split("\n"):
        if _NUMBERED.match(line):
            line = _BRACKET_AFTER_FORM.sub(lambda m: m.group("form"), line)
        lines.append(line)
    return "\n".join(lines)


# "your/the/our RTA" -> GNSA. "RTAs" (plural/class), "Master Circular for RTAs" and lines containing
# quotation marks are left alone, so official titles and direct quotations are preserved.
_RTA_OWN = re.compile(r"(?<![\w\"'“])(?:your|the|our) RTA(?P<poss>'s|’s)?(?![\w\"'”])", re.I)


def gnsa_wording(text: str) -> str:
    def repl(m: re.Match) -> str:
        return "GNSA" + (m.group("poss") or "")

    out = []
    for line in text.split("\n"):
        quoted = line.count('"') >= 2 or "“" in line
        out.append(line if quoted else _RTA_OWN.sub(repl, line))
    return "\n".join(out)


def clean_sources(sources: list) -> list:
    seen, out = set(), []
    for src in sources or []:
        title, url = (src.get("title") or "").strip(), (src.get("url") or "").strip()
        if title and url and url.lower() not in seen:
            seen.add(url.lower())
            out.append({"title": title, "url": url})
    return out


def finish_text(text: str, profile: str = "general") -> str:
    if profile == "signature_mismatch":
        text = strip_form_descriptions(text)
    return form_links.linkify(gnsa_wording(text))
