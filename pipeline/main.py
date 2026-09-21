import os
import json
import time
from tqdm import tqdm

from pipeline.parsers import extract_text, is_image_file, extract_shipping_fields_fast, extract_inline_si_fields
from pipeline.edge_cases import check_attachments, check_unreadable, check_wrong_doc_type, is_si_attachment
from pipeline.ai_engine import (
    classify_email_with_provenance,
    extract_fields_tiered,
    comparison_intent,
    analyze_document_image,
    CORE_FIELDS,
)
from pipeline.comparator import compare_fields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(BASE_DIR, "sdoc-hackathon-bundle")
INBOX_DIR = os.path.join(BUNDLE_DIR, "inbox")
SUBMISSION_PATH = os.path.join(BASE_DIR, "submission.json")
DETAILS_PATH = os.path.join(BASE_DIR, "verification_details.json")

SUBMISSION_KEYS = ("category", "status", "review_reason", "has_defect", "defect_fields")


def submission_row(result):
    """Reduce a process_email result to exactly the 5 organizer-scored keys."""
    return {k: result.get(k) for k in SUBMISSION_KEYS}


def _verdict(category, status, review_reason, has_defect, defect_fields, details):
    return {
        "category": category,
        "status": status,
        "review_reason": review_reason,
        "has_defect": has_defect,
        "defect_fields": defect_fields,
        "details": details,
    }


def _evidence_from_comparisons(field_comparisons, si_fields, bl_fields, si_prov, bl_prov):
    evidence = []
    for field, comp in (field_comparisons or {}).items():
        if comp.get("match"):
            continue
        entry = {
            "field": field,
            "si_value": si_fields.get(field),
            "bl_value": bl_fields.get(field),
            "provenance": {"si": si_prov.get(field), "bl": bl_prov.get(field)},
            "reason": comp.get("reason"),
        }
        if comp.get("confidence"):
            entry["confidence"] = comp["confidence"]
        evidence.append(entry)
    return evidence


