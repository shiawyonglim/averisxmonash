"""Honest scorer: submission.json vs the organizers' ground truth.

Mirrors the weighting published in sdoc-hackathon-bundle/README.md:
    final = 0.50 * end-to-end defect catch
          + 0.30 * stage-1 category macro-F1
          + 0.20 * stage-3 field-level defect F1
NEEDS_REVIEW handling is reported separately as a reliability axis, per the
organizers' note that it is not part of the weighted score.

Usage:
    python tests/score_against_ground_truth.py [submission.json] [ground_truth.json]

This file is measurement infrastructure: it must never be made aware of
which emails are edge cases, and must not be "tuned" to raise a number.
"""

import json
import os
import sys
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SUBMISSION = os.path.join(BASE_DIR, "submission.json")
DEFAULT_GROUND_TRUTH = os.path.join(
    BASE_DIR, "sdoc-hackathon-docker", "data_v2", "ground_truth.json"
)

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
REVIEW_REASONS = ["wrong_doc_type", "missing_attachment", "unreadable", "missing_value"]


def _f1(tp, fp, fn):
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def stage1_macro_f1(gt, sub):
    """Macro-F1 over the 5 categories."""
    per_class = {}
    for cat in CATEGORIES:
        tp = fp = fn = 0
        for eid, g in gt.items():
            p = (sub.get(eid) or {}).get("category")
            if g["category"] == cat and p == cat:
                tp += 1
            elif g["category"] != cat and p == cat:
                fp += 1
            elif g["category"] == cat and p != cat:
                fn += 1
        per_class[cat] = _f1(tp, fp, fn)
    macro = sum(v[2] for v in per_class.values()) / len(CATEGORIES)
    accuracy = sum(
        1 for eid, g in gt.items()
        if (sub.get(eid) or {}).get("category") == g["category"]
    ) / len(gt)
    return macro, accuracy, per_class


def stage3_field_defect_f1(gt, sub):
    """Field-level defect F1 over every (email, field) pair."""
    tp = fp = fn = 0
    for eid, g in gt.items():
        gold = set(g.get("defect_fields") or [])
        pred = set((sub.get(eid) or {}).get("defect_fields") or [])
        tp += len(gold & pred)
        fp += len(pred - gold)
        fn += len(gold - pred)
    return _f1(tp, fp, fn) + ((tp, fp, fn),)


def end_to_end_defect_catch(gt, sub):
    """A defect email counts as caught only if status==MISMATCH AND the
    reported defect_fields match gold exactly. Also reports the laxer
    'flagged at all' number so regressions are visible."""
    exact = flagged = total = 0
    misses = []
    for eid, g in gt.items():
        if g["status"] != "MISMATCH":
            continue
        total += 1
        p = sub.get(eid) or {}
        gold = set(g.get("defect_fields") or [])
        pred = set(p.get("defect_fields") or [])
        if p.get("status") == "MISMATCH":
            flagged += 1
            if gold == pred:
                exact += 1
                continue
        misses.append({
            "email_id": eid,
            "gold_status": g["status"], "pred_status": p.get("status"),
            "gold_fields": sorted(gold), "pred_fields": sorted(pred),
        })
    return {
        "total": total,
        "exact": exact,
        "flagged": flagged,
        "exact_rate": exact / total if total else 0.0,
        "flagged_rate": flagged / total if total else 0.0,
        "misses": misses,
    }


def false_alarms(gt, sub):
    """Gold-OK emails we wrongly reported as MISMATCH (the metric Averis
    cares about most: 'accuracy means ... without creating false alarms')."""
    out = []
    for eid, g in gt.items():
        p = sub.get(eid) or {}
        if g["status"] == "OK" and p.get("status") == "MISMATCH":
            out.append({"email_id": eid, "pred_fields": p.get("defect_fields")})
    return out


def reliability(gt, sub):
    """NEEDS_REVIEW axis: overall + per review_reason."""
    tp = fp = fn = 0
    for eid, g in gt.items():
        p = (sub.get(eid) or {}).get("status")
        if g["status"] == "NEEDS_REVIEW" and p == "NEEDS_REVIEW":
            tp += 1
        elif g["status"] != "NEEDS_REVIEW" and p == "NEEDS_REVIEW":
            fp += 1
        elif g["status"] == "NEEDS_REVIEW" and p != "NEEDS_REVIEW":
            fn += 1
    overall = _f1(tp, fp, fn)

    per_reason = {}
    for reason in REVIEW_REASONS:
        rtp = rfp = rfn = 0
        for eid, g in gt.items():
            pr = (sub.get(eid) or {}).get("review_reason")
            if g.get("review_reason") == reason and pr == reason:
                rtp += 1
            elif g.get("review_reason") != reason and pr == reason:
                rfp += 1
            elif g.get("review_reason") == reason and pr != reason:
                rfn += 1
        per_reason[reason] = (_f1(rtp, rfp, rfn), (rtp, rfp, rfn))
    return overall, (tp, fp, fn), per_reason


