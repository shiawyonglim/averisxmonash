"""Score the pipeline on the hand-written OOD set (tests/ood_dataset).

DETERMINISTIC ONLY: this script pins AI_FALLBACK=0 so it never touches the
API. The point is to measure what the rule tier alone generalizes to; the
model tier's job is to catch what this misses.

Run:  python tests/generate_ood_dataset.py   (once)
      python tests/eval_ood.py
"""

import os

os.environ.setdefault("AI_FALLBACK", "0")  # deterministic tier only — no API

import glob
import json
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.main import process_email

OOD = os.path.join(BASE, "ood_dataset")


def main():
    with open(os.path.join(OOD, "ood_expectations.json"), "r", encoding="utf-8") as f:
        expectations = json.load(f)

    rows = []
    met = 0
    for path in sorted(glob.glob(os.path.join(OOD, "inbox", "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            email = json.load(f)
        eid = email["email_id"]
        result, _, _ = process_email(email, bundle_dir=OOD)
        exp = expectations[eid]
        ok = True
        checks = []
        for key in ("category", "status", "review_reason"):
            if key in exp:
                want, got = exp[key], result.get(key)
                ok &= want == got
                checks.append(f"{key}: want {want} got {got}")
        if "defect_fields" in exp:
            want, got = set(exp["defect_fields"]), set(result.get("defect_fields") or [])
            ok &= want == got
            checks.append(f"defects: want {sorted(want)} got {sorted(got)}")
        met += int(ok)
        rows.append((eid, ok, "; ".join(checks)))

    print("=" * 66)
    print(f"  OOD GENERALIZATION TEST — {len(rows)} hand-written cases (AI off)")
    print("=" * 66)
    for eid, ok, summary in rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {eid}  {summary}")
    print("-" * 66)
    print(f"  {met}/{len(rows)} expectations met")
    print("  (FAIL rows are honest gaps — either fix tier-1, or they are")
    print("   exactly what the LLM tier exists to catch.)")


if __name__ == "__main__":
    main()