def process_email(email_data, bundle_dir=BUNDLE_DIR):
    eid = email_data["email_id"]
    subj = email_data.get("subject", "")
    body = email_data.get("body", "")
    atts = email_data.get("attachments", [])
    has_atts = len(atts) > 0

    details = {
        "classification_provenance": None,
        "intent": None,
        "intent_provenance": None,
        "si_provenance": {},
        "bl_provenance": {},
        "si_fields": {},
        "bl_fields": {},
        "field_comparisons": {},
        "attachment_paths": list(atts),
        "evidence": [],
    }

    # 1. Classify Email
    category, cls_prov = classify_email_with_provenance(
        subj, body, has_atts, sender=email_data.get("from", ""))
    details["classification_provenance"] = cls_prov

    if category != "BL_COMPARISON":
        return _verdict(category, "OK", None, False, [], details), {}, {}

    # 2. Fewer than 2 attachments: decide by sender intent whether a
    #    comparison was actually requested (nothing to compare otherwise).
    if len(atts) < 2:
        # An SI written into the email body is still real SI content —
        # record it in the audit trail even though no comparison can run.
        inline_si = extract_inline_si_fields(body)
        if inline_si:
            details["si_fields"] = inline_si
            details["si_provenance"] = {k: "email_body" for k in CORE_FIELDS}
            details["si_source"] = "email_body"
        intent, intent_prov = comparison_intent(subj, body, sender=email_data.get("from", ""))
        details["intent"] = intent
        details["intent_provenance"] = intent_prov
        att_err = check_attachments(email_data, bundle_dir, intent)
        if att_err:
            return _verdict(category, att_err[0], att_err[1], False, [], details), {}, {}
        return _verdict(category, "OK", None, False, [], details), {}, {}

    read_err = check_unreadable(email_data, bundle_dir)
    if read_err:
        return _verdict(category, read_err[0], read_err[1], False, [], details), {}, {}

    # Read documents — text parsers for office files, a single vision pass
    # for camera photos/scans of paper documents (incl. handwritten bills).
    path1 = os.path.join(bundle_dir, atts[0])
    path2 = os.path.join(bundle_dir, atts[1])

    docs = []
    for p in (path1, path2):
        if is_image_file(p):
            fields, meta = analyze_document_image(p)
            docs.append({"path": p, "fields": fields, "meta": meta,
                         "text": meta.get("transcription", "")})
        else:
            docs.append({"path": p, "fields": None, "meta": {},
                         "text": extract_text(p)})

    # An illegible photo escalates the same way as an unreadable file
    if any(d["meta"].get("legibility") == "ILLEGIBLE" for d in docs):
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), {}, {}

    # Vision-reported doc types catch paper wrong-docs (e.g. a photographed
    # commercial invoice) that the text keyword check cannot see.
    KNOWN_PAPER_TYPES = (
        "SHIPPING_INSTRUCTION", "BILL_OF_LADING", "DRAFT_BILL_OF_LADING",
        "SI", "BL", "OTHER", "UNKNOWN"
    )
    if any((d["meta"].get("document_type") or "").upper().replace(" ", "_") not in KNOWN_PAPER_TYPES
           and d["meta"].get("document_type") for d in docs):
        return _verdict(category, "NEEDS_REVIEW", "wrong_doc_type", False, [], details), {}, {}

    # Check Wrong Doc Type
    wrong_doc_err = check_wrong_doc_type(docs[0]["text"], docs[1]["text"])
    if wrong_doc_err:
        return _verdict(category, wrong_doc_err[0], wrong_doc_err[1], False, [], details), {}, {}

    # 3. Determine which is SI and which is BL (filename-first detection;
    #    for photos the vision-reported document_type acts as the content marker)
    si_key = docs[0]["meta"].get("document_type") or docs[0]["text"]
    if is_si_attachment(path1, si_key):
        si_doc, bl_doc = docs[0], docs[1]
    else:
        si_doc, bl_doc = docs[1], docs[0]

    # 4. Extract Fields — tiered regex->LLM for text docs; vision fields for photos
    def _extract(doc):
        if doc.get("fields"):
            return doc["fields"], {k: "vision" for k in CORE_FIELDS}
        return extract_fields_tiered(doc["text"])

    si_fields, si_prov = _extract(si_doc)
    bl_fields, bl_prov = _extract(bl_doc)
    details["si_provenance"] = si_prov
    details["bl_provenance"] = bl_prov
    details["si_fields"] = si_fields
    details["bl_fields"] = bl_fields

    # Extraction failures return "ERROR" placeholders - never treat as a match
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), si_fields, bl_fields

    # 5. Compare Fields
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)
    details["field_comparisons"] = field_comparisons
    details["evidence"] = _evidence_from_comparisons(
        field_comparisons, si_fields, bl_fields, si_prov, bl_prov)

    # Precedence: a genuine value-vs-value defect is never swallowed by an
    # escalation; blanks (one-sided or both) go to details/evidence.
    if defect_fields:
        return _verdict(category, "MISMATCH", None, True, defect_fields, details), si_fields, bl_fields

    if is_missing_val:
        return _verdict(category, "NEEDS_REVIEW", "missing_value", False, [], details), si_fields, bl_fields

    # Fields blank in BOTH documents mean we could not read them — we are
    # not entitled to report OK.
    both_blank = [f for f, c in field_comparisons.items() if c.get("blank") == "both"]
    if both_blank:
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), si_fields, bl_fields

    return _verdict(category, "OK", None, False, [], details), si_fields, bl_fields


def main():
    email_files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    submission = {}
    details_out = {}

    print(f"Processing {len(email_files)} emails...")

    for f in tqdm(email_files):
        path = os.path.join(INBOX_DIR, f)
        with open(path, 'r', encoding='utf-8') as file:
            email_data = json.load(file)

        eid = email_data["email_id"]
        result, si_fields, bl_fields = process_email(email_data)
        details_out[eid] = result.pop("details", {})
        submission[eid] = submission_row(result)
        print(f"\n{eid} Result: {result['status']}")

    with open(SUBMISSION_PATH, "w") as f:
        json.dump(submission, f, indent=2)

    with open(DETAILS_PATH, "w") as f:
        json.dump(details_out, f, indent=2)

    print("Saved submission.json and verification_details.json")

if __name__ == "__main__":
    main()
