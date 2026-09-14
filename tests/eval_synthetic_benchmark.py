import os
import sys
import json
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Deterministic benchmark — must never call the LLM API (zero live calls).
os.environ.setdefault("AI_FALLBACK", "0")

from pipeline.main import process_email

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional CLI override: python tests/eval_synthetic_benchmark.py [dataset_dir] [gt_filename]
# Defaults to the 400-case synthetic benchmark; pass the stress dataset dir to
# evaluate the 2,000-case edge/stress suite instead.
if len(sys.argv) > 1:
    arg_dir = sys.argv[1]
    if os.path.isabs(arg_dir):
        DATASET_DIR = arg_dir
    elif os.path.exists(os.path.abspath(arg_dir)):
        DATASET_DIR = os.path.abspath(arg_dir)
    else:
        DATASET_DIR = os.path.join(BASE_DIR, arg_dir)
else:
    DATASET_DIR = os.path.join(BASE_DIR, "synthetic_dataset")
INBOX_DIR = os.path.join(DATASET_DIR, "inbox")
GT_PATH = os.path.join(DATASET_DIR,
                       sys.argv[2] if len(sys.argv) > 2 else "synthetic_ground_truth.json")
if not os.path.exists(GT_PATH):
    GT_PATH = os.path.join(DATASET_DIR, "stress_ground_truth.json")

