"""Run the question set through the bot and score it.

Usage:
    python eval.py                 # run everything
    python eval.py --limit 10      # quick smoke test
    python eval.py --topic kyc     # one topic only

Automatic checks (cheap, but NOT proof of correctness):
  in_scope : bot answered, cited at least one official source, contains expected keywords
  refuse   : bot refused (no answer was generated)

The CSV has an empty `human_verdict` column. Fill it with CORRECT / WRONG / OUTDATED
after reading each answer against the official source; that is your real accuracy number.
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).parent
secrets = BASE / ".streamlit" / "secrets.toml"
if not os.getenv("OPENAI_API_KEY") and secrets.exists():
    import tomllib

    os.environ.update({k: str(v) for k, v in tomllib.loads(secrets.read_text()).items()})

import sta_bot  # noqa: E402


def auto_check(item: dict, reply: sta_bot.BotReply) -> tuple[bool, str]:
    if item["type"] == "refuse":
        ok = reply.kind in ("refusal", "greeting")
        return ok, "" if ok else f"expected refusal, got {reply.kind}"
    if reply.kind != "answer":
        return False, f"expected answer, got {reply.kind}"
    if not reply.sources:
        return False, "no official source cited"
    missing = [k for k in item.get("kw", []) if k.lower() not in reply.text.lower()]
    if missing:
        return False, "missing keywords: " + ", ".join(missing)
    shown = {f["code"] for f in reply.forms}
    if "expected_forms" in item and shown != set(item["expected_forms"]):
        return False, "form selection differs from reviewed expectation"
    forbidden = [word for word in item.get("forbidden_claims", []) if word.lower() in reply.text.lower()]
    if forbidden:
        return False, "contains prohibited claim: " + ", ".join(forbidden)
    return True, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--topic")
    ap.add_argument("--out", default="eval_report.csv")
    args = ap.parse_args()

    items = json.loads((BASE / "eval_set.json").read_text(encoding="utf-8"))
    if args.topic:
        items = [i for i in items if i["topic"] == args.topic]
    if args.limit:
        items = items[: args.limit]

    rows, passed = [], {"in_scope": [0, 0], "refuse": [0, 0]}
    for n, item in enumerate(items, 1):
        reply = sta_bot.answer(item["q"], [])
        ok, why = auto_check(item, reply)
        passed[item["type"]][1] += 1
        passed[item["type"]][0] += ok
        print(f"[{n}/{len(items)}] {'PASS' if ok else 'FAIL'} {item['id']} {why}")
        rows.append(
            {
                "id": item["id"],
                "topic": item["topic"],
                "type": item["type"],
                "question": item["q"],
                "bot_kind": reply.kind,
                "auto_pass": ok,
                "auto_note": why,
                "answer": reply.text,
                "sources": " | ".join(s["url"] for s in reply.sources),
                "forms_shown": ", ".join(f["code"] for f in reply.forms),
                "human_verdict": "",
                "reviewer_notes": "",
            }
        )

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\nAutomatic results (human review still required):")
    for kind, (ok, total) in passed.items():
        if total:
            print(f"  {kind:9s} {ok}/{total}")
    print(f"Report written to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