def main():
    sub_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SUBMISSION
    gt_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_GROUND_TRUTH

    with open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)
    with open(sub_path, "r", encoding="utf-8") as f:
        sub = json.load(f)

    missing = [eid for eid in gt if eid not in sub]
    extra = [eid for eid in sub if eid not in gt]

    macro, cat_acc, per_class = stage1_macro_f1(gt, sub)
    fprec, frec, ff1, fcounts = stage3_field_defect_f1(gt, sub)
    e2e = end_to_end_defect_catch(gt, sub)
    fa = false_alarms(gt, sub)
    (rprec, rrec, rf1), rcounts, per_reason = reliability(gt, sub)

    final = 0.50 * e2e["exact_rate"] + 0.30 * macro + 0.20 * ff1

    print("=" * 66)
    print(f"  SCORE vs GROUND TRUTH   ({os.path.basename(sub_path)})")
    print("=" * 66)
    print(f"  emails in ground truth : {len(gt)}")
    print(f"  emails in submission   : {len(sub)}"
          + (f"   [MISSING {len(missing)}]" if missing else "")
          + (f"   [EXTRA {len(extra)}]" if extra else ""))
    if missing:
        print(f"  !! missing ids (first 10): {missing[:10]}")

    print(f"\nFINAL WEIGHTED SCORE: {final * 100:.2f}%")
    print("  = 0.50 * end-to-end  + 0.30 * stage1_macro_F1 + 0.20 * field_defect_F1")
    print(f"  = 0.50 * {e2e['exact_rate']:.4f} + 0.30 * {macro:.4f} + 0.20 * {ff1:.4f}")

    print("\n-- STAGE 1: CATEGORY --")
    print(f"  accuracy  : {cat_acc * 100:.2f}%")
    print(f"  macro-F1  : {macro:.4f}")
    for cat, (p, r, f1) in per_class.items():
        print(f"    {cat:<16} P={p:.3f} R={r:.3f} F1={f1:.3f}")

    print("\n-- STAGE 3: FIELD-LEVEL DEFECTS --")
    print(f"  precision : {fprec:.4f}   recall: {frec:.4f}   F1: {ff1:.4f}")
    print(f"  tp={fcounts[0]} fp={fcounts[1]} fn={fcounts[2]}")

    print("\n-- END-TO-END DEFECT CATCH --")
    print(f"  exact  (status + exact field set): {e2e['exact']}/{e2e['total']}"
          f"  ({e2e['exact_rate'] * 100:.2f}%)")
    print(f"  flagged(status only)             : {e2e['flagged']}/{e2e['total']}"
          f"  ({e2e['flagged_rate'] * 100:.2f}%)")
    print(f"  FALSE ALARMS (gold OK -> MISMATCH): {len(fa)}")
    for row in fa[:10]:
        print(f"    {row['email_id']}  pred_fields={row['pred_fields']}")

    print("\n-- RELIABILITY: NEEDS_REVIEW (separate axis) --")
    print(f"  overall   P={rprec:.3f} R={rrec:.3f} F1={rf1:.4f}"
          f"  (tp={rcounts[0]} fp={rcounts[1]} fn={rcounts[2]})")
    for reason, ((p, r, f1), c) in per_reason.items():
        print(f"    {reason:<20} P={p:.3f} R={r:.3f} F1={f1:.3f}"
              f"  (tp={c[0]} fp={c[1]} fn={c[2]})")

    if e2e["misses"]:
        print(f"\n-- DEFECT MISSES ({len(e2e['misses'])}) --")
        for m in e2e["misses"][:20]:
            print(f"    {m['email_id']}: gold {m['gold_status']}{m['gold_fields']}"
                  f" -> pred {m['pred_status']}{m['pred_fields']}")

    # Every row where anything differs, for triage.
    diffs = []
    for eid, g in gt.items():
        p = sub.get(eid) or {}
        key_g = (g["category"], g["status"], g.get("review_reason"),
                 tuple(sorted(g.get("defect_fields") or [])))
        key_p = (p.get("category"), p.get("status"), p.get("review_reason"),
                 tuple(sorted(p.get("defect_fields") or [])))
        if key_g != key_p:
            diffs.append((eid, key_g, key_p))
    print(f"\n-- TOTAL ROWS DIFFERING FROM GROUND TRUTH: {len(diffs)} / {len(gt)} --")
    for eid, kg, kp in diffs[:30]:
        print(f"    {eid}: gold={kg}  pred={kp}")
    if len(diffs) > 30:
        print(f"    ... and {len(diffs) - 30} more")

    kinds = Counter((kg[1], kp[1]) for _, kg, kp in diffs)
    if kinds:
        print("\n  differing-row status transitions (gold -> pred):")
        for (a, b), n in kinds.most_common():
            print(f"    {a} -> {b}: {n}")

    return {
        "final": final, "stage1_macro_f1": macro, "category_accuracy": cat_acc,
        "field_defect_f1": ff1, "end_to_end": e2e, "false_alarms": len(fa),
        "reliability_f1": rf1, "rows_differing": len(diffs),
    }


if __name__ == "__main__":
    main()
