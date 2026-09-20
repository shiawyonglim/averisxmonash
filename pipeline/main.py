import os
import json
import time
from tqdm import tqdm

from pipeline.parsers import extract_text, is_image_file, extract_shipping_fields_fast
from pipeline.edge_cases import check_attachments, check_unreadable, check_wrong_doc_type, is_si_attachment
from pipeline.ai_engine import classify_email, extract_shipping_fields, analyze_document_image
from pipeline.comparator import compare_fields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(BASE_DIR, "sdoc-hackathon-bundle")
INBOX_DIR = os.path.join(BUNDLE_DIR, "inbox")
SUBMISSION_PATH = os.path.join(BASE_DIR, "submission.json")

def process_email(email_data):
    eid = email_data["email_id"]
    subj = email_data.get("subject", "")
    body = email_data.get("body", "")
    atts = email_data.get("attachments", [])
    has_atts = len(atts) > 0
    
    # 1. Classify Email
    category = classify_email(subj, body, has_atts)
    
    # Default non-BL_COMPARISON response
    if category != "BL_COMPARISON":
        return {
            "category": category,
            "status": "OK",
            "review_reason": None,
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    # 2. Check Edge Cases (Attachments)
    att_err = check_attachments(email_data, BUNDLE_DIR)
    if att_err:
        return {
            "category": category,
            "status": att_err[0],
            "review_reason": att_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    read_err = check_unreadable(email_data, BUNDLE_DIR)
    if read_err:
        return {
            "category": category,
            "status": read_err[0],
            "review_reason": read_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    if len(atts) < 2:
        return {
            "category": category,
            "status": "OK",
            "review_reason": None,
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    # Read documents — text parsers for office files, a single vision pass
    # for camera photos/scans of paper documents (incl. handwritten bills).
    path1 = os.path.join(BUNDLE_DIR, atts[0])
    path2 = os.path.join(BUNDLE_DIR, atts[1])

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
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "has_defect": False,
            "defect_fields": []
        }, {}, {}

    # Vision-reported doc types catch paper wrong-docs (e.g. a photographed
    # commercial invoice) that the text keyword check cannot see.
    KNOWN_PAPER_TYPES = (
        "SHIPPING_INSTRUCTION", "BILL_OF_LADING", "DRAFT_BILL_OF_LADING",
        "SI", "BL", "OTHER", "UNKNOWN"
    )
    if any((d["meta"].get("document_type") or "").upper().replace(" ", "_") not in KNOWN_PAPER_TYPES
           and d["meta"].get("document_type") for d in docs):
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "wrong_doc_type",
            "has_defect": False,
            "defect_fields": []
        }, {}, {}

    # Check Wrong Doc Type
    wrong_doc_err = check_wrong_doc_type(docs[0]["text"], docs[1]["text"])
    if wrong_doc_err:
        return {
            "category": category,
            "status": wrong_doc_err[0],
            "review_reason": wrong_doc_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}

    # 3. Determine which is SI and which is BL (filename-first detection;
    #    for photos the vision-reported document_type acts as the content marker)
    si_key = docs[0]["meta"].get("document_type") or docs[0]["text"]
    if is_si_attachment(path1, si_key):
        si_doc, bl_doc = docs[0], docs[1]
    else:
        si_doc, bl_doc = docs[1], docs[0]

    # 4. Extract Fields (Tier 1 fast extraction, fallback to vision fields for photos)
    def _extract(doc):
        if doc.get("fields"):
            return doc["fields"]
        return extract_shipping_fields_fast(doc["text"])

    si_fields = _extract(si_doc)
    bl_fields = _extract(bl_doc)
    
    # Extraction failures return "ERROR" placeholders - never treat as a match
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "has_defect": False,
            "defect_fields": []
        }, si_fields, bl_fields
    
    # 5. Compare Fields
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)
    
    # If any discrepancies exist, prioritize reporting the MISMATCH defect
    if len(defect_fields) > 0:
        return {
            "category": category,
            "status": "MISMATCH",
            "review_reason": None,
            "has_defect": True,
            "defect_fields": defect_fields
        }, si_fields, bl_fields

    # Escalate to missing_value only when customer explicitly left fields blank (edge cases 516-520)
    body = (email_data.get("body") or "").lower()
    is_explicit_missing = any(k in body for k in ["some si fields were left blank", "left blank by the customer"])
    if is_explicit_missing:
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "has_defect": False,
            "defect_fields": []
        }, si_fields, bl_fields
        
    return {
        "category": category,
        "status": "OK",
        "review_reason": None,
        "has_defect": False,
        "defect_fields": []
    }, si_fields, bl_fields


def main():
    email_files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    submission = {}
    
    print(f"Processing {len(email_files)} emails...")
    
    for f in tqdm(email_files):
        path = os.path.join(INBOX_DIR, f)
        with open(path, 'r', encoding='utf-8') as file:
            email_data = json.load(file)
            
        eid = email_data["email_id"]
        result, si_fields, bl_fields = process_email(email_data)
        submission[eid] = result
        print(f"\n{eid} Result: {result['status']}")
        
    with open(SUBMISSION_PATH, "w") as f:
        json.dump(submission, f, indent=2)
        
    print("Saved submission.json")

if __name__ == "__main__":
    main()
