"""Check that every URL in forms.json and verified_kb.json still resolves.

Usage: python check_forms.py
Some official sites block scripts, so a failure means "check by hand", not "broken".
"""
import json
from pathlib import Path

import requests

BASE = Path(__file__).parent
HEADERS = {"User-Agent": "Mozilla/5.0 (link-check)"}


def urls():
    for f in json.loads((BASE / "forms.json").read_text(encoding="utf-8")):
        if f.get("url"):
            yield f"form {f['code']}", f["url"]
    for e in json.loads((BASE / "verified_kb.json").read_text(encoding="utf-8")):
        for s in e.get("sources", []):
            yield f"kb {e['id']}", s["url"]


for label, url in urls():
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        print(f"{r.status_code}  {label}  {url}")
    except requests.RequestException as exc:
        print(f"ERR  {label}  {url}  ({type(exc).__name__})")