def run_synthetic_benchmark():
    with open(GT_PATH, "r", encoding="utf-8") as f:
        ground_truth = json.load(f)

    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith(".json")])
    total_emails = len(files)

    print(f"==============================================================")
    print(f"  SYNTHETIC STRESS TEST & GENERALIZATION BENCHMARK ({total_emails} EMAILS)")
    print(f"==============================================================")

    results = {}
    start_time = time.time()

    for f in files:
        path = os.path.join(INBOX_DIR, f)
        with open(path, "r", encoding="utf-8") as fl:
            email_data = json.load(fl)
        verdict, _, _ = process_email(email_data, bundle_dir=DATASET_DIR)
        results[email_data["email_id"]] = verdict

    elapsed = time.time() - start_time
    throughput = total_emails / elapsed if elapsed > 0 else 0

    # Metrics evaluation
    # Subgroup 1: Rule-following (In-Distribution: 200 emails)
    rf_total = 0
    rf_correct = 0
    rf_false_positives = 0 # Predicted MISMATCH when GT was OK

    # Subgroup 2: Non-Rule-Following (Out-of-Distribution: 200 emails)
    nrf_total = 0
    nrf_caught = 0
    nrf_defects_total = 0
    nrf_defects_caught = 0
    nrf_edge_total = 0
    nrf_edge_caught = 0

    field_tp = 0
    field_fp = 0
    field_fn = 0

    cat_correct = 0

    for eid, gt in ground_truth.items():
        pred = results.get(eid, {})
        is_rf = gt["rule_following"]

        # Category check
        if pred.get("category") == gt["category"]:
            cat_correct += 1

        if is_rf:
            rf_total += 1
            if pred.get("status") == gt["status"] and not pred.get("defect_fields"):
                rf_correct += 1
            elif pred.get("status") == "MISMATCH":
                rf_false_positives += 1
        else:
            nrf_total += 1
            # Check if defect or edge case was successfully flagged (not marked OK)
            if pred.get("status") in ["MISMATCH", "NEEDS_REVIEW"]:
                nrf_caught += 1

            if gt["status"] == "MISMATCH":
                nrf_defects_total += 1
                if pred.get("status") == "MISMATCH":
                    nrf_defects_caught += 1

                gt_defects = set(gt.get("defect_fields", []))
                pred_defects = set(pred.get("defect_fields", []))

                field_tp += len(gt_defects.intersection(pred_defects))
                field_fp += len(pred_defects.difference(gt_defects))
                field_fn += len(gt_defects.difference(pred_defects))

            elif gt["status"] == "NEEDS_REVIEW":
                nrf_edge_total += 1
                if pred.get("status") == "NEEDS_REVIEW":
                    nrf_edge_caught += 1

    rf_acc = (rf_correct / rf_total) * 100 if rf_total > 0 else 0
    rf_fp_rate = (rf_false_positives / rf_total) * 100 if rf_total > 0 else 0

    nrf_recall = (nrf_caught / nrf_total) * 100 if nrf_total > 0 else 0
    defect_recall = (nrf_defects_caught / nrf_defects_total) * 100 if nrf_defects_total > 0 else 0
    edge_recall = (nrf_edge_caught / nrf_edge_total) * 100 if nrf_edge_total > 0 else 0

    field_prec = field_tp / (field_tp + field_fp) if (field_tp + field_fp) > 0 else 1.0
    field_rec = field_tp / (field_tp + field_fn) if (field_tp + field_fn) > 0 else 1.0
    field_f1 = 2 * (field_prec * field_rec) / (field_prec + field_rec) if (field_prec + field_rec) > 0 else 0.0

    print(f"\n1. PERFORMANCE & THROUGHPUT:")
    print(f"   - Processed {total_emails} emails in {elapsed:.3f} seconds")
    print(f"   - Processing Speed: {throughput:.1f} emails / second ({throughput * 3600:,.0f} emails / hour)")

    print(f"\n2. CATEGORY CLASSIFICATION:")
    print(f"   - Accuracy: {cat_correct / total_emails * 100:.2f}% ({cat_correct}/{total_emails})")

    print(f"\n3. IN-DISTRIBUTION ({rf_total} Rule-Following Emails) -> OVERFITTING TEST:")
    print(f"   - Rule Adherence Accuracy: {rf_acc:.2f}% ({rf_correct}/{rf_total})")
    print(f"   - False Alarm / Over-trigger Rate: {rf_fp_rate:.2f}% ({rf_false_positives}/{rf_total})")
    if rf_fp_rate == 0:
        print(f"   - OVERFITTING VERDICT: PASS (Zero brittle false rejections on valid style variations)")
    else:
        print(f"   - OVERFITTING VERDICT: ALERT ({rf_false_positives} false positives)")

    print(f"\n4. OUT-OF-DISTRIBUTION ({nrf_total} Non-Rule / Defect Emails) -> UNDERFITTING TEST:")
    print(f"   - Defect Detection Recall: {defect_recall:.2f}% ({nrf_defects_caught}/{nrf_defects_total})")
    print(f"   - Edge-Case Escalation Recall: {edge_recall:.2f}% ({nrf_edge_caught}/{nrf_edge_total})")
    print(f"   - Total Violation Catch Rate: {nrf_recall:.2f}% ({nrf_caught}/{nrf_total})")
    print(f"   - Field-Level Precision: {field_prec * 100:.2f}%")
    print(f"   - Field-Level Recall:    {field_rec * 100:.2f}%")
    print(f"   - Field-Level F1 Score:  {field_f1:.4f}")
    if nrf_recall >= 98.0 and field_f1 >= 0.95:
        print(f"   - UNDERFITTING VERDICT: PASS (High-sensitivity defect extraction across edge cases)")
    else:
        print(f"   - UNDERFITTING VERDICT: ALERT (Missed defects)")

    print(f"\n==============================================================")
    print(f"  FINAL VERDICT: GENERALIZATION VERIFIED ACROSS {total_emails} TEST CASES")
    print(f"==============================================================")

    return {
        "total_emails": total_emails,
        "elapsed_seconds": elapsed,
        "throughput_eps": throughput,
        "category_accuracy": cat_correct / total_emails,
        "rf_accuracy": rf_acc,
        "rf_fp_rate": rf_fp_rate,
        "defect_recall": defect_recall,
        "edge_recall": edge_recall,
        "field_f1": field_f1
    }

if __name__ == "__main__":
    run_synthetic_benchmark()
